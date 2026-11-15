"""Comprehensive test suite for M-FHIR-SAFE-SELECTION.

Validates:
1. Strict verification normalization across DB, schema, and verification engine enums.
   - Closed-world mapping to CanonicalVerificationVerdict.
   - Fails closed on missing, unknown, or internally contradictory payloads.
2. Slot-type-aware clinical equivalence:
   - Quantitative lab values & medication doses (numeric precision, UCUM scale conversions, unit mismatches).
   - Qualitative terms (route, frequency, abnormal flags).
   - Medication names (prefix cleaning, RxNorm codes, distinct drug rejection).
3. Human review precedence & exhaustive ClaimReviewState matrix:
   - ACCEPTED, EDITED, REJECTED, ESCALATED, PENDING, None.
   - Anti-fallback invariant: human REJECTED/ESCALATED/PENDING binds the slot; no silent fallback to unreviewed model candidates.
   - Multiple conflicting ACCEPTED candidates fail closed and escalate.
   - Replacement reviewer-origin candidate wins, superseded EDITED candidate is suppressed.
4. Pairwise clinical equivalence check for unreviewed candidate slots:
   - Complete pairwise clique check: any incompatible pair fails the whole slot closed.
   - Confidence is strictly a tie-breaker after equivalence is proven.
5. Audit event recording:
   - Unresolved/suppressed slots recorded with metadata_json in audit_trail and AuditService ledger.
6. Benchmark 696 multi-candidate slot audit across the 34 benchmark documents:
   - 679 supported representatives selected.
   - Exactly 17 slots fail closed (including the 2 false-positive groups MED_0028_med_1 and MED_0036_med_2).
"""
from __future__ import annotations

from collections import defaultdict
import glob
import json
from pathlib import Path
from unittest.mock import MagicMock
import types
from typing import Any
import uuid
import pytest

from clindoc.db.enums import (
    ClaimOrigin,
    ClaimReviewState,
    VerificationLabel as DBVerificationLabel,
)
from clindoc.extract.contracts import Claim
from clindoc.fhir.selection import (
    CanonicalVerificationVerdict,
    are_clinically_equivalent,
    normalize_verification_verdict,
    select_deterministic_slot_candidate,
)
from clindoc.schema.verification import VerificationResult
from clindoc.verify.schema import VerificationStatus


# ─── HELPER FIXTURES ─────────────────────────────────────────────────────────

def _make_test_claim(
    claim_id: str,
    field_type: str,
    value: str,
    *,
    value_numeric: float | None = None,
    unit_raw: str | None = None,
    confidence: float = 0.95,
    origin: ClaimOrigin = ClaimOrigin.PIPELINE,
    review_state: ClaimReviewState | None = None,
    document_id: str = "DOC_001",
    group_id: str = "grp-1",
    code_system: str | None = None,
    normalized_code: str | None = None,
) -> Claim:
    c = Claim(
        claim_id=claim_id,
        document_id=document_id,
        group_id=group_id,
        field_type=field_type,
        value=value,
        value_numeric=value_numeric,
        unit_raw=unit_raw,
        extraction_method="deterministic",
        extraction_confidence=confidence,
        evidence_id="ev-1",
        region_id="reg-1",
        code_system=code_system,
        normalized_code=normalized_code,
    )
    if origin is not None:
        object.__setattr__(c, "origin", origin)
    if review_state is not None:
        object.__setattr__(c, "review_state", review_state)
    return c


def _make_test_verification(
    claim_id: str,
    label: Any,
    confidence: float = 0.95,
    contradiction_type: str | None = None,
) -> Any:
    """Create verification representation (supports DB, schema, and engine objects)."""
    return types.SimpleNamespace(
        claim_id=claim_id,
        evidence_id="ev-1",
        label=label,
        confidence=confidence,
        contradiction_type=contradiction_type,
    )


# ─── 1. VERIFICATION NORMALIZATION TESTS ─────────────────────────────────────

