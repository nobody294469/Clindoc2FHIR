"""Regression test verifying selector behavior against the frozen 696-group baseline.

Authority: M-TERMINOLOGY-SCALE Phase 2 Prerequisite.
Verifies that current and future safe-selection behavior matches the frozen per-group
behavioral baseline recorded in `tests/fixtures/fhir_safe_selection_696_oracle.json`.
"""

from __future__ import annotations

from collections import defaultdict
import glob
import json
from pathlib import Path
import pytest

from clindoc.extract.contracts import Claim
from clindoc.fhir.selection import select_deterministic_slot_candidate
from clindoc.schema.verification import VerificationResult

ORACLE_FIXTURE_PATH = Path("tests/fixtures/fhir_safe_selection_696_oracle.json")


@pytest.fixture(scope="module")
def oracle_data() -> dict:
    assert ORACLE_FIXTURE_PATH.exists(), f"Oracle fixture missing at {ORACLE_FIXTURE_PATH}"
    with open(ORACLE_FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def reconstructed_multi_candidate_slots():
    report_paths = sorted(glob.glob("data/fhir_phase5_v8/*_audit_report.json"))
    assert len(report_paths) == 34, f"Expected 34 reports, got {len(report_paths)}"

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
    assert len(multi_candidate_slots) == 696
    return multi_candidate_slots, verifications


class TestFhirSafeSelection696Baseline:
    """Validates selector against the per-group frozen behavioral regression baseline."""

    def test_fixture_metadata_and_aggregates(self, oracle_data: dict):
        meta = oracle_data.get("_metadata", {})
        assert meta.get("artifact_type") == "BEHAVIORAL_REGRESSION_BASELINE"
        assert "BEHAVIORAL REGRESSION BASELINE" in meta.get("disclaimer", "")
        assert meta.get("total_slots") == 696
        assert meta.get("selected_count") == 679
        assert meta.get("suppressed_count") == 17
        assert meta.get("suppressed_contradiction_count") == 2
        assert meta.get("suppressed_ineligible_count") == 15
        assert len(oracle_data.get("slots", [])) == 696

    def test_per_group_selector_matches_baseline_exactly(
        self,
        oracle_data: dict,
        reconstructed_multi_candidate_slots,
    ):
        multi_slots, verifications = reconstructed_multi_candidate_slots
        oracle_slots_by_key = {s["slot_key"]: s for s in oracle_data["slots"]}

        assert len(oracle_slots_by_key) == 696

        winners_count = 0
        suppressed_count = 0
        divergences = []

        for (doc_id, grp_id, ft), candidates in multi_slots.items():
            slot_key = f"{doc_id}:{grp_id}:{ft}"
            assert slot_key in oracle_slots_by_key, f"Slot {slot_key} missing from oracle"
            expected_slot = oracle_slots_by_key[slot_key]

            # Run deterministic selector
            winner, _ = select_deterministic_slot_candidate(
                candidates=sorted(candidates, key=lambda c: str(c.claim_id)),
                verification_map=verifications,
                field_slot=ft,
                group_id=grp_id,
                document_id=doc_id,
            )

            if winner is not None:
                winners_count += 1
                if expected_slot["is_suppressed"]:
                    divergences.append(
                        f"Slot {slot_key}: expected SUPPRESSED but got winner '{winner.claim_id}'"
                    )
                else:
                    exp_winner_id = expected_slot["selected_candidate"]["claim_id"]
                    if winner.claim_id != exp_winner_id:
                        divergences.append(
                            f"Slot {slot_key}: expected winner '{exp_winner_id}' but got '{winner.claim_id}'"
                        )
            else:
                suppressed_count += 1
                if not expected_slot["is_suppressed"]:
                    exp_winner_id = expected_slot["selected_candidate"]["claim_id"]
                    divergences.append(
                        f"Slot {slot_key}: expected winner '{exp_winner_id}' but got SUPPRESSED"
                    )

        assert not divergences, f"Found {len(divergences)} divergences from baseline:\n" + "\n".join(divergences[:10])
        assert winners_count == 679
        assert suppressed_count == 17

    def test_known_contradiction_slots_fail_closed(self, oracle_data: dict):
        oracle_slots_by_key = {s["slot_key"]: s for s in oracle_data["slots"]}

        # MED_0028_med_1 (Metformin vs Amoxicillin)
        s1 = oracle_slots_by_key.get("MED_0028:MED_0028_med_1:med.name")
        assert s1 is not None
        assert s1["is_suppressed"] is True
        assert s1["outcome"] == "SUPPRESSED"
        assert "contradiction" in s1["suppression_reason"].lower()

        # MED_0036_med_2 (Metformin vs Metoprolol)
        s2 = oracle_slots_by_key.get("MED_0036:MED_0036_med_2:med.name")
        assert s2 is not None
        assert s2["is_suppressed"] is True
        assert s2["outcome"] == "SUPPRESSED"
        assert "contradiction" in s2["suppression_reason"].lower()

