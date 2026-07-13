#!/usr/bin/env python3
"""
Engineering Docs Pipeline v5 — PDF + DOCX → Markdown
======================================================
Self-contained, self-logging, resume-safe converter for engineering documents.
Runs on G14 WSL2 with GPU-accelerated OCR for PDFs, MarkItDown for Word files.

- Skips .xlsx (no value in converting spreadsheets to MD)
- When .docx and .pdf share same folder+name, keeps .docx only
- No picture/image extraction — refer to originals for accuracy

Usage:
    python3 pipeline.py --test              # Test batch: 50 files, then stop
    python3 pipeline.py                     # Full run: resumes where it left off
    python3 pipeline.py --status            # Show progress summary
    python3 pipeline.py --failed            # List failed files
    python3 pipeline.py --retry-failed      # Re-attempt only failed files
"""

import argparse
import gc
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime, timedelta
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

# OneDrive source folder (WSL2 path)
# Create symlink first: ln -s "/mnt/c/Users/Chris/OneDrive/02. Work" ~/work-docs
_symlink = Path.home() / "work-docs"
ONEDRIVE_ROOT = str(_symlink) if _symlink.exists() else "/mnt/c/Users/Chris/OneDrive/02. Work"

# Where converted .md files are saved (preserves source folder structure)
MD_STORE = Path.home() / "engineering-md"

# Temporary staging area for downloads
STAGE_DIR = MD_STORE / ".staging"

# SQLite progress database
PROGRESS_DB = MD_STORE / ".pipeline.db"

# Log file
LOG_FILE = MD_STORE / "pipeline.log"

# Hybrid server (opendataloader-pdf GPU backend for PDFs)
HYBRID_URL = "http://localhost:5002"
HYBRID_PORT = 5002

# Parallel workers for file I/O
IO_WORKERS = 4

# Pre-fetch buffer: stages this many files ahead of conversion
DOWNLOAD_AHEAD = 10

# Files per batch
BATCH_SIZE = 50

# Test batch
TEST_BATCH_SIZE = 50

# Retry settings
MAX_RETRIES = 3
RETRY_DELAYS = [2, 8, 30]

# Minimum output file size (bytes) — anything smaller = failed
SMALL_FILE_THRESHOLD = 100

# rsync target (empty = skip)
RSYNC_TARGET = ""

# Free OneDrive space after staging?
FREE_SPACE_AFTER_STAGING = True

# File extensions
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {".docx"}   # Word only — no Excel
ALL_EXTENSIONS = PDF_EXTENSIONS | OFFICE_EXTENSIONS

# attrib.exe path for OneDrive space management
ATTRIB_EXE = "/mnt/c/Windows/system32/attrib.exe"

# =============================================================================
# STARTUP CHECK — Fail fast if venv not activated
# =============================================================================

_VENV_DIR = Path.home() / "pdf-convert-venv"
_VENV_SITE_PACKAGES = _VENV_DIR / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _VENV_SITE_PACKAGES.exists() and _VENV_SITE_PACKAGES not in Path(sys.executable).parts:
    # Check if we're running outside the venv
    if str(_VENV_SITE_PACKAGES) not in sys.path:
        _venv_python = str(_VENV_DIR / "bin" / "python3")
        print(f"\n{'='*60}")
        print("❌ WRONG PYTHON — You're not running inside the venv!")
        print(f"{'='*60}")
        print(f"Current:  {sys.executable}")
        print(f"Use this: {_venv_python}")
        print(f"{'='*60}")
        print(f"\nRun these commands first:")
        print(f"  cd ~/pdf-convert-venv && source bin/activate")
        print(f"  cd ~/engineering-docs-pipeline && python3 pipeline.py --test")
        print()
        sys.exit(1)

del _VENV_DIR, _VENV_SITE_PACKAGES

# =============================================================================
# LOGGING
# =============================================================================

def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging(LOG_FILE)

