"""End-to-end test of stage 6 against a throwaway PostgreSQL database (not Supabase).

Needs a local PostgreSQL server you can create databases on, `psql` on PATH and, optionally, Node with the `xlsx`
package to import the hand-made workbooks first, the way the tracker does. Run from retro/:

    python tests/test_stage6_postgres.py <out folder> "<psql connection args>" [<NODE_PATH with xlsx> <workbooks folder>]
    e.g. python tests/test_stage6_postgres.py out "-h /tmp -p 54329 -U postgres" /path/node_modules gold_workbooks

What it checks
    1. schema.sql + 001 + 002 apply on a Supabase-like stub (auth.users, auth.uid()); 002 runs twice without error.
    2. The hand-made workbooks import (import/workbooks_to_sql.js), then every stage-6 script runs without error.
    3. Workbook pilots are matched, not duplicated; they keep their human hypotheses and values; the extraction only
       fills fields they left empty.
    4. Running the scripts again inserts nothing (idempotent).
    5. A value a person saves after the load is never written over by a later load, even when the extraction changes.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RETRO = ROOT / "retro"
out, conn = Path(sys.argv[1]), sys.argv[2].split()
node_path = sys.argv[3] if len(sys.argv) > 3 else None
workbooks = Path(sys.argv[4]) if len(sys.argv) > 4 else RETRO / "gold_workbooks"
DB = "clab_stage6_test"


def psql(*args, db=DB, check=True):
    r = subprocess.run(["psql", *conn, "-d", db, "-v", "ON_ERROR_STOP=1", "-q", "-At", *args], capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"psql failed: {r.stderr[-2000:]}")
    return r


def val(sql):
    return psql("-c", sql).stdout.strip()


def load_all():
    notes = []
    for f in sorted((out / "load").glob("load_*.sql")):
        r = psql("-f", str(f))
        notes += [line.split("NOTICE:  ")[1] for line in r.stderr.splitlines() if "NOTICE:  CLAB-P" in line]
    return notes


psql("-c", f"drop database if exists {DB}", db="postgres")
psql("-c", f"create database {DB}", db="postgres")
psql("-c", "create schema auth; create table auth.users (id uuid primary key default gen_random_uuid(), email text);"
           "create function auth.uid() returns uuid language sql stable as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;")
psql("-f", str(ROOT / "db" / "schema.sql"))
psql("-f", str(ROOT / "db" / "migrations" / "001_import_source.sql"))
psql("-c", "insert into allowed_emails values ('admin@test.org','Admin','admin'),('editor@test.org','Editor','editor');"
           "insert into auth.users(email) values ('admin@test.org'),('editor@test.org');")

gold_files = sorted(workbooks.glob("*.xls*"))
if node_path and gold_files:
    with tempfile.TemporaryDirectory() as td:
        sql = Path(td) / "gold.sql"
        subprocess.run(["node", str(ROOT / "import" / "workbooks_to_sql.js"), str(sql), *map(str, gold_files)], check=True,
                       env={"NODE_PATH": node_path, "PATH": "/usr/bin:/bin:/usr/local/bin"}, capture_output=True)
        psql("-f", str(sql))
n_gold = int(val("select count(*) from pilots"))
human_rows = int(val("select count(*) from responses"))
human_hyps = int(val("select count(*) from hypotheses"))

psql("-f", str(ROOT / "db" / "migrations" / "002_retro_provenance.sql"))
psql("-f", str(ROOT / "db" / "migrations" / "002_retro_provenance.sql"))
print("ok: migration 002 applies twice")

subprocess.run([sys.executable, str(RETRO / "stage6_load_sql.py"), "--out", str(out), "--author-email", "admin@test.org"], check=True, capture_output=True)
notes = load_all()
n_ext = len(list((out / "extraction").glob("CLAB-P-*.json")))
assert len(notes) == n_ext, f"{len(notes)} pilots loaded, expected {n_ext}"
assert int(val("select count(*) from pilots")) == n_ext + n_gold - sum("existing pilot" in n for n in notes), "a workbook pilot was duplicated"
assert int(val("select count(*) from responses where origin = 'human'")) == human_rows, "human rows changed"
assert int(val("select count(*) from hypotheses where origin = 'human'")) == human_hyps
assert val("select count(*) from hypotheses h where origin = 'retro_extraction' and exists "
           "(select 1 from hypotheses x where x.pilot_id = h.pilot_id and x.origin = 'human')") == "0", "machine hypotheses added to a human-evaluated pilot"
assert val("select count(*) from (select pilot_id, dimension_code, field_key, hypothesis_id from responses_current where origin = 'retro_extraction' "
           "intersect select pilot_id, dimension_code, field_key, hypothesis_id from responses where origin = 'human') x") == "0", "a human value was written over"
print(f"ok: first load - {n_ext} pilots ({sum('existing pilot' in n for n in notes)} matched to workbook pilots), "
      f"{val('select count(*) from responses where origin = $$retro_extraction$$')} draft values")

again = load_all()
assert all("(0 values inserted" in n for n in again), [n for n in again if "(0 values inserted" not in n][:3]
print("ok: second load inserts nothing")

# a person edits one draft; the extraction then changes that field and another one; reload
pid_key = val("select pilot_key from pilots where import_source = 'retro_extraction' order by pilot_key limit 1")
pid = val(f"select id from pilots where pilot_key = '{pid_key}'")
target = val(f"select dimension_code || '|' || field_key from responses_current where pilot_id = '{pid}' and origin = 'retro_extraction' "
             "and hypothesis_id is null and status = 'filled' and field_key = 'justification' order by dimension_code limit 2").splitlines()
(d1, f1), (d2, f2) = (t.split("|") for t in target)
editor = val("select id from users where email = 'editor@test.org'")
psql("-c", f"insert into responses (pilot_id, dimension_code, field_key, stage, status, value_text, author_id) "
           f"values ('{pid}', '{d1}', '{f1}', 'evaluation', 'filled', 'Edited by a person', '{editor}')")
src = out / "extraction" / f"{pid_key}.json"
backup = src.read_text(encoding="utf-8")
res = json.loads(backup)
for r in res["responses"]:
    if r["hypothesis_index"] is None and (r["dimension"], r["field"]) in ((d1, f1), (d2, f2)):
        r["value"] = "Changed by a new extraction"
try:
    src.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    subprocess.run([sys.executable, str(RETRO / "stage6_load_sql.py"), "--out", str(out), "--pilots", pid_key], check=True, capture_output=True)
    load_all()
finally:
    src.write_text(backup, encoding="utf-8")
    subprocess.run([sys.executable, str(RETRO / "stage6_load_sql.py"), "--out", str(out)], check=True, capture_output=True)
cur = lambda d, f: val(f"select origin || '|' || value_text from responses_current where pilot_id = '{pid}' and dimension_code = '{d}' and field_key = '{f}' and hypothesis_id is null")
assert cur(d1, f1) == "human|Edited by a person", cur(d1, f1)
assert cur(d2, f2) == "retro_extraction|Changed by a new extraction", cur(d2, f2)
print(f"ok: the human edit on {d1}/{f1} survives a new extraction; the untouched draft {d2}/{f2} is updated")
print("ok: evaluability baselines -", val("select string_agg(source || ' ' || n, ', ') from (select source, count(*) n from evaluability_baselines group by 1) x"))
psql("-c", f"drop database {DB}", db="postgres")
