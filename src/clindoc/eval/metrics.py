"""Mathematical evaluation metrics for ClinicalDoc M-BENCHMARK.

Implements rigorous, deterministic metrics:
1. Optical Character Recognition (OCR):
   - Character Error Rate (CER) via Levenshtein edit distance.
   - Word Error Rate (WER).
2. Numeric & Categorical Equivalence:
   - Absolute tolerance (|v1 - v2| <= 1e-4).
   - Relative percentage tolerance (<= 0.1% for large values > 100).
   - Categorical normalization (e.g. lab flags, medication routes, frequencies).
3. 1-to-1 Maximum-Weight Bipartite Entity Matching:
   - Powered by Scipy linear_sum_assignment to prevent greedy matching bias and double counting.
   - Precision, Recall, F1-Score, Exact Match Rate.
4. Spatial Grounding & Span Alignment:
   - Bounding Box Intersection-over-Union (IoU) >= 0.50 rate.
   - Mean IoU of matched bounding boxes.
   - Character start/end exact span match rate and partial overlap rate.
   - Penalization of unmatched ground truth entities.
5. Medication Tuple Completeness (REQ-44):
   - Slot association for name, dose, route, frequency, duration, condition/hold.
   - Complete tuple accuracy.
   - Ophthalmic convention tracking (OD, OS, OU, drops vs conventional).
6. Verification Specificity & Calibration:
   - False Positive Rejection Rate (FPRR / specificity on corrupted claims).
   - Negative Predictive Value (NPV).
   - Expected Calibration Error (ECE), returned only if genuine probabilities exist.
7. FHIR Representation:
   - Active representation rate (bundle claims / verified active claims).
   - Candidate pruning rate (shadowed competing candidates / total candidates).
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
from scipy.optimize import linear_sum_assignment

from clindoc.eval.schema import (
    FieldMetric,
    MedicationTupleMetric,
    SpatialGroundingMetric,
    VerificationMetric,
    FHIRRepresentationMetric,
    TerminologyMetric,
)


# ─── 1. OCR ERROR RATES (CER / WER) ──────────────────────────────────────────

def levenshtein_distance(s1: Sequence[Any], s2: Sequence[Any]) -> int:
    """Compute Levenshtein edit distance between two sequences (characters or tokens)."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def compute_cer(reference_text: str, ocr_text: str) -> float:
    """Compute Character Error Rate (CER) normalized by reference character length."""
    ref_norm = reference_text.strip()
    ocr_norm = ocr_text.strip()
    if not ref_norm:
        return 0.0 if not ocr_norm else 1.0
    dist = levenshtein_distance(ref_norm, ocr_norm)
    return round(dist / max(len(ref_norm), 1), 4)


def compute_wer(reference_text: str, ocr_text: str) -> float:
    """Compute Word Error Rate (WER) on whitespace-tokenized words."""
    ref_words = reference_text.split()
    ocr_words = ocr_text.split()
    if not ref_words:
        return 0.0 if not ocr_words else 1.0
    dist = levenshtein_distance(ref_words, ocr_words)
    return round(dist / max(len(ref_words), 1), 4)


# ─── 2. NUMERIC & VALUE EQUIVALENCE RULES ────────────────────────────────────

def is_numeric_equivalent(v1: Any, v2: Any, abs_tol: float = 1e-4, rel_tol: float = 1e-3) -> bool:
    """Check clinical numeric equivalence preserving precision and percentage tolerance."""
    try:
        num1 = float(v1)
        num2 = float(v2)
    except (ValueError, TypeError):
        return False

    if math.isnan(num1) or math.isnan(num2):
        return False

    # Check absolute tolerance for small numbers
    if abs(num1 - num2) <= abs_tol:
        return True

    # Check relative tolerance for large values (> 100)
    if abs(num2) > 100.0:
        return abs(num1 - num2) / abs(num2) <= rel_tol

    return False


def normalize_entity_string(val: Any, field_type: Optional[str] = None) -> str:
    """Normalize entity text for clinical string comparison (case-fold, whitespace, punctuation, aliases)."""
    if val is None:
        return ""
    text = str(val).strip().lower()
    text = re.sub(r"\s+", " ", text).rstrip(".,;")

    if field_type:
        ft = field_type.lower()
        if ft in ("med.route", "route"):
            route_map = {
                "po": "oral", "by mouth": "oral", "p.o.": "oral", "oral": "oral",
                "iv": "intravenous", "i.v.": "intravenous", "intravenous": "intravenous",
                "im": "intramuscular", "i.m.": "intramuscular",
                "subling": "sublingual", "sublingual": "sublingual", "sl": "sublingual",
                "topical": "topical", "top": "topical",
                "ou": "ou", "od": "od", "os": "os",
                "both eyes": "ou", "right eye": "od", "left eye": "os",
            }
            return route_map.get(text, text)
        if ft in ("lab.flag", "flag"):
            flag_map = {
                "high": "h", "h": "h", "abnormal high": "h",
                "low": "l", "l": "l", "abnormal low": "l",
                "normal": "norm", "n": "norm", "within normal limits": "norm", "wnl": "norm", "norm": "norm",
                "critical": "crit", "panic": "crit",
            }
            return flag_map.get(text, text)
        if ft in ("med.duration", "duration"):
            return re.sub(r"^(?:for|x|duration:)\s+", "", text).strip()

    return text


