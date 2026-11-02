"""Stage 3 - Screen the documents of unvalidated entries against the team-validated pilots (Anthropic API).

Why
    The retrospective evaluation covers only the pilots the team validated (Review = ✓). The other entries in the
    repository ("?", the doubtful entry and the two umbrella entries) are not evaluated, but their folders may hold
    documents that belong to a validated pilot (misfiled reports, programme reports with a section per pilot).
    This stage finds those documents so they can be moved into the right pilot's knowledge base.
    It does NOT try to decide whether an unvalidated entry is a pilot of its own.

How
    1. Profiles: one API call per validated pilot builds a short profile (summary, distinctive terms, period) from
       excerpts of its own documents. Profiles are cached in <out>/screening/profiles.json.
    2. Screening: one API call per distinct document filed under an unvalidated entry. The system prompt holds the
       catalog of validated-pilot profiles (prompt-cached across calls); the user turn holds the document text with
       page/slide/block markers. Claude answers through a forced tool call (structured JSON): verdict, matched
       pilots, verbatim evidence quote and its location.
    3. Verification: every evidence quote is checked against the document's full text (whitespace/case-insensitive).
       Unverified quotes are flagged, never silently accepted.
    4. Aggregation: per-entity proposal + a list of proposed document reassignments for the team to approve.

Prompts live in prompts/*.md and are versioned with the code; their hash is part of the cache key, so editing a
prompt re-runs the affected calls and nothing else.

Usage (from retro/, after stages 1 and 2)
    set ANTHROPIC_API_KEY=sk-ant-...            (PowerShell: $env:ANTHROPIC_API_KEY="sk-ant-...")
    python stage3_screen_unreviewed.py --out out --dry-run      # builds every request, estimates tokens, no API call
    python stage3_screen_unreviewed.py --out out --limit 5      # try on 5 documents first
    python stage3_screen_unreviewed.py --out out                # full run (resumes where it stopped)
    python stage3_screen_unreviewed.py --out out --mock         # offline end-to-end test with a fake model

Reads
    <out>/pilot_registry.xlsx (sheets Registry, Documents) from stage 2, <out>/text/*.json from stage 1
Writes
    <out>/screening/profiles.json            validated-pilot profiles (input to the catalog)
    <out>/screening/responses.jsonl          one raw API result per document (append-only cache)
    <out>/screening/requests_preview.jsonl   what would be sent (dry-run only)
    <out>/screening_unreviewed.xlsx          README, ByEntity, ByDocument, Reassignments, Profiles, Run
    <out>/document_reassignments.csv         proposed moves for team approval (column team_decision)
"""
import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).parent
PROMPTS = HERE / "prompts"
DEFAULT_MODEL = "claude-sonnet-5-5"

VALIDATED_STATUSES = ("confirmed", "ongoing")                                # Review = ✓ in the repository
SCREEN_STATUSES = ("to_review", "doubtful_files", "umbrella_not_a_pilot")   # entries whose documents are screened
ADMIN_ROLES = {"invoice", "affidavit", "agreement"}                          # skipped when building profiles
PROFILE_ROLE_ORDER = ["final_report", "deliverable", "progress_report", "presentation", "work_plan", "tor"]

VERDICTS = ["belongs_to_validated_pilot", "covers_several_pilots", "mentions_validated_pilot_only",
            "unrelated_to_validated_pilots", "insufficient_text"]

PROFILE_TOOL = {
    "name": "record_profile",
    "description": "Record the profile of one validated pilot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "2-3 sentences in Spanish"},
            "key_terms": {"type": "array", "items": {"type": "string"}, "maxItems": 15},
            "period": {"type": "string"},
        },
        "required": ["summary", "key_terms", "period"],
    },
}

SCREEN_TOOL = {
    "name": "record_screening",
    "description": "Record whether the document belongs to one or more validated pilots.",
    "input_schema": {
        "type": "object",
        "properties": {
            "document_subject": {"type": "string"},
            "verdict": {"type": "string", "enum": VERDICTS},
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "pilot_id": {"type": "string"},
                        "role": {"type": "string", "enum": ["primary", "section", "mention"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence_quote": {"type": "string"},
                        "evidence_location": {"type": "string"},
                    },
                    "required": ["pilot_id", "role", "confidence", "evidence_quote", "evidence_location"],
                },
            },
            "rationale": {"type": "string"},
        },
        "required": ["document_subject", "verdict", "matches", "rationale"],
    },
}


