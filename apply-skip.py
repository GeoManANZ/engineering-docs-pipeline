#!/usr/bin/env python3
"""
apply-skip.py — mark drawing-skip files as permanently handled so every lane
skips them. Appends "ok (skipped-drawing)" records (verdict thin) to:
  1. the laptop manifest
  2. the VPS manifest (VPS-scope subset), then restarts the worker-loop

Run after drawing-scan.py has produced ~/drawings-skip.jsonl.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

MF = Path.home() / "engineering-md/.fastpass.jsonl"
SKIP = Path.home() / "drawings-skip.jsonl"
VPS_TOP = {"TSP Projects", "Vancouver Geotechnical Society", "M Bolton Publications",
           "Geotechnique", "Byland Technical", "Civil Engineering Books",
           "RR Technical Info", "Conferences", "ICE Professional Review Docs"}
GEO = "Geotechnical Documents"
VPS_GEO_SUB = {"ICE Civil Engineering", "BHe HardDrive", "Road & Pavement Design",
               "GRL Engineers", "ICE Ground Improvement",
               "Australian Geomechanics Journal", "Victoria Basin Studies"}


def in_vps_scope(rel: str) -> bool:
    p = rel.split("/")
    return p[0] in VPS_TOP or (p[0] == GEO and len(p) > 1 and p[1] in VPS_GEO_SUB)


def main() -> None:
    recs = [json.loads(l) for l in SKIP.open(encoding="utf-8") if l.strip()]
    print(f"skip records: {len(recs):,}")

    with MF.open("a", encoding="utf-8") as fh:
        for s in recs:
            fh.write(json.dumps({
                "rel": s["rel"], "status": "ok (skipped-drawing)", "verdict": "thin",
                "ext": ".pdf", "size": s["size"], "pages": s["pages"],
                "chars": s["chars"], "secs": 0.0}, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    print(f"local manifest updated: {MF} (now {sum(1 for _ in MF.open())} lines)")

    vps_recs = [s for s in recs if in_vps_scope(s["rel"])]
    print(f"vps-scope subset: {len(vps_recs):,}")
    if vps_recs:
        payload = "\n".join(json.dumps({
            "rel": s["rel"], "status": "ok (skipped-drawing)", "verdict": "thin",
            "ext": ".pdf", "size": s["size"], "pages": s["pages"],
            "chars": s["chars"], "secs": 0.0}, ensure_ascii=False) for s in vps_recs)
        tmp = Path.home() / ".hermes/cache/scratch/vps-skip.jsonl"
        tmp.write_text(payload + "\n", encoding="utf-8")
        subprocess.run(["scp", "-q", str(tmp), "vps:/tmp/vps-skip.jsonl"], check=True)
        subprocess.run(["ssh", "vps",
                        "cat /tmp/vps-skip.jsonl >> ~/fastpass-vps/out/.fastpass.jsonl "
                        "&& wc -l < ~/fastpass-vps/out/.fastpass.jsonl"], check=True)
        # restart the loop so the next pass honours the skips immediately
        subprocess.run(["ssh", "vps", "pkill -f 'worker-loop[.]sh'; pkill -f 'fastpass[.]py'; true"],
                       capture_output=True)
        subprocess.run(["ssh", "-f", "vps", "nohup ~/fastpass-vps/worker-loop.sh >/dev/null 2>&1"],
                       capture_output=True)
        print("vps manifest updated + worker-loop restarted")
    print("DONE")


if __name__ == "__main__":
    main()
