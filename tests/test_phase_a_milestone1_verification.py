"""Milestone 1 Targeted Defect Correction & Schema Verification Test Suite (Phase A).

Authority:
  - Phase A Frozen Architectural Specification
  - chk_fhir_exports_lifecycle check constraint
  - chk_fhir_exports_attempt_lineage check constraint
  - chk_reconciliation_distinct_actors check constraint
  - uq_fhir_exports_logical_attempt unique constraint
"""

from __future__ import annotations

import datetime
import hashlib
import json
import uuid
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

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
    FHIRExportMigrationAudit,
    User,
    VerificationResult as DBVerificationResult,
)
from clindoc.observability.metrics import ClinDocMetrics, set_metrics
from clindoc.services.ehr_integration import (
    EHRIntegrationService,
    EHRTransmissionResult,
)
from clindoc.services.ehr_transport import MockFHIRTransport


# ─── FIXTURES & HELPERS ───────────────────────────────────────────────────────

@pytest.fixture
def db_session() -> Session:
    """In-memory SQLite database session initialized with full Phase A tables & constraints."""
    from sqlalchemy import event
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False)
    session = SessionLocal()
    session.session_factory = SessionLocal
    yield session
    session.close()


@pytest.fixture
def metrics_cleanup():
    set_metrics(ClinDocMetrics())
    yield
    set_metrics(None)


def _create_approved_document(session: Session, clinician: User) -> DBDocument:
    """Helper to create a valid approved document eligible for export."""
    doc = DBDocument(
        uploaded_by=clinician.id,
        source_type="scanned_pdf",
        original_filename="pilot_test.pdf",
        storage_ref="blobs/test/pilot_test.pdf",
        mime_type="application/pdf",
        file_size_bytes=2048,
        sha256="abc12345def67890",
        status=DocumentStatus.APPROVED,
        identity_verification_status=IdentityVerificationStatus.CONFIRMED,
        identity_confirmed_by=clinician.id,
        patient_context_id="PAT-TEST-001",
        page_count=1,
    )
    session.add(doc)
    session.flush()

    ev = DBEvidence(
        document_id=doc.id,
        page_number=1,
        char_start=0,
        char_end=20,
        evidence_text="Metformin 500mg BID",
        payload={
            "document_id": str(doc.id),
            "evidence_id": str(uuid.uuid4()),
            "page_number": 1,
            "char_start": 0,
            "char_end": 20,
            "text": "Metformin 500mg BID",
            "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2},
            "ocr_confidence": 0.99,
        },
    )
    session.add(ev)
    session.flush()

    claim_uuid = uuid.uuid4()
    claim = DBClaim(
        id=claim_uuid,
        claim_id=claim_uuid,
        document_id=doc.id,
        evidence_id=ev.id,
        group_id="med-group-1",
        field_type="med.name",
        value="Metformin",
        review_state=ClaimReviewState.ACCEPTED,
        payload={
            "claim_id": str(claim_uuid),
            "document_id": str(doc.id),
            "group_id": "med-group-1",
            "field_type": "med.name",
            "value": "Metformin",
            "extraction_method": "deterministic",
            "extraction_confidence": 0.95,
            "evidence_id": str(ev.id),
            "region_id": "reg-1",
        },
    )
    session.add(claim)
    session.flush()

    ver = DBVerificationResult(
        claim_id=claim.id,
        verification_stage=VerificationStage.STAGE_A,
        label=VerificationLabel.SUPPORTED,
        calibrated_confidence=0.99,
        provider_kind="deterministic",
        is_fallback=False,
        payload={
            "verification_id": str(uuid.uuid4()),
            "claim_id": str(claim.claim_id),
            "stage_reached": "A",
            "label": "SUPPORTED",
            "is_fallback": False,
            "confidence": 0.99,
            "evidence_id": str(ev.id),
        },
    )
    session.add(ver)
    session.flush()

    approval = DocumentApproval(
        document_id=doc.id,
        approver_id=clinician.id,
        approval_status=DocumentApprovalStatus.APPROVED,
        patient_identity_status=PatientIdentityApprovalStatus.CONFIRMED,
        policy_version="1.0",
        is_export_eligible=True,
        identity_notes="Verified by clinician",
    )
    session.add(approval)
    session.flush()
    return doc


# ─── 1. RESOLVE EHR SUCCESS LIFECYCLE CONTRADICTION ─────────────────────────