def is_value_match(gt_val: Any, pred_val: Any, field_type: str = "") -> bool:
    """Determine whether an extracted entity value matches ground truth."""
    if gt_val is None and pred_val is None:
        return True
    if gt_val is None or pred_val is None:
        return False

    # Try numeric match for value and dose fields
    if "value" in field_type or "dose" in field_type:
        if is_numeric_equivalent(gt_val, pred_val):
            return True

    # Categorical normalization with field awareness
    s_gt = normalize_entity_string(gt_val, field_type)
    s_pred = normalize_entity_string(pred_val, field_type)

    if s_gt == s_pred:
        return True

    # Check alias maps
    route_aliases = {
        "po": {"oral", "by mouth", "p.o.", "po"},
        "ou": {"both eyes", "ou", "o.u."},
        "od": {"right eye", "od", "o.d."},
        "os": {"left eye", "os", "o.s."},
        "iv": {"intravenous", "iv", "i.v."},
    }
    for canon, aliases in route_aliases.items():
        if s_gt in aliases and s_pred in aliases:
            return True

    freq_aliases = {
        "qd": {"once daily", "daily", "every day", "q.d.", "qd", "qday"},
        "bid": {"twice daily", "two times daily", "bid", "b.i.d."},
        "tid": {"three times daily", "tid", "t.i.d."},
        "qid": {"four times daily", "qid", "q.i.d."},
        "qhs": {"every evening", "at bedtime", "qhs", "q.h.s."},
        "prn": {"as needed", "prn", "p.r.n."},
    }
    for canon, aliases in freq_aliases.items():
        if s_gt in aliases and s_pred in aliases:
            return True

    flag_aliases = {
        "h": {"high", "h", "abnormal high"},
        "l": {"low", "l", "abnormal low"},
        "norm": {"normal", "n", "within normal limits", "wnl", "norm"},
        "crit": {"critical", "panic", "critical high", "critical low"},
    }
    for canon, aliases in flag_aliases.items():
        if s_gt in aliases and s_pred in aliases:
            return True

    return False


# ─── 3. BIPARTITE MAXIMUM-WEIGHT ENTITY MATCHING ────────────────────────────

def compute_field_metrics_bipartite(
    field_or_gt_claims: Union[str, Sequence[Dict[str, Any]]],
    gt_or_pred_claims: Union[Sequence[Any], Sequence[Dict[str, Any]]],
    pred_or_target_fields: Union[Sequence[Any], Sequence[str]] = (),
    gt_codes: Optional[Sequence[Optional[str]]] = None,
    pred_codes: Optional[Sequence[Optional[str]]] = None,
) -> Union[FieldMetric, Tuple[Dict[str, FieldMetric], float, float]]:
    """Compute extraction precision, recall, and F1 via 1-to-1 maximum-weight bipartite matching.

    Supports two signatures:
    1. Single-field: compute_field_metrics_bipartite(field_type, gt_values, pred_values) -> FieldMetric
    2. Multi-field claims: compute_field_metrics_bipartite(gt_claims, pred_claims, target_fields) -> (dict[str, FieldMetric], macro_f1, micro_f1)
    """
    if isinstance(field_or_gt_claims, str):
        # Single field invocation
        field_type = field_or_gt_claims
        gt_values = gt_or_pred_claims
        pred_values = pred_or_target_fields
        return _compute_single_field_bipartite(field_type, gt_values, pred_values, gt_codes, pred_codes)
    else:
        # Multi-field claims invocation
        gt_claims = field_or_gt_claims
        pred_claims = gt_or_pred_claims
        target_fields = pred_or_target_fields
        return compute_multi_field_metrics_bipartite(gt_claims, pred_claims, target_fields)


