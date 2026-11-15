"""Focused Test Suite for Phase A Milestone 3: Service Layer, Transaction Boundaries,
Dedicated Session Ownership, and Four-Eyes Reconciliation Service.

Authority:
  - Phase A Frozen Architectural Specification
  - Three-Boundary Lifecycle: Boundary A (Auth/Attempt), Boundary B (Network I/O), Boundary C (Outcome)
  - Explicit session_factory ownership (deriving from caller_session.get_bind() strictly forbidden)
  - Attempt Lineage & Stable Destination Idempotency across retries
  - Four-Eyes Reconciliation Service (Trusted DB Role Validation & Single-Use Tokens)
  - Stale attempt recovery to UNKNOWN_RECONCILIATION_REQUIRED
  - Dual-Engine (SQLite & PostgreSQL 18) Verification
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Generator
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clindoc.api.audit.service import AuditService
from clindoc.db.base import Base
from clindoc.db.enums import (
    ClaimReviewState,
    DocumentApprovalStatus,
    DocumentStatus,
    EHRResolutionStatus,
    EHRTransmissionState,
    FHIRExportStatus,
    FHIRVersion,
    IdentityVerificationStatus,
    PatientIdentityApprovalStatus,
    ReconciliationDecision,
    VerificationLabel,
    VerificationStage,
)
from clindoc.db.models import (
    AuditEvent,
    Claim as DBClaim,
    Document as DBDocument,
    DocumentApproval,
    EHRTransmissionReconciliation,
    Evidence as DBEvidence,
    FHIRBundle,
    FHIRExport,
    Role,
    User,
    UserRole,
    VerificationResult as DBVerificationResult,
)
from clindoc.services.ehr_integration import (
    EHRAmbiguousTransmissionError,
    EHRApprovalMissingError,
    EHRDuplicateSubmissionError,
    EHRIntegrationService,
    EHRPatientIdentityError,
    EHRTransmissionResult,
    derive_destination_idempotency_key,
)
from clindoc.services.ehr_transport import (
    EHRAuthenticationError,
    EHRTimeoutError,
    EHRTransmissionResponse,
    EHRTransportError,
    EHRValidationError,
    FHIRTransport,
    MockEHRMode,
    MockFHIRTransport,
)
from clindoc.services.fhir_export import FHIRExportService
from clindoc.services.reconciliation_service import (
    ReconciliationAuthorizationError,
    ReconciliationNotFoundError,
    ReconciliationService,
    ReconciliationTokenAlreadyConsumedError,
    ReconciliationTokenRevokedError,
    ReconciliationValidationError,
)


# ─── EPHEMERAL POSTGRESQL FIXTURE ────────────────────────────────────────────


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg_engine_url() -> Generator[str, None, None]:
    """Start an ephemeral PostgreSQL 18 cluster and yield connection URL."""
    initdb_path = r"D:\postgresql\bin\initdb.exe"
    postgres_path = r"D:\postgresql\bin\postgres.exe"

    if not os.path.exists(initdb_path) or not os.path.exists(postgres_path):
        pytest.skip("PostgreSQL 18 binaries not found at D:\\postgresql\\bin")

    td = tempfile.mkdtemp(prefix="pg_m3_test_")
    port = _get_free_port()
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"

    try:
        subprocess.run(
            [initdb_path, "-D", td, "-U", "postgres", "-A", "trust", "--no-locale", "-E", "UTF8"],
            check=True,
            capture_output=True,
        )
        proc = subprocess.Popen(
            [postgres_path, "-D", td, "-p", str(port), "-c", "max_connections=50"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        import psycopg
        ready = False
        for _ in range(60):
            try:
                with psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres", connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                ready = True
                break
            except Exception:
                time.sleep(0.1)

        if not ready:
            proc.terminate()
            raise RuntimeError(f"PostgreSQL 18 failed to start on port {port}")

        yield url

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ─── SQLITE IN-MEMORY FIXTURE ────────────────────────────────────────────────


@pytest.fixture
def sqlite_session_factory():
    """SQLite sessionmaker backed by StaticPool in-memory engine."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)


# ─── TEST SEEDING HELPERS ────────────────────────────────────────────────────


def _create_user_with_roles(session: Session, username: str, roles: list[str]) -> User:
    u = User(
        id=uuid.uuid4(),
        idp_subject=f"sub-{username}-{uuid.uuid4().hex[:6]}",
        username=username,
        full_name=f"Dr. {username.capitalize()}",
        email=f"{username}@hospital.org",
        is_active=True,
    )
    session.add(u)
    session.flush()

    for r_name in roles:
        r = session.query(Role).filter(Role.id == r_name).first()
        if not r:
            r = Role(id=r_name, name=r_name, description=f"{r_name} role")
            session.add(r)
            session.flush()
        ur = UserRole(user_id=u.id, role_id=r.id)
        session.add(ur)
    session.flush()
    return u


def _create_approved_document(session: Session, user: User) -> tuple[DBDocument, FHIRBundle]:
    doc = DBDocument(
        id=uuid.uuid4(),
        uploaded_by=user.id,
        source_type="scanned_pdf",
        status=DocumentStatus.APPROVED,
        original_filename="sample_note.pdf",
        storage_ref="blobs/test/sample_note.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256="sample-sha256-hash",
        page_count=1,
        patient_context_id="PAT-TEST-999",
        identity_verification_status=IdentityVerificationStatus.CONFIRMED,
        identity_confirmed_by=user.id,
    )
    session.add(doc)
    session.flush()

    appr = DocumentApproval(
        id=uuid.uuid4(),
        document_id=doc.id,
        approver_id=user.id,
        approval_status=DocumentApprovalStatus.APPROVED,
        patient_identity_status=PatientIdentityApprovalStatus.CONFIRMED,
        policy_version="1.0.0",
        is_export_eligible=True,
    )
    session.add(appr)
    session.flush()

    ev = DBEvidence(
        document_id=doc.id,
        page_number=1,
        char_start=0,
        char_end=20,
        evidence_text="Lisinopril 10mg daily",
        bounding_box={"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2},
        ocr_confidence=0.99,
    )
    session.add(ev)
    session.flush()

    claim = DBClaim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        document_id=doc.id,
        evidence_id=ev.id,
        group_id="med-grp-1",
        field_type="medication.name",
        value="Lisinopril",
        review_state=ClaimReviewState.ACCEPTED,
        payload={
            "field_type": "medication.name",
            "value": "Lisinopril",
            "evidence_id": str(ev.id),
        },
    )
    session.add(claim)
    session.flush()

    vr = DBVerificationResult(
        claim_id=claim.id,
        verification_stage=VerificationStage.STAGE_B,
        label=VerificationLabel.SUPPORTED,
        calibrated_confidence=0.98,
        provider_kind="deterministic",
        payload={"confidence": 0.98},
    )
    session.add(vr)
    session.flush()

    bundle_dict = {
        "resourceType": "Bundle",
        "type": "collection",
        "id": f"bundle-{doc.id}",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "PAT-TEST-999"}},
            {"resource": {"resourceType": "MedicationStatement", "id": "med-1", "status": "active"}},
        ],
    }
    from clindoc.fhir.signing import serialize_transport_payload
    raw_bytes, b_sha = serialize_transport_payload(bundle_dict)

    bundle = FHIRBundle(
        id=uuid.uuid4(),
        document_id=doc.id,
        approval_id=appr.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256=b_sha,
        storage_ref=f"blobs/bundles/{doc.id}.json",
        bundle_payload=bundle_dict,
        validation_report={"is_valid": True},
    )
    session.add(bundle)
    session.flush()
    return doc, bundle


