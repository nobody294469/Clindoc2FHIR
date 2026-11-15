"""Focused unit and integration test suite for TerminologyNormalizer adapter (Phase 3D.2).

Validates:
1. CDCI medication candidate retrieval.
2. RxNorm medication candidate retrieval.
3. LOINC laboratory candidate retrieval.
4. CLCI candidate retrieval (curated Indian lab mappings).
5. Ambiguous laboratory mention resulting in abstention.
6. Medication brand/generic distinction.
7. Retrieval with insufficient context (abstention on conflict/missing).
8. Preservation of competing candidates in audit_candidates.
9. UCUM normalization unchanged (lab.unit, unit_raw, numeric parsing, qualitative values).
10. Original claim value and evidence unchanged (§14.3 invariant).
11. Missing or unavailable terminology service (fails closed safely).
12. Backward compatibility with legacy constructor (no retrieval_service passed).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from clindoc.extract.contracts import Claim
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.terminology.service import (
    ContextStatus,
    QueryContext,
    TerminologyRetrievalService,
)

ACTIVE_TERM_DIR = Path("data/terminology")


@pytest.fixture(scope="module")
def term_service():
    """Provides TerminologyRetrievalService instance connected to active terminology database."""
    if not ACTIVE_TERM_DIR.exists():
        pytest.skip("Active terminology directory 'data/terminology' does not exist")
    # Initialize with preload_cache=False for fast startup (< 30ms) using indexed SQLite
    svc = TerminologyRetrievalService(ACTIVE_TERM_DIR, preload_cache=False)
    yield svc
    svc.close()


@pytest.fixture
def normalizer(term_service):
    """Provides TerminologyNormalizer injected with active TerminologyRetrievalService."""
    return TerminologyNormalizer(retrieval_service=term_service)


def _make_claim(
    field_type: str,
    value: str,
    unit_raw: str | None = None,
    claim_id: str = "test-claim-1",
    evidence_id: str = "ev-100",
    region_id: str = "reg-200",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        document_id="doc-test-1",
        group_id="grp-1",
        field_type=field_type,
        value=value,
        unit_raw=unit_raw,
        extraction_method="deterministic",
        extraction_confidence=0.95,
        evidence_id=evidence_id,
        region_id=region_id,
    )


class TestTerminologyNormalizerAdapter:
    """Phase 3D.2 TerminologyNormalizer Adapter Integration Tests."""

    def test_01_cdci_medication_candidate_retrieval(self, normalizer):
        """1. CDCI medication candidate retrieval: Indian brand medication resolves to CDCI."""
        claim = _make_claim(field_type="med.name", value="Safedox")
        normalizer.normalize_claim(claim)

        assert claim.code_system == "CDCI"
        assert claim.normalized_code == "2430421000189104"
        assert claim.code_confidence >= 0.75

        # Check candidate audit details
        candidates = normalizer.get_audit_candidates(claim.claim_id)
        assert len(candidates) > 0
        top = candidates[0]
        assert top["system"] == "CDCI"
        assert top["code"] == "2430421000189104"
        assert "Safedox 200 mg" in top["display"]

    def test_02_rxnorm_medication_candidate_retrieval(self, normalizer):
        """2. RxNorm medication candidate retrieval: validates medication retrieval across standard systems."""
        claim = _make_claim(field_type="med.name", value="methotrexate 10 MG Oral Tablet")
        normalizer.normalize_claim(claim)

        assert claim.code_system in ("CDCI", "RxNorm")
        assert claim.normalized_code is not None
        assert claim.code_confidence >= 0.75

        # Verify both CDCI and RxNorm candidates exist in audit store
        candidates = normalizer.get_audit_candidates(claim.claim_id)
        systems = {c["system"] for c in candidates}
        assert "RxNorm" in systems
        assert "CDCI" in systems

    def test_03_loinc_laboratory_candidate_retrieval(self, normalizer):
        """3. LOINC laboratory candidate retrieval: specific lab test name resolves to LOINC."""
        claim = _make_claim(field_type="lab.test_name", value="Hemoglobin [Mass/volume] in Blood")
        normalizer.normalize_claim(claim)

        assert claim.code_system == "LOINC"
        assert claim.normalized_code == "718-7"
        assert claim.code_confidence >= 0.75

        candidates = normalizer.get_audit_candidates(claim.claim_id)
        assert len(candidates) > 0
        assert candidates[0]["code"] == "718-7"
        assert candidates[0]["system"] == "LOINC"

    def test_04_clci_candidate_retrieval(self, normalizer):
        """4. CLCI candidate retrieval: Indian lab test alias maps to LOINC via CLCI_GENERAL_NAME."""
        claim = _make_claim(field_type="lab.test_name", value="aspergillus fumigatus allergy test")
        normalizer.normalize_claim(claim)

        assert claim.code_system == "LOINC"
        assert claim.normalized_code == "6025-1"
        assert claim.code_confidence >= 0.75

        # Verify CLCI origin in audit candidates
        candidates = normalizer.get_audit_candidates(claim.claim_id)
        assert len(candidates) > 0
        assert candidates[0]["code"] == "6025-1"

    def test_05_ambiguous_laboratory_mention_resulting_in_abstention(self, normalizer):
        """5. Ambiguous laboratory mention resulting in abstention: tied concepts in lab domain abstain."""
        # 'eosinophils, blood' matches both LOINC 26449-9 and 711-2 with Tier 1 score 1.0
        claim = _make_claim(field_type="lab.test_name", value="eosinophils, blood")
        normalizer.normalize_claim(claim)

        # Fails closed on ambiguity: normalized code is NOT guessed
        assert claim.normalized_code is None
        assert claim.code_system is None
        assert claim.code_confidence == 0.0

        # Competing candidates are preserved for downstream human review
        candidates = normalizer.get_audit_candidates(claim.claim_id)
        assert len(candidates) >= 2
        top_codes = {c["code"] for c in candidates[:2]}
        assert "26449-9" in top_codes
        assert "711-2" in top_codes

        # Abstention reason is explicitly recorded
        reasons = normalizer.get_audit_reasons(claim.claim_id)
        assert any("Ambiguous laboratory mention" in r for r in reasons)

    def test_06_medication_brand_generic_distinction(self, normalizer):
        """6. Medication brand/generic distinction: preserves brand vs generic typology."""
        # Brand mention
        brand_claim = _make_claim(field_type="med.name", value="Safedox 200 mg", claim_id="brand-1")
        normalizer.normalize_claim(brand_claim)
        brand_cands = normalizer.get_audit_candidates("brand-1")
        assert len(brand_cands) > 0
        assert brand_cands[0]["tty"] == "BRAND"

        # Generic / substance mention
        generic_claim = _make_claim(field_type="med.name", value="metformin hydrochloride", claim_id="generic-1")
        normalizer.normalize_claim(generic_claim)
        generic_cands = normalizer.get_audit_candidates("generic-1")
        assert len(generic_cands) > 0
        assert generic_cands[0]["tty"] in ("GENERIC", "SUBSTANCE")

    def test_07_retrieval_with_insufficient_context_abstention(self, normalizer):
        """7. Retrieval with insufficient context: abstains when context rules fail."""
        # '%' has strict context rules requiring specimen/scale; without context it abstains
        claim = _make_claim(field_type="lab.test_name", value="%")
        normalizer.normalize_claim(claim)

        assert claim.normalized_code is None
        assert claim.code_system is None
        assert claim.code_confidence == 0.0

        # Abstention reasons capture missing dimensions
        reasons = normalizer.get_audit_reasons(claim.claim_id)
        assert len(reasons) > 0
        assert any("Missing required context" in r for r in reasons)

    def test_08_preservation_of_competing_candidates(self, normalizer):
        """8. Preservation of competing candidates: all evaluated candidates retained in audit store."""
        claim = _make_claim(field_type="med.name", value="methotrexate 10 MG Oral Tablet")
        normalizer.normalize_claim(claim)

        cands = normalizer.get_audit_candidates(claim.claim_id)
        assert len(cands) >= 2
        for c in cands:
            assert "code" in c
            assert "display" in c
            assert "score" in c
            assert "system" in c
            assert "tty" in c
            assert "context_status" in c
            assert "retrieval_method" in c

    def test_09_ucum_normalization_unchanged(self, normalizer):
        """9. UCUM normalization unchanged: unit normalization and numeric parsing completely preserved."""
        # Standard unit claim
        unit_claim = _make_claim(field_type="lab.unit", value="mg/dL")
        normalizer.normalize_claim(unit_claim)
        assert unit_claim.normalized_code == "mg/dL"
        assert unit_claim.code_system == "UCUM"
        assert unit_claim.code_confidence == 1.0

        # Quantitative numeric lab value with embedded unit
        val_claim = _make_claim(field_type="lab.value", value="14.2 g/dL")
        normalizer.normalize_claim(val_claim)
        assert val_claim.value_numeric == 14.2
        assert val_claim.normalized_code == "g/dL"
        assert val_claim.code_system == "UCUM"

        # Qualitative laboratory determination (PD-022)
        qual_claim = _make_claim(field_type="lab.value", value="non-reactive")
        normalizer.normalize_claim(qual_claim)
        assert qual_claim.value_numeric is None
        assert qual_claim.normalized_code == "NON_REACTIVE"
        assert qual_claim.code_system is None
        assert qual_claim.code_confidence == 1.0

        # Medication dose claim
        dose_claim = _make_claim(field_type="med.dose", value="500 mg")
        normalizer.normalize_claim(dose_claim)
        assert dose_claim.value_numeric == 500.0
        assert dose_claim.normalized_code == "mg"
        assert dose_claim.code_system == "UCUM"

    def test_10_original_claim_value_and_evidence_unchanged(self, normalizer):
        """10. Original claim value and evidence unchanged: §14.3 auditability invariant."""
        original_val = "  Safedox 200 mg oral  "
        original_unit = "mg"
        claim = _make_claim(
            field_type="med.name",
            value=original_val,
            unit_raw=original_unit,
            evidence_id="ev-fixed-999",
            region_id="reg-fixed-888",
        )
        normalizer.normalize_claim(claim)

        # Strictly untouched
        assert claim.value == original_val
        assert claim.unit_raw == original_unit
        assert claim.evidence_id == "ev-fixed-999"
        assert claim.region_id == "reg-fixed-888"

    def test_11_missing_or_unavailable_terminology_service(self):
        """11. Missing or unavailable terminology service: fails closed safely without exceptions."""
        broken_svc = MagicMock(spec=TerminologyRetrievalService)
        broken_svc.retrieve.side_effect = RuntimeError("Database file is locked or corrupt")

        faulty_normalizer = TerminologyNormalizer(retrieval_service=broken_svc)
        claim = _make_claim(field_type="med.name", value="paracetamol")

        # Must not raise; must fail closed safely
        faulty_normalizer.normalize_claim(claim)

        assert claim.normalized_code is None
        assert claim.code_system is None
        assert claim.code_confidence == 0.0

    def test_12_backward_compatibility_legacy_constructor(self):
        """12. Backward compatibility with legacy constructor: works without retrieval_service."""
        legacy_normalizer = TerminologyNormalizer()
        assert legacy_normalizer.retrieval_service is None

        claim = _make_claim(field_type="lab.test_name", value="Hemoglobin")
        legacy_normalizer.normalize_claim(claim)

        assert claim.code_system == "LOINC"
        assert claim.normalized_code == "718-7"
        assert claim.code_confidence >= 0.75
