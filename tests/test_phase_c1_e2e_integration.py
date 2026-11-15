"""Phase C1 End-to-End Clinical Document Processing and Export Integration Test Suite.

Authority: Phase C1 Implementation Mandate
Pipeline Lifecycle:
  1. Document Ingestion: Store PDF document -> Document record in INGESTED state.
  2. Background Worker Processing:
     - Worker daemon claims job from queue.
     - Sequential execution of all 5 stages (OCR -> Extraction -> Normalization -> Verification -> Persistence).
     - Atomic CAS lease fencing and proactive lease renewal.
     - Document transitions from INGESTED -> PROCESSING -> PENDING_REVIEW.
     - Job transitions PENDING -> RUNNING -> COMPLETED.
  3. Human Review & Four-Eyes Approval Gate:
     - Patient identity verified and confirmed by clinician.
     - Extracted clinical claims reviewed into terminal states (ACCEPTED).
     - Four-Eyes approval enforced (Approver != Uploader).
     - Document transitions from PENDING_REVIEW -> APPROVED.
  4. FHIR R4 Export & Transactional Outbox Staging:
     - Validates fail-closed export eligibility.
     - Generates compliant FHIR R4 Bundle with DiagnosticReport, Observations, and MedicationStatements.
     - Stages transmission intent into EHR Transmission Outbox.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import uuid
import pytest
import pymupdf

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentApproval,
    DocumentStatus,
    EHRTransmissionOutbox,
    Evidence,
    FHIRExport,
    IdentityVerificationStatus,
    JobStageEvent,
    JobStatus,
    OutboxStatus,
    ProcessingJob,
    User,
    VerificationResult,
)
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingDaemon, DocumentProcessingWorker
from clindoc.review.approval import DocumentApprovalEngine, evaluate_export_eligibility
from clindoc.review.service import ClaimReviewService
from clindoc.services.ehr_integration import EHRIntegrationService
from clindoc.services.ehr_transport import MockFHIRTransport
from clindoc.services.fhir_export import FHIRExportService
from clindoc.services.outbox_service import EHROutboxService
from clindoc.storage.blob import EncryptedFileBlobStore


@pytest.fixture
def session_factory():
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
def db_session(session_factory):
    session = session_factory()
    session.session_factory = session_factory
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def blob_store(tmp_path: Path) -> EncryptedFileBlobStore:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    key = b"0123456789abcdef0123456789abcdef"
    return EncryptedFileBlobStore(root_dir=blob_dir, encryption_key=key)


@pytest.fixture
def clinical_actors(db_session: Session) -> tuple[User, User]:
    uploader = User(
        idp_subject="sub_clinic_nurse_01",
        username="nurse_sarah",
        full_name="Sarah Jenkins, RN",
        email="sjenkins@hospital.org",
        is_active=True,
    )
    approver = User(
        idp_subject="sub_clinic_physician_02",
        username="dr_collins",
        full_name="Dr. Arthur Collins, MD",
        email="acollins@hospital.org",
        is_active=True,
    )
    db_session.add_all([uploader, approver])
    db_session.commit()
    return uploader, approver


def _create_clinical_discharge_pdf() -> bytes:
    """Generate a clean synthetic clinical discharge note with lab and medication sections."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    text = (
        "CARDIOLOGY DISCHARGE NOTE\n"
        "PATIENT: Synthetic Patient John Doe\n"
        "MRN: MRN-E2E-1002\n"
        "DATE: 2026-03-20\n\n"
        "LABORATORY FINDINGS:\n"
        "Recent serum potassium was measured at 4.5 mmol/L (NORMAL).\n\n"
        "DISCHARGE MEDICATIONS:\n"
        "Continue Lisinopril 10 mg PO once daily for hypertension.\n"
        "Continue Metoprolol 25 mg oral twice daily for rate control.\n"
    )
    page.insert_text((50, 50), text)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def test_full_pipeline_ingest_worker_review_and_fhir_outbox_staging(
    session_factory,
    db_session: Session,
    blob_store: EncryptedFileBlobStore,
    clinical_actors: tuple[User, User],
):
    """Execute complete end-to-end clinical document pipeline lifecycle:

    1. Ingestion: Document uploaded & stored in INGESTED state.
    2. Background worker daemon: claims job, processes all 5 stages, transitions to PENDING_REVIEW.
    3. Clinical Review: identity confirmation & claim acceptance.
    4. Four-Eyes Approval: Approver != Uploader -> APPROVED.
    5. FHIR Export: FHIRExport record, FHIRBundle, and Outbox entry staged.
    """
    uploader, approver = clinical_actors

    # ─── 1. DOCUMENT INGESTION ────────────────────────────────────────────────
    pdf_bytes = _create_clinical_discharge_pdf()
    pdf_sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    storage_ref = blob_store.put(pdf_bytes)

    doc = Document(
        sha256=pdf_sha256,
        storage_ref=storage_ref,
        original_filename="cardiology_discharge.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        page_count=1,
        source_type="born_digital",
        uploaded_by=uploader.id,
        status=DocumentStatus.INGESTED,
        patient_context_id="MRN-E2E-1002",
    )
    db_session.add(doc)
    db_session.commit()
    assert doc.status == DocumentStatus.INGESTED

    # ─── 2. BACKGROUND WORKER EXECUTION ───────────────────────────────────────
    queue = JobQueue(default_lease_seconds=60)
    job = queue.enqueue(session=db_session, document_id=doc.id)
    db_session.commit()

    worker = DocumentProcessingWorker(
        queue=queue,
        blob_store=blob_store,
        worker_id="e2e-worker-primary",
        session_factory=session_factory,
    )
    daemon = DocumentProcessingDaemon(
        worker=worker,
        session_factory=session_factory,
        poll_interval=0.1,
    )

    # Execute single worker pass
    processed = daemon.run_single_iteration()
    assert processed is True

    # Verify background processing results
    db_session.refresh(job)
    assert job.status == JobStatus.COMPLETED
    assert job.current_stage == "COMPLETED"

    db_session.refresh(doc)
    assert doc.status == DocumentStatus.PENDING_REVIEW

    # Verify all 5 stages recorded COMPLETED telemetry events
    stage_events = (
        db_session.query(JobStageEvent)
        .filter(JobStageEvent.job_id == job.id, JobStageEvent.status == "COMPLETED")
        .order_by(JobStageEvent.created_at.asc())
        .all()
    )
    recorded_stages = [ev.stage_name for ev in stage_events]
    assert recorded_stages == ["OCR", "EXTRACTION", "NORMALIZATION", "VERIFICATION", "PERSISTENCE"]

    # Verify extracted claims and evidence persisted in database
    persisted_claims = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
    assert len(persisted_claims) > 0
    med_claims = [c for c in persisted_claims if c.field_type == "med.name"]
    assert len(med_claims) >= 1
    for mc in med_claims:
        assert mc.origin == ClaimOrigin.PIPELINE
        assert mc.review_state in (ClaimReviewState.PENDING, ClaimReviewState.ESCALATED)

    # ─── 3. HUMAN CLINICAL REVIEW & FOUR-EYES APPROVAL ───────────────────────
    # Clinician confirms patient identity
    doc.identity_verification_status = IdentityVerificationStatus.CONFIRMED
    doc.identity_confirmed_by = approver.id
    db_session.commit()

    # Clinician reviews and accepts all active claims
    review_svc = ClaimReviewService()
    for cl in persisted_claims:
        if cl.review_state == ClaimReviewState.ESCALATED:
            # Explicit reviewer resolution of escalated claim
            review_svc.reject_claim(
                session=db_session,
                claim_id=cl.claim_id,
                actor_id=approver.id,
                reason_code="incorrect_extraction",
                expected_version=cl.version,
            )
        else:
            review_svc.accept_claim(
                session=db_session,
                claim_id=cl.claim_id,
                actor_id=approver.id,
                expected_version=cl.version,
            )
    db_session.commit()

    # Formal Four-Eyes Approval
    approval_engine = DocumentApprovalEngine()
    approval = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=approver.id,
    )
    db_session.commit()
    assert approval is not None

    db_session.refresh(doc)
    assert doc.status == DocumentStatus.APPROVED

    # ─── 4. FHIR EXPORT & EHR OUTBOX STAGING ──────────────────────────────────
    # Evaluate dynamic export eligibility
    is_eligible, reason, _ = evaluate_export_eligibility(
        session=db_session,
        document_id=doc.id,
        allow_synthetic_identity=True,
    )
    assert is_eligible is True

    # Generate FHIR export and bundle
    export_svc = FHIRExportService()
    export_result = export_svc.export_document(
        session=db_session,
        document_id=doc.id,
        exported_by=approver.id,
    )
    db_session.commit()

    assert export_result is not None
    assert export_result.bundle_json is not None
    import json
    bundle_data = json.loads(export_result.bundle_json)
    assert bundle_data.get("resourceType") == "Bundle"
    assert len(bundle_data.get("entry", [])) > 0

    # Stage into EHR Transmission Outbox
    ehr_svc = EHRIntegrationService(session_factory=session_factory, transport=MockFHIRTransport())
    outbox_svc = EHROutboxService(session_factory=session_factory, ehr_integration_service=ehr_svc)
    staging_res = outbox_svc.stage_transmission_intent(
        document_id=doc.id,
        destination_identity="https://ehr.hospital.org/fhir/r4",
        actor_id=approver.id,
    )

    assert staging_res is not None
    assert staging_res.status in (OutboxStatus.PENDING, "PENDING")
    assert staging_res.outbox_id is not None
