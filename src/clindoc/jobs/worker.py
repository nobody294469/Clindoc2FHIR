"""Document processing background worker and pipeline orchestration skeleton.

Authority:
  - PRODUCTIZATION_SPEC.md §2 (W-03 to W-07), §4.1 (R-3), §4.2, §4.3, §9.2, §13 (F-17), §16
  - PRODUCTIZATION_DECISIONS.md PD-001, PD-007, §7 Item 3

Implements Phase 8.3 Task 3B Step 1:
  - Claims ProcessingJobs atomically from JobQueue.
  - Loads Document and manages state transitions (INGESTED -> PROCESSING -> PENDING_REVIEW / FAILED).
  - Provides sequential orchestration boundaries:
      1. Ingestion / OCR
      2. Extraction
      3. Normalization
      4. Verification
      5. Persistence
  - Platform-independent temporary working file management and guaranteed cleanup.
  - Top-level exception handling delegating retry and terminal-failure behavior to JobQueue.
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Optional, Sequence
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from clindoc.config import (
    ProductionProfileError,
    RuntimeProfile,
    get_active_profile,
    validate_daemon_profile,
)
from clindoc.db.enums import (
    ClaimOrigin,
    ClaimReviewState,
    ContradictionType as DBContradictionType,
    DocumentStatus,
    JobStatus,
    VerificationLabel as DBVerificationLabel,
    VerificationStage as DBVerificationStage,
)
from clindoc.db.models import (
    Claim,
    Document,
    Evidence,
    JobStageEvent,
    ModelRun,
    ProcessingJob,
    RoutingDecision,
    VerificationResult,
)
from clindoc.extract.contracts import (
    Claim as ContractClaim,
    DocumentExtractionOutput,
    Evidence as ContractEvidence,
)
from clindoc.extract.router import ExtractionRouter
from clindoc.extract.medication_association import NO_ANCHOR_SENTINEL, AssociationStatus
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.terminology.service import (
    TerminologyRetrievalService,
    get_process_terminology_service,
)
from clindoc.verify.cascade import VerificationCascade
from clindoc.jobs.queue import (
    InvalidStateTransitionError,
    JobNotFoundError,
    JobQueue,
)
from clindoc.manifest import (
    CANONICAL_CALIBRATION_TEMPERATURE,
    CANONICAL_MODERNBERT_CHECKPOINT_SHA256,
    CANONICAL_MODERNBERT_MODEL_ID,
    CANONICAL_QWEN_MAX_TOKENS,
    CANONICAL_QWEN_MODEL_ID,
    CANONICAL_QWEN_QUANTIZATION,
    CANONICAL_QWEN_REVISION_HASH,
    CANONICAL_QWEN_SEED,
    CANONICAL_QWEN_SERVING_ENGINE,
    CANONICAL_QWEN_TEMPERATURE,
    CANONICAL_STAGE_B_AUTONOMOUS_THRESHOLD,
    CANONICAL_STAGE_B_ESCALATION_THRESHOLD,
    CANONICAL_STAGE_C_PROMPT_TEXT_SHA256,
    CANONICAL_STAGE_C_PROMPT_VERSION,
)
from clindoc.ocr.document_text import DocumentText
from clindoc.ocr.ingest import (
    IngestionError,
    UnreadableDocumentError,
    ingest_document,
)
from clindoc.storage.blob import BlobNotFoundError, BlobStore
from clindoc.observability.metrics import get_metrics

logger = logging.getLogger(__name__)


def _map_verification_label(label: str) -> DBVerificationLabel:
    """Map verification label string/enum to DBVerificationLabel."""
    if label in ("DISCREPANCY", "REFUTED"):
        return DBVerificationLabel.REFUTED
    if label == "SUPPORTED":
        return DBVerificationLabel.SUPPORTED
    if label == "INSUFFICIENT_EVIDENCE":
        return DBVerificationLabel.INSUFFICIENT_EVIDENCE
    try:
        return DBVerificationLabel(label)
    except ValueError:
        return DBVerificationLabel.REFUTED


def _map_contradiction_type(c_type: Optional[str]) -> Optional[DBContradictionType]:
    """Map verification contradiction type to DBContradictionType."""
    if not c_type:
        return None
    mapping = {
        "value_mismatch": DBContradictionType.NUMERICAL_MISMATCH,
        "dose_mismatch": DBContradictionType.NUMERICAL_MISMATCH,
        "unit_mismatch": DBContradictionType.NUMERICAL_MISMATCH,
        "negation_mismatch": DBContradictionType.NEGATION,
        "wrong_entity": DBContradictionType.ENTITY_MISMATCH,
        "code_mismatch": DBContradictionType.ENTITY_MISMATCH,
        "temporal_mismatch": DBContradictionType.TEMPORAL_MISMATCH,
    }
    return mapping.get(c_type, DBContradictionType.OTHER)


def _map_verification_stage(stage_letter: str) -> DBVerificationStage:
    """Map stage letter to DBVerificationStage."""
    mapping = {
        "A": DBVerificationStage.STAGE_A,
        "B": DBVerificationStage.STAGE_B,
        "C": DBVerificationStage.STAGE_C,
    }
    return mapping.get(stage_letter, DBVerificationStage.STAGE_A)



class WorkerError(Exception):
    """Base exception for worker orchestration failures."""
    pass


class DocumentNotFoundError(WorkerError):
    """Raised when the document referenced by a job does not exist."""
    pass


class InvalidDocumentStateError(WorkerError):
    """Raised when the document is not in a valid state for processing."""
    pass


class ModernBERTUnavailableError(WorkerError):
    """Raised when ModernBERT is unavailable, unloadable, or fails checksum/integrity validation (F-05)."""
    pass


class StaleWorkerFencedError(WorkerError):
    """Raised when worker detects its lease has expired, been stolen, or reclaimed."""
    pass


class LeaseHeartbeatThread:
    """Proactive background lease heartbeat thread for running jobs.

    Regularly renews the job lease using atomic CAS predicate (id, worker_id, attempts, RUNNING, not expired).
    If renewal fails or worker is fenced out, marks is_fenced and terminates.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        queue: JobQueue,
        job_id: uuid.UUID,
        worker_id: str,
        claimed_attempts: int,
        lease_seconds: int = 60,
        heartbeat_interval: Optional[float] = None,
        on_fenced: Optional[Callable[[], None]] = None,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.job_id = job_id
        self.worker_id = worker_id
        self.claimed_attempts = claimed_attempts
        self.lease_seconds = lease_seconds
        self.heartbeat_interval = heartbeat_interval or max(1.0, lease_seconds / 3.0)
        self.on_fenced = on_fenced
        self._stop_event = threading.Event()
        self._fenced = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self.worker_id}-{str(self.job_id)[:8]}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._stop_event.wait(self.heartbeat_interval):
                break
            try:
                with self.session_factory() as sess:
                    renewed = self.queue.renew_lease(
                        session=sess,
                        job_id=self.job_id,
                        worker_id=self.worker_id,
                        claimed_attempts=self.claimed_attempts,
                        lease_seconds=self.lease_seconds,
                    )
                    if renewed:
                        sess.commit()
                    else:
                        logger.warning(
                            "Lease renewal CAS failed for job %s: worker %s (attempt %d) is fenced out.",
                            self.job_id,
                            self.worker_id,
                            self.claimed_attempts,
                        )
                        self._fenced.set()
                        if self.on_fenced:
                            try:
                                self.on_fenced()
                            except Exception as fe_exc:
                                logger.warning("Error in on_fenced callback: %s", fe_exc)
                        break
            except Exception as exc:
                logger.warning(
                    "Heartbeat encounter exception renewing lease for job %s: %s",
                    self.job_id,
                    exc,
                )

    def is_fenced(self) -> bool:
        return self._fenced.is_set()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)


