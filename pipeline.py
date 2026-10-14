#!/usr/bin/env python3
"""
Engineering Docs Pipeline v7.1 — Dual-lane + work-budget packing (RAM-safe)
==========================================================================
Self-contained, self-logging, resume-safe converter for engineering documents.
Runs on G14 WSL2 with GPU-accelerated OCR for PDFs, MarkItDown for Word files.

v7/v7.1 (no files skipped for size — everything is processed):
- Dual lanes run concurrently:
    FAST  → digital / text PDFs  → :5002
    HEAVY → scanned / large PDFs → :5003  (never starves FAST)
    DOCX  → MarkItDown
- Work-budget packs (MB + pages) → one JVM call per pack when same output dir
- Adaptive per-job timeout (hang kill only; large files get longer budgets)
- Content hash dedupe (identical PDFs convert once)
- Per-file DB commit (visible progress)
- Smallest-first within each lane
- v7.1 RAM safety (after host crash):
    * hard caps: FAST_W=1, HEAVY_W=1, global MAX_JAVA_SLOTS=2
    * MemAvailable gate before every Java convert
    * smaller stage window; parent-owned Java slots (no semaphore leak on timeout)

Usage:
    python3 pipeline.py --test              # Test batch: 50 files, then stop
    python3 pipeline.py                     # Full run: resumes where it left off
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import io
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import warnings
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ── Suppress pypdf noise ──
warnings.filterwarnings("ignore")
_old_stderr = sys.stderr
sys.stderr = io.StringIO()

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
HYBRID_DIGITAL_URL = "http://localhost:5002"
HYBRID_SCANNED_URL = "http://localhost:5003"
HYBRID_DIGITAL_PORT = 5002
HYBRID_SCANNED_PORT = 5003

# ── Staging / batch outer window ──
# Keep small: each staged PDF is a full copy; large windows blow RAM/disk.
IO_WORKERS = 4
DOWNLOAD_AHEAD = 10
STAGE_WINDOW = 50           # was 200 — too much concurrent disk/RAM pressure
TEST_BATCH_SIZE = 50
MAX_RETRIES = 3
RETRY_DELAYS = [2, 8, 30]
SMALL_FILE_THRESHOLD = 100
FREE_SPACE_AFTER_STAGING = True
RSYNC_TARGET = ""
ATTRIB_EXE = "/mnt/c/Windows/system32/attrib.exe"

PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {".docx"}
ALL_EXTENSIONS = PDF_EXTENSIONS | OFFICE_EXTENSIONS

# ── v7.1 dual-lane + pack budgets (G14 RAM-safe) ──
# Two hybrid OCR servers already hold ~20–25GB. Leave headroom for Windows/WSL.
# FAST=1 + HEAVY=1 keeps one text + one OCR job without FAST monopolising both Java slots.
FAST_WORKERS = 1
HEAVY_WORKERS = 1
DOCX_WORKERS = 2            # cheap (no Java / no OCR server)
MAX_JAVA_SLOTS = 2          # hard global cap on concurrent odl/Java converts
MIN_MEM_AVAILABLE_GB = 8.0  # pause new Java work below this
MEM_WAIT_LOG_EVERY_S = 60

PACK_BUDGET_MB = 15.0       # was 25 — smaller packs = lower peak RSS
PACK_BUDGET_PAGES = 40      # was 80
HEAVY_SOLO_PAGES = 80       # pages ≥ this → pack of 1
HEAVY_SOLO_MB = 8.0         # size ≥ this → pack of 1

# Adaptive timeouts (hang detection only — large files get longer budgets)
FAST_TIMEOUT_BASE_S = 90
FAST_TIMEOUT_PER_PAGE_S = 8
HEAVY_TIMEOUT_BASE_S = 180
HEAVY_TIMEOUT_PER_PAGE_S = 20
TIMEOUT_HARD_CAP_S = 3 * 3600   # 3h absolute max per job
HYBRID_REQ_TIMEOUT_MS_CAP = 600_000  # 10 min per backend request inside ODL

# Global convert coordination (set in run_pipeline, also usable at module level)
_JAVA_SLOTS = threading.Semaphore(MAX_JAVA_SLOTS)
_STDERR_LOCK = threading.Lock()  # odl/pypdf must not race on sys.stderr

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
    # Repair crash-corrupted logs before attaching FileHandler
    try:
        if log_file.exists() and log_file.stat().st_size > 0:
            raw = log_file.read_bytes()
            if b"\x00" in raw:
                cleaned = raw.replace(b"\x00", b"")
                if cleaned and not cleaned.endswith(b"\n"):
                    cleaned += b"\n"
                log_file.write_bytes(cleaned)
    except OSError:
        pass

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
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging(LOG_FILE)

# =============================================================================
# DATABASE
# =============================================================================

def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS files (
        rel_path TEXT PRIMARY KEY, size_bytes INTEGER, mtime REAL, ext TEXT,
        status TEXT DEFAULT 'pending', md5_hash TEXT, md_size INTEGER,
        started_at TEXT, finished_at TEXT, duration_s REAL,
        error TEXT, retry_count INTEGER DEFAULT 0)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON files(status)")
    # content-hash → first completed rel_path (dedupe)
    conn.execute("""CREATE TABLE IF NOT EXISTS content_hash (
        md5_hash TEXT PRIMARY KEY,
        rel_path TEXT NOT NULL,
        md_size INTEGER,
        finished_at TEXT)""")
    conn.commit()
    return conn

# =============================================================================
# UTILS
# =============================================================================

def _format_size(n: int | float) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def mem_available_gb() -> float:
    """Read MemAvailable from /proc/meminfo (Linux/WSL). Returns -1 on failure."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    # kB
                    kb = float(line.split()[1])
                    return kb / (1024 * 1024)
    except OSError:
        pass
    return -1.0


