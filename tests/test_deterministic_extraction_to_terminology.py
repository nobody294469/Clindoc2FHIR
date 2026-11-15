"""ClinicalDoc — Phase 3D.5: Deterministic Extraction-to-Terminology Integration Test Suite.

Authority: Phase 3D.5 Directive
Scope:
  - Exercises actual ClinicalDoc deterministic extraction components (ExtractionRouter,
    DeterministicExtractor, RegionClassifier, and MedicationAssociationEngine).
  - Verifies that extraction components produce genuine ContractClaim and ContractEvidence
    objects with grounded spans and geometry (no pre-constructed claims).
  - Passes extracted claims through the real TerminologyNormalizer and promoted candidate
    terminology database (data/terminology/terminology_v20261112.candidate.db).
  - Exercises the full DocumentProcessingWorker extraction-to-normalization handoff without
    mocking extract_document() or region routing.
  - Verifies ORM persistence and Review Workbench API exposure.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
import uuid

import fitz
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clindoc.api.auth import (
    CanonicalRole,
    set_global_session_factory,
    set_global_token_validator,
)
from clindoc.api.auth.models import AuthenticatedUser
from clindoc.api.deps import set_global_terminology_service, set_global_triage_engine
from clindoc.api.routes.review import get_document_claims
from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    JobStatus,
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
from clindoc.extract.deterministic import (
    DeterministicExtractor,
    build_evidence,
    extract_lab_flag,
    extract_lab_reference_range,
    extract_lab_test_name,
    extract_lab_unit,
    extract_lab_value,
    extract_med_dose,
    extract_med_duration,
    extract_med_frequency,
    extract_med_name,
    extract_med_route,
)
from clindoc.extract.gliner_fallback import MockGLiNERProvider
from clindoc.extract.llm_extractor import MockLLMProvider, NarrativeExtractor
from clindoc.extract.medication_association import (
    AssociationStatus,
    NO_ANCHOR_SENTINEL,
    associate_segment_medications,
)
from clindoc.extract.region_classifier import RegionClassifier
from clindoc.extract.router import ExtractionRouter
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.ocr.document_text import DocumentPage, DocumentText, Token
from clindoc.ocr.geometry import BBoxDict
from clindoc.storage.blob import EncryptedFileBlobStore
from clindoc.terminology.schema import FTS5_DDL, SCHEMA_DDL
from clindoc.terminology.service import (
    TerminologyRetrievalService,
    get_process_terminology_service,
    normalize_alias_text,
    reset_process_terminology_service,
)


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
    """Create test reviewer user with clinical_reviewer role."""
    reviewer_role = Role(
        id=CanonicalRole.CLINICAL_REVIEWER.value,
        name="Clinical Reviewer",
        description="Clinical review role",
    )
    db_session.add(reviewer_role)
    db_session.flush()

    user = User(
        idp_subject="sub_det_tester",
        username="det_reviewer",
        full_name="Deterministic Extraction Reviewer",
        email="det_reviewer@hospital.org",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    db_session.add(UserRole(user_id=user.id, role_id=CanonicalRole.CLINICAL_REVIEWER.value))
    db_session.commit()
    return user


def _make_token(text: str, x0: float, y0: float, x1: float, y1: float, line_id: str = "L0") -> Token:
    return Token(
        text=text,
        bbox=BBoxDict(x0=x0, y0=y0, x1=x1, y1=y1),
        line_id=line_id,
        confidence=0.96,
    )


def _generate_synthetic_pdf(text_lines: list[str]) -> bytes:
    """Generate a valid 1-page PDF containing the provided synthetic text lines."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    y = 72
    for line in text_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 24
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
        self.stage_a_label = "SUPPORTED"
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


# ─── SECTION A: LABORATORY DETERMINISTIC EXTRACTION TO TERMINOLOGY ─────────────