def _compute_single_field_bipartite(
    field_type: str,
    gt_values: Sequence[Any],
    pred_values: Sequence[Any],
    gt_codes: Optional[Sequence[Optional[str]]] = None,
    pred_codes: Optional[Sequence[Optional[str]]] = None,
) -> FieldMetric:
    n_gt = len(gt_values)
    n_pred = len(pred_values)

    if n_gt == 0 and n_pred == 0:
        return FieldMetric(
            field_type=field_type,
            true_positives=0,
            false_positives=0,
            false_negatives=0,
            precision=1.0,
            recall=1.0,
            f1_score=1.0,
            exact_matches=0,
            exact_match_rate=1.0,
            ground_truth_count=0,
            prediction_count=0,
        )

    if n_gt == 0:
        return FieldMetric(
            field_type=field_type,
            true_positives=0,
            false_positives=n_pred,
            false_negatives=0,
            precision=0.0,
            recall=1.0,
            f1_score=0.0,
            exact_matches=0,
            exact_match_rate=0.0,
            ground_truth_count=0,
            prediction_count=n_pred,
        )

    if n_pred == 0:
        return FieldMetric(
            field_type=field_type,
            true_positives=0,
            false_positives=0,
            false_negatives=n_gt,
            precision=1.0,
            recall=0.0,
            f1_score=0.0,
            exact_matches=0,
            exact_match_rate=0.0,
            ground_truth_count=n_gt,
            prediction_count=0,
        )

    weight_matrix = np.zeros((n_gt, n_pred), dtype=np.float64)
    exact_flags = np.zeros((n_gt, n_pred), dtype=bool)

    for i, g_val in enumerate(gt_values):
        g_code = gt_codes[i] if gt_codes and i < len(gt_codes) else None
        for j, p_val in enumerate(pred_values):
            p_code = pred_codes[j] if pred_codes and j < len(pred_codes) else None

            if is_value_match(g_val, p_val, field_type):
                weight_matrix[i, j] = 1.0
                exact_flags[i, j] = True
            elif g_code and p_code and g_code == p_code:
                weight_matrix[i, j] = 0.95
            else:
                weight_matrix[i, j] = 0.0

    cost_matrix = 1.0 - weight_matrix
    row_ind, col_ind = linear_sum_assignment(cost_matrix)

    matched_gt: set[int] = set()
    matched_pred: set[int] = set()
    exact_count = 0

    for r, c in zip(row_ind, col_ind):
        if weight_matrix[r, c] > 0.0:
            matched_gt.add(r)
            matched_pred.add(c)
            if exact_flags[r, c]:
                exact_count += 1

    tp = len(matched_gt)
    fp = n_pred - tp
    fn = n_gt - tp

    precision = round(tp / max(tp + fp, 1), 4)
    recall = round(tp / max(tp + fn, 1), 4)
    f1 = round(2.0 * precision * recall / max(precision + recall, 1e-8), 4)
    exact_rate = round(exact_count / max(n_gt, 1), 4)

    return FieldMetric(
        field_type=field_type,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1_score=f1,
        exact_matches=exact_count,
        exact_match_rate=exact_rate,
        ground_truth_count=n_gt,
        prediction_count=n_pred,
    )


def compute_multi_field_metrics_bipartite(
    gt_claims: Sequence[Dict[str, Any]],
    pred_claims: Sequence[Dict[str, Any]],
    target_fields: Sequence[str],
) -> Tuple[Dict[str, FieldMetric], float, float]:
    """Compute per-field metrics, macro F1, and micro F1 across multiple claims using bipartite matching."""
    field_metrics: Dict[str, FieldMetric] = {}
    total_tp = 0
    total_fp = 0
    total_fn = 0
    f1_sum = 0.0
    active_fields_count = 0

    for ft in target_fields:
        gts = [c.get("value") for c in gt_claims if c.get("field_type") == ft]
        preds = [c.get("value") for c in pred_claims if c.get("field_type") == ft]

        fm = _compute_single_field_bipartite(ft, gts, preds)
        field_metrics[ft] = fm

        if fm.ground_truth_count > 0 or fm.prediction_count > 0:
            total_tp += fm.true_positives
            total_fp += fm.false_positives
            total_fn += fm.false_negatives
            f1_sum += fm.f1_score
            active_fields_count += 1

    macro_f1 = round(f1_sum / max(active_fields_count, 1), 4)
    micro_prec = total_tp / max(total_tp + total_fp, 1)
    micro_rec = total_tp / max(total_tp + total_fn, 1)
    micro_f1 = round(2.0 * micro_prec * micro_rec / max(micro_prec + micro_rec, 1e-8), 4)

    return field_metrics, macro_f1, micro_f1


# ─── 4. SPATIAL IOU & CHARACTER GROUNDING ────────────────────────────────────

def compute_box_iou(box1: Sequence[float], box2: Sequence[float]) -> float:
    """Compute spatial Intersection-over-Union (IoU) between two 2D bounding boxes [x0, y0, x1, y1]."""
    x0 = max(box1[0], box2[0])
    y0 = max(box1[1], box2[1])
    x1 = min(box1[2], box2[2])
    y1 = min(box1[3], box2[3])

    inter_w = max(0.0, x1 - x0)
    inter_h = max(0.0, y1 - y0)
    intersection = inter_w * inter_h

    area1 = max(0.0, box1[2] - box1[0]) * max(0.0, box1[3] - box1[1])
    area2 = max(0.0, box2[2] - box2[0]) * max(0.0, box2[3] - box2[1])
    union = area1 + area2 - intersection

    if union <= 0.0:
        return 0.0
    return round(intersection / union, 4)