# =============================================================================
# DATABASE
# =============================================================================

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            rel_path    TEXT PRIMARY KEY,
            size_bytes  INTEGER,
            mtime       REAL,
            ext         TEXT,
            status      TEXT DEFAULT 'pending',
            md5_hash    TEXT,
            md_size     INTEGER,
            started_at  TEXT,
            finished_at TEXT,
            duration_s  REAL,
            error       TEXT,
            retry_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
    conn.commit()
    return conn

# =============================================================================
# ONEDRIVE SPACE MANAGEMENT
# =============================================================================

def free_onedrive_space(path: str):
    if not FREE_SPACE_AFTER_STAGING:
        return
    try:
        win_path = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True
        ).stdout.strip()
        subprocess.run([ATTRIB_EXE, "+U", "-P", win_path], capture_output=True, timeout=10)
    except Exception as e:
        log.debug(f"Could not free space: {e}")

def ensure_local(path: str, timeout: int = 120) -> bool:
    """Trigger OneDrive download by reading file. Returns True when data is local."""
    start = time.time()
    first_attempt = True
    while time.time() - start < timeout:
        try:
            with open(path, "rb") as f:
                data = f.read(4096)
            if data and data != b"\x00" * len(data):
                return True
        except (OSError, IOError):
            pass
        if first_attempt:
            first_attempt = False
        time.sleep(3)
    log.warning(f"Download timeout: {os.path.basename(path)}")
    return False

# =============================================================================
# STAGING
# =============================================================================

def stage_file(rel: str) -> Path | None:
    """Copy from OneDrive → staging, then free OneDrive space. Returns staged path."""
    src = Path(ONEDRIVE_ROOT) / rel
    dst = STAGE_DIR / rel
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() and dst.stat().st_size > 0:
        return dst
    if not ensure_local(str(src)):
        return None
    try:
        shutil.copy2(str(src), str(dst))
    except OSError as e:
        log.warning(f"Stage copy failed: {rel}: {e}")
        return None

    free_onedrive_space(str(src))
    return dst

def get_md_path(rel: str, md_store: Path) -> Path:
    """Preserve folder structure: Subfolder/report.pdf → Subfolder/report.md"""
    return md_store / Path(rel).with_suffix(".md")

# =============================================================================
# FILE SCANNING
# =============================================================================

def scan_files(root: str) -> list[tuple[str, int, float, str]]:
    results = []
    root_path = Path(root)
    if not root_path.exists():
        log.error(f"Source path does not exist: {root}")
        return results

    log.info(f"Scanning {root}...")
    log.info("  (First scan of 154GB OneDrive may take 5-15 min. Progress every 1,000 files.)")
    count = 0
    scan_start = time.time()
    for dirpath, _, filenames in os.walk(root_path):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue
            full = Path(dirpath) / fname
            try:
                st = full.stat()
                results.append((str(full.relative_to(root_path)), st.st_size, st.st_mtime, ext))
                count += 1
                if count % 1000 == 0:
                    elapsed = time.time() - scan_start
                    rate = count / elapsed if elapsed > 0 else 0
                    log.info(f"  Scanned {count:,} files ({rate:.0f} f/s)...")
            except OSError:
                continue

    elapsed = time.time() - scan_start
    total_gb = sum(r[1] for r in results) / (1024**3)
    log.info(f"Scan complete: {count:,} files ({total_gb:.1f} GB) in {elapsed:.0f}s "
             f"({count/elapsed:.0f} f/s)")
    return results

