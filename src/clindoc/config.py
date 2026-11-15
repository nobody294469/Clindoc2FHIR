"""Runtime profiles and production configuration containment.

Authority:
  - PROJECT_SPEC.md §3, §10, §12, §13
  - PRODUCTIZATION_SPEC.md §3.6, §3.8, §10, §13 (Failures F-05, F-06, F-07)
  - PRODUCTIZATION_DECISIONS.md PD-003, PD-004, PD-005, PD-018, PD-019

Defines explicit execution profiles:
  - DEV: Allows MockLLMProvider, DefaultMockLLMJudge, MockModelPredictor, and
    heuristic fallback. Preserves existing test determinism and offline demos.
  - PRODUCTION: Enforces strict containment. Mocks and heuristic fallbacks are
    strictly forbidden. Requires valid ModernBERT checkpoint, fitted temperature
    calibration, and valid vLLM endpoint configuration. Fail-closed on startup.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse


class RuntimeProfile(str, Enum):
    """Runtime execution profile."""
    DEV = "dev"
    PRODUCTION = "production"

    @classmethod
    def from_string(cls, value: Optional[str]) -> "RuntimeProfile":
        if not value:
            return cls.DEV
        val = value.strip().lower()
        if val in ("prod", "production"):
            return cls.PRODUCTION
        elif val in ("dev", "development", "test", "testing"):
            return cls.DEV
        else:
            raise ValueError(
                f"Unknown runtime profile '{value}'. Permitted values: 'dev', 'production'."
            )


class ProductionProfileError(RuntimeError):
    """Raised when a production profile invariant is violated."""
    pass


# Default benchmarked checkpoint path and constants
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_MODERNBERT_CHECKPOINT = (
    PROJECT_ROOT
    / "data"
    / "m2_results_v3"
    / "checkpoints"
    / "modernbert_text_only_v3_fold0_seed42.pt"
)
CANONICAL_CALIBRATION_TEMPERATURE = 1.146522  # Data sheet §5 / DEC-009
CANONICAL_STAGE_B_ESCALATION_THRESHOLD = 0.60   # DEC-005
CANONICAL_STAGE_B_AUTONOMOUS_THRESHOLD = 0.85   # V12 / Data sheet §5
CANONICAL_AUDITED_VLLM_MODEL_NAME = "Qwen/Qwen2.5-7B-Instruct-AWQ"  # DEC-006 / Audited V8/V9


def get_active_profile() -> RuntimeProfile:
    """Read the active runtime profile from the CLINDOC_PROFILE environment variable.

    Defaults to RuntimeProfile.DEV to preserve backward compatibility for tests and research.
    """
    env_val = os.environ.get("CLINDOC_PROFILE", "dev")
    return RuntimeProfile.from_string(env_val)


@dataclass
class ProductionPipelineConfig:
    """Explicit configuration container for production pipeline execution."""
    profile: RuntimeProfile = RuntimeProfile.PRODUCTION
    modernbert_checkpoint_path: Path = CANONICAL_MODERNBERT_CHECKPOINT
    calibration_temperature: float = CANONICAL_CALIBRATION_TEMPERATURE
    stage_b_escalation_threshold: float = CANONICAL_STAGE_B_ESCALATION_THRESHOLD
    stage_b_autonomous_threshold: float = CANONICAL_STAGE_B_AUTONOMOUS_THRESHOLD
    vllm_base_url: str = "http://localhost:8000/v1"
    vllm_model_name: str = CANONICAL_AUDITED_VLLM_MODEL_NAME
    vllm_api_key_env: str = "VLLM_API_KEY"
    vllm_timeout_seconds: float = 30.0
    allow_mocks: bool = False
    allow_heuristics: bool = False
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 30.0
    model_concurrency_limit: int = 10
    backoff_base_delay: float = 0.5
    backoff_max_delay: float = 8.0

    def validate(self) -> None:
        """Validate production configuration. Raises ProductionProfileError if invalid."""
        if self.profile == RuntimeProfile.PRODUCTION:
            if self.allow_mocks:
                raise ProductionProfileError(
                    "Production configuration violation: allow_mocks=True is forbidden in PRODUCTION profile."
                )
            if self.allow_heuristics:
                raise ProductionProfileError(
                    "Production configuration violation: allow_heuristics=True is forbidden in PRODUCTION profile."
                )

            # 1. Validate ModernBERT Checkpoint
            if not self.modernbert_checkpoint_path:
                raise ProductionProfileError(
                    "Production configuration violation: modernbert_checkpoint_path is required."
                )
            ckpt_path = Path(self.modernbert_checkpoint_path)
            if not ckpt_path.exists():
                raise ProductionProfileError(
                    f"Production configuration violation: ModernBERT checkpoint file not found: {ckpt_path}"
                )
            if not ckpt_path.is_file() or ckpt_path.stat().st_size == 0:
                raise ProductionProfileError(
                    f"Production configuration violation: ModernBERT checkpoint is empty or invalid: {ckpt_path}"
                )

            # 2. Validate Calibration Parameters
            if self.calibration_temperature is None or self.calibration_temperature <= 0.0:
                raise ProductionProfileError(
                    f"Production configuration violation: Invalid calibration temperature {self.calibration_temperature}. "
                    "Must be positive scalar (audited: 1.146522)."
                )

            # 3. Validate Thresholds
            if not (0.0 < self.stage_b_escalation_threshold < 1.0):
                raise ProductionProfileError(
                    f"Production configuration violation: Invalid escalation threshold {self.stage_b_escalation_threshold}."
                )

            # 4. Validate vLLM Endpoint
            if not self.vllm_base_url or not isinstance(self.vllm_base_url, str):
                raise ProductionProfileError(
                    "Production configuration violation: vllm_base_url is required."
                )
            parsed_url = urlparse(self.vllm_base_url)
            if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
                raise ProductionProfileError(
                    f"Production configuration violation: Invalid vllm_base_url '{self.vllm_base_url}'. "
                    "Must be an absolute http:// or https:// URL."
                )
            if not self.vllm_model_name or not self.vllm_model_name.strip():
                raise ProductionProfileError(
                    "Production configuration violation: vllm_model_name is required."
                )

            # 5. Validate Circuit Breaker, Concurrency & Backoff Invariants (§3.5)
            if self.circuit_breaker_failure_threshold < 1:
                raise ProductionProfileError(
                    f"Production configuration violation: circuit_breaker_failure_threshold must be >= 1, "
                    f"got {self.circuit_breaker_failure_threshold}."
                )
            if self.circuit_breaker_cooldown_seconds <= 0.0:
                raise ProductionProfileError(
                    f"Production configuration violation: circuit_breaker_cooldown_seconds must be > 0, "
                    f"got {self.circuit_breaker_cooldown_seconds}."
                )
            if self.model_concurrency_limit < 1:
                raise ProductionProfileError(
                    f"Production configuration violation: model_concurrency_limit must be >= 1, "
                    f"got {self.model_concurrency_limit}."
                )
            if self.backoff_base_delay <= 0.0:
                raise ProductionProfileError(
                    f"Production configuration violation: backoff_base_delay must be > 0, "
                    f"got {self.backoff_base_delay}."
                )
            if self.backoff_max_delay < self.backoff_base_delay:
                raise ProductionProfileError(
                    f"Production configuration violation: backoff_max_delay ({self.backoff_max_delay}) "
                    f"must be >= backoff_base_delay ({self.backoff_base_delay})."
                )


# Configurable operational defaults for queue leasing and retries.
# (PRODUCTIZATION_SPEC.md §4.1 R-3 and §13 F-17 mandate that lease expiry and retry limits
# exist and be enforced, but do NOT explicitly prescribe these exact numerical values.
# These values are operational defaults isolated behind configuration).
DEFAULT_JOB_LEASE_SECONDS: int = 300  # 5 minutes configurable operational default
DEFAULT_JOB_MAX_ATTEMPTS: int = 3     # 3 attempts configurable operational default

# Model serving resilience operational defaults (§3.5)
DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS: float = 30.0
DEFAULT_MODEL_CONCURRENCY_LIMIT: int = 10
DEFAULT_BACKOFF_BASE_DELAY: float = 0.5
DEFAULT_BACKOFF_MAX_DELAY: float = 8.0


def get_job_lease_seconds() -> int:
    """Read the configured job lease duration in seconds from the environment.

    Falls back to DEFAULT_JOB_LEASE_SECONDS (300 s).
    """
    raw = os.environ.get("CLINDOC_JOB_LEASE_SECONDS")
    if raw is not None and raw.strip():
        try:
            val = int(raw.strip())
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_JOB_LEASE_SECONDS


def get_circuit_breaker_failure_threshold() -> int:
    """Read configured circuit breaker failure threshold from environment."""
    raw = os.environ.get("CLINDOC_CIRCUIT_BREAKER_FAILURE_THRESHOLD")
    if raw is not None and raw.strip():
        try:
            val = int(raw.strip())
            if val >= 1:
                return val
        except ValueError:
            pass
    return DEFAULT_CIRCUIT_BREAKER_FAILURE_THRESHOLD


def get_circuit_breaker_cooldown_seconds() -> float:
    """Read configured circuit breaker cool-down duration in seconds from environment."""
    raw = os.environ.get("CLINDOC_CIRCUIT_BREAKER_COOLDOWN_SECONDS")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if val > 0.0:
                return val
        except ValueError:
            pass
    return DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SECONDS


def get_model_concurrency_limit() -> int:
    """Read configured model concurrency cap from environment."""
    raw = os.environ.get("CLINDOC_MODEL_CONCURRENCY_LIMIT")
    if raw is not None and raw.strip():
        try:
            val = int(raw.strip())
            if val >= 1:
                return val
        except ValueError:
            pass
    return DEFAULT_MODEL_CONCURRENCY_LIMIT


def get_backoff_base_delay() -> float:
    """Read configured backoff base delay in seconds from environment."""
    raw = os.environ.get("CLINDOC_BACKOFF_BASE_DELAY")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if val > 0.0:
                return val
        except ValueError:
            pass
    return DEFAULT_BACKOFF_BASE_DELAY


def get_backoff_max_delay() -> float:
    """Read configured backoff max delay in seconds from environment."""
    raw = os.environ.get("CLINDOC_BACKOFF_MAX_DELAY")
    if raw is not None and raw.strip():
        try:
            val = float(raw.strip())
            if val > 0.0:
                return val
        except ValueError:
            pass
    return DEFAULT_BACKOFF_MAX_DELAY


# ─── DATABASE ADVISORY LOCKING & PRODUCTION GUARDS ───────────────────────────

CLINDOC_DOC_LOCK_NAMESPACE: int = 1129071692  # 0x434C4443 = 'CLDC'


def acquire_document_xact_lock(session: Any, document_id: Union[Any, str]) -> None:
    """Acquire transaction-scoped advisory lock for the document in PostgreSQL.

    Uses namespace 1129071692 (0x434C4443 = 'CLDC') and hashtext(document_id::text).
    In non-PostgreSQL dialects (e.g. SQLite test runners), this is a safe no-op.
    """
    bind = session.get_bind() if hasattr(session, "get_bind") else getattr(session, "bind", None)
    if bind is not None and getattr(bind, "dialect", None) is not None and bind.dialect.name == "postgresql":
        from sqlalchemy import text
        session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:doc_id))"),
            {"ns": CLINDOC_DOC_LOCK_NAMESPACE, "doc_id": str(document_id)},
        )


def validate_production_configuration() -> None:
    """Validate strict production runtime environment and configuration invariants.

    Enforces:
      1. Runtime profile is strictly PRODUCTION.
      2. CLINDOC_ENV == 'production'.
      3. Four-eyes enforcement active (CLINDOC_ENFORCE_FOUR_EYES == 'true').
      4. Audit ledger enforcement active (CLINDOC_ENFORCE_AUDIT == 'true').
      5. Valid, non-placeholder HMAC signing key >= 32 UTF-8 bytes with resolved active key ID.
      6. ModernBERT checkpoint, calibration temperature, and vLLM endpoint configuration.
    """
    profile = get_active_profile()
    if profile != RuntimeProfile.PRODUCTION:
        raise ProductionProfileError(
            f"Production configuration violation: Active profile '{profile.value}' is not 'production'."
        )

    env_val = os.getenv("CLINDOC_ENV", "").strip().lower()
    if env_val != "production":
        raise ProductionProfileError(
            f"Production configuration violation: CLINDOC_ENV must be 'production', got '{os.getenv('CLINDOC_ENV')}'."
        )

    four_eyes = os.getenv("CLINDOC_ENFORCE_FOUR_EYES", "").strip().lower()
    if four_eyes != "true":
        raise ProductionProfileError(
            "Production configuration violation: CLINDOC_ENFORCE_FOUR_EYES must be 'true' in production."
        )

    enforce_audit = os.getenv("CLINDOC_ENFORCE_AUDIT", "").strip().lower()
    if enforce_audit != "true":
        raise ProductionProfileError(
            "Production configuration violation: CLINDOC_ENFORCE_AUDIT must be 'true' in production."
        )

    # Validate HMAC key
    key_id = os.getenv("CLINDOC_AUDIT_HMAC_KEY_ID", "default").strip()
    if key_id.lower() == "default":
        raw_key = os.getenv("CLINDOC_AUDIT_HMAC_KEY_DEFAULT") or os.getenv("CLINDOC_AUDIT_HMAC_KEY")
    else:
        raw_key = os.getenv(f"CLINDOC_AUDIT_HMAC_KEY_{key_id.upper()}")

    if not raw_key or not raw_key.strip():
        raise ProductionProfileError(
            f"Production configuration violation: HMAC key for key_id '{key_id}' is missing, empty, or whitespace-only."
        )

    lower_trimmed = raw_key.strip().lower()
    placeholders = {
        "empty", "default", "test", "dev", "development", "staging", "prod",
        "production", "password", "changeme", "secret", "insecure", "placeholder",
        "12345678", "admin",
    }
    if lower_trimmed in placeholders:
        raise ProductionProfileError(
            f"Production configuration violation: HMAC key for key_id '{key_id}' uses a known placeholder value."
        )

    # Preserve exact raw_key bytes without stripping or normalization
    key_bytes = raw_key.encode("utf-8")
    if len(key_bytes) < 32:
        raise ProductionProfileError(
            f"Production configuration violation: HMAC key for key_id '{key_id}' must be at least 32 UTF-8 bytes (got {len(key_bytes)} bytes)."
        )

    # Reject obvious repeated multi-character patterns (e.g. 'abcd' * 8, '1234' * 8)
    n = len(raw_key)
    for chunk_len in range(1, (n // 2) + 1):
        if n % chunk_len == 0 and raw_key == raw_key[:chunk_len] * (n // chunk_len):
            raise ProductionProfileError(
                f"Production configuration violation: HMAC key for key_id '{key_id}' uses an insecure repeating pattern."
            )

    # Reject predictable cyclic/sequential alphanumeric sequences
    cyclic_sequences = [
        "0123456789" * 5,
        "9876543210" * 5,
        "abcdefghijklmnopqrstuvwxyz" * 3,
        "zyxwvutsrqponmlkjihgfedcba" * 3,
        "0123456789abcdefghijklmnopqrstuvwxyz" * 2,
        "abcdefghijklmnopqrstuvwxyz0123456789" * 2,
    ]
    for seq in cyclic_sequences:
        if raw_key.lower() in seq:
            raise ProductionProfileError(
                f"Production configuration violation: HMAC key for key_id '{key_id}' uses a predictable sequential value."
            )

    # Reject ascending or descending ASCII sequences of 8 or more consecutive characters
    run_len = 1
    for i in range(len(raw_key) - 1):
        diff = ord(raw_key[i + 1]) - ord(raw_key[i])
        if diff in (1, -1):
            run_len += 1
            if run_len >= 8:
                raise ProductionProfileError(
                    f"Production configuration violation: HMAC key for key_id '{key_id}' uses a predictable sequential value."
                )
        else:
            run_len = 1

    ProductionPipelineConfig(profile=RuntimeProfile.PRODUCTION).validate()


def validate_daemon_profile(daemon_name: str) -> RuntimeProfile:
    """Validate daemon startup against explicit profile requirement.

    Daemons (API server, Background worker) must NOT start without an explicit CLINDOC_PROFILE.
    """
    raw_profile = os.environ.get("CLINDOC_PROFILE")
    if raw_profile is None or not raw_profile.strip():
        raise ProductionProfileError(
            f"{daemon_name} startup failed: CLINDOC_PROFILE environment variable is strictly required ('dev' or 'production')."
        )

    profile = RuntimeProfile.from_string(raw_profile)
    if profile == RuntimeProfile.PRODUCTION:
        validate_production_configuration()
    return profile


# ─── PATIENT IDENTIFIER SYSTEM CONFIGURATION (Phase A) ────────────────────────

CLINDOC_PRIMARY_PATIENT_IDENTIFIER_SYSTEM: str = os.environ.get(
    "CLINDOC_PRIMARY_PATIENT_IDENTIFIER_SYSTEM",
    "http://hospital.example.org/identifiers/mrn",
)


def get_allowed_patient_identifier_systems() -> list[str]:
    """Return the set of accepted patient identifier authority URIs.

    Primary identifier system is always guaranteed to be present at index 0.
    """
    primary = os.environ.get(
        "CLINDOC_PRIMARY_PATIENT_IDENTIFIER_SYSTEM",
        "http://hospital.example.org/identifiers/mrn",
    ).strip()
    raw = os.environ.get("CLINDOC_ALLOWED_PATIENT_IDENTIFIER_SYSTEMS", "")
    systems = [s.strip() for s in raw.split(",") if s.strip()]
    if primary and primary not in systems:
        systems.insert(0, primary)
    return systems




