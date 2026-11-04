"""Stage 5b - Framework extraction per pilot (Anthropic API).

Why
    Applies the Pilot Evaluation Framework to each validated pilot's knowledge base (stage 4) and returns, for every
    field the framework asks for, either a value with verbatim evidence or an explicit gap (no_evidence with a note
    saying what was searched). Output is the intermediate JSON the tracker loader (stage 6) will read; the same
    format as the gold workbooks (stage 5a), so stage 5c can compare them.

How
    - The field checklist is generated from catalog/framework_catalog.json (definitions, guiding questions, field
      types, enum options, applies_to). Changing the catalog changes the extraction; no code change.
    - Four calls per pilot: (1) cover + Level 1, (2) Level 2 (with cost line items), (3) Level 3 (with impact
      hypotheses), (4) Level 4. The system prompt = prompts/extract_system.md + the pilot's corpus.md, prompt-cached,
      so calls 2-4 read the corpus from cache. Answers are JSON constrained by one schema shared by the four calls
      (structured outputs; a schema that changed per call would break the cache), with dimension codes and field
      keys as enums.
    - Corpora above --budget tokens are packed: documents in knowledge-base order (final reports first) until the
      budget; the next document is cut and the rest are omitted. Omitted documents are listed in the output and
      in the corpus itself, so gaps can be traced to the cut.
    - Checks in code after each answer:
        * every requested (dimension, field) is present; missing ones become no_evidence, flagged "not returned by the
          extractor" and counted in checks.not_returned (labels like `L1.1 Relevance` are normalised to `L1.1` first).
          Before that, a degree / justification left out next to its partner's gap takes the partner's status and
          note, and the rest are asked for once more in a small follow-up call (cached as <pid>_L<n>_followup.json);
        * enum values (degree, on_time, attribution_method, opex.period, pilot_type) and dates are validated;
        * every evidence quote is searched in the cited document (case/whitespace-insensitive); if found in another
          document the doc_id is corrected; quotes not found are flagged and the field's status is kept but marked
          `unverified`;
        * not filled without a note gets a flag.
    - Each response gets the stage at which the catalog matures that field (last stage listing it).
    - Raw answers are cached per (pilot, level) under a key hashing prompt, corpus, checklist and model, so re-runs
      only call what changed.

Usage (from retro/, after stage 4)
    python stage5_extract.py --out out --gold-only --dry-run   # the 5 calibration pilots: requests + token estimate
    python stage5_extract.py --out out --gold-only             # calibration run
    python stage5_extract.py --out out --pilots CLAB-P-083     # one pilot
    python stage5_extract.py --out out                         # all pilots in scope
    python stage5_extract.py --out out --gold-only --mock      # offline end-to-end test

Writes
    out/extraction/<pilot_id>.json        pilot (cover), responses, hypotheses, cost_items, corpus info, checks, usage
    out/extraction/raw/<pilot_id>_<level>.json   raw model answer + cache key
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from stage3_screen_unreviewed import DEFAULT_MODEL, Model  # noqa: E402
from stage5_gold_workbooks import find_catalog  # noqa: E402
from evaluability import for_extraction  # noqa: E402

CHARS_PER_TOKEN = 3.3
STATUSES = ["filled", "no_evidence", "not_applicable"]
DEGREES = ["high", "medium", "low", "na"]
COVER_FIELDS = [
    ("pilot_type", "Pilot type", "enum: urban | digital | behavioral | institutional | methodology. Urban = physical intervention in "
     "space; digital = data/software/digital tool; behavioral = nudges/behaviour change; institutional = rules, financing or "
     "governance instruments; methodology = an innovation methodology (process-based)."),
    ("city", "City (or cities) where the pilot ran", "text"),
    ("country", "Country", "text"),
    ("problem_definition", "Problem the pilot addresses", "longtext"),
    ("target_population", "Target population / beneficiaries", "longtext"),
    ("lead", "Lead / responsible (IDB team, implementing firm or consultant)", "text"),
    ("counterpart", "Local counterpart / implementer", "text"),
    ("date_started", "Date the pilot started", "date YYYY-MM-DD, or YYYY-MM / YYYY if only that is known"),
    ("date_ended", "Date the pilot ended", "date YYYY-MM-DD, or YYYY-MM / YYYY if only that is known"),
]
COST_SUBCATS = ["capex.construction", "capex.equipment", "capex.software", "capex.studies", "capex.training", "capex.other",
                "opex.personnel", "opex.facilities", "opex.materials", "opex.program_inputs", "opex.beneficiary_inputs", "opex.other"]

EVIDENCE = {"type": "array", "items": {"type": "object", "properties": {
    "doc_id": {"type": "string"}, "location": {"type": "string"}, "quote": {"type": "string"}},
    "required": ["doc_id", "location", "quote"]}}
FIELD = {"type": "object", "properties": {
    "dimension": {"type": "string"}, "field": {"type": "string"}, "status": {"type": "string", "enum": STATUSES},
    "value": {"type": "string"}, "note": {"type": "string"}, "confidence": {"type": "number"}, "evidence": EVIDENCE},
    "required": ["dimension", "field", "status", "value", "note", "confidence", "evidence"]}
HYP = {"type": "object", "properties": {"label": {"type": "string"}, "fields": {"type": "array", "items": FIELD}},
       "required": ["label", "fields"]}
COST = {"type": "object", "properties": {
    "subcategory": {"type": "string", "enum": COST_SUBCATS}, "label": {"type": "string"},
    "amount_original": {"anyOf": [{"type": "number"}, {"type": "null"}]}, "currency": {"type": "string"},
    "year": {"type": "string"}, "covers": {"type": "string"}, "evidence": EVIDENCE},
    "required": ["subcategory", "label", "amount_original", "currency", "year", "covers", "evidence"]}


def sha(s, n=16):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def fold(s):
    s = unicodedata.normalize("NFKC", str(s)).replace("­", "").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"-\s*\n\s*", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ------------------------------------------------------------------------------------------------ catalog -> checklist
def dim_fields(dim, catalog):
    f = dim["fields"]
    return f if isinstance(f, list) else catalog["field_sets"][f]


def stage_of(dim, key):
    """Last stage at which the catalog lists this field (retrospective responses are stored there)."""
    st = None
    for s in ("ideation", "design", "implementation", "evaluation"):
        keys = dim.get("stages", {}).get(s, [])
        if "*" in keys or key in keys:
            st = s
    return st or "evaluation"


def field_line(f):
    t = f.get("type", "text")
    if t == "degree":
        return f"- `{f['key']}`: degree, one of high | medium | low | na"
    if t == "enum":
        return f"- `{f['key']}`: one of {' | '.join(f['options'])}" + (f" ({f.get('help')})" if f.get("help") else "")
    return f"- `{f['key']}`: {t}" + (f" — {f['help']}" if f.get("help") else "")


def checklist(level, catalog):
    """(text for the user turn, list of required (dimension, field) pairs, hypothesis field keys or None)."""
    lines, pairs, hyp_keys = [], [], None
    if level == 1:
        lines.append("## Cover (dimension `COVER`)")
        for k, label, t in COVER_FIELDS:
            lines.append(f"- `{k}`: {label} — {t}")
            pairs.append(("COVER", k))
    for dim in [d for d in catalog["dimensions"] if d["level"] == level]:
        lines.append(f"\n## {dim['code']} {dim['name']}" + ("" if "all" in dim["applies_to"] else f" (applies to: {', '.join(dim['applies_to'])}; {dim.get('applies_note', '')})"))
        if dim.get("definition"):
            lines.append(f"Definition: {dim['definition']}")
        if dim.get("guiding_questions"):
            lines.append("Guiding questions: " + " ".join(dim["guiding_questions"]))
        fields = [f for f in dim_fields(dim, catalog) if f["key"] != "notes"]
        if dim.get("repeatable") == "hypothesis":
            hyp_keys = [f["key"] for f in fields]
            lines.append("Return these in `hypotheses`, not in `fields`: one item per expected effect, each with one entry "
                         "per field below (dimension `L3.2`). A hypothesis is one testable claim about one effect, measurable "
                         "with one indicator: split a compound objective (\"improves comfort, reduces energy use and is "
                         "accepted\") into separate hypotheses. The pilot's stated objectives, expected results and planned "
                         "indicators count as hypotheses even when the documents never use that word: restate each as a "
                         "testable claim, cite the passage and say in `note` that it was reconstructed from the objective. "
                         "Look for effects on the urban or environmental outcome, on users' behaviour or perception, and on "
                         "institutional capacity or adoption. Usually 2 to 4 (at most 4). Return an empty list only if the "
                         "documents state no objective or expected effect at all, and then explain in a `fields` entry "
                         "(dimension `L3.2`, field `hypothesis`, status no_evidence).")
            lines += [field_line(f) for f in fields]
            continue
        if dim["code"] == "L2.1":
            lines.append("Return every cost figure the documents give in `cost_items`: the amount exactly as written "
                         "(amount_original, currency, year), what it covers, and the capex/opex subcategory. Do not convert "
                         "currencies and do not sum. Also return these fields:")
            fields = [f for f in fields if f.get("group") is None]  # money subcategories come from cost_items
        for f in fields:
            lines.append(field_line(f))
            pairs.append((dim["code"], f["key"]))
    return "\n".join(lines), pairs, hyp_keys


def build_schema(catalog):
    """One schema for all four levels, with the valid dimension codes and field keys as enums.

    Why one schema: the prompt cache covers output_config.format, so a schema that changes between levels makes
    every call rewrite the corpus to the cache instead of reading it (seen in the first calibration run:
    1.27M tokens written, 0.42M read). Why enums: in that run the model sometimes wrote `L1.1 Relevance` instead of
    `L1.1`, and those answers were lost as "not returned". Arrays a level does not ask for come back empty."""
    codes = ["COVER"] + [d["code"] for d in catalog["dimensions"]]
    keys = [k for k, _, _ in COVER_FIELDS]
    for d in catalog["dimensions"]:
        for f in dim_fields(d, catalog):
            if f["key"] != "notes" and f["key"] not in keys:
                keys.append(f["key"])
    field = {**FIELD, "properties": {**FIELD["properties"], "dimension": {"type": "string", "enum": codes},
                                      "field": {"type": "string", "enum": keys}}}
    hyp = {**HYP, "properties": {**HYP["properties"], "fields": {"type": "array", "items": field}}}
    return {"type": "object", "properties": {"fields": {"type": "array", "items": field},
                                             "cost_items": {"type": "array", "items": COST},
                                             "hypotheses": {"type": "array", "items": hyp}},
            "required": ["fields", "cost_items", "hypotheses"]}


def norm_dim(s):
    """Safety net for labels such as `L1.1 Relevance` or `**L1.1**`."""
    m = re.search(r"COVER|L\d\.\d", str(s or ""), re.I)
    return m.group(0).upper() if m else s


def norm_key(s):
    return re.sub(r"[`*\s]", "", str(s or "")).lower()


# ------------------------------------------------------------------------------------------------ corpus
def load_corpus(kb_dir, budget_tokens):
    text = (kb_dir / "corpus.md").read_text(encoding="utf-8")
    parts = re.split(r"\n(?=## (?:Document|Section of document) )", text)
    header, docs = parts[0], parts[1:]
    budget = int(budget_tokens * CHARS_PER_TOKEN)
    kept, used, cut, omitted = [header], len(header), None, []
    for d in docs:
        name = d.splitlines()[0][3:]
        if used + len(d) <= budget:
            kept.append(d)
            used += len(d)
        elif cut is None and budget - used > 4000:
            kept.append(d[: budget - used] + "\n[... document cut here for length ...]")
            cut, used = name, budget
        else:
            omitted.append(name)
    note = ""
    if cut or omitted:
        note = ("\n\n[Corpus note: the knowledge base exceeds the reading budget. "
                + (f"Cut: {cut}. " if cut else "") + (f"Omitted: {'; '.join(omitted)}." if omitted else "") + "]")
    corpus = "\n\n".join(kept) + note
    # per-document text for quote verification (full texts, not only the kept part)
    units = {}
    for m in re.finditer(r"\[([0-9a-f]{12}) · (\w+) (\d+)\]\n(.*?)(?=\n\n\[[0-9a-f]{12} · |\n\n## |\Z)", text, re.S):
        units.setdefault(m.group(1), []).append(m.group(4))
    doc_text = {k: fold(" ".join(v)) for k, v in units.items()}
    return corpus, {"chars_total": len(text), "chars_sent": len(corpus), "est_tokens_sent": int(len(corpus) / CHARS_PER_TOKEN),
                    "cut": cut, "omitted": omitted}, doc_text


# ------------------------------------------------------------------------------------------------ checks
def check_evidence(ev_list, doc_text):
    out = []
    for ev in ev_list or []:
        q = fold(ev.get("quote", "")).strip(" .\"'")
        found_in = None
        if len(q) >= 12:
            if q in doc_text.get(ev.get("doc_id", ""), ""):
                found_in = ev["doc_id"]
            else:
                found_in = next((d for d, t in doc_text.items() if q in t or (len(q) > 40 and q[5:-5] in t)), None)
        out.append({**ev, "verified": found_in is not None, **({"doc_id_corrected_from": ev.get("doc_id")} if found_in and found_in != ev.get("doc_id") else {}),
                    "doc_id": found_in or ev.get("doc_id")})
    return out


MONTHS = {m: i + 1 for i, names in enumerate([
    "january enero janeiro jan ene", "february febrero fevereiro feb fev", "march marzo março marco mar",
    "april abril apr abr", "may mayo maio", "june junio junho jun", "july julio julho jul",
    "august agosto aug ago", "september septiembre setembro sep set sept", "october octubre outubro oct out",
    "november noviembre novembro nov", "december diciembre dezembro dec dic dez"]) for m in names.split()}


def norm_date(v):
    """The model sometimes writes a date with commentary ('September 2025 (Final Report, Product 5)').
    Returns (ISO date at the precision given, remaining text) or (None, v) when no date is found.
    Accepts YYYY-MM-DD, YYYY-MM, 'D Month YYYY', 'Month YYYY' and a bare year (en/es/pt month names)."""
    t = str(v or "").strip()
    m = re.match(r"\D{0,25}?(\d{4}-\d{2}-\d{2}|\d{4}-\d{2})\b", t)
    if m:
        return m.group(1), t[m.end():].strip(" ;,.()")
    m = re.match(r"\D{0,25}?\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:de\s+)?([A-Za-zçÇ]+)\.?\s+(?:de\s+|del\s+)?(\d{4})\b", t)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}", t[m.end():].strip(" ;,.()")
    m = re.match(r"\D{0,25}?\b([A-Za-zçÇ]+)\.?\s+(?:de\s+|del\s+)?(\d{4})\b", t)
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1).lower()]:02d}", t[m.end():].strip(" ;,.()")
    m = re.match(r"\D{0,25}?\b((?:19|20)\d{2})\b", t)
    if m:
        return m.group(1), t[m.end():].strip(" ;,.()")
    return None, t


def validate(entry, ftype, options, doc_text):
    flags = []
    st = entry.get("status")
    if st not in STATUSES:
        flags.append(f"invalid status {st!r}")
        entry["status"] = st = "no_evidence"
    v = (entry.get("value") or "").strip()
    if st == "filled":
        if ftype == "degree" and v.lower() not in DEGREES:
            flags.append(f"invalid degree {v!r}")
        if ftype == "enum" and options and v not in options:
            flags.append(f"value {v!r} not in {options}")
        if ftype == "date" and v and not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", v):
            iso, rest = norm_date(v)
            if iso:  # keep the date, move the commentary to the note
                entry["value"] = iso
                entry["note"] = ((entry.get("note") or "").strip() + f" [Date as written: {v}]").strip()
            else:
                flags.append(f"date {v!r} not ISO")
        if ftype == "degree":
            entry["value"] = v.lower()
        if not entry.get("evidence"):
            flags.append("filled without evidence")
    else:
        if not (entry.get("note") or "").strip():
            flags.append(f"{st} without a note")
        entry["value"] = None
    entry["evidence"] = check_evidence(entry.get("evidence"), doc_text)
    if entry["evidence"] and not all(e["verified"] for e in entry["evidence"]):
        flags.append("unverified quote")
    entry["confidence"] = min(1.0, max(0.0, float(entry.get("confidence") or 0)))
    if entry.get("missing_in_answer"):
        flags.append("not returned by the extractor")
    entry["flags"] = flags
    return entry


# ------------------------------------------------------------------------------------------------ one pilot
def cached_call(model, system, user, key, raw_path, catalog, mock):
    """One API call, cached on disk under `key` (prompt + corpus + request + model + schema)."""
    cached = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else None
    if cached and cached.get("key") == key and not model.mock:
        return cached["answer"], {}
    ans, usage = model.call(system, user, {"input_schema": build_schema(catalog)}, mock)
    if not model.mock:
        raw_path.write_text(json.dumps({"key": key, "time": time.strftime("%Y-%m-%d %H:%M"), "answer": ans, "usage": usage},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
    return ans, usage


def pair_gaps(got, pairs):
    """A degree and its justification are one judgment. When the model returns one of them as a gap
    (no_evidence / not_applicable) and leaves the other out, the missing one takes the same status and note."""
    for d, f in pairs:
        if f not in ("degree", "justification") or (d, f) in got:
            continue
        other = got.get((d, "justification" if f == "degree" else "degree"))
        if other and other.get("status") in ("no_evidence", "not_applicable"):
            got[(d, f)] = {"dimension": d, "field": f, "status": other["status"], "value": "", "confidence": other.get("confidence", 0),
                           "evidence": [], "note": other.get("note") or "Same as the dimension's other field.", "paired_gap": True}


def followup_text(level, miss, miss_h, hyps, dims, catalog):
    lines = [f"Follow-up for Level {level}. Your answer for this level left out the entries below. Return exactly these, "
             "following the same rules (verbatim evidence for anything filled; a gap with a note saying what you looked for). "
             "Leave every other entry out."]
    if miss:
        lines.append("\nIn `fields`:")
        for d, f in miss:
            if d == "COVER":
                lines.append(f"- `COVER` / `{f}`: " + next(f"{lab} — {t}" for k, lab, t in COVER_FIELDS if k == f))
            else:
                fdef = next(x for x in dim_fields(dims[d], catalog) if x["key"] == f)
                lines.append(f"- `{d}` {dims[d]['name']} / " + field_line(fdef)[2:])
    if miss_h:
        lines.append("\nIn `hypotheses` (one item per hypothesis, `label` exactly as given, dimension `L3.2`):")
        for i, label, keys in miss_h:
            stmt = next((x.get("value") for x in hyps[i].get("fields", []) if norm_key(x.get("field")) == "hypothesis"), "")
            lines.append(f'- label "{label}" (hypothesis: {stmt}): fields ' + ", ".join(f"`{k}`" for k in keys))
    return "\n".join(lines)


def extract_pilot(pid, model, catalog, out, budget, dry_run, mock_run=False):
    kb = out / "kb" / pid
    if not (kb / "corpus.md").exists():
        print(f"  ! {pid}: no knowledge base (run stage 4)")
        return None
    corpus, cinfo, doc_text = load_corpus(kb, budget)
    system = (HERE / "prompts" / "extract_system.md").read_text(encoding="utf-8") + "\n\n" + corpus
    ext_dir = out / ("extraction_mock" if mock_run else "extraction")  # a mock run never touches real results
    raw_dir = ext_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    dims = {d["code"]: d for d in catalog["dimensions"]}
    result = {"pilot_id": pid, "model": model.model, "catalog_version": catalog.get("catalog_version"),
              "prompt_sha": sha((HERE / "prompts" / "extract_system.md").read_text(encoding="utf-8")),
              "corpus": cinfo, "pilot": {}, "responses": [], "hypotheses": [], "cost_items": [], "usage": [], "preview": []}
    for level in (1, 2, 3, 4):
        text, pairs, hyp_keys = checklist(level, catalog)
        user = (f"Evaluate Level {level} of the framework for this pilot. Return one entry in `fields` per field listed "
                f"below, with `dimension` set to the code (e.g. `L{level}.1`{', or `COVER`' if level == 1 else ''}) and `field` "
                f"to the key. Leave `cost_items` and `hypotheses` empty unless this level asks for them.\n\n{text}")
        key = sha(system + user + model.model + json.dumps(build_schema(catalog), sort_keys=True))
        raw_path = raw_dir / f"{pid}_L{level}.json"
        if dry_run:
            result["preview"].append({"level": level, "est_input_tokens": int((len(system) + len(user)) / CHARS_PER_TOKEN),
                                      "cached": raw_path.exists() and json.loads(raw_path.read_text(encoding='utf-8')).get("key") == key})
            continue
        mock = lambda: {"fields": [{"dimension": d, "field": f, "status": "no_evidence", "value": "", "note": "(mock)", "confidence": 0, "evidence": []} for d, f in pairs],
                        "cost_items": [], "hypotheses": []}
        ans, usage = cached_call(model, system, user, key, raw_path, catalog, mock)
        result["usage"].append({"level": level, **usage})

        got = {(norm_dim(e.get("dimension")), norm_key(e.get("field"))): e for e in ans.get("fields", [])}
        hyps = ans.get("hypotheses", [])
        # A "hypothesis" item whose statement is itself a gap ("No stated objective") means no hypothesis:
        # it becomes the L3.2 gap entry, not a hypothesis with nine empty fields.
        for h in [h for h in hyps if any(norm_key(x.get("field")) == "hypothesis" and x.get("status") != "filled" for x in h.get("fields", []))]:
            hyps.remove(h)
            if ("L3.2", "hypothesis") not in got:
                got[("L3.2", "hypothesis")] = next(x for x in h["fields"] if norm_key(x.get("field")) == "hypothesis")
        pair_gaps(got, pairs)
        # Follow-up: the model sometimes leaves out a few requested entries (run of 2026-11-04: 21 of 1,440 fields).
        # Ask once more for those only; the corpus is read from the cache, so the call is small.
        miss = [(d, f) for d, f in pairs if (d, f) not in got]
        miss_h = [(i, h.get("label", ""), [k for k in hyp_keys if k not in {norm_key(x.get("field")) for x in h.get("fields", [])}])
                  for i, h in enumerate(hyps)] if hyp_keys else []
        miss_h = [m for m in miss_h if m[2]]
        if (miss or miss_h) and not dry_run:
            fu = followup_text(level, miss, miss_h, hyps, dims, catalog)
            fans, fusage = cached_call(model, system, fu, sha(system + fu + model.model + json.dumps(build_schema(catalog), sort_keys=True)),
                                       raw_dir / f"{pid}_L{level}_followup.json", catalog, lambda: {"fields": [], "cost_items": [], "hypotheses": []})
            result["usage"].append({"level": level, "followup": True, **fusage})
            for e in fans.get("fields", []):
                k = (norm_dim(e.get("dimension")), norm_key(e.get("field")))
                if k in miss and k not in got:
                    got[k] = {**e, "from_followup": True}
            for fh in fans.get("hypotheses", []):
                i = next((j for j, lab, _ in miss_h if fold(lab) == fold(fh.get("label", ""))), miss_h[0][0] if len(miss_h) == 1 else None)
                if i is None:
                    continue
                have = {norm_key(x.get("field")) for x in hyps[i].get("fields", [])}
                hyps[i].setdefault("fields", []).extend({**x, "from_followup": True} for x in fh.get("fields", []) if norm_key(x.get("field")) not in have)
            pair_gaps(got, pairs)
        for d, f in pairs:
            e = got.get((d, f)) or {"dimension": d, "field": f, "status": "no_evidence", "value": "", "confidence": 0, "evidence": [],
                                    "note": "Field not returned by the extractor.", "missing_in_answer": True}
            if d == "COVER":
                ftype = "enum" if f == "pilot_type" else "date" if f.startswith("date") else "text"
                opts = [p["key"] for p in catalog["pilot_types"]] if f == "pilot_type" else None
                result["pilot"][f] = validate(e, ftype, opts, doc_text)
                continue
            fdef = next(x for x in dim_fields(dims[d], catalog) if x["key"] == f)
            e = validate(e, fdef.get("type"), fdef.get("options"), doc_text)
            result["responses"].append({"dimension": d, "field": f, "hypothesis_index": None, "stage": stage_of(dims[d], f), **{k: v for k, v in e.items() if k not in ("dimension", "field")}})
        extra = [k for k in got if k not in set(pairs) and not (k[0] == "L3.2")]
        if extra:
            result.setdefault("ignored_fields", []).extend(f"{a}/{b}" for a, b in extra)
        if hyp_keys is not None:
            if not hyps:
                e = got.get(("L3.2", "hypothesis")) or {"status": "no_evidence", "value": "", "confidence": 0, "evidence": [],
                                                        "note": "No hypotheses returned by the extractor.", "missing_in_answer": True}
                e = validate(e, "longtext", None, doc_text)
                result["responses"].append({"dimension": "L3.2", "field": "hypothesis", "hypothesis_index": None, "stage": "ideation",
                                            **{k: v for k, v in e.items() if k not in ("dimension", "field")}})
            for i, h in enumerate(hyps):
                result["hypotheses"].append({"index": i, "label": h.get("label", f"Impact ({i + 1})")})
                hg = {norm_key(x.get("field")): x for x in h.get("fields", [])}
                for f in hyp_keys:
                    fdef = next(x for x in dim_fields(dims["L3.2"], catalog) if x["key"] == f)
                    e = hg.get(f) or {"status": "no_evidence", "value": "", "confidence": 0, "evidence": [], "note": "Field not returned by the extractor.", "missing_in_answer": True}
                    e = validate(e, fdef.get("type"), fdef.get("options"), doc_text)
                    result["responses"].append({"dimension": "L3.2", "field": f, "hypothesis_index": i, "stage": stage_of(dims["L3.2"], f),
                                                **{k: v for k, v in e.items() if k not in ("dimension", "field")}})
        for c in ans.get("cost_items", []):
            c["evidence"] = check_evidence(c.get("evidence"), doc_text)
            result["cost_items"].append(c)
    if dry_run:
        return result
    # A degree rests on its justification: the quotes go there, so the degree may carry none of its own.
    just = {r["dimension"]: r for r in result["responses"] if r["field"] == "justification" and r["hypothesis_index"] is None}
    for r in result["responses"]:
        if r["field"] == "degree" and "filled without evidence" in r["flags"] and just.get(r["dimension"], {}).get("evidence"):
            r["flags"].remove("filled without evidence")
    # Likewise the identification strategy is a classification of the hypothesis' counterfactual / method fields.
    hyp_ev = {r["hypothesis_index"] for r in result["responses"] if r["dimension"] == "L3.2" and r["hypothesis_index"] is not None
              and r["field"] in ("counterfactual", "description", "data_source", "indicator") and r.get("evidence")}
    for r in result["responses"]:
        if r["field"] == "attribution_method" and "filled without evidence" in r["flags"] and r["hypothesis_index"] in hyp_ev:
            r["flags"].remove("filled without evidence")
    pt = result["pilot"].get("pilot_type", {})
    for r in result["responses"]:  # dimensions that do not apply to the pilot type
        ap = dims[r["dimension"]]["applies_to"]
        if pt.get("status") == "filled" and "all" not in ap and pt.get("value") not in ap and r["status"] == "filled":
            r["flags"].append(f"dimension applies to {ap}, pilot typed as {pt.get('value')}")
    result["checks"] = {
        "fields": len(result["responses"]),
        "filled": sum(r["status"] == "filled" for r in result["responses"]),
        "no_evidence": sum(r["status"] == "no_evidence" for r in result["responses"]),
        "not_applicable": sum(r["status"] == "not_applicable" for r in result["responses"]),
        "with_flags": sum(bool(r["flags"]) for r in result["responses"]),
        "not_returned": sum(bool(r.get("missing_in_answer")) for r in result["responses"]),
        "from_followup": sum(bool(r.get("from_followup")) for r in result["responses"]),
        "quotes": sum(len(r["evidence"]) for r in result["responses"]),
        "quotes_verified": sum(e["verified"] for r in result["responses"] for e in r["evidence"]),
        "hypotheses": len(result["hypotheses"]), "cost_items": len(result["cost_items"]),
    }
    result["evaluability"] = for_extraction(result, catalog)  # retrospective baseline (see evaluability.py)
    result["checks"]["evaluability"] = result["evaluability"]["score"]
    result.pop("preview")
    (ext_dir / f"{pid}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--pilots", help="comma-separated pilot_ids (default: all in retro scope)")
    ap.add_argument("--gold-only", action="store_true", help="only the pilots listed in data/gold_workbooks.csv (calibration)")
    ap.add_argument("--model", default=os.environ.get("CLAB_MODEL", DEFAULT_MODEL))
    ap.add_argument("--effort", default="medium", choices=["low", "medium", "high"])
    ap.add_argument("--max-tokens", type=int, default=32000, help="per call; covers thinking + JSON answer")
    ap.add_argument("--budget", type=int, default=150000, help="max corpus tokens sent per pilot")
    ap.add_argument("--workers", type=int, default=3, help="pilots processed in parallel (levels of one pilot run in sequence to reuse the cache)")
    ap.add_argument("--catalog")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    catalog = find_catalog(a.catalog)
    R = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Registry")
    pilots = list(R[R.in_retro_scope == True].pilot_id)  # noqa: E712
    if a.gold_only:
        pilots = [p for p in pd.read_csv(HERE / "data" / "gold_workbooks.csv", dtype=str).pilot_id if p in pilots]
    if a.pilots:
        pilots = [p.strip() for p in a.pilots.split(",")]
    model = Model(a.model, mock=a.mock or a.dry_run, max_tokens=a.max_tokens, effort=a.effort)
    print(f"{len(pilots)} pilots, model {a.model}{' (dry run)' if a.dry_run else ' (MOCK)' if a.mock else ''}")

    def run(pid):
        try:
            return pid, extract_pilot(pid, model, catalog, out, a.budget, a.dry_run, mock_run=a.mock)
        except Exception as e:
            print(f"  ! {pid} failed: {e}", flush=True)
            return pid, None

    rows = []
    with ThreadPoolExecutor(a.workers) as ex:
        for pid, res in ex.map(run, pilots):
            if not res:
                continue
            if a.dry_run:
                tot = sum(p["est_input_tokens"] for p in res["preview"])
                rows.append({"pilot_id": pid, "corpus_tokens_sent": res["corpus"]["est_tokens_sent"], "cut": res["corpus"]["cut"] or "",
                             "omitted_docs": len(res["corpus"]["omitted"]), "calls": 4, "input_tokens_if_uncached": tot})
            else:
                rows.append({"pilot_id": pid, **res["checks"], "omitted_docs": len(res["corpus"]["omitted"])})
                print(f"  {pid}: {res['checks']}", flush=True)
    S = pd.DataFrame(rows)
    if len(S):
        print(S.to_string(index=False))
    if a.dry_run and len(S):
        print(f"corpus tokens: {S.corpus_tokens_sent.sum():,} (written to cache once per pilot, read 3 more times)")
    if not a.dry_run:
        print("usage:", model.usage)
        print("evaluability (retrospective baseline) -> run `python evaluability.py --out out` for out/evaluability.csv")


if __name__ == "__main__":
    main()
