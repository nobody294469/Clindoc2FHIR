"""Hybrid PDF Modality Detection and Multi-Page Processing.

Conforms to PROJECT_SPEC §7.1 and schemas/document_text.json.
Guarantees:
- Page-level modality detection: distinguishes born-digital vector text from scanned raster pages.
- Preserves born-digital vector text with exact coordinates and zero OCR noise.
- Renders and processes scanned pages via Tesseract 5.x with composed affine transformations.
- Supports mixed documents with page-level provenance.
- Deterministic reading order across pages.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import io
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Optional, Sequence
from PIL import Image
import pymupdf

from clindoc.ocr.geometry import BBox, BBoxDict
from clindoc.ocr.schema import DocumentOCRRecord, LineBox, WordBox
from clindoc.ocr.preprocessing import preprocess_page_image
from clindoc.ocr.tesseract_runner import (
    TesseractConfig,
    run_tesseract_tsv,
    parse_tesseract_tsv,
)
from clindoc.ocr.layout import sort_page_lines_reading_order

logger = logging.getLogger(__name__)


class PageModality(str, Enum):
    BORN_DIGITAL = "born_digital"
    SCANNED_IMAGE = "scanned_image"
    HYBRID = "hybrid"
    BLANK = "blank"


@dataclass
class ModalityConfig:
    """Configurable thresholds for page modality detection."""
    min_char_count: int = 15
    hybrid_image_area_ratio: float = 0.3


def detect_page_modality(
    page: pymupdf.Page,
    min_char_count: int = 15,
    hybrid_image_area_ratio: float = 0.3,
    config: Optional[ModalityConfig] = None,
) -> PageModality:
    """Inspect a single PDF page and determine its text/image modality.

    Accepts optional ModalityConfig or individual threshold overrides.
    Defaults preserve complete backward compatibility (15 chars, 0.3 area ratio).
    """
    eff_min_chars = config.min_char_count if config else min_char_count
    eff_area_ratio = config.hybrid_image_area_ratio if config else hybrid_image_area_ratio

    raw_text = page.get_text().strip()
    char_count = len(raw_text)
    images = page.get_images(full=True)

    if char_count >= eff_min_chars:
        if not images:
            return PageModality.BORN_DIGITAL
        # Has both digital text and images
        page_area = page.rect.width * page.rect.height
        total_img_area = 0.0
        for img_info in images:
            xref = img_info[0]
            try:
                rects = page.get_image_rects(xref)
                for r in rects:
                    total_img_area += r.width * r.height
            except Exception:
                pass
        if total_img_area > eff_area_ratio * page_area:
            return PageModality.HYBRID
        return PageModality.BORN_DIGITAL
    else:
        if images:
            return PageModality.SCANNED_IMAGE
        # Check if page has vector drawings or any raster content
        drawings = page.get_drawings()
        if drawings or char_count > 0:
            return PageModality.SCANNED_IMAGE
        return PageModality.BLANK


def extract_born_digital_page(
    page: pymupdf.Page,
    page_num: int,
) -> tuple[list[WordBox], list[LineBox]]:
    """Extract vector text from a born-digital PDF page directly via PyMuPDF.

    Accurately maps coordinates into canonical visual page space (page.rect):
    - Applies page.rotation_matrix when page.rotation is non-zero (0, 90, 180, 270).
    - Offsets by page.rect.x0 and page.rect.y0 to handle non-zero CropBox origins.
    """
    raw_words = page.get_text("words")
    all_words: list[WordBox] = []
    lines_dict: dict[tuple[int, int], list[WordBox]] = {}

    rot_mat = page.rotation_matrix if getattr(page, "rotation", 0) != 0 else None
    origin_x = float(page.rect.x0)
    origin_y = float(page.rect.y0)

    for w in raw_words:
        x0, y0, x1, y1, word_text, b_no, l_no, _ = w
        clean_text = word_text.strip()
        if not clean_text:
            continue

        if rot_mat:
            r = pymupdf.Rect(x0, y0, x1, y1) * rot_mat
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1

        # Offset by visible origin
        x0 -= origin_x
        y0 -= origin_y
        x1 -= origin_x
        y1 -= origin_y

        word_box = WordBox(
            text=clean_text,
            bbox=[round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
            confidence=1.0,
            page=page_num,
        )
        all_words.append(word_box)
        key = (b_no, l_no)
        if key not in lines_dict:
            lines_dict[key] = []
        lines_dict[key].append(word_box)

    page_lines: list[LineBox] = []
    for (b_no, l_no), words in lines_dict.items():
        sorted_words = sorted(words, key=lambda w: w.bbox[0])
        line_text = " ".join(w.text for w in sorted_words)
        min_x = min(w.bbox[0] for w in sorted_words)
        min_y = min(w.bbox[1] for w in sorted_words)
        max_x = max(w.bbox[2] for w in sorted_words)
        max_y = max(w.bbox[3] for w in sorted_words)

        page_lines.append(
            LineBox(
                text=line_text,
                bbox=[round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)],
                confidence=1.0,
                page=page_num,
                words=sorted_words,
            )
        )

    p_w = float(page.rect.width)
    p_h = float(page.rect.height)
    sorted_lines = sort_page_lines_reading_order(page_lines, page_width=p_w, page_height=p_h)
    return all_words, sorted_lines


def process_scanned_page(
    page: pymupdf.Page,
    page_num: int,
    config: Optional[TesseractConfig] = None,
    dpi: int = 300,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> tuple[list[WordBox], list[LineBox], dict[str, Any]]:
    """Rasterize scanned PDF page and execute managed Tesseract OCR with affine transformation."""
    p_w = float(page.rect.width)
    p_h = float(page.rect.height)

    # Render at high DPI for character recognition accuracy.
    # PyMuPDF's page.get_pixmap() renders the visual page as displayed,
    # already incorporating page.rotation and page.cropbox!
    pix = page.get_pixmap(dpi=dpi)
    raw_img = Image.open(io.BytesIO(pix.tobytes("png")))

    # Preprocess image and compose affine matrix (tracks scaling, OSD, deskew)
    prep_img, forward_matrix, prep_meta = preprocess_page_image(
        image=raw_img,
        source_width_pt=p_w,
        source_height_pt=p_h,
        target_dpi=dpi,
        source_dpi=72,
        auto_rotate=True,
        auto_deskew=True,
        enhance=True,
        tesseract_cmd=config.executable_path if config else None,
        tessdata_dir=config.tessdata_dir if config else None,
        register_callback=register_callback,
        deregister_callback=deregister_callback,
        cancellation_event=cancellation_event,
    )

    # Execute Tesseract
    tsv_text = run_tesseract_tsv(
        image=prep_img,
        config=config,
        register_callback=register_callback,
        deregister_callback=deregister_callback,
        cancellation_event=cancellation_event,
    )

    # Parse TSV and invert coordinates back to canonical PDF points (72 pt)
    words, lines = parse_tesseract_tsv(
        tsv_text=tsv_text,
        forward_matrix=forward_matrix,
        page_num=page_num,
        canonical_width=p_w,
        canonical_height=p_h,
    )

    return words, lines, prep_meta


def process_hybrid_pdf(
    pdf_path: str | Path,
    doc_id: Optional[str] = None,
    config: Optional[TesseractConfig] = None,
    dpi: int = 300,
    force_ocr: bool = False,
    modality_config: Optional[ModalityConfig] = None,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> DocumentOCRRecord:
    """Process a multi-page PDF with page-level modality routing.

    - Born-digital pages extract native vector text directly.
    - Scanned/raster pages are rasterized and processed via Tesseract 5.x.
    - Output coordinates are strictly in canonical PDF points (72 DPI).
    """
    start_time = time.perf_counter()
    p_path = Path(pdf_path)
    resolved_id = doc_id or p_path.stem

    doc = pymupdf.open(str(p_path))
    num_pages = len(doc)

    all_lines: list[LineBox] = []
    all_words: list[WordBox] = []
    page_modalities: list[str] = []
    page_metadata: list[dict[str, Any]] = []

    for page_idx in range(num_pages):
        if cancellation_event is not None and cancellation_event.is_set():
            raise TimeoutError("Hybrid PDF processing cancelled due to lease loss or shutdown")
        page_num = page_idx + 1
        page = doc[page_idx]

        modality = detect_page_modality(page, config=modality_config)
        if force_ocr and modality != PageModality.BLANK:
            modality = PageModality.SCANNED_IMAGE

        page_modalities.append(modality.value)

        if modality == PageModality.BORN_DIGITAL:
            words, lines = extract_born_digital_page(page, page_num=page_num)
            all_words.extend(words)
            all_lines.extend(lines)
            page_metadata.append({"modality": modality.value, "word_count": len(words)})
        elif modality == PageModality.BLANK:
            page_metadata.append({"modality": modality.value, "word_count": 0})
        else:
            # Scanned or Hybrid
            words, lines, meta = process_scanned_page(
                page=page,
                page_num=page_num,
                config=config,
                dpi=dpi,
                register_callback=register_callback,
                deregister_callback=deregister_callback,
                cancellation_event=cancellation_event,
            )
            all_words.extend(words)
            all_lines.extend(lines)
            meta["modality"] = modality.value
            meta["word_count"] = len(words)
            page_metadata.append(meta)

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    overall_engine = "hybrid" if len(set(page_modalities)) > 1 else (
        "pymupdf_digital" if all(m == "born_digital" for m in page_modalities) else "tesseract"
    )

    return DocumentOCRRecord(
        doc_id=resolved_id,
        source_path=str(p_path),
        engine=overall_engine,
        device="cpu",
        num_pages=num_pages,
        lines=all_lines,
        words=all_words,
        metadata={
            "processing_time_ms": elapsed_ms,
            "page_modalities": page_modalities,
            "page_details": page_metadata,
        },
    )


def process_image_file(
    image_path: str | Path,
    doc_id: Optional[str] = None,
    config: Optional[TesseractConfig] = None,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> DocumentOCRRecord:
    """Process a standalone raster image file (.png, .jpg, .tiff) with Tesseract 5.x.

    Canonical coordinate space is the unrotated image's native pixels.
    """
    start_time = time.perf_counter()
    img_p = Path(image_path)
    resolved_id = doc_id or img_p.stem

    raw_img = Image.open(str(img_p))
    orig_w, orig_h = float(raw_img.size[0]), float(raw_img.size[1])

    # Preprocess image: scale factor 1.0 (preserve native pixels), OSD rotate, deskew, enhance
    prep_img, forward_matrix, prep_meta = preprocess_page_image(
        image=raw_img,
        source_width_pt=orig_w,
        source_height_pt=orig_h,
        target_dpi=300,
        source_dpi=300,  # 1:1 scale for native images
        auto_rotate=True,
        auto_deskew=True,
        enhance=True,
        tesseract_cmd=config.executable_path if config else None,
        tessdata_dir=config.tessdata_dir if config else None,
        register_callback=register_callback,
        deregister_callback=deregister_callback,
        cancellation_event=cancellation_event,
    )

    tsv_text = run_tesseract_tsv(
        image=prep_img,
        config=config,
        register_callback=register_callback,
        deregister_callback=deregister_callback,
        cancellation_event=cancellation_event,
    )

    words, lines = parse_tesseract_tsv(
        tsv_text=tsv_text,
        forward_matrix=forward_matrix,
        page_num=1,
        canonical_width=orig_w,
        canonical_height=orig_h,
    )

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    prep_meta["modality"] = "scanned_image"
    prep_meta["word_count"] = len(words)

    return DocumentOCRRecord(
        doc_id=resolved_id,
        source_path=str(img_p),
        engine="tesseract",
        device="cpu",
        num_pages=1,
        lines=lines,
        words=words,
        metadata={
            "processing_time_ms": elapsed_ms,
            "page_modalities": ["scanned_image"],
            "page_details": [prep_meta],
        },
    )

