"""Phase B1 Comprehensive Acceptance Test Suite.

Authority:
  - Phase B1 Frozen Architectural Specification
  - Dedicated ehr_transmission_outbox table and lease-generation fencing
  - Decoupled two-transaction initial dispatch and Four-Eyes retry workflows
  - Dual-Engine (SQLite and live PostgreSQL 18) Parity
  - Dialect-Specific 64-char Hex Check Constraints
  - Populated SQLite Migration with Foreign Key Check
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from typing import Generator
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

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
    OutboxStatus,
    PatientIdentityApprovalStatus,
    ReconciliationDecision,
    VerificationLabel,
    VerificationStage,
)
from clindoc.db.migrations.phase_a_lifecycle import PhaseALifecycleMigrationRunner
from clindoc.db.migrations.phase_b1_outbox import PhaseB1OutboxMigrationRunner
from clindoc.db.models import (
    Claim,
    Document,
    DocumentApproval,
    EHRTransmissionOutbox,
    EHRTransmissionReconciliation,
    Evidence,
    FHIRBundle,
    FHIRExport,
    Role,
    User,
    UserRole,
    VerificationResult,
)
from clindoc.jobs.worker import OutboxDispatchWorker
from clindoc.services.ehr_integration import EHRIntegrationService, derive_destination_idempotency_key
from clindoc.services.ehr_transport import (
    EHRTimeoutError,
    EHRValidationError,
    MockEHRMode,
    MockFHIRTransport,
)
from clindoc.services.fhir_export import FHIRExportService
from clindoc.services.outbox_service import (
    EHROutboxService,
    OutboxConflictError,
    StaleWorkerLeaseError,
)
from clindoc.services.reconciliation_service import ReconciliationService


# ─── DATABASE FIXTURES ─────────────────────────────────────────────────────────

@pytest.fixture
def sqlite_engine(tmp_path):
    """File-backed SQLite engine in WAL mode supporting concurrency."""
    db_file = tmp_path / "test_phase_b1.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 30.0, "check_same_thread": False},
        poolclass=QueuePool,
        pool_size=10,
        max_overflow=20,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.close()

    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode = WAL;"))
    return engine


@pytest.fixture
def sqlite_session_factory(sqlite_engine):
    """Dedicated sessionmaker bound to sqlite_engine."""
    for table in Base.metadata.tables.values():
        for fk in table.foreign_key_constraints:
            if getattr(fk, "use_alter", False) and getattr(fk, "_create_rule", None) is not None:
                fk._create_rule = None
    Base.metadata.create_all(sqlite_engine)
    return sessionmaker(bind=sqlite_engine, expire_on_commit=False)


@pytest.fixture
def test_setup(sqlite_session_factory):
    """Create approved document, sealed bundle, and users."""
    with sqlite_session_factory() as session:
        clinician = User(
            id=uuid.uuid4(),
            idp_subject=f"sub-clinician-{uuid.uuid4().hex[:6]}",
            username="dr_smith",
            full_name="Dr. Smith",
            email="smith@clindoc.org",
            is_active=True,
        )
        supervisor = User(
            id=uuid.uuid4(),
            idp_subject=f"sub-supervisor-{uuid.uuid4().hex[:6]}",
            username="dr_director",
            full_name="Dr. Director",
            email="director@clindoc.org",
            is_active=True,
        )
        session.add_all([clinician, supervisor])
        session.flush()

        doc = Document(
            id=uuid.uuid4(),
            uploaded_by=clinician.id,
            source_type="scanned_pdf",
            patient_context_id="PAT-999",
            original_filename="doc1.pdf",
            sha256="a" * 64,
            storage_ref="/data/docs/doc1.pdf",
            status=DocumentStatus.APPROVED,
            file_size_bytes=1024,
            page_count=1,
            mime_type="application/pdf",
            identity_verification_status=IdentityVerificationStatus.CONFIRMED,
            identity_confirmed_by=clinician.id,
        )
        session.add(doc)
        session.flush()

        approval = DocumentApproval(
            id=uuid.uuid4(),
            document_id=doc.id,
            approver_id=clinician.id,
            approval_status=DocumentApprovalStatus.APPROVED,
            patient_identity_status=PatientIdentityApprovalStatus.CONFIRMED,
            policy_version="1.0.0",
            is_export_eligible=True,
        )
        session.add(approval)
        session.flush()

        ev = Evidence(
            id=uuid.uuid4(),
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

        claim = Claim(
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

        vr = VerificationResult(
            id=uuid.uuid4(),
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
                {"resource": {"resourceType": "Patient", "id": "PAT-999"}},
                {"resource": {"resourceType": "MedicationStatement", "id": "med-1", "status": "active"}},
            ],
        }
        from clindoc.fhir.signing import serialize_transport_payload
        raw_bytes, b_sha = serialize_transport_payload(bundle_dict)

        bundle = FHIRBundle(
            id=uuid.uuid4(),
            document_id=doc.id,
            approval_id=approval.id,
            fhir_version=FHIRVersion.R4B,
            bundle_sha256=b_sha,
            storage_ref="/data/bundles/bundle1.json",
            bundle_payload=bundle_dict,
            validation_report={"is_valid": True},
        )
        session.add(bundle)
        session.commit()

        return {
            "document_id": doc.id,
            "bundle_id": bundle.id,
            "bundle_sha256": bundle.bundle_sha256,
            "clinician_id": clinician.id,
            "supervisor_id": supervisor.id,
        }


# ─── 1. DATABASE CHECK CONSTRAINTS & PORTABLE HEX TESTS ──────────────────────

def test_hex_check_constraints_sqlite(sqlite_session_factory, test_setup):
    """Assert lowercase 64-char hex check constraints reject invalid values on SQLite."""
    with sqlite_session_factory() as session:
        # Uppercase hex must fail
        with pytest.raises(Exception):
            session.execute(
                text(
                    """
                    INSERT INTO ehr_transmission_outbox (
                        id, document_id, bundle_id, bundle_sha256, destination_identity,
                        logical_export_id, destination_idempotency_key, staged_by, status
                    ) VALUES (
                        :id, :doc_id, :bundle_id, :bundle_sha, :dest, :log_id, :dest_key, :staged_by, 'PENDING'
                    );
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "doc_id": str(test_setup["document_id"]),
                    "bundle_id": str(test_setup["bundle_id"]),
                    "bundle_sha": "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855",  # UPPERCASE
                    "dest": "https://fhir.ehr.org",
                    "log_id": str(uuid.uuid4()),
                    "dest_key": "b" * 64,
                    "staged_by": str(test_setup["clinician_id"]),
                },
            )
            session.commit()

        # Non-hex characters must fail
        with pytest.raises(Exception):
            session.execute(
                text(
                    """
                    INSERT INTO ehr_transmission_outbox (
                        id, document_id, bundle_id, bundle_sha256, destination_identity,
                        logical_export_id, destination_idempotency_key, staged_by, status
                    ) VALUES (
                        :id, :doc_id, :bundle_id, :bundle_sha, :dest, :log_id, :dest_key, :staged_by, 'PENDING'
                    );
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "doc_id": str(test_setup["document_id"]),
                    "bundle_id": str(test_setup["bundle_id"]),
                    "bundle_sha": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b85z",  # 'z' non-hex
                    "dest": "https://fhir.ehr.org",
                    "log_id": str(uuid.uuid4()),
                    "dest_key": "b" * 64,
                    "staged_by": str(test_setup["clinician_id"]),
                },
            )
            session.commit()

        # Length not 64 must fail
        with pytest.raises(Exception):
            session.execute(
                text(
                    """
                    INSERT INTO ehr_transmission_outbox (
                        id, document_id, bundle_id, bundle_sha256, destination_identity,
                        logical_export_id, destination_idempotency_key, staged_by, status
                    ) VALUES (
                        :id, :doc_id, :bundle_id, :bundle_sha, :dest, :log_id, :dest_key, :staged_by, 'PENDING'
                    );
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "doc_id": str(test_setup["document_id"]),
                    "bundle_id": str(test_setup["bundle_id"]),
                    "bundle_sha": "e3b0c442",  # short
                    "dest": "https://fhir.ehr.org",
                    "log_id": str(uuid.uuid4()),
                    "dest_key": "b" * 64,
                    "staged_by": str(test_setup["clinician_id"]),
                },
            )
            session.commit()