class DocumentProcessingWorker:
    """Background worker executing the clinical document verification cascade.

    Orchestrates ingestion, extraction, normalization, verification, and persistence
    for claimed jobs.
    """

    def __init__(
        self,
        queue: Optional[JobQueue] = None,
        blob_store: Optional[BlobStore] = None,
        worker_id: Optional[str] = None,
        lease_seconds: Optional[int] = None,
        ingest_fn: Optional[Callable[..., DocumentText]] = None,
        router: Optional[ExtractionRouter] = None,
        normalizer: Optional[TerminologyNormalizer] = None,
        cascade: Optional[VerificationCascade] = None,
        stop_after_stage: Optional[str] = None,
        session_factory: Optional[Callable[[], Session]] = None,
        terminology_service: Optional[TerminologyRetrievalService] = None,
        terminology_dir: Optional[Union[Path, str]] = None,
        use_scaled_terminology: bool = True,
        owns_terminology_service: bool = False,
    ) -> None:
        self.queue = queue or JobQueue()
        self.blob_store = blob_store
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = lease_seconds
        self.session_factory = session_factory
        self._active_heartbeat: Optional[LeaseHeartbeatThread] = None
        self.ingest_fn = ingest_fn or ingest_document
        self.router = router or ExtractionRouter()
        self.cascade = cascade or VerificationCascade()
        self.stop_after_stage = stop_after_stage
        self.terminology_dir = terminology_dir
        self._owns_terminology_service = owns_terminology_service

        if normalizer is not None:
            self.normalizer = normalizer
            existing_service = getattr(normalizer, "retrieval_service", None)

            if existing_service is not None and terminology_service is not None:
                # Case D: Both supplied; raise error if conflicting
                if existing_service is not terminology_service:
                    raise ValueError(
                        "Conflicting terminology services provided: supplied normalizer already contains "
                        "a retrieval service that differs from the explicit terminology_service argument."
                    )
                self.terminology_service = terminology_service
            elif existing_service is not None and terminology_service is None:
                # Case E: Explicit normalizer already containing service; no explicit service
                self.terminology_service = existing_service
            elif existing_service is None and terminology_service is not None:
                # Case C: Explicit normalizer without service; inject supplied service into normalizer
                normalizer.retrieval_service = terminology_service
                self.terminology_service = terminology_service
            else:
                # Case B: Explicit normalizer with no service anywhere (legacy fallback)
                self.terminology_service = None
        else:
            # Case A: No explicit normalizer
            if terminology_service is not None:
                self.terminology_service = terminology_service
            elif use_scaled_terminology:
                self.terminology_service = get_process_terminology_service(data_dir=terminology_dir)
            else:
                self.terminology_service = None

            # Construct worker-local normalizer instance to ensure mutable audit state is not shared across jobs
            self.normalizer = TerminologyNormalizer(retrieval_service=self.terminology_service)

        self.last_ocr_output: Optional[DocumentText] = None
        self.last_extraction_output: Optional[DocumentExtractionOutput] = None
        self.last_normalized_claims: Optional[list[Any]] = None
        self.last_verification_output: Optional[list[Any]] = None

    def close(self) -> None:
        """Release worker resources, active heartbeat threads, and owned services."""
        if self._active_heartbeat:
            self._active_heartbeat.stop()
            self._active_heartbeat = None
        if self._owns_terminology_service and self.terminology_service:
            self.terminology_service.close()
            self.terminology_service = None

    def _resolve_session_factory(self, session: Optional[Session] = None) -> Optional[Callable[[], Session]]:
        """Resolve a sessionmaker callable from instance, session, or global state."""
        if self.session_factory:
            return self.session_factory
        if session and hasattr(session, "session_factory") and session.session_factory:
            return session.session_factory
        if session and session.get_bind() is not None:
            from sqlalchemy.orm import sessionmaker
            return sessionmaker(bind=session.get_bind())
        return None

    def _check_fenced(self, session: Session, job: ProcessingJob, claimed_attempts: int) -> None:
        """Enforce fencing check. Raises StaleWorkerFencedError if worker no longer holds an active, valid lease."""
        if self._active_heartbeat and self._active_heartbeat.is_fenced():
            raise StaleWorkerFencedError(
                f"Worker {self.worker_id} was fenced out by heartbeat monitor for job {job.id}."
            )

        now = datetime.now(timezone.utc)
        stmt = (
            select(ProcessingJob.id)
            .where(
                ProcessingJob.id == job.id,
                ProcessingJob.worker_id == self.worker_id,
                ProcessingJob.attempts == claimed_attempts,
                ProcessingJob.status == JobStatus.RUNNING,
                ProcessingJob.lease_expires_at > now,
            )
        )
        row = session.scalars(stmt).first()
        if row is None:
            raise StaleWorkerFencedError(
                f"Worker {self.worker_id} (attempt {claimed_attempts}) is fenced out for job {job.id}: lease expired or reclaimed."
            )

    @staticmethod
    def _callable_supports_cancellation(fn: Any) -> bool:
        """Determine whether an ingestion callable supports the `cancellation_event` keyword argument.

        Must return True only when:
        - `cancellation_event` is declared as POSITIONAL_OR_KEYWORD or KEYWORD_ONLY, or
        - the callable accepts `**kwargs` (VAR_KEYWORD).

        A positional-only `cancellation_event` is NOT keyword-compatible and returns False.
        """
        if not callable(fn):
            return False
        try:
            sig = inspect.signature(fn)
            has_var_keyword = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if has_var_keyword:
                return True

            for p in sig.parameters.values():
                if p.name == "cancellation_event":
                    if p.kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY,
                    ):
                        return True
                    # Positional-only parameter named 'cancellation_event'
                    return False
            return False
        except (ValueError, TypeError):
            return False

    def _verify_modernbert_availability(self) -> None:
        """Enforce F-05: ModernBERT is mandatory. If unavailable/unloadable, refuse to process."""
        stage_b = getattr(self.cascade, "stage_b", None)
        if stage_b is not None:
            profile = get_active_profile()
            if profile == RuntimeProfile.PRODUCTION:
                if hasattr(stage_b, "_predictor") and stage_b._predictor.__class__.__name__ == "MockModelPredictor":
                    raise ModernBERTUnavailableError("MockModelPredictor is forbidden in PRODUCTION profile.")
                if getattr(stage_b, "allow_heuristic_fallback", False):
                    raise ModernBERTUnavailableError("Heuristic fallback is forbidden in PRODUCTION profile.")
                if hasattr(stage_b, "is_model_loaded") and not stage_b.is_model_loaded:
                    try:
                        loaded = stage_b.load_model()
                        if not loaded:
                            raise ModernBERTUnavailableError("ModernBERT checkpoint failed to load.")
                    except Exception as e:
                        raise ModernBERTUnavailableError(f"ModernBERT checkpoint failed to load: {e}") from e
            elif hasattr(stage_b, "verify_readiness"):
                stage_b.verify_readiness()

    def _get_or_create_model_run(self, session: Session, job: ProcessingJob) -> ModelRun:
        """Resolve existing or create canonical ModelRun record for provenance (§3.7)."""
        if job.model_run_id:
            existing = session.get(ModelRun, job.model_run_id)
            if existing:
                return existing

        profile = get_active_profile()
        is_prod = (profile == RuntimeProfile.PRODUCTION)
        model_run = ModelRun(
            is_qualified=is_prod,
            runtime_profile=profile.value if hasattr(profile, "value") else str(profile),
            component="pipeline_cascade",
            provider_kind="vllm" if is_prod else "mock",
            stage_b_model_id=CANONICAL_MODERNBERT_MODEL_ID,
            stage_b_checkpoint_hash=CANONICAL_MODERNBERT_CHECKPOINT_SHA256,
            stage_b_temperature=CANONICAL_CALIBRATION_TEMPERATURE,
            stage_b_escalation_threshold=CANONICAL_STAGE_B_ESCALATION_THRESHOLD,
            stage_b_autonomous_threshold=CANONICAL_STAGE_B_AUTONOMOUS_THRESHOLD,
            stage_c_model_id=CANONICAL_QWEN_MODEL_ID,
            model_revision_hash=CANONICAL_QWEN_REVISION_HASH,
            quantization=CANONICAL_QWEN_QUANTIZATION,
            serving_engine_version="0.6.3",
            prompt_id=CANONICAL_STAGE_C_PROMPT_VERSION,
            stage_c_prompt_version=CANONICAL_STAGE_C_PROMPT_VERSION,
            stage_c_prompt_hash=CANONICAL_STAGE_C_PROMPT_TEXT_SHA256,
            stage_c_serving_engine=CANONICAL_QWEN_SERVING_ENGINE,
            stage_c_parameters={
                "temperature": CANONICAL_QWEN_TEMPERATURE,
                "seed": CANONICAL_QWEN_SEED,
                "max_tokens": CANONICAL_QWEN_MAX_TOKENS,
            },
            calibrator_version="v3_fold0_seed42",
            schema_version="1.0.0",
            extraction_model_id=CANONICAL_QWEN_MODEL_ID,
            extraction_prompt_version="V1_NARRATIVE_EXTRACTION_PROMPT",
            extraction_prompt_hash="11223344" * 8,
            extraction_serving_engine=CANONICAL_QWEN_SERVING_ENGINE,
            extraction_parameters={"temperature": 0.0, "seed": 42, "max_tokens": 512},
        )
        session.add(model_run)
        session.flush()
        job.model_run_id = model_run.id
        session.flush()
        return model_run

    @staticmethod
    def _sanitize_error(
        exc: Exception,
        sensitive_strings: Optional[Sequence[Optional[str]]] = None,
    ) -> str:
        """Sanitize error messages to prevent leaking PHI, paths, or secrets into database records."""
        msg = str(exc).strip()
        if not msg:
            return type(exc).__name__
        if sensitive_strings:
            for s in sensitive_strings:
                if s and s in msg:
                    msg = msg.replace(s, "[REDACTED]")
        if len(msg) > 500:
            msg = msg[:497] + "..."
        exc_type = type(exc).__name__
        if msg.startswith(exc_type):
            return msg
        return f"{exc_type}: {msg}"

    def claim_job(self, session: Session) -> Optional[ProcessingJob]:
        """Claim the next available pending job from the queue."""
        return self.queue.claim_next(
            session=session,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )

    def _prepare_scratch_file(self, document: Document) -> tuple[Optional[Path], bool]:
        """Prepare a platform-appropriate temporary working file from blob store or disk.

        Returns (temp_path, is_temporary).
        If is_temporary is True, caller is responsible for unlinking temp_path.
        """
        # Determine appropriate file extension
        suffix = ".pdf"
        if document.original_filename:
            p = Path(document.original_filename)
            if p.suffix:
                suffix = p.suffix.lower()
        elif document.mime_type:
            if "pdf" in document.mime_type:
                suffix = ".pdf"
            elif "png" in document.mime_type:
                suffix = ".png"
            elif "jpeg" in document.mime_type or "jpg" in document.mime_type:
                suffix = ".jpg"
            elif "tiff" in document.mime_type:
                suffix = ".tiff"

        if self.blob_store and document.storage_ref:
            # Decrypt blob to temporary file
            data = self.blob_store.get(document.storage_ref)
            with tempfile.NamedTemporaryFile(
                prefix=f"clindoc_job_{document.id.hex[:8]}_",
                suffix=suffix,
                delete=False,
            ) as tf:
                tf.write(data)
                temp_path = Path(tf.name)
            return temp_path, True
        elif document.storage_ref and document.storage_ref.startswith("file://"):
            local_path = Path(document.storage_ref.removeprefix("file://"))
            if local_path.exists():
                return local_path, False

        return None, False

    def process_job(self, session: Session, job: ProcessingJob) -> ProcessingJob:
        """Execute the sequential pipeline lifecycle for a claimed job."""
        if job not in session:
            attached_job = session.get(ProcessingJob, job.id)
            if attached_job is not None:
                job = attached_job
            else:
                job = session.merge(job)

        if job.status != JobStatus.RUNNING:
            raise InvalidStateTransitionError(
                f"Worker cannot process job {job.id} with status '{job.status}'. Expected 'RUNNING'."
            )

        job_start_time = time.perf_counter()
        get_metrics().record_job_started()

        document = session.get(Document, job.document_id)
        if document is None:
            err_msg = f"Document {job.document_id} referenced by job {job.id} does not exist."
            logger.error(err_msg)
            return self.queue.mark_failed(
                session=session,
                job_id=job.id,
                error_code="DOCUMENT_NOT_FOUND",
                error_message=err_msg,
                retryable=False,
            )

        # Validate document state
        if document.status in (DocumentStatus.QUARANTINED, DocumentStatus.FAILED_INGEST):
            err_msg = f"Cannot process document {document.id} in state '{document.status}'."
            logger.error(err_msg)
            return self.queue.mark_failed(
                session=session,
                job_id=job.id,
                error_code="INVALID_DOCUMENT_STATE",
                error_message=err_msg,
                retryable=False,
            )

        # Transition document state INGESTED -> PROCESSING
        document.status = DocumentStatus.PROCESSING
        job.current_stage = "PROCESSING"
        session.flush()

        temp_path: Optional[Path] = None
        is_temp = False
        heartbeat: Optional[LeaseHeartbeatThread] = None

        try:
            session_factory = self._resolve_session_factory(session)
            if session_factory:
                heartbeat = LeaseHeartbeatThread(
                    session_factory=session_factory,
                    queue=self.queue,
                    job_id=job.id,
                    worker_id=self.worker_id,
                    claimed_attempts=job.attempts,
                    lease_seconds=self.lease_seconds or 60,
                    on_fenced=lambda: getattr(self, "daemon", None).reap_subprocesses() if getattr(self, "daemon", None) else None,
                )
                self._active_heartbeat = heartbeat
                heartbeat.start()

            self._verify_modernbert_availability()
            temp_path, is_temp = self._prepare_scratch_file(document)

            # ─── SEQUENTIAL ORCHESTRATION SKELETON ───────────────────────────
            # Stage 1: Ingestion / OCR
            self._check_fenced(session, job, job.attempts)
            ocr_output = self._orchestrate_ocr_stage(session, job, document, temp_path)
            self.last_ocr_output = ocr_output

            # Stage 2: Extraction
            self._check_fenced(session, job, job.attempts)
            extraction_output = self._orchestrate_extraction_stage(session, job, document, ocr_output)
            self.last_extraction_output = extraction_output

            # Step 3 Stop boundary: Do NOT proceed to normalization or verification yet
            if self.stop_after_stage == "EXTRACTION":
                session.flush()
                return job

            # Stage 3: Normalization
            self._check_fenced(session, job, job.attempts)
            normalized_claims = self._orchestrate_normalization_stage(
                session=session,
                job=job,
                document=document,
                extraction_output=extraction_output,
            )
            self.last_normalized_claims = normalized_claims

            # Step 4 Stop boundary: Do NOT proceed to verification yet
            if self.stop_after_stage == "NORMALIZATION":
                session.flush()
                return job

            # Stage 4: Verification Cascade
            self._check_fenced(session, job, job.attempts)
            verification_output = self._orchestrate_verification_stage(
                session=session,
                job=job,
                document=document,
                normalized_claims=normalized_claims,
                extraction_output=extraction_output,
                ocr_output=ocr_output,
            )
            self.last_verification_output = verification_output

            if self.stop_after_stage == "VERIFICATION":
                session.flush()
                return job

            # Stage 5: Persistence
            self._check_fenced(session, job, job.attempts)
            self._orchestrate_persistence_stage(
                session, job, document, extraction_output, normalized_claims, verification_output
            )

            # On successful completion:
            self._check_fenced(session, job, job.attempts)
            document.status = DocumentStatus.PENDING_REVIEW
            job.current_stage = "COMPLETED"
            session.flush()
            job_duration = time.perf_counter() - job_start_time
            get_metrics().record_job_completed(job_duration)
            get_metrics().record_document_status("pending_review")
            return self.queue.mark_completed(session=session, job_id=job.id)

        except StaleWorkerFencedError as fe:
            logger.warning("Fenced out during processing for job %s: %s", job.id, fe)
            raise

        except ModernBERTUnavailableError as mb_exc:
            logger.error("F-05 ModernBERT unavailable/unloadable for job %s: %s", job.id, mb_exc)
            get_metrics().record_model_unavailable("modernbert", "model_not_loaded")
            # F-05: worker must refuse to process the job; job must remain QUEUED;
            # document state reverts to INGESTED; no heuristic/mock fallback;
            document.status = DocumentStatus.INGESTED
            job.status = JobStatus.PENDING
            job.worker_id = None
            job.lease_expires_at = None
            job.current_stage = "QUEUED"
            job.error_code = "MODERNBERT_UNAVAILABLE"
            job.error_message = self._sanitize_error(mb_exc)
            if job.attempts > 0:
                job.attempts -= 1  # Release lease without penalizing retry budget
            session.flush()

            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="VERIFICATION",
                status="FAILED",
                error_message="ModernBERT unavailable/unloadable; job remains QUEUED (fail-closed, no fallback permitted)",
            )
            return job
        except ProductionProfileError as ppe:
            if "modernbert" in str(ppe).lower():
                logger.error("F-05 ModernBERT production profile violation for job %s: %s", job.id, ppe)
                document.status = DocumentStatus.INGESTED
                job.status = JobStatus.PENDING
                job.worker_id = None
                job.lease_expires_at = None
                job.current_stage = "QUEUED"
                job.error_code = "MODERNBERT_UNAVAILABLE"
                job.error_message = self._sanitize_error(ppe)
                if job.attempts > 0:
                    job.attempts -= 1
                session.flush()
                self.queue.record_stage_event(
                    session=session,
                    job_id=job.id,
                    stage_name="VERIFICATION",
                    status="FAILED",
                    error_message="ModernBERT unavailable/unloadable; job remains QUEUED (fail-closed, no fallback permitted)",
                )
                return job

            logger.error("Terminal failure during job %s: %s", job.id, ppe)
            document.status = DocumentStatus.FAILED
            session.flush()
            job_duration = time.perf_counter() - job_start_time
            get_metrics().record_job_failed(job_duration)
            get_metrics().record_document_status("failed")
            sanitized_err = self._sanitize_error(
                ppe,
                sensitive_strings=[
                    str(temp_path) if temp_path else None,
                    document.original_filename,
                    document.patient_context_id,
                ],
            )
            return self.queue.mark_failed(
                session=session,
                job_id=job.id,
                error_code=type(ppe).__name__,
                error_message=sanitized_err,
                retryable=False,
            )
        except (DocumentNotFoundError, BlobNotFoundError, IngestionError) as terminal_exc:
            logger.error("Terminal failure during job %s: %s", job.id, terminal_exc)
            document.status = DocumentStatus.FAILED
            session.flush()
            job_duration = time.perf_counter() - job_start_time
            get_metrics().record_job_failed(job_duration)
            get_metrics().record_document_status("failed")
            sanitized_err = self._sanitize_error(
                terminal_exc,
                sensitive_strings=[
                    str(temp_path) if temp_path else None,
                    document.original_filename,
                    document.patient_context_id,
                ],
            )
            return self.queue.mark_failed(
                session=session,
                job_id=job.id,
                error_code=type(terminal_exc).__name__,
                error_message=sanitized_err,
                retryable=False,
            )
        except Exception as exc:
            logger.exception("Unexpected error during job %s: %s", job.id, exc)
            sanitized_err = self._sanitize_error(
                exc,
                sensitive_strings=[
                    str(temp_path) if temp_path else None,
                    document.original_filename,
                    document.patient_context_id,
                ],
            )
            # Transient / unclassified error -> delegate retry evaluation to JobQueue
            res = self.queue.mark_failed(
                session=session,
                job_id=job.id,
                error_code=type(exc).__name__,
                error_message=sanitized_err,
                retryable=True,
            )
            if res.status == JobStatus.FAILED:
                document.status = DocumentStatus.FAILED
                session.flush()
                job_duration = time.perf_counter() - job_start_time
                get_metrics().record_job_failed(job_duration)
                get_metrics().record_document_status("failed")
            return res
        finally:
            if heartbeat:
                heartbeat.stop()
                self._active_heartbeat = None
            if is_temp and temp_path and temp_path.exists():
                import gc
                gc.collect()
                for attempt in range(5):
                    try:
                        temp_path.unlink()
                        break
                    except PermissionError:
                        time.sleep(0.05)
                        gc.collect()
                    except OSError as unlink_err:
                        logger.warning("Failed to unlink scratch file %s: %s", temp_path, unlink_err)
                        break
                else:
                    if temp_path.exists():
                        try:
                            temp_path.unlink()
                        except OSError as unlink_err:
                            logger.warning("Failed to unlink scratch file %s: %s", temp_path, unlink_err)

    def run_once(self, session: Session) -> Optional[ProcessingJob]:
        """Claim and process a single job from the queue if one is available."""
        job = self.claim_job(session=session)
        if job is None:
            return None
        return self.process_job(session=session, job=job)

    # ─── STAGE BOUNDARIES (SKELETON) ─────────────────────────────────────────

    def _orchestrate_ocr_stage(
        self,
        session: Session,
        job: ProcessingJob,
        document: Document,
        file_path: Optional[Path],
    ) -> DocumentText:
        """Stage 1: Ingestion & OCR boundary (PROJECT_SPEC §7.1, PRODUCTIZATION_SPEC §2 W-03).

        Executes canonical ingest_document on the decrypted document file, records
        granular telemetry to job_stage_events (STARTED, COMPLETED, FAILED), computes
        PHI-free operational metrics, and retains DocumentText for downstream extraction.
        """
        job.current_stage = "OCR"
        session.flush()

        # Telemetry: Record stage STARTED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="OCR",
            status="STARTED",
        )

        start_time = time.monotonic()
        sensitive_elements = [
            str(file_path) if file_path else None,
            document.original_filename,
            document.patient_context_id,
        ]

        if file_path is None or not file_path.exists():
            err = UnreadableDocumentError(
                f"No readable document file available for processing document {document.id}"
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(err, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="OCR",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            raise err

        try:
            # Map source_type from document if known
            doc_source_type = "auto"
            if document.source_type in ("born_digital", "scanned_image"):
                doc_source_type = document.source_type

            # Provide tabular layout hint if recognizable from original_filename
            is_tabular_hint: Optional[bool] = None
            if document.original_filename and document.original_filename.upper().startswith("LAB_"):
                is_tabular_hint = True

            cancel_evt = self._active_heartbeat._fenced if self._active_heartbeat else None
            supports_cancellation = self._callable_supports_cancellation(self.ingest_fn)

            # Check whether cancellation event is already set before invoking ingestion callable
            if cancel_evt is not None and cancel_evt.is_set():
                raise StaleWorkerFencedError(
                    f"Worker {self.worker_id} lease lost before starting OCR stage."
                )

            # Policy: If _active_heartbeat exists, cancellation support is mandatory before invoking
            # ingestion, regardless of whether the lease has already been lost.
            cancellation_required = (
                self._active_heartbeat is not None
                or getattr(self, "require_cancellation_support", False)
            )

            if cancellation_required and not supports_cancellation:
                raise StaleWorkerFencedError(
                    f"Worker {self.worker_id} has active lease heartbeat or cancellation required, "
                    f"but ingestion callable '{getattr(self.ingest_fn, '__name__', repr(self.ingest_fn))}' "
                    f"does not support cancellation_event (cancellation support is mandatory)."
                )

            if supports_cancellation:
                doc_text = self.ingest_fn(
                    path=file_path,
                    document_id=str(document.id),
                    source_type=doc_source_type,
                    is_tabular=is_tabular_hint,
                    cancellation_event=cancel_evt,
                )
            else:
                doc_text = self.ingest_fn(
                    path=file_path,
                    document_id=str(document.id),
                    source_type=doc_source_type,
                    is_tabular=is_tabular_hint,
                )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(exc, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="OCR",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            if cancel_evt is not None and cancel_evt.is_set() and not isinstance(exc, StaleWorkerFencedError):
                raise StaleWorkerFencedError(
                    f"Worker {self.worker_id} lease lost during OCR stage: {exc}"
                ) from exc
            raise

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Operational PHI-free metrics (§11)
        confidences = [
            t.confidence
            for p in doc_text.pages
            for t in p.tokens
            if t.confidence is not None
        ]
        ocr_confidence_mean = (
            round(sum(confidences) / len(confidences), 4) if confidences else None
        )

        metrics: dict[str, Any] = {
            "page_count": doc_text.num_pages,
            "modality": doc_text.source_type,
            "total_tokens": doc_text.total_tokens,
        }
        if ocr_confidence_mean is not None:
            metrics["ocr_confidence_mean"] = ocr_confidence_mean

        # Telemetry: Record stage COMPLETED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="OCR",
            status="COMPLETED",
            duration_ms=duration_ms,
            metrics=metrics,
        )

        return doc_text

    def _orchestrate_extraction_stage(
        self,
        session: Session,
        job: ProcessingJob,
        document: Document,
        ocr_output: Any,
    ) -> DocumentExtractionOutput:
        """Stage 2: Extraction & relational persistence boundary (PROJECT_SPEC §7.2, §8, §9).

        Executes ExtractionRouter on DocumentText, records granular telemetry to
        job_stage_events (STARTED, COMPLETED, FAILED), computes PHI-free operational
        metrics, persists Evidence (reusing matching spans), RoutingDecisions, and Claims
        linked to the appropriate ModelRun, ensuring strict retry idempotency.
        """
        job.current_stage = "EXTRACTION"
        session.flush()

        # Telemetry: Record stage STARTED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="EXTRACTION",
            status="STARTED",
        )

        start_time = time.monotonic()
        sensitive_elements = [
            document.original_filename,
            document.patient_context_id,
        ]

        try:
            if ocr_output is None or not isinstance(ocr_output, DocumentText):
                raise ValueError(
                    f"Invalid or missing DocumentText for document {document.id} in extraction stage."
                )

            # 1. Execute extraction router (is_lab_default=None: use native content auto-detection)
            extraction_output = self.router.extract_document(doc=ocr_output, is_lab_default=None)

            # 2. Resolve or create ModelRun provenance record (§3.7)
            model_run = self._get_or_create_model_run(session, job)

            # 3. Persist Evidence (reusing existing matching spans per uq_evidence_document_span)
            existing_evidence = (
                session.query(Evidence)
                .filter(Evidence.document_id == document.id)
                .all()
            )
            evidence_by_span: dict[tuple[int, int, int], Evidence] = {
                (ev.page_number, ev.char_start, ev.char_end): ev
                for ev in existing_evidence
            }

            contract_to_db_evidence: dict[str, uuid.UUID] = {}

            for contract_ev in extraction_output.all_evidence:
                span_key = (contract_ev.page_number, contract_ev.char_start, contract_ev.char_end)
                if span_key in evidence_by_span:
                    db_ev = evidence_by_span[span_key]
                else:
                    db_ev = Evidence(
                        document_id=document.id,
                        page_number=contract_ev.page_number,
                        char_start=contract_ev.char_start,
                        char_end=contract_ev.char_end,
                        bounding_box=contract_ev.bbox.model_dump(),
                        ocr_confidence=contract_ev.ocr_confidence,
                        evidence_type="ocr_text",
                        evidence_text=contract_ev.text,
                        payload=contract_ev.model_dump(mode="json"),
                    )
                    session.add(db_ev)
                    session.flush()
                    evidence_by_span[span_key] = db_ev

                contract_to_db_evidence[contract_ev.evidence_id] = db_ev.id

            # 4. Persist Routing Decisions (idempotent: replace previous decisions for this document)
            session.query(RoutingDecision).filter(
                RoutingDecision.document_id == document.id
            ).delete(synchronize_session=False)

            for rd in extraction_output.all_routing_decisions:
                db_rd = RoutingDecision(
                    document_id=document.id,
                    region_id=rd.region_id,
                    route=rd.route,
                    reason=rd.reason,
                    confidence=rd.confidence,
                )
                session.add(db_rd)
            session.flush()

            # 5. Persist Claims (idempotent: replace previous pipeline claims for this document)
            session.query(Claim).filter(
                Claim.document_id == document.id,
                Claim.origin == ClaimOrigin.PIPELINE,
            ).delete(synchronize_session=False)

            for contract_claim in extraction_output.all_claims:
                try:
                    c_uuid = uuid.UUID(contract_claim.claim_id)
                except (ValueError, TypeError):
                    c_uuid = uuid.uuid4()
                    contract_claim.claim_id = str(c_uuid)

                grp_id = contract_claim.group_id or contract_claim.region_id or "default"
                is_unanchored = (
                    grp_id in (NO_ANCHOR_SENTINEL, "__unanchored_review__")
                    or getattr(contract_claim, "orphan", False)
                    or getattr(contract_claim, "unanchored", False)
                    or getattr(contract_claim, "association_status", "") == AssociationStatus.UNANCHORED_ORPHAN.value
                )
                if is_unanchored:
                    grp_id = "__unanchored_review__"
                    review_state = ClaimReviewState.ESCALATED
                else:
                    review_state = ClaimReviewState.PENDING

                claim_payload = contract_claim.model_dump(mode="json")
                if is_unanchored:
                    claim_payload["review_reason"] = "unanchored_medication_attribute"
                    claim_payload["unanchored"] = True
                    claim_payload["escalated"] = True

                db_claim = Claim(
                    claim_id=c_uuid,
                    document_id=document.id,
                    group_id=grp_id,
                    field_type=contract_claim.field_type,
                    value=contract_claim.value,
                    normalized_code=contract_claim.normalized_code,
                    code_system=contract_claim.code_system,
                    version=1,
                    origin=ClaimOrigin.PIPELINE,
                    review_state=review_state,
                    evidence_id=contract_to_db_evidence.get(contract_claim.evidence_id),
                    model_run_id=model_run.id if model_run else None,
                    payload=claim_payload,
                )
                session.add(db_claim)
            session.flush()

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(exc, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="EXTRACTION",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            raise

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Operational PHI-free telemetry metrics
        route_counts: dict[str, int] = {}
        for rd in extraction_output.all_routing_decisions:
            route_counts[rd.route] = route_counts.get(rd.route, 0) + 1

        metrics: dict[str, Any] = {
            "num_regions": len(extraction_output.regions),
            "num_claims": len(extraction_output.all_claims),
            "num_evidence_spans": len(extraction_output.all_evidence),
            "route_counts": route_counts,
        }

        # Telemetry: Record stage COMPLETED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="EXTRACTION",
            status="COMPLETED",
            duration_ms=duration_ms,
            metrics=metrics,
        )

        return extraction_output

    def _orchestrate_normalization_stage(
        self,
        session: Session,
        job: ProcessingJob,
        document: Document,
        extraction_output: Any,
    ) -> list[Any]:
        """Stage 3: Terminology normalization boundary (PROJECT_SPEC §7.5, §8.3, §14).

        Executes TerminologyNormalizer on extracted claims, records granular telemetry
        to job_stage_events (STARTED, COMPLETED, FAILED), computes PHI-free operational
        metrics, and updates existing persisted pipeline Claim records with standardized
        normalized codes, code systems, units, and audit metadata in-place.
        """
        job.current_stage = "NORMALIZATION"
        session.flush()

        # Telemetry: Record stage STARTED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="NORMALIZATION",
            status="STARTED",
        )

        start_time = time.monotonic()
        sensitive_elements = [
            document.original_filename,
            document.patient_context_id,
        ]

        # Check for terminology database hot-reload at bounded job boundary (Phase 3D.3)
        if self.terminology_service is not None:
            try:
                reloaded = self.terminology_service.reload_if_updated()
                if reloaded:
                    logger.info(
                        "TerminologyRetrievalService reloaded updated release '%s' at job boundary (job_id=%s)",
                        self.terminology_service.db_version,
                        job.id,
                    )
            except Exception as e:
                logger.error("Error during job-boundary terminology reload check: %s", e)

        try:
            # 1. Fetch existing pipeline claims for this document
            db_claims = (
                session.query(Claim)
                .filter(
                    Claim.document_id == document.id,
                    Claim.origin == ClaimOrigin.PIPELINE,
                )
                .all()
            )

            # 2. Collect contract Claim objects to normalize
            claims_to_normalize: list[Any] = []
            if extraction_output is not None and hasattr(extraction_output, "all_claims") and extraction_output.all_claims:
                claims_to_normalize = list(extraction_output.all_claims)
            elif extraction_output is not None and isinstance(extraction_output, list) and extraction_output:
                claims_to_normalize = list(extraction_output)
            else:
                valid_claim_keys = ContractClaim.model_fields.keys()
                for dc in db_claims:
                    if dc.payload:
                        try:
                            claim_data = {k: v for k, v in dc.payload.items() if k in valid_claim_keys}
                            claims_to_normalize.append(ContractClaim.model_validate(claim_data))
                        except Exception:
                            pass

            # 3. Execute terminology normalizer
            normalized_claims = self.normalizer.normalize_claims(claims_to_normalize)

            # 4. Update existing persisted Claim records in-place
            db_claims_by_id = {str(c.claim_id): c for c in db_claims}

            for idx, norm_claim in enumerate(normalized_claims):
                db_claim = db_claims_by_id.get(norm_claim.claim_id)
                if db_claim is None and idx < len(db_claims):
                    db_claim = db_claims[idx]

                if db_claim is not None:
                    db_claim.normalized_code = norm_claim.normalized_code
                    db_claim.code_system = norm_claim.code_system

                    # Enrich full JSON payload with normalization metadata
                    payload = dict(db_claim.payload or {})
                    payload.update({
                        "normalized_code": norm_claim.normalized_code,
                        "code_system": norm_claim.code_system,
                        "code_confidence": getattr(norm_claim, "code_confidence", 0.0),
                        "value_numeric": getattr(norm_claim, "value_numeric", None),
                    })

                    norm_unit = self.normalizer.get_normalized_unit(norm_claim.claim_id)
                    if norm_unit is not None:
                        payload["normalized_unit"] = norm_unit

                    candidates = self.normalizer.get_audit_candidates(norm_claim.claim_id)
                    if candidates:
                        payload["candidates"] = candidates

                    reasons = self.normalizer.get_audit_reasons(norm_claim.claim_id)
                    if reasons:
                        payload["abstention_reasons"] = reasons

                    db_claim.payload = payload

            session.flush()

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(exc, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="NORMALIZATION",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            raise
        finally:
            if hasattr(self.normalizer, "clear_audit_state"):
                self.normalizer.clear_audit_state()
            elif hasattr(self.normalizer, "clear_audit_candidates"):
                self.normalizer.clear_audit_candidates()

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Operational PHI-free telemetry metrics
        num_coded = sum(
            1
            for c in normalized_claims
            if getattr(c, "normalized_code", None) and getattr(c, "code_system", None)
        )
        num_uncoded = len(normalized_claims) - num_coded
        system_counts: dict[str, int] = {}
        for c in normalized_claims:
            cs = getattr(c, "code_system", None)
            if cs:
                s_name = str(cs)
                system_counts[s_name] = system_counts.get(s_name, 0) + 1

        metrics: dict[str, Any] = {
            "total_claims": len(normalized_claims),
            "num_coded": num_coded,
            "num_uncoded": num_uncoded,
            "system_counts": system_counts,
        }

        # Telemetry: Record stage COMPLETED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="NORMALIZATION",
            status="COMPLETED",
            duration_ms=duration_ms,
            metrics=metrics,
        )

        return normalized_claims




    def _orchestrate_verification_stage(
        self,
        session: Session,
        job: ProcessingJob,
        document: Document,
        normalized_claims: Any,
        extraction_output: Any,
        ocr_output: Any,
    ) -> list[Any]:
        """Stage 4: Verification cascade boundary (PROJECT_SPEC §7.6, §10, §12).

        Executes VerificationCascade.verify_claims with claims, evidence_map, and
        page_text_map. Records granular telemetry (STARTED, COMPLETED, FAILED),
        computes PHI-free operational metrics, and persists VerificationResult records
        linked to Claim.id and ModelRun.id with idempotent cleanup.
        """
        job.current_stage = "VERIFICATION"
        session.flush()

        # Telemetry: Record stage STARTED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="VERIFICATION",
            status="STARTED",
        )

        start_time = time.monotonic()
        sensitive_elements = [
            document.original_filename,
            document.patient_context_id,
        ]

        try:
            # 1. Fetch persisted pipeline claims for this document
            db_claims = (
                session.query(Claim)
                .filter(
                    Claim.document_id == document.id,
                    Claim.origin == ClaimOrigin.PIPELINE,
                )
                .all()
            )

            # 2. Reconstruct / collect contract Claim objects
            claims_to_verify: list[Any] = []
            if normalized_claims is not None and isinstance(normalized_claims, list) and normalized_claims:
                claims_to_verify = list(normalized_claims)
            else:
                valid_claim_keys = ContractClaim.model_fields.keys()
                for dc in db_claims:
                    if dc.payload:
                        try:
                            claim_data = {k: v for k, v in dc.payload.items() if k in valid_claim_keys}
                            claims_to_verify.append(ContractClaim.model_validate(claim_data))
                        except Exception:
                            pass

            # 3. Construct evidence_map: dict[str, ContractEvidence]
            evidence_map: dict[str, ContractEvidence] = {}
            if extraction_output is not None and hasattr(extraction_output, "all_evidence") and extraction_output.all_evidence:
                evidence_map = {ev.evidence_id: ev for ev in extraction_output.all_evidence}
            else:
                valid_ev_keys = ContractEvidence.model_fields.keys()
                db_evidences = (
                    session.query(Evidence)
                    .filter(Evidence.document_id == document.id)
                    .all()
                )
                for dev in db_evidences:
                    if dev.payload and "evidence_id" in dev.payload:
                        try:
                            ev_data = {k: v for k, v in dev.payload.items() if k in valid_ev_keys}
                            evidence_map[dev.payload["evidence_id"]] = ContractEvidence.model_validate(ev_data)
                        except Exception:
                            pass

            # 4. Construct page_text_map: dict[str, str]
            page_text_map: Optional[dict[str, str]] = None
            source_doc_text = ocr_output or self.last_ocr_output
            if source_doc_text and hasattr(source_doc_text, "pages"):
                page_text_map = {
                    str(p.page_number): p.get_page_text()
                    for p in source_doc_text.pages
                }

            # 5. Invoke verification cascade
            verification_results = self.cascade.verify_claims(
                claims=claims_to_verify,
                evidence_map=evidence_map,
                page_text_map=page_text_map,
            )

            # 6. Idempotent cleanup of prior VerificationResults for these pipeline claims
            claim_primary_ids = [c.id for c in db_claims]
            if claim_primary_ids:
                session.query(VerificationResult).filter(
                    VerificationResult.claim_id.in_(claim_primary_ids)
                ).delete(synchronize_session=False)

            # 7. Persist VerificationResult records
            db_claims_by_claim_id = {str(c.claim_id): c for c in db_claims}
            profile = get_active_profile()
            is_prod = (profile == RuntimeProfile.PRODUCTION)
            provider_kind = "modernbert_vllm" if is_prod else "mock_dev"

            for idx, res in enumerate(verification_results):
                db_claim = db_claims_by_claim_id.get(res.claim_id)
                if db_claim is None and idx < len(db_claims):
                    db_claim = db_claims[idx]

                if db_claim is None:
                    continue

                try:
                    vr_id = uuid.UUID(res.verification_id)
                except (ValueError, TypeError):
                    vr_id = uuid.uuid4()

                is_fallback = bool(getattr(res, "is_fallback", False))
                fallback_reason = getattr(res, "fallback_reason", None)

                stage_c_details = None
                if res.rationale or is_fallback:
                    stage_c_details = {
                        "rationale": res.rationale,
                        "is_fallback": is_fallback,
                        "fallback_reason": fallback_reason,
                    }
                    if is_fallback and fallback_reason == "llm_unavailable":
                        stage_c_details["handling_path"] = "AWAITING_LLM"

                db_vr = VerificationResult(
                    id=vr_id,
                    claim_id=db_claim.id,
                    label=_map_verification_label(res.label),
                    calibrated_confidence=float(res.confidence),
                    verification_stage=_map_verification_stage(res.stage_reached),
                    escalated=bool(res.escalated),
                    is_fallback=is_fallback,
                    fallback_reason=fallback_reason,
                    provider_kind=provider_kind,
                    model_run_id=job.model_run_id,
                    contradiction_type=_map_contradiction_type(res.contradiction_type),
                    rationale=res.rationale,
                    stage_a_details={"label": res.stage_a_label} if res.stage_a_label else None,
                    stage_b_details={"label": res.stage_b_label, "raw_confidence": res.stage_b_raw_confidence} if res.stage_b_label else None,
                    stage_c_details=stage_c_details,
                    payload=res.model_dump(mode="json"),
                )
                session.add(db_vr)

                # Route escalated fallback claims to human review with reason and AWAITING_LLM provenance (F-06, F-07)
                if is_fallback:
                    db_claim.review_state = ClaimReviewState.ESCALATED
                    claim_payload = dict(db_claim.payload or {})
                    claim_payload["is_fallback"] = True
                    claim_payload["fallback_reason"] = fallback_reason
                    claim_payload["review_reason"] = fallback_reason
                    if fallback_reason == "llm_unavailable":
                        claim_payload["awaiting_llm"] = True
                    db_claim.payload = claim_payload

                # Record claim metrics
                stage_str = f"stage_{str(res.stage_reached).lower()}"
                label_str = str(res.label).lower()
                get_metrics().record_claim(stage_str, label_str)
                if res.escalated or is_fallback:
                    get_metrics().record_claim_requiring_review()

            session.flush()

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(exc, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="VERIFICATION",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            raise

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Operational PHI-free telemetry metrics
        num_supported = sum(1 for r in verification_results if r.label == "SUPPORTED")
        num_discrepancy = sum(1 for r in verification_results if r.label in ("DISCREPANCY", "REFUTED"))
        num_insufficient = sum(1 for r in verification_results if r.label == "INSUFFICIENT_EVIDENCE")
        num_escalated = sum(1 for r in verification_results if r.escalated)
        stage_counts = {"A": 0, "B": 0, "C": 0}
        for r in verification_results:
            if r.stage_reached in stage_counts:
                stage_counts[r.stage_reached] += 1

        metrics: dict[str, Any] = {
            "total_claims_verified": len(verification_results),
            "num_supported": num_supported,
            "num_discrepancy": num_discrepancy,
            "num_insufficient_evidence": num_insufficient,
            "num_escalated": num_escalated,
            "stage_counts": stage_counts,
        }

        # Telemetry: Record stage COMPLETED
        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="VERIFICATION",
            status="COMPLETED",
            duration_ms=duration_ms,
            metrics=metrics,
        )

        return verification_results

    def _orchestrate_persistence_stage(
        self,
        session: Session,
        job: ProcessingJob,
        document: Document,
        extraction_output: Any,
        normalized_claims: Any,
        verification_output: Any,
    ) -> None:
        """Stage 5: Relational persistence and consistency verification boundary."""
        job.current_stage = "PERSISTENCE"
        session.flush()

        start_time = time.monotonic()
        sensitive_elements = [
            document.original_filename,
            document.patient_context_id,
        ]

        self.queue.record_stage_event(
            session=session,
            job_id=job.id,
            stage_name="PERSISTENCE",
            status="STARTED",
        )

        try:
            # 1. Consistency check: verify persisted claims and verifications
            db_claims = (
                session.query(Claim)
                .filter(Claim.document_id == document.id)
                .all()
            )
            claim_ids = [c.id for c in db_claims]
            db_vrs = (
                session.query(VerificationResult)
                .filter(VerificationResult.claim_id.in_(claim_ids))
                .all()
                if claim_ids else []
            )

            duration_ms = int((time.monotonic() - start_time) * 1000)
            metrics = {
                "num_claims_persisted": len(db_claims),
                "num_verifications_persisted": len(db_vrs),
                "num_unanchored_claims": sum(1 for c in db_claims if c.group_id == "__unanchored_review__"),
            }

            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="PERSISTENCE",
                status="COMPLETED",
                duration_ms=duration_ms,
                metrics=metrics,
            )
            session.flush()

        except Exception as exc:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            sanitized_err = self._sanitize_error(exc, sensitive_elements)
            self.queue.record_stage_event(
                session=session,
                job_id=job.id,
                stage_name="PERSISTENCE",
                status="FAILED",
                duration_ms=duration_ms,
                error_message=sanitized_err,
            )
            raise


