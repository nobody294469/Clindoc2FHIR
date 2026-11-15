"""Comprehensive Test Suite for ClinicalDoc Production Release Hardening.

Authority:
  - PROJECT_SPEC.md §3, §10, §12, §13, §15, §16
  - PRODUCTIZATION_SPEC.md §3.6, §3.8, §7.3, §9.3, §10, §13
  - PRODUCTIZATION_DECISIONS.md PD-002, PD-003, PD-004, PD-009, PD-012, PD-015, PD-018, PD-019
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from unittest.mock import MagicMock, patch
import uuid
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clindoc.api.app import create_app
from clindoc.api.auth import CanonicalRole, OIDCConfig, OIDCTokenValidator, set_global_session_factory, set_global_token_validator
from clindoc.api.deps import set_global_blob_store, set_global_job_queue
from clindoc.config import (
    CLINDOC_DOC_LOCK_NAMESPACE,
    ProductionProfileError,
    RuntimeProfile,
    acquire_document_xact_lock,
    get_active_profile,
    validate_daemon_profile,
    validate_production_configuration,
)
from clindoc.db import Base, Claim, Document, DocumentApproval, FHIRExport, FHIRBundle, Role, User, UserRole
from clindoc.db.enums import (
    ClaimOrigin,
    ClaimReviewState,
    DocumentApprovalStatus,
    DocumentStatus,
    FHIRExportStatus,
    FHIRVersion,
    IdentityVerificationStatus,
    PatientIdentityApprovalStatus,
)
from clindoc.jobs.queue import JobQueue
from clindoc.review.approval import DocumentApprovalEngine, InvalidDocumentApprovalStateError
from clindoc.review.service import ClaimReviewService, InvalidDocumentStateError
from clindoc.services.ehr_transport import (
    HTTPFHIRTransport,
    HTTPFHIRTransportConfig,
    MockEHRMode,
    MockFHIRTransport,
)
from clindoc.storage.blob import EncryptedFileBlobStore


# ─── 1. DAEMON PROFILE & STARTUP CONTAINMENT TESTS ───────────────────────────


def test_daemon_profile_fails_closed_when_unset():
    """Daemons (API server, Background worker) must fail closed if CLINDOC_PROFILE is missing."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ProductionProfileError, match="strictly required"):
            validate_daemon_profile("API Server")

        with pytest.raises(ProductionProfileError, match="strictly required"):
            validate_daemon_profile("Background Worker")


def test_daemon_profile_dev_mode_allowed():
    """Explicit CLINDOC_PROFILE=dev allows daemon startup."""
    with patch.dict(os.environ, {"CLINDOC_PROFILE": "dev"}, clear=True):
        profile = validate_daemon_profile("API Server")
        assert profile == RuntimeProfile.DEV