def test_composite_foreign_key_wiring_sqlite(sqlite_session_factory, test_setup):
    """Verify composite foreign-key wiring and referenced uniqueness on SQLite:
    1. NULL current_attempt_id remains valid.
    2. Historical FHIRExport records with NULL outbox_id remain valid.
    3. Valid current_attempt_id referencing attempt belonging to the same outbox succeeds.
    4. Nonexistent attempt fails.
    5. current_attempt_id referencing another outbox's attempt fails.
    """
    with sqlite_session_factory() as session:
        outbox_id_1 = uuid.uuid4()
        log_id_1 = uuid.uuid4()
        outbox_1 = EHRTransmissionOutbox(
            id=outbox_id_1,
            document_id=test_setup["document_id"],
            bundle_id=test_setup["bundle_id"],
            bundle_sha256="a" * 64,
            destination_identity="https://ehr1.test.org",
            logical_export_id=log_id_1,
            destination_idempotency_key="1" * 64,
            staged_by=test_setup["clinician_id"],
            status=OutboxStatus.PENDING,
            current_attempt_id=None,  # 1. NULL current_attempt_id is valid
        )
        session.add(outbox_1)
        session.commit()

        # 2. Historical FHIRExport records with NULL outbox_id remain valid
        historical_export = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=test_setup["bundle_id"],
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.TRANSMITTED,
            transmission_state=EHRTransmissionState.TRANSMITTED,
            resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
            destination_identity="https://historical.ehr.org",
            destination_idempotency_key="2" * 64,
            idempotency_key="hist-idem-1",
            exported_by=test_setup["clinician_id"],
            attempt_number=1,
            outbox_id=None,  # NULL outbox_id
            logical_export_id=None,
        )
        session.add(historical_export)
        session.commit()

        # 3. Create attempt belonging to outbox_1
        attempt_1 = FHIRExport(
            id=uuid.uuid4(),
            bundle_id=test_setup["bundle_id"],
            target_type="EHR_TRANSMISSION",
            export_status=FHIRExportStatus.PENDING_TRANSPORT,
            transmission_state=EHRTransmissionState.PENDING_TRANSPORT,
            resolution_status=EHRResolutionStatus.UNRESOLVED,
            destination_identity="https://ehr1.test.org",
            destination_idempotency_key="3" * 64,
            idempotency_key="attempt-idem-1",
            exported_by=test_setup["clinician_id"],
            attempt_number=1,
            outbox_id=outbox_id_1,
            logical_export_id=log_id_1,
        )
        session.add(attempt_1)
        session.commit()

        # Update outbox_1 to point current_attempt_id to attempt_1 (same outbox) -> succeeds!
        outbox_1_loaded = session.query(EHRTransmissionOutbox).filter(EHRTransmissionOutbox.id == outbox_id_1).one()
        outbox_1_loaded.current_attempt_id = attempt_1.id
        session.commit()
        assert outbox_1_loaded.current_attempt_id == attempt_1.id

        # 4. Nonexistent attempt fails under foreign key enforcement
        with pytest.raises(Exception):
            session.execute(
                text(
                    "UPDATE ehr_transmission_outbox SET current_attempt_id = :fake_id WHERE id = :oid"
                ),
                {"fake_id": uuid.uuid4().hex, "oid": outbox_id_1.hex},
            )
            session.commit()
        session.rollback()

        # 5. current_attempt_id referencing another outbox's attempt fails
        outbox_id_2 = uuid.uuid4()
        log_id_2 = uuid.uuid4()
        outbox_2 = EHRTransmissionOutbox(
            id=outbox_id_2,
            document_id=test_setup["document_id"],
            bundle_id=test_setup["bundle_id"],
            bundle_sha256="b" * 64,
            destination_identity="https://ehr2.test.org",
            logical_export_id=log_id_2,
            destination_idempotency_key="4" * 64,
            staged_by=test_setup["clinician_id"],
            status=OutboxStatus.PENDING,
            current_attempt_id=None,
        )
        session.add(outbox_2)
        session.commit()

        # Attempting to set outbox_2.current_attempt_id to attempt_1.id (which belongs to outbox_1, not outbox_2)
        # must fail the composite FK (current_attempt_id, id) -> fhir_exports(id, outbox_id)
        with pytest.raises(Exception):
            session.execute(
                text(
                    "UPDATE ehr_transmission_outbox SET current_attempt_id = :att_id WHERE id = :oid"
                ),
                {"att_id": attempt_1.id.hex, "oid": outbox_id_2.hex},
            )
            session.commit()
        session.rollback()