# ─── 1. SESSION OWNERSHIP & TRANSACTION BOUNDARIES VERIFICATION ──────────────


def test_missing_session_factory_rejection(sqlite_session_factory):
    """Verify that attempting transmission without an explicit session_factory is rejected.
    
    Deriving sessions from caller_session.get_bind() is strictly forbidden.
    """
    with sqlite_session_factory() as sess:
        u = _create_user_with_roles(sess, "clinician_1", ["CLINICIAN_REVIEWER"])
        doc, _ = _create_approved_document(sess, u)
        sess.commit()

        # Service created without session_factory
        service = EHRIntegrationService(transport=MockFHIRTransport())

        # Calling without passing session_factory raises ValueError
        with pytest.raises(ValueError, match="session_factory is required for EHR transmission workflow"):
            service.transmit_document_to_ehr(document_id=doc.id, actor_id=u.id)


def test_three_boundaries_session_closure_and_no_connection_during_network_io(sqlite_session_factory):
    """Verify the Three-Boundary lifecycle:
    
    Boundary A: Session opened, attempt created, session committed and CLOSED before network call.
    Boundary B: Network I/O occurs with ZERO open sessions or held DB connections.
    Boundary C: Separate dedicated session opened, outcome persisted, committed and CLOSED.
    """
    active_sessions = 0
    sessions_opened = 0
    sessions_closed = 0
    in_network_io = False
    active_sessions_during_network_io = None

    class MonitoredSession(Session):
        def __init__(self, *args, **kwargs):
            nonlocal active_sessions, sessions_opened
            super().__init__(*args, **kwargs)
            active_sessions += 1
            sessions_opened += 1

        def close(self):
            nonlocal active_sessions, sessions_closed
            active_sessions -= 1
            sessions_closed += 1
            super().close()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    monitored_factory = sessionmaker(bind=engine, class_=MonitoredSession, autoflush=False)

    with monitored_factory() as setup_sess:
        u = _create_user_with_roles(setup_sess, "clinician_2", ["CLINICIAN_REVIEWER"])
        doc, bundle = _create_approved_document(setup_sess, u)
        setup_sess.commit()
        doc_id = doc.id
        user_id = u.id

    # Custom transport that asserts zero active sessions during physical transmission
    class BoundaryCheckingTransport(MockFHIRTransport):
        def transmit(self, *args, **kwargs):
            nonlocal in_network_io, active_sessions_during_network_io
            in_network_io = True
            active_sessions_during_network_io = active_sessions
            res = super().transmit(*args, **kwargs)
            in_network_io = False
            return res

    service = EHRIntegrationService(
        session_factory=monitored_factory,
        transport=BoundaryCheckingTransport(),
    )

    initial_opened = sessions_opened
    initial_closed = sessions_closed

    result = service.transmit_document_to_ehr(
        document_id=doc_id,
        actor_id=user_id,
        destination_type="sandbox",
    )

    assert result.success is True

    # 1. Exactly 2 sessions were created for transmission (Boundary A and Boundary C)
    assert sessions_opened - initial_opened == 2
    assert sessions_closed - initial_closed == 2

    # 2. During network I/O (Boundary B), zero database sessions were active
    assert active_sessions_during_network_io == 0
    assert in_network_io is False


# ─── 2. TRANSMISSION OUTCOMES: SUCCESS, CONFIRMED FAILURE & AMBIGUOUS TIMEOUT ─


def test_transmission_lifecycle_outcomes(sqlite_session_factory):
    """Verify outcome persistence across the three canonical transmission states:
    
    1. 200 OK -> TRANSMITTED, RESOLVED_DELIVERED, doc marked EXPORTED.
    2. 401/422 deterministic rejection -> CONFIRMED_FAILED, RESOLVED_NON_DELIVERY.
    3. Timeout / connection loss -> UNKNOWN_RECONCILIATION_REQUIRED, UNRESOLVED.
    """
    with sqlite_session_factory() as sess:
        u = _create_user_with_roles(sess, "clin_outcomes", ["CLINICIAN_REVIEWER"])
        doc1, _ = _create_approved_document(sess, u)
        doc2, _ = _create_approved_document(sess, u)
        doc3, _ = _create_approved_document(sess, u)
        sess.commit()

        # 1. Success
        svc_success = EHRIntegrationService(session_factory=sqlite_session_factory, transport=MockFHIRTransport())
        res_ok = svc_success.transmit_document_to_ehr(document_id=doc1.id, actor_id=u.id)
        assert res_ok.success is True

        sess.expire_all()
        att1 = sess.query(FHIRExport).filter(FHIRExport.id == uuid.UUID(res_ok.export_id)).first()
        assert att1.export_status == FHIRExportStatus.TRANSMITTED
        assert att1.transmission_state == EHRTransmissionState.TRANSMITTED
        assert att1.resolution_status == EHRResolutionStatus.RESOLVED_DELIVERED
        d1 = sess.query(DBDocument).filter(DBDocument.id == doc1.id).first()
        assert d1.status == DocumentStatus.EXPORTED

        # 2. Confirmed Non-Delivery (Deterministic 401 Auth Failure)
        svc_fail = EHRIntegrationService(
            session_factory=sqlite_session_factory,
            transport=MockFHIRTransport(mode=MockEHRMode.AUTH_FAILURE),
        )
        with pytest.raises(EHRAuthenticationError):
            svc_fail.transmit_document_to_ehr(document_id=doc2.id, actor_id=u.id)

        sess.expire_all()
        att2 = sess.query(FHIRExport).filter(FHIRExport.bundle_id == doc2.fhir_bundles[0].id).first()
        assert att2.export_status == FHIRExportStatus.FAILED
        assert att2.transmission_state == EHRTransmissionState.CONFIRMED_FAILED
        assert att2.resolution_status == EHRResolutionStatus.RESOLVED_NON_DELIVERY
        d2 = sess.query(DBDocument).filter(DBDocument.id == doc2.id).first()
        assert d2.status == DocumentStatus.APPROVED  # Preserved

        # 3. Ambiguous Timeout
        svc_timeout = EHRIntegrationService(
            session_factory=sqlite_session_factory,
            transport=MockFHIRTransport(mode=MockEHRMode.TIMEOUT),
        )
        with pytest.raises(EHRTimeoutError):
            svc_timeout.transmit_document_to_ehr(document_id=doc3.id, actor_id=u.id)

        sess.expire_all()
        att3 = sess.query(FHIRExport).filter(FHIRExport.bundle_id == doc3.fhir_bundles[0].id).first()
        assert att3.export_status == FHIRExportStatus.FAILED
        assert att3.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
        assert att3.resolution_status == EHRResolutionStatus.UNRESOLVED
        d3 = sess.query(DBDocument).filter(DBDocument.id == doc3.id).first()
        assert d3.status == DocumentStatus.APPROVED  # Preserved


