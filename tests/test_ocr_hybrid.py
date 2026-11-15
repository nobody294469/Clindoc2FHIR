"""Tests for Page-Level Hybrid PDF Modality Detection and Merging."""
from pathlib import Path
import tempfile
import pymupdf
import pytest
from PIL import Image, ImageDraw

from clindoc.ocr.hybrid import (
    PageModality,
    detect_page_modality,
    process_hybrid_pdf,
)
from clindoc.ocr.ingest import ingest_document


@pytest.fixture
def sample_hybrid_pdf():
    """Create a 2-page hybrid PDF: Page 1 is born-digital, Page 2 is a scanned raster image."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        pdf_path = Path(f.name)

    doc = pymupdf.open()

    # Page 1: Born-digital vector text
    p1 = doc.new_page(width=612, height=792)
    p1.insert_text((54, 100), "Clinical Discharge Summary\nPatient Name: John Doe\nMedication: Metformin 500 mg PO BID", fontsize=12)

    # Page 2: Scanned raster image
    p2 = doc.new_page(width=612, height=792)
    img = Image.new("RGB", (612 * 2, 792 * 2), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((100, 200), "LABORATORY RESULTS REPORT - HEMOGLOBIN A1C 7.2%", fill="black")
    
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_f:
        img_temp_p = Path(img_f.name)
    img.save(str(img_temp_p))

    # Insert image into page 2
    p2.insert_image(pymupdf.Rect(0, 0, 612, 792), filename=str(img_temp_p))

    doc.save(str(pdf_path))
    doc.close()
    if img_temp_p.exists():
        img_temp_p.unlink()

    yield pdf_path

    if pdf_path.exists():
        pdf_path.unlink()


def test_page_modality_detection(sample_hybrid_pdf):
    doc = pymupdf.open(str(sample_hybrid_pdf))
    assert len(doc) == 2

    modality_p1 = detect_page_modality(doc[0])
    modality_p2 = detect_page_modality(doc[1])

    assert modality_p1 == PageModality.BORN_DIGITAL
    assert modality_p2 == PageModality.SCANNED_IMAGE


def test_process_hybrid_pdf(sample_hybrid_pdf):
    record = process_hybrid_pdf(sample_hybrid_pdf, doc_id="TEST_HYBRID_01")
    assert record.num_pages == 2
    assert record.engine == "hybrid"

    # Page 1 tokens should have confidence 1.0 (born digital)
    p1_words = [w for w in record.words if w.page == 1]
    assert len(p1_words) > 0
    assert all(w.confidence == 1.0 for w in p1_words)
    p1_text = " ".join(w.text for w in p1_words)
    assert "Metformin" in p1_text

    # Page 2 tokens should be extracted via OCR
    p2_words = [w for w in record.words if w.page == 2]
    assert len(p2_words) > 0
    p2_text = " ".join(w.text for w in p2_words)
    assert "LABORATORY" in p2_text or "HEMOGLOBIN" in p2_text


def test_ingest_document_hybrid(sample_hybrid_pdf):
    doc_text = ingest_document(sample_hybrid_pdf, document_id="TEST_HYBRID_DOC")
    assert doc_text.num_pages == 2
    assert doc_text.pages[0].page_number == 1
    assert doc_text.pages[1].page_number == 2

    full_text = doc_text.get_full_text()
    assert "Metformin" in full_text
    assert "LABORATORY" in full_text or "HEMOGLOBIN" in full_text


def test_configurable_modality_thresholds(tmp_path):
    """Verify that configured thresholds actually alter page modality classification."""
    from clindoc.ocr.hybrid import ModalityConfig

    pdf_p = tmp_path / "threshold_test.pdf"
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    # Insert exactly 30 characters of text
    p.insert_text((54, 100), "Short clinical note line text.")
    doc.save(str(pdf_p))
    doc.close()

    read_doc = pymupdf.open(str(pdf_p))
    page = read_doc[0]

    # Default min_char_count is 15 -> 30 chars qualifies as BORN_DIGITAL
    mod_default = detect_page_modality(page)
    assert mod_default == PageModality.BORN_DIGITAL

    # Custom higher threshold of 50 -> 30 chars is insufficient, classifies as BLANK or SCANNED
    cfg_high = ModalityConfig(min_char_count=50)
    mod_custom = detect_page_modality(page, config=cfg_high)
    assert mod_custom != PageModality.BORN_DIGITAL


