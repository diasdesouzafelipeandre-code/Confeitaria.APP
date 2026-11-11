CREATE TABLE IF NOT EXISTS public.app_access (
    email TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT app_access_email_lowercase CHECK (email = lower(email))
);

ALTER TABLE public.app_access ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS app_access_read_own_email ON public.app_access;
CREATE POLICY app_access_read_own_email
    ON public.app_access
    FOR SELECT
    TO authenticated
    USING (email = lower(auth.jwt() ->> 'email'));

REVOKE ALL ON TABLE public.app_access FROM anon, authenticated;
GRANT SELECT ON TABLE public.app_access TO authenticated;

CREATE OR REPLACE FUNCTION public.has_app_access()
RETURNS BOOLEAN
LANGUAGE SQL
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.app_access
        WHERE email = lower(auth.jwt() ->> 'email')
    );
$$;

REVOKE ALL ON FUNCTION public.has_app_access() FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.has_app_access() TO authenticated;

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'clientes',
        'produtos',
        'vendas',
        'itens_venda',
        'gastos',
        'ingredientes',
        'lotes',
        'receitas',
        'itens_receita',
        'producoes',
        'perdas',
        'configuracoes',
        'historico_precos'
    ] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('DROP POLICY IF EXISTS app_access_all ON public.%I', table_name);
        EXECUTE format(
            'CREATE POLICY app_access_all ON public.%I FOR ALL TO authenticated USING (public.has_app_access()) WITH CHECK (public.has_app_access())',
            table_name
        );
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.%I TO authenticated',
            table_name
        );
    END LOOP;
END;
$$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;

