# C-LAB Pilot Evaluation Tracker — Specification

**Status:** v0.1, agreed 2026-10-19 (Juan Menéndez / Claude). Supersedes the Excel data-collection template as the capture instrument once the prototype is validated.
**Source framework:** Pilot Evaluation Framework v1.3.3 (May 2026).

## 1. Purpose

A guided web application through which Cities LAB team members register a pilot and, at each milestone of its life cycle (Ideation → Design → Implementation → Evaluation), enter the evidence the framework asks for. All entries are stored in a single database whose unit of observation is the pilot, exportable as one row per pilot and one column per indicator. Retrospective pilots are loaded through the same instrument, so the whole portfolio is evaluated under one criterion.

## 2. Decisions

| # | Topic | Decision |
|---|---|---|
| 1 | Approach | Prototype first, outside IDB infrastructure, with test or non-sensitive data. Production target: Azure (to be confirmed with IDB IT). |
| 2 | Code hosting | Private GitHub repository. The dimension catalog is versioned there. |
| 3 | Data hosting (prototype) | Hosted PostgreSQL (Supabase free tier). Migration to Azure Database for PostgreSQL is a dump/restore. |
| 4 | Users | 5–10 team members. Login by email magic link against a whitelist. Microsoft SSO deferred (requires Azure AD app registration). |
| 5 | Audit | Who changed which field and when, at field level. Achieved by an append-only responses table. |
| 6 | Impact hypotheses | Variable number per pilot (L3.2 is repeatable). |
| 7 | Costs | Structured by the framework's capex/opex subcategories, in USD, totals computed. An "Other" line in each group absorbs anything not covered. Optional line-item breakdown keeps original currency. |
| 8 | Process-based pilots | Catalog v1 keeps parity with the current template. The differentiated L3 (organizational capacities, process evaluation) is a later catalog version. |
| 9 | Retrospective pilots | Supported via a pilot mode that opens all four stages at once. |
| 10 | Stage completion | Stores a snapshot and marks the stage complete; never locks. The UI warns before saving changes to a completed stage. |
| 11 | Language | English. |

## 3. Architecture

- **Catalog-driven UI.** `framework_catalog.json` defines pilot fields, dimensions, their fields, which stage each field matures at, which pilot types each dimension applies to, guiding questions and the core set. Forms, validation, the summary view and the wide export are all generated from it.
- **Long storage, wide export.** `responses` stores one row per save of one field (`pilot × dimension × field × hypothesis → value, author, time`). The current state is the latest row per key. The export pivots this into one row per pilot.
- **Front end:** static single-page app (plain HTML/JS, no build step) served from the repository.
- **Back end:** Supabase (PostgreSQL + Auth + row-level security). A thin Python layer (FastAPI) for the wide export and the workbook importer; everything else talks to the database directly.
- **Schema:** see `schema.sql`.

## 4. Screens

1. **New pilot** — the cover sheet: name, TC code, city/country, pilot type (attribution category pre-filled from type), mode (prospective/retrospective), problem definition, target population, lead, counterpart, dates.
2. **Pilots** — list with current stage, completion per level and last edit. Select one to open it.
3. **Stage form** — one section per level that matures at the stage. Each field shows the framework's guiding questions beside it. Autosave. Every field can be marked *Filled*, *No evidence* or *Not applicable* (the last two require a note), so gaps are explicit rather than blank. Type-limited dimensions are hidden for pilots they do not apply to. Hypotheses can be added at any stage. Costs are entered by subcategory in USD with totals computed; an optional breakdown table holds line items and original currency. "Complete stage" takes the snapshot.
4. **Pilot summary** — the Evaluation Sheet, generated from the database: every dimension with its current value, status, author and date, plus stage completions. Printable.
5. **Export** — CSV/XLSX, one row per pilot, one column per indicator; hypotheses as `h1…hN` column groups. Also a per-field change history export.

## 5. Phases

1. Specification, catalog and schema — this document. **Done, pending Juan's validation.**
2. Repository + Supabase project + schema applied + whitelist seeded.
3. Front end: screens 1–3.
4. Front end: screens 4–5; Python export.
5. Importer for the five retrospective workbooks (Geothermal, Yaku, Last Mile, Rio, Montería), with the corrections identified in review (currency, classification, counterfactual wording).
6. Pilot test with 2–3 team members; adjust catalog and UI.
7. Hand-over package for IDB IT: deployment notes for Azure.

## 6. Open items

- Confirm with IDB IT whether Azure hosting is available and whether data may sit in Supabase during the prototype.
- Decide whether L4.4 (Evaluation lessons) is part of the core set — the template is inconsistent; the catalog currently marks it core.
- Catalog v1.1: process-based L3 fields (organizational capacities; process evaluation).
- Microsoft SSO once hosted inside IDB.
