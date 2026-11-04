"""Stage 4 - Knowledge base per validated pilot.

Why
    The framework extraction (stage 5) reads one pilot at a time. Each pilot therefore needs one place with all of
    its evidence: its own documents (deduplicated, current versions) plus the sections of multi-pilot documents that
    stage 3 recovered from unvalidated entries (consultant progress reports, programme reports).

What goes in
    - Pilots: the registry's validated pilots in retrospective scope (`in_retro_scope`, i.e. status confirmed).
    - Own documents: every document cluster filed under the pilot (stage 2 Documents sheet), read through its
      representative (stage 1). Includes documents shared through a split (e.g. Renca / Providencia). Invoices,
      affidavits and agreements are kept, at the end: invoices carry cost figures for L2.1.
    - Recovered sections: rows of out/document_reassignments.csv pointing to the pilot, unless the team marked
      team_decision = "reject" (team decision 2026-11-03: accepted by default). Only the relevant part of each
      multi-pilot document is kept (see "Section excerpts").

Section excerpts
    A multi-pilot report has a section on this pilot and others on other pilots; sending it whole would mix evidence.
    Kept units (pages / slides / blocks):
        1. the unit cited by the screening evidence;
        2. units that contain one of the pilot's distinctive terms (from its stage-3 profile and its city), where a
           term is distinctive if it appears in no other validated pilot's profile (shared names such as consultants,
           TC codes or "Cities LAB" are dropped because they appear across pilots);
        3. the neighbouring unit on each side of those, because sections often span a page break.
    At most --section-units units per document, preferring the units with most term hits.

Usage (from retro/, after stage 3)
    python stage4_knowledge_base.py --out out

Writes
    out/kb/<pilot_id>/manifest.json   pilot metadata + one entry per document (source, role, units kept, evidence)
    out/kb/<pilot_id>/corpus.md        the text stage 5 reads: own documents in full, then recovered sections,
                                       each unit headed by [doc_id · unit n] so evidence can be cited
    out/kb/kb_summary.csv              one row per pilot: documents, characters, estimated tokens, context flag
"""
import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pandas as pd

ROLE_ORDER = ["final_report", "deliverable", "progress_report", "presentation", "work_plan", "tor", "webinar_or_media",
              "agreement", "affidavit", "invoice"]
CHARS_PER_TOKEN = 3.3        # measured on stage-3 calls (Spanish/Portuguese text, Sonnet 5.5 tokenizer)
CONTEXT_BUDGET_TOKENS = 150_000  # above this, stage 5 must retrieve chunks instead of sending the whole corpus


def fold(s):
    return unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()


def load_units(text_dir, doc_id):
    return json.loads((text_dir / f"{doc_id}.json").read_text(encoding="utf-8"))["units"]


def parse_location(loc):
    m = re.search(r"(page|slide|block)\s*(\d+)", str(loc), re.I)
    return int(m.group(2)) if m else None


def distinctive_terms(profiles, registry):
    """Per pilot: profile key terms + cities that no other validated pilot shares."""
    raw = {}
    for pid, p in profiles.items():
        terms = {t.strip() for t in p.get("key_terms", []) if t and len(t.strip()) >= 5}
        cities = registry.get(pid, {}).get("cities", "")
        terms |= {c.strip() for c in re.split(r"[,;()]", str(cities)) if len(c.strip()) >= 5}
        raw[pid] = {t for t in terms if not re.fullmatch(r"[A-Z]{2}-T\d{4}.*", t)}
    counts = Counter(fold(t) for ts in raw.values() for t in {fold(x) for x in ts})
    return {pid: sorted({t for t in ts if counts[fold(t)] == 1}, key=len, reverse=True) for pid, ts in raw.items()}


def section_units(units, terms, evidence_ns, max_units):
    folded_terms = [fold(t) for t in terms]
    hits = {}
    for u in units:
        txt = fold(u["text"])
        n = sum(1 for t in folded_terms if t in txt)
        if n:
            hits[u["n"]] = n
    for n in evidence_ns:
        hits[n] = hits.get(n, 0) + 100            # the cited unit always survives the cap
    core = sorted(hits, key=lambda n: -hits[n])[:max_units]
    keep = set(core)
    valid = {u["n"] for u in units}
    for n in core:
        keep |= {n - 1, n + 1} & valid
    return sorted(keep)[: max_units * 2], hits


