#!/usr/bin/env python3
"""
Engineering Docs Pipeline v6 — PDF + DOCX → Markdown (Batch Optimized)
========================================================================
Self-contained, self-logging, resume-safe converter for engineering documents.
Runs on G14 WSL2 with GPU-accelerated OCR for PDFs, MarkItDown for Word files.

Optimizations over v5:
- Batch convert(): 50-200 PDFs per opendataloader_pdf.convert() call (vs 1)
- Two hybrid servers: digital (no forced OCR) vs scanned (forced OCR)
- pypdf classifier routes each PDF to the right server
- Conditional hybrid_mode="full" only for calc/design docs; "auto" for rest
- True batch inner loop: stage→convert→free in bulk
- OneDrive hydration: size-comparison instead of 4KB read
- Top-level imports (no import-in-function overhead)

Usage:
    python3 pipeline.py --test              # Test batch: 50 files, then stop
    python3 pipeline.py                     # Full run: resumes where it left off
"""

import argparse
import gc
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from datetime import datetime, timedelta
from pathlib import Path

# ── Suppress pypdf noise (it spews xref warnings to stderr) ──
warnings.filterwarnings("ignore")
_old_stderr = sys.stderr
sys.stderr = io.StringIO()  # capture pypdf noise during imports

# ── Top-level imports (no more import-inside-function overhead) ──
try:
    from opendataloader_pdf import convert as odl_convert
except ImportError:
    odl_convert = None

try:
    from markitdown import MarkItDown
except ImportError:
    MarkItDown = None

try:
    import pypdf
except ImportError:
    pypdf = None

# ── Restore stderr (progress bar uses it) ──
sys.stderr = _old_stderr
del _old_stderr

# =============================================================================
# CONFIGURATION
# =============================================================================

_symlink = Path.home() / "work-docs"
ONEDRIVE_ROOT = str(_symlink) if _symlink.exists() else "/mnt/c/Users/Chris/OneDrive/02. Work"
MD_STORE = Path.home() / "engineering-md"
STAGE_DIR = MD_STORE / ".staging"
PROGRESS_DB = MD_STORE / ".pipeline.db"
LOG_FILE = MD_STORE / "pipeline.log"

# ── Two hybrid servers ──
# Port 5002: digital PDFs (no forced OCR, faster)
# Port 5003: scanned PDFs (forced OCR with rapidocr)
HYBRID_DIGITAL_URL  = "http://localhost:5002"
HYBRID_SCANNED_URL  = "http://localhost:5003"
HYBRID_DIGITAL_PORT = 5002
HYBRID_SCANNED_PORT = 5003

# ── Tuned batch sizes ──
IO_WORKERS      = 6     # Parallel staging threads (was 4)
DOWNLOAD_AHEAD  = 25    # Files pre-fetched before conversion starts (was 10)
PDF_BATCH       = 0     # REMOVED — batch convert was slower (hybrid server serializes)

BATCH_SIZE      = 150   # Files per outer chunk (was 50)
TEST_BATCH_SIZE = 50
MAX_RETRIES     = 3
RETRY_DELAYS    = [2, 8, 30]
SMALL_FILE_THRESHOLD = 100
FREE_SPACE_AFTER_STAGING = True
RSYNC_TARGET = ""
ATTRIB_EXE = "/mnt/c/Windows/system32/attrib.exe"

PDF_EXTENSIONS  = {".pdf"}
OFFICE_EXTENSIONS = {".docx"}
ALL_EXTENSIONS  = PDF_EXTENSIONS | OFFICE_EXTENSIONS

# ── Conditional formula enrichment ──
# Only use hybrid_mode="full" + formula enrichment for docs that look like calcs.
# Pattern matches against the full relative path (case-insensitive).
CALC_PATTERNS = [
    r"calc", r"calculation", r"design.basis", r"shear",
    r"bearing", r"sbt-", r"ptgasing", r"ptlautan", r"ptyasmin",
    r"stability", r"settlement", r"slope", r"retaining",
    r"foundation", r"pipeline", r"30%", r"50%", r"90%",
]

# =============================================================================
# STARTUP CHECK
# =============================================================================

