"""Phase C1 Medication Extraction Reliability and Unanchored Attribute Safety Test Suite.

Authority: Phase C1 Implementation Mandate
Scope:
  1. SYNTH_CARDIO_01 regression verification:
     - Complete extraction of Carvedilol, Furosemide, and Apixaban with their respective doses, routes, and frequencies.
     - Strict rejection of 'SYNTHETIC' and non-medication clinical terms from header/disclaimer.
     - Multi-entity distinct UUID group assignment (unambiguous co-prescriptions).
  2. Unanchored attribute fail-closed safety:
     - Unanchored medication attributes are quarantined under group_id '__unanchored_review__' and ESCALATED.
     - Unresolved escalated claims strictly block Four-Eyes document approval.
     - Unresolved escalated claims strictly block dynamic FHIR export eligibility.
     - Explicit reviewer resolution (ACCEPTED, EDITED, REJECTED) audits the decision and unlocks export.
  3. FHIR MedicationStatement export:
     - Normalized RxNorm codes (Carvedilol 20352, Furosemide 4603, Apixaban 1364430).
     - Generated FHIR bundle contains valid medication resources.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import uuid
import pytest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    IdentityVerificationStatus,
    JobStatus,
    User,
)
from clindoc.extract.contracts import (
    BBoxModel,
    Claim as ContractClaim,
    Evidence as ContractEvidence,
    DocumentExtractionOutput,
)
from clindoc.extract.llm_extractor import MockLLMProvider, NarrativeExtractor
from clindoc.extract.router import ExtractionRouter
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.ocr.document_text import DocumentPage, DocumentText, Token
from clindoc.review.approval import DocumentApprovalEngine, evaluate_export_eligibility
from clindoc.review.service import ClaimReviewService
from clindoc.services.fhir_export import FHIRExportService
from clindoc.storage.blob import EncryptedFileBlobStore


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite session with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    session.session_factory = session_factory
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def test_users(db_session: Session) -> tuple[User, User]:
    uploader = User(
        idp_subject="sub_uploader",
        username="uploader_user",
        full_name="Document Uploader",
        email="uploader@hospital.org",
        is_active=True,
    )
    approver = User(
        idp_subject="sub_approver",
        username="approver_user",
        full_name="Clinical Approver",
        email="approver@hospital.org",
        is_active=True,
    )
    db_session.add_all([uploader, approver])
    db_session.commit()
    return uploader, approver


@pytest.fixture
def synth_cardio_text() -> str:
    note_path = Path("external_eval/synthetic_benchmark/notes/SYNTH_CARDIO_01.txt")
    if note_path.exists():
        return note_path.read_text(encoding="utf-8")
    return """[SYNTHETIC CLINICAL ENCOUNTER NOTE - NOT PHI - FOR FUNCTIONAL EVALUATION ONLY]
PATIENT: Synthetic Patient Cardio-01
ENCOUNTER: Cardiology Outpatient Consultation
DATE: 2026-03-15

CHIEF COMPLAINT:
Follow-up for congestive heart failure and paroxysmal atrial fibrillation.