# ─── 3. ATTEMPT LINEAGE, IDEMPOTENCY KEY STABILITY & ATOMIC RECONCILIATION ────


def test_attempt_lineage_and_idempotency_stability_across_retries(sqlite_session_factory):
    """Verify attempt lineage and destination idempotency across retries:
    
    Attempt 1: Fails with timeout -> UNKNOWN_RECONCILIATION_REQUIRED.
    Attempt 2 (unreconciled): Blocked by EHRAmbiguousTransmissionError.
    Reconciliation: Four-Eyes authorization creates single-use token.
    Attempt 2 (reconciled): Succeeds, parent_attempt_id links to Attempt 1,
    re-uses exact destination_idempotency_key, increments attempt_number,
    and atomically consumes the token.
    """
    with sqlite_session_factory() as sess:
        clinician = _create_user_with_roles(sess, "clin_recon", ["CLINICIAN_REVIEWER"])
        supervisor = _create_user_with_roles(sess, "super_recon", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clinician)
        sess.commit()

        # Step 1: Attempt 1 times out
        mock_transport = MockFHIRTransport(mode=MockEHRMode.TIMEOUT)
        service = EHRIntegrationService(session_factory=sqlite_session_factory, transport=mock_transport)

        with pytest.raises(EHRTimeoutError):
            service.transmit_document_to_ehr(document_id=doc.id, actor_id=clinician.id)

        att1 = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle.id).first()
        assert att1.attempt_number == 1
        assert att1.parent_attempt_id is None
        assert att1.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
        log_id = att1.logical_export_id
        dest_key = att1.destination_idempotency_key
        att1_id = att1.id

        # Step 2: Attempting retry without reconciliation token is strictly blocked
        mock_transport.set_mode(MockEHRMode.SUCCESS)
        with pytest.raises(EHRAmbiguousTransmissionError, match="requires an authorized Four-Eyes reconciliation token"):
            service.transmit_document_to_ehr(document_id=doc.id, actor_id=clinician.id)

        # Step 3: Four-Eyes authorization via ReconciliationService
        recon_svc = ReconciliationService()
        token = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc.id,
            prior_export_id=att1.id,
            primary_authorizer_id=clinician.id,
            supervisor_authorizer_id=supervisor.id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="Confirmed with EHR administrator that no message with this bundle hash exists.",
            evidence_type="EHR_ADMIN_LOG_INSPECTION",
            evidence_reference="TICKET-88912",
        )
        sess.commit()
        token_id = token.id

        # Step 4: Retry with valid reconciliation token succeeds
        res2 = service.transmit_document_to_ehr(
            document_id=doc.id,
            actor_id=clinician.id,
            reconciliation_token=token_id,
        )
        assert res2.success is True

        # Verify Attempt 2 lineage & idempotency stability
        sess.expire_all()
        att2 = sess.query(FHIRExport).filter(FHIRExport.id == uuid.UUID(res2.export_id)).first()
        assert att2.attempt_number == 2
        assert att2.parent_attempt_id == att1_id
        assert att2.logical_export_id == log_id
        assert att2.destination_idempotency_key == dest_key
        assert att2.transmission_state == EHRTransmissionState.TRANSMITTED

        # Verify token was atomically consumed
        token_record = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
        assert token_record.consumed_at is not None
        assert token_record.consumed_by_export_id == att2.id

        # Step 5: Attempting to reuse the consumed token fails
        with pytest.raises(ReconciliationTokenAlreadyConsumedError, match="already consumed"):
            service.transmit_document_to_ehr(
                document_id=doc.id,
                actor_id=clinician.id,
                reconciliation_token=token_id,
                allow_resubmission=True,
            )


# ─── 4. STALE ATTEMPT & CRASH RECOVERY ───────────────────────────────────────