def test_ehr_successful_transmission_satisfies_lifecycle_constraint(db_session: Session, metrics_cleanup):
    """Verify that a successful EHR transmission writes TRANSMITTED / TRANSMITTED / RESOLVED_DELIVERED.
    
    Proves:
      1. Canonical status is TRANSMITTED (not COMPLETED).
      2. Transaction commits successfully with chk_fhir_exports_lifecycle active.
      3. Any attempt to write COMPLETED with EHR_TRANSMISSION raises IntegrityError.
    """
    clinician = User(
        idp_subject="sub-dr-smith",
        username="dr_smith",
        full_name="Dr. Smith",
        email="smith@hospital.org",
    )
    db_session.add(clinician)
    db_session.flush()

    doc = _create_approved_document(db_session, clinician)

    mock_transport = MockFHIRTransport()
    audit_svc = AuditService()
    service = EHRIntegrationService(audit_service=audit_svc, transport=mock_transport)

    # Execute actual transmission
    res: EHRTransmissionResult = service.transmit_document_to_ehr(
        db_session,
        document_id=doc.id,
        actor_id=clinician.id,
        destination_type="sandbox",
        correlation_id="corr-m1-001",
    )

    assert res.success is True
    assert res.status_code == 200

    # Query persisted FHIRExport row
    db_session.expire_all()
    export_record = db_session.query(FHIRExport).filter(FHIRExport.id == uuid.UUID(res.export_id)).first()
    assert export_record is not None
    assert export_record.target_type == "EHR_TRANSMISSION"
    assert export_record.export_status == FHIRExportStatus.TRANSMITTED
    assert export_record.transmission_state == EHRTransmissionState.TRANSMITTED
    assert export_record.resolution_status == EHRResolutionStatus.RESOLVED_DELIVERED
    assert export_record.destination_identity == "ehr://sandbox"

    # Commit must succeed under engine check constraint
    db_session.commit()

    # Counterexample proof: Attempting to write export_status="COMPLETED" on EHR_TRANSMISSION must fail
    illegal_export = FHIRExport(
        bundle_id=export_record.bundle_id,
        target_type="EHR_TRANSMISSION",
        export_status="COMPLETED",  # Illegal under chk_fhir_exports_lifecycle
        transmission_state=EHRTransmissionState.TRANSMITTED,
        resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
        destination_identity="ehr://sandbox",
        idempotency_key=f"illegal-key-{uuid.uuid4()}",
        exported_by=clinician.id,
        exported_at=datetime.datetime.now(datetime.timezone.utc),
    )
    db_session.add(illegal_export)
    with pytest.raises(IntegrityError, match="chk_fhir_exports_lifecycle"):
        db_session.commit()
    db_session.rollback()


# ─── 2. FOUR-EYES RECONCILIATION ACTOR CONSTRAINT ───────────────────────────

def test_reconciliation_distinct_actors_constraint(db_session: Session):
    """Verify that primary authorizer and supervisor authorizer must be distinct actors.
    
    Database check constraint chk_reconciliation_distinct_actors blocks identical IDs.
    """
    actor1 = User(idp_subject="sub-1", username="doc1", full_name="Dr. 1", email="1@h.org")
    actor2 = User(idp_subject="sub-2", username="doc2", full_name="Dr. 2", email="2@h.org")
    db_session.add_all([actor1, actor2])
    db_session.flush()

    doc = _create_approved_document(db_session, actor1)
    approval = db_session.query(DocumentApproval).filter(DocumentApproval.document_id == doc.id).first()
    bundle = FHIRBundle(
        document_id=doc.id,
        approval_id=approval.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256="abc12345" * 8,
        storage_ref="test/ref",
        bundle_payload={},
        validation_report={"is_valid": True},
    )
    db_session.add(bundle)
    db_session.flush()

    export = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
        resolution_status=EHRResolutionStatus.UNRESOLVED,
        destination_identity="https://ehr.hospital.org/r4",
        idempotency_key=f"exp-{uuid.uuid4()}",
        exported_by=actor1.id,
    )
    db_session.add(export)
    db_session.flush()

    # Case A: Distinct actors -> Commits cleanly
    recon_valid = EHRTransmissionReconciliation(
        document_id=doc.id,
        bundle_id=bundle.id,
        prior_export_id=export.id,
        destination_identity="https://ehr.hospital.org/r4",
        bundle_sha256="abc12345" * 8,
        destination_idempotency_key="dest-key-001",
        decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
        justification="Verified in destination EHR audit log; record not present",
        evidence_type="DIRECT_EHR_ADMIN_AUDIT",
        evidence_reference="AUDIT-TICKET-7712",
        verification_level="CLINICAL_CHIEF_REVIEW",
        primary_authorizer_id=actor1.id,
        primary_authorizer_role="CLINICIAN_REVIEWER",
        supervisor_authorizer_id=actor2.id,
        supervisor_authorizer_role="CLINICAL_SUPERVISOR",
    )
    db_session.add(recon_valid)
    db_session.commit()
    assert recon_valid.id is not None

    # Case B: Identical actors (actor1 == actor1) -> Blocked by database check constraint
    recon_invalid = EHRTransmissionReconciliation(
        document_id=doc.id,
        bundle_id=bundle.id,
        prior_export_id=export.id,
        destination_identity="https://ehr.hospital.org/r4",
        bundle_sha256="abc12345" * 8,
        destination_idempotency_key="dest-key-002",
        decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
        justification="Self-authorized retry without distinct supervisor",
        evidence_type="DIRECT_EHR_ADMIN_AUDIT",
        evidence_reference="AUDIT-TICKET-7713",
        verification_level="CLINICAL_CHIEF_REVIEW",
        primary_authorizer_id=actor1.id,
        primary_authorizer_role="CLINICIAN_REVIEWER",
        supervisor_authorizer_id=actor1.id,  # VIOLATION: identical to primary authorizer
        supervisor_authorizer_role="CLINICAL_SUPERVISOR",
    )
    db_session.add(recon_invalid)
    with pytest.raises(IntegrityError, match="chk_reconciliation_distinct_actors"):
        db_session.commit()
    db_session.rollback()


