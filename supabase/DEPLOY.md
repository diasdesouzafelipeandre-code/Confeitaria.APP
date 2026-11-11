# Publicacao do DoceGestao no Supabase

## 1. Configurar acesso ao banco

No SQL Editor do projeto, execute `access.sql`. O arquivo ativa RLS, limita as
tabelas a contas autorizadas em `public.app_access`, cria as funcoes transacionais
usadas pela API e cria o bucket publico `docegestao`.

Crie o usuario de login em **Authentication > Users**. Depois, autorize o mesmo
endereco de e-mail no SQL Editor:

```sql
INSERT INTO public.app_access (email)
VALUES (lower('seu-email@exemplo.com'))
ON CONFLICT (email) DO NOTHING;
```

`schema.sql` cria as tabelas. Execute-o apenas se essas tabelas ainda nao
existirem; para um projeto ja preparado, basta executar `access.sql`.

## 2. Publicar a API

Instale o Supabase CLI, autentique-se e associe a pasta ao projeto:

```powershell
supabase login
supabase link --project-ref dksugfzjisqjisqwbdfo
supabase functions deploy api
```

A Edge Function valida o token do Supabase Auth e consulta `app_access` antes
de atender qualquer rota. Ela usa a chave publica e as politicas RLS; nenhuma
service-role key deve ser colocada no navegador ou no repositorio.

## 3. Hospedagem da interface

Edite `static/supabase-config.js` e use a chave **publishable** (ou `anon`)
publica do projeto. Essa chave pode estar no navegador; nunca use
`service_role` ou uma chave `sb_secret_`.

O Supabase Storage e as Edge Functions nao hospedam esta interface HTML: as
respostas HTML sao servidas como `text/plain` com sandbox, e o navegador nao
executa a pagina. O bucket publico `docegestao` pode guardar arquivos, mas a
interface precisa ser publicada em um provedor de hospedagem estatica. Publique
`static/index.html` e `static/supabase-config.js` juntos, na mesma pasta, e
configure essa hospedagem para servir `index.html` como `text/html`.

A API e os dados continuam no Supabase; configure a URL publica da interface
nas regras de redirecionamento do Supabase Auth antes de habilitar o login.
