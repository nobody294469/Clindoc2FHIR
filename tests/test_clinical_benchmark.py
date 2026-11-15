"""Comprehensive test suite for M-BENCHMARK: Clinical Document Quality & Reliability Evaluation.

Validates:
1. Exact mathematical metric implementations:
   - Character Error Rate (CER) and Word Error Rate (WER)
   - Strict numeric equivalence (absolute <= 1e-4, relative <= 0.1%)
   - String normalization across clinical domains (routes, flags, durations)
   - 1-to-1 maximum weight bipartite matching via Scipy linear_sum_assignment
   - Spatial grounding bounding box IoU and exact character-span tracking
   - Medication slot association and complete tuple completeness (REQ-44)
   - Verification cascade specificity (FPRR) and uncalibrated ECE handling
   - FHIR candidate hypothesis pruning vs active representation fidelity
2. In-memory execution safety & isolation (zero DB writes, zero outbox dispatch)
3. Quality gate threshold enforcement
4. CLI eval subcommand integration
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from clindoc.eval.metrics import (
    compute_cer,
    compute_wer,
    is_numeric_equivalent,
    normalize_entity_string,
    is_value_match,
    compute_field_metrics_bipartite,
    compute_box_iou,
    compute_spatial_grounding_metrics,
    compute_medication_tuple_metrics,
    compute_verification_metrics,
    compute_fhir_representation_metrics,
    compute_wilson_score_interval,
    compute_terminology_metrics,
)
from clindoc.eval.schema import (
    BenchmarkReport,
    EnvironmentMetadata,
    FieldMetric,
    GateCheckResult,
    TerminologyMetric,
)
from clindoc.eval.gates import evaluate_gates
from clindoc.eval.harness import (
    get_environment_metadata,
    run_clinical_benchmark,
)
from clindoc.cli import main


# ─── 1. MATHEMATICAL METRIC TESTS ───────────────────────────────────────────

def test_cer_and_wer_exact_and_degraded():
    # Identical strings
    assert compute_cer("Metabolic Panel 14.0", "Metabolic Panel 14.0") == 0.0
    assert compute_wer("Metabolic Panel 14.0", "Metabolic Panel 14.0") == 0.0

    # Empty strings
    assert compute_cer("", "") == 0.0
    assert compute_wer("", "") == 0.0

    # Degraded / OCR noise
    ref = "Hemoglobin 14.0 g/dL"
    hyp = "Hem0gl0bin 14.0 g/dL"  # 2 char substitutions
    cer = compute_cer(ref, hyp)
    assert 0.0 < cer < 0.20
    assert compute_wer(ref, hyp) == pytest.approx(1 / 3, 0.01)


def test_numeric_equivalence_tolerances():
    # Exact float and int match
    assert is_numeric_equivalent(14.0, 14) is True
    assert is_numeric_equivalent("14.0", "14") is True

    # Absolute tolerance <= 1e-4
    assert is_numeric_equivalent(0.00501, 0.00505, abs_tol=1e-4) is True
    assert is_numeric_equivalent(0.005, 0.006, abs_tol=1e-4) is False

    # Relative tolerance <= 0.1%
    assert is_numeric_equivalent(1000.0, 1000.5, rel_tol=0.001) is True
    assert is_numeric_equivalent(1000.0, 1005.0, rel_tol=0.001) is False

    # Non-numeric gracefully handled (not numeric equivalent, but matches via is_value_match)
    assert is_numeric_equivalent("Normal", 10.0) is False
    assert is_numeric_equivalent("Normal", "Normal") is False
    assert is_value_match("Normal", "Normal", "lab.flag") is True


def test_normalize_entity_string():
    # Route mapping
    assert normalize_entity_string("po", "med.route") == "oral"
    assert normalize_entity_string("by mouth", "med.route") == "oral"
    assert normalize_entity_string("IV", "med.route") == "intravenous"
    assert normalize_entity_string("subling", "med.route") == "sublingual"

    # Flag mapping
    assert normalize_entity_string("HIGH", "lab.flag") == "h"
    assert normalize_entity_string("LOW", "lab.flag") == "l"
    assert normalize_entity_string("NORMAL", "lab.flag") == "norm"

    # Duration normalization
    assert normalize_entity_string("for 7 days", "med.duration") == "7 days"
    assert normalize_entity_string("x 10 days", "med.duration") == "10 days"
    assert normalize_entity_string("duration: 14 days", "med.duration") == "14 days"


def test_bipartite_matching_prevents_greedy_bias_and_double_counting():
    """Verify Scipy linear_sum_assignment matches 1-to-1 without double counting."""
    # 2 Ground Truth claims with same test name
    gt_claims = [
        {"document_id": "D1", "field_type": "lab.value", "value": "10.0", "value_numeric": 10.0, "group_id": "G1"},
        {"document_id": "D1", "field_type": "lab.value", "value": "20.0", "value_numeric": 20.0, "group_id": "G2"},
    ]
    # Prediction only predicted "10.0" once
    pred_claims = [
        {"document_id": "D1", "field_type": "lab.value", "value": "10.0", "value_numeric": 10.0, "group_id": "G1"},
    ]

    metrics, macro_f1, micro_f1 = compute_field_metrics_bipartite(
        gt_claims, pred_claims, ["lab.value"]
    )
    val_metric = metrics["lab.value"]

    # Must be 1 TP, 0 FP, 1 FN (NOT 2 TP via double-counting)
    assert val_metric.true_positives == 1
    assert val_metric.false_positives == 0
    assert val_metric.false_negatives == 1
    assert val_metric.precision == 1.0
    assert val_metric.recall == 0.5
    assert val_metric.f1_score == pytest.approx(0.6667, 0.001)


def test_box_iou_computation():
    box_a = [10.0, 10.0, 50.0, 50.0]
    box_b = [10.0, 10.0, 50.0, 50.0]
    assert compute_box_iou(box_a, box_b) == pytest.approx(1.0, 1e-5)

    # Disjoint boxes
    box_c = [100.0, 100.0, 150.0, 150.0]
    assert compute_box_iou(box_a, box_c) == 0.0

    # 50% overlap box
    box_d = [10.0, 10.0, 30.0, 50.0]  # half width
    # intersection = 20 * 40 = 800, union = 1600
    assert compute_box_iou(box_a, box_d) == pytest.approx(0.50, 1e-5)


def test_spatial_grounding_unmatched_gt_penalized():
    """Verify that unmatched ground truth entities are penalized in IoU >= 0.50 rate."""
    gt_entities = [
        {"document_id": "D1", "bbox": [10, 10, 50, 50], "char_start": 0, "char_end": 10},
        {"document_id": "D1", "bbox": [100, 100, 150, 150], "char_start": 50, "char_end": 60},
    ]
    # Predict only the first one
    pred_entities = [
        {"document_id": "D1", "bbox": [10, 10, 50, 50], "char_start": 0, "char_end": 10},
    ]

    metric = compute_spatial_grounding_metrics(gt_entities, pred_entities)
    assert metric.total_ground_truth_entities == 2
    assert metric.iou_50_matches == 1
    # Rate must be 1 / 2 = 0.50, NOT 1 / 1 = 1.0
    assert metric.iou_50_rate == 0.50
    assert metric.exact_char_span_rate == 0.50


def test_medication_tuple_completeness_and_ophthalmic_conventions():
    gt_prescriptions = [
        {
            "document_id": "D1",
            "name": "Lisinopril",
            "dose": "40 mg",
            "route": "oral",
            "frequency": "daily",
            "duration": "30 days",
            "is_conditional": False,
        },
        {
            "document_id": "D1",
            "name": "Latanoprost",
            "dose": "0.005%",
            "route": "OU",  # Ophthalmic
            "frequency": "qhs",
            "duration": None,
            "is_conditional": False,
        },
    ]

    # Perfect prediction for Lisinopril, partial for Latanoprost
    pred_prescriptions = [
        {
            "document_id": "D1",
            "name": "Lisinopril",
            "dose": "40 mg",
            "route": "oral",
            "frequency": "daily",
            "duration": "30 days",
            "is_conditional": False,
        },
        {
            "document_id": "D1",
            "name": "Latanoprost",
            "dose": "1 drop",  # mismatch with 0.005%
            "route": "OU",
            "frequency": "qhs",
            "duration": None,
            "is_conditional": False,
        },
    ]

    tuple_metric = compute_medication_tuple_metrics(gt_prescriptions, pred_prescriptions)
    assert tuple_metric.total_prescriptions == 2
    assert tuple_metric.complete_tuple_matches == 1
    assert tuple_metric.tuple_accuracy == 0.50
    assert tuple_metric.name_accuracy == 1.0
    assert tuple_metric.ophthalmic_convention_count == 1
    assert tuple_metric.conventional_count == 1


def test_verification_metrics_fprr_and_uncalibrated_ece():
    # 100 claims, 20 corrupted. Cascade rejects 19 corrupted claims.
    v_metric = compute_verification_metrics(
        total_claims=100,
        correct_verifications=98,
        corrupted_claims_evaluated=20,
        corrupted_claims_rejected=19,
        probabilities_available=False,
    )
    assert v_metric.false_positive_rejection_rate == 0.95
    assert v_metric.expected_calibration_error is None
    assert v_metric.calibration_status == "UNAVAILABLE_NO_GENUINE_PROBABILITIES"


def test_fhir_representation_metrics_and_shadowing():
    # Out of 1,826 candidates: 1,130 verified active, 1,093 in bundle, 696 shadowed candidates
    fhir_metric = compute_fhir_representation_metrics(
        total_candidate_hypotheses=1826,
        verified_active_claims=1130,
        claims_represented_in_bundle=1093,
        shadowed_competing_candidates=696,
        unrepresented_reasons={"cascade_discrepancy": 37},
    )
    assert fhir_metric.active_representation_rate == pytest.approx(1093 / 1130, 0.001)
    assert fhir_metric.candidate_pruning_rate == pytest.approx(696 / 1826, 0.001)
    assert fhir_metric.unrepresented_claims_count == 37


def test_wilson_score_interval_exact_references_and_boundaries():
    # 1. Zero observations must raise ValueError (undefined sample size)
    with pytest.raises(ValueError, match="total=0 is undefined"):
        compute_wilson_score_interval(0, 0)

    # 2. Zero successes out of 10 (Wilson 1927 lower bound = 0.0, upper bound exact formula)
    # Upper bound = (z^2/n) / (1 + z^2/n) where z = 1.95996398454... => z^2 approx 3.841459
    # Upper = 0.3841459 / 1.3841459 approx 0.277533
    low, high = compute_wilson_score_interval(0, 10, confidence=0.95)
    assert low == 0.0
    assert high == pytest.approx(0.277533, abs=1e-4)

    # 3. All successes out of 10 (Symmetric lower bound, upper bound = 1.0)
    low, high = compute_wilson_score_interval(10, 10, confidence=0.95)
    assert low == pytest.approx(1.0 - 0.277533, abs=1e-4)
    assert high == 1.0

    # 4. Standard intermediate proportion: 50 / 100 at 95% confidence
    low, high = compute_wilson_score_interval(50, 100, confidence=0.95)
    assert low == pytest.approx(0.403832, abs=1e-4)
    assert high == pytest.approx(0.596168, abs=1e-4)
    assert (low + high) / 2.0 == pytest.approx(0.50, abs=1e-6)

    # 5. Small sample: 2 / 10 at 95% confidence
    low, high = compute_wilson_score_interval(2, 10, confidence=0.95)
    assert low == pytest.approx(0.056682, abs=1e-4)
    assert high == pytest.approx(0.509838, abs=1e-4)

    # 6. Large sample rare event: 1 / 1000 (0.10% CFMR) at 95% confidence
    low, high = compute_wilson_score_interval(1, 1000, confidence=0.95)
    assert low == pytest.approx(0.0001765, abs=1e-5)
    assert high == pytest.approx(0.0056426, abs=1e-5)

    # 7. Guaranteed bounds in [0.0, 1.0]
    for k in (0, 1, 5, 10, 20):
        l, h = compute_wilson_score_interval(k, 20)
        assert 0.0 <= l <= h <= 1.0


def test_wilson_score_interval_validation_and_confidences():
    # Input validation
    with pytest.raises(ValueError, match="total must be non-negative"):
        compute_wilson_score_interval(0, -5)

    with pytest.raises(ValueError, match="successes must be non-negative"):
        compute_wilson_score_interval(-1, 10)

    with pytest.raises(ValueError, match="cannot exceed total"):
        compute_wilson_score_interval(11, 10)

    with pytest.raises(ValueError, match="confidence must be in"):
        compute_wilson_score_interval(5, 10, confidence=0.0)

    with pytest.raises(ValueError, match="confidence must be in"):
        compute_wilson_score_interval(5, 10, confidence=1.0)

    with pytest.raises(TypeError, match="must be integers"):
        compute_wilson_score_interval("5", 10)  # type: ignore

    # Nested confidence interval widths: 90% < 95% < 99%
    low_90, high_90 = compute_wilson_score_interval(50, 100, confidence=0.90)
    low_95, high_95 = compute_wilson_score_interval(50, 100, confidence=0.95)
    low_99, high_99 = compute_wilson_score_interval(50, 100, confidence=0.99)

    width_90 = high_90 - low_90
    width_95 = high_95 - low_95
    width_99 = high_99 - low_99

    assert width_90 < width_95 < width_99
    assert low_99 < low_95 < low_90
    assert high_90 < high_95 < high_99


def test_terminology_metrics_hits_misses_precision_and_cfmr_identity():
    # 10 queries:
    # - 6 clean in-vocab, correct accepted (hits top-1, top-5)
    # - 1 clean in-vocab, incorrect accepted (c1 wrong, assigned wrong => CFMR error)
    # - 1 clean in-vocab, top-1 miss but top-5 hit, abstained (insufficient evidence)
    # - 1 clean in-vocab, ambiguous abstained
    # - 1 nonsense query, correctly abstained (nonsense bucket)
    records = [
        # 1-6: Correct accepted
        {
            "query_text": "Potassium",
            "target_code": "2823-3",
            "target_system": "LOINC",
            "retrieval_candidates": [{"code": "2823-3", "system": "LOINC", "score": 1.0}],
            "assigned_code": "2823-3",
            "assigned_system": "LOINC",
            "is_abstained": False,
        },
        {
            "query_text": "Sodium",
            "target_code": "2951-2",
            "target_system": "LOINC",
            "retrieval_candidates": [{"code": "2951-2", "system": "LOINC", "score": 1.0}],
            "assigned_code": "2951-2",
            "assigned_system": "LOINC",
            "is_abstained": False,
        },
        {
            "query_text": "Glucose",
            "target_code": "2345-7",
            "target_system": "LOINC",
            "retrieval_candidates": [{"code": "2345-7", "system": "LOINC", "score": 1.0}],
            "assigned_code": "2345-7",
            "assigned_system": "LOINC",
            "is_abstained": False,
        },
        {
            "query_text": "Metformin",
            "target_code": "6809",
            "target_system": "RxNorm",
            "retrieval_candidates": [{"code": "6809", "system": "RxNorm", "score": 1.0}],
            "assigned_code": "6809",
            "assigned_system": "RxNorm",
            "is_abstained": False,
        },
        {
            "query_text": "Amoxicillin",
            "target_code": "723",
            "target_system": "RxNorm",
            "retrieval_candidates": [{"code": "723", "system": "RxNorm", "score": 1.0}],
            "assigned_code": "723",
            "assigned_system": "RxNorm",
            "is_abstained": False,
        },
        {
            "query_text": "Omeprazole",
            "target_code": "7646",
            "target_system": "RxNorm",
            "retrieval_candidates": [{"code": "7646", "system": "RxNorm", "score": 1.0}],
            "assigned_code": "7646",
            "assigned_system": "RxNorm",
            "is_abstained": False,
        },
        # 7: Incorrect accepted (CFMR error: assigned wrong drug CUI 9999)
        {
            "query_text": "Lisinopril",
            "target_code": "29046",
            "target_system": "RxNorm",
            "retrieval_candidates": [{"code": "9999", "system": "RxNorm", "score": 0.9}],
            "assigned_code": "9999",
            "assigned_system": "RxNorm",
            "is_abstained": False,
        },
        # 8: In-vocab abstained (missing context; top-1 miss, but top-5 hit)
        {
            "query_text": "K",
            "target_code": "2823-3",
            "target_system": "LOINC",
            "retrieval_candidates": [
                {"code": "2828-2", "system": "LOINC", "score": 0.95},
                {"code": "2823-3", "system": "LOINC", "score": 0.90},
            ],
            "assigned_code": None,
            "assigned_system": None,
            "is_abstained": True,
            "abstention_bucket": "INSUFFICIENT_EVIDENCE",
        },
        # 9: In-vocab ambiguous abstained
        {
            "query_text": "Calcium",
            "target_code": "17861-6",
            "target_system": "LOINC",
            "retrieval_candidates": [
                {"code": "2000-8", "system": "LOINC", "score": 0.85},
                {"code": "17861-6", "system": "LOINC", "score": 0.85},
            ],
            "assigned_code": None,
            "assigned_system": None,
            "is_abstained": True,
            "abstention_bucket": "AMBIGUOUS",
        },
        # 10: Nonsense query (OOV gibberish, not in vocab, abstained)
        {
            "query_text": "XYZ999FOOBAR",
            "target_code": None,
            "target_system": None,
            "is_in_vocabulary": False,
            "is_nonsense": True,
            "retrieval_candidates": [],
            "assigned_code": None,
            "assigned_system": None,
            "is_abstained": True,
            "abstention_bucket": "NONSENSE",
        },
    ]

    metric = compute_terminology_metrics(records)

    # 1. Counts verification
    assert metric.total_queries == 10
    assert metric.eligible_queries == 10
    assert metric.in_vocabulary_queries == 9  # Query 10 is nonsense
    assert metric.top1_hits == 6              # Queries 1-6 hit top-1
    assert metric.top5_hits == 8              # Queries 1-6 + Query 8 hit top-5
    assert metric.accepted_assignments == 7   # Queries 1-7 accepted
    assert metric.correct_accepted_assignments == 6
    assert metric.incorrect_accepted_assignments == 1  # Query 7 is false match
    assert metric.abstained_assignments == 3

    # 2. Metric proportions
    assert metric.top1_accuracy == pytest.approx(6 / 9, 0.001)
    assert metric.top5_recall == pytest.approx(8 / 9, 0.001)
    assert metric.precision == pytest.approx(6 / 7, 0.001)
    assert metric.coverage == pytest.approx(7 / 10, 0.001)
    assert metric.cfmr == pytest.approx(1 / 10, 0.001)

    # 3. Mathematical Identity: CFMR = Coverage * (1 - Precision)
    expected_cfmr = metric.coverage * (1.0 - metric.precision)
    assert metric.cfmr == pytest.approx(expected_cfmr, abs=1e-4)

    # 4. Wilson intervals present and bounded
    assert metric.cfmr_wilson_95 is not None
    cfmr_low, cfmr_high = metric.cfmr_wilson_95
    assert 0.0 <= cfmr_low <= metric.cfmr <= cfmr_high <= 1.0

    # 5. Abstention buckets
    assert metric.abstention_counts["INSUFFICIENT_EVIDENCE"] == 1
    assert metric.abstention_counts["AMBIGUOUS"] == 1
    assert metric.abstention_counts["NONSENSE"] == 1
    assert metric.nonsense_queries_evaluated == 1
    assert metric.nonsense_queries_abstained == 1
    assert metric.nonsense_abstention_rate == 1.0


def test_terminology_metrics_code_system_mismatch_fails_closed():
    # Assigned code matches "2823-3", but code system is CDCI instead of LOINC!
    record = {
        "query_text": "Potassium",
        "target_code": "2823-3",
        "target_system": "LOINC",
        "retrieval_candidates": [{"code": "2823-3", "system": "CDCI", "score": 1.0}],
        "assigned_code": "2823-3",
        "assigned_system": "CDCI",  # MISMATCH
        "is_abstained": False,
    }
    metric = compute_terminology_metrics([record])
    assert metric.accepted_assignments == 1
    assert metric.correct_accepted_assignments == 0
    assert metric.incorrect_accepted_assignments == 1
    assert metric.precision == 0.0
    assert metric.cfmr == 1.0
    assert metric.top1_hits == 0


def test_terminology_metrics_empty_denominators_handled_explicitly():
    # 1. Zero accepted assignments (all abstained): precision is None, NOT 0% or 100%
    all_abstained = [
        {
            "query_text": "K",
            "target_code": "2823-3",
            "target_system": "LOINC",
            "assigned_code": None,
            "assigned_system": None,
            "is_abstained": True,
            "abstention_bucket": "INSUFFICIENT_EVIDENCE",
        }
    ]
    m_abs = compute_terminology_metrics(all_abstained)
    assert m_abs.accepted_assignments == 0
    assert m_abs.precision is None
    assert m_abs.precision_wilson_95 is None
    assert m_abs.coverage == 0.0
    assert m_abs.cfmr == 0.0

    # 2. Zero eligible queries: all proportions are None
    m_empty = compute_terminology_metrics([])
    assert m_empty.total_queries == 0
    assert m_empty.eligible_queries == 0
    assert m_empty.top1_accuracy is None
    assert m_empty.precision is None
    assert m_empty.coverage is None
    assert m_empty.cfmr is None


def test_terminology_metrics_validation_rejects_malformed_records():
    # Missing query text
    with pytest.raises(ValueError, match="missing required query text"):
        compute_terminology_metrics([{"target_code": "123", "target_system": "LOINC"}])

    # Contradictory: nonsense but provides target_code
    with pytest.raises(ValueError, match="marked nonsense/out-of-scope cannot have a valid target_code"):
        compute_terminology_metrics([
            {
                "query_text": "bad",
                "is_nonsense": True,
                "target_code": "2823-3",
            }
        ])

    # In-vocabulary with code but missing target_system
    with pytest.raises(ValueError, match="provided without target_system"):
        compute_terminology_metrics([
            {
                "query_text": "potassium",
                "target_code": "2823-3",
                "target_system": None,
            }
        ])


# ─── 2. IN-MEMORY ISOLATION & PROVENANCE TESTS ──────────────────────────────

def test_environment_metadata_capture():
    env = get_environment_metadata()
    assert env.python_version != ""
    assert env.platform != ""
    assert env.is_isolated_in_memory is True
    assert env.deterministic_execution is True
    assert env.tessdata_hashes is not None


def test_benchmark_runner_tier1_smoke():
    """Verify tier1 smoke benchmark executes in-memory with zero DB writes."""
    report = run_clinical_benchmark(tier="tier1")

    assert report.benchmark_version == "1.0.0"
    assert "synthetic_labs" in report.datasets
    assert "synthetic_meds" in report.datasets
    assert "synthetic_narrative" in report.datasets
    assert "degraded_scans" in report.datasets
    assert "real_world_glaucoma" in report.datasets
    assert "expanded_multi_specialty" in report.datasets

    # Verify synthetic labs quality
    labs = report.datasets["synthetic_labs"]
    assert labs.macro_f1 >= 0.85
    assert labs.field_metrics["lab.value"].f1_score >= 0.95

    # Verify gates were evaluated
    assert len(report.gate_checks) > 0
    assert report.overall_gates_passed is True


# ─── 3. QUALITY GATE EVALUATION TESTS ────────────────────────────────────────

def test_gate_evaluator_passes_and_fails():
    report = run_clinical_benchmark(tier="tier1")

    # Test with standard config
    checks, overall_passed = evaluate_gates(report, gate_config_path=Path("configs/eval_gates.json"))
    assert overall_passed is True
    assert all(c.passed for c in checks)

    # Test with impossible threshold config to verify fail detection
    strict_gates = {
        "version": "1.0.0",
        "gates": [
            {
                "metric_name": "Impossible Macro F1 Gate",
                "target_threshold": 0.9999,
                "dataset_name": "synthetic_labs",
                "path": "macro_f1",
                "comparison": "gte",
            }
        ],
    }
    strict_checks, strict_passed = evaluate_gates(report, gate_config_dict=strict_gates)
    assert strict_passed is False
    assert strict_checks[0].passed is False


# ─── 4. CLI INTEGRATION TESTS ────────────────────────────────────────────────

def test_cli_eval_run_and_verify(tmp_path: Path):
    out_file = tmp_path / "test_report.json"

    # Test clindoc eval run --tier tier1 -o ...
    ret = main(["eval", "run", "--tier", "tier1", "--output", str(out_file)])
    assert ret == 0
    assert out_file.exists()

    with open(out_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["benchmark_version"] == "1.0.0"

    # Test clindoc eval verify --report ...
    ret_verify = main(["eval", "verify", "--report", str(out_file)])
    assert ret_verify == 0
