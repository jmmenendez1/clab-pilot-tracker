"""Parity: retro/evaluability.py (baseline) and web/evaluability.js (tracker, current score) give the same result.

Run from retro/ (needs Node):  python tests/test_evaluability_parity.py <out folder>
Each extraction result is turned into the rows the tracker would hold after stage 6 (the L2.1 cost line items as
the L2.1 notes field, hypotheses with their own ids), scored by both implementations, and compared subdimension by
subdimension. The hand-made workbooks in <out>/gold are compared too.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluability import for_extraction, for_gold  # noqa: E402
from stage5_gold_workbooks import find_catalog  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1])
catalog = find_catalog()


def tracker_rows(responses, cost_items=()):
    rows = [{"dimension_code": r["dimension"], "field_key": r["field"],
             "hypothesis_id": None if r["hypothesis_index"] is None else f"h{r['hypothesis_index']}",
             "status": r["status"], "value_text": r.get("value")} for r in responses
            if not (r["dimension"] == "L3.2" and r["hypothesis_index"] is None)]
    if any(c.get("amount_original") is not None for c in cost_items):
        rows.append({"dimension_code": "L2.1", "field_key": "notes", "hypothesis_id": None, "status": "filled", "value_text": "costs"})
    hyps = sorted({r["hypothesis_id"] for r in rows if r["hypothesis_id"]})
    return rows, [{"id": h} for h in hyps]


cases = []
for p in sorted((out / "extraction").glob("CLAB-P-*.json")):
    res = json.loads(p.read_text(encoding="utf-8"))
    rows, hyps = tracker_rows(res["responses"], res.get("cost_items", []))
    cases.append((f"extraction {p.stem}", for_extraction(res, catalog)["by_subdimension"], rows, hyps))
for p in sorted((out / "gold").glob("CLAB-P-*.json")):
    g = json.loads(p.read_text(encoding="utf-8"))
    rows, hyps = tracker_rows(g["responses"])
    cases.append((f"workbook {p.stem}", for_gold(g, catalog)["by_subdimension"], rows, hyps))

js = ("const {evaluability}=require(process.argv[1]);const fs=require('fs');const [cat,cases]=JSON.parse(fs.readFileSync(0,'utf8'));"
      "process.stdout.write(JSON.stringify(cases.map(c=>evaluability(cat,c[0],c[1]).by_subdimension)));")
res = subprocess.run(["node", "-e", js, str(ROOT / "web" / "evaluability.js")], input=json.dumps([catalog, [[c[2], c[3]] for c in cases]]),
                     capture_output=True, text=True, check=True)
bad = 0
for (name, py, *_), jsd in zip(cases, json.loads(res.stdout)):
    if py != jsd:
        bad += 1
        print(name, {k: (py[k], jsd[k]) for k in py if py[k] != jsd[k]})
assert not bad, f"{bad} of {len(cases)} differ"
print(f"ok: {len(cases)} pilots score the same in Python and JavaScript")
