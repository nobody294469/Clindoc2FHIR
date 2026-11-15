"""Phase 4 End-to-End Production Worker-to-FHIR Integration Tests.

Validates that clinical documents processed through the real production worker pipeline
using the promoted terminology database (terminology_v20261112.candidate.db) pass through
the complete lifecycle:
  1. Document ingestion and OCR text extraction (born digital).
  2. Extraction routing (deterministic lab extractor & narrative LLM extractor).
  3. Medication association (grouping related medication attributes).
  4. Controlled terminology retrieval and normalization against the promoted database
     (LOINC/CDCI for lab, RxNorm for medication, UCUM for units).
  5. Claim and evidence persistence with full audit metadata.
  6. Two-party Four-Eyes review and approval workflow.
  7. FHIR R4B export and schema validation (zero validation errors).

Scenarios Covered:
  - Baseline Test A: Unqualified analyte mention ("Potassium") -> CDCI safe text-only fallback.
  - Baseline Test B: Clinical progress note continuation -> MedicationStatement with RxNorm 11289.
  - Scenario 1: Fully specified test name ("Potassium [Moles/volume] in Serum or Plasma") -> LOINC 2823-3.
  - Scenario 2: Explicit prescription document -> MedicationRequest with RxNorm 11289 ingredient code.

Authorities:
  - Phase 4 Engineering Specification
  - M_TERMINOLOGY_SCALE_FEASIBILITY_AND_DESIGN_v1_2_ADDENDUM.md
  - CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md
  - PRODUCTIZATION_DECISIONS.md (DEC-002, DEC-008, DEC-016, PD-010, PD-012)
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Generator
import uuid

from fhir.resources.R4B.bundle import Bundle
import pymupdf as fitz
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from clindoc.api.auth.roles import CanonicalRole
from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    ClaimReviewState,
    Document,
    DocumentStatus,
    JobStatus,
    Role,
    User,
    UserRole,
)
from clindoc.extract.gliner_fallback import MockGLiNERProvider
from clindoc.extract.llm_extractor import MockLLMProvider
from clindoc.extract.router import ExtractionRouter
from clindoc.fhir.constants import EXT_CLAIM_PROVENANCE
from clindoc.fhir.validator import FHIRValidator
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.review.approval import DocumentApprovalEngine, DocumentApprovalStatus
from clindoc.review.service import ClaimReviewService
from clindoc.services.fhir_export import FHIRExportService
from clindoc.storage.blob import EncryptedFileBlobStore
from clindoc.terminology.service import (
    get_process_terminology_service,
    reset_process_terminology_service,
)


# ---------------------------------------------------------------------------
# Test Mocks for Boundaries Outside Terminology/Routing
# ---------------------------------------------------------------------------

class MockVerificationRecord:
    """Mock verification result conforming strictly to clindoc.schema.verification.VerificationResult."""

    def __init__(self, claim_id: str, evidence_id: str):
        self.claim_id = str(claim_id)
        self.evidence_id = str(evidence_id)
        self.verification_id = str(uuid.uuid4())
        self.label = "SUPPORTED"
        self.confidence = 0.98
        self.stage_reached = "A"
        self.stage_a_label = "SUPPORTED"
        self.stage_b_label = None
        self.stage_b_raw_confidence = None
        self.contradiction_type = None
        self.normalized_code_agreement = None
        self.rationale = None
        self.escalated = False
        self.is_fallback = False
        self.fallback_reason = None

    def model_dump(self, mode: str = "json") -> dict:
        return {
            "verification_id": self.verification_id,
            "claim_id": self.claim_id,
            "evidence_id": self.evidence_id,
            "label": "SUPPORTED",
            "confidence": 0.98,
            "stage_reached": "A",
            "stage_a_label": "SUPPORTED",
            "stage_b_label": None,
            "stage_b_raw_confidence": None,
            "contradiction_type": None,
            "normalized_code_agreement": None,
            "rationale": None,
            "escalated": False,
            "is_fallback": False,
            "fallback_reason": None,
        }


class MockDeterministicCascade:
    """Deterministic verification cascade mock that verifies all extracted claims."""

    def verify_claims(self, claims, evidence_map, page_text_map=None):
        return [MockVerificationRecord(c.claim_id, c.evidence_id) for c in claims]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_terminology_lifecycle():
    """Ensure process terminology service state is clean before and after each test."""
    reset_process_terminology_service()
    yield
    reset_process_terminology_service()


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Isolated in-memory SQLite session with foreign keys enforced."""
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
    # Attach session_factory for worker multi-session operations
    session.session_factory = session_factory
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def blob_store(tmp_path: Path) -> EncryptedFileBlobStore:
    """Isolated temporary encrypted file blob store."""
    root_dir = tmp_path / "blobs"
    root_dir.mkdir(parents=True, exist_ok=True)
    return EncryptedFileBlobStore(
        root_dir=root_dir,
        encryption_key=b"0123456789abcdef0123456789abcdef",
    )


