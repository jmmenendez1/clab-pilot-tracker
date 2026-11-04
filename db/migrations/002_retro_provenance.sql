-- 002 — Provenance for the retrospective evaluation pipeline (retro/), and evaluability baselines.
-- Run once in the Supabase SQL editor, after schema.sql and 001. Safe to re-run.
--
-- What it adds
--   pilots      pilot_key (CLAB-P-xxx from the retro registry), related_tcs, lifecycle_status, retro_cover
--               (the extraction's cover values with evidence, for review)
--   hypotheses  origin (human | retro_extraction)
--   responses   origin (human | workbook_import | retro_extraction), confidence, evidence (verbatim quotes), flags
--   evaluability_baselines   one row per pilot, source and date: the completeness score at that moment
--
-- Rules the loader relies on (retro/stage6_load_sql.py)
--   * responses stay append-only. A machine draft is a row with origin = 'retro_extraction'. A person confirms
--     or corrects it by saving the field, which inserts a row with origin = 'human'.
--   * The loader never writes over a human value. It only inserts where a field has no current row, or where the
--     current row is itself a machine draft that changed.

alter table pilots add column if not exists import_source text;
alter table pilots add column if not exists pilot_key text;
alter table pilots add column if not exists related_tcs text[];
alter table pilots add column if not exists lifecycle_status text;
alter table pilots add column if not exists retro_cover jsonb;
create unique index if not exists pilots_pilot_key_idx on pilots (pilot_key) where pilot_key is not null;

alter table hypotheses add column if not exists origin text not null default 'human';
alter table responses  add column if not exists origin text not null default 'human';
alter table responses  add column if not exists confidence numeric(3,2);
alter table responses  add column if not exists evidence jsonb;          -- [{doc_id, document, location, quote, verified}]
alter table responses  add column if not exists flags text[];            -- checks raised by the extraction

do $$ begin
  alter table hypotheses add constraint hypotheses_origin_chk check (origin in ('human','retro_extraction'));
exception when duplicate_object then null; end $$;
do $$ begin
  alter table responses add constraint responses_origin_chk check (origin in ('human','workbook_import','retro_extraction'));
exception when duplicate_object then null; end $$;

-- The view was created with select *, which Postgres expands at creation time: recreate it so it carries the
-- new columns (they are appended, so create or replace is allowed).
create or replace view responses_current with (security_invoker = true) as
select distinct on (pilot_id, dimension_code, field_key, hypothesis_id) *
from   responses
order  by pilot_id, dimension_code, field_key, hypothesis_id, created_at desc, id desc;

-- Evaluability (completeness score) = complete subdimensions / all subdimensions; see retro/evaluability.py.
-- source: retro_extraction (documents only) | evaluator_workbook (documents + interviews) | snapshot (later, from the app)
create table if not exists evaluability_baselines (
  id               bigserial primary key,
  pilot_id         uuid not null references pilots(id) on delete cascade,
  source           text not null check (source in ('retro_extraction','evaluator_workbook','snapshot')),
  score            numeric(4,3) not null,
  complete         smallint not null,
  total            smallint not null,
  detail           jsonb not null,                 -- {"L1.1": 1, "L1.2": 0, ...}
  catalog_version  text,
  computed_at      date not null,
  loaded_at        timestamptz not null default now(),
  unique (pilot_id, source, computed_at)
);
alter table evaluability_baselines enable row level security;
do $$ begin
  create policy team_all on evaluability_baselines for all using (is_team_member()) with check (is_team_member());
exception when duplicate_object then null; end $$;