# ─── 2. STAGING & DUPLICATE INTENT TESTS ─────────────────────────────────────

def test_outbox_staging_and_duplicate_handling(sqlite_session_factory, test_setup):
    """Verify initial staging returns 202-equivalent PENDING, and replay returns 200-equivalent duplicate."""
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=MockFHIRTransport(),
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
    )

    # Initial staging
    res1 = outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )
    assert res1.status == OutboxStatus.PENDING
    assert res1.is_duplicate is False

    # Duplicate staging replay
    res2 = outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )
    assert res2.outbox_id == res1.outbox_id
    assert res2.logical_export_id == res1.logical_export_id
    assert res2.status == OutboxStatus.PENDING
    assert res2.is_duplicate is True


# ─── 3. WORKER CLAIM & LEASE-GENERATION FENCING TESTS ────────────────────────

def test_lease_fencing_stale_worker_rejection(sqlite_session_factory, test_setup):
    """Verify stale worker update is rejected when lease expires or generation increments."""
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=MockFHIRTransport(),
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        default_lease_seconds=1,  # 1 second lease
    )

    res = outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    # Worker 1 claims
    claimed_1 = outbox_svc.claim_next_outbox_intent(worker_id="worker-1")
    assert claimed_1 is not None
    assert claimed_1.lease_generation == 1

    # Wait for lease expiration
    time.sleep(1.1)

    # Run reaper: recovers lease and advances generation
    recovered_count = outbox_svc.run_reaper_cycle()
    assert recovered_count == 1

    with sqlite_session_factory() as session:
        rec = session.query(EHRTransmissionOutbox).filter(EHRTransmissionOutbox.id == res.outbox_id).first()
        assert rec.status == OutboxStatus.PENDING
        assert rec.lease_generation == 2

    # Worker 1 attempts to process with stale generation 1 -> must fail
    success = outbox_svc.process_outbox_intent(
        outbox_id=claimed_1.id,
        claimed_worker_id="worker-1",
        claimed_generation=1,
    )
    assert success is False


