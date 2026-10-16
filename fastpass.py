#!/usr/bin/env python3
"""
fastpass.py — Engineering docs: cheap extraction pass (NO GPU, NO DAEMON, NO OCR)
=================================================================================
Replaces the heavy `pipeline.py` for the portion of the corpus that does NOT
need OCR: digitally-generated PDFs and Word docs already carry a perfect text
layer. These cost ~0.01-0.1s/page here vs 2-5s/page through a GPU vision
model — and they are exactly what the old pipeline called "the easy files".

What this does
--------------
1. Walks the source tree once.
2. PROBES each PDF cheaply with raw PyMuPDF (no layout analysis) to measure
   how much real text it contains.
3. Writes .md (layout-aware) ONLY for docs that turn out to have text.
4. Records everything it saw, so the leftovers are your exact OCR residue list.

Why this is safe
----------------
- Single process, one file at a time -> bounded memory, cannot OOM the laptop.
- No daemons to die at hour 3.
- Append-only JSONL manifest, fsynced per file -> a crash loses nothing.
- Never touches, moves, or deletes your originals.

Verdict bands (characters per page)
-----------------------------------
    digital   >= 150 cpp   real text layer -> .md written
    thin      50-149 cpp   sparse text (drawing sets, part-scanned compilations)
                           -> .md written, flagged for eyeballing, NOT OCR'd
    needs_ocr  < 50 cpp    image-only -> goes on the OCR residue list
    error                  unreadable / encrypted / corrupt

Usage
-----
    python3 fastpass.py --dry-run          # measure only, write no .md
    python3 fastpass.py                    # full run (re-run to resume)
    python3 fastpass.py --report           # show the split
    python3 fastpass.py --report --residue residue.txt   # + OCR to-do list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────

_DEFAULT_ROOT_CANDIDATES = [
    Path.home() / "work-docs",
    Path("/mnt/c/Users/Chris/OneDrive/02. Work"),
]
DEFAULT_OUT = Path.home() / "engineering-md"

DIGITAL_CPP = 150   # >= this many chars/page -> real text layer
THIN_CPP = 50       # >= this, below DIGITAL_CPP -> sparse but not empty
DOCX_MIN_CHARS = 200

PDF_EXT = {".pdf"}
DOC_EXT = {".docx", ".doc"}
ALL_EXT = PDF_EXT | DOC_EXT


def find_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for c in _DEFAULT_ROOT_CANDIDATES:
        if c.exists():
            return c
    return _DEFAULT_ROOT_CANDIDATES[-1]


def _pymupdf():
    """Return the pymupdf module (module name moved from `fitz` to `pymupdf`)."""
    try:
        import pymupdf  # PyMuPDF >= 1.24
        return pymupdf
    except ImportError:
        try:
            import fitz  # legacy name
            return fitz
        except ImportError:
            return None


# ── Manifest (append-only JSONL — crash-safe, resumable) ─────────────────────

def load_seen(manifest: Path) -> dict[str, dict]:
    """Read the manifest back. Later records win (allows re-running a file)."""
    seen: dict[str, dict] = {}
    if not manifest.exists():
        return seen
    with manifest.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                seen[rec["rel"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue  # tolerate a torn final line from a hard crash
    return seen


def append_record(manifest: Path, rec: dict) -> None:
    with manifest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ── Extraction ───────────────────────────────────────────────────────────────

def probe_pdf(path: Path) -> tuple[int, str, str]:
    """
    CHEAP pass: open the PDF and pull the raw text layer. No layout analysis,
    no models, ~0.01s/page. This is the honest measurement of the text layer.

    Returns (pages, raw_text, status).
    """
    pymupdf = _pymupdf()
    if pymupdf is None:
        return (0, "", "error: pymupdf not installed")

    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        return (0, "", f"error: cannot open ({type(e).__name__}: {e})")

    try:
        try:
            if doc.is_encrypted and not doc.authenticate(""):
                return (len(doc), "", "error: encrypted")
        except Exception:
            pass
        npages = len(doc)
        if npages == 0:
            return (0, "", "error: zero pages")

        chunks: list[str] = []
        try:
            for page in doc:
                chunks.append(page.get_text("text") or "")
        except Exception as e:
            return (npages, "\n".join(chunks), f"error: extract failed ({type(e).__name__})")
        return (npages, "\n\f\n".join(chunks), "ok")
    finally:
        try:
            doc.close()
        except Exception:
            pass


def pdf_to_markdown(path: Path, fallback_text: str) -> tuple[str, str]:
    """
    EXPENSIVE pass: layout-aware markdown. Only called for docs already known
    to have a text layer, so the cost is never paid on image-only scans.
    Falls back to the plain text we already extracted.
    """
    try:
        import pymupdf4llm  # type: ignore
        md = pymupdf4llm.to_markdown(str(path), show_progress=False)
        if md and md.strip():
            return (md, "ok")
    except Exception:
        pass
    return (fallback_text, "plaintext")


def extract_docx(path: Path) -> tuple[int, str, str]:
    """Word docs always carry a text layer — never needs OCR."""
    try:
        from markitdown import MarkItDown
        content = MarkItDown().convert(str(path)).text_content or ""
        return (0, content, "ok")
    except ImportError:
        pass
    except Exception as e:
        return (0, "", f"error: markitdown ({type(e).__name__}: {e})")

    try:
        import docx  # python-docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return (0, "\n\n".join(parts), "ok")
    except Exception as e:
        return (0, "", f"error: docx ({type(e).__name__}: {e})")


# ── Walk ─────────────────────────────────────────────────────────────────────

def walk_sources(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".staging", "_images")]
        for fn in sorted(filenames):
            if Path(fn).suffix.lower() in ALL_EXT:
                yield Path(dirpath) / fn


# ── Main pass ────────────────────────────────────────────────────────────────

def run(walk_roots: list[Path], root_files: list[Path], rel_root: Path, out: Path,
        dry_run: bool, limit: int | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / ".fastpass.jsonl"
    seen = load_seen(manifest)

    files: list[Path] = []
    for wr in walk_roots:
        files.extend(walk_sources(wr))
    for rf in root_files:  # immediate children only — no subdirectories
        files.extend(p for p in sorted(rf.iterdir())
                     if p.is_file() and p.suffix.lower() in ALL_EXT)
    if limit:
        files = files[:limit]
    total = len(files)

    print(f"Corpus : {rel_root}")
    if len(walk_roots) > 1:
        print(f"Walk   : {len(walk_roots)} subtrees")
    print(f"Output : {out}")
    print(f"Found  : {total:,} documents")
    print(f"Already: {len(seen):,} in manifest")
    print("-" * 64)

    t0 = time.time()
    counts = {"digital": 0, "thin": 0, "needs_ocr": 0, "error": 0}
    done = skipped = 0

    for i, path in enumerate(files, 1):
        try:
            rel = str(path.relative_to(rel_root))
        except ValueError:
            rel = str(path)

        prev = seen.get(rel)
        # Skip anything already classified with a clean status. Status may be
        # "ok", "ok (ok)", "ok (plaintext)" or "ok (docx)" depending on which
        # writer produced it — match on the prefix, never equality. Errors are
        # deliberately NOT skipped so re-runs retry them.
        if (prev
                and prev.get("verdict") in ("digital", "thin", "needs_ocr")
                and str(prev.get("status", "")).startswith("ok")):
            skipped += 1
            continue

        try:
            size = path.stat().st_size
        except OSError as e:
            append_record(manifest, {"rel": rel, "status": "error", "verdict": "error",
                                     "err": f"stat: {e}"})
            counts["error"] += 1
            continue

        ext = path.suffix.lower()
        tb = time.time()

        if ext in PDF_EXT:
            pages, text, status = probe_pdf(path)
            chars = len(text.strip()) if text else 0
            cpp = (chars / pages) if pages else 0.0
            if status != "ok":
                verdict = "error"
            elif cpp >= DIGITAL_CPP:
                verdict = "digital"
            elif cpp >= THIN_CPP:
                verdict = "thin"
            else:
                verdict = "needs_ocr"
        else:
            pages, text, status = extract_docx(path)
            chars = len(text.strip()) if text else 0
            cpp = None  # type: ignore[assignment]
            verdict = "error" if status != "ok" else (
                "digital" if chars >= DOCX_MIN_CHARS else "thin")

        # Only pay for layout-aware markdown when there IS a text layer.
        if verdict in ("digital", "thin") and not dry_run:
            md, how = pdf_to_markdown(path, text) if ext in PDF_EXT else (text, "docx")
            if how == "plaintext":
                verdict = "thin"
            md_path = out / Path(rel).with_suffix(".md")
            try:
                md_path.parent.mkdir(parents=True, exist_ok=True)
                md_path.write_text(md, encoding="utf-8")
                status = f"ok ({how})"
            except OSError as e:
                status = f"error: write failed ({e})"
                verdict = "error"

        elapsed = time.time() - tb
        counts[verdict] = counts.get(verdict, 0) + 1

        rec = {
            "rel": rel, "status": status, "verdict": verdict, "ext": ext,
            "size": size, "pages": pages, "chars": chars,
            "cpp": round(cpp, 1) if cpp is not None else None,
            "secs": round(elapsed, 2),
        }
        append_record(manifest, rec)
        seen[rel] = rec
        done += 1

        if done % 20 == 0 or done == 1:
            rate = done / max(time.time() - t0, 0.001)
            print(f"[{i}/{total}] {counts['digital']}d {counts['thin']}t "
                  f"{counts['needs_ocr']}ocr {counts['error']}err · {rate:.1f} f/s · "
                  f"{rel[-52:]}", flush=True)

    elapsed = time.time() - t0
    print("-" * 64)
    print(f"Finished in {elapsed/60:.1f} min")
    print(f"  digital   : {counts['digital']:,}")
    print(f"  thin      : {counts['thin']:,}")
    print(f"  needs OCR : {counts['needs_ocr']:,}")
    print(f"  errors    : {counts['error']:,}")
    print(f"  skipped   : {skipped:,}")
    print(f"\nManifest: {manifest}")
    print("Next: --report (and --residue FILE for the OCR to-do list)")


# ── Report ───────────────────────────────────────────────────────────────────

def report(root: Path, out: Path, residue_path: str | None) -> None:
    manifest = out / ".fastpass.jsonl"
    seen = load_seen(manifest)
    if not seen:
        print(f"No manifest at {manifest}. Run the pass first.")
        return

    buckets: dict[str, dict] = {}
    for rec in seen.values():
        v = rec.get("verdict", "unknown")
        b = buckets.setdefault(v, {"n": 0, "bytes": 0, "pages": 0})
        b["n"] += 1
        b["bytes"] += rec.get("size") or 0
        b["pages"] += rec.get("pages") or 0

    total = sum(b["n"] for b in buckets.values()) or 1

    print("=" * 64)
    print("ENGINEERING DOCS — FASTPASS REPORT")
    print("=" * 64)
    for name, blurb in (
        ("digital", "text layer -> .md written"),
        ("thin", "sparse text -> .md written, check these"),
        ("needs_ocr", "image-only -> OCR required"),
        ("error", "unreadable / encrypted"),
    ):
        b = buckets.get(name)
        if not b:
            continue
        print(f"  {name:10s}: {b['n']:>7,} ({b['n']/total*100:5.1f}%)  "
              f"{b['bytes']/2**30:8,.2f} GB   {blurb}")
    print(f"  {'TOTAL':10s}: {total:>7,}")

    digital = buckets.get("digital", {}).get("n", 0)
    thin = buckets.get("thin", {}).get("n", 0)
    ocr = buckets.get("needs_ocr", {}).get("n", 0)
    ocr_bytes = buckets.get("needs_ocr", {}).get("bytes", 0)
    ocr_pages = buckets.get("needs_ocr", {}).get("pages", 0)

    print("-" * 64)
    print(f"  Handled with zero GPU time : {digital + thin:,} "
          f"({(digital+thin)/total*100:.1f}%)")
    print(f"  Genuine OCR workload       : {ocr:,} files, "
          f"{ocr_pages:,} pages, {ocr_bytes/2**30:,.2f} GB")
    if ocr_pages:
        for sp, label in ((2, "at 2s/page"), (5, "at 5s/page")):
            print(f"      {label:12s}: {ocr_pages*sp/3600:,.1f} hours")
    print("=" * 64)

    if residue_path:
        rows = sorted((r for r in seen.values() if r.get("verdict") == "needs_ocr"),
                      key=lambda r: r.get("size") or 0, reverse=True)
        with open(residue_path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(str(Path(root) / r["rel"]) + "\n")
        print(f"\n  OCR to-do list -> {residue_path} ({len(rows):,} files)")

    errs = [r for r in seen.values() if r.get("verdict") == "error"]
    if errs:
        print(f"\n  First 15 errors:")
        for r in errs[:15]:
            print(f"    {r['rel'][-58:]}  ->  {r.get('status')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Cheap PDF/DOCX -> Markdown fast pass")
    ap.add_argument("--root", help="source tree (default: ~/work-docs or the OneDrive path)")
    ap.add_argument("--out", help="output dir (default: ~/engineering-md)")
    ap.add_argument("--dry-run", action="store_true", help="measure only, write no .md")
    ap.add_argument("--report", action="store_true", help="show the split from the manifest")
    ap.add_argument("--residue", metavar="FILE", help="write the OCR to-do list to FILE")
    ap.add_argument("--limit", type=int, help="only process the first N files")
    ap.add_argument("--subtree", action="append", metavar="DIR",
                    help="walk only this subtree (repeatable); rel paths and "
                         "the output layout stay relative to the corpus root")
    ap.add_argument("--root-files", action="append", metavar="DIR",
                    help="process only files directly in DIR, not its "
                         "subdirectories (repeatable)")
    args = ap.parse_args()

    root = find_root(args.root)
    out = Path(args.out) if args.out else DEFAULT_OUT

    if args.report:
        report(root, out, args.residue)
        return

    if not root.exists():
        print(f"Source path does not exist: {root}", file=sys.stderr)
        print("Pass --root /path/to/docs to point at it.", file=sys.stderr)
        sys.exit(1)

    walk_roots = [Path(s) for s in (args.subtree or [])]
    root_files = [Path(s) for s in (args.root_files or [])]
    for st in walk_roots + root_files:
        if not st.exists():
            print(f"Subtree does not exist: {st}", file=sys.stderr)
            sys.exit(1)
    if not walk_roots and not root_files:  # plain --root: walk the whole corpus
        walk_roots = [root]
    run(walk_roots, root_files, root, out, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
