# Retrospective evaluation — methods, decisions and results

This file records what each stage of the retrospective pipeline does, the parameters it uses, how it was checked, and the team decisions it encodes. The pipeline is in `retro/` and is run from that folder. The plan it implements is the guide *Plan guía — Evaluación retrospectiva automatizada de pilotos*, a Claude Doc shared with the team.

## 1. Decisions

| Date | Decision | Who | Where it is applied |
|---|---|---|---|
| 2026-11-02 | Everything runs locally. Model calls use Juan's Anthropic API key, and nothing derived from documents is committed to git. | Juan | `README.md` (data policy), `.gitignore` |
| 2026-11-02 | The team's *Review* / *Notes* columns are binding, and the algorithms never reopen them. | Juan | `stage2_registry.py` → `parse_decisions` |
| 2026-11-02 | Missing information is loaded as `no_evidence` with a note for framework fields, and as `Unknown` without a note for cover fields. Fields are never dropped. | Juan | stage 5 (planned) |
| 2026-11-02 | **Scope.** Only the pilots the team validated (✓) are evaluated (29; see §3). The unreviewed (“?”) entries are not classified as pilots or non-pilots. They are only screened, to recover documents that belong to validated pilots. | Juan | `stage2_registry.py` → `in_retro_scope`; `stage3_screen_unreviewed.py` |
| 2026-11-02 | The Cities HUB (RG-T4182) is **not** evaluated as a pilot. The three HUB-level entries are documents to distribute among the pilots they cover: *Public Innovation Pilots*, *Urban Experimentation Projects*, and *Collaborative Regional Platform* (the TC's official name). | Juan | stage 2 status `umbrella_not_a_pilot` (E085 through `data/team_decisions_overrides.csv`); stage 3 screens their documents |
| 2026-11-02 | RG-T4182 contains 4 pilots: Quito (Proyecto Yaku), San Salvador (Geothermal Climate Prototype), Renca, and Providencia. Water Consumption Reduction in Buildings corresponds to Renca. Juan believes Providencia overlaps with Urban Tree Detection, but this is not yet checked. | Juan | see next rows |
| 2026-11-02 | **Providencia is split out** as its own pilot, *Water Management Pilot Providencia* (`CLAB-P-201`). *Water Consumption Reduction in Buildings* (`CLAB-P-081`) becomes Renca. The documents that cover both communes belong to both pilots. The basis is the HUB process evaluation, which lists Renca and Providencia as separate pilots. | Juan | `data/pilot_splits.csv` → stage 2 |
| 2026-11-02 | *Urban Tree Detection* and *Urban Oasis Colonia Bethania* stay as validated pilots with no change. Whatever their documents do not cover is recorded as gaps, and the team reviews them later. | Juan | no change; stage 5 fills gaps as `no_evidence` |
| 2026-11-03 | Stage 3's proposed document moves are **accepted by default**. The team only marks `reject` where a move is wrong. | Juan | `out/document_reassignments.csv` → stage 4 |
| 2026-11-03 | **Degrees rate what the documents show the pilot did.** A guiding question the documents leave unanswered lowers `confidence` and goes in the note. It does not lower the degree. Only weaknesses the documents show lower it. | Juan | `prompts/extract_system.md` rule 6 |
| 2026-11-02 | The reviewer of each pilot depends on the pilot (Juan, Eli, Rober, Angie or Toni). It is recorded per pilot, with no fixed assignment. | Juan | `reviewer` column in the registry |

## 2. Stage 1 — text, duplicates, versions (`stage1_extract.py`)

**Input.** `downloads_pilots/<TC>/<pilot folder>/<file>`. The script reads the 325 PDF, DOCX and PPTX files found there. The pipeline's own JSON files in the root are ignored.

**Extraction.** Text is split into units so evidence can be cited by location:

- PDF: one unit per page, via PyMuPDF. A page with fewer than 40 characters of text and at least one image is OCR'd with Tesseract at 200 dpi, languages `spa+por+eng`. The unit records `ocr: true`.
- DOCX: paragraphs and table rows, grouped into blocks of about 3,000 characters, because DOCX files have no pages.
- PPTX: one unit per slide, including text frames and tables.

Each file is identified by `doc_id`, the first 12 hex characters of its SHA-256, and its text is written to `out/text/<doc_id>.json`.

**One logical document, many files.** Files are grouped with a union-find whenever one of these holds:

1. Identical bytes (same SHA-256).
2. Same content. TF-IDF cosine on word 1–2-grams is at least **0.95**, whatever the name. This catches DOCX/PDF pairs, renamed copies and files saved with a pipeline slug prefix (`slug__Original name.pdf`).
3. Successive versions. Same *name family* and cosine at least **0.85**. The name family is the name without accents, without the slug prefix, and without version tokens: `v2`, `(1)`, `final`, `revisado`, `R03`, `10jan2023`, 6–8 digit dates and 4-digit years.

Rule 3 needs content similarity because dated monthly reports from the same consultant share a name family but not content. For example, the five *Data Science for Urban Development Projects Consultant* reports reach cosine 0.52–0.76, so they stay separate documents. True versions measured 0.97–1.00.

The **representative** of each group is the file ranked highest on version (`final` > higher `v`/`R` number > others, with copies such as `(1)` last), then on text length. Only representatives are used downstream (`use_for_extraction = true`). `cluster_locations` lists every pilot folder holding a copy.

**Results (2026-11-02):**

- Files: 325 → 295 unique by bytes → **269 documents**.
- Of the non-representative files, 27 are exact copies, 20 have the same content in another format or name, and 9 are older versions.
- 127 pages were OCR'd, with 0 extraction errors.
- One document sits in two pilot folders: `entrega6.docx` in *Big Data for Smart Cities* is the same as *Entrega4_Consultoria2…* in *Vem pro Centro*.
- Reconciliation with the repository's *Files* sheet: 310 files match it and 15 are only on disk. The 15 include the documents of two entries the repository listed with 0 files, *Informal Settlement Mapping Asunción* and *INCLUIA*, which sit in folders named after other source slugs.

## 3. Stage 2 — pilot registry (`stage2_registry.py`)

**Entries.** Each row of the repository's *Pilots* sheet is an entry `E<row>` (105 entries, 14 TCs).

**Folders → entries.** Each folder maps to an entry by, in order:

1. the canonical folder;
2. a match against the entry's source slugs;
3. fuzzy matching, used as a last resort.

The method used is recorded per document as `folder_mapping`.

**Parsing the team's decisions.** Each Review / Notes value is interpreted as follows:

| Review / Notes | Interpreted as |
|---|---|
| `✓` | `confirmed` |
| `?` or empty | `unreviewed` |
| “redundant, this is X”, “redundant (X)”, “is the same as X” | `merge_into` X. X is resolved by fuzzy match (`token_set_ratio`) on name and cities. |
| “never happened” | `never_happened` |
| “this is all BPR pilots together” | `umbrella` (HUB-level, not a pilot) |
| starts with “Not started” | `not_started`. Two rows with the same text and name are the same pilot (`not_started_duplicate`). |
| “still ongoing” | `ongoing` (evaluated prospectively, not retrospectively) |
| starts with “Maybe” | `doubtful` (files may belong elsewhere) |
| “spin off tandil” | `parent_entry` = the entry the team noted as “original tree detection pilot” |
| “city should be only X” | `metadata_fix` (`cities=X`) |

Every interpretation is written to `out/team_decisions.csv`. Corrections go in `data/team_decisions_overrides.csv` and are applied after parsing. There is one override so far:

- **E096.** “redundant (tree detection tandil)” refers to the original Tandil pilot, E036. Fuzzy matching tied it with the spin-off E080, because both mention tree detection and Tandil.

**Entities.** Merged entries fold into their target, following chains. The surviving entry keeps its documents plus the merged entries' documents, names and slugs, which become aliases.

- `pilot_id` is `CLAB-P-<repository row − 3>` of the surviving entry. It is stable as long as the repository rows are not reordered, and it does not depend on the TC, because 6 of the 12 team merges join entries from different TCs.
- `tcs_all` lists every cooperation involved.

Result: **93 entities**, including one split.

| Status | Entities |
|---|---|
| confirmed | 29 |
| to_review | 57 |
| umbrella_not_a_pilot | 3 |
| doubtful_files | 1 |
| ongoing | 1 |
| not_started | 1 |
| never_happened | 1 |

`in_retro_scope` is true for the 29 confirmed. The repository has 29 ✓ entries. *Collaborative Regional Platform* turned out to be the HUB TC itself, so it drops out, and *Providencia* is added by splitting Renca.

**Splits.** `data/pilot_splits.csv` turns one repository entry into two pilots. The new pilot copies the source's status and TCs, gets a `pilot_id` from 201 upward (it has no row in the repository), and records `split_from`. With `documents = all` it shares every document of the source, and `source_cities` corrects the source's cities. There is one split so far: Renca and Providencia.

**Duplicate candidates.** For every pair of entries, the script computes:

| Feature | Definition | Weight |
|---|---|---|
| `name_sim` | `token_set_ratio` of names, after removing stop words such as pilot/proyecto/de | 0.25 |
| `content_sim` | TF-IDF cosine of the entries' pooled document text. Invoices, affidavits and agreements are excluded. | 0.25 |
| `geo` | 1 if they share a place token, 0 if both have places and none is shared, 0.5 if unknown | 0.15 |
| `mention` | Share of one entry's *distinctive* terms found in the other's text. Distinctive terms come from its name and source slugs, appear in at most 2 entries' names and at most 6 entries' texts, and are not place names. The maximum over both directions is used. | 0.35 |
| `shared_docs` | The entries share a document cluster | +0.30 |

**Validation against the team's 13 known merges** (12 “redundant” rows plus the duplicated *not started* row). The algorithm ranks the team's target **first for 8 of the 13**. Recall against false positives by threshold:

| Score threshold | Known merges flagged | Pairs of confirmed, distinct pilots wrongly flagged |
|---|---|---|
| 0.30 | 10 / 13 | 12 |
| 0.40 | 6 / 13 | 6 |
| 0.55 (“likely”) | 5 / 13 | 0 |

The misses are merges whose documents share neither names nor content, for example *Guatemala City Pilot* → *Urban Oasis Colonia Bethania* and *Emissions Reduction Public Policy Pilot* → *Last-Mile Cargo*. Lexical matching alone cannot resolve these, so it is used to rank candidates for human review, never to decide.

`Candidates` lists, for each entity still to review, its 3 best partners with a score of at least 0.35, plus every pair at 0.55 or above, for 77 pairs in total. Following the scope decision, these are informational only.

## 4. Stage 3 — screening of unvalidated entries (`stage3_screen_unreviewed.py`)

**Question asked.** For each document filed under an unvalidated entry (“?”, doubtful or HUB-level): does its content belong to one or more of the validated pilots? The stage does not ask whether the entry is a pilot of its own.

**Profiles (3a).** The model needs to recognise a validated pilot even when a document never uses the registry name, so each of the 30 validated pilots (29 confirmed plus the ongoing one) gets a profile. The profile is built in one API call from:

- the pilot's registry metadata;
- up to 3 of its own documents, ranked final report > deliverable > progress report > presentation > work plan > TOR;
- the first 6,000 characters of each.

The model returns a Spanish `summary`, up to 15 `key_terms` and `period` as JSON constrained by a schema (`record_profile`; see *API call* below). The prompt is `prompts/profile_system.md`. Profiles are cached in `out/screening/profiles.json`.

**Screening (3b).** One call per distinct document. Each copy is screened once, through its cluster representative. The call has three parts:

- **System prompt:** `prompts/screen_system.md` plus the catalog of profiles. It is identical across calls and marked for prompt caching.
- **User turn:** the entries the document is filed under, the TC, the file name, and the text with `[page n]` / `[slide n]` / `[block n]` markers. Documents longer than 24,000 characters send the first 75% and the last 25% of that budget (the conclusions), with `[... omitted ...]` between them.
- **Answer:** JSON constrained by a schema (`record_screening`) with `verdict` (one of 5 values), `matches` (pilot, role primary/section/mention, confidence, verbatim quote, location), `document_subject` and `rationale`.

**Verification.** Each quote is searched in the document's full text, after normalising case, whitespace and hyphenation. A quote that is not found is shown as `[NOT FOUND IN TEXT]` and counted in `unverified_quotes`. It is never accepted silently.

**Aggregation:**

- **Per entity:** documents screened and documents matched. The proposal is one of:
  - fold the entry into a validated pilot, when all its documents belong to one;
  - move the matched documents;
  - stays out of scope.
- **Reassignments:** primary and section matches only, with an empty `team_decision` column. Stage 4 builds the knowledge bases with the accepted moves.

**Size (dry run, 2026-11-02).** 30 profile calls (about 130k input tokens) and 154 screening calls (about 607k document tokens, plus a catalog of about 6k tokens cached across calls). The default model is `claude-sonnet-5-5`; it can be changed with `--model` or the `CLAB_MODEL` variable.

**Caching and resumption.** Every answer is appended to `out/screening/responses.jsonl` under a key that hashes the prompt, the catalog, the document text and the model. A re-run only calls what changed. `--mock` runs the whole stage with a fake model and writes nothing to the real cache.

**API call.** Requests go to the Messages API with structured outputs: `output_config.format` = `json_schema`.

- Every object in the schema gets `additionalProperties: false`, and unsupported numeric and array constraints are removed (`strict_schema()`). Confidence is clamped to 0–1 in code.
- The first version forced a tool call. Claude Sonnet 5.5 rejects that with error 400 (*tool_choice: type "tool" and "any" are not supported for this model*), so it was replaced on 2026-11-02, following the [Sonnet 5.5 migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).
- Sonnet 5.5 thinks adaptively by default. `--effort` (`medium` by default) controls how much it thinks, and `--max-tokens` (16,000 by default) covers thinking plus the answer.
- Requests are streamed (`messages.stream` → `get_final_message()`). On 2026-11-03 stage 5's first live run failed on every pilot because the SDK refuses non-streaming requests whose `max_tokens` (32,000) could take over 10 minutes (*Streaming is required for operations that may take longer than 10 minutes*). Streaming is the SDK's recommended route for long requests and also keeps the connection alive on slow networks. Stages 3 and 5 share this code.
- Refusals and truncated answers raise a clear error. A call that fails is reported and left uncached, so the next run retries it. Profiles must be complete before screening starts.

**Testing done.**

- `--dry-run` and `--mock` were run end to end on the full corpus: 153–154 documents, quote verification and the Excel output.
- `tests/test_stage3_api_shape.py` checks the request sent to the API offline: no tools, a valid strict schema, prompt caching on the system prompt, parsing of the answer and clamping of confidence.
- First live run (2026-11-02, Juan's machine, `--limit 5`): error 400 on forced tool calls, fixed as described above.
- Second live run (2026-11-03, `--limit 5`): 30 profiles and 5 documents, all 5 correctly judged `unrelated_to_validated_pilots` (São Luís data diagnostics under BR-T1496).
    - Usage: 35 calls; 190k input tokens; 63k tokens written to cache and 32k read from it; 13k output tokens.
    - The real catalog is about 15k tokens. The dry-run estimate (6k) undercounts it because it uses placeholder summaries.
    - Four of the 5 screening calls each wrote the catalog to the cache because they started in parallel. Since then the first uncached call runs alone to warm the cache.
- The profiles were reviewed on 2026-11-03. *Bairro Conectado Terminal Sapopemba* (`CLAB-P-099`) and *Stairways Requalification Sao Paulo* (`CLAB-P-103`) describe the same consultancy, on stairway requalification in Sapopemba. Both are validated (✓). Whether they are one pilot or two is listed as an open item.

**Full run (2026-11-03).**

| Measure | Result |
|---|---|
| Documents screened | 154 |
| `unrelated_to_validated_pilots` | 125 |
| `covers_several_pilots` | 25 |
| `mentions_validated_pilot_only` | 4 |
| `belongs_to_validated_pilot` | 0 |
| Proposed moves | 109 (all `section`, all 109 quotes verified in the text) |
| Validated pilots that receive documents | 25 of 30 |
| Usage | 149 calls; 880k input tokens; 15k tokens written to cache, 2.22M read from it; 47k output tokens |

- **No unvalidated entry turned out to be a misfiled validated pilot.** What the screening recovers is multi-pilot documents: consultant progress reports (Data Science lead, IT consultant, Scetta), Roberto Madera's products, and the HUB documents. Each has sections on validated pilots.
- Several unvalidated entries are such reports filed under a pilot-like name. Examples: *Rutas Limpias Waste Collection Pilot* holds Antonio Vázquez Brust's progress reports 1–3, and *Digital Tool for Amazonian Cities* holds 7 monthly reports that together cover 15 validated pilots.
- The pilots that gain the most are *Automated Tree Inventory* (14 documents), *Smart Cities Pilot Tres de Febrero*, *Citizen Services Catalog* and *Land Identification Platform Cuenca* (9 each).
- Five pilots receive nothing: Tactical Urbanism Santo Domingo, Climate Shelters Belo Horizonte, Construction with Recycled Polymer Bales, Bairro Conectado Terminal Sapopemba and Stairways Requalification Sao Paulo.
- *Electric Micromobility Pilot* (doubtful) holds Roberto Madera's dissemination materials (Producto 4). They have sections on the Quito last-mile pilot and the Montevideo children's diagnosis, and nothing on micromobility.
- **Renca / Providencia.** The selection report (*Producto1_SeleccionPilotosBPR*) and Madera's Producto 3 describe a single joint proposal, “Santiago 2030 con Agua”. The HUB process evaluation lists them as two pilots, consistent with the 2026-11-03 split.

## 5. Stage 4 — knowledge base per pilot (`stage4_knowledge_base.py`)

**Scope.** The 29 pilots with `in_retro_scope = true`. San José is ongoing and is handled prospectively, so it is not included.

**Content of each knowledge base (`out/kb/<pilot_id>/`):**

- **Own documents.** Every document cluster filed under the pilot, read through its representative. This includes documents shared through a split. Documents are ordered: final report, deliverables, progress reports, presentations, work plan, TOR, media, then agreements, affidavits and invoices. Invoices are kept because they carry cost figures for L2.1.
- **Recovered sections.** Stage 3 moves to the pilot that the team has not rejected. A multi-pilot report has a section on this pilot next to sections on other pilots, so only part of it is kept:
    - the unit cited by the screening evidence;
    - every unit that contains one of the pilot's distinctive terms;
    - the neighbouring unit on each side of those;
    - up to `--section-units` (8) core units.

    A distinctive term is a key term from the stage-3 profile, or the pilot's city, that appears in **no other** validated pilot's profile. Shared names (consultants, TC codes, "Cities LAB") are dropped, because they would pull in other pilots' sections. Most progress reports are 4–10 pages, so in practice the kept part is the cited page and its neighbours.
- **`corpus.md`** is the text stage 5 reads. Every unit is headed `[doc_id · page n]`, so evidence can be cited down to the page.
- **`manifest.json`** records, per document: its source (own or recovered), role, units kept, term hits and screening evidence.

**Result (2026-11-03, on the full stage-3 output):**

- 29 knowledge bases, with 118 own documents and 103 recovered sections.
- Estimated size ranges from 4k tokens (*Stairways Requalification Sao Paulo*) to 222k (*Sustainability Corridors Pilot*), using 3.3 characters per token, the rate measured on stage-3 calls.
- Two pilots exceed the 150k-token context budget: *Sustainability Corridors* (222k) and *Respira Lima* (178k, which includes the merged Clean Air Routes reports). Stage 5 has to retrieve chunks for these two instead of sending the whole corpus.

## 6. Stage 5 — framework extraction and calibration

### 5a. Reference evaluations (`stage5_gold_workbooks.py`)

This script reads the five workbooks Sabrina completed by hand: Geothermal, Yaku, Montería, Sustainability Corridors and Last Mile. They are mapped to `pilot_id` in `data/gold_workbooks.csv`.

- The reader is a Python port of the tracker's in-app importer (`web/import.js`). It uses the same cover labels, the same stage tabs, and the same rule that the latest non-empty value across tabs wins. Degrees are normalised to high/medium/low/na, NA-like text becomes `no_evidence`, L2.1 is kept as free text, and L3.2 gets one hypothesis per distinct "Impact (n)" label.
- `tests/test_gold_parity.py` runs both importers on the same files and compares every value. All five workbooks match.
- The comparison found one difference, which was fixed. A year typed into a date cell, such as "2022" in Last Mile, is read by openpyxl as 1905-07-14. It is now converted back to the year, as the JS importer does.

### 5b. Extraction (`stage5_extract.py`, prompt `prompts/extract_system.md`)

- **Checklist.** It is generated from `catalog/framework_catalog.json`: definition, guiding questions, field types, enum options and `applies_to`. "Notes" fields are not requested, because each entry carries its own `note`.
    - The cover asks for: pilot_type, city, country, problem_definition, target_population, lead, counterpart and the two dates.
    - L2.1 asks for cost line items (original amount, currency, year, subcategory, what it covers), plus `opex.period` and `funding_note`. Conversion to USD and totals are left to code, never to the model.
    - L3.2 asks for up to 4 hypotheses (one testable claim per effect), each with the 10 hypothesis fields.
- **Calls.** There are four calls per pilot: cover + L1, L2, L3, L4. The system prompt is the instructions plus the pilot's `corpus.md`, prompt-cached, so the corpus is paid once and read three times. Answers are JSON constrained by one schema shared by the four calls (so the cache holds), with dimension codes and field keys as enums, and every entry has status, value, note, confidence and evidence (doc_id, location, verbatim quote).
- **Reading budget.** `--budget` defaults to 150k tokens. Documents go in knowledge-base order (final reports first). Beyond the budget the next document is cut and the rest are omitted. The corpus itself tells the model what was left out, and the output lists it.
    - Only *Sustainability Corridors* (222k) and *Respira Lima* (178k) exceed the budget.
    - For Sustainability Corridors this means the technical proposal is cut and 6 documents are omitted.
- **Checks in code:**
    - every requested field is present; a missing one becomes `no_evidence`, flagged *not returned by the extractor* and counted in `checks.not_returned` (labels such as `L1.1 Relevance` are normalised first);
    - enum values and ISO dates are validated;
    - every quote is searched in the cited document, and the doc_id is corrected if the quote is found in another document; unfound quotes are flagged and never dropped silently;
    - a gap must come with a note;
    - a filled field must have evidence;
    - a dimension that does not apply to the extracted pilot type is flagged;
    - confidence is clamped to 0–1.
- **Stage.** Each response is stored at the last stage where the catalog lists that field (the rule from the plan). Raw answers are cached per pilot and level.
- **Tested offline.** `tests/test_stage5_offline.py` uses a fake model whose answers include a verbatim quote, an invented quote, an invalid degree, a gap without a note and omitted fields, and checks that each case is detected. `--mock` runs the whole stage and the comparison end to end.
- **Calibration size (dry run, 2026-11-03).** The 5 pilots send about 294k corpus tokens, each written to the cache once and read three times.

### Calibration run 1 (2026-11-03, Juan's machine) and fixes

The first live run extracted the 5 pilots in 20 calls, with 135 of 139 quotes verified. Agreement with the workbooks on filled vs gap was 0.68 (Geothermal), 0.68 (Yaku), 0.64 (Last Mile), 0.25 (Montería) and 0.25 (Sustainability Corridors). The review found four causes.

1. **Dimension labels (bug).** For Montería and Sustainability Corridors the model wrote `L1.1 Relevance` instead of `L1.1` in Levels 1 and 4. Code looked the answers up by the exact code, so 19 answered fields were stored as "not returned". The check counted them as gaps but did not flag them, so the summary did not show it. Fixes:
    - the schema now restricts `dimension` and `field` to the valid codes and keys (enums);
    - code normalises labels as a safety net;
    - a field not returned is flagged and counted in `checks.not_returned`.

   Replaying run 1's answers through the fixed code, without new calls, raises agreement to 0.86, 0.86, 0.64, 0.64 and 0.86.
2. **Prompt cache (cost).** 1.27M tokens were written to the cache and only 0.42M read. The schema changed between levels, and the prompt cache covers `output_config.format` ([structured outputs docs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs): *"Changing the output_config.format parameter will invalidate any prompt cache"*). The four calls now share one schema, and levels that do not ask for costs or hypotheses return those arrays empty.
3. **Hypotheses.** The workbooks have 3 hypotheses per pilot. The model returned one compound hypothesis (four pilots) or none (Last Mile, where it read the GHG objective as "no hypothesis beyond the stated objective"). The L3.2 instruction now asks for:
    - one testable claim per effect, with compound objectives split;
    - stated objectives and planned indicators restated as hypotheses and marked as reconstructed;
    - a search across urban or environmental, behavioural and institutional effects, usually 2 to 4.
4. **Degrees and evaluative fields.** On the 33 degrees both sides graded, the model never rated above the workbook: of 21 workbook "high", it gave 3 high, 17 medium and 1 low. The model lowered degrees for unanswered guiding questions, and returned `no_evidence` when no document carried out the evaluation itself (L1.5 regulations in 4 of 5 pilots, some L4 lessons). Two new rules address this:
    - rule 3: judge from the facts reported, and do not wait for a document that states the judgment;
    - rule 6: unanswered questions lower confidence, not the degree (decision 2026-11-03).

The workbooks may also draw on knowledge the documents lack. In Last Mile, the workbook states that the Cities LAB did not deploy the vehicles. Remaining differences go to the `review` column.

### Calibration run 2 (2026-11-04, Juan's machine)

The second run used the fixes from run 1, again 20 calls. The cache now works: 0.43M tokens were written and 1.30M read, against 1.27M and 0.42M before. All requested fields came back (`not_returned` = 0), and 264 of 268 quotes were verified. The model now returns 2–3 hypotheses per pilot.

| Pilot | Agreement, filled vs gap | Degree, exact | Degree, within one step |
|---|---|---|---|
| Geothermal | 0.96 | 0.22 | 1.00 |
| Yaku | 0.96 | 0.11 | 1.00 |
| Montería | 0.72 | 0.60 | 1.00 |
| Sustainability Corridors | 0.96 | 0.89 | 1.00 |
| Last Mile | 1.00 | 0.62 | 1.00 |

Agreement here follows the scoring adopted after run 2, described below.

- **Information outside the repository.** Some fields the workbooks fill and the model leaves as gaps rest on sources that are not in the pilot's knowledge base:
    - Geothermal's lifecycle costs (US$11,500 vs US$1,600; savings of 1.43 t CO2 a year);
    - Yaku's cost per m² (USD 749.36);
    - Montería's POT 2021–2032.

  None of these appears in the corpus, so the extractor is right to leave them as gaps. If those sources exist, such as a results presentation, adding them to `downloads_pilots` closes the gap.
- **Montería** has documents only up to the co-creation stage, so results (L3) cannot be assessed. The workbook says so with degree NA, and the extractor returns `no_evidence`.
- **Pilot type.** For Sustainability Corridors the extractor says `methodology` and the workbook says `urban`. The team decides.
- **Degrees** still sit one step below the workbook. Of 26 workbook "high", the model gave 7 high and 19 medium. Some of its reasons are real weaknesses the documents show (Geothermal L1.4: staff did not take part, and there was no co-creation). Others still reflect what was not documented. Rule 6 now gives anchors for high, medium and low and says that `medium` is not a way to express uncertainty.

Changes after run 2:

- **Compare scoring.** Cells the evaluator left blank are no longer scored; the extractor's answers there are counted separately (`model_adds_where_gold_blank`). A workbook degree NA counts as "not graded", the same answer as the extractor's `no_evidence` / `not_applicable`.
- **Degree evidence.** A degree with no quotes of its own is not flagged when its justification has evidence.

### 5c. Comparison (`stage5_compare.py`)

`out/calibration.xlsx` compares, per field:

- status, where filled vs not filled agrees when both filled or both left a gap;
- degrees, as an exact match and as a match within one step;
- hypotheses, texts side by side;
- costs, the workbook's text against the extractor's line items;
- the cover fields.

Text fields are not scored automatically. They appear side by side with the extractor's evidence, and a person records in a `review` column which side is right. The workbooks are a reference, not ground truth: the SPEC already lists corrections for them (currency, classification, counterfactual wording).

## 7. Open items

- [x] RG-T4182, Providencia. The HUB process evaluation (*4. Informe Pilotos V1.docx*) lists four pilots:
    1. YAKU, in Quito;
    2. Geotermia, in San Salvador;
    3. Gestión Hídrica y Uso Eficiente del Agua, in Renca (Condominio Vista Hermosa);
    4. Gestión Hídrica y Uso Eficiente del Agua, in Providencia (a municipal building).

    The corpus has no document linking tree detection to Providencia. Resolved: Providencia is split from Renca (see the decision log).
- [ ] With the team: what *Urban Tree Detection* (Belém, San José del Guaviare) and *Urban Oasis Colonia Bethania* (Guatemala City) are relative to RG-T4182. Consultant reports list the HUB work as "Piloto en Renca y Providencia" and "Piloto en Guatemala". Both stay in scope for now, with whatever gaps their documents leave.
- [ ] With the team: *Bairro Conectado Terminal Sapopemba* (`CLAB-P-099`, 3 documents) and *Stairways Requalification Sao Paulo* (`CLAB-P-103`, 1 document, a work plan) describe the same SMUL/C40 consultancy on stairway requalification in Sapopemba. Are they one pilot or two?
- [ ] Big Data for Smart Cities (BR-T1496, 51 documents) holds proofs of concept per city and theme. Decide whether it is one pilot or several. It is not validated, so it is out of scope unless the team validates it.
- [ ] Electric Micromobility Pilot (Quito): the team suspects its files belong to San Telmo (Buenos Aires) and green roofs (San Salvador). Stage 3 screens them.
- [ ] Before stage 6: source of the exchange rates used to convert costs to USD.
