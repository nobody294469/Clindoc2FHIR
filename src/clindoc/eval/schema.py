"""Pydantic schemas for ClinicalDoc M-BENCHMARK evaluation system.

Defines schemas for:
- Environment and provenance metadata (Git SHA, Tesseract version, model digests, dataset hashes).
- Field-level clinical entity extraction metrics.
- Medication-specific slot and tuple association metrics.
- Spatial grounding and character-span metrics.
- Verification cascade calibration and specificity metrics.
- FHIR representation and slot-pruning metrics.
- Comprehensive BenchmarkReport.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class EnvironmentMetadata(BaseModel):
    """Runtime and provenance metadata captured during evaluation."""
    timestamp: str
    git_commit_sha: Optional[str] = None
    tesseract_version: Optional[str] = None
    tessdata_hashes: Dict[str, str] = Field(default_factory=dict)
    model_digests: Dict[str, str] = Field(default_factory=dict)
    python_version: str
    platform: str
    is_isolated_in_memory: bool = True
    deterministic_execution: bool = True


class DatasetProvenance(BaseModel):
    """Provenance and licensing metadata for a benchmark split."""
    dataset_name: str
    dataset_type: str  # "synthetic", "degraded_synthetic", "real_world"
    license: str
    license_url_or_ref: str
    manifest_hash: Optional[str] = None
    document_count: int
    claim_count: int
    is_synthetic: bool
    clinical_specialties: List[str] = Field(default_factory=list)


class FieldMetric(BaseModel):
    """Extraction precision, recall, and F1 for an individual clinical field."""
    field_type: str
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1_score: float
    exact_matches: int
    exact_match_rate: float
    ground_truth_count: int
    prediction_count: int


class MedicationTupleMetric(BaseModel):
    """Medication slot association and tuple completeness metrics (REQ-44)."""
    total_prescriptions: int
    complete_tuple_matches: int
    tuple_accuracy: float
    name_accuracy: float
    dose_accuracy: float
    route_accuracy: float
    frequency_accuracy: float
    duration_accuracy: float
    conditional_hold_accuracy: float
    ophthalmic_convention_count: int = 0
    conventional_count: int = 0


class SpatialGroundingMetric(BaseModel):
    """Spatial bounding box and character span grounding metrics."""
    total_ground_truth_entities: int
    evaluated_entities_count: int
    iou_50_matches: int
    iou_50_rate: float  # Unmatched GT count as failures
    mean_iou_matched: float
    exact_char_span_matches: int
    exact_char_span_rate: float
    partial_char_overlap_matches: int
    partial_char_overlap_rate: float


class VerificationMetric(BaseModel):
    """Verification cascade calibration and specificity metrics."""
    total_claims: int
    overall_accuracy: float
    corrupted_claims_evaluated: int
    corrupted_claims_rejected: int
    false_positive_rejection_rate: float  # Specificity against corrupted claims
    negative_predictive_value: float
    expected_calibration_error: Optional[float] = None
    calibration_status: str = "AVAILABLE"  # "AVAILABLE" or "UNAVAILABLE_NO_GENUINE_PROBABILITIES"


class FHIRRepresentationMetric(BaseModel):
    """FHIR representation and candidate slot-pruning evaluation."""
    total_candidate_hypotheses: int
    verified_active_claims: int
    claims_represented_in_bundle: int
    active_representation_rate: float  # claims_represented / verified_active_claims
    shadowed_competing_candidates: int
    candidate_pruning_rate: float  # shadowed / total_candidate_hypotheses
    unrepresented_claims_count: int
    unrepresented_claims_reasons: Dict[str, int] = Field(default_factory=dict)


class TerminologyMetric(BaseModel):
    """Terminology retrieval and clinical normalization evaluation metrics."""
    total_queries: int
    eligible_queries: int
    in_vocabulary_queries: int

    # Retrieval Metrics (Tier 1)
    top1_hits: int
    top1_accuracy: Optional[float] = None
    top1_accuracy_wilson_95: Optional[Tuple[float, float]] = None
    top5_hits: int
    top5_recall: Optional[float] = None
    top5_recall_wilson_95: Optional[Tuple[float, float]] = None

    # Normalization Metrics (Tier 2)
    accepted_assignments: int
    correct_accepted_assignments: int
    incorrect_accepted_assignments: int
    abstained_assignments: int

    precision: Optional[float] = None
    precision_wilson_95: Optional[Tuple[float, float]] = None
    coverage: Optional[float] = None
    coverage_wilson_95: Optional[Tuple[float, float]] = None
    cfmr: Optional[float] = None
    cfmr_wilson_95: Optional[Tuple[float, float]] = None

    # Abstention Breakdown & Nonsense Handling
    abstention_counts: Dict[str, int] = Field(default_factory=dict)
    nonsense_queries_evaluated: int = 0
    nonsense_queries_abstained: int = 0
    nonsense_abstention_rate: Optional[float] = None


class DatasetEvaluationResult(BaseModel):
    """Complete evaluation result for a single benchmark split."""
    dataset_provenance: DatasetProvenance
    ocr_metrics: Optional[Dict[str, float]] = None  # cer, wer
    field_metrics: Dict[str, FieldMetric] = Field(default_factory=dict)
    macro_f1: float
    micro_f1: float
    medication_metrics: Optional[MedicationTupleMetric] = None
    spatial_grounding: Optional[SpatialGroundingMetric] = None
    verification_metrics: Optional[VerificationMetric] = None
    fhir_representation: Optional[FHIRRepresentationMetric] = None
    terminology_metrics: Optional[TerminologyMetric] = None



class GateCheckResult(BaseModel):
    """Threshold check result for a single metric."""
    metric_name: str
    target_threshold: float
    measured_value: float
    passed: bool
    dataset_name: str


class BenchmarkReport(BaseModel):
    """Authoritative consolidated M-BENCHMARK report."""
    benchmark_version: str = "1.0.0"
    environment: EnvironmentMetadata
    datasets: Dict[str, DatasetEvaluationResult] = Field(default_factory=dict)
    gate_checks: List[GateCheckResult] = Field(default_factory=list)
    overall_gates_passed: bool
    summary: Dict[str, Any] = Field(default_factory=dict)