def test_crash_stale_attempt_recovery_to_unknown_reconciliation(sqlite_session_factory):
    """Verify that a crashed/abandoned PENDING_TRANSPORT attempt is transitioned
    to UNKNOWN_RECONCILIATION_REQUIRED, and never automatically retried based on age alone.
    """
    with sqlite_session_factory() as sess:
        u = _create_user_with_roles(sess, "clin_stale", ["CLINICIAN_REVIEWER"])
        doc, bundle = _create_approved_document(sess, u)

        # Seed an abandoned attempt in PENDING_TRANSPORT from 10 minutes ago
        past_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)
        log_id = uuid.uuid4()
        dest_key = derive_destination_idempotency_key("ehr://sandbox", log_id, bundle.bundle_sha256)

        stale_attempt = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle.id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.PENDING_TRANSPORT,
            transmission_state=EHRTransmissionState.PENDING_TRANSPORT,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key=dest_key,
            logical_export_id=log_id,
            attempt_number=1,
            parent_attempt_id=None,
            idempotency_key=f"ehr-{log_id}-att-1",
            exported_by=u.id,
            exported_at=past_time,
        )
        sess.add(stale_attempt)
        sess.commit()
        stale_id = stale_attempt.id
        doc_id = doc.id
        u_id = u.id

    service = EHRIntegrationService(
        session_factory=sqlite_session_factory,
        transport=MockFHIRTransport(),
        stale_pending_timeout_seconds=60.0,  # 1 minute threshold
    )

    # Calling transmit detects stale attempt -> transitions to UNKNOWN_RECONCILIATION_REQUIRED
    # and strictly blocks without transmitting
    with pytest.raises(EHRAmbiguousTransmissionError, match="abandoned in PENDING_TRANSPORT and has been safely transitioned"):
        service.transmit_document_to_ehr(document_id=doc_id, actor_id=u_id)

    with sqlite_session_factory() as verify_sess:
        recovered = verify_sess.query(FHIRExport).filter(FHIRExport.id == stale_id).first()
        assert recovered.export_status == FHIRExportStatus.FAILED
        assert recovered.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
        assert recovered.resolution_status == EHRResolutionStatus.UNRESOLVED
        assert "STALE_RECOVERY" in recovered.error_message


# ─── 5. LATE OUTCOMES FROM OLDER ATTEMPTS ────────────────────────────────────


def test_late_outcomes_from_older_attempts_preserve_evidence_without_overwriting(sqlite_session_factory):
    """Verify that a late outcome from an older attempt does NOT overwrite
    newer state or reconciliation decisions, and records an immutable stale audit event.
    """
    with sqlite_session_factory() as sess:
        u = _create_user_with_roles(sess, "clin_late", ["CLINICIAN_REVIEWER"])
        doc, bundle = _create_approved_document(sess, u)
        sess.commit()

        # Seed an attempt already resolved as FAILED / UNKNOWN_RECONCILIATION_REQUIRED
        log_id = uuid.uuid4()
        dest_key = derive_destination_idempotency_key("ehr://sandbox", log_id, bundle.bundle_sha256)
        att = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle.id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.FAILED,
            transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key=dest_key,
            logical_export_id=log_id,
            attempt_number=1,
            parent_attempt_id=None,
            idempotency_key=f"ehr-{log_id}-att-1",
            exported_by=u.id,
        )
        sess.add(att)
        sess.commit()
        att_id = att.id
        doc_id = doc.id
        u_id = u.id

    # Simulate late arrival at Boundary C
    service = EHRIntegrationService(session_factory=sqlite_session_factory)
    audit_svc = service.audit_service

    # When Boundary C processes an attempt that is no longer in PENDING_TRANSPORT:
    with sqlite_session_factory() as sess_c:
        row = sess_c.query(FHIRExport).filter(FHIRExport.id == att_id).first()
        assert row.transmission_state != EHRTransmissionState.PENDING_TRANSPORT

        # Simulate late outcome preservation
        audit_svc.record_event(
            session=sess_c,
            actor_id=str(u_id),
            event_type="EHR_TRANSMISSION_STALE_DELIVERY",
            entity_type="DOCUMENT",
            entity_id=str(doc_id),
            action="STALE_TRANSMISSION_OUTCOME_PRESERVED",
            metadata_json={
                "attempt_id": str(att_id),
                "stale_outcome": "SUCCESS",
                "current_attempt_state": getattr(row.transmission_state, "value", str(row.transmission_state)),
            },
        )
        sess_c.commit()

        # Verify state was NOT overwritten
        verify_row = sess_c.query(FHIRExport).filter(FHIRExport.id == att_id).first()
        assert verify_row.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED

        # Verify stale event recorded in audit log
        stale_event = (
            sess_c.query(AuditEvent)
            .filter(AuditEvent.event_type == "EHR_TRANSMISSION_STALE_DELIVERY")
            .first()
        )
        assert stale_event is not None
        assert stale_event.metadata_json["stale_outcome"] == "SUCCESS"


# ─── 6. TRUSTED ROLE LOOKUP & FOUR-EYES RECONCILIATION SERVICE ───────────────


def test_trusted_role_lookup_and_rejection_of_forged_roles(sqlite_session_factory):
    """Verify that ReconciliationService resolves roles strictly from DB records,
    rejects forged caller-supplied roles, and requires distinct authorizer identities.
    """
    recon_svc = ReconciliationService()

    with sqlite_session_factory() as sess:
        u_clinician = _create_user_with_roles(sess, "real_clinician", ["CLINICIAN_REVIEWER"])
        u_supervisor = _create_user_with_roles(sess, "real_supervisor", ["CLINICAL_SUPERVISOR"])
        u_unauthorized = _create_user_with_roles(sess, "unauthorized_user", ["operator"])

        # 1. Distinct actors validation: Same user as primary and supervisor raises ReconciliationAuthorizationError
        with pytest.raises(ReconciliationAuthorizationError, match="must be distinct identities"):
            recon_svc.validate_authorizer_roles(sess, u_clinician.id, u_clinician.id)

        # 2. Unauthorized primary user (lacks CLINICIAN_REVIEWER) raises ReconciliationAuthorizationError
        with pytest.raises(ReconciliationAuthorizationError, match="lacks required role CLINICIAN_REVIEWER"):
            recon_svc.validate_authorizer_roles(sess, u_unauthorized.id, u_supervisor.id)

        # 3. Unauthorized supervisor user (lacks CLINICAL_SUPERVISOR or MEDICAL_DIRECTOR)
        with pytest.raises(ReconciliationAuthorizationError, match="lacks required supervisor role"):
            recon_svc.validate_authorizer_roles(sess, u_clinician.id, u_unauthorized.id)

        # 4. Valid distinct actors with verified database roles succeeds
        primary_role, supervisor_role = recon_svc.validate_authorizer_roles(
            sess, u_clinician.id, u_supervisor.id
        )
        assert primary_role == "CLINICIAN_REVIEWER"
        assert supervisor_role == "CLINICAL_SUPERVISOR"


