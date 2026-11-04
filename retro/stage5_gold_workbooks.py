"""Stage 5a - Reference ("gold") evaluations from the retrospective workbooks done by hand (Sabrina's five).

Why
    Before the framework extraction (stage 5b) runs on all pilots, it is calibrated against pilots that a person
    already evaluated with the Excel template. This script turns those workbooks into the same intermediate JSON
    that the extractor writes, so stage 5c can compare them field by field.

How
    A faithful Python port of the tracker's in-app importer (web/import.js, `importWorkbook`): same cover-sheet
    labels, same stage tabs, "latest non-empty value across stage tabs wins", degrees normalised to
    high/medium/low/na, "NA"-like text read as no_evidence, L2.1 free text kept as notes, L3.2 one hypothesis per
    distinct "Impact (n)" label. tests/test_gold_parity.py checks that both importers agree.
    data/gold_workbooks.csv maps each workbook (by a substring of its file name) to the registry's pilot_id.

Usage (from retro/)
    python stage5_gold_workbooks.py --out out --workbooks "<folder with the .xlsm files>"

Writes
    out/gold/<pilot_id>.json   {pilot_id, source, pilot (cover fields), hypotheses, responses, warnings}
"""
import argparse
import datetime as dt
import json
import re
from pathlib import Path

import openpyxl
import pandas as pd

HERE = Path(__file__).parent
STAGE_SHEETS = [("ideation", re.compile(r"ideation", re.I)), ("design", re.compile(r"design", re.I)),
                ("implementation", re.compile(r"implementation", re.I)), ("evaluation", re.compile(r"^(?!.*sheet).*evaluation", re.I))]
NA_RE = re.compile(r"^\s*(na|n/a|n\.a\.|-|—|not available|not applicable)\s*\.?$", re.I)
COVER_MAP = [(re.compile(r"^pilot name", re.I), "name"), (re.compile(r"^tc code", re.I), "tc_code"), (re.compile(r"^city", re.I), "city_country"),
             (re.compile(r"^pilot type", re.I), "pilot_type"), (re.compile(r"^attribution", re.I), "attribution_category"),
             (re.compile(r"^problem", re.I), "problem_definition"), (re.compile(r"^target pop", re.I), "target_population"),
             (re.compile(r"^lead", re.I), "lead"), (re.compile(r"^date started", re.I), "date_started"),
             (re.compile(r"^last updated|^date ended", re.I), "date_ended")]


def find_catalog(explicit=None):
    for p in [explicit, HERE.parent / "catalog" / "framework_catalog.json", HERE / "catalog" / "framework_catalog.json"]:
        if p and Path(p).exists():
            return json.loads(Path(p).read_text(encoding="utf-8"))
    raise SystemExit("framework_catalog.json not found (looked in ../catalog/ and ./catalog/); pass --catalog")


def clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return re.sub(r"\s+", " ", re.sub("[‑‐‒–]", "-", str(v))).strip()


def norm(v):
    return clean(v).lower()


def rows_of(ws):
    return [list(r) for r in ws.iter_rows(min_row=1, min_col=1, values_only=True)]


def to_date(v, warnings, label):
    if v is None or v == "":
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        if v.year < 1950:  # "2022" typed into a date-formatted cell is the serial 2022 = 1905-07-14
            serial = (dt.datetime(v.year, v.month, v.day) - dt.datetime(1899, 12, 30)).days
            warnings.append(f"{label}: workbook had the date {v.strftime('%Y-%m-%d')}, read as the year {serial}; stored as {serial}-01-01.")
            return f"{serial}-01-01"
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and v < 3000:
        warnings.append(f"{label}: only a year ({int(v)}) in the workbook; stored as {int(v)}-01-01.")
        return f"{int(v)}-01-01"
    s = clean(v)
    if re.fullmatch(r"\d{4}", s):
        warnings.append(f"{label}: only a year ({s}); stored as {s}-01-01.")
        return f"{s}-01-01"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    warnings.append(f'{label}: could not parse "{s}".')
    return None


