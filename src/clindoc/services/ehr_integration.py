"""EHR Integration Service for Controlled Clinical Pilot Transmission.

Authority:
  - Phase 8.12: Controlled Clinical Pilot / Live EHR Integration Boundary
  - PRODUCTIZATION_SPEC.md §1.2, §4.3, §7.2, §9.3, §9.4, §10
  - PRODUCTIZATION_DECISIONS.md PD-002, PD-009, PD-012, PD-015
  - PROJECT_SPEC.md §15, §16
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import hashlib
import json
import logging
import re
import time
from typing import Any, Optional, Sequence, Set, Tuple
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from clindoc.config import (
    ProductionProfileError,
    RuntimeProfile,
    acquire_document_xact_lock,
    get_active_profile,
)
from clindoc.api.audit.service import AuditService, validate_metadata_phi_free
from clindoc.db.enums import (
    ClaimReviewState,
    DocumentApprovalStatus,
    DocumentStatus,
    EHRResolutionStatus,
    EHRTransmissionState,
    FHIRExportStatus,
    FHIRVersion,
    IdentityVerificationStatus,
    PatientIdentityApprovalStatus,
    ReconciliationDecision,
)
from clindoc.db.models import (
    AuditEvent,
    Claim,
    Document as DBDocument,
    DocumentApproval,
    EHRTransmissionReconciliation,
    FHIRBundle,
    FHIRExport,
)
from clindoc.fhir.signing import (
    AuditKeyRevokedError,
    AuditKeyUnavailableError,
    HMACKeyring,
    IdentityAttestationError,
    verify_identity_attestation,
)
from clindoc.observability.metrics import get_metrics
from clindoc.services.ehr_transport import (
    EHRAuthenticationError,
    EHRDuplicateSubmissionError,
    EHRTimeoutError,
    EHRTransmissionResponse,
    EHRTransportError,
    EHRValidationError,
    FHIRTransport,
    MockFHIRTransport,
)
from clindoc.services.fhir_export import (
    FHIRExportEligibilityError,
    FHIRExportNotFoundError,
    FHIRExportResult,
    FHIRExportService,
)
from clindoc.services.reconciliation_service import (
    ReconciliationError,
    ReconciliationService,
    ReconciliationTokenAlreadyConsumedError,
    ReconciliationTokenRevokedError,
)

logger = logging.getLogger(__name__)


def canonicalize_destination_identity(destination_identity: str) -> str:
    """Canonicalize destination identity URL/URI per Phase A specification:
    - Lowercase
    - Trimmed whitespace
    - Normalized scheme (e.g. 'https', 'http', 'ehr')
    - Default ports omitted (:80 for http, :443 for https)
    - Trailing slashes removed
    """
    dest = str(destination_identity).strip().lower()
    if not dest:
        return ""
    from urllib.parse import urlparse, urlunparse
    try:
        parsed = urlparse(dest)
        if parsed.scheme:
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname.lower() if parsed.hostname else ""
            port = parsed.port
            # Omit default ports
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                netloc = hostname
            elif port:
                netloc = f"{hostname}:{port}"
            elif parsed.netloc:
                netloc = parsed.netloc.lower()
            else:
                netloc = ""

            path = parsed.path.rstrip("/")
            canonical_dest = urlunparse((
                scheme,
                netloc,
                path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            ))
            return canonical_dest.rstrip("/")
    except Exception:
        pass
    return dest.rstrip("/")


HEX64_REGEX = re.compile(r"^[0-9a-f]{64}$")


def derive_destination_idempotency_key(
    destination_identity: str,
    logical_export_id: uuid.UUID | str,
    bundle_sha256: str,
) -> str:
    """Deterministic SHA-256 derivation of destination idempotency key (Phase A).
    
    Formula: SHA256(canonical_destination + ":" + logical_export_id + ":" + canonical_bundle_sha256)
    
    Invariants:
      - canonical_destination: lowercase, trimmed whitespace, normalized scheme, trailing slashes removed, default ports omitted
      - logical_export_id: canonical lowercase, hyphenated UUIDv4 string
      - canonical_bundle_sha256: lowercase 64-character hexadecimal string
    """
    canonical_dest = canonicalize_destination_identity(destination_identity)
    
    # 1. Enforce UUIDv4
    if isinstance(logical_export_id, uuid.UUID):
        parsed_uuid = logical_export_id
    else:
        try:
            parsed_uuid = uuid.UUID(str(logical_export_id).strip())
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"Invalid logical_export_id: {logical_export_id}") from exc

    if parsed_uuid.version != 4:
        raise ValueError(
            f"logical_export_id must be a UUIDv4, got version {parsed_uuid.version}: {logical_export_id}"
        )
    canonical_logical_id = str(parsed_uuid).lower()

    # 2. Validate entire bundle hash against strict 64-character lowercase hexadecimal regex
    canonical_bundle = str(bundle_sha256).strip().lower()
    if not HEX64_REGEX.fullmatch(canonical_bundle):
        raise ValueError(
            f"bundle_sha256 must be a 64-character hexadecimal string matching [0-9a-f]{{64}}, got: {bundle_sha256!r}"
        )
        
    data = f"{canonical_dest}:{canonical_logical_id}:{canonical_bundle}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ─── EXCEPTIONS ───────────────────────────────────────────────────────────────


class EHRIntegrationError(Exception):
    """Base exception for EHR integration and safety boundary violations."""


class EHRPatientIdentityError(EHRIntegrationError):
    """Raised when patient identity is not confirmed, mismatched, or missing."""


class EHRApprovalMissingError(EHRIntegrationError):
    """Raised when Four-Eyes document approval is missing or not export-eligible."""


class EHRUnresolvedClaimError(EHRIntegrationError):
    """Raised when the document contains unreviewed, fallback, or unresolved claims."""


class EHRAmbiguousTransmissionError(EHRIntegrationError):
    """Raised when re-transmission is attempted after a prior ambiguous transmission timeout.

    A prior attempt for this document/bundle resulted in a transport timeout and was durably recorded
    with UNKNOWN_RECONCILIATION_REQUIRED. Re-transmission is blocked to protect non-idempotent
    destinations from duplicate clinical record creation. An explicit, auditable reconciliation
    decision from a controlled vocabulary is required before any re-transmission can proceed.
    """


STANDARD_RECONCILIATION_DECISIONS = frozenset({
    "CONFIRMED_NOT_RECEIVED_RETRY_PERMITTED",
    "MANUAL_REPAIR_COMPLETE_RETRY_PERMITTED",
})


# ─── DATA CONTRACTS ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OutboxAttemptCreationResult:
    """Immutable outcome descriptor returned after Phase A Boundary A commits."""

    attempt_id: uuid.UUID
    attempt_number: int
    logical_export_id: uuid.UUID
    destination_idempotency_key: str
    transmission_state: EHRTransmissionState
    resolution_status: EHRResolutionStatus


@dataclass(frozen=True)
class EHROutcomePersistenceResult:
    """Immutable outcome descriptor returned after Phase A Boundary C commits."""

    attempt_id: uuid.UUID
    transmission_state: EHRTransmissionState
    resolution_status: EHRResolutionStatus
    export_status: FHIRExportStatus
    error_message: Optional[str] = None


@dataclass(frozen=True)
class EHRTransmissionResult:
    """Outcome of an EHR transmission operation."""

    document_id: str
    bundle_id: str
    bundle_sha256: str
    destination_type: str
    remote_message_id: Optional[str]
    success: bool
    status_code: int
    idempotency_key: str
    latency_seconds: float
    export_id: Optional[str] = None
    audit_event_id: Optional[str] = None
    correlation_id: Optional[str] = None
    attempt_number: Optional[int] = None
    logical_export_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert transmission summary to safe non-PHI dictionary."""
        return {
            "document_id": self.document_id,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "destination_type": self.destination_type,
            "remote_message_id": self.remote_message_id,
            "success": self.success,
            "status_code": self.status_code,
            "idempotency_key": self.idempotency_key,
            "latency_seconds": round(self.latency_seconds, 4),
            "export_id": self.export_id,
            "audit_event_id": self.audit_event_id,
        }


