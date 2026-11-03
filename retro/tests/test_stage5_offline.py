"""Offline end-to-end test of stage 5b checks (no key, no network).

A fake Anthropic client answers each level with: a filled field quoting the pilot's corpus verbatim (must verify),
a filled field with an invented quote (must be flagged), an invalid degree (flagged), a gap without a note
(flagged), one hypothesis and one cost item. Run from retro/ after stage 4:
    python tests/test_stage5_offline.py <out folder> <pilot_id>
"""
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stage5_extract as s5  # noqa: E402
from stage3_screen_unreviewed import Model, strict_schema  # noqa: E402
from tests.test_stage3_api_shape import FakeStream, walk  # noqa: E402

out, pid = Path(sys.argv[1]), sys.argv[2]
corpus = (out / "kb" / pid / "corpus.md").read_text(encoding="utf-8")
m = re.search(r"\[([0-9a-f]{12}) · (\w+ \d+)\]\n(.{80,400}?)\n", corpus)
doc_id, loc, line = m.group(1), m.group(2), m.group(3)
quote = line.strip()[:120]
catalog = s5.find_catalog()


class Fake:
    def create(self, **kw):
        walk(kw["output_config"]["format"]["schema"])
        level = int(re.search(r"Level (\d)", kw["messages"][0]["content"]).group(1))
        _, pairs, hyp = s5.checklist(level, catalog)
        ev = [{"doc_id": doc_id, "location": loc, "quote": quote}]
        fields = []
        for i, (d, f) in enumerate(pairs):
            if i == 0:
                fields.append({"dimension": d, "field": f, "status": "filled", "value": "x", "note": "", "confidence": 0.9, "evidence": ev})
            elif i == 1:
                fields.append({"dimension": d, "field": f, "status": "filled", "value": "x", "note": "", "confidence": 1.4,
                               "evidence": [{"doc_id": doc_id, "location": loc, "quote": "this sentence is not in any document at all"}]})
            elif f == "degree":
                fields.append({"dimension": d, "field": f, "status": "filled", "value": "very high", "note": "", "confidence": 0.5, "evidence": ev})
            elif i == 3:
                fields.append({"dimension": d, "field": f, "status": "no_evidence", "value": "", "note": "", "confidence": 0, "evidence": []})
            # remaining pairs omitted on purpose -> must come back as missing_in_answer
        ans = {"fields": fields}
        if level == 2:
            ans["cost_items"] = [{"subcategory": "capex.studies", "label": "consultancy", "amount_original": 1000.0, "currency": "USD",
                                  "year": "2024", "covers": "x", "evidence": ev}]
        if hyp:
            ans["hypotheses"] = [{"label": "H1", "fields": [{"dimension": "L3.2", "field": "hypothesis", "status": "filled", "value": "h",
                                                             "note": "", "confidence": 0.7, "evidence": ev}]}]
        usage = SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(ans))], usage=usage, stop_reason="end_turn")

    def stream(self, **kw):
        return FakeStream(self.create(**kw))


model = Model("claude-sonnet-5-5", mock=True)
model.mock = False
model.client = SimpleNamespace(messages=Fake())
res = s5.extract_pilot(pid, model, catalog, out, 150000, dry_run=False, mock_run=True)
rs = res["responses"]
first = rs[0]
assert first["status"] == "filled" and first["evidence"][0]["verified"], "verbatim quote must verify"
assert any("unverified quote" in r["flags"] for r in rs), "invented quote must be flagged"
assert any(r["flags"] and r["flags"][0].startswith("invalid degree") for r in rs), "invalid degree must be flagged"
assert any("no_evidence without a note" in r["flags"] for r in rs), "gap without note must be flagged"
assert any(r.get("missing_in_answer") for r in rs), "omitted fields must be filled in as gaps"
assert all(0 <= r["confidence"] <= 1 for r in rs)
assert res["hypotheses"] and res["cost_items"] and res["cost_items"][0]["evidence"][0]["verified"]
assert res["pilot"], "cover fields present"
print("ok:", res["checks"])
