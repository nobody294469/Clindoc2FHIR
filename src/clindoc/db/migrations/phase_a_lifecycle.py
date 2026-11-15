"""Native Schema Migration Engine for Phase A EHR Transmission Lifecycle.

Authority:
  - Phase A Frozen Architectural Specification
  - chk_fhir_exports_lifecycle & chk_fhir_exports_attempt_lineage constraints
  - Dual-Engine (PostgreSQL & SQLite) Table Rebuild & Constraint Hardening
  - fhir_export_migration_audit Ledger

Invariants:
  1. Single-writer migration locking:
     - PostgreSQL: pg_advisory_xact_lock(918273645)
     - SQLite: BEGIN IMMEDIATE exclusive transaction
  2. Idempotency & Safe Version Tracking:
     - schema_migrations version table tracking 'phase_a_lifecycle'
     - Clean exit with MigrationResult(already_applied=True) if rerun
  3. Fail-safe Recovery:
     - Atomic transaction rollback on any DDL, data classification, or check constraint failure
  4. SQLite Table Rebuild:
     - Drops and recreates dependent views/triggers (e.g. v_active_exports) within transaction
     - Preserves existing data, foreign keys (ON DELETE RESTRICT), indexes, unique constraints
  5. Audit Accuracy:
     - Records true before-and-after values in fhir_export_migration_audit
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import logging
from typing import Any, List, Optional
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

MIGRATION_VERSION = "phase_a_lifecycle"
POSTGRESQL_ADVISORY_LOCK_ID = 918273645


@dataclass
class MigrationResult:
    """Result summary of Phase A lifecycle migration execution."""

    version: str = MIGRATION_VERSION
    dialect: str = ""
    already_applied: bool = False
    migrated_rows_count: int = 0
    audit_records_count: int = 0
    views_recreated: list[str] = field(default_factory=list)
    indexes_recreated: list[str] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None


# ─── CANONICAL EVIDENCE MARKERS ──────────────────────────────────────────────

AMBIGUITY_MARKERS = [
    "TIMEOUT",
    "504",
    "UNKNOWN_RECONCILIATION_REQUIRED",
    "READTIMEOUT",
    "GATEWAY TIMEOUT",
    "MOCK_TIMEOUT",
]

STRUCTURED_DNS_MARKERS = [
    "NXDOMAIN",
    "NAME OR SERVICE NOT KNOWN",
    "NODENAME NOR SERVNAME PROVIDED",
    "GETADDRINFO FAILED",
]

DETERMINISTIC_FAILURE_MARKERS = [
    "AUTH_ERROR",
    "401",
    "403",
    "VALIDATION_ERROR",
    "CONNECTIONREFUSED",
    "CONNECTION REFUSED",
    "CONNECTERROR",
    "REJECT_VALIDATION",
    "AUTH_FAILURE",
]


# ─── MIGRATION RUNNER CLASS ──────────────────────────────────────────────────

class PhaseALifecycleMigrationRunner:
    """Native SQLAlchemy 2.0 migration runner for Phase A lifecycle schema."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.dialect_name = engine.dialect.name

    def run(self) -> MigrationResult:
        """Execute Phase A lifecycle migration with dialect-specific single-writer locking."""
        with self.engine.connect() as conn:
            if self.dialect_name == "postgresql":
                return self._run_postgresql(conn)
            elif self.dialect_name == "sqlite":
                return self._run_sqlite(conn)
            else:
                raise NotImplementedError(f"Unsupported database dialect: {self.dialect_name}")

    # ─────────────────────────────────────────────────────────────────────────
    # POSTGRESQL MIGRATION PATH
    # ─────────────────────────────────────────────────────────────────────────

    def _run_postgresql(self, conn: Connection) -> MigrationResult:
        """PostgreSQL migration with advisory lock and ALTER TABLE semantics."""
        result = MigrationResult(dialect="postgresql")
        trans = conn.begin()
        try:
            # 1. Acquire PostgreSQL Advisory Transaction Lock
            conn.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": POSTGRESQL_ADVISORY_LOCK_ID},
            )

            # 2. Ensure schema_migrations table exists
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(128) PRIMARY KEY,
                    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    applied_by VARCHAR(128) DEFAULT CURRENT_USER
                )
                """)
            )

            # 3. Check if already applied
            applied = conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = :ver"),
                {"ver": MIGRATION_VERSION},
            ).scalar_one_or_none()

            if applied is not None:
                trans.rollback()
                result.already_applied = True
                return result

            # 4. Ensure auxiliary tables exist: ehr_transmission_reconciliations & fhir_export_migration_audit
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS ehr_transmission_reconciliations (
                    id UUID PRIMARY KEY,
                    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
                    bundle_id UUID NOT NULL REFERENCES fhir_bundles(id) ON DELETE RESTRICT,
                    prior_export_id UUID NOT NULL REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                    destination_identity VARCHAR(512) NOT NULL,
                    bundle_sha256 VARCHAR(64) NOT NULL,
                    destination_idempotency_key VARCHAR(128) NOT NULL,
                    decision VARCHAR(64) NOT NULL,
                    justification TEXT NOT NULL,
                    evidence_type VARCHAR(64) NOT NULL,
                    evidence_reference VARCHAR(256) NOT NULL,
                    verification_level VARCHAR(32) NOT NULL DEFAULT 'CLINICAL_CHIEF_REVIEW',
                    primary_authorizer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    primary_authorizer_role VARCHAR(64) NOT NULL,
                    supervisor_authorizer_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    supervisor_authorizer_role VARCHAR(64) NOT NULL,
                    authorized_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    consumed_at TIMESTAMP WITH TIME ZONE,
                    consumed_by_export_id UUID REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                    revoked_at TIMESTAMP WITH TIME ZONE,
                    revoked_by_id UUID REFERENCES users(id) ON DELETE SET NULL,
                    CONSTRAINT chk_reconciliation_distinct_actors CHECK (primary_authorizer_id != supervisor_authorizer_id)
                )
                """)
            )

            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS fhir_export_migration_audit (
                    id SERIAL PRIMARY KEY,
                    export_id VARCHAR(36) NOT NULL,
                    old_target_type VARCHAR(32),
                    new_target_type VARCHAR(32) NOT NULL,
                    old_export_status VARCHAR(32),
                    new_export_status VARCHAR(32) NOT NULL,
                    old_transmission_state VARCHAR(32),
                    new_transmission_state VARCHAR(32),
                    old_resolution_status VARCHAR(32),
                    new_resolution_status VARCHAR(32),
                    has_conflicting_evidence BOOLEAN NOT NULL DEFAULT FALSE,
                    migration_reason VARCHAR(64) NOT NULL,
                    migrated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            )

            # 5. Add new columns to fhir_exports if missing
            new_columns = [
                ("transmission_state", "VARCHAR(32)"),
                ("resolution_status", "VARCHAR(32)"),
                ("destination_identity", "VARCHAR(512)"),
                ("destination_idempotency_key", "VARCHAR(128)"),
                ("logical_export_id", "UUID"),
                ("attempt_number", "INTEGER DEFAULT 1 NOT NULL"),
                ("parent_attempt_id", "UUID REFERENCES fhir_exports(id) ON DELETE RESTRICT"),
                ("error_message", "TEXT"),
            ]
            for col_name, col_type in new_columns:
                col_exists = conn.execute(
                    text("""
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'fhir_exports' AND column_name = :cname
                    """),
                    {"cname": col_name},
                ).scalar_one_or_none()
                if not col_exists:
                    conn.execute(text(f"ALTER TABLE fhir_exports ADD COLUMN {col_name} {col_type}"))

            # 6. Normalize existing data & populate audit table
            audit_count, migrated_count = self._normalize_postgresql_data(conn)
            result.audit_records_count = audit_count
            result.migrated_rows_count = migrated_count

            # 7. Add Constraints & Indexes
            # Attempt lineage check
            conn.execute(
                text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chk_fhir_exports_attempt_lineage'
                    ) THEN
                        ALTER TABLE fhir_exports ADD CONSTRAINT chk_fhir_exports_attempt_lineage
                        CHECK (((attempt_number = 1 AND parent_attempt_id IS NULL) OR (attempt_number > 1 AND parent_attempt_id IS NOT NULL)));
                    END IF;
                END $$;
                """)
            )

            # Lifecycle check
            conn.execute(
                text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'chk_fhir_exports_lifecycle'
                    ) THEN
                        ALTER TABLE fhir_exports ADD CONSTRAINT chk_fhir_exports_lifecycle
                        CHECK (
                            (target_type = 'FILE_DOWNLOAD' AND 
                             export_status IN ('PENDING', 'GENERATED', 'FAILED') AND 
                             transmission_state IS NULL AND 
                             resolution_status IS NULL AND 
                             destination_identity IS NULL)
                            OR
                            (target_type = 'EHR_TRANSMISSION' AND destination_identity IS NOT NULL AND (
                                (export_status = 'PENDING_TRANSPORT' AND transmission_state = 'PENDING_TRANSPORT' AND resolution_status = 'UNRESOLVED') OR
                                (export_status = 'TRANSMITTED' AND transmission_state = 'TRANSMITTED' AND resolution_status = 'RESOLVED_DELIVERED') OR
                                (export_status = 'FAILED' AND transmission_state = 'CONFIRMED_FAILED' AND resolution_status = 'RESOLVED_NON_DELIVERY') OR
                                (export_status = 'FAILED' AND transmission_state IN ('UNKNOWN_RECONCILIATION_REQUIRED', 'HISTORICAL_INDETERMINATE') AND resolution_status = 'UNRESOLVED')
                            ))
                        );
                    END IF;
                END $$;
                """)
            )

            # Unique constraint on (logical_export_id, attempt_number)
            conn.execute(
                text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint WHERE conname = 'uq_fhir_exports_logical_attempt'
                    ) THEN
                        ALTER TABLE fhir_exports ADD CONSTRAINT uq_fhir_exports_logical_attempt
                        UNIQUE (logical_export_id, attempt_number);
                    END IF;
                END $$;
                """)
            )

            # Indexes
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fhir_exports_logical_id ON fhir_exports(logical_export_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fhir_exports_dest_idem_key ON fhir_exports(destination_idempotency_key)"))

            # 8. Mark version as applied
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:ver)"),
                {"ver": MIGRATION_VERSION},
            )

            trans.commit()
            result.success = True
            return result

        except Exception as exc:
            trans.rollback()
            logger.error("PostgreSQL Phase A migration failed: %s", exc)
            result.success = False
            result.error_message = str(exc)
            raise

    def _normalize_postgresql_data(self, conn: Connection) -> tuple[int, int]:
        """Classify and normalize PostgreSQL fhir_exports rows into Phase A states."""
        # Query rows that need normalization (where transmission_state is NULL on EHR_TRANSMISSION or unclassified)
        rows = conn.execute(
            text("""
            SELECT id, target_type, export_status, transmission_state, resolution_status,
                   destination_path, idempotency_key, error_message
            FROM fhir_exports
            """)
        ).fetchall()

        migrated_count = 0
        audit_count = 0

        for row in rows:
            r_id = str(row.id)
            orig_target = row.target_type
            orig_status = row.export_status
            orig_state = row.transmission_state
            orig_res = row.resolution_status
            dest_path = row.destination_path or ""
            idem_key = row.idempotency_key or ""
            err_msg = row.error_message or ""

            norm_target, norm_status, norm_state, norm_res, norm_dest, has_conflict, reason = self._classify_row(
                orig_target, orig_status, orig_state, orig_res, dest_path, idem_key, err_msg
            )

            # Check if values actually changed
            changed = (
                norm_target != orig_target
                or norm_status != orig_status
                or norm_state != orig_state
                or norm_res != orig_res
            )

            # Always populate attempt_number and logical_export_id if missing
            conn.execute(
                text("""
                UPDATE fhir_exports
                SET attempt_number = COALESCE(attempt_number, 1),
                    logical_export_id = COALESCE(logical_export_id, id)
                WHERE id = :id
                """),
                {"id": row.id},
            )

            if changed:
                # Update row
                conn.execute(
                    text("""
                    UPDATE fhir_exports
                    SET target_type = :target,
                        export_status = :status,
                        transmission_state = :state,
                        resolution_status = :res,
                        destination_identity = :dest
                    WHERE id = :id
                    """),
                    {
                        "id": row.id,
                        "target": norm_target,
                        "status": norm_status,
                        "state": norm_state,
                        "res": norm_res,
                        "dest": norm_dest,
                    },
                )
                migrated_count += 1

                # Record in audit
                conn.execute(
                    text("""
                    INSERT INTO fhir_export_migration_audit (
                        export_id, old_target_type, new_target_type,
                        old_export_status, new_export_status,
                        old_transmission_state, new_transmission_state,
                        old_resolution_status, new_resolution_status,
                        has_conflicting_evidence, migration_reason
                    ) VALUES (
                        :export_id, :old_target, :new_target,
                        :old_status, :new_status,
                        :old_state, :new_state,
                        :old_res, :new_res,
                        :has_conflict, :reason
                    )
                    """),
                    {
                        "export_id": r_id,
                        "old_target": orig_target,
                        "new_target": norm_target,
                        "old_status": orig_status,
                        "new_status": norm_status,
                        "old_state": orig_state,
                        "new_state": norm_state,
                        "old_res": orig_res,
                        "new_res": norm_res,
                        "has_conflict": has_conflict,
                        "reason": reason,
                    },
                )
                audit_count += 1

        return audit_count, migrated_count

    # ─────────────────────────────────────────────────────────────────────────
    # SQLITE MIGRATION PATH
    # ─────────────────────────────────────────────────────────────────────────

    def _run_sqlite(self, conn: Connection) -> MigrationResult:
        """SQLite migration with table rebuild, view preservation, and foreign keys."""
        result = MigrationResult(dialect="sqlite")

        # 1. Single-writer check / table lock
        conn.execute(text("BEGIN IMMEDIATE"))

        try:
            # 2. Ensure schema_migrations exists
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    applied_by TEXT DEFAULT 'sqlite_local'
                )
                """)
            )

            # 3. Check if already applied
            applied = conn.execute(
                text("SELECT version FROM schema_migrations WHERE version = :ver"),
                {"ver": MIGRATION_VERSION},
            ).scalar_one_or_none()

            if applied is not None:
                conn.execute(text("COMMIT"))
                result.already_applied = True
                return result

            # 4. Create auxiliary tables if not exists
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS ehr_transmission_reconciliations (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
                    bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE RESTRICT,
                    prior_export_id TEXT NOT NULL REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                    destination_identity TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    destination_idempotency_key TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    justification TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    evidence_reference TEXT NOT NULL,
                    verification_level TEXT NOT NULL DEFAULT 'CLINICAL_CHIEF_REVIEW',
                    primary_authorizer_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    primary_authorizer_role TEXT NOT NULL,
                    supervisor_authorizer_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    supervisor_authorizer_role TEXT NOT NULL,
                    authorized_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    consumed_at TIMESTAMP,
                    consumed_by_export_id TEXT REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                    revoked_at TIMESTAMP,
                    revoked_by_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    CONSTRAINT chk_reconciliation_distinct_actors CHECK (primary_authorizer_id != supervisor_authorizer_id)
                )
                """)
            )

            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS fhir_export_migration_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    export_id TEXT NOT NULL,
                    old_target_type TEXT,
                    new_target_type TEXT NOT NULL,
                    old_export_status TEXT,
                    new_export_status TEXT NOT NULL,
                    old_transmission_state TEXT,
                    new_transmission_state TEXT,
                    old_resolution_status TEXT,
                    new_resolution_status TEXT,
                    has_conflicting_evidence BOOLEAN NOT NULL DEFAULT 0,
                    migration_reason TEXT NOT NULL,
                    migrated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
            )

            # 5. Inspect existing fhir_exports table
            tbl_exists = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='fhir_exports'")
            ).scalar_one_or_none()

            if not tbl_exists:
                # Fresh database creation
                self._create_fresh_sqlite_fhir_exports(conn)
            else:
                # Check if rebuild is necessary
                columns = [
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info('fhir_exports')")).fetchall()
                ]
                needs_rebuild = "transmission_state" not in columns or "destination_identity" not in columns

                if needs_rebuild:
                    audit_count, migrated_count, recreated_views, recreated_indexes = self._rebuild_sqlite_table(conn)
                    result.audit_records_count = audit_count
                    result.migrated_rows_count = migrated_count
                    result.views_recreated = recreated_views
                    result.indexes_recreated = recreated_indexes

            # 6. Verify foreign keys and integrity
            fk_violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
            if fk_violations:
                raise RuntimeError(f"Foreign key violations detected after migration: {fk_violations}")

            # 7. Record version
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:ver)"),
                {"ver": MIGRATION_VERSION},
            )

            conn.execute(text("COMMIT"))
            result.success = True
            return result

        except Exception as exc:
            conn.execute(text("ROLLBACK"))
            logger.error("SQLite Phase A migration failed: %s", exc)
            result.success = False
            result.error_message = str(exc)
            raise

    def _create_fresh_sqlite_fhir_exports(self, conn: Connection) -> None:
        """Create fresh fhir_exports table in SQLite with all constraints."""
        conn.execute(
            text("""
            CREATE TABLE fhir_exports (
                id TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL DEFAULT 'EHR_TRANSMISSION',
                export_status TEXT NOT NULL,
                transmission_state TEXT,
                resolution_status TEXT,
                destination_identity TEXT,
                destination_idempotency_key TEXT,
                logical_export_id TEXT,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                parent_attempt_id TEXT REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                idempotency_key TEXT NOT NULL UNIQUE,
                destination_path TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                exported_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                exported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                CONSTRAINT chk_fhir_exports_lifecycle CHECK (
                    (target_type = 'FILE_DOWNLOAD' AND 
                     export_status IN ('PENDING', 'GENERATED', 'FAILED') AND 
                     transmission_state IS NULL AND 
                     resolution_status IS NULL AND 
                     destination_identity IS NULL)
                    OR
                    (target_type = 'EHR_TRANSMISSION' AND destination_identity IS NOT NULL AND (
                        (export_status = 'PENDING_TRANSPORT' AND transmission_state = 'PENDING_TRANSPORT' AND resolution_status = 'UNRESOLVED') OR
                        (export_status = 'TRANSMITTED' AND transmission_state = 'TRANSMITTED' AND resolution_status = 'RESOLVED_DELIVERED') OR
                        (export_status = 'FAILED' AND transmission_state = 'CONFIRMED_FAILED' AND resolution_status = 'RESOLVED_NON_DELIVERY') OR
                        (export_status = 'FAILED' AND transmission_state IN ('UNKNOWN_RECONCILIATION_REQUIRED', 'HISTORICAL_INDETERMINATE') AND resolution_status = 'UNRESOLVED')
                    ))
                ),
                CONSTRAINT chk_fhir_exports_attempt_lineage CHECK (
                    ((attempt_number = 1 AND parent_attempt_id IS NULL) OR (attempt_number > 1 AND parent_attempt_id IS NOT NULL))
                ),
                CONSTRAINT uq_fhir_exports_logical_attempt UNIQUE (logical_export_id, attempt_number)
            )
            """)
        )
        conn.execute(text("CREATE INDEX ix_fhir_exports_bundle_id ON fhir_exports(bundle_id)"))
        conn.execute(text("CREATE INDEX ix_fhir_exports_logical_id ON fhir_exports(logical_export_id)"))
        conn.execute(text("CREATE INDEX ix_fhir_exports_dest_idem_key ON fhir_exports(destination_idempotency_key)"))

    def _rebuild_sqlite_table(self, conn: Connection) -> tuple[int, int, list[str], list[str]]:
        """Perform SQLite 12-step table rebuild preserving views, triggers, indexes and foreign keys."""
        # Step A: Collect dependent views and triggers
        views_and_triggers = conn.execute(
            text("""
            SELECT type, name, sql FROM sqlite_master 
            WHERE (tbl_name = 'fhir_exports' OR sql LIKE '%fhir_exports%')
              AND type IN ('view', 'trigger')
            """)
        ).fetchall()

        # Step B: Drop views and triggers temporarily
        for item in views_and_triggers:
            conn.execute(text(f"DROP {item.type.upper()} IF EXISTS {item.name}"))

        # Step C: Create staging table with target schema
        conn.execute(
            text("""
            CREATE TABLE fhir_exports__new (
                id TEXT PRIMARY KEY,
                bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE CASCADE,
                target_type TEXT NOT NULL DEFAULT 'EHR_TRANSMISSION',
                export_status TEXT NOT NULL,
                transmission_state TEXT,
                resolution_status TEXT,
                destination_identity TEXT,
                destination_idempotency_key TEXT,
                logical_export_id TEXT,
                attempt_number INTEGER NOT NULL DEFAULT 1,
                parent_attempt_id TEXT REFERENCES fhir_exports__new(id) ON DELETE RESTRICT,
                idempotency_key TEXT NOT NULL UNIQUE,
                destination_path TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                exported_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                exported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                error_message TEXT,
                CONSTRAINT chk_fhir_exports_lifecycle CHECK (
                    (target_type = 'FILE_DOWNLOAD' AND 
                     export_status IN ('PENDING', 'GENERATED', 'FAILED') AND 
                     transmission_state IS NULL AND 
                     resolution_status IS NULL AND 
                     destination_identity IS NULL)
                    OR
                    (target_type = 'EHR_TRANSMISSION' AND destination_identity IS NOT NULL AND (
                        (export_status = 'PENDING_TRANSPORT' AND transmission_state = 'PENDING_TRANSPORT' AND resolution_status = 'UNRESOLVED') OR
                        (export_status = 'TRANSMITTED' AND transmission_state = 'TRANSMITTED' AND resolution_status = 'RESOLVED_DELIVERED') OR
                        (export_status = 'FAILED' AND transmission_state = 'CONFIRMED_FAILED' AND resolution_status = 'RESOLVED_NON_DELIVERY') OR
                        (export_status = 'FAILED' AND transmission_state IN ('UNKNOWN_RECONCILIATION_REQUIRED', 'HISTORICAL_INDETERMINATE') AND resolution_status = 'UNRESOLVED')
                    ))
                ),
                CONSTRAINT chk_fhir_exports_attempt_lineage CHECK (
                    ((attempt_number = 1 AND parent_attempt_id IS NULL) OR (attempt_number > 1 AND parent_attempt_id IS NOT NULL))
                ),
                CONSTRAINT uq_fhir_exports_logical_attempt UNIQUE (logical_export_id, attempt_number)
            )
            """)
        )

        # Step D: Read existing records and classify
        existing_rows = conn.execute(text("SELECT * FROM fhir_exports")).fetchall()
        col_names = [d[0] for d in conn.execute(text("SELECT * FROM fhir_exports LIMIT 0")).cursor.description]

        migrated_count = 0
        audit_count = 0

        for r in existing_rows:
            r_dict = dict(zip(col_names, r))
            r_id = str(r_dict.get("id"))
            orig_target = r_dict.get("target_type")
            orig_status = r_dict.get("export_status")
            orig_state = r_dict.get("transmission_state")
            orig_res = r_dict.get("resolution_status")
            dest_path = r_dict.get("destination_path") or ""
            idem_key = r_dict.get("idempotency_key") or ""
            err_msg = r_dict.get("error_message") or ""

            norm_target, norm_status, norm_state, norm_res, norm_dest, has_conflict, reason = self._classify_row(
                orig_target, orig_status, orig_state, orig_res, dest_path, idem_key, err_msg
            )

            # Insert into staging table
            conn.execute(
                text("""
                INSERT INTO fhir_exports__new (
                    id, bundle_id, target_type, export_status, transmission_state,
                    resolution_status, destination_identity, destination_idempotency_key,
                    logical_export_id, attempt_number, parent_attempt_id, idempotency_key,
                    destination_path, attempts, exported_by, exported_at, error_message
                ) VALUES (
                    :id, :bundle_id, :target_type, :export_status, :transmission_state,
                    :resolution_status, :destination_identity, :destination_idempotency_key,
                    :logical_export_id, :attempt_number, :parent_attempt_id, :idempotency_key,
                    :destination_path, :attempts, :exported_by, :exported_at, :error_message
                )
                """),
                {
                    "id": r_dict["id"],
                    "bundle_id": r_dict["bundle_id"],
                    "target_type": norm_target,
                    "export_status": norm_status,
                    "transmission_state": norm_state,
                    "resolution_status": norm_res,
                    "destination_identity": norm_dest,
                    "destination_idempotency_key": r_dict.get("destination_idempotency_key"),
                    "logical_export_id": r_dict.get("logical_export_id") or r_dict["id"],
                    "attempt_number": r_dict.get("attempt_number") or 1,
                    "parent_attempt_id": r_dict.get("parent_attempt_id"),
                    "idempotency_key": r_dict["idempotency_key"],
                    "destination_path": r_dict.get("destination_path"),
                    "attempts": r_dict.get("attempts") or 1,
                    "exported_by": r_dict["exported_by"],
                    "exported_at": r_dict.get("exported_at"),
                    "error_message": r_dict.get("error_message"),
                },
            )
            # Check if lifecycle values actually changed
            changed = (
                norm_target != orig_target
                or norm_status != orig_status
                or norm_state != orig_state
                or norm_res != orig_res
            )

            if changed:
                migrated_count += 1
                # Populate audit ledger
                conn.execute(
                    text("""
                    INSERT INTO fhir_export_migration_audit (
                        export_id, old_target_type, new_target_type,
                        old_export_status, new_export_status,
                        old_transmission_state, new_transmission_state,
                        old_resolution_status, new_resolution_status,
                        has_conflicting_evidence, migration_reason
                    ) VALUES (
                        :export_id, :old_target, :new_target,
                        :old_status, :new_status,
                        :old_state, :new_state,
                        :old_res, :new_res,
                        :has_conflict, :reason
                    )
                    """),
                    {
                        "export_id": r_id,
                        "old_target": orig_target,
                        "new_target": norm_target,
                        "old_status": orig_status,
                        "new_status": norm_status,
                        "old_state": orig_state,
                        "new_state": norm_state,
                        "old_res": orig_res,
                        "new_res": norm_res,
                        "has_conflict": has_conflict,
                        "reason": reason,
                    },
                )
                audit_count += 1

        # Step E: Drop old table and rename staging table
        conn.execute(text("DROP TABLE fhir_exports"))
        conn.execute(text("ALTER TABLE fhir_exports__new RENAME TO fhir_exports"))

        # Step F: Recreate indexes
        recreated_indexes = [
            "ix_fhir_exports_bundle_id",
            "ix_fhir_exports_logical_id",
            "ix_fhir_exports_dest_idem_key",
        ]
        conn.execute(text("CREATE INDEX ix_fhir_exports_bundle_id ON fhir_exports(bundle_id)"))
        conn.execute(text("CREATE INDEX ix_fhir_exports_logical_id ON fhir_exports(logical_export_id)"))
        conn.execute(text("CREATE INDEX ix_fhir_exports_dest_idem_key ON fhir_exports(destination_idempotency_key)"))

        # Step G: Recreate views and triggers
        recreated_views = []
        for item in views_and_triggers:
            conn.execute(text(item.sql))
            recreated_views.append(item.name)

        return audit_count, migrated_count, recreated_views, recreated_indexes

    # ─────────────────────────────────────────────────────────────────────────
    # CLASSIFICATION LOGIC (SHARED ENGINE)
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def _classify_row(
        cls,
        orig_target: Optional[str],
        orig_status: Optional[str],
        orig_state: Optional[str],
        orig_res: Optional[str],
        dest_path: str,
        idem_key: str,
        err_msg: str,
    ) -> tuple[str, str, Optional[str], Optional[str], Optional[str], bool, str]:
        """Classify a legacy row into valid (target_type, export_status, state, res, dest_ident, has_conflict, reason).
        
        Adheres strictly to the frozen Phase A specification:
          1. Ambiguity / In-Flight Markers ≻ Deterministic Failure Markers.
          2. Structured Pre-Connection DNS ≻ Generic Unstructured DNS.
          3. Unexpected legacy status fails safe to FAILED / HISTORICAL_INDETERMINATE.
          4. EHR_TRANSMISSION with GENERATED fails safe to FAILED / HISTORICAL_INDETERMINATE.
        """
        dest_upper = dest_path.upper()
        idem_upper = idem_key.upper()
        err_upper = err_msg.upper()
        status_upper = (orig_status or "").upper()
        target_upper = (orig_target or "").upper()

        # Detect evidence types
        has_file_evidence = (
            target_upper == "FILE_DOWNLOAD"
            or idem_upper.startswith("EXPORT-")
            or dest_upper.endswith(".JSON")
            or dest_upper.startswith("BLOBS/")
        )

        has_ehr_evidence = (
            target_upper == "EHR_TRANSMISSION"
            or idem_upper.startswith("EHR-")
            or dest_upper.startswith("EHR://")
            or dest_upper.startswith("HTTP://")
            or dest_upper.startswith("HTTPS://")
            or any(m in err_upper for m in AMBIGUITY_MARKERS)
            or any(m in err_upper for m in DETERMINISTIC_FAILURE_MARKERS)
            or any(m in err_upper for m in STRUCTURED_DNS_MARKERS)
        )

        has_conflict = bool(has_file_evidence and has_ehr_evidence)

        # Precedence 1: Resolve target_type
        # If target is EHR_TRANSMISSION or has explicit EHR destination, treat as EHR
        is_ehr = (
            target_upper == "EHR_TRANSMISSION"
            or dest_upper.startswith("EHR://")
            or dest_upper.startswith("HTTP://")
            or dest_upper.startswith("HTTPS://")
            or (has_ehr_evidence and not has_file_evidence)
        )

        if not is_ehr:
            # Normalized FILE_DOWNLOAD
            # Must satisfy: export_status IN ('PENDING', 'GENERATED', 'FAILED'), state/res/dest IS NULL
            if status_upper in ("PENDING", "PENDING_TRANSPORT"):
                new_status = "PENDING"
            elif status_upper in ("GENERATED", "EXPORTED_FILE", "COMPLETED", "TRANSMITTED"):
                new_status = "GENERATED"
            else:
                new_status = "FAILED"

            return (
                "FILE_DOWNLOAD",
                new_status,
                None,
                None,
                None,
                has_conflict,
                "CLASSIFIED_FILE_DOWNLOAD",
            )

        # ─── EHR_TRANSMISSION ────────────────────────────────────────────────
        canonical_dest = dest_path if (dest_upper.startswith("HTTP") or dest_upper.startswith("EHR")) else "ehr://legacy-unknown"

        # Check markers
        has_ambiguity = any(m in err_upper for m in AMBIGUITY_MARKERS)
        has_structured_dns = any(m in err_upper for m in STRUCTURED_DNS_MARKERS)
        has_deterministic_fail = any(m in err_upper for m in DETERMINISTIC_FAILURE_MARKERS) or has_structured_dns
        has_generic_dns = "DNS" in err_upper and not has_structured_dns

        # 1. Success States (TRANSMITTED, EXPORTED_FILE, COMPLETED)
        if status_upper in ("TRANSMITTED", "EXPORTED_FILE", "COMPLETED"):
            return (
                "EHR_TRANSMISSION",
                "TRANSMITTED",
                "TRANSMITTED",
                "RESOLVED_DELIVERED",
                canonical_dest,
                has_conflict,
                "LEGACY_DELIVERED_SUCCESS",
            )

        # 2. In-Flight / Pre-Transport States (PENDING, PENDING_TRANSPORT)
        if status_upper in ("PENDING", "PENDING_TRANSPORT"):
            return (
                "EHR_TRANSMISSION",
                "PENDING_TRANSPORT",
                "PENDING_TRANSPORT",
                "UNRESOLVED",
                canonical_dest,
                has_conflict,
                "LEGACY_PENDING_TRANSPORT",
            )

        # 3. EHR_TRANSMISSION + GENERATED (Invalid lifecycle combination)
        # In frozen Phase A design: Fails safe to FAILED / HISTORICAL_INDETERMINATE / UNRESOLVED
        if status_upper == "GENERATED":
            return (
                "EHR_TRANSMISSION",
                "FAILED",
                "HISTORICAL_INDETERMINATE",
                "UNRESOLVED",
                canonical_dest,
                True,  # Conflicting evidence: GENERATED on EHR transmission
                "INVALID_EHR_GENERATED_FAILS_SAFE",
            )

        # 4. Failure States (FAILED or unclassified unexpected legacy status)
        # Precedence Rule: Ambiguity Markers ≻ Deterministic Failure Markers
        if has_ambiguity:
            return (
                "EHR_TRANSMISSION",
                "FAILED",
                "UNKNOWN_RECONCILIATION_REQUIRED",
                "UNRESOLVED",
                canonical_dest,
                has_conflict,
                "AMBIGUOUS_TRANSPORT_FAILURE",
            )

        if has_deterministic_fail:
            # Confirmed deterministic pre-connection or transport rejection
            reason_str = "STRUCTURED_DNS_PRECONNECTION_FAILURE" if has_structured_dns else "DETERMINISTIC_TRANSPORT_REJECTION"
            return (
                "EHR_TRANSMISSION",
                "FAILED",
                "CONFIRMED_FAILED",
                "RESOLVED_NON_DELIVERY",
                canonical_dest,
                has_conflict,
                reason_str,
            )

        if has_generic_dns:
            # Generic unstructured DNS mention -> Fails safe to HISTORICAL_INDETERMINATE
            return (
                "EHR_TRANSMISSION",
                "FAILED",
                "HISTORICAL_INDETERMINATE",
                "UNRESOLVED",
                canonical_dest,
                has_conflict,
                "UNSTRUCTURED_DNS_HISTORICAL_INDETERMINATE",
            )

        # Unstructured error, corrupt or unexpected status -> Fails safe to HISTORICAL_INDETERMINATE
        return (
            "EHR_TRANSMISSION",
            "FAILED",
            "HISTORICAL_INDETERMINATE",
            "UNRESOLVED",
            canonical_dest,
            has_conflict or (status_upper not in ("FAILED",)),
            "UNCLASSIFIED_FAILS_SAFE_HISTORICAL_INDETERMINATE",
        )