# ------------------------------------------------------------------------------------------------ helpers
def sha(s, n=16):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:n]


def norm_for_match(s):
    """Lowercase, strip accents' combining marks inconsistently produced by PDF extraction, collapse whitespace."""
    s = unicodedata.normalize("NFKC", str(s)).replace("­", "").replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"-\s*\n\s*", "", s)                       # words hyphenated across lines
    return re.sub(r"\s+", " ", s).strip().lower()


def load_units(text_dir, doc_id):
    return json.loads((Path(text_dir) / f"{doc_id}.json").read_text(encoding="utf-8"))["units"]


def render(units):
    return "\n".join(f"[{u['unit']} {u['n']}]\n{u['text'].strip()}" for u in units if u["text"].strip())


def excerpt(units, max_chars, tail_share=0.25):
    """Whole text if it fits; otherwise the beginning and the end (conclusions), cut at unit boundaries."""
    full = render(units)
    if len(full) <= max_chars:
        return full, False
    head_budget, tail_budget = int(max_chars * (1 - tail_share)), int(max_chars * tail_share)
    head, used = [], 0
    for u in units:
        piece = f"[{u['unit']} {u['n']}]\n{u['text'].strip()}"
        if used + len(piece) > head_budget:
            head.append(piece[: max(0, head_budget - used)])
            break
        head.append(piece)
        used += len(piece) + 1
    tail, used = [], 0
    for u in reversed(units):
        piece = f"[{u['unit']} {u['n']}]\n{u['text'].strip()}"
        if used + len(piece) > tail_budget:
            break
        tail.insert(0, piece)
        used += len(piece) + 1
    return "\n".join(head) + "\n[... omitted ...]\n" + "\n".join(tail), True


def quote_verified(quote, full_text_norm):
    q = norm_for_match(quote).strip(" .\"'")
    if len(q) < 12:
        return False
    if q in full_text_norm:
        return True
    # tolerate a dropped/extra character at the edges (common when the model trims a word)
    return len(q) > 40 and q[5:-5] in full_text_norm


def est_tokens(chars):
    return int(chars / 3.5)


# ------------------------------------------------------------------------------------------------ model access
class Model:
    """Thin wrapper around the Anthropic Messages API with a forced tool call; --mock replaces the network call."""

    def __init__(self, model, mock=False, max_tokens=1500):
        self.model, self.mock, self.max_tokens = model, mock, max_tokens
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        self.lock = threading.Lock()
        if not mock:
            import anthropic  # imported lazily so --dry-run / --mock work without the SDK or a key
            if not os.environ.get("ANTHROPIC_API_KEY"):
                sys.exit("ANTHROPIC_API_KEY is not set. PowerShell: $env:ANTHROPIC_API_KEY=\"sk-ant-...\"")
            self.client = anthropic.Anthropic(max_retries=6)

    def call(self, system, user, tool, mock_fn=None):
        if self.mock:
            return mock_fn(), {}
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        out = next(b.input for b in resp.content if b.type == "tool_use")
        u = resp.usage
        usage = {k: getattr(u, k, 0) or 0 for k in self.usage if k != "calls"}
        with self.lock:
            self.usage["calls"] += 1
            for k, v in usage.items():
                self.usage[k] += v
        return out, usage


