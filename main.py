#!/usr/bin/env python3
"""
Gestão simples para um negócio de doces (ou qualquer pequeno negócio).

Registra clientes, produtos, vendas/encomendas e gastos, e calcula o lucro.
Os dados ficam em um arquivo SQLite (negocio.db) na mesma pasta do programa.
Só usa a biblioteca padrão do Python (3.8+). Para rodar:  python gestao_doceria.py

COMO O LUCRO É CALCULADO
  Receita          = soma de (quantidade x preço de venda) das vendas não canceladas
  Custo dos produtos = soma de (quantidade x custo unitário) -> custo da receita de cada doce
  Lucro bruto      = Receita - Custo dos produtos
  Gastos operacionais = gastos registrados, EXCETO a categoria "Insumos"
                        (insumos já entram no custo unitário do produto; contar de novo
                        seria contar em dobro)
  Lucro líquido    = Lucro bruto - Gastos operacionais
  Perdas de estoque = ingredientes jogados fora/estragados (custo real dos lotes)
  Lucro líquido    = Lucro bruto - Gastos operacionais - Perdas de estoque
  Caixa do mês     = valor efetivamente RECEBIDO - TODOS os gastos pagos (inclui insumos)

ESTOQUE E RECEITAS
  Cada compra de ingrediente vira um LOTE (quantidade + preço pago) e já lança o gasto
  na categoria "Insumos". O PREÇO ESPERADO de um ingrediente é a média ponderada do que
  ainda existe em estoque (sem estoque: preço da última compra). O custo estimado de uma
  receita usa esses preços. Ao registrar uma produção, os ingredientes saem do estoque
  pelo lote mais antigo (FIFO) e o custo REAL da produção é calculado.
"""

import math
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

DB = Path(os.environ.get("NEGOCIO_DB") or Path(__file__).with_name("negocio.db"))

CAT_INSUMOS = "Insumos"
CATEGORIAS_GASTO = [CAT_INSUMOS, "Embalagem", "Gás/Energia", "Marketing",
                    "Transporte", "Equipamentos", "Outros"]
