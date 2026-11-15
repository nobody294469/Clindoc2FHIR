"""OCR and document text extraction module."""

from clindoc.ocr.geometry import BBox, BBoxDict, enclose_bboxes
from clindoc.ocr.document_text import (
    DocumentText, DocumentPage, Token, DegradationMetadata,
)
from clindoc.ocr.ingest import (
    ingest_document, batch_ingest, ingest_from_record,
    IngestionError, UnreadableDocumentError, UnsupportedFileTypeError,
    EmptyDocumentError, OCREngineError,
)
from clindoc.ocr.schema import DocumentOCRRecord, LineBox, WordBox
from clindoc.ocr.born_digital import extract_born_digital_pdf
from clindoc.ocr.layout import sort_page_lines_reading_order
from clindoc.ocr.table_layout import detect_tabular_layout, reconstruct_tabular_page_text
from clindoc.ocr.pipeline import process_document, batch_process_dataset
from clindoc.ocr.preprocessing import (
    AffineMatrix,
    OrientationResult,
    detect_orientation_osd,
    estimate_skew_angle,
    preprocess_page_image,
)
from clindoc.ocr.tesseract_runner import (
    TesseractConfig,
    find_tesseract_binary,
    check_tesseract_readiness,
    run_tesseract_tsv,
    parse_tesseract_tsv,
)
from clindoc.ocr.hybrid import (
    PageModality,
    ModalityConfig,
    detect_page_modality,
    process_hybrid_pdf,
    process_image_file,
)

__all__ = [
    # Canonical DocumentText contract & geometry
    "DocumentText",
    "DocumentPage",
    "Token",
    "DegradationMetadata",
    "BBox",
    "BBoxDict",
    "enclose_bboxes",
    # Ingestion API & exceptions
    "ingest_document",
    "batch_ingest",
    "ingest_from_record",
    "IngestionError",
    "UnreadableDocumentError",
    "UnsupportedFileTypeError",
    "EmptyDocumentError",
    "OCREngineError",
    # Underlying OCR models & extraction
    "DocumentOCRRecord",
    "LineBox",
    "WordBox",
    "extract_born_digital_pdf",
    "sort_page_lines_reading_order",
    "detect_tabular_layout",
    "reconstruct_tabular_page_text",
    "process_document",
    "batch_process_dataset",
    # M-OCR Scanned & Preprocessing Pipeline
    "AffineMatrix",
    "OrientationResult",
    "detect_orientation_osd",
    "estimate_skew_angle",
    "preprocess_page_image",
    "TesseractConfig",
    "find_tesseract_binary",
    "check_tesseract_readiness",
    "run_tesseract_tsv",
    "parse_tesseract_tsv",
    "PageModality",
    "ModalityConfig",
    "detect_page_modality",
    "process_hybrid_pdf",
    "process_image_file",
]