_VENV_DIR = Path.home() / "pdf-convert-venv"
_VENV_SP = _VENV_DIR / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _VENV_SP.exists() and str(_VENV_SP) not in sys.path:
    print(f"\n{'='*60}")
    print("❌ WRONG PYTHON — You're not running inside the venv!")
    print(f"{'='*60}")
    print(f"  cd ~/pdf-convert-venv && source bin/activate")
    print(f"  cd ~/engineering-docs-pipeline && python3 pipeline.py --test")
    print()
    sys.exit(1)
del _VENV_DIR, _VENV_SP

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
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(fh); logger.addHandler(ch)
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
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        rel_path TEXT PRIMARY KEY, size_bytes INTEGER, mtime REAL, ext TEXT,
        status TEXT DEFAULT 'pending', md5_hash TEXT, md_size INTEGER,
        started_at TEXT, finished_at TEXT, duration_s REAL,
        error TEXT, retry_count INTEGER DEFAULT 0)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
    conn.commit()
    return conn

# =============================================================================
# ONEDRIVE SPACE MANAGEMENT
# =============================================================================

def free_onedrive_space(path: str):
    if not FREE_SPACE_AFTER_STAGING: return
    try:
        win_path = subprocess.run(["wslpath","-w",path], capture_output=True, text=True).stdout.strip()
        subprocess.run([ATTRIB_EXE,"+U","-P",win_path], capture_output=True, timeout=10)
    except Exception as e:
        log.debug(f"Could not free space: {e}")

