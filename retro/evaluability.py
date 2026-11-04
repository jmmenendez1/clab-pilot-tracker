"""Evaluability (completeness score) of a pilot's evaluation.

Definition (decision 2026-11-04, Juan; same as the Evaluation Sheet of the original data-collection instrument)
    score = complete subdimensions / total subdimensions in the catalog (18 in catalog 1.0.0)

    - The unit is the subdimension (L1.1, L1.2, ... L4.5): not the field, so a pilot with three impact
      hypotheses does not weigh more than one with a single hypothesis, and not the level.
    - The denominator is always every subdimension. A subdimension marked not_applicable counts as incomplete:
      the strictest standard, which keeps pilots comparable. A dimension usually does not apply because of how
      the pilot was designed, not because of what the pilot is.
    - A subdimension is complete when every field the catalog marks `required` has status `filled`.
        * Degree dimensions: degree (high / medium / low; `na` does not count) and justification.
        * Narrative dimensions: text.
        * L2.2: on_time and text.
        * L3.2 (one record per hypothesis): at least one hypothesis with hypothesis, counterfactual and indicator.
        * A dimension with no required field (L2.1 Disaggregated costs): at least one cost amount. In the
          extraction, a cost line item with an amount; in the tracker or a workbook, a USD subcategory or the L2.1
          notes, where costs written as text land. The funding note and the opex period alone do not count.
    - Status only: the score does not check quotes. The extraction already requires evidence for `filled`,
      and the tracker cannot check quotes, so baseline and current scores use the same rule.

Two moments are recorded (decision 2026-11-04):
    - Retrospective baseline: the score of the stage-5 extraction, i.e. how much could be evaluated from what was
      documented. Fixed, with its date. Written to out/evaluability.csv and, by the loader (stage 6), to the tracker.
    - Current evaluability: computed in the tracker from the current responses, so it rises as people fill gaps
      (for example after interviews with the implementing team).

The tracker computes the current score with web/evaluability.js, the same rule in JavaScript
(tests/test_evaluability_parity.py checks that both give the same result).

Usage (from retro/)
    python evaluability.py --out out      # -> out/evaluability.csv (extraction, plus the hand-made workbooks if present)
"""
import argparse
import json
import time
from pathlib import Path

import pandas as pd

DEGREES = {"high", "medium", "low"}


def dim_fields(dim, catalog):
    f = dim["fields"]
    return f if isinstance(f, list) else catalog["field_sets"][f]


def subdimension_complete(dim, catalog, responses, has_costs):
    """responses: [{dimension, field, hypothesis_index, status, value}] for this pilot."""
    code = dim["code"]
    req = [f["key"] for f in dim_fields(dim, catalog) if f.get("required")]
    rs = [r for r in responses if r["dimension"] == code]

    def ok(r):
        if r.get("status") != "filled":
            return False
        if r["field"] == "degree":
            return str(r.get("value") or "").lower() in DEGREES
        return True

    if not req:  # L2.1: no required field -> at least one cost amount
        return bool(has_costs) or any(ok(r) for r in rs if r["field"] not in ("opex.period", "funding_note"))
    if dim.get("repeatable") == "hypothesis":
        idx = {r["hypothesis_index"] for r in rs if r.get("hypothesis_index") is not None}
        return any(all(any(ok(r) for r in rs if r.get("hypothesis_index") == i and r["field"] == k) for k in req) for i in idx)
    return all(any(ok(r) for r in rs if r["field"] == k and r.get("hypothesis_index") is None) for k in req)


def evaluability(catalog, responses, has_costs=False):
    detail = {d["code"]: int(subdimension_complete(d, catalog, responses, has_costs)) for d in catalog["dimensions"]}
    n, total = sum(detail.values()), len(detail)
    return {"score": round(n / total, 3), "complete": n, "total": total, "by_subdimension": detail,
            "rule": "complete subdimensions / all subdimensions; complete = all required fields filled; N/A counts as incomplete",
            "catalog_version": catalog.get("catalog_version"), "computed_at": time.strftime("%Y-%m-%d")}


def for_extraction(result, catalog):
    has_costs = any(c.get("amount_original") is not None for c in result.get("cost_items", []))
    return evaluability(catalog, result["responses"], has_costs)


def for_gold(gold, catalog):
    return evaluability(catalog, gold["responses"], has_costs=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--catalog")
    a = ap.parse_args()
    from stage5_gold_workbooks import find_catalog
    catalog = find_catalog(a.catalog)
    out = Path(a.out)
    rows = []
    for source, folder, fn in (("retro_extraction", "extraction", for_extraction), ("evaluator_workbook", "gold", for_gold)):
        for p in sorted((out / folder).glob("CLAB-P-*.json")):
            res = json.loads(p.read_text(encoding="utf-8"))
            ev = fn(res, catalog)
            if source == "retro_extraction":  # keep the baseline with the extraction itself
                res["evaluability"] = ev
                p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            rows.append({"pilot_id": p.stem, "source": source, "score": ev["score"], "complete": ev["complete"], "total": ev["total"],
                         **ev["by_subdimension"], "computed_at": ev["computed_at"]})
    if not rows:
        raise SystemExit("no extraction results: run stage5_extract.py first")
    df = pd.DataFrame(rows)
    reg = out / "pilot_registry.xlsx"
    if reg.exists():
        R = pd.read_excel(reg, sheet_name="Registry")
        name = next((c for c in R.columns if c.lower() in ("name", "pilot_name", "canonical_name")), None)
        if name:
            df.insert(1, "name", df.pilot_id.map(dict(zip(R.pilot_id, R[name]))))
    df = df.sort_values(["source", "score"], ascending=[False, False])
    df.to_csv(out / "evaluability.csv", index=False, encoding="utf-8-sig")
    base = df[df.source == "retro_extraction"]
    print(base.drop(columns=[c for c in ("computed_at",) if c in base]).to_string(index=False))
    print(f"\nretrospective baseline: {len(base)} pilots, mean {base.score.mean():.2f}, median {base.score.median():.2f}")
    gold = df[df.source == "evaluator_workbook"]
    if len(gold):
        cmp = gold[["pilot_id", "score"]].merge(base[["pilot_id", "score"]], on="pilot_id", suffixes=("_workbook", "_extraction"))
        print("\nhand-made workbooks (documents + interviews) vs extraction (documents only):")
        print(cmp.to_string(index=False))
    print(f"-> {out / 'evaluability.csv'}")


if __name__ == "__main__":
    main()
