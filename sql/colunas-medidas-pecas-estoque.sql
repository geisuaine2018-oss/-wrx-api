-- Colunas de PESO e MEDIDAS no pecas_estoque (pro Cadastro Rápido e pro frete).
-- Rode no Supabase WRX (uthsiihzpsgarargegcw) -> SQL Editor -> Run. Seguro rodar de novo.

ALTER TABLE pecas_estoque ADD COLUMN IF NOT EXISTS peso         FLOAT;
ALTER TABLE pecas_estoque ADD COLUMN IF NOT EXISTS altura       FLOAT;
ALTER TABLE pecas_estoque ADD COLUMN IF NOT EXISTS largura      FLOAT;
ALTER TABLE pecas_estoque ADD COLUMN IF NOT EXISTS comprimento  FLOAT;

-- 'origem' marca cadastro manual (o sync do PartsHub só mexe em origem IS NULL,
-- então o que você cadastra na mão nunca é apagado pelo sync).
ALTER TABLE pecas_estoque ADD COLUMN IF NOT EXISTS origem TEXT;