class DocumentProcessingDaemon:
    """Production background processing daemon for clinical document pipeline.

    Orchestrates continuous queue polling, periodic lease reaper cycles,
    graceful shutdown via SIGTERM/SIGINT, and child subprocess management.
    """

    def __init__(
        self,
        worker: DocumentProcessingWorker,
        session_factory: Callable[[], Session],
        poll_interval: float = 1.0,
        max_backoff: float = 10.0,
        reaper_interval: float = 30.0,
    ) -> None:
        self.worker = worker
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.reaper_interval = reaper_interval
        self._stop_requested = threading.Event()
        self._active_subprocesses: set[Any] = set()
        self._last_reaper_time: float = 0.0

    def register_subprocess(self, proc: Any) -> None:
        """Register a spawned child subprocess for graceful lifecycle management."""
        self._active_subprocesses.add(proc)

    def deregister_subprocess(self, proc: Any) -> None:
        """Deregister a child subprocess that exited normally."""
        self._active_subprocesses.discard(proc)

    def reap_subprocesses(self, timeout: float = 5.0) -> None:
        """Terminate and reap all tracked child subprocesses on shutdown."""
        for proc in list(self._active_subprocesses):
            try:
                if hasattr(proc, "poll") and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=timeout)
                    except Exception:
                        if hasattr(proc, "kill"):
                            proc.kill()
                            proc.wait(timeout=2.0)
            except Exception as e:
                logger.warning("Error terminating child subprocess: %s", e)
            finally:
                self._active_subprocesses.discard(proc)

    def stop(self) -> None:
        """Signal the daemon loop to stop gracefully."""
        self._stop_requested.set()
        self.reap_subprocesses()
        if hasattr(self.worker, "close"):
            self.worker.close()

    def run_single_iteration(self) -> bool:
        """Execute a single reaper check, queue poll, and job processing pass.

        Returns True if a job was claimed and processed, False otherwise.
        """
        now = time.monotonic()
        if now - self._last_reaper_time >= self.reaper_interval:
            with self.session_factory() as sess:
                try:
                    reclaimed = self.worker.queue.reclaim_expired_leases(sess)
                    if reclaimed:
                        sess.commit()
                        logger.info("Reclaimed %d expired job leases", len(reclaimed))
                except Exception as e:
                    sess.rollback()
                    logger.error("Error running queue lease reaper: %s", e)
            self._last_reaper_time = now

        with self.session_factory() as sess:
            claimed_job = self.worker.claim_job(sess)
            if claimed_job is None:
                return False
            sess.commit()
            job_id = claimed_job.id

        # Process claimed job
        with self.session_factory() as sess:
            job = sess.get(ProcessingJob, job_id)
            if job is None:
                return False
            try:
                self.worker.daemon = self
                self.worker.process_job(sess, job)
                sess.commit()
                return True
            except StaleWorkerFencedError as fe:
                sess.rollback()
                logger.warning("Job %s processing aborted: worker fenced out (%s)", job.id, fe)
                return False
            except Exception as e:
                sess.rollback()
                logger.error("Job %s failed with exception: %s", job.id, e)
                return False

    def run(self) -> None:
        """Continuous execution loop with exponential backoff and graceful shutdown."""
        backoff = self.poll_interval

        # Setup signal handlers if running in main thread
        try:
            import signal

            def _signal_handler(signum, frame):
                logger.info("Received signal %s; requesting graceful shutdown", signum)
                self.stop()

            signal.signal(signal.SIGINT, _signal_handler)
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass

        logger.info("DocumentProcessingDaemon started with worker %s", self.worker.worker_id)
        while not self._stop_requested.is_set():
            try:
                processed = self.run_single_iteration()
                if processed:
                    backoff = self.poll_interval
                else:
                    self._stop_requested.wait(backoff)
                    backoff = min(backoff * 1.5, self.max_backoff)
            except Exception as e:
                logger.exception("Unexpected error in daemon iteration: %s", e)
                self._stop_requested.wait(backoff)

        self.reap_subprocesses()
        logger.info("DocumentProcessingDaemon shutdown complete.")