ORIGENS = ["Instagram", "Indicação", "WhatsApp", "Presencial", "Outra"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL,
    telefone TEXT,
    origem TEXT,
    observacoes TEXT,
    criado_em TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS produtos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE,
    custo_unitario REAL NOT NULL CHECK (custo_unitario >= 0),
    preco_venda REAL NOT NULL CHECK (preco_venda >= 0),
    ativo INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS vendas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_id INTEGER REFERENCES clientes(id),
    data TEXT NOT NULL,
    tipo TEXT NOT NULL,              -- 'Pronta entrega' ou 'Encomenda'
    data_entrega TEXT,
    pago INTEGER NOT NULL DEFAULT 0,
    entregue INTEGER NOT NULL DEFAULT 0,
    cancelada INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS itens_venda (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venda_id INTEGER NOT NULL REFERENCES vendas(id),
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    quantidade INTEGER NOT NULL CHECK (quantidade > 0),
    preco_unitario REAL NOT NULL,    -- preço no dia da venda (não muda depois)
    custo_unitario REAL NOT NULL     -- custo no dia da venda (não muda depois)
);
CREATE TABLE IF NOT EXISTS gastos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    categoria TEXT NOT NULL,
    descricao TEXT NOT NULL,
    valor REAL NOT NULL CHECK (valor >= 0)
);
CREATE TABLE IF NOT EXISTS ingredientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nome TEXT NOT NULL UNIQUE COLLATE NOCASE,
    unidade TEXT NOT NULL CHECK (unidade IN ('g', 'ml', 'un')),
    estoque_minimo REAL NOT NULL DEFAULT 0,
    tamanho_embalagem REAL            -- última embalagem comprada (para sugerir e arredondar)
);
CREATE TABLE IF NOT EXISTS lotes (    -- cada compra é um lote
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingrediente_id INTEGER NOT NULL REFERENCES ingredientes(id),
    data TEXT NOT NULL,
    local TEXT,
    quantidade REAL NOT NULL CHECK (quantidade > 0),
    restante REAL NOT NULL CHECK (restante >= 0),
    preco_total REAL NOT NULL CHECK (preco_total > 0),
    custo_unitario REAL NOT NULL,     -- preço por g / ml / unidade
    gasto_id INTEGER REFERENCES gastos(id)
);
CREATE TABLE IF NOT EXISTS receitas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    produto_id INTEGER NOT NULL UNIQUE REFERENCES produtos(id),
    rendimento INTEGER NOT NULL CHECK (rendimento > 0)   -- unidades que a receita rende
);
CREATE TABLE IF NOT EXISTS itens_receita (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receita_id INTEGER NOT NULL REFERENCES receitas(id) ON DELETE CASCADE,
    ingrediente_id INTEGER NOT NULL REFERENCES ingredientes(id),
    quantidade REAL NOT NULL CHECK (quantidade > 0),     -- na unidade do ingrediente
    UNIQUE (receita_id, ingrediente_id)
);
CREATE TABLE IF NOT EXISTS producoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    produto_id INTEGER NOT NULL REFERENCES produtos(id),
    unidades INTEGER NOT NULL CHECK (unidades > 0),
    custo_total REAL NOT NULL         -- custo real, pelos lotes consumidos
);
CREATE TABLE IF NOT EXISTS perdas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    ingrediente_id INTEGER NOT NULL REFERENCES ingredientes(id),
    quantidade REAL NOT NULL,
    valor REAL NOT NULL,
    motivo TEXT
);
"""

EPS = 1e-6                       # tolerância para comparar quantidades
UNIDADES = ["g", "ml", "un"]
FATORES = {("kg", "g"): 1000, ("l", "ml"): 1000, ("g", "g"): 1, ("ml", "ml"): 1, ("un", "un"): 1}
EXEMPLO_QTD = {"g": "250g ou 1,5kg", "ml": "500ml ou 1l", "un": "12"}

TOTAL_VENDA = "COALESCE(SUM(i.quantidade * i.preco_unitario), 0)"


# ----------------------------------------------------------------------------
# Utilitários de entrada e saída
# ----------------------------------------------------------------------------
def brl(valor):
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_data(iso):
    if not iso:
        return "-"
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def ler_texto(msg, obrigatorio=True):
    while True:
        valor = input(msg).strip()
        if valor or not obrigatorio:
            return valor
        print("  Este campo é obrigatório.")


def parse_numero(bruto):
    """'1.250,50' -> 1250.5 ; '12,5' -> 12.5 ; '7' -> 7.0 (ValueError se inválido)."""
    bruto = bruto.strip().replace("R$", "").replace(" ", "")
    if "," in bruto:
        bruto = bruto.replace(".", "").replace(",", ".")
    return float(bruto)


def ler_valor(msg, padrao=None):
    while True:
        bruto = input(msg).strip()
        if not bruto and padrao is not None:
            return padrao
        try:
            valor = parse_numero(bruto)
            if valor < 0 or not math.isfinite(valor):
                raise ValueError
            return round(valor, 2)
        except ValueError:
            print("  Valor inválido. Exemplo: 12,50")


def ler_int(msg, minimo=1, padrao=None):
    while True:
        bruto = input(msg).strip()
        if not bruto and padrao is not None:
            return padrao
        try:
            valor = int(bruto)
            if valor < minimo:
                raise ValueError
            return valor
        except ValueError:
            print(f"  Digite um número inteiro (mínimo {minimo}).")


def num(valor, casas=3):
    """Número no formato brasileiro, sem zeros sobrando: 1500.0 -> '1500', 0.25 -> '0,25'."""
    texto = f"{valor:.{casas}f}".rstrip("0").rstrip(".")
    return (texto or "0").replace(".", ",")


def fmt_qtd(q, un):
    if un == "g" and abs(q) >= 1000:
        return f"{num(q / 1000)} kg"
    if un == "ml" and abs(q) >= 1000:
        return f"{num(q / 1000)} L"
    return f"{num(q)} {un}"


def fmt_preco_un(custo, un):
    """Preço por unidade de medida, em escala amigável (R$/kg, R$/L, R$/un)."""
    if un == "g":
        return f"{brl(custo * 1000)}/kg"
    if un == "ml":
        return f"{brl(custo * 1000)}/L"
    return f"{brl(custo)}/un"


def ler_quantidade(msg, unidade, padrao=None, permitir_zero=False):
    """Lê '250g', '1,5kg', '500ml', '2l' ou só o número, e devolve na unidade base."""
    while True:
        bruto = input(msg).strip().lower().replace(" ", "")
        if not bruto and padrao is not None:
            return padrao
        m = re.fullmatch(r"([\d.,]+)(kg|g|ml|l|un)?", bruto)
        valor = None
        if m:
            try:
                valor = parse_numero(m.group(1))
            except ValueError:
                valor = None
        if valor is None:
            print(f"  Quantidade inválida. Exemplo: {EXEMPLO_QTD[unidade]}")
            continue
        sufixo = m.group(2)
        fator = 1 if sufixo is None else FATORES.get((sufixo, unidade))
        if fator is None:
            print(f"  '{sufixo}' não serve para um ingrediente medido em {unidade}.")
        elif valor < 0 or (valor == 0 and not permitir_zero):
            print("  A quantidade deve ser maior que zero.")
        else:
            return round(valor * fator, 6)


def ler_data(msg, permitir_vazio=False):
    """Aceita dd/mm/aaaa. Enter = hoje (ou vazio, se permitir_vazio)."""
    while True:
        bruto = input(msg).strip()
        if not bruto:
            return None if permitir_vazio else date.today().isoformat()
        try:
            return datetime.strptime(bruto, "%d/%m/%Y").date().isoformat()
        except ValueError:
            print("  Data inválida. Use dd/mm/aaaa.")


def ler_mes(msg="Mês (mm/aaaa, Enter = mês atual): "):
    while True:
        bruto = input(msg).strip()
        if not bruto:
            return date.today().strftime("%Y-%m")
        try:
            return datetime.strptime(bruto, "%m/%Y").strftime("%Y-%m")
        except ValueError:
            print("  Mês inválido. Use mm/aaaa.")


def confirmar(msg):
    return input(f"{msg} (s/n): ").strip().lower() in ("s", "sim")


def escolher(msg, opcoes):
    for i, op in enumerate(opcoes, 1):
        print(f"  {i}. {op}")
    while True:
        try:
            n = int(input(msg).strip())
            if 1 <= n <= len(opcoes):
                return opcoes[n - 1]
        except ValueError:
            pass
        print("  Opção inválida.")


def tabela(cabecalhos, linhas):
    if not linhas:
        print("  (nenhum registro)")
        return
    linhas = [[str(c) for c in l] for l in linhas]
    larg = [max(len(h), *(len(l[i]) for l in linhas)) for i, h in enumerate(cabecalhos)]
    print("  " + " | ".join(h.ljust(larg[i]) for i, h in enumerate(cabecalhos)))
    print("  " + "-+-".join("-" * w for w in larg))
    for l in linhas:
        print("  " + " | ".join(c.ljust(larg[i]) for i, c in enumerate(l)))


def menu(titulo, opcoes):
    """opcoes: lista de (rótulo, função). Volta quando o usuário digita 0."""
    while True:
        print(f"\n=== {titulo} ===")
        for i, (rotulo, _) in enumerate(opcoes, 1):
            print(f"{i}. {rotulo}")
        print("0. Voltar" if titulo != "MENU PRINCIPAL" else "0. Sair")
        esc = input("> ").strip()
        if esc == "0":
            return
        if esc.isdigit() and 1 <= int(esc) <= len(opcoes):
            opcoes[int(esc) - 1][1]()
        else:
            print("Opção inválida.")


# ----------------------------------------------------------------------------
# Clientes
# ----------------------------------------------------------------------------
def cadastrar_cliente(con):
    print("\n-- Novo cliente --")
    nome = ler_texto("Nome: ")
    telefone = ler_texto("Telefone/WhatsApp (opcional): ", obrigatorio=False)
    print("Como o cliente chegou até você?")
    origem = escolher("Origem: ", ORIGENS)
    obs = ler_texto("Observações (sabores favoritos, alergias, aniversário...): ",
                    obrigatorio=False)
    with con:
        cur = con.execute(
            "INSERT INTO clientes (nome, telefone, origem, observacoes) VALUES (?,?,?,?)",
            (nome, telefone, origem, obs))
    print(f"Cliente '{nome}' cadastrado (ID {cur.lastrowid}).")
    return cur.lastrowid


def listar_clientes(con):
    print("\n-- Clientes --")
    linhas = con.execute(f"""
        SELECT c.id, c.nome, c.telefone, c.origem,
               COUNT(DISTINCT v.id) AS compras, {TOTAL_VENDA} AS total
        FROM clientes c
        LEFT JOIN vendas v ON v.cliente_id = c.id AND v.cancelada = 0
        LEFT JOIN itens_venda i ON i.venda_id = v.id
        GROUP BY c.id ORDER BY c.nome""").fetchall()
    tabela(["ID", "Nome", "Telefone", "Origem", "Compras", "Total gasto"],
           [(r["id"], r["nome"], r["telefone"] or "-", r["origem"] or "-",
             r["compras"], brl(r["total"])) for r in linhas])


def historico_cliente(con):
    listar_clientes(con)
    cid = ler_int("\nID do cliente: ")
    cliente = con.execute("SELECT * FROM clientes WHERE id = ?", (cid,)).fetchone()
    if not cliente:
        print("Cliente não encontrado.")
        return
    print(f"\n{cliente['nome']} - {cliente['telefone'] or 'sem telefone'}"
          f" - origem: {cliente['origem'] or '-'}")
    if cliente["observacoes"]:
        print(f"Obs.: {cliente['observacoes']}")
    linhas = con.execute(f"""
        SELECT v.id, v.data, v.tipo, v.pago, v.cancelada, {TOTAL_VENDA} AS total
        FROM vendas v LEFT JOIN itens_venda i ON i.venda_id = v.id
        WHERE v.cliente_id = ? GROUP BY v.id ORDER BY v.data DESC""", (cid,)).fetchall()
    tabela(["Venda", "Data", "Tipo", "Total", "Situação"],
           [(r["id"], fmt_data(r["data"]), r["tipo"], brl(r["total"]),
             "Cancelada" if r["cancelada"] else ("Paga" if r["pago"] else "A receber"))
            for r in linhas])


# ----------------------------------------------------------------------------
# Produtos
# ----------------------------------------------------------------------------
def cadastrar_produto(con):
    print("\n-- Novo produto --")
    nome = ler_texto("Nome do doce: ")
    custo = ler_valor("Custo para produzir 1 unidade (R$): ")
    preco = ler_valor("Preço de venda de 1 unidade (R$): ")
    if preco <= custo:
        print("  ATENÇÃO: o preço é menor ou igual ao custo. Você teria prejuízo.")
        if not confirmar("Cadastrar mesmo assim?"):
            return
    try:
        with con:
            con.execute("INSERT INTO produtos (nome, custo_unitario, preco_venda) VALUES (?,?,?)",
                        (nome, custo, preco))
        print(f"Produto '{nome}' cadastrado.")
    except sqlite3.IntegrityError:
        print("Já existe um produto com esse nome.")


def listar_produtos(con, apenas_ativos=False):
    print("\n-- Produtos --")
    filtro = "WHERE ativo = 1" if apenas_ativos else ""
    linhas = con.execute(f"SELECT * FROM produtos {filtro} ORDER BY nome").fetchall()
    tabela(["ID", "Produto", "Custo", "Preço", "Lucro/un.", "Margem", "Ativo"],
           [(r["id"], r["nome"], brl(r["custo_unitario"]), brl(r["preco_venda"]),
             brl(r["preco_venda"] - r["custo_unitario"]),
             f"{(r['preco_venda'] - r['custo_unitario']) / r['preco_venda'] * 100:.0f}%"
             if r["preco_venda"] else "-",
             "sim" if r["ativo"] else "não") for r in linhas])
    return linhas


def atualizar_produto(con):
    listar_produtos(con)
    pid = ler_int("\nID do produto a atualizar: ")
    prod = con.execute("SELECT * FROM produtos WHERE id = ?", (pid,)).fetchone()
    if not prod:
        print("Produto não encontrado.")
        return
    print("Enter mantém o valor atual. Vendas já feitas não mudam.")
    custo = ler_valor(f"Novo custo [{brl(prod['custo_unitario'])}]: ", prod["custo_unitario"])
    preco = ler_valor(f"Novo preço [{brl(prod['preco_venda'])}]: ", prod["preco_venda"])
    ativo = 1 if confirmar("Produto ativo (aparece nas vendas)?") else 0
    with con:
        con.execute("UPDATE produtos SET custo_unitario=?, preco_venda=?, ativo=? WHERE id=?",
                    (custo, preco, ativo, pid))
    print("Produto atualizado.")


# ----------------------------------------------------------------------------
# Vendas e encomendas
# ----------------------------------------------------------------------------
def nova_venda(con):
    print("\n-- Nova venda / encomenda --")
    produtos = con.execute("SELECT * FROM produtos WHERE ativo = 1 ORDER BY nome").fetchall()
    if not produtos:
        print("Cadastre ao menos um produto antes de registrar vendas.")
        return
    por_id = {p["id"]: p for p in produtos}

    clientes = con.execute("SELECT id, nome FROM clientes ORDER BY nome").fetchall()
    for c in clientes:
        print(f"  {c['id']}. {c['nome']}")
    esc = input("ID do cliente (N = novo cliente, Enter = sem cadastro): ").strip().lower()
    cliente_id = None
    if esc == "n":
        cliente_id = cadastrar_cliente(con)
    elif esc.isdigit():
        if any(c["id"] == int(esc) for c in clientes):
            cliente_id = int(esc)
        else:
            print("  Cliente não encontrado; venda será registrada sem cadastro.")

    itens = []
    while True:
        print()
        for p in produtos:
            print(f"  {p['id']}. {p['nome']} - {brl(p['preco_venda'])}")
        pid = ler_int("ID do produto (0 para finalizar): ", minimo=0)
        if pid == 0:
            break
        if pid not in por_id:
            print("  Produto não encontrado.")
            continue
        prod = por_id[pid]
        qtd = ler_int(f"Quantidade de '{prod['nome']}': ")
        preco = ler_valor(f"Preço unitário [{brl(prod['preco_venda'])}]: ", prod["preco_venda"])
        itens.append((pid, qtd, preco, prod["custo_unitario"]))
        print(f"  Parcial: {brl(sum(q * p for _, q, p, _ in itens))}")
    if not itens:
        print("Nenhum item informado. Venda não registrada.")
        return

    tipo = escolher("Tipo: ", ["Pronta entrega", "Encomenda"])
    data_venda = ler_data("Data da venda (dd/mm/aaaa, Enter = hoje): ")
    data_entrega = None
    if tipo == "Encomenda":
        data_entrega = ler_data("Data de entrega (dd/mm/aaaa): ")
    pago = 1 if confirmar("Já foi paga?") else 0
    entregue = 0 if tipo == "Encomenda" else 1

    with con:
        cur = con.execute(
            "INSERT INTO vendas (cliente_id, data, tipo, data_entrega, pago, entregue)"
            " VALUES (?,?,?,?,?,?)",
            (cliente_id, data_venda, tipo, data_entrega, pago, entregue))
        con.executemany(
            "INSERT INTO itens_venda (venda_id, produto_id, quantidade, preco_unitario,"
            " custo_unitario) VALUES (?,?,?,?,?)",
            [(cur.lastrowid, *item) for item in itens])
    total = sum(q * p for _, q, p, _ in itens)
    lucro = sum(q * (p - c) for _, q, p, c in itens)
    print(f"Venda {cur.lastrowid} registrada: total {brl(total)}, lucro bruto {brl(lucro)}.")


def listar_vendas(con, limite=20):
    print(f"\n-- Últimas {limite} vendas --")
    linhas = con.execute(f"""
        SELECT v.id, v.data, COALESCE(c.nome, '(sem cadastro)') AS cliente, v.tipo,
               v.data_entrega, v.pago, v.entregue, v.cancelada, {TOTAL_VENDA} AS total
        FROM vendas v
        LEFT JOIN clientes c ON c.id = v.cliente_id
        LEFT JOIN itens_venda i ON i.venda_id = v.id
        GROUP BY v.id ORDER BY v.data DESC, v.id DESC LIMIT ?""", (limite,)).fetchall()

    def situacao(r):
        if r["cancelada"]:
            return "Cancelada"
        partes = ["Paga" if r["pago"] else "A receber"]
        partes.append("entregue" if r["entregue"] else "a entregar")
        return ", ".join(partes)

    tabela(["Venda", "Data", "Cliente", "Tipo", "Entrega", "Total", "Situação"],
           [(r["id"], fmt_data(r["data"]), r["cliente"], r["tipo"], fmt_data(r["data_entrega"]),
             brl(r["total"]), situacao(r)) for r in linhas])


def atualizar_venda(con):
    listar_vendas(con)
    vid = ler_int("\nID da venda: ")
    venda = con.execute("SELECT * FROM vendas WHERE id = ?", (vid,)).fetchone()
    if not venda:
        print("Venda não encontrada.")
        return
    acao = escolher("O que fazer? ", ["Marcar como paga", "Marcar como entregue",
                                       "Cancelar venda"])
    campo = {"Marcar como paga": "pago = 1", "Marcar como entregue": "entregue = 1",
             "Cancelar venda": "cancelada = 1"}[acao]
    if acao == "Cancelar venda" and not confirmar("Cancelar esta venda?"):
        return
    with con:
        con.execute(f"UPDATE vendas SET {campo} WHERE id = ?", (vid,))
    print("Venda atualizada.")


def lista_producao(con):
    print("\n-- Encomendas pendentes de entrega --")
    pedidos = con.execute(f"""
        SELECT v.id, v.data_entrega, COALESCE(c.nome, '(sem cadastro)') AS cliente,
               v.pago, {TOTAL_VENDA} AS total
        FROM vendas v LEFT JOIN clientes c ON c.id = v.cliente_id
        LEFT JOIN itens_venda i ON i.venda_id = v.id
        WHERE v.entregue = 0 AND v.cancelada = 0
        GROUP BY v.id ORDER BY v.data_entrega, v.id""").fetchall()
    tabela(["Venda", "Entrega", "Cliente", "Total", "Pagamento"],
           [(r["id"], fmt_data(r["data_entrega"]), r["cliente"], brl(r["total"]),
             "Paga" if r["pago"] else "A receber") for r in pedidos])
    if not pedidos:
        return
    print("\n-- O que produzir (soma de todas as encomendas pendentes) --")
    linhas = con.execute("""
        SELECT p.nome, SUM(i.quantidade) AS qtd
        FROM itens_venda i JOIN vendas v ON v.id = i.venda_id
        JOIN produtos p ON p.id = i.produto_id
        WHERE v.entregue = 0 AND v.cancelada = 0
        GROUP BY p.id ORDER BY p.nome""").fetchall()
    tabela(["Produto", "Quantidade"], [(r["nome"], r["qtd"]) for r in linhas])


# ----------------------------------------------------------------------------
# Gastos
# ----------------------------------------------------------------------------
def registrar_gasto(con):
    print("\n-- Novo gasto --")
    data_gasto = ler_data("Data (dd/mm/aaaa, Enter = hoje): ")
    categoria = escolher("Categoria: ", CATEGORIAS_GASTO)
    if categoria == CAT_INSUMOS:
        print("  Dica: compras de ingredientes devem ser lançadas em Estoque > Registrar "
              "compra, que já cria este gasto e atualiza o estoque.")
    descricao = ler_texto("Descrição: ")
    valor = ler_valor("Valor (R$): ")
    with con:
        con.execute("INSERT INTO gastos (data, categoria, descricao, valor) VALUES (?,?,?,?)",
                    (data_gasto, categoria, descricao, valor))
    print("Gasto registrado.")


def listar_gastos(con):
    mes = ler_mes()
    print(f"\n-- Gastos de {mes[5:]}/{mes[:4]} --")
    linhas = con.execute("""SELECT data, categoria, descricao, valor FROM gastos
                            WHERE strftime('%Y-%m', data) = ? ORDER BY data""", (mes,)).fetchall()
    tabela(["Data", "Categoria", "Descrição", "Valor"],
           [(fmt_data(r["data"]), r["categoria"], r["descricao"], brl(r["valor"]))
            for r in linhas])
    print(f"\n  Total: {brl(sum(r['valor'] for r in linhas))}")


# ----------------------------------------------------------------------------
# Estoque
# ----------------------------------------------------------------------------
def estoque_atual(con, ing_id):
    return con.execute("SELECT COALESCE(SUM(restante), 0) FROM lotes WHERE ingrediente_id = ?",
                       (ing_id,)).fetchone()[0]


def preco_esperado(con, ing_id):
    """Preço por g/ml/un que se espera pagar pelo ingrediente.

    Média ponderada do que ainda está em estoque; sem estoque, o preço da última compra.
    Devolve (preço, origem) ou (None, None) se o ingrediente nunca foi comprado.
    """
    r = con.execute("""SELECT SUM(restante * custo_unitario) / SUM(restante) AS preco
                       FROM lotes WHERE ingrediente_id = ? AND restante > ?""",
                    (ing_id, EPS)).fetchone()
    if r["preco"] is not None:
        return r["preco"], "estoque"
    r = con.execute("""SELECT custo_unitario FROM lotes WHERE ingrediente_id = ?
                       ORDER BY data DESC, id DESC LIMIT 1""", (ing_id,)).fetchone()
    if r:
        return r["custo_unitario"], "última compra"
    return None, None


def consumir(con, ing_id, quantidade):
    """Tira do estoque pelo lote mais antigo (FIFO) e devolve o custo real da retirada.

    Deve ser chamada dentro de `with con:` para que uma falha desfaça tudo.
    """
    falta = quantidade
    custo = 0.0
    lotes = con.execute("""SELECT id, restante, custo_unitario FROM lotes
                           WHERE ingrediente_id = ? AND restante > ?
                           ORDER BY data, id""", (ing_id, EPS)).fetchall()
    for lote in lotes:
        if falta <= EPS:
            break
        tira = min(lote["restante"], falta)
        novo = lote["restante"] - tira
        con.execute("UPDATE lotes SET restante = ? WHERE id = ?",
                    (0.0 if novo < EPS else round(novo, 6), lote["id"]))
        custo += tira * lote["custo_unitario"]
        falta -= tira
    if falta > EPS:
        raise ValueError("Estoque insuficiente.")
    return custo


def cadastrar_ingrediente(con):
    print("\n-- Novo ingrediente --")
    nome = ler_texto("Nome (ex.: Leite condensado): ")
    opcoes = ["g  (gramas: farinha, açúcar, chocolate...)",
              "ml (mililitros: leite, creme de leite, óleo...)",
              "un (unidades: ovos, forminhas, caixinhas...)"]
    unidade = UNIDADES[opcoes.index(escolher("Medido em: ", opcoes))]
    minimo = ler_quantidade(
        f"Avisar quando o estoque chegar a (ex.: {EXEMPLO_QTD[unidade]}; Enter = não avisar): ",
        unidade, padrao=0.0, permitir_zero=True)
    try:
        with con:
            cur = con.execute(
                "INSERT INTO ingredientes (nome, unidade, estoque_minimo) VALUES (?,?,?)",
                (nome, unidade, minimo))
    except sqlite3.IntegrityError:
        print("Já existe um ingrediente com esse nome.")
        return None
    print(f"Ingrediente '{nome}' cadastrado (ID {cur.lastrowid}).")
    return cur.lastrowid


def pedir_ingrediente(con, permitir_novo=False):
    linhas = con.execute("SELECT * FROM ingredientes ORDER BY nome").fetchall()
    for r in linhas:
        print(f"  {r['id']}. {r['nome']} ({r['unidade']})")
    if not linhas and not permitir_novo:
        print("  Nenhum ingrediente cadastrado. Use Estoque > Cadastrar ingrediente.")
        return None
    dica = " (N = novo ingrediente)" if permitir_novo else ""
    esc = input(f"ID do ingrediente{dica}, Enter = cancelar: ").strip().lower()
    if esc == "n" and permitir_novo:
        ing_id = cadastrar_ingrediente(con)
        if ing_id is None:
            return None
    elif esc.isdigit():
        ing_id = int(esc)
    else:
        return None
    ing = con.execute("SELECT * FROM ingredientes WHERE id = ?", (ing_id,)).fetchone()
    if not ing:
        print("  Ingrediente não encontrado.")
    return ing


def registrar_compra(con):
    print("\n-- Registrar compra de ingrediente --")
    ing = pedir_ingrediente(con, permitir_novo=True)
    if not ing:
        return
    un = ing["unidade"]
    n = ler_int("Quantas embalagens/unidades você comprou? ")
    padrao = ing["tamanho_embalagem"]
    dica = f" [{fmt_qtd(padrao, un)}]" if padrao else ""
    tamanho = ler_quantidade(
        f"Tamanho de cada embalagem (ex.: {EXEMPLO_QTD[un]}){dica}: ", un, padrao=padrao)
    quantidade = round(n * tamanho, 6)
    total = ler_valor("Valor total pago (R$): ")
    while total <= 0:
        print("  O valor pago deve ser maior que zero.")
        total = ler_valor("Valor total pago (R$): ")
    data_compra = ler_data("Data da compra (dd/mm/aaaa, Enter = hoje): ")
    local = ler_texto("Onde comprou (opcional): ", obrigatorio=False)
    custo_un = total / quantidade

    anterior = con.execute("""SELECT custo_unitario FROM lotes WHERE ingrediente_id = ?
                              ORDER BY data DESC, id DESC LIMIT 1""", (ing["id"],)).fetchone()
    with con:
        gasto = con.execute(
            "INSERT INTO gastos (data, categoria, descricao, valor) VALUES (?,?,?,?)",
            (data_compra, CAT_INSUMOS,
             f"Compra: {ing['nome']} ({n} x {fmt_qtd(tamanho, un)})", total))
        con.execute("""INSERT INTO lotes (ingrediente_id, data, local, quantidade, restante,
                                          preco_total, custo_unitario, gasto_id)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (ing["id"], data_compra, local or None, quantidade, quantidade, total,
                     custo_un, gasto.lastrowid))
        con.execute("UPDATE ingredientes SET tamanho_embalagem = ? WHERE id = ?",
                    (tamanho, ing["id"]))
    print(f"Compra registrada: {fmt_qtd(quantidade, un)} de {ing['nome']} por {brl(total)} "
          f"({fmt_preco_un(custo_un, un)}). Gasto lançado em Insumos.")
    if anterior and anterior["custo_unitario"] > 0:
        var = (custo_un / anterior["custo_unitario"] - 1) * 100
        if abs(var) >= 1:
            print(f"  O preço {'subiu' if var > 0 else 'caiu'} {abs(var):.0f}% em relação à "
                  f"compra anterior ({fmt_preco_un(anterior['custo_unitario'], un)}).")
    print(f"  Estoque atual: {fmt_qtd(estoque_atual(con, ing['id']), un)}")


