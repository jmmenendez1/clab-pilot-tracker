# Retrospective evaluation — methods, decisions and results

This file records what each stage of the retrospective pipeline does, the parameters it uses, how it was checked, and the team decisions it encodes. The pipeline is in `retro/` and is run from that folder. The plan it implements is the guide *Plan guía — Evaluación retrospectiva automatizada de pilotos*, a Claude Doc shared with the team.

## 1. Decisions

| Date | Decision | Who | Where it is applied |
|---|---|---|---|
| 2026-11-02 | Everything runs locally. Model calls use Juan's Anthropic API key, and nothing derived from documents is committed to git. | Juan | `README.md` (data policy), `.gitignore` |
| 2026-11-02 | The team's *Review* / *Notes* columns are binding, and the algorithms never reopen them. | Juan | `stage2_registry.py` → `parse_decisions` |
| 2026-11-02 | Missing information is loaded as `no_evidence` with a note for framework fields, and as `Unknown` without a note for cover fields. Fields are never dropped. | Juan | stage 5 (planned) |
| 2026-11-02 | **Scope.** Only the pilots the team validated (✓) are evaluated. The unreviewed (“?”) entries are not classified as pilots or non-pilots. They are only screened, to recover documents that belong to validated pilots. | Juan | `stage2_registry.py` → `in_retro_scope`; `stage3_screen_unreviewed.py` |
| 2026-11-02 | The Cities HUB (RG-T4182) is **not** evaluated as a pilot. That cooperation contains 4 pilots. The two HUB-level entries (*Public Innovation Pilots*, *Urban Experimentation Projects*) are documents to distribute among the pilots they cover. | Juan | stage 2 status `umbrella_not_a_pilot`; stage 3 screens their documents |
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

Result: **92 entities.**

| Status | Entities |
|---|---|
| confirmed | 29 |
| to_review | 57 |
| umbrella_not_a_pilot | 2 |
| doubtful_files | 1 |
| ongoing | 1 |
| not_started | 1 |
| never_happened | 1 |

`in_retro_scope` is true for the 29 confirmed.

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

**Question asked.** For each document filed under an unvalidated entry (“?”, doubtful or umbrella): does its content belong to one or more of the validated pilots? The stage does not ask whether the entry is a pilot of its own.

**Profiles (3a).** The model needs to recognise a validated pilot even when a document never uses the registry name, so each of the 30 validated pilots (29 confirmed plus the ongoing one) gets a profile. The profile is built in one API call from:

- the pilot's registry metadata;
- up to 3 of its own documents, ranked final report > deliverable > progress report > presentation > work plan > TOR;
- the first 6,000 characters of each.

The model returns a Spanish `summary`, up to 15 `key_terms` and `period` through a forced tool call (`record_profile`). The prompt is `prompts/profile_system.md`. Profiles are cached in `out/screening/profiles.json`.

**Screening (3b).** One call per distinct document. Each copy is screened once, through its cluster representative. The call has three parts:

- **System prompt:** `prompts/screen_system.md` plus the catalog of profiles. It is identical across calls and marked for prompt caching.
- **User turn:** the entries the document is filed under, the TC, the file name, and the text with `[page n]` / `[slide n]` / `[block n]` markers. Documents longer than 24,000 characters send the first 75% and the last 25% of that budget (the conclusions), with `[... omitted ...]` between them.
- **Answer:** a forced tool call (`record_screening`) with `verdict` (one of 5 values), `matches` (pilot, role primary/section/mention, confidence, verbatim quote, location), `document_subject` and `rationale`.

**Verification.** Each quote is searched in the document's full text, after normalising case, whitespace and hyphenation. A quote that is not found is shown as `[NOT FOUND IN TEXT]` and counted in `unverified_quotes`. It is never accepted silently.

**Aggregation:**

- **Per entity:** documents screened and documents matched. The proposal is one of:
  - fold the entry into a validated pilot, when all its documents belong to one;
  - move the matched documents;
  - stays out of scope.
- **Reassignments:** primary and section matches only, with an empty `team_decision` column. Stage 4 builds the knowledge bases with the accepted moves.

**Size (dry run, 2026-11-02).** 30 profile calls (about 127k input tokens) and 153 screening calls (about 604k document tokens, plus a catalog of about 6k tokens cached across calls). The default model is `claude-sonnet-5-5`; it can be changed with `--model` or the `CLAB_MODEL` variable.

**Caching and resumption.** Every answer is appended to `out/screening/responses.jsonl` under a key that hashes the prompt, the catalog, the document text and the model. A re-run only calls what changed. `--mock` runs the whole stage with a fake model and writes nothing to the real cache.

**Testing done.**

- `--dry-run` and `--mock` were run end to end on the full corpus: 153 documents, quote verification and the Excel output.
- The live API path was not run from the development environment, because it needs Juan's key. Run it first with `--limit 5`.

## 5. Open items

- [ ] Name the 4 pilots of RG-T4182. Six RG-T4182 entries are marked ✓ (Urban Tree Detection, Urban Oasis Colonia Bethania, Water Consumption Reduction in Buildings, Collaborative Regional Platform, Geothermal Climate Prototype, Proyecto Yaku). Either 2 of them belong elsewhere, or the 4 refers to the BPR pilots only. The HUB-level documents mention Quito, San Salvador, Renca and Providencia.
- [ ] Big Data for Smart Cities (BR-T1496, 51 documents) holds proofs of concept per city and theme. Decide whether it is one pilot or several. It is not validated, so it is out of scope unless the team validates it.
- [ ] Electric Micromobility Pilot (Quito): the team suspects its files belong to San Telmo (Buenos Aires) and green roofs (San Salvador). Stage 3 screens them.
- [ ] Before stage 6: source of the exchange rates used to convert costs to USD.