def queue_new_files(conn: sqlite3.Connection, files: list[tuple[str, int, float, str]]) -> int:
    """Add new/changed files. When .docx and .pdf share a stem+parent, keep .docx only."""
    # Build lookup: (parent_dir, stem) → preferred extension
    prefer: dict[tuple[str, str], tuple[str, int, float, str]] = {}
    for rel, size, mtime, ext in files:
        key = (str(Path(rel).parent), Path(rel).stem)
        existing = prefer.get(key)
        if existing is None:
            prefer[key] = (rel, size, mtime, ext)
        else:
            # .docx beats .pdf
            if ext == ".docx" and existing[3] == ".pdf":
                prefer[key] = (rel, size, mtime, ext)

    queued = 0
    for rel, size, mtime, ext in prefer.values():
        existing = conn.execute(
            "SELECT mtime, size_bytes, status FROM files WHERE rel_path=?", (rel,)
        ).fetchone()
        if existing:
            old_mtime, old_size, old_status = existing
            if old_mtime == mtime and old_size == size and old_status == "done":
                continue
            conn.execute(
                "UPDATE files SET mtime=?, size_bytes=?, status='pending', error=NULL WHERE rel_path=?",
                (mtime, size, rel))
            queued += 1
        else:
            conn.execute(
                "INSERT INTO files (rel_path, size_bytes, mtime, ext, status) VALUES (?,?,?,?,'pending')",
                (rel, size, mtime, ext))
            queued += 1
    conn.commit()
    return queued

# =============================================================================
# HYBRID SERVER CHECK
# =============================================================================

def check_hybrid_server(port: int = HYBRID_PORT) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False

# =============================================================================
# CONVERTERS
# =============================================================================

def convert_pdf(pdf_path: Path, output_path: Path) -> dict:
    result = {"status": "done", "md_size": 0, "error": None}
    try:
        from opendataloader_pdf import convert
        output_path.parent.mkdir(parents=True, exist_ok=True)
        convert(
            input_path=[str(pdf_path)], output_dir=str(output_path.parent),
            format="markdown", hybrid="docling-fast", hybrid_mode="full",
            hybrid_url=HYBRID_URL, quiet=True,
        )
        if output_path.exists():
            result["md_size"] = output_path.stat().st_size
            if result["md_size"] < SMALL_FILE_THRESHOLD:
                result["status"] = "tiny"
                result["error"] = f"Output {result['md_size']}B"
        else:
            result["status"] = "missing"
            result["error"] = "No .md produced"
    except ImportError:
        result["status"] = "error"
        result["error"] = "opendataloader_pdf not installed"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
    return result

def convert_office(file_path: Path, output_path: Path) -> dict:
    result = {"status": "done", "md_size": 0, "error": None}
    try:
        from markitdown import MarkItDown
        md_content = MarkItDown().convert(str(file_path)).text_content
        if not md_content or len(md_content.strip()) < 20:
            result["status"] = "tiny"
            result["error"] = f"Only {len(md_content)} chars"
            return result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(md_content, encoding="utf-8")
        result["md_size"] = output_path.stat().st_size
    except ImportError:
        result["status"] = "error"
        result["error"] = "markitdown not installed"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
    return result

def convert_file(file_path: Path, output_path: Path, ext: str) -> dict:
    if ext == ".pdf":
        return convert_pdf(file_path, output_path)
    elif ext == ".docx":
        return convert_office(file_path, output_path)
    return {"status": "skipped", "md_size": 0, "error": f"Unsupported: {ext}"}

# =============================================================================
# LIVE PROGRESS
# =============================================================================

def _live_line(text: str):
    """Overwrite current terminal line with progress info."""
    width = shutil.get_terminal_size((120, 40)).columns
    sys.stderr.write(f"\r\033[K{text[:width]}")
    sys.stderr.flush()

def _format_size(b: int) -> str:
    if b < 1024:
        return f"{b}B"
    elif b < 1024**2:
        return f"{b/1024:.1f}KB"
    elif b < 1024**3:
        return f"{b/1024**2:.1f}MB"
    return f"{b/1024**3:.2f}GB"