class OutboxDispatchWorker:
    """Background worker daemon thread/loop for dispatching transactional EHR outbox intents (Phase B1)."""

    def __init__(
        self,
        outbox_service: Any,
        worker_id: Optional[str] = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self.outbox_service = outbox_service
        self.worker_id = worker_id or f"outbox-worker-{uuid.uuid4().hex[:8]}"
        self.poll_interval_seconds = poll_interval_seconds
        self._running = False

    def run_single_iteration(self) -> bool:
        """Execute a single claim, dispatch, and reaper cycle."""
        # 1. First run reaper cycle to recover any expired leases
        try:
            self.outbox_service.run_reaper_cycle()
        except Exception as exc:
            logger.error("Outbox reaper cycle encountered error: %s", exc)

        # 2. Claim next pending outbox intent
        claimed_outbox = self.outbox_service.claim_next_outbox_intent(worker_id=self.worker_id)
        if not claimed_outbox:
            return False

        # 3. Process claimed outbox intent
        return self.outbox_service.process_outbox_intent(
            outbox_id=claimed_outbox.id,
            claimed_worker_id=self.worker_id,
            claimed_generation=claimed_outbox.lease_generation,
        )


def main() -> None:
    """Daemon configuration validation and execution entrypoint for background document processing worker."""
    profile = validate_daemon_profile("Background Worker")
    logger.info("Starting Background Document Processing Worker with profile: %s", profile.value)

    from clindoc.api.auth.dependencies import _GLOBAL_SESSION_FACTORY
    if _GLOBAL_SESSION_FACTORY is None:
        logger.warning("Session factory not initialized; worker daemon starting in standalone mode.")
        return

    from clindoc.terminology.service import reset_process_terminology_service
    try:
        worker = DocumentProcessingWorker(session_factory=_GLOBAL_SESSION_FACTORY)
        daemon = DocumentProcessingDaemon(worker=worker, session_factory=_GLOBAL_SESSION_FACTORY)
        daemon.run()
    finally:
        reset_process_terminology_service()


if __name__ == "__main__":
    main()

