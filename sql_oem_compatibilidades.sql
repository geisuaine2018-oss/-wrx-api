-- Executar no Supabase SQL Editor do PartHub (iftzoceaalhpyckuznae)
-- Tabela: oem_compatibilidades

create table if not exists oem_compatibilidades (
    id              uuid default gen_random_uuid() primary key,
    oem             text not null,
    marca           text not null,
    modelo          text,
    motor           text,
    cambio          text,
    ano_inicial     integer,
    ano_final       integer,
    fonte           text default 'mercadolivre',
    confianca       integer default 0,
    data_validacao  timestamptz default now(),
    criado_em       timestamptz default now()
);

-- Index para busca rápida por OEM
create index if not exists idx_oem_compat_oem on oem_compatibilidades(oem);

-- RLS: qualquer autenticado pode ler e inserir
alter table oem_compatibilidades enable row level security;

create policy "autenticados podem ler" on oem_compatibilidades
    for select using (auth.role() = 'authenticated');

create policy "autenticados podem inserir" on oem_compatibilidades
    for insert with check (auth.role() = 'authenticated');

create policy "autenticados podem deletar proprios" on oem_compatibilidades
    for delete using (auth.role() = 'authenticated');
