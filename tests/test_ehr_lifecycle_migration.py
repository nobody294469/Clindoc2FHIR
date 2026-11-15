"""Comprehensive Migration and Dual-Engine Verification Test Suite for Phase A Lifecycle.

Authority:
  - Phase A Frozen Architectural Specification
  - Native SQLAlchemy 2.0 Migration Engine (src/clindoc/db/migrations/phase_a_lifecycle.py)
  - Dual-Engine (SQLite & PostgreSQL) Parity
  - chk_fhir_exports_lifecycle & chk_fhir_exports_attempt_lineage Enforcement
  - fhir_export_migration_audit Accuracy
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Generator
import uuid
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from clindoc.db.base import Base
from clindoc.db.enums import (
    ClaimReviewState,
    DocumentApprovalStatus,
    DocumentStatus,
    EHRResolutionStatus,
    EHRTransmissionState,
    FHIRExportStatus,
    FHIRVersion,
    PatientIdentityApprovalStatus,
    VerificationLabel,
    VerificationStage,
)
from clindoc.db.migrations.phase_a_lifecycle import (
    MIGRATION_VERSION,
    POSTGRESQL_ADVISORY_LOCK_ID,
    PhaseALifecycleMigrationRunner,
)
from clindoc.db.models import (
    Claim as DBClaim,
    Document as DBDocument,
    DocumentApproval,
    EHRTransmissionReconciliation,
    Evidence as DBEvidence,
    FHIRBundle,
    FHIRExport,
    FHIRExportMigrationAudit,
    User,
    VerificationResult as DBVerificationResult,
)


# ─── POSTGRESQL EPHEMERAL FIXTURE ─────────────────────────────────────────────

def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def pg_engine_url() -> Generator[str, None, None]:
    """Start an ephemeral PostgreSQL 18 instance if binaries exist; otherwise skip."""
    initdb_path = r"D:\postgresql\bin\initdb.exe"
    postgres_path = r"D:\postgresql\bin\postgres.exe"

    if not os.path.exists(initdb_path) or not os.path.exists(postgres_path):
        pytest.skip("PostgreSQL binaries not found at D:\\postgresql\\bin")

    td = tempfile.mkdtemp(prefix="pg_phase_a_migration_")
    port = _get_free_port()
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"

    try:
        subprocess.run(
            [initdb_path, "-D", td, "-U", "postgres", "-A", "trust", "--no-locale", "-E", "UTF8"],
            check=True,
            capture_output=True,
        )
        proc = subprocess.Popen(
            [postgres_path, "-D", td, "-p", str(port), "-c", "deadlock_timeout=50ms", "-c", "max_connections=50"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        import psycopg
        ready = False
        for _ in range(60):
            try:
                with psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres", connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                ready = True
                break
            except Exception:
                time.sleep(0.1)

        if not ready:
            proc.terminate()
            raise RuntimeError(f"Ephemeral PostgreSQL failed to start on port {port}.")

        yield url

        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        shutil.rmtree(td, ignore_errors=True)


# ─── LEGACY DATABASE SEEDING HELPERS ──────────────────────────────────────────

LEGACY_TEST_ROWS = [
    # 1. FILE_DOWNLOAD with GENERATED (UNCHANGED)
    ("exp_file_gen", "b1", "FILE_DOWNLOAD", "GENERATED", "export-file-01", "blobs/export.json", None),
    # 2. FILE_DOWNLOAD with PENDING (UNCHANGED)
    ("exp_file_pend", "b1", "FILE_DOWNLOAD", "PENDING", "export-file-02", None, None),
    # 3. FILE_DOWNLOAD with FAILED (UNCHANGED)
    ("exp_file_fail", "b1", "FILE_DOWNLOAD", "FAILED", "export-file-03", None, "Disk write error"),
    # 4. EHR_TRANSMISSION with TRANSMITTED
    ("exp_ehr_tx", "b1", "EHR_TRANSMISSION", "TRANSMITTED", "ehr-tx-01", "ehr://epic/r4", None),
    # 5. EHR_TRANSMISSION with EXPORTED_FILE (Legacy status mapping to TRANSMITTED)
    ("exp_ehr_expfile", "b1", "EHR_TRANSMISSION", "EXPORTED_FILE", "ehr-expfile-01", "ehr://cerner/r4", None),
    # 6. EHR_TRANSMISSION with COMPLETED (Legacy status mapping to TRANSMITTED)
    ("exp_ehr_comp", "b1", "EHR_TRANSMISSION", "COMPLETED", "ehr-comp-01", "https://ehr.hospital.org/r4", None),
    # 7. EHR_TRANSMISSION with PENDING (maps to PENDING_TRANSPORT)
    ("exp_ehr_pend", "b1", "EHR_TRANSMISSION", "PENDING", "ehr-pend-01", "https://ehr.hospital.org/r4", None),
    # 8. EHR_TRANSMISSION with FAILED + Ambiguous Timeout Marker -> UNKNOWN_RECONCILIATION_REQUIRED
    ("exp_ehr_timeout", "b1", "EHR_TRANSMISSION", "FAILED", "ehr-to-01", "https://ehr.hospital.org/r4", "HTTP 504 Gateway Timeout"),
    # 9. EHR_TRANSMISSION with FAILED + Structured Pre-Connection DNS -> CONFIRMED_FAILED / RESOLVED_NON_DELIVERY
    ("exp_ehr_dns_struct", "b1", "EHR_TRANSMISSION", "FAILED", "ehr-dns-01", "https://ehr.hospital.org/r4", "Connection failed: NXDOMAIN host not found"),
    # 10. EHR_TRANSMISSION with FAILED + Generic Unstructured DNS -> HISTORICAL_INDETERMINATE / UNRESOLVED
    ("exp_ehr_dns_unstruct", "b1", "EHR_TRANSMISSION", "FAILED", "ehr-dns-02", "https://ehr.hospital.org/r4", "Generic DNS error occurred"),
    # 11. EHR_TRANSMISSION with FAILED + Connection Refused -> CONFIRMED_FAILED / RESOLVED_NON_DELIVERY
    ("exp_ehr_refused", "b1", "EHR_TRANSMISSION", "FAILED", "ehr-ref-01", "https://ehr.hospital.org/r4", "ConnectionRefusedError: port 443 closed"),
    # 12. EHR_TRANSMISSION with GENERATED (Invalid legacy combination -> Fails safe to FAILED / HISTORICAL_INDETERMINATE)
    ("exp_ehr_gen_invalid", "b1", "EHR_TRANSMISSION", "GENERATED", "ehr-gen-01", "https://ehr.hospital.org/r4", None),
    # 13. EHR_TRANSMISSION with Corrupt / Unexpected Legacy Status -> Fails safe to FAILED / HISTORICAL_INDETERMINATE
    ("exp_ehr_corrupt", "b1", "EHR_TRANSMISSION", "CORRUPT_STATUS_XYZ", "ehr-corrupt-01", "https://ehr.hospital.org/r4", "Unknown failure code"),
]


def _create_legacy_sqlite_database(db_path: str) -> None:
    """Create a representative pre-Phase A legacy SQLite database with old schema, views, and triggers."""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        conn.execute(text("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            idp_subject TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))
        conn.execute(text("""
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            uploaded_by TEXT NOT NULL REFERENCES users(id),
            source_type TEXT NOT NULL,
            status TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            storage_ref TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            page_count INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))
        conn.execute(text("""
        CREATE TABLE fhir_bundles (
            id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL REFERENCES documents(id),
            approval_id TEXT,
            fhir_version TEXT NOT NULL,
            provenance_minimized BOOLEAN NOT NULL DEFAULT 0,
            bundle_sha256 TEXT NOT NULL,
            storage_ref TEXT NOT NULL,
            bundle_payload TEXT NOT NULL,
            validation_report TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """))
        # Old legacy fhir_exports table (without transmission_state, resolution_status, destination_identity, etc.)
        conn.execute(text("""
        CREATE TABLE fhir_exports (
            id TEXT PRIMARY KEY,
            bundle_id TEXT NOT NULL REFERENCES fhir_bundles(id) ON DELETE CASCADE,
            target_type TEXT NOT NULL DEFAULT 'FILE_DOWNLOAD',
            export_status TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE,
            destination_path TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            exported_by TEXT NOT NULL REFERENCES users(id),
            exported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT
        );
        """))
        conn.execute(text("CREATE INDEX ix_fhir_exports_bundle_id ON fhir_exports(bundle_id);"))

        # Create dependent legacy view: v_active_exports
        conn.execute(text("""
        CREATE VIEW v_active_exports AS 
        SELECT id, bundle_id, target_type, export_status, destination_path 
        FROM fhir_exports;
        """))

        # Create dependent legacy audit trigger & log table
        conn.execute(text("""
        CREATE TABLE fhir_export_audit_trigger_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id TEXT NOT NULL,
            action TEXT NOT NULL
        );
        """))
        conn.execute(text("""
        CREATE TRIGGER trg_fhir_exports_audit_insert
        AFTER INSERT ON fhir_exports
        BEGIN
            INSERT INTO fhir_export_audit_trigger_log (export_id, action)
            VALUES (NEW.id, 'INSERT');
        END;
        """))

        # Seed users and documents
        conn.execute(text("""
        INSERT INTO users (id, idp_subject, username, full_name, email)
        VALUES ('u1', 'sub-1', 'dr_smith', 'Dr. Smith', 'smith@hospital.org');
        """))
        conn.execute(text("""
        INSERT INTO documents (id, uploaded_by, source_type, status, original_filename, storage_ref, mime_type, file_size_bytes, sha256, page_count)
        VALUES ('d1', 'u1', 'scanned_pdf', 'APPROVED', 'test.pdf', 'ref/1', 'application/pdf', 1000, 'sha1', 1);
        """))
        conn.execute(text("""
        INSERT INTO fhir_bundles (id, document_id, fhir_version, bundle_sha256, storage_ref, bundle_payload, validation_report)
        VALUES ('b1', 'd1', 'R4B', 'sha256b1', 'ref/b1', '{}', '{"is_valid": true}');
        """))

        for r_id, b_id, target, status, idem, dest, err in LEGACY_TEST_ROWS:
            conn.execute(
                text("""
                INSERT INTO fhir_exports (id, bundle_id, target_type, export_status, idempotency_key, destination_path, exported_by, error_message)
                VALUES (:id, :b_id, :target, :status, :idem, :dest, 'u1', :err);
                """),
                {"id": r_id, "b_id": b_id, "target": target, "status": status, "idem": idem, "dest": dest, "err": err},
            )
        conn.commit()