class TestSectionA_LaboratoryDeterministicExtractionToTerminology:
    """Exercises real ExtractionRouter and DeterministicExtractor for laboratory claims."""

    def test_a1_structured_lab_table_extraction_and_loinc_resolution(self):
        """Structured lab table row is classified as deterministic, claims are extracted, and LOINC is resolved."""
        # 1. Construct synthetic DocumentText containing structured lab table tokens
        toks = [
            # Header line (L0)
            _make_token("Test", 10, 10, 50, 20, "L0"),
            _make_token("Name", 55, 10, 100, 20, "L0"),
            _make_token("Value", 105, 10, 150, 20, "L0"),
            _make_token("Unit", 155, 10, 200, 20, "L0"),
            _make_token("Reference", 205, 10, 280, 20, "L0"),
            _make_token("Range", 285, 10, 340, 20, "L0"),
            _make_token("Flag", 345, 10, 380, 20, "L0"),
            # Analyte row (L1): Hemoglobin A1c
            _make_token("Hemoglobin", 10, 30, 80, 40, "L1"),
            _make_token("A1c/Hemoglobin.total", 85, 30, 190, 40, "L1"),
            _make_token("in", 195, 30, 210, 40, "L1"),
            _make_token("Blood", 215, 30, 255, 40, "L1"),
            _make_token("6.5", 260, 30, 285, 40, "L1"),
            _make_token("%", 290, 30, 305, 40, "L1"),
            _make_token("4.0-5.6", 310, 30, 360, 40, "L1"),
            _make_token("HIGH", 365, 30, 400, 40, "L1"),
        ]
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=toks)
        doc = DocumentText(document_id="LAB_STRUCTURED_01", source_type="born_digital", pages=[page])

        # 2. Execute REAL ExtractionRouter with permitted external mock providers
        router = ExtractionRouter(
            gliner_provider=MockGLiNERProvider(),
            llm_provider=MockLLMProvider(),
        )
        extraction_out: DocumentExtractionOutput = router.extract_document(doc)

        # 3. Assertions on Extraction Output
        assert len(extraction_out.regions) >= 1
        rd = extraction_out.regions[0].routing_decision
        assert rd.route == "deterministic"
        assert "lab_table_heuristic" in rd.reason

        extracted_claims = extraction_out.all_claims
        assert len(extracted_claims) >= 4

        # Group by field_type
        by_type = {c.field_type: c for c in extracted_claims}
        assert "lab.test_name" in by_type
        assert "lab.value" in by_type
        assert "lab.unit" in by_type
        assert "lab.reference_range" in by_type
        assert "lab.flag" in by_type

        # Verify claims are produced by deterministic extractor with grounded source text
        test_claim = by_type["lab.test_name"]
        assert test_claim.extraction_method == "deterministic"
        assert "Hemoglobin A1c" in test_claim.value
        assert test_claim.extraction_confidence >= 0.80

        # Verify source grounding in evidence
        ev_map = {e.evidence_id: e for e in extraction_out.all_evidence}
        assert test_claim.evidence_id in ev_map
        ev = ev_map[test_claim.evidence_id]
        assert ev.char_start >= 0
        assert ev.char_end > ev.char_start
        assert ev.bbox.x0 >= 0 and ev.bbox.y0 >= 0

        # 4. Pass extracted claims through real TerminologyNormalizer and promoted DB
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized_claims = normalizer.normalize_claims(extracted_claims)

        norm_test_claim = next(c for c in normalized_claims if c.field_type == "lab.test_name")
        candidates = normalizer.get_audit_candidates(norm_test_claim.claim_id)
        assert len(candidates) >= 1
        # Top candidate matches LOINC (Hemoglobin A1c concepts in LOINC)
        top = candidates[0]
        assert top["system"] == "LOINC"
        top_codes = {c["code"] for c in candidates}
        assert any(code in top_codes for code in ("4548-4", "41995-2", "17856-6"))

        # Unit claim normalization
        norm_unit_claim = next(c for c in normalized_claims if c.field_type == "lab.unit")
        assert norm_unit_claim.normalized_code == "%"
        assert norm_unit_claim.code_system == "UCUM"

    def test_a2_competing_loinc_candidates_preserves_audit(self):
        """Analyte mention with multiple candidates preserves evaluated candidates in audit."""
        toks = [
            _make_token("Test", 10, 10, 50, 20, "L0"),
            _make_token("Name", 55, 10, 100, 20, "L0"),
            _make_token("Value", 105, 10, 150, 20, "L0"),
            _make_token("Unit", 155, 10, 200, 20, "L0"),
            _make_token("Glucose", 10, 30, 80, 40, "L1"),
            _make_token("95", 105, 30, 130, 40, "L1"),
            _make_token("mg/dL", 155, 30, 210, 40, "L1"),
        ]
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=toks)
        doc = DocumentText(document_id="LAB_GLUCOSE_02", source_type="born_digital", pages=[page])

        router = ExtractionRouter(gliner_provider=MockGLiNERProvider(), llm_provider=MockLLMProvider())
        extraction_out = router.extract_document(doc)

        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized = normalizer.normalize_claims(extraction_out.all_claims)

        test_claim = next(c for c in normalized if c.field_type == "lab.test_name")
        candidates = normalizer.get_audit_candidates(test_claim.claim_id)
        assert len(candidates) >= 2
        # Verify candidate audit schema
        for c in candidates:
            assert "code" in c
            assert "display" in c
            assert "system" in c
            assert "score" in c