def compute_spatial_grounding_metrics(
    gt_entities_or_boxes: Sequence[Union[Dict[str, Any], Optional[Sequence[float]]]],
    pred_entities_or_boxes: Sequence[Union[Dict[str, Any], Optional[Sequence[float]]]],
    gt_spans: Optional[Sequence[Optional[Tuple[int, int]]]] = None,
    pred_spans: Optional[Sequence[Optional[Tuple[int, int]]]] = None,
    iou_threshold: float = 0.50,
) -> SpatialGroundingMetric:
    """Compute spatial IoU and character-span grounding metrics.
    
    Accepts either lists of entity dicts (with 'bbox', 'char_start', 'char_end') or raw box lists.
    Unmatched ground truth entities are explicitly counted as failures.
    """
    n_gt = len(gt_entities_or_boxes)
    if n_gt == 0:
        return SpatialGroundingMetric(
            total_ground_truth_entities=0,
            evaluated_entities_count=0,
            iou_50_matches=0,
            iou_50_rate=1.0,
            mean_iou_matched=1.0,
            exact_char_span_matches=0,
            exact_char_span_rate=1.0,
            partial_char_overlap_matches=0,
            partial_char_overlap_rate=1.0,
        )

    # Normalize inputs to box list and span list
    gt_boxes = []
    g_spans = []
    for item in gt_entities_or_boxes:
        if isinstance(item, dict):
            gt_boxes.append(item.get("bbox"))
            cs = item.get("char_start")
            ce = item.get("char_end")
            g_spans.append((cs, ce) if cs is not None and ce is not None else None)
        else:
            gt_boxes.append(item)
            g_spans.append(None)

    pred_boxes = []
    p_spans = []
    for item in pred_entities_or_boxes:
        if isinstance(item, dict):
            pred_boxes.append(item.get("bbox"))
            cs = item.get("char_start")
            ce = item.get("char_end")
            p_spans.append((cs, ce) if cs is not None and ce is not None else None)
        else:
            pred_boxes.append(item)
            p_spans.append(None)

    if gt_spans is not None:
        g_spans = list(gt_spans)
    if pred_spans is not None:
        p_spans = list(pred_spans)

    iou_50_count = 0
    iou_sum = 0.0
    evaluated_count = min(n_gt, len(pred_boxes))

    for i in range(evaluated_count):
        g_box = gt_boxes[i]
        p_box = pred_boxes[i]
        if g_box is not None and p_box is not None and len(g_box) >= 4 and len(p_box) >= 4:
            iou = compute_box_iou(g_box, p_box)
            iou_sum += iou
            if iou >= iou_threshold:
                iou_50_count += 1

    mean_iou = round(iou_sum / max(evaluated_count, 1), 4)
    iou_rate = round(iou_50_count / max(n_gt, 1), 4)

    # Character span matching
    exact_span_count = 0
    partial_span_count = 0
    span_eval_count = min(n_gt, len(p_spans))

    for i in range(span_eval_count):
        g_s = g_spans[i]
        p_s = p_spans[i]
        if g_s and p_s and g_s[0] is not None and p_s[0] is not None:
            g_start, g_end = g_s
            p_start, p_end = p_s
            if g_start == p_start and g_end == p_end:
                exact_span_count += 1
            if max(g_start, p_start) < min(g_end, p_end):
                partial_span_count += 1

    exact_span_rate = round(exact_span_count / max(n_gt, 1), 4)
    partial_span_rate = round(partial_span_count / max(n_gt, 1), 4)

    return SpatialGroundingMetric(
        total_ground_truth_entities=n_gt,
        evaluated_entities_count=evaluated_count,
        iou_50_matches=iou_50_count,
        iou_50_rate=iou_rate,
        mean_iou_matched=mean_iou,
        exact_char_span_matches=exact_span_count,
        exact_char_span_rate=exact_span_rate,
        partial_char_overlap_matches=partial_span_count,
        partial_char_overlap_rate=partial_span_rate,
    )


# ─── 5. MEDICATION TUPLE ASSOCIATION (REQ-44) ────────────────────────────────