def _create_legacy_postgresql_database(engine) -> None:
    """Create a representative pre-Phase A legacy PostgreSQL database with old schema and rows."""
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS fhir_export_migration_audit CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS ehr_transmission_reconciliations CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS fhir_exports CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS fhir_bundles CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS documents CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS users CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS schema_migrations CASCADE;"))

        conn.execute(text("""
        CREATE TABLE users (
            id UUID PRIMARY KEY,
            idp_subject VARCHAR(256) UNIQUE NOT NULL,
            username VARCHAR(64) UNIQUE NOT NULL,
            full_name VARCHAR(128) NOT NULL,
            email VARCHAR(256) UNIQUE NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """))
        conn.execute(text("""
        CREATE TABLE documents (
            id UUID PRIMARY KEY,
            uploaded_by UUID NOT NULL REFERENCES users(id),
            source_type VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            original_filename VARCHAR(256) NOT NULL,
            storage_ref VARCHAR(512) NOT NULL,
            mime_type VARCHAR(64) NOT NULL,
            file_size_bytes INTEGER NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            page_count INTEGER NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """))
        conn.execute(text("""
        CREATE TABLE fhir_bundles (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL REFERENCES documents(id),
            approval_id UUID,
            fhir_version VARCHAR(16) NOT NULL,
            provenance_minimized BOOLEAN NOT NULL DEFAULT FALSE,
            bundle_sha256 VARCHAR(64) NOT NULL,
            storage_ref VARCHAR(512) NOT NULL,
            bundle_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
            validation_report JSONB NOT NULL DEFAULT '{"is_valid": true}'::jsonb,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        """))
        # Old legacy fhir_exports table (without transmission_state, resolution_status, etc.)
        conn.execute(text("""
        CREATE TABLE fhir_exports (
            id UUID PRIMARY KEY,
            bundle_id UUID NOT NULL REFERENCES fhir_bundles(id) ON DELETE CASCADE,
            target_type VARCHAR(32) NOT NULL DEFAULT 'FILE_DOWNLOAD',
            export_status VARCHAR(32) NOT NULL,
            idempotency_key VARCHAR(128) NOT NULL UNIQUE,
            destination_path VARCHAR(1024),
            attempts INTEGER NOT NULL DEFAULT 1,
            exported_by UUID NOT NULL REFERENCES users(id),
            exported_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT
        );
        """))
        conn.execute(text("CREATE INDEX ix_fhir_exports_bundle_id ON fhir_exports(bundle_id);"))

        u1_id = str(uuid.uuid4())
        d1_id = str(uuid.uuid4())
        b1_id = str(uuid.uuid4())

        conn.execute(text("""
        INSERT INTO users (id, idp_subject, username, full_name, email)
        VALUES (:id, 'sub-pg-1', 'dr_pg', 'Dr. Postgres', 'pg@hospital.org');
        """), {"id": u1_id})

        conn.execute(text("""
        INSERT INTO documents (id, uploaded_by, source_type, status, original_filename, storage_ref, mime_type, file_size_bytes, sha256, page_count)
        VALUES (:id, :u, 'scanned_pdf', 'APPROVED', 'test.pdf', 'ref/1', 'application/pdf', 1000, 'sha1', 1);
        """), {"id": d1_id, "u": u1_id})

        conn.execute(text("""
        INSERT INTO fhir_bundles (id, document_id, fhir_version, bundle_sha256, storage_ref)
        VALUES (:id, :d, 'R4B', 'sha256pg', 'ref/pg');
        """), {"id": b1_id, "d": d1_id})

        # Insert same representative legacy test rows using UUIDs
        for r_id_name, _, target, status, idem, dest, err in LEGACY_TEST_ROWS:
            conn.execute(
                text("""
                INSERT INTO fhir_exports (id, bundle_id, target_type, export_status, idempotency_key, destination_path, exported_by, error_message)
                VALUES (:id, :b, :target, :status, :idem, :dest, :u, :err);
                """),
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, r_id_name)),
                    "b": b1_id,
                    "target": target,
                    "status": status,
                    "idem": idem,
                    "dest": dest,
                    "u": u1_id,
                    "err": err,
                },
            )
        conn.commit()