def render(doc_id, units, keep=None):
    out = []
    for u in units:
        if keep is not None and u["n"] not in keep:
            continue
        if u["text"].strip():
            out.append(f"[{doc_id} · {u['unit']} {u['n']}]\n{u['text'].strip()}")
    return "\n\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--section-units", type=int, default=8, help="max cited/term-matching units kept per recovered document (neighbours added)")
    a = ap.parse_args()
    out = Path(a.out)
    text_dir = out / "text"

    R = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Registry")
    D = pd.read_excel(out / "pilot_registry.xlsx", sheet_name="Documents", dtype={"doc_id": str, "cluster_id": str})
    profiles = json.loads((out / "screening" / "profiles.json").read_text(encoding="utf-8"))
    moves_path = out / "document_reassignments.csv"
    M = pd.read_csv(moves_path, dtype=str).fillna("") if moves_path.exists() else pd.DataFrame(columns=["to_pilot"])
    rejected = M[M.team_decision.str.strip().str.lower().isin(["reject", "rejected", "no"])] if len(M) else M
    M = M.drop(rejected.index) if len(M) else M

    scope = R[R.in_retro_scope == True]  # noqa: E712  (Excel round-trip keeps booleans)
    reg = {r.pilot_id: r._asdict() for r in R.itertuples()}
    terms = distinctive_terms(profiles, reg)
    rep = D[D.is_representative].drop_duplicates("cluster_id").set_index("cluster_id")

    kb_root = out / "kb"
    kb_root.mkdir(exist_ok=True)
    summary = []
    for p in scope.itertuples():
        pid = p.pilot_id
        own = D[D.pilot_id == pid]
        entries, parts = [], []

        # --- own documents
        own_clusters = list(dict.fromkeys(own.cluster_id))
        own_rows = []
        for c in own_clusters:
            r = rep.loc[c]
            via = own[own.cluster_id == c].folder_mapping.iloc[0]
            own_rows.append((ROLE_ORDER.index(r.role) if r.role in ROLE_ORDER else 1.5, c, r, via))
        for _, c, r, via in sorted(own_rows, key=lambda x: (x[0], -x[2].chars)):
            units = load_units(text_dir, c)
            body = render(c, units)
            parts.append(f"## Document {c} — {r.original_name}\nSource: own ({'shared via split' if str(via).startswith('shared') else 'filed under this pilot'}) · role: {r.role} · {len(units)} {units[0]['unit'] if units else 'unit'}s\n\n{body}")
            entries.append({"doc_id": c, "file": r.original_name, "rel_path": r.rel_path, "role": r.role, "source": "own",
                            "shared_via_split": str(via).startswith("shared"), "units_total": len(units), "units_kept": "all",
                            "chars": len(body)})

        # --- recovered sections (stage 3), accepted by default
        mv = M[M.to_pilot == pid] if len(M) else M
        for c, g in (mv.groupby("cluster_id") if len(mv) else []):
            if c in own_clusters:
                continue
            r = rep.loc[c]
            units = load_units(text_dir, c)
            ev_ns = [n for n in (parse_location(x) for x in g.evidence_location) if n]
            keep, hits = section_units(units, terms.get(pid, []), ev_ns, a.section_units)
            body = render(c, units, set(keep))
            parts.append(f"## Section of document {c} — {r.original_name}\nSource: recovered by screening from "
                         f"{'; '.join(sorted(set(g.from_name)))} · role: {r.role} · units kept: {', '.join(map(str, keep))} of {len(units)}\n"
                         f"Screening evidence: " + " | ".join(f"{l}: \"{q}\"" for l, q in zip(g.evidence_location, g.evidence_quote)) + f"\n\n{body}")
            entries.append({"doc_id": c, "file": r.original_name, "rel_path": r.rel_path, "role": r.role, "source": "recovered_section",
                            "recovered_from": sorted(set(g.from_entry)), "units_total": len(units), "units_kept": keep,
                            "term_hits": {str(k): v for k, v in hits.items() if v < 100},
                            "evidence": [{"location": l, "quote": q, "confidence": cf} for l, q, cf in zip(g.evidence_location, g.evidence_quote, g.confidence)],
                            "chars": len(body)})

        header = (f"# {pid} — {p.canonical_name}\n"
                  f"Technical cooperation(s): {p.tcs_all} · Cities: {p.cities} · Status: {p.status}"
                  + (f" · Spin-off of {p.parent_pilot}" if isinstance(p.parent_pilot, str) and p.parent_pilot else "")
                  + (f" · Split from {p.split_from}" if isinstance(getattr(p, 'split_from', ''), str) and getattr(p, 'split_from', '') else "") + "\n"
                  + (f"Other names: {p.merged_rows}\n" if isinstance(p.merged_rows, str) and p.merged_rows else "")
                  + f"Profile (stage 3): {profiles.get(pid, {}).get('summary', '(none)')}\n")
        corpus = header + "\n\n" + "\n\n".join(parts) + "\n"
        d = kb_root / pid
        d.mkdir(exist_ok=True)
        (d / "corpus.md").write_text(corpus, encoding="utf-8")
        manifest = {"pilot_id": pid, "name": p.canonical_name, "tcs": p.tcs_all, "cities": p.cities, "status": p.status,
                    "parent_pilot": p.parent_pilot if isinstance(p.parent_pilot, str) else "",
                    "split_from": getattr(p, "split_from", "") if isinstance(getattr(p, "split_from", ""), str) else "",
                    "aliases": p.aliases if isinstance(p.aliases, str) else "", "distinctive_terms": terms.get(pid, []),
                    "documents": entries}
        (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tokens = int(len(corpus) / CHARS_PER_TOKEN)
        summary.append({"pilot_id": pid, "name": p.canonical_name, "own_documents": sum(e["source"] == "own" for e in entries),
                        "recovered_sections": sum(e["source"] == "recovered_section" for e in entries), "chars": len(corpus),
                        "est_tokens": tokens, "fits_context": tokens <= CONTEXT_BUDGET_TOKENS})

    S = pd.DataFrame(summary).sort_values("est_tokens", ascending=False)
    S.to_csv(kb_root / "kb_summary.csv", index=False, encoding="utf-8-sig")
    print(f"knowledge bases: {len(S)} pilots -> {kb_root}")
    print(f"own documents: {S.own_documents.sum()} · recovered sections: {S.recovered_sections.sum()} "
          f"(rejected by team: {len(rejected)}) · over {CONTEXT_BUDGET_TOKENS:,} tokens: {int((~S.fits_context).sum())}")
    print(S.to_string(index=False))


if __name__ == "__main__":
    main()
