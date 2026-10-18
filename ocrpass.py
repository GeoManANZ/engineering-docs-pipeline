#!/usr/bin/env python3
"""
ocrpass.py — Stage 2 GPU OCR over the fastpass residue (scans + thin-fast).
MinerU 4.x edition: per-file `mineru parse` against the local GPU server.

- Queue: ~/residue.txt (absolute paths), regenerable via
  `fastpass.py --report --residue ~/residue.txt` any time.
- Manifest: ~/engineering-md/.ocrpass.jsonl (append-only, fsync per record,
  later-wins) — crash-safe pause/resume like fastpass.
- Output .md goes to ~/engineering-md/<rel>.md (replaces thin-fast text or
  creates new for scans). Sibling image dirs are preserved under
  _ocr_images/<key>/ with links rewritten.
- Two-strike error parking: a file that fails twice is parked, not retried.

Usage:
  nice -n 10 taskset -c 12-17 python3 ocrpass.py            # run lane
  python3 ocrpass.py --limit 20                             # small test
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

CORPUS_ROOTS = [Path("/mnt/c/Users/Chris/OneDrive/02. Work"),
                Path.home() / "work-docs"]  # symlink alias used by residue.txt


def rel_for(abspath: str) -> str | None:
    p = Path(abspath)
    for root in CORPUS_ROOTS:
        try:
            return str(p.relative_to(root))
        except ValueError:
            continue
    return None
OUT = Path.home() / "engineering-md"
MANIFEST = OUT / ".ocrpass.jsonl"
MINERU = str(Path.home() / "mineru-venv/bin/mineru")
SCRATCH = Path.home() / ".hermes/cache/scratch/ocrpass-out"
WAIT_S = 720  # max seconds per file: measured p99=591s, max=760s on valid parses;
              # 300s cut off real work, 900s fed genuine hangs. Keep above the tail.


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
    args = ap.parse_args()

    queue = [l.strip() for l in open(args.queue, encoding="utf-8") if l.strip()]
    seen = load_seen()
    pending = []
    for abspath in queue:
        rel = rel_for(abspath)
        if rel is None:
            continue
        prev = seen.get(rel)
        if prev and str(prev.get("status", "")).startswith("ok"):
            continue
        if prev and prev.get("attempts", 0) >= 2:
            continue
        pending.append((rel, abspath))
    if args.limit:
        pending = pending[:args.limit]

    print(f"queue: {len(queue)} | done: {len(seen)} | pending: {len(pending)}", flush=True)
    if not pending:
        return

    SCRATCH.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    ok = 0
    for n, (rel, abspath) in enumerate(pending, 1):
        tb = time.time()
        attempts = seen.get(rel, {}).get("attempts", 0) + 1
        src = Path(abspath)
        if not src.exists():
            append({"rel": rel, "status": "error: source missing", "attempts": attempts})
            continue
        key = key_for(rel)
        tmp_md = SCRATCH / f"{key}.md"
        tmp_md.unlink(missing_ok=True)
        try:
            r = subprocess.run(
                [MINERU, "parse", str(src), "-o", str(tmp_md),
                 "--pages", "all", "--tier", "basic", "--wait", str(WAIT_S)],
                capture_output=True, text=True, timeout=WAIT_S + 120)
        except subprocess.TimeoutExpired:
            r = None
        if r is None or r.returncode != 0 or not tmp_md.exists():
            why = "timeout" if r is None else ((r.stderr or r.stdout or "no output")[-160:])
            append({"rel": rel, "status": f"error: parse ({why})", "attempts": attempts})
            print(f"[{n}/{len(pending)}] FAIL {rel[-60:]}: {why[:80]}", flush=True)
            continue

        try:
            target = OUT / Path(rel).with_suffix(".md")
            target.parent.mkdir(parents=True, exist_ok=True)
            md_text = tmp_md.read_text(encoding="utf-8")
            # capture any sibling images dir mineru produced
            img_src = tmp_md.parent / (tmp_md.stem + "_images")
            if not img_src.exists():
                img_src = tmp_md.parent / "images"
            if img_src.exists():
                keep = OUT / "_ocr_images" / key
                keep.mkdir(parents=True, exist_ok=True)
                for f in img_src.iterdir():
                    shutil.copy2(f, keep / f.name)
                md_text = md_text.replace("images/", f"_ocr_images/{key}/")
            target.write_text(md_text, encoding="utf-8")
            append({"rel": rel, "status": "ok (ocr)", "attempts": attempts,
                    "secs": round(time.time() - tb, 1)})
            ok += 1
            if n % 10 == 0 or n == len(pending):
                rate = n / max(time.time() - t0, 1) * 3600
                print(f"[{n}/{len(pending)}] ok={ok} · {rate:.0f} files/h · "
                      f"eta {((len(pending)-n)/max(rate/3600,1e-9))/3600:.1f}h", flush=True)
        except OSError as e:
            append({"rel": rel, "status": f"error: write ({e})", "attempts": attempts})
        finally:
            tmp_md.unlink(missing_ok=True)

    print(f"finished: {ok} converted, {len(pending)-ok} deferred/errored "
          f"in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