class TestVerificationNormalization:
    """Validate closed-world verification normalization and fail-closed handling."""

    def test_normalize_db_verification_labels(self):
        assert normalize_verification_verdict(
            _make_test_verification("c1", DBVerificationLabel.SUPPORTED)
        ) == CanonicalVerificationVerdict.ELIGIBLE

        assert normalize_verification_verdict(
            _make_test_verification("c2", DBVerificationLabel.REFUTED)
        ) == CanonicalVerificationVerdict.REFUTED

        assert normalize_verification_verdict(
            _make_test_verification("c3", DBVerificationLabel.INSUFFICIENT_EVIDENCE)
        ) == CanonicalVerificationVerdict.INSUFFICIENT

    def test_normalize_schema_verification_labels(self):
        assert normalize_verification_verdict(
            {"label": "SUPPORTED"}
        ) == CanonicalVerificationVerdict.ELIGIBLE

        assert normalize_verification_verdict(
            {"label": "DISCREPANCY"}
        ) == CanonicalVerificationVerdict.REFUTED

        assert normalize_verification_verdict(
            {"label": "INSUFFICIENT_EVIDENCE"}
        ) == CanonicalVerificationVerdict.INSUFFICIENT

    def test_normalize_engine_verification_status(self):
        assert normalize_verification_verdict(
            {"status": VerificationStatus.VERIFIED}
        ) == CanonicalVerificationVerdict.ELIGIBLE

        assert normalize_verification_verdict(
            {"status": VerificationStatus.DISCREPANCY}
        ) == CanonicalVerificationVerdict.REFUTED

        assert normalize_verification_verdict(
            {"status": VerificationStatus.MISSING_SOURCE_EVIDENCE}
        ) == CanonicalVerificationVerdict.INSUFFICIENT

        assert normalize_verification_verdict(
            {"status": VerificationStatus.UNCHECKED}
        ) == CanonicalVerificationVerdict.UNCHECKED

    def test_normalize_missing_or_none_returns_unchecked(self):
        assert normalize_verification_verdict(None) == CanonicalVerificationVerdict.UNCHECKED

    def test_normalize_internal_contradiction_returns_invalid(self):
        # SUPPORTED or VERIFIED with contradiction_type is internally inconsistent -> INVALID
        contra_ver = _make_test_verification(
            "c1",
            DBVerificationLabel.SUPPORTED,
            contradiction_type="value_mismatch",
        )
        assert normalize_verification_verdict(contra_ver) == CanonicalVerificationVerdict.INVALID

        contra_dict = {
            "label": "SUPPORTED",
            "contradiction_type": "value_mismatch",
        }
        assert normalize_verification_verdict(contra_dict) == CanonicalVerificationVerdict.INVALID

    def test_normalize_unknown_or_corrupted_payload_returns_invalid(self):
        assert normalize_verification_verdict({"label": "UNKNOWN_GARBAGE"}) == CanonicalVerificationVerdict.INVALID
        assert normalize_verification_verdict(12345) == CanonicalVerificationVerdict.INVALID
        assert normalize_verification_verdict({"no_label_or_status": 1}) == CanonicalVerificationVerdict.INVALID


# ─── 2. CLINICAL EQUIVALENCE TESTS ──────────────────────────────────────────

