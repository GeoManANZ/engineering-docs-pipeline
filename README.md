# Engineering Docs Pipeline v2

Multi-format document converter for engineering libraries. Converts PDF, DOCX, and XLSX files to Markdown with GPU-accelerated OCR, formula extraction, and OneDrive space management.

## Quick Reference

```bash
git clone git@github.com:GeoManANZ/engineering-docs-pipeline.git
cd engineering-docs-pipeline
bash setup.sh
python3 pipeline.py --test
```

---

## Prerequisites

1. **G14 laptop** with WSL2 (Ubuntu)
2. **NVIDIA GPU** + CUDA drivers installed in WSL2
3. **Python 3.10+** (should already be there)
4. **Java 17+**: `sudo apt install openjdk-17-jdk`
5. **OneDrive folder**: `C:\Users\Chris\OneDrive\02. Work`

---

## Setup — Step by Step

### Step 1: Clone the Repo

```bash
git clone git@github.com:GeoManANZ/engineering-docs-pipeline.git
cd engineering-docs-pipeline
```

### Step 2: Run Setup (Creates venv + Installs Tools)

```bash
bash setup.sh
```

This creates `~/pdf-convert-venv/` and installs:
- `opendataloader-pdf[hybrid]` — GPU-accelerated PDF converter
- `markitdown[all]` — DOCX/XLSX converter (Microsoft)

**Time:** ~5 minutes (mostly downloading models)

### Step 3: Create the Symlink (Recommended)

Your OneDrive folder path has spaces:

```
/mnt/c/Users/Chris/OneDrive/02. Work
```

That space makes every command awkward — you'd need to quote it every time. A symlink solves this:

```bash
ln -s "/mnt/c/Users/Chris/OneDrive/02. Work" ~/work-docs
```

**What this does:** Creates a shortcut at `~/work-docs` that points to your OneDrive folder. Now you can type `~/work-docs` instead of the full path with quotes.

**Why it matters:**
- No quoting spaces: `ls ~/work-docs` vs `ls "/mnt/c/Users/Chris/OneDrive/02. Work"`
- No typing the full path every time
- Works in every tool (Python, shell, etc.)
- If you ever move the folder, update one symlink, not every script

### Step 4: Configure the Pipeline

Edit `pipeline.py` and find the `CONFIGURATION` section (top of file):

```python
# Option A: With symlink (from Step 3)
import os
ONEDRIVE_ROOT = os.path.expanduser("~/work-docs")

# Option B: Direct path (no symlink)
ONEDRIVE_ROOT = "/mnt/c/Users/Chris/OneDrive/02. Work"
```

Replace `<YOUR_USERNAME>` if needed. Verify it works:

```bash
ls ~/work-docs/
# or
ls "/mnt/c/Users/Chris/OneDrive/02. Work/"
```

You should see your files and folders.

### Step 5: Start the Hybrid Server (Terminal 1)

This is the GPU-powered backend that converts PDFs. It loads AI models into your GPU and stays running. **You need this running whenever you process PDFs.**

Open a terminal and run:

```bash
cd ~/pdf-convert-venv
source bin/activate
opendataloader-pdf-hybrid --port 5002 \
  --force-ocr --ocr-engine rapidocr --device cuda \
  --enrich-formula --enrich-picture-description
```

You'll see:
```
INFO: Uvicorn running on http://0.0.0.0:5002
```

**Do not close this terminal.** It's the engine. Leave it running.

### Step 6: Run the Test Batch (Terminal 2)

Open a **second terminal** and run:

```bash
cd ~/engineering-docs-pipeline
python3 pipeline.py --test
```

**What happens:**
1. Scans your OneDrive folder (counts all PDF/DOCX/XLSX)
2. Picks the first 50 files
3. For each file: downloads from OneDrive → converts → frees local disk space
4. Saves `.md` to `~/engineering-md/`
5. Prints a summary

**Time:** ~5-10 minutes for 50 files (depends on file sizes)

**How to check it worked:**

```bash
ls ~/engineering-md/          # See converted files
python3 pipeline.py --status  # See progress breakdown
python3 pipeline.py --failed  # See any errors
```

### Step 7: Run the Full Pipeline

Once the test looks good:

```bash
python3 pipeline.py
```

The pipeline:
- Resumes where `--test` left off
- Only processes new/changed files on subsequent runs
- Safe to stop (Ctrl+C) and restart — uses SQLite tracking
- Can run overnight — it's self-logging and resume-safe

---

## Commands

| Command | What it does |
|---------|-------------|
| `python3 pipeline.py --test` | Convert first 50 files (test quality) |
| `python3 pipeline.py` | Full run — resumes from last checkpoint |
| `python3 pipeline.py --status` | Show progress without converting |
| `python3 pipeline.py --scan-only` | Index files without converting |
| `python3 pipeline.py --failed` | List failed files with error details |
| `python3 pipeline.py --retry-failed` | Re-attempt only failed files |
| `python3 pipeline.py --reset` | Delete tracking database (start over) |

---

## What Converts What

| Format | Converter | How |
|--------|-----------|-----|
| `.pdf` | opendataloader-pdf hybrid | Sent to GPU server on port 5002 |
| `.docx` | MarkItDown (Microsoft) | Local processing, no GPU needed |
| `.xlsx` | MarkItDown | Local processing |
| `.doc` `.xls` `.ppt` | **Skipped** | Legacy formats — convert manually first |

