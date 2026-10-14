#!/usr/bin/env python3
"""Testes simples de funções não-interativas do programa.

Executa verificações de `preco_esperado`, `consumir` e `custo_receita`.
"""
from main import SCHEMA, preco_esperado, estoque_atual, consumir, custo_receita
import sqlite3


def run():
    con = sqlite3.connect(':memory:')
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)

    # criar um ingrediente com dois lotes (FIFO)
    cur = con.execute("INSERT INTO ingredientes (nome, unidade, estoque_minimo) VALUES (?,?,?)",
                      ("Farinha", "g", 0))
    ing_id = cur.lastrowid

    # lote antigo: 1000g a R$0.05/g -> custo_total 50
    con.execute("INSERT INTO lotes (ingrediente_id, data, local, quantidade, restante, preco_total, custo_unitario) VALUES (?,?,?,?,?,?,?)",
                (ing_id, '2026-01-01', 'Loja A', 1000.0, 1000.0, 50.0, 0.05))
    # lote mais recente: 500g a R$0.06/g -> custo_total 30
    con.execute("INSERT INTO lotes (ingrediente_id, data, local, quantidade, restante, preco_total, custo_unitario) VALUES (?,?,?,?,?,?,?)",
                (ing_id, '2026-02-01', 'Loja B', 500.0, 500.0, 30.0, 0.06))

    # preco esperado é média ponderada: (1000*0.05 + 500*0.06) / 1500
    preco, origem = preco_esperado(con, ing_id)
    assert origem == 'estoque'
    assert abs(preco - ((1000*0.05 + 500*0.06) / 1500)) < 1e-9

    # consumir 1200g: deve tirar 1000 do primeiro lote e 200 do segundo
    custo = consumir(con, ing_id, 1200.0)
    expected = 1000.0 * 0.05 + 200.0 * 0.06
    assert abs(custo - expected) < 1e-9

    # verificar estoques restantes
    r = con.execute("SELECT restante, custo_unitario FROM lotes ORDER BY data, id").fetchall()
    assert abs(r[0]['restante'] - 0.0) < 1e-6
    assert abs(r[1]['restante'] - 300.0) < 1e-6

    # testar custo_receita: criar produto, receita e item de receita
    cur = con.execute("INSERT INTO produtos (nome, custo_unitario, preco_venda) VALUES (?,?,?)",
                      ("Bolo", 0.0, 20.0))
    prod_id = cur.lastrowid
    cur = con.execute("INSERT INTO receitas (produto_id, rendimento) VALUES (?,?)", (prod_id, 1))
    rec_id = cur.lastrowid
    # receita usa 200g de farinha por unidade
    con.execute("INSERT INTO itens_receita (receita_id, ingrediente_id, quantidade) VALUES (?,?,?)",
                (rec_id, ing_id, 200.0))

    c = custo_receita(con, prod_id)
    assert c is not None
    # por_unidade deve usar o preço esperado ATUAL (após o consumo)
    preco_atual, _ = preco_esperado(con, ing_id)
    assert preco_atual is not None
    assert abs(c['por_unidade'] - preco_atual * 200.0) < 1e-9

    print("Todos os testes passaram.")


if __name__ == '__main__':
    run()
