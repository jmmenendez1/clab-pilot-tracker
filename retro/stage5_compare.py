"""Stage 5c - Calibration: compare the extractor (stage 5b) with the hand-made evaluations (stage 5a).

Why
    Before running the extractor on every pilot, check where it agrees with an evaluator who read the same pilot,
    and where it does not: fields one of them filled and the other left as a gap, degrees that differ, hypotheses
    one of them saw and the other did not. The differences go to a person, who decides which side is right; the
    prompts are adjusted from that review.

What is compared (per pilot in data/gold_workbooks.csv that has both files)
    - Status per (dimension, field): filled vs not filled. The workbook's "NA" counts as not filled (no_evidence);
      a field the workbook leaves blank is "empty". Agreement = both filled or both not filled.
    - Degrees (L1, L3.1, L3.3): exact match and match within one step (high-medium-low).
    - L3.2 hypotheses: count, and the hypothesis texts side by side (order is not meaningful).
    - L2.1 costs: the workbook's cost text vs the extractor's cost line items.
    - Cover fields: side by side.
    Text fields are not scored automatically: they are shown side by side with the extractor's evidence for review.

Usage (from retro/, after stages 5a and 5b)
    python stage5_compare.py --out out

Writes
    out/calibration.xlsx   README, Summary (per pilot), ByDimension, Fields (one row per field, with a `review`
                           column), Hypotheses, Costs, Cover
"""
import argparse
import json
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).parent
RANK = {"high": 3, "medium": 2, "low": 1}
COVER = ["pilot_type", "city", "country", "problem_definition", "target_population", "lead", "counterpart", "date_started", "date_ended"]


