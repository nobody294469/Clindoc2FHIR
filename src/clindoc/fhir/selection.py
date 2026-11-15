"""Canonical candidate selection and verification normalization for FHIR export (M-FHIR-SAFE-SELECTION).

Authority:
  - PROJECT_SPEC.md §7.7, §10, §14-16
  - PRODUCTIZATION_SPEC.md §4.3, §7.3
  - PRODUCTIZATION_DECISIONS.md PD-002, PD-009, PD-012, DEC-020
  - M-FHIR-SAFE-SELECTION v3, v3.1, v3.2
"""

from __future__ import annotations

from enum import Enum
import logging
import math
import re
from typing import Any, Optional, Sequence, Union

from clindoc.db.enums import ClaimOrigin, ClaimReviewState, VerificationLabel as DBVerificationLabel
from clindoc.extract.contracts import Claim
from clindoc.schema.verification import VerificationLabel as SchemaVerificationLabel, VerificationResult
from clindoc.verify.schema import VerificationStatus

logger = logging.getLogger(__name__)


# ─── 1. VERIFICATION NORMALIZATION (CLOSED-WORLD MAPPING) ────────────────────


class CanonicalVerificationVerdict(str, Enum):
    """Closed-world normalized verification verdicts for candidate selection."""

    ELIGIBLE = "ELIGIBLE"          # Verified as factually supported by source evidence
    REFUTED = "REFUTED"            # Explicitly refuted or contradictory
    INSUFFICIENT = "INSUFFICIENT"  # Insufficient or missing evidence
    UNCHECKED = "UNCHECKED"        # Unverified or missing verification record
    INVALID = "INVALID"            # Internally contradictory, unknown, or corrupted payload


def normalize_verification_verdict(ver: Optional[Any]) -> CanonicalVerificationVerdict:
    """Normalize verification results across DB, schema, and verification engine enums.

    Rules:
      1. ver is None -> UNCHECKED
      2. Internal contradiction (label indicates supported/verified but contradiction_type is present) -> INVALID
      3. DBVerificationLabel.SUPPORTED, SchemaVerificationLabel "SUPPORTED", VerificationStatus.VERIFIED -> ELIGIBLE
      4. DBVerificationLabel.REFUTED, SchemaVerificationLabel "DISCREPANCY", VerificationStatus.DISCREPANCY -> REFUTED
      5. DBVerificationLabel.INSUFFICIENT_EVIDENCE, SchemaVerificationLabel "INSUFFICIENT_EVIDENCE",
         VerificationStatus.MISSING_SOURCE_EVIDENCE -> INSUFFICIENT
      6. VerificationStatus.UNCHECKED -> UNCHECKED
      7. Any unknown value, invalid string, or inconsistent type -> INVALID
    """
    if ver is None:
        return CanonicalVerificationVerdict.UNCHECKED

    # Extract label and contradiction fields
    raw_label: Optional[str] = None
    contradiction_type: Optional[str] = None

    if hasattr(ver, "label") and ver.label is not None:
        lbl = ver.label
        raw_label = lbl.value if hasattr(lbl, "value") else str(lbl)
    elif hasattr(ver, "status") and ver.status is not None:
        st = ver.status
        raw_label = st.value if hasattr(st, "value") else str(st)
    elif isinstance(ver, dict):
        lbl = ver.get("label") or ver.get("status")
        if lbl is not None:
            raw_label = lbl.value if hasattr(lbl, "value") else str(lbl)

    if hasattr(ver, "contradiction_type"):
        ct = getattr(ver, "contradiction_type")
        if ct:
            contradiction_type = str(ct).strip()
    elif isinstance(ver, dict):
        ct = ver.get("contradiction_type")
        if ct:
            contradiction_type = str(ct).strip()

    if not raw_label or not isinstance(raw_label, str):
        return CanonicalVerificationVerdict.INVALID

    norm_label = raw_label.strip().upper()

    # Integrity invariant: supported/verified claims with contradiction indicators are corrupted
    if norm_label in ("SUPPORTED", "VERIFIED") and contradiction_type:
        logger.warning(
            "Internally contradictory verification record: label='%s' but contradiction_type='%s'",
            norm_label,
            contradiction_type,
        )
        return CanonicalVerificationVerdict.INVALID

    if norm_label in ("SUPPORTED", "VERIFIED"):
        return CanonicalVerificationVerdict.ELIGIBLE
    elif norm_label in ("REFUTED", "DISCREPANCY"):
        return CanonicalVerificationVerdict.REFUTED
    elif norm_label in ("INSUFFICIENT_EVIDENCE", "MISSING_SOURCE_EVIDENCE"):
        return CanonicalVerificationVerdict.INSUFFICIENT
    elif norm_label == "UNCHECKED":
        return CanonicalVerificationVerdict.UNCHECKED
    else:
        logger.warning("Unrecognized verification label: '%s'", raw_label)
        return CanonicalVerificationVerdict.INVALID