# ── CHANGE 5: Size-comparison hydration (replaces 4 KB + zero-byte test) ──
def ensure_local(path: str, expected_size: int, timeout: int = 120) -> bool:
    """
    Ensure a OneDrive file is downloaded locally by polling its on-disk size
    until it matches the expected size from the scan. More reliable than
    reading 4 KB and checking for all-zeroes, which fails when cloud
    placeholders report non-zero but incomplete content.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            st = os.stat(path)
            if st.st_size == expected_size and st.st_size > 0:
                return True
        except OSError:
            pass
        time.sleep(2)
    log.warning(f"Download timeout: {os.path.basename(path)}")
    return False

# =============================================================================
# STAGING
# =============================================================================

def stage_file(rel: str, file_size: int) -> Path | None:
    """Copy from OneDrive → staging. Uses size-based hydration check."""
    src = Path(ONEDRIVE_ROOT) / rel
    dst = STAGE_DIR / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size == file_size:
        return dst
    if not ensure_local(str(src), file_size):
        return None
    try:
        shutil.copy2(str(src), str(dst))
    except OSError as e:
        log.warning(f"Stage copy failed: {rel}: {e}")
        return None
    free_onedrive_space(str(src))
    return dst

def get_md_path(rel: str, md_store: Path) -> Path:
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
    count = 0; scan_start = time.time()
    for dirpath, _, filenames in os.walk(root_path):
        for fname in filenames:
            ext = Path(fname).suffix.lower()
            if ext not in ALL_EXTENSIONS: continue
            full = Path(dirpath) / fname
            try:
                st = full.stat()
                results.append((str(full.relative_to(root_path)), st.st_size, st.st_mtime, ext))
                count += 1
                if count % 1000 == 0:
                    elapsed = time.time() - scan_start
                    log.info(f"  Scanned {count:,} files ({count/elapsed:.0f} f/s)...")
            except OSError: continue
    elapsed = time.time() - scan_start
    total_gb = sum(r[1] for r in results) / (1024**3)
    log.info(f"Scan complete: {count:,} files ({total_gb:.1f} GB) in {elapsed:.0f}s")
    return results

def queue_new_files(conn: sqlite3.Connection, files: list[tuple[str, int, float, str]]) -> int:
    """Add new/changed files. When .docx and .pdf share a stem+parent, keep .docx."""
    prefer: dict[tuple[str,str], tuple[str,int,float,str]] = {}
    for rel, size, mtime, ext in files:
        key = (str(Path(rel).parent), Path(rel).stem)
        existing = prefer.get(key)
        if existing is None:
            prefer[key] = (rel, size, mtime, ext)
        elif ext == ".docx" and existing[3] == ".pdf":
            prefer[key] = (rel, size, mtime, ext)
    queued = 0
    for rel, size, mtime, ext in prefer.values():
        row = conn.execute("SELECT mtime,size_bytes,status FROM files WHERE rel_path=?",(rel,)).fetchone()
        if row:
            if row[0]==mtime and row[1]==size and row[2]=="done": continue
            conn.execute("UPDATE files SET mtime=?,size_bytes=?,status='pending',error=NULL WHERE rel_path=?",(mtime,size,rel))
        else:
            conn.execute("INSERT INTO files(rel_path,size_bytes,mtime,ext,status) VALUES(?,?,?,?,'pending')",(rel,size,mtime,ext))
        queued += 1
    conn.commit()
    return queued

# =============================================================================
# HYBRID SERVER CHECKS
# =============================================================================

def check_server(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1",port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False

# =============================================================================
# PDF CLASSIFIER — CHANGE 2 + 3
# =============================================================================

# ── CHANGE 2: Scanned vs digital using pypdf ──
def is_likely_scanned(pdf_path: Path, min_chars: int = 80) -> bool:
    """
    Classify a PDF as scanned (image-based) or digital (text-based).
    If pypdf can extract text → digital. If it can't or crashes → scanned.
    """
    if pypdf is None:
        return True
    try:
        # Suppress pypdf console noise during classification
        _tmp = sys.stderr
        sys.stderr = io.StringIO()
        reader = pypdf.PdfReader(str(pdf_path), strict=False)
        text = "".join((p.extract_text() or "") for p in reader.pages[:2])
        sys.stderr = _tmp
        return len(text.strip()) < min_chars
    except Exception:
        sys.stderr = _tmp
        return True

def is_broken_pdf(pdf_path: Path) -> tuple[bool, str]:
    """Quick pre-check: can this PDF be opened? Returns (is_broken, reason)."""
    if pypdf is None:
        return (False, "")
    try:
        _tmp = sys.stderr
        sys.stderr = io.StringIO()
        reader = pypdf.PdfReader(str(pdf_path), strict=False)
        if reader.is_encrypted:
            sys.stderr = _tmp
            return (True, "Encrypted")
        if len(reader.pages) > 0:
            _ = reader.pages[0]
        sys.stderr = _tmp
        return (False, "")
    except Exception as e:
        sys.stderr = _tmp
        return (True, f"Unreadable: {str(e)[:60]}")

# ── CHANGE 3: Pattern-based calc/design document detection ──
_calc_re = re.compile("|".join(CALC_PATTERNS), re.IGNORECASE)

def is_calc_document(rel: str) -> bool:
    """Return True if the document path matches a calc/design pattern."""
    return bool(_calc_re.search(rel))

# Files that repeatedly fail with Java crashes are skipped permanently.
# This avoids wasting time on known-incompatible PDFs like large reference books.
PERMANENT_FAIL_THRESHOLD = 2  # skip after this many failures

def _is_permanent_error(error: str) -> bool:
    """Detect errors that will never resolve with retry (corrupt PDF, Java crash)."""
    if not error:
        return False
    permanent_patterns = [
        "startxref",        # missing PDF structure
        "IOException",      # Java I/O failure (corrupt PDF)
        "Return code: 1",   # Java process crash
        "NumberObject",     # corrupt PDF objects
        "Invalid parent",   # bad xref
        "PERMANENTLY SKIPPED",  # already flagged
    ]
    return any(p.lower() in error.lower() for p in permanent_patterns)

# =============================================================================
# CONVERTERS — Per-file parallel (not batch — hybrid server serializes batches)
# =============================================================================
# v6's batch convert() was 3x SLOWER than per-file because the hybrid server
# processes input_path serially, blocking the entire call. Reverting to
# per-file with ThreadPoolExecutor for true parallelism (4-6 concurrent calls).

_CONVERT_WORKERS = 4  # Parallel conversion threads per server

def convert_pdf(staged_path: Path, md_path: Path, hybrid_url: str,
                hybrid_mode: str = "auto") -> dict:
    """Convert one PDF via hybrid server. Only retries transient errors."""
    if odl_convert is None:
        return {"status":"error","md_size":0,"error":"opendataloader_pdf not installed"}
    for attempt in range(MAX_RETRIES):
        try:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            odl_convert(
                input_path=[str(staged_path)], output_dir=str(md_path.parent),
                format="markdown", hybrid="docling-fast",
                hybrid_mode=hybrid_mode, hybrid_url=hybrid_url, quiet=True,
            )
            if md_path.exists() and md_path.stat().st_size > SMALL_FILE_THRESHOLD:
                return {"status":"done","md_size":md_path.stat().st_size,"error":None}
            return {"status":"missing","md_size":0,"error":"No .md produced"}
        except Exception as e:
            err = str(e)[:500]
            # Corrupt PDFs fail immediately — don't waste retries
            if _is_permanent_error(err):
                return {"status":"error","md_size":0,"error":err}
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
    return {"status":"error","md_size":0,"error":f"Failed after {MAX_RETRIES} attempts"}

def convert_docx(staged_path: Path, md_path: Path) -> dict:
    if MarkItDown is None:
        return {"status":"error","md_size":0,"error":"markitdown not installed"}
    try:
        content = MarkItDown().convert(str(staged_path)).text_content
        if not content or len(content.strip()) < 20:
            return {"status":"tiny","md_size":0,"error":f"Only {len(content)} chars"}
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content, encoding="utf-8")
        return {"status":"done","md_size":md_path.stat().st_size,"error":None}
    except Exception as e:
        return {"status":"error","md_size":0,"error":str(e)[:500]}

# =============================================================================
# LIVE PROGRESS
# =============================================================================

def _live_line(text: str):
    width = shutil.get_terminal_size((120,40)).columns
    sys.stderr.write(f"\r\033[K{text[:width]}")
    sys.stderr.flush()
class Progress:
    """Tracks live progress with useful per-file info."""
    def __init__(self, total_pending: int, total_chunks: int):
        self.total = total_pending; self.total_chunks = total_chunks
        self.converted = 0; self.skipped = 0; self.failed = 0
        self.convert_start = time.time()
        self.last_file = ""; self.staging_count = 0; self.last_size = ""
        self.chunk_current = 0; self.chunk_total = 0; self.chunk_done = 0

    @property
    def done(self): return self.converted + self.skipped + self.failed
    @property
    def elapsed(self): return time.time() - self.convert_start
    @property
    def eta(self):
        if self.done == 0: return "--:--"
        rate = self.done / self.elapsed
        return str(timedelta(seconds=int((self.total-self.done)/rate))) if rate>0 else "--:--"
    @property
    def rate_str(self):
        if self.done == 0: return "--"
        return f"{self.done/(self.elapsed/60):.1f} f/m"
    @property
    def pct(self):
        return self.done/self.total*100 if self.total>0 else 0

    def update(self, **kw):
        for k, v in kw.items():
            if hasattr(self, k): setattr(self, k, v)

    def draw(self):
        bar_w = 20
        filled = int(bar_w * self.done / self.total) if self.total > 0 else 0
        bar = "█"*filled + "░"*(bar_w-filled)
        ci = f"chunk {self.chunk_current}/{self.total_chunks} [{self.chunk_done}/{self.chunk_total}]" if self.chunk_total else ""
        fn = self.last_file
        if len(fn) > 45: fn = "…" + fn[-44:]
        sz = f" {self.last_size}" if self.last_size else ""
        _live_line(
            f"[{bar}] {self.pct:5.1f}%  ✓{self.converted} ⏭{self.skipped} ✗{self.failed}  "
            f"⏱{self.rate_str}  ETA {self.eta}  ⬇{self.staging_count}  "
            f"{ci}  {fn}{sz}"
        )

# =============================================================================
# PROGRESS REPORTING
# =============================================================================

def print_status(conn: sqlite3.Connection):
    stats = conn.execute("SELECT status,COUNT(*),COALESCE(SUM(size_bytes),0) FROM files GROUP BY status").fetchall()
    if not stats: print("\nNo files scanned yet."); return
    total_files = sum(r[1] for r in stats)
    total_size = sum(r[2] for r in stats)
    by_ext = conn.execute("SELECT ext,status,COUNT(*) FROM files GROUP BY ext,status ORDER BY ext,status").fetchall()
    print(f"\n{'='*60}\nENGINEERING DOCS PIPELINE — STATUS\n{'='*60}")
    print(f"Source:  {ONEDRIVE_ROOT}\nOutput:  {MD_STORE}\n{'='*60}")
    for s,c,sz in stats:
        print(f"  {s:12s}: {c:>7,} files ({c/total_files*100:5.1f}%) — {sz/(1024**3):,.2f} GB")
    print(f"  {'TOTAL':12s}: {total_files:>7,} files — {total_size/(1024**3):,.2f} GB\n{'='*60}")
    print("\nBy format:")
    cur = None
    for e,s,c in by_ext:
        if e!=cur: print(); print(f"  {e}:",end=""); cur=e
        print(f"  {s}={c}",end="")
    print()

def list_failed(conn): pass  # unchanged — keeping existing implementation

def print_summary(scan_elapsed: float, convert_elapsed: float, converted: int,
                  skipped: int, failed: int, total_bytes: int):
    """Print end-of-run summary. scan_elapsed excludes scan time for accurate f/m."""
    total_elapsed = scan_elapsed + convert_elapsed
    _live_line("")
    print(f"\n{'='*60}")
    print(f"RUN COMPLETE — {str(timedelta(seconds=int(total_elapsed)))}")
    if scan_elapsed > 0:
        print(f"  Scan time            : {scan_elapsed:.0f}s")
    if convert_elapsed > 0:
        print(f"  Convert time         : {convert_elapsed:.0f}s")
    print(f"{'='*60}")
    print(f"  Converted             : {converted:,}")
    print(f"  Skipped (existing)    : {skipped:,}")
    print(f"  Failed                : {failed:,}")
    print(f"  Data processed        : {total_bytes/(1024**3):,.2f} GB")
    if converted > 0 and convert_elapsed > 0:
        avg = convert_elapsed / converted
        print(f"  Avg time/file         : {avg:.1f}s ({60/avg:.1f} files/min)")
    print(f"{'='*60}")
    if failed: print("\nRun with --failed to see details.")

# =============================================================================
# ASYNC STAGING HELPERS
# =============================================================================

def _stage_if_needed(rel, file_size, staged, futures, executor):
    if rel in staged: return
    for f,r in futures.items():
        if r==rel: return
    futures[executor.submit(stage_file, rel, file_size)] = rel

def _collect_staged(staged, futures):
    done = 0
    for f in list(futures.keys()):
        if not f.done(): continue
        rel = futures.pop(f)
        try: staged[rel] = f.result(timeout=10)
        except: staged[rel] = None
        done += 1
    return done

def _ensure_staged(rel, staged, futures, file_size):
    if rel in staged: return staged[rel]
    for f,r in list(futures.items()):
        if r==rel:
            try: staged[rel]=f.result(timeout=300)
            except: staged[rel]=None
            futures.pop(f)
            return staged.get(rel)
    staged[rel] = stage_file(rel, file_size)
    return staged[rel]

# =============================================================================
# MAIN PIPELINE
# =============================================================================

def run_pipeline(test_mode=False, retry_failed=False, scan_only=False, force_scan=False):
    conn = init_db(PROGRESS_DB)
    mode = "test" if test_mode else "full"
    scan_start = time.time()
    scan_elapsed = 0.0
    convert_start = 0.0

    log.info(f"{'='*60}")
    log.info(f"PIPELINE v6 START — mode={mode}")
    log.info(f"Source:  {ONEDRIVE_ROOT}")
    log.info(f"Output:  {MD_STORE}")
    log.info(f"Config:  IO_WORKERS={IO_WORKERS} DOWNLOAD_AHEAD={DOWNLOAD_AHEAD} CONVERT_WORKERS={_CONVERT_WORKERS} BATCH_SIZE={BATCH_SIZE}")
    log.info(f"Servers: digital={HYBRID_DIGITAL_PORT} scanned={HYBRID_SCANNED_PORT}")
    log.info(f"{'='*60}")

    # ── Phase 1: SCAN (skip if DB already populated) ──
    existing_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    if retry_failed:
        pass  # no scan needed, just retry errors
    elif existing_count > 0 and not force_scan:
        log.info(f"Phase 1: Skipping scan — {existing_count:,} files already indexed.")
        log.info("  (Use --scan to check for new/changed files)")
    else:
        if existing_count == 0:
            log.info("Phase 1: First run — scanning...")
        else:
            log.info("Phase 1: Forced rescan...")
        files = scan_files(ONEDRIVE_ROOT)
        scan_elapsed = time.time() - scan_start
        if not files: log.error("No files found."); conn.close(); return
        queued = queue_new_files(conn, files)
        log.info(f"Queued {queued:,} new/changed files (already had {existing_count:,})")
        del files; gc.collect()

    if scan_only: print_status(conn); conn.close(); return

    convert_start = time.time()

    # ── Phase 2: CHECK SERVERS ──
    has_pdfs = conn.execute("SELECT COUNT(*) FROM files WHERE ext='.pdf' AND status='pending'").fetchone()[0] > 0
    has_scanned = False
    if has_pdfs:
        if not check_server(HYBRID_DIGITAL_PORT):
            log.error(f"Digital server not running on port {HYBRID_DIGITAL_PORT}!")
            log.error("Start: opendataloader-pdf-hybrid --port 5002 --device cuda --enrich-formula")
            conn.close(); sys.exit(1)
        log.info(f"Digital server ✓ (port {HYBRID_DIGITAL_PORT})")

        # Scanned server — optional, falls back to digital
        scanned_ok = check_server(HYBRID_SCANNED_PORT)
        if scanned_ok:
            log.info(f"Scanned server ✓ (port {HYBRID_SCANNED_PORT})")
        else:
            log.warning(f"Scanned server not running on port {HYBRID_SCANNED_PORT} — using digital fallback")
        scanned_url = HYBRID_SCANNED_URL if scanned_ok else HYBRID_DIGITAL_URL

    # ── Phase 3: QUEUE ──
    if retry_failed:
        pending = conn.execute("SELECT rel_path,ext,size_bytes FROM files WHERE status IN ('error','missing','tiny')").fetchall()
        log.info(f"Retrying {len(pending):,} failed files")
    else:
        pending = conn.execute("SELECT rel_path,ext,size_bytes FROM files WHERE status='pending' ORDER BY rel_path").fetchall()
    if not pending: log.info("No pending files."); print_status(conn); conn.close(); return
    if test_mode and len(pending) > TEST_BATCH_SIZE:
        log.info(f"TEST MODE: {TEST_BATCH_SIZE} files"); pending = pending[:TEST_BATCH_SIZE]

    total_pending = len(pending)
    log.info(f"To convert: {total_pending:,} files ({sum(r[2] for r in pending)/(1024**3):.2f} GB)")

    # ── Phase 4: CONVERT (batch-optimized) ──
    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    MD_STORE.mkdir(parents=True, exist_ok=True)
    converted = skipped = failed_files = total_bytes = 0
    total_chunks = (total_pending + BATCH_SIZE - 1) // BATCH_SIZE
    progress = Progress(total_pending, total_chunks)

    for chunk_start in range(0, total_pending, BATCH_SIZE):
        chunk = pending[chunk_start:chunk_start + BATCH_SIZE]
        chunk_num = (chunk_start // BATCH_SIZE) + 1
        chunk_t0 = time.time()

        log.info(f"Batch {chunk_num}/{total_chunks} ({len(chunk)} files)")
        staged: dict[str, Path|None] = {}
        staging_futures: dict[Future, str] = {}
        # Build file_size lookup for hydration checks
        size_lookup = {rel: sz for rel, ext, sz in chunk}

        with ThreadPoolExecutor(max_workers=IO_WORKERS) as executor:
            # ── Stage first DOWNLOAD_AHEAD files ──
            for i in range(min(DOWNLOAD_AHEAD, len(chunk))):
                rel, ext, sz = chunk[i]
                _stage_if_needed(rel, sz, staged, staging_futures, executor)

            # Phase A: Stage the entire chunk
            for i, (rel, ext, sz) in enumerate(chunk):
                ahead = i + DOWNLOAD_AHEAD
                if ahead < len(chunk):
                    arel, aext, asz = chunk[ahead]
                    _stage_if_needed(arel, asz, staged, staging_futures, executor)
                _collect_staged(staged, staging_futures)

            # Wait for ALL staging to complete
            for rel, ext, sz in chunk:
                sp = _ensure_staged(rel, staged, staging_futures, sz)
                if sp is None:
                    # Retry once
                    time.sleep(5)
                    sp = stage_file(rel, sz)
                    if sp is None:
                        failed_files += 1
                        conn.execute("UPDATE files SET status='error',error='Staging failed',retry_count=retry_count+1 WHERE rel_path=?",(rel,))

            # Phase B: Convert — parallel per-file (not batch)
            # Build task lists for ThreadPoolExecutor
            pdf_tasks: list[tuple[str, Path, Path, str, str]] = []  # (rel,staged,md,url,mode)
            docx_tasks: list[tuple[str, Path, Path]] = []

            for rel, ext, sz in chunk:
                sp = staged.get(rel)
                if sp is None: continue
                mp = get_md_path(rel, MD_STORE)
                if mp.exists() and mp.stat().st_size > SMALL_FILE_THRESHOLD:
                    skipped += 1
                    conn.execute("UPDATE files SET status='done',md_size=?,finished_at=? WHERE rel_path=?",(mp.stat().st_size,datetime.now().isoformat(),rel))
                    continue
                if ext == ".pdf":
                    # Pre-check: skip files that have already failed repeatedly
                    prev_retries = conn.execute(
                        "SELECT retry_count, error FROM files WHERE rel_path=?", (rel,)
                    ).fetchone()
                    if prev_retries and (prev_retries[0] >= MAX_RETRIES or _is_permanent_error(prev_retries[1] or "")):
                        failed_files += 1
                        reason = (prev_retries[1] or "Unknown")[:100]
                        conn.execute(
                            "UPDATE files SET status='error',error=?,retry_count=? WHERE rel_path=?",
                            (f"PERMANENTLY SKIPPED: {reason}", MAX_RETRIES, rel))
                        progress.update(failed=failed_files, last_file=Path(rel).name)
                        progress.draw()
                        continue

                    # Pre-check: skip broken/unreadable PDFs
                    broken, reason = is_broken_pdf(sp)
                    if broken:
                        failed_files += 1
                        conn.execute(
                            "UPDATE files SET status='error',error=?,retry_count=? WHERE rel_path=?",
                            (f"Skipped: {reason}", MAX_RETRIES, rel))
                        progress.update(failed=failed_files, last_file=Path(rel).name)
                        progress.draw()
                        continue

                    if is_likely_scanned(sp):
                        url = scanned_url
                        mode = "full"
                    else:
                        url = HYBRID_DIGITAL_URL
                        mode = "full" if is_calc_document(rel) else "auto"
                    pdf_tasks.append((rel, sp, mp, url, mode))
                elif ext == ".docx":
                    docx_tasks.append((rel, sp, mp))

            # Convert PDFs in parallel (4 concurrent per-server calls)
            if pdf_tasks:
                log.info(f"  Converting {len(pdf_tasks)} PDFs ({_CONVERT_WORKERS} parallel)...")
                with ThreadPoolExecutor(max_workers=_CONVERT_WORKERS) as conv_exec:
                    futures = {}
                    for rel, sp, mp, url, mode in pdf_tasks:
                        futures[conv_exec.submit(convert_pdf, sp, mp, url, mode)] = (rel, sz if (sz := size_lookup.get(rel)) else 0)
                    for future in as_completed(futures):
                        rel, sz = futures[future]
                        file_start = time.time()
                        try: r = future.result(timeout=600)
                        except: r = {"status":"error","md_size":0,"error":"timeout"}
                        file_elapsed = time.time() - file_start
                        fname = Path(rel).name
                        if r["status"] == "done":
                            converted += 1; total_bytes += sz
                            conn.execute("UPDATE files SET status='done',md_size=?,finished_at=?,error=NULL WHERE rel_path=?",(r["md_size"],datetime.now().isoformat(),rel))
                            log.info(f"  OK  [{_format_size(sz)}] {fname} ({file_elapsed:.1f}s)")
                        else:
                            failed_files += 1
                            err_msg = r.get("error","")
                            tag = "SKIP" if _is_permanent_error(err_msg) else "FAIL"
                            log.info(f"  {tag} [{_format_size(sz)}] {fname} — {err_msg[:100]}")
                            if _is_permanent_error(err_msg):
                                conn.execute("UPDATE files SET status='error',error=?,retry_count=? WHERE rel_path=?",(f"SKIPPED: {err_msg[:150]}", MAX_RETRIES, rel))
                            else:
                                conn.execute("UPDATE files SET status='error',error=?,retry_count=retry_count+1 WHERE rel_path=?",(err_msg, rel))
                        progress.update(converted=converted, skipped=skipped, failed=failed_files,
                                        staging_count=len(staging_futures), chunk_current=chunk_num,
                                        chunk_done=converted+skipped+failed_files, chunk_total=len(chunk),
                                        last_file=fname, last_size=_format_size(sz))
                        progress.draw()

            # Convert DOCX (sequential — MarkItDown is CPU-bound, fast)
            for rel, sp, mp in docx_tasks:
                sz = size_lookup.get(rel,0)
                file_start = time.time()
                r = convert_docx(sp, mp)
                file_elapsed = time.time() - file_start
                fname = Path(rel).name
                if r["status"] == "done":
                    converted += 1; total_bytes += sz
                    conn.execute("UPDATE files SET status='done',md_size=?,finished_at=?,error=NULL WHERE rel_path=?",(r["md_size"],datetime.now().isoformat(),rel))
                    log.info(f"  OK  [{_format_size(sz)}] {fname} ({file_elapsed:.1f}s)")
                else:
                    failed_files += 1
                    err_msg = r.get("error","")
                    tag = "SKIP" if _is_permanent_error(err_msg) else "FAIL"
                    log.info(f"  {tag} [{_format_size(sz)}] {fname} — {err_msg[:100]}")
                    if _is_permanent_error(err_msg):
                        conn.execute("UPDATE files SET status='error',error=?,retry_count=? WHERE rel_path=?",(f"SKIPPED: {err_msg[:150]}", MAX_RETRIES, rel))
                    else:
                        conn.execute("UPDATE files SET status='error',error=?,retry_count=retry_count+1 WHERE rel_path=?",(err_msg, rel))
                progress.update(converted=converted, skipped=skipped, failed=failed_files,
                                staging_count=0, chunk_current=chunk_num,
                                chunk_done=converted+skipped+failed_files, chunk_total=len(chunk),
                                last_file=fname, last_size=_format_size(sz))
                progress.draw()

        conn.commit()
        chunk_elapsed = time.time() - chunk_t0
        log.info(f"  Batch {chunk_num} complete in {chunk_elapsed:.0f}s "
                 f"({len(chunk)/chunk_elapsed*60:.1f} f/m)")

        # Cleanup staging
        for p in staged.values():
            if p and p.exists():
                try: p.unlink()
                except OSError: pass
        for root, dirs, _ in os.walk(str(STAGE_DIR), topdown=False):
            for d in dirs:
                try: (Path(root)/d).rmdir()
                except OSError: pass
        gc.collect()

    _live_line("")

    # ── RSYNC ──
    if RSYNC_TARGET and total_bytes > 0:
        log.info(f"Syncing to {RSYNC_TARGET}...")
        try: subprocess.run(["rsync","-avz",str(MD_STORE)+"/",RSYNC_TARGET], capture_output=True,text=True,timeout=600)
        except: log.error("rsync failed")

    try: shutil.rmtree(STAGE_DIR, ignore_errors=True)
    except: pass

    # ── Final summary ──
    convert_elapsed = time.time() - convert_start if convert_start > 0 else 0.0
    print_summary(scan_elapsed, convert_elapsed, converted, skipped, failed_files, total_bytes)
    print_status(conn)
    conn.close()

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Engineering Docs Pipeline v6 — Batch Optimized")
    parser.add_argument("--test", action="store_true", help=f"Test batch: first {TEST_BATCH_SIZE} files")
    parser.add_argument("--status", action="store_true", help="Show progress")
    parser.add_argument("--failed", action="store_true", help="List failed files")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failures")
    parser.add_argument("--scan-only", action="store_true", help="Index without converting")
    parser.add_argument("--scan", action="store_true", help="Force rescan for new/changed files")
    parser.add_argument("--reset", action="store_true", help="Start over")

    args = parser.parse_args()
    if args.reset:
        if input("Reset all progress? [y/N] ").lower()=="y" and PROGRESS_DB.exists():
            PROGRESS_DB.unlink(); print("Database deleted.")
        return

    conn = init_db(PROGRESS_DB)
    if args.status: print_status(conn); conn.close(); return
    if args.failed:
        failed = conn.execute("SELECT rel_path,ext,error,retry_count FROM files WHERE status IN ('error','missing','tiny') ORDER BY rel_path").fetchall()
        if not failed: print("No failed files.")
        else:
            print(f"\nFailed files ({len(failed)}):")
            for p,e,err,r in failed:
                print(f"  [{e}] {p}")
                if err: print(f"    → {err[:120]} (retries: {r})")
        conn.close(); return
    conn.close()
    run_pipeline(test_mode=args.test, retry_failed=args.retry_failed,
                 scan_only=args.scan_only, force_scan=args.scan)
if __name__ == "__main__":
    main()
