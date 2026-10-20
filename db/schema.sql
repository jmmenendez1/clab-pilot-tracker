-- C-LAB Pilot Evaluation Tracker — database schema (PostgreSQL on Supabase)
--
-- Principles
--   * The framework lives in catalog/framework_catalog.json; the database stores values
--     keyed by dimension code and field key, so a catalog change needs no migration.
--   * `responses` is append-only: every save inserts a row; the current value is the latest row.
--   * Login is Supabase Auth (email magic link). Only emails in `allowed_emails` can sign in.
--
-- Apply once in the Supabase SQL editor (whole file), then run seed_allowed_emails.sql.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Whitelist + user profiles
-- ---------------------------------------------------------------------------
create table allowed_emails (
  email      text primary key,
  full_name  text not null,
  role       text not null default 'editor' check (role in ('admin', 'editor')),
  added_at   timestamptz not null default now()
);

create table users (
  id          uuid primary key references auth.users(id) on delete cascade,
  email       text not null unique,
  full_name   text not null,
  role        text not null default 'editor' check (role in ('admin', 'editor')),
  active      boolean not null default true,
  created_at  timestamptz not null default now()
);

-- Block sign-in attempts from emails that are not whitelisted.
create or replace function auth_guard_whitelist() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  if not exists (select 1 from allowed_emails a where lower(a.email) = lower(new.email)) then
    raise exception 'Email % is not authorised for the C-LAB Pilot Tracker', new.email
      using errcode = 'P0001';
  end if;
  return new;
end $$;

create trigger auth_users_whitelist before insert on auth.users
  for each row execute function auth_guard_whitelist();

-- Create the profile row the first time a whitelisted user signs in.
create or replace function handle_new_auth_user() returns trigger
language plpgsql security definer set search_path = public as $$
begin
  insert into users (id, email, full_name, role)
  select new.id, a.email, a.full_name, a.role
  from   allowed_emails a
  where  lower(a.email) = lower(new.email)
  on conflict (id) do nothing;
  return new;
end $$;

create trigger auth_users_profile after insert on auth.users
  for each row execute function handle_new_auth_user();

-- Authorization helpers. SECURITY DEFINER so they read `users` without triggering its
-- own row-level policy (which would otherwise recurse).
create or replace function is_team_member() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from users u where u.id = auth.uid() and u.active)
$$;

create or replace function is_admin() returns boolean
language sql stable security definer set search_path = public as $$
  select exists (select 1 from users u where u.id = auth.uid() and u.active and u.role = 'admin')
$$;

-- ---------------------------------------------------------------------------
-- Pilots (unit of observation)
-- ---------------------------------------------------------------------------
create table pilots (
  id                   uuid primary key default gen_random_uuid(),
  tc_code              text not null,                         -- e.g. RG-T4182
  name                 text not null,
  city                 text not null,
  country              text not null,
  pilot_type           text not null check (pilot_type in ('urban','digital','behavioral','institutional','methodology')),
  attribution_category text not null check (attribution_category in ('counterfactual','process')),
  mode                 text not null check (mode in ('prospective','retrospective')),
  problem_definition   text,
  target_population    text,
  lead                 text,
  counterpart          text,
  date_started         date,
  date_ended           date,
  current_stage        text not null default 'ideation'
                       check (current_stage in ('ideation','design','implementation','evaluation')),
  archived             boolean not null default false,
  created_by           uuid not null references users(id) default auth.uid(),
  created_at           timestamptz not null default now(),
  updated_by           uuid references users(id),
  updated_at           timestamptz not null default now(),
  unique (tc_code, name)
);

create or replace function touch_updated() returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  new.updated_by := auth.uid();
  return new;
end $$;

create trigger pilots_touch before update on pilots for each row execute function touch_updated();

-- ---------------------------------------------------------------------------
-- Impact hypotheses (L3.2 is repeatable: N per pilot)
-- ---------------------------------------------------------------------------
create table hypotheses (
  id          uuid primary key default gen_random_uuid(),
  pilot_id    uuid not null references pilots(id) on delete cascade,
  ordinal     smallint not null,                               -- 1, 2, 3 … display order
  label       text,                                            -- short handle, optional
  archived    boolean not null default false,
  created_by  uuid not null references users(id) default auth.uid(),
  created_at  timestamptz not null default now(),
  unique (pilot_id, ordinal)
);

-- ---------------------------------------------------------------------------
-- Responses — the long table. One row per save of one field.
-- dimension_code + field_key are validated against the catalog in the front end / API.
-- ---------------------------------------------------------------------------
create table responses (
  id             bigserial primary key,
  pilot_id       uuid not null references pilots(id) on delete cascade,
  dimension_code text not null,                                -- 'L1.1' … 'L4.5'
  field_key      text not null,                                -- 'degree', 'justification', 'capex.construction', 'endline_t2' …
  hypothesis_id  uuid references hypotheses(id) on delete cascade,  -- only for L3.2
  stage          text not null check (stage in ('ideation','design','implementation','evaluation')),
  status         text not null default 'filled' check (status in ('filled','no_evidence','not_applicable')),
  value_text     text,
  value_num      numeric(16,2),                                 -- money (USD) and other numbers
  value_date     date,
  note           text,                                          -- required when status <> 'filled' or degree = 'na'
  author_id      uuid not null references users(id) default auth.uid(),
  created_at     timestamptz not null default now(),
  check (hypothesis_id is not null or dimension_code <> 'L3.2'),
  check (status = 'filled' or note is not null)
);

