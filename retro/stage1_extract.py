"""Stage 1 - Full-text extraction, deduplication and version detection.

Usage:
    python stage1_extract.py --src <downloads_pilots> --repo <clab_pilot_repository.xlsx> --out <out_dir>

Outputs (in out_dir):
    text/<doc_id>.json        full text per page / slide / block
    documents.csv             one row per file with hash, pages, OCR, duplicates, version family
    reconciliation.csv        files on disk vs. the repository "Files" sheet
"""
import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz  # PyMuPDF
import openpyxl
from docx import Document
from pptx import Presentation

DOC_EXT = {".pdf", ".docx", ".pptx"}
OCR_MIN_CHARS = 40
DOCX_BLOCK_CHARS = 3000


def nfc(s):
    return unicodedata.normalize("NFC", s)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def strip_slug(name):
    """'big_data_para_cidades_..._recife__Relatório Final.pdf' -> ('Relatório Final.pdf', slug)."""
    if "__" in name:
        slug, rest = name.split("__", 1)
        if re.fullmatch(r"[a-z0-9_]+", slug):
            return rest, slug
    return name, ""


VERSION_TOKENS = re.compile(
    r"(\(\d+\)|\bv\d+(\.\d+)*\b|_v\d+|\bversao\s*\d+(\.\d+)*|\bversion\s*\d+|\br\d{2}\b|_r\d{2}\b|\bfinal\b|"
    r"\brevisad[oa]\b|\budpated\b|\bupdated\b|\baprovada\b|\d{1,2}(jan|fev|feb|mar|abr|apr|mai|may|jun|jul|ago|aug|set|sep|out|oct|nov|dez|dec)\d{4}|"
    r"\d{6,8}|\b\d{4}\b)",
    re.I,
)


def family_key(name):
    base = os.path.splitext(nfc(name).lower())[0]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    base = VERSION_TOKENS.sub(" ", base)
    base = re.sub(r"[^a-z0-9]+", " ", base)
    return " ".join(base.split())


def version_rank(name):
    n = nfc(name).lower()
    score = 0
    if "final" in n:
        score += 100
    m = re.findall(r"(?:v|versao|versão|version|r)\s*0?(\d+)", n)
    if m:
        score += max(int(x) for x in m)
    if re.search(r"\(\d+\)", n):
        score -= 1
    return score


def ocr_page(page):
    import pytesseract
    from PIL import Image
    pix = page.get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return pytesseract.image_to_string(img, lang="spa+por+eng")


def extract_pdf(path):
    pages, ocr = [], 0
    with fitz.open(path) as d:
        for i, p in enumerate(d, 1):
            t = p.get_text()
            did_ocr = False
            if len(t.strip()) < OCR_MIN_CHARS and p.get_images():
                try:
                    t = ocr_page(p)
                    ocr += 1
                    did_ocr = True
                except Exception as e:  # keep going; record the failure in the text
                    t = f"[OCR failed: {e}]"
            pages.append({"unit": "page", "n": i, "text": t, "ocr": did_ocr})
    return pages, ocr


def extract_docx(path):
    d = Document(path)
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for tbl in d.tables:
        for row in tbl.rows:
            cells = []
            for c in row.cells:
                if c.text.strip() and c.text.strip() not in cells:
                    cells.append(c.text.strip())
            if cells:
                parts.append(" | ".join(cells))
    blocks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) > DOCX_BLOCK_CHARS and cur:
            blocks.append(cur)
            cur = ""
        cur += p + "\n"
    if cur:
        blocks.append(cur)
    return [{"unit": "block", "n": i, "text": b} for i, b in enumerate(blocks, 1)], 0


def extract_pptx(path):
    prs = Presentation(path)
    out = []
    for i, s in enumerate(prs.slides, 1):
        txt = []
        for sh in s.shapes:
            if sh.has_text_frame:
                txt.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for r in sh.table.rows:
                    txt.append(" | ".join(c.text for c in r.cells))
        out.append({"unit": "slide", "n": i, "text": "\n".join(t for t in txt if t.strip())})
    return out, 0


def process(args):
    path, src, out_dir = args
    rel = Path(path).relative_to(src)
    parts = rel.parts
    tc, folder, fname = parts[0], parts[1] if len(parts) > 2 else "", nfc(parts[-1])
    ext = Path(fname).suffix.lower()
    h = sha256(path)
    doc_id = h[:12]
    rec = {
        "doc_id": doc_id, "sha256": h, "tc": tc, "folder": nfc(folder), "filename": fname,
        "original_name": strip_slug(fname)[0], "source_slug": strip_slug(fname)[1],
        "ext": ext, "bytes": os.path.getsize(path), "rel_path": nfc(str(rel)),
    }
    text_file = Path(out_dir) / "text" / f"{doc_id}.json"
    try:
        if text_file.exists():
            units = json.loads(text_file.read_text())["units"]
            ocr = sum(1 for u in units if u.get("ocr"))
        else:
            fn = {".pdf": extract_pdf, ".docx": extract_docx, ".pptx": extract_pptx}[ext]
            units, ocr = fn(path)
            text_file.write_text(json.dumps({"doc_id": doc_id, "units": units}, ensure_ascii=False))
        rec.update(units=len(units), unit_type=units[0]["unit"] if units else "",
                   chars=sum(len(u["text"]) for u in units), ocr_pages=ocr, error="")
    except Exception as e:
        rec.update(units=0, unit_type="", chars=0, ocr_pages=0, error=repr(e)[:200])
    return rec


