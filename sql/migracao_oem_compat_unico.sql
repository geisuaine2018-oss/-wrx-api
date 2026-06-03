-- ============================================================================
-- Migração: dedup + constraint única em oem_compatibilidades
-- RODAR NO SUPABASE DO CRM: projeto uthsiihzpsgarargegcw
--   Dashboard > SQL Editor > cole tudo > Run
-- ----------------------------------------------------------------------------
-- Por que: a busca de OEM (local_compat_server) salvava no cache a cada execução
-- SEM deduplicar, acumulando linhas idênticas. A rota de produção
-- (/compatibilidade/buscar-oem) serve esse cache direto pro painel, então as
-- duplicatas apareciam na tela. A chave anon não tem DELETE (RLS), então a
-- limpeza precisa ser feita aqui (service_role do dashboard).
-- ============================================================================

-- 1) Remove duplicatas EXATAS, mantendo uma de cada grupo.
--    IS NOT DISTINCT FROM trata NULL como igual (modelo/motor/anos podem ser NULL).
--    ctid é o identificador físico da linha — funciona com id uuid ou bigint.
DELETE FROM oem_compatibilidades a
USING oem_compatibilidades b
WHERE a.ctid > b.ctid
  AND a.oem        =                 b.oem
  AND a.marca       IS NOT DISTINCT FROM b.marca
  AND a.modelo      IS NOT DISTINCT FROM b.modelo
  AND a.motor       IS NOT DISTINCT FROM b.motor
  AND a.ano_inicial IS NOT DISTINCT FROM b.ano_inicial
  AND a.ano_final   IS NOT DISTINCT FROM b.ano_final;

-- 2) Constraint única para impedir duplicatas futuras.
--    NULLS NOT DISTINCT (Postgres 15+, que o Supabase usa) faz a constraint tratar
--    NULL como igual — sem isso, linhas com motor/anos NULL ainda duplicariam.
--    Casa com o on_conflict do upsert no local_compat_server._salvar_supabase.
ALTER TABLE oem_compatibilidades
  ADD CONSTRAINT oem_compat_unico
  UNIQUE NULLS NOT DISTINCT (oem, marca, modelo, motor, ano_inicial, ano_final);

-- 3) (Conferência) quantas linhas sobraram por OEM — rode separado se quiser ver:
-- SELECT oem, count(*) FROM oem_compatibilidades GROUP BY oem ORDER BY count(*) DESC;
