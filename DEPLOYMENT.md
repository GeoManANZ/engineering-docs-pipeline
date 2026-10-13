# DEPLOYMENT.md — Engineering Docs Pipeline v7

**Method: many small jobs, not one big job.**

Supersedes the v6 `pipeline.py` two-server method (see "Why v6 died" at the
bottom). Nothing in v7 requires a daemon, concurrency, or an uninterrupted run.

## The goal, restated

1. Every engineering document/book queryable semantically.
2. Laptop memory respected at every stage.
3. OCR used only where required, economically.

## Guarantees per stage

| Stage | Laptop RAM | GPU | Can you close the lid? |
|---|---|---|---|
| 1 fastpass | ~300 MB, 1 process | none | yes, any time |
| 2 OCR residue | one process, chunked | yes, chunk windows only | yes, between chunks |
| 3 rsync | trivial | none | yes |
| 4 gbrain index | **zero — runs on VPS** | none | n/a |
| 5 query | zero | none | n/a |

The only stage that touches the GPU is Stage 2, and only for the measured
OCR residue — never the whole library, never in the background.

---

## Stage 0 — one-time prep (~10 min)

Cap the WSL2 VM so its page cache cannot balloon into Windows and OOM the
laptop (this is the fix for "dies after a few hours"). Windows side,
`%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=40GB
swap=16GB
pageReporting=false
```

Then from PowerShell: `wsl --shutdown`, reopen WSL.

```bash
cd ~/engineering-docs-pipeline && git pull
source ~/pdf-convert-venv/bin/activate
pip install pymupdf pymupdf4llm     # only new deps Stage 1 needs
```

## Stage 1 — measure, then convert the digital majority

```bash
source ~/pdf-convert-venv/bin/activate
cd ~/engineering-docs-pipeline

python3 fastpass.py --root ~/work-docs                 # converts all text-layer docs
python3 fastpass.py --report --residue ~/residue.txt  # quantifies the OCR tail
```

- Single process, one file at a time, no GPU, no daemon. ~300 MB resident.
- Resume-safe: append-only JSONL manifest; re-running skips finished files.
- Interrupt any time — sleep, reboot, travel. Nothing to corrupt, nothing
  left running.
- Writes `~/engineering-md/**.md` for everything with a text layer, and
  `~/residue.txt` listing exactly what still needs OCR.
- Never touches, moves, or deletes originals.

**Decision gate G1 — read the report before spending a single GPU-minute.**
The report prints the OCR workload as files / pages / estimated hours:
- Residue is small (a few hundred files) → Stage 2 in a couple of evenings.
- Residue is dominated by a few giant scanned books → triage by value
  (recent projects and core references first). A 500-page scanned spec book
  is a calendar-eater, not a technical problem; you choose whether it earns
  its GPU time.

## Stage 2 — OCR the residue, economically

Run **one** batch process over chunks of the residue list. No server, no
second terminal, models load once per chunk and the process exits:

```bash
source ~/pdf-convert-venv/bin/activate
mkdir -p ~/ocr-stage
# fill ~/ocr-stage with the next ~200-300 files from ~/residue.txt
mineru -p ~/ocr-stage -o ~/engineering-md/ocr -b pipeline
# move .md outputs into ~/engineering-md (keep names), then: rm -rf ~/ocr-stage/*
```

- `-b pipeline` runs CPU-or-GPU, fast and stable. Switch to the VLM backend
  only if spot-checks show the pipeline path mangles formulas you care about.
- Long books are safe: MinerU's sliding-window keeps peak memory bounded on
  very long documents.
- Low VRAM: tune `MINERU_HYBRID_BATCH_RATIO` down (≤4 GB → 4, ≤3 GB → 2).
- One MinerU process at a time. Never parallel OCR workers on this laptop.
- A small `ocrpass.py` runner (same JSONL-manifest pattern as fastpass) will
  automate the chunk-copy → convert → collect → clean loop. **Build it after
  G1**, once the residue's real size and content are known — not before.

## Stage 3 — sync to the VPS

```bash
rsync -avz --progress ~/engineering-md/ <your-vps-ssh-alias>:/data/engineering-docs/
```

Incremental. Total payload is the .md set (~1–2 GB for a 100 GB library).

## Stage 4 — semantic index (all on the VPS; laptop fully out of the loop)

```bash
# on the VPS
gbrain import /data/engineering-docs/ --no-embed   # instant, no vectors yet
gbrain embed --stale                               # embeds only new/changed
```

Two-phase on purpose: import is near-instant for tens of thousands of files;
the expensive embedding pass runs on the always-on VPS — this is what keeps
the laptop's role to convert-and-upload.

**Decision gate G2 — embedding is a paid Voyage API run.** Embed one
top-level project folder first, check search quality and the API cost on
that batch, then commit the rest. Do not blind-fire the whole library.

## Stage 5 — use it

```bash
gbrain search "pile driveability hammer selection N-values"
gbrain query "what do we have on jetting effects in dense sand"
```

Every .md retains its source path, so a hit points back at the original PDF
when you need the figure or the signature page.

---

## What is deliberately NOT in this method

- **No two-server hybrid architecture.** Two docling+torch daemons plus 4-way
  parallel `hybrid_mode="full"` conversions is what OOM-killed v6.
- **No concurrency knobs.** Tuned blind on a job that had never survived a
  full run; durability, not throughput, was the constraint.
- **No mid-run OneDrive space reclamation.** Hold `attrib +U -P` until the
  corpus is converted and spot-checked, so a bad conversion never costs you
  local access to the source.
- **No third Hermes instance.** The existing gbrain is the semantic store.

## Why v6 died (kept for the record)

52k files at the claimed 26.8 files/min is a **~32-hour continuous run**.
Two GPU daemons + four concurrent full-mode conversions cannot hold that on
a laptop. `check_server()` ran once at startup, staging was freed only at
150-file chunk boundaries, and five bare `except:` swallowed the
`MemoryError` that killed it — which is why the logs never said why.
Confirm anytime with `dmesg -T | grep -i -E "killed process|out of memory"`.