# ─── 2. CLINICAL EQUIVALENCE CHECK ───────────────────────────────────────────


def are_clinically_equivalent(
    c1: Claim,
    c2: Claim,
    field_slot: str,
    *,
    abs_tol: float = 1e-4,
    rel_tol: float = 1e-3,
) -> bool:
    """Evaluate whether two claims for the same slot are clinically equivalent.

    Equivalence is slot-type aware:
      - Quantitative (lab.value, med.dose): numeric equivalence within precision/tolerance,
        identical or convertible units.
      - Qualitative (lab.flag, med.route, med.frequency, qualitative lab.value):
        exact canonical code or alias match.
      - Drug names (med.name): matching RxNorm CUI or case-insensitive cleaned name match.
      - Duration (med.duration): numeric and unit duration match.
    """
    v1_raw = c1.value
    v2_raw = c2.value

    if v1_raw is None and v2_raw is None:
        return True
    if v1_raw is None or v2_raw is None:
        return False

    # 1. Exact string match after basic trimming
    s1 = str(v1_raw).strip()
    s2 = str(v2_raw).strip()
    if s1.lower() == s2.lower():
        # Check units if quantitative
        if "value" in field_slot or "dose" in field_slot:
            u1 = (c1.unit_raw or "").strip().lower()
            u2 = (c2.unit_raw or "").strip().lower()
            if u1 and u2 and u1 != u2:
                # Different unit strings on identical values -> not equivalent (e.g. 10 mg vs 10 g)
                return False
        return True

    # 2. Medication Name Equivalence
    if field_slot == "med.name" or "name" in field_slot:
        # Check RxNorm code match
        if c1.normalized_code and c2.normalized_code:
            if c1.code_system == c2.code_system and c1.normalized_code == c2.normalized_code:
                return True
        # Clean standard prefixes (e.g. "Medication: Metformin" -> "metformin")
        clean1 = re.sub(r"^(?:medication|drug|rx):\s*", "", s1, flags=re.IGNORECASE).strip().lower()
        clean2 = re.sub(r"^(?:medication|drug|rx):\s*", "", s2, flags=re.IGNORECASE).strip().lower()
        return clean1 == clean2

    # 3. Route Equivalence
    if "route" in field_slot:
        route_aliases = {
            "oral": {"po", "by mouth", "p.o.", "oral", "per os"},
            "intravenous": {"iv", "i.v.", "intravenous"},
            "intramuscular": {"im", "i.m.", "intramuscular"},
            "sublingual": {"sl", "subling", "sublingual"},
            "topical": {"topical", "top"},
            "ou": {"both eyes", "ou", "o.u."},
            "od": {"right eye", "od", "o.d."},
            "os": {"left eye", "os", "o.s."},
        }
        clean1 = re.sub(r"^.*?route:\s*", "", s1, flags=re.IGNORECASE).strip().lower()
        clean2 = re.sub(r"^.*?route:\s*", "", s2, flags=re.IGNORECASE).strip().lower()
        if clean1 == clean2:
            return True
        for canon, aliases in route_aliases.items():
            if clean1 in aliases and clean2 in aliases:
                return True
        return False

    # 4. Frequency Equivalence
    if "frequency" in field_slot:
        freq_aliases = {
            "daily": {"daily", "once daily", "qday", "qd", "q.d.", "every day", "1x daily"},
            "bid": {"twice daily", "bid", "b.i.d.", "2x daily", "every 12 hours", "q12h"},
            "tid": {"three times daily", "tid", "t.i.d.", "3x daily", "every 8 hours", "q8h"},
            "qid": {"four times daily", "qid", "q.i.d.", "4x daily", "every 6 hours", "q6h"},
            "prn": {"as needed", "prn", "p.r.n."},
            "bedtime": {"at bedtime", "bedtime", "qhs", "q.h.s.", "nightly"},
            "morning": {"in the morning", "morning", "qam", "every morning"},
        }
        clean1 = re.sub(r"^.*?frequency:\s*", "", s1, flags=re.IGNORECASE).strip().lower()
        clean2 = re.sub(r"^.*?frequency:\s*", "", s2, flags=re.IGNORECASE).strip().lower()
        if clean1 == clean2:
            return True
        for canon, aliases in freq_aliases.items():
            if clean1 in aliases and clean2 in aliases:
                return True
        return False

    # 5. Flag Equivalence
    if "flag" in field_slot:
        flag_aliases = {
            "h": {"h", "high", "abnormal high", "hh", "critical high"},
            "l": {"l", "low", "abnormal low", "ll", "critical low"},
            "normal": {"normal", "n", "norm", "within normal limits", "wnl"},
        }
        clean1 = re.sub(r"^.*?flag:\s*", "", s1, flags=re.IGNORECASE).strip().lower()
        clean2 = re.sub(r"^.*?flag:\s*", "", s2, flags=re.IGNORECASE).strip().lower()
        if clean1 == clean2:
            return True
        for canon, aliases in flag_aliases.items():
            if clean1 in aliases and clean2 in aliases:
                return True
        return False

    # 6. Quantitative Numeric Equivalence (lab.value, med.dose)
    if "value" in field_slot or "dose" in field_slot:
        # Check units
        u1 = (c1.unit_raw or "").strip().lower()
        u2 = (c2.unit_raw or "").strip().lower()

        # If both provide units and units differ, check for known unit conversion (e.g. g/dL vs mg/dL)
        unit_scale = 1.0
        if u1 and u2 and u1 != u2:
            if u1 == "g/dl" and u2 == "mg/dl":
                unit_scale = 1000.0  # 1 g/dL = 1000 mg/dL
            elif u1 == "mg/dl" and u2 == "g/dl":
                unit_scale = 0.001
            elif u1 == "g/l" and u2 == "g/dl":
                unit_scale = 0.1     # 1 g/L = 0.1 g/dL
            elif u1 == "g/dl" and u2 == "g/l":
                unit_scale = 10.0
            else:
                # Incompatible or unknown units
                return False

        # Attempt numeric comparison
        num1 = c1.value_numeric
        num2 = c2.value_numeric

        if num1 is None:
            m1 = re.search(r"[-+]?\d*\.?\d+", s1)
            if m1:
                try:
                    num1 = float(m1.group())
                except ValueError:
                    num1 = None
        if num2 is None:
            m2 = re.search(r"[-+]?\d*\.?\d+", s2)
            if m2:
                try:
                    num2 = float(m2.group())
                except ValueError:
                    num2 = None

        if num1 is not None and num2 is not None:
            adj_num1 = num1 * unit_scale
            if math.isnan(adj_num1) or math.isnan(num2):
                return False
            # Absolute tolerance
            if abs(adj_num1 - num2) <= abs_tol:
                return True
            # Relative tolerance for values > 100
            if abs(num2) > 100.0:
                return abs(adj_num1 - num2) / abs(num2) <= rel_tol
            return False

    return False