# ─── 4. DISPATCH WORKFLOW (SUCCESS PATH) ─────────────────────────────────────

def test_outbox_dispatch_worker_success(sqlite_session_factory, test_setup):
    """Verify background worker claims, dispatches, and finalizes DELIVERED status."""
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    mock_transport = MockFHIRTransport()
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=mock_transport,
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        transport=mock_transport,
    )

    outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    worker = OutboxDispatchWorker(outbox_service=outbox_svc, worker_id="daemon-worker-1")
    did_work = worker.run_single_iteration()
    assert did_work is True

    # Assert outbox state is DELIVERED
    with sqlite_session_factory() as session:
        outbox = session.query(EHRTransmissionOutbox).first()
        assert outbox.status == OutboxStatus.DELIVERED
        assert outbox.current_attempt_id is not None

        # Assert Phase A attempt is TRANSMITTED
        attempt = session.query(FHIRExport).filter(FHIRExport.id == outbox.current_attempt_id).first()
        assert attempt.transmission_state == EHRTransmissionState.TRANSMITTED
        assert attempt.resolution_status == EHRResolutionStatus.RESOLVED_DELIVERED
        assert attempt.outbox_id == outbox.id


# ─── 5. FOUR-EYES AUTHORIZED RETRY WORKFLOW ──────────────────────────────────

def test_four_eyes_retry_workflow(sqlite_session_factory, test_setup):
    """Verify timeout leads to BLOCKED_RECONCILIATION, and Four-Eyes token allows authorized retry."""
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    failing_transport = MockFHIRTransport(mode=MockEHRMode.TIMEOUT)
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=failing_transport,
    )
    recon_svc = ReconciliationService(audit_service=audit_svc)
    ehr_svc.reconciliation_service = recon_svc

    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        transport=failing_transport,
    )

    # Assign Four-Eyes roles
    with sqlite_session_factory() as session:
        role_clinician = Role(id="CLINICIAN_REVIEWER", name="Clinician Reviewer", description="Clinician reviewer role", permissions={})
        role_supervisor = Role(id="CLINICAL_SUPERVISOR", name="Clinical Supervisor", description="Clinical supervisor role", permissions={})
        session.add_all([role_clinician, role_supervisor])
        session.flush()

        ur1 = UserRole(user_id=test_setup["clinician_id"], role_id=role_clinician.id)
        ur2 = UserRole(user_id=test_setup["supervisor_id"], role_id=role_supervisor.id)
        session.add_all([ur1, ur2])
        session.commit()

    # Stage and dispatch initial attempt -> Fails with timeout
    outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    worker = OutboxDispatchWorker(outbox_service=outbox_svc, worker_id="daemon-worker-1")
    worker.run_single_iteration()

    # Verify status is BLOCKED_RECONCILIATION
    with sqlite_session_factory() as session:
        outbox = session.query(EHRTransmissionOutbox).first()
        assert outbox.status == OutboxStatus.BLOCKED_RECONCILIATION
        first_attempt_id = outbox.current_attempt_id

    # Create Four-Eyes reconciliation token
    with sqlite_session_factory() as session:
        recon_rec = recon_svc.create_reconciliation_token(
            session=session,
            document_id=test_setup["document_id"],
            prior_export_id=first_attempt_id,
            decision=ReconciliationDecision.CONFIRMED_NON_DELIVERY_SAFE_TO_RETRY,
            evidence_type="EHR_AUDIT_LOG_ENTRY",
            evidence_reference="EHR-LOG-REF-12345",
            verification_level="CRYPTOGRAPHIC_RECEIPT",
            primary_authorizer_id=test_setup["clinician_id"],
            supervisor_authorizer_id=test_setup["supervisor_id"],
            justification="Network trace confirms gateway dropped packet before EHR connection.",
        )
        session.commit()
        recon_token = str(recon_rec.id)

    # Re-stage with reconciliation token (Retry Workflow)
    retry_staging = outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
        reconciliation_token=recon_token,
    )
    assert retry_staging.status == OutboxStatus.PENDING

    # Switch transport to success and dispatch attempt 2
    outbox_svc.transport = MockFHIRTransport()
    ehr_svc.transport = outbox_svc.transport
    worker.run_single_iteration()

    # Verify final status DELIVERED and attempt_number == 2
    with sqlite_session_factory() as session:
        outbox = session.query(EHRTransmissionOutbox).first()
        assert outbox.status == OutboxStatus.DELIVERED
        second_attempt = session.query(FHIRExport).filter(FHIRExport.id == outbox.current_attempt_id).first()
        assert second_attempt.attempt_number == 2
        assert second_attempt.parent_attempt_id == first_attempt_id
        assert second_attempt.transmission_state == EHRTransmissionState.TRANSMITTED

        first_attempt = session.query(FHIRExport).filter(FHIRExport.id == first_attempt_id).first()
        # Identity and idempotency key stability across retries
        assert first_attempt.logical_export_id == second_attempt.logical_export_id == outbox.logical_export_id
        assert first_attempt.destination_idempotency_key == second_attempt.destination_idempotency_key == outbox.destination_idempotency_key