# ------------------------------------------------------------------------------------------------ stage 3a: profiles
def build_profiles(model, R, D, text_dir, out_dir, profile_chars, docs_per_pilot, dry_run, mock_run=False):
    # A mock run must never write into the real cache (same cache key as a real run of the same model)
    path = out_dir / ("profiles_mock.json" if mock_run else "profiles.json")
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    system = (PROMPTS / "profile_system.md").read_text(encoding="utf-8")
    jobs = []
    for r in R[R.status.isin(VALIDATED_STATUSES)].itertuples():
        docs = D[(D.pilot_id == r.pilot_id) & D.is_representative & ~D.role.isin(ADMIN_ROLES)].copy()
        docs["rank"] = docs.role.map(lambda x: PROFILE_ROLE_ORDER.index(x) if x in PROFILE_ROLE_ORDER else 99)
        docs = docs.sort_values(["rank", "chars"], ascending=[True, False]).head(docs_per_pilot)
        parts = []
        for d in docs.itertuples():
            ex, _ = excerpt(load_units(text_dir, d.doc_id), profile_chars, tail_share=0.0)
            parts.append(f"## Document: {d.original_name} ({d.role})\n{ex}")
        meta = (f"pilot_id: {r.pilot_id}\nname: {r.canonical_name}\ntechnical cooperation(s): {r.tcs_all}\n"
                f"cities: {r.cities}\naliases: {r.aliases if isinstance(r.aliases, str) else ''}")
        user = meta + "\n\n" + ("\n\n".join(parts) if parts else "(no documents with text)")
        key = sha(system + user + model.model)
        jobs.append((r, user, key, len(parts)))

    if dry_run:
        return cache, [{"stage": "profile", "pilot_id": r.pilot_id, "chars": len(system) + len(u), "cached": cache.get(r.pilot_id, {}).get("key") == k}
                       for r, u, k, _ in jobs]

    def run(job):
        r, user, key, n = job
        if cache.get(r.pilot_id, {}).get("key") == key:
            return r.pilot_id, cache[r.pilot_id]
        mock = lambda: {"summary": f"(mock) {r.canonical_name} en {r.cities}.", "key_terms": [w for w in re.findall(r"[A-Za-zÁ-ú]{5,}", r.canonical_name)][:5], "period": "unknown"}
        res, usage = model.call(system, user, PROFILE_TOOL, mock)
        return r.pilot_id, {"key": key, "name": r.canonical_name, "tcs": r.tcs_all, "cities": r.cities,
                            "aliases": r.aliases if isinstance(r.aliases, str) else "", "parent_pilot": r.parent_pilot if isinstance(r.parent_pilot, str) else "",
                            "n_docs_used": n, **res, "usage": usage}

    with ThreadPoolExecutor(4) as ex:
        for pid, prof in ex.map(run, jobs):
            cache[pid] = prof
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache, []


def catalog_text(profiles):
    blocks = []
    for pid in sorted(profiles):
        p = profiles[pid]
        aliases = "; ".join(a for a in str(p.get("aliases", "")).split("; ") if a and "_" not in a)  # drop folder slugs
        blocks.append(
            f"### {pid} — {p['name']}\n"
            f"Technical cooperation(s): {p['tcs']} · Cities: {p['cities']}"
            + (f" · Spin-off of {p['parent_pilot']}" if p.get("parent_pilot") else "") + "\n"
            + (f"Other names: {aliases}\n" if aliases else "")
            + f"Summary: {p['summary']}\nKey terms: {', '.join(p['key_terms'])}\nPeriod: {p['period']}")
    return "\n\n".join(blocks)


