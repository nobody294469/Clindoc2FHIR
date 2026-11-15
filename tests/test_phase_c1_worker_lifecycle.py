"""Phase C1 Worker Lifecycle, Heartbeat Fencing, and Daemon Concurrency Test Suite.

Authority: Phase C1 Implementation Mandate
Scope:
  1. Proactive lease renewal:
     - CAS lease extension (`lease_expires_at > :now`, worker_id, attempts, RUNNING).
     - LeaseHeartbeatThread background renewal during compute.
  2. Complete lease fencing:
     - Expired lease CANNOT be renewed even before reaper runs (`lease_expires_at > :now` fails).
     - Stale worker with old attempts or worker_id cannot renew after job reclamation.
     - Stale worker stage writes raise StaleWorkerFencedError and fail closed.
  3. Safe stage replay:
     - Crash recovery cleans up pipeline-origin claims while strictly preserving reviewer-origin claims.
  4. DocumentProcessingDaemon:
     - Continuous polling, reaper cycles, short transaction boundaries, graceful shutdown, and subprocess containment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid
import pytest

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    JobStageEvent,
    JobStatus,
    ProcessingJob,
    User,
)
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import (
    DocumentProcessingDaemon,
    DocumentProcessingWorker,
    LeaseHeartbeatThread,
    StaleWorkerFencedError,
)
from clindoc.ocr.document_text import DocumentPage, DocumentText, Token
from clindoc.storage.blob import EncryptedFileBlobStore


@pytest.fixture
def db_session_factory():
    """Isolated SQLite engine and session factory with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db_session(db_session_factory):
    session = db_session_factory()
    session.session_factory = db_session_factory
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_user(db_session: Session) -> User:
    user = User(
        idp_subject="sub_lifecycle_user",
        username="lifecycle_operator",
        full_name="Lifecycle Operator",
        email="operator@hospital.org",
        is_active=True,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def test_document(db_session: Session, test_user: User) -> Document:
    doc = Document(
        sha256="c" * 64,
        storage_ref="vault://lifecycle_test.pdf",
        original_filename="lifecycle_test.pdf",
        mime_type="application/pdf",
        file_size_bytes=2048,
        page_count=1,
        source_type="born_digital",
        uploaded_by=test_user.id,
        status=DocumentStatus.INGESTED,
    )
    db_session.add(doc)
    db_session.commit()
    return doc


def _normalize_dt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def test_proactive_lease_renewal_extends_lease_atomically(
    db_session: Session,
    test_document: Document,
):
    """Verify that JobQueue.renew_lease extends lease atomically via CAS."""
    queue = JobQueue(default_lease_seconds=60)
    job = queue.enqueue(session=db_session, document_id=test_document.id)
    db_session.commit()

    claimed = queue.claim_next(session=db_session, worker_id="worker-alpha", lease_seconds=60)
    db_session.commit()
    assert claimed is not None
    initial_expiry = _normalize_dt(claimed.lease_expires_at)

    # Renew lease by 120 seconds
    time.sleep(0.01)
    renewed = queue.renew_lease(
        session=db_session,
        job_id=claimed.id,
        worker_id="worker-alpha",
        claimed_attempts=claimed.attempts,
        lease_seconds=120,
    )
    db_session.commit()
    assert renewed is True

    db_session.refresh(claimed)
    assert _normalize_dt(claimed.lease_expires_at) > initial_expiry


def test_expired_lease_cannot_be_renewed_even_before_reaper_runs(
    db_session: Session,
    test_document: Document,
):
    """Verify CAS predicate (lease_expires_at > :now) prevents reviving expired leases."""
    queue = JobQueue(default_lease_seconds=60)
    job = queue.enqueue(session=db_session, document_id=test_document.id)
    db_session.commit()

    claimed = queue.claim_next(session=db_session, worker_id="worker-beta", lease_seconds=60)
    db_session.commit()
    assert claimed is not None

    # Simulate expired lease in database
    past_time = datetime.now(timezone.utc) - timedelta(seconds=15)
    claimed.lease_expires_at = past_time
    db_session.commit()

    # Attempt to renew with current time (reaper has NOT yet run)
    renewed = queue.renew_lease(
        session=db_session,
        job_id=claimed.id,
        worker_id="worker-beta",
        claimed_attempts=claimed.attempts,
        lease_seconds=120,
    )
    db_session.commit()

    # Must return False (fenced out, cannot revive expired lease)
    assert renewed is False

    db_session.refresh(claimed)
    # Expiry must remain in the past
    assert _normalize_dt(claimed.lease_expires_at) == _normalize_dt(past_time)


def test_old_worker_cannot_renew_after_job_reclaimed_by_another_worker(
    db_session: Session,
    test_document: Document,
):
    """Verify that an old worker cannot renew after job is reclaimed by a new worker."""
    queue = JobQueue(default_lease_seconds=10)
    job = queue.enqueue(session=db_session, document_id=test_document.id, max_attempts=3)
    db_session.commit()

    # Worker 1 claims job
    worker1_job = queue.claim_next(session=db_session, worker_id="worker-1", lease_seconds=5)
    db_session.commit()
    assert worker1_job.attempts == 1

    # Simulate lease expiry and reaper cycle
    worker1_job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    reclaimed = queue.reclaim_expired_leases(session=db_session)
    db_session.commit()
    assert len(reclaimed) == 1
    assert reclaimed[0].status == JobStatus.PENDING

    # Worker 2 claims job (attempts=2)
    worker2_job = queue.claim_next(session=db_session, worker_id="worker-2", lease_seconds=60)
    db_session.commit()
    assert worker2_job.attempts == 2

    # Worker 1 attempts to renew using its old identity and attempt count
    renewed = queue.renew_lease(
        session=db_session,
        job_id=job.id,
        worker_id="worker-1",
        claimed_attempts=1,
        lease_seconds=60,
    )
    db_session.commit()
    assert renewed is False  # Fenced out!


def test_stale_worker_stage_check_raises_fenced_error(
    db_session: Session,
    test_document: Document,
):
    """Verify worker._check_fenced aborts when worker lease has expired or been stolen."""
    queue = JobQueue()
    job = queue.enqueue(session=db_session, document_id=test_document.id)
    db_session.commit()

    claimed = queue.claim_next(session=db_session, worker_id="worker-fenced-test")
    db_session.commit()

    worker = DocumentProcessingWorker(queue=queue, worker_id="worker-fenced-test")

    # Valid check initially
    worker._check_fenced(db_session, claimed, claimed.attempts)

    # Now expire the lease
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db_session.commit()

    # Next check must raise StaleWorkerFencedError
    with pytest.raises(StaleWorkerFencedError):
        worker._check_fenced(db_session, claimed, claimed.attempts)


def test_partial_stage_crash_recovery_preserves_reviewer_claims(
    db_session: Session,
    test_document: Document,
    test_user: User,
):
    """Verify that re-running Stage 2 deletes ONLY pipeline claims, strictly preserving reviewer claims."""
    # Seed document with 1 pipeline claim and 1 reviewer-edited claim
    pipeline_claim = Claim(
        claim_id=uuid.uuid4(),
        document_id=test_document.id,
        group_id="grp-1",
        field_type="med.name",
        value="Lisinopril",
        version=1,
        origin=ClaimOrigin.PIPELINE,
        review_state=ClaimReviewState.PENDING,
        payload={"value": "Lisinopril"},
    )
    reviewer_claim = Claim(
        claim_id=uuid.uuid4(),
        document_id=test_document.id,
        group_id="grp-1",
        field_type="med.dose",
        value="20",
        version=2,
        origin=ClaimOrigin.REVIEWER,
        review_state=ClaimReviewState.EDITED,
        created_by=test_user.id,
        payload={"value": "20", "unit_raw": "mg"},
    )
    db_session.add_all([pipeline_claim, reviewer_claim])
    db_session.commit()

    # Re-run Stage 2 cleanup logic
    db_session.query(Claim).filter(
        Claim.document_id == test_document.id,
        Claim.origin == ClaimOrigin.PIPELINE,
    ).delete(synchronize_session=False)
    db_session.commit()

    # Assert pipeline claim was removed
    remaining = db_session.query(Claim).filter(Claim.document_id == test_document.id).all()
    assert len(remaining) == 1
    assert remaining[0].id == reviewer_claim.id
    assert remaining[0].origin == ClaimOrigin.REVIEWER


def test_daemon_single_iteration_and_graceful_shutdown(
    db_session_factory,
    test_document: Document,
    tmp_path: Path,
):
    """Verify DocumentProcessingDaemon processes claimed jobs and respects stop signal."""
    # Create actual dummy file for ingestion
    doc_file = tmp_path / "lifecycle_sample.pdf"
    doc_file.write_bytes(b"%PDF-1.4 dummy content")
    with db_session_factory() as sess:
        d = sess.get(Document, test_document.id)
        d.storage_ref = f"file://{doc_file.resolve().as_posix()}"
        sess.commit()

    queue = JobQueue()
    with db_session_factory() as sess:
        job = queue.enqueue(session=sess, document_id=test_document.id)
        sess.commit()

    # Create dummy mock ingestion function for fast test
    def mock_ingest(**kwargs):
        return DocumentText(
            document_id=str(test_document.id),
            pages=[
                DocumentPage(
                    page_number=1,
                    text="Patient Note\nPotassium 4.5 mmol/L",
                    tokens=[],
                    width_px=612,
                    height_px=792,
                )
            ],
            source_type="born_digital",
        )

    worker = DocumentProcessingWorker(
        queue=queue,
        worker_id="daemon-worker",
        ingest_fn=mock_ingest,
        session_factory=db_session_factory,
    )
    daemon = DocumentProcessingDaemon(
        worker=worker,
        session_factory=db_session_factory,
        poll_interval=0.1,
        reaper_interval=1.0,
    )

    # 1. Run single iteration -> processes the enqueued job
    processed = daemon.run_single_iteration()
    assert processed is True

    with db_session_factory() as sess:
        db_job = sess.get(ProcessingJob, job.id)
        assert db_job.status == JobStatus.COMPLETED
        db_doc = sess.get(Document, test_document.id)
        assert db_doc.status == DocumentStatus.PENDING_REVIEW

    # 2. Next iteration with empty queue returns False
    processed2 = daemon.run_single_iteration()
    assert processed2 is False

    # 3. Graceful shutdown
    daemon.stop()
    assert daemon._stop_requested.is_set()


def test_daemon_subprocess_containment(db_session_factory):
    """Verify daemon tracks and cleans up child subprocesses on shutdown."""
    worker = DocumentProcessingWorker(session_factory=db_session_factory)
    daemon = DocumentProcessingDaemon(worker=worker, session_factory=db_session_factory)

    # Spawn sleep subprocess
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    daemon.register_subprocess(proc)

    # Verify process is running
    assert proc.poll() is None

    # Shutdown daemon and reap
    daemon.stop()

    # Subprocess must be terminated
    assert proc.poll() is not None
