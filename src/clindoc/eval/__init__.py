"""ClinicalDoc Evaluation Subsystem (M-BENCHMARK).

Unified Clinical Document Quality & Reliability Evaluation system providing:
- Rigorous mathematical evaluation schemas (EnvironmentMetadata, FieldMetric, BenchmarkReport)
- Bipartite matching with Scipy linear_sum_assignment (preventing greedy bias and double counting)
- True positive, false positive, false negative, precision, recall, F1, exact match
- Medication slot association and full tuple completeness metrics (REQ-44)
- Spatial grounding with exact bbox IoU and character-span tracking
- Verification cascade calibration and specificity (corrupted-claim rejection rate)
- FHIR representation rate and candidate hypothesis pruning rate
- Threshold gating engine for quality gate enforcement
- In-memory isolated harness runner
"""
from clindoc.eval.schema import (
    EnvironmentMetadata,
    DatasetProvenance,
    FieldMetric,
    MedicationTupleMetric,
    SpatialGroundingMetric,
    VerificationMetric,
    FHIRRepresentationMetric,
    TerminologyMetric,
    DatasetEvaluationResult,
    GateCheckResult,
    BenchmarkReport,
)
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
from clindoc.eval.gates import evaluate_gates
from clindoc.eval.harness import run_clinical_benchmark, get_environment_metadata

__all__ = [
    "EnvironmentMetadata",
    "DatasetProvenance",
    "FieldMetric",
    "MedicationTupleMetric",
    "SpatialGroundingMetric",
    "VerificationMetric",
    "FHIRRepresentationMetric",
    "TerminologyMetric",
    "DatasetEvaluationResult",
    "GateCheckResult",
    "BenchmarkReport",
    "compute_cer",
    "compute_wer",
    "is_numeric_equivalent",
    "normalize_entity_string",
    "is_value_match",
    "compute_field_metrics_bipartite",
    "compute_box_iou",
    "compute_spatial_grounding_metrics",
    "compute_medication_tuple_metrics",
    "compute_verification_metrics",
    "compute_fhir_representation_metrics",
    "compute_wilson_score_interval",
    "compute_terminology_metrics",
    "evaluate_gates",
    "run_clinical_benchmark",
    "get_environment_metadata",
]