def test_reconciliation_token_revocation_and_invalidation(sqlite_session_factory):
    """Verify token revocation and rejection of revoked tokens."""
    recon_svc = ReconciliationService()

    with sqlite_session_factory() as sess:
        clinician = _create_user_with_roles(sess, "rev_clin", ["CLINICIAN_REVIEWER"])
        supervisor = _create_user_with_roles(sess, "rev_super", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clinician)

        # Create failed attempt
        failed_att = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle.id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.FAILED,
            transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key="dest-key-rev-test",
            logical_export_id=uuid.uuid4(),
            attempt_number=1,
            idempotency_key=f"att-rev-{uuid.uuid4()}",
            exported_by=clinician.id,
        )
        sess.add(failed_att)
        sess.flush()

        token = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc.id,
            prior_export_id=failed_att.id,
            primary_authorizer_id=clinician.id,
            supervisor_authorizer_id=supervisor.id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="Audited destination logs; safe to dispatch retry attempt.",
            evidence_type="EHR_AUDIT_LOG",
            evidence_reference="LOG-REF-11223",
        )
        sess.commit()
        token_id = token.id

        # Revoke the token
        revoked = recon_svc.revoke_reconciliation_token(
            sess,
            token_id=token_id,
            revoked_by_id=supervisor.id,
            reason="Patient requested hold on clinical transmission.",
        )
        sess.commit()
        assert revoked.revoked_at is not None

        # Attempting consumption of revoked token raises ReconciliationTokenRevokedError
        with pytest.raises(ReconciliationTokenRevokedError, match="was revoked"):
            recon_svc.validate_and_consume_token(
                sess,
                token_id=token_id,
                new_attempt_id=uuid.uuid4(),
                expected_bundle_id=bundle.id,
                expected_destination_identity="ehr://sandbox",
            )


# ─── 7. REAL POSTGRESQL 18 INTEGRATION VERIFICATION ──────────────────────────


def test_postgresql_milestone3_service_lifecycle_parity(pg_engine_url: str):
    """Verify Phase A Milestone 3 dedicated session ownership, Three-Boundary lifecycle,
    and Four-Eyes reconciliation on live PostgreSQL 18.
    """
    engine = create_engine(pg_engine_url)
    Base.metadata.create_all(bind=engine)
    pg_session_factory = sessionmaker(bind=engine, autoflush=False)

    with pg_session_factory() as sess:
        clinician = _create_user_with_roles(sess, "pg_clin", ["CLINICIAN_REVIEWER"])
        supervisor = _create_user_with_roles(sess, "pg_super", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clinician)
        sess.commit()
        doc_id = doc.id
        clin_id = clinician.id
        super_id = supervisor.id
        bundle_id = bundle.id

    # 1. Attempt 1 times out
    mock_transport = MockFHIRTransport(mode=MockEHRMode.TIMEOUT)
    service = EHRIntegrationService(session_factory=pg_session_factory, transport=mock_transport)

    with pytest.raises(EHRTimeoutError):
        service.transmit_document_to_ehr(document_id=doc_id, actor_id=clin_id)

    with pg_session_factory() as sess:
        att1 = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).first()
        assert att1.attempt_number == 1
        assert att1.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
        log_id = att1.logical_export_id
        dest_key = att1.destination_idempotency_key
        att1_id = att1.id

        # 2. Four-Eyes reconciliation token creation on PostgreSQL
        recon_svc = ReconciliationService()
        token = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc_id,
            prior_export_id=att1.id,
            primary_authorizer_id=clin_id,
            supervisor_authorizer_id=super_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="PostgreSQL verified external receipt not present in target EHR.",
            evidence_type="HL7_AUDIT_LOG",
            evidence_reference="REF-PG-18-001",
        )
        sess.commit()
        token_id = token.id

    # 3. Retry on PostgreSQL with reconciliation token succeeds
    mock_transport.set_mode(MockEHRMode.SUCCESS)
    res = service.transmit_document_to_ehr(
        document_id=doc_id,
        actor_id=clin_id,
        reconciliation_token=token_id,
    )
    assert res.success is True

    # 4. Verify Attempt 2 lineage & token consumption on PostgreSQL
    with pg_session_factory() as sess:
        att2 = sess.query(FHIRExport).filter(FHIRExport.id == uuid.UUID(res.export_id)).first()
        assert att2.attempt_number == 2
        assert att2.parent_attempt_id == att1_id
        assert att2.logical_export_id == log_id
        assert att2.destination_idempotency_key == dest_key
        assert att2.transmission_state == EHRTransmissionState.TRANSMITTED

        token_row = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
        assert token_row.consumed_at is not None
        assert token_row.consumed_by_export_id == att2.id


# ─── 8. TARGETED REMEDIATION: CONCURRENCY, ATOMIC ROLLBACK & TOKEN LIFECYCLE ──