def ver_estoque(con):
    print("\n-- Estoque e preços esperados --")
    linhas = con.execute("""
        SELECT g.id, g.nome, g.unidade, g.estoque_minimo,
               COALESCE(SUM(l.restante), 0) AS qtd,
               COALESCE(SUM(l.restante * l.custo_unitario), 0) AS valor
        FROM ingredientes g LEFT JOIN lotes l ON l.ingrediente_id = g.id AND l.restante > ?
        GROUP BY g.id ORDER BY g.nome""", (EPS,)).fetchall()
    saida = []
    for r in linhas:
        preco, origem = preco_esperado(con, r["id"])
        if preco is None:
            preco_txt = "sem preço"
        else:
            preco_txt = fmt_preco_un(preco, r["unidade"]) + ("" if origem == "estoque" else " *")
        if r["qtd"] <= EPS:
            alerta = "SEM ESTOQUE"
        elif r["estoque_minimo"] > 0 and r["qtd"] <= r["estoque_minimo"] + EPS:
            alerta = "BAIXO"
        else:
            alerta = ""
        saida.append((r["id"], r["nome"], fmt_qtd(r["qtd"], r["unidade"]), preco_txt,
                      brl(r["valor"]), alerta))
    tabela(["ID", "Ingrediente", "Em estoque", "Preço esperado", "Valor em estoque", "Alerta"],
           saida)
    if linhas:
        print(f"\n  Valor total em estoque: {brl(sum(r['valor'] for r in linhas))}")
        if any(s[3].endswith("*") for s in saida):
            print("  * sem estoque: mostrando o preço da última compra")