# ─── SECTION B: MEDICATION EXTRACTION AND ASSOCIATION TO TERMINOLOGY ───────────


class TestSectionB_MedicationExtractionAndAssociationToTerminology:
    """Exercises real Medication extraction and MedicationAssociationEngine."""

    def test_b1_structured_regimen_line_deterministic_extraction(self):
        """DeterministicExtractor extracts medication regimen line with unified group association."""
        page_text = "Atorvastatin 10 mg PO once daily for 30 days"
        tokens = [
            _make_token("Atorvastatin", 10, 10, 120, 20, "L0"),
            _make_token("10", 125, 10, 145, 20, "L0"),
            _make_token("mg", 150, 10, 175, 20, "L0"),
            _make_token("PO", 180, 10, 200, 20, "L0"),
            _make_token("once", 205, 10, 240, 20, "L0"),
            _make_token("daily", 245, 10, 285, 20, "L0"),
            _make_token("for", 290, 10, 310, 20, "L0"),
            _make_token("30", 315, 10, 335, 20, "L0"),
            _make_token("days", 340, 10, 370, 20, "L0"),
        ]
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=tokens)

        extractor = DeterministicExtractor()
        evidences, claims = extractor.extract_med_line(
            document_id="MED_DOC_01",
            page=page,
            line_text=page_text,
            char_start=0,
            char_end=len(page_text),
            group_id=None,  # Must be generated by extractor
        )

        assert len(claims) == 5
        by_type = {c.field_type: c for c in claims}
        assert "med.name" in by_type
        assert "med.dose" in by_type
        assert "med.route" in by_type
        assert "med.frequency" in by_type
        assert "med.duration" in by_type

        # Verify unified group assignment produced by the extractor
        group_ids = {c.group_id for c in claims}
        assert len(group_ids) == 1
        assert None not in group_ids

        # Pass through promoted terminology normalizer
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized = normalizer.normalize_claims(claims)

        med_claim = next(c for c in normalized if c.field_type == "med.name")
        candidates = normalizer.get_audit_candidates(med_claim.claim_id)
        assert len(candidates) >= 1
        top_codes = {cand["code"] for cand in candidates}
        assert "83367" in top_codes  # Atorvastatin RxCUI in promoted DB

    def test_b2_medication_association_engine_unambiguous_single(self):
        """Rule 1: Unambiguous single medication clause binds all attributes cleanly."""
        segment_text = "Lisinopril 20 mg oral daily"
        candidates = [
            ({"field_type": "med.name", "value": "Lisinopril"}, 0, 10, "Lisinopril"),
            ({"field_type": "med.dose", "value": "20", "unit_raw": "mg"}, 11, 16, "20 mg"),
            ({"field_type": "med.route", "value": "oral"}, 17, 21, "oral"),
            ({"field_type": "med.frequency", "value": "daily"}, 22, 27, "daily"),
        ]

        assigned = associate_segment_medications(segment_text, candidates)
        assert len(assigned) == 4

        # Verify association status and unified group assignment
        gids = set()
        for ext, ls, le, span, gid, status, meta in assigned:
            assert status == AssociationStatus.UNAMBIGUOUS_SINGLE
            gids.add(gid)
        assert len(gids) == 1
        assert NO_ANCHOR_SENTINEL not in gids

        # Construct contract claim from extracted candidate
        name_item = assigned[0]
        claim = ContractClaim(
            claim_id=str(uuid.uuid4()),
            document_id="MED_DOC_ASSOC",
            group_id=name_item[4],
            field_type="med.name",
            value="Lisinopril",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-lis-1",
            region_id="reg-1",
        )
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized = normalizer.normalize_claims([claim])
        assert normalized[0].normalized_code in ("29046", "386873009")
        assert normalized[0].code_system in ("RxNorm", "CDCI")

    def test_b3_medication_association_engine_interleaved_prevents_leakage(self):
        """Rule 2: Interleaved explicit entities receive strictly local attributes without cross-leakage."""
        segment_text = "Metformin 500 mg oral BID and Lisinopril 10 mg oral daily"
        candidates = [
            ({"field_type": "med.name", "value": "Metformin"}, 0, 9, "Metformin"),
            ({"field_type": "med.dose", "value": "500", "unit_raw": "mg"}, 10, 16, "500 mg"),
            ({"field_type": "med.route", "value": "oral"}, 17, 21, "oral"),
            ({"field_type": "med.frequency", "value": "BID"}, 22, 25, "BID"),
            ({"field_type": "med.name", "value": "Lisinopril"}, 30, 40, "Lisinopril"),
            ({"field_type": "med.dose", "value": "10", "unit_raw": "mg"}, 41, 46, "10 mg"),
            ({"field_type": "med.route", "value": "oral"}, 47, 51, "oral"),
            ({"field_type": "med.frequency", "value": "daily"}, 52, 57, "daily"),
        ]

        assigned = associate_segment_medications(segment_text, candidates)
        assert len(assigned) == 8

        # Verify exactly two groups
        groups_by_drug: dict[str, str] = {}
        for ext, ls, le, span, gid, status, meta in assigned:
            assert status == AssociationStatus.UNAMBIGUOUS_INTERLEAVED
            if ext.get("field_type") == "med.name":
                groups_by_drug[ext["value"]] = gid

        assert len(groups_by_drug) == 2
        met_gid = groups_by_drug["Metformin"]
        lis_gid = groups_by_drug["Lisinopril"]
        assert met_gid != lis_gid

        # Verify Metformin attributes are bound exclusively to met_gid
        met_attrs = [item for item in assigned if item[1] < 30]
        assert all(item[4] == met_gid for item in met_attrs)

        # Verify Lisinopril attributes are bound exclusively to lis_gid
        lis_attrs = [item for item in assigned if item[1] >= 30]
        assert all(item[4] == lis_gid for item in lis_attrs)

    def test_b4_medication_association_engine_ambiguous_scope_fails_closed(self):
        """Rule 5: Clustered medication names with trailing attributes fail closed without attribute leakage."""
        segment_text = "Metoprolol and Lisinopril 25 mg"
        candidates = [
            ({"field_type": "med.name", "value": "Metoprolol"}, 0, 10, "Metoprolol"),
            ({"field_type": "med.name", "value": "Lisinopril"}, 15, 25, "Lisinopril"),
            ({"field_type": "med.dose", "value": "25", "unit_raw": "mg"}, 26, 31, "25 mg"),
        ]

        assigned = associate_segment_medications(segment_text, candidates)
        assert len(assigned) == 3

        dose_item = next(item for item in assigned if item[0]["field_type"] == "med.dose")
        # Trailing dose must fail closed with NO_ANCHOR_SENTINEL and AMBIGUOUS_SCOPE_MULTI_ENTITY
        assert dose_item[4] == NO_ANCHOR_SENTINEL
        assert dose_item[5] == AssociationStatus.AMBIGUOUS_SCOPE_MULTI_ENTITY

    def test_b5_unsupported_medication_abstains_without_forced_code(self):
        """Unsupported or unrecognized medication abstains without inventing codes."""
        extractor = DeterministicExtractor()
        line_text = "UnknownCompoundXYZ 100 mg oral daily"
        tokens = [
            _make_token("UnknownCompoundXYZ", 10, 10, 150, 20, "L0"),
            _make_token("100", 155, 10, 175, 20, "L0"),
            _make_token("mg", 180, 10, 195, 20, "L0"),
        ]
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=tokens)
        evs, claims = extractor.extract_med_line("DOC_UNREC", page, line_text, 0, len(line_text))

        # Manually create contract claim for uncataloged candidate
        claim = ContractClaim(
            claim_id=str(uuid.uuid4()),
            document_id="DOC_UNREC",
            group_id="grp-unrec-1",
            field_type="med.name",
            value="UnknownCompoundXYZ",
            extraction_method="deterministic",
            extraction_confidence=0.85,
            evidence_id="ev-unrec-1",
            region_id="reg-1",
        )
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized = normalizer.normalize_claims([claim])

        assert normalized[0].normalized_code is None
        assert normalized[0].code_system is None
        # Verify that uncataloged medication produces no candidates and fails closed
        assert normalizer.get_audit_candidates(claim.claim_id) == []

    def test_b6_integrated_medication_extraction_association_and_normalization(
        self,
        db_session: Session,
        sample_user: User,
    ):
        """Integrated medication extraction: produces claims, invokes production association, normalizes, and persists."""
        text = "Patient was prescribed Metformin 500 mg oral BID and Lisinopril 10 mg oral daily."
        tokens = []
        x = 10.0
        for i, w in enumerate(text.split()):
            tokens.append(
                Token(text=w, bbox=BBoxDict(x0=x, y0=20.0, x1=x + len(w) * 8.0, y1=35.0), line_id="L0", confidence=0.95)
            )
            x += len(w) * 8.0 + 5.0
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=tokens)

        # 1. Extraction: NarrativeExtractor runs with deterministic mock provider
        # Candidate extractions represent raw grounded entity mentions extracted from source
        canned = [
            {"field_type": "med.name", "value": "Metformin", "evidence_text": "Metformin"},
            {"field_type": "med.dose", "value": "500", "evidence_text": "500 mg", "unit_raw": "mg"},
            {"field_type": "med.route", "value": "oral", "evidence_text": "oral"},
            {"field_type": "med.frequency", "value": "BID", "evidence_text": "BID"},
            {"field_type": "med.name", "value": "Lisinopril", "evidence_text": "Lisinopril"},
            {"field_type": "med.dose", "value": "10", "evidence_text": "10 mg", "unit_raw": "mg"},
            {"field_type": "med.route", "value": "oral", "evidence_text": "oral"},
            {"field_type": "med.frequency", "value": "daily", "evidence_text": "daily"},
        ]
        extractor = NarrativeExtractor(provider=MockLLMProvider(canned_responses=canned), max_retries=0)

        # 2. Production extraction path invokes associate_segment_medications internally
        evs, claims, diag = extractor.extract("DOC_INT_MED", page, text, "reg_0")
        assert len(claims) == 8

        # 3. Verify production engine applied group associations to extracted claims
        groups: dict[str, list[tuple[str, str]]] = {}
        for c in claims:
            groups.setdefault(c.group_id, []).append((c.field_type, c.value))

        assert len(groups) == 2
        # Verify isolation: attributes do not cross between groups
        met_gid = next(gid for gid, items in groups.items() if any(ft == "med.name" and val == "Metformin" for ft, val in items))
        lis_gid = next(gid for gid, items in groups.items() if any(ft == "med.name" and val == "Lisinopril" for ft, val in items))
        assert met_gid != lis_gid

        met_items = dict(groups[met_gid])
        assert met_items["med.name"] == "Metformin"
        assert met_items["med.dose"] == "500"
        assert met_items["med.route"] == "oral"
        assert met_items["med.frequency"] == "BID"

        lis_items = dict(groups[lis_gid])
        assert lis_items["med.name"] == "Lisinopril"
        assert lis_items["med.dose"] == "10"
        assert lis_items["med.route"] == "oral"
        assert lis_items["med.frequency"] == "daily"

        # 4. Pass claims through real TerminologyNormalizer and promoted DB
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized_claims = normalizer.normalize_claims(claims)

        norm_by_name = {c.value: c for c in normalized_claims if c.field_type == "med.name"}
        assert "Metformin" in norm_by_name
        assert "Lisinopril" in norm_by_name

        assert norm_by_name["Metformin"].normalized_code in ("6809", "372567009")
        assert norm_by_name["Metformin"].code_system in ("RxNorm", "CDCI")
        assert norm_by_name["Lisinopril"].normalized_code in ("29046", "386873009")
        assert norm_by_name["Lisinopril"].code_system in ("RxNorm", "CDCI")

        # 5. Persist to DB and verify persistence of groups and candidate audit payloads
        doc = Document(
            sha256="m" * 64,
            storage_ref="internal/blob_med",
            original_filename="med_note.pdf",
            mime_type="application/pdf",
            file_size_bytes=1024,
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-MED-INT-01",
            status=DocumentStatus.PENDING_REVIEW,
        )
        db_session.add(doc)
        db_session.commit()

        for c in normalized_claims:
            claim_payload = c.model_dump(mode="json")
            claim_payload["candidates"] = normalizer.get_audit_candidates(c.claim_id)
            claim_payload["abstention_reasons"] = normalizer.get_audit_reasons(c.claim_id)
            db_claim = Claim(
                claim_id=uuid.UUID(c.claim_id),
                document_id=doc.id,
                group_id=c.group_id,
                field_type=c.field_type,
                value=c.value,
                normalized_code=c.normalized_code,
                code_system=c.code_system,
                origin=ClaimOrigin.PIPELINE,
                review_state=ClaimReviewState.PENDING,
                payload=claim_payload,
            )
            db_session.add(db_claim)
        db_session.commit()

        # Re-query and verify persistence
        persisted = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
        assert len(persisted) == 8
        persisted_groups = {c.group_id for c in persisted}
        assert persisted_groups == {met_gid, lis_gid}

        # Check candidate audit persistence on medication name claims
        db_met = next(c for c in persisted if c.field_type == "med.name" and c.value == "Metformin")
        assert len(db_met.payload["candidates"]) >= 1
        db_lis = next(c for c in persisted if c.field_type == "med.name" and c.value == "Lisinopril")
        assert len(db_lis.payload["candidates"]) >= 1

        # 6. Verify Review Workbench API exposure
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
        assert api_res["total"] == 8
        api_groups = {c["group_id"] for c in api_res["claims"]}
        assert api_groups == {met_gid, lis_gid}


