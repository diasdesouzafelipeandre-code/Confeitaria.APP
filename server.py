from flask import Flask, jsonify, send_from_directory, request, abort, current_app
from confeitaria import repository
from flask import Blueprint
import sqlite3
import math

bp = Blueprint('api', __name__)


def create_app():
    app = Flask(__name__, static_folder='static', static_url_path='')
    app.register_blueprint(bp)
    return app

# Unit normalization and conversion factors used across endpoints
UNIT_NORMALIZE = { 'kg':'g', 'g':'g', 'l':'ml', 'ml':'ml', 'un':'un' }
FACTORS = {('kg','g'):1000, ('g','kg'):1/1000, ('l','ml'):1000, ('ml','l'):1/1000}

def normalize_unit(u):
    if not u: return None
    uu = str(u).lower()
    return UNIT_NORMALIZE.get(uu, uu)

def can_convert(frm, to):
    if not frm or not to: return False
    a = normalize_unit(frm); b = normalize_unit(to)
    return a == b or (a, b) in FACTORS

def convert_qtd(value, frm, to):
    a = normalize_unit(frm); b = normalize_unit(to)
    if a == b: return float(value)
    f = FACTORS.get((a, b))
    return float(value) * f if f is not None else None


@bp.route('/')
def index():
    return send_from_directory(current_app.static_folder, 'index.html')


@bp.route('/api/state')
def api_state():
    con = repository.get_db()

    # Produtos
    prods = [dict(p) for p in con.execute("SELECT id, nome AS name, custo_unitario as unitCost, preco_venda as price, ativo FROM produtos").fetchall()]

    # Ingredientes: compute current stock and unit cost from lotes
    ings = []
    for r in con.execute("SELECT * FROM ingredientes").fetchall():
        rid = r['id']
        lots = con.execute("SELECT restante, custo_unitario FROM lotes WHERE ingrediente_id = ?", (rid,)).fetchall()
        current = sum([l['restante'] for l in lots])
        if current > 0:
            # weighted avg
            total = sum([l['restante'] * l['custo_unitario'] for l in lots])
            unit_cost = total / current
        else:
            # fallback: last purchase
            last = con.execute("SELECT custo_unitario FROM lotes WHERE ingrediente_id = ? ORDER BY data DESC LIMIT 1", (rid,)).fetchone()
            unit_cost = last['custo_unitario'] if last else 0
        ings.append({
            'id': rid,
            'name': r['nome'],
            'unit': r['unidade'],
            'currentStock': current,
            'minStock': r['estoque_minimo'],
            'unitCost': unit_cost
        })

    # Clientes
    clients = [dict(c) for c in con.execute("SELECT id, nome AS name, telefone, origem AS origin, observacoes as notes FROM clientes").fetchall()]

    # Vendas (brief)
    sales = []
    for v in con.execute("SELECT * FROM vendas").fetchall():
        items = [dict(i) for i in con.execute("SELECT produto_id as productId, quantidade as qty, preco_unitario as price, custo_unitario as cost FROM itens_venda WHERE venda_id = ?", (v['id'],)).fetchall()]
        sales.append({
            'id': v['id'],
            'clientId': v['cliente_id'],
            'type': v['tipo'],
            'date': v['data'],
            'deliveryDate': v['data_entrega'],
            'status': 'entregue' if v['entregue'] else ('cancelada' if v['cancelada'] else 'a_fazer'),
            'paymentStatus': 'Pago' if v['pago'] else 'A Receber',
            'items': items
        })

    # Gastos
    expenses = [dict(e) for e in con.execute("SELECT id, data as date, categoria as category, descricao as description, valor as amount FROM gastos ORDER BY data DESC").fetchall()]

    # Perdas
    losses = []
    for l in con.execute("SELECT p.id, p.data, p.ingrediente_id as ingredientId, p.quantidade as qty, p.valor as cost, p.motivo as reason FROM perdas p").fetchall():
        losses.append(dict(l))

    # Receitas (recipes)
    recipes = {}
    for r in con.execute("SELECT r.id, r.produto_id as productId, r.rendimento as yield FROM receitas r").fetchall():
        items = [dict(it) for it in con.execute("SELECT ingrediente_id as ingredientId, quantidade as qty, unidade as unit FROM itens_receita WHERE receita_id = ?", (r['id'],)).fetchall()]
        recipes[r['productId']] = items

    con.close()
    return jsonify({
        'products': prods,
        'ingredients': ings,
        'clients': clients,
        'sales': sales,
        'expenses': expenses,
        'losses': losses,
        'recipes': recipes
    })