# ------------------------------------------------------------------------------------------------ stage 3b: screening
def screen(model, R, D, profiles, text_dir, out_dir, max_chars, workers, limit, dry_run):
    system_tpl = (PROMPTS / "screen_system.md").read_text(encoding="utf-8")
    system = system_tpl.replace("{catalog}", catalog_text(profiles))
    validated = set(R[R.status.isin(VALIDATED_STATUSES)].pilot_id)
    screened_pilots = set(R[R.status.isin(SCREEN_STATUSES)].pilot_id)
    name_of = dict(zip(R.pilot_id, R.canonical_name))

    # One job per distinct document (cluster) filed under at least one screened entry
    rows = D[D.pilot_id.isin(screened_pilots)]
    jobs = []
    for cid, g in rows.groupby("cluster_id"):
        all_copies = D[D.cluster_id == cid]
        rep = all_copies[all_copies.is_representative].iloc[0]
        filed_under = sorted(set(g.pilot_id))
        also_validated = sorted(set(all_copies.pilot_id) & validated)
        units = load_units(text_dir, rep.doc_id)
        ex, truncated = excerpt(units, max_chars)
        user = (f"Filed under (unvalidated entry): {', '.join(f'{p} {name_of[p]}' for p in filed_under)}\n"
                f"Technical cooperation: {rep.tc}\nFile name: {rep.original_name}\n"
                f"Text{' (excerpt)' if truncated else ''}:\n\n{ex}")
        jobs.append({"cluster_id": cid, "doc_id": rep.doc_id, "filed_under": filed_under, "also_filed_under_validated": also_validated,
                     "file": rep.original_name, "tc": rep.tc, "role": rep.role, "chars": int(rep.chars), "truncated": truncated,
                     "user": user, "units": units, "key": sha(system + user + model.model)})
    jobs.sort(key=lambda j: (j["filed_under"][0], j["file"]))
    if limit:
        jobs = jobs[:limit]

    resp_path = out_dir / "responses.jsonl"
    done = {}
    if resp_path.exists():
        for line in resp_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            done[rec["key"]] = rec

    if dry_run:
        with open(out_dir / "requests_preview.jsonl", "w", encoding="utf-8") as f:
            for j in jobs:
                f.write(json.dumps({k: v for k, v in j.items() if k != "units"}, ensure_ascii=False) + "\n")
        return [], [{"stage": "screen", "doc": j["file"], "chars": len(j["user"]), "system_chars": len(system), "cached": j["key"] in done} for j in jobs]

    lock = threading.Lock()

    def mock_answer(j):
        low = norm_for_match(j["user"])
        for pid, p in profiles.items():
            terms = [t for t in p["key_terms"] if len(t) > 4]
            if any(norm_for_match(t) in low for t in terms):
                q = re.sub(r"\s+", " ", j["units"][0]["text"]).strip()[:120]
                return {"document_subject": "(mock)", "verdict": "belongs_to_validated_pilot", "rationale": "(mock)",
                        "matches": [{"pilot_id": pid, "role": "primary", "confidence": 0.5, "evidence_quote": q, "evidence_location": "page 1"}]}
        return {"document_subject": "(mock)", "verdict": "unrelated_to_validated_pilots", "matches": [], "rationale": "(mock)"}

    def run(j):
        if j["key"] in done:
            return done[j["key"]]
        res, usage = model.call(system, j["user"], SCREEN_TOOL, lambda: mock_answer(j))
        full_norm = norm_for_match(render(j["units"]))
        for m in res.get("matches", []):
            m["quote_verified"] = quote_verified(m.get("evidence_quote", ""), full_norm)
            m["pilot_known"] = m.get("pilot_id") in validated
        rec = {"key": j["key"], "model": model.model, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
               **{k: j[k] for k in ("cluster_id", "doc_id", "filed_under", "also_filed_under_validated", "file", "tc", "role", "chars", "truncated")},
               "result": res, "usage": usage}
        if not model.mock:
            with lock, open(resp_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    results = []
    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(run, j) for j in jobs]
        for k, fu in enumerate(as_completed(futs), 1):
            results.append(fu.result())
            if k % 10 == 0 or k == len(futs):
                print(f"  screened {k}/{len(futs)}", flush=True)
    return results, []