create index responses_lookup_idx
  on responses (pilot_id, dimension_code, field_key, hypothesis_id, created_at desc, id desc);

-- Current value of every field = latest row per (pilot, dimension, field, hypothesis)
create view responses_current with (security_invoker = true) as
select distinct on (pilot_id, dimension_code, field_key, hypothesis_id) *
from   responses
order  by pilot_id, dimension_code, field_key, hypothesis_id, created_at desc, id desc;

-- ---------------------------------------------------------------------------
-- Optional full cost breakdown (line items). Subcategory totals live in
-- `responses` (dimension L2.1) in USD; this table keeps the detail and the
-- original currency when the source figure was not in USD.
-- ---------------------------------------------------------------------------
create table cost_items (
  id               bigserial primary key,
  pilot_id         uuid not null references pilots(id) on delete cascade,
  category         text not null check (category in ('capex','opex')),
  subcategory      text not null,                              -- catalog field key, e.g. 'capex.construction'
  label            text not null,                              -- 'Mural artist + materials'
  amount_usd       numeric(16,2) not null,
  amount_original  numeric(16,2),
  currency         char(3),                                    -- ISO 4217, e.g. 'BRL'
  fx_note          text,                                       -- 'June 2024 rate'
  period           text,                                       -- 'YYYY-MM' for month-by-month budgets, optional
  source           text,                                       -- invoice, report, estimate
  active           boolean not null default true,              -- soft delete; edits insert a new row and deactivate the old one
  replaces_id      bigint references cost_items(id),
  author_id        uuid not null references users(id) default auth.uid(),
  created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Stage completions — snapshot, never a lock
-- ---------------------------------------------------------------------------
create table stage_completions (
  id            bigserial primary key,
  pilot_id      uuid not null references pilots(id) on delete cascade,
  stage         text not null check (stage in ('ideation','design','implementation','evaluation')),
  snapshot      jsonb not null,                                -- {dimension_code: {field_key: value}} as of completion
  completed_by  uuid not null references users(id) default auth.uid(),
  completed_at  timestamptz not null default now()
);

create index stage_completions_idx on stage_completions (pilot_id, stage, completed_at desc);

-- ---------------------------------------------------------------------------
-- Audit log for the mutable tables (responses is self-auditing)
-- ---------------------------------------------------------------------------
create table audit_log (
  id          bigserial primary key,
  table_name  text not null,
  row_id      text not null,
  action      text not null check (action in ('insert','update','delete')),
  old_row     jsonb,
  new_row     jsonb,
  actor_id    uuid,
  at          timestamptz not null default now()
);

create or replace function audit_trigger() returns trigger language plpgsql security definer as $$
begin
  insert into audit_log (table_name, row_id, action, old_row, new_row, actor_id)
  values (tg_table_name,
          coalesce(new.id::text, old.id::text),
          lower(tg_op),
          case when tg_op <> 'INSERT' then to_jsonb(old) end,
          case when tg_op <> 'DELETE' then to_jsonb(new) end,
          auth.uid());
  return coalesce(new, old);
end $$;

create trigger pilots_audit     after insert or update or delete on pilots     for each row execute function audit_trigger();
create trigger hypotheses_audit after insert or update or delete on hypotheses for each row execute function audit_trigger();
create trigger cost_items_audit after insert or update or delete on cost_items for each row execute function audit_trigger();

-- ---------------------------------------------------------------------------
-- Row-level security: any active whitelisted user reads and writes;
-- only admins archive or delete pilots and manage the whitelist.
-- ---------------------------------------------------------------------------
alter table allowed_emails    enable row level security;
alter table users             enable row level security;
alter table pilots            enable row level security;
alter table hypotheses        enable row level security;
alter table responses         enable row level security;
alter table cost_items        enable row level security;
alter table stage_completions enable row level security;
alter table audit_log         enable row level security;

create policy team_read   on pilots for select using (is_team_member());
create policy team_insert on pilots for insert with check (is_team_member());
create policy team_update on pilots for update using (is_team_member())
                                               with check (is_team_member() and (archived = false or is_admin()));
create policy admin_delete on pilots for delete using (is_admin());

create policy team_all on hypotheses        for all using (is_team_member()) with check (is_team_member());
create policy team_all on responses         for all using (is_team_member()) with check (is_team_member() and author_id = auth.uid());
create policy team_all on cost_items        for all using (is_team_member()) with check (is_team_member());
create policy team_all on stage_completions for all using (is_team_member()) with check (is_team_member());

create policy users_read     on users          for select using (is_team_member());
create policy users_admin    on users          for all    using (is_admin()) with check (is_admin());
create policy whitelist_read on allowed_emails for select using (is_team_member());
create policy whitelist_admin on allowed_emails for all   using (is_admin()) with check (is_admin());
create policy audit_read     on audit_log      for select using (is_team_member());

-- ---------------------------------------------------------------------------
-- Wide export: one row per pilot, one column per indicator.
-- Generated by the API from the catalog (column list = catalog fields), e.g.
--   L1_1_degree, L1_1_justification, …, L2_1_capex_construction, …,
--   L3_2_h1_hypothesis, L3_2_h1_endline_t2, …, L4_5_text,
-- plus pilot fields and computed totals. Delivered as CSV/XLSX.
-- ---------------------------------------------------------------------------