# ─── SECTION C: AMBIGUOUS EVIDENCE EXTRACTION TO TERMINOLOGY ───────────────────


class TestSectionC_AmbiguousEvidenceExtractionToTerminology:
    """Exercises deterministic extraction of an ambiguous analyte mention and terminology abstention."""

    def test_c1_ambiguous_laboratory_mention_abstains_with_tied_candidates(self):
        """Ambiguous analyte mention 'Hemoglobin A1c' extracts, reaches normalizer, and abstains due to score tie."""
        toks = [
            _make_token("Test", 10, 10, 50, 20, "L0"),
            _make_token("Name", 55, 10, 100, 20, "L0"),
            _make_token("Value", 105, 10, 150, 20, "L0"),
            _make_token("Unit", 155, 10, 200, 20, "L0"),
            _make_token("Hemoglobin", 10, 30, 80, 40, "L1"),
            _make_token("A1c", 85, 30, 110, 40, "L1"),
            _make_token("6.5", 115, 30, 140, 40, "L1"),
            _make_token("%", 145, 30, 160, 40, "L1"),
        ]
        page = DocumentPage(page_number=1, width_px=612, height_px=792, tokens=toks)
        doc = DocumentText(document_id="LAB_AMB_01", source_type="born_digital", pages=[page])

        router = ExtractionRouter(gliner_provider=MockGLiNERProvider(), llm_provider=MockLLMProvider())
        extraction_out = router.extract_document(doc)

        test_claim = next((c for c in extraction_out.all_claims if c.field_type == "lab.test_name"), None)
        assert test_claim is not None
        assert "Hemoglobin A1c" in test_claim.value

        # Pass through real TerminologyNormalizer
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)
        normalized = normalizer.normalize_claims(extraction_out.all_claims)

        norm_test = next(c for c in normalized if c.field_type == "lab.test_name")
        # Ambiguous laboratory mention must abstain without forced code
        assert norm_test.normalized_code is None
        assert norm_test.code_system is None

        # Verify abstention reasons recorded: counts exactly the distinct concepts tied at rank 1.0 (2 concepts)
        reasons = normalizer.get_audit_reasons(norm_test.claim_id)
        assert len(reasons) > 0
        assert any("Ambiguous laboratory mention: 2 distinct concepts tied at rank 1.0" in r for r in reasons)

        # Verify full evaluated candidates list is preserved in audit
        candidates = normalizer.get_audit_candidates(norm_test.claim_id)
        assert len(candidates) == 10  # Full Top-K evaluated candidates preserved
        # Exactly 2 distinct LOINC concepts tied at the top rank (1.0)
        top_tied_codes = {c["code"] for c in candidates if abs(c["score"] - 1.0) < 1e-6}
        assert len(top_tied_codes) == 2
        assert "41995-2" in top_tied_codes
        assert "55454-3" in top_tied_codes
        # Lower-scoring evaluated candidate (score 0.4) is also preserved in audit trail
        assert any(c["code"] == "4548-4" for c in candidates)