class Progress:
    """Tracks live progress for bash display."""
    def __init__(self, total_pending: int, total_chunks: int):
        self.total = total_pending
        self.total_chunks = total_chunks
        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self.start_time = time.time()
        self.last_file = ""
        self.staging_count = 0
        self.chunk_current = 0
        self.chunk_total = 0
        self.chunk_done = 0

    @property
    def done(self) -> int:
        return self.converted + self.skipped + self.failed

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def eta(self) -> str:
        if self.done == 0:
            return "--:--"
        rate = self.done / self.elapsed
        remaining = (self.total - self.done) / rate if rate > 0 else 0
        return str(timedelta(seconds=int(remaining)))

    @property
    def rate_str(self) -> str:
        if self.done == 0:
            return "--"
        rate = self.done / (self.elapsed / 60)
        return f"{rate:.1f} f/m"

    def update(self, last_file: str = "", converted: int = -1, skipped: int = -1,
               failed: int = -1, staging_count: int = -1, chunk_current: int = -1,
               chunk_done: int = -1, chunk_total: int = -1):
        if converted >= 0:
            self.converted = converted
        if skipped >= 0:
            self.skipped = skipped
        if failed >= 0:
            self.failed = failed
        if last_file:
            # Truncate for display
            if len(last_file) > 60:
                last_file = "…" + last_file[-59:]
            self.last_file = last_file
        if staging_count >= 0:
            self.staging_count = staging_count
        if chunk_current >= 0:
            self.chunk_current = chunk_current
        if chunk_done >= 0:
            self.chunk_done = chunk_done
        if chunk_total >= 0:
            self.chunk_total = chunk_total

    def draw(self):
        pct = (self.done / self.total * 100) if self.total > 0 else 0
        bar_width = 20
        filled = int(bar_width * self.done / self.total) if self.total > 0 else 0
        bar = "█" * filled + "░" * (bar_width - filled)

        if self.chunk_total > 0:
            chunk_info = f"chunk {self.chunk_current}/{self.total_chunks} [{self.chunk_done}/{self.chunk_total}]"
        else:
            chunk_info = ""

        line = (
            f"[{bar}] {pct:5.1f}% "
            f"✓{self.converted} ⏭{self.skipped} ✗{self.failed} "
            f"⏱{self.rate_str}  ETA {self.eta} "
            f"⬇{self.staging_count} "
            f"{chunk_info} "
            f"{self.last_file}"
        )
        _live_line(line)

# =============================================================================
# PROGRESS REPORTING
# =============================================================================

def print_status(conn: sqlite3.Connection):
    stats = conn.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(size_bytes),0) FROM files GROUP BY status"
    ).fetchall()
    if not stats:
        print("\nNo files scanned yet.")
        return

    total_files = sum(r[1] for r in stats)
    total_size = sum(r[2] for r in stats)
    by_ext = conn.execute(
        "SELECT ext, status, COUNT(*) FROM files GROUP BY ext, status ORDER BY ext, status"
    ).fetchall()

    print(f"\n{'='*60}")
    print("ENGINEERING DOCS PIPELINE — STATUS")
    print(f"{'='*60}")
    print(f"Source:  {ONEDRIVE_ROOT}")
    print(f"Output:  {MD_STORE}")
    print(f"{'='*60}")
    for status, count, size in stats:
        gb = size / (1024**3)
        pct = (count / total_files * 100) if total_files else 0
        print(f"  {status:12s}: {count:>7,} files ({pct:5.1f}%) — {gb:,.2f} GB")
    print(f"  {'TOTAL':12s}: {total_files:>7,} files — {total_size/(1024**3):,.2f} GB")
    print(f"{'='*60}")

    print("\nBy format:")
    cur = None
    for ext, status, count in by_ext:
        if ext != cur:
            if cur:
                print()
            print(f"  {ext}:", end="")
            cur = ext
        print(f"  {status}={count}", end="")
    print()

def list_failed(conn: sqlite3.Connection):
    failed = conn.execute(
        "SELECT rel_path, ext, error, retry_count FROM files "
        "WHERE status IN ('error','missing','tiny') ORDER BY rel_path"
    ).fetchall()
    if not failed:
        print("No failed files.")
        return
    print(f"\nFailed files ({len(failed)}):")
    for path, ext, err, retries in failed:
        print(f"  [{ext}] {path}")
        if err:
            print(f"    → {err[:120]} (retries: {retries})")