def parse_cover(ws, catalog, warnings):
    raw = {}
    for r in rows_of(ws):
        label = norm(r[1] if len(r) > 1 else None)
        if not label:
            continue
        for rx, key in COVER_MAP:
            if rx.search(label):
                raw[key] = r[2] if len(r) > 2 else None
    p = {"mode": "retrospective"}
    p["name"], p["tc_code"] = clean(raw.get("name")), clean(raw.get("tc_code")).upper()
    cc = clean(raw.get("city_country"))
    i = cc.rfind(",")
    p["city"], p["country"] = (cc[:i].strip(), cc[i + 1:].strip()) if i > 0 else (cc, "")
    t = norm(raw.get("pilot_type"))
    p["pilot_type"] = next((x["key"] for x in catalog["pilot_types"] if t.startswith(x["key"]) or x["label"].lower().startswith(t)), None) or \
        ("methodology" if re.search("method|process", t) else "institutional" if "instit" in t else "behavioral" if "behav" in t else "digital" if "digit" in t else "urban")
    p["attribution_category"] = "process" if "process" in norm(raw.get("attribution_category")) else "counterfactual"
    for k in ("problem_definition", "target_population", "lead"):
        p[k] = clean(raw.get(k)) or None
    p["date_started"] = to_date(raw.get("date_started"), warnings, "Date started")
    p["date_ended"] = to_date(raw.get("date_ended"), warnings, "Last updated -> date ended")
    return p


def parse_stage(ws, stage):
    headers, out = None, []
    for r in rows_of(ws):
        a = norm(r[0] if r else None)
        if a == "code":
            headers = {norm(h): i for i, h in enumerate(r) if h is not None and i >= 4}
            continue
        if re.fullmatch(r"l\d\.\d", a) and headers:
            fields = {}
            for h, i in headers.items():
                v = r[i] if i < len(r) else None
                if v is not None and clean(v) != "":
                    fields[h] = clean(v) if isinstance(v, str) else v
            out.append({"code": a.upper(), "label": clean(r[1]), "stage": stage, "fields": fields})
    return out


def degree(v):
    s = norm(v)
    return "high" if s.startswith("high") else "medium" if s.startswith("med") else "low" if s.startswith("low") else "na" if NA_RE.match(s) else None


def pick(f, *names):
    for n in names:
        if f.get(n) is not None:
            return f[n]
    return None