# ─── 3. DETERMINISTIC SAFE SLOT SELECTION ENGINE ─────────────────────────────


def select_deterministic_slot_candidate(
    candidates: Sequence[Claim],
    verification_map: dict[str, Any],
    field_slot: str,
    group_id: str,
    audit_trail: Optional[list[dict[str, Any]]] = None,
    audit_service: Optional[Any] = None,
    session: Optional[Any] = None,
    document_id: Optional[str] = None,
) -> tuple[Optional[Claim], list[dict[str, Any]]]:
    """Select a single candidate deterministically under M-FHIR-SAFE-SELECTION v3.2 invariants.

    Invariants:
      1. Human review precedence:
         - Any REJECTED, ESCALATED, or PENDING candidate in the slot binds the slot.
           Silently falling back to an unreviewed model candidate is strictly forbidden.
           Slot fails closed.
         - Multiple conflicting ACCEPTED candidates fail closed (ESCALATED_CONFLICT).
         - Single authoritative ACCEPTED candidate (or reviewer edit replacement) wins.
           Superseded EDITED claims and unreviewed model candidates are shadowed.
      2. Autonomous selection (entirely unreviewed slots):
         - Filter by normalized verification verdict: only CanonicalVerificationVerdict.ELIGIBLE passes.
         - Fallback claims (is_fallback=True) and negated claims are strictly ineligible.
         - If 0 eligible: fail closed.
         - If 1 eligible: single winner.
         - If > 1 eligible: complete pairwise compatibility check.
           - ANY pair incompatible -> fail closed immediately (contradiction).
           - ALL pairs compatible -> select deterministic representative:
             1. origin == ClaimOrigin.REVIEWER (if any)
             2. highest confidence (only after clinical equivalence is established)
             3. lowest UUID string (lexicographical ascending)
           - Equivalent non-winners preserved in shadowed_records for EXT_COMPETING_CANDIDATE.
    """
    if not candidates:
        return None, []

    if len(candidates) == 1:
        c = candidates[0]
        # Evaluate single candidate eligibility
        rev_state = (
            (c.review_state.value if hasattr(c.review_state, "value") else str(c.review_state)).upper()
            if hasattr(c, "review_state") and c.review_state is not None
            else None
        )
        if rev_state in ("REJECTED", "ESCALATED", "PENDING"):
            _record_unresolved_slot(
                group_id=group_id,
                field_slot=field_slot,
                reason=f"Slot blocked by human review state: {rev_state}",
                candidates=[c],
                audit_trail=audit_trail,
                audit_service=audit_service,
                session=session,
                document_id=document_id,
            )
            return None, []
        if rev_state == "EDITED":
            _record_unresolved_slot(
                group_id=group_id,
                field_slot=field_slot,
                reason="Slot contains superseded EDITED version without active replacement",
                candidates=[c],
                audit_trail=audit_trail,
                audit_service=audit_service,
                session=session,
                document_id=document_id,
            )
            return None, []

        if rev_state == "ACCEPTED":
            return c, []

        # Unreviewed single candidate
        if getattr(c, "is_fallback", False) or getattr(c, "negated", False):
            return None, []
        ver = verification_map.get(c.claim_id)
        if normalize_verification_verdict(ver) != CanonicalVerificationVerdict.ELIGIBLE:
            _record_unresolved_slot(
                group_id=group_id,
                field_slot=field_slot,
                reason="Single candidate lacks ELIGIBLE verification verdict",
                candidates=[c],
                audit_trail=audit_trail,
                audit_service=audit_service,
                session=session,
                document_id=document_id,
            )
            return None, []
        return c, []

    # Partition candidates by human review status
    reviewed_candidates = [
        c for c in candidates
        if hasattr(c, "review_state") and c.review_state is not None
    ]
    unreviewed_candidates = [
        c for c in candidates
        if not (hasattr(c, "review_state") and c.review_state is not None)
    ]

    # ─── PATHWAY A: HUMAN REVIEW PRECEDENCE ──────────────────────────────────
    if reviewed_candidates:
        # Check for any binding human suppression (REJECTED, ESCALATED, PENDING)
        binding_suppression = next(
            (
                (c.review_state.value if hasattr(c.review_state, "value") else str(c.review_state)).upper()
                for c in reviewed_candidates
                if (c.review_state.value if hasattr(c.review_state, "value") else str(c.review_state)).upper()
                in ("REJECTED", "ESCALATED", "PENDING")
            ),
            None,
        )

        if binding_suppression:
            # Anti-fallback invariant: human rejection/escalation/pending binds the entire slot
            shadowed_records: list[dict[str, Any]] = []
            for c in candidates:
                ver = verification_map.get(c.claim_id)
                s_rec = {
                    "group_id": group_id,
                    "slot": field_slot,
                    "selected_claim_id": None,
                    "selected_value": None,
                    "competing_claim_id": c.claim_id,
                    "competing_value": c.value,
                    "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
                    "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
                    "selection_reason": (
                        f"Human review decision ({binding_suppression}) blocks slot export; "
                        "silent model fallback forbidden"
                    ),
                }
                shadowed_records.append(s_rec)
                if audit_trail is not None:
                    audit_trail.append(s_rec)

            _record_unresolved_slot(
                group_id=group_id,
                field_slot=field_slot,
                reason=f"HUMAN_REVIEW_SUPPRESSION: Slot blocked by {binding_suppression}; silent model fallback forbidden",
                candidates=candidates,
                audit_trail=audit_trail,
                audit_service=audit_service,
                session=session,
                document_id=document_id,
            )
            return None, shadowed_records

        # Collect ACCEPTED candidates
        accepted_candidates = [
            c for c in reviewed_candidates
            if (c.review_state.value if hasattr(c.review_state, "value") else str(c.review_state)).upper() == "ACCEPTED"
        ]

        if not accepted_candidates:
            # All reviewed candidates are superseded EDITED versions
            _record_unresolved_slot(
                group_id=group_id,
                field_slot=field_slot,
                reason="Slot contains only superseded EDITED versions without active ACCEPTED replacement",
                candidates=candidates,
                audit_trail=audit_trail,
                audit_service=audit_service,
                session=session,
                document_id=document_id,
            )
            return None, []

        # Check for multiple conflicting ACCEPTED candidates
        if len(accepted_candidates) > 1:
            all_accepted_equiv = True
            for i in range(len(accepted_candidates)):
                for j in range(i + 1, len(accepted_candidates)):
                    if not are_clinically_equivalent(accepted_candidates[i], accepted_candidates[j], field_slot):
                        all_accepted_equiv = False
                        break
                if not all_accepted_equiv:
                    break

            if not all_accepted_equiv:
                # Conflicting authoritative human reviews fail closed and escalate
                shadowed_records = []
                for c in candidates:
                    s_rec = {
                        "group_id": group_id,
                        "slot": field_slot,
                        "selected_claim_id": None,
                        "selected_value": None,
                        "competing_claim_id": c.claim_id,
                        "competing_value": c.value,
                        "competing_confidence": 1.0,
                        "competing_label": "HUMAN_ACCEPTED",
                        "selection_reason": "Multiple authoritative human-reviewed candidates conflict; escalated fail-closed",
                    }
                    shadowed_records.append(s_rec)
                    if audit_trail is not None:
                        audit_trail.append(s_rec)

                _record_unresolved_slot(
                    group_id=group_id,
                    field_slot=field_slot,
                    reason="ESCALATED_CONFLICT: Multiple authoritative human-reviewed candidates conflict in slot",
                    candidates=candidates,
                    audit_trail=audit_trail,
                    audit_service=audit_service,
                    session=session,
                    document_id=document_id,
                )
                return None, shadowed_records

        # Deterministic winner among equivalent accepted candidates
        def accepted_sort_key(c: Claim) -> tuple:
            # Prefer reviewer-origin, then lowest UUID
            is_rev = 1 if getattr(c, "origin", None) == ClaimOrigin.REVIEWER else 0
            return (-is_rev, str(c.claim_id))

        sorted_accepted = sorted(accepted_candidates, key=accepted_sort_key)
        winner = sorted_accepted[0]

        shadowed_records = []
        for other in candidates:
            if other.claim_id == winner.claim_id:
                continue
            o_ver = verification_map.get(other.claim_id)
            o_rev = getattr(other, "review_state", None)
            reason = (
                "Superseded EDITED claim version suppressed"
                if o_rev and (o_rev.value if hasattr(o_rev, "value") else str(o_rev)).upper() == "EDITED"
                else "Human-accepted candidate selected; autonomous/alternative candidate shadowed"
            )
            s_rec = {
                "group_id": group_id,
                "slot": field_slot,
                "selected_claim_id": winner.claim_id,
                "selected_value": winner.value,
                "competing_claim_id": other.claim_id,
                "competing_value": other.value,
                "competing_confidence": float(getattr(o_ver, "confidence", 0.0) or 0.0) if o_ver else 0.0,
                "competing_label": str(getattr(o_ver, "label", "UNVERIFIED")) if o_ver else "UNVERIFIED",
                "selection_reason": reason,
            }
            shadowed_records.append(s_rec)
            if audit_trail is not None:
                audit_trail.append(s_rec)

        return winner, shadowed_records

    # ─── PATHWAY B: AUTONOMOUS CANDIDATE SELECTION ───────────────────────────
    # Entirely unreviewed candidates: filter by verification eligibility
    eligible_candidates: list[Claim] = []
    ineligible_candidates: list[tuple[Claim, str]] = []

    for c in unreviewed_candidates:
        if getattr(c, "is_fallback", False):
            ineligible_candidates.append((c, "Fallback claim output is not verified fact"))
            continue
        if getattr(c, "negated", False):
            ineligible_candidates.append((c, "Clinically negated claim cannot generate positive observation"))
            continue

        ver = verification_map.get(c.claim_id)
        verdict = normalize_verification_verdict(ver)
        if verdict == CanonicalVerificationVerdict.ELIGIBLE:
            eligible_candidates.append(c)
        else:
            ineligible_candidates.append((c, f"Normalized verification verdict is '{verdict.value}' (not ELIGIBLE)"))

    if not eligible_candidates:
        # Fail closed: zero eligible candidates
        shadowed_records = []
        for c, reason in ineligible_candidates:
            ver = verification_map.get(c.claim_id)
            s_rec = {
                "group_id": group_id,
                "slot": field_slot,
                "selected_claim_id": None,
                "selected_value": None,
                "competing_claim_id": c.claim_id,
                "competing_value": c.value,
                "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
                "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
                "selection_reason": reason,
            }
            shadowed_records.append(s_rec)
            if audit_trail is not None:
                audit_trail.append(s_rec)

        _record_unresolved_slot(
            group_id=group_id,
            field_slot=field_slot,
            reason="NO_ELIGIBLE_CANDIDATES: All candidates in slot are unverified, refuted, or fallback",
            candidates=candidates,
            audit_trail=audit_trail,
            audit_service=audit_service,
            session=session,
            document_id=document_id,
        )
        return None, shadowed_records

    if len(eligible_candidates) == 1:
        winner = eligible_candidates[0]
        shadowed_records = []
        for c, reason in ineligible_candidates:
            ver = verification_map.get(c.claim_id)
            s_rec = {
                "group_id": group_id,
                "slot": field_slot,
                "selected_claim_id": winner.claim_id,
                "selected_value": winner.value,
                "competing_claim_id": c.claim_id,
                "competing_value": c.value,
                "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
                "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
                "selection_reason": reason,
            }
            shadowed_records.append(s_rec)
            if audit_trail is not None:
                audit_trail.append(s_rec)
        return winner, shadowed_records

    # Multiple eligible candidates: complete pairwise compatibility check
    all_pairs_equivalent = True
    incompatible_pair = None

    for i in range(len(eligible_candidates)):
        for j in range(i + 1, len(eligible_candidates)):
            c_i = eligible_candidates[i]
            c_j = eligible_candidates[j]
            if not are_clinically_equivalent(c_i, c_j, field_slot):
                all_pairs_equivalent = False
                incompatible_pair = (c_i, c_j)
                break
        if not all_pairs_equivalent:
            break

    if not all_pairs_equivalent:
        # Incompatible pair -> whole slot fails closed!
        c_i, c_j = incompatible_pair
        logger.warning(
            "Slot '%s' in group '%s' contains incompatible eligible candidates: '%s' vs '%s'. Failing closed.",
            field_slot,
            group_id,
            c_i.value,
            c_j.value,
        )
        shadowed_records = []
        for c in candidates:
            ver = verification_map.get(c.claim_id)
            s_rec = {
                "group_id": group_id,
                "slot": field_slot,
                "selected_claim_id": None,
                "selected_value": None,
                "competing_claim_id": c.claim_id,
                "competing_value": c.value,
                "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
                "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
                "selection_reason": (
                    "Incompatible competing candidates in slot; clinical contradiction fails closed "
                    f"('{c_i.value}' vs '{c_j.value}')"
                ),
            }
            shadowed_records.append(s_rec)
            if audit_trail is not None:
                audit_trail.append(s_rec)

        _record_unresolved_slot(
            group_id=group_id,
            field_slot=field_slot,
            reason=f"SLOT_CONTRADICTION: Incompatible candidates in slot ('{c_i.value}' vs '{c_j.value}')",
            candidates=candidates,
            audit_trail=audit_trail,
            audit_service=audit_service,
            session=session,
            document_id=document_id,
        )
        return None, shadowed_records

    # All pairs are clinically equivalent! Deterministic representative selection
    def candidate_sort_key(c: Claim) -> tuple:
        ver = verification_map.get(c.claim_id)
        ver_conf = float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0
        ext_conf = float(getattr(c, "extraction_confidence", 0.0) or 0.0)
        # Use confidence only AFTER clinical equivalence has been proven
        best_conf = max(ver_conf, ext_conf)
        return (-best_conf, str(c.claim_id))

    sorted_eligible = sorted(eligible_candidates, key=candidate_sort_key)
    winner = sorted_eligible[0]

    shadowed_records = []
    # Shadowed equivalent candidates (eligible for EXT_COMPETING_CANDIDATE)
    for other in sorted_eligible[1:]:
        ver = verification_map.get(other.claim_id)
        s_rec = {
            "group_id": group_id,
            "slot": field_slot,
            "selected_claim_id": winner.claim_id,
            "selected_value": winner.value,
            "competing_claim_id": other.claim_id,
            "competing_value": other.value,
            "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
            "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
            "selection_reason": "Clinically equivalent redundant extraction candidate",
        }
        shadowed_records.append(s_rec)
        if audit_trail is not None:
            audit_trail.append(s_rec)

    # Ineligible candidates shadowed
    for c, reason in ineligible_candidates:
        ver = verification_map.get(c.claim_id)
        s_rec = {
            "group_id": group_id,
            "slot": field_slot,
            "selected_claim_id": winner.claim_id,
            "selected_value": winner.value,
            "competing_claim_id": c.claim_id,
            "competing_value": c.value,
            "competing_confidence": float(getattr(ver, "confidence", 0.0) or 0.0) if ver else 0.0,
            "competing_label": str(getattr(ver, "label", "UNVERIFIED")) if ver else "UNVERIFIED",
            "selection_reason": reason,
        }
        shadowed_records.append(s_rec)
        if audit_trail is not None:
            audit_trail.append(s_rec)

    return winner, shadowed_records