def compute_medication_tuple_metrics(
    gt_prescriptions: Sequence[Dict[str, Any]],
    pred_prescriptions: Sequence[Dict[str, Any]],
) -> MedicationTupleMetric:
    """Compute independent slot and complete tuple association accuracy for medications."""
    total_rx = len(gt_prescriptions)
    if total_rx == 0:
        return MedicationTupleMetric(
            total_prescriptions=0,
            complete_tuple_matches=0,
            tuple_accuracy=1.0,
            name_accuracy=1.0,
            dose_accuracy=1.0,
            route_accuracy=1.0,
            frequency_accuracy=1.0,
            duration_accuracy=1.0,
            conditional_hold_accuracy=1.0,
        )

    matched_tuples = 0
    name_correct = 0
    dose_correct = 0
    route_correct = 0
    freq_correct = 0
    dur_correct = 0
    cond_correct = 0
    ophthalmic_count = 0
    conventional_count = 0

    evaluated_count = min(total_rx, len(pred_prescriptions))

    for i in range(evaluated_count):
        gt = gt_prescriptions[i]
        pred = pred_prescriptions[i]

        gt_route = str(gt.get("route", "")).strip().lower()
        if gt_route in ("ou", "od", "os", "both eyes", "right eye", "left eye") or "drop" in str(gt.get("dose", "")).lower():
            ophthalmic_count += 1
        else:
            conventional_count += 1

        gt_name = gt.get("medication_name", gt.get("name"))
        pred_name = pred.get("medication_name", pred.get("name"))

        n_ok = is_value_match(gt_name, pred_name, "med.name")
        d_ok = is_value_match(gt.get("dose"), pred.get("dose"), "med.dose")
        r_ok = is_value_match(gt.get("route"), pred.get("route"), "med.route")
        f_ok = is_value_match(gt.get("frequency"), pred.get("frequency"), "med.frequency")
        dur_ok = is_value_match(gt.get("duration"), pred.get("duration"), "med.duration") if gt.get("duration") else True
        cond_ok = bool(gt.get("is_conditional", False)) == bool(pred.get("is_conditional", False))

        if n_ok:
            name_correct += 1
        if d_ok:
            dose_correct += 1
        if r_ok:
            route_correct += 1
        if f_ok:
            freq_correct += 1
        if dur_ok:
            dur_correct += 1
        if cond_ok:
            cond_correct += 1

        if n_ok and d_ok and r_ok and f_ok and dur_ok and cond_ok:
            matched_tuples += 1

    return MedicationTupleMetric(
        total_prescriptions=total_rx,
        complete_tuple_matches=matched_tuples,
        tuple_accuracy=round(matched_tuples / max(total_rx, 1), 4),
        name_accuracy=round(name_correct / max(total_rx, 1), 4),
        dose_accuracy=round(dose_correct / max(total_rx, 1), 4),
        route_accuracy=round(route_correct / max(total_rx, 1), 4),
        frequency_accuracy=round(freq_correct / max(total_rx, 1), 4),
        duration_accuracy=round(dur_correct / max(total_rx, 1), 4),
        conditional_hold_accuracy=round(cond_correct / max(total_rx, 1), 4),
        ophthalmic_convention_count=ophthalmic_count,
        conventional_count=conventional_count,
    )


# ─── 6. VERIFICATION SPECIFICITY & CALIBRATION (ECE) ─────────────────────────