class TestClinicalEquivalence:
    """Validate slot-type-aware clinical equivalence checks."""

    def test_exact_string_match(self):
        c1 = _make_test_claim("c1", "lab.value", "14.0", value_numeric=14.0, unit_raw="mg/dL")
        c2 = _make_test_claim("c2", "lab.value", "14.0", value_numeric=14.0, unit_raw="mg/dL")
        assert are_clinically_equivalent(c1, c2, "lab.value") is True

    def test_numeric_precision_tolerance(self):
        c1 = _make_test_claim("c1", "lab.value", "110", value_numeric=110.0, unit_raw="mg/dL")
        c2 = _make_test_claim("c2", "lab.value", "110.0", value_numeric=110.0, unit_raw="mg/dL")
        assert are_clinically_equivalent(c1, c2, "lab.value") is True

        # Large numeric values within 0.1% relative tolerance
        c3 = _make_test_claim("c3", "lab.value", "5000", value_numeric=5000.0, unit_raw="U/L")
        c4 = _make_test_claim("c4", "lab.value", "5002", value_numeric=5002.0, unit_raw="U/L")
        assert are_clinically_equivalent(c3, c4, "lab.value") is True

        # Incompatible numeric values outside tolerance
        c5 = _make_test_claim("c5", "lab.value", "51", value_numeric=51.0, unit_raw="U/L")
        c6 = _make_test_claim("c6", "lab.value", "510", value_numeric=510.0, unit_raw="U/L")
        assert are_clinically_equivalent(c5, c6, "lab.value") is False

    def test_unit_conversion_equivalence(self):
        # 1 g/dL == 1000 mg/dL
        c1 = _make_test_claim("c1", "lab.value", "1.0", value_numeric=1.0, unit_raw="g/dL")
        c2 = _make_test_claim("c2", "lab.value", "1000.0", value_numeric=1000.0, unit_raw="mg/dL")
        assert are_clinically_equivalent(c1, c2, "lab.value") is True

        # Incompatible units (e.g. mg vs g with identical numeric value)
        c3 = _make_test_claim("c3", "med.dose", "10", value_numeric=10.0, unit_raw="mg")
        c4 = _make_test_claim("c4", "med.dose", "10", value_numeric=10.0, unit_raw="g")
        assert are_clinically_equivalent(c3, c4, "med.dose") is False

    def test_qualitative_route_equivalence(self):
        c1 = _make_test_claim("c1", "med.route", "po")
        c2 = _make_test_claim("c2", "med.route", "by mouth")
        c3 = _make_test_claim("c3", "med.route", "oral")
        c4 = _make_test_claim("c4", "med.route", "iv")

        assert are_clinically_equivalent(c1, c2, "med.route") is True
        assert are_clinically_equivalent(c1, c3, "med.route") is True
        assert are_clinically_equivalent(c1, c4, "med.route") is False

    def test_qualitative_frequency_equivalence(self):
        c1 = _make_test_claim("c1", "med.frequency", "daily")
        c2 = _make_test_claim("c2", "med.frequency", "qd")
        c3 = _make_test_claim("c3", "med.frequency", "once daily")
        c4 = _make_test_claim("c4", "med.frequency", "bid")

        assert are_clinically_equivalent(c1, c2, "med.frequency") is True
        assert are_clinically_equivalent(c1, c3, "med.frequency") is True
        assert are_clinically_equivalent(c1, c4, "med.frequency") is False

    def test_qualitative_flag_equivalence(self):
        c1 = _make_test_claim("c1", "lab.flag", "H")
        c2 = _make_test_claim("c2", "lab.flag", "high")
        c3 = _make_test_claim("c3", "lab.flag", "L")

        assert are_clinically_equivalent(c1, c2, "lab.flag") is True
        assert are_clinically_equivalent(c1, c3, "lab.flag") is False

    def test_medication_name_equivalence_and_rejection(self):
        c1 = _make_test_claim("c1", "med.name", "Medication: Metformin")
        c2 = _make_test_claim("c2", "med.name", "metformin")
        c3 = _make_test_claim("c3", "med.name", "Medication: Amoxicillin")
        c4 = _make_test_claim("c4", "med.name", "Medication: Metoprolol")

        assert are_clinically_equivalent(c1, c2, "med.name") is True
        # Incompatible drug names must fail equivalence
        assert are_clinically_equivalent(c1, c3, "med.name") is False
        assert are_clinically_equivalent(c1, c4, "med.name") is False

    def test_medication_code_system_matching(self):
        c1 = _make_test_claim(
            "c1", "med.name", "Metformin 500 MG",
            code_system="RxNorm",
            normalized_code="860975",
        )
        c2 = _make_test_claim(
            "c2", "med.name", "Glucophage 500 MG",
            code_system="RxNorm",
            normalized_code="860975",
        )
        assert are_clinically_equivalent(c1, c2, "med.name") is True