# ─── 3. ATTEMPT-LINEAGE & UNIQUE CONSTRAINTS ─────────────────────────────────

def test_attempt_lineage_and_uniqueness_constraints(db_session: Session):
    """Verify attempt lineage rules:
    
    1. First attempt must have attempt_number=1 and parent_attempt_id=None.
    2. Attempt with attempt_number=1 and parent_attempt_id != None is rejected by CHECK constraint.
    3. Attempt with attempt_number > 1 and parent_attempt_id = None is rejected by CHECK constraint.
    4. Subsequent attempts link to parent and increment attempt_number.
    5. Duplicate (logical_export_id, attempt_number) is rejected by UNIQUE constraint.
    """
    user = User(idp_subject="sub-lineage", username="doc_lin", full_name="Dr. Lin", email="lin@h.org")
    db_session.add(user)
    db_session.flush()

    doc = _create_approved_document(db_session, user)
    approval = db_session.query(DocumentApproval).filter(DocumentApproval.document_id == doc.id).first()
    bundle = FHIRBundle(
        document_id=doc.id,
        approval_id=approval.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256="lin12345" * 8,
        storage_ref="test/ref",
        bundle_payload={},
        validation_report={"is_valid": True},
    )
    db_session.add(bundle)
    db_session.flush()

    logical_id = uuid.uuid4()
    dest_key = "dest-key-lineage-test"

    # 1. Attempt 1 valid: attempt_number=1, parent=None
    att1 = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
        resolution_status=EHRResolutionStatus.UNRESOLVED,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=1,
        parent_attempt_id=None,
        idempotency_key=f"att-1-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att1)
    db_session.commit()
    assert att1.attempt_number == 1
    assert att1.parent_attempt_id is None

    # 2. Attempt 1 INVALID: attempt_number=1 with a parent_attempt_id -> rejected by CHECK
    att1_invalid = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.CONFIRMED_FAILED,
        resolution_status=EHRResolutionStatus.RESOLVED_NON_DELIVERY,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=uuid.uuid4(),
        attempt_number=1,
        parent_attempt_id=att1.id,  # VIOLATION: attempt 1 cannot have parent
        idempotency_key=f"att-1-inv-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att1_invalid)
    with pytest.raises(IntegrityError, match="chk_fhir_exports_attempt_lineage"):
        db_session.commit()
    db_session.rollback()

    # 3. Attempt 2 INVALID: attempt_number=2 without parent_attempt_id -> rejected by CHECK
    att2_orphan = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.CONFIRMED_FAILED,
        resolution_status=EHRResolutionStatus.RESOLVED_NON_DELIVERY,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=2,
        parent_attempt_id=None,  # VIOLATION: attempt > 1 must have parent
        idempotency_key=f"att-2-orph-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att2_orphan)
    with pytest.raises(IntegrityError, match="chk_fhir_exports_attempt_lineage"):
        db_session.commit()
    db_session.rollback()

    # 4. Attempt 2 VALID: attempt_number=2 with parent_attempt_id=att1.id -> succeeds
    att2_valid = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.TRANSMITTED,
        transmission_state=EHRTransmissionState.TRANSMITTED,
        resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=2,
        parent_attempt_id=att1.id,  # Valid pointer to predecessor
        idempotency_key=f"att-2-val-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att2_valid)
    db_session.commit()
    assert att2_valid.parent_attempt_id == att1.id

    # 5. Duplicate attempt number collision: attempt_number=2 again for same logical_export_id -> rejected by UNIQUE
    att2_dup = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.TRANSMITTED,
        transmission_state=EHRTransmissionState.TRANSMITTED,
        resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=2,  # DUPLICATE of att2_valid under same logical_export_id
        parent_attempt_id=att1.id,
        idempotency_key=f"att-2-dup-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att2_dup)
    with pytest.raises(IntegrityError, match=r"(UNIQUE constraint failed|uq_fhir_exports_logical_attempt)"):
        db_session.commit()
    db_session.rollback()