# ─── SQLITE MIGRATION TESTS ──────────────────────────────────────────────────

def test_sqlite_fresh_database_migration():
    """Verify PhaseALifecycleMigrationRunner on a fresh database with no existing tables."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    runner = PhaseALifecycleMigrationRunner(engine)
    res = runner.run()

    assert res.success is True
    assert res.dialect == "sqlite"
    assert res.already_applied is False

    with engine.connect() as conn:
        v = conn.execute(text("SELECT version FROM schema_migrations")).scalar_one()
        assert v == MIGRATION_VERSION

        cols = [r[1] for r in conn.execute(text("PRAGMA table_info('fhir_exports')")).fetchall()]
        assert "transmission_state" in cols
        assert "resolution_status" in cols
        assert "destination_identity" in cols
        assert "parent_attempt_id" in cols


def test_sqlite_legacy_upgrade_and_view_preservation(tmp_path):
    """Verify PhaseALifecycleMigrationRunner upgrading a representative legacy SQLite database."""
    db_file = str(tmp_path / "legacy_test.db")
    _create_legacy_sqlite_database(db_file)

    engine = create_engine(f"sqlite:///{db_file}")
    runner = PhaseALifecycleMigrationRunner(engine)
    res = runner.run()

    assert res.success is True
    assert res.already_applied is False
    assert res.migrated_rows_count == 10  # Exactly 10 rows changed; 3 FILE_DOWNLOAD unchanged
    assert res.audit_records_count == 10   # Exactly 10 audit records written
    assert "v_active_exports" in res.views_recreated

    with engine.connect() as conn:
        # 1. Verify v_active_exports view is preserved and queryable
        view_rows = conn.execute(text("SELECT COUNT(*) FROM v_active_exports")).scalar_one()
        assert view_rows == 13

        # 2. Check each classified row against chk_fhir_exports_lifecycle
        rows = {
            r.id: r
            for r in conn.execute(
                text("""
                SELECT id, target_type, export_status, transmission_state, resolution_status, destination_identity 
                FROM fhir_exports
                """)
            ).fetchall()
        }

        # FILE_DOWNLOAD checks (unchanged)
        assert rows["exp_file_gen"].target_type == "FILE_DOWNLOAD"
        assert rows["exp_file_gen"].export_status == "GENERATED"
        assert rows["exp_file_gen"].transmission_state is None
        assert rows["exp_file_gen"].resolution_status is None

        assert rows["exp_file_pend"].target_type == "FILE_DOWNLOAD"
        assert rows["exp_file_pend"].export_status == "PENDING"

        assert rows["exp_file_fail"].target_type == "FILE_DOWNLOAD"
        assert rows["exp_file_fail"].export_status == "FAILED"

        # EHR Success: TRANSMITTED, EXPORTED_FILE, COMPLETED
        for tx_id in ["exp_ehr_tx", "exp_ehr_expfile", "exp_ehr_comp"]:
            assert rows[tx_id].target_type == "EHR_TRANSMISSION"
            assert rows[tx_id].export_status == "TRANSMITTED"
            assert rows[tx_id].transmission_state == "TRANSMITTED"
            assert rows[tx_id].resolution_status == "RESOLVED_DELIVERED"
            assert rows[tx_id].destination_identity is not None

        # EHR Pending: PENDING -> PENDING_TRANSPORT
        assert rows["exp_ehr_pend"].export_status == "PENDING_TRANSPORT"
        assert rows["exp_ehr_pend"].transmission_state == "PENDING_TRANSPORT"
        assert rows["exp_ehr_pend"].resolution_status == "UNRESOLVED"

        # EHR Ambiguous Timeout: FAILED / UNKNOWN_RECONCILIATION_REQUIRED / UNRESOLVED
        assert rows["exp_ehr_timeout"].export_status == "FAILED"
        assert rows["exp_ehr_timeout"].transmission_state == "UNKNOWN_RECONCILIATION_REQUIRED"
        assert rows["exp_ehr_timeout"].resolution_status == "UNRESOLVED"

        # EHR Structured Pre-connection DNS (NXDOMAIN): CONFIRMED_FAILED / RESOLVED_NON_DELIVERY
        assert rows["exp_ehr_dns_struct"].export_status == "FAILED"
        assert rows["exp_ehr_dns_struct"].transmission_state == "CONFIRMED_FAILED"
        assert rows["exp_ehr_dns_struct"].resolution_status == "RESOLVED_NON_DELIVERY"

        # EHR Unstructured generic DNS: HISTORICAL_INDETERMINATE / UNRESOLVED
        assert rows["exp_ehr_dns_unstruct"].export_status == "FAILED"
        assert rows["exp_ehr_dns_unstruct"].transmission_state == "HISTORICAL_INDETERMINATE"
        assert rows["exp_ehr_dns_unstruct"].resolution_status == "UNRESOLVED"

        # EHR Connection Refused: CONFIRMED_FAILED / RESOLVED_NON_DELIVERY
        assert rows["exp_ehr_refused"].export_status == "FAILED"
        assert rows["exp_ehr_refused"].transmission_state == "CONFIRMED_FAILED"
        assert rows["exp_ehr_refused"].resolution_status == "RESOLVED_NON_DELIVERY"

        # EHR Invalid GENERATED: Fails safe to FAILED / HISTORICAL_INDETERMINATE / UNRESOLVED
        assert rows["exp_ehr_gen_invalid"].export_status == "FAILED"
        assert rows["exp_ehr_gen_invalid"].transmission_state == "HISTORICAL_INDETERMINATE"
        assert rows["exp_ehr_gen_invalid"].resolution_status == "UNRESOLVED"

        # EHR Corrupt unexpected: Fails safe to FAILED / HISTORICAL_INDETERMINATE / UNRESOLVED
        assert rows["exp_ehr_corrupt"].export_status == "FAILED"
        assert rows["exp_ehr_corrupt"].transmission_state == "HISTORICAL_INDETERMINATE"
        assert rows["exp_ehr_corrupt"].resolution_status == "UNRESOLVED"

        # 3. Check migration audit entries: exactly 10 rows audited
        audits = {
            r.export_id: r
            for r in conn.execute(
                text("""
                SELECT export_id, old_target_type, new_target_type,
                       old_export_status, new_export_status,
                       old_transmission_state, new_transmission_state,
                       old_resolution_status, new_resolution_status,
                       has_conflicting_evidence, migration_reason
                FROM fhir_export_migration_audit
                """)
            ).fetchall()
        }
        assert len(audits) == 10

        # Unchanged FILE_DOWNLOAD records MUST NOT be audited
        assert "exp_file_gen" not in audits
        assert "exp_file_pend" not in audits
        assert "exp_file_fail" not in audits

        # Verify before/after accuracy of representative audited records
        assert audits["exp_ehr_tx"].old_export_status == "TRANSMITTED"
        assert audits["exp_ehr_tx"].new_export_status == "TRANSMITTED"
        assert audits["exp_ehr_tx"].old_transmission_state is None
        assert audits["exp_ehr_tx"].new_transmission_state == "TRANSMITTED"
        assert audits["exp_ehr_tx"].new_resolution_status == "RESOLVED_DELIVERED"
        assert bool(audits["exp_ehr_tx"].has_conflicting_evidence) is False

        assert audits["exp_ehr_comp"].old_export_status == "COMPLETED"
        assert audits["exp_ehr_comp"].new_export_status == "TRANSMITTED"
        assert audits["exp_ehr_comp"].new_transmission_state == "TRANSMITTED"

        assert audits["exp_ehr_dns_struct"].migration_reason == "STRUCTURED_DNS_PRECONNECTION_FAILURE"
        assert audits["exp_ehr_dns_struct"].new_transmission_state == "CONFIRMED_FAILED"
        assert audits["exp_ehr_dns_struct"].new_resolution_status == "RESOLVED_NON_DELIVERY"

        assert audits["exp_ehr_dns_unstruct"].migration_reason == "UNSTRUCTURED_DNS_HISTORICAL_INDETERMINATE"
        assert audits["exp_ehr_dns_unstruct"].new_transmission_state == "HISTORICAL_INDETERMINATE"
        assert audits["exp_ehr_dns_unstruct"].new_resolution_status == "UNRESOLVED"

        assert bool(audits["exp_ehr_gen_invalid"].has_conflicting_evidence) is True
        assert audits["exp_ehr_gen_invalid"].old_export_status == "GENERATED"
        assert audits["exp_ehr_gen_invalid"].new_export_status == "FAILED"

        # 4. Check trigger preservation: verify trigger exists in sqlite_master and fires on insert
        trg_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_fhir_exports_audit_insert'")
        ).scalar_one_or_none()
        assert trg_exists == "trg_fhir_exports_audit_insert"

        # Insert a new test export row to prove trigger execution after rebuild
        conn.execute(
            text("""
            INSERT INTO fhir_exports (
                id, bundle_id, target_type, export_status, idempotency_key, exported_by
            ) VALUES (
                'exp_trg_test', 'b1', 'FILE_DOWNLOAD', 'GENERATED', 'trg-test-key-01', 'u1'
            )
            """)
        )
        conn.commit()

        trg_log_count = conn.execute(
            text("SELECT COUNT(*) FROM fhir_export_audit_trigger_log WHERE export_id = 'exp_trg_test'")
        ).scalar_one()
        assert trg_log_count == 1

        # 5. Check PRAGMA foreign_key_check: Zero violations
        fk_violations = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
        assert len(fk_violations) == 0


def test_sqlite_migration_idempotency(tmp_path):
    """Verify that running PhaseALifecycleMigrationRunner twice on the same database is a safe no-op."""
    db_file = str(tmp_path / "idempotent_test.db")
    _create_legacy_sqlite_database(db_file)

    engine = create_engine(f"sqlite:///{db_file}")
    runner = PhaseALifecycleMigrationRunner(engine)

    # First run
    res1 = runner.run()
    assert res1.success is True
    assert res1.already_applied is False
    assert res1.migrated_rows_count == 10
    assert res1.audit_records_count == 10

    # Second run
    res2 = runner.run()
    assert res2.success is True
    assert res2.already_applied is True
    assert res2.migrated_rows_count == 0
    assert res2.audit_records_count == 0


def test_sqlite_migration_failure_rollback(tmp_path, monkeypatch):
    """Verify that a failure during SQLite migration triggers an atomic rollback, leaving database recoverable."""
    db_file = str(tmp_path / "rollback_test.db")
    _create_legacy_sqlite_database(db_file)

    engine = create_engine(f"sqlite:///{db_file}")
    runner = PhaseALifecycleMigrationRunner(engine)

    # Force failure during classification of row 5
    original_classify = runner._classify_row
    call_count = 0

    def mock_failing_classify(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 5:
            raise RuntimeError("Simulated mid-migration failure during data normalization")
        return original_classify(*args, **kwargs)

    monkeypatch.setattr(runner, "_classify_row", mock_failing_classify)

    with pytest.raises(RuntimeError, match="Simulated mid-migration failure"):
        runner.run()

    # Verify atomic rollback: database is untouched and schema_migrations is unpopulated
    with engine.connect() as conn:
        tbl_info = conn.execute(text("PRAGMA table_info('fhir_exports')")).fetchall()
        col_names = [r[1] for r in tbl_info]
        assert "transmission_state" not in col_names  # Table was not migrated
        assert conn.execute(text("SELECT COUNT(*) FROM fhir_exports")).scalar_one() == 13

        v_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        ).scalar_one_or_none()
        if v_exists:
            count_v = conn.execute(text("SELECT COUNT(*) FROM schema_migrations WHERE version = :v"), {"v": MIGRATION_VERSION}).scalar_one()
            assert count_v == 0


def test_sqlite_concurrent_migration_single_writer_protection(tmp_path):
    """Verify single-writer protection: concurrent write lock blocks secondary migration."""
    from sqlalchemy.exc import OperationalError

    db_file = str(tmp_path / "concurrent_test.db")
    _create_legacy_sqlite_database(db_file)

    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 0.1})
    runner = PhaseALifecycleMigrationRunner(engine)

    # Acquire exclusive write lock on connection 1
    with engine.connect() as lock_conn:
        lock_conn.execute(text("BEGIN IMMEDIATE"))

        # Connection 2 attempts migration while connection 1 holds lock
        with pytest.raises(OperationalError, match="database is locked"):
            runner.run()

        lock_conn.execute(text("ROLLBACK"))

    # After releasing lock, migration succeeds cleanly
    res = runner.run()
    assert res.success is True
    assert res.migrated_rows_count == 10


# ─── POSTGRESQL MIGRATION TESTS ──────────────────────────────────────────────

def test_postgresql_legacy_migration_and_parity(pg_engine_url: str):
    """Verify PhaseALifecycleMigrationRunner on real PostgreSQL 18 with legacy database upgrade and audit parity."""
    engine = create_engine(pg_engine_url)
    _create_legacy_postgresql_database(engine)

    runner = PhaseALifecycleMigrationRunner(engine)
    res = runner.run()

    assert res.success is True
    assert res.dialect == "postgresql"
    assert res.already_applied is False
    assert res.migrated_rows_count == 10  # 10 rows changed
    assert res.audit_records_count == 10   # Exactly 10 audit records written

    with engine.connect() as conn:
        # Verify schema_migrations
        ver = conn.execute(
            text("SELECT version FROM schema_migrations WHERE version = :v"),
            {"v": MIGRATION_VERSION},
        ).scalar_one_or_none()
        assert ver == MIGRATION_VERSION

        # Verify PostgreSQL check constraints exist
        constraints = conn.execute(
            text("""
            SELECT conname FROM pg_constraint 
            WHERE conname IN ('chk_fhir_exports_lifecycle', 'chk_fhir_exports_attempt_lineage', 'uq_fhir_exports_logical_attempt')
            """)
        ).scalars().all()
        assert "chk_fhir_exports_lifecycle" in constraints
        assert "chk_fhir_exports_attempt_lineage" in constraints
        assert "uq_fhir_exports_logical_attempt" in constraints

        # Verify audit records in PostgreSQL
        audit_rows = {
            str(r.export_id): r
            for r in conn.execute(
                text("""
                SELECT export_id, old_target_type, new_target_type,
                       old_export_status, new_export_status,
                       old_transmission_state, new_transmission_state,
                       old_resolution_status, new_resolution_status,
                       has_conflicting_evidence, migration_reason
                FROM fhir_export_migration_audit
                """)
            ).fetchall()
        }
        assert len(audit_rows) == 10

        def pg_id(name: str) -> str:
            return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))

        # FILE_DOWNLOAD rows were unchanged and must not be audited
        assert pg_id("exp_file_gen") not in audit_rows
        assert pg_id("exp_file_pend") not in audit_rows
        assert pg_id("exp_file_fail") not in audit_rows

        # Verify before/after accuracy in PostgreSQL
        assert audit_rows[pg_id("exp_ehr_gen_invalid")].old_export_status == "GENERATED"
        assert audit_rows[pg_id("exp_ehr_gen_invalid")].new_export_status == "FAILED"
        assert audit_rows[pg_id("exp_ehr_gen_invalid")].new_transmission_state == "HISTORICAL_INDETERMINATE"
        assert bool(audit_rows[pg_id("exp_ehr_gen_invalid")].has_conflicting_evidence) is True

        assert audit_rows[pg_id("exp_ehr_comp")].old_export_status == "COMPLETED"
        assert audit_rows[pg_id("exp_ehr_comp")].new_export_status == "TRANSMITTED"
        assert audit_rows[pg_id("exp_ehr_comp")].new_transmission_state == "TRANSMITTED"
        assert audit_rows[pg_id("exp_ehr_comp")].new_resolution_status == "RESOLVED_DELIVERED"

    # Test Idempotency on PostgreSQL
    res_repeat = runner.run()
    assert res_repeat.success is True
    assert res_repeat.already_applied is True
    assert res_repeat.migrated_rows_count == 0
    assert res_repeat.audit_records_count == 0


def test_postgresql_advisory_lock_contention(pg_engine_url: str):
    """Verify single-writer PostgreSQL advisory lock contention between two independent connections."""
    from sqlalchemy.exc import DBAPIError

    engine = create_engine(pg_engine_url)

    # Clean schema_migrations version if present
    with engine.connect() as conn:
        conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                applied_by VARCHAR(128) DEFAULT CURRENT_USER
            )
            """)
        )
        conn.execute(text("DELETE FROM schema_migrations WHERE version = :v"), {"v": MIGRATION_VERSION})
        conn.commit()

    # Connection A acquires advisory lock and holds transaction open
    conn_a = engine.connect()
    trans_a = conn_a.begin()
    conn_a.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": POSTGRESQL_ADVISORY_LOCK_ID},
    )

    # 1. Non-blocking probe on Connection B proves advisory lock is actively held by Connection A
    with engine.connect() as conn_b:
        acquired = conn_b.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": POSTGRESQL_ADVISORY_LOCK_ID},
        ).scalar()
        assert acquired is False

    # 2. Verify that PhaseALifecycleMigrationRunner itself attempts to acquire POSTGRESQL_ADVISORY_LOCK_ID
    # and is blocked/timed out while Connection A holds the lock.
    # If the runner did not acquire POSTGRESQL_ADVISORY_LOCK_ID, runner_b.run() would proceed and this assertion would fail!
    engine_b = create_engine(pg_engine_url, connect_args={"options": "-c statement_timeout=250ms"})
    runner_b = PhaseALifecycleMigrationRunner(engine_b)
    with pytest.raises(DBAPIError, match=r"(canceling statement due to statement timeout|statement_timeout)"):
        runner_b.run()

    # 3. Connection A completes migration and commits, releasing the transaction advisory lock
    conn_a.execute(
        text("INSERT INTO schema_migrations (version) VALUES (:v) ON CONFLICT DO NOTHING"),
        {"v": MIGRATION_VERSION},
    )
    trans_a.commit()
    conn_a.close()

    # 4. Connection B now runs migration runner: lock is released, version is observed, exits safely as already applied
    runner_b_unlocked = PhaseALifecycleMigrationRunner(engine)
    res_b = runner_b_unlocked.run()
    assert res_b.success is True
    assert res_b.already_applied is True
    assert res_b.migrated_rows_count == 0