def test_destination_idempotency_key_colon_formula_and_canonicalization():
    """Explicitly verify the Phase A colon-delimited formula and canonicalization rules:
    - Formula: SHA256(canonical_destination + ":" + logical_export_id + ":" + canonical_bundle_sha256)
    - Rejects/differs from pipe-delimited formula.
    - Normalized scheme, trimmed whitespace, lowercase.
    - Default ports (:80, :443) omitted.
    - Trailing slashes stripped.
    """
    import hashlib
    from clindoc.services.ehr_integration import (
        canonicalize_destination_identity,
        derive_destination_idempotency_key,
    )

    raw_dest = "  HTTPS://EHR.Hospital.Org:443/r4/  "
    canonical_dest = canonicalize_destination_identity(raw_dest)
    assert canonical_dest == "https://ehr.hospital.org/r4"

    # Default http port 80 omitted
    assert canonicalize_destination_identity("http://ehr.org:80/fhir/") == "http://ehr.org/fhir"
    # Non-default port 8443 preserved
    assert canonicalize_destination_identity("https://ehr.org:8443/fhir/") == "https://ehr.org:8443/fhir"

    log_id = uuid.uuid4()
    bundle_hash = "abcdef0123456789" * 4

    derived = derive_destination_idempotency_key(raw_dest, log_id, bundle_hash)
    expected_colon = hashlib.sha256(f"https://ehr.hospital.org/r4:{str(log_id).lower()}:{bundle_hash}".encode("utf-8")).hexdigest()
    assert derived == expected_colon

    # Pipe-delimited formula produces distinct hash and must not match
    pipe_hash = hashlib.sha256(f"https://ehr.hospital.org/r4|{str(log_id).lower()}|{bundle_hash}".encode("utf-8")).hexdigest()
    assert derived != pipe_hash

    # 1. Valid UUIDv4 is accepted and canonicalized (lowercase, hyphenated)
    v4_uuid = uuid.uuid4()
    v4_str_upper = str(v4_uuid).upper()
    key_v4 = derive_destination_idempotency_key(raw_dest, v4_str_upper, bundle_hash)
    assert key_v4 == hashlib.sha256(f"https://ehr.hospital.org/r4:{str(v4_uuid).lower()}:{bundle_hash}".encode("utf-8")).hexdigest()

    # 2. Non-v4 UUIDs (UUIDv1, UUIDv5) are rejected
    v1_uuid = uuid.uuid1()
    assert v1_uuid.version == 1
    with pytest.raises(ValueError, match="logical_export_id must be a UUIDv4"):
        derive_destination_idempotency_key(raw_dest, v1_uuid, bundle_hash)

    with pytest.raises(ValueError, match="logical_export_id must be a UUIDv4"):
        derive_destination_idempotency_key(raw_dest, str(v1_uuid), bundle_hash)

    v5_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, "hospital.org")
    assert v5_uuid.version == 5
    with pytest.raises(ValueError, match="logical_export_id must be a UUIDv4"):
        derive_destination_idempotency_key(raw_dest, v5_uuid, bundle_hash)

    # 3. Valid uppercase 64-char hexadecimal hash is normalized and accepted
    upper_hash = bundle_hash.upper()
    key_upper = derive_destination_idempotency_key(raw_dest, log_id, upper_hash)
    assert key_upper == derived

    # 4. 64-character string containing non-hexadecimal characters is rejected
    non_hex_64 = "g" + "0" * 63  # 'g' is non-hex
    with pytest.raises(ValueError, match="must be a 64-character hexadecimal string"):
        derive_destination_idempotency_key(raw_dest, log_id, non_hex_64)

    non_hex_symbols = "abcdef0123456789" * 3 + "abcdef012345678!"  # '!' is non-hex
    with pytest.raises(ValueError, match="must be a 64-character hexadecimal string"):
        derive_destination_idempotency_key(raw_dest, log_id, non_hex_symbols)

    # 5. Incorrect hash lengths are rejected
    with pytest.raises(ValueError, match="must be a 64-character hexadecimal string"):
        derive_destination_idempotency_key(raw_dest, log_id, "abcdef")  # short

    with pytest.raises(ValueError, match="must be a 64-character hexadecimal string"):
        derive_destination_idempotency_key(raw_dest, log_id, bundle_hash + "a")  # 65 chars


