# Engineering Docs Pipeline v3

Multi-format document converter for engineering libraries. Converts PDF, DOCX, and XLSX files to Markdown with GPU-accelerated OCR, formula extraction, pre-fetch staging, and OneDrive space management.

## What Changed in v3

| v2 | v3 |
|----|----|
| Download → convert → free per file | Stage 10 files ahead, convert from staging |
| Converter thread idle during downloads | Zero idle — downloads are pre-fetched |
| All .md files flat in `~/engineering-md/` | Preserves source folder structure |
| Files downloaded from OneDrive at convert time | Staged locally, OneDrive freed immediately |

---

## How It Works

```
┌─ On a single file ───────────────────────────────────────┐
│                                                           │
│  1. stage_file(rel):                                      │
│     └── ensure_local() — triggers OneDrive download       │
│     └── shutil.copy2() — copies to .staging/ directory    │
│     └── free_onedrive_space() — reclaims OneDrive copy    │
│                                                           │
│  2. process_single_file():                                │
│     └── convert_pdf() or convert_office() — from staging  │
│     └── saves .md to engineering-md/ (folder preserved)   │
│                                                           │
│  3. Staging file deleted after chunk completes            │
└───────────────────────────────────────────────────────────┘
```

**Pre-fetch buffer:** While file 1 is being converted, files 2-11 are being downloaded to staging. By the time the converter reaches file 10, it's already local. No idle time.

**Disk usage at peak:** `DOWNLOAD_AHEAD` × average file size. With 10 files at ~10MB each = ~100MB. Files are deleted from staging after each chunk (50 files).

---

## Quick Start

```bash
git clone git@github.com:GeoManANZ/engineering-docs-pipeline.git
cd engineering-docs-pipeline
bash setup.sh
python3 pipeline.py --test
```

---

## Setup — Step by Step

### Step 1: Clone the Repo

```bash
git clone git@github.com:GeoManANZ/engineering-docs-pipeline.git
cd engineering-docs-pipeline
```

### Step 2: Run Setup

```bash
bash setup.sh
```

Creates `~/pdf-convert-venv/` and installs:
- `opendataloader-pdf[hybrid]` — GPU-accelerated PDF converter
- `markitdown[all]` — DOCX/XLSX converter

**Time:** ~5 minutes.

### Step 3: Create the Symlink (Recommended)

Your OneDrive path has spaces:
```
/mnt/c/Users/Chris/OneDrive/02. Work
```

Create a symlink so you never have to quote spaces:

```bash
ln -s "/mnt/c/Users/Chris/OneDrive/02. Work" ~/work-docs
```

Now `~/work-docs` points to your OneDrive folder. The pipeline auto-detects this path.

Verify it works:
```bash
ls ~/work-docs/
```

### Step 4: Start the Hybrid Server (Terminal 1)

Open a terminal, start the GPU backend:

```bash
cd ~/pdf-convert-venv
source bin/activate
opendataloader-pdf-hybrid --port 5002 \
  --force-ocr --ocr-engine rapidocr --device cuda \
  --enrich-formula --enrich-picture-description
```

You'll see: `INFO: Uvicorn running on http://0.0.0.0:5002`

**Do not close this terminal.** It's the engine. Leave it running.

### Step 5: Run the Test Batch (Terminal 2)

Open a second terminal:

```bash
cd ~/engineering-docs-pipeline
python3 pipeline.py --test
```

**What happens:**
1. Scans OneDrive (counts PDF/DOCX/XLSX)
2. Picks first 50 files
3. Starts downloading 10 files to staging
4. As file 1 converts, file 11 starts downloading
5. Frees OneDrive space after each file is staged
6. Prints summary

**Time:** ~5-10 minutes.

**Check results:**
```bash
ls ~/engineering-md/          # See folder structure
python3 pipeline.py --status  # Progress breakdown
python3 pipeline.py --failed  # Any errors
```

### Step 6: Run the Full Pipeline

```bash
python3 pipeline.py
```

Resumes where `--test` left off. Subsequent runs only process new/changed files.

---

## Folder Structure Preserved

| Source | Output |
|--------|--------|
| `02. Work/Geotech/report.pdf` | `~/engineering-md/Geotech/report.md` |
| `02. Work/Subsea/pipeline.xlsx` | `~/engineering-md/Subsea/pipeline.md` |
| `02. Work/Specs/2024/spec.docx` | `~/engineering-md/Specs/2024/spec.md` |

The entire folder hierarchy is mirrored in `~/engineering-md/`. No filename collisions.

---

## Commands

| Command | What it does |
|---------|-------------|
| `--test` | Convert first 50 files |
| *(no args)* | Full run — resumes from checkpoint |
| `--status` | Show progress |
| `--scan-only` | Index files without converting |
| `--failed` | List failed files with errors |
| `--retry-failed` | Re-attempt only failed files |
| `--reset` | Start over |

---

## Configuration

Edit the top of `pipeline.py`:

| Setting | Default | What it does |
|---------|---------|-------------|
| `DOWNLOAD_AHEAD` | 10 | Files pre-fetched to staging before conversion |
| `BATCH_SIZE` | 50 | Files per batch (staging cleaned between batches) |
| `IO_WORKERS` | 4 | Parallel threads for downloading + staging |
| `MAX_RETRIES` | 3 | Retry attempts per file |
| `FREE_SPACE_AFTER_STAGING` | True | Free OneDrive space after copying to staging |
| `RSYNC_TARGET` | "" | Set to VPS address for auto-sync |

---

## Disk Usage During Run

| What | How much |
|------|----------|
| Staging files (peak) | `DOWNLOAD_AHEAD` × avg file size (~100MB) |
| Converted .md files | Same as source (but accumulates) |
| SQLite database | Negligible (< 10MB for 123k files) |

Staging is cleaned after each batch. .md files accumulate in `~/engineering-md/`.

---

## Incremental Updates (Nightly Cron)

```bash
crontab -e
# Add:
0 2 * * * cd ~/engineering-docs-pipeline && python3 pipeline.py >> ~/engineering-md/cron.log 2>&1
```

**Post-conversion filing:** After .md files are synced to VPS and ingested into gbrain, the folder structure on the VPS (`/data/engineering-docs/Geotech/`, `/data/engineering-docs/Subsea/`, etc.) is ready for browsing. gbrain indexes all files for semantic search regardless of folder layout. No additional filing step needed beyond what the pipeline already produces.

---

## After Conversion: gbrain Ingestion

```bash
# On VPS
gbrain import /data/engineering-docs/ --no-embed
gbrain embed --stale
```

---

## Troubleshooting

**"Source path does not exist"**
→ Check: `ls "/mnt/c/Users/Chris/OneDrive/02. Work/"` or `ls ~/work-docs/`

**"Hybrid server not running"**
→ Start Terminal 1 first

**"Download timeout"**
→ OneDrive network issue. File gets retried 3 times. Run `--retry-failed` later.

**Disk space filling up**
→ Check staging: `du -sh ~/engineering-md/.staging/`
→ Check `FREE_SPACE_AFTER_STAGING = True` in config
→ Increase `DOWNLOAD_AHEAD` if you see idle time during conversion
→ Decrease `DOWNLOAD_AHEAD` if staging is too large