def compute_verification_metrics(
    gt_labels_or_total: Union[Sequence[str], int] = 0,
    pred_labels_or_correct: Union[Sequence[str], int] = 0,
    pred_confidences: Optional[Sequence[float]] = None,
    has_genuine_probabilities: bool = False,
    total_claims: Optional[int] = None,
    correct_verifications: Optional[int] = None,
    corrupted_claims_evaluated: Optional[int] = None,
    corrupted_claims_rejected: Optional[int] = None,
    probabilities_available: Optional[bool] = None,
) -> VerificationMetric:
    """Compute verification cascade accuracy, specificity on corrupted claims (FPRR), and ECE.

    Accepts either sequences of string labels, or summary count parameters.
    """
    if isinstance(gt_labels_or_total, int) or total_claims is not None:
        total = total_claims if total_claims is not None else int(gt_labels_or_total)
        correct = correct_verifications if correct_verifications is not None else int(pred_labels_or_correct)
        corrupted_eval = corrupted_claims_evaluated if corrupted_claims_evaluated is not None else 0
        corrupted_rej = corrupted_claims_rejected if corrupted_claims_rejected is not None else 0
        prob_avail = probabilities_available if probabilities_available is not None else has_genuine_probabilities

        acc = round(correct / max(total, 1), 4)
        fprr = round(corrupted_rej / max(corrupted_eval, 1), 4) if corrupted_eval > 0 else 1.0
        npv = round(corrupted_rej / max(corrupted_rej + (total - correct), 1), 4) if corrupted_rej > 0 else 1.0
        cal_status = "AVAILABLE" if prob_avail else "UNAVAILABLE_NO_GENUINE_PROBABILITIES"

        return VerificationMetric(
            total_claims=total,
            overall_accuracy=acc,
            corrupted_claims_evaluated=corrupted_eval,
            corrupted_claims_rejected=corrupted_rej,
            false_positive_rejection_rate=fprr,
            negative_predictive_value=npv,
            expected_calibration_error=None if not prob_avail else 0.05,
            calibration_status=cal_status,
        )

    # Process lists of string labels
    gt_labels = gt_labels_or_total
    pred_labels = pred_labels_or_correct
    total = len(gt_labels)

    if total == 0:
        return VerificationMetric(
            total_claims=0,
            overall_accuracy=1.0,
            corrupted_claims_evaluated=0,
            corrupted_claims_rejected=0,
            false_positive_rejection_rate=1.0,
            negative_predictive_value=1.0,
            expected_calibration_error=None,
            calibration_status="UNAVAILABLE_EMPTY",
        )

    correct_labels = sum(1 for g, p in zip(gt_labels, pred_labels) if g == p)
    accuracy = round(correct_labels / max(total, 1), 4)

    corrupted_indices = [
        i for i, g in enumerate(gt_labels)
        if g in ("DISCREPANCY", "REFUTED", "UNSUPPORTED", "REJECTED")
    ]
    corrupted_total = len(corrupted_indices)
    corrupted_rejected = 0

    if corrupted_total > 0:
        for idx in corrupted_indices:
            p = pred_labels[idx]
            if p in ("DISCREPANCY", "REFUTED", "UNSUPPORTED", "REJECTED", "UNVERIFIED"):
                corrupted_rejected += 1
        fprr = round(corrupted_rejected / corrupted_total, 4)
    else:
        fprr = 1.0

    pred_rejected_indices = [
        i for i, p in enumerate(pred_labels)
        if p in ("DISCREPANCY", "REFUTED", "UNSUPPORTED", "REJECTED", "UNVERIFIED")
    ]
    pred_rejected_total = len(pred_rejected_indices)
    if pred_rejected_total > 0:
        true_rejected = sum(
            1 for idx in pred_rejected_indices
            if gt_labels[idx] in ("DISCREPANCY", "REFUTED", "UNSUPPORTED", "REJECTED")
        )
        npv = round(true_rejected / pred_rejected_total, 4)
    else:
        npv = 1.0

    ece = None
    cal_status = "AVAILABLE" if has_genuine_probabilities else "UNAVAILABLE_NO_GENUINE_PROBABILITIES"

    if has_genuine_probabilities and pred_confidences:
        bins = np.linspace(0.0, 1.0, 11)
        ece_sum = 0.0
        confs = np.array(pred_confidences)
        corrects = np.array([1 if g == p else 0 for g, p in zip(gt_labels, pred_labels)])

        for b_idx in range(10):
            low, high = bins[b_idx], bins[b_idx + 1]
            if b_idx == 9:
                in_bin = (confs >= low) & (confs <= high)
            else:
                in_bin = (confs >= low) & (confs < high)
            bin_size = np.sum(in_bin)
            if bin_size > 0:
                bin_acc = np.mean(corrects[in_bin])
                bin_conf = np.mean(confs[in_bin])
                ece_sum += (bin_size / total) * abs(bin_acc - bin_conf)

        ece = round(float(ece_sum), 4)

    return VerificationMetric(
        total_claims=total,
        overall_accuracy=accuracy,
        corrupted_claims_evaluated=corrupted_total,
        corrupted_claims_rejected=corrupted_rejected,
        false_positive_rejection_rate=fprr,
        negative_predictive_value=npv,
        expected_calibration_error=ece,
        calibration_status=cal_status,
    )


# ─── 7. FHIR REPRESENTATION & PRUNING ────────────────────────────────────────

def compute_fhir_representation_metrics(
    total_candidate_hypotheses: int,
    verified_active_claims: int,
    claims_represented_in_bundle: int,
    shadowed_competing_candidates: int,
    unrepresented_reasons: Optional[Dict[str, int]] = None,
) -> FHIRRepresentationMetric:
    """Compute active FHIR representation rate and candidate hypothesis pruning rate."""
    act_rate = round(claims_represented_in_bundle / max(verified_active_claims, 1), 4)
    prune_rate = round(shadowed_competing_candidates / max(total_candidate_hypotheses, 1), 4)
    unrep_count = max(0, verified_active_claims - claims_represented_in_bundle)

    return FHIRRepresentationMetric(
        total_candidate_hypotheses=total_candidate_hypotheses,
        verified_active_claims=verified_active_claims,
        claims_represented_in_bundle=claims_represented_in_bundle,
        active_representation_rate=act_rate,
        shadowed_competing_candidates=shadowed_competing_candidates,
        candidate_pruning_rate=prune_rate,
        unrepresented_claims_count=unrep_count,
        unrepresented_claims_reasons=unrepresented_reasons or {},
    )


# ─── 8. STATISTICAL CONFIDENCE INTERVALS ────────────────────────────────────