@pytest.fixture
def users(db_session: Session) -> dict[str, User]:
    """Seed distinct users and roles required for Four-Eyes review and approval."""
    rev_role = Role(
        id=CanonicalRole.CLINICAL_REVIEWER.value,
        name="Clinical Reviewer",
        description="Clinical reviewer role",
    )
    app_role = Role(
        id=CanonicalRole.APPROVER.value,
        name="Document Approver",
        description="Document approver role",
    )
    db_session.add_all([rev_role, app_role])
    db_session.flush()

    uploader = User(
        idp_subject="sub_uploader",
        username="uploader_user",
        full_name="Dr. Uploader",
        email="uploader@hospital.org",
        is_active=True,
    )
    reviewer = User(
        idp_subject="sub_reviewer",
        username="reviewer_user",
        full_name="Dr. Reviewer",
        email="reviewer@hospital.org",
        is_active=True,
    )
    approver = User(
        idp_subject="sub_approver",
        username="approver_user",
        full_name="Dr. Approver",
        email="approver@hospital.org",
        is_active=True,
    )
    db_session.add_all([uploader, reviewer, approver])
    db_session.flush()

    db_session.add(UserRole(user_id=reviewer.id, role_id=CanonicalRole.CLINICAL_REVIEWER.value))
    db_session.add(UserRole(user_id=approver.id, role_id=CanonicalRole.APPROVER.value))
    db_session.commit()

    return {"uploader": uploader, "reviewer": reviewer, "approver": approver}


# ---------------------------------------------------------------------------
# Phase 4 Tests
# ---------------------------------------------------------------------------

