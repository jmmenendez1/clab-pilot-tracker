# C-LAB Pilot Evaluation Tracker

Guided data-collection app for the Cities LAB Pilot Evaluation Framework. Team members register a pilot and enter the framework's evidence at each life-cycle milestone (Ideation → Design → Implementation → Evaluation). Everything is stored in one database — one pilot per row, one column per indicator on export — with a full change history per field.

See `SPEC.md` for scope and decisions.

## Layout

```
catalog/framework_catalog.json   the framework as configuration: dimensions, fields, stages, guiding questions
db/schema.sql                    PostgreSQL schema for Supabase (tables, auth whitelist, audit, row-level security)
db/seed_allowed_emails.sql       who can sign in
db/test_schema.sql               smoke test to run after applying the schema
web/                             static front end (no build step)        — phase 3
api/                             Python export + importer                 — phases 4–5
import/                          retrospective workbooks → database       — phase 5
docs/                            deployment notes for IDB IT              — phase 7
```

## Setup (prototype, ~20 minutes)

### 1. Supabase project

1. Create a free account at supabase.com and a new project (any region in the US; choose a strong database password and keep it).
2. In **Authentication → Sign In / Providers**: keep *Email* enabled, turn **Confirm email** off, keep the magic link on, and make sure **Allow new users to sign up** is ON (a magic-link first sign-in counts as a sign-up; the whitelist in step 5 is what restricts who gets in). Disable *Phone* and all social providers.
3. In **Authentication → URL Configuration**: set *Site URL* to where the front end will be served (for local testing: `http://localhost:8080`) and add `http://localhost:8080/**` under **Redirect URLs**. Add the GitHub Pages URL and its `/**` pattern later.
4. In **SQL Editor**, open a new query, paste the whole of `db/schema.sql` and run it. It must finish without errors.
5. Edit `db/seed_allowed_emails.sql` with the team's emails and roles, paste and run it.
6. In **Project Settings → API Keys**, copy the *Project URL* and the *anon public* key into `web/config.js` (see `web/config.example.js`). The anon key is safe to publish: every table is protected by row-level security and only whitelisted emails can sign in.

### 2. Verify

1. Open the app once and sign in with the admin email (you will receive a magic link). This creates your profile row.
2. In **SQL Editor**, run `db/test_schema.sql`. Expected: one row with `ok = true`.
3. Try signing in with an email that is not whitelisted: the link must be refused with *"is not authorised"*.

### 3. Adding people later

Append them to `db/seed_allowed_emails.sql` and re-run it (it upserts). Removing someone: set `active = false` on their row in `users`.

## Importing retrospective workbooks

Two ways to load a pilot evaluated in the Excel template (`.xlsx`/`.xlsm`):

- **In the app**: Pilots → *Import workbook…* → choose the file → review the preview and warnings → Import. The pilot is created as retrospective, attributed to you, with one response per field and the stage tab it came from.
- **As SQL** (several at once, from a terminal with Node and `npm i xlsx`): `node import/workbooks_to_sql.js out.sql file1.xlsm file2.xlsm …`, then paste `out.sql` into the Supabase SQL editor. Pilots that already exist (same TC code and name) are skipped.

Run `db/migrations/001_import_source.sql` once before the first import (adds the provenance column). Cost figures written as free text land in the L2.1 notes; enter the USD amounts by subcategory afterwards. `node import/test_import.js file.xlsm` shows what would be imported without writing anything.

## Local development

```
python3 -m http.server 8080   # from the repository root, then open http://localhost:8080/web/
# add ?local=1 to the URL to run against the in-browser store instead of Supabase
```

## Moving to IDB infrastructure (later)

The database is standard PostgreSQL: `pg_dump` from Supabase, `pg_restore` into Azure Database for PostgreSQL. The front end is static files: Azure Static Web Apps or any web server. Replace the magic-link login with Microsoft Entra ID (Azure AD) once hosted inside IDB. Details in `docs/` when phase 7 starts.