# ─── 3. HUMAN REVIEW PRECEDENCE & EXHAUSTIVE STATE MATRIX ────────────────────

class TestHumanReviewPrecedence:
    """Validate human review precedence and anti-fallback invariants."""

    def test_accepted_human_review_wins_over_unreviewed_model(self):
        c_accepted = _make_test_claim(
            "c-acc", "lab.value", "15.0",
            origin=ClaimOrigin.REVIEWER,
            review_state=ClaimReviewState.ACCEPTED,
        )
        c_model = _make_test_claim(
            "c-mod", "lab.value", "14.0",
            origin=ClaimOrigin.PIPELINE,
            review_state=None,
        )
        ver_map = {
            "c-acc": _make_test_verification("c-acc", DBVerificationLabel.SUPPORTED),
            "c-mod": _make_test_verification("c-mod", DBVerificationLabel.SUPPORTED),
        }

        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c_accepted, c_model],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is not None
        assert winner.claim_id == "c-acc"
        assert len(shadowed) == 1
        assert shadowed[0]["competing_claim_id"] == "c-mod"

    def test_anti_fallback_rejected_human_review_fails_closed(self):
        """Human REJECTED candidate coexisting with unreviewed SUPPORTED candidate MUST fail closed."""
        c_rejected = _make_test_claim(
            "c-rej", "lab.value", "999.0",
            origin=ClaimOrigin.PIPELINE,
            review_state=ClaimReviewState.REJECTED,
        )
        c_model = _make_test_claim(
            "c-mod", "lab.value", "14.0",
            origin=ClaimOrigin.PIPELINE,
            review_state=None,
        )
        ver_map = {
            "c-rej": _make_test_verification("c-rej", DBVerificationLabel.REFUTED),
            "c-mod": _make_test_verification("c-mod", DBVerificationLabel.SUPPORTED),
        }
        audit_trail: list[dict] = []

        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c_rejected, c_model],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
            audit_trail=audit_trail,
        )
        # Anti-fallback invariant: strictly fails closed!
        assert winner is None
        assert any(entry.get("status") == "UNRESOLVED_SUPPRESSED" for entry in audit_trail)
        assert any("REJECTED" in entry.get("reason", "") for entry in audit_trail)

    def test_anti_fallback_escalated_and_pending_fail_closed(self):
        for state in (ClaimReviewState.ESCALATED, ClaimReviewState.PENDING):
            c_bound = _make_test_claim(
                f"c-{state.value}", "lab.value", "100.0",
                review_state=state,
            )
            c_model = _make_test_claim(
                "c-mod", "lab.value", "14.0",
                review_state=None,
            )
            ver_map = {
                c_bound.claim_id: _make_test_verification(c_bound.claim_id, DBVerificationLabel.SUPPORTED),
                "c-mod": _make_test_verification("c-mod", DBVerificationLabel.SUPPORTED),
            }
            winner, _ = select_deterministic_slot_candidate(
                candidates=[c_bound, c_model],
                verification_map=ver_map,
                field_slot="lab.value",
                group_id="grp-1",
            )
            assert winner is None, f"State {state} should have failed closed"

    def test_multiple_conflicting_accepted_candidates_escalate_and_fail_closed(self):
        c1 = _make_test_claim(
            "c1", "lab.value", "14.0",
            review_state=ClaimReviewState.ACCEPTED,
        )
        c2 = _make_test_claim(
            "c2", "lab.value", "50.0",
            review_state=ClaimReviewState.ACCEPTED,
        )
        ver_map = {
            "c1": _make_test_verification("c1", DBVerificationLabel.SUPPORTED),
            "c2": _make_test_verification("c2", DBVerificationLabel.SUPPORTED),
        }
        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c1, c2],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is None
        assert any("Multiple authoritative human-reviewed candidates conflict" in s["selection_reason"] for s in shadowed)

    def test_edited_workflow_replacement_wins_and_superseded_suppressed(self):
        c_edited_old = _make_test_claim(
            "c-old", "med.dose", "250",
            origin=ClaimOrigin.PIPELINE,
            review_state=ClaimReviewState.EDITED,
        )
        c_replacement = _make_test_claim(
            "c-new", "med.dose", "500",
            origin=ClaimOrigin.REVIEWER,
            review_state=ClaimReviewState.ACCEPTED,
        )
        ver_map = {
            "c-old": _make_test_verification("c-old", DBVerificationLabel.SUPPORTED),
            "c-new": _make_test_verification("c-new", DBVerificationLabel.SUPPORTED),
        }
        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c_edited_old, c_replacement],
            verification_map=ver_map,
            field_slot="med.dose",
            group_id="grp-1",
        )
        assert winner is not None
        assert winner.claim_id == "c-new"
        assert len(shadowed) == 1
        assert "Superseded EDITED claim version suppressed" in shadowed[0]["selection_reason"]