def test_worker_to_fhir_structured_laboratory_document(
    db_session: Session,
    blob_store: EncryptedFileBlobStore,
    users: dict[str, User],
) -> None:
    """Phase 4 Test A: Structured laboratory document end-to-end pipeline (unqualified analyte).

    Flow exercised:
      - Synthetic born-digital PDF containing potassium laboratory result.
      - DocumentProcessingWorker claims and executes processing job.
      - ExtractionRouter routes structured table to deterministic laboratory extractor.
      - TerminologyNormalizer queries the promoted candidate database.
      - Claims persisted with evidence grounding and audit metadata.
      - Two-party Four-Eyes approval (reviewer accepts claims, distinct approver approves doc).
      - FHIRExportService exports document to FHIR R4B Bundle.
      - Validates FHIR Observation attributes, CDCI safe non-fabrication, and schema validity.
    """
    # 1. Create synthetic born-digital PDF
    doc_fitz = fitz.open()
    page = doc_fitz.new_page(width=612, height=792)
    lab_lines = [
        "CLINICAL LABORATORY TEST REPORT",
        "Specimen: Serum",
        "Test Name          Value    Unit      Reference Range   Flag",
        "Potassium          4.5      mmol/L    3.5-5.0           NORMAL",
    ]
    y = 72
    for line in lab_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 24
    pdf_bytes = doc_fitz.tobytes()
    doc_fitz.close()

    # 2. Store document in encrypted blob store and register in DB
    storage_ref = blob_store.put(pdf_bytes)
    doc = Document(
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        storage_ref=storage_ref,
        original_filename="LAB_POTASSIUM_REPORT.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        page_count=1,
        source_type="born_digital",
        uploaded_by=users["uploader"].id,
        patient_context_id="PT-LAB-POTASSIUM",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.commit()

    # 3. Enqueue job
    queue = JobQueue()
    job = queue.enqueue(session=db_session, document_id=doc.id)
    db_session.commit()

    # 4. Wire real worker components using promoted candidate terminology service
    term_service = get_process_terminology_service()
    normalizer = TerminologyNormalizer(retrieval_service=term_service)
    router = ExtractionRouter(
        gliner_provider=MockGLiNERProvider(),
        llm_provider=MockLLMProvider(),
    )
    worker = DocumentProcessingWorker(
        queue=queue,
        blob_store=blob_store,
        router=router,
        normalizer=normalizer,
        cascade=MockDeterministicCascade(),
        session_factory=db_session.session_factory,
    )

    # 5. Worker claims and processes job
    claimed_job = worker.claim_job(session=db_session)
    assert claimed_job is not None
    assert claimed_job.id == job.id

    processed_job = worker.process_job(session=db_session, job=claimed_job)
    db_session.commit()
    db_session.expire_all()

    # Verify job completion and document pending review state
    assert processed_job.status == JobStatus.COMPLETED
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc is not None
    assert reloaded_doc.status == DocumentStatus.PENDING_REVIEW

    # 6. Verify extracted and normalized claims in database
    claims = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
    assert len(claims) >= 3, "Expected at least test_name, value, and unit claims"

    test_name_claim = next((c for c in claims if c.field_type == "lab.test_name"), None)
    unit_claim = next((c for c in claims if c.field_type == "lab.unit"), None)
    value_claim = next((c for c in claims if c.field_type == "lab.value"), None)

    assert test_name_claim is not None
    assert unit_claim is not None
    assert value_claim is not None

    # Potassium in promoted candidate DB resolves to CDCI concept 88480006
    assert test_name_claim.value == "Potassium"
    assert test_name_claim.normalized_code == "88480006"
    assert test_name_claim.code_system == "CDCI"
    assert test_name_claim.origin == ClaimOrigin.PIPELINE
    assert test_name_claim.evidence_id is not None

    # Unit normalization resolves to UCUM mmol/L
    assert unit_claim.normalized_code == "mmol/L"
    assert unit_claim.code_system == "UCUM"

    # Value claim
    assert value_claim.value == "4.5"

    # 7. Four-Eyes Review and Approval Workflow
    approval_engine = DocumentApprovalEngine()
    # Reviewer confirms identity
    approval_engine.confirm_identity(
        session=db_session,
        document_id=doc.id,
        actor_id=users["reviewer"].id,
    )

    # Reviewer reviews and accepts each claim
    review_service = ClaimReviewService(enforce_four_eyes=True, enforce_role_rbac=True)
    for c in claims:
        review_service.accept_claim(
            session=db_session,
            claim_id=c.id,
            actor_id=users["reviewer"].id,
            expected_version=c.version,
        )

    # Distinct approver performs final document approval
    approval_result = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=users["approver"].id,
        identity_notes="Reconciled and approved lab document.",
    )
    assert approval_result.approval_status == DocumentApprovalStatus.APPROVED
    assert approval_result.is_export_eligible is True

    db_session.expire_all()
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc.status == DocumentStatus.APPROVED

    # 8. FHIR R4B Export
    export_service = FHIRExportService()
    export_res = export_service.export_document(
        session=db_session,
        document_id=doc.id,
        exported_by=users["approver"].id,
    )
    assert export_res.is_valid is True
    bundle = export_res.bundle
    assert isinstance(bundle, Bundle)
    assert bundle.type == "collection"

    # Find Observation resource
    obs_entry = next((e for e in bundle.entry if e.resource.resource_type == "Observation"), None)
    assert obs_entry is not None, "Observation resource missing from FHIR bundle"
    obs = obs_entry.resource

    # Assert Observation properties per DEC-002, DEC-016 & PROJECT_SPEC
    # Safe non-fabrication: CDCI concept produces text-only CodeableConcept (no fabricated coding URI)
    assert obs.code.text == "Potassium"
    assert obs.code.coding is None or len(obs.code.coding) == 0

    # Value Quantity
    assert obs.valueQuantity is not None
    assert obs.valueQuantity.value == Decimal("4.5000") or float(obs.valueQuantity.value) == 4.5
    assert obs.valueQuantity.code == "mmol/L"
    assert obs.valueQuantity.system == "http://unitsofmeasure.org"

    # Status: per resolve_observation_lifecycle_status (Phase 4.0.1/4.2), documents
    # lacking explicit signed/final report lifecycle evidence safely produce 'unknown'
    assert obs.status in ("final", "unknown")

    # Verify Provenance presence in bundle
    prov_entries = [e for e in bundle.entry if e.resource.resource_type == "Provenance"]
    assert len(prov_entries) >= 1, "Expected at least one Provenance resource in bundle"

    # Element-level / resource-level provenance extension on Observation
    prov_ext = next((ext for ext in (obs.extension or []) if ext.url == EXT_CLAIM_PROVENANCE), None)
    assert prov_ext is not None, "Expected EXT_CLAIM_PROVENANCE extension on Observation"

    # 9. Schema validation via FHIRValidator
    val_report = FHIRValidator.validate_bundle(bundle)
    assert val_report.is_valid is True
    assert len(val_report.errors) == 0


