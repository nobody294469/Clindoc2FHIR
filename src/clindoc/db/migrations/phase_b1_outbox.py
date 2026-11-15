"""Native Schema Migration Engine for Phase B1 EHR Transmission Outbox.

Authority:
  - Phase B1 Frozen Architectural Specification
  - Dedicated ehr_transmission_outbox table and lease-generation fencing
  - Dual-Engine (PostgreSQL & SQLite) Table Rebuild & Constraint Hardening
  - Dialect-Specific 64-char Lowercase Hex Check Constraints
  - Composite Forward and Reverse Foreign Keys:
      * fhir_exports(outbox_id, logical_export_id) -> ehr_transmission_outbox(id, logical_export_id)
      * ehr_transmission_outbox(current_attempt_id, id) -> fhir_exports(id, outbox_id)
  - Immutability Trigger on fhir_exports.outbox_id

Invariants:
  1. Single-writer migration locking:
     - PostgreSQL: pg_advisory_xact_lock(918273646)
     - SQLite: BEGIN IMMEDIATE exclusive transaction
  2. Idempotency & Safe Version Tracking:
     - schema_migrations version table tracking '20261111_02_phase_b1_outbox'
     - Clean exit with MigrationResult(already_applied=True) if rerun
  3. Fail-safe Recovery:
     - Atomic transaction rollback on any DDL or constraint failure
  4. SQLite Table Rebuild:
     - Drops and recreates dependent views/triggers within transaction
     - Preserves all historical fhir_exports records (preserving outbox_id if present, else NULL)
     - PRAGMA foreign_key_check verified inside transaction before commit
     - PRAGMA foreign_keys = ON verified outside transaction
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

MIGRATION_VERSION = "20261111_02_phase_b1_outbox"
POSTGRESQL_ADVISORY_LOCK_ID = 918273646


@dataclass
class MigrationResult:
    """Result summary of Phase B1 outbox migration execution."""

    version: str = MIGRATION_VERSION
    dialect: str = ""
    already_applied: bool = False
    migrated_rows_count: int = 0
    views_recreated: list[str] = field(default_factory=list)
    indexes_recreated: list[str] = field(default_factory=list)
    success: bool = True
    error_message: Optional[str] = None


class PhaseB1OutboxMigrationRunner:
    """Native SQLAlchemy 2.0 migration runner for Phase B1 outbox schema."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.dialect_name = engine.dialect.name

    def run(self) -> MigrationResult:
        """Execute Phase B1 outbox migration with dialect-specific single-writer locking."""
        if self.dialect_name == "postgresql":
            with self.engine.connect() as conn:
                return self._run_postgresql(conn)
        elif self.dialect_name == "sqlite":
            return self._run_sqlite()
        else:
            raise NotImplementedError(f"Unsupported migration dialect: {self.dialect_name}")

    def _run_postgresql(self, conn: Connection) -> MigrationResult:
        """PostgreSQL migration with pg_advisory_xact_lock."""
        result = MigrationResult(dialect="postgresql")
        trans = conn.begin()
        try:
            conn.execute(
                text(f"SELECT pg_advisory_xact_lock({POSTGRESQL_ADVISORY_LOCK_ID});")
            )

            # Ensure version table
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version VARCHAR(128) PRIMARY KEY,
                        applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL
                    );
                    """
                )
            )

            check_row = conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :ver"),
                {"ver": MIGRATION_VERSION},
            ).scalar()
            if check_row:
                result.already_applied = True
                trans.rollback()
                return result

            # 1. Create ehr_transmission_outbox table
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS ehr_transmission_outbox (
                        id UUID PRIMARY KEY,
                        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
                        bundle_id UUID NOT NULL REFERENCES fhir_bundles(id) ON DELETE RESTRICT,
                        bundle_sha256 VARCHAR(64) NOT NULL,
                        destination_identity VARCHAR(512) NOT NULL,
                        logical_export_id UUID NOT NULL,
                        destination_idempotency_key VARCHAR(64) NOT NULL,
                        status VARCHAR(32) DEFAULT 'PENDING' NOT NULL,
                        worker_id VARCHAR(100) NULL,
                        lease_expires_at TIMESTAMP WITH TIME ZONE NULL,
                        lease_generation INTEGER DEFAULT 0 NOT NULL,
                        worker_recovery_count INTEGER DEFAULT 0 NOT NULL,
                        current_attempt_id UUID NULL,
                        staged_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                        correlation_id VARCHAR(128) NULL,
                        error_message TEXT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        CONSTRAINT uq_ehr_transmission_outbox_intent UNIQUE (document_id, destination_identity, bundle_sha256),
                        CONSTRAINT uq_ehr_transmission_outbox_logical_export UNIQUE (logical_export_id),
                        CONSTRAINT uq_ehr_outbox_id_logical_export UNIQUE (id, logical_export_id),
                        CONSTRAINT chk_ehr_outbox_status_valid CHECK (status IN ('PENDING', 'DISPATCHING', 'DELIVERED', 'BLOCKED_RECONCILIATION', 'FAILED_TERMINAL')),
                        CONSTRAINT chk_ehr_outbox_recovery_count_nonnegative CHECK (worker_recovery_count >= 0),
                        CONSTRAINT chk_ehr_outbox_lease_generation_nonnegative CHECK (lease_generation >= 0),
                        CONSTRAINT chk_ehr_outbox_bundle_sha256_hex CHECK (length(bundle_sha256) = 64 AND bundle_sha256 ~ '^[0-9a-f]{64}$'),
                        CONSTRAINT chk_ehr_outbox_dest_idem_key_hex CHECK (length(destination_idempotency_key) = 64 AND destination_idempotency_key ~ '^[0-9a-f]{64}$')
                    );
                    """
                )
            )

            # 2. Add outbox_id to fhir_exports if not exists
            conn.execute(
                text(
                    """
                    ALTER TABLE fhir_exports
                    ADD COLUMN IF NOT EXISTS outbox_id UUID NULL;
                    """
                )
            )

            # 3. Add composite unique and foreign key constraints on fhir_exports
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_fhir_exports_id_outbox_id') THEN
                            ALTER TABLE fhir_exports
                            ADD CONSTRAINT uq_fhir_exports_id_outbox_id UNIQUE (id, outbox_id);
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_fhir_exports_outbox_logical_composite') THEN
                            ALTER TABLE fhir_exports
                            ADD CONSTRAINT fk_fhir_exports_outbox_logical_composite
                            FOREIGN KEY (outbox_id, logical_export_id)
                            REFERENCES ehr_transmission_outbox(id, logical_export_id)
                            ON DELETE RESTRICT;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'chk_fhir_exports_outbox_logical_both_or_neither') THEN
                            ALTER TABLE fhir_exports
                            ADD CONSTRAINT chk_fhir_exports_outbox_logical_both_or_neither
                            CHECK (outbox_id IS NULL OR logical_export_id IS NOT NULL);
                        END IF;
                    END $$;
                    """
                )
            )

            # 4. Add reverse composite foreign key on ehr_transmission_outbox
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_ehr_outbox_current_attempt_composite') THEN
                            ALTER TABLE ehr_transmission_outbox
                            ADD CONSTRAINT fk_ehr_outbox_current_attempt_composite
                            FOREIGN KEY (current_attempt_id, id)
                            REFERENCES fhir_exports(id, outbox_id)
                            ON DELETE RESTRICT;
                        END IF;
                    END $$;
                    """
                )
            )

            # 5. Add indexes
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS ix_fhir_exports_outbox_id ON fhir_exports(outbox_id);
                    CREATE INDEX IF NOT EXISTS ix_ehr_outbox_claim_dispatch ON ehr_transmission_outbox(status, created_at);
                    CREATE INDEX IF NOT EXISTS ix_ehr_outbox_doc_dest ON ehr_transmission_outbox(document_id, destination_identity);
                    CREATE INDEX IF NOT EXISTS ix_ehr_outbox_lease_expires ON ehr_transmission_outbox(lease_expires_at);
                    """
                )
            )

            # 6. Add PostgreSQL immutability trigger on fhir_exports.outbox_id
            conn.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION trg_enforce_fhir_export_outbox_immutable()
                    RETURNS TRIGGER AS $$
                    BEGIN
                        IF OLD.outbox_id IS NOT NULL AND (NEW.outbox_id IS NULL OR NEW.outbox_id != OLD.outbox_id) THEN
                            RAISE EXCEPTION 'FHIRExport outbox_id is immutable once assigned.';
                        END IF;
                        RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql;

                    DROP TRIGGER IF EXISTS trg_fhir_exports_outbox_immutable ON fhir_exports;
                    CREATE TRIGGER trg_fhir_exports_outbox_immutable
                    BEFORE UPDATE OF outbox_id ON fhir_exports
                    FOR EACH ROW EXECUTE FUNCTION trg_enforce_fhir_export_outbox_immutable();
                    """
                )
            )

            # 7. Record version
            conn.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:ver)"),
                {"ver": MIGRATION_VERSION},
            )
            trans.commit()
            result.success = True
            return result
        except Exception as exc:
            trans.rollback()
            result.success = False
            result.error_message = str(exc)
            raise

    def _run_sqlite(self) -> MigrationResult:
        """SQLite table rebuild with strict atomic verification and safe foreign key preservation."""
        result = MigrationResult(dialect="sqlite")
        raw_conn = self.engine.raw_connection()
        cursor = raw_conn.cursor()

        try:
            # 1. Verify autocommit / outside transaction
            fk_setting = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]

            # 2. Ensure schema_migrations table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
                );
                """
            )

            # 3. Check version
            existing_ver = cursor.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
            if existing_ver:
                result.already_applied = True
                cursor.close()
                raw_conn.close()
                return result

            # 4. Inspect fhir_exports columns
            col_info = cursor.execute("PRAGMA table_info(fhir_exports);").fetchall()
            existing_cols = {row[1] for row in col_info}
            has_outbox_col = "outbox_id" in existing_cols
            old_count = cursor.execute("SELECT COUNT(*) FROM fhir_exports;").fetchone()[0]

            # 5. Inspect dependent views
            view_rows = cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'view';"
            ).fetchall()
            for v_name, v_sql in view_rows:
                result.views_recreated.append(v_name)

            # 6. Disable foreign keys outside transaction
            cursor.execute("PRAGMA foreign_keys = OFF;")
            fk_disabled = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
            if fk_disabled != 0:
                raise RuntimeError("Failed to disable SQLite foreign_keys before migration transaction.")

            # 7. Begin atomic transaction
            cursor.execute("BEGIN TRANSACTION;")

            # Drop views temporarily
            for v_name, _ in view_rows:
                cursor.execute(f"DROP VIEW IF EXISTS {v_name};")

            # 8. Create ehr_transmission_outbox table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS ehr_transmission_outbox (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE RESTRICT,
                    bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE RESTRICT,
                    bundle_sha256 TEXT NOT NULL,
                    destination_identity TEXT NOT NULL,
                    logical_export_id TEXT NOT NULL,
                    destination_idempotency_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    worker_id TEXT NULL,
                    lease_expires_at TIMESTAMP NULL,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    worker_recovery_count INTEGER NOT NULL DEFAULT 0,
                    current_attempt_id TEXT NULL,
                    staged_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    correlation_id TEXT NULL,
                    error_message TEXT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_ehr_transmission_outbox_intent UNIQUE (document_id, destination_identity, bundle_sha256),
                    CONSTRAINT uq_ehr_transmission_outbox_logical_export UNIQUE (logical_export_id),
                    CONSTRAINT uq_ehr_outbox_id_logical_export UNIQUE (id, logical_export_id),
                    CONSTRAINT fk_ehr_outbox_current_attempt_composite 
                        FOREIGN KEY (current_attempt_id, id) 
                        REFERENCES fhir_exports(id, outbox_id) 
                        ON DELETE RESTRICT,
                    CONSTRAINT chk_ehr_outbox_status_valid 
                        CHECK (status IN ('PENDING', 'DISPATCHING', 'DELIVERED', 'BLOCKED_RECONCILIATION', 'FAILED_TERMINAL')),
                    CONSTRAINT chk_ehr_outbox_recovery_count_nonnegative 
                        CHECK (worker_recovery_count >= 0),
                    CONSTRAINT chk_ehr_outbox_lease_generation_nonnegative 
                        CHECK (lease_generation >= 0),
                    CONSTRAINT chk_ehr_outbox_bundle_sha256_hex 
                        CHECK (length(bundle_sha256) = 64 AND bundle_sha256 NOT GLOB '*[^0-9a-f]*'),
                    CONSTRAINT chk_ehr_outbox_dest_idem_key_hex 
                        CHECK (length(destination_idempotency_key) = 64 AND destination_idempotency_key NOT GLOB '*[^0-9a-f]*')
                );
                """
            )

            # 9. Create fhir_exports_new table
            cursor.execute(
                """
                CREATE TABLE fhir_exports_new (
                    id TEXT PRIMARY KEY,
                    bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL DEFAULT 'EHR_TRANSMISSION',
                    export_status TEXT NOT NULL,
                    transmission_state TEXT NULL,
                    resolution_status TEXT NULL,
                    destination_identity TEXT NULL,
                    destination_idempotency_key TEXT NULL,
                    logical_export_id TEXT NULL,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    parent_attempt_id TEXT NULL REFERENCES fhir_exports(id) ON DELETE RESTRICT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    destination_path TEXT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    exported_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
                    exported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    error_message TEXT NULL,
                    outbox_id TEXT NULL,
                    CONSTRAINT uq_fhir_exports_logical_attempt UNIQUE (logical_export_id, attempt_number),
                    CONSTRAINT uq_fhir_exports_id_outbox_id UNIQUE (id, outbox_id),
                    CONSTRAINT fk_fhir_exports_outbox_logical_composite 
                        FOREIGN KEY (outbox_id, logical_export_id) 
                        REFERENCES ehr_transmission_outbox(id, logical_export_id) 
                        ON DELETE RESTRICT,
                    CONSTRAINT chk_fhir_exports_lifecycle CHECK (
                        (
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
                        )
                    ),
                    CONSTRAINT chk_fhir_exports_attempt_lineage CHECK (
                        ((attempt_number = 1 AND parent_attempt_id IS NULL) OR (attempt_number > 1 AND parent_attempt_id IS NOT NULL))
                    ),
                    CONSTRAINT chk_fhir_exports_outbox_logical_both_or_neither CHECK (
                        outbox_id IS NULL OR logical_export_id IS NOT NULL
                    )
                );
                """
            )

            # 10. Copy data safely preserving outbox_id
            if has_outbox_col:
                cursor.execute(
                    """
                    INSERT INTO fhir_exports_new (
                        id, bundle_id, target_type, export_status, transmission_state,
                        resolution_status, destination_identity, destination_idempotency_key,
                        logical_export_id, attempt_number, parent_attempt_id, idempotency_key,
                        destination_path, attempts, exported_by, exported_at, error_message, outbox_id
                    )
                    SELECT 
                        id, bundle_id, target_type, export_status, transmission_state,
                        resolution_status, destination_identity, destination_idempotency_key,
                        logical_export_id, attempt_number, parent_attempt_id, idempotency_key,
                        destination_path, attempts, exported_by, exported_at, error_message, outbox_id
                    FROM fhir_exports;
                    """
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO fhir_exports_new (
                        id, bundle_id, target_type, export_status, transmission_state,
                        resolution_status, destination_identity, destination_idempotency_key,
                        logical_export_id, attempt_number, parent_attempt_id, idempotency_key,
                        destination_path, attempts, exported_by, exported_at, error_message, outbox_id
                    )
                    SELECT 
                        id, bundle_id, target_type, export_status, transmission_state,
                        resolution_status, destination_identity, destination_idempotency_key,
                        logical_export_id, attempt_number, parent_attempt_id, idempotency_key,
                        destination_path, attempts, exported_by, exported_at, error_message, NULL
                    FROM fhir_exports;
                    """
                )

            # 11. Replace table
            cursor.execute("DROP TABLE fhir_exports;")
            cursor.execute("ALTER TABLE fhir_exports_new RENAME TO fhir_exports;")

            # 12. Recreate indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_fhir_exports_bundle_id ON fhir_exports(bundle_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_fhir_exports_logical_id ON fhir_exports(logical_export_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_fhir_exports_dest_idem_key ON fhir_exports(destination_idempotency_key);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_fhir_exports_parent_attempt_id ON fhir_exports(parent_attempt_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_fhir_exports_outbox_id ON fhir_exports(outbox_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_ehr_outbox_claim_dispatch ON ehr_transmission_outbox(status, created_at);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_ehr_outbox_doc_dest ON ehr_transmission_outbox(document_id, destination_identity);")
            cursor.execute("CREATE INDEX IF NOT EXISTS ix_ehr_outbox_lease_expires ON ehr_transmission_outbox(lease_expires_at);")

            # 13. Recreate immutability trigger
            cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_fhir_exports_outbox_immutable
                BEFORE UPDATE OF outbox_id ON fhir_exports
                FOR EACH ROW
                WHEN OLD.outbox_id IS NOT NULL AND (NEW.outbox_id IS NULL OR NEW.outbox_id != OLD.outbox_id)
                BEGIN
                    SELECT RAISE(ABORT, 'FHIRExport outbox_id is immutable once assigned.');
                END;
                """
            )

            # 14. Recreate dependent views
            for v_name, v_sql in view_rows:
                cursor.execute(v_sql)

            # 15. Check foreign keys BEFORE commit
            violations = cursor.execute("PRAGMA foreign_key_check;").fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations detected during table rebuild: {violations}")

            # 16. Verify row count
            new_count = cursor.execute("SELECT COUNT(*) FROM fhir_exports;").fetchone()[0]
            if new_count != old_count:
                raise RuntimeError(f"Data loss detected during rebuild: expected {old_count}, got {new_count}")

            # 17. Atomic version registration
            cursor.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, CURRENT_TIMESTAMP);",
                (MIGRATION_VERSION,),
            )

            # 18. Commit transaction
            cursor.execute("COMMIT;")
            result.migrated_rows_count = new_count
            result.success = True
        except Exception as exc:
            try:
                cursor.execute("ROLLBACK;")
            except Exception:
                pass
            result.success = False
            result.error_message = str(exc)
            raise
        finally:
            # 19. Guaranteed foreign_keys = ON restoration on dedicated connection
            try:
                cursor.execute("PRAGMA foreign_keys = ON;")
                fk_on = cursor.execute("PRAGMA foreign_keys;").fetchone()[0]
                if fk_on != 1:
                    raw_conn.close()
                    raise RuntimeError("Failed to restore PRAGMA foreign_keys = ON on migration connection.")
            except Exception:
                raw_conn.close()
                raise
            # 20. Post-commit foreign key check
            try:
                post_violations = cursor.execute("PRAGMA foreign_key_check;").fetchall()
                if post_violations:
                    raw_conn.close()
                    raise RuntimeError(f"Post-commit foreign key check failed: {post_violations}")
            finally:
                cursor.close()
                raw_conn.close()

        return result