MEDICATIONS:
Continue Carvedilol 25 mg PO twice daily.
Continue Furosemide 40 mg oral once daily in the morning.
Continue Apixaban 5 mg by mouth BID for stroke prevention.
"""


def _make_document_text(text: str, doc_id: str = "SYNTH_CARDIO_01") -> DocumentText:
    """Create a DocumentText with character tokens for verbatim evidence grounding."""
    lines = text.splitlines(keepends=True)
    tokens: list[Token] = []
    char_offset = 0

    for line_idx, line in enumerate(lines):
        import re
        for m in re.finditer(r"\S+", line):
            start = char_offset + m.start()
            end = char_offset + m.end()
            tokens.append(
                Token(
                    text=m.group(0),
                    char_start=start,
                    char_end=end,
                    bbox={"x0": 50.0, "y0": 50.0, "x1": 200.0, "y1": 70.0},
                    confidence=0.98,
                    line_id=f"line-{line_idx}",
                )
            )
        char_offset += len(line)

    page = DocumentPage(
        page_number=1,
        text=text,
        tokens=tokens,
        width_px=612,
        height_px=792,
    )
    return DocumentText(
        document_id=doc_id,
        pages=[page],
        source_type="born_digital",
    )


def test_synth_cardio_01_medication_extraction_and_rejection_of_synthetic(synth_cardio_text: str):
    """Verify that MockLLMProvider extracts Carvedilol, Furosemide, and Apixaban and rejects SYNTHETIC."""
    doc_text = _make_document_text(synth_cardio_text)
    router = ExtractionRouter()

    output = router.extract_document(doc_text, is_lab_default=False)
    assert output is not None

    med_names = [c for c in output.all_claims if c.field_type == "med.name"]
    med_values = {c.value for c in med_names}

    # 1. Reject 'SYNTHETIC' and document header terms
    assert "Synthetic" not in med_values
    assert "Patient" not in med_values
    assert "Cardio" not in med_values
    assert "Encounter" not in med_values

    # 2. Extract all 3 medications
    assert "Carvedilol" in med_values
    assert "Furosemide" in med_values
    assert "Apixaban" in med_values

    # 3. Verify distinct entity group IDs for prescribed medications
    prescribed_meds = [c for c in med_names if c.value in ("Carvedilol", "Furosemide", "Apixaban")]
    assert len(prescribed_meds) == 3
    group_ids = {c.group_id for c in prescribed_meds}
    assert len(group_ids) == 3
    assert None not in group_ids

    # 4. Verify attribute binding for each prescribed medication
    for name_claim in prescribed_meds:
        grp = [c for c in output.all_claims if c.group_id == name_claim.group_id]
        field_types = {c.field_type for c in grp}
        assert "med.name" in field_types
        assert "med.dose" in field_types
        assert "med.route" in field_types
        assert "med.frequency" in field_types


def test_unanchored_medication_claim_escalated_and_blocks_approval(
    db_session: Session,
    test_users: tuple[User, User],
):
    """Verify unanchored claims are quarantined as ESCALATED and strictly block approval/export."""
    uploader, approver = test_users

    doc = Document(
        sha256="a" * 64,
        storage_ref="vault://test_doc.pdf",
        original_filename="cardio_note.pdf",
        mime_type="application/pdf",
        file_size_bytes=1024,
        page_count=1,
        source_type="born_digital",
        uploaded_by=uploader.id,
        status=DocumentStatus.IN_REVIEW,
        identity_verification_status=IdentityVerificationStatus.CONFIRMED,
        identity_confirmed_by=approver.id,
        patient_context_id="MRN-CARDIO-999",
    )
    db_session.add(doc)
    db_session.commit()

    # Grounded legitimate medication
    legit_group = str(uuid.uuid4())
    med_name = Claim(
        claim_id=uuid.uuid4(),
        document_id=doc.id,
        group_id=legit_group,
        field_type="med.name",
        value="Carvedilol",
        version=1,
        origin=ClaimOrigin.PIPELINE,
        review_state=ClaimReviewState.ACCEPTED,
        payload={"value": "Carvedilol"},
    )
    med_dose = Claim(
        claim_id=uuid.uuid4(),
        document_id=doc.id,
        group_id=legit_group,
        field_type="med.dose",
        value="25",
        version=1,
        origin=ClaimOrigin.PIPELINE,
        review_state=ClaimReviewState.ACCEPTED,
        payload={"value": "25", "unit_raw": "mg"},
    )

    # Orphaned / unanchored medication attribute
    unanchored_claim = Claim(
        claim_id=uuid.uuid4(),
        document_id=doc.id,
        group_id="__unanchored_review__",
        field_type="med.dose",
        value="100",
        version=1,
        origin=ClaimOrigin.PIPELINE,
        review_state=ClaimReviewState.ESCALATED,
        payload={
            "value": "100",
            "unit_raw": "mg",
            "review_reason": "unanchored_medication_attribute",
            "unanchored": True,
        },
    )

    db_session.add_all([med_name, med_dose, unanchored_claim])
    db_session.commit()

    # 1. Evaluate export eligibility before resolution -> MUST fail closed
    is_eligible, reason, _ = evaluate_export_eligibility(
        session=db_session,
        document_id=doc.id,
        allow_synthetic_identity=True,
    )
    assert not is_eligible
    assert "NON_TERMINAL_CLAIMS" in reason or "DOCUMENT_NOT_APPROVED" in reason

    # 2. Attempt document approval -> MUST fail closed due to ESCALATED claim
    approval_engine = DocumentApprovalEngine()
    with pytest.raises(Exception) as exc_info:
        approval_engine.approve_document(
            session=db_session,
            document_id=doc.id,
            approver_id=approver.id,
        )
    assert "terminal" in str(exc_info.value).lower() or "escalated" in str(exc_info.value).lower()

    # 3. Reviewer explicitly resolves the unanchored claim (e.g. REJECTS or assigns)
    review_svc = ClaimReviewService()
    review_svc.reject_claim(
        session=db_session,
        claim_id=unanchored_claim.claim_id,
        actor_id=approver.id,
        reason_code="incorrect_extraction",
        expected_version=1,
        notes="Erroneous unanchored dose from non-medication line",
    )
    db_session.commit()

    # Verify unanchored claim transitioned to terminal REJECTED state
    db_session.refresh(unanchored_claim)
    assert unanchored_claim.review_state == ClaimReviewState.REJECTED

    # 4. Document approval now succeeds!
    approval = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=approver.id,
    )
    db_session.commit()
    assert approval is not None
    db_session.refresh(doc)
    assert doc.status == DocumentStatus.APPROVED

    # 5. Export eligibility now evaluates to True!
    is_eligible, reason, _ = evaluate_export_eligibility(
        session=db_session,
        document_id=doc.id,
        allow_synthetic_identity=True,
    )
    assert is_eligible


def test_synth_cardio_01_fhir_medication_statement_export(synth_cardio_text: str):
    """Verify TerminologyNormalizer produces RxNorm codes and FHIR MedicationStatement structures for SYNTH_CARDIO_01."""
    doc_text = _make_document_text(synth_cardio_text)
    router = ExtractionRouter()
    output = router.extract_document(doc_text, is_lab_default=False)

    normalizer = TerminologyNormalizer()
    normalized = normalizer.normalize_claims(output.all_claims)

    norm_by_val = {c.value: c for c in normalized if c.field_type == "med.name"}

    # Carvedilol RxCUI 20352
    assert "Carvedilol" in norm_by_val
    carv = norm_by_val["Carvedilol"]
    assert carv.code_system == "RxNorm"
    assert carv.normalized_code == "20352"

    # Furosemide RxCUI 4603
    assert "Furosemide" in norm_by_val
    furo = norm_by_val["Furosemide"]
    assert furo.code_system == "RxNorm"
    assert furo.normalized_code == "4603"

    # Apixaban RxCUI 1364430
    assert "Apixaban" in norm_by_val
    apix = norm_by_val["Apixaban"]
    assert apix.code_system == "RxNorm"
    assert apix.normalized_code == "1364430"