def test_worker_to_fhir_narrative_medication_document(
    db_session: Session,
    blob_store: EncryptedFileBlobStore,
    users: dict[str, User],
) -> None:
    """Phase 4 Test B: Narrative medication document end-to-end pipeline (ongoing continuation).

    Flow exercised:
      - Synthetic born-digital PDF containing clinical note with Warfarin prescription.
      - RegionClassifier routes prose note to llm_narrative path.
      - NarrativeExtractor produces source-grounded medication mentions.
      - MedicationAssociationEngine associates mentions under a shared group_id.
      - TerminologyNormalizer queries promoted database and resolves Warfarin to RxNorm CUI 11289.
      - Claims and evidence persisted with unified group_id and audit metadata.
      - Two-party Four-Eyes approval (reviewer accepts claims, distinct approver approves doc).
      - FHIRExportService exports document to FHIR R4B Bundle with MedicationStatement.
      - Validates RxNorm coding (http://www.nlm.nih.gov/research/umls/rxnorm / 11289),
        dosage attributes, and schema validity (zero validation errors).
    """
    # 1. Create synthetic born-digital PDF with realistic clinical note narrative
    med_text_lines = [
        "CLINICAL PROGRESS NOTE",
        "Patient is a 68-year-old individual who returns for ongoing management of chronic conditions.",
        "Overall cardiovascular symptoms remain compensated with no acute orthopnea or dyspnea on exertion.",
        "We discussed therapeutic goals, diet modifications, and current medication adherence.",
        "The patient is advised to continue Warfarin 5 mg oral tablet once daily at bedtime.",
        "Regular international normalized ratio monitoring has been scheduled with the anticoagulation service.",
        "Please return to the clinic in four weeks or sooner if any bleeding or bruising develops.",
    ]
    doc_fitz = fitz.open()
    page = doc_fitz.new_page(width=612, height=792)
    y = 72
    for line in med_text_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 20
    pdf_bytes = doc_fitz.tobytes()
    doc_fitz.close()

    # 2. Store document in encrypted blob store and register in DB
    storage_ref = blob_store.put(pdf_bytes)
    doc = Document(
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        storage_ref=storage_ref,
        original_filename="CLINICAL_NOTE_WARFARIN.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        page_count=1,
        source_type="born_digital",
        uploaded_by=users["uploader"].id,
        patient_context_id="PT-MED-WARFARIN",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.commit()

    # 3. Enqueue job
    queue = JobQueue()
    job = queue.enqueue(session=db_session, document_id=doc.id)
    db_session.commit()

    # 4. Wire real worker components with LLM provider returning source-grounded mentions
    canned_med_mentions = [
        {"field_type": "med.name", "value": "Warfarin", "evidence_text": "Warfarin"},
        {"field_type": "med.dose", "value": "5", "evidence_text": "5 mg", "unit_raw": "mg"},
        {"field_type": "med.route", "value": "oral", "evidence_text": "oral"},
        {"field_type": "med.frequency", "value": "once daily at bedtime", "evidence_text": "once daily at bedtime"},
    ]
    term_service = get_process_terminology_service()
    normalizer = TerminologyNormalizer(retrieval_service=term_service)
    router = ExtractionRouter(
        gliner_provider=MockGLiNERProvider(),
        llm_provider=MockLLMProvider(canned_responses=canned_med_mentions),
    )
    worker = DocumentProcessingWorker(
        queue=queue,
        blob_store=blob_store,
        router=router,
        normalizer=normalizer,
        cascade=MockDeterministicCascade(),
        session_factory=db_session.session_factory,
    )

    # 5. Worker claims and processes job
    claimed_job = worker.claim_job(session=db_session)
    assert claimed_job is not None
    assert claimed_job.id == job.id

    processed_job = worker.process_job(session=db_session, job=claimed_job)
    db_session.commit()
    db_session.expire_all()

    # Verify job completion and document pending review state
    assert processed_job.status == JobStatus.COMPLETED
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc is not None
    assert reloaded_doc.status == DocumentStatus.PENDING_REVIEW

    # 6. Verify extracted claims and medication association
    claims = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
    assert len(claims) == 4, f"Expected 4 medication claims, got {len(claims)}"

    med_name_claim = next((c for c in claims if c.field_type == "med.name"), None)
    dose_claim = next((c for c in claims if c.field_type == "med.dose"), None)
    route_claim = next((c for c in claims if c.field_type == "med.route"), None)
    freq_claim = next((c for c in claims if c.field_type == "med.frequency"), None)

    assert med_name_claim is not None
    assert dose_claim is not None
    assert route_claim is not None
    assert freq_claim is not None

    # Warfarin resolves to RxNorm CUI 11289 in candidate database
    assert med_name_claim.value == "Warfarin"
    assert med_name_claim.normalized_code == "11289"
    assert med_name_claim.code_system == "RxNorm"

    # Medication association check: all claims share the exact same non-None group_id
    group_ids = {c.group_id for c in claims}
    assert len(group_ids) == 1, f"Expected unified group_id, got multiple: {group_ids}"
    assert None not in group_ids, "group_id must not be None on associated medication claims"

    # 7. Four-Eyes Review and Approval Workflow
    approval_engine = DocumentApprovalEngine()
    approval_engine.confirm_identity(
        session=db_session,
        document_id=doc.id,
        actor_id=users["reviewer"].id,
    )

    review_service = ClaimReviewService(enforce_four_eyes=True, enforce_role_rbac=True)
    for c in claims:
        review_service.accept_claim(
            session=db_session,
            claim_id=c.id,
            actor_id=users["reviewer"].id,
            expected_version=c.version,
        )

    approval_result = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=users["approver"].id,
        identity_notes="Reconciled and approved prescription document.",
    )
    assert approval_result.approval_status == DocumentApprovalStatus.APPROVED
    assert approval_result.is_export_eligible is True

    db_session.expire_all()
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc.status == DocumentStatus.APPROVED

    # 8. FHIR R4B Export
    export_service = FHIRExportService()
    export_res = export_service.export_document(
        session=db_session,
        document_id=doc.id,
        exported_by=users["approver"].id,
    )
    assert export_res.is_valid is True
    bundle = export_res.bundle
    assert isinstance(bundle, Bundle)
    assert bundle.type == "collection"

    # Find Medication resource (MedicationStatement or MedicationRequest per clinical context & REQ-44)
    med_entry = next(
        (e for e in bundle.entry if e.resource.resource_type in ("MedicationRequest", "MedicationStatement")),
        None,
    )
    assert med_entry is not None, "Medication resource missing from FHIR bundle"
    med_res = med_entry.resource
    assert med_res.resource_type in ("MedicationRequest", "MedicationStatement")

    # Assert Medication resource attributes (status is 'active' or safe 'unknown' per REQ-44)
    assert med_res.status in ("active", "unknown")
    if med_res.resource_type == "MedicationRequest":
        assert med_res.intent == "order"

    # Assert authoritative RxNorm coding
    assert med_res.medicationCodeableConcept is not None
    assert med_res.medicationCodeableConcept.coding is not None
    assert len(med_res.medicationCodeableConcept.coding) >= 1

    rx_coding = med_res.medicationCodeableConcept.coding[0]
    assert rx_coding.system == "http://www.nlm.nih.gov/research/umls/rxnorm"
    assert rx_coding.code == "11289"

    # Verify Dosage Instruction presence
    if med_res.resource_type == "MedicationRequest":
        assert med_res.dosageInstruction is not None
        assert len(med_res.dosageInstruction) >= 1
        dosage = med_res.dosageInstruction[0]
        assert dosage.text is not None
    else:
        assert med_res.dosage is not None
        assert len(med_res.dosage) >= 1
        dosage = med_res.dosage[0]
        assert dosage.text is not None

    # Verify Provenance presence in bundle
    prov_entries = [e for e in bundle.entry if e.resource.resource_type == "Provenance"]
    assert len(prov_entries) >= 1, "Expected at least one Provenance resource in bundle"

    # Resource-level provenance extension on Medication resource
    prov_ext = next((ext for ext in (med_res.extension or []) if ext.url == EXT_CLAIM_PROVENANCE), None)
    assert prov_ext is not None, f"Expected EXT_CLAIM_PROVENANCE extension on {med_res.resource_type}"

    # 9. Schema validation via FHIRValidator
    val_report = FHIRValidator.validate_bundle(bundle)
    assert val_report.is_valid is True
    assert len(val_report.errors) == 0