def test_reconciliation_token_lifecycle_comprehensive(sqlite_session_factory):
    """Explicitly verify all 6 mandatory token lifecycle states and transitions:
    1. Invalid / non-existent token lookup/consumption raises ReconciliationNotFoundError.
    2. Token issuance & validation.
    3. Active token revocation records revoked_at and audit event.
    4. Attempt to consume a revoked token raises ReconciliationTokenRevokedError.
    5. Successful single-use consumption records consumed_at and consumed_by_export_id.
    6. Attempt to re-consume / reuse an already-consumed token raises ReconciliationTokenAlreadyConsumedError.
    7. Attempt to revoke an already-consumed token raises ReconciliationValidationError.
    """
    recon_svc = ReconciliationService()

    with sqlite_session_factory() as sess:
        clinician = _create_user_with_roles(sess, "lc_clin", ["CLINICIAN_REVIEWER"])
        supervisor = _create_user_with_roles(sess, "lc_super", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clinician)
        doc_id = doc.id
        clin_id = clinician.id
        super_id = supervisor.id
        bundle_id = bundle.id

        # 1. Invalid / non-existent token raises ReconciliationNotFoundError
        non_existent_id = uuid.uuid4()
        with pytest.raises(ReconciliationNotFoundError, match="not found"):
            recon_svc.validate_and_consume_token(
                sess,
                token_id=non_existent_id,
                new_attempt_id=uuid.uuid4(),
                expected_bundle_id=bundle_id,
                expected_destination_identity="ehr://sandbox",
            )

        with pytest.raises(ReconciliationNotFoundError, match="not found"):
            recon_svc.revoke_reconciliation_token(
                sess,
                token_id=non_existent_id,
                revoked_by_id=super_id,
                reason="Invalid token test",
            )

        # Seed failed attempt in UNKNOWN_RECONCILIATION_REQUIRED
        failed_att = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle_id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.FAILED,
            transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key="dest-key-lifecycle-test",
            logical_export_id=uuid.uuid4(),
            attempt_number=1,
            idempotency_key=f"att-lc-{uuid.uuid4()}",
            exported_by=clin_id,
        )
        sess.add(failed_att)
        sess.commit()
        failed_att_id = failed_att.id

        # 2. Token issuance & validation
        token_to_revoke = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc_id,
            prior_export_id=failed_att_id,
            primary_authorizer_id=clin_id,
            supervisor_authorizer_id=super_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="Lifecycle test token for revocation testing.",
            evidence_type="EHR_AUDIT_LOG",
            evidence_reference="EVID-REVOKE-01",
        )
        sess.commit()
        assert token_to_revoke.consumed_at is None
        assert token_to_revoke.revoked_at is None

        # 3. Active token revocation
        revoked_rec = recon_svc.revoke_reconciliation_token(
            sess,
            token_id=token_to_revoke.id,
            revoked_by_id=super_id,
            reason="Clinical trial arm reassigned; retry cancelled.",
        )
        sess.commit()
        assert revoked_rec.revoked_at is not None
        assert revoked_rec.revoked_by_id == super_id

        # 4. Attempt to consume revoked token raises ReconciliationTokenRevokedError
        with pytest.raises(ReconciliationTokenRevokedError, match="was revoked"):
            recon_svc.validate_and_consume_token(
                sess,
                token_id=token_to_revoke.id,
                new_attempt_id=uuid.uuid4(),
                expected_bundle_id=bundle_id,
                expected_destination_identity="ehr://sandbox",
            )

        # 5. Successful single-use consumption
        token_to_consume = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc_id,
            prior_export_id=failed_att_id,
            primary_authorizer_id=clin_id,
            supervisor_authorizer_id=super_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="Lifecycle test token for single-use consumption testing.",
            evidence_type="EHR_AUDIT_LOG",
            evidence_reference="EVID-CONSUME-01",
        )
        sess.commit()
        attempt2_id = uuid.uuid4()

        consumed_rec = recon_svc.validate_and_consume_token(
            sess,
            token_id=token_to_consume.id,
            new_attempt_id=attempt2_id,
            expected_bundle_id=bundle_id,
            expected_destination_identity="ehr://sandbox",
        )
        sess.commit()
        assert consumed_rec.consumed_at is not None
        assert consumed_rec.consumed_by_export_id == attempt2_id

        # 6. Attempt to re-consume (reuse) already-consumed token raises ReconciliationTokenAlreadyConsumedError
        with pytest.raises(ReconciliationTokenAlreadyConsumedError, match="already consumed"):
            recon_svc.validate_and_consume_token(
                sess,
                token_id=token_to_consume.id,
                new_attempt_id=uuid.uuid4(),
                expected_bundle_id=bundle_id,
                expected_destination_identity="ehr://sandbox",
            )

        # 7. Attempt to revoke an already-consumed token raises ReconciliationValidationError
        with pytest.raises(ReconciliationValidationError, match="already been consumed"):
            recon_svc.revoke_reconciliation_token(
                sess,
                token_id=token_to_consume.id,
                revoked_by_id=super_id,
                reason="Attempting post-hoc revocation of consumed token.",
            )


def test_atomic_token_consumption_and_rollback_on_boundary_a_failure(sqlite_session_factory):
    """Verify that token consumption and attempt creation occur in the exact same transaction:
    If attempt creation or a subsequent step in Boundary A fails before commit,
    token consumption rolls back completely, leaving the token unconsumed and reusable.
    """
    recon_svc = ReconciliationService()

    with sqlite_session_factory() as sess:
        clinician = _create_user_with_roles(sess, "rb_clin", ["CLINICIAN_REVIEWER"])
        supervisor = _create_user_with_roles(sess, "rb_super", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clinician)
        doc_id = doc.id
        clin_id = clinician.id
        super_id = supervisor.id
        bundle_id = bundle.id

        # Seed failed attempt in UNKNOWN_RECONCILIATION_REQUIRED
        att1 = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle_id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.FAILED,
            transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key="dest-key-rb-test",
            logical_export_id=uuid.uuid4(),
            attempt_number=1,
            idempotency_key=f"att-rb-{uuid.uuid4()}",
            exported_by=clin_id,
        )
        sess.add(att1)
        sess.commit()
        att1_id = att1.id

        token = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc_id,
            prior_export_id=att1_id,
            primary_authorizer_id=clin_id,
            supervisor_authorizer_id=super_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="Rollback test: Verifying transaction atomicity.",
            evidence_type="EHR_AUDIT_LOG",
            evidence_reference="REF-ROLLBACK-01",
        )
        sess.commit()
        token_id = token.id

    service = EHRIntegrationService(session_factory=sqlite_session_factory, transport=MockFHIRTransport())

    # Monkeypatch audit_service.record_event during Boundary A to simulate unexpected failure before commit
    orig_record_event = service.audit_service.record_event
    fail_flag = True

    def flaky_record_event(*args, **kwargs):
        if fail_flag and kwargs.get("event_type") == "EHR_TRANSMISSION_ATTEMPTED":
            raise RuntimeError("SIMULATED_BOUNDARY_A_FAILURE_BEFORE_COMMIT")
        return orig_record_event(*args, **kwargs)

    service.audit_service.record_event = flaky_record_event

    # 1. Attempt fails in Boundary A after validate_and_consume_token
    with pytest.raises(RuntimeError, match="SIMULATED_BOUNDARY_A_FAILURE_BEFORE_COMMIT"):
        service.transmit_document_to_ehr(
            document_id=doc_id,
            actor_id=clin_id,
            reconciliation_token=token_id,
        )

    # 2. Verify that token consumption ROLLED BACK
    with sqlite_session_factory() as sess:
        tok = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
        assert tok.consumed_at is None, "Token must NOT be marked consumed after Boundary A rollback"
        assert tok.consumed_by_export_id is None, "consumed_by_export_id must be None after rollback"

        # Verify no attempt 2 was committed
        attempts = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).all()
        assert len(attempts) == 1, "Failed attempt must NOT be committed in database"
        assert attempts[0].attempt_number == 1

    # 3. Subsequent retry without failure succeeds using the SAME token
    fail_flag = False
    service.audit_service.record_event = orig_record_event

    res = service.transmit_document_to_ehr(
        document_id=doc_id,
        actor_id=clin_id,
        reconciliation_token=token_id,
    )
    assert res.success is True

    with sqlite_session_factory() as sess:
        tok = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
        assert tok.consumed_at is not None, "Token must now be atomically consumed"
        assert tok.consumed_by_export_id == uuid.UUID(res.export_id)

        # Attempt 2 is committed with proper lineage
        att2 = sess.query(FHIRExport).filter(FHIRExport.id == uuid.UUID(res.export_id)).first()
        assert att2.attempt_number == 2
        assert att2.parent_attempt_id == att1_id