# ------------------------------------------------------------------------------------------------ aggregation + output
def aggregate(results, R):
    name_of = dict(zip(R.pilot_id, R.canonical_name))
    status_of = dict(zip(R.pilot_id, R.status))
    doc_rows, moves = [], []
    for rec in results:
        res = rec["result"]
        ms = res.get("matches", [])
        strong = [m for m in ms if m["role"] in ("primary", "section") and m.get("pilot_known")]
        doc_rows.append({
            "filed_under": "; ".join(f"{p} {name_of.get(p, '')}" for p in rec["filed_under"]),
            "file": rec["file"], "tc": rec["tc"], "role": rec["role"], "chars": rec["chars"], "truncated": rec["truncated"],
            "verdict": res.get("verdict"), "document_subject": res.get("document_subject"),
            "matched_pilots": "; ".join(f"{m['pilot_id']} {name_of.get(m['pilot_id'], '?')} [{m['role']}, {m['confidence']:.2f}]" for m in ms),
            "all_quotes_verified": all(m.get("quote_verified") for m in ms) if ms else "",
            "evidence": " || ".join(f"{m['pilot_id']} @ {m['evidence_location']}: \"{m['evidence_quote']}\"" + ("" if m.get("quote_verified") else " [NOT FOUND IN TEXT]") for m in ms),
            "rationale": res.get("rationale"),
            "also_filed_under_validated": "; ".join(rec["also_filed_under_validated"]),
            "doc_id": rec["doc_id"], "cluster_id": rec["cluster_id"],
        })
        for m in strong:
            for src in rec["filed_under"]:
                moves.append({"doc_id": rec["doc_id"], "cluster_id": rec["cluster_id"], "file": rec["file"], "from_entry": src,
                              "from_name": name_of.get(src, ""), "from_status": status_of.get(src, ""), "to_pilot": m["pilot_id"],
                              "to_name": name_of.get(m["pilot_id"], ""), "role": m["role"], "confidence": m["confidence"],
                              "quote_verified": m.get("quote_verified"), "evidence_location": m["evidence_location"],
                              "evidence_quote": m["evidence_quote"], "team_decision": ""})
    B = pd.DataFrame(doc_rows)
    M = pd.DataFrame(moves, columns=["doc_id", "cluster_id", "file", "from_entry", "from_name", "from_status", "to_pilot", "to_name", "role",
                                     "confidence", "quote_verified", "evidence_location", "evidence_quote", "team_decision"])

    ent_rows = []
    for r in R[R.status.isin(SCREEN_STATUSES)].itertuples():
        docs = [x for x in results if r.pilot_id in x["filed_under"]]
        if not docs:
            continue
        mv = M[M.from_entry == r.pilot_id]
        n_to = mv.drop_duplicates("cluster_id")
        targets = mv.groupby("to_pilot").cluster_id.nunique().sort_values(ascending=False)
        n, k = len(docs), int(n_to.cluster_id.nunique())
        if k == 0:
            proposal = "no documents of validated pilots: stays out of the retrospective scope"
        elif k == n and len(targets) == 1:
            proposal = f"all {n} documents belong to {targets.index[0]} {name_of.get(targets.index[0], '')}: fold this entry into it"
        else:
            proposal = f"{k} of {n} documents belong to validated pilots: move those documents"
        ent_rows.append({
            "pilot_id": r.pilot_id, "name": r.canonical_name, "tc": r.tcs_all, "cities": r.cities, "status": r.status,
            "documents_screened": n, "documents_matched": k,
            "matched_to": "; ".join(f"{p} {name_of.get(p, '')} ({c} docs)" for p, c in targets.items()),
            "unverified_quotes": int((mv.quote_verified == False).sum()),  # noqa: E712
            "verdicts": "; ".join(f"{v}:{c}" for v, c in pd.Series([x["result"].get("verdict") for x in docs]).value_counts().items()),
            "proposal": proposal, "team_decision": "",
        })
    E = pd.DataFrame(ent_rows).sort_values(["documents_matched", "pilot_id"], ascending=[False, True]) if ent_rows else pd.DataFrame()
    return E, B, M


def style(xlsx):
    wb = load_workbook(xlsx)
    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F4E78")
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for col in ws.columns:
            w = min(70, max(10, max(len(str(c.value or "")) for c in col[:200]) * 0.9))
            ws.column_dimensions[get_column_letter(col[0].column)].width = w
    wb["README"].column_dimensions["A"].width = 150
    wb.save(xlsx)