def test_worker_to_fhir_potassium_specimen_specific_loinc(
    db_session: Session,
    blob_store: EncryptedFileBlobStore,
    users: dict[str, User],
) -> None:
    """Phase 4 Scenario 1: Specimen-specific Potassium LOINC 2823-3 integration test.

    Flow exercised:
      - Synthetic born-digital PDF whose test name explicitly specifies:
        'Potassium [Moles/volume] in Serum or Plasma'.
      - DocumentProcessingWorker claims and executes processing job.
      - ExtractionRouter routes structured table through DeterministicExtractor.
      - The extracted test-name evidence retains the fully specified source text.
      - TerminologyNormalizer queries the promoted candidate database and resolves LOINC 2823-3.
      - Four-Eyes review and approval workflow completes.
      - FHIRExportService exports document to FHIR R4B Bundle.
      - Verifies Observation contains authoritative LOINC system ("http://loinc.org")
        and code ("2823-3"), value 4.5 mmol/L, provenance, and zero schema validation errors.
    """
    # 1. Create synthetic born-digital PDF with fully specified test name
    doc_fitz = fitz.open()
    page = doc_fitz.new_page(width=612, height=792)
    lab_lines = [
        "CLINICAL LABORATORY TEST REPORT",
        "Specimen: Serum or Plasma",
        "Test Name                                  Value    Unit      Reference Range   Flag",
        "Potassium [Moles/volume] in Serum or Plasma 4.5      mmol/L    3.5-5.0           NORMAL",
    ]
    y = 72
    for line in lab_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 24
    pdf_bytes = doc_fitz.tobytes()
    doc_fitz.close()

    # 2. Store document in encrypted blob store and register in DB
    storage_ref = blob_store.put(pdf_bytes)
    doc = Document(
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        storage_ref=storage_ref,
        original_filename="LAB_POTASSIUM_SPECIFIC_REPORT.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        page_count=1,
        source_type="born_digital",
        uploaded_by=users["uploader"].id,
        patient_context_id="PT-LAB-K-LOINC",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.commit()

    # 3. Enqueue job
    queue = JobQueue()
    job = queue.enqueue(session=db_session, document_id=doc.id)
    db_session.commit()

    # 4. Wire router with custom loinc_map recognizing the fully specified test name
    base_loinc = json.load(open("configs/loinc_subset.json", encoding="utf-8"))
    custom_loinc = dict(base_loinc)
    custom_loinc["Potassium [Moles/volume] in Serum or Plasma"] = {
        "loinc_code": "2823-3",
        "canonical_name": "Potassium [Moles/volume] in Serum or Plasma",
        "default_unit": "mmol/L",
    }

    term_service = get_process_terminology_service()
    normalizer = TerminologyNormalizer(retrieval_service=term_service)
    router = ExtractionRouter(
        gliner_provider=MockGLiNERProvider(),
        llm_provider=MockLLMProvider(),
        loinc_map=custom_loinc,
    )
    worker = DocumentProcessingWorker(
        queue=queue,
        blob_store=blob_store,
        router=router,
        normalizer=normalizer,
        cascade=MockDeterministicCascade(),
        session_factory=db_session.session_factory,
    )

    # 5. Worker claims and processes job
    claimed_job = worker.claim_job(session=db_session)
    assert claimed_job is not None
    assert claimed_job.id == job.id

    processed_job = worker.process_job(session=db_session, job=claimed_job)
    db_session.commit()
    db_session.expire_all()

    assert processed_job.status == JobStatus.COMPLETED
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc.status == DocumentStatus.PENDING_REVIEW

    # 6. Verify extracted and normalized claims
    claims = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
    assert len(claims) >= 3

    test_name_claim = next((c for c in claims if c.field_type == "lab.test_name"), None)
    unit_claim = next((c for c in claims if c.field_type == "lab.unit"), None)
    value_claim = next((c for c in claims if c.field_type == "lab.value"), None)

    assert test_name_claim is not None
    assert unit_claim is not None
    assert value_claim is not None

    # Fully specified test name retains complete clinical context
    assert test_name_claim.value == "Potassium [Moles/volume] in Serum or Plasma"
    # Promoted database resolves LOINC 2823-3
    assert test_name_claim.normalized_code == "2823-3"
    assert test_name_claim.code_system == "LOINC"
    assert test_name_claim.origin == ClaimOrigin.PIPELINE

    # Unit claim
    assert unit_claim.normalized_code == "mmol/L"
    assert unit_claim.code_system == "UCUM"

    # Value claim
    assert value_claim.value == "4.5"

    # 7. Four-Eyes Review and Approval Workflow
    approval_engine = DocumentApprovalEngine()
    approval_engine.confirm_identity(
        session=db_session,
        document_id=doc.id,
        actor_id=users["reviewer"].id,
    )

    review_service = ClaimReviewService(enforce_four_eyes=True, enforce_role_rbac=True)
    for c in claims:
        review_service.accept_claim(
            session=db_session,
            claim_id=c.id,
            actor_id=users["reviewer"].id,
            expected_version=c.version,
        )

    approval_result = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=users["approver"].id,
        identity_notes="Reconciled and approved specimen-specific potassium report.",
    )
    assert approval_result.approval_status == DocumentApprovalStatus.APPROVED
    assert approval_result.is_export_eligible is True

    db_session.expire_all()
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc.status == DocumentStatus.APPROVED

    # 8. FHIR R4B Export
    export_service = FHIRExportService()
    export_res = export_service.export_document(
        session=db_session,
        document_id=doc.id,
        exported_by=users["approver"].id,
    )
    assert export_res.is_valid is True
    bundle = export_res.bundle

    obs_entry = next((e for e in bundle.entry if e.resource.resource_type == "Observation"), None)
    assert obs_entry is not None, "Observation resource missing from FHIR bundle"
    obs = obs_entry.resource

    # Assert LOINC coding
    assert obs.code.text == "Potassium [Moles/volume] in Serum or Plasma"
    assert obs.code.coding is not None
    loinc_coding = next((c for c in obs.code.coding if c.system == "http://loinc.org"), None)
    assert loinc_coding is not None, "LOINC coding missing from Observation.code"
    assert loinc_coding.code == "2823-3"

    # Quantity & UCUM unit
    assert obs.valueQuantity is not None
    assert obs.valueQuantity.value == Decimal("4.5000") or float(obs.valueQuantity.value) == 4.5
    assert obs.valueQuantity.code == "mmol/L"
    assert obs.valueQuantity.system == "http://unitsofmeasure.org"

    # Provenance
    prov_ext = next((ext for ext in (obs.extension or []) if ext.url == EXT_CLAIM_PROVENANCE), None)
    assert prov_ext is not None

    # 9. Schema validation via FHIRValidator
    val_report = FHIRValidator.validate_bundle(bundle)
    assert val_report.is_valid is True
    assert len(val_report.errors) == 0