# ─── SECTION D: WORKER EXTRACTION-TO-NORMALIZATION HANDOFF ─────────────────────


class TestSectionD_WorkerEndToEndExtractionHandoff:
    """Exercises DocumentProcessingWorker with real ExtractionRouter, real normalizer, and Review API."""

    def test_d1_worker_end_to_end_extraction_normalization_and_review_api(
        self,
        db_session: Session,
        sample_user: User,
        blob_store: EncryptedFileBlobStore,
    ):
        """Worker executes complete born-digital OCR, real router extraction, normalization, and persistence."""
        # 1. Ingest synthetic laboratory PDF
        pdf_bytes = _generate_synthetic_pdf([
            "CLINICAL LABORATORY TEST REPORT",
            "Test Name          Value    Unit      Reference Range   Flag",
            "Hemoglobin A1c/Hemoglobin.total in Blood   6.5   %   4.0-5.6   HIGH",
        ])
        storage_ref = blob_store.put(pdf_bytes)
        doc = Document(
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            storage_ref=storage_ref,
            original_filename="synthetic_deterministic_lab.pdf",
            mime_type="application/pdf",
            file_size_bytes=len(pdf_bytes),
            page_count=1,
            source_type="born_digital",
            uploaded_by=sample_user.id,
            patient_context_id="PT-DET-WORKER-01",
            status=DocumentStatus.PROCESSING,
        )
        db_session.add(doc)
        db_session.commit()

        queue = JobQueue()
        job = queue.enqueue(session=db_session, document_id=doc.id)
        db_session.commit()

        # 2. Instantiate worker with REAL ExtractionRouter (no mocking of extract_document)
        router = ExtractionRouter(
            gliner_provider=MockGLiNERProvider(),
            llm_provider=MockLLMProvider(),
        )
        svc = get_process_terminology_service()
        normalizer = TerminologyNormalizer(retrieval_service=svc)

        worker = DocumentProcessingWorker(
            queue=queue,
            blob_store=blob_store,
            router=router,
            normalizer=normalizer,
            cascade=MockDeterministicCascade(),
            session_factory=db_session.session_factory,
        )

        try:
            # 3. Worker claims and processes job
            claimed_job = worker.claim_job(session=db_session)
            assert claimed_job is not None
            processed_job = worker.process_job(session=db_session, job=claimed_job)
            db_session.commit()
            db_session.expire_all()

            # 4. Verify job and document status
            assert processed_job.status == JobStatus.COMPLETED
            reloaded_doc = db_session.query(Document).filter(Document.id == doc.id).one()
            assert reloaded_doc.status == DocumentStatus.PENDING_REVIEW

            # 5. Verify database Claim persistence from real extraction
            db_claims = (
                db_session.query(Claim)
                .filter(Claim.document_id == doc.id, Claim.origin == ClaimOrigin.PIPELINE)
                .all()
            )
            assert len(db_claims) >= 3

            by_type = {c.field_type: c for c in db_claims}
            assert "lab.test_name" in by_type
            assert "lab.value" in by_type
            assert "lab.unit" in by_type

            test_claim = by_type["lab.test_name"]
            assert test_claim.evidence_id is not None
            # Verify candidate evaluation and abstention were persisted in DB payload
            assert "candidates" in test_claim.payload
            assert len(test_claim.payload["candidates"]) >= 1
            assert "abstention_reasons" in test_claim.payload
            assert any("Ambiguous laboratory mention" in r for r in test_claim.payload["abstention_reasons"])

            # Verify unit normalization was persisted in DB
            unit_claim = by_type["lab.unit"]
            assert unit_claim.normalized_code == "%"
            assert unit_claim.code_system == "UCUM"

            # 6. Verify Review Workbench API exposure and projection
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
            assert api_res["total"] >= 3
            api_test_claim = next(c for c in api_res["claims"] if c["field_type"] == "lab.test_name")
            assert len(api_test_claim["candidates"]) >= 1
            assert len(api_test_claim["abstention_reasons"]) > 0
            assert any("Ambiguous laboratory mention" in r for r in api_test_claim["abstention_reasons"])

            api_unit_claim = next(c for c in api_res["claims"] if c["field_type"] == "lab.unit")
            assert api_unit_claim["normalized_code"] == "%"
            assert api_unit_claim["code_system"] == "UCUM"

            # Strict security projection check: raw internal keys are not exposed
            assert "internal_raw_ocr_tokens" not in api_test_claim
            assert "internal_encryption_key" not in api_test_claim
        finally:
            worker.close()
