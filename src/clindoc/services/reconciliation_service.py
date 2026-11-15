"""Four-Eyes Clinical Reconciliation Service for Ambiguous EHR Transmissions (Phase A).

Authority:
  - Phase A Frozen Architectural Specification
  - chk_reconciliation_distinct_actors constraint
  - Trusted Database Role Lookup (CLINICIAN_REVIEWER & CLINICAL_SUPERVISOR / MEDICAL_DIRECTOR)
  - Single-Use Authorization Token Lifecycle (Authorize, Consume, Revoke)
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional, Sequence, Tuple
import uuid

from sqlalchemy.orm import Session

from clindoc.api.audit.service import AuditService
from clindoc.db.enums import (
    EHRResolutionStatus,
    EHRTransmissionState,
    FHIRExportStatus,
    ReconciliationDecision,
)
from clindoc.db.models import (
    AuditEvent,
    Document as DBDocument,
    EHRTransmissionReconciliation,
    FHIRBundle,
    FHIRExport,
    Role,
    User,
    UserRole,
)

logger = logging.getLogger(__name__)

# Trusted role sets for Four-Eyes reconciliation
PRIMARY_AUTHORIZER_ROLES = {"CLINICIAN_REVIEWER", "reviewer", "clinician"}
SUPERVISOR_AUTHORIZER_ROLES = {"CLINICAL_SUPERVISOR", "MEDICAL_DIRECTOR", "approver", "clinical_supervisor", "medical_director"}


# ─── EXCEPTIONS ───────────────────────────────────────────────────────────────


class ReconciliationError(Exception):
    """Base exception for clinical transmission reconciliation errors."""


class ReconciliationAuthorizationError(ReconciliationError):
    """Raised when authorizer identities, roles, or Four-Eyes constraints are violated."""


class ReconciliationValidationError(ReconciliationError):
    """Raised when evidence, justification, or attempt linkage is invalid."""


class ReconciliationNotFoundError(ReconciliationError):
    """Raised when a referenced reconciliation token, attempt, or document is not found."""


class ReconciliationTokenAlreadyConsumedError(ReconciliationError):
    """Raised when an authorization token was already consumed by an earlier attempt."""


class ReconciliationTokenRevokedError(ReconciliationError):
    """Raised when an authorization token was revoked prior to consumption."""


# ─── SERVICE IMPLEMENTATION ───────────────────────────────────────────────────


class ReconciliationService:
    """Orchestrates trusted Four-Eyes authorization for retrying ambiguous EHR transmissions."""

    def __init__(self, audit_service: Optional[AuditService] = None):
        self.audit_service = audit_service or AuditService()

    def get_trusted_user_roles(self, session: Session, user_id: uuid.UUID) -> set[str]:
        """Resolve effective roles for a user strictly from database records."""
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            raise ReconciliationAuthorizationError(f"User '{user_id}' does not exist in the database.")
        if not user.is_active:
            raise ReconciliationAuthorizationError(f"User '{user_id}' is deactivated.")

        user_roles = (
            session.query(UserRole.role_id)
            .filter(UserRole.user_id == user_id)
            .all()
        )
        return {r[0] for r in user_roles}

    def validate_authorizer_roles(
        self,
        session: Session,
        primary_authorizer_id: uuid.UUID,
        supervisor_authorizer_id: uuid.UUID,
    ) -> Tuple[str, str]:
        """Enforce Four-Eyes distinct actors and trusted database role validation.
        
        Never trusts caller-supplied role strings. Resolves role codes from database.
        """
        if primary_authorizer_id == supervisor_authorizer_id:
            raise ReconciliationAuthorizationError(
                "Primary authorizer and supervisor authorizer must be distinct identities (Four-Eyes principle)."
            )

        primary_roles = self.get_trusted_user_roles(session, primary_authorizer_id)
        matching_primary = primary_roles.intersection(PRIMARY_AUTHORIZER_ROLES)
        if not matching_primary:
            raise ReconciliationAuthorizationError(
                f"Primary authorizer '{primary_authorizer_id}' lacks required role CLINICIAN_REVIEWER. "
                f"Assigned roles: {sorted(primary_roles)}"
            )
        resolved_primary_role = sorted(matching_primary)[0]

        supervisor_roles = self.get_trusted_user_roles(session, supervisor_authorizer_id)
        matching_supervisor = supervisor_roles.intersection(SUPERVISOR_AUTHORIZER_ROLES)
        if not matching_supervisor:
            raise ReconciliationAuthorizationError(
                f"Supervisor authorizer '{supervisor_authorizer_id}' lacks required supervisor role "
                f"(CLINICAL_SUPERVISOR or MEDICAL_DIRECTOR). Assigned roles: {sorted(supervisor_roles)}"
            )
        resolved_supervisor_role = sorted(matching_supervisor)[0]

        return resolved_primary_role, resolved_supervisor_role

    def create_reconciliation_token(
        self,
        session: Session,
        *,
        document_id: uuid.UUID | str,
        prior_export_id: uuid.UUID | str,
        primary_authorizer_id: uuid.UUID | str,
        supervisor_authorizer_id: uuid.UUID | str,
        decision: ReconciliationDecision | str,
        justification: str,
        evidence_type: str,
        evidence_reference: str,
        verification_level: str = "CLINICAL_CHIEF_REVIEW",
        correlation_id: Optional[str] = None,
    ) -> EHRTransmissionReconciliation:
        """Create a single-use Four-Eyes reconciliation authorization token."""
        doc_uuid = uuid.UUID(str(document_id)) if isinstance(document_id, str) else document_id
        prior_uuid = uuid.UUID(str(prior_export_id)) if isinstance(prior_export_id, str) else prior_export_id
        primary_uuid = uuid.UUID(str(primary_authorizer_id)) if isinstance(primary_authorizer_id, str) else primary_authorizer_id
        supervisor_uuid = uuid.UUID(str(supervisor_authorizer_id)) if isinstance(supervisor_authorizer_id, str) else supervisor_authorizer_id

        # 1. Enforce trusted roles and distinct actors
        primary_role, supervisor_role = self.validate_authorizer_roles(session, primary_uuid, supervisor_uuid)

        # 2. Validate prior attempt exists and is in ambiguous or historical indeterminate state
        prior_export = session.query(FHIRExport).filter(FHIRExport.id == prior_uuid).first()
        if not prior_export:
            raise ReconciliationNotFoundError(f"Prior export attempt '{prior_uuid}' not found.")

        if prior_export.transmission_state not in (
            EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED,
            EHRTransmissionState.HISTORICAL_INDETERMINATE,
            EHRTransmissionState.CONFIRMED_FAILED,
        ):
            raise ReconciliationValidationError(
                f"Prior export attempt '{prior_uuid}' is in state '{prior_export.transmission_state}'. "
                "Reconciliation is only permitted for UNKNOWN_RECONCILIATION_REQUIRED, HISTORICAL_INDETERMINATE, or CONFIRMED_FAILED."
            )

        # 3. Validate bundle and document linkage
        bundle = session.query(FHIRBundle).filter(FHIRBundle.id == prior_export.bundle_id).first()
        if not bundle or bundle.document_id != doc_uuid:
            raise ReconciliationValidationError(
                f"Prior export bundle '{prior_export.bundle_id}' does not match document '{doc_uuid}'."
            )

        # 4. Validate decision
        decision_val = decision.value if isinstance(decision, ReconciliationDecision) else str(decision)
        valid_decisions = {d.value for d in ReconciliationDecision}
        if decision_val not in valid_decisions:
            raise ReconciliationValidationError(
                f"Invalid reconciliation decision '{decision_val}'. Must be one of {sorted(valid_decisions)}."
            )

        # 5. Validate substantive justification
        clean_justification = (justification or "").strip()
        if len(clean_justification) < 15:
            raise ReconciliationValidationError(
                "Clinical justification must contain at least 15 characters of substantive technical/clinical explanation."
            )

        # 6. Validate evidence fields
        if not evidence_type or not evidence_type.strip():
            raise ReconciliationValidationError("evidence_type is required.")
        if not evidence_reference or not evidence_reference.strip():
            raise ReconciliationValidationError("evidence_reference is required.")

        destination_identity = prior_export.destination_identity or "ehr://sandbox"
        dest_idem_key = prior_export.destination_idempotency_key or prior_export.idempotency_key

        now = datetime.datetime.now(datetime.timezone.utc)
        recon_record = EHRTransmissionReconciliation(
            id=uuid.uuid4(),
            document_id=doc_uuid,
            bundle_id=bundle.id,
            prior_export_id=prior_export.id,
            destination_identity=destination_identity,
            bundle_sha256=bundle.bundle_sha256,
            destination_idempotency_key=dest_idem_key,
            decision=ReconciliationDecision(decision_val),
            justification=clean_justification,
            evidence_type=evidence_type.strip(),
            evidence_reference=evidence_reference.strip(),
            verification_level=verification_level.strip(),
            primary_authorizer_id=primary_uuid,
            primary_authorizer_role=primary_role,
            supervisor_authorizer_id=supervisor_uuid,
            supervisor_authorizer_role=supervisor_role,
            authorized_at=now,
        )
        session.add(recon_record)
        session.flush()

        # Audit Event
        self.audit_service.record_event(
            session=session,
            actor_id=str(supervisor_uuid),
            event_type="EHR_TRANSMISSION_RECONCILED",
            entity_type="DOCUMENT",
            entity_id=str(doc_uuid),
            action="RECONCILE_TRANSMISSION_AUTHORIZATION",
            metadata_json={
                "reconciliation_id": str(recon_record.id),
                "prior_export_id": str(prior_export.id),
                "decision": decision_val,
                "primary_authorizer_id": str(primary_uuid),
                "supervisor_authorizer_id": str(supervisor_uuid),
                "evidence_type": evidence_type.strip(),
                "evidence_reference": evidence_reference.strip(),
            },
            correlation_id=correlation_id,
        )
        session.flush()

        return recon_record

    def revoke_reconciliation_token(
        self,
        session: Session,
        *,
        token_id: uuid.UUID | str,
        revoked_by_id: uuid.UUID | str,
        reason: str,
        correlation_id: Optional[str] = None,
    ) -> EHRTransmissionReconciliation:
        """Revoke an active reconciliation token before it is consumed."""
        token_uuid = uuid.UUID(str(token_id)) if isinstance(token_id, str) else token_id
        revoker_uuid = uuid.UUID(str(revoked_by_id)) if isinstance(revoked_by_id, str) else revoked_by_id

        record = session.query(EHRTransmissionReconciliation).filter(EHRTransmissionReconciliation.id == token_uuid).first()
        if not record:
            raise ReconciliationNotFoundError(f"Reconciliation token '{token_uuid}' not found.")

        if record.consumed_at is not None:
            raise ReconciliationValidationError(
                f"Reconciliation token '{token_uuid}' has already been consumed by export '{record.consumed_by_export_id}' and cannot be revoked."
            )

        if record.revoked_at is not None:
            return record  # Idempotent revocation

        now = datetime.datetime.now(datetime.timezone.utc)
        record.revoked_at = now
        record.revoked_by_id = revoker_uuid
        session.flush()

        self.audit_service.record_event(
            session=session,
            actor_id=str(revoker_uuid),
            event_type="EHR_RECONCILIATION_REVOKED",
            entity_type="DOCUMENT",
            entity_id=str(record.document_id),
            action="REVOKE_RECONCILIATION_TOKEN",
            metadata_json={
                "reconciliation_id": str(record.id),
                "revocation_reason": reason,
                "revoked_by_id": str(revoker_uuid),
            },
            correlation_id=correlation_id,
        )
        session.flush()
        return record

    def validate_and_consume_token(
        self,
        session: Session,
        *,
        token_id: uuid.UUID | str,
        new_attempt_id: uuid.UUID,
        expected_bundle_id: uuid.UUID,
        expected_destination_identity: str,
    ) -> EHRTransmissionReconciliation:
        """Atomically validate and consume a reconciliation token during retry attempt creation.
        
        Must be executed in the SAME transaction (Boundary A) as the new attempt creation.
        """
        token_uuid = uuid.UUID(str(token_id)) if isinstance(token_id, str) else token_id
        record = (
            session.query(EHRTransmissionReconciliation)
            .filter(EHRTransmissionReconciliation.id == token_uuid)
            .with_for_update()
            .first()
        )
        if not record:
            raise ReconciliationNotFoundError(f"Reconciliation token '{token_uuid}' not found.")

        if record.revoked_at is not None:
            raise ReconciliationTokenRevokedError(
                f"Reconciliation token '{token_uuid}' was revoked on {record.revoked_at.isoformat()}."
            )

        if record.consumed_at is not None:
            raise ReconciliationTokenAlreadyConsumedError(
                f"Reconciliation token '{token_uuid}' was already consumed by export '{record.consumed_by_export_id}' on {record.consumed_at.isoformat()}."
            )

        if record.bundle_id != expected_bundle_id:
            raise ReconciliationValidationError(
                f"Reconciliation token bundle '{record.bundle_id}' does not match export bundle '{expected_bundle_id}'."
            )

        if record.destination_identity != expected_destination_identity:
            raise ReconciliationValidationError(
                f"Reconciliation token destination '{record.destination_identity}' does not match export destination '{expected_destination_identity}'."
            )

        now = datetime.datetime.now(datetime.timezone.utc)
        record.consumed_at = now
        record.consumed_by_export_id = new_attempt_id
        session.flush()

        return record