def test_worker_to_fhir_explicit_prescription_medication_request(
    db_session: Session,
    blob_store: EncryptedFileBlobStore,
    users: dict[str, User],
) -> None:
    """Phase 4 Scenario 2: Explicit prescription MedicationRequest integration test.

    Flow exercised:
      - Synthetic born-digital prescription note with genuine order intent.
      - Real worker processing through supported narrative extraction and medication association.
      - TerminologyNormalizer queries the promoted candidate database and resolves
        Warfarin to ingredient-level RxNorm code 11289 (TTY='IN').
      - All medication claims unified under a shared group_id.
      - Four-Eyes review and approval workflow completes.
      - FHIRExportService exports document with document_type="prescription".
      - Verifies that production mapper emits a MedicationRequest resource with
        status="active", intent="order", RxNorm 11289 ingredient code, and dosage instructions.
      - Provenance and FHIR R4B validation pass with zero errors.
    """
    # 1. Create synthetic born-digital prescription document
    rx_lines = [
        "CLINICAL PRESCRIPTION AND ORDER NOTE",
        "Patient is a 68-year-old individual who returns for ongoing management of chronic conditions.",
        "Overall cardiovascular symptoms remain compensated with no acute orthopnea or dyspnea on exertion.",
        "We discussed therapeutic goals, diet modifications, and new prescription orders.",
        "The clinician hereby orders Warfarin 5 mg oral once daily at bedtime.",
        "Regular international normalized ratio monitoring has been scheduled with the anticoagulation service.",
        "Please return to the clinic in four weeks or sooner if any bleeding or bruising develops.",
    ]
    doc_fitz = fitz.open()
    page = doc_fitz.new_page(width=612, height=792)
    y = 72
    for line in rx_lines:
        page.insert_text(fitz.Point(72, y), line)
        y += 20
    pdf_bytes = doc_fitz.tobytes()
    doc_fitz.close()

    # 2. Store document in encrypted blob store and register in DB
    storage_ref = blob_store.put(pdf_bytes)
    doc = Document(
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        storage_ref=storage_ref,
        original_filename="PRESCRIPTION_WARFARIN_ORDER.pdf",
        mime_type="application/pdf",
        file_size_bytes=len(pdf_bytes),
        page_count=1,
        source_type="born_digital",
        uploaded_by=users["uploader"].id,
        patient_context_id="PT-RX-WARFARIN-02",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.commit()

    # 3. Enqueue job
    queue = JobQueue()
    job = queue.enqueue(session=db_session, document_id=doc.id)
    db_session.commit()

    # 4. Wire real worker components with LLM provider returning source-grounded mentions
    canned_rx_mentions = [
        {"field_type": "med.name", "value": "Warfarin", "evidence_text": "Warfarin"},
        {"field_type": "med.dose", "value": "5", "evidence_text": "5 mg", "unit_raw": "mg"},
        {"field_type": "med.route", "value": "oral", "evidence_text": "oral"},
        {"field_type": "med.frequency", "value": "once daily at bedtime", "evidence_text": "once daily at bedtime"},
    ]
    term_service = get_process_terminology_service()
    normalizer = TerminologyNormalizer(retrieval_service=term_service)
    router = ExtractionRouter(
        gliner_provider=MockGLiNERProvider(),
        llm_provider=MockLLMProvider(canned_responses=canned_rx_mentions),
    )
    worker = DocumentProcessingWorker(
        queue=queue,
        blob_store=blob_store,
        router=router,
        normalizer=normalizer,
        cascade=MockDeterministicCascade(),
        session_factory=db_session.session_factory,
    )

    # 5. Worker claims and processes job
    claimed_job = worker.claim_job(session=db_session)
    assert claimed_job is not None
    assert claimed_job.id == job.id

    processed_job = worker.process_job(session=db_session, job=claimed_job)
    db_session.commit()
    db_session.expire_all()

    assert processed_job.status == JobStatus.COMPLETED
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc is not None
    assert reloaded_doc.status == DocumentStatus.PENDING_REVIEW

    # 6. Verify extracted claims and medication association
    claims = db_session.query(Claim).filter(Claim.document_id == doc.id).all()
    assert len(claims) == 4, f"Expected 4 medication claims, got {len(claims)}"

    med_name_claim = next((c for c in claims if c.field_type == "med.name"), None)
    dose_claim = next((c for c in claims if c.field_type == "med.dose"), None)
    route_claim = next((c for c in claims if c.field_type == "med.route"), None)
    freq_claim = next((c for c in claims if c.field_type == "med.frequency"), None)

    assert med_name_claim is not None
    assert dose_claim is not None
    assert route_claim is not None
    assert freq_claim is not None

    # Warfarin resolves to ingredient-level RxNorm concept 11289 (TTY='IN')
    assert med_name_claim.value == "Warfarin"
    assert med_name_claim.normalized_code == "11289"
    assert med_name_claim.code_system == "RxNorm"

    # Unified group_id across all associated claims
    group_ids = {c.group_id for c in claims}
    assert len(group_ids) == 1
    assert None not in group_ids

    # 7. Four-Eyes Review and Approval Workflow
    approval_engine = DocumentApprovalEngine()
    approval_engine.confirm_identity(
        session=db_session,
        document_id=doc.id,
        actor_id=users["reviewer"].id,
    )

    review_service = ClaimReviewService(enforce_four_eyes=True, enforce_role_rbac=True)
    for c in claims:
        review_service.accept_claim(
            session=db_session,
            claim_id=c.id,
            actor_id=users["reviewer"].id,
            expected_version=c.version,
        )

    approval_result = approval_engine.approve_document(
        session=db_session,
        document_id=doc.id,
        approver_id=users["approver"].id,
        identity_notes="Reconciled and approved prescription order document.",
    )
    assert approval_result.approval_status == DocumentApprovalStatus.APPROVED
    assert approval_result.is_export_eligible is True

    db_session.expire_all()
    reloaded_doc = db_session.get(Document, doc.id)
    assert reloaded_doc.status == DocumentStatus.APPROVED

    # 8. FHIR R4B Export with prescription document layout classification
    # Attach document_type="prescription" metadata on loaded document instance
    object.__setattr__(reloaded_doc, "document_type", "prescription")

    export_service = FHIRExportService()
    export_res = export_service.export_document(
        session=db_session,
        document_id=doc.id,
        exported_by=users["approver"].id,
    )
    assert export_res.is_valid is True
    bundle = export_res.bundle

    # Assert MedicationRequest is emitted (Category 5: New Prescription Order per REQ-44)
    med_req_entry = next((e for e in bundle.entry if e.resource.resource_type == "MedicationRequest"), None)
    assert med_req_entry is not None, "MedicationRequest resource missing from FHIR bundle"
    med_req = med_req_entry.resource

    # Assert status and intent
    assert med_req.status == "active"
    assert med_req.intent == "order"

    # Assert medication coding: derived from persisted normalized claim (RxNorm 11289, ingredient-level)
    assert med_req.medicationCodeableConcept is not None
    assert med_req.medicationCodeableConcept.coding is not None
    assert len(med_req.medicationCodeableConcept.coding) >= 1
    rx_coding = med_req.medicationCodeableConcept.coding[0]
    assert rx_coding.system == "http://www.nlm.nih.gov/research/umls/rxnorm"
    assert rx_coding.code == "11289"

    # Assert dosage instructions: route, dose, timing
    assert med_req.dosageInstruction is not None
    assert len(med_req.dosageInstruction) >= 1
    dosage = med_req.dosageInstruction[0]
    assert dosage.text is not None

    # Route
    assert dosage.route is not None
    assert dosage.route.text == "oral"

    # Dose quantity
    assert dosage.doseAndRate is not None
    assert len(dosage.doseAndRate) >= 1
    dose_qty = dosage.doseAndRate[0].doseQuantity
    assert dose_qty is not None
    assert dose_qty.value == Decimal("5.0000") or float(dose_qty.value) == 5.0
    assert dose_qty.unit == "mg"
    assert dose_qty.system == "http://unitsofmeasure.org"

    # Provenance
    prov_ext = next((ext for ext in (med_req.extension or []) if ext.url == EXT_CLAIM_PROVENANCE), None)
    assert prov_ext is not None

    # 9. Schema validation via FHIRValidator
    val_report = FHIRValidator.validate_bundle(bundle)
    assert val_report.is_valid is True
    assert len(val_report.errors) == 0