def historico_compras(con):
    print("\n-- Histórico de compras e preços --")
    ing = pedir_ingrediente(con)
    if not ing:
        return
    un = ing["unidade"]
    lotes = con.execute("""SELECT * FROM lotes WHERE ingrediente_id = ?
                           ORDER BY data DESC, id DESC""", (ing["id"],)).fetchall()
    tabela(["Compra", "Data", "Onde", "Quantidade", "Total pago", "Preço", "Ainda resta"],
           [(l["id"], fmt_data(l["data"]), l["local"] or "-", fmt_qtd(l["quantidade"], un),
             brl(l["preco_total"]), fmt_preco_un(l["custo_unitario"], un),
             fmt_qtd(l["restante"], un)) for l in lotes])
    if lotes:
        menor = min(lotes, key=lambda l: l["custo_unitario"])
        maior = max(lotes, key=lambda l: l["custo_unitario"])
        print(f"\n  Menor preço pago: {fmt_preco_un(menor['custo_unitario'], un)}"
              f" ({menor['local'] or 'sem local'}, {fmt_data(menor['data'])})")
        print(f"  Maior preço pago: {fmt_preco_un(maior['custo_unitario'], un)}"
              f" ({maior['local'] or 'sem local'}, {fmt_data(maior['data'])})")


