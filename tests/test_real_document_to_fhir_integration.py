"""
M-1: First Real Document-to-FHIR Integration Test

Proves that one synthetic, non-PHI laboratory PDF passes through the actual
ClinicalDoc processing workflow and produces a valid, persisted FHIR R4B export.

Required 10-Stage Execution Path:
1. PDF ingestion and document persistence (DocumentIngestionService)
2. Encrypted blob storage (EncryptedFileBlobStore)
3. Real Tesseract OCR (not mocked)
4. Real region classification and deterministic extraction (ExtractionRouter)
5. Real terminology normalization (TerminologyNormalizer & active candidate DB)
6. Real Stage A verification (DeterministicVerifier)
7. Real Stage B ModernBERT inference (LearnedVerifier with actual checkpoint)
8. Real database persistence and provenance tracking (DocumentProcessingWorker)
9. Real human review and Four-Eyes approval (DocumentApprovalEngine)
10. Real FHIR R4B export and validation (FHIRExportService)

Stage C uses its authorized MockSupportedJudgeClient (no live vLLM required).
"""

import io
import os
import shutil
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF
import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from clindoc.db.enums import DocumentApprovalStatus, DocumentStatus
from clindoc.db.models import (
    Base,
    Claim,
    ClaimReviewState,
    Document,
    Role,
    User,
    UserRole,
)
from clindoc.extract.router import ExtractionRouter
from clindoc.ingest.service import DocumentIngestionService
from clindoc.jobs.queue import JobQueue
from clindoc.jobs.worker import DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.ocr.ingest import ingest_document as real_ocr_ingest
from clindoc.ocr.tesseract_runner import find_tesseract_binary
from clindoc.review.approval import DocumentApprovalEngine, FourEyesViolationError
from clindoc.storage import EncryptedFileBlobStore
from clindoc.services.fhir_export import FHIRExportEligibilityError, FHIRExportService
from clindoc.terminology.service import TerminologyRetrievalService
from clindoc.verify.cascade import VerificationCascade
from clindoc.verify.stage_a import DeterministicVerifier
from clindoc.verify.stage_b import LearnedVerifier
from clindoc.verify.stage_c import DefaultMockLLMJudge, StageCJudge

CHECKPOINT_PATH = Path("data/m2_results_v3/checkpoints/modernbert_text_only_v3_fold0_seed42.pt")
TERMINOLOGY_DB_PATH = Path("data/terminology/terminology_v20261112.candidate.db")

TESSERACT_AVAILABLE = find_tesseract_binary() is not None
CHECKPOINT_AVAILABLE = CHECKPOINT_PATH.exists()
TERMINOLOGY_AVAILABLE = TERMINOLOGY_DB_PATH.exists()


def create_synthetic_lab_pdf_bytes() -> bytes:
    """Generate a clean, high-DPI synthetic laboratory report PDF as an image scan."""
    width, height = 1700, 2200
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("arial.ttf", 44)
        font_header = ImageFont.truetype("arial.ttf", 30)
        font_body = ImageFont.truetype("arial.ttf", 26)
    except IOError:
        font_title = font_header = font_body = ImageFont.load_default()

    # Title & Clinical Header
    draw.text((100, 100), "METROPOLITAN CLINICAL LABORATORY REPORT", fill="black", font=font_title)
    draw.line([(100, 160), (1600, 160)], fill="black", width=3)

    draw.text((100, 190), "Patient Name: Jane Doe", fill="black", font=font_body)
    draw.text((100, 230), "Patient ID: 12345", fill="black", font=font_body)
    draw.text((100, 270), "DOB: 1985-05-15", fill="black", font=font_body)
    draw.text((900, 190), "Date Collected: 2026-11-12 08:30", fill="black", font=font_body)
    draw.text((900, 230), "Ordering Physician: Dr. Marcus Vance, MD", fill="black", font=font_body)
    draw.text((900, 270), "Report Status: Final", fill="black", font=font_body)
    draw.line([(100, 320), (1600, 320)], fill="gray", width=2)

    # Section Header
    draw.text((100, 360), "ROUTINE CHEMISTRY PANEL", fill="black", font=font_header)
    draw.line([(100, 410), (1600, 410)], fill="black", width=2)

    # Table Header
    y = 440
    draw.text((120, y), "Test Name", fill="black", font=font_header)
    draw.text((500, y), "Result", fill="black", font=font_header)
    draw.text((750, y), "Units", fill="black", font=font_header)
    draw.text((1000, y), "Reference Interval", fill="black", font=font_header)
    draw.text((1350, y), "Flag", fill="black", font=font_header)
    draw.line([(100, y + 50), (1600, y + 50)], fill="black", width=1)

    # Table Row (Fasting Glucose)
    y = 520
    draw.text((120, y), "Fasting Blood Glucose", fill="black", font=font_body)
    draw.text((520, y), "95", fill="black", font=font_body)
    draw.text((750, y), "mg/dL", fill="black", font=font_body)
    draw.text((1020, y), "70 - 99", fill="black", font=font_body)
    draw.text((1360, y), "Normal", fill="black", font=font_body)

    # Convert PIL Image to single-page PDF bytes via PyMuPDF
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    img_bytes = img_byte_arr.getvalue()

    doc = fitz.open()
    rect = fitz.Rect(0, 0, 612, 792)  # Standard Letter page
    page = doc.new_page(width=rect.width, height=rect.height)
    page.insert_image(rect, stream=img_bytes)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


