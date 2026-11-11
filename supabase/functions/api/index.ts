import { createClient, type SupabaseClient } from "npm:@supabase/supabase-js@2";

type JsonObject = Record<string, unknown>;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, apikey, content-type, x-client-info",
  "Access-Control-Allow-Methods": "GET, POST, PATCH, PUT, OPTIONS",
};

function respond(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...corsHeaders,
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}

function fail(status: number, message: string): Response {
  return respond({ error: message }, status);
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function positiveNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function databaseError(error: { code?: string; message?: string }): Response {
  if (error.code === "P0002") return fail(404, "Registro não encontrado");
  if (
    error.code === "P0001" || error.code === "23503" ||
    error.code === "23505" ||
    error.code === "23514" || error.code === "22P02" ||
    error.code === "22007"
  ) {
    return fail(400, error.message || "Dados inválidos");
  }
  console.error("Database request failed:", error.code ?? "unknown");
  return fail(500, "Erro ao acessar o banco de dados");
}

async function requestJson(request: Request): Promise<JsonObject | null> {
  try {
    const body: unknown = await request.json();
    return isObject(body) ? body : null;
  } catch {
    return null;
  }
}

async function loadState(db: SupabaseClient): Promise<Response> {
  const [
    productsResult,
    ingredientsResult,
    lotsResult,
    clientsResult,
    salesResult,
    saleItemsResult,
    expensesResult,
    lossesResult,
    recipesResult,
    recipeItemsResult,
  ] = await Promise.all([
    db.from("produtos").select("id,nome,custo_unitario,preco_venda,ativo")
      .order("id").range(0, 9999),
    db.from("ingredientes").select("id,nome,unidade,estoque_minimo").order("id")
      .range(0, 9999),
    db.from("lotes").select("ingrediente_id,data,restante,custo_unitario")
      .order("data").range(0, 9999),
    db.from("clientes").select("id,nome,telefone,origem,observacoes").order(
      "id",
    ).range(0, 9999),
    db.from("vendas").select("*").order("data", { ascending: false }).range(
      0,
      9999,
    ),
    db.from("itens_venda").select(
      "venda_id,produto_id,quantidade,preco_unitario,custo_unitario",
    ).range(0, 9999),
    db.from("gastos").select("id,data,categoria,descricao,valor").order(
      "data",
      { ascending: false },
    ).range(0, 9999),
    db.from("perdas").select("id,data,ingrediente_id,quantidade,valor,motivo")
      .range(0, 9999),
    db.from("receitas").select("id,produto_id,rendimento").range(0, 9999),
    db.from("itens_receita").select(
      "receita_id,ingrediente_id,quantidade,unidade",
    ).range(0, 9999),
  ]);
  const results = [
    productsResult,
    ingredientsResult,
    lotsResult,
    clientsResult,
    salesResult,
    saleItemsResult,
    expensesResult,
    lossesResult,
    recipesResult,
    recipeItemsResult,
  ];
  const failed = results.find((result) => result.error);
  if (failed?.error) return databaseError(failed.error);

  const products = productsResult.data ?? [];
  const lotsByIngredient = new Map<number, typeof lotsResult.data>();
  for (const lot of lotsResult.data ?? []) {
    const lots = lotsByIngredient.get(lot.ingrediente_id) ?? [];
    lots.push(lot);
    lotsByIngredient.set(lot.ingrediente_id, lots);
  }
  const ingredients = (ingredientsResult.data ?? []).map((ingredient) => {
    const lots = lotsByIngredient.get(ingredient.id) ?? [];
    const currentStock = lots.reduce(
      (total, lot) => total + Number(lot.restante),
      0,
    );
    const unitCost = currentStock > 0
      ? lots.reduce(
        (total, lot) =>
          total + Number(lot.restante) * Number(lot.custo_unitario),
        0,
      ) / currentStock
      : Number(lots.at(-1)?.custo_unitario ?? 0);
    return {
      id: ingredient.id,
      name: ingredient.nome,
      unit: ingredient.unidade,
      currentStock,
      minStock: ingredient.estoque_minimo,
      unitCost,
    };
  });

  const saleItemsByOrder = new Map<number, typeof saleItemsResult.data>();
  for (const item of saleItemsResult.data ?? []) {
    const items = saleItemsByOrder.get(item.venda_id) ?? [];
    items.push(item);
    saleItemsByOrder.set(item.venda_id, items);
  }
  const sales = (salesResult.data ?? []).map((sale) => ({
    id: sale.id,
    clientId: sale.cliente_id,
    type: sale.tipo,
    date: sale.data,
    deliveryDate: sale.data_entrega,
    status: sale.entregue
      ? "entregue"
      : sale.cancelada
      ? "cancelada"
      : "a_fazer",
    paymentStatus: sale.pago ? "Pago" : "A Receber",
    items: (saleItemsByOrder.get(sale.id) ?? []).map((item) => ({
      productId: item.produto_id,
      qty: item.quantidade,
      price: item.preco_unitario,
      cost: item.custo_unitario,
    })),
  }));

  const recipeItemsById = new Map<number, typeof recipeItemsResult.data>();
  for (const item of recipeItemsResult.data ?? []) {
    const items = recipeItemsById.get(item.receita_id) ?? [];
    items.push(item);
    recipeItemsById.set(item.receita_id, items);
  }
  const recipes: Record<number, unknown[]> = {};
  for (const recipe of recipesResult.data ?? []) {
    recipes[recipe.produto_id] = (recipeItemsById.get(recipe.id) ?? []).map((
      item,
    ) => ({
      ingredientId: item.ingrediente_id,
      qty: item.quantidade,
      unit: item.unidade,
    }));
  }

  return respond({
    products: products.map((product) => ({
      id: product.id,
      name: product.nome,
      unitCost: product.custo_unitario,
      price: product.preco_venda,
      ativo: product.ativo,
    })),
    ingredients,
    clients: (clientsResult.data ?? []).map((client) => ({
      id: client.id,
      name: client.nome,
      telefone: client.telefone,
      origin: client.origem,
      notes: client.observacoes,
    })),
    sales,
    expenses: (expensesResult.data ?? []).map((expense) => ({
      id: expense.id,
      date: expense.data,
      category: expense.categoria,
      description: expense.descricao,
      amount: expense.valor,
    })),
    losses: (lossesResult.data ?? []).map((loss) => ({
      id: loss.id,
      data: loss.data,
      ingredientId: loss.ingrediente_id,
      qty: loss.quantidade,
      cost: loss.valor,
      reason: loss.motivo,
    })),
    recipes,
  });
}

async function handleApi(
  request: Request,
  db: SupabaseClient,
  path: string,
): Promise<Response> {
  const method = request.method;

  if (method === "GET" && path === "/state") return loadState(db);

  if (method === "POST" && path === "/ingredients") {
    const body = await requestJson(request);
    const name = typeof body?.name === "string" ? body.name.trim() : "";
    const unit = body?.unit;
    const minStock = Number(body?.minStock ?? 0);
    if (
      !name || !["g", "ml", "un"].includes(String(unit)) ||
      !Number.isFinite(minStock) || minStock < 0
    ) {
      return fail(
        400,
        "Nome, unidade e estoque mínimo válidos são obrigatórios",
      );
    }
    const { data, error } = await db.from("ingredientes")
      .insert({ nome: name, unidade: unit, estoque_minimo: minStock })
      .select("id,nome,unidade,estoque_minimo").single();
    if (error) return databaseError(error);
    return respond({
      id: data.id,
      name: data.nome,
      unit: data.unidade,
      minStock: data.estoque_minimo,
    }, 201);
  }

  if (method === "POST" && path === "/products") {
    const body = await requestJson(request);
    const name = typeof body?.name === "string" ? body.name.trim() : "";
    const price = Number(body?.price ?? 0);
    const cost = Number(body?.cost ?? 0);
    if (
      !name || !Number.isFinite(price) || price < 0 || !Number.isFinite(cost) ||
      cost < 0
    ) {
      return fail(400, "Nome, preço e custo válidos são obrigatórios");
    }
    const { data, error } = await db.from("produtos")
      .insert({ nome: name, preco_venda: price, custo_unitario: cost })
      .select("id,nome,custo_unitario,preco_venda").single();
    if (error) return databaseError(error);
    return respond({
      id: data.id,
      name: data.nome,
      unitCost: data.custo_unitario,
      price: data.preco_venda,
    }, 201);
  }

  const historyMatch = path.match(/^\/products\/(\d+)\/price-history$/);
  if (method === "GET" && historyMatch) {
    const productId = Number(historyMatch[1]);
    const { data: product, error: productError } = await db.from("produtos")
      .select("id").eq("id", productId).maybeSingle();
    if (productError) return databaseError(productError);
    if (!product) return fail(404, "Produto não encontrado");
    const { data, error } = await db.from("historico_precos")
      .select(
        "id,preco_anterior,preco_novo,custo_anterior,custo_novo,alterado_em,origem",
      )
      .eq("produto_id", productId).order("alterado_em", { ascending: false })
      .order("id", { ascending: false })
      .range(0, 9999);
    if (error) return databaseError(error);
    return respond((data ?? []).map((entry) => ({
      id: entry.id,
      previousPrice: entry.preco_anterior,
      newPrice: entry.preco_novo,
      previousCost: entry.custo_anterior,
      newCost: entry.custo_novo,
      changedAt: entry.alterado_em,
      source: entry.origem,
    })));
  }

  const productMatch = path.match(/^\/products\/(\d+)$/);
  if (method === "PATCH" && productMatch) {
    const body = await requestJson(request);
    if (!body || !["name", "price", "cost"].some((field) => field in body)) {
      return fail(400, "Informe ao menos nome, preço ou custo");
    }
    const productId = Number(productMatch[1]);
    const { data: current, error: currentError } = await db.from("produtos")
      .select("id,nome,preco_venda,custo_unitario").eq("id", productId)
      .maybeSingle();
    if (currentError) return databaseError(currentError);
    if (!current) return fail(404, "Produto não encontrado");
    const name = "name" in body
      ? (typeof body.name === "string" ? body.name.trim() : "")
      : current.nome;
    const price = "price" in body
      ? Number(body.price)
      : Number(current.preco_venda);
    const cost = "cost" in body
      ? Number(body.cost)
      : Number(current.custo_unitario);
    if (
      !name || !Number.isFinite(price) || price < 0 || !Number.isFinite(cost) ||
      cost < 0
    ) {
      return fail(400, "Nome, preço e custo devem ser válidos e não negativos");
    }
    const { data, error } = await db.rpc("update_product_with_price_history", {
      p_product_id: productId,
      p_name: name,
      p_price: price,
      p_cost: cost,
      p_source: typeof body.source === "string" ? body.source : "api",
    });
    if (error) return databaseError(error);
    const updated = data as {
      id: number;
      nome: string;
      custo_unitario: number;
      preco_venda: number;
    } | null;
    if (!updated) return fail(404, "Produto não encontrado");
    return respond({
      id: updated.id,
      name: updated.nome,
      unitCost: updated.custo_unitario,
      price: updated.preco_venda,
    });
  }

  if (method === "POST" && path === "/clients") {
    const body = await requestJson(request);
    const name = typeof body?.name === "string" ? body.name.trim() : "";
    if (!name) return fail(400, "Nome do cliente é obrigatório");
    const { data, error } = await db.from("clientes").insert({
      nome: name,
      telefone: body?.phone ?? null,
      origem: body?.origin ?? null,
      observacoes: body?.notes ?? null,
    }).select("id,nome,telefone,origem,observacoes").single();
    if (error) return databaseError(error);
    return respond({
      id: data.id,
      name: data.nome,
      telefone: data.telefone,
      origin: data.origem,
      notes: data.observacoes,
    }, 201);
  }

  if (method === "POST" && path === "/orders") {
    const body = await requestJson(request);
    if (!body) return fail(400, "Corpo do pedido inválido");
    const clientId = positiveNumber(body?.clientId);
    const items = body?.items;
    if (!clientId || !Array.isArray(items) || items.length === 0) {
      return fail(400, "Cliente e itens do pedido são obrigatórios");
    }
    const normalizedItems: JsonObject[] = [];
    for (const item of items) {
      if (!isObject(item)) return fail(400, "Item de pedido inválido");
      const productId = positiveNumber(item.productId);
      const quantity = positiveNumber(item.qty);
      if (!productId || !quantity || !Number.isInteger(quantity)) {
        return fail(
          400,
          "Cada item deve informar produto e quantidade inteira positiva",
        );
      }
      normalizedItems.push({
        productId,
        qty: quantity,
        ...(item.price !== undefined ? { price: item.price } : {}),
      });
    }
    const { data, error } = await db.rpc("create_order", {
      p_client_id: clientId,
      p_type: typeof body.type === "string" ? body.type : "Encomenda",
      p_date: typeof body.date === "string" ? body.date : null,
      p_delivery_date: typeof body.deliveryDate === "string"
        ? body.deliveryDate
        : null,
      p_items: normalizedItems,
    });
    if (error) return databaseError(error);
    return respond(data, 201);
  }

  const orderMatch = path.match(/^\/orders\/(\d+)$/);
  if (method === "PATCH" && orderMatch) {
    const body = await requestJson(request);
    if (!body) return fail(400, "Corpo da requisição inválido");
    const updates: Record<string, boolean> = {};
    if ("paid" in body) updates.pago = Boolean(body.paid);
    if ("delivered" in body) updates.entregue = Boolean(body.delivered);
    if ("canceled" in body) updates.cancelada = Boolean(body.canceled);
    const orderId = Number(orderMatch[1]);
    if (Object.keys(updates).length) {
      const { data: updated, error } = await db.from("vendas").update(updates)
        .eq("id", orderId).select("id").maybeSingle();
      if (error) return databaseError(error);
      if (!updated) return fail(404, "Pedido não encontrado");
    }
    const { data: order, error: orderError } = await db.from("vendas")
      .select("id,cliente_id,tipo,data,data_entrega,pago,entregue,cancelada")
      .eq("id", orderId).maybeSingle();
    if (orderError) return databaseError(orderError);
    if (!order) return fail(404, "Pedido não encontrado");
    const { data: items, error: itemsError } = await db.from("itens_venda")
      .select("produto_id,quantidade,preco_unitario,custo_unitario").eq(
        "venda_id",
        orderId,
      );
    if (itemsError) return databaseError(itemsError);
    return respond({
      id: order.id,
      clientId: order.cliente_id,
      type: order.tipo,
      date: order.data,
      deliveryDate: order.data_entrega,
      status: order.entregue
        ? "entregue"
        : order.cancelada
        ? "cancelada"
        : "a_fazer",
      paymentStatus: order.pago ? "Pago" : "A Receber",
      items: (items ?? []).map((item) => ({
        productId: item.produto_id,
        qty: item.quantidade,
        price: item.preco_unitario,
        cost: item.custo_unitario,
      })),
    });
  }

  if (method === "POST" && path === "/recipes") {
    const body = await requestJson(request);
    const productId = positiveNumber(body?.productId);
    const yieldCount = positiveNumber(body?.rendimento);
    if (
      !productId || !yieldCount || !Number.isInteger(yieldCount) ||
      !Array.isArray(body?.items) || body.items.length === 0
    ) {
      return fail(400, "Produto, rendimento inteiro e itens são obrigatórios");
    }
    const { data, error } = await db.rpc("save_recipe", {
      p_product_id: productId,
      p_yield: yieldCount,
      p_items: body.items,
      p_update: false,
    });
    if (error) return databaseError(error);
    return respond({ id: data, productId }, 201);
  }

  const recipeMatch = path.match(/^\/recipes\/(\d+)$/);
  if (method === "PUT" && recipeMatch) {
    const body = await requestJson(request);
    const productId = Number(recipeMatch[1]);
    const yieldCount = positiveNumber(body?.rendimento);
    if (
      !yieldCount || !Number.isInteger(yieldCount) ||
      !Array.isArray(body?.items) || body.items.length === 0
    ) {
      return fail(400, "Rendimento inteiro e itens são obrigatórios");
    }
    const { data, error } = await db.rpc("save_recipe", {
      p_product_id: productId,
      p_yield: yieldCount,
      p_items: body.items,
      p_update: true,
    });
    if (error) return databaseError(error);
    return respond({ id: data, productId });
  }

  return fail(404, "Rota não encontrada");
}

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") {
    return new Response(null, { headers: corsHeaders });
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const supabaseKey = Deno.env.get("SUPABASE_ANON_KEY") ??
    Deno.env.get("SUPABASE_PUBLISHABLE_KEY");
  if (!supabaseUrl || !supabaseKey) {
    console.error("Supabase URL or publishable key is not configured");
    return fail(503, "API não configurada");
  }

  const authorization = request.headers.get("Authorization");
  const token = authorization?.match(/^Bearer\s+(.+)$/i)?.[1];
  if (!token) return fail(401, "Autenticação obrigatória");

  const db = createClient(supabaseUrl, supabaseKey, {
    global: { headers: { Authorization: `Bearer ${token}` } },
    auth: { autoRefreshToken: false, persistSession: false },
  });
  const { data: authData, error: authError } = await db.auth.getUser(token);
  if (authError || !authData.user?.email) {
    return fail(401, "Sessão inválida ou expirada");
  }

  const { data: access, error: accessError } = await db.from("app_access")
    .select("email")
    .eq("email", authData.user.email.toLowerCase())
    .maybeSingle();
  if (accessError) return databaseError(accessError);
  if (!access) return fail(403, "Esta conta não está autorizada");

  const routePrefix = "/functions/v1/api";
  const pathname = new URL(request.url).pathname;
  const routePath = pathname.includes(routePrefix)
    ? pathname.slice(pathname.lastIndexOf(routePrefix) + routePrefix.length) ||
      "/"
    : pathname.startsWith("/api/")
    ? pathname.slice("/api".length)
    : pathname || "/";

  try {
    return await handleApi(request, db, routePath);
  } catch (error) {
    console.error(
      "Unhandled API error:",
      error instanceof Error ? error.message : "unknown",
    );
    return fail(500, "Erro interno da API");
  }
});
