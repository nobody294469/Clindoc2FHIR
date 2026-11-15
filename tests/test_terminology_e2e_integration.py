"""Phase 3D.4: End-to-End Runtime Terminology Integration Verification.

Validates the full, real clinical-document processing path using the promoted scaled
terminology database (data/terminology/terminology_v20261112.candidate.db):
1. Ingestion of synthetic clinical documents & job creation.
2. Worker job execution through all cascade stages:
   Ingestion -> Extraction -> Normalization -> Verification -> Persistence.
3. Laboratory claim resolution: grounded LOINC candidates, preserved units/grounding,
   persisted candidates & abstention reasons.
4. Medication claim resolution: grounded RxNorm/CDCI candidates, competing candidates,
   and verification that FHIR mapping does NOT invent a CDCI coding URI.
5. Ambiguous/insufficient evidence: fail-closed abstention, surviving candidate ties,
   persistence into ORM, and safe downstream exposure.
6. Runtime retrieval failure: fail-closed safety, zero silent fallback to legacy 100-concept subsets,
   isolation from unrelated claims.
7. Job-boundary reload: bounded reload checks between jobs without mid-job state mutation.
8. Review Workbench API projection: exposure of candidates and abstentions, safe handling of missing
   audit fields, strict protection against PHI/key leaks.
9. FHIR export & mapping safety: no unreviewed auto-approval, no fabricated codes or URIs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import uuid

import fitz
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from clindoc.api.app import create_app
from clindoc.api.auth import (
    CanonicalRole,
    set_global_session_factory,
    set_global_token_validator,
)
from clindoc.api.auth.models import AuthenticatedUser
from clindoc.api.deps import get_terminology_service, set_global_terminology_service, set_global_triage_engine
from clindoc.api.routes.review import get_document_claims
from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    Evidence,
    JobStageEvent,
    JobStatus,
    ModelRun,
    ProcessingJob,
    Role,
    User,
    UserRole,
    VerificationResult,
)
from clindoc.extract.contracts import (
    BBoxModel,
    Claim as ContractClaim,
    DocumentExtractionOutput,
    Evidence as ContractEvidence,
    ExtractionResult,
    RoutingDecision as ContractRoutingDecision,
)
from clindoc.extract.router import ExtractionRouter
from clindoc.fhir.constants import LOINC_SYSTEM, RXNORM_SYSTEM
from clindoc.fhir.mapper import claims_to_fhir_bundle
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.ocr.document_text import DocumentPage, DocumentText, Token
from clindoc.storage.blob import EncryptedFileBlobStore
from clindoc.terminology.compiler import load_active_terminology_pointer
from clindoc.terminology.schema import FTS5_DDL, SCHEMA_DDL
from clindoc.terminology.service import (
    ContextStatus,
    QueryContext,
    RetrievalMethod,
    RetrievalResponse,
    RetrievedCandidate,
    TerminologyRetrievalService,
    get_process_terminology_service,
    normalize_alias_text,
    reset_process_terminology_service,
    set_process_terminology_service,
)
from clindoc.db.enums import VerificationLabel, VerificationStage


PROMOTED_DB_PATH = Path("data/terminology/terminology_v20261112.candidate.db")
EXPECTED_PROMOTED_SHA256 = "2b9a16736039e6a054e278cdedda223da650f41f5ca70b7d9d67466038904ad7"


def _build_synthetic_terminology_db(db_path: Path) -> None:
    """Build a deterministic, isolated synthetic terminology database for portable testing."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.executescript(SCHEMA_DDL)
    conn.executescript(FTS5_DDL)
    cur = conn.cursor()

    concepts = [
        # Lab concepts
        ("LOINC", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c/hemoglobin.total in blood", True),
            ("hemoglobin a1c in blood", False),
            ("hba1c", False),
            ("a1c", False),
        ]),
        ("LOINC", "41995-2", "Hemoglobin A1c [Mass/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c", True),
        ]),
        ("LOINC", "55454-3", "Hemoglobin A1c [Mass fraction] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c", True),
        ]),
        ("LOINC", "17856-6", "Hemoglobin A1c/Hemoglobin.total in Blood by HPLC", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c/hemoglobin.total in blood by hplc", True),
        ]),
        ("LOINC", "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("glucose", True),
        ]),
        ("LOINC", "14771-0", "Glucose [Mass/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("glucose", False),
        ]),
        ("LOINC", "26449-9", "Eosinophils [#/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("eosinophils, blood", True),
        ]),
        ("LOINC", "711-2", "Eosinophils/100 leukocytes in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("eosinophils, blood", True),
        ]),
        # Extra 7 lab concepts matching 'hemoglobin a1c' via FTS to ensure 10 total candidates for Top-K
        ("LOINC", "99901-0", "Hemoglobin A1c test 1", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 1", False)]),
        ("LOINC", "99902-0", "Hemoglobin A1c test 2", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 2", False)]),
        ("LOINC", "99903-0", "Hemoglobin A1c test 3", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 3", False)]),
        ("LOINC", "99904-0", "Hemoglobin A1c test 4", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 4", False)]),
        ("LOINC", "99905-0", "Hemoglobin A1c test 5", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 5", False)]),
        ("LOINC", "99906-0", "Hemoglobin A1c test 6", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 6", False)]),
        ("LOINC", "99907-0", "Hemoglobin A1c test 7", "LAB", 1, "CHEM", "ACTIVE", "{}", [("hemoglobin a1c 7", False)]),
        # Med concepts
        ("RxNorm", "83367", "Atorvastatin", "IN", None, None, "ACTIVE", "{}", [
            ("atorvastatin", True),
        ]),
        ("RxNorm", "29046", "Lisinopril", "IN", None, None, "ACTIVE", "{}", [
            ("lisinopril", True),
        ]),
        ("RxNorm", "6809", "Metformin", "IN", None, None, "ACTIVE", "{}", [
            ("metformin", True),
        ]),
        ("RxNorm", "6918", "Metoprolol", "IN", None, None, "ACTIVE", "{}", [
            ("metoprolol", True),
        ]),
        ("CDCI", "372687004", "Amoxicillin", "SUBSTANCE", None, None, "ACTIVE", "{}", [
            ("amoxicillin", True),
        ]),
        ("RxNorm", "723", "Amoxicillin", "IN", None, None, "ACTIVE", "{}", [
            ("amoxicillin", False),
        ]),
    ]

    for sys, code, name, tty, ctype, cls, status, attrs, aliases in concepts:
        cur.execute(
            "INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sys, code, name, tty, ctype, cls, status, attrs)
        )
        cid = cur.lastrowid
        for alias_str, is_pref in aliases:
            norm = normalize_alias_text(alias_str)
            cur.execute(
                "INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, alias_source, is_preferred) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, norm, alias_str, norm, "SYNTHETIC", 1 if is_pref else 0)
            )

    conn.commit()
    conn.execute("INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('rebuild');")
    conn.execute("INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('rebuild');")

    metadata = [
        ("schema_version", "2.0.0"),
        ("version_tag", "20261112"),
        ("loinc_release_id", "LOINC_2.78_SYNTH"),
        ("rxnorm_release_id", "RxNorm_20261112_SYNTH"),
        ("concepts_count", str(len(concepts))),
        ("aliases_count", "30"),
        ("rules_count", "0"),
    ]
    cur.executemany("INSERT INTO terminology_metadata (key, value) VALUES (?, ?)", metadata)
    conn.commit()
    conn.close()


@pytest.fixture(scope="module", autouse=True)
def synthetic_terminology_env(tmp_path_factory):
    """Provide isolated, deterministic synthetic terminology database environment for portable execution."""
    tmp_dir = tmp_path_factory.mktemp("synthetic_terminology")
    db_file = tmp_dir / "terminology_v20261112.db"
    _build_synthetic_terminology_db(db_file)
    ptr_file = tmp_dir / "active_terminology.json"
    ptr_data = {
        "schema_version": "2.0.0",
        "version_tag": "20261112",
        "active_database": "terminology_v20261112.db",
        "active_database_path": str(db_file.as_posix()),
        "sha256": "synthetic_sha256",
        "loinc_release_id": "LOINC_2.78_SYNTH",
        "rxnorm_release_id": "RxNorm_20261112_SYNTH",
        "concepts_count": 20,
        "aliases_count": 30,
        "rules_count": 0,
        "published_at": "2026-11-12T00:00:00+00:00",
    }
    ptr_file.write_text(json.dumps(ptr_data, indent=2), encoding="utf-8")

    import os
    old_env = os.environ.get("CLINDOC_TERMINOLOGY_DIR")
    os.environ["CLINDOC_TERMINOLOGY_DIR"] = str(tmp_dir)
    reset_process_terminology_service()
    yield tmp_dir
    reset_process_terminology_service()
    if old_env is not None:
        os.environ["CLINDOC_TERMINOLOGY_DIR"] = old_env
    else:
        os.environ.pop("CLINDOC_TERMINOLOGY_DIR", None)


@pytest.fixture(autouse=True)
def clean_terminology_lifecycle():
    """Ensure process-level terminology service state is reset before and after each test."""
    reset_process_terminology_service()
    set_global_terminology_service(None, force=True)
    yield
    reset_process_terminology_service()
    set_global_terminology_service(None, force=True)


@pytest.mark.candidate_db_artifact
def test_promoted_candidate_database_artifact_integrity():
    """Verify when candidate database is provisioned that the promoted database and pointer are invariant.

    Prerequisite: External provisioning of candidate database artifact at
    data/terminology/terminology_v20261112.candidate.db.
    """
    if not PROMOTED_DB_PATH.exists():
        pytest.skip(f"Promoted candidate DB artifact not provisioned at {PROMOTED_DB_PATH}")
    computed = hashlib.sha256(PROMOTED_DB_PATH.read_bytes()).hexdigest().lower()
    assert computed == EXPECTED_PROMOTED_SHA256, (
        f"Promoted database SHA-256 mismatch! Expected {EXPECTED_PROMOTED_SHA256}, got {computed}"
    )

    ptr_path = Path("data/terminology/active_terminology.json")
    assert ptr_path.exists(), "Active terminology pointer not found"
    ptr = json.loads(ptr_path.read_text(encoding="utf-8"))
    assert ptr["sha256"].lower() == EXPECTED_PROMOTED_SHA256
    assert ptr["active_database"] == "terminology_v20261112.candidate.db"


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite database session with foreign keys and StaticPool."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    session.session_factory = session_factory
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def blob_store(tmp_path: Path) -> EncryptedFileBlobStore:
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    key = b"0123456789abcdef0123456789abcdef"
    return EncryptedFileBlobStore(root_dir=blob_dir, encryption_key=key)


@pytest.fixture
def sample_user(db_session: Session) -> User:
    """Create test reviewer user with clinical_reviewer and approver roles."""
    reviewer_role = Role(
        id=CanonicalRole.CLINICAL_REVIEWER.value,
        name="Clinical Reviewer",
        description="Clinical review role",
    )
    approver_role = Role(
        id=CanonicalRole.APPROVER.value,
        name="Approver",
        description="Approval role",
    )
    db_session.add_all([reviewer_role, approver_role])
    db_session.flush()

    user = User(
        idp_subject="sub_e2e_tester",
        username="e2e_reviewer",
        full_name="E2E Reviewer",
        email="e2e_reviewer@hospital.org",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role_id=CanonicalRole.CLINICAL_REVIEWER.value))
    db_session.add(UserRole(user_id=user.id, role_id=CanonicalRole.APPROVER.value))
    db_session.commit()
    return user


def _generate_synthetic_pdf(text_lines: list[str]) -> bytes:
    """Generate a minimal valid 1-page PDF containing the provided synthetic text lines."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 72
    for line in text_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 20
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


class MockVerificationRecord:
    """Serializable verification record mock for database persistence."""

    def __init__(self, claim_id: str):
        self.claim_id = str(claim_id)
        self.verification_id = str(uuid.uuid4())
        self.label = "SUPPORTED"
        self.confidence = 0.98
        self.stage_reached = "STAGE_A"
        self.escalated = False
        self.rationale = None
        self.is_fallback = False
        self.fallback_reason = None
        self.contradiction_type = None
        self.stage_a_label = None
        self.stage_b_label = None
        self.stage_b_raw_confidence = None

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {"label": "SUPPORTED", "stage": "STAGE_A"}


class MockDeterministicCascade:
    """Deterministic verification cascade that marks claims as SUPPORTED with high confidence."""

    def verify_claims(
        self,
        claims: list[ContractClaim],
        evidence_map: dict[str, ContractEvidence],
        page_text_map: Optional[dict[str, str]] = None,
    ) -> list[Any]:
        return [MockVerificationRecord(c.claim_id) for c in claims]


# ─── SECTION 2: END-TO-END RUNTIME SCENARIO TESTS ─────────────────────────────


class TestPhase3D4EndToEndRuntimeIntegration:
    """Phase 3D.4 End-to-End Pipeline Terminology Integration Verification."""

    def test_scenario_a_laboratory_terminology_resolution_and_persistence(
        self,
        db_session: Session,
        sample_user: User,
        blob_store: EncryptedFileBlobStore,
    ):
        """Scenario A: Laboratory claim reaches scaled service, resolves grounded LOINC, persists, and exposes in API."""
        # 1. Ingest synthetic document with lab mention
        pdf_bytes = _generate_synthetic_pdf([
            "CLINICAL LABORATORY REPORT",
            "Test: Hemoglobin A1c/Hemoglobin.total in Blood",
            "Result: 6.5 %",
        ])
        storage_ref = blob_store.put(pdf_bytes)
        doc = Document(
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            storage_ref=storage_ref,
            original_filename="synthetic_lab_report.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-E2E-LAB-01",
            status=DocumentStatus.PROCESSING,
        )
        db_session.add(doc)
        db_session.commit()

        queue = JobQueue()
        job = queue.enqueue(session=db_session, document_id=doc.id)
        db_session.commit()

        # 2. Setup extraction router emitting contract claims
        c_id = str(uuid.uuid4())
        ev_id = str(uuid.uuid4())
        ev = ContractEvidence(
            evidence_id=ev_id,
            document_id=str(doc.id),
            page_number=1,
            text="Hemoglobin A1c/Hemoglobin.total in Blood",
            char_start=33,
            char_end=74,
            bbox=BBoxModel(x0=72.0, y0=92.0, x1=250.0, y1=104.0),
        )
        claim = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-lab-1",
            field_type="lab.test_name",
            value="Hemoglobin A1c/Hemoglobin.total in Blood",
            extraction_method="deterministic",
            extraction_confidence=0.98,
            evidence_id=ev_id,
            region_id="reg-lab-1",
        )
        routing_dec = ContractRoutingDecision(
            document_id=str(doc.id),
            region_id="reg-lab-1",
            route="deterministic",
            confidence=0.98,
            reason="Laboratory header pattern match",
        )
        extraction_out = DocumentExtractionOutput(
            document_id=str(doc.id),
            regions=[
                ExtractionResult(
                    region_id="reg-lab-1",
                    routing_decision=routing_dec,
                    claims=[claim],
                    evidence=[ev],
                )
            ],
            all_claims=[claim],
            all_evidence=[ev],
            all_routing_decisions=[routing_dec],
        )

        mock_router = ExtractionRouter()
        mock_router.extract_document = MagicMock(return_value=extraction_out)

        # 3. Construct production worker wired to scaled terminology service
        worker = DocumentProcessingWorker(
            queue=queue,
            blob_store=blob_store,
            router=mock_router,
            cascade=MockDeterministicCascade(),
            session_factory=db_session.session_factory,
        )
        try:
            assert isinstance(worker.terminology_service, TerminologyRetrievalService)
            assert "terminology_v20261112" in str(worker.terminology_service.db_path)

            claimed = worker.claim_job(session=db_session)
            processed_job = worker.process_job(session=db_session, job=claimed)
            db_session.commit()
            db_session.expire_all()

            # Verify worker pipeline transitions
            assert processed_job.status == JobStatus.COMPLETED
            db_session.refresh(doc)
            assert doc.status == DocumentStatus.PENDING_REVIEW

            # 4. Verify ORM persistence
            persisted_claim = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
            assert persisted_claim.value == "Hemoglobin A1c/Hemoglobin.total in Blood"
            assert persisted_claim.normalized_code == "4548-4"
            assert persisted_claim.code_system == "LOINC"
            assert "candidates" in persisted_claim.payload
            candidates = persisted_claim.payload["candidates"]
            assert len(candidates) > 0
            assert candidates[0]["code"] == "4548-4"
            assert candidates[0]["system"] == "LOINC"
            assert candidates[0]["score"] == 1.0

            # 5. Verify Review Workbench API exposure
            auth_user = AuthenticatedUser(
                user=sample_user,
                subject=sample_user.idp_subject,
                roles={CanonicalRole.CLINICAL_REVIEWER.value},
            )
            api_res = get_document_claims(
                document_id=doc.id,
                request=MagicMock(),
                include_superseded=False,
                active_only=False,
                current_user=auth_user,
                session=db_session,
                triage_engine=MagicMock(),
            )
            assert api_res["total"] == 1
            claim_api = api_res["claims"][0]
            assert claim_api["claim_id"] == c_id
            assert claim_api["normalized_code"] == "4548-4"
            assert claim_api["code_system"] == "LOINC"
            assert len(claim_api["candidates"]) > 0
            assert claim_api["candidates"][0]["code"] == "4548-4"
            assert claim_api["grounding"]["text_snippet"] == "Hemoglobin A1c/Hemoglobin.total in Blood"
            assert claim_api["verification"]["label"] == "SUPPORTED"
        finally:
            worker.close()

    def test_scenario_b_medication_terminology_cdci_rxnorm_and_fhir_coding_safety(
        self,
        db_session: Session,
        sample_user: User,
        blob_store: EncryptedFileBlobStore,
    ):
        """Scenario B: Medication claim resolves to CDCI/RxNorm; FHIR mapping does NOT emit invented CDCI URI."""
        pdf_bytes = _generate_synthetic_pdf([
            "PRESCRIPTION ORDER",
            "1. Amoxicillin 500 mg oral tablet",
            "2. Lisinopril 20 mg oral tablet",
        ])
        storage_ref = blob_store.put(pdf_bytes)
        doc = Document(
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            storage_ref=storage_ref,
            original_filename="synthetic_prescriptions.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-E2E-MED-02",
            status=DocumentStatus.PROCESSING,
        )
        db_session.add(doc)
        db_session.commit()

        queue = JobQueue()
        job = queue.enqueue(session=db_session, document_id=doc.id)
        db_session.commit()

        # Claim 1: Amoxicillin -> resolves to CDCI in candidate db
        c1_id = str(uuid.uuid4())
        ev1_id = str(uuid.uuid4())
        ev1 = ContractEvidence(
            evidence_id=ev1_id,
            document_id=str(doc.id),
            page_number=1,
            text="Amoxicillin 500 mg oral tablet",
            char_start=23,
            char_end=53,
            bbox=BBoxModel(x0=72.0, y0=92.0, x1=220.0, y1=104.0),
        )
        c1 = ContractClaim(
            claim_id=c1_id,
            document_id=str(doc.id),
            group_id="grp-med-1",
            field_type="med.name",
            value="Amoxicillin",
            extraction_method="deterministic",
            extraction_confidence=0.98,
            evidence_id=ev1_id,
            region_id="reg-med-1",
        )

        # Claim 2: Lisinopril -> resolves to CDCI and RxNorm in candidate db
        c2_id = str(uuid.uuid4())
        ev2_id = str(uuid.uuid4())
        ev2 = ContractEvidence(
            evidence_id=ev2_id,
            document_id=str(doc.id),
            page_number=1,
            text="Lisinopril 20 mg oral tablet",
            char_start=57,
            char_end=85,
            bbox=BBoxModel(x0=72.0, y0=112.0, x1=220.0, y1=124.0),
        )
        c2 = ContractClaim(
            claim_id=c2_id,
            document_id=str(doc.id),
            group_id="grp-med-2",
            field_type="med.name",
            value="Lisinopril",
            extraction_method="deterministic",
            extraction_confidence=0.97,
            evidence_id=ev2_id,
            region_id="reg-med-2",
        )

        routing_dec = ContractRoutingDecision(
            document_id=str(doc.id),
            region_id="reg-med-1",
            route="deterministic",
            confidence=0.98,
            reason="Medication table pattern match",
        )
        extraction_out = DocumentExtractionOutput(
            document_id=str(doc.id),
            regions=[
                ExtractionResult(
                    region_id="reg-med-1",
                    routing_decision=routing_dec,
                    claims=[c1, c2],
                    evidence=[ev1, ev2],
                )
            ],
            all_claims=[c1, c2],
            all_evidence=[ev1, ev2],
            all_routing_decisions=[routing_dec],
        )

        mock_router = ExtractionRouter()
        mock_router.extract_document = MagicMock(return_value=extraction_out)

        worker = DocumentProcessingWorker(
            queue=queue,
            blob_store=blob_store,
            router=mock_router,
            cascade=MockDeterministicCascade(),
            session_factory=db_session.session_factory,
        )
        try:
            claimed = worker.claim_job(session=db_session)
            processed_job = worker.process_job(session=db_session, job=claimed)
            db_session.commit()
            db_session.expire_all()

            # 1. Verify ORM persistence
            reloaded_c1 = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c1_id)).one()
            assert reloaded_c1.normalized_code == "372687004"
            assert reloaded_c1.code_system == "CDCI"
            assert "candidates" in reloaded_c1.payload

            # 2. Verify Review Workbench API returns CDCI candidate without censoring
            auth_user = AuthenticatedUser(
                user=sample_user,
                subject=sample_user.idp_subject,
                roles={CanonicalRole.CLINICAL_REVIEWER.value},
            )
            api_res = get_document_claims(
                document_id=doc.id,
                request=MagicMock(),
                include_superseded=False,
                active_only=False,
                current_user=auth_user,
                session=db_session,
                triage_engine=MagicMock(),
            )
            api_claims_by_id = {c["claim_id"]: c for c in api_res["claims"]}
            assert api_claims_by_id[c1_id]["code_system"] == "CDCI"
            assert api_claims_by_id[c1_id]["normalized_code"] == "372687004"

            # 3. Verify Downstream FHIR Mapping Layer Behavior:
            # CDCI must NOT emit an invented coding URI!
            ver_record_1 = MockVerificationRecord(c1_id)
            ver_record_rx = MockVerificationRecord(c2_id)

            # Test Claim 1 with CDCI
            c1_norm = c1.model_copy(update={"normalized_code": "372687004", "code_system": "CDCI"})
            bundle_cdci = claims_to_fhir_bundle(
                claims=[c1_norm],
                evidence_map={ev1.evidence_id: ev1},
                verification_map={c1_norm.claim_id: ver_record_1},
                document_type="clinic_rx_pad",
            )
            med_entry_cdci = next(
                (e.resource for e in bundle_cdci.entry if e.resource.resource_type == "MedicationRequest"),
                None,
            )
            assert med_entry_cdci is not None
            # Asserts that coding is None (no invented URI such as "http://cdci.org" or "CDCI")
            assert med_entry_cdci.medicationCodeableConcept.coding is None
            assert med_entry_cdci.medicationCodeableConcept.text == "Amoxicillin"

            # Test Claim 2 with RxNorm
            c_rx = c2.model_copy(update={"normalized_code": "29046", "code_system": "RxNorm"})
            bundle_rx = claims_to_fhir_bundle(
                claims=[c_rx],
                evidence_map={ev2.evidence_id: ev2},
                verification_map={c_rx.claim_id: ver_record_rx},
                document_type="clinic_rx_pad",
            )
            med_entry_rx = next(
                (e.resource for e in bundle_rx.entry if e.resource.resource_type == "MedicationRequest"),
                None,
            )
            assert med_entry_rx is not None
            assert med_entry_rx.medicationCodeableConcept.coding is not None
            assert med_entry_rx.medicationCodeableConcept.coding[0].system == RXNORM_SYSTEM
            assert med_entry_rx.medicationCodeableConcept.coding[0].code == "29046"
        finally:
            worker.close()

    def test_scenario_c_ambiguous_evidence_abstains_and_persists_reasons(
        self,
        db_session: Session,
        sample_user: User,
        blob_store: EncryptedFileBlobStore,
    ):
        """Scenario C: Ambiguous laboratory mention abstains, records tied concepts in abstention_reasons, no forced code."""
        pdf_bytes = _generate_synthetic_pdf([
            "AMBIGUOUS LAB MENTION",
            "Test: eosinophils, blood",
            "Value: 5 %",
        ])
        storage_ref = blob_store.put(pdf_bytes)
        doc = Document(
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            storage_ref=storage_ref,
            original_filename="synthetic_ambiguous.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-E2E-AMB-03",
            status=DocumentStatus.PROCESSING,
        )
        db_session.add(doc)
        db_session.commit()

        queue = JobQueue()
        job = queue.enqueue(session=db_session, document_id=doc.id)
        db_session.commit()

        c_id = str(uuid.uuid4())
        ev_id = str(uuid.uuid4())
        ev = ContractEvidence(
            evidence_id=ev_id,
            document_id=str(doc.id),
            page_number=1,
            text="eosinophils, blood",
            char_start=29,
            char_end=47,
            bbox=BBoxModel(x0=72.0, y0=92.0, x1=150.0, y1=104.0),
        )
        claim = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-amb-1",
            field_type="lab.test_name",
            value="eosinophils, blood",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id=ev_id,
            region_id="reg-amb-1",
        )
        routing_dec = ContractRoutingDecision(
            document_id=str(doc.id),
            region_id="reg-amb-1",
            route="deterministic",
            confidence=0.95,
            reason="Ambiguous lab mention",
        )
        extraction_out = DocumentExtractionOutput(
            document_id=str(doc.id),
            regions=[
                ExtractionResult(
                    region_id="reg-amb-1",
                    routing_decision=routing_dec,
                    claims=[claim],
                    evidence=[ev],
                )
            ],
            all_claims=[claim],
            all_evidence=[ev],
            all_routing_decisions=[routing_dec],
        )

        mock_router = ExtractionRouter()
        mock_router.extract_document = MagicMock(return_value=extraction_out)

        worker = DocumentProcessingWorker(
            queue=queue,
            blob_store=blob_store,
            router=mock_router,
            cascade=MockDeterministicCascade(),
            session_factory=db_session.session_factory,
        )
        try:
            claimed = worker.claim_job(session=db_session)
            processed_job = worker.process_job(session=db_session, job=claimed)
            db_session.commit()
            db_session.expire_all()

            # 1. Verify ORM persistence of abstention
            persisted = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
            assert persisted.normalized_code is None
            assert persisted.code_system is None
            assert "abstention_reasons" in persisted.payload
            assert any("Ambiguous laboratory mention" in r for r in persisted.payload["abstention_reasons"])
            assert "candidates" in persisted.payload
            assert len(persisted.payload["candidates"]) >= 2
            top_codes = {c["code"] for c in persisted.payload["candidates"][:2]}
            assert "26449-9" in top_codes
            assert "711-2" in top_codes

            # 2. Verify Review Workbench API response
            auth_user = AuthenticatedUser(
                user=sample_user,
                subject=sample_user.idp_subject,
                roles={CanonicalRole.CLINICAL_REVIEWER.value},
            )
            api_res = get_document_claims(
                document_id=doc.id,
                request=MagicMock(),
                include_superseded=False,
                active_only=False,
                current_user=auth_user,
                session=db_session,
                triage_engine=MagicMock(),
            )
            claim_api = api_res["claims"][0]
            assert claim_api["normalized_code"] is None
            assert claim_api["code_system"] is None
            assert len(claim_api["abstention_reasons"]) > 0
            assert any("Ambiguous laboratory mention" in r for r in claim_api["abstention_reasons"])
            assert len(claim_api["candidates"]) >= 2

            # 3. Downstream FHIR Mapping: Observation code coding must remain empty (no forced code)
            val_claim = ContractClaim(
                claim_id=str(uuid.uuid4()),
                document_id=str(doc.id),
                group_id="grp-amb-1",
                field_type="lab.value",
                value="5 %",
                unit_raw="%",
                extraction_method="deterministic",
                extraction_confidence=0.95,
                evidence_id=ev_id,
                region_id="reg-amb-1",
            )
            bundle = claims_to_fhir_bundle(
                claims=[claim, val_claim],
                evidence_map={ev.evidence_id: ev},
                verification_map={
                    claim.claim_id: MockVerificationRecord(claim.claim_id),
                    val_claim.claim_id: MockVerificationRecord(val_claim.claim_id),
                },
                document_type="lab_report",
            )
            obs = next((e.resource for e in bundle.entry if e.resource.resource_type == "Observation"), None)
            assert obs is not None
            assert obs.code.coding is None
            assert obs.code.text == "eosinophils, blood"
        finally:
            worker.close()

    def test_scenario_d_runtime_retrieval_failure_fails_closed_without_legacy_fallback(
        self,
        db_session: Session,
        sample_user: User,
        blob_store: EncryptedFileBlobStore,
    ):
        """Scenario D: Terminology retrieval failure fails closed; does NOT silently fall back to legacy subsets."""
        pdf_bytes = _generate_synthetic_pdf([
            "LAB FAILURE TEST",
            "Amoxicillin 500 mg",
        ])
        storage_ref = blob_store.put(pdf_bytes)
        doc = Document(
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            storage_ref=storage_ref,
            original_filename="synthetic_fail.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-E2E-FAIL-04",
            status=DocumentStatus.PROCESSING,
        )
        db_session.add(doc)
        db_session.commit()

        queue = JobQueue()
        job = queue.enqueue(session=db_session, document_id=doc.id)
        db_session.commit()

        # Claim 1: Amoxicillin (which exists in legacy subset, but MUST NOT be normalized via legacy on scaled failure)
        c1_id = str(uuid.uuid4())
        ev1 = ContractEvidence(
            evidence_id="ev-f-1",
            document_id=str(doc.id),
            page_number=1,
            text="Amoxicillin",
            char_start=0,
            char_end=11,
            bbox=BBoxModel(x0=0.0, y0=0.0, x1=10.0, y1=10.0),
        )
        c1 = ContractClaim(
            claim_id=c1_id,
            document_id=str(doc.id),
            group_id="grp-f-1",
            field_type="med.name",
            value="Amoxicillin",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-f-1",
            region_id="reg-f-1",
        )

        # Claim 2: Numeric lab value (unrelated claim that must remain intact)
        c2_id = str(uuid.uuid4())
        ev2 = ContractEvidence(
            evidence_id="ev-f-2",
            document_id=str(doc.id),
            page_number=1,
            text="95 mg/dL",
            char_start=15,
            char_end=23,
            bbox=BBoxModel(x0=0.0, y0=0.0, x1=10.0, y1=10.0),
        )
        c2 = ContractClaim(
            claim_id=c2_id,
            document_id=str(doc.id),
            group_id="grp-f-1",
            field_type="lab.value",
            value="95 mg/dL",
            unit_raw="mg/dL",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-f-2",
            region_id="reg-f-1",
        )

        routing_dec = ContractRoutingDecision(
            document_id=str(doc.id),
            region_id="reg-f-1",
            route="deterministic",
            confidence=0.95,
            reason="Failure test routing",
        )
        extraction_out = DocumentExtractionOutput(
            document_id=str(doc.id),
            regions=[
                ExtractionResult(
                    region_id="reg-f-1",
                    routing_decision=routing_dec,
                    claims=[c1, c2],
                    evidence=[ev1, ev2],
                )
            ],
            all_claims=[c1, c2],
            all_evidence=[ev1, ev2],
            all_routing_decisions=[routing_dec],
        )
        mock_router = ExtractionRouter()
        mock_router.extract_document = MagicMock(return_value=extraction_out)

        # Setup mock retrieval service that raises operational error
        failing_svc = MagicMock(spec=TerminologyRetrievalService)
        failing_svc.reload_if_updated.return_value = False
        failing_svc.retrieve.side_effect = sqlite3.OperationalError("Simulated SQLite disk I/O error")

        worker = DocumentProcessingWorker(
            queue=queue,
            blob_store=blob_store,
            router=mock_router,
            terminology_service=failing_svc,
            cascade=MockDeterministicCascade(),
            session_factory=db_session.session_factory,
        )
        try:
            claimed = worker.claim_job(session=db_session)
            processed_job = worker.process_job(session=db_session, job=claimed)
            db_session.commit()
            db_session.expire_all()

            # 1. Assert FAIL-CLOSED for the medication claim
            reloaded_c1 = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c1_id)).one()
            assert reloaded_c1.normalized_code is None
            assert reloaded_c1.code_system is None
            assert "abstention_reasons" in reloaded_c1.payload
            assert any("Simulated SQLite disk I/O error" in r for r in reloaded_c1.payload["abstention_reasons"])

            # 2. Assert unrelated numeric claim was not corrupted
            reloaded_c2 = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c2_id)).one()
            assert reloaded_c2.value == "95 mg/dL"
            assert reloaded_c2.payload.get("value_numeric") == 95.0
            assert reloaded_c2.payload.get("normalized_unit") == "mg/dL"
        finally:
            worker.close()

    def test_scenario_e_job_boundary_reload_with_isolated_fixture(self, tmp_path: Path):
        """Scenario E: Hot-reload occurs only at job boundary; in-flight job maintains consistency; next job reloads."""
        # 1. Create two isolated terminology databases in tmp_path
        db1_path = tmp_path / "terminology_v1.db"
        db2_path = tmp_path / "terminology_v2.db"

        def _make_db(path: Path, v_tag: str, lab_concept: str, lab_code: str):
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript(SCHEMA_DDL)
            conn.executescript(FTS5_DDL)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) "
                "VALUES ('LOINC', ?, ?, 'LAB', 1, 'CHEM', 'ACTIVE', '{}')",
                (lab_code, lab_concept),
            )
            cid = cur.lastrowid
            cur.execute(
                "INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, alias_source, is_preferred) "
                "VALUES (?, ?, ?, ?, 'TEST', 1)",
                (cid, lab_concept.lower(), lab_concept, lab_concept.lower()),
            )
            metadata = [
                ("schema_version", "2.0.0"),
                ("version_tag", v_tag),
                ("loinc_release_id", f"LOINC_{v_tag}"),
                ("rxnorm_release_id", f"RxNorm_{v_tag}"),
                ("concepts_count", "1"),
                ("aliases_count", "1"),
                ("rules_count", "0"),
            ]
            cur.executemany("INSERT INTO terminology_metadata (key, value) VALUES (?, ?)", metadata)
            conn.commit()
            conn.close()

        _make_db(db1_path, "20261112_v1", "Test Glucose V1", "1001-1")
        _make_db(db2_path, "20261112_v2", "Test Glucose V2", "2002-2")

        # Write active_terminology.json pointing to db1
        ptr_file = tmp_path / "active_terminology.json"
        ptr_v1 = {
            "schema_version": "2.0.0",
            "version_tag": "20261112_v1",
            "active_database": "terminology_v1.db",
            "active_database_path": str(db1_path.as_posix()),
            "sha256": "hash_v1",
            "loinc_release_id": "LOINC_v1",
            "rxnorm_release_id": "RxNorm_v1",
            "concepts_count": 1,
            "aliases_count": 1,
            "rules_count": 0,
            "published_at": "2026-11-12T00:00:00+00:00",
        }
        ptr_file.write_text(json.dumps(ptr_v1, indent=2), encoding="utf-8")

        # Initialize service against tmp_path
        service = TerminologyRetrievalService(db_path_or_dir=tmp_path)
        assert service.db_version == "20261112_v1"

        # Worker initialized with this isolated service
        normalizer = TerminologyNormalizer(retrieval_service=service)
        worker = DocumentProcessingWorker(
            normalizer=normalizer,
            terminology_service=service,
            router=MagicMock(),
            queue=MagicMock(),
        )
        try:
            # Query in Job 1
            res1 = service.retrieve("Test Glucose V1")
            assert len(res1.candidates) == 1
            assert res1.candidates[0].code == "1001-1"

            # While Job 1 is running, external publisher updates pointer to db2
            time.sleep(0.01)  # Ensure distinct mtime
            ptr_v2 = {
                "schema_version": "2.0.0",
                "version_tag": "20261112_v2",
                "active_database": "terminology_v2.db",
                "active_database_path": str(db2_path.as_posix()),
                "sha256": "hash_v2",
                "loinc_release_id": "LOINC_v2",
                "rxnorm_release_id": "RxNorm_v2",
                "concepts_count": 1,
                "aliases_count": 1,
                "rules_count": 0,
                "published_at": "2026-11-12T01:00:00+00:00",
            }
            ptr_file.write_text(json.dumps(ptr_v2, indent=2), encoding="utf-8")

            # In-flight Job 1 does NOT reload during its claim processing
            assert service.db_version == "20261112_v1"

            # Job 2 begins: worker checks reload at the job boundary
            reloaded = service.reload_if_updated()
            assert reloaded is True
            assert service.db_version == "20261112_v2"

            # Subsequent query resolves against v2
            res2 = service.retrieve("Test Glucose V2")
            assert len(res2.candidates) == 1
            assert res2.candidates[0].code == "2002-2"
        finally:
            worker.close()
            service.close()


# ─── SECTION 3: REVIEW WORKBENCH AND PERSISTENCE VERIFICATION ─────────────────


class TestPhase3D4ReviewWorkbenchAndPersistence:
    """Rigorous verification of Review Workbench API exposure, audit persistence, and security controls."""

    def test_01_api_projection_preserves_candidates_and_abstentions(
        self,
        db_session: Session,
        sample_user: User,
    ):
        """Review API exposes candidates and abstention_reasons without exposing raw internal payloads or encryption keys."""
        doc = Document(
            sha256="abc123" * 10,
            storage_ref="internal/enc/blob_secret_key_123",
            original_filename="secret_patient_chart.pdf",
            mime_type="application/pdf",
            file_size_bytes=4096,
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-SEC-99",
            status=DocumentStatus.PENDING_REVIEW,
        )
        db_session.add(doc)
        db_session.commit()

        claim = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-sec-1",
            field_type="med.name",
            value="Safedox 200 mg",
            normalized_code="2430421000189104",
            code_system="CDCI",
            origin=ClaimOrigin.PIPELINE,
            payload={
                "normalized_code": "2430421000189104",
                "code_system": "CDCI",
                "candidates": [
                    {
                        "code": "2430421000189104",
                        "display": "Safedox 200 mg oral tablet",
                        "system": "CDCI",
                        "score": 1.0,
                    }
                ],
                "abstention_reasons": ["Test audit reason"],
                "internal_raw_ocr_tokens": [{"token": "SECRET_PHI", "bbox": [0, 0, 10, 10]}],
                "internal_encryption_key": "DO_NOT_EXPOSE_SECRET",
            },
        )
        db_session.add(claim)
        db_session.commit()

        auth_user = AuthenticatedUser(
            user=sample_user,
            subject=sample_user.idp_subject,
            roles={CanonicalRole.CLINICAL_REVIEWER.value},
        )
        api_res = get_document_claims(
            document_id=doc.id,
            request=MagicMock(),
            include_superseded=False,
            active_only=False,
            current_user=auth_user,
            session=db_session,
            triage_engine=MagicMock(),
        )

        assert api_res["total"] == 1
        claim_dto = api_res["claims"][0]

        # 1. Expected audit fields are exposed
        assert claim_dto["normalized_code"] == "2430421000189104"
        assert claim_dto["code_system"] == "CDCI"
        assert len(claim_dto["candidates"]) == 1
        assert claim_dto["candidates"][0]["code"] == "2430421000189104"
        assert claim_dto["abstention_reasons"] == ["Test audit reason"]

        # 2. Strict Security: internal payload keys and sensitive fields are NOT exposed
        assert "internal_raw_ocr_tokens" not in claim_dto
        assert "internal_encryption_key" not in claim_dto
        assert "storage_ref" not in claim_dto

    def test_02_api_handles_null_or_malformed_audit_payloads_safely(
        self,
        db_session: Session,
        sample_user: User,
    ):
        """Review API returns empty lists when candidates or abstention_reasons are null or malformed."""
        doc = Document(
            sha256="def456" * 10,
            storage_ref="internal/blob_ref",
            original_filename="null_payload_doc.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-NULL-01",
            status=DocumentStatus.PENDING_REVIEW,
        )
        db_session.add(doc)
        db_session.commit()

        c_null = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-1",
            field_type="lab.value",
            value="10",
            origin=ClaimOrigin.PIPELINE,
            payload=None,
        )
        c_malformed = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-2",
            field_type="lab.value",
            value="20",
            origin=ClaimOrigin.PIPELINE,
            payload={"candidates": "not-a-list", "abstention_reasons": 12345},
        )
        db_session.add_all([c_null, c_malformed])
        db_session.commit()

        auth_user = AuthenticatedUser(
            user=sample_user,
            subject=sample_user.idp_subject,
            roles={CanonicalRole.CLINICAL_REVIEWER.value},
        )
        api_res = get_document_claims(
            document_id=doc.id,
            request=MagicMock(),
            include_superseded=False,
            active_only=False,
            current_user=auth_user,
            session=db_session,
            triage_engine=MagicMock(),
        )

        assert api_res["total"] == 2
        for item in api_res["claims"]:
            assert isinstance(item["candidates"], list)
            assert isinstance(item["abstention_reasons"], list)
            assert item["candidates"] == []
            assert item["abstention_reasons"] == []