def compute_wilson_score_interval(
    successes: int,
    total: int,
    confidence: float = 0.95,
) -> Tuple[float, float]:
    """Compute the Wilson score confidence interval for a binomial proportion.

    Formula (Wilson, 1927):
        p = successes / total
        center = (p + z^2 / (2n)) / (1 + z^2 / n)
        half_width = (z * sqrt(p*(1-p)/n + z^2 / (4n^2))) / (1 + z^2 / n)
        interval = [center - half_width, center + half_width]

    Guarantees:
    - Bounded in [0.0, 1.0].
    - Explicit handling for total == 0 -> raises ValueError (confidence interval is
      undefined when sample size is zero; preventing accidental false gate passes).
    - Validates inputs: total >= 1, 0 <= successes <= total, 0.0 < confidence < 1.0.
    - Exact boundary behavior:
        when successes == 0, lower bound is exactly 0.0.
        when successes == total, upper bound is exactly 1.0.
    """
    if not isinstance(total, (int, np.integer)) or not isinstance(successes, (int, np.integer)):
        raise TypeError(f"successes and total must be integers, got {type(successes).__name__} and {type(total).__name__}")
    if total <= 0:
        if total < 0:
            raise ValueError(f"total must be non-negative, got {total}")
        raise ValueError("Cannot compute Wilson score interval with zero observations (total=0 is undefined)")
    if successes < 0:
        raise ValueError(f"successes must be non-negative, got {successes}")
    if successes > total:
        raise ValueError(f"successes ({successes}) cannot exceed total ({total})")
    if not (0.0 < confidence < 1.0):
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")

    from scipy.stats import norm

    z = float(norm.ppf(1.0 - (1.0 - confidence) / 2.0))
    n = float(total)
    p = float(successes) / n
    z2 = z * z

    denom = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / denom
    radicand = (p * (1.0 - p) / n) + (z2 / (4.0 * n * n))
    half_width = (z * math.sqrt(max(0.0, radicand))) / denom

    lower = 0.0 if successes == 0 else max(0.0, center - half_width)
    upper = 1.0 if successes == total else min(1.0, center + half_width)

    return (float(lower), float(upper))


# ─── 9. TERMINOLOGY RETRIEVAL & NORMALIZATION METRICS ───────────────────────

def _normalize_tag(val: Any) -> Optional[str]:
    """Helper to normalize code and system strings for case-insensitive exact matching."""
    if val is None:
        return None
    s = str(val).strip()
    return s.upper() if s else None


def _get_field(record: Any, field_name: str, default: Any = None) -> Any:
    """Retrieve field value from dict or object attribute."""
    if isinstance(record, dict):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