def load_repository_files(repo_path):
    wb = openpyxl.load_workbook(repo_path, data_only=True)
    ws = wb["Files"]
    rows = []
    for r in ws.iter_rows(min_row=4, values_only=True):
        if not r[0]:
            continue
        canon = nfc(str(r[5] or "")).replace("\\", "/")
        rows.append({"tc": r[0], "pilot": r[1], "filename": nfc(r[2] or ""), "orig_folder": r[4],
                     "canonical_file": canon.split("/")[-1], "canonical_folder": canon.split("/")[-2] if "/" in canon else ""})
    return rows


def text_of(out_dir, doc_id):
    f = Path(out_dir) / "text" / f"{doc_id}.json"
    return " ".join(u["text"] for u in json.loads(f.read_text())["units"]) if f.exists() else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=os.cpu_count())
    a = ap.parse_args()
    src, out = Path(a.src).resolve(), Path(a.out)
    if os.name == "nt" and not str(src).startswith("\\\\?\\"):
        src = Path("\\\\?\\" + str(src))  # Windows MAX_PATH workaround (OneDrive paths are long)
    (out / "text").mkdir(parents=True, exist_ok=True)

    files = sorted(str(p) for p in src.rglob("*") if p.is_file() and p.suffix.lower() in DOC_EXT and len(p.relative_to(src).parts) >= 3)
    with ProcessPoolExecutor(a.workers) as ex:
        recs = list(ex.map(process, [(f, src, out) for f in files]))

    # Document clusters: one logical document = same bytes, OR same content (cosine >= DUP_SIM, e.g. docx vs pdf
    # export, renamed copy), OR same name family with very similar content (cosine >= VERSION_SIM: successive versions).
    # Dated monthly reports share a name family but not content (cosine ~0.5-0.75), so they stay separate.
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    DUP_SIM, VERSION_SIM = 0.95, 0.85
    parent = list(range(len(recs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    by_hash = defaultdict(list)
    for i, r in enumerate(recs):
        by_hash[r["sha256"]].append(i)
        r["family"] = family_key(r["original_name"])
    for idx in by_hash.values():
        for j in idx[1:]:
            union(idx[0], j)
    reps = [idx[0] for idx in by_hash.values() if recs[idx[0]]["chars"] > 500]
    X = TfidfVectorizer(max_df=0.9, ngram_range=(1, 2), sublinear_tf=True).fit_transform([text_of(out, recs[i]["doc_id"]) for i in reps])
    S = cosine_similarity(X)
    for a_, i in enumerate(reps):
        for b_ in range(a_ + 1, len(reps)):
            j = reps[b_]
            if S[a_, b_] >= DUP_SIM or (S[a_, b_] >= VERSION_SIM and recs[i]["family"] == recs[j]["family"]):
                union(i, j)

    clusters = defaultdict(list)
    for i in range(len(recs)):
        clusters[find(i)].append(i)
    for members in clusters.values():
        ranked = sorted(members, key=lambda i: (version_rank(recs[i]["original_name"]), recs[i]["chars"], -len(recs[i]["filename"])), reverse=True)
        best = recs[ranked[0]]
        locations = sorted({f'{recs[i]["tc"]}/{recs[i]["folder"]}' for i in members})
        for i in members:
            r = recs[i]
            r["cluster_id"] = best["doc_id"]
            r["cluster_size"] = len(members)
            r["cluster_locations"] = "; ".join(locations)
            r["is_representative"] = i == ranked[0]
            if i == ranked[0]:
                r["relation_to_representative"] = ""
            elif r["sha256"] == best["sha256"]:
                r["relation_to_representative"] = "exact_copy"
            elif r["family"] == best["family"]:
                r["relation_to_representative"] = "older_version"
            else:
                r["relation_to_representative"] = "same_content"

    for r in recs:
        r["use_for_extraction"] = bool(r["is_representative"] and r["chars"] > 0)

    cols = ["doc_id", "tc", "folder", "filename", "original_name", "source_slug", "ext", "bytes", "unit_type", "units",
            "chars", "ocr_pages", "error", "cluster_id", "cluster_size", "is_representative", "relation_to_representative",
            "cluster_locations", "use_for_extraction", "rel_path", "sha256"]
    with open(out / "documents.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(recs, key=lambda r: r["rel_path"]):
            w.writerow({c: r.get(c, "") for c in cols})

    # Reconciliation with the repository's Files sheet
    repo = load_repository_files(a.repo)
    disk = {(r["tc"], r["folder"], r["filename"]): r for r in recs}
    repo_keys = {(x["tc"], x["canonical_folder"], x["canonical_file"]): x for x in repo}
    rows = []
    for k, x in repo_keys.items():
        rows.append({"status": "in_both" if k in disk else "only_in_repository", "tc": k[0], "folder": k[1], "filename": k[2], "pilot": x["pilot"]})
    for k, r in disk.items():
        if k not in repo_keys:
            rows.append({"status": "only_on_disk", "tc": k[0], "folder": k[1], "filename": k[2], "pilot": ""})
    with open(out / "reconciliation.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["status", "tc", "folder", "filename", "pilot"])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: (x["status"], x["tc"], x["folder"], x["filename"])))

    n = len(recs)
    print(f"files={n} unique_bytes={len(by_hash)} documents={len(clusters)} for_extraction={sum(r['use_for_extraction'] for r in recs)} "
          f"ocr_pages={sum(r['ocr_pages'] for r in recs)} errors={sum(1 for r in recs if r['error'])}")


if __name__ == "__main__":
    main()