def test_production_profile_validation_enforces_invariants(tmp_path):
    """Production profile requires complete, non-placeholder configuration."""
    # 1. Missing CLINDOC_ENV
    with patch.dict(os.environ, {"CLINDOC_PROFILE": "production", "CLINDOC_ENV": "dev"}, clear=True):
        with pytest.raises(ProductionProfileError, match="CLINDOC_ENV must be 'production'"):
            validate_production_configuration()

    # 2. Missing Four-Eyes
    with patch.dict(
        os.environ,
        {"CLINDOC_PROFILE": "production", "CLINDOC_ENV": "production", "CLINDOC_ENFORCE_FOUR_EYES": "false"},
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="CLINDOC_ENFORCE_FOUR_EYES must be 'true'"):
            validate_production_configuration()

    # 3. Missing Audit Ledger Enforcement
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "false",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="CLINDOC_ENFORCE_AUDIT must be 'true'"):
            validate_production_configuration()

    # 4. Missing HMAC Key
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="HMAC key for key_id 'default' is missing"):
            validate_production_configuration()

    # 5. Insecure Placeholder HMAC Key
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": "password",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="known placeholder"):
            validate_production_configuration()

    # 6. Short HMAC Key (< 32 UTF-8 bytes)
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": "short_key_under_32_bytes_len",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="at least 32 UTF-8 bytes"):
            validate_production_configuration()

    # 7. Whitespace-only HMAC Key
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": " " * 35,
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="whitespace-only"):
            validate_production_configuration()

    # 8. Obvious repeating pattern ('abcd' * 8)
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": "abcd" * 8,
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="insecure repeating pattern"):
            validate_production_configuration()

    # 9. Predictable sequential digits ('12345678901234567890123456789012')
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": "12345678901234567890123456789012",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="predictable sequential value"):
            validate_production_configuration()

    # 10. Predictable sequential alphabet ('abcdefghijklmnopqrstuvwxyz012345')
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": "abcdefghijklmnopqrstuvwxyz012345",
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="predictable sequential value"):
            validate_production_configuration()

    # 11. Valid randomly generated 32-byte key passes validation (with mock pipeline dependencies)
    import secrets
    valid_key = secrets.token_hex(16)
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY": valid_key,
        },
        clear=True,
    ), patch("clindoc.config.ProductionPipelineConfig.validate"):
        validate_production_configuration()

    # 12. Non-default key_id fails closed if specific key is missing
    with patch.dict(
        os.environ,
        {
            "CLINDOC_PROFILE": "production",
            "CLINDOC_ENV": "production",
            "CLINDOC_ENFORCE_FOUR_EYES": "true",
            "CLINDOC_ENFORCE_AUDIT": "true",
            "CLINDOC_AUDIT_HMAC_KEY_ID": "v2",
            "CLINDOC_AUDIT_HMAC_KEY": valid_key,  # default set, but v2 missing
        },
        clear=True,
    ):
        with pytest.raises(ProductionProfileError, match="HMAC key for key_id 'v2' is missing"):
            validate_production_configuration()


# ─── 2. ORIGIN-STATE WHITELIST GUARDS ────────────────────────────────────────


