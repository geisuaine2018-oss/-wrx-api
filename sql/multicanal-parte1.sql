-- ════════════════════════════════════════════════════════════════════
-- MÓDULO B (ANTI-VENDA-DUPLA) — BANCO
-- Rodar no Supabase → SQL Editor → Run. Idempotente.
-- Bloco separado do Módulo A (expedição). Aqui só o que o multi-canal precisa.
-- ════════════════════════════════════════════════════════════════════

-- 1) SHOPEE_ANUNCIOS — mapa SKU → anúncio Shopee (hoje NÃO existe; a cascata
--    Shopee depende dela). O backend popula via /integracoes/shopee/sincronizar.
create table if not exists public.shopee_anuncios (
  id        bigint generated always as identity primary key,
  shop_id   text not null,
  item_id   text not null,
  sku       text,
  titulo    text,
  preco     numeric(10,2) default 0,
  estoque   integer default 0,
  status    text default 'NORMAL',          -- NORMAL | UNLIST
  fotos     jsonb default '[]',
  sync_at   timestamptz default now(),
  unique (shop_id, item_id)
);
create index if not exists idx_shopee_anuncios_sku on public.shopee_anuncios(sku);

-- 2) SYNC_LOG — histórico de cada sincronização de venda (o que foi pausado).
create table if not exists public.sync_log (
  id        bigint generated always as identity primary key,
  sku       text,
  origem    text,                            -- canal onde vendeu (ml/shopee)
  pausados  integer default 0,
  total     integer default 0,
  detalhe   text,                            -- json com o resultado por anúncio
  criado_em timestamptz default now()
);
create index if not exists idx_sync_log_sku on public.sync_log(sku);
create index if not exists idx_sync_log_data on public.sync_log(criado_em);

-- ════════════════════════════════════════════════════════════════════
-- Observação: `ml_anuncios` já existe (2.893 registros) e é o mapa do ML.
-- 821 deles estão SEM sku → não-sincronizáveis até vincular o SKU.
-- ════════════════════════════════════════════════════════════════════
