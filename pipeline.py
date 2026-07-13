#!/usr/bin/env python3
"""
Engineering Docs Pipeline v2 — Multi-format PDF/DOCX/XLSX → Markdown
=====================================================================
Self-contained, self-logging, resume-safe converter for engineering documents.
Runs on G14 WSL2 with GPU-accelerated OCR for PDFs, MarkItDown for Office files.

Usage:
    python3 pipeline.py --test              # Test batch: 50 files, then stop
    python3 pipeline.py                     # Full run: resumes where it left off
    python3 pipeline.py --status            # Show progress summary
    python3 pipeline.py --failed            # List failed files
    python3 pipeline.py --retry-failed      # Re-attempt only failed files
    python3 pipeline.py --reset             # Reset all progress (start over)
    python3 pipeline.py --scan-only         # Index files without converting
"""

import argparse
import gc
import logging
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

# =============================================================================
# CONFIGURATION
# =============================================================================

# OneDrive source folder (WSL2 path)
# Symlink recommended: ln -s "/mnt/c/Users/Chris/OneDrive/02. Work" ~/work-docs
ONEDRIVE_ROOT = "/mnt/c/Users/Chris/OneDrive/02. Work"

# Where converted .md files are saved
MD_STORE = Path.home() / "engineering-md"

# SQLite progress database
PROGRESS_DB = MD_STORE / ".pipeline.db"

# Log file
LOG_FILE = MD_STORE / "pipeline.log"

# Hybrid server (opendataloader-pdf GPU backend for PDFs)
HYBRID_URL = "http://localhost:5002"
HYBRID_PORT = 5002

# Parallel workers for file I/O (downloading from OneDrive)
IO_WORKERS = 4

# Files per batch (controls memory and rsync frequency)
BATCH_SIZE = 500

# Test batch: how many files in --test mode
TEST_BATCH_SIZE = 50

# Retry settings
MAX_RETRIES = 3
RETRY_DELAYS = [2, 8, 30]  # exponential backoff in seconds

# Minimum output file size (bytes) — anything smaller is a failed conversion
SMALL_FILE_THRESHOLD = 100

# rsync target (set to empty string to skip)
RSYNC_TARGET = ""

# Free OneDrive space after successful conversion?
FREE_SPACE_AFTER_CONVERT = True

# File extensions to process
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {".docx", ".xlsx"}
ALL_EXTENSIONS = PDF_EXTENSIONS | OFFICE_EXTENSIONS

# attrib.exe path for OneDrive space management
ATTRIB_EXE = "/mnt/c/Windows/system32/attrib.exe"

# =============================================================================
# LOGGING
# =============================================================================

def setup_logging(log_file: Path) -> logging.Logger:
    """Dual logging: file (detailed) + console (info+)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.DEBUG)
    # Prevent duplicate handlers on reload
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
    """Create or open the progress database with WAL mode."""
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
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_status ON files(status)
    """)
    conn.commit()
    return conn

# =============================================================================
# ONEDRIVE SPACE MANAGEMENT
# =============================================================================