def excluir_compra(con):
    print("\n-- Excluir compra lançada por engano --")
    lotes = con.execute("""SELECT l.*, g.nome, g.unidade FROM lotes l
                           JOIN ingredientes g ON g.id = l.ingrediente_id
                           ORDER BY l.data DESC, l.id DESC LIMIT 15""").fetchall()
    tabela(["Compra", "Data", "Ingrediente", "Quantidade", "Total pago", "Situação"],
           [(l["id"], fmt_data(l["data"]), l["nome"], fmt_qtd(l["quantidade"], l["unidade"]),
             brl(l["preco_total"]),
             "intacta" if l["restante"] >= l["quantidade"] - EPS else "já usada") for l in lotes])
    if not lotes:
        return
    lid = ler_int("ID da compra a excluir (0 = cancelar): ", minimo=0)
    lote = next((l for l in lotes if l["id"] == lid), None)
    if lid == 0:
        return
    if not lote:
        print("Compra não encontrada entre as últimas 15.")
        return
    if lote["restante"] < lote["quantidade"] - EPS:
        print("Parte desta compra já foi usada em produção/perda; não dá para excluir.")
        return
    if not confirmar(f"Excluir a compra de {lote['nome']} e o gasto correspondente?"):
        return
    with con:
        con.execute("DELETE FROM lotes WHERE id = ?", (lid,))
        if lote["gasto_id"]:
            con.execute("DELETE FROM gastos WHERE id = ?", (lote["gasto_id"],))
    print("Compra e gasto excluídos.")


def registrar_perda(con):
    print("\n-- Registrar perda / desperdício --")
    ing = pedir_ingrediente(con)
    if not ing:
        return
    un = ing["unidade"]
    disp = estoque_atual(con, ing["id"])
    print(f"  Em estoque: {fmt_qtd(disp, un)}")
    qtd = ler_quantidade(f"Quantidade perdida (ex.: {EXEMPLO_QTD[un]}): ", un)
    if qtd > disp + EPS:
        print("Não há essa quantidade em estoque.")
        return
    data_perda = ler_data("Data (dd/mm/aaaa, Enter = hoje): ")
    motivo = ler_texto("Motivo (estragou, caiu, venceu...): ", obrigatorio=False)
    with con:
        valor = consumir(con, ing["id"], qtd)
        con.execute("""INSERT INTO perdas (data, ingrediente_id, quantidade, valor, motivo)
                       VALUES (?,?,?,?,?)""", (data_perda, ing["id"], qtd, valor, motivo))
    print(f"Perda registrada: {fmt_qtd(qtd, un)} de {ing['nome']} = {brl(valor)} "
          f"(entra no cálculo do lucro).")


# ----------------------------------------------------------------------------
# Receitas e custo estimado
# ----------------------------------------------------------------------------
def escolher_produto(con, apenas_com_receita=False):
    filtro = "WHERE id IN (SELECT produto_id FROM receitas)" if apenas_com_receita else ""
    produtos = con.execute(f"SELECT * FROM produtos {filtro} ORDER BY nome").fetchall()
    if not produtos:
        print("  Nenhum produto " + ("com receita" if apenas_com_receita else "cadastrado")
              + " ainda.")
        return None
    for p in produtos:
        print(f"  {p['id']}. {p['nome']}")
    pid = ler_int("ID do produto (0 = cancelar): ", minimo=0)
    return next((p for p in produtos if p["id"] == pid), None)


def custo_receita(con, produto_id):
    """Custo estimado da receita com os preços esperados do estoque (None se não há receita)."""
    rec = con.execute("SELECT * FROM receitas WHERE produto_id = ?", (produto_id,)).fetchone()
    if not rec:
        return None
    itens = con.execute("""SELECT ir.ingrediente_id, ir.quantidade, g.nome, g.unidade
                           FROM itens_receita ir JOIN ingredientes g ON g.id = ir.ingrediente_id
                           WHERE ir.receita_id = ? ORDER BY g.nome""", (rec["id"],)).fetchall()
    linhas, total, incompleto = [], 0.0, not itens
    for it in itens:
        preco, origem = preco_esperado(con, it["ingrediente_id"])
        if preco is None:
            incompleto = True
        subtotal = it["quantidade"] * preco if preco is not None else 0.0
        total += subtotal
        linhas.append({"ingrediente_id": it["ingrediente_id"], "nome": it["nome"],
                       "unidade": it["unidade"], "quantidade": it["quantidade"],
                       "preco": preco, "origem": origem, "subtotal": subtotal,
                       "estoque": estoque_atual(con, it["ingrediente_id"])})
    return {"receita_id": rec["id"], "rendimento": rec["rendimento"], "linhas": linhas,
            "total": total, "por_unidade": total / rec["rendimento"],
            "incompleto": incompleto, "vazia": not itens}