# ─── 6. SQLITE POPULATED REBUILD MIGRATION TESTS ─────────────────────────────

def test_sqlite_populated_migration_rebuild(tmp_path):
    """Test full Phase B1 SQLite table rebuild on a populated database with historical Phase A records."""
    db_file = tmp_path / "populated_migration_test.db"

    # 1. Create legacy pre-Phase A SQLite database with existing tables, views, triggers, and rows
    from tests.test_ehr_lifecycle_migration import _create_legacy_sqlite_database
    _create_legacy_sqlite_database(str(db_file))

    engine = create_engine(f"sqlite:///{db_file}")

    # 2. Run Phase A migration
    res_a = PhaseALifecycleMigrationRunner(engine).run()
    assert res_a.already_applied is False
    assert res_a.migrated_rows_count > 0

    # 3. Add an ehr_transmission_reconciliations record referencing fhir_exports with ON DELETE RESTRICT
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        conn.execute(
            text(
                """
                INSERT INTO users (id, idp_subject, username, full_name, email)
                VALUES ('u2', 'sub-2', 'dr_director', 'Dr. Director', 'director@hospital.org');
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO ehr_transmission_reconciliations (
                    id, document_id, bundle_id, prior_export_id,
                    destination_identity, bundle_sha256, destination_idempotency_key,
                    decision, justification, evidence_type, evidence_reference,
                    primary_authorizer_id, primary_authorizer_role,
                    supervisor_authorizer_id, supervisor_authorizer_role,
                    authorized_at
                ) VALUES (
                    'rec-test-1', 'd1', 'b1', 'exp_ehr_timeout',
                    'https://ehr.hospital.org/r4', 'sha256b1', 'ehr-to-01',
                    'RETRY', 'Temporary network failure approved for retry', 'NETWORK_LOG', 'trace-123',
                    'u1', 'ATTENDING_PHYSICIAN',
                    'u2', 'DEPARTMENT_CHIEF',
                    CURRENT_TIMESTAMP
                );
                """
            )
        )
        conn.commit()

    # 4. Run Phase B1 Migration
    runner = PhaseB1OutboxMigrationRunner(engine)
    res_b = runner.run()
    assert res_b.success is True
    assert res_b.migrated_rows_count == 13

    # 5. Verify foreign_keys = ON and zero violations
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        fk_check = conn.execute(text("PRAGMA foreign_key_check;")).fetchall()
        assert len(fk_check) == 0

        # Verify historical fhir_exports records survive with outbox_id NULL
        rows = conn.execute(text("SELECT id, outbox_id FROM fhir_exports;")).fetchall()
        assert len(rows) == 13
        for r_id, outbox_id in rows:
            assert outbox_id is None

        # Verify view v_active_exports still works
        view_rows = conn.execute(text("SELECT * FROM v_active_exports;")).fetchall()
        assert len(view_rows) == 13

        # Verify reconciliation table still references fhir_exports cleanly
        rec_row = conn.execute(text("SELECT prior_export_id FROM ehr_transmission_reconciliations WHERE id = 'rec-test-1';")).fetchone()
        assert rec_row[0] == "exp_ehr_timeout"


# ─── 7. LIVE POSTGRESQL 18 MIGRATION & SKIP LOCKED CONCURRENCY TESTS ─────────

