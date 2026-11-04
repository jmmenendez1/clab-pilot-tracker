"""Stage 2 - Pilot disambiguation: team rules + automatic candidates -> pilot_registry.xlsx

Usage:
    python stage2_registry.py --repo clab_pilot_repository.xlsx --stage1 out --out out

Reads:
    Pilots sheet of the repository (Review / Notes columns are binding team decisions)
    out/documents.csv and out/text/ from stage 1
    data/team_decisions_overrides.csv (optional, --overrides) - manual corrections to the parsed decisions
Writes:
    out/team_decisions.csv          how every Review/Notes value was interpreted
    out/pilot_registry.xlsx         Registry / Candidates / Validation / Documents / Decisions / README
"""
import argparse
import csv
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import numpy as np
import openpyxl
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from rapidfuzz import fuzz, process
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

STOP = {"pilot", "piloto", "pilots", "pilotos", "project", "proyecto", "program", "programa", "the", "for", "of", "in",
        "de", "del", "la", "el", "en", "y", "and", "with", "con", "para", "a"}


def ascii_low(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def norm_name(s):
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]+", " ", ascii_low(s)).split() if w not in STOP)


# --------------------------------------------------------------------------------------------- repository
def load_entries(repo):
    ws = openpyxl.load_workbook(repo, data_only=True)["Pilots"]
    rows = []
    for i, r in enumerate(ws.iter_rows(min_row=4, values_only=True), start=4):
        if not r[0] or r[0] == "TOTAL":
            continue
        rows.append({
            "entry_id": f"E{i:03d}", "excel_row": i, "tc": r[0], "name": r[1], "cities": r[2] or "",
            "repo_files": r[3], "canonical_folder": r[4], "source_slugs": [s for s in str(r[5] or "").splitlines() if s],
            "review": str(r[6]).strip() if r[6] is not None else "", "notes": str(r[7]).strip() if r[7] is not None else "",
        })
    return rows


# --------------------------------------------------------------------------------------------- team rules
def resolve_target(text, entries, self_id):
    """Map a free-text reference ('Last-mile cargo', 'urban oasis in guatemala') to an entry."""
    t = norm_name(re.sub(r"(?i)\b(redundant|this is|is the same as|spin off)\b", " ", text))
    choices = {e["entry_id"]: norm_name(e["name"]) + " " + norm_name(e["cities"]) for e in entries if e["entry_id"] != self_id}
    best = process.extractOne(t, choices, scorer=fuzz.token_set_ratio)
    return (best[2], best[1]) if best else ("", 0)


