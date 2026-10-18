#!/usr/bin/env python3
"""gpu-queue-cut.py — remove agreed low-value classes from the GPU queue.
Approved 2026-10-18: RR Architect/Civil&Structural drawings, OHW standard
drawings, OLE drawing-coded files, Leeds Standard Details, photos, certs,
blank forms, emails/faxes. Judgment-call items KEPT: schedules, spec-clause
extracts, capacity sheets, handouts."""
import json, os, re

MF = os.path.expanduser("~/engineering-md/.fastpass.jsonl")

FOLDER_CUTS = [
    r"^RR Technical Info/Architect/",
    r"^RR Technical Info/Civil & Strucutral Engineer/",
    r"^OHW Standard Drawings/",
    r"/Leeds Standard Details/",
]
NAME_CUTS = [
    (r"DRG", "drawing"),
    (r"(^|/)uk\d{1,3}-", "drawing"),
    (r"-\w+-C\d(\.\d)?-\d", "drawing"),
    (r"(^|/)(IMG_|DSC_|DJI_)", "photo"),
    (r"photo|snapshot", "photo"),
    (r"certificat|charter|credential|licen[cs]e|diploma|transcript", "cert"),
    (r"(^|[^a-z])forms?([^a-z]|$)|template|checklist|questionnaire", "form"),
    (r"email|\bfax\b|(^|/)memo", "email"),
]

seen = {}
for l in open(MF):
    l = l.strip()
    if not l:
        continue
    try:
        r = json.loads(l)
    except json.JSONDecodeError:
        continue
    seen[r["rel"]] = r

queue, cuts = {}, {}
for rel, r in seen.items():
    v, s = r.get("verdict"), str(r.get("status", ""))
    if not (v == "needs_ocr" or (v == "thin" and s.startswith("ok (thin"))):
        continue
    why = None
    for pat in FOLDER_CUTS:
        if re.search(pat, rel):
            why = "drawing-folder"
            break
    if not why:
        for pat, cls in NAME_CUTS:
            base = rel.rsplit("/", 1)[-1]
            if re.search(pat, base, re.I):
                why = cls
                break
    if why:
        cuts[rel] = why
    else:
        queue[rel] = True

import collections
by = collections.Counter(cuts.values())
print(f"queue before: {len(queue) + len(cuts):,} -> after: {len(queue):,}  (cut {len(cuts):,})")
for k, v in by.most_common():
    print(f"  {v:5d}  {k}")

with open(MF, "a", encoding="utf-8") as fh:
    for rel, why in cuts.items():
        fh.write(json.dumps({"rel": rel, "status": f"ok (skipped-{why})",
                             "verdict": "thin", "ext": ".pdf", "size": 0,
                             "pages": 0, "chars": 0, "secs": 0.0}) + "\n")
    fh.flush()
    os.fsync(fh.fileno())
with open(os.path.expanduser("~/engineering-md/.ocrpass.jsonl"), "a", encoding="utf-8") as fh:
    for rel in cuts:
        fh.write(json.dumps({"rel": rel, "status": "ok (skipped)", "attempts": 1}) + "\n")
    fh.flush()
    os.fsync(fh.fileno())
print("skip records written to both manifests")
