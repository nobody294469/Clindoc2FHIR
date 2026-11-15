"""Tests for Phase 3D.2.1: Audit Persistence, Review API Exposure & Normalizer Lifecycle Remediation.

Validates:
1. Candidate dictionaries and abstention reasons both reach the ORM payload in worker normalization.
2. Both survive ORM commit and subsequent database reload.
3. The Review Workbench claims endpoint (/api/v1/documents/{document_id}/claims) returns both fields.
4. Claims without candidates or abstention reasons return safe empty lists ([]).
5. Existing payload fields remain intact during payload update.
6. All three transient normalizer dictionaries (audit_candidates, audit_reasons, normalized_units)
   are cleared after successful normalization.
7. All three transient dictionaries are cleared after an exception in worker normalization.
8. Clearing transient normalizer state does not remove or mutate already-persisted audit data in Claim.payload.
9. Existing missing-service versus unavailable-service behavior remains unchanged.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from clindoc.api.app import create_app
from clindoc.api.auth import (
    CanonicalRole,
    set_global_session_factory,
    set_global_token_validator,
)
from clindoc.api.deps import set_global_triage_engine
from clindoc.api.routes.review import get_document_claims
from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    Evidence,
    JobStatus,
    ProcessingJob,
    Role,
    User,
    UserRole,
)
from clindoc.extract.contracts import Claim as ContractClaim
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.terminology.service import (
    ContextStatus,
    QueryContext,
    RetrievalMethod,
    RetrievalResponse,
    RetrievedCandidate,
    TerminologyRetrievalService,
)


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite session."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def sample_setup(db_session: Session):
    """Creates a sample user, document, and job."""
    reviewer_role = Role(
        id=CanonicalRole.CLINICAL_REVIEWER.value,
        name="Clinical Reviewer",
        description="Clinical review",
    )
    db_session.add(reviewer_role)
    db_session.flush()

    user = User(
        idp_subject="sub_test_user",
        username="test_reviewer",
        full_name="Test Reviewer",
        email="test_reviewer@clindoc.local",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    role = UserRole(user_id=user.id, role_id=CanonicalRole.CLINICAL_REVIEWER.value)
    db_session.add(role)

    doc = Document(
        uploaded_by=user.id,
        original_filename="test_doc.pdf",
        storage_ref="test_ref",
        mime_type="application/pdf",
        file_size_bytes=1024,
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.flush()

    job = ProcessingJob(
        document_id=doc.id,
        status=JobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()

    return {"user": user, "doc": doc, "job": job}


def _create_mock_service() -> TerminologyRetrievalService:
    """Creates a mock TerminologyRetrievalService with controlled responses."""
    svc = MagicMock(spec=TerminologyRetrievalService)

    def mock_retrieve(query: str, context: QueryContext | None = None, max_candidates: int = 10):
        # 1. Successful CDCI candidate
        if query == "Safedox":
            cand = RetrievedCandidate(
                concept_id=101,
                system="CDCI",
                code="2430421000189104",
                canonical_name="Safedox 200 mg film-coated oral tablet",
                tty="BRAND",
                attributes={},
                alias_id=1,
                matched_alias="safedox",
                alias_source="CDCI_BRAND_NAME",
                is_abbreviation=False,
                is_preferred=True,
                retrieval_method=RetrievalMethod.EXACT_DB,
                rank_score=1.0,
                context_status=ContextStatus.NO_RULES,
            )
            return RetrievalResponse(
                query=query,
                normalized_query="safedox",
                candidates=[cand],
                is_ambiguous=False,
                is_abstained=False,
                abstention_reasons=[],
                database_version="20261112",
                source_releases={"LOINC": "2.83", "RxNorm": "20261102"},
                query_duration_ms=1.5,
            )
        # 2. Abstained query (missing context)
        elif query == "%":
            cand = RetrievedCandidate(
                concept_id=202,
                system="LOINC",
                code="1234-5",
                canonical_name="Percentage Test",
                tty=None,
                attributes={},
                alias_id=2,
                matched_alias="%",
                alias_source="LOINC_SHORTNAME",
                is_abbreviation=True,
                is_preferred=False,
                retrieval_method=RetrievalMethod.EXACT_DB,
                rank_score=0.9,
                context_status=ContextStatus.ABSTAINED_MISSING_CONTEXT,
                context_reasons=["Missing required context for dimension 'SPECIMEN'"],
            )
            return RetrievalResponse(
                query=query,
                normalized_query="%",
                candidates=[cand],
                is_ambiguous=False,
                is_abstained=True,
                abstention_reasons=["Missing required context for dimension 'SPECIMEN'"],
                database_version="20261112",
                source_releases={"LOINC": "2.83", "RxNorm": "20261102"},
                query_duration_ms=1.2,
            )
        # 3. Empty / out-of-vocabulary query
        return RetrievalResponse(
            query=query,
            normalized_query=query.lower(),
            candidates=[],
            is_ambiguous=False,
            is_abstained=False,
            abstention_reasons=[],
            database_version="20261112",
            source_releases={"LOINC": "2.83", "RxNorm": "20261102"},
            query_duration_ms=0.8,
        )

    svc.retrieve.side_effect = mock_retrieve
    return svc


class TestPhase3D21PersistenceAndLifecycle:
    """Verifies audit persistence, Review API projection, and normalizer lifecycle cleanup."""

    def test_01_and_02_candidates_and_abstention_reasons_persisted_to_orm_and_survive_reload(
        self, db_session: Session, sample_setup
    ):
        """1 & 2: Candidates and abstention reasons reach ORM payload and survive DB commit/reload."""
        doc = sample_setup["doc"]
        job = sample_setup["job"]

        c1_id = str(uuid.uuid4())
        c2_id = str(uuid.uuid4())

        # Claim 1: Medication resolving to CDCI
        claim1 = Claim(
            claim_id=uuid.UUID(c1_id),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            payload={"pre_existing_key": "val_1", "extraction_confidence": 0.95},
        )
        # Claim 2: Lab mention that abstains
        claim2 = Claim(
            claim_id=uuid.UUID(c2_id),
            document_id=doc.id,
            group_id="grp-2",
            field_type="lab.test_name",
            value="%",
            origin=ClaimOrigin.PIPELINE,
            payload={"pre_existing_key": "val_2", "extraction_confidence": 0.90},
        )
        db_session.add_all([claim1, claim2])
        db_session.commit()

        # Contract claims corresponding to the pipeline claims
        cc1 = ContractClaim(
            claim_id=c1_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )
        cc2 = ContractClaim(
            claim_id=c2_id,
            document_id=str(doc.id),
            group_id="grp-2",
            field_type="lab.test_name",
            value="%",
            extraction_method="deterministic",
            extraction_confidence=0.90,
            evidence_id="ev-2",
            region_id="reg-2",
        )

        # Execute normalization stage
        mock_svc = _create_mock_service()
        normalizer = TerminologyNormalizer(retrieval_service=mock_svc)
        worker = DocumentProcessingWorker(normalizer=normalizer, router=MagicMock(), queue=MagicMock())

        worker._orchestrate_normalization_stage(
            session=db_session,
            job=job,
            document=doc,
            extraction_output=[cc1, cc2],
        )
        db_session.commit()

        # Expire session to force physical reload from database
        db_session.expire_all()

        reloaded_c1 = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c1_id)).one()
        assert reloaded_c1.normalized_code == "2430421000189104"
        assert reloaded_c1.code_system == "CDCI"
        assert "candidates" in reloaded_c1.payload
        assert len(reloaded_c1.payload["candidates"]) == 1
        assert reloaded_c1.payload["candidates"][0]["code"] == "2430421000189104"
        assert reloaded_c1.payload["candidates"][0]["system"] == "CDCI"
        # Claim 1 succeeded so no abstention reasons
        assert "abstention_reasons" not in reloaded_c1.payload

        reloaded_c2 = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c2_id)).one()
        assert reloaded_c2.normalized_code is None
        assert reloaded_c2.code_system is None
        assert "candidates" in reloaded_c2.payload
        assert "abstention_reasons" in reloaded_c2.payload
        assert len(reloaded_c2.payload["abstention_reasons"]) > 0
        assert "Missing required context for dimension 'SPECIMEN'" in reloaded_c2.payload["abstention_reasons"][0]

    def test_03_and_04_review_workbench_claims_endpoint_exposes_audit_fields(
        self, db_session: Session, sample_setup
    ):
        """3 & 4: Review API exposes candidates and abstention_reasons; returns empty lists when absent."""
        doc = sample_setup["doc"]
        user = sample_setup["user"]

        c_with_audit = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            normalized_code="2430421000189104",
            code_system="CDCI",
            payload={
                "candidates": [{"code": "2430421000189104", "display": "Safedox 200 mg", "system": "CDCI", "score": 1.0}],
                "abstention_reasons": ["Example reason"],
            },
        )
        c_without_audit = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-2",
            field_type="lab.value",
            value="10",
            origin=ClaimOrigin.PIPELINE,
            payload=None,  # Null payload
        )
        db_session.add_all([c_with_audit, c_without_audit])
        db_session.commit()

        # Test Review API claims endpoint
        app = create_app()
        set_global_session_factory(lambda: db_session)
        set_global_triage_engine(None)

        client = TestClient(app)

        # Mock auth by directly invoking endpoint handler or setting test user
        from clindoc.api.auth.models import AuthenticatedUser
        auth_user = AuthenticatedUser(
            user=user,
            subject=user.idp_subject,
            roles={CanonicalRole.CLINICAL_REVIEWER.value},
        )

        res = get_document_claims(
            document_id=doc.id,
            request=MagicMock(),
            include_superseded=False,
            active_only=False,
            current_user=auth_user,
            session=db_session,
            triage_engine=MagicMock(),
        )

        assert res["total"] == 2
        claim_map = {c["value"]: c for c in res["claims"]}

        # 3. Claim with audit data exposes the two fields
        c1 = claim_map["Safedox"]
        assert "candidates" in c1
        assert len(c1["candidates"]) == 1
        assert c1["candidates"][0]["code"] == "2430421000189104"
        assert "abstention_reasons" in c1
        assert c1["abstention_reasons"] == ["Example reason"]

        # 4. Claim without audit data returns safe empty lists
        c2 = claim_map["10"]
        assert "candidates" in c2
        assert c2["candidates"] == []
        assert "abstention_reasons" in c2
        assert c2["abstention_reasons"] == []

    def test_05_existing_payload_fields_remain_intact(
        self, db_session: Session, sample_setup
    ):
        """5: Existing payload fields are preserved and not replaced during normalization."""
        doc = sample_setup["doc"]
        job = sample_setup["job"]

        c_id = str(uuid.uuid4())
        claim = Claim(
            claim_id=uuid.UUID(c_id),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            payload={
                "custom_pipeline_meta": "preserve_this_value",
                "ocr_box": [10, 20, 30, 40],
                "char_offsets": {"start": 5, "end": 12},
            },
        )
        db_session.add(claim)
        db_session.commit()

        cc = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        mock_svc = _create_mock_service()
        normalizer = TerminologyNormalizer(retrieval_service=mock_svc)
        worker = DocumentProcessingWorker(normalizer=normalizer, router=MagicMock(), queue=MagicMock())

        worker._orchestrate_normalization_stage(
            session=db_session,
            job=job,
            document=doc,
            extraction_output=[cc],
        )
        db_session.commit()
        db_session.expire_all()

        reloaded = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
        assert reloaded.payload["custom_pipeline_meta"] == "preserve_this_value"
        assert reloaded.payload["ocr_box"] == [10, 20, 30, 40]
        assert reloaded.payload["char_offsets"] == {"start": 5, "end": 12}
        assert "candidates" in reloaded.payload

    def test_06_transient_dictionaries_cleared_after_successful_processing(
        self, db_session: Session, sample_setup
    ):
        """6: Normalizer per-claim transient state is cleanly emptied on successful stage completion."""
        doc = sample_setup["doc"]
        job = sample_setup["job"]

        claim = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            payload={},
        )
        db_session.add(claim)
        db_session.commit()

        c_id = str(claim.claim_id)
        cc = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        mock_svc = _create_mock_service()
        normalizer = TerminologyNormalizer(retrieval_service=mock_svc)
        worker = DocumentProcessingWorker(normalizer=normalizer, router=MagicMock(), queue=MagicMock())

        # Before run: dictionaries are empty
        assert len(normalizer.audit_candidates) == 0
        assert len(normalizer.audit_reasons) == 0
        assert len(normalizer.normalized_units) == 0

        worker._orchestrate_normalization_stage(
            session=db_session,
            job=job,
            document=doc,
            extraction_output=[cc],
        )

        # After stage completion: dictionaries are cleared in finally block
        assert len(normalizer.audit_candidates) == 0
        assert len(normalizer.audit_reasons) == 0
        assert len(normalizer.normalized_units) == 0

    def test_07_transient_dictionaries_cleared_after_exception(
        self, db_session: Session, sample_setup
    ):
        """7: Normalizer transient state is cleared even when normalization stage raises an exception."""
        doc = sample_setup["doc"]
        job = sample_setup["job"]

        claim = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            payload={},
        )
        db_session.add(claim)
        db_session.commit()

        c_id = str(claim.claim_id)
        cc = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        # Normalizer whose normalize_claims injects items then raises
        normalizer = TerminologyNormalizer()
        def faulty_normalize_claims(claims, contexts=None):
            normalizer.audit_candidates["dummy-id"] = [{"code": "TEST"}]
            normalizer.audit_reasons["dummy-id"] = ["TEST REASON"]
            normalizer.normalized_units["dummy-id"] = "mg"
            raise RuntimeError("Simulated unhandled stage error")

        normalizer.normalize_claims = faulty_normalize_claims

        worker = DocumentProcessingWorker(normalizer=normalizer, router=MagicMock(), queue=MagicMock())

        with pytest.raises(RuntimeError, match="Simulated unhandled stage error"):
            worker._orchestrate_normalization_stage(
                session=db_session,
                job=job,
                document=doc,
                extraction_output=[cc],
            )

        # Finally block must have executed despite exception
        assert len(normalizer.audit_candidates) == 0
        assert len(normalizer.audit_reasons) == 0
        assert len(normalizer.normalized_units) == 0

    def test_08_clearing_transient_state_does_not_remove_persisted_audit_data(
        self, db_session: Session, sample_setup
    ):
        """8: Clearing normalizer dictionaries does not affect already-persisted claim.payload."""
        doc = sample_setup["doc"]
        job = sample_setup["job"]

        c_id = str(uuid.uuid4())
        claim = Claim(
            claim_id=uuid.UUID(c_id),
            document_id=doc.id,
            group_id="grp-1",
            field_type="lab.test_name",
            value="%",
            origin=ClaimOrigin.PIPELINE,
            payload={},
        )
        db_session.add(claim)
        db_session.commit()

        cc = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="lab.test_name",
            value="%",
            extraction_method="deterministic",
            extraction_confidence=0.90,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        mock_svc = _create_mock_service()
        normalizer = TerminologyNormalizer(retrieval_service=mock_svc)
        worker = DocumentProcessingWorker(normalizer=normalizer, router=MagicMock(), queue=MagicMock())

        worker._orchestrate_normalization_stage(
            session=db_session,
            job=job,
            document=doc,
            extraction_output=[cc],
        )
        db_session.commit()

        # Explicitly call clear_audit_state again to ensure independence
        normalizer.clear_audit_state()
        assert len(normalizer.audit_candidates) == 0

        # Persisted database claim still has the complete payload
        db_session.expire_all()
        persisted = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
        assert len(persisted.payload["candidates"]) == 1
        assert len(persisted.payload["abstention_reasons"]) == 1

    def test_09_missing_service_versus_unavailable_service(self):
        """9: Legacy fallback on None vs fail-closed on service failure remains strictly decoupled."""
        # A. None service -> legacy fallback
        legacy_norm = TerminologyNormalizer(retrieval_service=None)
        c_legacy = ContractClaim(
            claim_id="test-1",
            document_id="doc-1",
            field_type="lab.test_name",
            value="Hemoglobin",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )
        legacy_norm.normalize_claim(c_legacy)
        assert c_legacy.normalized_code == "718-7"
        assert c_legacy.code_system == "LOINC"

        # B. Faulty service -> fails closed, zero legacy fallback
        faulty_svc = MagicMock(spec=TerminologyRetrievalService)
        faulty_svc.retrieve.side_effect = RuntimeError("Service crashed")
        faulty_norm = TerminologyNormalizer(retrieval_service=faulty_svc)
        c_faulty = ContractClaim(
            claim_id="test-2",
            document_id="doc-1",
            field_type="lab.test_name",
            value="Hemoglobin",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-2",
            region_id="reg-2",
        )
        faulty_norm.normalize_claim(c_faulty)
        assert c_faulty.normalized_code is None
        assert c_faulty.code_system is None
        assert c_faulty.code_confidence == 0.0