@bp.route('/api/ingredients', methods=['POST'])
def api_create_ingredient():
    data = request.get_json() or {}
    name = data.get('name')
    unit = data.get('unit')
    min_stock = data.get('minStock', 0)
    if not name or unit not in ('g', 'kg', 'ml', 'l', 'un'):
        return abort(400, 'name and unit (g|ml|un) required')
    con = repository.get_db()
    try:
        cur = con.execute("INSERT INTO ingredientes (nome, unidade, estoque_minimo) VALUES (?,?,?)", (name, unit, min_stock))
        con.commit()
        nid = cur.lastrowid
        row = con.execute("SELECT id, nome AS name, unidade as unit, estoque_minimo as minStock FROM ingredientes WHERE id = ?", (nid,)).fetchone()
        return jsonify(dict(row)), 201
    finally:
        con.close()


@bp.route('/api/products', methods=['POST'])
def api_create_product():
    data = request.get_json() or {}
    name = data.get('name')
    price = float(data.get('price', 0))
    cost = float(data.get('cost', 0))
    if not name:
        return abort(400, 'name required')
    con = repository.get_db()
    try:
        cur = con.execute("INSERT INTO produtos (nome, custo_unitario, preco_venda) VALUES (?,?,?)", (name, cost, price))
        con.commit()
        nid = cur.lastrowid
        row = con.execute("SELECT id, nome AS name, custo_unitario as unitCost, preco_venda as price FROM produtos WHERE id = ?", (nid,)).fetchone()
        return jsonify(dict(row)), 201
    except sqlite3.IntegrityError as e:
        con.rollback()
        return abort(400, str(e))
    finally:
        con.close()


