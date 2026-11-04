"""Stage 6 - Load the extraction into the tracker (SQL scripts for the Supabase SQL editor).

Why SQL scripts
    It follows the path the tracker already uses for workbooks (import/workbooks_to_sql.js). The scripts run in the
    Supabase SQL editor under the project owner's session, so no service key leaves Supabase and the IDB network
    never has to reach the database from Python. Every script is plain text you can read before running.

What a script does, per pilot (one DO block each, so one pilot failing does not stop the others)
    1. Finds the pilot in the tracker:
         a. by pilot_key (CLAB-P-xxx), the stable key from the retro registry;
         b. for the hand-made workbooks already imported (data/gold_workbooks.csv), by import_source;
         c. by (tc_code, name).
       If it is not there, creates it as a retrospective pilot at the evaluation stage, from the extraction's cover
       fields (registry values where the extraction has none; `Unknown` where neither has one, without a note,
       decision 2026-11-02). The cover values with their evidence are kept in pilots.retro_cover for review.
    2. Hypotheses: only for pilots with no human hypotheses. A pilot evaluated by a person keeps its hypotheses
       and gets no machine ones.
    3. Responses: inserts the extraction as drafts (origin = 'retro_extraction', with confidence, evidence and
       flags) only where the field has no current row, or where the current row is an older machine draft whose
       content changed. **A human value is never written over** (decision 2026-11-04).
    4. Evaluability baseline: the extraction's score (and the hand-made workbook's score, where there is one).

    Partial dates (YYYY or YYYY-MM) are stored as the first day of the period, and the precision goes in the note.
    Cost line items have no USD amount yet (exchange-rate source is an open item), so they go to the L2.1 notes field
    in their original currency, with their evidence; USD subcategory totals are entered later.

Prerequisites (once): db/migrations/002_retro_provenance.sql in the SQL editor.

Usage (from retro/, after stage 5 and evaluability.py)
    python stage6_load_sql.py --out out                          # all pilots in out/extraction
    python stage6_load_sql.py --out out --author-email you@iadb.org
    python stage6_load_sql.py --out out --pilots CLAB-P-083       # one pilot

Writes
    out/load/load_NN.sql        scripts for the SQL editor, a few pilots each (run them in order)
    out/load/load_preview.csv   one row per value the scripts propose (what a reviewer will see), for checking first
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd

from evaluability import for_extraction, for_gold
from stage5_gold_workbooks import find_catalog

HERE = Path(__file__).parent
TAG = "$clab$"          # quotes every value
BLOCK = "$clab_do$"     # delimits each DO block; plain $$ would break on text such as "cost ($ to $$$)"
COVER_TEXT = ["city", "country", "problem_definition", "target_population", "lead", "counterpart"]


def q(v):
    if v is None:
        return "null"
    s = str(v)
    for t in (TAG, BLOCK):
        s = s.replace(t, t.replace("$", "$ "))
    return f"{TAG}{s}{TAG}"


def date_parts(v):
    """'2025-03' -> ('2025-03-01', 'month'); '2025' -> ('2025-01-01', 'year'); ISO day -> (v, None)."""
    v = str(v or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v, None
    if re.fullmatch(r"\d{4}-\d{2}", v):
        return v + "-01", "month"
    if re.fullmatch(r"\d{4}", v):
        return v + "-01-01", "year"
    return None, None


def dim_fields(dim, catalog):
    f = dim["fields"]
    return f if isinstance(f, list) else catalog["field_sets"][f]


def evidence_json(ev, docs):
    return [{"doc_id": e.get("doc_id"), "document": docs.get(e.get("doc_id")), "location": e.get("location"),
             "quote": e.get("quote"), "verified": bool(e.get("verified"))} for e in ev or []]


def rows_for(res, catalog, docs):
    """Extraction result -> response rows for the tracker."""
    types = {(d["code"], f["key"]): f.get("type") for d in catalog["dimensions"] for f in dim_fields(d, catalog)}
    out = []
    for r in res["responses"]:
        if r["dimension"] == "L3.2" and r["hypothesis_index"] is None:
            continue  # the "no hypothesis" gap: the tracker keeps L3.2 per hypothesis, so it goes to retro_cover
        t = types.get((r["dimension"], r["field"]))
        note = (r.get("note") or "").strip() or None
        vt = vd = None
        if r["status"] == "filled":
            if t == "date":
                vd, prec = date_parts(r["value"])
                if prec:
                    note = ((note or "") + f" [Date precision: {prec}; stored as the first day of the {prec}.]").strip()
                if not vd:
                    vt = r["value"]
            else:
                vt = r["value"]
        elif not note:
            note = "No evidence found in the pilot's documents (retrospective extraction)."
        out.append({"dimension": r["dimension"], "field": r["field"], "hyp": r["hypothesis_index"], "stage": r["stage"],
                    "status": r["status"], "value_text": vt, "value_date": vd, "note": note,
                    "confidence": round(float(r.get("confidence") or 0), 2), "evidence": evidence_json(r.get("evidence"), docs),
                    "flags": r.get("flags") or []})
    if res.get("cost_items"):
        lines, ev = [], []
        for c in res["cost_items"]:
            amt = c.get("amount_original")
            lines.append(f"- {c.get('subcategory')}: {amt if amt is not None else 'amount not stated'} {c.get('currency') or ''}"
                         f" ({c.get('year') or 'year not stated'}) - {c.get('label')}; covers: {c.get('covers')}")
            ev += c.get("evidence") or []
        out.append({"dimension": "L2.1", "field": "notes", "hyp": None, "stage": "evaluation", "status": "filled",
                    "value_text": "Cost line items found in the documents (original currency, not converted to USD):\n" + "\n".join(lines),
                    "value_date": None, "note": "USD subcategory totals to be entered once an exchange-rate source is agreed.",
                    "confidence": 0.9, "evidence": evidence_json(ev, docs), "flags": []})
    return out


def pilot_block(pid, res, reg, gold_pattern, catalog, docs, gold_ev):
    p = res["pilot"]
    rg = reg.get(pid, {})

    def cover(k, fallback=None):
        e = p.get(k) or {}
        if e.get("status") == "filled" and e.get("value"):
            return e["value"]
        return fallback if fallback else "Unknown"

    ptype = cover("pilot_type")
    if ptype not in {t["key"] for t in catalog["pilot_types"]}:
        raise SystemExit(f"{pid}: pilot_type {ptype!r} is not in the catalog; fix it before loading")
    attribution = next(t["attribution"] for t in catalog["pilot_types"] if t["key"] == ptype)
    tc = str(rg.get("tc_primary") or "").strip()
    if not re.fullmatch(r"[A-Z]{2}-T\d{4}", tc):
        raise SystemExit(f"{pid}: TC code {tc!r} does not match AZ-T####")
    tcs = [t.strip() for t in str(rg.get("tcs_all") or tc).split(";") if t.strip()]
    name = rg.get("canonical_name") or pid
    ds, _ = date_parts(cover("date_started", "")) if p.get("date_started", {}).get("status") == "filled" else (None, None)
    de, _ = date_parts(cover("date_ended", "")) if p.get("date_ended", {}).get("status") == "filled" else (None, None)
    retro_cover = {k: {"status": v.get("status"), "value": v.get("value"), "note": v.get("note"), "confidence": v.get("confidence"),
                       "evidence": evidence_json(v.get("evidence"), docs)} for k, v in p.items()}
    lifecycle = rg.get("status") or None
    nohyp = next((r for r in res["responses"] if r["dimension"] == "L3.2" and r["hypothesis_index"] is None), None)
    if nohyp:
        retro_cover["L3.2_no_hypothesis"] = {"status": nohyp["status"], "note": nohyp.get("note"), "evidence": evidence_json(nohyp.get("evidence"), docs)}

    rows = rows_for(res, catalog, docs)
    n_h = len(res.get("hypotheses", []))
    ev = res.get("evaluability") or for_extraction(res, catalog)

    L = [f"-- {pid} · {name} ({tc})", f"do {BLOCK}", "declare",
         "  pid uuid; uid uuid; existing boolean := false; human_h int := 0; h uuid[] := array[]::uuid[]; hid uuid; n int := 0; skipped int := 0;",
         "begin",
         "  select id into uid from users where lower(email) = lower(current_setting('clab.author_email', true)) and active;",
         "  if uid is null then select id into uid from users where role = 'admin' and active order by created_at limit 1; end if;",
         "  if uid is null then raise exception 'No author: sign in once with an admin email first'; end if;",
         f"  select id into pid from pilots where pilot_key = {q(pid)};"]
    if gold_pattern:
        L.append(f"  if pid is null then select id into pid from pilots where import_source ilike {q('%' + gold_pattern + '%')} order by created_at limit 1; end if;")
    L += [f"  if pid is null then select id into pid from pilots where tc_code = {q(tc)} and name = {q(name)}; end if;",
          "  if pid is null then",
          "    insert into pilots (tc_code, name, city, country, pilot_type, attribution_category, mode, problem_definition, target_population,",
          "                        lead, counterpart, date_started, date_ended, current_stage, created_by, import_source, pilot_key, related_tcs,",
          "                        lifecycle_status, retro_cover)",
          f"    values ({q(tc)}, {q(name)}, {q(cover('city', rg.get('cities')))}, {q(cover('country'))}, {q(ptype)}, {q(attribution)}, 'retrospective',",
          f"            {q(cover('problem_definition'))}, {q(cover('target_population'))}, {q(cover('lead'))}, {q(cover('counterpart'))},",
          f"            {q(ds) + '::date' if ds else 'null'}, {q(de) + '::date' if de else 'null'}, 'evaluation', uid, 'retro_extraction', {q(pid)},",
          f"            array[{', '.join(q(t) for t in tcs)}]::text[], {q(lifecycle)}, {q(json.dumps(retro_cover, ensure_ascii=False))}::jsonb)",
          "    returning id into pid;",
          "  else",
          "    existing := true;",
          f"    update pilots set pilot_key = coalesce(pilot_key, {q(pid)}), related_tcs = coalesce(related_tcs, array[{', '.join(q(t) for t in tcs)}]::text[]),",
          f"                      lifecycle_status = coalesce(lifecycle_status, {q(lifecycle)}), retro_cover = {q(json.dumps(retro_cover, ensure_ascii=False))}::jsonb",
          "    where id = pid;",
          "  end if;",
          "  select count(*) into human_h from hypotheses where pilot_id = pid and origin = 'human' and not archived;"]
    if n_h:
        L.append("  if human_h = 0 then")
        for i, hy in enumerate(res["hypotheses"]):
            L += [f"    select id into hid from hypotheses where pilot_id = pid and origin = 'retro_extraction' and ordinal = {i + 1};",
                  "    if hid is null then",
                  f"      insert into hypotheses (pilot_id, ordinal, label, created_by, origin) values (pid, {i + 1}, {q(hy['label'])}, uid, 'retro_extraction') returning id into hid;",
                  f"    else update hypotheses set label = {q(hy['label'])} where id = hid; end if;",
                  "    h := h || hid; hid := null;"]
        L.append("  end if;")
    vals = []
    for r in rows:
        vals.append(f"    ({q(r['dimension'])}, {q(r['field'])}, {r['hyp'] + 1 if r['hyp'] is not None else 'null'}::int, {q(r['stage'])}, {q(r['status'])}, "
                    f"{q(r['value_text'])}, {q(r['value_date']) + '::date' if r['value_date'] else 'null::date'}, {q(r['note'])}, {r['confidence']}, "
                    f"{q(json.dumps(r['evidence'], ensure_ascii=False))}::jsonb, array[{', '.join(q(f) for f in r['flags'])}]::text[])")
    # the "no hypothesis" gap (L3.2 at pilot level) has no hypothesis to hang on: keep it in retro_cover instead
    L += ["  insert into responses (pilot_id, dimension_code, field_key, hypothesis_id, stage, status, value_text, value_date, note,",
          "                         author_id, origin, confidence, evidence, flags)",
          "  select pid, v.d, v.f, case when v.hi is null then null else h[v.hi] end, v.stage, v.status, v.vt, v.vd, v.note,",
          "         uid, 'retro_extraction', v.conf, v.ev, v.flags",
          "  from (values",
          ",\n".join(vals),
          "  ) as v(d, f, hi, stage, status, vt, vd, note, conf, ev, flags)",
          "  left join responses_current c",
          "    on c.pilot_id = pid and c.dimension_code = v.d and c.field_key = v.f",
          "   and c.hypothesis_id is not distinct from (case when v.hi is null then null else h[v.hi] end)",
          "  where (v.hi is null or h[v.hi] is not null)",
          "    and (c.id is null or (c.origin = 'retro_extraction' and",
          "         (c.status, coalesce(c.value_text, ''), coalesce(c.value_date::text, ''), coalesce(c.note, ''))",
          "         is distinct from (v.status, coalesce(v.vt, ''), coalesce(v.vd::text, ''), coalesce(v.note, ''))));",
          "  get diagnostics n = row_count;",
          f"  skipped := {len(rows)} - n;",
          "  insert into evaluability_baselines (pilot_id, source, score, complete, total, detail, catalog_version, computed_at)",
          f"  values (pid, 'retro_extraction', {ev['score']}, {ev['complete']}, {ev['total']}, {q(json.dumps(ev['by_subdimension']))}::jsonb,"
          f" {q(ev.get('catalog_version'))}, {q(ev['computed_at'])}::date)",
          "  on conflict (pilot_id, source, computed_at) do update set score = excluded.score, complete = excluded.complete, detail = excluded.detail;"]
    if gold_ev:
        L += ["  insert into evaluability_baselines (pilot_id, source, score, complete, total, detail, catalog_version, computed_at)",
              f"  values (pid, 'evaluator_workbook', {gold_ev['score']}, {gold_ev['complete']}, {gold_ev['total']}, {q(json.dumps(gold_ev['by_subdimension']))}::jsonb,"
              f" {q(gold_ev.get('catalog_version'))}, {q(gold_ev['computed_at'])}::date)",
              "  on conflict (pilot_id, source, computed_at) do update set score = excluded.score, complete = excluded.complete, detail = excluded.detail;"]
    L += [f"  raise notice '{pid}: % (% values inserted, % kept: human value or unchanged draft; human hypotheses: %)',"
          f" case when existing then 'existing pilot' else 'new pilot' end, n, skipped, human_h;",
          f"end {BLOCK};", ""]
    return "\n".join(L), rows, name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--pilots", help="comma-separated pilot_ids (default: every out/extraction/CLAB-P-*.json)")
    ap.add_argument("--author-email", help="tracker user the drafts are attributed to (default: the first admin)")
    ap.add_argument("--batch", type=int, default=5, help="pilots per SQL file")
    ap.add_argument("--catalog")
    a = ap.parse_args()
    out = Path(a.out)
    catalog = find_catalog(a.catalog)
    R = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Registry")
    reg = {r.pilot_id: {k: (None if pd.isna(v) else v) for k, v in r._asdict().items()} for r in R.itertuples(index=False)}
    docs = {}
    if (out / "documents.csv").exists():
        D = pd.read_csv(out / "documents.csv", dtype=str)
        docs = dict(zip(D.doc_id, D.filename))
    gold_map = dict(pd.read_csv(HERE / "data" / "gold_workbooks.csv", dtype=str)[["pilot_id", "file_contains"]].values)
    files = sorted((out / "extraction").glob("CLAB-P-*.json"))
    if a.pilots:
        keep = {p.strip() for p in a.pilots.split(",")}
        files = [f for f in files if f.stem in keep]
    if not files:
        raise SystemExit("no extraction results in out/extraction")
    load = out / "load"
    load.mkdir(exist_ok=True)
    for old in load.glob("load_*.sql"):
        old.unlink()
    preview, blocks = [], []
    for f in files:
        res = json.loads(f.read_text(encoding="utf-8"))
        pid = res["pilot_id"]
        gold_ev = None
        gp = out / "gold" / f"{pid}.json"
        if gp.exists():
            gold_ev = for_gold(json.loads(gp.read_text(encoding="utf-8")), catalog)
        sql, rows, name = pilot_block(pid, res, reg, gold_map.get(pid), catalog, docs, gold_ev)
        blocks.append((pid, sql))
        for r in rows:
            preview.append({"pilot_id": pid, "pilot": name, "dimension": r["dimension"], "field": r["field"],
                            "hypothesis": None if r["hyp"] is None else r["hyp"] + 1, "status": r["status"],
                            "value": r["value_text"] if r["value_text"] is not None else r["value_date"], "note": r["note"],
                            "confidence": r["confidence"], "quotes": len(r["evidence"]),
                            "quotes_verified": sum(e["verified"] for e in r["evidence"]), "flags": "; ".join(r["flags"])})
    head = ["-- C-LAB tracker: retrospective extraction, generated by retro/stage6_load_sql.py. Run in the Supabase SQL editor,",
            "-- after db/migrations/002_retro_provenance.sql. Safe to re-run: human values are never written over.",
            f"select set_config('clab.author_email', {q(a.author_email)}, false);" if a.author_email else "-- author: the first admin user (use --author-email to choose)",
            ""]
    nfiles = 0
    for i in range(0, len(blocks), a.batch):
        part = blocks[i:i + a.batch]
        nfiles += 1
        (load / f"load_{nfiles:02d}.sql").write_text("\n".join(head + [s for _, s in part]), encoding="utf-8")
        print(f"load_{nfiles:02d}.sql: {', '.join(p for p, _ in part)}")
    P = pd.DataFrame(preview)
    P.to_csv(load / "load_preview.csv", index=False, encoding="utf-8-sig")
    print(f"{len(files)} pilots, {len(P)} values proposed ({(P.status == 'filled').sum()} filled, {(P.status != 'filled').sum()} gaps) -> {load}")


if __name__ == "__main__":
    main()