# ------------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", help="stage 1/2 output folder (holds pilot_registry.xlsx and text/)")
    ap.add_argument("--model", default=os.environ.get("CLAB_MODEL", DEFAULT_MODEL))
    ap.add_argument("--max-chars", type=int, default=24000, help="document text sent per screening call (beginning + end)")
    ap.add_argument("--profile-chars", type=int, default=6000, help="text per document when building a profile")
    ap.add_argument("--profile-docs", type=int, default=3, help="documents per validated pilot used for its profile")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="screen only the first N documents (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="build requests and estimate tokens; no API calls")
    ap.add_argument("--mock", action="store_true", help="offline test: fake model answers, nothing is cached")
    a = ap.parse_args()

    out = Path(a.out)
    sdir = out / "screening"
    sdir.mkdir(parents=True, exist_ok=True)
    R = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Registry")
    D = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Documents", dtype={"doc_id": str, "cluster_id": str})
    model = Model(a.model, mock=a.mock or a.dry_run)
    t0 = time.time()

    print(f"[1/3] profiles of {R.status.isin(VALIDATED_STATUSES).sum()} validated pilots")
    profiles, prev_p = build_profiles(model, R, D, out / "text", sdir, a.profile_chars, a.profile_docs, a.dry_run, mock_run=a.mock)
    if a.dry_run:
        # The catalog cannot be built without profiles; estimate it with placeholders of typical size
        profiles = {r.pilot_id: {"name": r.canonical_name, "tcs": r.tcs_all, "cities": r.cities, "aliases": "", "parent_pilot": "",
                                 "summary": "x" * 400, "key_terms": ["term"] * 12, "period": "2022-2024"}
                    for r in R[R.status.isin(VALIDATED_STATUSES)].itertuples()} | {k: v for k, v in profiles.items()}
    print(f"[2/3] screening documents filed under {R.status.isin(SCREEN_STATUSES).sum()} unvalidated entries")
    results, prev_s = screen(model, R, D, profiles, out / "text", sdir, a.max_chars, a.workers, a.limit, a.dry_run)

    if a.dry_run:
        P = pd.DataFrame(prev_p + prev_s)
        sys_tokens = est_tokens(P[P.stage == "screen"].system_chars.max()) if (P.stage == "screen").any() else 0
        n_screen = int((P.stage == "screen").sum())
        print(f"profile calls: {int((P.stage == 'profile').sum())} (~{est_tokens(P[P.stage == 'profile'].chars.sum()):,} input tokens)")
        print(f"screening calls: {n_screen} (~{est_tokens(P[P.stage == 'screen'].chars.sum()):,} document tokens "
              f"+ catalog ~{sys_tokens:,} tokens, written to cache once and read {max(n_screen - 1, 0)} times)")
        print(f"requests written to {sdir / 'requests_preview.jsonl'}")
        return

    print("[3/3] verifying quotes and aggregating")
    E, B, M = aggregate(results, R)
    Pf = pd.DataFrame([{"pilot_id": k, "name": v["name"], "tcs": v["tcs"], "cities": v["cities"], "summary": v["summary"],
                        "key_terms": ", ".join(v["key_terms"]), "period": v["period"], "docs_used": v.get("n_docs_used", "")}
                       for k, v in sorted(profiles.items())])
    run = pd.DataFrame([{"item": k, "value": v} for k, v in {
        "date": time.strftime("%Y-%m-%d %H:%M"), "model": a.model + (" (MOCK - not a real run)" if a.mock else ""),
        "prompt_profile_sha": sha((PROMPTS / "profile_system.md").read_text(encoding="utf-8")),
        "prompt_screen_sha": sha((PROMPTS / "screen_system.md").read_text(encoding="utf-8")),
        "max_chars": a.max_chars, "documents_screened": len(results), "minutes": round((time.time() - t0) / 60, 1),
        **{f"api_{k}": v for k, v in model.usage.items()},
    }.items()])
    readme = pd.DataFrame({"README": [
        "screening_unreviewed.xlsx — stage 3: documents filed under unvalidated entries, screened against the validated pilots.",
        "Scope (team decision 2026-11-02): only validated pilots are evaluated; this screening only recovers their misfiled documents.",
        "ByEntity: one row per unvalidated entry with the proposal. Fill team_decision.",
        "ByDocument: the model's verdict per document with the verbatim evidence; [NOT FOUND IN TEXT] marks quotes that failed verification.",
        "Reassignments: proposed moves (primary/section matches only). Fill team_decision (accept / reject); the knowledge base (stage 4) applies the accepted ones.",
        "Profiles: the catalog of validated pilots the model compared against (built from each pilot's own documents).",
        "Run: model, prompt versions, token usage. Raw answers: screening/responses.jsonl.",
    ]})
    xlsx = out / "screening_unreviewed.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        readme.to_excel(w, sheet_name="README", index=False)
        E.to_excel(w, sheet_name="ByEntity", index=False)
        B.to_excel(w, sheet_name="ByDocument", index=False)
        M.to_excel(w, sheet_name="Reassignments", index=False)
        Pf.to_excel(w, sheet_name="Profiles", index=False)
        run.to_excel(w, sheet_name="Run", index=False)
    style(xlsx)
    M.to_csv(out / "document_reassignments.csv", index=False, encoding="utf-8-sig")
    print(f"done: {len(results)} documents, {int((E.documents_matched > 0).sum()) if len(E) else 0} entries with documents of validated pilots, "
          f"{len(M)} proposed moves -> {xlsx}")
    print("usage:", model.usage)


if __name__ == "__main__":
    main()
