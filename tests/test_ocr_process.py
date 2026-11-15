"""Tests for M-OCR Subprocess Management, Daemon Registration, and Timeout Enforcement."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import pytest
from unittest.mock import MagicMock

from clindoc.ocr.tesseract_runner import (
    TesseractConfig,
    execute_managed_tesseract,
    terminate_process_tree,
    find_tesseract_binary,
    find_tessdata_dir,
    run_tesseract_tsv,
)
from clindoc.jobs.worker import DocumentProcessingDaemon


def test_daemon_registration_and_deregistration():
    """Verify that subprocesses are registered upon start and cleanly deregistered upon normal exit."""
    mock_worker = MagicMock()
    mock_session_factory = MagicMock()
    daemon = DocumentProcessingDaemon(worker=mock_worker, session_factory=mock_session_factory)

    assert len(daemon._active_subprocesses) == 0

    registered = []
    deregistered = []

    def on_reg(p):
        daemon.register_subprocess(p)
        registered.append(p)

    def on_dereg(p):
        daemon.deregister_subprocess(p)
        deregistered.append(p)

    cmd = [sys.executable, "-c", "import time; time.sleep(0.1)"]
    ret, out, err = execute_managed_tesseract(
        cmd=cmd,
        env=os.environ.copy(),
        timeout=5.0,
        register_callback=on_reg,
        deregister_callback=on_dereg,
    )

    assert ret == 0
    assert len(registered) == 1
    assert len(deregistered) == 1
    # Daemon set must be empty after clean completion
    assert len(daemon._active_subprocesses) == 0


def test_timeout_enforcement_and_process_tree_termination():
    """Verify that a hanging subprocess is killed and reaped upon timeout."""
    registered = []
    deregistered = []

    # Hanging script
    cmd = [sys.executable, "-c", "import time; time.sleep(10.0)"]

    with pytest.raises(TimeoutError):
        execute_managed_tesseract(
            cmd=cmd,
            env=os.environ.copy(),
            timeout=0.5,
            register_callback=registered.append,
            deregister_callback=deregistered.append,
        )

    # Process must have been deregistered
    assert len(registered) == 1
    assert len(deregistered) == 1
    # Process must be dead
    proc = registered[0]
    assert proc.poll() is not None


def test_temporary_directory_cleanup():
    """Verify that run_tesseract_tsv leaves zero orphaned temp files in system temp."""
    from PIL import Image

    img = Image.new("RGB", (200, 50), color="white")
    tmp_before = set(os.listdir(tempfile.gettempdir()))

    try:
        run_tesseract_tsv(img)
    except Exception:
        pass

    tmp_after = set(os.listdir(tempfile.gettempdir()))
    # Any clindoc_tess_ directories must have been cleaned up
    new_clindoc_dirs = [f for f in (tmp_after - tmp_before) if "clindoc_tess_" in f]
    assert len(new_clindoc_dirs) == 0


def test_lease_loss_cancellation_terminates_subprocess_promptly():
    """Verify that worker lease loss promptly terminates running OCR subprocess."""
    import threading

    cancel_evt = threading.Event()
    registered = []
    deregistered = []

    # Process that would normally run for 30 seconds
    cmd = [sys.executable, "-c", "import time; time.sleep(30.0)"]

    def trigger_lease_loss_after_delay():
        time.sleep(0.1)
        cancel_evt.set()

    t_killer = threading.Thread(target=trigger_lease_loss_after_delay, daemon=True)
    t_killer.start()

    start_time = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled"):
        execute_managed_tesseract(
            cmd=cmd,
            env=os.environ.copy(),
            timeout=10.0,
            register_callback=registered.append,
            deregister_callback=deregistered.append,
            cancellation_event=cancel_evt,
        )
    duration = time.monotonic() - start_time

    # Must terminate promptly (well under 10.0s timeout, typically ~0.15s)
    assert duration < 2.0, f"Subprocess took too long to cancel: {duration:.2f}s"
    assert len(registered) == 1
    assert len(deregistered) == 1
    proc = registered[0]
    assert proc.poll() is not None


def test_legacy_ingest_callable_rejected_when_heartbeat_active_even_if_not_fenced(tmp_path):
    """Verify that a legacy callable without cancellation support is rejected before invocation when a heartbeat is active, even if the heartbeat is not yet fenced."""
    from clindoc.jobs.worker import DocumentProcessingWorker, StaleWorkerFencedError
    import threading

    invocations = 0

    def legacy_ingest(path, document_id, source_type, is_tabular):
        nonlocal invocations
        invocations += 1
        return None

    worker = DocumentProcessingWorker(ingest_fn=legacy_ingest)

    # Active heartbeat, but NOT fenced
    mock_heartbeat = MagicMock()
    mock_heartbeat._fenced = threading.Event()  # is_set() is False!
    mock_heartbeat.is_fenced.return_value = False
    worker._active_heartbeat = mock_heartbeat

    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-active-hb-1"
    mock_doc = MagicMock()
    mock_doc.id = "doc-active-hb-1"
    mock_doc.source_type = "born_digital"
    mock_doc.original_filename = "doc.pdf"
    mock_doc.patient_context_id = "PAT-123"

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    # Must fail closed BEFORE invocation
    with pytest.raises(StaleWorkerFencedError, match="does not support cancellation_event"):
        worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    # Ingestion callable must never have been called!
    assert invocations == 0, f"Expected 0 invocations, got {invocations}"


def test_positional_only_cancellation_event_classified_unsupported():
    """Verify that a positional-only cancellation_event parameter is classified as unsupported."""
    from clindoc.jobs.worker import DocumentProcessingWorker

    # Positional-only parameter (cannot be passed as keyword cancellation_event=...)
    def pos_only(path, document_id, source_type, is_tabular, cancellation_event, /):
        pass

    assert DocumentProcessingWorker._callable_supports_cancellation(pos_only) is False

    # Standard positional-or-keyword parameter (keyword-compatible)
    def pos_or_kw(path, document_id, source_type, is_tabular, cancellation_event=None):
        pass

    assert DocumentProcessingWorker._callable_supports_cancellation(pos_or_kw) is True

    # Keyword-only parameter (keyword-compatible)
    def kw_only(path, document_id, source_type, is_tabular, *, cancellation_event=None):
        pass

    assert DocumentProcessingWorker._callable_supports_cancellation(kw_only) is True


def test_callable_accepting_kwargs_classified_supported():
    """Verify that a callable accepting **kwargs is classified as supported."""
    from clindoc.jobs.worker import DocumentProcessingWorker

    def kwargs_fn(path, document_id, **kwargs):
        pass

    assert DocumentProcessingWorker._callable_supports_cancellation(kwargs_fn) is True

    lambda_kwargs = lambda path, **kw: None
    assert DocumentProcessingWorker._callable_supports_cancellation(lambda_kwargs) is True


def test_legacy_ingest_callable_without_cancellation_works_when_not_fenced(tmp_path):
    """Verify that a legacy ingestion callable without cancellation_event works normally when cancellation is not required."""
    from clindoc.jobs.worker import DocumentProcessingWorker
    from clindoc.ocr.document_text import DocumentText, DocumentPage, Token, BBoxDict

    call_params = {}

    def legacy_ingest(path, document_id, source_type, is_tabular):
        call_params["path"] = path
        call_params["document_id"] = document_id
        call_params["source_type"] = source_type
        call_params["is_tabular"] = is_tabular
        tokens = [
            Token(
                text="Legacy",
                bbox=BBoxDict(x0=10.0, y0=10.0, x1=50.0, y1=20.0),
                confidence=0.90,
                line_id="line_0",
            ),
        ]
        return DocumentText(
            document_id=document_id,
            pages=[DocumentPage(page_number=1, tokens=tokens, width_px=612, height_px=792)],
            source_type=source_type,
        )

    worker = DocumentProcessingWorker(ingest_fn=legacy_ingest)
    # No heartbeat exists
    worker._active_heartbeat = None

    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-legacy-1"
    mock_doc = MagicMock()
    mock_doc.id = "doc-legacy-1"
    mock_doc.source_type = "born_digital"
    mock_doc.original_filename = "LAB_sample.pdf"
    mock_doc.patient_context_id = "PAT-123"

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    res = worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    assert isinstance(res, DocumentText)
    assert res.pages[0].tokens[0].text == "Legacy"
    assert call_params["document_id"] == "doc-legacy-1"
    assert call_params["is_tabular"] is True


def test_internal_typeerror_in_ingest_propagates_without_retry(tmp_path):
    """Verify that an internal TypeError raised by ingest_fn propagates directly and does not trigger retry."""
    from clindoc.jobs.worker import DocumentProcessingWorker

    call_count = 0

    def broken_internal_ingest(path, document_id, source_type, is_tabular, cancellation_event=None):
        nonlocal call_count
        call_count += 1
        # Genuine internal TypeError (e.g. bug in layout parsing)
        raise TypeError("Internal engine bug: unsupported operand type(s) for +: 'NoneType' and 'int'")

    worker = DocumentProcessingWorker(ingest_fn=broken_internal_ingest)
    worker._active_heartbeat = None

    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-err-1"
    mock_doc = MagicMock()
    mock_doc.id = "doc-err-1"
    mock_doc.source_type = "born_digital"
    mock_doc.original_filename = "doc.pdf"
    mock_doc.patient_context_id = "PAT-123"

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    with pytest.raises(TypeError, match="Internal engine bug: unsupported operand"):
        worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    # Must have been invoked exactly ONCE: NO unsafe retry!
    assert call_count == 1, f"Expected exactly 1 call, got {call_count}"


def test_legacy_ingest_callable_fails_closed_when_cancellation_required(tmp_path):
    """Verify that a callable lacking cancellation_event fails closed before invocation when cancellation is required."""
    from clindoc.jobs.worker import DocumentProcessingWorker, StaleWorkerFencedError
    import threading

    invocations = 0

    def legacy_ingest(path, document_id, source_type, is_tabular):
        nonlocal invocations
        invocations += 1
        return None

    worker = DocumentProcessingWorker(ingest_fn=legacy_ingest)

    # Case A: Lease is already lost
    mock_heartbeat = MagicMock()
    mock_heartbeat._fenced = threading.Event()
    mock_heartbeat._fenced.set()  # Fenced out!
    mock_heartbeat.is_fenced.return_value = True
    worker._active_heartbeat = mock_heartbeat

    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-fenced-1"
    mock_doc = MagicMock()
    mock_doc.id = "doc-fenced-1"
    mock_doc.source_type = "born_digital"
    mock_doc.original_filename = "doc.pdf"
    mock_doc.patient_context_id = "PAT-123"

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    with pytest.raises(StaleWorkerFencedError, match=r"(does not support cancellation_event|lease lost before starting OCR stage)"):
        worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    assert invocations == 0, f"Expected 0 invocations, got {invocations}"


def test_pre_fenced_heartbeat_causes_zero_ingest_invocations(tmp_path):
    """Verify that a pre-fenced heartbeat causes zero ingestion invocations even with a cancellation-compatible callable."""
    from clindoc.jobs.worker import DocumentProcessingWorker, StaleWorkerFencedError
    import threading

    invocations = 0

    def compatible_ingest(path, document_id, source_type, is_tabular, cancellation_event=None):
        nonlocal invocations
        invocations += 1
        return None

    worker = DocumentProcessingWorker(ingest_fn=compatible_ingest)

    mock_heartbeat = MagicMock()
    mock_heartbeat._fenced = threading.Event()
    mock_heartbeat._fenced.set()  # Heartbeat is already fenced!
    mock_heartbeat.is_fenced.return_value = True
    worker._active_heartbeat = mock_heartbeat

    mock_session = MagicMock()
    mock_job = MagicMock()
    mock_job.id = "job-pre-fenced-1"
    mock_doc = MagicMock()
    mock_doc.id = "doc-pre-fenced-1"
    mock_doc.source_type = "born_digital"
    mock_doc.original_filename = "doc.pdf"
    mock_doc.patient_context_id = "PAT-123"

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    with pytest.raises(StaleWorkerFencedError, match="lease lost before starting OCR stage"):
        worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    assert invocations == 0, f"Expected 0 invocations, got {invocations}"


def test_pre_set_cancellation_causes_zero_popen_invocations(monkeypatch):
    """Verify that a pre-set cancellation event causes zero Popen invocations in execute_managed_tesseract."""
    import threading
    import subprocess
    from clindoc.ocr.tesseract_runner import execute_managed_tesseract

    popen_calls = 0
    orig_popen = subprocess.Popen

    def mock_popen(*args, **kwargs):
        nonlocal popen_calls
        popen_calls += 1
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    cancel_evt = threading.Event()
    cancel_evt.set()

    with pytest.raises(TimeoutError, match="already set"):
        execute_managed_tesseract(
            cmd=[sys.executable, "-c", "import time; time.sleep(1)"],
            env=os.environ.copy(),
            timeout=5.0,
            cancellation_event=cancel_evt,
        )

    assert popen_calls == 0, f"Expected 0 Popen calls, got {popen_calls}"


def test_pre_set_cancellation_causes_zero_osd_subprocess_invocations(monkeypatch):
    """Verify that a pre-set cancellation event causes zero subprocess invocations in detect_orientation_osd."""
    import threading
    import subprocess
    from PIL import Image
    from clindoc.ocr.preprocessing import detect_orientation_osd

    popen_calls = 0
    orig_popen = subprocess.Popen

    def mock_popen(*args, **kwargs):
        nonlocal popen_calls
        popen_calls += 1
        return orig_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", mock_popen)

    img = Image.new("RGB", (100, 100), color="white")
    cancel_evt = threading.Event()
    cancel_evt.set()

    with pytest.raises(TimeoutError, match="already set"):
        detect_orientation_osd(
            image=img,
            cancellation_event=cancel_evt,
        )

    assert popen_calls == 0, f"Expected 0 subprocess calls during OSD, got {popen_calls}"


def test_cancellation_during_osd_terminates_subprocess_promptly(tmp_path):
    """Verify that cancellation during OSD execution terminates the subprocess promptly."""
    import threading
    from PIL import Image
    from clindoc.ocr.preprocessing import detect_orientation_osd

    if sys.platform == "win32":
        mock_tess = tmp_path / "mock_tess.bat"
        mock_tess.write_text(f'@echo off\n"{sys.executable}" -c "import time; time.sleep(30)"\n')
    else:
        mock_tess = tmp_path / "mock_tess.sh"
        mock_tess.write_text(f'#!/bin/sh\n"{sys.executable}" -c "import time; time.sleep(30)"\n')
        mock_tess.chmod(0o755)

    img = Image.new("RGB", (100, 100), color="white")
    cancel_evt = threading.Event()

    def cancel_after_delay():
        time.sleep(0.1)
        cancel_evt.set()

    t = threading.Thread(target=cancel_after_delay, daemon=True)
    t.start()

    start_t = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled"):
        detect_orientation_osd(
            image=img,
            tesseract_cmd=str(mock_tess),
            timeout=10.0,
            cancellation_event=cancel_evt,
        )
    elapsed = time.monotonic() - start_t

    assert elapsed < 2.0, f"OSD subprocess cancellation took too long: {elapsed:.2f}s"


def test_cancellation_propagates_through_ingest_and_prevents_subsequent_stages(tmp_path):
    """Verify that cancellation propagates through ingestion and aborts the worker before subsequent pipeline stages."""
    from clindoc.jobs.worker import DocumentProcessingWorker, StaleWorkerFencedError
    from clindoc.db.models import Document, DocumentStatus, ProcessingJob, JobStatus
    import threading

    cancel_evt = threading.Event()
    extraction_called = False

    def cancelling_ingest(path, document_id, source_type, is_tabular, cancellation_event=None):
        if cancellation_event:
            cancellation_event.set()
        raise TimeoutError("OCR cancelled due to lease loss")

    def mock_extract(*args, **kwargs):
        nonlocal extraction_called
        extraction_called = True
        return None

    worker = DocumentProcessingWorker(ingest_fn=cancelling_ingest)
    worker._orchestrate_extraction_stage = mock_extract

    mock_heartbeat = MagicMock()
    mock_heartbeat._fenced = cancel_evt
    mock_heartbeat.is_fenced.side_effect = lambda: cancel_evt.is_set()
    worker._active_heartbeat = mock_heartbeat

    mock_session = MagicMock()
    mock_job = MagicMock(spec=ProcessingJob)
    mock_job.id = "job-cancel-prop-1"
    mock_job.status = JobStatus.RUNNING
    mock_job.attempts = 1
    mock_job.document_id = "doc-cancel-prop-1"

    mock_doc = MagicMock(spec=Document)
    mock_doc.id = "doc-cancel-prop-1"
    mock_doc.status = DocumentStatus.PROCESSING
    mock_doc.original_filename = "doc.pdf"
    mock_doc.patient_context_id = "PAT-123"
    mock_doc.storage_ref = None

    test_file = tmp_path / "sample.pdf"
    test_file.write_bytes(b"%PDF-1.4 mock")

    with pytest.raises(StaleWorkerFencedError, match="lease lost during OCR stage"):
        worker._orchestrate_ocr_stage(mock_session, mock_job, mock_doc, test_file)

    assert not extraction_called, "Extraction stage must NOT be invoked after cancellation during OCR"
    assert worker.last_extraction_output is None