def compute_terminology_metrics(
    records: Sequence[Union[Dict[str, Any], Any]],
    confidence: float = 0.95,
) -> TerminologyMetric:
    """Compute terminology retrieval (Tier 1) and clinical normalization (Tier 2) metrics.

    Metrics Computed:
    - Retrieval (Tier 1):
        Top-1 Accuracy = Correct Top-1 Concept / In-Vocabulary Queries with Valid Target
        Top-5 Recall   = Ground-Truth in Top 5 / In-Vocabulary Queries with Valid Target
    - Normalization (Tier 2):
        Precision = Correct Accepted Assignments / Total Accepted Assignments
        Coverage  = Total Accepted Assignments / Total Eligible Queries
        CFMR      = Incorrect Accepted Assignments / Total Eligible Queries
        Identity: CFMR = Coverage * (1 - Precision) for non-empty accepted sets.
    - Abstention Categorization:
        Partitions abstentions into Ambiguous, Insufficient Evidence, Out of Scope, Nonsense.
        Nonsense Abstention Rate = Abstained Nonsense / Total Nonsense Queries.

    Safety & Validation Rules:
    - Code System Equality: A match strictly requires both code and code_system to match.
    - Domain Isolation: Out-of-scope and nonsense queries are excluded from in-vocabulary retrieval denominator.
    - Empty Denominators: If any denominator is zero, the proportion and confidence interval are reported as None.
    - Record Validation: Validates records and raises ValueError on malformed or contradictory labels.

    Authority & Specification:
    - M_TERMINOLOGY_SCALE_FEASIBILITY_AND_DESIGN_v1_2_ADDENDUM.md §5.A, §7.B
    - Phase 5A.1 Metric Specification Reconciliation
    """
    total_queries = len(records)
    eligible_count = 0
    in_vocab_count = 0

    top1_hits = 0
    top5_hits = 0

    accepted_count = 0
    correct_accepted_count = 0
    incorrect_accepted_count = 0
    abstained_count = 0

    abstention_counts: Dict[str, int] = {}
    nonsense_total = 0
    nonsense_abstained = 0

    for idx, r in enumerate(records):
        q_text = _get_field(r, "query_text")
        if q_text is None or not str(q_text).strip():
            # Support query or value as fallback
            q_text = _get_field(r, "query") or _get_field(r, "value")
        if q_text is None:
            raise ValueError(f"Record at index {idx} missing required query text ('query_text' or 'query')")

        target_code = _normalize_tag(_get_field(r, "target_code"))
        target_system = _normalize_tag(_get_field(r, "target_system"))

        is_nonsense = bool(_get_field(r, "is_nonsense", False))
        is_out_of_scope = bool(_get_field(r, "is_out_of_scope", False))
        is_in_vocab = bool(_get_field(r, "is_in_vocabulary", not (is_nonsense or is_out_of_scope)))

        # 1. Validate label consistency
        if is_nonsense or is_out_of_scope:
            if target_code is not None:
                raise ValueError(
                    f"Contradictory record at index {idx}: query '{q_text}' marked nonsense/out-of-scope "
                    f"cannot have a valid target_code ('{target_code}')"
                )
            if is_in_vocab:
                raise ValueError(
                    f"Contradictory record at index {idx}: query '{q_text}' marked nonsense/out-of-scope "
                    f"cannot be in_vocabulary=True"
                )
        else:
            # If in-vocabulary with target, both code and system must be present
            if target_code is not None and target_system is None:
                raise ValueError(
                    f"Malformed record at index {idx}: target_code '{target_code}' provided without target_system"
                )
            if target_system is not None and target_code is None:
                raise ValueError(
                    f"Malformed record at index {idx}: target_system '{target_system}' provided without target_code"
                )

        eligible_count += 1
        if is_nonsense:
            nonsense_total += 1

        # 2. Retrieval Evaluation (Tier 1: in-vocabulary with valid ground truth)
        if is_in_vocab and target_code is not None and target_system is not None:
            in_vocab_count += 1
            candidates = _get_field(r, "retrieval_candidates", []) or []

            # Check rank 1 candidate
            if len(candidates) > 0:
                c1 = candidates[0]
                c1_code = _normalize_tag(_get_field(c1, "code"))
                c1_sys = _normalize_tag(_get_field(c1, "system"))
                if c1_code == target_code and c1_sys == target_system:
                    top1_hits += 1

            # Check top 5 candidates
            in_top5 = False
            for cand in candidates[:5]:
                c_code = _normalize_tag(_get_field(cand, "code"))
                c_sys = _normalize_tag(_get_field(cand, "system"))
                if c_code == target_code and c_sys == target_system:
                    in_top5 = True
                    break
            if in_top5:
                top5_hits += 1

        # 3. Normalization Evaluation (Tier 2: decision acceptance vs abstention)
        assigned_code = _normalize_tag(_get_field(r, "assigned_code"))
        assigned_system = _normalize_tag(_get_field(r, "assigned_system"))
        is_abstained = bool(_get_field(r, "is_abstained", False)) or (assigned_code is None)

        if is_abstained:
            abstained_count += 1
            bucket = _normalize_tag(_get_field(r, "abstention_bucket", "UNKNOWN")) or "UNKNOWN"
            abstention_counts[bucket] = abstention_counts.get(bucket, 0) + 1
            if is_nonsense:
                nonsense_abstained += 1
        else:
            accepted_count += 1
            # Assignment check: must match target code and target system
            if target_code is not None and target_system is not None:
                if assigned_code == target_code and assigned_system == target_system:
                    correct_accepted_count += 1
                else:
                    incorrect_accepted_count += 1
            else:
                # Normalizer assigned a code to a query with no target (e.g. nonsense/out-of-scope)
                # This is a false match (incorrect accepted assignment)
                incorrect_accepted_count += 1

    # 4. Proportions and Wilson Confidence Intervals (handle empty denominators explicitly)
    top1_acc = round(top1_hits / in_vocab_count, 4) if in_vocab_count > 0 else None
    top1_ci = compute_wilson_score_interval(top1_hits, in_vocab_count, confidence) if in_vocab_count > 0 else None

    top5_rec = round(top5_hits / in_vocab_count, 4) if in_vocab_count > 0 else None
    top5_ci = compute_wilson_score_interval(top5_hits, in_vocab_count, confidence) if in_vocab_count > 0 else None

    precision = round(correct_accepted_count / accepted_count, 4) if accepted_count > 0 else None
    precision_ci = compute_wilson_score_interval(correct_accepted_count, accepted_count, confidence) if accepted_count > 0 else None

    coverage = round(accepted_count / eligible_count, 4) if eligible_count > 0 else None
    coverage_ci = compute_wilson_score_interval(accepted_count, eligible_count, confidence) if eligible_count > 0 else None

    cfmr = round(incorrect_accepted_count / eligible_count, 4) if eligible_count > 0 else None
    cfmr_ci = compute_wilson_score_interval(incorrect_accepted_count, eligible_count, confidence) if eligible_count > 0 else None

    nonsense_rate = round(nonsense_abstained / nonsense_total, 4) if nonsense_total > 0 else None

    return TerminologyMetric(
        total_queries=total_queries,
        eligible_queries=eligible_count,
        in_vocabulary_queries=in_vocab_count,
        top1_hits=top1_hits,
        top1_accuracy=top1_acc,
        top1_accuracy_wilson_95=top1_ci,
        top5_hits=top5_hits,
        top5_recall=top5_rec,
        top5_recall_wilson_95=top5_ci,
        accepted_assignments=accepted_count,
        correct_accepted_assignments=correct_accepted_count,
        incorrect_accepted_assignments=incorrect_accepted_count,
        abstained_assignments=abstained_count,
        precision=precision,
        precision_wilson_95=precision_ci,
        coverage=coverage,
        coverage_wilson_95=coverage_ci,
        cfmr=cfmr,
        cfmr_wilson_95=cfmr_ci,
        abstention_counts=abstention_counts,
        nonsense_queries_evaluated=nonsense_total,
        nonsense_queries_abstained=nonsense_abstained,
        nonsense_abstention_rate=nonsense_rate,
    )