# ─── PRE-DISPATCH INVARIANT VERIFIER (Phase A TOCTOU Protection) ─────────────


def _verify_pre_dispatch_invariants(
    session: Session,
    doc: DBDocument,
    latest_approval: DocumentApproval,
    keyring: Optional[HMACKeyring] = None,
    allow_synthetic_identity: bool = False,
) -> Tuple[bool, str]:
    """Dynamically verify the 6 export eligibility invariants immediately prior to physical dispatch.

    Crucially, this check supports both APPROVED documents (first export) and EXPORTED documents
    (legitimate retry or dispatch of pre-generated bundle) WITHOUT mutating or flushing doc.status.
    Concurrent code and ORM state will NEVER observe an artificial intermediate status.

    Clauses:
      1. Document State:
         - Document must have status in (APPROVED, EXPORTED).
         - Must not be REJECTED, DRAFT, IN_REVIEW, ARCHIVED, etc.
         - latest_approval must be DocumentApprovalStatus.APPROVED and is_export_eligible=True.
      2. Patient Identity State:
         - Must be CONFIRMED or MISMATCH_ACCEPTED.
         - doc.identity_confirmed_by must be set.
      3. Dual-Authorization Verification:
         - If MISMATCH_ACCEPTED: dual human authorization (supervisor_actor_id != actor_id)
           and standardized reason code in STANDARD_MISMATCH_REASON_CODES.
      4. Cryptographic Attestation Verification:
         - RFC 8785 canonical JSON attestation with valid HMAC-SHA256 signature.
         - Key not revoked (raising AuditKeyRevokedError) and not unavailable (AuditKeyUnavailableError).
         - Bound to document.id, document.sha256, and patient_context_id.
      5. Patient Identifier System & Token Invariants:
         - Non-empty patient_context_id.
         - In production profile, rejects synthetic tokens (synthetic-patient-*, patient-*).
      6. Terminal Claims:
         - All active claims must be in terminal review states (ACCEPTED, EDITED, REJECTED).
         - Document must contain active claims.
    """
    # Clause 1: Document & Approval State
    status_val = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
    if status_val not in (DocumentStatus.APPROVED.value, DocumentStatus.EXPORTED.value):
        return False, f"DOCUMENT_NOT_APPROVED: Current state is '{status_val}'."

    if latest_approval.approval_status != DocumentApprovalStatus.APPROVED:
        return False, f"LATEST_APPROVAL_NOT_APPROVED: Latest approval state is '{latest_approval.approval_status}'."

    if not latest_approval.is_export_eligible:
        return False, "EXPORT_ELIGIBILITY_REVOKED: Latest approval has is_export_eligible=False."

    # Clause 2: Patient Identity Verification Status
    id_status = (
        doc.identity_verification_status.value
        if hasattr(doc.identity_verification_status, "value")
        else str(doc.identity_verification_status)
    )
    if id_status not in (
        IdentityVerificationStatus.CONFIRMED.value,
        IdentityVerificationStatus.MISMATCH_ACCEPTED.value,
    ):
        return False, f"PATIENT_IDENTITY_NOT_VERIFIED: Identity status is '{id_status}'."

    if doc.identity_confirmed_by is None:
        return False, "IDENTITY_CONFIRMED_BY_MISSING: Document lacks confirming actor ID."

    # Clause 3: Dual-Authorization Verification for Identity Mismatches
    if id_status == IdentityVerificationStatus.MISMATCH_ACCEPTED.value:
        latest_mismatch_audit = (
            session.query(AuditEvent)
            .filter(
                AuditEvent.entity_id == str(doc.id),
                AuditEvent.action == "ACCEPT_IDENTITY_MISMATCH",
            )
            .order_by(AuditEvent.sequence_number.desc())
            .first()
        )
        if not latest_mismatch_audit or not latest_mismatch_audit.metadata_json:
            return False, "MISMATCH_AUDIT_TRAIL_MISSING: Audit event for mismatch acceptance not found."

        meta = latest_mismatch_audit.metadata_json
        supervisor_id = meta.get("supervisor_actor_id")
        primary_actor_id = meta.get("confirmed_by") or str(doc.identity_confirmed_by)
        reason_code = meta.get("reason_code")

        if not supervisor_id or not str(supervisor_id).strip():
            return False, "MISMATCH_SUPERVISOR_AUTHORIZATION_MISSING: Missing supervisor dual-authorization."

        if str(supervisor_id).strip() == str(primary_actor_id).strip():
            return False, "MISMATCH_FOUR_EYES_VIOLATION: Supervisor actor equals primary reviewing actor."

        if not reason_code or reason_code not in STANDARD_MISMATCH_REASON_CODES:
            return (
                False,
                f"MISMATCH_INVALID_REASON_CODE: Reason '{reason_code}' not in standard vocabulary {sorted(STANDARD_MISMATCH_REASON_CODES)}.",
            )

    # Clause 4: Cryptographic Attestation Verification
    id_audit_event = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.entity_id == str(doc.id),
            AuditEvent.action.in_(["CONFIRM_IDENTITY", "ACCEPT_IDENTITY_MISMATCH"]),
        )
        .order_by(AuditEvent.sequence_number.desc())
        .first()
    )
    if not id_audit_event or not id_audit_event.metadata_json or "identity_attestation" not in id_audit_event.metadata_json:
        profile = get_active_profile()
        if profile == RuntimeProfile.PRODUCTION:
            return False, "IDENTITY_ATTESTATION_MISSING: No cryptographically signed attestation found in audit ledger."
    else:
        attestation = id_audit_event.metadata_json["identity_attestation"]
        current_state = {
            "document_id": str(doc.id),
            "document_content_hash": str(doc.sha256 or ""),
            "patient_context_id": str(doc.patient_context_id or ""),
            "identity_status": id_status,
        }
        try:
            verify_identity_attestation(attestation, current_state, keyring=keyring)
        except AuditKeyRevokedError as exc:
            return False, f"KEY_REVOKED: {exc}"
        except AuditKeyUnavailableError as exc:
            return False, f"KEY_UNAVAILABLE: {exc}"
        except IdentityAttestationError as exc:
            return False, f"IDENTITY_ATTESTATION_INVALID: {exc}"

    # Clause 5: Patient Identifier Validation
    if not doc.patient_context_id or not str(doc.patient_context_id).strip():
        return False, "PATIENT_CONTEXT_ID_MISSING: Document has no patient_context_id."

    pat_id_str = str(doc.patient_context_id).strip()
    profile = get_active_profile()
    if profile == RuntimeProfile.PRODUCTION and not allow_synthetic_identity:
        pat_lower = pat_id_str.lower()
        if pat_lower.startswith("synthetic-patient-") or pat_lower.startswith("patient-"):
            return (
                False,
                f"SYNTHETIC_PATIENT_IDENTIFIER_FORBIDDEN_IN_PRODUCTION: Placeholder '{pat_id_str}' cannot be exported.",
            )

    # Clause 6: Terminal Claims
    all_claims = session.query(Claim).filter(Claim.document_id == doc.id).all()
    if not all_claims:
        return False, "NO_CLAIMS_FOR_DOCUMENT: Document contains no extracted claims."

    superseded_ids = {c.supersedes_claim_id for c in all_claims if c.supersedes_claim_id is not None}
    active_claims = [c for c in all_claims if c.id not in superseded_ids]
    if not active_claims:
        return False, "NO_ACTIVE_CLAIMS: All claims for this document are superseded."

    terminal_states = {
        ClaimReviewState.ACCEPTED.value,
        ClaimReviewState.EDITED.value,
        ClaimReviewState.REJECTED.value,
    }
    non_terminal = [
        c
        for c in active_claims
        if (c.review_state.value if hasattr(c.review_state, "value") else str(c.review_state)) not in terminal_states
    ]
    if non_terminal:
        return (
            False,
            f"UNRESOLVED_CLAIMS: {len(non_terminal)} claims remain in non-terminal review states.",
        )

    return True, "ELIGIBLE"