def wait_for_memory(min_gb: float = MIN_MEM_AVAILABLE_GB, label: str = "") -> float:
    """Block until MemAvailable >= min_gb. Returns current available GB."""
    last_log = 0.0
    while True:
        avail = mem_available_gb()
        if avail < 0 or avail >= min_gb:
            return avail
        now = time.time()
        if now - last_log >= MEM_WAIT_LOG_EVERY_S:
            log.warning(
                f"  RAM gate: MemAvailable={avail:.1f}GB < {min_gb:.1f}GB "
                f"— pausing new Java work{(' [' + label + ']') if label else ''}"
            )
            last_log = now
            gc.collect()
        time.sleep(5)


def count_odl_java() -> int:
    """Count live opendataloader-pdf-cli Java processes."""
    try:
        r = subprocess.run(
            ["pgrep", "-fc", "opendataloader-pdf-cli.jar"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip().isdigit():
            return int(r.stdout.strip())
        # pgrep -c prints count even with no match on some versions; handle empty
        if r.returncode in (0, 1):
            s = (r.stdout or "").strip()
            return int(s) if s.isdigit() else 0
    except Exception:
        pass
    return 0


@contextlib.contextmanager
def _java_slot(label: str = ""):
    """Acquire global Java slot and RAM headroom before spawning odl_convert."""
    wait_for_memory(MIN_MEM_AVAILABLE_GB, label=label)
    # Also wait if orphaned Java already at cap (crash leftovers)
    while count_odl_java() >= MAX_JAVA_SLOTS:
        wait_for_memory(MIN_MEM_AVAILABLE_GB, label=f"{label} java-cap")
        time.sleep(3)
    _JAVA_SLOTS.acquire()
    try:
        wait_for_memory(MIN_MEM_AVAILABLE_GB, label=label)
        yield
    finally:
        _JAVA_SLOTS.release()


@contextlib.contextmanager
def _quiet_stderr():
    """
    Thread-safe temporary stderr sink for short noisy calls (pypdf).
    Do NOT wrap long odl_convert calls — quiet=True already suppresses CLI noise,
    and holding a global lock across multi-minute converts serializes the pipeline.
    """
    with _STDERR_LOCK:
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            yield
        finally:
            sys.stderr = old


def repair_log_if_corrupt(path: Path):
    """Strip trailing NUL bytes left by hard crashes mid-write."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return
        data = path.read_bytes()
        if b"\x00" not in data:
            return
        cleaned = data.replace(b"\x00", b"")
        # ensure trailing newline
        if cleaned and not cleaned.endswith(b"\n"):
            cleaned += b"\n"
        path.write_bytes(cleaned)
        # reopen logging handlers if already configured
        logger = logging.getLogger("pipeline")
        for h in list(logger.handlers):
            if isinstance(h, logging.FileHandler):
                try:
                    h.close()
                    logger.removeHandler(h)
                except Exception:
                    pass
        # force re-setup on next setup_logging call via clearing handlers flag
    except OSError as e:
        print(f"log repair failed: {e}", file=sys.stderr)


def file_content_hash(path: Path, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Fast content fingerprint: size + hash of head (+ tail if large)."""
    h = hashlib.md5()
    try:
        size = path.stat().st_size
        h.update(str(size).encode())
        with open(path, "rb") as f:
            chunk = f.read(max_bytes)
            h.update(chunk)
            if size > max_bytes * 2:
                f.seek(max(0, size - 1024 * 1024))
                h.update(f.read(1024 * 1024))
        return h.hexdigest()
    except OSError:
        return ""

# =============================================================================
# ONEDRIVE / STAGING
# =============================================================================

def free_onedrive_space(path: str):
    if not FREE_SPACE_AFTER_STAGING:
        return
    try:
        win_path = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True).stdout.strip()
        subprocess.run([ATTRIB_EXE, "+U", "-P", win_path], capture_output=True, timeout=10)
    except Exception as e:
        log.debug(f"Could not free space: {e}")

def ensure_local(path: str, expected_size: int, timeout: int = 120) -> bool:
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

def stage_file(rel: str, file_size: int) -> Path | None:
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
                    log.info(f"  Scanned {count:,} files ({count/elapsed:.0f} f/s)...")
            except OSError:
                continue
    elapsed = time.time() - scan_start
    total_gb = sum(r[1] for r in results) / (1024**3)
    log.info(f"Scan complete: {count:,} files ({total_gb:.1f} GB) in {elapsed:.0f}s")
    return results

def queue_new_files(conn: sqlite3.Connection, files: list[tuple[str, int, float, str]]) -> int:
    prefer: dict[tuple[str, str], tuple[str, int, float, str]] = {}
    for rel, size, mtime, ext in files:
        key = (str(Path(rel).parent), Path(rel).stem)
        existing = prefer.get(key)
        if existing is None:
            prefer[key] = (rel, size, mtime, ext)
        elif ext == ".docx" and existing[3] == ".pdf":
            prefer[key] = (rel, size, mtime, ext)
    queued = 0
    for rel, size, mtime, ext in prefer.values():
        row = conn.execute("SELECT mtime,size_bytes,status FROM files WHERE rel_path=?", (rel,)).fetchone()
        if row:
            if row[0] == mtime and row[1] == size and row[2] == "done":
                continue
            conn.execute(
                "UPDATE files SET mtime=?,size_bytes=?,status='pending',error=NULL WHERE rel_path=?",
                (mtime, size, rel),
            )
        else:
            conn.execute(
                "INSERT INTO files(rel_path,size_bytes,mtime,ext,status) VALUES(?,?,?,?,'pending')",
                (rel, size, mtime, ext),
            )
        queued += 1
    conn.commit()
    return queued

# =============================================================================
# SERVER / PDF CLASSIFIER
# =============================================================================

def check_server(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except (ConnectionRefusedError, OSError):
        return False

def pdf_meta(pdf_path: Path, min_chars: int = 80) -> tuple[bool, int, str]:
    """Return (is_scanned, page_count, err). page_count=0 if unknown."""
    if pypdf is None:
        return True, 0, ""
    try:
        with _quiet_stderr():
            reader = pypdf.PdfReader(str(pdf_path), strict=False)
            if reader.is_encrypted:
                return True, 0, "Encrypted"
            n = len(reader.pages)
            if n > 0:
                _ = reader.pages[0]
            text = "".join((p.extract_text() or "") for p in reader.pages[:2])
        scanned = len(text.strip()) < min_chars
        return scanned, n, ""
    except Exception as e:
        return True, 0, f"Unreadable: {str(e)[:60]}"

def is_broken_pdf(pdf_path: Path) -> tuple[bool, str]:
    _scanned, _pages, err = pdf_meta(pdf_path)
    if err == "Encrypted":
        return True, "Encrypted"
    if err.startswith("Unreadable"):
        return True, err
    return False, ""

_calc_re = re.compile("|".join(CALC_PATTERNS), re.IGNORECASE)

def is_calc_document(rel: str) -> bool:
    return bool(_calc_re.search(rel))

def _is_permanent_error(error: str) -> bool:
    if not error:
        return False
    permanent_patterns = [
        "startxref", "IOException", "NumberObject",
        "Invalid parent", "PERMANENTLY SKIPPED", "Encrypted", "Unreadable",
        # NOTE: do NOT treat bare "Return code: 1" as permanent — hybrid CUDA
        # 500s and transient backend failures also surface as exit code 1.
    ]
    return any(p.lower() in error.lower() for p in permanent_patterns)

# =============================================================================
# CONVERTERS
# =============================================================================

def _kill_java_for_path(staged_path: Path):
    """Best-effort kill of hung opendataloader java for this input path."""
    # Prefer full path fragment to avoid killing unrelated jobs with same basename
    name = Path(staged_path).name
    try:
        subprocess.run(
            ["pkill", "-f", f"opendataloader-pdf-cli.jar.*{re.escape(name)}"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

def _job_timeout_s(lane: str, pages: int, size_bytes: int) -> int:
    pages = max(pages, 1)
    if lane == "HEAVY":
        t = HEAVY_TIMEOUT_BASE_S + HEAVY_TIMEOUT_PER_PAGE_S * pages
    else:
        t = FAST_TIMEOUT_BASE_S + FAST_TIMEOUT_PER_PAGE_S * pages
    # size floor: ~45s per MB for heavy-ish
    t = max(t, int(size_bytes / (1024 * 1024) * (45 if lane == "HEAVY" else 20)))
    return int(min(max(t, 120), TIMEOUT_HARD_CAP_S))

def _hybrid_timeout_ms(lane: str, pages: int) -> str:
    pages = max(pages, 1)
    # per-request timeout inside ODL; keep bounded
    if lane == "HEAVY":
        ms = min(HYBRID_REQ_TIMEOUT_MS_CAP, max(120_000, pages * 3_000))
    else:
        ms = min(300_000, max(60_000, pages * 2_000))
    return str(ms)

def convert_pdf(
    staged_path: Path,
    md_path: Path,
    hybrid_url: str,
    hybrid_mode: str = "auto",
    lane: str = "FAST",
    pages: int = 1,
    size_bytes: int = 0,
) -> dict:
    """Convert one PDF with hang-kill timeout + global Java/RAM slot. Never skips for size."""
    _convert = odl_convert
    if _convert is None:
        return {"status": "error", "md_size": 0, "error": "opendataloader_pdf not installed"}

    timeout_s = _job_timeout_s(lane, pages, size_bytes)
    hybrid_to = _hybrid_timeout_ms(lane, pages)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    label = f"{lane}:{staged_path.name[:40]}"

    for attempt in range(MAX_RETRIES):
        result: dict = {}
        exc_holder: list[BaseException] = []

        def _run():
            try:
                _convert(
                    input_path=[str(staged_path)],
                    output_dir=str(md_path.parent),
                    format="markdown",
                    hybrid="docling-fast",
                    hybrid_mode=hybrid_mode,
                    hybrid_url=hybrid_url,
                    hybrid_timeout=hybrid_to,
                    hybrid_fallback=True,  # local Java if CUDA/backend 500s
                    quiet=True,
                )
                if md_path.exists() and md_path.stat().st_size > SMALL_FILE_THRESHOLD:
                    result.update(
                        status="done",
                        md_size=md_path.stat().st_size,
                        error=None,
                    )
                else:
                    result.update(status="missing", md_size=0, error="No .md produced")
            except BaseException as e:
                exc_holder.append(e)

        # Parent owns the Java slot so timeout paths always release it
        with _java_slot(label):
            t = threading.Thread(target=_run, daemon=True)
            t0 = time.time()
            t.start()
            t.join(timeout=timeout_s)
            elapsed = time.time() - t0

            if t.is_alive():
                _kill_java_for_path(staged_path)
                t.join(timeout=30)
                err = (
                    f"timeout after {elapsed:.0f}s "
                    f"(limit {timeout_s}s, pages={pages}, lane={lane})"
                )
                log.warning(f"  TIMEOUT {_format_size(size_bytes)} {staged_path.name} — {err}")
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                return {"status": "error", "md_size": 0, "error": err}

        if exc_holder:
            err = str(exc_holder[0])[:500]
            if _is_permanent_error(err):
                return {"status": "error", "md_size": 0, "error": err}
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAYS[attempt])
                continue
            return {
                "status": "error",
                "md_size": 0,
                "error": err or f"Failed after {MAX_RETRIES} attempts",
            }

        if result:
            result["elapsed_s"] = elapsed
            return result

    return {"status": "error", "md_size": 0, "error": f"Failed after {MAX_RETRIES} attempts"}


def convert_pdf_pack(
    items: list[tuple[str, Path, Path, int, int]],  # rel, staged, md, pages, size
    hybrid_url: str,
    hybrid_mode: str,
    lane: str,
) -> list[tuple[str, dict]]:
    """
    Convert a pack of PDFs that share the same output_dir via one odl_convert call.
    Falls back to per-file on failure/timeout.
    """
    _convert = odl_convert
    if _convert is None:
        return [
            (rel, {"status": "error", "md_size": 0, "error": "opendataloader_pdf not installed"})
            for rel, *_ in items
        ]
    if not items:
        return []
    if len(items) == 1:
        rel, sp, mp, pages, sz = items[0]
        r = convert_pdf(sp, mp, hybrid_url, hybrid_mode, lane, pages, sz)
        return [(rel, r)]

    # same parent guaranteed by packer
    out_dir = items[0][2].parent
    out_dir.mkdir(parents=True, exist_ok=True)
    total_pages = sum(max(it[3], 1) for it in items)
    total_size = sum(it[4] for it in items)
    timeout_s = _job_timeout_s(lane, total_pages, total_size)
    hybrid_to = _hybrid_timeout_ms(lane, max(it[3] for it in items))
    label = f"{lane}:pack{len(items)}"

    paths = [str(it[1]) for it in items]
    exc_holder: list[BaseException] = []
    ok = False

    def _run():
        nonlocal ok
        try:
            _convert(
                input_path=paths,
                output_dir=str(out_dir),
                format="markdown",
                hybrid="docling-fast",
                hybrid_mode=hybrid_mode,
                hybrid_url=hybrid_url,
                hybrid_timeout=hybrid_to,
                hybrid_fallback=True,  # local Java if CUDA/backend 500s
                quiet=True,
            )
            ok = True
        except BaseException as e:
            exc_holder.append(e)

    with _java_slot(label):
        t = threading.Thread(target=_run, daemon=True)
        t0 = time.time()
        t.start()
        t.join(timeout=timeout_s)
        elapsed = time.time() - t0

        if t.is_alive() or exc_holder or not ok:
            for _, sp, _, _, _ in items:
                _kill_java_for_path(sp)
            t.join(timeout=30)

    if t.is_alive() or exc_holder or not ok:
        log.warning(
            f"  pack({len(items)}) failed/timeout in {elapsed:.0f}s — falling back per-file "
            f"({exc_holder[0] if exc_holder else 'timeout'})"
        )
        # Sequential fallback to avoid fan-out memory spike
        out = []
        for rel, sp, mp, pages, sz in items:
            out.append((rel, convert_pdf(sp, mp, hybrid_url, hybrid_mode, lane, pages, sz)))
        return out

    # Map results
    out = []
    for rel, sp, mp, pages, sz in items:
        if mp.exists() and mp.stat().st_size > SMALL_FILE_THRESHOLD:
            out.append((rel, {
                "status": "done",
                "md_size": mp.stat().st_size,
                "error": None,
                "elapsed_s": elapsed / max(len(items), 1),
            }))
        else:
            out.append((rel, convert_pdf(sp, mp, hybrid_url, hybrid_mode, lane, pages, sz)))
    return out


def convert_docx(staged_path: Path, md_path: Path) -> dict:
    if MarkItDown is None:
        return {"status": "error", "md_size": 0, "error": "markitdown not installed"}
    try:
        content = MarkItDown().convert(str(staged_path)).text_content
        if not content or len(content.strip()) < 20:
            return {"status": "tiny", "md_size": 0, "error": f"Only {len(content) if content else 0} chars"}
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content, encoding="utf-8")
        return {"status": "done", "md_size": md_path.stat().st_size, "error": None}
    except Exception as e:
        return {"status": "error", "md_size": 0, "error": str(e)[:500]}

# =============================================================================
# LIVE PROGRESS
# =============================================================================

def _live_line(text: str):
    width = shutil.get_terminal_size((120, 40)).columns
    sys.stderr.write(f"\r\033[K{text[:width]}")
    sys.stderr.flush()


class Progress:
    def __init__(self, total_pending: int):
        self.total = total_pending
        self.converted = 0
        self.skipped = 0
        self.failed = 0
        self.deduped = 0
        self.convert_start = 0.0
        self.last_file = ""
        self.last_size = ""
        self.lane = ""
        self.lock = threading.Lock()

    @property
    def done(self):
        return self.converted + self.skipped + self.failed + self.deduped

    @property
    def elapsed(self):
        if self.convert_start == 0:
            return 0.001
        return max(time.time() - self.convert_start, 0.001)

    @property
    def eta(self):
        if self.done < 3 or self.elapsed < 10:
            return "warming up..."
        rate = self.done / self.elapsed
        return str(timedelta(seconds=int((self.total - self.done) / max(rate, 1e-9))))

    @property
    def rate_str(self):
        if self.done < 3 or self.elapsed < 10:
            return "--"
        return f"{self.done / (self.elapsed / 60):.1f} f/m"

    @property
    def pct(self):
        return self.done / self.total * 100 if self.total > 0 else 0

    def bump(self, **kw):
        with self.lock:
            for k, v in kw.items():
                if hasattr(self, k):
                    setattr(self, k, v)
            if self.convert_start == 0 and self.done > 0:
                self.convert_start = time.time()
            self.draw()

    def draw(self):
        bar_w = 20
        filled = int(bar_w * self.done / self.total) if self.total > 0 else 0
        bar = "█" * filled + "░" * (bar_w - filled)
        fn = self.last_file
        if len(fn) > 40:
            fn = "…" + fn[-39:]
        sz = f" {self.last_size}" if self.last_size else ""
        lane = f"[{self.lane}] " if self.lane else ""
        _live_line(
            f"[{bar}] {self.pct:5.1f}%  ✓{self.converted} ⏭{self.skipped} "
            f"⊕{self.deduped} ✗{self.failed}  ⏱{self.rate_str}  ETA {self.eta}  "
            f"{lane}{fn}{sz}"
        )

# =============================================================================
# STATUS / SUMMARY
# =============================================================================

def print_status(conn: sqlite3.Connection):
    stats = conn.execute(
        "SELECT status,COUNT(*),COALESCE(SUM(size_bytes),0) FROM files GROUP BY status"
    ).fetchall()
    if not stats:
        print("\nNo files scanned yet.")
        return
    total_files = sum(r[1] for r in stats)
    total_size = sum(r[2] for r in stats)
    by_ext = conn.execute(
        "SELECT ext,status,COUNT(*) FROM files GROUP BY ext,status ORDER BY ext,status"
    ).fetchall()
    print(f"\n{'='*60}\nENGINEERING DOCS PIPELINE — STATUS\n{'='*60}")
    print(f"Source:  {ONEDRIVE_ROOT}\nOutput:  {MD_STORE}\n{'='*60}")
    for s, c, sz in stats:
        print(f"  {s:12s}: {c:>7,} files ({c/total_files*100:5.1f}%) — {sz/(1024**3):,.2f} GB")
    print(f"  {'TOTAL':12s}: {total_files:>7,} files — {total_size/(1024**3):,.2f} GB\n{'='*60}")
    print("\nBy format:")
    cur = None
    for e, s, c in by_ext:
        if e != cur:
            print()
            print(f"  {e}:", end="")
            cur = e
        print(f"  {s}={c}", end="")
    print()


def print_summary(scan_elapsed, convert_elapsed, converted, skipped, failed, deduped, total_bytes):
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
    print(f"  Deduped (hash)        : {deduped:,}")
    print(f"  Failed                : {failed:,}")
    print(f"  Data processed        : {total_bytes/(1024**3):,.2f} GB")
    if converted > 0 and convert_elapsed > 0:
        avg = convert_elapsed / converted
        print(f"  Avg time/file         : {avg:.1f}s ({60/avg:.1f} files/min)")
    print(f"{'='*60}")
    if failed:
        print("\nRun with --failed to see details.")

# =============================================================================
# STAGING HELPERS
# =============================================================================

def _stage_if_needed(rel, file_size, staged, futures, executor):
    if rel in staged:
        return
    for f, r in futures.items():
        if r == rel:
            return
    futures[executor.submit(stage_file, rel, file_size)] = rel


def _collect_staged(staged, futures):
    for f in list(futures.keys()):
        if not f.done():
            continue
        rel = futures.pop(f)
        try:
            staged[rel] = f.result(timeout=10)
        except Exception:
            staged[rel] = None


def _ensure_staged(rel, staged, futures, file_size):
    if rel in staged:
        return staged[rel]
    for f, r in list(futures.items()):
        if r == rel:
            try:
                staged[rel] = f.result(timeout=300)
            except Exception:
                staged[rel] = None
            futures.pop(f, None)
            return staged.get(rel)
    staged[rel] = stage_file(rel, file_size)
    return staged[rel]

# =============================================================================
# v7 PACKING + LANES
# =============================================================================

@dataclass(order=True)
class WorkItem:
    sort_key: tuple = field(init=False, repr=False)
    rel: str = field(compare=False)
    ext: str = field(compare=False)
    size: int = field(compare=False)
    staged: Path = field(compare=False)
    md_path: Path = field(compare=False)
    lane: str = field(compare=False)          # FAST | HEAVY | DOCX
    pages: int = field(compare=False, default=1)
    hybrid_url: str = field(compare=False, default="")
    hybrid_mode: str = field(compare=False, default="auto")
    content_hash: str = field(compare=False, default="")

    def __post_init__(self):
        # smallest first within lane
        self.sort_key = (self.size, self.pages, self.rel)


def classify_item(
    rel: str,
    ext: str,
    size: int,
    staged: Path,
    scanned_url: str,
) -> WorkItem | dict:
    """
    Return WorkItem or error dict {status,error}.
    """
    mp = get_md_path(rel, MD_STORE)
    if ext == ".docx":
        return WorkItem(
            rel=rel, ext=ext, size=size, staged=staged, md_path=mp,
            lane="DOCX", pages=1, content_hash=file_content_hash(staged),
        )

    # Single pypdf pass (avoid double open via is_broken_pdf + pdf_meta)
    scanned, pages, err = pdf_meta(staged)
    if err == "Encrypted":
        return {"status": "error", "error": "Skipped: Encrypted", "permanent": True}
    if err.startswith("Unreadable"):
        return {"status": "error", "error": f"Skipped: {err}", "permanent": True}

    pages = pages or max(1, size // 80_000)  # rough fallback
    ch = file_content_hash(staged)

    # Route: true scans → HEAVY; text PDFs → FAST even if large
    if scanned:
        lane = "HEAVY"
        url = scanned_url
        mode = "full"
    else:
        lane = "FAST"
        url = HYBRID_DIGITAL_URL
        mode = "full" if is_calc_document(rel) else "auto"

    return WorkItem(
        rel=rel, ext=ext, size=size, staged=staged, md_path=mp,
        lane=lane, pages=pages, hybrid_url=url, hybrid_mode=mode,
        content_hash=ch,
    )


def build_packs(items: list[WorkItem]) -> list[list[WorkItem]]:
    """
    Pack items that share the same md_path.parent up to MB/page budget.
    Solo if HEAVY and (pages >= HEAVY_SOLO_PAGES or MB >= HEAVY_SOLO_MB).
    """
    items = sorted(items)  # size ascending
    by_dir: dict[str, list[WorkItem]] = {}
    for it in items:
        by_dir.setdefault(str(it.md_path.parent), []).append(it)

    packs: list[list[WorkItem]] = []
    for _dir, group in by_dir.items():
        cur: list[WorkItem] = []
        cur_mb = 0.0
        cur_pages = 0
        for it in group:
            mb = it.size / (1024 * 1024)
            solo = (
                it.lane == "HEAVY"
                and (it.pages >= HEAVY_SOLO_PAGES or mb >= HEAVY_SOLO_MB)
            ) or (
                it.lane == "FAST"
                and (it.pages >= HEAVY_SOLO_PAGES or mb >= HEAVY_SOLO_MB)
            )
            if solo:
                if cur:
                    packs.append(cur)
                    cur, cur_mb, cur_pages = [], 0.0, 0
                packs.append([it])
                continue
            # would exceed budget?
            if cur and (
                cur_mb + mb > PACK_BUDGET_MB
                or cur_pages + it.pages > PACK_BUDGET_PAGES
            ):
                packs.append(cur)
                cur, cur_mb, cur_pages = [], 0.0, 0
            cur.append(it)
            cur_mb += mb
            cur_pages += max(it.pages, 1)
        if cur:
            packs.append(cur)
    # process smaller packs first overall
    packs.sort(key=lambda p: (sum(i.size for i in p), len(p)))
    return packs


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def run_pipeline(test_mode=False, retry_failed=False, scan_only=False, force_scan=False):
    conn = init_db(PROGRESS_DB)
    db_lock = threading.Lock()
    mode = "test" if test_mode else "full"
    scan_start = time.time()
    scan_elapsed = 0.0
    convert_start = 0.0

    log.info(f"{'='*60}")
    log.info(f"PIPELINE v7.1 START — mode={mode}")
    log.info(f"Source:  {ONEDRIVE_ROOT}")
    log.info(f"Output:  {MD_STORE}")
    avail = mem_available_gb()
    log.info(
        f"Config:  FAST_W={FAST_WORKERS} HEAVY_W={HEAVY_WORKERS} DOCX_W={DOCX_WORKERS} "
        f"JAVA_SLOTS={MAX_JAVA_SLOTS} MIN_RAM={MIN_MEM_AVAILABLE_GB:.0f}GB "
        f"PACK={PACK_BUDGET_MB:.0f}MB/{PACK_BUDGET_PAGES}p SOLO≥{HEAVY_SOLO_PAGES}p/{HEAVY_SOLO_MB:.0f}MB "
        f"STAGE_WIN={STAGE_WINDOW}"
    )
    log.info(
        f"MemAvailable now: {avail:.1f}GB" if avail >= 0 else "MemAvailable: unknown"
    )
    log.info(f"Servers: digital={HYBRID_DIGITAL_PORT} scanned={HYBRID_SCANNED_PORT}")
    log.info(f"{'='*60}")
    if avail >= 0 and avail < MIN_MEM_AVAILABLE_GB + 4:
        log.warning(
            f"Low free RAM at start ({avail:.1f}GB). "
            f"Pipeline will pause converts until ≥{MIN_MEM_AVAILABLE_GB:.0f}GB available. "
            "Consider not loading extra apps; OCR servers alone use ~20–25GB."
        )

    # ── Phase 1: SCAN ──
    existing_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    if retry_failed:
        pass
    elif existing_count > 0 and not force_scan:
        log.info(f"Phase 1: Skipping scan — {existing_count:,} files already indexed.")
        log.info("  (Use --scan to check for new/changed files)")
    else:
        log.info("Phase 1: Scanning..." if existing_count == 0 else "Phase 1: Forced rescan...")
        files = scan_files(ONEDRIVE_ROOT)
        scan_elapsed = time.time() - scan_start
        if not files:
            log.error("No files found.")
            conn.close()
            return
        queued = queue_new_files(conn, files)
        log.info(f"Queued {queued:,} new/changed files (already had {existing_count:,})")
        del files
        gc.collect()

    if scan_only:
        print_status(conn)
        conn.close()
        return

    convert_start = time.time()

    # ── Phase 2: SERVERS ──
    has_pdfs = conn.execute(
        "SELECT COUNT(*) FROM files WHERE ext='.pdf' AND status='pending'"
    ).fetchone()[0] > 0
    scanned_url = HYBRID_DIGITAL_URL
    if has_pdfs:
        if not check_server(HYBRID_DIGITAL_PORT):
            log.error(f"Digital server not running on port {HYBRID_DIGITAL_PORT}!")
            log.error("Start: opendataloader-pdf-hybrid --port 5002 --device cuda --enrich-formula")
            conn.close()
            sys.exit(1)
        log.info(f"Digital server ✓ (port {HYBRID_DIGITAL_PORT})")
        if check_server(HYBRID_SCANNED_PORT):
            log.info(f"Scanned server ✓ (port {HYBRID_SCANNED_PORT})")
            scanned_url = HYBRID_SCANNED_URL
        else:
            log.warning(f"Scanned server down on {HYBRID_SCANNED_PORT} — HEAVY falls back to digital")

    # ── Phase 3: QUEUE (smallest first) ──
    if retry_failed:
        pending = conn.execute(
            "SELECT rel_path,ext,size_bytes FROM files "
            "WHERE status IN ('error','missing','tiny') ORDER BY size_bytes ASC"
        ).fetchall()
        log.info(f"Retrying {len(pending):,} failed files")
    else:
        pending = conn.execute(
            "SELECT rel_path,ext,size_bytes FROM files "
            "WHERE status='pending' ORDER BY size_bytes ASC"
        ).fetchall()
    if not pending:
        log.info("No pending files.")
        print_status(conn)
        conn.close()
        return
    if test_mode and len(pending) > TEST_BATCH_SIZE:
        log.info(f"TEST MODE: {TEST_BATCH_SIZE} files")
        pending = pending[:TEST_BATCH_SIZE]

    total_pending = len(pending)
    log.info(f"To convert: {total_pending:,} files ({sum(r[2] for r in pending)/(1024**3):.2f} GB)")

    STAGE_DIR.mkdir(parents=True, exist_ok=True)
    MD_STORE.mkdir(parents=True, exist_ok=True)

    converted = skipped = failed_files = deduped = total_bytes = 0
    progress = Progress(total_pending)
    counters_lock = threading.Lock()

    def mark_done(rel: str, md_size: int, size: int, ch: str = "", elapsed: float = 0.0):
        nonlocal converted, total_bytes
        with db_lock:
            conn.execute(
                "UPDATE files SET status='done',md_size=?,finished_at=?,duration_s=?,error=NULL,md5_hash=? WHERE rel_path=?",
                (md_size, datetime.now().isoformat(), elapsed, ch or None, rel),
            )
            if ch:
                conn.execute(
                    "INSERT OR IGNORE INTO content_hash(md5_hash, rel_path, md_size, finished_at) VALUES(?,?,?,?)",
                    (ch, rel, md_size, datetime.now().isoformat()),
                )
            conn.commit()
        with counters_lock:
            converted += 1
            total_bytes += size

    def mark_skip_existing(rel: str, md_size: int):
        nonlocal skipped
        with db_lock:
            conn.execute(
                "UPDATE files SET status='done',md_size=?,finished_at=?,error=NULL WHERE rel_path=?",
                (md_size, datetime.now().isoformat(), rel),
            )
            conn.commit()
        with counters_lock:
            skipped += 1

    def mark_dedupe(rel: str, src_rel: str, md_size: int, ch: str):
        nonlocal deduped
        # copy md if possible
        src_md = get_md_path(src_rel, MD_STORE)
        dst_md = get_md_path(rel, MD_STORE)
        try:
            if src_md.exists() and not (dst_md.exists() and dst_md.stat().st_size > SMALL_FILE_THRESHOLD):
                dst_md.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_md, dst_md)
                # copy images dir if present
                src_img = src_md.with_name(src_md.stem + "_images")
                dst_img = dst_md.with_name(dst_md.stem + "_images")
                if src_img.is_dir() and not dst_img.exists():
                    shutil.copytree(src_img, dst_img, dirs_exist_ok=True)
            md_size = dst_md.stat().st_size if dst_md.exists() else md_size
        except OSError as e:
            log.debug(f"dedupe copy failed {rel}: {e}")
        with db_lock:
            conn.execute(
                "UPDATE files SET status='done',md_size=?,finished_at=?,error=?,md5_hash=? WHERE rel_path=?",
                (md_size, datetime.now().isoformat(), f"DEDUPED from {src_rel}", ch, rel),
            )
            conn.commit()
        with counters_lock:
            deduped += 1

    def mark_fail(rel: str, err: str, permanent: bool = False):
        nonlocal failed_files
        with db_lock:
            if permanent or _is_permanent_error(err):
                conn.execute(
                    "UPDATE files SET status='error',error=?,retry_count=? WHERE rel_path=?",
                    (f"SKIPPED: {err[:150]}", MAX_RETRIES, rel),
                )
            else:
                conn.execute(
                    "UPDATE files SET status='error',error=?,retry_count=retry_count+1 WHERE rel_path=?",
                    (err[:500], rel),
                )
            conn.commit()
        with counters_lock:
            failed_files += 1

    def lookup_dedupe(ch: str) -> tuple[str, int] | None:
        if not ch:
            return None
        with db_lock:
            row = conn.execute(
                "SELECT rel_path, md_size FROM content_hash WHERE md5_hash=?", (ch,)
            ).fetchone()
            if row:
                return row[0], row[1] or 0
            row = conn.execute(
                "SELECT rel_path, md_size FROM files WHERE md5_hash=? AND status='done' LIMIT 1",
                (ch,),
            ).fetchone()
            if row:
                return row[0], row[1] or 0
        return None

    def process_pack(pack: list[WorkItem]):
        lane = pack[0].lane
        if lane == "DOCX":
            for it in pack:
                t0 = time.time()
                r = convert_docx(it.staged, it.md_path)
                el = time.time() - t0
                if r["status"] == "done":
                    mark_done(it.rel, r["md_size"], it.size, it.content_hash, el)
                    log.info(f"  OK  [DOCX] [{_format_size(it.size)}] {Path(it.rel).name} ({el:.1f}s)")
                    progress.bump(
                        converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                        last_file=Path(it.rel).name, last_size=_format_size(it.size), lane="DOCX",
                    )
                else:
                    mark_fail(it.rel, r.get("error") or "docx fail", permanent=_is_permanent_error(r.get("error") or ""))
                    log.info(f"  FAIL [DOCX] {Path(it.rel).name} — {(r.get('error') or '')[:100]}")
                    progress.bump(
                        converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                        last_file=Path(it.rel).name, last_size=_format_size(it.size), lane="DOCX",
                    )
            return

        # PDF pack
        payload = [(it.rel, it.staged, it.md_path, it.pages, it.size) for it in pack]
        url = pack[0].hybrid_url
        mode = pack[0].hybrid_mode
        # if pack mixes modes (shouldn't), use first
        results = convert_pdf_pack(payload, url, mode, lane)
        by_rel = {rel: r for rel, r in results}
        for it in pack:
            r = by_rel.get(it.rel, {"status": "error", "md_size": 0, "error": "missing result"})
            el = float(r.get("elapsed_s") or 0)
            fname = Path(it.rel).name
            if r["status"] == "done":
                mark_done(it.rel, r["md_size"], it.size, it.content_hash, el)
                log.info(
                    f"  OK  [{lane}] [{_format_size(it.size)}] p={it.pages} "
                    f"{fname} ({el:.1f}s)"
                )
            else:
                err = r.get("error") or "fail"
                mark_fail(it.rel, err, permanent=_is_permanent_error(err))
                tag = "SKIP" if _is_permanent_error(err) else "FAIL"
                log.info(f"  {tag} [{lane}] [{_format_size(it.size)}] {fname} — {err[:100]}")
            progress.bump(
                converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                last_file=fname, last_size=_format_size(it.size), lane=lane,
            )

    # ── Phase 4: sliding window stage → classify → dual-lane convert ──
    idx = 0
    n = len(pending)

    while idx < n:
        window = pending[idx: idx + STAGE_WINDOW]
        idx += len(window)
        log.info(f"Window {min(idx, n)}/{n}: staging {len(window)} files...")

        staged: dict[str, Path | None] = {}
        staging_futures: dict[Future, str] = {}
        with ThreadPoolExecutor(max_workers=IO_WORKERS) as executor:
            for i, (rel, ext, sz) in enumerate(window):
                ahead = i + DOWNLOAD_AHEAD
                if ahead < len(window):
                    arel, _aext, asz = window[ahead]
                    _stage_if_needed(arel, asz, staged, staging_futures, executor)
                _stage_if_needed(rel, sz, staged, staging_futures, executor)
                _collect_staged(staged, staging_futures)
            for rel, ext, sz in window:
                sp = _ensure_staged(rel, staged, staging_futures, sz)
                if sp is None:
                    time.sleep(3)
                    sp = stage_file(rel, sz)
                    staged[rel] = sp
                    if sp is None:
                        mark_fail(rel, "Staging failed")
                        progress.bump(
                            converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                            last_file=Path(rel).name, lane="STAGE",
                        )

        # Classify into lane lists
        fast_items: list[WorkItem] = []
        heavy_items: list[WorkItem] = []
        docx_items: list[WorkItem] = []

        for rel, ext, sz in window:
            sp = staged.get(rel)
            if sp is None:
                continue
            mp = get_md_path(rel, MD_STORE)
            if mp.exists() and mp.stat().st_size > SMALL_FILE_THRESHOLD:
                mark_skip_existing(rel, mp.stat().st_size)
                progress.bump(
                    converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                    last_file=Path(rel).name, last_size=_format_size(sz), lane="SKIP",
                )
                continue

            # permanent prior failures
            with db_lock:
                prev = conn.execute(
                    "SELECT retry_count, error FROM files WHERE rel_path=?", (rel,)
                ).fetchone()
            if prev and (prev[0] or 0) >= MAX_RETRIES and not retry_failed:
                mark_fail(rel, f"PERMANENTLY SKIPPED: {(prev[1] or 'Unknown')[:100]}", permanent=True)
                progress.bump(
                    converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                    last_file=Path(rel).name, lane="SKIP",
                )
                continue

            classified = classify_item(rel, ext, sz, sp, scanned_url)
            if isinstance(classified, dict):
                mark_fail(rel, classified.get("error") or "classify fail", permanent=classified.get("permanent", False))
                progress.bump(
                    converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                    last_file=Path(rel).name, lane="SKIP",
                )
                continue

            # Dedupe by content hash
            if classified.content_hash:
                hit = lookup_dedupe(classified.content_hash)
                if hit and hit[0] != rel:
                    mark_dedupe(rel, hit[0], hit[1], classified.content_hash)
                    log.info(f"  DEDUPE [{_format_size(sz)}] {Path(rel).name} ← {Path(hit[0]).name}")
                    progress.bump(
                        converted=converted, skipped=skipped, failed=failed_files, deduped=deduped,
                        last_file=Path(rel).name, last_size=_format_size(sz), lane="DEDUP",
                    )
                    continue

            if classified.lane == "FAST":
                fast_items.append(classified)
            elif classified.lane == "HEAVY":
                heavy_items.append(classified)
            else:
                docx_items.append(classified)

        fast_packs = build_packs(fast_items)
        heavy_packs = build_packs(heavy_items)
        # docx: one-per-pack
        docx_packs = [[d] for d in sorted(docx_items)]

        log.info(
            f"  Classified: FAST={len(fast_items)} ({len(fast_packs)} packs)  "
            f"HEAVY={len(heavy_items)} ({len(heavy_packs)} packs)  "
            f"DOCX={len(docx_items)}"
        )

        # Run lanes concurrently
        with ThreadPoolExecutor(max_workers=3) as lane_pool:
            futs = []

            def run_lane(name: str, packs: list[list[WorkItem]], workers: int):
                if not packs:
                    return
                log.info(f"  Lane {name}: {len(packs)} packs, {workers} workers")
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    list(pool.map(process_pack, packs))

            futs.append(lane_pool.submit(run_lane, "FAST", fast_packs, FAST_WORKERS))
            futs.append(lane_pool.submit(run_lane, "HEAVY", heavy_packs, HEAVY_WORKERS))
            futs.append(lane_pool.submit(run_lane, "DOCX", docx_packs, DOCX_WORKERS))
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    log.error(f"Lane error: {e}")

        # Cleanup staged files for this window
        for p in staged.values():
            if p and p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        for root, dirs, _ in os.walk(str(STAGE_DIR), topdown=False):
            for d in dirs:
                try:
                    (Path(root) / d).rmdir()
                except OSError:
                    pass
        gc.collect()
        log.info(
            f"  Window done — ✓{converted} ⏭{skipped} ⊕{deduped} ✗{failed_files} "
            f"({progress.rate_str})"
        )

    _live_line("")

    if RSYNC_TARGET and total_bytes > 0:
        log.info(f"Syncing to {RSYNC_TARGET}...")
        try:
            subprocess.run(
                ["rsync", "-avz", str(MD_STORE) + "/", RSYNC_TARGET],
                capture_output=True, text=True, timeout=600,
            )
        except Exception:
            log.error("rsync failed")

    try:
        shutil.rmtree(STAGE_DIR, ignore_errors=True)
    except Exception:
        pass

    convert_elapsed = time.time() - convert_start if convert_start > 0 else 0.0
    print_summary(scan_elapsed, convert_elapsed, converted, skipped, failed_files, deduped, total_bytes)
    print_status(conn)
    conn.close()

# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Engineering Docs Pipeline v7.1 — RAM-safe dual-lane pack scheduler")
    parser.add_argument("--test", action="store_true", help=f"Test batch: first {TEST_BATCH_SIZE} files")
    parser.add_argument("--status", action="store_true", help="Show progress")
    parser.add_argument("--failed", action="store_true", help="List failed files")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failures")
    parser.add_argument("--scan-only", action="store_true", help="Index without converting")
    parser.add_argument("--scan", action="store_true", help="Force rescan for new/changed files")
    parser.add_argument("--reset", action="store_true", help="Start over")

    args = parser.parse_args()
    if args.reset:
        if input("Reset all progress? [y/N] ").lower() == "y" and PROGRESS_DB.exists():
            PROGRESS_DB.unlink()
            print("Database deleted.")
        return

    conn = init_db(PROGRESS_DB)
    if args.status:
        print_status(conn)
        conn.close()
        return
    if args.failed:
        failed = conn.execute(
            "SELECT rel_path,ext,error,retry_count FROM files "
            "WHERE status IN ('error','missing','tiny') ORDER BY rel_path"
        ).fetchall()
        if not failed:
            print("No failed files.")
        else:
            print(f"\nFailed files ({len(failed)}):")
            for p, e, err, r in failed:
                print(f"  [{e}] {p}")
                if err:
                    print(f"    → {err[:120]} (retries: {r})")
        conn.close()
        return
    conn.close()
    run_pipeline(
        test_mode=args.test,
        retry_failed=args.retry_failed,
        scan_only=args.scan_only,
        force_scan=args.scan,
    )


if __name__ == "__main__":
    main()