# ─── 4. PAIRWISE CLIQUE SELECTION FOR UNREVIEWED SLOTS ───────────────────────

class TestPairwiseCliqueSelection:
    """Validate unreviewed slot selection requires 100% mutual pairwise equivalence."""

    def test_unreviewed_equivalent_candidates_deterministic_winner(self):
        c1 = _make_test_claim("c1", "lab.value", "14.0", confidence=0.90)
        c2 = _make_test_claim("c2", "lab.value", "14.0", confidence=0.99)
        ver_map = {
            "c1": _make_test_verification("c1", DBVerificationLabel.SUPPORTED, confidence=0.90),
            "c2": _make_test_verification("c2", DBVerificationLabel.SUPPORTED, confidence=0.99),
        }
        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c1, c2],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is not None
        assert winner.claim_id == "c2"  # Higher confidence tie-breaker
        assert len(shadowed) == 1
        assert shadowed[0]["competing_claim_id"] == "c1"

    def test_unreviewed_incompatible_candidates_fail_closed(self):
        c1 = _make_test_claim("c1", "lab.value", "51", value_numeric=51.0, confidence=0.90)
        c2 = _make_test_claim("c2", "lab.value", "510", value_numeric=510.0, confidence=0.99)
        ver_map = {
            "c1": _make_test_verification("c1", DBVerificationLabel.SUPPORTED, confidence=0.90),
            "c2": _make_test_verification("c2", DBVerificationLabel.SUPPORTED, confidence=0.99),
        }
        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c1, c2],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is None
        assert any("clinical contradiction fails closed" in s["selection_reason"] for s in shadowed)

    def test_intransitive_pairwise_candidates_fail_closed(self):
        """If A is equivalent to B, and B to C, but A is NOT equivalent to C, slot must fail closed."""
        # 100.0 is equiv to 100.05 (diff 0.05 <= rel_tol of 0.1% -> 0.1)
        # 100.05 is equiv to 100.12 (diff 0.07 <= rel_tol of 0.1% -> 0.1)
        # But 100.0 is NOT equiv to 100.12 (diff 0.12 > rel_tol of 0.1% -> 0.1)
        cA = _make_test_claim("cA", "lab.value", "100.00", value_numeric=100.00)
        cB = _make_test_claim("cB", "lab.value", "100.05", value_numeric=100.05)
        cC = _make_test_claim("cC", "lab.value", "100.12", value_numeric=100.12)

        ver_map = {
            "cA": _make_test_verification("cA", DBVerificationLabel.SUPPORTED),
            "cB": _make_test_verification("cB", DBVerificationLabel.SUPPORTED),
            "cC": _make_test_verification("cC", DBVerificationLabel.SUPPORTED),
        }
        winner, _ = select_deterministic_slot_candidate(
            candidates=[cA, cB, cC],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is None

    def test_one_supported_one_refuted_selects_supported_winner(self):
        c_good = _make_test_claim("c-good", "lab.value", "14.0")
        c_bad = _make_test_claim("c-bad", "lab.value", "999.0")
        ver_map = {
            "c-good": _make_test_verification("c-good", DBVerificationLabel.SUPPORTED),
            "c-bad": _make_test_verification("c-bad", DBVerificationLabel.REFUTED),
        }
        winner, shadowed = select_deterministic_slot_candidate(
            candidates=[c_good, c_bad],
            verification_map=ver_map,
            field_slot="lab.value",
            group_id="grp-1",
        )
        assert winner is not None
        assert winner.claim_id == "c-good"
        assert len(shadowed) == 1
        assert shadowed[0]["competing_claim_id"] == "c-bad"


# ─── 5. AUDIT EVENT RECORDING TESTS ──────────────────────────────────────────

class TestAuditEventRecording:
    """Validate audit event recording for unresolved slots."""

    def test_audit_event_recorded_on_contradiction(self):
        c1 = _make_test_claim("c1", "med.name", "Metformin")
        c2 = _make_test_claim("c2", "med.name", "Amoxicillin")
        ver_map = {
            "c1": _make_test_verification("c1", DBVerificationLabel.SUPPORTED),
            "c2": _make_test_verification("c2", DBVerificationLabel.SUPPORTED),
        }
        audit_service = MagicMock()
        session = MagicMock()

        winner, _ = select_deterministic_slot_candidate(
            candidates=[c1, c2],
            verification_map=ver_map,
            field_slot="med.name",
            group_id="MED_0028_med_1",
            audit_service=audit_service,
            session=session,
            document_id="MED_0028",
        )
        assert winner is None
        audit_service.record_event.assert_called_once()
        call_kwargs = audit_service.record_event.call_args[1]
        assert call_kwargs["event_type"] == "SLOT_UNRESOLVED"
        assert call_kwargs["entity_id"] == "MED_0028:MED_0028_med_1:med.name"
        assert call_kwargs["metadata_json"]["is_export_eligible"] is False
        assert call_kwargs["metadata_json"]["candidate_count"] == 2


# ─── 6. BENCHMARK 696 CANDIDATE SLOT EVALUATION ──────────────────────────────

class TestBenchmarkCandidateSlotAudit:
    """Evaluate all 696 multi-candidate slots across the 34 frozen benchmark documents."""

    def test_all_696_benchmark_slots_evaluation(self):
        report_paths = sorted(glob.glob("data/fhir_phase5_v8/*_audit_report.json"))
        assert len(report_paths) == 34, f"Expected 34 audit reports, found {len(report_paths)}"

        all_claims: list[Claim] = []
        verifications: dict[str, VerificationResult] = {}

        for p in report_paths:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            for c_dict in data.get("claims", []):
                all_claims.append(Claim(**c_dict))
            for v_dict in data.get("verifications", []):
                verifications[v_dict["claim_id"]] = VerificationResult(**v_dict)

        by_slot: dict[tuple[str, str, str], list[Claim]] = defaultdict(list)
        for c in all_claims:
            by_slot[(c.document_id, c.group_id, c.field_type)].append(c)

        multi_candidate_slots = {k: v for k, v in by_slot.items() if len(v) > 1}
        assert len(multi_candidate_slots) == 696, (
            f"Expected exactly 696 multi-candidate slots, found {len(multi_candidate_slots)}"
        )

        winners_count = 0
        suppressed_count = 0
        suppressed_slot_keys = set()

        for (doc_id, grp, ft), candidates in multi_candidate_slots.items():
            win, _ = select_deterministic_slot_candidate(
                candidates=candidates,
                verification_map=verifications,
                field_slot=ft,
                group_id=grp,
            )
            if win is not None:
                winners_count += 1
            else:
                suppressed_count += 1
                suppressed_slot_keys.add((doc_id, grp, ft))

        # Expected counts under M-FHIR-SAFE-SELECTION
        assert winners_count == 679, f"Expected 679 selected winners, got {winners_count}"
        assert suppressed_count == 17, f"Expected 17 suppressed slots, got {suppressed_count}"

        # Explicitly verify the two false-positive corrupted groups failed closed
        assert ("MED_0028", "MED_0028_med_1", "med.name") in suppressed_slot_keys
        assert ("MED_0036", "MED_0036_med_2", "med.name") in suppressed_slot_keys