CREATE OR REPLACE FUNCTION public.update_product_with_price_history(
    p_product_id BIGINT,
    p_name TEXT,
    p_price DOUBLE PRECISION,
    p_cost DOUBLE PRECISION,
    p_source TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
    old_product public.produtos%ROWTYPE;
    new_product public.produtos%ROWTYPE;
BEGIN
    SELECT * INTO old_product
    FROM public.produtos
    WHERE id = p_product_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'product not found' USING ERRCODE = 'P0002';
    END IF;

    UPDATE public.produtos
    SET nome = p_name, preco_venda = p_price, custo_unitario = p_cost
    WHERE id = p_product_id
    RETURNING * INTO new_product;

    IF old_product.preco_venda <> new_product.preco_venda
       OR old_product.custo_unitario <> new_product.custo_unitario THEN
        INSERT INTO public.historico_precos (
            produto_id, preco_anterior, preco_novo,
            custo_anterior, custo_novo, origem
        ) VALUES (
            p_product_id, old_product.preco_venda, new_product.preco_venda,
            old_product.custo_unitario, new_product.custo_unitario, p_source
        );
    END IF;

    RETURN jsonb_build_object(
        'id', new_product.id,
        'nome', new_product.nome,
        'custo_unitario', new_product.custo_unitario,
        'preco_venda', new_product.preco_venda
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.create_order(
    p_client_id BIGINT,
    p_type TEXT,
    p_date DATE,
    p_delivery_date DATE,
    p_items JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
    order_id BIGINT;
    item JSONB;
    item_product_id BIGINT;
    item_quantity INTEGER;
    product public.produtos%ROWTYPE;
    recipe public.receitas%ROWTYPE;
    recipe_item RECORD;
    need_quantity DOUBLE PRECISION;
    available_stock DOUBLE PRECISION;
    required_per_unit DOUBLE PRECISION;
    producible INTEGER;
    maximum_units INTEGER;
    has_convertible_recipe BOOLEAN;
    inserted_items JSONB;
BEGIN
    IF p_items IS NULL OR jsonb_typeof(p_items) <> 'array' OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'order items are required' USING ERRCODE = 'P0001';
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        item_product_id := (item ->> 'productId')::BIGINT;
        item_quantity := (item ->> 'qty')::INTEGER;
        IF item_product_id IS NULL OR item_quantity IS NULL OR item_quantity <= 0 THEN
            RAISE EXCEPTION 'invalid order item' USING ERRCODE = 'P0001';
        END IF;

        SELECT * INTO product FROM public.produtos WHERE id = item_product_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'product not found' USING ERRCODE = 'P0001';
        END IF;

        SELECT * INTO recipe FROM public.receitas WHERE produto_id = item_product_id;
        IF FOUND AND recipe.rendimento > 0 THEN
            maximum_units := NULL;
            has_convertible_recipe := TRUE;
            FOR recipe_item IN
                SELECT ir.ingrediente_id, ir.quantidade, ir.unidade, i.unidade AS ingrediente_unidade
                FROM public.itens_receita ir
                JOIN public.ingredientes i ON i.id = ir.ingrediente_id
                WHERE ir.receita_id = recipe.id
            LOOP
                need_quantity := recipe_item.quantidade;
                IF recipe_item.unidade IS NOT NULL
                   AND recipe_item.unidade <> recipe_item.ingrediente_unidade THEN
                    IF recipe_item.unidade = 'kg' AND recipe_item.ingrediente_unidade = 'g' THEN
                        need_quantity := need_quantity * 1000;
                    ELSIF recipe_item.unidade = 'g' AND recipe_item.ingrediente_unidade = 'kg' THEN
                        need_quantity := need_quantity / 1000;
                    ELSIF recipe_item.unidade = 'l' AND recipe_item.ingrediente_unidade = 'ml' THEN
                        need_quantity := need_quantity * 1000;
                    ELSIF recipe_item.unidade = 'ml' AND recipe_item.ingrediente_unidade = 'l' THEN
                        need_quantity := need_quantity / 1000;
                    ELSE
                        has_convertible_recipe := FALSE;
                        EXIT;
                    END IF;
                END IF;

                SELECT COALESCE(SUM(restante), 0) INTO available_stock
                FROM public.lotes
                WHERE ingrediente_id = recipe_item.ingrediente_id;

                required_per_unit := need_quantity / recipe.rendimento;
                IF required_per_unit <= 0 THEN
                    has_convertible_recipe := FALSE;
                    EXIT;
                END IF;
                producible := FLOOR(available_stock / required_per_unit)::INTEGER;
                maximum_units := CASE
                    WHEN maximum_units IS NULL THEN producible
                    ELSE LEAST(maximum_units, producible)
                END;
            END LOOP;

            IF has_convertible_recipe AND maximum_units IS NOT NULL
               AND item_quantity > maximum_units THEN
                RAISE EXCEPTION 'requested quantity exceeds available stock for product %', item_product_id
                    USING ERRCODE = 'P0001';
            END IF;
        END IF;
    END LOOP;

    INSERT INTO public.vendas (cliente_id, data, tipo, data_entrega)
    VALUES (p_client_id, COALESCE(p_date, CURRENT_DATE), COALESCE(p_type, 'Encomenda'), p_delivery_date)
    RETURNING id INTO order_id;

    FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        item_product_id := (item ->> 'productId')::BIGINT;
        item_quantity := (item ->> 'qty')::INTEGER;
        SELECT * INTO product FROM public.produtos WHERE id = item_product_id;
        INSERT INTO public.itens_venda (
            venda_id, produto_id, quantidade, preco_unitario, custo_unitario
        ) VALUES (
            order_id,
            item_product_id,
            item_quantity,
            COALESCE(NULLIF(item ->> 'price', '')::DOUBLE PRECISION, product.preco_venda),
            product.custo_unitario
        );
    END LOOP;

    SELECT jsonb_agg(jsonb_build_object(
        'productId', produto_id,
        'qty', quantidade,
        'price', preco_unitario,
        'cost', custo_unitario
    )) INTO inserted_items
    FROM public.itens_venda WHERE venda_id = order_id;

    RETURN jsonb_build_object(
        'id', order_id,
        'clientId', p_client_id,
        'type', COALESCE(p_type, 'Encomenda'),
        'date', COALESCE(p_date, CURRENT_DATE),
        'deliveryDate', p_delivery_date,
        'status', 'a_fazer',
        'items', COALESCE(inserted_items, '[]'::JSONB)
    );
END;
$$;

CREATE OR REPLACE FUNCTION public.save_recipe(
    p_product_id BIGINT,
    p_yield INTEGER,
    p_items JSONB,
    p_update BOOLEAN
)
RETURNS BIGINT
LANGUAGE plpgsql
SET search_path = ''
AS $$
DECLARE
    recipe_id BIGINT;
    item JSONB;
    ingredient_id BIGINT;
    quantity DOUBLE PRECISION;
    item_unit TEXT;
    ingredient_unit TEXT;
BEGIN
    IF p_yield IS NULL OR p_yield <= 0
       OR p_items IS NULL OR jsonb_typeof(p_items) <> 'array'
       OR jsonb_array_length(p_items) = 0 THEN
        RAISE EXCEPTION 'valid recipe yield and items are required' USING ERRCODE = 'P0001';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM public.produtos WHERE id = p_product_id) THEN
        RAISE EXCEPTION 'product not found' USING ERRCODE = 'P0001';
    END IF;

    SELECT id INTO recipe_id
    FROM public.receitas
    WHERE produto_id = p_product_id
    FOR UPDATE;

    IF p_update AND recipe_id IS NULL THEN
        RAISE EXCEPTION 'recipe not found' USING ERRCODE = 'P0002';
    ELSIF NOT p_update AND recipe_id IS NOT NULL THEN
        RAISE EXCEPTION 'recipe already exists; use PUT to update' USING ERRCODE = 'P0001';
    END IF;

    IF p_update THEN
        UPDATE public.receitas SET rendimento = p_yield WHERE id = recipe_id;
        DELETE FROM public.itens_receita WHERE receita_id = recipe_id;
    ELSE
        INSERT INTO public.receitas (produto_id, rendimento)
        VALUES (p_product_id, p_yield)
        RETURNING id INTO recipe_id;
    END IF;

    FOR item IN SELECT value FROM jsonb_array_elements(p_items) LOOP
        ingredient_id := COALESCE((item ->> 'ingredientId')::BIGINT, (item ->> 'ingrediente_id')::BIGINT);
        quantity := COALESCE((item ->> 'quantidade')::DOUBLE PRECISION, (item ->> 'qty')::DOUBLE PRECISION);
        item_unit := COALESCE(item ->> 'unidade', item ->> 'unit');

        IF ingredient_id IS NULL OR quantity IS NULL OR quantity <= 0 THEN
            RAISE EXCEPTION 'recipe item requires an ingredient and positive quantity' USING ERRCODE = 'P0001';
        END IF;

        SELECT unidade INTO ingredient_unit
        FROM public.ingredientes
        WHERE id = ingredient_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'ingredient not found' USING ERRCODE = 'P0001';
        END IF;

        IF item_unit IS NOT NULL
           AND NOT (
               item_unit = ingredient_unit
               OR (item_unit = 'kg' AND ingredient_unit = 'g')
               OR (item_unit = 'g' AND ingredient_unit = 'g')
               OR (item_unit = 'l' AND ingredient_unit = 'ml')
               OR (item_unit = 'ml' AND ingredient_unit = 'ml')
               OR (item_unit = 'un' AND ingredient_unit = 'un')
           ) THEN
            RAISE EXCEPTION 'incompatible recipe item unit' USING ERRCODE = 'P0001';
        END IF;

        INSERT INTO public.itens_receita (receita_id, ingrediente_id, quantidade, unidade)
        VALUES (recipe_id, ingredient_id, quantity, item_unit);
    END LOOP;

    RETURN recipe_id;
END;
$$;

REVOKE ALL ON FUNCTION public.update_product_with_price_history(BIGINT, TEXT, DOUBLE PRECISION, DOUBLE PRECISION, TEXT) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.create_order(BIGINT, TEXT, DATE, DATE, JSONB) FROM PUBLIC, anon;
REVOKE ALL ON FUNCTION public.save_recipe(BIGINT, INTEGER, JSONB, BOOLEAN) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.update_product_with_price_history(BIGINT, TEXT, DOUBLE PRECISION, DOUBLE PRECISION, TEXT) TO authenticated;
GRANT EXECUTE ON FUNCTION public.create_order(BIGINT, TEXT, DATE, DATE, JSONB) TO authenticated;
GRANT EXECUTE ON FUNCTION public.save_recipe(BIGINT, INTEGER, JSONB, BOOLEAN) TO authenticated;

INSERT INTO storage.buckets (id, name, public)
VALUES ('docegestao', 'docegestao', TRUE)
ON CONFLICT (id) DO UPDATE SET public = EXCLUDED.public;
