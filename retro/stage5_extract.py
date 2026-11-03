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
      so calls 2-4 read the corpus from cache. Answers are JSON constrained by a schema (structured outputs).
    - Corpora above --budget tokens are packed: documents in knowledge-base order (final reports first) until the
      budget; the next document is cut and the rest are omitted. Omitted documents are listed in the output and
      in the corpus itself, so gaps can be traced to the cut.
    - Checks in code after each answer:
        * every requested (dimension, field) is present; missing ones become no_evidence, flagged `missing_in_answer`;
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
            lines.append("Return these in `hypotheses`: one item per impact hypothesis the documents state or clearly imply "
                         "(at most 4), each with one entry per field below (dimension `L3.2`). If the documents state no "
                         "hypothesis or expected impact at all, return an empty list and explain in a `fields` entry "
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


def schema_for(level, has_hyp):
    props = {"fields": {"type": "array", "items": FIELD}}
    req = ["fields"]
    if level == 2:
        props["cost_items"] = {"type": "array", "items": COST}
        req.append("cost_items")
    if has_hyp:
        props["hypotheses"] = {"type": "array", "items": HYP}
        req.append("hypotheses")
    return {"type": "object", "properties": props, "required": req}


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
    entry["flags"] = flags
    return entry


# ------------------------------------------------------------------------------------------------ one pilot
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
        user = (f"Evaluate Level {level} of the framework for this pilot. Return one entry per field listed below.\n\n{text}")
        key = sha(system + user + model.model)
        raw_path = raw_dir / f"{pid}_L{level}.json"
        if dry_run:
            result["preview"].append({"level": level, "est_input_tokens": int((len(system) + len(user)) / CHARS_PER_TOKEN),
                                      "cached": raw_path.exists() and json.loads(raw_path.read_text(encoding='utf-8')).get("key") == key})
            continue
        cached = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else None
        if cached and cached.get("key") == key and not model.mock:
            ans, usage = cached["answer"], {}
        else:
            mock = lambda: {"fields": [{"dimension": d, "field": f, "status": "no_evidence", "value": "", "note": "(mock)", "confidence": 0, "evidence": []} for d, f in pairs],
                            "cost_items": [], "hypotheses": []}
            ans, usage = model.call(system, user, {"input_schema": schema_for(level, hyp_keys is not None)}, mock)
            if not model.mock:
                raw_path.write_text(json.dumps({"key": key, "time": time.strftime("%Y-%m-%d %H:%M"), "answer": ans, "usage": usage}, ensure_ascii=False, indent=1), encoding="utf-8")
        result["usage"].append({"level": level, **usage})

        got = {(e.get("dimension"), e.get("field")): e for e in ans.get("fields", [])}
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
            if not ans.get("hypotheses"):
                e = got.get(("L3.2", "hypothesis")) or {"status": "no_evidence", "value": "", "confidence": 0, "evidence": [],
                                                        "note": "No hypotheses returned by the extractor.", "missing_in_answer": True}
                e = validate(e, "longtext", None, doc_text)
                result["responses"].append({"dimension": "L3.2", "field": "hypothesis", "hypothesis_index": None, "stage": "ideation",
                                            **{k: v for k, v in e.items() if k not in ("dimension", "field")}})
            for i, h in enumerate(ans.get("hypotheses", [])):
                result["hypotheses"].append({"index": i, "label": h.get("label", f"Impact ({i + 1})")})
                hg = {x.get("field"): x for x in h.get("fields", [])}
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
        "quotes": sum(len(r["evidence"]) for r in result["responses"]),
        "quotes_verified": sum(e["verified"] for r in result["responses"] for e in r["evidence"]),
        "hypotheses": len(result["hypotheses"]), "cost_items": len(result["cost_items"]),
    }
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


if __name__ == "__main__":
    main()