@pytest.mark.requires_real_model
@pytest.mark.skipif(not TESSERACT_AVAILABLE, reason="Tesseract OCR executable not available on host")
@pytest.mark.skipif(not CHECKPOINT_AVAILABLE, reason="ModernBERT canonical checkpoint not found")
@pytest.mark.skipif(not TERMINOLOGY_AVAILABLE, reason="Candidate terminology DB not found")
def test_real_document_to_fhir_end_to_end_integration():
    """Execute complete 10-stage document ingestion to FHIR R4B export pipeline."""
    # Setup temporary directory for encrypted blob store and master keys
    temp_dir = tempfile.mkdtemp(prefix="clindoc_m1_test_")
    master_key = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    try:
        # Isolated SQLite database in memory
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        # Seed roles and users
        role_operator = Role(id=str(uuid.uuid4()), name="document_operator", description="Document Ingestion Operator")
        role_reviewer = Role(id=str(uuid.uuid4()), name="clinical_reviewer", description="Clinical Reviewer")
        role_approver = Role(id=str(uuid.uuid4()), name="approver", description="Four-Eyes Clinical Approver")
        session.add_all([role_operator, role_reviewer, role_approver])
        session.flush()

        user_uploader = User(
            id=str(uuid.uuid4()),
            username="nurse_sarah",
            email="sarah@hospital.org",
            password_hash="mock_hash_1",
            full_name="Sarah Jenkins, RN",
            is_active=True,
        )
        user_approver = User(
            id=str(uuid.uuid4()),
            username="dr_vance",
            email="vance@hospital.org",
            password_hash="mock_hash_2",
            full_name="Marcus Vance, MD",
            is_active=True,
        )
        session.add_all([user_uploader, user_approver])
        session.flush()

        session.add(UserRole(user_id=user_uploader.id, role_id=role_operator.id))
        session.add(UserRole(user_id=user_approver.id, role_id=role_reviewer.id))
        session.add(UserRole(user_id=user_approver.id, role_id=role_approver.id))
        session.commit()

        # Initialize Encrypted Blob Storage
        blob_store = EncryptedFileBlobStore(storage_dir=temp_dir, master_key_hex=master_key)

        # ---------------------------------------------------------------------
        # Stage 1 & 2: Real Document Ingestion & Encrypted Blob Storage
        # ---------------------------------------------------------------------
        pdf_content = create_synthetic_lab_pdf_bytes()
        ingestion_service = DocumentIngestionService(session=session, blob_store=blob_store)

        doc = ingestion_service.ingest_document(
            file_content=pdf_content,
            filename="synthetic_lab_report_jane_doe.pdf",
            mime_type="application/pdf",
            uploaded_by=user_uploader.id,
            patient_id="12345",
        )
        # Mark as scanned_image so that OCR pipeline triggers real Tesseract
        doc.source_type = "scanned_image"
        session.commit()

        assert doc.id is not None
        assert doc.status == DocumentStatus.RECEIVED
        assert blob_store.get(doc.storage_reference) == pdf_content

        # ---------------------------------------------------------------------
        # Stage 3 to 8: Real OCR, Extraction, Normalization, Cascade & Persistence
        # ---------------------------------------------------------------------
        queue = JobQueue(session)
        job = queue.enqueue(document_id=doc.id)
        session.commit()

        # Initialize Real Verification Cascade
        stage_a = DeterministicVerifier()
        stage_b = LearnedVerifier(
            checkpoint_path=str(CHECKPOINT_PATH),
            device="cuda" if os.environ.get("USE_CUDA", "1") == "1" else "cpu",
        )
        stage_c = StageCJudge(client=DefaultMockLLMJudge(), allow_mock_client=True)
        cascade = VerificationCascade(stage_a=stage_a, stage_b=stage_b, stage_c=stage_c)

        # Initialize Real Normalizer with candidate database
        retrieval_service = TerminologyRetrievalService(db_path_or_dir="data/terminology")
        normalizer = TerminologyNormalizer(retrieval_service=retrieval_service)

        # Worker wiring
        worker = DocumentProcessingWorker(
            session_factory=SessionLocal,
            blob_store=blob_store,
            queue=queue,
            ingest_fn=real_ocr_ingest,
            router=ExtractionRouter(),
            normalizer=normalizer,
            cascade=cascade,
        )

        # Worker claims and processes job
        claimed_job = worker.claim_job(session)
        assert claimed_job is not None
        assert claimed_job.id == job.id

        worker.process_job(claimed_job.id)

        # Verify Document status and extraction artifacts in DB
        session.expire_all()
        processed_doc = session.query(Document).filter(Document.id == doc.id).first()
        assert processed_doc.status == DocumentStatus.PENDING_REVIEW

        persisted_claims = session.query(Claim).filter(Claim.document_id == doc.id).all()
        assert len(persisted_claims) >= 3, f"Expected at least 3 claims, found {len(persisted_claims)}"

        claim_types = {c.type for c in persisted_claims}
        assert "lab.test_name" in claim_types or "lab.value" in claim_types

        # Verify normalization occurred
        test_name_claims = [c for c in persisted_claims if c.type == "lab.test_name"]
        assert len(test_name_claims) >= 1
        glucose_claim = test_name_claims[0]
        assert "glucose" in glucose_claim.raw_value.lower()
        assert glucose_claim.normalized_code is not None

        # Verify Stage A & B verification ran on claims
        assert any(c.verification_status is not None for c in persisted_claims)

        # ---------------------------------------------------------------------
        # Stage 9: Human Review & Four-Eyes Governance
        # ---------------------------------------------------------------------
        fhir_service = FHIRExportService(
            db_session=session,
            blob_store=blob_store,
            export_dir=os.path.join(temp_dir, "fhir_exports"),
        )

        # 9a. Pre-approval export must be blocked
        with pytest.raises(FHIRExportEligibilityError):
            fhir_service.export_document(doc.id)

        # 9b. Mark all claims as accepted during review
        for c in persisted_claims:
            c.review_state = ClaimReviewState.ACCEPTED
        session.commit()

        approval_engine = DocumentApprovalEngine()

        # 9c. Confirm patient identity
        approval_engine.confirm_identity(
            session=session,
            document_id=doc.id,
            actor_id=user_approver.id,
        )

        # 9d. Four-Eyes violation: uploader cannot approve their own document
        with pytest.raises(FourEyesViolationError):
            approval_engine.approve_document(
                session=session,
                document_id=doc.id,
                approver_id=user_uploader.id,
            )

        # 9e. Valid Four-Eyes approval by distinct clinical approver
        approval_result = approval_engine.approve_document(
            session=session,
            document_id=doc.id,
            approver_id=user_approver.id,
        )
        assert approval_result.approval_status == DocumentApprovalStatus.APPROVED

        session.expire_all()
        approved_doc = session.query(Document).filter(Document.id == doc.id).first()
        assert approved_doc.status == DocumentStatus.APPROVED
        assert approved_doc.is_export_eligible is True

        # ---------------------------------------------------------------------
        # Stage 10: FHIR R4B Export & Validation
        # ---------------------------------------------------------------------
        export_result = fhir_service.export_document(doc.id)
        assert export_result.is_valid is True
        bundle = export_result.bundle
        assert bundle.resource_type == "Bundle"
        assert bundle.type == "collection"

        # Assert presence of Observation resource
        entries = bundle.entry or []
        observations = [e.resource for e in entries if e.resource.resource_type == "Observation"]
        assert len(observations) >= 1, "Export bundle must contain at least one Observation"

        glucose_obs = observations[0]
        assert glucose_obs.status == "final"

        # Check code / text
        assert any(
            coding.display and "glucose" in coding.display.lower()
            for coding in glucose_obs.code.coding
        ) or (glucose_obs.code.text and "glucose" in glucose_obs.code.text.lower())

        # Check numeric quantity
        assert glucose_obs.valueQuantity is not None
        assert glucose_obs.valueQuantity.value == Decimal("95")
        assert glucose_obs.valueQuantity.code == "mg/dL"
        assert glucose_obs.valueQuantity.system == "http://unitsofmeasure.org"

        # Check interpretation
        if glucose_obs.interpretation:
            interp_coding = glucose_obs.interpretation[0].coding[0]
            assert interp_coding.code == "N"

        # Check element-level provenance extension
        assert glucose_obs.extension is not None
        prov_extensions = [
            ext for ext in glucose_obs.extension
            if "claim-provenance" in ext.url or "source-document" in ext.url
        ]
        assert len(prov_extensions) >= 1

        # Check final document lifecycle status in DB
        session.expire_all()
        final_doc = session.query(Document).filter(Document.id == doc.id).first()
        assert final_doc.status == DocumentStatus.EXPORTED

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
