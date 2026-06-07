-- Tabela da REVISÃO DE PREÇOS (anúncios ativos vs concorrência ML)
-- Rode no Supabase WRX (uthsiihzpsgarargegcw) → SQL Editor → Run.
-- Pode rodar "without RLS" / como service. É seguro rodar de novo (IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS revisao_precos (
  id BIGSERIAL PRIMARY KEY,
  sku TEXT,
  ml_id TEXT,
  conta TEXT DEFAULT 'default',
  titulo TEXT,
  thumbnail TEXT,
  oem TEXT,
  meu_preco FLOAT DEFAULT 0,
  menor_mercado FLOAT DEFAULT 0,
  media_mercado FLOAT DEFAULT 0,
  sugestao FLOAT DEFAULT 0,
  diferenca_pct FLOAT DEFAULT 0,
  prioridade TEXT DEFAULT 'manter',   -- manter / revisar / alta
  fonte_qtd INTEGER DEFAULT 0,        -- quantos preços de mercado foram coletados
  status TEXT DEFAULT 'pendente',     -- pendente / aprovado / ignorado
  preco_aplicado FLOAT,
  criado_em TIMESTAMPTZ DEFAULT NOW(),
  revisado_em TIMESTAMPTZ,
  UNIQUE(sku, conta)
);

CREATE INDEX IF NOT EXISTS idx_revisao_status ON revisao_precos(status);
CREATE INDEX IF NOT EXISTS idx_revisao_prioridade ON revisao_precos(prioridade);
