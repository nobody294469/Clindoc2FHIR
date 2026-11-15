"""Managed Tesseract 5.x Subprocess Runner and Native TSV Parser.

Conforms to PROJECT_SPEC §7.1 and schemas/document_text.json.
Guarantees:
- Safe managed subprocess execution with process group containment.
- Robust timeout enforcement (per-page budget and document-level deadline).
- Worker daemon registration and deregistration hooks.
- Secure temporary directory isolation and guaranteed cleanup.
- Parsing of word-level bounding boxes and confidence directly from native TSV (level 5).
- Inverse 4-corner coordinate transformation to canonical page coordinates.
- Complete preservation of low-confidence tokens (zero token deletion).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import csv
import io
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional, Sequence, Tuple
from PIL import Image

from clindoc.ocr.geometry import BBox, BBoxDict
from clindoc.ocr.schema import LineBox, WordBox
from clindoc.ocr.preprocessing import AffineMatrix

logger = logging.getLogger(__name__)


# ─── CONFIGURATION & BINARY DISCOVERY ────────────────────────────────────────

@dataclass
class TesseractConfig:
    """Tesseract OCR engine configuration."""
    executable_path: Optional[str] = None
    tessdata_dir: Optional[str] = None
    language: str = "eng"
    psm: int = 3                # Page segmentation mode: 3 = Fully automatic page segmentation
    oem: int = 1                # OCR Engine mode: 1 = Neural nets LSTM engine only
    timeout_per_page: float = 30.0
    document_deadline: Optional[float] = None
    extra_args: list[str] = field(default_factory=list)


def find_tesseract_binary() -> Optional[Path]:
    """Locate the Tesseract executable in PATH or standard installation directories."""
    # 1. Environment variable override
    env_cmd = os.environ.get("TESSERACT_CMD")
    if env_cmd:
        p = Path(env_cmd)
        if p.is_file() and os.access(str(p), os.X_OK):
            return p

    # 2. System PATH lookup
    which_path = shutil.which("tesseract")
    if which_path:
        return Path(which_path)

    # 3. Standard platform-specific directories
    candidates = [
        # Windows User AppData (extracted portable or user install)
        Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe",
        # Windows System Program Files
        Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
        Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
        # POSIX standard paths
        Path("/usr/bin/tesseract"),
        Path("/usr/local/bin/tesseract"),
        Path("/opt/homebrew/bin/tesseract"),
    ]
    for c in candidates:
        if c.is_file():
            return c

    return None


def find_tessdata_dir(tesseract_binary: Optional[Path] = None) -> Optional[Path]:
    """Locate the tessdata directory containing traineddata files."""
    # 1. Environment variable override
    env_dir = os.environ.get("TESSDATA_PREFIX")
    if env_dir:
        p = Path(env_dir)
        if p.is_dir() and (p / "eng.traineddata").exists():
            return p

    # 2. Sibling of tesseract executable
    tess_exe = tesseract_binary or find_tesseract_binary()
    if tess_exe:
        sibling_tessdata = tess_exe.parent / "tessdata"
        if sibling_tessdata.is_dir() and (sibling_tessdata / "eng.traineddata").exists():
            return sibling_tessdata

    # 3. Standard POSIX paths
    posix_dirs = [
        Path("/usr/share/tesseract-ocr/5/tessdata"),
        Path("/usr/share/tesseract-ocr/4.00/tessdata"),
        Path("/usr/share/tessdata"),
        Path("/usr/local/share/tessdata"),
        Path("/opt/homebrew/share/tessdata"),
    ]
    for pd in posix_dirs:
        if pd.is_dir() and (pd / "eng.traineddata").exists():
            return pd

    return None


def check_tesseract_readiness() -> tuple[bool, str, dict[str, Any]]:
    """Verify that Tesseract 5.x and required trained models are present and functional.

    Returns:
        (is_ready: bool, message: str, details: dict)
    """
    binary_path = find_tesseract_binary()
    if not binary_path:
        return False, "Tesseract executable not found in PATH or standard locations.", {}

    tessdata_path = find_tessdata_dir(binary_path)
    details: dict[str, Any] = {
        "binary_path": str(binary_path),
        "tessdata_path": str(tessdata_path) if tessdata_path else None,
    }

    # Verify version
    try:
        res = subprocess.run([str(binary_path), "--version"], capture_output=True, text=True, timeout=5.0)
        version_line = res.stdout.splitlines()[0] if res.stdout else res.stderr.splitlines()[0]
        details["version_string"] = version_line
    except Exception as e:
        return False, f"Failed to execute tesseract binary: {e}", details

    # Verify traineddata models
    if not tessdata_path:
        return False, "tessdata directory not found.", details

    eng_data = tessdata_path / "eng.traineddata"
    osd_data = tessdata_path / "osd.traineddata"

    details["has_eng_traineddata"] = eng_data.exists()
    details["has_osd_traineddata"] = osd_data.exists()

    if not eng_data.exists():
        return False, f"Missing eng.traineddata in {tessdata_path}", details
    if not osd_data.exists():
        return False, f"Missing osd.traineddata in {tessdata_path}", details

    return True, f"Tesseract is ready: {version_line}", details


# ─── SUBPROCESS MANAGEMENT & LIFECYCLE ───────────────────────────────────────

def terminate_process_tree(proc: subprocess.Popen, timeout: float = 3.0) -> None:
    """Terminate a process and its child processes cross-platform."""
    if proc.poll() is not None:
        return

    pid = proc.pid
    try:
        if sys.platform == "win32":
            # Forcefully terminate process and all child processes
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=timeout)
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                proc.wait(timeout=timeout)
            except Exception:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Error during process tree termination for PID %d: %s", pid, e)
    finally:
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=1.0)
        except Exception:
            pass


def execute_managed_tesseract(
    cmd: list[str],
    env: dict[str, str],
    timeout: float,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> tuple[int, str, str]:
    """Execute Tesseract in an isolated process group with timeout and lifecycle tracking.

    Supports prompt termination on cancellation_event (e.g. lease loss or shutdown).
    Returns: (returncode, stdout, stderr)
    """
    creationflags = 0
    start_new_session = False

    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        start_new_session = True

    # Check cancellation immediately before subprocess.Popen
    if cancellation_event is not None and cancellation_event.is_set():
        raise TimeoutError("Tesseract OCR execution aborted: cancellation event is already set")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )

    if register_callback:
        try:
            register_callback(proc)
        except Exception as e:
            logger.warning("Failed to register subprocess with daemon: %s", e)

    try:
        if cancellation_event is not None:
            start_t = time.monotonic()
            while proc.poll() is None:
                if cancellation_event.is_set():
                    logger.warning("Tesseract process PID %d cancelled; terminating process tree", proc.pid)
                    terminate_process_tree(proc)
                    raise TimeoutError("Tesseract OCR process cancelled due to lease loss or shutdown")
                if time.monotonic() - start_t >= timeout:
                    terminate_process_tree(proc)
                    raise TimeoutError(f"Tesseract OCR process timed out after {timeout:.1f}s")
                time.sleep(0.05)

            stdout_data, stderr_data = proc.communicate()
            return proc.returncode, stdout_data, stderr_data
        else:
            stdout_data, stderr_data = proc.communicate(timeout=timeout)
            return proc.returncode, stdout_data, stderr_data
    except subprocess.TimeoutExpired:
        logger.error("Tesseract process PID %d timed out after %.1f seconds", proc.pid, timeout)
        terminate_process_tree(proc)
        raise TimeoutError(f"Tesseract OCR process timed out after {timeout:.1f}s")
    except Exception as e:
        terminate_process_tree(proc)
        raise
    finally:
        if deregister_callback:
            try:
                deregister_callback(proc)
            except Exception as e:
                logger.warning("Failed to deregister subprocess from daemon: %s", e)


# ─── RUN TESSERACT TSV ───────────────────────────────────────────────────────

def run_tesseract_tsv(
    image: Image.Image,
    config: Optional[TesseractConfig] = None,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> str:
    """Run Tesseract on PIL Image and return raw TSV stdout text.

    Guarantees isolated temporary directory creation and cleanup.
    """
    cfg = config or TesseractConfig()
    binary_path = cfg.executable_path or find_tesseract_binary()
    if not binary_path:
        raise FileNotFoundError("Tesseract executable not found.")

    tessdata_path = cfg.tessdata_dir or find_tessdata_dir(Path(binary_path))

    # Compute effective timeout respecting remaining document deadline
    timeout = cfg.timeout_per_page
    if cfg.document_deadline is not None:
        remaining = cfg.document_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Document processing deadline expired before OCR page start")
        timeout = min(timeout, remaining)

    with tempfile.TemporaryDirectory(prefix="clindoc_tess_") as tmpdir:
        tmp_img = Path(tmpdir) / "page_input.png"
        image.save(str(tmp_img), format="PNG")

        cmd = [
            str(binary_path),
            str(tmp_img),
            "stdout",
            "tsv",
            "-l", cfg.language,
            "--psm", str(cfg.psm),
            "--oem", str(cfg.oem),
        ]
        if cfg.extra_args:
            cmd.extend(cfg.extra_args)

        env = os.environ.copy()
        if tessdata_path:
            env["TESSDATA_PREFIX"] = str(tessdata_path)

        retcode, stdout, stderr = execute_managed_tesseract(
            cmd=cmd,
            env=env,
            timeout=timeout,
            register_callback=register_callback,
            deregister_callback=deregister_callback,
            cancellation_event=cancellation_event,
        )

        if retcode != 0:
            raise RuntimeError(f"Tesseract OCR failed (exit {retcode}):\n{stderr}")

        return stdout
    """Run Tesseract on PIL Image and return raw TSV stdout text.

    Guarantees isolated temporary directory creation and cleanup.
    """
    cfg = config or TesseractConfig()
    binary_path = cfg.executable_path or find_tesseract_binary()
    if not binary_path:
        raise FileNotFoundError("Tesseract executable not found.")

    tessdata_path = cfg.tessdata_dir or find_tessdata_dir(Path(binary_path))

    # Compute effective timeout respecting remaining document deadline
    timeout = cfg.timeout_per_page
    if cfg.document_deadline is not None:
        remaining = cfg.document_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Document processing deadline expired before OCR page start")
        timeout = min(timeout, remaining)

    with tempfile.TemporaryDirectory(prefix="clindoc_tess_") as tmpdir:
        tmp_img = Path(tmpdir) / "page_input.png"
        image.save(str(tmp_img), format="PNG")

        cmd = [
            str(binary_path),
            str(tmp_img),
            "stdout",
            "tsv",
            "-l", cfg.language,
            "--psm", str(cfg.psm),
            "--oem", str(cfg.oem),
        ]
        if cfg.extra_args:
            cmd.extend(cfg.extra_args)

        env = os.environ.copy()
        if tessdata_path:
            env["TESSDATA_PREFIX"] = str(tessdata_path)

        retcode, stdout, stderr = execute_managed_tesseract(
            cmd=cmd,
            env=env,
            timeout=timeout,
            register_callback=register_callback,
            deregister_callback=deregister_callback,
        )

        if retcode != 0:
            raise RuntimeError(f"Tesseract OCR failed (exit {retcode}):\n{stderr}")

        return stdout


# ─── NATIVE TSV PARSING & 4-CORNER INVERSE MAPPING ───────────────────────────

def parse_tesseract_tsv(
    tsv_text: str,
    forward_matrix: AffineMatrix,
    page_num: int,
    canonical_width: float,
    canonical_height: float,
    min_confidence: float = 0.0,
) -> tuple[list[WordBox], list[LineBox]]:
    """Parse native Tesseract TSV output directly into WordBox and LineBox structures.

    Guarantees:
    - Reads word-level boxes directly from level 5 rows (no heuristic splitting).
    - Uses exact inverse affine matrix transformation via 4 corners to map back to canonical coordinates.
    - Preserves all low-confidence tokens (zero token deletion).
    - Formats confidence into normalized [0.0, 1.0] range.
    - Groups words into lines and applies 2D reading-order layout sorting.
    """
    if not tsv_text.strip():
        return [], []

    inv_matrix = forward_matrix.inverse()
    words_by_line: dict[tuple[int, int, int], list[WordBox]] = {}
    all_words: list[WordBox] = []

    reader = csv.reader(io.StringIO(tsv_text), delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader, None)
    if not header:
        return [], []

    # Map column headers to indices
    col_idx = {name.strip().lower(): i for i, name in enumerate(header)}
    req_cols = {"level", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
    if not req_cols.issubset(col_idx.keys()):
        logger.warning("TSV missing required columns: %s", req_cols - set(col_idx.keys()))
        return [], []

    for row in reader:
        if not row or len(row) <= max(col_idx.values()):
            continue

        try:
            level = int(row[col_idx["level"]])
        except ValueError:
            continue

        # We extract native word-level tokens at level 5
        if level != 5:
            continue

        raw_text = row[col_idx["text"]]
        clean_text = raw_text.strip()
        if not clean_text:
            continue

        try:
            b_no = int(row[col_idx["block_num"]])
            p_no = int(row[col_idx["par_num"]])
            l_no = int(row[col_idx["line_num"]])
            left = float(row[col_idx["left"]])
            top = float(row[col_idx["top"]])
            width = float(row[col_idx["width"]])
            height = float(row[col_idx["height"]])
            raw_conf = float(row[col_idx["conf"]])
        except (ValueError, IndexError):
            continue

        # Normalize confidence to [0.0, 1.0]
        # Tesseract outputs -1 for unknown/special tokens, or 0.0 - 100.0
        if raw_conf < 0:
            norm_conf = 0.0
        else:
            norm_conf = round(max(0.0, min(100.0, raw_conf)) / 100.0, 4)

        # Map 4 corners back to canonical coordinates
        raster_bbox = [left, top, left + width, top + height]
        canon_bbox = inv_matrix.transform_bbox_4corners(
            raster_bbox,
            clip_bounds=(canonical_width, canonical_height),
        )

        word_box = WordBox(
            text=clean_text,
            bbox=canon_bbox,
            confidence=norm_conf,
            page=page_num,
        )

        all_words.append(word_box)
        line_key = (b_no, p_no, l_no)
        if line_key not in words_by_line:
            words_by_line[line_key] = []
        words_by_line[line_key].append(word_box)

    # Assemble lines
    raw_lines: list[LineBox] = []
    for (b_no, p_no, l_no), line_words in words_by_line.items():
        # Sort words in line from left to right
        sorted_words = sorted(line_words, key=lambda w: w.bbox[0])
        line_text = " ".join(w.text for w in sorted_words)
        min_x = min(w.bbox[0] for w in sorted_words)
        min_y = min(w.bbox[1] for w in sorted_words)
        max_x = max(w.bbox[2] for w in sorted_words)
        max_y = max(w.bbox[3] for w in sorted_words)
        avg_conf = round(sum(w.confidence for w in sorted_words) / len(sorted_words), 4)

        raw_lines.append(
            LineBox(
                text=line_text,
                bbox=[round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)],
                confidence=avg_conf,
                page=page_num,
                words=sorted_words,
            )
        )

    # Sort lines into reading order
    from clindoc.ocr.layout import sort_page_lines_reading_order
    sorted_lines = sort_page_lines_reading_order(
        raw_lines,
        page_width=canonical_width,
        page_height=canonical_height,
    )

    return all_words, sorted_lines

