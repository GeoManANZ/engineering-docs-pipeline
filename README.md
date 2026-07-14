# Engineering Docs Pipeline v6 — Batch Optimized

PDF + DOCX → Markdown converter with GPU-accelerated OCR, formula extraction, pre-fetch staging, and OneDrive space management.

## What Changed in v6 (Major Performance Update)

| v5 | v6 |
|----|----|
| 1 PDF per `convert()` call | 100 PDFs per batch call |
| Blanket `--force-ocr` on everything | Scanned vs digital classifier (pypdf) |
| Single hybrid server | Two servers: digital (5002) + scanned (5003) |
| `hybrid_mode="full"` on all docs | "full" only for calcs, "auto" for rest |
| 4KB zero-byte hydration check | Size-comparison hydration |
| Per-file convert | Batch stage → batch convert → batch free |

**Expected speedup:** 3-8x (from ~2.8 f/m to ~10-22 f/m)

## Quick Start

```bash
git clone git@github.com:GeoManANZ/engineering-docs-pipeline.git
cd engineering-docs-pipeline
bash setup.sh
python3 pipeline.py --test
```

## Two-Server Setup

You now need **three terminals**:

**Terminal 1 — Digital PDF server (port 5002):**
```bash
cd ~/pdf-convert-venv && source bin/activate
opendataloader-pdf-hybrid --port 5002 \
  --device cuda --enrich-formula
```

**Terminal 2 — Scanned PDF server (port 5003):**
```bash
cd ~/pdf-convert-venv && source bin/activate
opendataloader-pdf-hybrid --port 5003 \
  --force-ocr --ocr-engine rapidocr --device cuda
```

**Terminal 3 — Pipeline:**
```bash
cd ~/pdf-convert-venv && source bin/activate
cd ~/engineering-docs-pipeline
python3 pipeline.py --test
```

## Commands

| Command | What it does |
|---------|-------------|
| `--test` | Convert first 50 files |
| *(no args)* | Full run (resumes) |
| `--status` | Show progress |
| `--scan-only` | Index without converting |
| `--failed` | List failed files |
| `--retry-failed` | Retry failures |

## Configuration

Edit the top of `pipeline.py`:

| Setting | Default | What it does |
|---------|---------|-------------|
| `PDF_BATCH` | 100 | PDFs per opendataloader call |
| `BATCH_SIZE` | 150 | Files per outer chunk |
| `DOWNLOAD_AHEAD` | 25 | Files pre-fetched before conversion |
| `IO_WORKERS` | 6 | Parallel staging threads |
| `CALC_PATTERNS` | See config | Paths that trigger full formula extraction |

## OneDrive Space

Files are staged locally, converted, then OneDrive space is freed. Peak disk usage: ~2-3GB during active conversion. After each batch: staging cleaned. Final output: `~/engineering-md/`.

## Post-Conversion: gbrain

```bash
# rsync to VPS
rsync -avz ~/engineering-md/ user@vps:/data/engineering-docs/

# On VPS
gbrain import /data/engineering-docs/ --no-embed
gbrain embed --stale
```