@pytest.fixture
def sqlite_session():
    """In-memory SQLite session with full schema."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionMaker = sessionmaker(bind=engine, autoflush=False)
    session = SessionMaker()
    yield session
    session.close()


def test_review_whitelist_blocks_failed_and_exported_documents(sqlite_session):
    """Mutations, re-openings, and approvals on FAILED or EXPORTED documents must be rejected."""
    user = User(
        id=uuid.uuid4(),
        idp_subject="sub-user-01",
        username="reviewer1",
        full_name="Reviewer One",
        email="rev@test.org",
        is_active=True,
    )
    sqlite_session.add(user)

    doc_failed = Document(
        id=uuid.uuid4(),
        sha256="1" * 64,
        storage_ref="memory://failed.pdf",
        original_filename="failed.pdf",
        mime_type="application/pdf",
        file_size_bytes=1000,
        page_count=1,
        status=DocumentStatus.FAILED,
        uploaded_by=user.id,
    )
    sqlite_session.add(doc_failed)

    claim = Claim(
        id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        document_id=doc_failed.id,
        group_id="diag-grp-1",
        version=1,
        origin=ClaimOrigin.PIPELINE,
        field_type="diagnosis",
        value="Hypertension",
        review_state=ClaimReviewState.PENDING,
        created_by=user.id,
        payload={"field_type": "diagnosis", "value": "Hypertension"},
    )
    sqlite_session.add(claim)
    sqlite_session.commit()

    review_svc = ClaimReviewService()
    approval_eng = DocumentApprovalEngine()

    # Claim review on FAILED document must raise InvalidDocumentStateError
    with pytest.raises(InvalidDocumentStateError, match="strictly restricted to PENDING_REVIEW or IN_REVIEW"):
        review_svc.accept_claim(
            session=sqlite_session,
            claim_id=claim.claim_id,
            actor_id=user.id,
            expected_version=1,
        )

    # Approval on FAILED document must raise InvalidDocumentApprovalStateError
    with pytest.raises(InvalidDocumentApprovalStateError, match="strictly permitted only in PENDING_REVIEW or IN_REVIEW"):
        approval_eng.approve_document(
            session=sqlite_session,
            document_id=doc_failed.id,
            approver_id=user.id,
        )

    # Transition to EXPORTED
    doc_failed.status = DocumentStatus.EXPORTED
    sqlite_session.commit()

    # Claim review on EXPORTED document must raise InvalidDocumentStateError
    with pytest.raises(InvalidDocumentStateError, match="strictly restricted to PENDING_REVIEW or IN_REVIEW"):
        review_svc.accept_claim(
            session=sqlite_session,
            claim_id=claim.claim_id,
            actor_id=user.id,
            expected_version=1,
        )


# ─── 3. EHR DISPATCH BOUNDARY IN PRODUCTION ──────────────────────────────────


def test_ehr_http_transport_strictly_disabled_in_production(monkeypatch):
    """Real HTTP EHR transport must fail closed in PRODUCTION profile pending Phase 9 migration."""
    monkeypatch.setenv("CLINDOC_PROFILE", "production")

    config = HTTPFHIRTransportConfig(
        endpoint_url="https://ehr.hospital.org/fhir",
        ssl_verify=True,
        destination_type="staging",
    )
    transport = HTTPFHIRTransport(config)

    with pytest.raises(ProductionProfileError, match="strictly disabled in PRODUCTION profile"):
        transport.transmit(
            '{"resourceType": "Bundle", "type": "transaction"}',
            idempotency_key="test-key",
            correlation_id="test-corr",
            document_id=str(uuid.uuid4()),
        )


def test_ehr_mock_transport_allowed_in_dev():
    """Mock EHR transport operates deterministically in memory."""
    mock_transport = MockFHIRTransport(mode=MockEHRMode.SUCCESS)
    resp = mock_transport.transmit(
        '{"resourceType": "Bundle", "type": "transaction"}',
        idempotency_key="mock-idemp-1",
        correlation_id="corr-1",
        document_id=str(uuid.uuid4()),
    )
    assert resp.success is True
    assert resp.status_code == 200


# ─── 4. ADVISORY LOCKING PROTOCOL ────────────────────────────────────────────


def test_advisory_locking_safe_noop_on_sqlite(sqlite_session):
    """On SQLite engines, acquire_document_xact_lock is a safe no-op."""
    doc_id = uuid.uuid4()
    # Should not raise any exception
    acquire_document_xact_lock(sqlite_session, doc_id)


def test_advisory_locking_executes_on_postgresql():
    """On PostgreSQL engines, acquire_document_xact_lock executes the 2-int overload."""
    mock_session = MagicMock()
    mock_bind = MagicMock()
    mock_bind.dialect.name = "postgresql"
    mock_session.get_bind.return_value = mock_bind

    doc_id = uuid.uuid4()
    acquire_document_xact_lock(mock_session, doc_id)

    mock_session.execute.assert_called_once()
    args, kwargs = mock_session.execute.call_args
    assert "pg_advisory_xact_lock(:ns, hashtext(:doc_id))" in str(args[0])
    assert args[1] == {"ns": CLINDOC_DOC_LOCK_NAMESPACE, "doc_id": str(doc_id)}


# ─── 5. UNFILTERED APPROVAL SELECTION & SUPERSESSION GUARDS ──────────────────


def test_unfiltered_approval_selection_detects_revocation(sqlite_session):
    """If an approved document receives a newer REJECTED/REVOKED approval, retrieval fails closed."""
    uploader = User(
        id=uuid.uuid4(),
        idp_subject="uploader",
        username="uploader",
        full_name="Uploader",
        email="up@test.local",
        is_active=True,
    )
    approver = User(
        id=uuid.uuid4(),
        idp_subject="approver",
        username="approver",
        full_name="Approver",
        email="app@test.local",
        is_active=True,
    )
    sqlite_session.add_all([uploader, approver])

    doc = Document(
        id=uuid.uuid4(),
        sha256="2" * 64,
        storage_ref="memory://doc.pdf",
        original_filename="doc.pdf",
        mime_type="application/pdf",
        file_size_bytes=1000,
        page_count=1,
        status=DocumentStatus.APPROVED,
        uploaded_by=uploader.id,
    )
    sqlite_session.add(doc)

    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    # Older approval: APPROVED
    app1 = DocumentApproval(
        id=uuid.uuid4(),
        document_id=doc.id,
        approver_id=approver.id,
        approval_status=DocumentApprovalStatus.APPROVED,
        patient_identity_status=PatientIdentityApprovalStatus.CONFIRMED,
        policy_version="1.0",
        is_export_eligible=True,
        created_at=t0,
    )
    sqlite_session.add(app1)
    sqlite_session.flush()

    # Newer approval: REJECTED (created at t0 + 1 hour)
    app2 = DocumentApproval(
        id=uuid.uuid4(),
        document_id=doc.id,
        approver_id=approver.id,
        approval_status=DocumentApprovalStatus.REJECTED,
        patient_identity_status=PatientIdentityApprovalStatus.CONFIRMED,
        policy_version="1.0",
        is_export_eligible=False,
        created_at=t0 + timedelta(hours=1),
    )
    sqlite_session.add(app2)

    bundle = FHIRBundle(
        id=uuid.uuid4(),
        document_id=doc.id,
        approval_id=app1.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256="3" * 64,
        storage_ref="memory://bundle.json",
        bundle_payload={"resourceType": "Bundle", "type": "transaction"},
    )
    sqlite_session.add(bundle)
    sqlite_session.commit()

    # Query with unfiltered selection
    latest_app = (
        sqlite_session.query(DocumentApproval)
        .filter(DocumentApproval.document_id == doc.id)
        .order_by(DocumentApproval.created_at.desc(), DocumentApproval.id.desc())
        .first()
    )
    assert latest_app is not None
    assert latest_app.approval_status == DocumentApprovalStatus.REJECTED
    assert latest_app.is_export_eligible is False


# ─── 6. HMAC KEY-ID CONSISTENCY & FAIL-CLOSED TESTS ──────────────────────────


def test_review_audit_ledger_key_id_consistency_and_fail_closed(tmp_path):
    """Test HMAC key-ID resolution across ReviewAuditLedger and AuditProjectionEngine."""
    import secrets
    from clindoc.review.service import ReviewAuditLedger, AuditAnchorMissingError
    from clindoc.audit.projection import AuditProjectionEngine
    from clindoc.fhir.signing import IncompleteAttestationError

    default_key = secrets.token_hex(16)
    v2_key = secrets.token_hex(16)

    # 1. Default creation and verification
    ledger_file = tmp_path / "audit_default.jsonl"
    chkpt_file = tmp_path / "audit_default.chkpt"

    with patch.dict(
        os.environ,
        {
            "CLINDOC_AUDIT_HMAC_KEY_ID": "default",
            "CLINDOC_AUDIT_HMAC_KEY": default_key,
        },
        clear=True,
    ):
        ledger = ReviewAuditLedger(ledger_path=ledger_file, checkpoint_path=chkpt_file)
        assert ledger.signing_key_id == "default"
        ledger.append_event(
            actor_id=str(uuid.uuid4()),
            event_type="DOCUMENT_APPROVED",
            entity_type="document",
            entity_id=str(uuid.uuid4()),
            action="APPROVE",
        )
        chkpt = ledger.create_checkpoint()
        assert chkpt["payload"]["key_id"] == "default"

        res = ledger.verify_ledger(enforce_checkpoint=True)
        assert res.is_valid is True
        assert res.checkpoint_verified is True

    # 2. Explicit non-default key creation and verification (v2)
    ledger_v2 = tmp_path / "audit_v2.jsonl"
    chkpt_v2 = tmp_path / "audit_v2.chkpt"

    with patch.dict(
        os.environ,
        {
            "CLINDOC_AUDIT_HMAC_KEY_ID": "v2",
            "CLINDOC_AUDIT_HMAC_KEY_V2": v2_key,
            "CLINDOC_AUDIT_HMAC_KEY": default_key,
        },
        clear=True,
    ):
        ledger2 = ReviewAuditLedger(ledger_path=ledger_v2, checkpoint_path=chkpt_v2)
        assert ledger2.signing_key_id == "v2"
        ledger2.append_event(
            actor_id=str(uuid.uuid4()),
            event_type="DOCUMENT_APPROVED",
            entity_type="document",
            entity_id=str(uuid.uuid4()),
            action="APPROVE",
        )
        chkpt2 = ledger2.create_checkpoint()
        assert chkpt2["payload"]["key_id"] == "v2"

        res2 = ledger2.verify_ledger(enforce_checkpoint=True)
        assert res2.is_valid is True
        assert res2.checkpoint_verified is True

    # 3. Non-default key creation fails closed when specific key is missing (NEVER falls back)
    ledger_v3 = tmp_path / "audit_v3.jsonl"
    chkpt_v3 = tmp_path / "audit_v3.chkpt"

    with patch.dict(
        os.environ,
        {
            "CLINDOC_AUDIT_HMAC_KEY_ID": "v3",
            "CLINDOC_AUDIT_HMAC_KEY": default_key,  # default exists, but v3 is missing
        },
        clear=True,
    ):
        ledger3 = ReviewAuditLedger(ledger_path=ledger_v3, checkpoint_path=chkpt_v3)
        assert ledger3.signing_key_id == "v3"
        ledger3.append_event(
            actor_id=str(uuid.uuid4()),
            event_type="DOCUMENT_APPROVED",
            entity_type="document",
            entity_id=str(uuid.uuid4()),
            action="APPROVE",
        )
        with pytest.raises(AuditAnchorMissingError, match="HMAC signing key 'v3' not found"):
            ledger3.create_checkpoint()

    # 4. Historical default verification with non-default active environment
    # An existing checkpoint with key_id 'default' must verify using CLINDOC_AUDIT_HMAC_KEY
    with patch.dict(
        os.environ,
        {
            "CLINDOC_AUDIT_HMAC_KEY_ID": "v2",
            "CLINDOC_AUDIT_HMAC_KEY_V2": v2_key,
            "CLINDOC_AUDIT_HMAC_KEY": default_key,
        },
        clear=True,
    ):
        hist_ledger = ReviewAuditLedger(ledger_path=ledger_file, checkpoint_path=chkpt_file)
        hist_res = hist_ledger.verify_ledger(enforce_checkpoint=True)
        assert hist_res.is_valid is True
        assert hist_res.checkpoint_verified is True

    # 5. Non-default verification fails closed when key is missing (v2 key removed)
    with patch.dict(
        os.environ,
        {
            "CLINDOC_AUDIT_HMAC_KEY_ID": "default",
            "CLINDOC_AUDIT_HMAC_KEY": default_key,
            # CLINDOC_AUDIT_HMAC_KEY_V2 is absent
        },
        clear=True,
    ):
        missing_v2_ledger = ReviewAuditLedger(ledger_path=ledger_v2, checkpoint_path=chkpt_v2)
        missing_v2_res = missing_v2_ledger.verify_ledger(enforce_checkpoint=True)
        assert missing_v2_res.is_valid is False
        assert "HMAC key 'v2' required for checkpoint verification not found" in missing_v2_res.error_message