def import_workbook(path, catalog):
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    warnings = []
    cover = next((n for n in wb.sheetnames if re.search("cover", n, re.I)), wb.sheetnames[0])
    pilot = parse_cover(wb[cover], catalog, warnings)
    entries = []
    for stage, rx in STAGE_SHEETS:
        n = next((x for x in wb.sheetnames if rx.search(x)), None)
        if n:
            entries += parse_stage(wb[n], stage)
    responses, hypotheses = [], []

    def add(code, key, h, stage, raw, note_raw, kind):
        note = clean(note_raw) if note_raw is not None else ""
        if raw is None or clean(raw) == "":
            return
        if kind == "degree":
            d = degree(raw)
            if not d:
                warnings.append(f'{code}: unrecognised degree "{raw}" ignored.')
                return
            responses.append({"dimension": code, "field": key, "hypothesis_index": h, "stage": stage, "status": "filled", "value": d,
                              "note": (note or "Marked NA in the retrospective workbook") if d == "na" else (note or None)})
            return
        if isinstance(raw, str) and NA_RE.match(raw):
            responses.append({"dimension": code, "field": key, "hypothesis_index": h, "stage": stage, "status": "no_evidence", "value": None,
                              "note": note or "Marked NA in the retrospective workbook"})
            return
        responses.append({"dimension": code, "field": key, "hypothesis_index": h, "stage": stage, "status": "filled",
                          "value": raw if isinstance(raw, str) else clean(raw), "note": note or None})

    def latest(rows, *names):
        best = None
        for r in rows:
            v = pick(r["fields"], *names)
            if v is not None and clean(v) != "":
                best = {"v": v, "stage": r["stage"], "note": pick(r["fields"], "notes", "note")}
        return best

    for dim in catalog["dimensions"]:
        rows = [e for e in entries if e["code"] == dim["code"]]
        if not rows:
            continue
        if dim.get("repeatable") == "hypothesis":
            for label in dict.fromkeys(r["label"] for r in rows):
                hr = [r for r in rows if r["label"] == label]
                mapping = [("hypothesis", ["hypothesis"]), ("counterfactual", ["counterfactual"]), ("indicator", ["indicator"]),
                           ("description", ["description / formula", "description"]), ("data_source", ["data source"]),
                           ("baseline_t0", ["baseline (t0)", "baseline"]), ("midterm_t1", ["t1"]), ("endline_t2", ["t2 / endline", "t2"])]
                vals = [(k, latest(hr, *names)) for k, names in mapping]
                vals = [(k, b) for k, b in vals if b]
                if not any(not (isinstance(b["v"], str) and NA_RE.match(b["v"])) for _, b in vals):
                    continue
                idx = len(hypotheses)
                hypotheses.append({"index": idx, "label": label})
                for k, b in vals:
                    add(dim["code"], k, idx, b["stage"], b["v"], None, "text")
                n = latest(hr, "notes")
                if n:
                    add(dim["code"], "notes", idx, n["stage"], n["v"], None, "text")
        elif dim["code"] == "L2.1":
            parts, notes, stage = [], [], "implementation"
            for r in rows:
                d = pick(r["fields"], "detail")
                if d is not None and clean(d) != "" and not (isinstance(d, str) and NA_RE.match(d)):
                    parts.append(f"{r['label']}: {clean(d)}")
                    stage = r["stage"]
                n = pick(r["fields"], "notes")
                if n:
                    notes.append(f"{r['label']}: {clean(n)}")
            if parts:
                add("L2.1", "notes", None, stage, "\n".join(parts) + ("\n\nWorkbook notes — " + " | ".join(notes) if notes else ""), None, "text")
            else:
                add("L2.1", "notes", None, stage, "NA", " | ".join(notes) or None, "text")
        else:
            fields = dim["fields"] if isinstance(dim["fields"], list) else catalog["field_sets"][dim["fields"]]
            for f in fields:
                b = None
                if f["key"] == "degree":
                    b = latest(rows, "degree")
                elif f["key"] == "justification":
                    b = latest(rows, "justification")
                elif f["key"] == "text":
                    b = latest(rows, "detail", "descriptive field", "descriptive fields", "description")
                elif f["key"] == "notes":
                    b = latest(rows, "notes")
                if b:
                    add(dim["code"], f["key"], None, b["stage"], b["v"], b["note"] if f["key"] == "degree" else None,
                        "degree" if f.get("type") == "degree" else "text")
    return {"pilot": pilot, "hypotheses": hypotheses, "responses": responses, "warnings": warnings}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--workbooks", required=True, help="folder with the hand-made evaluation workbooks (.xlsm/.xlsx)")
    ap.add_argument("--map", default=str(HERE / "data" / "gold_workbooks.csv"))
    ap.add_argument("--catalog")
    a = ap.parse_args()
    catalog = find_catalog(a.catalog)
    mp = pd.read_csv(a.map, dtype=str)
    gold_dir = Path(a.out) / "gold"
    gold_dir.mkdir(parents=True, exist_ok=True)
    files = [f for f in Path(a.workbooks).iterdir() if f.suffix.lower() in (".xlsm", ".xlsx") and not f.name.startswith("~$")]
    for row in mp.itertuples():
        hits = [f for f in files if row.file_contains.lower() in f.name.lower()]
        if len(hits) != 1:
            print(f"  ! {row.pilot_id}: {len(hits)} workbooks match '{row.file_contains}'; skipped")
            continue
        r = import_workbook(hits[0], catalog)
        r = {"pilot_id": row.pilot_id, "source": hits[0].name, **r}
        (gold_dir / f"{row.pilot_id}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{row.pilot_id} <- {hits[0].name}: {len(r['hypotheses'])} hypotheses, {len(r['responses'])} values, {len(r['warnings'])} warnings")


if __name__ == "__main__":
    main()