def free_onedrive_space(path: str):
    """Mark a file as online-only (free local disk space)."""
    if not FREE_SPACE_AFTER_CONVERT:
        return
    try:
        # attrib.exe works from WSL2 on Windows paths
        win_path = subprocess.run(
            ["wslpath", "-w", path], capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(
            [ATTRIB_EXE, "+U", "-P", win_path],
            capture_output=True, timeout=10
        )
    except Exception as e:
        log.debug(f"Could not free space for {path}: {e}")

def ensure_local(path: str, timeout: int = 120) -> bool:
    """
    Ensure a OneDrive file is downloaded locally.
    Cloud-only placeholders may report a fake file size on WSL2, so we always
    attempt to read the actual bytes. OneDrive downloads on access.
    Returns True once we can read real data.
    """
    start = time.time()
    first_attempt = True
    while time.time() - start < timeout:
        try:
            with open(path, "rb") as f:
                data = f.read(4096)  # Enough to trigger full download
            if data and data != b"\x00" * len(data):
                return True
        except (OSError, IOError):
            pass

        if first_attempt:
            log.info(f"  Waiting for OneDrive: {os.path.basename(path)}")
            first_attempt = False
        time.sleep(3)

    log.warning(f"  Download timeout ({timeout}s): {os.path.basename(path)}")
    return False

# =============================================================================
# FILE SCANNING
# =============================================================================

def scan_files(root: str) -> list[tuple[str, int, float, str]]:
    """
    Walk the source folder and return (rel_path, size, mtime, ext) for all
    supported files (.pdf, .docx, .xlsx).
    """
    results = []
    root_path = Path(root)
    if not root_path.exists():
        log.error(f"Source path does not exist: {root}")
        return results

    log.info(f"Scanning {root}...")
    count = 0
    for dirpath, dirnames, filenames in os.walk(root_path):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue
            full = Path(dirpath) / fname
            try:
                st = full.stat()
                rel = str(full.relative_to(root_path))
                results.append((rel, st.st_size, st.st_mtime, ext))
                count += 1
                if count % 10000 == 0:
                    log.info(f"  Scanned {count:,} files...")
            except OSError:
                continue  # Skip inaccessible files

    log.info(f"Scan complete: {count:,} files ({sum(r[1] for r in results) / (1024**3):.1f} GB)")
    return results

def queue_new_files(conn: sqlite3.Connection, files: list[tuple[str, int, float, str]]) -> int:
    """Add new/changed files to the tracking database."""
    queued = 0
    for rel, size, mtime, ext in files:
        # Check if file exists and is unchanged
        existing = conn.execute(
            "SELECT mtime, size_bytes, status FROM files WHERE rel_path=?", (rel,)
        ).fetchone()

        if existing:
            # File exists — check if changed
            old_mtime, old_size, old_status = existing
            if old_mtime == mtime and old_size == size and old_status == "done":
                continue  # Unchanged, already converted
            # Changed — update and re-queue
            conn.execute(
                "UPDATE files SET mtime=?, size_bytes=?, status='pending', error=NULL WHERE rel_path=?",
                (mtime, size, rel)
            )
            queued += 1
        else:
            # New file
            conn.execute(
                "INSERT INTO files (rel_path, size_bytes, mtime, ext, status) VALUES (?, ?, ?, ?, 'pending')",
                (rel, size, mtime, ext)
            )
            queued += 1

    conn.commit()
    return queued

# =============================================================================
# HYBRID SERVER CHECK
# =============================================================================

def check_hybrid_server(port: int = HYBRID_PORT) -> bool:
    """Check if the opendataloader-pdf hybrid server is running."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False

# =============================================================================
# CONVERTERS
# =============================================================================

def convert_pdf(pdf_path: Path, output_dir: Path) -> dict:
    """Convert PDF using opendataloader-pdf hybrid mode (GPU OCR)."""
    result = {"status": "done", "md_size": 0, "error": None}
    try:
        from opendataloader_pdf import convert
        convert(
            input_path=[str(pdf_path)],
            output_dir=str(output_dir),
            format="markdown",
            hybrid="docling-fast",
            hybrid_mode="full",
            hybrid_url=HYBRID_URL,
            quiet=True,
        )
        md_path = output_dir / (pdf_path.stem + ".md")
        if md_path.exists():
            result["md_size"] = md_path.stat().st_size
            if result["md_size"] < SMALL_FILE_THRESHOLD:
                result["status"] = "tiny"
                result["error"] = f"Output only {result['md_size']}B"
        else:
            result["status"] = "missing"
            result["error"] = "No .md file produced"
    except ImportError:
        result["status"] = "error"
        result["error"] = "opendataloader_pdf not installed"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
    return result

def convert_office(file_path: Path, output_dir: Path) -> dict:
    """Convert DOCX/XLSX using MarkItDown."""
    result = {"status": "done", "md_size": 0, "error": None}
    try:
        from markitdown import MarkItDown
        m = MarkItDown()
        md_result = m.convert(str(file_path))
        md_content = md_result.text_content

        if not md_content or len(md_content.strip()) < 20:
            result["status"] = "tiny"
            result["error"] = f"Output only {len(md_content)} chars"
            return result

        md_path = output_dir / (file_path.stem + ".md")
        md_path.write_text(md_content, encoding="utf-8")
        result["md_size"] = md_path.stat().st_size

    except ImportError:
        result["status"] = "error"
        result["error"] = "markitdown not installed — pip install markitdown"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)[:500]
    return result

def convert_file(file_path: Path, output_dir: Path, ext: str) -> dict:
    """Route to the correct converter based on file extension."""
    if ext == ".pdf":
        return convert_pdf(file_path, output_dir)
    elif ext in {".docx", ".xlsx"}:
        return convert_office(file_path, output_dir)
    else:
        return {"status": "skipped", "md_size": 0, "error": f"Unsupported: {ext}"}

# =============================================================================
# FILE PROCESSING (with retry + free space)
# =============================================================================

def process_single_file(rel: str, ext: str, file_size: int, md_store: Path) -> dict:
    """
    Process a single file: ensure local → convert → free space.
    Retries up to MAX_RETRIES on failure with exponential backoff.
    Returns result dict with status, md_size, duration_s, error.
    """
    full_path = Path(ONEDRIVE_ROOT) / rel

    # Check if MD already exists (from previous partial run)
    md_name = Path(rel).stem + ".md"
    md_path = md_store / md_name
    if md_path.exists() and md_path.stat().st_size > SMALL_FILE_THRESHOLD:
        return {"status": "done", "md_size": md_path.stat().st_size,
                "duration_s": 0, "error": None, "already_exists": True}

    for attempt in range(MAX_RETRIES):
        start = time.time()

        # Ensure file is downloaded locally
        if not ensure_local(str(full_path)):
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return {"status": "error", "md_size": 0,
                    "duration_s": time.time() - start, "error": "Download timeout"}

        # Convert
        result = convert_file(full_path, md_store, ext)
        result["duration_s"] = round(time.time() - start, 2)

        if result["status"] == "done":
            # Free OneDrive space after successful conversion
            if FREE_SPACE_AFTER_CONVERT:
                free_onedrive_space(str(full_path))
            return result

        # Failed — retry if attempts remain
        if attempt < MAX_RETRIES - 1:
            log.warning(f"  Retrying {Path(rel).name} "
                        f"(attempt {attempt + 2}/{MAX_RETRIES})…")
            time.sleep(RETRY_DELAYS[attempt])

    # All retries exhausted
    return result  # Last failure

# =============================================================================
# PROGRESS REPORTING
# =============================================================================

def print_status(conn: sqlite3.Connection):
    """Print current pipeline status."""
    stats = conn.execute("""
        SELECT status, COUNT(*), COALESCE(SUM(size_bytes), 0)
        FROM files GROUP BY status
    """).fetchall()

    if not stats:
        print("\nNo files scanned yet. Run without --status to begin.")
        return

    total_files = sum(r[1] for r in stats)
    total_size = sum(r[2] for r in stats)

    # Format breakdown
    by_ext = conn.execute("""
        SELECT ext, status, COUNT(*)
        FROM files GROUP BY ext, status ORDER BY ext, status
    """).fetchall()

    print(f"\n{'='*60}")
    print(f"ENGINEERING DOCS PIPELINE — STATUS")
    print(f"{'='*60}")
    print(f"Source: {ONEDRIVE_ROOT}")
    print(f"Output: {MD_STORE}")
    print(f"{'='*60}")

    for status, count, size in stats:
        gb = size / (1024**3)
        pct = (count / total_files * 100) if total_files else 0
        print(f"  {status:12s}: {count:>7,} files ({pct:5.1f}%) — {gb:,.2f} GB")

    print(f"  {'TOTAL':12s}: {total_files:>7,} files — {total_size / (1024**3):,.2f} GB")
    print(f"{'='*60}")

    # By extension
    print("\nBy format:")
    current_ext = None
    for ext, status, count in by_ext:
        if ext != current_ext:
            if current_ext:
                print()
            print(f"  {ext}:", end="")
            current_ext = ext
        print(f"  {status}={count}", end="")
    print()

def list_failed(conn: sqlite3.Connection):
    """List all failed files with errors."""
    failed = conn.execute(
        "SELECT rel_path, ext, error, retry_count FROM files "
        "WHERE status IN ('error', 'missing', 'tiny') ORDER BY rel_path"
    ).fetchall()

    if not failed:
        print("No failed files.")
        return

    print(f"\nFailed files ({len(failed)}):")
    for path, ext, err, retries in failed:
        print(f"  [{ext}] {path}")
        if err:
            print(f"    → {err[:120]} (retries: {retries})")

def print_summary(start_time: float, total: int, converted: int,
                  skipped: int, failed: int, total_bytes: int):
    """Print end-of-run summary."""
    elapsed = time.time() - start_time
    elapsed_str = str(timedelta(seconds=int(elapsed)))
    gb = total_bytes / (1024**3)

    print(f"\n{'='*60}")
    print(f"RUN COMPLETE — {elapsed_str}")
    print(f"{'='*60}")
    print(f"  Total files processed : {total:,}")
    print(f"  Converted             : {converted:,}")
    print(f"  Skipped (existing)    : {skipped:,}")
    print(f"  Failed                : {failed:,}")
    print(f"  Data processed        : {gb:,.2f} GB")
    if converted > 0:
        avg = elapsed / converted
        print(f"  Avg time/file         : {avg:.1f}s")
    print(f"{'='*60}")

    if failed > 0:
        print(f"\nRun with --failed to see details.")
        print(f"Run with --retry-failed to re-attempt.")

# =============================================================================
# MAIN PIPELINE
# =============================================================================

def run_pipeline(test_mode: bool = False, retry_failed: bool = False,
                 scan_only: bool = False):
    """Main pipeline loop."""
    conn = init_db(PROGRESS_DB)
    mode = "test" if test_mode else "full"
    start_time = time.time()

    log.info(f"{'='*60}")
    log.info(f"PIPELINE START — mode={mode}")
    log.info(f"Source: {ONEDRIVE_ROOT}")
    log.info(f"Output: {MD_STORE}")
    log.info(f"{'='*60}")

    # Phase 1: SCAN
    if not retry_failed:
        log.info("Phase 1: Scanning source folder...")
        files = scan_files(ONEDRIVE_ROOT)
        if not files:
            log.error("No files found. Check ONEDRIVE_ROOT path.")
            conn.close()
            return

        queued = queue_new_files(conn, files)
        log.info(f"Queued {queued:,} new/changed files")
        del files  # Free memory
        gc.collect()

    if scan_only:
        print_status(conn)
        conn.close()
        return

    # Phase 2: CHECK HYBRID SERVER (for PDFs)
    has_pdfs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE ext='.pdf' AND status='pending'"
    ).fetchone()[0] > 0

    if has_pdfs and not check_hybrid_server():
        log.error(f"Hybrid server not running on port {HYBRID_PORT}!")
        log.error("Start it first (Terminal 1):")
        log.error(f"  cd ~/pdf-convert-venv && source bin/activate")
        log.error(f"  opendataloader-pdf-hybrid --port {HYBRID_PORT} \\")
        log.error(f"    --force-ocr --ocr-engine rapidocr --device cuda \\")
        log.error(f"    --enrich-formula --enrich-picture-description")
        print("\n❌ Hybrid server not running. Start it in another terminal.")
        conn.close()
        sys.exit(1)

    if has_pdfs:
        log.info("Hybrid server detected ✓")

    # Get pending files with sizes
    if retry_failed:
        pending = conn.execute(
            "SELECT rel_path, ext, size_bytes FROM files WHERE status IN ('error', 'missing', 'tiny')"
        ).fetchall()
        log.info(f"Retrying {len(pending):,} failed files")
    else:
        pending = conn.execute(
            "SELECT rel_path, ext, size_bytes FROM files WHERE status='pending' ORDER BY rel_path"
        ).fetchall()

    if not pending:
        log.info("No pending files.")
        print_status(conn)
        conn.close()
        return

    # Apply test batch limit
    if test_mode and len(pending) > TEST_BATCH_SIZE:
        log.info(f"TEST MODE: limiting to first {TEST_BATCH_SIZE} files")
        pending = pending[:TEST_BATCH_SIZE]

    # Stats
    pending_total_size = sum(r[2] for r in pending)
    log.info(f"To convert: {len(pending):,} files ({pending_total_size / (1024**3):.2f} GB)")
    log.info(f"{'='*60}")

    # Phase 4: CONVERT
    MD_STORE.mkdir(parents=True, exist_ok=True)
    converted = 0
    skipped = 0
    failed = 0
    total_bytes = 0

    # Process in chunks with ThreadPoolExecutor for I/O parallelism
    chunk_size = min(BATCH_SIZE, len(pending))
    for chunk_start in range(0, len(pending), chunk_size):
        chunk = pending[chunk_start:chunk_start + chunk_size]
        chunk_num = (chunk_start // chunk_size) + 1
        total_chunks = (len(pending) + chunk_size - 1) // chunk_size
        log.info(f"Batch {chunk_num}/{total_chunks} ({len(chunk)} files)")

        # Process chunk with thread pool (I/O bound: OneDrive downloads)
        with ThreadPoolExecutor(max_workers=IO_WORKERS) as executor:
            futures = {}
            for rel, ext, file_size in chunk:
                future = executor.submit(process_single_file, rel, ext, file_size, MD_STORE)
                futures[future] = (rel, ext, file_size)

            for i, future in enumerate(as_completed(futures), 1):
                rel, ext, file_size = futures[future]
                try:
                    result = future.result(timeout=300)
                except Exception as e:
                    result = {"status": "error", "md_size": 0, "duration_s": 0,
                              "error": str(e)[:500]}

                # Update database
                conn.execute(
                    "UPDATE files SET status=?, md_size=?, finished_at=?, duration_s=?, "
                    "error=? WHERE rel_path=?",
                    (result["status"], result.get("md_size", 0),
                     datetime.now().isoformat(), result.get("duration_s", 0),
                     result.get("error"), rel)
                )

                if result["status"] == "done":
                    if result.get("already_exists"):
                        skipped += 1
                    else:
                        converted += 1
                        total_bytes += file_size
                elif result["status"] == "skipped":
                    skipped += 1
                else:
                    failed += 1
                    conn.execute(
                        "UPDATE files SET retry_count=retry_count+1 WHERE rel_path=?",
                        (rel,)
                    )

                # Progress update every 10 files
                if i % 10 == 0 or i == len(chunk):
                    log.info(f"  [{i}/{len(chunk)}] "
                             f"ok={converted} skip={skipped} fail={failed}")

            conn.commit()

        # Memory cleanup between chunks
        gc.collect()

    # Phase 5: RSYNC (if configured)
    if RSYNC_TARGET and total_bytes > 0:
        log.info(f"Syncing to {RSYNC_TARGET}...")
        try:
            subprocess.run(
                ["rsync", "-avz", "--progress", str(MD_STORE) + "/", RSYNC_TARGET],
                capture_output=True, text=True, timeout=600
            )
            log.info("rsync complete")
        except Exception as e:
            log.error(f"rsync failed: {e}")

    # Phase 6: SUMMARY
    total = converted + skipped + failed
    print_summary(start_time, total, converted, skipped, failed, total_bytes)
    print_status(conn)
    conn.close()

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Engineering Docs Pipeline — PDF/DOCX/XLSX → Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 pipeline.py --test              # Test with 50 files
    python3 pipeline.py                     # Full run
    python3 pipeline.py --status            # Check progress
    python3 pipeline.py --scan-only         # Index files without converting
    python3 pipeline.py --retry-failed      # Retry failures
        """
    )
    parser.add_argument("--test", action="store_true",
                        help=f"Test batch: convert first {TEST_BATCH_SIZE} files only")
    parser.add_argument("--status", action="store_true",
                        help="Show progress without converting")
    parser.add_argument("--failed", action="store_true",
                        help="List failed files")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Re-attempt only failed files")
    parser.add_argument("--scan-only", action="store_true",
                        help="Scan and index files without converting")
    parser.add_argument("--reset", action="store_true",
                        help="Reset all progress (start over)")

    args = parser.parse_args()

    if args.reset:
        confirm = input("Reset all progress? This deletes the tracking database. [y/N] ")
        if confirm.lower() == "y":
            if PROGRESS_DB.exists():
                PROGRESS_DB.unlink()
                print("Database deleted.")
            else:
                print("No database to reset.")
        return

    conn = init_db(PROGRESS_DB)

    if args.status:
        print_status(conn)
        conn.close()
        return

    if args.failed:
        list_failed(conn)
        conn.close()
        return

    conn.close()
    run_pipeline(
        test_mode=args.test,
        retry_failed=args.retry_failed,
        scan_only=args.scan_only
    )

if __name__ == "__main__":
    main()