def parse_decisions(entries, overrides_path):
    by_id = {e["entry_id"]: e for e in entries}
    dec = []
    for e in entries:
        rv, nt = e["review"], e["notes"]
        both = f"{rv} {nt}".strip()
        d = {"entry_id": e["entry_id"], "tc": e["tc"], "name": e["name"], "review": rv, "notes": nt,
             "decision": "", "target_entry": "", "target_name": "", "parent_entry": "", "parent_name": "",
             "metadata_fix": "", "match_score": "", "interpretation_source": "parser"}
        low = ascii_low(both)
        if rv == "✓":
            d["decision"] = "confirmed"
        elif rv == "?" or rv == "":
            d["decision"] = "unreviewed"
        if "never happened" in low:
            d["decision"] = "never_happened"
        elif "all bpr pilots together" in low:
            d["decision"] = "umbrella"
        elif low.startswith("not started"):
            d["decision"] = "not_started"
        elif "still ongoing" in low:
            d["decision"] = "ongoing"
        elif low.startswith("maybe"):
            d["decision"] = "doubtful"
        m = re.search(r"(?i)redundant[ ,(]*(?:this is)?\s*([^)]*)\)?|is the same as (.*)", both)
        if m:
            ref = (m.group(1) or m.group(2) or "").split(", spin off")[0]
            tid, sc = resolve_target(ref, entries, e["entry_id"])
            d.update(decision="merge_into", target_entry=tid, target_name=by_id[tid]["name"] if tid else "", match_score=round(sc))
        if "spin off" in low and d["decision"] != "merge_into":
            d["parent_ref"] = "tandil"
        if "city should be only" in low:
            d["metadata_fix"] = "cities=" + re.search(r"(?i)city should be only (.*)", nt).group(1).strip()
        dec.append(d)

    # Spin-offs: the parent is the entry the team called the original
    originals = [d for d in dec if "original" in ascii_low(d["notes"])]
    for d in dec:
        if d.pop("parent_ref", None) and originals:
            d["parent_entry"], d["parent_name"] = originals[0]["entry_id"], originals[0]["name"]
    # A merge into an entry that is itself merged (or a spin-off of an original) inherits the parent link
    for d in dec:
        if d["decision"] == "merge_into" and "spin off" in ascii_low(d["notes"]) and originals:
            tgt = next(x for x in dec if x["entry_id"] == d["target_entry"])
            if not tgt["parent_entry"]:
                tgt["parent_entry"], tgt["parent_name"] = originals[0]["entry_id"], originals[0]["name"]
                tgt["interpretation_source"] = "parser (spin-off inherited from merged row)"

    # Rows carrying the same status text and near-identical names are the same pilot
    groups = defaultdict(list)
    for d in dec:
        if d["decision"] in ("not_started",):
            groups[d["review"]].append(d)
    for g in groups.values():
        for d in g[1:]:
            if fuzz.token_set_ratio(norm_name(d["name"]), norm_name(g[0]["name"])) >= 80:
                d.update(target_entry=g[0]["entry_id"], target_name=g[0]["name"], decision="not_started_duplicate",
                         interpretation_source="same Review text and name as another row")

    if overrides_path and Path(overrides_path).exists():
        ov = pd.read_csv(overrides_path, dtype=str).fillna("")
        for _, o in ov.iterrows():
            d = next(x for x in dec if x["entry_id"] == o["entry_id"])
            for k in ("decision", "target_entry", "parent_entry", "metadata_fix"):
                if o.get(k, ""):
                    d[k] = o[k]
            d["target_name"] = by_id[d["target_entry"]]["name"] if d["target_entry"] else ""
            d["parent_name"] = by_id[d["parent_entry"]]["name"] if d["parent_entry"] else ""
            d["interpretation_source"] = "override: " + o.get("comment", "")
    return dec


# --------------------------------------------------------------------------------------------- documents
def map_folders(entries, docs):
    """Folder on disk -> entry. Canonical folder first; otherwise match the source slugs."""
    fmap = {}
    for (tc, folder) in docs[["tc", "folder"]].drop_duplicates().itertuples(index=False):
        cands = [e for e in entries if e["tc"] == tc]
        hit = [e for e in cands if e["canonical_folder"] == folder]
        how = "canonical folder"
        if not hit:
            hit = [e for e in cands if any(folder.startswith(s) or s.startswith(folder) for s in e["source_slugs"])]
            how = "source slug"
        if not hit:
            best = process.extractOne(folder.replace("_", " "), {e["entry_id"]: " ".join(e["source_slugs"]).replace("_", " ") + " " + norm_name(e["name"]) for e in cands}, scorer=fuzz.token_set_ratio)
            hit = [next(e for e in cands if e["entry_id"] == best[2])]
            how = f"fuzzy ({round(best[1])})"
        fmap[(tc, folder)] = (hit[0]["entry_id"], how)
    return fmap


ROLE_RULES = [
    ("invoice", r"\b(invoice|factura)\b"), ("tor", r"\b(tors?|terminos de referencia|t[eé]rminos de referencia|solicitud expresiones)\b"),
    ("affidavit", r"affidavit"), ("agreement", r"\b(acordo|convenio|acuerdo)\b"), ("work_plan", r"plan(o)? de trabajo|plano de trabalho|workplan|cronograma|plan trabajo"),
    ("final_report", r"(informe|relat[oó]rio|reporte|report)[ _-]*final|producto final|final report|informe 4 de 4"),
    ("progress_report", r"informe \d de \d|relat[oó]rio (mensal|de atividades)|reporte de avance|informe de progreso|\d+º relat"),
    ("webinar_or_media", r"webinar|episodio|guion"), ("presentation", r"\.pptx$|presenta"),
]