# ─── 4. DESTINATION IDEMPOTENCY KEY DERIVATION & STABILITY ───────────────────

def test_destination_idempotency_key_derivation_isolation():
    """Verify deterministic destination idempotency key derivation:
    
    Formula: SHA256(canonical_destination + ":" + logical_export_id + ":" + canonical_bundle_sha256)
    
    Proves:
      1. Key remains identical across multiple attempts of the SAME logical export.
      2. Two SEPARATE logical exports with the exact same destination and bundle content
         generate mathematically distinct destination idempotency keys.
      3. Canonicalization normalizes uppercase, whitespace, default ports, and trailing slashes.
      4. Formula strictly uses colons, not pipes or other delimiters.
    """
    from clindoc.services.ehr_integration import (
        canonicalize_destination_identity,
        derive_destination_idempotency_key,
    )

    dest = "https://ehr.hospital.org/r4"
    bundle_hash = "abcdef1234567890" * 4

    logical_id_1 = str(uuid.uuid4())
    logical_id_2 = str(uuid.uuid4())

    # Direct helper matches explicit colon formula
    expected_data = f"{dest}:{logical_id_1}:{bundle_hash}"
    expected_key = hashlib.sha256(expected_data.encode("utf-8")).hexdigest()
    assert derive_destination_idempotency_key(dest, logical_id_1, bundle_hash) == expected_key

    # Pipe-delimited formula is explicitly distinct and NOT used
    pipe_key = hashlib.sha256(f"{dest}|{logical_id_1}|{bundle_hash}".encode("utf-8")).hexdigest()
    assert expected_key != pipe_key

    # Attempt 1 and Attempt 2 of logical export 1: Identical key
    key_log1_att1 = derive_destination_idempotency_key(dest, logical_id_1, bundle_hash)
    key_log1_att2 = derive_destination_idempotency_key(dest, logical_id_1, bundle_hash)
    assert key_log1_att1 == key_log1_att2

    # Canonicalization equality across variants (scheme, whitespace, port 443, trailing slash)
    variant_dest = "  HTTPS://EHR.Hospital.Org:443/r4/  "
    key_variant = derive_destination_idempotency_key(variant_dest, logical_id_1, bundle_hash)
    assert key_variant == key_log1_att1

    # Separate logical export 2: Must NOT share idempotency key
    key_log2 = derive_destination_idempotency_key(dest, logical_id_2, bundle_hash)
    assert key_log1_att1 != key_log2


# ─── 5. MIGRATION AUDIT TABLE NAME CONFORMANCE ───────────────────────────────

def test_migration_audit_table_name():
    """Assert FHIRExportMigrationAudit compiles with exact singular table name."""
    assert FHIRExportMigrationAudit.__tablename__ == "fhir_export_migration_audit"
    assert "fhir_export_migration_audit" in Base.metadata.tables
    assert "fhir_export_migration_audits" not in Base.metadata.tables


# ─── 6. ATTEMPT LINEAGE RESTRICT & RECONCILIATION RETENTION ──────────────────