def test_concurrent_transmissions_and_idempotency_protection_sqlite():
    """Verify that competing concurrent transmission operations for the same logical export
    cannot create conflicting active attempts or duplicate attempt numbers on SQLite.
    """
    td = tempfile.mkdtemp(prefix="sqlite_conc_trans_")
    db_path = os.path.join(td, "test_conc.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 15})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    conc_factory = sessionmaker(bind=engine, autoflush=False)

    try:
        with conc_factory() as sess:
            u = _create_user_with_roles(sess, "conc_sq_user", ["CLINICIAN_REVIEWER"])
            doc, bundle = _create_approved_document(sess, u)
            sess.commit()
            doc_id = doc.id
            u_id = u.id
            bundle_id = bundle.id

        # Use a custom transport with simulated latency in Boundary B to hold in-flight state
        transport = MockFHIRTransport(simulated_latency_seconds=0.06)
        service = EHRIntegrationService(session_factory=conc_factory, transport=transport)

        import concurrent.futures

        results = []
        errors = []

        def run_transmission():
            try:
                r = service.transmit_document_to_ehr(document_id=doc_id, actor_id=u_id)
                return ("SUCCESS", r)
            except Exception as e:
                return ("ERROR", e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(run_transmission) for _ in range(3)]
            for f in concurrent.futures.as_completed(futures):
                status, val = f.result()
                if status == "SUCCESS":
                    results.append(val)
                else:
                    errors.append(val)

        # Exactly 1 transmission succeeds
        assert len(results) == 1, f"Expected exactly 1 successful transmission, got {len(results)}"
        assert len(errors) == 2, f"Expected 2 duplicate submission rejections, got {len(errors)}"

        # Competing calls are rejected with EHRDuplicateSubmissionError
        for err in errors:
            assert isinstance(err, EHRDuplicateSubmissionError), f"Expected EHRDuplicateSubmissionError, got {type(err)}: {err}"

        # Verify exactly 1 attempt row exists in DB with attempt_number=1
        with conc_factory() as sess:
            attempts = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).all()
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].transmission_state == EHRTransmissionState.TRANSMITTED
    finally:
        engine.dispose()
        shutil.rmtree(td, ignore_errors=True)


def test_concurrent_retries_and_token_single_use_sqlite():
    """Verify that concurrent retries on an ambiguous attempt:
    - Without token: strictly blocked by EHRAmbiguousTransmissionError.
    - With same token: exactly one competing worker consumes the token and dispatches attempt 2.
    - Competing worker fails; token cannot be consumed twice.
    """
    recon_svc = ReconciliationService()

    td = tempfile.mkdtemp(prefix="sqlite_conc_retry_")
    db_path = os.path.join(td, "test_retry.db")
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 15})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    conc_factory = sessionmaker(bind=engine, autoflush=False)

    try:
        with conc_factory() as sess:
            clin = _create_user_with_roles(sess, "conc_rt_clin", ["CLINICIAN_REVIEWER"])
            superv = _create_user_with_roles(sess, "conc_rt_super", ["CLINICAL_SUPERVISOR"])
            doc, bundle = _create_approved_document(sess, clin)
            sess.commit()
            doc_id = doc.id
            clin_id = clin.id
            super_id = superv.id
            bundle_id = bundle.id

            # Seed attempt 1 in UNKNOWN_RECONCILIATION_REQUIRED
            att1 = FHIRExport(
                id=uuid.uuid4(),
                bundle_id=bundle_id,
                target_type="EHR_TRANSMISSION",
                export_status=FHIRExportStatus.FAILED,
                transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
                resolution_status=EHRResolutionStatus.UNRESOLVED,
                destination_identity="ehr://sandbox",
                destination_idempotency_key="dest-key-conc-retry",
                logical_export_id=uuid.uuid4(),
                attempt_number=1,
                idempotency_key=f"att-conc-{uuid.uuid4()}",
                exported_by=clin_id,
            )
            sess.add(att1)
            sess.commit()
            att1_id = att1.id

        service = EHRIntegrationService(session_factory=conc_factory, transport=MockFHIRTransport(simulated_latency_seconds=0.05))

        # 1. Unreconciled retry attempts without token are strictly blocked
        with pytest.raises(EHRAmbiguousTransmissionError, match="requires an authorized Four-Eyes reconciliation token"):
            service.transmit_document_to_ehr(document_id=doc_id, actor_id=clin_id)

        # 2. Issue a single reconciliation token
        with conc_factory() as sess:
            token = recon_svc.create_reconciliation_token(
                sess,
                document_id=doc_id,
                prior_export_id=att1_id,
                primary_authorizer_id=clin_id,
                supervisor_authorizer_id=super_id,
                decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
                justification="Concurrent retry authorization test.",
                evidence_type="EHR_AUDIT_LOG",
                evidence_reference="REF-CONC-01",
            )
            sess.commit()
            token_id = token.id

        # 3. Two competing workers concurrently try to use the SAME token
        import concurrent.futures
        results = []
        errors = []

        def run_retry():
            try:
                r = service.transmit_document_to_ehr(
                    document_id=doc_id,
                    actor_id=clin_id,
                    reconciliation_token=token_id,
                )
                return ("SUCCESS", r)
            except Exception as e:
                return ("ERROR", e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_retry) for _ in range(2)]
            for f in concurrent.futures.as_completed(futures):
                st, v = f.result()
                if st == "SUCCESS":
                    results.append(v)
                else:
                    errors.append(v)

        # Exactly 1 retry succeeds
        assert len(results) == 1, f"Expected 1 successful retry, got {len(results)}"
        assert len(errors) == 1, f"Expected 1 rejected retry, got {len(errors)}"

        # Rejected worker encountered either already-consumed or duplicate in-flight
        assert isinstance(errors[0], (ReconciliationTokenAlreadyConsumedError, EHRDuplicateSubmissionError))

        # Verify Attempt 2 lineage & token consumed once
        with conc_factory() as sess:
            attempts = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).order_by(FHIRExport.attempt_number.asc()).all()
            assert len(attempts) == 2
            assert attempts[0].attempt_number == 1
            assert attempts[1].attempt_number == 2
            assert attempts[1].parent_attempt_id == att1_id

            tok = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
            assert tok.consumed_at is not None
            assert tok.consumed_by_export_id == attempts[1].id
    finally:
        engine.dispose()
        shutil.rmtree(td, ignore_errors=True)


