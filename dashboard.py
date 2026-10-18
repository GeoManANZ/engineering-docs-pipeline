#!/usr/bin/env python3
"""
pipeline-dashboard.py — text dashboard for the engineering-docs pipeline.

Three lanes + upload, completion bars, live rates. Stdlib only.

  python3 ~/engineering-docs-pipeline/dashboard.py            # 15s refresh
  python3 ~/engineering-docs-pipeline/dashboard.py -r 5       # 5s refresh
  python3 ~/engineering-docs-pipeline/dashboard.py --no-ssh   # skip VPS panel

Ctrl-C to quit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
FASTPASS_MF = HOME / "engineering-md/.fastpass.jsonl"
OCRPASS_MF = HOME / "engineering-md/.ocrpass.jsonl"
RESIDUE = HOME / "residue.txt"
TOTAL_SCOPE = 20_193          # docs in scope (OLEMI excluded)
CORPUS_TARGET_GB = 23.0       # expected VPS corpus size when upload completes

# VPS-owned scopes (disjoint from laptop lanes): overlap between the two
# manifests equals local-done files inside these scopes (arrive via vps-pull).
VPS_TOP = {"TSP Projects", "Vancouver Geotechnical Society", "M Bolton Publications",
           "Geotechnique", "Byland Technical", "Civil Engineering Books",
           "RR Technical Info", "Conferences", "ICE Professional Review Docs"}
GEO = "Geotechnical Documents"
VPS_GEO_SUB = {"ICE Civil Engineering", "BHe HardDrive", "Road & Pavement Design",
               "GRL Engineers", "ICE Ground Improvement",
               "Australian Geomechanics Journal", "Victoria Basin Studies"}


def in_vps_scope(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] in VPS_TOP:
        return True
    return parts[0] == GEO and len(parts) > 1 and f"{parts[1]}" in VPS_GEO_SUB

C_GREEN, C_YELLOW, C_RED, C_CYAN, C_DIM, C_RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[36m", "\033[2m", "\033[0m")

vps_cache: dict = {"ts": 0.0, "data": None}


def bar(done: float, total: float, width: int = 32) -> str:
    if total <= 0:
        return "[" + " " * width + "]  -%"
    frac = max(0.0, min(1.0, done / total))
    filled = int(frac * width)
    pct = f"{frac * 100:5.1f}%"
    return "[" + "█" * filled + "░" * (width - filled) + "] " + pct


def parse_manifest(path: Path) -> tuple[dict[str, str], int, list[dict], int]:
    """Return {rel: verdict-class} unique-by-rel (later wins), line count,
    the last 10 completed-file records, and the live GPU-queue contribution
    (needs_ocr + thin-fast files: everything Stage 2 still owes a pass)."""
    seen: dict[str, str] = {}
    lines = 0
    recent: list[dict] = []
    queue = 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rel, v, s = r.get("rel"), r.get("verdict"), str(r.get("status", ""))
                if rel is None:
                    continue
                is_done = v in ("digital", "thin") and s.startswith("ok")
                if is_done:
                    seen[rel] = "done"
                    if s.startswith("ok (thin"):  # thin-fast: still owes a GPU pass
                        queue += 1
                    recent.append({"rel": rel, "size": r.get("size") or 0,
                                   "secs": r.get("secs"), "how": s.strip("() \n")[:12]})
                    del recent[:-10]
                elif v == "needs_ocr":
                    seen[rel] = "ocr"
                    queue += 1
                else:
                    seen[rel] = "err"
    except FileNotFoundError:
        pass
    # thin-fast files are 'done' in seen but still owe a GPU pass
    return seen, lines, recent, queue


def gpu_recent(n: int = 10) -> list[dict]:
    out: list[dict] = []
    try:
        with OCRPASS_MF.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(r.get("status", "")).startswith("ok"):
                    out.append({"rel": r.get("rel", "?"),
                                "size": r.get("size") or 0,
                                "secs": r.get("secs"), "how": "ocr"})
                    del out[:-n]
    except FileNotFoundError:
        pass
    # enrich with live size (records don't store it)
    for rec in out:
        if not rec["size"]:
            for root in ("/mnt/c/Users/Chris/OneDrive/02. Work", str(HOME / "work-docs")):
                try:
                    rec["size"] = os.path.getsize(os.path.join(root, rec["rel"]))
                    break
                except OSError:
                    continue
    return out


def fmt_size(n) -> str:
    n = n or 0
    return f"{n/2**20:.1f}MB" if n >= 2**20 else f"{n/2**10:.0f}KB"


def fmt_secs(s) -> str:
    if s is None:
        return "  -  "
    s = float(s)
    return f"{int(s//60)}m{s%60:02.0f}" if s >= 60 else f"{s:.1f}s"


def print_recent(items: list[dict], indent: str = "  ") -> None:
    if not items:
        print(f"{indent}{C_DIM}(nothing completed yet){C_RESET}")
        return
    for r in reversed(items[-10:]):  # newest last, like a log
        rel = r["rel"]
        tail = ("…" + rel[-58:]) if len(rel) > 59 else rel
        print(f"{indent}{fmt_size(r['size']):>8}  {fmt_secs(r['secs']):>6}  {tail}")


def local_workers_alive() -> int:
    try:
        out = subprocess.run(["pgrep", "-f", "pdf-convert-venv/bin/python.*fastpass"],
                             capture_output=True, text=True, timeout=5).stdout.split()
        return len(out)
    except Exception:
        return -1


def gpu_done() -> int:
    n = 0
    try:
        with OCRPASS_MF.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    if str(json.loads(line).get("status", "")).startswith("ok"):
                        n += 1
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return n


def queue_len() -> int:
    try:
        return sum(1 for l in RESIDUE.open(encoding="utf-8") if l.strip())
    except FileNotFoundError:
        return 0


def fetch_vps(force: bool = False) -> dict | None:
    if not force and vps_cache["data"] is not None and time.time() - vps_cache["ts"] < 60:
        return vps_cache["data"]
    script = (
        "python3 - <<'PY'\n"
        "import json, os, subprocess\n"
        "uniq={}; recent=[]\n"
        "for l in open('/home/geo/fastpass-vps/out/.fastpass.jsonl'):\n"
        "    l=l.strip()\n"
        "    if not l: continue\n"
        "    try: r=json.loads(l)\n"
        "    except: continue\n"
        "    v,s=r.get('verdict'),str(r.get('status',''))\n"
        "    cls='done' if (v in ('digital','thin') and s.startswith('ok')) "
        "else ('ocr' if v=='needs_ocr' else 'err')\n"
        "    uniq[r['rel']]=cls\n"
        "    if cls=='done':\n"
        "        recent.append({'rel':r['rel'],'size':r.get('size') or 0,'secs':r.get('secs')})\n"
        "        del recent[:-10]\n"
        "gb=0\n"
        "try:\n"
        "    out=subprocess.run(['du','-sb','/home/geo/fastpass-vps/corpus'],"
        "capture_output=True,text=True).stdout.split()\n"
        "    gb=round(int(out[0])/2**30,1)\n"
        "except Exception: pass\n"
        "print(json.dumps({'done':sum(1 for x in uniq.values() if x=='done'),"
        "'ocr':sum(1 for x in uniq.values() if x=='ocr'),"
        "'err':sum(1 for x in uniq.values() if x=='err'),"
        "'gb':gb,"
        "'loop': subprocess.run(['pgrep','-f','worker-loop'],"
        "capture_output=True).returncode==0,"
        "'recent':recent}))\n"
        "PY"
    )
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                            "vps", script], capture_output=True, text=True, timeout=20)
        data = json.loads(r.stdout.strip().splitlines()[-1])
        vps_cache.update(ts=time.time(), data=data)
        return data
    except Exception:
        return vps_cache["data"]


def rate(delta: float, seconds: float) -> str:
    if seconds < 30 or delta <= 0:
        return ""
    h = delta / seconds * 3600
    return f"  {C_CYAN}{h:,.0f}/h{C_RESET}"


def eta_str(remaining: float, per_hour: float) -> str:
    if per_hour <= 0 or remaining <= 0:
        return ""
    hrs = remaining / per_hour
    if hrs < 1:
        return f"  ~{hrs*60:.0f}min left"
    if hrs < 48:
        return f"  ~{hrs:.1f}h left"
    return f"  ~{hrs/24:.1f}d left"


def render(state: dict) -> None:
    print("\033[2J\033[H")
    print(f"{C_CYAN}╔══════════════ ENGINEERING DOCS PIPELINE ══════════════╗{C_RESET}")
    print(f"  {time.strftime('%Y-%m-%d %H:%M:%S')}   Ctrl-C to quit")
    print()

    # ── Stage 1 ──
    loc = state["local"]
    vps = state["vps"]
    l_done, v_done = loc["done"], (vps or {}).get("done", 0)
    combined_done = l_done + max(0, v_done - loc["done_vps_scope"])  # conservative when vps data absent
    print(f"{C_GREEN}STAGE 1 — text conversion (fastpass){C_RESET}")
    print(f"  laptop  {bar(l_done, TOTAL_SCOPE)}  {l_done:,} done{rate(state['d_lap'], state['elapsed'])}")
    if vps is not None:
        print(f"  vps     {bar(v_done, TOTAL_SCOPE)}  {v_done:,} done{rate(state['d_vps'], state['elapsed'])}")
    else:
        print(f"  vps     {C_DIM}ssh disabled or unreachable — combined shows laptop + pulled only{C_RESET}")
    print(f"  {C_YELLOW}COMBINED{C_RESET} {bar(combined_done, TOTAL_SCOPE)}  "
          f"{combined_done:,}/{TOTAL_SCOPE:,}")
    print(f"          OCR-queue {loc['ocr'] + (vps or {}).get('ocr', 0):,} · "
          f"errors parked {loc['err'] + (vps or {}).get('err', 0):,} · "
          f"workers {state['workers']}/4 + vps "
          f"{('loop ' + (C_GREEN + 'on' + C_RESET)) if (vps or {}).get('loop') else (C_RED + 'OFF' + C_RESET)}")
    print(f"  {C_DIM}last completed — laptop workers:{C_RESET}")
    print_recent(state["loc_recent"])
    if vps and vps.get("recent"):
        print(f"  {C_DIM}last completed — vps worker:{C_RESET}")
        print_recent(vps["recent"])
    print()

    # ── Upload ──
    gb = (vps or {}).get("gb", 0)
    print(f"{C_GREEN}UPLOAD → VPS{C_RESET}")
    print(f"  corpus  {bar(gb, CORPUS_TARGET_GB)}  {gb:.1f}/{CORPUS_TARGET_GB:.0f} GB   "
          f"rsync {'running' if state['rsync'] else C_DIM + 'idle' + C_RESET}")
    print()

    # ── Stage 2 ──
    q, g = state["queue"], state["gpu"]
    print(f"{C_GREEN}STAGE 2 — GPU OCR (MinerU on 5070 Ti){C_RESET}")
    print(f"  ocr     {bar(g, max(q, 1))}  {g:,}/{q:,} queue"
          f"{rate(state['d_gpu'], state['elapsed'])}{eta_str(q - g, state['gpu_rate'])}")
    print(f"  {C_DIM}last completed — gpu worker:{C_RESET}")
    print_recent(state["gpu_recent"])
    print()

    print(f"{C_DIM}  progress cmd: wc -l < ~/engineering-md/.fastpass.jsonl · "
          f"gpu log: tail ~/engineering-md/ocrpass.log{C_RESET}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-r", "--refresh", type=float, default=15)
    ap.add_argument("--no-ssh", action="store_true")
    args = ap.parse_args()

    state: dict = {"first": True, "t0": time.time(), "elapsed": 0.0,
                   "d_lap": 0, "d_vps": 0, "d_gpu": 0, "gpu_rate": 0.0,
                   "l_prev": None, "v_prev": None, "g_prev": None}
    try:
        while True:
            loc_seen, _, loc_recent, queue = parse_manifest(FASTPASS_MF)
            loc = {"done": sum(1 for v in loc_seen.values() if v == "done"),
                   "ocr": sum(1 for v in loc_seen.values() if v == "ocr"),
                   "err": sum(1 for v in loc_seen.values() if v == "err"),
                   "done_vps_scope": sum(1 for k, v in loc_seen.items()
                                          if v == "done" and in_vps_scope(k))}
            state["loc_recent"] = loc_recent
            state["gpu_recent"] = gpu_recent()
            vps = None if args.no_ssh else fetch_vps(force=state["first"])
            vps_done_set = set()
            if state["first"]:
                state["l_prev"], state["v_prev"] = loc["done"], (vps or {}).get("done", 0)
                state["g_prev"] = gpu_done()
                state["first"] = False
            else:
                state["d_lap"] = loc["done"] - state["l_prev"]
                state["l_prev"] = loc["done"]
                state["d_vps"] = (vps or {}).get("done", state["v_prev"]) - state["v_prev"]
                state["v_prev"] = (vps or {}).get("done", state["v_prev"])
                g = gpu_done()
                state["d_gpu"] = g - state["g_prev"]
                state["g_prev"] = g
                if state["elapsed"] > 0:
                    state["gpu_rate"] = state["d_gpu"] / state["elapsed"] * 3600
            state.update(local=loc, vps=vps, workers=local_workers_alive(),
                         queue=queue, gpu=state.get("g_prev", 0),
                         rsync=subprocess.run(["pgrep", "-f", "rsync.*02. Work"],
                                              capture_output=True).returncode == 0)
            render(state)
            time.sleep(args.refresh)
            state["elapsed"] += args.refresh
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
