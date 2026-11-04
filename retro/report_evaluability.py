"""Evaluability report data: which subdimensions are hardest to evaluate, per pilot, and evaluability over time.

Feeds the "Pilot Evaluability" dashboard (two CSV files attached to it). The rule is evaluability.py's, applied here
cell by cell so each pilot x subdimension also says *why* it is not complete.

Inputs
    --tracker-csv  the tracker's current state: the CSV of the query in METHODS.md section 8 (one row per pilot with its
                   current responses as JSON). It covers prospective pilots and every human edit. Without it, the
                   report uses the retrospective extraction in out/extraction (retrospective pilots only, as loaded).
    out/extraction (always): the retrospective baseline, the documentation volume and the dates' original precision.

Cell states (per pilot and subdimension)
    complete        every required field filled (evaluability.py)
    partial         something filled, but not every required field (e.g. a hypothesis without counterfactual or
                    indicator, a degree without justification, a funding note without any cost amount)
    no_evidence     nothing filled; the fields were marked no evidence
    not_applicable  nothing filled; marked not applicable (counts as incomplete in the score)
    empty           nothing recorded at all (tracker only)

Execution date (x axis of the scatter): the start date; if missing, the end date (`date_basis` says which).

Usage (from retro/)
    python report_evaluability.py --out out                                   # retrospective extraction only
    python report_evaluability.py --out out --tracker-csv tracker_state.csv   # tracker export, all pilots

Writes
    out/report/pilots.csv   one row per pilot: mode, type, TC, execution date, evaluability (current, baseline), ...
    out/report/cells.csv    one row per pilot x subdimension: state and the required fields that are missing
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd

from evaluability import DEGREES, evaluability, for_extraction
from stage5_gold_workbooks import find_catalog


def dim_fields(dim, catalog):
    f = dim["fields"]
    return f if isinstance(f, list) else catalog["field_sets"][f]


def filled(r):
    return r.get("status") == "filled" and (r["field"] != "degree" or str(r.get("value") or "").lower() in DEGREES)


def cell(dim, catalog, rows, hyps, has_costs):
    """State of one subdimension and the required fields that are missing (labels)."""
    code = dim["code"]
    fields = dim_fields(dim, catalog)
    label = {f["key"]: f["label"] for f in fields}
    req = [f["key"] for f in fields if f.get("required")]
    rs = [r for r in rows if r["dimension"] == code]
    complete = evaluability(catalog, [{"dimension": r["dimension"], "field": r["field"], "hypothesis_index": r.get("hyp"),
                                       "status": r["status"], "value": r.get("value")} for r in rs],
                            has_costs)["by_subdimension"][code] if rs or has_costs else 0
    if dim.get("repeatable") == "hypothesis":
        hs = [h for h in hyps if any(r.get("hyp") == h for r in rs)]
        if not hs:
            missing = ["no hypothesis"]
        else:
            per = [[label[k] for k in req if not any(r.get("hyp") == h and r["field"] == k and filled(r) for r in rs)] for h in hs]
            missing = min(per, key=len)
    elif not req:
        missing = [] if complete else ["no cost amount"]
    else:
        missing = [label[k] for k in req if not any(r["field"] == k and r.get("hyp") is None and filled(r) for r in rs)]
    if complete:
        state = "complete"
    elif not rs:
        state = "empty"
    elif any(filled(r) or (r["status"] == "filled") for r in rs):
        state = "partial"
    elif all(r["status"] == "not_applicable" for r in rs):
        state = "not_applicable"
    else:
        state = "no_evidence"
    return state, ("" if complete else "; ".join(missing))


def exec_date(start, end):
    for v, basis in ((start, "start"), (end, "end")):
        v = str(v or "").strip()
        m = re.match(r"(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", v)
        if m and v.lower() not in ("unknown", "nan", "none"):
            y, mo, d = m.group(1), m.group(2) or "07", m.group(3) or "01"
            if not m.group(2):   # a bare year sits mid-year
                mo, d = "07", "01"
            elif not m.group(3):  # a month sits mid-month
                d = "15"
            return f"{y}-{mo}-{d}", basis
    return "", ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--tracker-csv")
    ap.add_argument("--catalog")
    a = ap.parse_args()
    out = Path(a.out)
    catalog = find_catalog(a.catalog)
    dims = catalog["dimensions"]

    extr = {}
    for p in sorted((out / "extraction").glob("CLAB-P-*.json")):
        res = json.loads(p.read_text(encoding="utf-8"))
        extr[res["pilot_id"]] = res
    reg = {}
    if (out / "pilot_registry.xlsx").exists():
        R = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Registry")
        reg = {r.pilot_id: r for r in R.itertuples(index=False)}

    pilots = []  # (meta, rows, hyps, has_costs)
    if a.tracker_csv:
        T = pd.read_csv(a.tracker_csv, dtype=str, keep_default_na=False)
        for t in T.itertuples(index=False):
            resp = json.loads(t.responses or "[]")
            hyp_ids = json.loads(t.hypotheses or "[]")
            hix = {h: i for i, h in enumerate(hyp_ids)}
            rows = [{"dimension": x["d"], "field": x["f"], "hyp": hix.get(x["h"]) if x.get("h") else None, "status": x["s"],
                     "value": x.get("v")} for x in resp if not x.get("h") or x["h"] in hix]
            has_costs = any(r["dimension"] == "L2.1" and r["field"] not in ("opex.period", "funding_note") and r["status"] == "filled" for r in rows)
            rows = [r for r in rows if not (r["dimension"] == "L2.1" and r["field"] not in ("opex.period", "funding_note"))]
            key = t.pilot_key or ""
            pilots.append(({"pilot_id": key or t.id, "pilot_key": key, "name": t.name, "tc": t.tc_code, "mode": t.mode,
                            "type": t.pilot_type, "stage": t.current_stage, "start": t.date_started, "end": t.date_ended,
                            "drafts": sum(x.get("o") == "retro_extraction" for x in resp)}, rows, list(range(len(hyp_ids))), has_costs))
    else:
        for pid, res in extr.items():
            c = res["pilot"]
            g = lambda k: (c.get(k) or {}).get("value") if (c.get(k) or {}).get("status") == "filled" else ""
            rows = [{"dimension": r["dimension"], "field": r["field"], "hyp": r["hypothesis_index"], "status": r["status"],
                     "value": r.get("value")} for r in res["responses"] if not (r["dimension"] == "L3.2" and r["hypothesis_index"] is None)]
            has_costs = any(x.get("amount_original") is not None for x in res.get("cost_items", []))
            rg = reg.get(pid)
            pilots.append(({"pilot_id": pid, "pilot_key": pid, "name": getattr(rg, "canonical_name", pid) if rg is not None else pid,
                            "tc": getattr(rg, "tc_primary", "") if rg is not None else "", "mode": "retrospective",
                            "type": g("pilot_type"), "stage": "evaluation", "start": g("date_started"), "end": g("date_ended"),
                            "drafts": len(res["responses"])}, rows, sorted({r["hyp"] for r in rows if r["hyp"] is not None}), has_costs))

    prow, crow = [], []
    for meta, rows, hyps, has_costs in pilots:
        states = {}
        res = extr.get(meta["pilot_key"])
        nohyp = bool(res) and any(r["dimension"] == "L3.2" and r["hypothesis_index"] is None for r in res["responses"])
        for d in dims:
            st, miss = cell(d, catalog, rows, hyps, has_costs)
            if d["code"] == "L3.2" and st == "empty" and nohyp:
                st = "no_evidence"  # the extraction found no hypothesis (kept in retro_cover in the tracker)
            states[d["code"]] = st
            crow.append({"pilot_id": meta["pilot_id"], "pilot": meta["name"], "mode": meta["mode"], "type": meta["type"],
                         "dimension": d["code"], "dimension_name": d["name"], "level": d["level"], "core": bool(d.get("core")),
                         "state": st, "missing": miss})
        n = sum(s == "complete" for s in states.values())
        base = for_extraction(res, catalog)["score"] if res else None
        start, end = meta["start"], meta["end"]
        if res:  # prefer the extraction's original precision (the tracker stores partial dates as the first day)
            c = res["pilot"]
            start = (c.get("date_started") or {}).get("value") or start if (c.get("date_started") or {}).get("status") == "filled" else start
            end = (c.get("date_ended") or {}).get("value") or end if (c.get("date_ended") or {}).get("status") == "filled" else end
        xd, basis = exec_date(start, end)
        prow.append({"pilot_id": meta["pilot_id"], "pilot": meta["name"], "tc": meta["tc"], "mode": meta["mode"], "type": meta["type"],
                     "stage": meta["stage"], "exec_date": xd, "date_basis": basis, "date_started": start or "", "date_ended": end or "",
                     "evaluability": round(n / len(dims), 3), "complete": n, "total": len(dims),
                     "baseline": base, "drafts_open": meta["drafts"],
                     "corpus_tokens": res["corpus"]["est_tokens_sent"] if res else None})

    rep = out / "report"
    rep.mkdir(exist_ok=True)
    P, C = pd.DataFrame(prow), pd.DataFrame(crow)
    P.to_csv(rep / "pilots.csv", index=False, encoding="utf-8")
    C.to_csv(rep / "cells.csv", index=False, encoding="utf-8")
    print(P[["pilot_id", "mode", "type", "exec_date", "date_basis", "evaluability", "baseline"]].to_string(index=False))
    print(C.pivot_table(index="dimension", columns="state", values="pilot_id", aggfunc="count", fill_value=0))
    print(f"-> {rep}")


if __name__ == "__main__":
    main()
