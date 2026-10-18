#!/usr/bin/env python3
"""
ocrpass.py — Stage 2: GPU OCR of the fastpass residue (scans + thin-fast).
Per DEPLOYMENT.md: chunked MinerU runs, one process, crash-safe manifest.

Flow per file:
  1. skip if already ok in ~/.ocrpass.jsonl (append-only, fsync per record)
  2. stage a collision-safe copy to ~/ocr-stage/<key>__<basename>
  3. `mineru -p <staged> -o <chunk-out> -b pipeline` (one invocation per chunk)
  4. move the resulting .md to ~/engineering-md/<rel>.md (replaces thin-fast
     output or creates new for needs_ocr scans), keep images under
     ~/engineering-md/_ocr_images/<key>/
  5. record + fsync, clean staging

Usage:
  python3 ocrpass.py                    # full run over ~/residue.txt
  python3 ocrpass.py --queue FILE       # alternate queue
  python3 ocrpass.py --limit N          # only first N pending
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

CORPUS = Path("/mnt/c/Users/Chris/OneDrive/02. Work")
OUT = Path.home() / "engineering-md"
STAGE = Path.home() / "ocr-stage"
MANIFEST = OUT / ".ocrpass.jsonl"
MINERU = Path.home() / "mineru-venv/bin/mineru"
CHUNK = 100  # files per mineru invocation (large books -> keep modest)


def load_seen() -> dict[str, dict]:
    seen: dict[str, dict] = {}
    if MANIFEST.exists():
        for line in MANIFEST.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                seen[rec["rel"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def append(rec: dict) -> None:
    with MANIFEST.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def key_for(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()[:10]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(Path.home() / "residue.txt"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    queue = [l.strip() for l in open(args.queue, encoding="utf-8") if l.strip()]
    seen = load_seen()
    pending = []
    for abspath in queue:
        try:
            rel = str(Path(abspath).relative_to(CORPUS))
        except ValueError:
            continue
        prev = seen.get(rel)
        if prev and str(prev.get("status", "")).startswith("ok"):
            continue
        if prev and prev.get("attempts", 0) >= 2:
            continue  # two strikes: parked
        pending.append((rel, abspath))
    if args.limit:
        pending = pending[:args.limit]

    print(f"queue: {len(queue)} | done: {len(seen)} | pending: {len(pending)}")
    if args.dry_run or not pending:
        return

    t0 = time.time()
    done = 0
    for i in range(0, len(pending), CHUNK):
        batch = pending[i:i + CHUNK]
        STAGE.mkdir(parents=True, exist_ok=True)
        staged: list[tuple[str, Path]] = []
        for rel, abspath in batch:
            src = Path(abspath)
            if not src.exists():
                append({"rel": rel, "status": "error: source missing",
                        "attempts": (seen.get(rel, {}).get("attempts", 0) + 1)})
                continue
            key = key_for(rel)
            dst = STAGE / f"{key}__{src.name}"
            try:
                shutil.copy2(src, dst)
                staged.append((rel, dst))
            except OSError as e:
                append({"rel": rel, "status": f"error: stage ({e})",
                        "attempts": (seen.get(rel, {}).get("attempts", 0) + 1)})

        if not staged:
            continue
        chunk_out = STAGE / "_out"
        cmd = [str(MINERU), "-p", str(STAGE), "-o", str(chunk_out), "-b", "pipeline"]
        print(f"[{i + len(batch)}/{len(pending)}] mineru on {len(staged)} files "
              f"({time.time() - t0:.0f}s elapsed)", flush=True)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600 * 3)
            if r.returncode != 0:
                tail = (r.stderr or r.stdout or "")[-300:]
                for rel, _ in staged:
                    append({"rel": rel, "status": f"error: mineru rc={r.returncode}",
                            "attempts": (seen.get(rel, {}).get("attempts", 0) + 1)})
                print(f"  mineru FAILED rc={r.returncode}: {tail}", flush=True)
                for _, dst in staged:
                    dst.unlink(missing_ok=True)
                continue
        except subprocess.TimeoutExpired:
            for rel, _ in staged:
                append({"rel": rel, "status": "error: mineru timeout",
                        "attempts": (seen.get(rel, {}).get("attempts", 0) + 1)})
            continue

        # collect results: mineru writes <chunk_out>/<stem>/<name>_*.md per file
        for rel, dst in staged:
            tb = time.time()
            key = key_for(rel)
            stem = dst.stem
            md_found = None
            for cand in chunk_out.rglob("*.md"):
                if cand.stem.startswith(stem) or cand.stem.startswith(key):
                    md_found = cand
                    break
            attempts = seen.get(rel, {}).get("attempts", 0) + 1
            if md_found is None:
                append({"rel": rel, "status": "error: no md produced",
                        "attempts": attempts})
                continue
            target = OUT / Path(rel).with_suffix(".md")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # move sibling images dir if present, rewrite md image links
                md_text = md_found.read_text(encoding="utf-8")
                img_dir = md_found.parent / "images"
                if img_dir.exists():
                    keep = OUT / "_ocr_images" / key
                    keep.mkdir(parents=True, exist_ok=True)
                    for f in img_dir.iterdir():
                        shutil.copy2(f, keep / f.name)
                    md_text = md_text.replace("images/", f"_ocr_images/{key}/")
                target.write_text(md_text, encoding="utf-8")
                append({"rel": rel, "status": "ok (ocr)",
                        "attempts": attempts, "secs": round(time.time() - tb, 1)})
                done += 1
            except OSError as e:
                append({"rel": rel, "status": f"error: write ({e})",
                        "attempts": attempts})

        # clean staging for next chunk
        for _, dst in staged:
            dst.unlink(missing_ok=True)
        shutil.rmtree(chunk_out, ignore_errors=True)

    print(f"finished: {done} converted, {len(pending) - done} deferred/errored "
          f"in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