def test_parent_attempt_deletion_is_restricted(db_session: Session):
    """Verify that deleting a parent attempt with an existing child attempt is blocked by ON DELETE RESTRICT.
    
    Protects attempt lineage from being silently broken or turned into invalid orphan states.
    """
    user = User(idp_subject="sub-del-lin", username="doc_lin_del", full_name="Dr. Lin Del", email="lindel@h.org")
    db_session.add(user)
    db_session.flush()

    doc = _create_approved_document(db_session, user)
    approval = db_session.query(DocumentApproval).filter(DocumentApproval.document_id == doc.id).first()
    bundle = FHIRBundle(
        document_id=doc.id,
        approval_id=approval.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256="del12345" * 8,
        storage_ref="test/ref",
        bundle_payload={},
        validation_report={"is_valid": True},
    )
    db_session.add(bundle)
    db_session.flush()

    logical_id = uuid.uuid4()
    dest_key = "dest-key-del-test"

    # Attempt 1
    att1 = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
        resolution_status=EHRResolutionStatus.UNRESOLVED,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=1,
        parent_attempt_id=None,
        idempotency_key=f"att-1-del-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att1)
    db_session.commit()

    # Attempt 2 (references Attempt 1 as parent)
    att2 = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.TRANSMITTED,
        transmission_state=EHRTransmissionState.TRANSMITTED,
        resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
        destination_identity="https://ehr.hospital.org/r4",
        destination_idempotency_key=dest_key,
        logical_export_id=logical_id,
        attempt_number=2,
        parent_attempt_id=att1.id,
        idempotency_key=f"att-2-del-{uuid.uuid4()}",
        exported_by=user.id,
    )
    db_session.add(att2)
    db_session.commit()

    # Deleting att1 must be blocked because att2 has parent_attempt_id pointing to it
    db_session.delete(att1)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.commit()
    db_session.rollback()

    # Verify att1 and att2 still exist and lineage is fully intact
    att1_reloaded = db_session.get(FHIRExport, att1.id)
    att2_reloaded = db_session.get(FHIRExport, att2.id)
    assert att1_reloaded is not None
    assert att2_reloaded is not None
    assert att2_reloaded.parent_attempt_id == att1.id


def test_reconciliation_evidence_deletion_is_restricted(db_session: Session):
    """Verify that deleting a document, bundle, or prior export is blocked by ON DELETE RESTRICT
    when referenced by an EHRTransmissionReconciliation record.
    
    Protects clinical reconciliation authorization, justification, and audit evidence from being deleted.
    """
    user1 = User(idp_subject="sub-rec-1", username="rec_u1", full_name="Dr. Rec 1", email="rec1@h.org")
    user2 = User(idp_subject="sub-rec-2", username="rec_u2", full_name="Dr. Rec 2", email="rec2@h.org")
    db_session.add_all([user1, user2])
    db_session.flush()

    doc = _create_approved_document(db_session, user1)
    approval = db_session.query(DocumentApproval).filter(DocumentApproval.document_id == doc.id).first()
    bundle = FHIRBundle(
        document_id=doc.id,
        approval_id=approval.id,
        fhir_version=FHIRVersion.R4B,
        bundle_sha256="recbundle" * 8,
        storage_ref="test/ref",
        bundle_payload={},
        validation_report={"is_valid": True},
    )
    db_session.add(bundle)
    db_session.flush()

    prior_export = FHIRExport(
        bundle_id=bundle.id,
        target_type="EHR_TRANSMISSION",
        export_status=FHIRExportStatus.FAILED,
        transmission_state=EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
        resolution_status=EHRResolutionStatus.UNRESOLVED,
        destination_identity="https://ehr.hospital.org/r4",
        idempotency_key=f"exp-rec-{uuid.uuid4()}",
        exported_by=user1.id,
    )
    db_session.add(prior_export)
    db_session.flush()

    recon = EHRTransmissionReconciliation(
        document_id=doc.id,
        bundle_id=bundle.id,
        prior_export_id=prior_export.id,
        destination_identity="https://ehr.hospital.org/r4",
        bundle_sha256="recbundle" * 8,
        destination_idempotency_key="dest-key-rec-001",
        decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
        justification="Verified in EHR admin audit; confirmed non-delivery",
        evidence_type="DIRECT_EHR_ADMIN_AUDIT",
        evidence_reference="TICKET-8899",
        verification_level="CLINICAL_CHIEF_REVIEW",
        primary_authorizer_id=user1.id,
        primary_authorizer_role="CLINICIAN_REVIEWER",
        supervisor_authorizer_id=user2.id,
        supervisor_authorizer_role="CLINICAL_SUPERVISOR",
    )
    db_session.add(recon)
    db_session.commit()

    # 1. Attempt to delete prior_export -> Must fail RESTRICT
    db_session.delete(prior_export)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.commit()
    db_session.rollback()

    # 2. Attempt to delete bundle -> Must fail RESTRICT
    db_session.delete(bundle)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.commit()
    db_session.rollback()

    # 3. Attempt to delete document -> Must fail RESTRICT
    db_session.delete(doc)
    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
        db_session.commit()
    db_session.rollback()

    # Assert reconciliation record is completely preserved
    recon_reloaded = db_session.get(EHRTransmissionReconciliation, recon.id)
    assert recon_reloaded is not None
    assert recon_reloaded.evidence_reference == "TICKET-8899"