def _record_unresolved_slot(
    group_id: str,
    field_slot: str,
    reason: str,
    candidates: Sequence[Claim],
    audit_trail: Optional[list[dict[str, Any]]] = None,
    audit_service: Optional[Any] = None,
    session: Optional[Any] = None,
    document_id: Optional[str] = None,
) -> None:
    """Record an unresolved/suppressed slot in the audit trail and append-only AuditEvent ledger."""
    entry = {
        "group_id": group_id,
        "slot": field_slot,
        "status": "UNRESOLVED_SUPPRESSED",
        "reason": reason,
        "candidate_count": len(candidates),
        "candidate_claim_ids": [c.claim_id for c in candidates],
    }
    if audit_trail is not None:
        audit_trail.append(entry)

    if audit_service and session and document_id:
        try:
            metadata = {
                "document_id": str(document_id),
                "group_id": str(group_id),
                "field_slot": str(field_slot),
                "reason": str(reason),
                "candidate_count": len(candidates),
                "candidate_claim_ids": [str(c.claim_id) for c in candidates],
                "is_export_eligible": False,
            }
            audit_service.record_event(
                session=session,
                actor_id="system:fhir_safe_selection",
                event_type="SLOT_UNRESOLVED",
                entity_type="slot",
                entity_id=f"{document_id}:{group_id}:{field_slot}",
                action="SUPPRESS",
                metadata_json=metadata,
            )
        except Exception as exc:
            logger.error("Failed to record SLOT_UNRESOLVED audit event: %s", exc)

