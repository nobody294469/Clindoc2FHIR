"""Transactional EHR Outbox Dispatch Service (Phase B1).

Authority:
  - Phase B1 Frozen Architectural Specification
  - Dedicated ehr_transmission_outbox staging and lease-generation fencing
  - Decoupled two-transaction initial dispatch and Four-Eyes retry workflows
  - Integration with existing worker daemon (clindoc.jobs.worker)
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import logging
from typing import Any, Optional, Tuple
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from clindoc.config import acquire_document_xact_lock
from clindoc.db.enums import (
    EHRResolutionStatus,
    EHRTransmissionState,
    FHIRExportStatus,
    OutboxStatus,
)
from clindoc.db.models import (
    Document as DBDocument,
    DocumentApproval,
    EHRTransmissionOutbox,
    FHIRBundle,
    FHIRExport,
)
from clindoc.services.ehr_integration import (
    EHRIntegrationService,
    EHROutcomePersistenceResult,
    EHRTransmissionResult,
    OutboxAttemptCreationResult,
    canonicalize_destination_identity,
    derive_destination_idempotency_key,
)
from clindoc.services.ehr_transport import (
    EHRTransportError,
    FHIRTransport,
    MockFHIRTransport,
)

logger = logging.getLogger(__name__)


class OutboxError(Exception):
    """Base exception for outbox service operations."""
    pass


class OutboxIntentNotFoundError(OutboxError):
    """Raised when an outbox intent is not found."""
    pass


class OutboxConflictError(OutboxError):
    """Raised when an outbox staging request conflicts with existing state."""
    pass


class StaleWorkerLeaseError(OutboxError):
    """Raised when a worker attempts an outbox update with an expired or stale lease."""
    pass


def _format_uuid_for_raw_sql(val: Any, dialect_name: str) -> Optional[str]:
    if val is None:
        return None
    u = uuid.UUID(str(val)) if not isinstance(val, uuid.UUID) else val
    return u.hex if dialect_name == "sqlite" else str(u)


@dataclass(frozen=True)
class OutboxStagingResult:
    """Outcome of an outbox staging request."""

    outbox_id: uuid.UUID
    document_id: uuid.UUID
    bundle_id: uuid.UUID
    bundle_sha256: str
    logical_export_id: uuid.UUID
    destination_identity: str
    destination_idempotency_key: str
    status: OutboxStatus
    is_duplicate: bool = False
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        st_val = self.status.value if hasattr(self.status, "value") else str(self.status)
        return {
            "outbox_id": str(self.outbox_id),
            "document_id": str(self.document_id),
            "bundle_id": str(self.bundle_id),
            "bundle_sha256": self.bundle_sha256,
            "logical_export_id": str(self.logical_export_id),
            "destination_identity": self.destination_identity,
            "destination_idempotency_key": self.destination_idempotency_key,
            "status": st_val,
            "is_duplicate": self.is_duplicate,
            "message": self.message,
        }


class EHROutboxService:
    """Service for staging, claiming, and dispatching EHR outbox intents (Phase B1)."""

    def __init__(
        self,
        session_factory: sessionmaker,
        ehr_integration_service: EHRIntegrationService,
        transport: Optional[FHIRTransport] = None,
        default_lease_seconds: int = 60,
    ) -> None:
        self.session_factory = session_factory
        self.ehr_service = ehr_integration_service
        self.transport = transport or getattr(ehr_integration_service, "transport", MockFHIRTransport())
        self.default_lease_seconds = default_lease_seconds

    def stage_transmission_intent(
        self,
        document_id: uuid.UUID | str,
        destination_identity: str,
        actor_id: uuid.UUID | str,
        correlation_id: Optional[str] = None,
        reconciliation_token: Optional[str] = None,
    ) -> OutboxStagingResult:
        """Stage or retry an EHR transmission intent in ehr_transmission_outbox."""
        doc_uuid = uuid.UUID(str(document_id)) if not isinstance(document_id, uuid.UUID) else document_id
        actor_uuid = uuid.UUID(str(actor_id)) if not isinstance(actor_id, uuid.UUID) else actor_id
        corr_id = correlation_id or str(uuid.uuid4())
        dest_ident = canonicalize_destination_identity(destination_identity)

        with self.session_factory() as session:
            acquire_document_xact_lock(session, doc_uuid)

            # Resolve sealed bundle
            bundle = (
                session.query(FHIRBundle)
                .filter(FHIRBundle.document_id == doc_uuid)
                .order_by(FHIRBundle.created_at.desc(), FHIRBundle.id.desc())
                .first()
            )
            if not bundle:
                raise OutboxError(f"No FHIRBundle found for document '{doc_uuid}'.")

            bundle_sha256 = str(bundle.bundle_sha256).strip().lower()

            # Inspect existing outbox intent
            existing_outbox = (
                session.query(EHRTransmissionOutbox)
                .filter(
                    EHRTransmissionOutbox.document_id == doc_uuid,
                    EHRTransmissionOutbox.destination_identity == dest_ident,
                    EHRTransmissionOutbox.bundle_sha256 == bundle_sha256,
                )
                .with_for_update()
                .first()
            )

            if existing_outbox:
                if existing_outbox.status in (OutboxStatus.PENDING, OutboxStatus.DISPATCHING):
                    st_enum = OutboxStatus(existing_outbox.status) if isinstance(existing_outbox.status, str) else existing_outbox.status
                    st_val = st_enum.value
                    return OutboxStagingResult(
                        outbox_id=existing_outbox.id,
                        document_id=doc_uuid,
                        bundle_id=existing_outbox.bundle_id,
                        bundle_sha256=existing_outbox.bundle_sha256,
                        logical_export_id=existing_outbox.logical_export_id,
                        destination_identity=existing_outbox.destination_identity,
                        destination_idempotency_key=existing_outbox.destination_idempotency_key,
                        status=st_enum,
                        is_duplicate=True,
                        message=f"Transmission intent is already actively {st_val}.",
                    )
                elif existing_outbox.status == OutboxStatus.DELIVERED:
                    raise OutboxConflictError(
                        f"FHIR bundle for document '{doc_uuid}' has already been delivered to '{dest_ident}'."
                    )
                elif existing_outbox.status == OutboxStatus.FAILED_TERMINAL:
                    raise OutboxConflictError(
                        f"Transmission intent for document '{doc_uuid}' previously failed terminally."
                    )
                elif existing_outbox.status == OutboxStatus.BLOCKED_RECONCILIATION:
                    if not reconciliation_token:
                        raise OutboxConflictError(
                            f"Previous transmission attempt failed ambiguously. Four-Eyes clinical reconciliation token required to retry."
                        )
                    # Authorized Retry Workflow: Transaction 1 (Phase A Boundary A) followed by Transaction 2 (Outbox activation)
                    # Transaction 1: Invokes Phase A create_outbox_transmission_attempt (uses dedicated session)
                    pass

            if not existing_outbox:
                # Initial staging: Generate canonical UUIDv4 logical_export_id once
                logical_export_id = uuid.uuid4()
                dest_idem_key = derive_destination_idempotency_key(dest_ident, logical_export_id, bundle_sha256)

                new_outbox = EHRTransmissionOutbox(
                    id=uuid.uuid4(),
                    document_id=doc_uuid,
                    bundle_id=bundle.id,
                    bundle_sha256=bundle_sha256,
                    destination_identity=dest_ident,
                    logical_export_id=logical_export_id,
                    destination_idempotency_key=dest_idem_key,
                    status=OutboxStatus.PENDING,
                    worker_id=None,
                    lease_expires_at=None,
                    lease_generation=0,
                    worker_recovery_count=0,
                    current_attempt_id=None,
                    staged_by=actor_uuid,
                    correlation_id=corr_id,
                )
                session.add(new_outbox)
                session.commit()

                return OutboxStagingResult(
                    outbox_id=new_outbox.id,
                    document_id=doc_uuid,
                    bundle_id=bundle.id,
                    bundle_sha256=bundle_sha256,
                    logical_export_id=logical_export_id,
                    destination_identity=dest_ident,
                    destination_idempotency_key=dest_idem_key,
                    status=OutboxStatus.PENDING,
                    is_duplicate=False,
                    message="Transmission intent staged for asynchronous background dispatch.",
                )

        # Retry Path: Transaction 1 (Phase A Boundary A) outside Session 1
        observed_generation = existing_outbox.lease_generation
        attempt_res = self.ehr_service.create_outbox_transmission_attempt(
            outbox_id=existing_outbox.id,
            document_id=doc_uuid,
            logical_export_id=existing_outbox.logical_export_id,
            destination_identity=dest_ident,
            destination_idempotency_key=existing_outbox.destination_idempotency_key,
            actor_id=actor_uuid,
            correlation_id=corr_id,
            reconciliation_token=reconciliation_token,
        )

        # Transaction 2: Outbox activation with CAS on generation and BLOCKED_RECONCILIATION status
        with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = bind.dialect.name if bind is not None else "sqlite"
            rows_updated = session.execute(
                text(
                    """
                    UPDATE ehr_transmission_outbox
                    SET status = 'PENDING',
                        current_attempt_id = :attempt_id,
                        lease_generation = lease_generation + 1,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        error_message = NULL
                    WHERE id = :outbox_id
                      AND status = 'BLOCKED_RECONCILIATION'
                      AND lease_generation = :expected_gen;
                    """
                ),
                {
                    "attempt_id": _format_uuid_for_raw_sql(attempt_res.attempt_id, dialect_name),
                    "outbox_id": _format_uuid_for_raw_sql(existing_outbox.id, dialect_name),
                    "expected_gen": observed_generation,
                },
            ).rowcount
            if rows_updated != 1:
                session.rollback()
                raise StaleWorkerLeaseError("Failed to activate outbox intent for retry: state changed concurrently.")
            session.commit()

        return OutboxStagingResult(
            outbox_id=existing_outbox.id,
            document_id=doc_uuid,
            bundle_id=existing_outbox.bundle_id,
            bundle_sha256=existing_outbox.bundle_sha256,
            logical_export_id=existing_outbox.logical_export_id,
            destination_identity=dest_ident,
            destination_idempotency_key=existing_outbox.destination_idempotency_key,
            status=OutboxStatus.PENDING,
            is_duplicate=False,
            message="Reconciliation token consumed. Retry attempt staged for dispatch.",
        )

    def claim_next_outbox_intent(self, worker_id: str) -> Optional[EHRTransmissionOutbox]:
        """Claim the next PENDING outbox intent with monotonic lease-generation fencing."""
        now = datetime.datetime.now(datetime.timezone.utc)
        expires_at = now + datetime.timedelta(seconds=self.default_lease_seconds)

        with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = bind.dialect.name if bind is not None else "sqlite"

            if dialect_name == "postgresql":
                row = session.execute(
                    text(
                        """
                        UPDATE ehr_transmission_outbox
                        SET status = 'DISPATCHING',
                            worker_id = :worker_id,
                            lease_expires_at = :expires_at,
                            lease_generation = lease_generation + 1
                        WHERE id = (
                            SELECT id FROM ehr_transmission_outbox
                            WHERE status = 'PENDING'
                            ORDER BY created_at ASC
                            FOR UPDATE SKIP LOCKED LIMIT 1
                        )
                        RETURNING id;
                        """
                    ),
                    {"worker_id": worker_id, "expires_at": expires_at},
                ).fetchone()
                if not row:
                    session.rollback()
                    return None
                claimed_id = row[0]
                session.commit()
            else:
                # SQLite sequential leasing
                row = session.execute(
                    text(
                        """
                        SELECT id, lease_generation FROM ehr_transmission_outbox
                        WHERE status = 'PENDING'
                        ORDER BY created_at ASC
                        LIMIT 1;
                        """
                    )
                ).fetchone()
                if not row:
                    return None
                claimed_id, claimed_gen = row[0], row[1]
                rows_updated = session.execute(
                    text(
                        """
                        UPDATE ehr_transmission_outbox
                        SET status = 'DISPATCHING',
                            worker_id = :worker_id,
                            lease_expires_at = :expires_at,
                            lease_generation = lease_generation + 1
                        WHERE id = :id 
                          AND status = 'PENDING'
                          AND lease_generation = :gen;
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "expires_at": expires_at,
                        "id": _format_uuid_for_raw_sql(claimed_id, dialect_name),
                        "gen": claimed_gen,
                    },
                ).rowcount
                if rows_updated != 1:
                    session.rollback()
                    return None
                session.commit()

        # Load detached claimed entity
        with self.session_factory() as session:
            claimed_uuid = uuid.UUID(str(claimed_id))
            return session.query(EHRTransmissionOutbox).filter(EHRTransmissionOutbox.id == claimed_uuid).first()

    def process_outbox_intent(self, outbox_id: uuid.UUID | str, claimed_worker_id: str, claimed_generation: int) -> bool:
        """Execute Stage 3A, Stage 3B, Stage 4 (Boundary B), Stage 5 (Boundary C), and Stage 6."""
        outbox_uuid = uuid.UUID(str(outbox_id)) if not isinstance(outbox_id, uuid.UUID) else outbox_id

        # Reload outbox record
        with self.session_factory() as session:
            outbox = session.query(EHRTransmissionOutbox).filter(EHRTransmissionOutbox.id == outbox_uuid).first()
            if not outbox:
                return False
            doc_id = outbox.document_id
            logical_id = outbox.logical_export_id
            dest_ident = outbox.destination_identity
            dest_idem_key = outbox.destination_idempotency_key
            bundle_id = outbox.bundle_id
            current_attempt_id = outbox.current_attempt_id

        # Stage 3A: Create attempt if current_attempt_id is NULL (initial dispatch)
        if current_attempt_id is None:
            attempt_res = self.ehr_service.create_outbox_transmission_attempt(
                outbox_id=outbox_uuid,
                document_id=doc_id,
                logical_export_id=logical_id,
                destination_identity=dest_ident,
                destination_idempotency_key=dest_idem_key,
                actor_id=outbox.staged_by,
                correlation_id=outbox.correlation_id,
                reconciliation_token=None,
            )
            attempt_id = attempt_res.attempt_id

            # Stage 3B: Associate attempt to outbox
            now = datetime.datetime.now(datetime.timezone.utc)
            with self.session_factory() as session:
                bind = session.get_bind()
                dialect_name = bind.dialect.name if bind is not None else "sqlite"
                rows_updated = session.execute(
                    text(
                        """
                        UPDATE ehr_transmission_outbox
                        SET current_attempt_id = :attempt_id
                        WHERE id = :outbox_id
                          AND status = 'DISPATCHING'
                          AND lease_generation = :gen
                          AND worker_id = :worker_id
                          AND lease_expires_at >= :now;
                        """
                    ),
                    {
                        "attempt_id": _format_uuid_for_raw_sql(attempt_id, dialect_name),
                        "outbox_id": _format_uuid_for_raw_sql(outbox_uuid, dialect_name),
                        "gen": claimed_generation,
                        "worker_id": claimed_worker_id,
                        "now": now,
                    },
                ).rowcount
                if rows_updated != 1:
                    session.rollback()
                    logger.warning("Stage 3B association failed: lease expired or worker superseded.")
                    return False
                session.commit()
        else:
            attempt_id = current_attempt_id

        # Stage 4: Boundary B (Network I/O - ZERO open DB sessions or connections)
        # Fetch bundle payload bytes before Boundary B
        with self.session_factory() as session:
            bundle_row = session.query(FHIRBundle).filter(FHIRBundle.id == bundle_id).first()
            bundle_payload = bundle_row.bundle_payload if bundle_row else {}
            bundle_sha256 = bundle_row.bundle_sha256 if bundle_row else outbox.bundle_sha256

        from clindoc.fhir.signing import serialize_transport_payload
        if isinstance(bundle_payload, dict):
            raw_bytes, _ = serialize_transport_payload(bundle_payload)
            bundle_json = raw_bytes.decode("utf-8")
        else:
            bundle_json = str(bundle_payload)

        transport_result: Optional[EHRTransmissionResult] = None
        transport_exc: Optional[Exception] = None

        try:
            if hasattr(self.transport, "transmit"):
                raw_response = self.transport.transmit(
                    bundle_json=bundle_json,
                    idempotency_key=dest_idem_key,
                    correlation_id=outbox.correlation_id or str(uuid.uuid4()),
                    document_id=str(doc_id),
                    expected_transport_sha256=bundle_sha256,
                )
            elif hasattr(self.transport, "send_bundle"):
                raw_response = self.transport.send_bundle(
                    bundle_payload=bundle_payload,
                    destination_url=dest_ident,
                    idempotency_key=dest_idem_key,
                )
            else:
                raise RuntimeError(f"Unknown transport interface: {type(self.transport)}")

            transport_result = EHRTransmissionResult(
                document_id=str(doc_id),
                bundle_id=str(bundle_id),
                bundle_sha256=bundle_sha256,
                destination_type=getattr(raw_response, "destination_type", dest_ident),
                remote_message_id=getattr(raw_response, "remote_message_id", None) or getattr(raw_response, "remote_id", None),
                success=True,
                status_code=getattr(raw_response, "status_code", 200),
                idempotency_key=dest_idem_key,
                latency_seconds=getattr(raw_response, "latency_seconds", 0.05),
            )
        except Exception as exc:
            transport_exc = exc

        # Stage 5: Boundary C (Phase A Outcome Persistence using dedicated session)
        outcome_res = self.ehr_service.persist_outbox_transmission_outcome(
            attempt_id=attempt_id,
            document_id=doc_id,
            transport_result=transport_result,
            transport_exception=transport_exc,
            actor_id=outbox.staged_by,
            correlation_id=outbox.correlation_id,
        )

        # Stage 6: Outbox Outcome CAS Synchronization
        if outcome_res.transmission_state == EHRTransmissionState.TRANSMITTED:
            final_outbox_status = OutboxStatus.DELIVERED
        elif outcome_res.transmission_state == EHRTransmissionState.CONFIRMED_FAILED:
            final_outbox_status = OutboxStatus.FAILED_TERMINAL
        else:
            final_outbox_status = OutboxStatus.BLOCKED_RECONCILIATION

        now = datetime.datetime.now(datetime.timezone.utc)
        with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = bind.dialect.name if bind is not None else "sqlite"
            rows = session.execute(
                text(
                    """
                    UPDATE ehr_transmission_outbox
                    SET status = :final_status,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        error_message = :err_msg
                    WHERE id = :outbox_id
                      AND status = 'DISPATCHING'
                      AND lease_generation = :gen
                      AND worker_id = :worker_id
                      AND lease_expires_at >= :now
                      AND current_attempt_id = :attempt_id;
                    """
                ),
                {
                    "final_status": final_outbox_status.value if hasattr(final_outbox_status, "value") else str(final_outbox_status),
                    "err_msg": outcome_res.error_message,
                    "outbox_id": _format_uuid_for_raw_sql(outbox_uuid, dialect_name),
                    "gen": claimed_generation,
                    "worker_id": claimed_worker_id,
                    "now": now,
                    "attempt_id": _format_uuid_for_raw_sql(attempt_id, dialect_name),
                },
            ).rowcount
            if rows != 1:
                session.rollback()
                logger.warning("Session 4 CAS outcome synchronization rejected: worker lease expired or superseded.")
                return False
            session.commit()
            return True

    def run_reaper_cycle(self) -> int:
        """Scan expired outbox leases and conservatively reconcile with Phase A attempts."""
        now = datetime.datetime.now(datetime.timezone.utc)
        reconciled_count = 0

        with self.session_factory() as session:
            expired_records = (
                session.query(EHRTransmissionOutbox)
                .filter(
                    EHRTransmissionOutbox.status == OutboxStatus.DISPATCHING,
                    EHRTransmissionOutbox.lease_expires_at < now,
                )
                .all()
            )

        for rec in expired_records:
            # Query attempts and reconcile within one session
            with self.session_factory() as session:
                bind = session.get_bind()
                dialect_name = bind.dialect.name if bind is not None else "sqlite"
                latest_attempt = (
                    session.query(FHIRExport)
                    .filter(FHIRExport.outbox_id == rec.id)
                    .order_by(FHIRExport.attempt_number.desc())
                    .first()
                )

                if latest_attempt is None:
                    # Case A: Worker crashed before Boundary A attempt creation -> Safe lease recovery
                    rows = session.execute(
                        text(
                            """
                            UPDATE ehr_transmission_outbox
                            SET status = 'PENDING',
                                worker_id = NULL,
                                lease_expires_at = NULL,
                                worker_recovery_count = worker_recovery_count + 1,
                                lease_generation = lease_generation + 1
                            WHERE id = :id 
                              AND status = 'DISPATCHING'
                              AND lease_generation = :gen;
                            """
                        ),
                        {"id": _format_uuid_for_raw_sql(rec.id, dialect_name), "gen": rec.lease_generation},
                    ).rowcount
                    if rows == 1:
                        session.commit()
                        reconciled_count += 1
                else:
                    # Case B, C, D: Attempt exists
                    # Inspect attempt status in Phase A
                    if latest_attempt.transmission_state == EHRTransmissionState.PENDING_TRANSPORT:
                        # Conservative safety: Convert to UNKNOWN_RECONCILIATION_REQUIRED in Phase A
                        latest_attempt.export_status = FHIRExportStatus.FAILED
                        latest_attempt.transmission_state = EHRTransmissionState.UNKNOWN_RECONCILIATION_REQUIRED
                        latest_attempt.resolution_status = EHRResolutionStatus.UNRESOLVED
                        latest_attempt.error_message = "[REAPER_CRASH_RECOVERY] Worker lease expired during transport."
                        session.flush()

                        session.execute(
                            text(
                                """
                                UPDATE ehr_transmission_outbox
                                SET status = 'BLOCKED_RECONCILIATION',
                                    current_attempt_id = :attempt_id,
                                    worker_id = NULL,
                                    lease_expires_at = NULL,
                                    lease_generation = lease_generation + 1,
                                    error_message = :err_msg
                                WHERE id = :id 
                                  AND status = 'DISPATCHING'
                                  AND lease_generation = :gen;
                                """
                            ),
                            {
                                "attempt_id": _format_uuid_for_raw_sql(latest_attempt.id, dialect_name),
                                "err_msg": latest_attempt.error_message,
                                "id": _format_uuid_for_raw_sql(rec.id, dialect_name),
                                "gen": rec.lease_generation,
                            },
                        )
                        session.commit()
                        reconciled_count += 1
                    elif latest_attempt.transmission_state == EHRTransmissionState.TRANSMITTED:
                        # Boundary C committed, Session 4 missed -> Catch-up to DELIVERED
                        session.execute(
                            text(
                                """
                                UPDATE ehr_transmission_outbox
                                SET status = 'DELIVERED',
                                    current_attempt_id = :attempt_id,
                                    worker_id = NULL,
                                    lease_expires_at = NULL,
                                    lease_generation = lease_generation + 1
                                WHERE id = :id 
                                  AND status = 'DISPATCHING'
                                  AND lease_generation = :gen;
                                """
                            ),
                            {
                                "attempt_id": _format_uuid_for_raw_sql(latest_attempt.id, dialect_name),
                                "id": _format_uuid_for_raw_sql(rec.id, dialect_name),
                                "gen": rec.lease_generation,
                            },
                        )
                        session.commit()
                        reconciled_count += 1
                    else:
                        # Terminal failure or already unknown
                        final_st = (
                            OutboxStatus.FAILED_TERMINAL
                            if latest_attempt.transmission_state == EHRTransmissionState.CONFIRMED_FAILED
                            else OutboxStatus.BLOCKED_RECONCILIATION
                        )
                        session.execute(
                            text(
                                """
                                UPDATE ehr_transmission_outbox
                                SET status = :st,
                                    current_attempt_id = :attempt_id,
                                    worker_id = NULL,
                                    lease_expires_at = NULL,
                                    lease_generation = lease_generation + 1
                                WHERE id = :id 
                                  AND status = 'DISPATCHING'
                                  AND lease_generation = :gen;
                                """
                            ),
                            {
                                "st": final_st.value if hasattr(final_st, "value") else str(final_st),
                                "attempt_id": _format_uuid_for_raw_sql(latest_attempt.id, dialect_name),
                                "id": _format_uuid_for_raw_sql(rec.id, dialect_name),
                                "gen": rec.lease_generation,
                            },
                        )
                        session.commit()
                        reconciled_count += 1

        return reconciled_count