def doc_role(name):
    n = ascii_low(name)
    for role, pat in ROLE_RULES:
        if re.search(ascii_low(pat), n):
            return role
    return "deliverable"


PILOT_TERMS = re.compile(r"\b(piloto|pilot|prueba de concepto|prova de conceito|prototipo|prot[oó]tipo|proof of concept|implementa)", re.I)
RESULT_TERMS = re.compile(r"\b(resultados|results|lecciones aprendidas|lessons learned|li[cç][oõ]es aprendidas|evaluaci[oó]n|avalia[cç][aã]o|impacto|impact)\b", re.I)


# --------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--stage1", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--overrides", default=str(Path(__file__).parent / "data" / "team_decisions_overrides.csv"))
    ap.add_argument("--splits", default=str(Path(__file__).parent / "data" / "pilot_splits.csv"))
    a = ap.parse_args()
    s1, out = Path(a.stage1), Path(a.out)

    entries = load_entries(a.repo)
    by_id = {e["entry_id"]: e for e in entries}
    dec = parse_decisions(entries, a.overrides)
    dec_by = {d["entry_id"]: d for d in dec}

    docs = pd.read_csv(s1 / "documents.csv", dtype={"doc_id": str, "cluster_id": str})
    fmap = map_folders(entries, docs)
    docs["entry_id"] = [fmap[(t, f)][0] for t, f in zip(docs.tc, docs.folder)]
    docs["folder_mapping"] = [fmap[(t, f)][1] for t, f in zip(docs.tc, docs.folder)]
    docs["role"] = docs.original_name.map(doc_role)

    def text(doc_id):
        return " ".join(u["text"] for u in json.loads((s1 / "text" / f"{doc_id}.json").read_text())["units"])

    # One text per entry = its distinct documents (cluster representatives), administrative docs excluded
    entry_docs = defaultdict(set)
    for r in docs.itertuples():
        entry_docs[r.entry_id].add(r.cluster_id)
    admin = {"invoice", "affidavit", "agreement"}
    rep_role = dict(zip(docs[docs.is_representative].cluster_id, docs[docs.is_representative].role))
    cluster_text = {c: text(c) for c in docs[docs.is_representative].cluster_id}
    ids = [e["entry_id"] for e in entries]
    corpus = [" ".join(cluster_text[c] for c in entry_docs[i] if rep_role.get(c) not in admin) for i in ids]
    has_text = np.array([len(t) > 200 for t in corpus])
    X = TfidfVectorizer(max_df=0.5, min_df=2, ngram_range=(1, 2), sublinear_tf=True, max_features=200000).fit_transform(corpus)
    C = cosine_similarity(X)

    # ------------------------------------------------------------------ pairwise features over the 105 rows
    def geo_tokens(e):
        return {w for w in norm_name(e["cities"]).split() if w not in {"unknown", "multiple", "cities", "city"} and len(w) > 2}

    # Mentions: does one entry's text name the other's distinctive terms (from its name and source slugs)?
    # Distinctive = appears in at most 2 entries' names/slugs, not a place name, not generic.
    def name_tokens(e):
        toks = set(norm_name(e["name"]).split())
        for s in e["source_slugs"]:
            toks |= set(norm_name(s.replace("_", " ")).split())
        return {t for t in toks if len(t) > 3 and not t.isdigit()}
    place = set().union(*(set(norm_name(e["cities"]).split()) for e in entries)) | {"brazil", "colombia", "ecuador", "chile", "paraguay", "argentina", "peru", "mexico", "unknown", "ciudades", "cidades", "amazonicas"}
    tok_df = defaultdict(int)
    for e in entries:
        for t in name_tokens(e):
            tok_df[t] += 1
    low_text = {i: ascii_low(t) for i, t in zip(ids, corpus)}
    # ... and rare in the corpus too: a term that appears in many entries' documents ('evaluation', 'climate') is not a name
    text_df = defaultdict(int)
    for t in {t for e in entries for t in name_tokens(e)}:
        pat = re.compile(r"\b" + re.escape(t) + r"\b")
        text_df[t] = sum(1 for i in ids if pat.search(low_text[i]))
    distinct = {e["entry_id"]: {t for t in name_tokens(e) if tok_df[t] <= 2 and t not in place and text_df[t] <= 6} for e in entries}

    def mention(x, y):
        toks = distinct[y]
        if not toks or not low_text[x]:
            return 0.0
        return sum(1 for t in toks if re.search(r"\b" + re.escape(t) + r"\b", low_text[x])) / len(toks)

    pairs = []
    for i, ei in enumerate(ids):
        for j in range(i + 1, len(ids)):
            ej = ids[j]
            A, B = by_id[ei], by_id[ej]
            name_sim = fuzz.token_set_ratio(norm_name(A["name"]), norm_name(B["name"])) / 100
            content = float(C[i, j]) if has_text[i] and has_text[j] else np.nan
            shared = len(entry_docs[ei] & entry_docs[ej])
            ga, gb = geo_tokens(A), geo_tokens(B)
            geo = 1.0 if ga & gb else (0.0 if ga and gb else 0.5)
            ment = max(mention(ei, ej), mention(ej, ei))
            score = 0.25 * name_sim + 0.25 * (0 if np.isnan(content) else content) + 0.15 * geo + 0.35 * ment + (0.3 if shared else 0)
            pairs.append({"a": ei, "b": ej, "a_name": A["name"], "b_name": B["name"], "a_tc": A["tc"], "b_tc": B["tc"],
                          "a_cities": A["cities"], "b_cities": B["cities"], "name_sim": round(name_sim, 2),
                          "content_sim": None if np.isnan(content) else round(content, 2), "mention": round(ment, 2), "shared_docs": shared,
                          "same_place": geo == 1.0, "cross_tc": A["tc"] != B["tc"], "score": round(score, 3)})
    P = pd.DataFrame(pairs)

    # ------------------------------------------------------------------ validation against the team's known merges
    known = {frozenset((d["entry_id"], d["target_entry"])) for d in dec if d["decision"] in ("merge_into", "not_started_duplicate")}
    confirmed = {d["entry_id"] for d in dec if d["decision"] in ("confirmed", "ongoing")}
    lineage = {frozenset((d["entry_id"], d["parent_entry"])) for d in dec if d["parent_entry"]}
    P["known_merge"] = [frozenset((x, y)) in known for x, y in zip(P.a, P.b)]
    P["known_distinct"] = [(x in confirmed and y in confirmed and frozenset((x, y)) not in known) for x, y in zip(P.a, P.b)]
    P["known_lineage"] = [frozenset((x, y)) in lineage for x, y in zip(P.a, P.b)]

    val_rows = []
    for d in dec:
        if d["decision"] not in ("merge_into", "not_started_duplicate"):
            continue
        sub = P[(P.a == d["entry_id"]) | (P.b == d["entry_id"])].sort_values("score", ascending=False).reset_index(drop=True)
        rank = sub.index[(sub.a == d["target_entry"]) | (sub.b == d["target_entry"])][0] + 1
        row = sub.iloc[rank - 1]
        val_rows.append({"row": d["entry_id"], "name": d["name"], "team_target": d["target_name"], "rank_of_target": int(rank),
                         "score": row.score, "name_sim": row.name_sim, "content_sim": row.content_sim, "mention": row.mention, "shared_docs": row.shared_docs})
    V = pd.DataFrame(val_rows)
    thresholds = []
    for t in (0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
        flagged = P[P.score >= t]
        thresholds.append({"threshold": t, "known_merges_recovered": int(flagged.known_merge.sum()), "known_merges_total": len(known),
                           "confirmed_distinct_pairs_flagged": int(flagged.known_distinct.sum()),
                           "lineage_pairs_flagged": int(flagged.known_lineage.sum()), "pairs_flagged": len(flagged)})
    T = pd.DataFrame(thresholds)
    thr = 0.55  # "likely" threshold; see Validation sheet for the recall / false-positive trade-off

    # ------------------------------------------------------------------ entities after the team's rules
    survivor = {}
    for d in dec:
        survivor[d["entry_id"]] = d["target_entry"] if d["decision"] in ("merge_into", "not_started_duplicate") else d["entry_id"]
    for k in survivor:  # follow chains
        while survivor[survivor[k]] != survivor[k]:
            survivor[k] = survivor[survivor[k]]
    members = defaultdict(list)
    for k, v in survivor.items():
        members[v].append(k)

    status_map = {"confirmed": "confirmed", "unreviewed": "to_review", "never_happened": "never_happened", "umbrella": "umbrella_not_a_pilot",
                  "not_started": "not_started", "ongoing": "ongoing", "doubtful": "doubtful_files"}
    reg = []
    for sid, mem in members.items():
        d, e = dec_by[sid], by_id[sid]
        cities = e["cities"]
        if d["metadata_fix"].startswith("cities="):
            cities = d["metadata_fix"].split("=", 1)[1]
        tcs = sorted({by_id[m]["tc"] for m in mem})
        clusters = set().union(*(entry_docs[m] for m in mem))
        reps = docs[docs.cluster_id.isin(clusters) & docs.is_representative]
        n_docs = len(reps)
        n_substantive = int((~reps.role.isin(list(admin))).sum())
        txt = " ".join(cluster_text[c] for c in clusters if rep_role.get(c) not in admin)
        reg.append({
            "pilot_id": f"CLAB-P-{e['excel_row'] - 3:03d}", "survivor_entry": sid, "canonical_name": e["name"], "tc_primary": e["tc"],
            "tcs_all": "; ".join(tcs), "cross_tc": len(tcs) > 1, "cities": cities, "status": status_map.get(d["decision"], d["decision"]),
            "parent_pilot": "", "parent_entry": d["parent_entry"], "merged_rows": "; ".join(f"{m} {by_id[m]['name']} ({by_id[m]['tc']})" for m in mem if m != sid),
            "aliases": "; ".join(sorted({by_id[m]["name"] for m in mem if m != sid} | {s for m in mem for s in by_id[m]["source_slugs"]})),
            "n_documents": n_docs, "n_substantive_documents": n_substantive, "chars": len(txt),
            "pilot_term_hits": len(PILOT_TERMS.findall(txt)), "result_term_hits": len(RESULT_TERMS.findall(txt)),
            "document_roles": "; ".join(f"{k}:{v}" for k, v in reps.role.value_counts().items()),
            "team_review": d["review"], "team_notes": d["notes"], "metadata_fix": d["metadata_fix"],
            "auto_proposal": "", "auto_confidence": "", "auto_evidence": "", "reviewer": "", "final_decision": "",
        })
    R = pd.DataFrame(reg)
    pid = dict(zip(R.survivor_entry, R.pilot_id))
    R["parent_pilot"] = [pid.get(survivor.get(p, p), "") if p else "" for p in R.parent_entry]
    # Scope decision (Juan, 2026-11-02): the retrospective evaluation covers only the pilots the team validated (✓).
    # Unreviewed entries are screened (stage 3) only to recover documents that belong to validated pilots.
    R["in_retro_scope"] = R.status.eq("confirmed")

    # Splits (data/pilot_splits.csv): one repository entry that the team decided is really two pilots.
    # The new pilot copies the source's status and TCs; with documents = "all" it shares every document of the
    # source (documents that cover both pilots belong to both knowledge bases). pilot_id >= CLAB-P-201 marks
    # pilots that have no row of their own in the repository.
    R["split_from"] = ""
    splits = pd.read_csv(a.splits, dtype=str).fillna("") if Path(a.splits).exists() else pd.DataFrame()
    for sp in splits.itertuples():
        src_pid = pid[survivor[sp.source_entry]]
        src = R[R.pilot_id == src_pid].iloc[0].copy()
        if sp.source_cities:
            R.loc[R.pilot_id == src_pid, "cities"] = sp.source_cities
            R.loc[R.pilot_id == src_pid, "metadata_fix"] = (str(src.metadata_fix) + " | split: cities=" + sp.source_cities).strip(" |")
        new = src.copy()
        new.update({"pilot_id": sp.new_pilot_id, "survivor_entry": f"{sp.source_entry}#split", "canonical_name": sp.name,
                    "cities": sp.cities, "merged_rows": "", "aliases": "", "candidate_matches": "", "auto_proposal": "",
                    "team_review": "split by team", "team_notes": sp.comment, "metadata_fix": "", "split_from": src_pid})
        R = pd.concat([R, new.to_frame().T], ignore_index=True)

    # ------------------------------------------------------------------ candidates for the unreviewed entities
    status_by_entry = {}
    for _, r in R.iterrows():
        for m in [r.survivor_entry] + [x.split()[0] for x in r.merged_rows.split("; ") if x]:
            status_by_entry[m] = r.status
    # For every entity still to review: its 3 best partners (score >= POSSIBLE), plus any pair >= LIKELY.
    LIKELY, POSSIBLE = 0.55, 0.35
    review_entries = [k for k, v in status_by_entry.items() if v in ("to_review", "doubtful_files")]
    out_of_scope = {k for k, v in status_by_entry.items() if v in ("umbrella_not_a_pilot", "never_happened")}
    P2 = P[~P.known_merge & ~P.known_lineage & (P.a.map(survivor) != P.b.map(survivor)) & ~P.a.isin(out_of_scope) & ~P.b.isin(out_of_scope)]
    keep = set()
    for e_ in review_entries:
        sub = P2[((P2.a == e_) | (P2.b == e_)) & (P2.score >= POSSIBLE)].nlargest(3, "score")
        keep |= set(sub.index)
    keep |= set(P2[(P2.score >= LIKELY) & (P2.a.isin(review_entries) | P2.b.isin(review_entries))].index)
    cand = P2.loc[sorted(keep)].copy()
    cand["a_status"] = cand.a.map(status_by_entry)
    cand["b_status"] = cand.b.map(status_by_entry)
    cand["a_pilot"] = cand.a.map(lambda x: pid[survivor[x]])
    cand["b_pilot"] = cand.b.map(lambda x: pid[survivor[x]])
    cand["proposal"] = np.where((cand.score >= LIKELY) | (cand.shared_docs > 0), "likely same pilot", "possible - check")
    cand = cand.sort_values("score", ascending=False)
    cm = defaultdict(list)
    for r in cand.itertuples():
        cm[r.a_pilot].append(f"{r.b_pilot} {r.b_name} ({r.score:.2f})")
        cm[r.b_pilot].append(f"{r.a_pilot} {r.a_name} ({r.score:.2f})")
    R["candidate_matches"] = R.pilot_id.map(lambda p: "; ".join(dict.fromkeys(cm.get(p, []))))
    R["auto_proposal"] = [("posible duplicado: revisar Candidates" if any(float(x.rsplit("(", 1)[1][:-1]) >= LIKELY for x in cm.get(p, [])) else "")
                          if st in ("to_review", "doubtful_files") else "" for p, st in zip(R.pilot_id, R.status)]
    cand["team_decision"] = ""

    # Umbrella entries: which pilots their documents name (distinctive terms found in the umbrella's text)
    umb_rows = []
    for sid in R[R.status == "umbrella_not_a_pilot"].survivor_entry:
        for j in ids:
            if j == sid or not distinct[j]:
                continue
            hits = sorted(t for t in distinct[j] if re.search(r"\b" + re.escape(t) + r"\b", low_text[sid]))
            if hits:
                umb_rows.append({"umbrella": f"{sid} {by_id[sid]['name']} ({by_id[sid]['tc']})", "related_entry": j, "pilot_id": pid[survivor[j]],
                                 "name": by_id[j]["name"], "tc": by_id[j]["tc"], "terms_found": ", ".join(hits),
                                 "share_of_terms": round(len(hits) / len(distinct[j]), 2), "content_sim": round(float(C[ids.index(sid), ids.index(j)]), 2)})
    umb_rows.sort(key=lambda r: (r["umbrella"], -r["share_of_terms"]))
    U = pd.DataFrame(umb_rows)

    # ------------------------------------------------------------------ write
    D = pd.DataFrame(dec)
    D.to_csv(out / "team_decisions.csv", index=False, encoding="utf-8-sig")
    docs_out = docs.copy()
    docs_out["pilot_id"] = docs_out.entry_id.map(lambda x: pid[survivor[x]])
    for sp in splits.itertuples():
        if sp.documents.strip().lower() == "all":
            extra = docs_out[docs_out.pilot_id == pid[survivor[sp.source_entry]]].copy()
            extra["pilot_id"] = sp.new_pilot_id
            extra["folder_mapping"] = "shared via split of " + sp.source_entry
            docs_out = pd.concat([docs_out, extra], ignore_index=True)
    docs_out = docs_out[["pilot_id", "entry_id", "folder_mapping", "tc", "folder", "original_name", "role", "ext", "units", "chars", "ocr_pages",
                         "is_representative", "relation_to_representative", "cluster_id", "cluster_locations", "use_for_extraction", "rel_path", "doc_id"]]
    R = R.sort_values(["status", "tc_primary", "canonical_name"], key=lambda s: s.map({"to_review": 0, "doubtful_files": 1, "confirmed": 2, "ongoing": 3, "not_started": 4, "umbrella_not_a_pilot": 5, "never_happened": 6}).fillna(s) if s.name == "status" else s)

    xlsx = out / "pilot_registry.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        pd.DataFrame({"README": [
            "pilot_registry.xlsx - borrador generado por stage2_registry.py (etapa 2 del plan guía).",
            "Registry: una fila por entidad tras aplicar las marcas del equipo (Review/Notes). Columnas auto_* = propuesta automática; reviewer y final_decision las completa el equipo.",
            "Candidates: pares de posibles duplicados que involucran pilotos sin revisar ('?'). Completar team_decision (same / different / spin-off).",
            f"Validation: cómo puntúa el algoritmo las fusiones que el equipo ya decidió. Umbral operativo = {thr}.",
            "Umbrella: pilotos más parecidos en contenido a las entradas paraguas (para repartir sus documentos).",
            "Documents: cada archivo, su pilot_id, su rol y si se usa en la extracción (representante de su grupo de duplicados/versiones).",
            "Decisions: cómo se interpretó cada valor de Review/Notes. Para corregir, editar data/team_decisions_overrides.csv (entry_id, decision, target_entry, parent_entry, metadata_fix, comment) y volver a correr.",
            "pilot_id = CLAB-P-<fila del repositorio - 3> de la fila sobreviviente; es estable mientras no se reordene el repositorio.",
        ]}).to_excel(w, sheet_name="README", index=False)
        R.to_excel(w, sheet_name="Registry", index=False)
        cand.drop(columns=["known_merge", "known_distinct", "known_lineage"]).to_excel(w, sheet_name="Candidates", index=False)
        V.to_excel(w, sheet_name="Validation", index=False, startrow=0)
        T.to_excel(w, sheet_name="Validation", index=False, startrow=len(V) + 3)
        U.to_excel(w, sheet_name="Umbrella", index=False)
        docs_out.to_excel(w, sheet_name="Documents", index=False)
        D.to_excel(w, sheet_name="Decisions", index=False)
    wb = openpyxl.load_workbook(xlsx)
    for ws in wb.worksheets:
        ws.freeze_panes = "A2"
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F4E78")
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for col in ws.columns:
            width = min(60, max(10, max(len(str(c.value or "")) for c in col[:200]) * 0.9))
            ws.column_dimensions[get_column_letter(col[0].column)].width = width
    wb["README"].column_dimensions["A"].width = 160
    wb.save(xlsx)

    print(f"entities={len(R)} " + " ".join(f"{k}={v}" for k, v in R.status.value_counts().items()))
    print(f"threshold={thr} candidates={len(cand)} likely_same={int((cand.proposal == 'likely same pilot').sum())}")
    print(T.to_string(index=False))
    print(V.to_string(index=False))


if __name__ == "__main__":
    main()
