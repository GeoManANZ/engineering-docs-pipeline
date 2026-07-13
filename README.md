# Engineering Docs Pipeline v2

Multi-format document converter for engineering libraries. Converts PDF, DOCX, and XLSX files to Markdown with GPU-accelerated OCR, formula extraction, and OneDrive space management.

## What It Does

| Format | Converter | Key Features |
|--------|-----------|-------------|
| **PDF** | opendataloader-pdf hybrid | GPU OCR, LaTeX formula extraction, 92.8% table accuracy |
| **DOCX** | MarkItDown (Microsoft) | Preserves headings, tables, lists |
| **XLSX** | MarkItDown | Converts spreadsheets to Markdown tables |

**Designed for:**
- 154GB / 123,000 files / 3,700 subfolders
- OneDrive Files On-Demand (download → convert → free space)
- Incremental processing (only new/changed files)
- Resume-safe (SQLite tracking, survives crashes)

**Note:** Only `.pdf`, `.docx`, and `.xlsx` are supported. Legacy formats (`.doc`, `.xls`, `.ppt`) are skipped. Convert those manually first if needed.

## Prerequisites

- G14 laptop with WSL2 (Ubuntu)
- NVIDIA GPU + CUDA drivers (RTX 5070 Ti or similar)
- Python 3.10+
- Java 17+ (`sudo apt install openjdk-17-jdk`)
- OneDrive folder accessible from WSL2

## Quick Start

### 1. Clone and Setup

```bash
git clone <repo-url>
cd engineering-docs-pipeline
bash setup.sh
```

### 2. Create Symlink (Optional but Recommended)

Avoids quoting spaces in every command:

```bash
ln -s "/mnt/c/Users/Chris/OneDrive/02. Work" ~/work-docs
```

If using symlink, update `ONEDRIVE_ROOT` in `pipeline.py`:

```python
ONEDRIVE_ROOT = os.path.expanduser("~/work-docs")
```

### 3. Start Hybrid Server (Terminal 1)

The hybrid server loads AI models into GPU memory and handles PDF OCR, formula extraction, and table recognition. **Must be running before processing PDFs.**

```bash
cd ~/pdf-convert-venv
source bin/activate
opendataloader-pdf-hybrid --port 5002 \
  --force-ocr --ocr-engine rapidocr --device cuda \
  --enrich-formula --enrich-picture-description
```

**Leave this terminal open.** It stays running until you close it.

### 4. Run Pipeline (Terminal 2)

```bash
cd ~/engineering-docs-pipeline

# Test batch — 50 files, then stop
python3 pipeline.py --test

# If test succeeds, run full pipeline
python3 pipeline.py
```

## All Commands

| Command | What it does |
|---------|-------------|
| `--test` | Convert first 50 files only (test quality) |
| *(no args)* | Full run — resumes from last checkpoint |
| `--status` | Show progress without converting |
| `--scan-only` | Index files without converting |
| `--failed` | List failed files with error details |
| `--retry-failed` | Re-attempt only failed files |
| `--reset` | Delete tracking database (start over) |

## How It Works

```
Phase 1: SCAN
  └── os.walk OneDrive → SQLite (path, size, mtime)
  └── Only queues new/changed files

Phase 2: DOWNLOAD
  └── Triggers OneDrive sync for each file
  └── Waits for download (120s timeout)

Phase 3: CONVERT (parallel, 4 workers)
  └── PDF → opendataloader-pdf hybrid (GPU)
  └── DOCX/XLSX → MarkItDown

Phase 4: FREE SPACE
  └── attrib.exe +U -P (mark as online-only)
  └── Reclaims local disk space

Phase 5: RSYNC (optional)
  └── Push .md files to VPS

Phase 6: REPORT
  └── Summary to console + log file
```

## Incremental Updates

After the initial run, just run `python3 pipeline.py` again. It:

1. Re-scans the OneDrive folder
2. Compares `(mtime, size)` against the database
3. Only processes new or modified files
4. Skips everything already converted

**Suggested cron for nightly incremental:**

```bash
# Run at 2am every night
0 2 * * * cd ~/engineering-docs-pipeline && python3 pipeline.py >> ~/engineering-md/cron.log 2>&1
```

## OneDrive Space Management

The pipeline automatically frees local disk space after converting each file:

```bash
attrib.exe +U -P "C:\Users\Chris\OneDrive\02. Work\file.pdf"
```

- `+U` = unpin (mark as online-only)
- `-P` = clear "always available" flag

**This means:** files download temporarily, get converted, then OneDrive reclaims the space. You don't need 154GB free disk space — just enough for the current batch.

## Why These Tools?

### PDF: opendataloader-pdf hybrid

| Metric | Score | Why it matters |
|--------|-------|---------------|
| Overall accuracy | 0.907 | #1 of 12 tested |
| Table extraction | 0.928 | Engineering specs = tables |
| Reading order | 0.934 | Multi-column layouts |
| Formula extraction | LaTeX | Equations become searchable |
| Speed | 0.46s/page | GPU-accelerated |

### DOCX/XLSX: MarkItDown

- Built by Microsoft
- Wraps mammoth (DOCX) + openpyxl (XLSX)
- MIT license, lightweight
- Good quality for structured documents

## Output

| File | Location |
|------|----------|
| Converted files | `~/engineering-md/*.md` |
| Progress database | `~/engineering-md/.pipeline.db` |
| Log file | `~/engineering-md/pipeline.log` |

## After Conversion: gbrain Ingestion

```bash
# On VPS
gbrain import /data/engineering-docs/ --no-embed
gbrain embed --stale
```

## Troubleshooting

**"Hybrid server not running"**
→ Start Terminal 1 first. The server must be running before you start the pipeline.

**"Download timeout"**
→ OneDrive may be syncing. Wait a few minutes and retry with `--retry-failed`.

**"No files found"**
→ Check `ONEDRIVE_ROOT` path. Test with: `ls "/mnt/c/Users/Chris/OneDrive/02. Work/"`

**Conversion is slow**
→ Normal for accuracy mode. Check GPU with `nvidia-smi`. PDFs take 0.5-2s/page.

**Pipeline crashed mid-run**
→ Just run it again. It resumes from the last completed file.

**Disk space filling up**
→ Check if `FREE_SPACE_AFTER_CONVERT = True` in config. Verify `attrib.exe` works:
```bash
/mnt/c/Windows/system32/attrib.exe +U -P "/mnt/c/Users/Chris/OneDrive/02. Work/test.pdf"
```

## Architecture

```
┌─ G14 (WSL2) ──────────────────────────────────────────────┐
│                                                             │
│  Terminal 1: Hybrid Server (GPU)                            │
│  └── opendataloader-pdf-hybrid --port 5002                  │
│      --enrich-formula --enrich-picture-description          │
│                                                             │
│  Terminal 2: Pipeline                                       │
│  └── pipeline.py --test (or full)                           │
│      ├── Scan → SQLite tracking                             │
│      ├── Download → OneDrive sync                           │
│      ├── Convert (parallel workers)                         │
│      │   ├── PDF → opendataloader (GPU)                     │
│      │   ├── DOCX → MarkItDown                              │
│      │   └── XLSX → MarkItDown                              │
│      ├── Free space → attrib.exe                            │
│      └── Report → log + console                             │
│                                                             │
│  ~/engineering-md/*.md  ← Output                            │
└──────────────────────────────┬──────────────────────────────┘
                               │ rsync (optional)
                               ▼
┌─ VPS ──────────────────────────────────────────────────────┐
│  gbrain import → gbrain embed → semantic search             │
└─────────────────────────────────────────────────────────────┘
```

## License

- opendataloader-pdf: Apache 2.0
- MarkItDown: MIT
- Pipeline script: MIT