def ev_text(r):
    return " || ".join(f"{e.get('doc_id')} @ {e.get('location')}: \"{e.get('quote')}\"" + ("" if e.get("verified") else " [NOT VERIFIED]")
                       for e in r.get("evidence", []))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--extraction", default="extraction", help="subfolder of --out with stage-5b results (extraction_mock for tests)")
    a = ap.parse_args()
    out = Path(a.out)
    gold_ids = list(pd.read_csv(HERE / "data" / "gold_workbooks.csv", dtype=str).pilot_id)
    fields, hyps, costs, cover, summary = [], [], [], [], []
    for pid in gold_ids:
        gp, mp = out / "gold" / f"{pid}.json", out / a.extraction / f"{pid}.json"
        if not (gp.exists() and mp.exists()):
            print(f"  {pid}: missing {'gold' if not gp.exists() else 'extraction'}; skipped")
            continue
        g, m = json.loads(gp.read_text(encoding="utf-8")), json.loads(mp.read_text(encoding="utf-8"))
        gr = {(r["dimension"], r["field"]): r for r in g["responses"] if r["hypothesis_index"] is None}
        mr = {(r["dimension"], r["field"]): r for r in m["responses"] if r["hypothesis_index"] is None}

        for key in sorted(set(mr) | {k for k in gr if k[1] != "notes"}):
            if key[0] in ("L2.1", "L3.2"):
                continue  # compared in their own sheets
            G, M = gr.get(key), mr.get(key)
            g_st = G["status"] if G else "empty"
            m_st = M["status"] if M else "not_requested"
            row = {"pilot_id": pid, "dimension": key[0], "field": key[1], "gold_status": g_st, "model_status": m_st,
                   "status_agree": (g_st == "filled") == (m_st == "filled"),
                   "gold_value": G["value"] if G else None, "model_value": M["value"] if M else None,
                   "model_confidence": M.get("confidence") if M else None, "model_note": M.get("note") if M else None,
                   "model_flags": "; ".join(M.get("flags", [])) if M else None, "model_evidence": ev_text(M) if M else None}
            if key[1] == "degree" and G and M and G["value"] in RANK and (M["value"] or "") in RANK:
                d = abs(RANK[G["value"]] - RANK[M["value"]])
                row.update(degree_exact=d == 0, degree_within_1=d <= 1)
            row["review"] = ""
            fields.append(row)

        gh = [r for r in g["responses"] if r["dimension"] == "L3.2" and r["field"] == "hypothesis" and r["hypothesis_index"] is not None]
        mh = [r for r in m["responses"] if r["dimension"] == "L3.2" and r["field"] == "hypothesis" and r["hypothesis_index"] is not None]
        for i in range(max(len(gh), len(mh))):
            gi = gh[i] if i < len(gh) else None
            mi = mh[i] if i < len(mh) else None
            attr = next((r for r in m["responses"] if r["dimension"] == "L3.2" and r["field"] == "attribution_method" and mi and r["hypothesis_index"] == mi["hypothesis_index"]), None)
            mfilled = sum(1 for r in m["responses"] if r["dimension"] == "L3.2" and mi and r["hypothesis_index"] == mi["hypothesis_index"] and r["status"] == "filled")
            hyps.append({"pilot_id": pid, "n": i + 1, "gold_hypothesis": gi["value"] if gi else None, "model_hypothesis": mi["value"] if mi else None,
                         "model_attribution_method": attr["value"] if attr else None, "model_fields_filled_of_10": mfilled if mi else None,
                         "model_evidence": ev_text(mi) if mi else None, "review": ""})

        g_cost = gr.get(("L2.1", "notes"))
        costs.append({"pilot_id": pid, "gold_status": g_cost["status"] if g_cost else "empty", "gold_costs": g_cost["value"] if g_cost else None,
                      "model_cost_items": len(m["cost_items"]),
                      "model_costs": "\n".join(f"{c['subcategory']}: {c['amount_original']} {c['currency']} ({c['year']}) — {c['label']}; covers {c['covers']}" for c in m["cost_items"]),
                      "model_funding_note": (mr.get(("L2.1", "funding_note")) or {}).get("value"), "review": ""})

        for k in COVER:
            gv = g["pilot"].get(k)
            mv = m["pilot"].get(k, {})
            cover.append({"pilot_id": pid, "field": k, "gold": gv, "model": mv.get("value"), "model_status": mv.get("status"),
                          "model_evidence": ev_text(mv) if mv else None, "review": ""})

        pf = pd.DataFrame([f for f in fields if f["pilot_id"] == pid])
        deg = pf[pf.field == "degree"] if len(pf) else pf
        summary.append({
            "pilot_id": pid, "source_workbook": g["source"], "fields_compared": len(pf),
            "status_agreement": round(pf.status_agree.mean(), 2) if len(pf) else None,
            "gold_filled": int((pf.gold_status == "filled").sum()), "model_filled": int((pf.model_status == "filled").sum()),
            "gap_only_in_model": int(((pf.gold_status == "filled") & (pf.model_status != "filled")).sum()),
            "filled_only_in_model": int(((pf.gold_status != "filled") & (pf.model_status == "filled")).sum()),
            "degree_exact": round(deg.degree_exact.mean(), 2) if "degree_exact" in deg and deg.degree_exact.notna().any() else None,
            "degree_within_1": round(deg.degree_within_1.mean(), 2) if "degree_within_1" in deg and deg.degree_within_1.notna().any() else None,
            "hypotheses_gold": len(gh), "hypotheses_model": len(mh),
            "quotes_verified": f"{m['checks']['quotes_verified']}/{m['checks']['quotes']}", "fields_with_flags": m["checks"]["with_flags"],
            "corpus_cut_or_omitted": bool(m["corpus"]["cut"] or m["corpus"]["omitted"]),
        })

    if not summary:
        raise SystemExit("nothing to compare: run stage5_gold_workbooks.py and stage5_extract.py --gold-only first")
    F = pd.DataFrame(fields)
    BD = F.groupby("dimension").agg(fields=("field", "size"), status_agreement=("status_agree", "mean"),
                                    gap_only_in_model=("gold_status", lambda s: int(((s == "filled") & (F.loc[s.index, "model_status"] != "filled")).sum())),
                                    filled_only_in_model=("gold_status", lambda s: int(((s != "filled") & (F.loc[s.index, "model_status"] == "filled")).sum())))
    if "degree_exact" in F:
        BD = BD.join(F[F.field == "degree"].groupby("dimension").agg(degree_exact=("degree_exact", "mean"), degree_within_1=("degree_within_1", "mean")))
    BD = BD.round(2).reset_index()
    readme = pd.DataFrame({"README": [
        "calibration.xlsx — stage 5c: extractor (stage 5b) vs hand-made workbook evaluations (stage 5a), same pilots.",
        "Summary: per pilot. status_agreement = share of fields where both filled or both left a gap. gap_only_in_model = the evaluator filled it, the extractor did not.",
        "ByDimension: the same, per framework dimension across pilots; degree_exact / degree_within_1 for graded dimensions.",
        "Fields: one row per field with both values, the extractor's evidence (verbatim quotes, [NOT VERIFIED] if a quote was not found) and its note on gaps. Use `review` to say which side is right (gold / model / both / neither).",
        "Hypotheses, Costs, Cover: side by side, for review.",
        "The workbooks are a reference, not ground truth: the spec already lists corrections for them (currency, classification, counterfactual wording).",
    ]})
    xlsx = out / ("calibration.xlsx" if a.extraction == "extraction" else f"calibration_{a.extraction}.xlsx")
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        readme.to_excel(w, sheet_name="README", index=False)
        pd.DataFrame(summary).to_excel(w, sheet_name="Summary", index=False)
        BD.to_excel(w, sheet_name="ByDimension", index=False)
        F.to_excel(w, sheet_name="Fields", index=False)
        pd.DataFrame(hyps).to_excel(w, sheet_name="Hypotheses", index=False)
        pd.DataFrame(costs).to_excel(w, sheet_name="Costs", index=False)
        pd.DataFrame(cover).to_excel(w, sheet_name="Cover", index=False)
    wb = load_workbook(xlsx)
    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="1F4E78")
        for col in ws.columns:
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(70, max(10, max(len(str(c.value or "")) for c in col[:100]) * 0.9))
            for c in col[1:]:
                c.alignment = Alignment(wrap_text=True, vertical="top")
    wb["README"].column_dimensions["A"].width = 160
    wb.save(xlsx)
    print(pd.DataFrame(summary).drop(columns=["source_workbook"]).to_string(index=False))
    print(f"-> {xlsx}")


if __name__ == "__main__":
    main()