def print_summary(start_time: float, converted: int, skipped: int, failed: int,
                  total_bytes: int, progress: Progress):
    elapsed = time.time() - start_time
    elapsed_str = str(timedelta(seconds=int(elapsed)))
    gb = total_bytes / (1024**3)

    _live_line("")  # Clear progress line
    print(f"\n{'='*60}")
    print(f"RUN COMPLETE — {elapsed_str}")
    print(f"{'='*60}")
    print(f"  Converted             : {converted:,}")
    print(f"  Skipped (existing)    : {skipped:,}")
    print(f"  Failed                : {failed:,}")
    print(f"  Data processed        : {gb:,.2f} GB")
    if converted > 0:
        avg = elapsed / converted
        print(f"  Avg time/file         : {avg:.1f}s ({60/avg:.1f} files/min)")
    print(f"{'='*60}")
    if failed > 0:
        print(f"\nRun with --failed to see details.")
        print(f"Run with --retry-failed to re-attempt.")

# =============================================================================
# ASYNC STAGING HELPERS
# =============================================================================

def _stage_if_needed(rel: str, staged: dict[str, Path | None],
                     staging_futures: dict[Future, str], executor: ThreadPoolExecutor):
    """Submit a stage task without blocking. No-op if already staged or in-flight."""
    if rel in staged:
        return
    # Check if already submitted as a future
    for f, r in staging_futures.items():
        if r == rel:
            return
    f = executor.submit(stage_file, rel)
    staging_futures[f] = rel

def _collect_staged(staged: dict[str, Path | None],
                    staging_futures: dict[Future, str]) -> int:
    """Collect completed futures into staged dict. Returns how many completed."""
    done = 0
    for f in list(staging_futures.keys()):
        if not f.done():
            continue
        rel = staging_futures.pop(f)
        try:
            staged[rel] = f.result(timeout=10)
        except Exception:
            staged[rel] = None
        done += 1
    return done

def _ensure_staged(rel: str, staged: dict[str, Path | None],
                   staging_futures: dict[Future, str]) -> Path | None:
    """Wait for a specific file to finish staging. Returns staged path or None."""
    if rel in staged:
        return staged[rel]
    # Find and wait on its future
    for f, r in list(staging_futures.items()):
        if r == rel:
            try:
                staged[rel] = f.result(timeout=300)
            except Exception:
                staged[rel] = None
            staging_futures.pop(f)
            return staged.get(rel)
    # Not in flight — stage it now (blocking)
    staged[rel] = stage_file(rel)
    return staged[rel]

# =============================================================================
# MAIN PIPELINE
# =============================================================================