def mostrar_receita(con, produto, perguntar_margem=True):
    c = custo_receita(con, produto["id"])
    if c is None:
        print("  Este produto ainda não tem receita.")
        return
    print(f"\n{produto['nome']} - receita que rende {c['rendimento']} unidade(s)")
    if c["vazia"]:
        print("  (receita ainda sem ingredientes)")
        return
    linhas = []
    for l in c["linhas"]:
        if l["preco"] is None:
            preco_txt, sub_txt = "sem preço", "-"
        else:
            preco_txt = fmt_preco_un(l["preco"], l["unidade"]) + \
                ("" if l["origem"] == "estoque" else " *")
            sub_txt = brl(l["subtotal"])
        linhas.append((l["nome"], fmt_qtd(l["quantidade"], l["unidade"]), preco_txt, sub_txt,
                       fmt_qtd(l["estoque"], l["unidade"])))
    tabela(["Ingrediente", "Na receita", "Preço esperado", "Subtotal", "Em estoque"], linhas)
    if any(l["origem"] == "última compra" for l in c["linhas"]):
        print("  * sem estoque: usando o preço da última compra")
    faltam = [l["nome"] for l in c["linhas"] if l["preco"] is None]
    if faltam:
        print(f"  ATENÇÃO: nunca foram comprados: {', '.join(faltam)}. Custo subestimado.")
    por_un = c["por_unidade"]
    print(f"\n  Custo estimado da receita:     {brl(c['total'])}")
    print(f"  Custo estimado por unidade:    {brl(por_un)}")
    print(f"  Custo cadastrado no produto:   {brl(produto['custo_unitario'])}")
    print(f"  Preço de venda:                {brl(produto['preco_venda'])}")
    if produto["preco_venda"] > 0:
        lucro = produto["preco_venda"] - por_un
        print(f"  Lucro por unidade (estimado):  {brl(lucro)} "
              f"({lucro / produto['preco_venda'] * 100:.0f}% de margem)")
        if lucro <= 0:
            print("  ATENÇÃO: com o custo estimado, este produto dá prejuízo.")
    possiveis = min(l["estoque"] / l["quantidade"] for l in c["linhas"])
    print(f"  Com o estoque atual dá para fazer ~{num(possiveis, 1)} receita(s) "
          f"({int(possiveis * c['rendimento'] + EPS)} unidades).")
    if perguntar_margem:
        bruto = input("\nMargem de lucro desejada em % para sugerir um preço (Enter = pular): ")
        if bruto.strip():
            try:
                margem = parse_numero(bruto.replace("%", ""))
                if not 0 <= margem < 100:
                    raise ValueError
                print(f"  Para {margem:.0f}% de margem, venda a {brl(por_un / (1 - margem / 100))}"
                      f" cada (custo estimado {brl(por_un)}).")
            except ValueError:
                print("  Margem inválida (use um valor entre 0 e 99).")


def ver_receita(con):
    print("\n-- Receita e custo estimado --")
    produto = escolher_produto(con, apenas_com_receita=True)
    if produto:
        mostrar_receita(con, produto)


def editar_receita(con):
    print("\n-- Definir / editar receita --")
    produto = escolher_produto(con)
    if not produto:
        return
    rec = con.execute("SELECT * FROM receitas WHERE produto_id = ?", (produto["id"],)).fetchone()
    if rec:
        rec_id = rec["id"]
    else:
        rendimento = ler_int(f"Quantas unidades de '{produto['nome']}' esta receita rende? ")
        with con:
            rec_id = con.execute("INSERT INTO receitas (produto_id, rendimento) VALUES (?,?)",
                                 (produto["id"], rendimento)).lastrowid
    while True:
        rec = con.execute("SELECT * FROM receitas WHERE id = ?", (rec_id,)).fetchone()
        itens = con.execute("""SELECT ir.ingrediente_id, ir.quantidade, g.nome, g.unidade
                               FROM itens_receita ir
                               JOIN ingredientes g ON g.id = ir.ingrediente_id
                               WHERE ir.receita_id = ? ORDER BY g.nome""", (rec_id,)).fetchall()
        print(f"\nReceita de {produto['nome']} (rende {rec['rendimento']} un.):")
        tabela(["ID", "Ingrediente", "Quantidade"],
               [(i["ingrediente_id"], i["nome"], fmt_qtd(i["quantidade"], i["unidade"]))
                for i in itens])
        acao = escolher("O que fazer? ", ["Adicionar ou alterar ingrediente",
                                          "Remover ingrediente", "Alterar rendimento",
                                          "Concluir e ver custo estimado"])
        if acao.startswith("Adicionar"):
            ing = pedir_ingrediente(con, permitir_novo=True)
            if ing:
                qtd = ler_quantidade(
                    f"Quantidade de {ing['nome']} em toda a receita "
                    f"(ex.: {EXEMPLO_QTD[ing['unidade']]}): ", ing["unidade"])
                with con:
                    con.execute("""INSERT INTO itens_receita (receita_id, ingrediente_id, quantidade)
                                   VALUES (?,?,?)
                                   ON CONFLICT (receita_id, ingrediente_id)
                                   DO UPDATE SET quantidade = excluded.quantidade""",
                                (rec_id, ing["id"], qtd))
        elif acao.startswith("Remover"):
            if not itens:
                print("  A receita não tem ingredientes.")
                continue
            ing_id = ler_int("ID do ingrediente a remover (0 = cancelar): ", minimo=0)
            if ing_id and any(i["ingrediente_id"] == ing_id for i in itens):
                with con:
                    con.execute("DELETE FROM itens_receita WHERE receita_id = ? "
                                "AND ingrediente_id = ?", (rec_id, ing_id))
        elif acao.startswith("Alterar"):
            novo = ler_int(f"Novo rendimento (unidades) [{rec['rendimento']}]: ",
                           padrao=rec["rendimento"])
            with con:
                con.execute("UPDATE receitas SET rendimento = ? WHERE id = ?", (novo, rec_id))
        else:
            break
    mostrar_receita(con, produto, perguntar_margem=True)


def panorama_custos(con):
    print("\n-- Custo estimado x custo cadastrado --")
    linhas = []
    for p in con.execute("SELECT * FROM produtos ORDER BY nome").fetchall():
        c = custo_receita(con, p["id"])
        if c is None or c["vazia"]:
            linhas.append((p["nome"], "-", brl(p["custo_unitario"]), brl(p["preco_venda"]),
                           "-", "sem receita"))
            continue
        est = c["por_unidade"]
        margem = f"{(p['preco_venda'] - est) / p['preco_venda'] * 100:.0f}%" \
            if p["preco_venda"] else "-"
        obs = []
        if c["incompleto"]:
            obs.append("faltam preços")
        if abs(est - p["custo_unitario"]) > max(0.05 * p["custo_unitario"], 0.005):
            obs.append("custo cadastrado difere")
        linhas.append((p["nome"], brl(est), brl(p["custo_unitario"]), brl(p["preco_venda"]),
                       margem, ", ".join(obs)))
    tabela(["Produto", "Custo estimado", "Custo cadastrado", "Preço", "Margem est.", "Obs."],
           linhas)