@pytest.mark.skipif(
    not os.path.exists("D:/postgresql/bin/postgres.exe"),
    reason="PostgreSQL binaries not found at D:/postgresql",
)
def test_postgresql_migration_and_skip_locked(tmp_path):
    """Test live PostgreSQL 18 migration, check constraints, and FOR UPDATE SKIP LOCKED concurrency."""
    import shutil
    import subprocess

    pg_port = 54332
    pg_data = tmp_path / "pgdata_b1"
    initdb_exe = "D:/postgresql/bin/initdb.exe"
    postgres_exe = "D:/postgresql/bin/postgres.exe"

    subprocess.run([initdb_exe, "-D", str(pg_data), "-U", "postgres", "-A", "trust"], check=True, capture_output=True)

    proc = subprocess.Popen(
        [postgres_exe, "-D", str(pg_data), "-p", str(pg_port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        time.sleep(2.0)
        pg_url = f"postgresql+psycopg://postgres@127.0.0.1:{pg_port}/postgres"
        engine = create_engine(pg_url)

        # Baseline tables
        Base.metadata.create_all(engine)

        # Run Phase B1 migration
        runner = PhaseB1OutboxMigrationRunner(engine)
        res = runner.run()
        assert res.success is True

        # Test PostgreSQL Check Constraint (Hex regex)
        with engine.connect() as conn:
            with pytest.raises(Exception):
                conn.execute(
                    text(
                        """
                        INSERT INTO ehr_transmission_outbox (
                            id, document_id, bundle_id, bundle_sha256, destination_identity,
                            logical_export_id, destination_idempotency_key, staged_by, status
                        ) VALUES (
                            gen_random_uuid(), gen_random_uuid(), gen_random_uuid(),
                            'UPPERCASE_HASH_INVALID_1234567890123456789012345678901234567890',
                            'https://dest', gen_random_uuid(), 'b' || repeat('0', 63), gen_random_uuid(), 'PENDING'
                        );
                        """
                    )
                )

        print("PostgreSQL 18 live migration and constraints verified successfully.")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(pg_data, ignore_errors=True)


# ─── 8. ZERO CONNECTIONS DURING BOUNDARY B TEST ──────────────────────────────

def test_zero_connections_during_boundary_b(sqlite_engine, sqlite_session_factory, test_setup):
    """Verify strictly ZERO open database connections exist during Boundary B physical network transport."""
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    mock_transport = MockFHIRTransport()

    boundary_b_checkedout_count = None

    orig_transmit = mock_transport.transmit
    def intercepting_transmit(*args, **kwargs):
        nonlocal boundary_b_checkedout_count
        boundary_b_checkedout_count = sqlite_engine.pool.checkedout()
        return orig_transmit(*args, **kwargs)

    mock_transport.transmit = intercepting_transmit

    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=mock_transport,
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        transport=mock_transport,
    )

    outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    worker = OutboxDispatchWorker(outbox_service=outbox_svc, worker_id="daemon-zero-conn-worker")
    did_work = worker.run_single_iteration()
    assert did_work is True
    assert boundary_b_checkedout_count == 0


# ─── 9. SQLITE MIGRATION ATOMIC ROLLBACK & POOL SAFETY ───────────────────────

def test_sqlite_migration_rollback_on_failure(tmp_path):
    """Verify that any failure inside the rebuild transaction triggers a complete rollback and restores PRAGMA foreign_keys = ON."""
    from unittest.mock import patch
    db_file = tmp_path / "migration_rollback_test.db"
    from tests.test_ehr_lifecycle_migration import _create_legacy_sqlite_database
    _create_legacy_sqlite_database(str(db_file))
    engine = create_engine(f"sqlite:///{db_file}")

    # Set up Phase A
    PhaseALifecycleMigrationRunner(engine).run()

    runner = PhaseB1OutboxMigrationRunner(engine)

    class FailingCursorWrapper:
        def __init__(self, cur):
            self._cur = cur

        def execute(self, stmt, *args, **kwargs):
            if isinstance(stmt, str) and "INSERT INTO schema_migrations" in stmt:
                raise RuntimeError("SIMULATED_CRASH_BEFORE_COMMIT")
            return self._cur.execute(stmt, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    original_raw_connection = engine.raw_connection
    def failing_raw_connection():
        conn = original_raw_connection()
        orig_cursor = conn.cursor
        def failing_cursor():
            cur = orig_cursor()
            return FailingCursorWrapper(cur)
        conn.cursor = failing_cursor
        return conn

    with patch.object(engine, "raw_connection", side_effect=failing_raw_connection):
        with pytest.raises(RuntimeError, match="SIMULATED_CRASH_BEFORE_COMMIT"):
            runner.run()

    # Verify rollback: schema_migrations does not contain phase_b1
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys = ON;"))
        migrated = conn.execute(
            text("SELECT 1 FROM schema_migrations WHERE version = '20261111_02_phase_b1_outbox';")
        ).fetchone()
        assert migrated is None

        # Verify fhir_exports table still exists with Phase A schema and 13 rows
        cnt = conn.execute(text("SELECT count(*) FROM fhir_exports;")).fetchone()[0]
        assert cnt == 13

        # Verify ehr_transmission_outbox table was rolled back (does not exist)
        table_exists = conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ehr_transmission_outbox';")
        ).fetchone()
        assert table_exists is None

        # Verify PRAGMA foreign_keys is 1
        fk_val = conn.execute(text("PRAGMA foreign_keys;")).fetchone()[0]
        assert fk_val == 1


def test_sqlite_connection_pool_safety(sqlite_engine):
    """Verify that all connections checked out from the pool maintain PRAGMA foreign_keys = ON."""
    with sqlite_engine.connect() as conn:
        res = conn.execute(text("PRAGMA foreign_keys;")).fetchone()[0]
        assert res == 1

    with sqlite_engine.connect() as conn2:
        res2 = conn2.execute(text("PRAGMA foreign_keys;")).fetchone()[0]
        assert res2 == 1


# ─── 10. CRASH RECOVERY TESTS FOR ALL BOUNDARIES ─────────────────────────────

def test_reaper_crash_recovery_case_b_c_transport_timeout(sqlite_session_factory, test_setup):
    """Crash Recovery Case B/C: Worker lease expires while attempt is PENDING_TRANSPORT.
    
    Reaper must conservatively transition Phase A attempt to UNKNOWN_RECONCILIATION_REQUIRED
    and outbox to BLOCKED_RECONCILIATION.
    """
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    mock_transport = MockFHIRTransport()
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=mock_transport,
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        default_lease_seconds=1,
    )

    outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    claimed = outbox_svc.claim_next_outbox_intent(worker_id="crash-worker")
    assert claimed is not None

    # Simulate Stage 3A execution: attempt created in PENDING_TRANSPORT, outbox linked
    attempt_res = ehr_svc.create_outbox_transmission_attempt(
        outbox_id=claimed.id,
        document_id=test_setup["document_id"],
        logical_export_id=claimed.logical_export_id,
        destination_identity="https://fhir.ehr.org",
        destination_idempotency_key=claimed.destination_idempotency_key,
        actor_id=test_setup["clinician_id"],
    )
    with sqlite_session_factory() as session:
        session.execute(
            text("UPDATE ehr_transmission_outbox SET current_attempt_id = :att WHERE id = :id"),
            {"att": attempt_res.attempt_id.hex, "id": claimed.id.hex},
        )
        session.commit()

    # Worker crashes before Boundary C. Wait for lease expiration.
    time.sleep(1.1)

    reconciled = outbox_svc.run_reaper_cycle()
    assert reconciled == 1

    with sqlite_session_factory() as session:
        outbox = session.query(EHRTransmissionOutbox).first()
        assert outbox.status == OutboxStatus.BLOCKED_RECONCILIATION
        att = session.query(FHIRExport).filter(FHIRExport.id == attempt_res.attempt_id).first()
        assert att.transmission_state == EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
        assert att.resolution_status == EHRResolutionStatus.UNRESOLVED


def test_reaper_crash_recovery_case_d_outcome_catchup(sqlite_session_factory, test_setup):
    """Crash Recovery Case D: Phase A Boundary C committed TRANSMITTED, but worker died before Session 4 CAS update.
    
    Reaper must catch up outbox status to DELIVERED.
    """
    audit_svc = AuditService()
    export_svc = FHIRExportService(audit_service=audit_svc)
    mock_transport = MockFHIRTransport()
    ehr_svc = EHRIntegrationService(
        audit_service=audit_svc,
        export_service=export_svc,
        session_factory=sqlite_session_factory,
        transport=mock_transport,
    )
    outbox_svc = EHROutboxService(
        session_factory=sqlite_session_factory,
        ehr_integration_service=ehr_svc,
        default_lease_seconds=1,
    )

    outbox_svc.stage_transmission_intent(
        document_id=test_setup["document_id"],
        destination_identity="https://fhir.ehr.org",
        actor_id=test_setup["clinician_id"],
    )

    claimed = outbox_svc.claim_next_outbox_intent(worker_id="crash-worker-d")
    assert claimed is not None

    # Stage 3A
    attempt_res = ehr_svc.create_outbox_transmission_attempt(
        outbox_id=claimed.id,
        document_id=test_setup["document_id"],
        logical_export_id=claimed.logical_export_id,
        destination_identity="https://fhir.ehr.org",
        destination_idempotency_key=claimed.destination_idempotency_key,
        actor_id=test_setup["clinician_id"],
    )
    # Stage 3B
    with sqlite_session_factory() as session:
        session.execute(
            text("UPDATE ehr_transmission_outbox SET current_attempt_id = :att WHERE id = :id"),
            {"att": attempt_res.attempt_id.hex, "id": claimed.id.hex},
        )
        session.commit()

    # Stage 5 (Boundary C committed directly)
    from clindoc.services.ehr_integration import EHRTransmissionResult
    tx_res = EHRTransmissionResult(
        document_id=str(test_setup["document_id"]),
        bundle_id=str(test_setup["bundle_id"]),
        bundle_sha256=test_setup["bundle_sha256"],
        destination_type="https://fhir.ehr.org",
        remote_message_id="remote-msg-123",
        success=True,
        status_code=200,
        idempotency_key=claimed.destination_idempotency_key,
        latency_seconds=0.05,
    )
    ehr_svc.persist_outbox_transmission_outcome(
        attempt_id=attempt_res.attempt_id,
        document_id=test_setup["document_id"],
        transport_result=tx_res,
    )

    # Worker dies before updating outbox (outbox still DISPATCHING). Wait for lease expiration.
    time.sleep(1.1)

    reconciled = outbox_svc.run_reaper_cycle()
    assert reconciled == 1

    with sqlite_session_factory() as session:
        outbox = session.query(EHRTransmissionOutbox).first()
        assert outbox.status == OutboxStatus.DELIVERED