@bp.route('/api/products/<int:product_id>', methods=['PATCH'])
def api_update_product(product_id):
    data = request.get_json() or {}
    if not any(field in data for field in ('name', 'price', 'cost')):
        return abort(400, 'at least one of name, price or cost is required')

    con = repository.get_db()
    try:
        current = con.execute(
            'SELECT id, nome, preco_venda, custo_unitario FROM produtos WHERE id = ?',
            (product_id,),
        ).fetchone()
        if not current:
            return abort(404, 'product not found')

        name = current['nome']
        if 'name' in data:
            if not isinstance(data['name'], str) or not data['name'].strip():
                return abort(400, 'name must not be empty')
            name = data['name'].strip()

        price = current['preco_venda']
        cost = current['custo_unitario']
        try:
            if 'price' in data:
                price = float(data['price'])
            if 'cost' in data:
                cost = float(data['cost'])
        except (TypeError, ValueError):
            return abort(400, 'price and cost must be valid non-negative numbers')
        if not math.isfinite(price) or price < 0 or not math.isfinite(cost) or cost < 0:
            return abort(400, 'price and cost must be valid non-negative numbers')

        price_changed = price != current['preco_venda']
        cost_changed = cost != current['custo_unitario']
        with con:
            con.execute(
                'UPDATE produtos SET nome = ?, preco_venda = ?, custo_unitario = ? WHERE id = ?',
                (name, price, cost, product_id),
            )
            if price_changed or cost_changed:
                con.execute(
                    '''INSERT INTO historico_precos
                       (produto_id, preco_anterior, preco_novo, custo_anterior, custo_novo, origem)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (product_id, current['preco_venda'], price,
                     current['custo_unitario'], cost, data.get('source') or 'api'),
                )

        updated = con.execute(
            'SELECT id, nome AS name, custo_unitario AS unitCost, preco_venda AS price '
            'FROM produtos WHERE id = ?',
            (product_id,),
        ).fetchone()
        return jsonify(dict(updated))
    except sqlite3.IntegrityError as exc:
        con.rollback()
        return abort(400, str(exc))
    finally:
        con.close()


@bp.route('/api/products/<int:product_id>/price-history', methods=['GET'])
def api_product_price_history(product_id):
    con = repository.get_db()
    try:
        product = con.execute('SELECT id FROM produtos WHERE id = ?', (product_id,)).fetchone()
        if not product:
            return abort(404, 'product not found')
        history = con.execute(
            '''SELECT id, preco_anterior AS previousPrice, preco_novo AS newPrice,
                      custo_anterior AS previousCost, custo_novo AS newCost,
                      alterado_em AS changedAt, origem AS source
               FROM historico_precos WHERE produto_id = ?
               ORDER BY alterado_em DESC, id DESC''',
            (product_id,),
        ).fetchall()
        return jsonify([dict(row) for row in history])
    finally:
        con.close()


@bp.route('/api/clients', methods=['POST'])
def api_create_client():
    data = request.get_json() or {}
    name = data.get('name')
    phone = data.get('phone')
    origin = data.get('origin')
    notes = data.get('notes')
    if not name:
        return abort(400, 'name required')
    con = repository.get_db()
    try:
        cur = con.execute("INSERT INTO clientes (nome, telefone, origem, observacoes) VALUES (?,?,?,?)", (name, phone, origin, notes))
        con.commit()
        nid = cur.lastrowid
        row = con.execute("SELECT id, nome AS name, telefone, origem AS origin, observacoes as notes FROM clientes WHERE id = ?", (nid,)).fetchone()
        return jsonify(dict(row)), 201
    finally:
        con.close()


@bp.route('/api/orders', methods=['POST'])
def api_create_order():
    data = request.get_json() or {}
    client_id = data.get('clientId')
    tipo = data.get('type', 'Encomenda')
    date_ = data.get('date') or None
    delivery = data.get('deliveryDate') or None
    items = data.get('items', [])
    if not client_id or not items:
        return abort(400, 'clientId and items required')
    con = repository.get_db()
    try:
        # helper: estimate max units producible for a given product using recipe + ingredient stock
        # reuse module-level normalize/can_convert/convert_qtd

        def estimate_max_units(product_id):
            # find recipe
            r = con.execute('SELECT id, rendimento FROM receitas WHERE produto_id = ?', (product_id,)).fetchone()
            if not r:
                return None
            receita_id = r['id']
            rendimento = r['rendimento']
            if not rendimento or rendimento <= 0:
                return None
            items = con.execute('SELECT ingrediente_id, quantidade, unidade FROM itens_receita WHERE receita_id = ?', (receita_id,)).fetchall()
            if not items:
                return None
            max_possible = float('inf')
            for it in items:
                ing_id = it['ingrediente_id']
                need = float(it['quantidade'])
                item_unit = it['unidade'] if 'unidade' in it.keys() else None
                ing = con.execute('SELECT id, unidade FROM ingredientes WHERE id = ?', (ing_id,)).fetchone()
                if not ing:
                    return None
                ing_unit = ing['unidade']
                # compute total available for ingredient
                total = con.execute('SELECT COALESCE(SUM(restante),0) as tot FROM lotes WHERE ingrediente_id = ?', (ing_id,)).fetchone()['tot']
                # convert need to ingredient unit if units provided
                need_in_ing_unit = need
                if item_unit and item_unit != ing_unit:
                    if can_convert(item_unit, ing_unit):
                        conv = convert_qtd(need, item_unit, ing_unit)
                        if conv is None:
                            return None
                        need_in_ing_unit = conv
                    else:
                        # incompatible units
                        return None
                # need is quantity per recipe; compute need per product unit
                need_per_unit = need_in_ing_unit / rendimento
                if need_per_unit <= 0:
                    return None
                possible = math.floor((total + 0.0) / need_per_unit)
                max_possible = min(max_possible, possible)
            return int(max_possible) if max_possible != float('inf') else None

        # insert venda
        # validate requested items against recipe/stock before inserting
        for it in items:
            pid = it.get('productId')
            qty = int(it.get('qty', 0))
            maxu = estimate_max_units(pid)
            if maxu is not None and qty > maxu:
                con.close()
                return abort(400, f'Requested qty for product {pid} exceeds available stock ({qty} > {maxu})')

        cur = con.execute("INSERT INTO vendas (cliente_id, data, tipo, data_entrega, pago, entregue, cancelada) VALUES (?,?,?,?,0,0,0)",
                          (client_id, date_ or str(__import__('datetime').date.today()), tipo, delivery))
        venda_id = cur.lastrowid
        # insert items
        for it in items:
            pid = it.get('productId')
            qty = int(it.get('qty', 0))
            if not pid or qty <= 0:
                con.rollback()
                return abort(400, 'each item needs productId and qty>0')
            # lookup product prices
            p = con.execute('SELECT preco_venda, custo_unitario FROM produtos WHERE id = ?', (pid,)).fetchone()
            if not p:
                con.rollback()
                return abort(400, f'product {pid} not found')
            preco_unit = float(it.get('price') or p['preco_venda'] or 0)
            custo_unit = float(p['custo_unitario'] or 0)
            con.execute('INSERT INTO itens_venda (venda_id, produto_id, quantidade, preco_unitario, custo_unitario) VALUES (?,?,?,?,?)',
                        (venda_id, pid, qty, preco_unit, custo_unit))
        con.commit()
        # return created order
        v = con.execute('SELECT * FROM vendas WHERE id = ?', (venda_id,)).fetchone()
        items_db = [dict(i) for i in con.execute('SELECT produto_id as productId, quantidade as qty, preco_unitario as price, custo_unitario as cost FROM itens_venda WHERE venda_id = ?', (venda_id,)).fetchall()]
        return jsonify({
            'id': v['id'], 'clientId': v['cliente_id'], 'type': v['tipo'], 'date': v['data'], 'deliveryDate': v['data_entrega'], 'status': 'a_fazer', 'items': items_db
        }), 201
    finally:
        con.close()


@bp.route('/api/orders/<int:order_id>', methods=['PATCH'])
def api_update_order(order_id):
    data = request.get_json() or {}
    con = repository.get_db()
    try:
        v = con.execute('SELECT * FROM vendas WHERE id = ?', (order_id,)).fetchone()
        if not v:
            return abort(404, 'order not found')
        updates = []
        params = []
        if 'paid' in data:
            updates.append('pago = ?'); params.append(1 if data.get('paid') else 0)
        if 'delivered' in data:
            updates.append('entregue = ?'); params.append(1 if data.get('delivered') else 0)
        if 'canceled' in data:
            updates.append('cancelada = ?'); params.append(1 if data.get('canceled') else 0)
        if updates:
            params.append(order_id)
            con.execute(f"UPDATE vendas SET {', '.join(updates)} WHERE id = ?", params)
            con.commit()
        v2 = con.execute('SELECT * FROM vendas WHERE id = ?', (order_id,)).fetchone()
        items_db = [dict(i) for i in con.execute('SELECT produto_id as productId, quantidade as qty, preco_unitario as price, custo_unitario as cost FROM itens_venda WHERE venda_id = ?', (order_id,)).fetchall()]
        status = 'entregue' if v2['entregue'] else ('cancelada' if v2['cancelada'] else 'a_fazer')
        return jsonify({ 'id': v2['id'], 'clientId': v2['cliente_id'], 'type': v2['tipo'], 'date': v2['data'], 'deliveryDate': v2['data_entrega'], 'status': status, 'paymentStatus': 'Pago' if v2['pago'] else 'A Receber', 'items': items_db })
    finally:
        con.close()


@bp.route('/api/recipes', methods=['POST'])
def api_create_recipe():
    data = request.get_json() or {}
    product_id = data.get('productId')
    rendimento = data.get('rendimento')
    items = data.get('items', [])
    if not product_id or not rendimento or not items:
        return abort(400, 'productId, rendimento and items required')
    con = repository.get_db()
    try:
        # ensure product exists
        prod = con.execute('SELECT id FROM produtos WHERE id = ?', (product_id,)).fetchone()
        if not prod:
            return abort(400, 'product not found')
        # check existing
        exists = con.execute('SELECT id FROM receitas WHERE produto_id = ?', (product_id,)).fetchone()
        if exists:
            return abort(400, 'recipe already exists; use PUT to update')
        # validate items
        for it in items:
            iid = it.get('ingredientId')
            qtd = it.get('quantidade') or it.get('qty')
            unit = it.get('unidade') or it.get('unit')
            if not iid or not qtd or float(qtd) <= 0:
                return abort(400, 'each item needs ingredientId and positive quantidade')
            ing = con.execute('SELECT id, unidade FROM ingredientes WHERE id = ?', (iid,)).fetchone()
            if not ing:
                return abort(400, f'ingredient {iid} not found')
            # if unit provided, ensure convertible to ingredient unit or equal
            if unit and not can_convert(unit, ing['unidade']):
                return abort(400, f'unit {unit} incompatible with ingredient {iid} unit {ing["unidade"]}')
        # create recipe
        cur = con.execute('INSERT INTO receitas (produto_id, rendimento) VALUES (?,?)', (product_id, rendimento))
        rid = cur.lastrowid
        # insert items
        for it in items:
            iid = it.get('ingredientId')
            qtd = float(it.get('quantidade') or it.get('qty'))
            unit = it.get('unidade') or it.get('unit') or None
            con.execute('INSERT INTO itens_receita (receita_id, ingrediente_id, quantidade, unidade) VALUES (?,?,?,?)', (rid, iid, qtd, unit))
        con.commit()
        return jsonify({'id': rid, 'productId': product_id}), 201
    finally:
        con.close()


@bp.route('/api/recipes/<int:product_id>', methods=['PUT'])
def api_update_recipe(product_id):
    data = request.get_json() or {}
    rendimento = data.get('rendimento')
    items = data.get('items', [])
    if not rendimento or not items:
        return abort(400, 'rendimento and items required')
    con = repository.get_db()
    try:
        prod = con.execute('SELECT id FROM produtos WHERE id = ?', (product_id,)).fetchone()
        if not prod:
            return abort(400, 'product not found')
        r = con.execute('SELECT id FROM receitas WHERE produto_id = ?', (product_id,)).fetchone()
        if not r:
            return abort(404, 'recipe not found')
        rid = r['id']
        # validate items
        for it in items:
            iid = it.get('ingredientId')
            qtd = it.get('quantidade') or it.get('qty')
            unit = it.get('unidade') or it.get('unit')
            if not iid or not qtd or float(qtd) <= 0:
                return abort(400, 'each item needs ingredientId and positive quantidade')
            ing = con.execute('SELECT id, unidade FROM ingredientes WHERE id = ?', (iid,)).fetchone()
            if not ing:
                return abort(400, f'ingredient {iid} not found')
            if unit and not can_convert(unit, ing['unidade']):
                return abort(400, f'unit {unit} incompatible with ingredient {iid} unit {ing["unidade"]}')
        # update receita
        con.execute('UPDATE receitas SET rendimento = ? WHERE id = ?', (rendimento, rid))
        con.execute('DELETE FROM itens_receita WHERE receita_id = ?', (rid,))
        for it in items:
            iid = it.get('ingredientId')
            qtd = float(it.get('quantidade') or it.get('qty'))
            unit = it.get('unidade') or it.get('unit') or None
            con.execute('INSERT INTO itens_receita (receita_id, ingrediente_id, quantidade, unidade) VALUES (?,?,?,?)', (rid, iid, qtd, unit))
        con.commit()
        return jsonify({'id': rid, 'productId': product_id})
    finally:
        con.close()


if __name__ == '__main__':
    app = create_app()
    print('Serving on http://127.0.0.1:5000')
    app.run(debug=True)
