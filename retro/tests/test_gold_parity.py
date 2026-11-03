"""Parity check: the Python workbook importer (stage5_gold_workbooks.py) must read a workbook exactly as the
tracker's in-app importer (web/import.js) does.

Run from retro/ (needs Node and `npm i xlsx@0.18.5` in some folder):
    NODE_PATH=<folder>/node_modules python tests/test_gold_parity.py <folder with workbooks>
Compares cover fields, hypotheses and every (dimension, field, hypothesis, stage, status, value, note).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import stage5_gold_workbooks as g  # noqa: E402

JS = """
const fs=require('fs');const {importWorkbook}=require(process.argv[1]+'/web/import.js');
const cat=JSON.parse(fs.readFileSync(process.argv[1]+'/catalog/framework_catalog.json'));
const r=importWorkbook(new Uint8Array(fs.readFileSync(process.argv[2])),cat,'x');process.stdout.write(JSON.stringify(r));
"""


def main(folder):
    catalog = g.find_catalog()
    files = sorted(p for p in Path(folder).iterdir() if p.suffix.lower() in (".xlsm", ".xlsx"))
    bad = 0
    for f in files:
        js = json.loads(subprocess.run(["node", "-e", JS, str(HERE.parent), str(f)], capture_output=True, text=True, check=True,
                                       env={**os.environ}).stdout)
        py = g.import_workbook(f, catalog)
        for k in ("name", "tc_code", "city", "country", "pilot_type", "attribution_category", "problem_definition", "target_population",
                  "lead", "date_started", "date_ended"):
            if (js["pilot"].get(k) or None) != (py["pilot"].get(k) or None):
                bad += 1
                print(f"{f.name}: cover {k}: js={js['pilot'].get(k)!r} py={py['pilot'].get(k)!r}")
        jr = sorted((x["dimension_code"], x["field_key"], x["hypothesis_index"], x["stage"], x["status"], x["value_text"], x["note"]) for x in js["responses"])
        pr = sorted((x["dimension"], x["field"], x["hypothesis_index"], x["stage"], x["status"], x["value"], x["note"]) for x in py["responses"])
        if jr != pr:
            bad += 1
            for a, b in zip(jr, pr):
                if a != b:
                    print(f"{f.name}: first difference\n  js={a}\n  py={b}")
                    break
            print(f"{f.name}: js {len(jr)} values, py {len(pr)} values")
        print(f"{'ok ' if jr == pr else 'DIFF'} {f.name[:70]}: {len(pr)} values, {len(py['hypotheses'])} hypotheses")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(sys.argv[1])
