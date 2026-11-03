# Retrospective evaluation pipeline

These scripts apply the Pilot Evaluation Framework to the Cities LAB's past pilots. They start from the documents the retrieval pipeline downloaded from the TC SharePoint sites and end with a pilot registry, a knowledge base per pilot and, later, the framework fields extracted and loaded into the tracker.

What each stage does and why, the parameters, the validation results and the team decisions are in [`METHODS.md`](METHODS.md). The prompts sent to the model are in [`prompts/`](prompts/).

| Stage | Script | Input | Output | Status |
|---|---|---|---|---|
| 1 | `stage1_extract.py` | `downloads_pilots/` | full text per document, duplicates and versions (`out/documents.csv`, `out/text/`) | done |
| 2 | `stage2_registry.py` | repository workbook (Review / Notes) + stage 1 | `out/pilot_registry.xlsx` | done; awaiting team review |
| 3 | `stage3_screen_unreviewed.py` | stage 2 + Anthropic API | `out/screening_unreviewed.xlsx`, `out/document_reassignments.csv` | done (2026-11-03) |
| 4 | `stage4_knowledge_base.py` | stages 2–3 (+ team rejections) | `out/kb/<pilot_id>/manifest.json`, `corpus.md`, `out/kb/kb_summary.csv` | done (2026-11-03) |
| 5a | `stage5_gold_workbooks.py` | hand-made evaluation workbooks | `out/gold/<pilot_id>.json` | ready |
| 5b | `stage5_extract.py` | stage 4 + catalog + Anthropic API | `out/extraction/<pilot_id>.json` | ready; calibrate first |
| 5c | `stage5_compare.py` | 5a + 5b | `out/calibration.xlsx` | ready |
| 6 | loader into the tracker | stage 5 | rows in Supabase | planned |

## Setup (Windows, once)

```powershell
cd retro
pip install -r requirements.txt
```

A virtual environment is optional. If you want one, create it once with `python -m venv .venv` and activate it in each new window with `.venv\Scripts\activate`; without it, the packages go into your main Python and no activation step is needed.

- **OCR is optional.** Scanned PDF pages are read with Tesseract (languages `spa`, `por`, `eng`). Without it, those pages come out empty and `documents.csv` reports them. To skip OCR altogether, unzip the shared `text_cache.zip` into `out/` before stage 1: stage 1 reuses every text it finds there.
- **Long paths are handled.** Stage 1 adds the `\\?\` prefix on Windows, because OneDrive paths exceed the 260-character limit.
- **The API key never goes in the repository.** Stage 3 reads `ANTHROPIC_API_KEY` from the environment. You can set it for the session:

  ```powershell
  $env:ANTHROPIC_API_KEY = "sk-ant-..."
  ```

  Or copy `.env.example` to `.env`, which git ignores, and load it yourself.

## Running

```powershell
# Stage 1 - text, duplicates, versions (about 2 minutes; OCR adds about 1 minute)
python stage1_extract.py --src "C:\Users\<you>\OneDrive - Inter-American Development Bank Group\Documents\Pilots Evaluation\idb_agent\downloads_pilots" --repo "<path>\clab_pilot_repository.xlsx" --out out

# Stage 2 - team decisions + duplicate candidates -> out\pilot_registry.xlsx
python stage2_registry.py --repo "<path>\clab_pilot_repository.xlsx" --stage1 out --out out

# Stage 3 - screen the unvalidated entries' documents against the validated pilots
python stage3_screen_unreviewed.py --out out --dry-run     # what would be sent, token estimate, no API call
python stage3_screen_unreviewed.py --out out --limit 5     # try 5 documents
python stage3_screen_unreviewed.py --out out               # full run; re-running resumes and only calls what changed

# Stage 4 - one knowledge base per validated pilot (no API calls, seconds)
python stage4_knowledge_base.py --out out

# Stage 5 - calibration on the 5 hand-made workbooks, then all pilots
python stage5_gold_workbooks.py --out out --workbooks gold_workbooks   # workbooks -> out/gold/
python stage5_extract.py --out out --gold-only --dry-run                # token estimate, no API call
python stage5_extract.py --out out --gold-only                          # extract the 5 calibration pilots
python stage5_compare.py --out out                                      # -> out/calibration.xlsx
python stage5_extract.py --out out                                      # all pilots, after the prompts are tuned
```

Offline tests (no key needed): `python tests/test_stage3_api_shape.py` (API request shape) and `python tests/test_stage5_offline.py out CLAB-P-083` (stage-5 checks). `tests/test_gold_parity.py` checks the Python workbook importer against the tracker's JS importer (needs Node).

Every stage can be re-run safely. Stage 1 reuses extracted text. Stage 3 keeps its API answers in `out/screening/` and only repeats a call when the document, the prompt or the model changes.

## What the team edits

| File | Purpose |
|---|---|
| Repository workbook, columns *Review* / *Notes* | Binding decisions: ✓, redundant → target, never happened, not started, ongoing. Stage 2 parses them. |
| `data/team_decisions_overrides.csv` | Corrections where the parser misread a note, and decisions taken after the repository was reviewed. One row per entry, with a `comment` explaining why. |
| `data/pilot_splits.csv` | One repository entry that is really two pilots, e.g. Renca and Providencia. |
| `out/pilot_registry.xlsx` → `final_decision`, `reviewer` | Decisions on the duplicate candidates. |
| `out/document_reassignments.csv` → `team_decision` | Proposed document moves from stage 3. **Empty = accepted** (decision 2026-11-03); write `reject` to exclude one. Re-running stage 3 keeps these decisions. |

## Data policy

`out/` is git-ignored. It holds document text, excerpts and model answers derived from IDB documents, and none of that goes to GitHub. Only code, prompts, the overrides file and documentation are versioned.
