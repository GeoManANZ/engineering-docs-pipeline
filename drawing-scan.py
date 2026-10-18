#!/usr/bin/env python3
"""
drawing-scan.py — classify PENDING corpus files as engineering drawings that
have no markdown value, so all lanes can skip them permanently.

Drawing test (geometric, no conversion):
    pages <= 4  AND  max page area >= ~A3 (842x1191 pt)  AND  chars/page < 1000
Path confirmation is recorded but not required (geometry is the signal).

Outputs:
    ~/drawings-skip.txt        absolute paths to skip
    ~/drawings-skip.jsonl      rel + stats, for appending to manifests
Run: python3 drawing-scan.py [--limit N]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CORPUS = Path("/mnt/c/Users/Chris/OneDrive/02. Work")
MF = Path.home() / "engineering-md/.fastpass.jsonl"
A3_AREA = 0.9 * 842 * 1191   # pt^2
MAX_PAGES = 4
MAX_CPP = 1000

sys.path.insert(0, str(Path.home() / "pdf-convert-venv/lib/python3.12/site-packages"))
import pymupdf  # noqa: E402


def pending_rels() -> set[str]:
    seen: dict[str, str] = {}
    for line in MF.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        rel, v, s = r.get("rel"), r.get("verdict"), str(r.get("status", ""))
        if rel is None:
            continue
        if v in ("digital", "thin") and s.startswith("ok"):
            seen[rel] = "done"
        elif v == "needs_ocr":
            seen[rel] = "done"   # classified terminal for stage 1
        else:
            seen[rel] = "pending"
    return {r for r, c in seen.items() if c == "pending"}


def probe(path: Path):
    try:
        doc = pymupdf.open(str(path))
    except Exception:
        return None
    try:
        try:
            if doc.is_encrypted and not doc.authenticate(""):
                return None
        except Exception:
            pass
        pages = len(doc)
        if pages == 0:
            return None
        area = 0.0
        chars = 0
        try:
            for page in doc:
                try:
                    r = page.rect
                    area = max(area, r.width * r.height)
                    chars += len(page.get_text("text") or "")
                except Exception:
                    pass
        except Exception:
            pass  # keep partial stats from the pages we got
        return pages, area, chars
    finally:
        try:
            doc.close()
        except Exception:
            pass


def main() -> None:
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    pend = sorted(r for r in pending_rels() if r.lower().endswith(".pdf"))
    print(f"pending pdfs: {len(pend):,}", flush=True)
    if limit:
        pend = pend[:limit]

    skips, checked = [], 0
    for n, rel in enumerate(pend, 1):
        p = CORPUS / rel
        try:
            if not p.is_file():
                continue
            size = p.stat().st_size
        except OSError:
            continue
        res = probe(p)
        checked += 1
        if res is None:
            continue
        pages, area, chars = res
        cpp = chars / max(pages, 1)
        if pages <= MAX_PAGES and area >= A3_AREA and cpp < MAX_CPP:
            skips.append({"rel": rel, "size": size, "pages": pages,
                          "area": int(area), "chars": chars})
        if n % 500 == 0:
            print(f"  {n}/{len(pend)} probed · {len(skips):,} drawings so far", flush=True)

    Path.home().joinpath("drawings-skip.txt").write_text(
        "\n".join(str(CORPUS / s["rel"]) for s in skips) + "\n", encoding="utf-8")
    Path.home().joinpath("drawings-skip.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in skips) + "\n", encoding="utf-8")
    gb = sum(s["size"] for s in skips) / 2**30
    print(f"\nDONE: checked {checked:,} pending pdfs")
    print(f"drawings to skip: {len(skips):,} files, {gb:.2f} GB")
    tops = {}
    for s in skips:
        top = "/".join(s["rel"].split("/")[:2])
        tops[top] = tops.get(top, 0) + 1
    for k, v in sorted(tops.items(), key=lambda x: -x[1])[:10]:
        print(f"  {v:5d}  {k}")


if __name__ == "__main__":
    main()