# ─── SERVICE IMPLEMENTATION ──────────────────────────────────────────────────


class EHRIntegrationService:
    """Production-grade EHR Integration Adapter.

    Architecture & Safety Boundaries:
      1. Concurrency & Dispatch Serialization:
         - All operations capable of invalidating export eligibility (approval rejection,
           identity confirmation/mismatch acceptance, claim reopening/corrections, and EHR dispatch)
           acquire the identical document advisory transaction lock via `acquire_document_xact_lock(session, doc_uuid)`.
         - In `transmit_document_to_ehr`, this lock is acquired at Step 0, before ANY document reads,
           and held continuously through pre-dispatch invariant verification and physical socket transmission
           until transaction commit or rollback.
      2. Pre-Dispatch Dynamic Invariant Verification (TOCTOU Defense):
         - Immediately prior to physical socket transmission, under the active transaction lock,
           `_verify_pre_dispatch_invariants()` re-evaluates all 6 eligibility clauses.
         - Supports both APPROVED documents and legitimate already-EXPORTED retry contexts WITHOUT
           mutating or flushing `doc.status`, guaranteeing concurrent readers never observe invalid states.
      3. External Transmission Uncertainty & Idempotency:
         - External REST/FHIR transmissions are not distributed two-phase commit (2PC) transactions.
         - If a network timeout (`EHRTimeoutError`) occurs after data has crossed the socket boundary,
           ClinicalDoc cannot know whether the destination EHR committed the payload. Local database rollback
           does not and cannot retract the external transmission.
         - To handle this uncertainty safely:
           a) Network timeouts are classified as `transmission_state="UNKNOWN_RECONCILIATION_REQUIRED"`.
           b) Retries reuse the deterministic `idempotency_key = f"ehr-{doc_uuid}-{bundle_sha256}"`,
              allowing idempotency-compliant EHRs to deduplicate transmissions safely.
           c) For non-idempotent EHRs, clinical/HIM reconciliation is mandatory before manual re-transmission.
      4. Identity Correction Lifecycle:
         - Local document rejection (`reject_document`) sets `is_export_eligible=False` and blocks future exports.
         - However, local database state cannot retract clinical data already accepted by a remote EHR.
         - Correction of already-transmitted documents requires an explicit out-of-band clinical workflow:
           sending a FHIR correction transaction (e.g. updating resource status to `entered-in-error` or
           submitting an amended bundle) and notifying the destination health facility's HIM department.
    """

    def __init__(
        self,
        audit_service: Optional[AuditService] = None,
        transport: Optional[FHIRTransport] = None,
        export_service: Optional[FHIRExportService] = None,
        audit_ledger: Optional[Any] = None,
        trust_store: Optional[Dict[str, Any]] = None,
        revoked_key_ids: Optional[Set[str]] = None,
        session_factory: Optional[sessionmaker] = None,
        reconciliation_service: Optional[ReconciliationService] = None,
        stale_pending_timeout_seconds: float = 300.0,
    ) -> None:
        self.session_factory = session_factory
        self.audit_service = audit_service or AuditService()
        self.transport = transport or MockFHIRTransport()
        self.export_service = export_service or FHIRExportService(audit_service=self.audit_service)
        self.reconciliation_service = reconciliation_service or ReconciliationService(audit_service=self.audit_service)
        self.audit_ledger = audit_ledger
        self.trust_store = trust_store
        self.revoked_key_ids = revoked_key_ids or set()
        self.stale_pending_timeout_seconds = stale_pending_timeout_seconds

    def transmit_document_to_ehr(
        self,
        session: Optional[Session] = None,
        *,
        document_id: uuid.UUID | str,
        actor_id: uuid.UUID | str,
        destination_type: str = "sandbox",
        destination_identity: Optional[str] = None,
        correlation_id: Optional[str] = None,
        allow_resubmission: bool = False,
        reconciliation_decision: Optional[str] = None,
        reconciliation_justification: Optional[str] = None,
        reconciliation_token: Optional[uuid.UUID | str] = None,
        session_factory: Optional[sessionmaker] = None,
        stale_timeout_override: Optional[float] = None,
    ) -> EHRTransmissionResult:
        """Deliver a verified, approved FHIR Bundle to the destination EHR boundary.

        Enforces the frozen Three-Boundary Lifecycle:
          Boundary A: Authorization, attempt lineage, reconciliation token consumption, PENDING_TRANSPORT.
          Boundary B: Physical network transmission without active DB transaction or connection.
          Boundary C: Atomically persist terminal or failure outcome and corresponding audit events.
        """
        doc_uuid = uuid.UUID(str(document_id)) if not isinstance(document_id, uuid.UUID) else document_id
        actor_uuid = uuid.UUID(str(actor_id)) if not isinstance(actor_id, uuid.UUID) else actor_id
        corr_id = correlation_id or str(uuid.uuid4())
        new_attempt_id = uuid.uuid4()
        start_time = time.perf_counter()

        # ─── PRE-FLIGHT COMPATIBILITY VALIDATION (IF CALLER_SESSION PROVIDED) ───
        initial_doc_status: Optional[DocumentStatus] = None
        pre_doc: Optional[DBDocument] = None

        if session is not None:
            # Check document existence on caller session
            pre_doc = session.query(DBDocument).filter(DBDocument.id == doc_uuid).first()
            if not pre_doc:
                get_metrics().record_ehr_transmission("failure", destination=destination_type)
                raise FHIRExportNotFoundError(f"Document with ID '{doc_uuid}' not found.")

            initial_doc_status = pre_doc.status

            latest_appr = (
                session.query(DocumentApproval)
                .filter(DocumentApproval.document_id == doc_uuid)
                .order_by(DocumentApproval.created_at.desc(), DocumentApproval.id.desc())
                .first()
            )
            if not latest_appr:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRApprovalMissingError(
                    f"Document '{doc_uuid}' has not undergone Four-Eyes approval. "
                    "Transmission to EHR is strictly prohibited prior to formal clinician approval (PD-002, PD-009)."
                )

            if latest_appr.approval_status != DocumentApprovalStatus.APPROVED:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRApprovalMissingError(
                    f"Document '{doc_uuid}' is not approved (Current status: {latest_appr.approval_status}). "
                    "Only APPROVED documents may be transmitted to EHR."
                )

            if latest_appr.patient_identity_status not in (
                PatientIdentityApprovalStatus.CONFIRMED,
                PatientIdentityApprovalStatus.MISMATCH_ACCEPTED,
            ):
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRPatientIdentityError(
                    f"Document '{doc_uuid}' patient identity is not confirmed (Status: {latest_appr.patient_identity_status}). "
                    "Transmission to EHR requires strictly CONFIRMED patient identity (PD-015)."
                )

            if not latest_appr.is_export_eligible:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                if latest_appr.patient_identity_status != PatientIdentityApprovalStatus.CONFIRMED:
                    raise EHRPatientIdentityError(
                        f"Document '{doc_uuid}' patient identity is not confirmed (Status: {latest_appr.patient_identity_status}). "
                        "Transmission to EHR requires strictly CONFIRMED patient identity (PD-015)."
                    )
                raise EHRApprovalMissingError(
                    f"Document '{doc_uuid}' approval record has is_export_eligible=False. Transmission blocked."
                )

            # Reconcile document status and identity to match active Four-Eyes approval on caller session
            if pre_doc.status == DocumentStatus.IN_REVIEW and latest_appr.approval_status == DocumentApprovalStatus.APPROVED:
                pre_doc.status = DocumentStatus.APPROVED
                if (
                    latest_appr.patient_identity_status == PatientIdentityApprovalStatus.CONFIRMED
                    and pre_doc.identity_verification_status != IdentityVerificationStatus.CONFIRMED
                ):
                    pre_doc.identity_verification_status = IdentityVerificationStatus.CONFIRMED
                    if pre_doc.identity_confirmed_by is None:
                        pre_doc.identity_confirmed_by = latest_appr.approver_id
                session.flush()

            # Pre-flight bundle/claim eligibility validation on caller session
            existing_bundle_pre = (
                session.query(FHIRBundle)
                .filter(FHIRBundle.document_id == doc_uuid)
                .first()
            )
            if not existing_bundle_pre:
                self.export_service.export_document(
                    session=session,
                    document_id=doc_uuid,
                    exported_by=actor_uuid,
                    target_type="EHR_TRANSMISSION",
                    correlation_id=corr_id,
                )

        # ─── REQUIRE EXPLICIT SESSION_FACTORY FOR TRANSMISSION WORKFLOW ─────────
        effective_factory = session_factory or self.session_factory or getattr(session, "session_factory", None)
        if effective_factory is None:
            raise ValueError(
                "An explicit session_factory is required for EHR transmission workflow. "
                "Deriving sessions from caller_session or get_bind() is strictly forbidden."
            )

        # ═══════════════════════════════════════════════════════════════════════
        # BOUNDARY A: AUTHORIZATION, ATTEMPT CREATION & ATOMIC TOKEN CONSUMPTION
        # ═══════════════════════════════════════════════════════════════════════
        canonical_dest = str(destination_identity).strip() if destination_identity else f"ehr://{destination_type}"
        bundle_json: str
        bundle_sha256: str
        db_bundle_id: uuid.UUID
        dest_idem_key: str
        logical_export_id: uuid.UUID
        attempt_number: int
        parent_attempt_id: Optional[uuid.UUID]

        session_a: Session = effective_factory()
        try:
            acquire_document_xact_lock(session_a, doc_uuid)

            if get_active_profile() == RuntimeProfile.PRODUCTION and self.audit_ledger is None:
                raise ProductionProfileError(
                    "Audit ledger is mandatory for EHR transmission in PRODUCTION profile."
                )

            doc_a = session_a.query(DBDocument).filter(DBDocument.id == doc_uuid).first()
            if not doc_a:
                get_metrics().record_ehr_transmission("failure", destination=destination_type)
                raise FHIRExportNotFoundError(f"Document with ID '{doc_uuid}' not found.")

            latest_approval = (
                session_a.query(DocumentApproval)
                .filter(DocumentApproval.document_id == doc_uuid)
                .order_by(DocumentApproval.created_at.desc(), DocumentApproval.id.desc())
                .first()
            )
            if not latest_approval:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRApprovalMissingError(f"Document '{doc_uuid}' has not undergone Four-Eyes approval.")

            if latest_approval.approval_status != DocumentApprovalStatus.APPROVED:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRApprovalMissingError(f"Document '{doc_uuid}' is not approved.")

            if latest_approval.patient_identity_status not in (
                PatientIdentityApprovalStatus.CONFIRMED,
                PatientIdentityApprovalStatus.MISMATCH_ACCEPTED,
            ):
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRPatientIdentityError(f"Document '{doc_uuid}' patient identity is not confirmed.")

            if not latest_approval.is_export_eligible:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                raise EHRApprovalMissingError(f"Document '{doc_uuid}' approval has is_export_eligible=False.")

            # Dynamic pre-dispatch invariants (attestations, key revocation, claims)
            keyring = getattr(self.audit_ledger, "keyring", None) if self.audit_ledger else None
            if keyring is None and self.revoked_key_ids:
                keyring = HMACKeyring(revoked_key_ids=self.revoked_key_ids)
            elif keyring is not None and self.revoked_key_ids:
                for k_id in self.revoked_key_ids:
                    keyring.revoke_key(k_id)

            is_dispatch_eligible, dispatch_reason = _verify_pre_dispatch_invariants(
                session=session_a,
                doc=doc_a,
                latest_approval=latest_approval,
                keyring=keyring,
            )
            if not is_dispatch_eligible:
                get_metrics().record_ehr_transmission("rejected", destination=destination_type)
                if "KEY_REVOKED" in dispatch_reason:
                    raise AuditKeyRevokedError(f"Document '{doc_uuid}' dispatch blocked by revoked signing key: {dispatch_reason}")
                if "KEY_UNAVAILABLE" in dispatch_reason:
                    raise AuditKeyUnavailableError(f"Document '{doc_uuid}' dispatch blocked by unavailable signing key: {dispatch_reason}")
                if "IDENTITY" in dispatch_reason or "PATIENT" in dispatch_reason or "MISMATCH" in dispatch_reason:
                    raise EHRPatientIdentityError(f"Document '{doc_uuid}' failed pre-dispatch export eligibility: {dispatch_reason}")
                raise EHRApprovalMissingError(f"Document '{doc_uuid}' failed pre-dispatch export eligibility: {dispatch_reason}")

            # Resolve / Generate FHIR Bundle
            existing_bundle = (
                session_a.query(FHIRBundle)
                .filter(FHIRBundle.document_id == doc_uuid)
                .order_by(FHIRBundle.created_at.desc())
                .first()
            )
            if existing_bundle:
                db_bundle_id = existing_bundle.id
                bundle_dict = existing_bundle.bundle_payload
                if self.audit_ledger is not None:
                    from clindoc.audit.projection import AuditProjectionEngine
                    transport_bytes, verified_sha256 = AuditProjectionEngine.verify_bundle_complete_attestation(
                        session=session_a,
                        bundle_dict=bundle_dict,
                        ledger_path=self.audit_ledger.ledger_path,
                        checkpoint_path=self.audit_ledger.checkpoint_path,
                        trust_store=self.trust_store,
                        revoked_key_ids=self.revoked_key_ids,
                        keyring=self.audit_ledger.keyring,
                        key_id=self.audit_ledger.signing_key_id,
                    )
                    bundle_sha256 = verified_sha256
                    bundle_json = transport_bytes.decode("utf-8")
                else:
                    from clindoc.fhir.signing import serialize_transport_payload
                    transport_bytes, verified_sha256 = serialize_transport_payload(bundle_dict)
                    bundle_sha256 = verified_sha256
                    if existing_bundle.bundle_sha256 != verified_sha256:
                        existing_bundle.bundle_sha256 = verified_sha256
                    bundle_json = transport_bytes.decode("utf-8")
            else:
                export_result = self.export_service.export_document(
                    session=session_a,
                    document_id=doc_uuid,
                    exported_by=actor_uuid,
                    target_type="EHR_TRANSMISSION",
                    correlation_id=corr_id,
                )
                db_bundle_id = export_result.db_bundle_id
                bundle_sha256 = export_result.bundle_sha256
                bundle_json = export_result.bundle_json

            # Idempotency and lineage query for this bundle & destination
            prior_attempts = (
                session_a.query(FHIRExport)
                .filter(
                    FHIRExport.bundle_id == db_bundle_id,
                    FHIRExport.destination_identity == canonical_dest,
                )
                .order_by(FHIRExport.attempt_number.desc())
                .all()
            )

            # Check if any prior attempt was already transmitted
            for p in prior_attempts:
                if p.export_status == FHIRExportStatus.TRANSMITTED and p.transmission_state == EHRTransmissionState.TRANSMITTED:
                    if not allow_resubmission:
                        get_metrics().record_ehr_transmission("duplicate", destination=destination_type)
                        raise EHRDuplicateSubmissionError(
                            f"Document '{doc_uuid}' was already transmitted to EHR under export {p.id}."
                        )

            token_to_consume = reconciliation_token
            if prior_attempts:
                latest_attempt = prior_attempts[0]

                # Check active in-flight or stale PENDING_TRANSPORT
                if latest_attempt.transmission_state == EHRTransmissionState.PENDING_TRANSPORT:
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    exp_time = (
                        latest_attempt.exported_at.replace(tzinfo=datetime.timezone.utc)
                        if latest_attempt.exported_at.tzinfo is None
                        else latest_attempt.exported_at
                    )
                    stale_duration = (now_utc - exp_time).total_seconds()
                    stale_limit = stale_timeout_override or self.stale_pending_timeout_seconds

                    if stale_duration >= stale_limit:
                        # Stale PENDING_TRANSPORT recovery -> transitions to UNKNOWN_RECONCILIATION_REQUIRED
                        latest_attempt.export_status = FHIRExportStatus.FAILED
                        latest_attempt.transmission_state = EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
                        latest_attempt.resolution_status = EHRResolutionStatus.UNRESOLVED
                        latest_attempt.error_message = (
                            f"[STALE_RECOVERY] Abandoned in PENDING_TRANSPORT for {int(stale_duration)}s; "
                            "transitioned to UNKNOWN_RECONCILIATION_REQUIRED"
                        )
                        self.audit_service.record_event(
                            session=session_a,
                            actor_id=str(actor_uuid),
                            event_type="EHR_TRANSMISSION_STALE_RECOVERED",
                            entity_type="DOCUMENT",
                            entity_id=str(doc_uuid),
                            action="STALE_PENDING_TRANSPORT_RECOVERED",
                            metadata_json={
                                "prior_attempt_id": str(latest_attempt.id),
                                "stale_duration_seconds": round(stale_duration, 2),
                            },
                            correlation_id=corr_id,
                        )
                        session_a.commit()
                        raise EHRAmbiguousTransmissionError(
                            f"Prior attempt '{latest_attempt.id}' was abandoned in PENDING_TRANSPORT and has been safely "
                            "transitioned to UNKNOWN_RECONCILIATION_REQUIRED. Clinical reconciliation is required before retry."
                        )
                    else:
                        raise EHRDuplicateSubmissionError(
                            f"A transmission attempt is currently in progress for document '{doc_uuid}' (PENDING_TRANSPORT)."
                        )

                # Check ambiguous or historical indeterminate state
                if latest_attempt.transmission_state in (
                    EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
                    EHRTransmissionState.HISTORICAL_INDETERMINATE,
                ):
                    if not token_to_consume:
                        raise EHRAmbiguousTransmissionError(
                            f"Document '{doc_uuid}' has an unresolved prior ambiguous transmission timeout "
                            f"(Export ID: {latest_attempt.id}) marked UNKNOWN_RECONCILIATION_REQUIRED. "
                            "Retransmission requires an authorized Four-Eyes reconciliation token."
                        )

                # Subsequent attempt lineage
                logical_export_id = latest_attempt.logical_export_id or uuid.uuid4()
                dest_idem_key = latest_attempt.destination_idempotency_key or derive_destination_idempotency_key(
                    canonical_dest, logical_export_id, bundle_sha256
                )
                attempt_number = latest_attempt.attempt_number + 1
                parent_attempt_id = latest_attempt.id
            else:
                # First attempt
                logical_export_id = uuid.uuid4()
                dest_idem_key = derive_destination_idempotency_key(canonical_dest, logical_export_id, bundle_sha256)
                attempt_number = 1
                parent_attempt_id = None

            now = datetime.datetime.now(datetime.timezone.utc)
            new_attempt = FHIRExport(
                id=new_attempt_id,
                bundle_id=db_bundle_id,
                target_type="EHR_TRANSMISSION",
                export_status=FHIRExportStatus.PENDING_TRANSPORT,
                transmission_state=EHRTransmissionState.PENDING_TRANSPORT,
                resolution_status=EHRResolutionStatus.UNRESOLVED,
                destination_identity=canonical_dest,
                destination_idempotency_key=dest_idem_key,
                logical_export_id=logical_export_id,
                attempt_number=attempt_number,
                parent_attempt_id=parent_attempt_id,
                idempotency_key=f"ehr-{logical_export_id}-att-{attempt_number}",
                destination_path=canonical_dest,
                attempts=attempt_number,
                exported_by=actor_uuid,
                exported_at=now,
            )
            session_a.add(new_attempt)
            session_a.flush()

            # Atomically validate and consume reconciliation token in Boundary A after attempt row is flushed
            if token_to_consume is not None:
                self.reconciliation_service.validate_and_consume_token(
                    session=session_a,
                    token_id=token_to_consume,
                    new_attempt_id=new_attempt_id,
                    expected_bundle_id=db_bundle_id,
                    expected_destination_identity=canonical_dest,
                )

            # Record EHR_TRANSMISSION_ATTEMPTED audit event
            self.audit_service.record_event(
                session=session_a,
                actor_id=str(actor_uuid),
                event_type="EHR_TRANSMISSION_ATTEMPTED",
                entity_type="DOCUMENT",
                entity_id=str(doc_uuid),
                action="TRANSMIT_TO_EHR",
                metadata_json={
                    "bundle_sha256": bundle_sha256,
                    "destination_type": destination_type,
                    "destination_identity": canonical_dest,
                    "destination_idempotency_key": dest_idem_key,
                    "logical_export_id": str(logical_export_id),
                    "attempt_number": attempt_number,
                    "parent_attempt_id": str(parent_attempt_id) if parent_attempt_id else None,
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
            session_a.commit()
            get_metrics().record_ehr_transmission("attempt", destination=destination_type)
        finally:
            session_a.close()

        # ═══════════════════════════════════════════════════════════════════════
        # BOUNDARY B: PHYSICAL NETWORK TRANSMISSION (NO DB SESSION/CONNECTION)
        # ═══════════════════════════════════════════════════════════════════════
        resp: Optional[EHRTransmissionResponse] = None
        network_outcome: str = "UNKNOWN"
        network_error: Optional[Exception] = None

        try:
            resp = self.transport.transmit(
                bundle_json,
                idempotency_key=dest_idem_key,
                correlation_id=corr_id,
                document_id=str(doc_uuid),
                expected_transport_sha256=bundle_sha256,
            )
            network_outcome = "SUCCESS"
        except EHRTimeoutError as exc:
            network_outcome = "AMBIGUOUS_TIMEOUT"
            network_error = exc
        except (EHRAuthenticationError, EHRValidationError) as exc:
            network_outcome = "CONFIRMED_FAILED"
            network_error = exc
        except EHRDuplicateSubmissionError as exc:
            network_outcome = "DUPLICATE_REJECTED"
            network_error = exc
        except Exception as exc:
            # Ambiguous socket/connection drops after sending
            network_outcome = "AMBIGUOUS_TIMEOUT"
            network_error = exc

        elapsed = time.perf_counter() - start_time

        # ═══════════════════════════════════════════════════════════════════════
        # BOUNDARY C: OUTCOME PERSISTENCE (DEDICATED OUTCOME SESSION)
        # ═══════════════════════════════════════════════════════════════════════
        session_c: Session = effective_factory()
        try:
            dialect_c = session_c.bind.dialect.name if session_c.bind else "sqlite"
            q = session_c.query(FHIRExport).filter(FHIRExport.id == new_attempt_id)
            if dialect_c == "postgresql":
                q = q.with_for_update()
            attempt_row = q.first()

            if not attempt_row:
                logger.error("Attempt row %s missing in Boundary C", new_attempt_id)
                if network_error:
                    raise network_error
                raise EHRTransportError(f"Attempt row '{new_attempt_id}' disappeared.")

            # Late Outcome Check: A late outcome from an older attempt must NEVER overwrite newer state
            if attempt_row.transmission_state != EHRTransmissionState.PENDING_TRANSPORT:
                self.audit_service.record_event(
                    session=session_c,
                    actor_id=str(actor_uuid),
                    event_type="EHR_TRANSMISSION_STALE_DELIVERY",
                    entity_type="DOCUMENT",
                    entity_id=str(doc_uuid),
                    action="STALE_TRANSMISSION_OUTCOME_PRESERVED",
                    metadata_json={
                        "attempt_id": str(new_attempt_id),
                        "stale_outcome": network_outcome,
                        "current_attempt_state": attempt_row.transmission_state.value,
                        "error": str(network_error) if network_error else None,
                    },
                    correlation_id=corr_id,
                )
                session_c.commit()
                if network_error:
                    raise network_error
                return EHRTransmissionResult(
                    document_id=str(doc_uuid),
                    bundle_id=str(db_bundle_id),
                    bundle_sha256=bundle_sha256,
                    destination_type=destination_type,
                    remote_message_id=getattr(resp, "remote_message_id", None),
                    success=True,
                    status_code=getattr(resp, "status_code", 200),
                    idempotency_key=dest_idem_key,
                    latency_seconds=elapsed,
                    export_id=str(attempt_row.id),
                    audit_event_id=None,
                    correlation_id=corr_id,
                )

            if network_outcome == "SUCCESS":
                assert resp is not None
                attempt_row.export_status = FHIRExportStatus.TRANSMITTED
                attempt_row.transmission_state = EHRTransmissionState.TRANSMITTED
                attempt_row.resolution_status = EHRResolutionStatus.RESOLVED_DELIVERED
                attempt_row.destination_path = resp.remote_message_id or canonical_dest

                doc_c = session_c.query(DBDocument).filter(DBDocument.id == doc_uuid).first()
                if doc_c:
                    doc_c.status = DocumentStatus.EXPORTED

                audit_success = self.audit_service.record_event(
                    session=session_c,
                    actor_id=str(actor_uuid),
                    event_type="EHR_TRANSMISSION_SUCCESS",
                    entity_type="DOCUMENT",
                    entity_id=str(doc_uuid),
                    action="TRANSMIT_TO_EHR_CONFIRMED",
                    metadata_json={
                        "bundle_sha256": bundle_sha256,
                        "destination_type": destination_type,
                        "destination_identity": canonical_dest,
                        "destination_idempotency_key": dest_idem_key,
                        "remote_message_id": resp.remote_message_id,
                        "status_code": resp.status_code,
                        "latency_seconds": round(elapsed, 4),
                        "attempt_number": attempt_number,
                        "logical_export_id": str(logical_export_id),
                    },
                    correlation_id=corr_id,
                )
                session_c.commit()
                get_metrics().record_ehr_transmission("success", destination=destination_type, duration_seconds=elapsed)

                return EHRTransmissionResult(
                    document_id=str(doc_uuid),
                    bundle_id=str(db_bundle_id),
                    bundle_sha256=bundle_sha256,
                    destination_type=destination_type,
                    remote_message_id=resp.remote_message_id,
                    success=True,
                    status_code=resp.status_code,
                    idempotency_key=dest_idem_key,
                    latency_seconds=elapsed,
                    export_id=str(attempt_row.id),
                    audit_event_id=str(audit_success.id),
                    correlation_id=corr_id,
                )

            elif network_outcome == "CONFIRMED_FAILED":
                if pre_doc is not None and initial_doc_status is not None:
                    pre_doc.status = initial_doc_status
                    if session is not None:
                        session.flush()

                attempt_row.export_status = FHIRExportStatus.FAILED
                attempt_row.transmission_state = EHRTransmissionState.CONFIRMED_FAILED
                attempt_row.resolution_status = EHRResolutionStatus.RESOLVED_NON_DELIVERY
                attempt_row.error_message = f"[CONFIRMED_FAILED] {network_error}"

                self.audit_service.record_event(
                    session=session_c,
                    actor_id=str(actor_uuid),
                    event_type="EHR_TRANSMISSION_FAILED",
                    entity_type="DOCUMENT",
                    entity_id=str(doc_uuid),
                    action="TRANSMIT_TO_EHR_FAILED",
                    metadata_json={
                        "bundle_sha256": bundle_sha256,
                        "destination_type": destination_type,
                        "destination_identity": canonical_dest,
                        "destination_idempotency_key": dest_idem_key,
                        "attempt_number": attempt_number,
                        "logical_export_id": str(logical_export_id),
                        "transmission_state": "CONFIRMED_FAILED",
                        "resolution_status": "RESOLVED_NON_DELIVERY",
                        "error_type": "CONFIRMED_FAILED",
                        "error": str(network_error),
                        "reconciliation_required": False,
                    },
                    correlation_id=corr_id,
                )
                session_c.commit()
                get_metrics().record_ehr_transmission("failure", destination=destination_type, duration_seconds=elapsed)
                assert network_error is not None
                raise network_error

            else:  # AMBIGUOUS_TIMEOUT or DUPLICATE_REJECTED
                if pre_doc is not None and initial_doc_status is not None:
                    pre_doc.status = initial_doc_status
                    if session is not None:
                        session.flush()

                attempt_row.export_status = FHIRExportStatus.FAILED
                attempt_row.transmission_state = EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
                attempt_row.resolution_status = EHRResolutionStatus.UNRESOLVED
                attempt_row.error_message = f"[TIMEOUT] [STATE:UNKNOWN_RECONCILIATION_REQUIRED] {network_error}"

                self.audit_service.record_event(
                    session=session_c,
                    actor_id=str(actor_uuid),
                    event_type="EHR_TRANSMISSION_FAILED",
                    entity_type="DOCUMENT",
                    entity_id=str(doc_uuid),
                    action="TRANSMIT_TO_EHR_FAILED",
                    metadata_json={
                        "bundle_sha256": bundle_sha256,
                        "destination_type": destination_type,
                        "destination_identity": canonical_dest,
                        "destination_idempotency_key": dest_idem_key,
                        "attempt_number": attempt_number,
                        "logical_export_id": str(logical_export_id),
                        "transmission_state": "UNKNOWN_RECONCILIATION_REQUIRED",
                        "resolution_status": "UNRESOLVED",
                        "error_type": "TIMEOUT",
                        "error": str(network_error),
                        "reconciliation_required": True,
                    },
                    correlation_id=corr_id,
                )
                session_c.commit()
                get_metrics().record_ehr_transmission("timeout", destination=destination_type, duration_seconds=elapsed)
                assert network_error is not None
                raise network_error
        finally:
            session_c.close()

    def _handle_transmission_failure(
        self,
        session: Session,
        doc: DBDocument,
        initial_doc_status: DocumentStatus,
        db_bundle_id: Optional[uuid.UUID],
        actor_uuid: uuid.UUID,
        idempotency_key: str,
        destination_type: str,
        error_type: str,
        error_message: str,
        corr_id: str,
        bundle_sha256: str,
        elapsed: float,
    ) -> None:
        """Record structured failure without mutating document verification state.

        External Transmission Uncertainty:
        When a timeout (EHRTimeoutError) occurs at the physical socket/transport boundary,
        ClinicalDoc cannot know with certainty whether the remote EHR received, processed,
        and committed the message, or whether the network failed before delivery.
        Local database transaction rollback does NOT and CANNOT retract an external network
        transmission. Therefore:
        1. The export attempt is explicitly classified with transmission_state='UNKNOWN_RECONCILIATION_REQUIRED'.
        2. Duplicate submission protection relies on the deterministic idempotency_key
           f"ehr-{doc_uuid}-{bundle_sha256}", allowing an idempotency-compliant EHR to recognize
           and deduplicate retried transmissions safely.
        3. For non-idempotent EHRs or persistent timeouts, clinical and HIM reconciliation is
           mandated before any manual re-transmission.
        """
        try:
            # Document status remains as-is (e.g. APPROVED/IN_REVIEW), NEVER falsely marked as EXPORTED.
            doc.status = initial_doc_status
            session.flush()

            is_timeout = error_type == "TIMEOUT"
            transmission_state = EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED if is_timeout else EHRTransmissionState.CONFIRMED_FAILED
            resolution_status = EHRResolutionStatus.UNRESOLVED if is_timeout else EHRResolutionStatus.RESOLVED_NON_DELIVERY
            dest_ident = f"ehr://{destination_type}"

            # Record failed FHIRExport row for operational audit
            if db_bundle_id:
                failed_export = FHIRExport(
                    bundle_id=db_bundle_id,
                    target_type="EHR_TRANSMISSION",
                    export_status=FHIRExportStatus.FAILED,
                    transmission_state=transmission_state,
                    resolution_status=resolution_status,
                    destination_identity=dest_ident,
                    idempotency_key=f"{idempotency_key}-failed-{uuid.uuid4().hex[:6]}",
                    destination_path=dest_ident,
                    exported_by=actor_uuid,
                    error_message=f"[{error_type}] [STATE:{transmission_state.value}] {error_message[:450]}",
                    exported_at=datetime.datetime.now(datetime.timezone.utc),
                )
                session.add(failed_export)
                session.flush()

            # Record failure audit event
            self.audit_service.record_event(
                session=session,
                actor_id=str(actor_uuid),
                event_type="EHR_TRANSMISSION_FAILED",
                entity_type="DOCUMENT",
                entity_id=str(doc.id),
                action="TRANSMIT_TO_EHR_FAILED",
                metadata_json={
                    "bundle_sha256": bundle_sha256,
                    "destination_type": destination_type,
                    "error_type": error_type,
                    "transmission_state": transmission_state,
                    "reconciliation_required": is_timeout,
                    "idempotency_key": idempotency_key,
                    "correlation_id": corr_id,
                    "latency_seconds": round(elapsed, 4),
                },
                correlation_id=corr_id,
            )
            session.flush()
        except Exception as exc:
            logger.error("Failed to record EHR transmission failure audit event: %s", exc)

    def record_transmission_reconciliation(
        self,
        session: Session,
        *,
        document_id: uuid.UUID | str,
        actor_id: uuid.UUID | str,
        reconciliation_decision: str,
        clinical_justification: str,
        correlation_id: Optional[str] = None,
    ) -> AuditEvent:
        """Record an explicit, auditable clinical/technical reconciliation decision for a timed-out transmission.

        Required before any re-transmission of a document with a prior UNKNOWN_RECONCILIATION_REQUIRED export.
        """
        doc_uuid = uuid.UUID(str(document_id)) if not isinstance(document_id, uuid.UUID) else document_id
        actor_uuid = uuid.UUID(str(actor_id)) if not isinstance(actor_id, uuid.UUID) else actor_id
        corr_id = correlation_id or str(uuid.uuid4())

        acquire_document_xact_lock(session, doc_uuid)

        if reconciliation_decision not in STANDARD_RECONCILIATION_DECISIONS:
            raise EHRAmbiguousTransmissionError(
                f"Invalid reconciliation decision '{reconciliation_decision}'. "
                f"Must be one of {sorted(STANDARD_RECONCILIATION_DECISIONS)}."
            )

        justification = (clinical_justification or "").strip()
        if len(justification) < 15:
            raise EHRAmbiguousTransmissionError(
                "Reconciliation justification must be at least 15 characters of substantive clinical/technical explanation."
            )

        # Find latest ambiguous export
        latest_ambiguous = (
            session.query(FHIRExport)
            .join(FHIRBundle, FHIRExport.bundle_id == FHIRBundle.id)
            .filter(
                FHIRBundle.document_id == doc_uuid,
                FHIRExport.export_status == FHIRExportStatus.FAILED,
                FHIRExport.error_message.like("%UNKNOWN_RECONCILIATION_REQUIRED%"),
            )
            .order_by(FHIRExport.exported_at.desc(), FHIRExport.id.desc())
            .first()
        )
        if not latest_ambiguous:
            raise EHRAmbiguousTransmissionError(
                f"No prior ambiguous transmission timeout found for document '{doc_uuid}'."
            )

        audit_ev = self.audit_service.record_event(
            session=session,
            actor_id=str(actor_uuid),
            event_type="EHR_TRANSMISSION_RECONCILED",
            entity_type="DOCUMENT",
            entity_id=str(doc_uuid),
            action="RECONCILE_TRANSMISSION_TIMEOUT",
            metadata_json={
                "prior_failed_export_id": str(latest_ambiguous.id),
                "reconciliation_decision": reconciliation_decision,
                "clinical_justification": justification,
                "correlation_id": corr_id,
            },
            correlation_id=corr_id,
        )
        session.flush()
        return audit_ev

    def create_outbox_transmission_attempt(
        self,
        outbox_id: uuid.UUID | str,
        document_id: uuid.UUID | str,
        logical_export_id: uuid.UUID | str,
        destination_identity: str,
        destination_idempotency_key: str,
        actor_id: uuid.UUID | str,
        correlation_id: Optional[str] = None,
        reconciliation_token: Optional[str] = None,
    ) -> OutboxAttemptCreationResult:
        """Phase A Boundary A: Create dedicated initial or retry transmission attempt.

        Ownership Contract:
          - Does NOT accept a caller-supplied Session.
          - Acquires a dedicated session from self.session_factory.
          - Acquires document concurrency lock (PG advisory lock / SQLite immediate).
          - Validates document approval, confirmed identity status, and export eligibility.
          - If reconciliation_token provided: validates and atomically consumes token.
          - Creates FHIRExport in PENDING_TRANSPORT bound to outbox_id.
          - Emits EHR_TRANSMISSION_INITIATED audit event to hash-chained ledger.
          - COMMITS and CLOSES the dedicated session.
          - Returns OutboxAttemptCreationResult ONLY after commit succeeds and session closes.
        """
        if self.session_factory is None:
            raise EHRIntegrationError(
                "EHRIntegrationService cannot execute create_outbox_transmission_attempt without an explicit session_factory."
            )

        outbox_uuid = uuid.UUID(str(outbox_id)) if not isinstance(outbox_id, uuid.UUID) else outbox_id
        doc_uuid = uuid.UUID(str(document_id)) if not isinstance(document_id, uuid.UUID) else document_id
        logical_uuid = uuid.UUID(str(logical_export_id)) if not isinstance(logical_export_id, uuid.UUID) else logical_export_id
        actor_uuid = uuid.UUID(str(actor_id)) if not isinstance(actor_id, uuid.UUID) else actor_id
        corr_id = correlation_id or str(uuid.uuid4())
        dest_ident = str(destination_identity).strip().lower()

        with self.session_factory() as session:
            acquire_document_xact_lock(session, doc_uuid)

            doc = session.query(DBDocument).filter(DBDocument.id == doc_uuid).first()
            if not doc:
                raise FHIRExportNotFoundError(f"Document with ID '{doc_uuid}' not found.")

            # Four-Eyes Document Approval Gate
            latest_approval = (
                session.query(DocumentApproval)
                .filter(DocumentApproval.document_id == doc_uuid)
                .order_by(DocumentApproval.created_at.desc(), DocumentApproval.id.desc())
                .first()
            )
            if not latest_approval or latest_approval.approval_status != DocumentApprovalStatus.APPROVED:
                raise EHRApprovalMissingError(
                    f"Document '{doc_uuid}' lacks required APPROVED DocumentApproval record."
                )

            if latest_approval.patient_identity_status != PatientIdentityApprovalStatus.CONFIRMED:
                raise EHRPatientIdentityError(
                    f"Document '{doc_uuid}' lacks CONFIRMED patient identity approval."
                )

            from clindoc.review.approval import evaluate_export_eligibility
            is_eligible, reason_msg, _ = evaluate_export_eligibility(
                session=session,
                document_id=doc_uuid,
                keyring=getattr(self, "keyring", None),
            )
            if not is_eligible:
                raise FHIRExportEligibilityError(
                    f"Document '{doc_uuid}' failed dynamic export eligibility: {reason_msg}"
                )

            # Discover latest attempt for attempt lineage
            latest_attempt = (
                session.query(FHIRExport)
                .filter(FHIRExport.logical_export_id == logical_uuid)
                .order_by(FHIRExport.attempt_number.desc())
                .first()
            )

            if latest_attempt is None:
                attempt_number = 1
                parent_attempt_id = None
            else:
                attempt_number = latest_attempt.attempt_number + 1
                parent_attempt_id = latest_attempt.id

                # Retry gate: must be UNKNOWN or FAILED with reconciliation_token
                if latest_attempt.transmission_state == EHRTransmissionState.TRANSMITTED:
                    raise EHRDuplicateSubmissionError(
                        f"Logical export '{logical_uuid}' has already been delivered successfully."
                    )
                if not reconciliation_token:
                    raise EHRAmbiguousTransmissionError(
                        f"Logical export '{logical_uuid}' prior attempt {latest_attempt.attempt_number} "
                        f"ended in state '{latest_attempt.transmission_state}'. Four-Eyes reconciliation token required."
                    )

            # Find finalized bundle
            bundle = (
                session.query(FHIRBundle)
                .filter(FHIRBundle.document_id == doc_uuid)
                .order_by(FHIRBundle.created_at.desc(), FHIRBundle.id.desc())
                .first()
            )
            if not bundle:
                raise FHIRExportNotFoundError(f"No FHIRBundle found for document '{doc_uuid}'.")

            new_attempt = FHIRExport(
                id=uuid.uuid4(),
                bundle_id=bundle.id,
                target_type="EHR_TRANSMISSION",
                export_status=FHIRExportStatus.PENDING_TRANSPORT,
                transmission_state=EHRTransmissionState.PENDING_TRANSPORT,
                resolution_status=EHRResolutionStatus.UNRESOLVED,
                destination_identity=dest_ident,
                destination_idempotency_key=destination_idempotency_key,
                logical_export_id=logical_uuid,
                attempt_number=attempt_number,
                parent_attempt_id=parent_attempt_id,
                idempotency_key=f"ehr-{logical_uuid}-att-{attempt_number}",
                destination_path=dest_ident,
                attempts=attempt_number,
                exported_by=actor_uuid,
                exported_at=datetime.datetime.now(datetime.timezone.utc),
                outbox_id=outbox_uuid,
            )
            session.add(new_attempt)
            session.flush()

            if reconciliation_token:
                self.reconciliation_service.validate_and_consume_token(
                    session=session,
                    token_id=reconciliation_token,
                    new_attempt_id=new_attempt.id,
                    expected_bundle_id=bundle.id,
                    expected_destination_identity=dest_ident,
                )

            self.audit_service.record_event(
                session=session,
                actor_id=str(actor_uuid),
                event_type="EHR_TRANSMISSION_INITIATED",
                entity_type="DOCUMENT",
                entity_id=str(doc.id),
                action="TRANSMIT_TO_EHR_INITIATED",
                metadata_json={
                    "outbox_id": str(outbox_uuid),
                    "logical_export_id": str(logical_uuid),
                    "attempt_number": attempt_number,
                    "bundle_sha256": bundle.bundle_sha256,
                    "destination_identity": dest_ident,
                    "destination_idempotency_key": destination_idempotency_key,
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
            session.commit()

            return OutboxAttemptCreationResult(
                attempt_id=new_attempt.id,
                attempt_number=attempt_number,
                logical_export_id=logical_uuid,
                destination_idempotency_key=destination_idempotency_key,
                transmission_state=EHRTransmissionState.PENDING_TRANSPORT,
                resolution_status=EHRResolutionStatus.UNRESOLVED,
            )

    def persist_outbox_transmission_outcome(
        self,
        attempt_id: uuid.UUID | str,
        document_id: uuid.UUID | str,
        transport_result: Optional[EHRTransmissionResult] = None,
        transport_exception: Optional[Exception] = None,
        actor_id: Optional[uuid.UUID | str] = None,
        correlation_id: Optional[str] = None,
    ) -> EHROutcomePersistenceResult:
        """Phase A Boundary C: Dedicated outcome persistence preserving Phase A rules."""
        if self.session_factory is None:
            raise EHRIntegrationError(
                "EHRIntegrationService cannot execute persist_outbox_transmission_outcome without an explicit session_factory."
            )

        attempt_uuid = uuid.UUID(str(attempt_id)) if not isinstance(attempt_id, uuid.UUID) else attempt_id
        doc_uuid = uuid.UUID(str(document_id)) if not isinstance(document_id, uuid.UUID) else document_id
        corr_id = correlation_id or str(uuid.uuid4())
        actor_str = str(actor_id) if actor_id else "SYSTEM_OUTBOX_WORKER"

        with self.session_factory() as session:
            acquire_document_xact_lock(session, doc_uuid)

            attempt = session.query(FHIRExport).filter(FHIRExport.id == attempt_uuid).first()
            if not attempt:
                raise FHIRExportNotFoundError(f"FHIRExport attempt '{attempt_uuid}' not found.")

            # Evaluate success vs failure
            if transport_result and transport_result.success:
                attempt.export_status = FHIRExportStatus.TRANSMITTED
                attempt.transmission_state = EHRTransmissionState.TRANSMITTED
                attempt.resolution_status = EHRResolutionStatus.RESOLVED_DELIVERED
                attempt.error_message = None

                self.audit_service.record_event(
                    session=session,
                    actor_id=actor_str,
                    event_type="EHR_TRANSMISSION_COMPLETED",
                    entity_type="DOCUMENT",
                    entity_id=str(doc_uuid),
                    action="TRANSMIT_TO_EHR_SUCCESS",
                    metadata_json={
                        "attempt_id": str(attempt.id),
                        "logical_export_id": str(attempt.logical_export_id),
                        "attempt_number": attempt.attempt_number,
                        "destination_idempotency_key": attempt.destination_idempotency_key,
                        "correlation_id": corr_id,
                    },
                    correlation_id=corr_id,
                )
                session.commit()
                return EHROutcomePersistenceResult(
                    attempt_id=attempt.id,
                    transmission_state=EHRTransmissionState.TRANSMITTED,
                    resolution_status=EHRResolutionStatus.RESOLVED_DELIVERED,
                    export_status=FHIRExportStatus.TRANSMITTED,
                )

            # Failure path
            is_timeout = False
            error_type = "TRANSPORT_ERROR"
            error_message = str(transport_exception or "Unknown transmission failure")

            if transport_exception:
                if isinstance(transport_exception, (EHRTimeoutError, TimeoutError)):
                    is_timeout = True
                    error_type = "TIMEOUT"
                elif isinstance(transport_exception, EHRAuthenticationError):
                    error_type = "AUTHENTICATION_FAILURE"
                elif isinstance(transport_exception, EHRValidationError):
                    error_type = "VALIDATION_ERROR"
                elif isinstance(transport_exception, EHRDuplicateSubmissionError):
                    error_type = "DUPLICATE_SUBMISSION"
                elif "504" in error_message or "502" in error_message or "503" in error_message or "timeout" in error_message.lower():
                    is_timeout = True
                    error_type = "GATEWAY_TIMEOUT"

            if is_timeout:
                attempt.export_status = FHIRExportStatus.FAILED
                attempt.transmission_state = EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
                attempt.resolution_status = EHRResolutionStatus.UNRESOLVED
            else:
                attempt.export_status = FHIRExportStatus.FAILED
                attempt.transmission_state = EHRTransmissionState.CONFIRMED_FAILED
                attempt.resolution_status = EHRResolutionStatus.RESOLVED_NON_DELIVERY

            attempt.error_message = f"[{error_type}] [STATE:{attempt.transmission_state.value}] {error_message[:450]}"

            self.audit_service.record_event(
                session=session,
                actor_id=actor_str,
                event_type="EHR_TRANSMISSION_FAILED",
                entity_type="DOCUMENT",
                entity_id=str(doc_uuid),
                action="TRANSMIT_TO_EHR_FAILED",
                metadata_json={
                    "attempt_id": str(attempt.id),
                    "logical_export_id": str(attempt.logical_export_id),
                    "attempt_number": attempt.attempt_number,
                    "error_type": error_type,
                    "transmission_state": attempt.transmission_state.value,
                    "correlation_id": corr_id,
                },
                correlation_id=corr_id,
            )
            session.commit()
            return EHROutcomePersistenceResult(
                attempt_id=attempt.id,
                transmission_state=attempt.transmission_state,
                resolution_status=attempt.resolution_status,
                export_status=attempt.export_status,
                error_message=attempt.error_message,
            )