def atualizar_custos(con):
    print("\n-- Atualizar custo cadastrado a partir das receitas --")
    mudancas, ignorados = [], 0
    for p in con.execute("SELECT * FROM produtos ORDER BY nome").fetchall():
        c = custo_receita(con, p["id"])
        if c is None or c["incompleto"]:
            ignorados += 1
            continue
        novo = round(c["por_unidade"], 4)
        if abs(novo - p["custo_unitario"]) >= 0.005:
            mudancas.append((p, novo))
    if ignorados:
        print(f"  {ignorados} produto(s) ignorado(s): sem receita ou com ingredientes sem preço.")
    if not mudancas:
        print("  Nenhum custo para atualizar.")
        return
    tabela(["Produto", "Custo atual", "Custo pela receita"],
           [(p["nome"], brl(p["custo_unitario"]), brl(novo)) for p, novo in mudancas])
    print("  As vendas já registradas não mudam; só as próximas usarão o novo custo.")
    if confirmar("Aplicar estes custos?"):
        with con:
            con.executemany("UPDATE produtos SET custo_unitario = ? WHERE id = ?",
                            [(novo, p["id"]) for p, novo in mudancas])
        print("Custos atualizados.")


# ----------------------------------------------------------------------------
# Produção e lista de compras
# ----------------------------------------------------------------------------
def necessidades(con, plano):
    """plano: {produto_id: unidades} -> ({ingrediente_id: quantidade}, [produtos sem receita])."""
    need, sem_receita = {}, []
    for pid, unidades in plano.items():
        if unidades <= 0:
            continue
        rec = con.execute("SELECT * FROM receitas WHERE produto_id = ?", (pid,)).fetchone()
        if not rec:
            sem_receita.append(con.execute("SELECT nome FROM produtos WHERE id = ?",
                                           (pid,)).fetchone()["nome"])
            continue
        fator = unidades / rec["rendimento"]
        for it in con.execute("SELECT * FROM itens_receita WHERE receita_id = ?", (rec["id"],)):
            need[it["ingrediente_id"]] = need.get(it["ingrediente_id"], 0.0) + \
                it["quantidade"] * fator
    return need, sem_receita


def registrar_producao(con):
    print("\n-- Registrar produção (dá baixa no estoque) --")
    produto = escolher_produto(con, apenas_com_receita=True)
    if not produto:
        return
    c = custo_receita(con, produto["id"])
    if c["vazia"]:
        print("A receita está sem ingredientes. Complete-a em Receitas e custos.")
        return
    unidades = ler_int(f"Quantas unidades de '{produto['nome']}' foram produzidas? "
                       f"[{c['rendimento']}]: ", padrao=c["rendimento"])
    need, _ = necessidades(con, {produto["id"]: unidades})
    ings = {r["id"]: r for r in con.execute("SELECT * FROM ingredientes")}
    faltas = []
    for ing_id, q in need.items():
        disp = estoque_atual(con, ing_id)
        if q > disp + EPS:
            un = ings[ing_id]["unidade"]
            faltas.append((ings[ing_id]["nome"], fmt_qtd(q, un), fmt_qtd(disp, un),
                           fmt_qtd(q - disp, un)))
    if faltas:
        print("\nEstoque insuficiente. Nada foi baixado.")
        tabela(["Ingrediente", "Precisa", "Em estoque", "Falta"], faltas)
        print("Use Estoque > Lista de compras para ver o que comprar.")
        return
    data_prod = ler_data("Data da produção (dd/mm/aaaa, Enter = hoje): ")
    with con:
        total = sum(consumir(con, ing_id, q) for ing_id, q in need.items())
        con.execute("INSERT INTO producoes (data, produto_id, unidades, custo_total) "
                    "VALUES (?,?,?,?)", (data_prod, produto["id"], unidades, total))
    estimado = c["por_unidade"] * unidades
    print(f"\nProdução registrada: {unidades} un. de {produto['nome']}.")
    print(f"  Custo real (lotes consumidos): {brl(total)} = {brl(total / unidades)} por unidade")
    print(f"  Custo estimado antes:          {brl(estimado)} = "
          f"{brl(c['por_unidade'])} por unidade")
    print(f"  Custo cadastrado no produto:   {brl(produto['custo_unitario'])} por unidade")


def lista_compras(con):
    print("\n-- Lista de compras --")
    pendentes = con.execute("""
        SELECT p.id, p.nome, SUM(i.quantidade) AS un
        FROM itens_venda i JOIN vendas v ON v.id = i.venda_id
        JOIN produtos p ON p.id = i.produto_id
        WHERE v.entregue = 0 AND v.cancelada = 0
        GROUP BY p.id ORDER BY p.nome""").fetchall()
    plano = {}
    if pendentes:
        print("Encomendas pendentes: unidades a produzir de cada produto.")
        print("(Se parte já foi produzida, digite a quantidade que ainda falta.)")
        for r in pendentes:
            plano[r["id"]] = ler_int(f"  {r['nome']} [{r['un']}]: ", minimo=0, padrao=r["un"])
    else:
        print("Não há encomendas pendentes.")
    while confirmar("Adicionar produção extra (ex.: para pronta entrega)?"):
        produto = escolher_produto(con)
        if not produto:
            break
        plano[produto["id"]] = plano.get(produto["id"], 0) + \
            ler_int(f"Unidades extras de {produto['nome']}: ")
    need, sem_receita = necessidades(con, plano)
    if sem_receita:
        print(f"  Sem receita (não entram no cálculo): {', '.join(sem_receita)}")
    if not need:
        print("  Nada para calcular.")
        return
    ings = {r["id"]: r for r in con.execute("SELECT * FROM ingredientes")}
    linhas, total, sem_preco = [], 0.0, []
    for ing_id in sorted(need, key=lambda i: ings[i]["nome"].lower()):
        ing, precisa, un = ings[ing_id], need[ing_id], ings[ing_id]["unidade"]
        disp = estoque_atual(con, ing_id)
        comprar = max(0.0, precisa - disp)
        if comprar < EPS:
            comprar = 0.0
        preco, _ = preco_esperado(con, ing_id)
        if comprar > 0 and preco is None:
            sem_preco.append(ing["nome"])
        custo = comprar * preco if preco is not None else None
        total += custo or 0.0
        emb = "-"
        if comprar > 0 and ing["tamanho_embalagem"]:
            emb = f"{math.ceil(comprar / ing['tamanho_embalagem'] - EPS)} x " \
                  f"{fmt_qtd(ing['tamanho_embalagem'], un)}"
        linhas.append((ing["nome"], fmt_qtd(precisa, un), fmt_qtd(disp, un),
                       fmt_qtd(comprar, un) if comprar else "ok",
                       "-" if not comprar else (brl(custo) if custo is not None else "sem preço"),
                       emb))
    print()
    tabela(["Ingrediente", "Precisa", "Em estoque", "Comprar", "Custo estimado",
            "Embalagens (aprox.)"], linhas)
    print(f"\n  Custo estimado das compras: {brl(total)}"
          " (pelo preço esperado; o valor real depende das embalagens e do mercado)")
    if sem_preco:
        print(f"  Sem preço de referência: {', '.join(sem_preco)} (nunca comprados).")


# ----------------------------------------------------------------------------
# Relatórios
# ----------------------------------------------------------------------------
def calcular_resumo(con, mes):
    v = con.execute("""
        SELECT COALESCE(SUM(i.quantidade * i.preco_unitario), 0) AS receita,
               COALESCE(SUM(i.quantidade * i.custo_unitario), 0) AS custo,
               COALESCE(SUM(CASE WHEN v.pago = 1 THEN i.quantidade * i.preco_unitario END), 0)
                   AS recebido
        FROM itens_venda i JOIN vendas v ON v.id = i.venda_id
        WHERE v.cancelada = 0 AND strftime('%Y-%m', v.data) = ?""", (mes,)).fetchone()
    g = con.execute("""
        SELECT COALESCE(SUM(valor), 0) AS total,
               COALESCE(SUM(CASE WHEN categoria != ? THEN valor END), 0) AS operacionais
        FROM gastos WHERE strftime('%Y-%m', data) = ?""", (CAT_INSUMOS, mes)).fetchone()
    perdas = con.execute("""SELECT COALESCE(SUM(valor), 0) FROM perdas
                            WHERE strftime('%Y-%m', data) = ?""", (mes,)).fetchone()[0]
    receita, custo, recebido = v["receita"], v["custo"], v["recebido"]
    return {
        "receita": receita, "custo": custo, "lucro_bruto": receita - custo,
        "gastos_op": g["operacionais"], "perdas": perdas,
        "lucro_liquido": receita - custo - g["operacionais"] - perdas,
        "recebido": recebido, "a_receber": receita - recebido,
        "gastos_total": g["total"], "caixa": recebido - g["total"],
    }


