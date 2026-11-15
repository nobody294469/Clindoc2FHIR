"""Image preprocessing and composed affine coordinate transformation pipeline.

Conforms to PROJECT_SPEC §7.1 and schemas/document_text.json.
Provides:
- Composed 3x3 affine transformation pipeline with exact matrix inversion.
- 4-corner transformation for bounding boxes under rotation, skew, scaling, and translation.
- Tesseract OSD orientation detection using local trained models (osd.traineddata).
- Projection-profile variance fine deskew (pure NumPy + Pillow, no OpenCV requirement).
- DPI normalization, contrast enhancement, and noise reduction for scanned documents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Any, Callable, Optional, Sequence, Tuple
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

from clindoc.ocr.geometry import BBox, BBoxDict


# ─── AFFINE TRANSFORMATION MATRIX (3x3) ───────────────────────────────────────

class AffineMatrix:
    """Rigorous 3x3 2D affine transformation matrix.

    Represents transformation:
        [x']   [m00  m01  m02] [x]
        [y'] = [m10  m11  m12] [y]
        [1 ]   [0    0    1  ] [1]

    Coordinates are composed in forward order:
        p_final = M_n @ ... @ M_2 @ M_1 @ p_init
    """

    __slots__ = ("m00", "m01", "m02", "m10", "m11", "m12")

    def __init__(
        self,
        m00: float = 1.0,
        m01: float = 0.0,
        m02: float = 0.0,
        m10: float = 0.0,
        m11: float = 1.0,
        m12: float = 0.0,
    ) -> None:
        self.m00 = float(m00)
        self.m01 = float(m01)
        self.m02 = float(m02)
        self.m10 = float(m10)
        self.m11 = float(m11)
        self.m12 = float(m12)

    @classmethod
    def identity(cls) -> AffineMatrix:
        return cls(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)

    @classmethod
    def scale(cls, sx: float, sy: float) -> AffineMatrix:
        return cls(sx, 0.0, 0.0, 0.0, sy, 0.0)

    @classmethod
    def translate(cls, tx: float, ty: float) -> AffineMatrix:
        return cls(1.0, 0.0, tx, 0.0, 1.0, ty)

    @classmethod
    def rotate_cw(cls, degrees: int, width: float, height: float) -> AffineMatrix:
        """Clockwise rotation by 0, 90, 180, or 270 degrees matching PIL image.rotate(-deg, expand=True)."""
        deg = degrees % 360
        if deg == 0:
            return cls.identity()
        elif deg == 90:
            # (x, y) -> (height - y, x)
            return cls(0.0, -1.0, height, 1.0, 0.0, 0.0)
        elif deg == 180:
            # (x, y) -> (width - x, height - y)
            return cls(-1.0, 0.0, width, 0.0, -1.0, height)
        elif deg == 270:
            # (x, y) -> (y, width - x)
            return cls(0.0, 1.0, 0.0, -1.0, 0.0, width)
        else:
            raise ValueError(f"rotate_cw only supports orthogonal angles [0, 90, 180, 270], got {degrees}")

    @classmethod
    def rotate_center(cls, angle_deg_ccw: float, cx: float, cy: float) -> AffineMatrix:
        """Arbitrary angle rotation counter-clockwise around center point (cx, cy)."""
        rad = math.radians(angle_deg_ccw)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)
        # T(cx, cy) @ R(rad) @ T(-cx, -cy)
        m00 = cos_a
        m01 = -sin_a
        m02 = cx * (1.0 - cos_a) + cy * sin_a
        m10 = sin_a
        m11 = cos_a
        m12 = cy * (1.0 - cos_a) - cx * sin_a
        return cls(m00, m01, m02, m10, m11, m12)

    def compose(self, next_transform: AffineMatrix) -> AffineMatrix:
        """Compose this transform with a subsequent transform: Result = next_transform @ self."""
        a = next_transform
        b = self
        return AffineMatrix(
            m00=a.m00 * b.m00 + a.m01 * b.m10,
            m01=a.m00 * b.m01 + a.m01 * b.m11,
            m02=a.m00 * b.m02 + a.m01 * b.m12 + a.m02,
            m10=a.m10 * b.m00 + a.m11 * b.m10,
            m11=a.m10 * b.m01 + a.m11 * b.m11,
            m12=a.m10 * b.m02 + a.m11 * b.m12 + a.m12,
        )

    def __matmul__(self, other: AffineMatrix) -> AffineMatrix:
        return other.compose(self)

    @property
    def determinant(self) -> float:
        return self.m00 * self.m11 - self.m01 * self.m10

    def inverse(self) -> AffineMatrix:
        """Compute the exact matrix inverse."""
        det = self.determinant
        if abs(det) < 1e-12:
            raise ValueError("Singular affine matrix cannot be inverted")
        inv_det = 1.0 / det
        m00 = self.m11 * inv_det
        m01 = -self.m01 * inv_det
        m02 = (self.m01 * self.m12 - self.m11 * self.m02) * inv_det
        m10 = -self.m10 * inv_det
        m11 = self.m00 * inv_det
        m12 = (self.m10 * self.m02 - self.m00 * self.m12) * inv_det
        return AffineMatrix(m00, m01, m02, m10, m11, m12)

    def transform_point(self, x: float, y: float) -> tuple[float, float]:
        """Transform a 2D point (x, y) through this matrix."""
        tx = self.m00 * x + self.m01 * y + self.m02
        ty = self.m10 * x + self.m11 * y + self.m12
        return tx, ty

    def transform_bbox_4corners(
        self,
        bbox: Sequence[float] | BBox | BBoxDict,
        clip_bounds: Optional[tuple[float, float]] = None,
    ) -> list[float]:
        """Transform a bounding box by projecting all 4 corners and computing the enclosing AABB.

        Ensures zero coordinate drift under rotation, skew, scaling, or translation.
        clip_bounds: Optional (max_x, max_y) to clamp the resulting bounding box.
        """
        if isinstance(bbox, BBox):
            x0, y0, x1, y1 = bbox.x0, bbox.y0, bbox.x1, bbox.y1
        elif isinstance(bbox, BBoxDict):
            x0, y0, x1, y1 = bbox.x0, bbox.y0, bbox.x1, bbox.y1
        else:
            x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])

        # 4 corners
        corners = [
            (x0, y0),
            (x1, y0),
            (x1, y1),
            (x0, y1),
        ]
        transformed = [self.transform_point(cx, cy) for cx, cy in corners]
        xs = [pt[0] for pt in transformed]
        ys = [pt[1] for pt in transformed]

        min_x = min(xs)
        min_y = min(ys)
        max_x = max(xs)
        max_y = max(ys)

        if clip_bounds is not None:
            w_limit, h_limit = clip_bounds
            min_x = max(0.0, min(min_x, w_limit))
            min_y = max(0.0, min(min_y, h_limit))
            max_x = max(min_x, min(max_x, w_limit))
            max_y = max(min_y, min(max_y, h_limit))

        return [round(min_x, 2), round(min_y, 2), round(max_x, 2), round(max_y, 2)]


# ─── ORIENTATION DETECTION VIA TESSERACT OSD ─────────────────────────────────

@dataclass
class OrientationResult:
    """Tesseract OSD orientation detection result."""
    orientation_degrees: int = 0      # Native orientation detected (0, 90, 180, 270)
    rotate_cw_degrees: int = 0        # Clockwise rotation needed to make upright (0, 90, 180, 270)
    confidence: float = 0.0           # Confidence score of orientation
    script: str = "Latin"             # Script name detected
    script_confidence: float = 0.0    # Confidence in script identification


def detect_orientation_osd(
    image: Image.Image,
    tesseract_cmd: Optional[str] = None,
    tessdata_dir: Optional[str] = None,
    min_confidence: float = 1.5,
    timeout: float = 15.0,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> OrientationResult:
    """Detect image orientation using Tesseract OSD (--psm 0).

    Returns OrientationResult with rotate_cw_degrees needed to make upright.
    If detection fails, confidence is too low, or text density is insufficient,
    safely returns 0 degrees rotation.
    Supports prompt termination on cancellation_event (lease loss or shutdown).
    """
    if cancellation_event is not None and cancellation_event.is_set():
        raise TimeoutError("OSD orientation detection aborted: cancellation event is already set")

    from clindoc.ocr.tesseract_runner import (
        find_tesseract_binary,
        find_tessdata_dir,
        execute_managed_tesseract,
    )

    tess_exe = tesseract_cmd or find_tesseract_binary()
    if not tess_exe or not Path(tess_exe).exists():
        return OrientationResult()

    tess_data = tessdata_dir or find_tessdata_dir()

    with tempfile.TemporaryDirectory(prefix="clindoc_osd_") as tmpdir:
        tmp_img = Path(tmpdir) / "osd_input.png"
        image.save(str(tmp_img), format="PNG")

        cmd = [str(tess_exe), str(tmp_img), "stdout", "--psm", "0"]
        env = os.environ.copy()
        if tess_data:
            env["TESSDATA_PREFIX"] = str(tess_data)

        try:
            retcode, stdout, stderr = execute_managed_tesseract(
                cmd=cmd,
                env=env,
                timeout=timeout,
                register_callback=register_callback,
                deregister_callback=deregister_callback,
                cancellation_event=cancellation_event,
            )
        except TimeoutError:
            if cancellation_event is not None and cancellation_event.is_set():
                raise
            return OrientationResult()
        except Exception:
            if cancellation_event is not None and cancellation_event.is_set():
                raise
            return OrientationResult()

        if retcode != 0 or not stdout.strip():
            return OrientationResult()

        return parse_osd_output(stdout, min_confidence=min_confidence)


def parse_osd_output(osd_text: str, min_confidence: float = 1.5) -> OrientationResult:
    """Parse Tesseract OSD stdout key-value format."""
    data: dict[str, str] = {}
    for line in osd_text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            data[k.strip().lower()] = v.strip()

    try:
        orient_deg = int(data.get("orientation in degrees", "0"))
        rotate_deg = int(data.get("rotate", "0"))
        conf = float(data.get("orientation confidence", "0.0"))
        script = data.get("script", "Latin")
        script_conf = float(data.get("script confidence", "0.0"))

        if conf < min_confidence:
            return OrientationResult(confidence=conf, script=script)

        return OrientationResult(
            orientation_degrees=orient_deg,
            rotate_cw_degrees=rotate_deg,
            confidence=conf,
            script=script,
            script_confidence=script_conf,
        )
    except (ValueError, TypeError):
        return OrientationResult()


# ─── DESKEW ESTIMATION (NUMPY + PILLOW) ───────────────────────────────────────

def estimate_skew_angle(
    image: Image.Image,
    max_angle: float = 5.0,
    step: float = 0.5,
) -> float:
    """Estimate skew angle in degrees using horizontal projection profile variance.

    Returns deskew angle in degrees counter-clockwise (Pillow rotate convention).
    Rotating image by `angle` degrees restores horizontal alignment.
    Pure NumPy + Pillow implementation without OpenCV.
    """
    gray = image.convert("L")
    w, h = gray.size
    # Downscale for performance if large
    scale = min(1.0, 1000.0 / max(w, h))
    if scale < 1.0:
        gray = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.BILINEAR)

    arr = np.array(gray)
    # Threshold dark pixels (text)
    threshold = np.percentile(arr, 35)
    bin_arr = (arr < threshold).astype(np.float32)

    best_score = -1.0
    best_angle = 0.0

    angles = np.arange(-max_angle, max_angle + step / 2, step)
    for angle in angles:
        pil_bin = Image.fromarray((bin_arr * 255).astype(np.uint8))
        rot = pil_bin.rotate(angle, resample=Image.Resampling.NEAREST)
        rot_arr = np.array(rot) > 0

        # Horizontal projection
        proj = np.sum(rot_arr, axis=1)
        score = float(np.var(proj))

        if score > best_score:
            best_score = score
            best_angle = float(angle)

    return best_angle


# ─── IMAGE ENHANCEMENT & PIPELINE PREPROCESSING ──────────────────────────────

def enhance_clinical_document_image(
    image: Image.Image,
    contrast_factor: float = 1.3,
    sharpness_factor: float = 1.2,
) -> Image.Image:
    """Enhance contrast and sharpness for scanned document legibility."""
    img = image.convert("RGB")
    if contrast_factor != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast_factor)
    if sharpness_factor != 1.0:
        img = ImageEnhance.Sharpness(img).enhance(sharpness_factor)
    return img


def preprocess_page_image(
    image: Image.Image,
    source_width_pt: float,
    source_height_pt: float,
    target_dpi: int = 300,
    source_dpi: int = 72,
    auto_rotate: bool = True,
    auto_deskew: bool = True,
    enhance: bool = True,
    tesseract_cmd: Optional[str] = None,
    tessdata_dir: Optional[str] = None,
    register_callback: Optional[Callable[[Any], None]] = None,
    deregister_callback: Optional[Callable[[Any], None]] = None,
    cancellation_event: Optional[threading.Event] = None,
) -> tuple[Image.Image, AffineMatrix, dict[str, Any]]:
    """Complete preprocessing pipeline for a page image.

    Tracks all geometric transformations in a composed AffineMatrix mapping
    from canonical PDF points (source_dpi, typically 72 pt) to preprocessed OCR pixel space.
    The inverse matrix maps Tesseract OCR bounding boxes back to canonical PDF points.

    Returns:
        (preprocessed_image, forward_affine_matrix, metadata_dict)
    """
    if cancellation_event is not None and cancellation_event.is_set():
        raise TimeoutError("Preprocessing aborted: cancellation event is already set")

    metadata: dict[str, Any] = {
        "source_size_pt": [source_width_pt, source_height_pt],
        "target_dpi": target_dpi,
        "source_dpi": source_dpi,
    }

    # Step 1: Scale from source points to raster pixels
    scale_factor = target_dpi / float(source_dpi)
    target_w = int(round(source_width_pt * scale_factor))
    target_h = int(round(source_height_pt * scale_factor))

    # Resize or ensure image matches target DPI dimensions
    if image.size != (target_w, target_h):
        curr_img = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    else:
        curr_img = image.copy()

    curr_matrix = AffineMatrix.scale(scale_factor, scale_factor)
    current_w, current_h = float(target_w), float(target_h)

    # Step 2: Orientation detection & rotation (OSD)
    osd_rot_cw = 0
    if auto_rotate:
        osd_res = detect_orientation_osd(
            curr_img,
            tesseract_cmd=tesseract_cmd,
            tessdata_dir=tessdata_dir,
            register_callback=register_callback,
            deregister_callback=deregister_callback,
            cancellation_event=cancellation_event,
        )
        metadata["osd"] = {
            "detected_deg": osd_res.orientation_degrees,
            "rotate_cw_deg": osd_res.rotate_cw_degrees,
            "confidence": osd_res.confidence,
        }
        if osd_res.rotate_cw_degrees in (90, 180, 270):
            osd_rot_cw = osd_res.rotate_cw_degrees
            # PIL image.rotate rotates CCW for positive angle, so -osd_rot_cw rotates CW
            curr_img = curr_img.rotate(-osd_rot_cw, expand=True)
            rot_mat = AffineMatrix.rotate_cw(osd_rot_cw, current_w, current_h)
            curr_matrix = curr_matrix.compose(rot_mat)
            current_w, current_h = float(curr_img.size[0]), float(curr_img.size[1])

    # Step 3: Fine deskew
    deskew_angle = 0.0
    if auto_deskew:
        deskew_angle = estimate_skew_angle(curr_img, max_angle=5.0, step=0.5)
        metadata["deskew_angle_ccw"] = deskew_angle
        if abs(deskew_angle) >= 0.5:
            # Rotate around center
            cx = current_w / 2.0
            cy = current_h / 2.0
            curr_img = curr_img.rotate(deskew_angle, resample=Image.Resampling.BICUBIC, fillcolor="white")
            deskew_mat = AffineMatrix.rotate_center(deskew_angle, cx, cy)
            curr_matrix = curr_matrix.compose(deskew_mat)

    # Step 4: Contrast enhancement
    if enhance:
        curr_img = enhance_clinical_document_image(curr_img)
        metadata["enhanced"] = True

    metadata["final_image_size_px"] = list(curr_img.size)
    return curr_img, curr_matrix, metadata