---

## Why the Hybrid Server?

The hybrid server is a **separate long-running process** for a reason: loading the AI models into GPU memory takes ~30 seconds. If we loaded/unloaded it per file, a 50-file test would take 25 minutes just loading. Running it once as a persistent server eliminates that overhead.

**Analogy:** It's like a database server. You start PostgreSQL once, then your app queries it. Same pattern here — start the server once, then the pipeline sends it PDFs.

---

## OneDrive Space Management

The pipeline downloads files from OneDrive, converts them, then frees the local disk space:

```
File is cloud-only (placeholder)
  → pipeline.py calls ensure_local()
  → OneDrive downloads the file
  → opendataloader-pdf converts to .md
  → free_onedrive_space() runs: attrib.exe +U -P
  → OneDrive reclaims the local copy
```

**You don't need 154GB free disk space.** Only enough for the files currently being processed (~1-2GB for the current batch).

**`attrib.exe` flags:**
- `+U` = unpin (mark as "online-only" — cloud only, no local copy)
- `-P` = clear "always available" flag

This works from WSL2 via `/mnt/c/Windows/system32/attrib.exe`.

---

## Output Files

| File | Location | Purpose |
|------|----------|---------|
| Converted docs | `~/engineering-md/*.md` | Your Markdown documents |
| Progress database | `~/engineering-md/.pipeline.db` | SQLite — tracks every file |
| Log file | `~/engineering-md/pipeline.log` | Full conversion log |

---

## Incremental Updates (Nightly Cron)

After the initial conversion, subsequent runs are fast. Periodically re-scan to catch new files:

```bash
crontab -e
```
Add:
```
0 2 * * * cd ~/engineering-docs-pipeline && python3 pipeline.py >> ~/engineering-md/cron.log 2>&1
```

This runs at 2am every night — scans OneDrive, processes only new/changed files.

---

## After Conversion: gbrain Ingestion

Once `.md` files are on the VPS, index for semantic search:

```bash
# On VPS
gbrain import /data/engineering-docs/ --no-embed
gbrain embed --stale
```

---

## Accuracy

| Metric | Score | Tool |
|--------|-------|------|
| Overall accuracy | 0.907 | #1 of 12 converters |
| Table extraction | 0.928 | Critical for engineering specs |
| Reading order | 0.934 | Multi-column layouts |
| Formula → LaTeX | ✅ | `--enrich-formula` flag |
| Figure captions | ✅ | `--enrich-picture-description` flag |

The hybrid server command uses every accuracy-enhancing flag available:

```bash
opendataloader-pdf-hybrid --port 5002 \
  --force-ocr              # OCR every page (scanned docs)
  --ocr-engine rapidocr     # ONNX-based OCR engine
  --device cuda             # Use GPU (10-50x faster)
  --enrich-formula          # Extract equations as LaTeX
  --enrich-picture-description  # Describe figures/charts
```

---

## Troubleshooting

**"Source path does not exist"**
```bash
ls "/mnt/c/Users/Chris/OneDrive/02. Work/"
```
If this fails, OneDrive isn't mounted in WSL2. Check: `ls /mnt/c/`

**"Hybrid server not running"**
→ Start Terminal 1 first. The server must be running before processing PDFs.
→ If only processing DOCX/XLSX: the pipeline skips the server check (no PDFs pending).

**"Download timeout"**
→ OneDrive may be syncing or network is slow. The file gets marked as failed.
→ Run `python3 pipeline.py --retry-failed` later.

**"opendataloader_pdf not installed"**
→ The venv isn't activated. Run: `cd ~/pdf-convert-venv && source bin/activate`

**Disk space filling up**
→ Check `FREE_SPACE_AFTER_CONVERT = True` in pipeline.py
→ Wait — OneDrive can take a minute to reclaim space after `attrib.exe`
→ Verify attrib works manually:
```bash
/mnt/c/Windows/system32/attrib.exe +U -P "/mnt/c/Users/Chris/OneDrive/02. Work/somefile.pdf"
```

**Pipeline crashed mid-run**
→ Just run it again: `python3 pipeline.py` — it resumes from the last completed file.

---

## Architecture

```
┌─ G14 (WSL2) ──────────────────────────────────────────────┐
│                                                             │
│  Terminal 1: Hybrid Server (GPU)                            │
│  └── opendataloader-pdf-hybrid :5002                        │
│      --enrich-formula --enrich-picture-description          │
│                                                             │
│  Terminal 2: Pipeline                                       │
│  └── pipeline.py --test                                     │
│      Phase 1: Scan OneDrive (os.walk)                       │
│      Phase 2: Check hybrid server                           │
│      Phase 3: Process 50 files (parallel, 4 workers)        │
│      │   per file:                                          │
│      │   ├── ensure_local() — download from OneDrive        │
│      │   ├── convert_file() — .pdf→GPU, .docx/.xlsx→CPU    │
│      │   └── free_onedrive_space() — attrib.exe +U -P      │
│      Phase 4: Report                                       │
│                                                             │
│  ~/engineering-md/*.md  ← Output                            │
└─────────────────────────────────────────────────────────────┘
```
