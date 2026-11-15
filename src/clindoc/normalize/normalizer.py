"""Unified Terminology Normalizer for clinical claims.

Authority:
  - PROJECT_SPEC §5 (Component 6), §7.5, §8.3, §14, §15.3, §21.6
  - DEC-002: code_confidence thresholding (target >=95% precision on accepted codes; provisional default 0.75)
  - DEC-007: hybrid lexical + semantic retrieval for LOINC/RxNorm; deterministic UCUM lookup
  - PHASE0_ARCHITECTURE_RESOLUTION AUDIT-14
  - Phase 3D.2: TerminologyNormalizer Adapter Integration with TerminologyRetrievalService

MUST Rules:
  - NEVER alter raw claim value (§14.3, auditability requirement).
  - Enforce code_confidence < threshold -> normalized_code: None, code_system: None (§7.5, §14.3).
  - UCUM unit normalization for lab.unit and unit_raw (§8.3, §14.2).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from clindoc.extract.contracts import Claim, CodeSystem
from clindoc.normalize.hybrid import CandidateMatch, HybridTerminologyRetriever, SemanticEmbedder
from clindoc.normalize.ucum import UCUMNormalizer
from clindoc.terminology.service import (
    ContextStatus,
    QueryContext,
    TerminologyRetrievalService,
)

logger = logging.getLogger(__name__)

DEFAULT_LOINC_PATH = Path("configs/loinc_subset.json")
DEFAULT_MED_PATH = Path("configs/medication_subset.json")
DEFAULT_UCUM_PATH = Path("configs/ucum_subset.json")


def _resolve_config_path(path: Path) -> Path:
    """Resolve configuration path relative to current or project root."""
    if path.exists():
        return path
    proj_root = Path(__file__).resolve().parents[3]
    alt = proj_root / path
    if alt.exists():
        return alt
    return path


# Canonical qualitative normalization vocabulary (PD-022)
QUALITATIVE_CANONICAL_MAP: dict[str, str] = {
    "negative": "NEGATIVE",
    "neg": "NEGATIVE",
    "-ve": "NEGATIVE",
    "positive": "POSITIVE",
    "pos": "POSITIVE",
    "+ve": "POSITIVE",
    "reactive": "REACTIVE",
    "non-reactive": "NON_REACTIVE",
    "non reactive": "NON_REACTIVE",
    "nonreactive": "NON_REACTIVE",
    "detected": "DETECTED",
    "not detected": "NOT_DETECTED",
    "not-detected": "NOT_DETECTED",
    "undetected": "NOT_DETECTED",
    "equivocal": "EQUIVOCAL",
    "indeterminate": "EQUIVOCAL",
}


def normalize_qualitative_value(val: str) -> Optional[str]:
    """Map recognized qualitative laboratory determinations to canonical vocabulary per PD-022.

    Returns canonical string (NEGATIVE, POSITIVE, REACTIVE, NON_REACTIVE, DETECTED, NOT_DETECTED, EQUIVOCAL)
    or None if the value is not a recognized qualitative determination.
    """
    if not val or not val.strip():
        return None
    val_clean = val.strip().lower()

    # Exact dictionary lookup
    if val_clean in QUALITATIVE_CANONICAL_MAP:
        return QUALITATIVE_CANONICAL_MAP[val_clean]

    # Phrase boundary checks (ordered multi-word first)
    if re.search(r"\bnon[- ]reactive\b", val_clean):
        return "NON_REACTIVE"
    if re.search(r"\bnot[- ]detected\b", val_clean) or re.search(r"\bundetected\b", val_clean):
        return "NOT_DETECTED"
    if re.search(r"\bnegative\b", val_clean) or re.search(r"\bneg\b", val_clean) or "-ve" in val_clean:
        return "NEGATIVE"
    if re.search(r"\bpositive\b", val_clean) or re.search(r"\bpos\b", val_clean) or "+ve" in val_clean:
        return "POSITIVE"
    if re.search(r"\breactive\b", val_clean):
        return "REACTIVE"
    if re.search(r"\bdetected\b", val_clean):
        return "DETECTED"
    if re.search(r"\bequivocal\b", val_clean) or re.search(r"\bindeterminate\b", val_clean):
        return "EQUIVOCAL"

    return None


class TerminologyNormalizer:
    """Production Terminology Normalization subsystem.

    Maps:
      - lab.test_name -> LOINC / CLCI
      - med.name      -> CDCI / RxNorm
      - lab.unit / unit_raw -> UCUM
      - lab.value / med.dose numeric parsing & UCUM unit normalization (§8.3)
    """

    def __init__(
        self,
        loinc_path: Optional[Path] = None,
        med_path: Optional[Path] = None,
        ucum_path: Optional[Path] = None,
        loinc_threshold: float = 0.75,   # DEC-002 provisional default
        rxnorm_threshold: float = 0.75,  # DEC-002 provisional default
        hybrid_alpha: float = 0.5,       # DEC-007 lexical vs semantic balance
        semantic_embedder: Optional[SemanticEmbedder] = None,
        retrieval_service: Optional[TerminologyRetrievalService] = None,
    ) -> None:
        self.loinc_threshold = float(loinc_threshold)
        self.rxnorm_threshold = float(rxnorm_threshold)
        self.retrieval_service = retrieval_service

        # 1. Initialize UCUM Deterministic Normalizer
        resolved_ucum = _resolve_config_path(ucum_path or DEFAULT_UCUM_PATH)
        self.ucum_normalizer = UCUMNormalizer(resolved_ucum)

        # 2. Initialize LOINC Hybrid Retriever (legacy fallback)
        self.loinc_retriever = HybridTerminologyRetriever(
            system_name="LOINC", alpha=hybrid_alpha, semantic_embedder=semantic_embedder
        )
        resolved_loinc = _resolve_config_path(loinc_path or DEFAULT_LOINC_PATH)
        if resolved_loinc.exists():
            with open(resolved_loinc, "r", encoding="utf-8") as f:
                self.loinc_retriever.index_concepts(json.load(f))

        # 3. Initialize RxNorm Hybrid Retriever (legacy fallback)
        self.rxnorm_retriever = HybridTerminologyRetriever(
            system_name="RxNorm", alpha=hybrid_alpha, semantic_embedder=semantic_embedder
        )
        resolved_med = _resolve_config_path(med_path or DEFAULT_MED_PATH)
        if resolved_med.exists():
            with open(resolved_med, "r", encoding="utf-8") as f:
                self.rxnorm_retriever.index_concepts(json.load(f))

        # Audit candidates store: keyed by claim_id -> list of candidate dicts
        self.audit_candidates: dict[str, list[dict[str, Any]]] = {}
        # Audit abstention reasons: keyed by claim_id -> list of reason strings
        self.audit_reasons: dict[str, list[str]] = {}
        # Normalized units store: keyed by claim_id -> canonical UCUM unit string
        self.normalized_units: dict[str, str] = {}

    def clear_audit_state(self) -> None:
        """Clear all stored transient per-claim state (candidates, reasons, units) to prevent memory leaks."""
        self.audit_candidates.clear()
        self.audit_reasons.clear()
        self.normalized_units.clear()

    # Backwards-compatible alias for existing callers
    clear_audit_candidates = clear_audit_state

    def get_audit_candidates(self, claim_id: str) -> list[dict[str, Any]]:
        """Retrieve stored normalization candidates for a given claim_id (§7.5)."""
        return list(self.audit_candidates.get(claim_id, []))

    def get_audit_reasons(self, claim_id: str) -> list[str]:
        """Retrieve stored abstention reasons for a given claim_id."""
        return list(self.audit_reasons.get(claim_id, []))

    def get_normalized_unit(self, claim_id: str) -> Optional[str]:
        """Retrieve the canonical UCUM unit for a claim_id (§8.3)."""
        return self.normalized_units.get(claim_id)

    def get_candidates(self, field_type: str, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Retrieve candidates for a given clinical field type and query string (§7.5)."""
        if not query or not query.strip():
            return []

        if self.retrieval_service is not None and field_type in ("lab.test_name", "med.name"):
            domain = "LAB" if field_type == "lab.test_name" else "MEDICATION"
            try:
                resp = self.retrieval_service.retrieve(query, context=QueryContext(domain=domain), max_candidates=top_k)
                return [
                    {
                        "code": c.code,
                        "display": c.canonical_name,
                        "matched_alias": c.matched_alias,
                        "score": round(c.rank_score, 4),
                        "system": c.system,
                        "tty": c.tty,
                        "context_status": c.context_status.value if hasattr(c.context_status, "value") else str(c.context_status),
                        "retrieval_method": c.retrieval_method.value if hasattr(c.retrieval_method, "value") else str(c.retrieval_method),
                    }
                    for c in resp.candidates
                ]
            except Exception as e:
                logger.warning(f"Terminology retrieval query failed for {field_type} '{query}': {e}")
                return []

        if field_type == "lab.test_name":
            matches = self.loinc_retriever.retrieve(query, top_k=top_k)
            return [m.to_dict() for m in matches]
        elif field_type == "med.name":
            matches = self.rxnorm_retriever.retrieve(query, top_k=top_k)
            return [m.to_dict() for m in matches]
        elif field_type in ("lab.unit", "unit_raw"):
            canon, conf = self.ucum_normalizer.normalize(query)
            if canon:
                return [{"code": canon, "display": canon, "score": conf, "system": "UCUM"}]
            return []
        return []

    def normalize_claim(self, claim: Claim, context: Optional[QueryContext] = None) -> Claim:
        """Enrich a single Claim with standardized terminology code, unit, and confidence.

        MUST NOT alter claim.value or claim.unit_raw (§14.3).
        If code_confidence < threshold, normalized_code and code_system are set to None.
        Populates value_numeric and normalized unit for numeric claims per §8.3.
        """
        # Store original raw value to assert §14.3 invariant
        original_value = claim.value
        original_unit = claim.unit_raw

        candidates: list[Any] = []
        best_code: Optional[str] = None
        best_system: CodeSystem = None
        best_conf: float = 0.0

        if claim.field_type in ("lab.test_name", "med.name") and self.retrieval_service is not None:
            domain = "LAB" if claim.field_type == "lab.test_name" else "MEDICATION"
            threshold = self.loinc_threshold if claim.field_type == "lab.test_name" else self.rxnorm_threshold

            ctx_obj = context if context is not None else QueryContext(domain=domain)

            try:
                resp = self.retrieval_service.retrieve(claim.value, context=ctx_obj, max_candidates=10)
            except Exception as e:
                logger.error(f"TerminologyRetrievalService failed for claim {claim.claim_id}: {e}")
                self.audit_reasons.setdefault(claim.claim_id, []).append(
                    f"Terminology retrieval service failure: {str(e)}"
                )
                self.audit_candidates[claim.claim_id] = []
                resp = None

            if resp is not None:
                if resp.abstention_reasons:
                    self.audit_reasons[claim.claim_id] = list(resp.abstention_reasons)

                audit_list = [
                    {
                        "code": c.code,
                        "display": c.canonical_name,
                        "matched_alias": c.matched_alias,
                        "score": round(c.rank_score, 4),
                        "system": c.system,
                        "tty": c.tty,
                        "context_status": c.context_status.value if hasattr(c.context_status, "value") else str(c.context_status),
                        "retrieval_method": c.retrieval_method.value if hasattr(c.retrieval_method, "value") else str(c.retrieval_method),
                    }
                    for c in resp.candidates
                ]
                self.audit_candidates[claim.claim_id] = audit_list

                # Selection logic:
                # 1. If explicit service abstention or no candidates: fail closed
                if not resp.is_abstained and resp.candidates:
                    valid_candidates = [
                        c for c in resp.candidates
                        if c.context_status in (ContextStatus.PASSED, ContextStatus.NO_RULES)
                    ]
                    if valid_candidates:
                        top = valid_candidates[0]
                        top_score = top.rank_score

                        # 2. Check threshold
                        if top_score >= threshold:
                            # 3. For laboratory claims, check for ambiguous ties among distinct concepts in the same coding system
                            if claim.field_type == "lab.test_name":
                                top_tier_concepts = {
                                    c.concept_id for c in valid_candidates
                                    if abs(c.rank_score - top_score) < 1e-6 and c.system == top.system
                                }
                                if len(top_tier_concepts) > 1:
                                    # Ambiguous laboratory mention: abstain
                                    best_code = None
                                    best_system = None
                                    best_conf = 0.0
                                    self.audit_reasons.setdefault(claim.claim_id, []).append(
                                        f"Ambiguous laboratory mention: {len(top_tier_concepts)} distinct concepts tied at rank {top_score}"
                                    )
                                else:
                                    best_code = top.code
                                    best_system = top.system  # "LOINC", "RxNorm", or "CDCI"
                                    best_conf = round(top_score, 4)
                            else:
                                # Medication claim
                                best_code = top.code
                                best_system = top.system  # "CDCI" or "RxNorm"
                                best_conf = round(top_score, 4)

        elif claim.field_type == "lab.test_name":
            threshold = self.loinc_threshold
            matches = self.loinc_retriever.retrieve(claim.value, top_k=5)
            candidates = matches
            if matches:
                top = matches[0]
                best_conf = round(top.score, 4)
                if best_conf >= threshold:
                    best_code = top.code
                    best_system = "LOINC"
            self.audit_candidates[claim.claim_id] = [c.to_dict() for c in candidates]

        elif claim.field_type == "med.name":
            threshold = self.rxnorm_threshold
            matches = self.rxnorm_retriever.retrieve(claim.value, top_k=5)
            candidates = matches
            if matches:
                top = matches[0]
                best_conf = round(top.score, 4)
                if best_conf >= threshold:
                    best_code = top.code
                    best_system = "RxNorm"
            self.audit_candidates[claim.claim_id] = [c.to_dict() for c in candidates]

        elif claim.field_type == "lab.unit":
            canon, conf = self.ucum_normalizer.normalize(claim.value)
            best_conf = round(conf, 4)
            if canon:
                best_code = canon
                best_system = "UCUM"
                self.normalized_units[claim.claim_id] = canon
                candidates = [CandidateMatch(code=canon, display=canon, score=conf, system="UCUM")]
            elif claim.unit_raw:
                canon_u, conf_u = self.ucum_normalizer.normalize(claim.unit_raw)
                if canon_u:
                    best_code = canon_u
                    best_system = "UCUM"
                    best_conf = round(conf_u, 4)
                    self.normalized_units[claim.claim_id] = canon_u
                    candidates = [CandidateMatch(code=canon_u, display=canon_u, score=conf_u, system="UCUM")]
            self.audit_candidates[claim.claim_id] = [c.to_dict() for c in candidates]

        # Handle numeric claims (lab.value, med.dose) and qualitative determinations (PD-022)
        elif claim.field_type in ("lab.value", "med.dose"):
            qual_canonical = None
            if claim.field_type == "lab.value":
                qual_canonical = normalize_qualitative_value(claim.value)

            if qual_canonical is not None:
                # Qualitative laboratory determination (PD-022):
                # value_numeric MUST be None; no unit normalization; canonical code populated
                claim.value_numeric = None
                best_code = qual_canonical
                best_system = None
                best_conf = 1.0
                candidates = [CandidateMatch(code=qual_canonical, display=qual_canonical, score=1.0, system="CANONICAL")]
            else:
                # Quantitative numeric claims (lab.value numeric, med.dose)
                if claim.value_numeric is None:
                    m = re.search(r"[-+]?\d+(?:\.\d+)?", claim.value)
                    if m:
                        try:
                            claim.value_numeric = float(m.group(0))
                        except ValueError:
                            pass

                # Normalize unit to UCUM without altering claim.value or unit_raw
                raw_unit_to_norm = claim.unit_raw
                if not raw_unit_to_norm and claim.value:
                    m_unit = re.search(r"[-+]?\d+(?:\.\d+)?\s*([a-zA-Z/%^0-9*\[\]{}]+.*)$", claim.value)
                    if m_unit:
                        raw_unit_to_norm = m_unit.group(1).strip()

                if raw_unit_to_norm:
                    canon_unit, ucum_conf = self.ucum_normalizer.normalize(raw_unit_to_norm)
                    if canon_unit:
                        self.normalized_units[claim.claim_id] = canon_unit
                        best_code = canon_unit
                        best_system = "UCUM"
                        best_conf = round(ucum_conf, 4)
                        candidates = [CandidateMatch(code=canon_unit, display=canon_unit, score=ucum_conf, system="UCUM")]

            self.audit_candidates[claim.claim_id] = [c.to_dict() for c in candidates]

        # Invariant checks: claim.value and claim.unit_raw MUST remain completely untouched (§14.3)
        assert claim.value == original_value, "Invariant violation: claim.value was modified (§14.3)"
        assert claim.unit_raw == original_unit, "Invariant violation: claim.unit_raw was modified (§14.3)"

        # Enrich claim fields
        claim.normalized_code = best_code
        claim.code_system = best_system
        claim.code_confidence = best_conf

        return claim

    def normalize_claims(
        self,
        claims: list[Claim],
        contexts: Optional[dict[str, QueryContext]] = None,
    ) -> list[Claim]:
        """Normalize a list of claims in place and return them."""
        for c in claims:
            ctx = contexts.get(c.claim_id) if contexts else None
            self.normalize_claim(c, context=ctx)
        return claims
