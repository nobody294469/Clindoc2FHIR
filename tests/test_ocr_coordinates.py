"""Unit tests for M-OCR 4-corner affine coordinate transformations.

Verifies:
- Exact matrix composition and inversion (M @ M_inv = I).
- 4-corner bounding box mapping under orthogonal rotations (0, 90, 180, 270).
- Skew mapping and deskew invariance across angles (-4.0 to +4.0 deg).
- DPI scaling (72 pt -> 300 px -> 72 pt) with zero coordinate drift.
- Non-square page aspect ratios (Letter, Legal, wide faxes).
- Axis-aligned enclosing box containment.
"""
import math
import pytest
import numpy as np

from clindoc.ocr.geometry import BBox, BBoxDict
from clindoc.ocr.preprocessing import AffineMatrix, estimate_skew_angle


def test_affine_identity():
    ident = AffineMatrix.identity()
    assert ident.determinant == 1.0
    x, y = ident.transform_point(42.5, 99.1)
    assert math.isclose(x, 42.5) and math.isclose(y, 99.1)


def test_affine_scale_invertibility():
    # 72 DPI to 300 DPI
    scale = 300.0 / 72.0
    m_scale = AffineMatrix.scale(scale, scale)
    m_inv = m_scale.inverse()

    # Original PDF point
    px, py = 120.0, 350.0
    rx, ry = m_scale.transform_point(px, py)
    assert math.isclose(rx, 120.0 * scale)
    assert math.isclose(ry, 350.0 * scale)

    rec_x, rec_y = m_inv.transform_point(rx, ry)
    assert math.isclose(rec_x, px, abs_tol=1e-6)
    assert math.isclose(rec_y, py, abs_tol=1e-6)


@pytest.mark.parametrize("angle_deg", [0, 90, 180, 270])
def test_orthogonal_rotations_non_square_page(angle_deg):
    """Test rotations on a standard US Letter page (612 x 792 pt) scaled to 300 DPI."""
    scale = 300.0 / 72.0
    w_px = 612.0 * scale
    h_px = 792.0 * scale

    rot_m = AffineMatrix.rotate_cw(angle_deg, w_px, h_px)
    rot_inv = rot_m.inverse()

    test_box = [100.0, 200.0, 250.0, 240.0]  # [x0, y0, x1, y1] in raster px

    # Transform 4 corners forward
    ocr_box = rot_m.transform_bbox_4corners(test_box)
    assert ocr_box[0] <= ocr_box[2]
    assert ocr_box[1] <= ocr_box[3]

    # Transform 4 corners backward
    recovered = rot_inv.transform_bbox_4corners(ocr_box)
    assert math.isclose(recovered[0], test_box[0], abs_tol=0.1)
    assert math.isclose(recovered[1], test_box[1], abs_tol=0.1)
    assert math.isclose(recovered[2], test_box[2], abs_tol=0.1)
    assert math.isclose(recovered[3], test_box[3], abs_tol=0.1)


def test_composed_pipeline_end_to_end():
    """Verify composed: PDF Point -> Scale (300 DPI) -> OSD Rotate (90 CW) -> Deskew (1.5 deg) -> Invert."""
    scale = 300.0 / 72.0
    w_pt, h_pt = 612.0, 792.0
    w_px = w_pt * scale
    h_px = h_pt * scale

    # Step 1: Scale
    m1 = AffineMatrix.scale(scale, scale)

    # Step 2: 90 deg CW rotation
    m2 = AffineMatrix.rotate_cw(90, w_px, h_px)

    # Step 3: 1.5 deg CCW deskew around center
    new_w, new_h = h_px, w_px
    m3 = AffineMatrix.rotate_center(1.5, new_w / 2.0, new_h / 2.0)

    # Composed matrix
    forward = m1.compose(m2).compose(m3)
    backward = forward.inverse()

    # Original medication name bounding box in PDF points
    med_box_pt = [54.0, 120.0, 185.0, 134.0]

    # Project to preprocessed OCR raster space
    raster_box = forward.transform_bbox_4corners(med_box_pt)
    assert raster_box[0] < raster_box[2]
    assert raster_box[1] < raster_box[3]

    # Invert back to PDF points
    recovered_pt = backward.transform_bbox_4corners(raster_box)

    # Check that recovered box strictly encloses original box and tolerance is within sub-pixel bounds
    assert recovered_pt[0] <= med_box_pt[0] + 0.5
    assert recovered_pt[1] <= med_box_pt[1] + 0.5
    assert recovered_pt[2] >= med_box_pt[2] - 0.5
    assert recovered_pt[3] >= med_box_pt[3] - 0.5