def resumo_mensal(con):
    mes = ler_mes()
    r = calcular_resumo(con, mes)
    print(f"\n===== RESUMO DE {mes[5:]}/{mes[:4]} =====")
    print(f"  Receita (vendas do mês)         {brl(r['receita']):>14}")
    print(f"  (-) Custo dos produtos          {brl(r['custo']):>14}")
    print(f"  = Lucro bruto                   {brl(r['lucro_bruto']):>14}")
    print(f"  (-) Gastos operacionais         {brl(r['gastos_op']):>14}")
    print(f"  (-) Perdas de estoque           {brl(r['perdas']):>14}")
    print(f"  = LUCRO LÍQUIDO                {brl(r['lucro_liquido']):>14}")
    if r["receita"]:
        print(f"  Margem líquida                  {r['lucro_liquido'] / r['receita'] * 100:>13.1f}%")
    print("\n  --- Caixa ---")
    print(f"  Recebido                        {brl(r['recebido']):>14}")
    print(f"  A receber (vendas não pagas)    {brl(r['a_receber']):>14}")
    print(f"  (-) Todos os gastos (c/ insumos){brl(r['gastos_total']):>14}")
    print(f"  = Saldo de caixa do mês         {brl(r['caixa']):>14}")
    if r["lucro_liquido"] < 0:
        print("\n  ATENÇÃO: prejuízo no mês. Revise preços, custos e gastos.")
    elif r["caixa"] < 0 <= r["lucro_liquido"]:
        print("\n  ATENÇÃO: há lucro, mas o caixa está negativo (muito a receber ou "
              "compras de insumos adiantadas).")

    gastos = con.execute("""SELECT categoria, SUM(valor) AS total FROM gastos
                            WHERE strftime('%Y-%m', data) = ? GROUP BY categoria
                            ORDER BY total DESC""", (mes,)).fetchall()
    if gastos:
        print("\n  Gastos por categoria:")
        for g in gastos:
            print(f"    {g['categoria']:<14}{brl(g['total']):>14}")


def lucro_por_produto(con):
    mes = ler_mes()
    print(f"\n-- Lucro por produto em {mes[5:]}/{mes[:4]} --")
    linhas = con.execute("""
        SELECT p.nome, SUM(i.quantidade) AS qtd,
               SUM(i.quantidade * i.preco_unitario) AS receita,
               SUM(i.quantidade * (i.preco_unitario - i.custo_unitario)) AS lucro
        FROM itens_venda i JOIN vendas v ON v.id = i.venda_id
        JOIN produtos p ON p.id = i.produto_id
        WHERE v.cancelada = 0 AND strftime('%Y-%m', v.data) = ?
        GROUP BY p.id ORDER BY lucro DESC""", (mes,)).fetchall()
    tabela(["Produto", "Qtd.", "Receita", "Lucro", "Margem"],
           [(r["nome"], r["qtd"], brl(r["receita"]), brl(r["lucro"]),
             f"{r['lucro'] / r['receita'] * 100:.0f}%" if r["receita"] else "-")
            for r in linhas])


def melhores_clientes(con):
    print("\n-- Melhores clientes (todo o período) --")
    linhas = con.execute(f"""
        SELECT c.nome, COUNT(DISTINCT v.id) AS compras, {TOTAL_VENDA} AS total,
               MAX(v.data) AS ultima
        FROM clientes c JOIN vendas v ON v.cliente_id = c.id AND v.cancelada = 0
        JOIN itens_venda i ON i.venda_id = v.id
        GROUP BY c.id ORDER BY total DESC LIMIT 10""").fetchall()
    tabela(["Cliente", "Compras", "Total gasto", "Última compra"],
           [(r["nome"], r["compras"], brl(r["total"]), fmt_data(r["ultima"]))
            for r in linhas])


def receita_por_origem(con):
    print("\n-- Receita por origem do cliente (todo o período) --")
    linhas = con.execute(f"""
        SELECT COALESCE(NULLIF(c.origem, ''), 'Sem cadastro/origem') AS origem,
               COUNT(DISTINCT v.id) AS vendas, {TOTAL_VENDA} AS total
        FROM vendas v LEFT JOIN clientes c ON c.id = v.cliente_id
        JOIN itens_venda i ON i.venda_id = v.id
        WHERE v.cancelada = 0 GROUP BY origem ORDER BY total DESC""").fetchall()
    tabela(["Origem", "Vendas", "Receita"],
           [(r["origem"], r["vendas"], brl(r["total"])) for r in linhas])


# ----------------------------------------------------------------------------
# Programa principal
# ----------------------------------------------------------------------------
def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    try:
        menu("MENU PRINCIPAL", [
            ("Clientes", lambda: menu("CLIENTES", [
                ("Cadastrar cliente", lambda: cadastrar_cliente(con)),
                ("Listar clientes", lambda: listar_clientes(con)),
                ("Histórico de um cliente", lambda: historico_cliente(con))])),
            ("Produtos", lambda: menu("PRODUTOS", [
                ("Cadastrar produto", lambda: cadastrar_produto(con)),
                ("Listar produtos e margens", lambda: listar_produtos(con)),
                ("Atualizar custo/preço", lambda: atualizar_produto(con))])),
            ("Vendas e encomendas", lambda: menu("VENDAS", [
                ("Nova venda / encomenda", lambda: nova_venda(con)),
                ("Listar últimas vendas", lambda: listar_vendas(con)),
                ("Marcar paga / entregue / cancelar", lambda: atualizar_venda(con)),
                ("Encomendas pendentes e lista de produção", lambda: lista_producao(con))])),
            ("Estoque", lambda: menu("ESTOQUE", [
                ("Registrar compra de ingrediente", lambda: registrar_compra(con)),
                ("Ver estoque e preços esperados", lambda: ver_estoque(con)),
                ("Histórico de compras e preços", lambda: historico_compras(con)),
                ("Cadastrar ingrediente", lambda: cadastrar_ingrediente(con)),
                ("Registrar produção (dá baixa no estoque)", lambda: registrar_producao(con)),
                ("Lista de compras", lambda: lista_compras(con)),
                ("Registrar perda / desperdício", lambda: registrar_perda(con)),
                ("Excluir compra lançada por engano", lambda: excluir_compra(con))])),
            ("Receitas e custos", lambda: menu("RECEITAS", [
                ("Definir / editar receita", lambda: editar_receita(con)),
                ("Ver receita e custo estimado", lambda: ver_receita(con)),
                ("Custo estimado de todos os produtos", lambda: panorama_custos(con)),
                ("Atualizar custo cadastrado pelas receitas", lambda: atualizar_custos(con))])),
            ("Gastos", lambda: menu("GASTOS", [
                ("Registrar gasto", lambda: registrar_gasto(con)),
                ("Listar gastos do mês", lambda: listar_gastos(con))])),
            ("Relatórios e lucro", lambda: menu("RELATÓRIOS", [
                ("Resumo mensal (lucro e caixa)", lambda: resumo_mensal(con)),
                ("Lucro por produto", lambda: lucro_por_produto(con)),
                ("Melhores clientes", lambda: melhores_clientes(con)),
                ("Receita por origem do cliente", lambda: receita_por_origem(con))])),
        ])
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        con.close()
        print("Até logo!")


if __name__ == "__main__":
    main()