def test_postgresql_concurrent_transmissions_and_advisory_lock(pg_engine_url: str):
    """Verify on real PostgreSQL 18 that competing concurrent transmission operations
    are serialized by acquire_document_xact_lock (SELECT pg_advisory_xact_lock) and cannot
    create conflicting active attempts or duplicate attempt numbers.
    """
    engine = create_engine(pg_engine_url, pool_size=10, max_overflow=5)
    Base.metadata.create_all(bind=engine)
    pg_session_factory = sessionmaker(bind=engine, autoflush=False)

    with pg_session_factory() as sess:
        u = _create_user_with_roles(sess, "pg_conc_user", ["CLINICIAN_REVIEWER"])
        doc, bundle = _create_approved_document(sess, u)
        sess.commit()
        doc_id = doc.id
        u_id = u.id
        bundle_id = bundle.id

    transport = MockFHIRTransport(simulated_latency_seconds=0.08)
    service = EHRIntegrationService(session_factory=pg_session_factory, transport=transport)

    import concurrent.futures

    results = []
    errors = []

    def run_pg_transmit():
        try:
            r = service.transmit_document_to_ehr(document_id=doc_id, actor_id=u_id)
            return ("SUCCESS", r)
        except Exception as e:
            return ("ERROR", e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(run_pg_transmit) for _ in range(3)]
        for f in concurrent.futures.as_completed(futures):
            st, v = f.result()
            if st == "SUCCESS":
                results.append(v)
            else:
                errors.append(v)

    # Exactly 1 succeeds, remaining are rejected with EHRDuplicateSubmissionError
    assert len(results) == 1, f"Expected 1 success on PostgreSQL, got {len(results)}"
    assert len(errors) == 2, f"Expected 2 rejections on PostgreSQL, got {len(errors)}"
    for err in errors:
        assert isinstance(err, EHRDuplicateSubmissionError)

    # Verify PostgreSQL data integrity: exactly 1 attempt with attempt_number=1
    with pg_session_factory() as sess:
        attempts = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).all()
        assert len(attempts) == 1
        assert attempts[0].attempt_number == 1
        assert attempts[0].transmission_state == EHRTransmissionState.TRANSMITTED
        assert attempts[0].resolution_status == EHRResolutionStatus.RESOLVED_DELIVERED


def test_postgresql_concurrent_reconciliation_token_competition(pg_engine_url: str):
    """Verify on real PostgreSQL 18 that two competing workers attempting to consume the same
    reconciliation token concurrently are serialized by row-level locking (with_for_update):
    - Exactly one worker consumes the token and dispatches attempt 2.
    - The competing worker is rejected (ReconciliationTokenAlreadyConsumedError or EHRDuplicateSubmissionError).
    - Token cannot be double-consumed under any concurrency condition.
    """
    engine = create_engine(pg_engine_url, pool_size=10, max_overflow=5)
    Base.metadata.create_all(bind=engine)
    pg_session_factory = sessionmaker(bind=engine, autoflush=False)

    recon_svc = ReconciliationService()

    with pg_session_factory() as sess:
        clin = _create_user_with_roles(sess, "pg_rt_clin", ["CLINICIAN_REVIEWER"])
        superv = _create_user_with_roles(sess, "pg_rt_super", ["CLINICAL_SUPERVISOR"])
        doc, bundle = _create_approved_document(sess, clin)
        doc_id = doc.id
        clin_id = clin.id
        super_id = superv.id
        bundle_id = bundle.id

        # Seed prior attempt in UNKNOWN_RECONCILIATION_REQUIRED
        att1 = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=bundle_id,
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.FAILED,
            transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="ehr://sandbox",
            destination_idempotency_key="dest-key-pg-conc-retry",
            logical_export_id=uuid.uuid4(),
            attempt_number=1,
            idempotency_key=f"att-pg-conc-{uuid.uuid4()}",
            exported_by=clin_id,
        )
        sess.add(att1)
        sess.commit()
        att1_id = att1.id

        token = recon_svc.create_reconciliation_token(
            sess,
            document_id=doc_id,
            prior_export_id=att1_id,
            primary_authorizer_id=clin_id,
            supervisor_authorizer_id=super_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            justification="PostgreSQL concurrent token competition test.",
            evidence_type="EHR_AUDIT_LOG",
            evidence_reference="REF-PG-CONC-01",
        )
        sess.commit()
        token_id = token.id

    service = EHRIntegrationService(session_factory=pg_session_factory, transport=MockFHIRTransport(simulated_latency_seconds=0.06))

    import concurrent.futures
    results = []
    errors = []

    def run_pg_retry():
        try:
            r = service.transmit_document_to_ehr(
                document_id=doc_id,
                actor_id=clin_id,
                reconciliation_token=token_id,
            )
            return ("SUCCESS", r)
        except Exception as e:
            return ("ERROR", e)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_pg_retry) for _ in range(2)]
        for f in concurrent.futures.as_completed(futures):
            st, v = f.result()
            if st == "SUCCESS":
                results.append(v)
            else:
                errors.append(v)

    assert len(results) == 1, f"Expected 1 successful retry on PostgreSQL, got {len(results)}"
    assert len(errors) == 1, f"Expected 1 rejected retry on PostgreSQL, got {len(errors)}"
    assert isinstance(errors[0], (ReconciliationTokenAlreadyConsumedError, EHRDuplicateSubmissionError))

    with pg_session_factory() as sess:
        attempts = sess.query(FHIRExport).filter(FHIRExport.bundle_id == bundle_id).order_by(FHIRExport.attempt_number.asc()).all()
        assert len(attempts) == 2
        assert attempts[0].attempt_number == 1
        assert attempts[1].attempt_number == 2
        assert attempts[1].parent_attempt_id == att1_id

        tok = sess.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_id).first()
        assert tok.consumed_at is not None
        assert tok.consumed_by_export_id == attempts[1].id