def run_pipeline(test_mode: bool = False, retry_failed: bool = False,
                 scan_only: bool = False):
    conn = init_db(PROGRESS_DB)
    mode = "test" if test_mode else "full"
    start_time = time.time()

    log.info(f"{'='*60}")
    log.info(f"PIPELINE START — mode={mode}")
    log.info(f"Source:  {ONEDRIVE_ROOT}")
    log.info(f"Output:  {MD_STORE}")
    log.info(f"Staging: {STAGE_DIR} (ahead={DOWNLOAD_AHEAD})")
    log.info(f"{'='*60}")

    # ── Phase 1: SCAN ──
    if not retry_failed:
        log.info("Phase 1: Scanning...")
        files = scan_files(ONEDRIVE_ROOT)
        if not files:
            log.error("No files found.")
            conn.close()
            return
        queued = queue_new_files(conn, files)
        log.info(f"Queued {queued:,} new/changed files")
        del files; gc.collect()

    if scan_only:
        print_status(conn); conn.close(); return

    # ── Phase 2: CHECK HYBRID ──
    has_pdfs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE ext='.pdf' AND status='pending'"
    ).fetchone()[0] > 0

    if has_pdfs and not check_hybrid_server():
        log.error(f"Hybrid server not running on port {HYBRID_PORT}!")
        log.error("Start in another terminal:")
        log.error(f"  cd ~/pdf-convert-venv && source bin/activate")
        log.error(f"  opendataloader-pdf-hybrid --port {HYBRID_PORT} \\")
        log.error(f"    --force-ocr --ocr-engine rapidocr --device cuda \\")
        log.error(f"    --enrich-formula")
        sys.exit(1)

    if has_pdfs:
        log.info("Hybrid server ✓")

    # ── Phase 3: QUEUE ──
    if retry_failed:
        pending = conn.execute(
            "SELECT rel_path, ext, size_bytes FROM files "
            "WHERE status IN ('error','missing','tiny')"
        ).fetchall()
        log.info(f"Retrying {len(pending):,} failed files")
    else:
        pending = conn.execute(
            "SELECT rel_path, ext, size_bytes FROM files "
            "WHERE status='pending' ORDER BY rel_path"
        ).fetchall()

    if not pending:
        log.info("No pending files.")
        print_status(conn); conn.close(); return

    if test_mode and len(pending) > TEST_BATCH_SIZE:
        log.info(f"TEST MODE: {TEST_BATCH_SIZE} files")
        pending = pending[:TEST_BATCH_SIZE]

    total_pending = len(pending)
    log.info(f"To convert: {total_pending:,} files "
             f"({sum(r[2] for r in pending) / (1024**3):.2f} GB)")

    # ── Phase 4: CONVERT (async pre-fetch) ──
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    MD_STORE.mkdir(parents=True, exist_ok=True)

    converted = 0; skipped = 0; failed_files = 0; total_bytes = 0
    total_chunks = (total_pending + BATCH_SIZE - 1) // BATCH_SIZE
    progress = Progress(total_pending, total_chunks)

    for chunk_start in range(0, total_pending, BATCH_SIZE):
        chunk = pending[chunk_start:chunk_start + BATCH_SIZE]
        chunk_num = (chunk_start // BATCH_SIZE) + 1

        log.info(f"Batch {chunk_num}/{total_chunks} ({len(chunk)} files)")

        staged: dict[str, Path | None] = {}
        staging_futures: dict[Future, str] = {}

        with ThreadPoolExecutor(max_workers=IO_WORKERS) as executor:
            # ── Kick off first DOWNLOAD_AHEAD stages (non-blocking) ──
            for i in range(min(DOWNLOAD_AHEAD, len(chunk))):
                rel = chunk[i][0]
                _stage_if_needed(rel, staged, staging_futures, executor)

            # ── Process files ──
            for i, (rel, ext, file_size) in enumerate(chunk):
                # Submit stage for file DOWNLOAD_AHEAD ahead (non-blocking)
                ahead = i + DOWNLOAD_AHEAD
                if ahead < len(chunk):
                    _stage_if_needed(chunk[ahead][0], staged, staging_futures, executor)

                # Collect any completed stages
                _collect_staged(staged, staging_futures)

                # Wait for THIS file's staging to complete
                staged_path = _ensure_staged(rel, staged, staging_futures)

                if not staged_path:
                    # Staging failed — retry
                    for attempt in range(MAX_RETRIES - 1):
                        time.sleep(RETRY_DELAYS[attempt])
                        staged_path = stage_file(rel)
                        if staged_path:
                            staged[rel] = staged_path
                            break
                    if not staged_path:
                        failed_files += 1
                        conn.execute(
                            "UPDATE files SET status='error', error='Staging failed', "
                            "retry_count=retry_count+1 WHERE rel_path=?",
                            (rel,))
                        progress.update(failed=failed_files, last_file=Path(rel).name)
                        progress.draw()
                        continue

                # Check for existing MD
                md_path = get_md_path(rel, MD_STORE)
                if md_path.exists() and md_path.stat().st_size > SMALL_FILE_THRESHOLD:
                    skipped += 1
                    conn.execute(
                        "UPDATE files SET status='done', md_size=?, finished_at=? WHERE rel_path=?",
                        (md_path.stat().st_size, datetime.now().isoformat(), rel))
                    progress.update(skipped=skipped, staging_count=len(staging_futures),
                                    chunk_current=chunk_num, chunk_done=i+1,
                                    chunk_total=len(chunk), last_file=Path(rel).name)
                    progress.draw()
                    continue

                # Convert (with retry)
                result = None
                for attempt in range(MAX_RETRIES):
                    t0 = time.time()
                    result = convert_file(staged_path, md_path, ext)
                    result["duration_s"] = round(time.time() - t0, 2)

                    if result["status"] == "done":
                        break
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(RETRY_DELAYS[attempt])

                if result is None:
                    result = {"status": "error", "md_size": 0, "duration_s": 0,
                              "error": "All retries exhausted"}

                # Update database
                conn.execute(
                    "UPDATE files SET status=?, md_size=?, finished_at=?, duration_s=?, "
                    "error=? WHERE rel_path=?",
                    (result["status"], result.get("md_size", 0),
                     datetime.now().isoformat(), result.get("duration_s", 0),
                     result.get("error"), rel))

                if result["status"] == "done":
                    converted += 1
                    total_bytes += file_size
                else:
                    failed_files += 1
                    conn.execute(
                        "UPDATE files SET retry_count=retry_count+1 WHERE rel_path=?",
                        (rel,))

                # Live progress update
                progress.update(
                    converted=converted, skipped=skipped, failed=failed_files,
                    staging_count=len(staging_futures),
                    chunk_current=chunk_num, chunk_done=i+1,
                    chunk_total=len(chunk), last_file=Path(rel).name)
                progress.draw()

        conn.commit()

        # Cleanup staging — files AND directories
        for p in staged.values():
            if p and p.exists():
                try: p.unlink()
                except OSError: pass
        # Remove empty staging subdirs (bottom-up)
        for root, dirs, _ in os.walk(str(STAGE_DIR), topdown=False):
            for d in dirs:
                dpath = Path(root) / d
                try: dpath.rmdir()
                except OSError: pass

        gc.collect()

    # ── Clear progress line ──
    _live_line("")

    # ── Phase 5: RSYNC ──
    if RSYNC_TARGET and total_bytes > 0:
        log.info(f"Syncing to {RSYNC_TARGET}...")
        try:
            subprocess.run(
                ["rsync", "-avz", str(MD_STORE) + "/", RSYNC_TARGET],
                capture_output=True, text=True, timeout=600)
            log.info("rsync ✓")
        except Exception as e:
            log.error(f"rsync failed: {e}")

    # ── Phase 6: CLEANUP ──
    try:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
    except Exception:
        pass

    # ── Phase 7: SUMMARY ──
    print_summary(start_time, converted, skipped, failed_files, total_bytes, progress)
    print_status(conn)
    conn.close()

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Engineering Docs Pipeline — PDF/DOCX/XLSX → Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
    python3 pipeline.py --test         Test with 50 files
    python3 pipeline.py                Full run (resumes)
    python3 pipeline.py --status       Show progress
    python3 pipeline.py --scan-only    Index without converting
    python3 pipeline.py --retry-failed Retry failures""")
    parser.add_argument("--test", action="store_true",
                        help=f"Test batch: first {TEST_BATCH_SIZE} files")
    parser.add_argument("--status", action="store_true", help="Show progress")
    parser.add_argument("--failed", action="store_true", help="List failed files")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failures")
    parser.add_argument("--scan-only", action="store_true", help="Index without converting")
    parser.add_argument("--reset", action="store_true", help="Start over")

    args = parser.parse_args()

    if args.reset:
        confirm = input("Reset all progress? [y/N] ")
        if confirm.lower() == "y" and PROGRESS_DB.exists():
            PROGRESS_DB.unlink()
            print("Database deleted.")
        return

    conn = init_db(PROGRESS_DB)

    if args.status:
        print_status(conn); conn.close(); return
    if args.failed:
        list_failed(conn); conn.close(); return
    conn.close()

    run_pipeline(test_mode=args.test, retry_failed=args.retry_failed,
                 scan_only=args.scan_only)

if __name__ == "__main__":
    main()