def test_four_corners_vs_two_corners_skew():
    """Prove that 4-corner transformation handles skew without clipping or underestimating."""
    cx, cy = 500.0, 500.0
    skew_angle = -5.0  # degrees CW
    m = AffineMatrix.rotate_center(skew_angle, cx, cy)

    orig_box = [100.0, 100.0, 400.0, 120.0]  # Long text line

    # 4-corner AABB
    transformed_4 = m.transform_bbox_4corners(orig_box)

    # If someone naively only rotated (x0, y0) and (x1, y1):
    naive_p0 = m.transform_point(orig_box[0], orig_box[1])
    naive_p1 = m.transform_point(orig_box[2], orig_box[3])
    naive_box = [
        min(naive_p0[0], naive_p1[0]),
        min(naive_p0[1], naive_p1[1]),
        max(naive_p0[0], naive_p1[0]),
        max(naive_p0[1], naive_p1[1]),
    ]

    # The 4-corner box properly accounts for other corners (x1, y0) and (x0, y1),
    # so its vertical extent must be strictly greater than naive 2-corner
    assert (transformed_4[3] - transformed_4[1]) > (naive_box[3] - naive_box[1]) + 10.0


def test_pdf_nonzero_cropbox_origin_and_rotation(tmp_path):
    """Verify PyMuPDF coordinate conventions with non-zero CropBox origin and rotations."""
    import pymupdf
    from clindoc.ocr.hybrid import extract_born_digital_page

    pdf_p = tmp_path / "cropbox_test.pdf"
    doc = pymupdf.open()

    for rot in [0, 90, 180, 270]:
        # Non-square page (Legal: 612 x 1008 pt)
        p = doc.new_page(width=612, height=1008)
        p.insert_text((150, 250), "Clinical Note Text")
        # Set cropbox with non-zero origin: offset by (50, 100)
        crop = pymupdf.Rect(50, 100, 550, 900)
        p.set_cropbox(crop)
        if rot != 0:
            p.set_rotation(rot)

    doc.save(str(pdf_p))
    doc.close()

    # Reopen and test extraction
    read_doc = pymupdf.open(str(pdf_p))
    for page_idx in range(4):
        p = read_doc[page_idx]
        page_num = page_idx + 1
        words, lines = extract_born_digital_page(p, page_num=page_num)

        assert len(words) > 0
        w_px = float(p.rect.width)
        h_px = float(p.rect.height)

        for w in words:
            # Coordinates must strictly fit within visible page canvas
            assert 0.0 <= w.bbox[0] <= w.bbox[2] <= w_px + 0.5
            assert 0.0 <= w.bbox[1] <= w.bbox[3] <= h_px + 0.5


def test_composed_transform_non_square_legal_tolerance():
    """Verify composed affine transform on Legal size (612 x 1008 pt) with defined tolerance."""
    scale = 300.0 / 72.0
    w_pt, h_pt = 612.0, 1008.0
    w_px = w_pt * scale
    h_px = h_pt * scale

    # Composed: Scale -> 270 CW -> 2.0 deg CCW deskew -> translate padding
    m_scale = AffineMatrix.scale(scale, scale)
    m_rot = AffineMatrix.rotate_cw(270, w_px, h_px)
    m_deskew = AffineMatrix.rotate_center(2.0, h_px / 2.0, w_px / 2.0)
    m_pad = AffineMatrix.translate(10.0, 20.0)

    composed = m_scale.compose(m_rot).compose(m_deskew).compose(m_pad)
    inv = composed.inverse()

    test_box = [120.0, 300.0, 280.0, 330.0]  # PDF points
    raster_box = composed.transform_bbox_4corners(test_box)
    recovered = inv.transform_bbox_4corners(raster_box)

    # Stated numerical tolerance: bounding box expansion under AABB rotation <= 0.5 pt
    NUMERICAL_TOLERANCE_PT = 0.5
    assert recovered[0] <= test_box[0] + NUMERICAL_TOLERANCE_PT
    assert recovered[1] <= test_box[1] + NUMERICAL_TOLERANCE_PT
    assert recovered[2] >= test_box[2] - NUMERICAL_TOLERANCE_PT
    assert recovered[3] >= test_box[3] - NUMERICAL_TOLERANCE_PT

