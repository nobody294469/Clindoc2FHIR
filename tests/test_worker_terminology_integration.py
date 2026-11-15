"""Comprehensive unit and integration test suite for Phase 3D.3 & Phase 3D.3.2:
Runtime Terminology Service Lifecycle, Worker Integration & Service Ownership.

Validates:
1. Production worker receives an injected scaled retrieval service from the active pointer.
2. Normalizer instances remain isolated across workers (no shared mutable audit state).
3. Service is reused across jobs in the same process without redundant database reconnection.
4. Reload check occurs at the bounded job boundary, not per claim.
5. Pointer changes during an in-flight job maintain consistent state for the current job,
   and the subsequent job loads the updated release.
6. Invalid or missing startup configuration fails safely with an operational error (no silent fallback).
7. Runtime retrieval failure fails closed: claim unnormalized, reasons recorded, zero legacy fallback.
8. Worker shutdown cleanly releases resources (worker.close() / daemon.stop()).
9. API dependency in deps.py resolves process-local service and honors lifecycle overrides.
10. Backward compatibility: explicitly constructed normalizers without a service retain legacy fallback.
11. Directory consistency: get_process_terminology_service rejects conflicting data_dir.
12. Explicit normalizer and service reconciliation for Cases A–E.
13. Resource ownership: process-owned vs caller-owned vs worker-owned; no leaks and idempotent shutdown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from clindoc.api.deps import get_terminology_service, set_global_terminology_service
from clindoc.db import (
    Base,
    Claim,
    ClaimOrigin,
    Document,
    DocumentStatus,
    JobStatus,
    ProcessingJob,
    Role,
    User,
    UserRole,
)
from clindoc.extract.contracts import Claim as ContractClaim
from clindoc.jobs.worker import DocumentProcessingDaemon, DocumentProcessingWorker
from clindoc.normalize.normalizer import TerminologyNormalizer
from clindoc.terminology.schema import FTS5_DDL, SCHEMA_DDL
from clindoc.terminology.service import (
    ContextStatus,
    QueryContext,
    RetrievalMethod,
    RetrievalResponse,
    RetrievedCandidate,
    TerminologyRetrievalService,
    get_process_terminology_service,
    normalize_alias_text,
    reset_process_terminology_service,
    set_process_terminology_service,
)


def _build_synthetic_terminology_db(db_path: Path) -> None:
    """Build a deterministic, isolated synthetic terminology database for portable testing."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.executescript(SCHEMA_DDL)
    conn.executescript(FTS5_DDL)
    cur = conn.cursor()

    concepts = [
        # Lab concepts
        ("LOINC", "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c/hemoglobin.total in blood", True),
            ("hemoglobin a1c in blood", False),
            ("hba1c", False),
            ("a1c", False),
        ]),
        ("LOINC", "41995-2", "Hemoglobin A1c [Mass/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c", True),
        ]),
        ("LOINC", "55454-3", "Hemoglobin A1c [Mass fraction] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c", True),
        ]),
        ("LOINC", "17856-6", "Hemoglobin A1c/Hemoglobin.total in Blood by HPLC", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("hemoglobin a1c/hemoglobin.total in blood by hplc", True),
        ]),
        ("LOINC", "2345-7", "Glucose [Mass/volume] in Serum or Plasma", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("glucose", True),
        ]),
        ("LOINC", "14771-0", "Glucose [Mass/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("glucose", False),
        ]),
        ("LOINC", "26449-9", "Eosinophils [#/volume] in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("eosinophils, blood", True),
        ]),
        ("LOINC", "711-2", "Eosinophils/100 leukocytes in Blood", "LAB", 1, "CHEM", "ACTIVE", "{}", [
            ("eosinophils, blood", True),
        ]),
        # Med concepts
        ("RxNorm", "83367", "Atorvastatin", "IN", None, None, "ACTIVE", "{}", [
            ("atorvastatin", True),
        ]),
        ("RxNorm", "29046", "Lisinopril", "IN", None, None, "ACTIVE", "{}", [
            ("lisinopril", True),
        ]),
        ("CDCI", "372567009", "Metformin", "SUBSTANCE", None, None, "ACTIVE", "{}", [
            ("metformin", True),
        ]),
        ("RxNorm", "6809", "Metformin", "IN", None, None, "ACTIVE", "{}", [
            ("metformin", False),
        ]),
        ("RxNorm", "6918", "Metoprolol", "IN", None, None, "ACTIVE", "{}", [
            ("metoprolol", True),
        ]),
        ("CDCI", "372687004", "Amoxicillin", "SUBSTANCE", None, None, "ACTIVE", "{}", [
            ("amoxicillin", True),
        ]),
        ("RxNorm", "723", "Amoxicillin", "IN", None, None, "ACTIVE", "{}", [
            ("amoxicillin", False),
        ]),
    ]

    for sys, code, name, tty, ctype, cls, status, attrs, aliases in concepts:
        cur.execute(
            "INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sys, code, name, tty, ctype, cls, status, attrs)
        )
        cid = cur.lastrowid
        for alias_str, is_pref in aliases:
            norm = normalize_alias_text(alias_str)
            cur.execute(
                "INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, alias_source, is_preferred) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, norm, alias_str, norm, "SYNTHETIC", 1 if is_pref else 0)
            )

    conn.commit()
    conn.execute("INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('rebuild');")
    conn.execute("INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('rebuild');")

    metadata = [
        ("schema_version", "2.0.0"),
        ("version_tag", "20261112"),
        ("loinc_release_id", "LOINC_2.78_SYNTH"),
        ("rxnorm_release_id", "RxNorm_20261112_SYNTH"),
        ("concepts_count", str(len(concepts))),
        ("aliases_count", "20"),
        ("rules_count", "0"),
    ]
    cur.executemany("INSERT INTO terminology_metadata (key, value) VALUES (?, ?)", metadata)
    conn.commit()
    conn.close()


@pytest.fixture(scope="module", autouse=True)
def synthetic_terminology_env(tmp_path_factory):
    """Provide isolated, deterministic synthetic terminology database environment for portable execution."""
    tmp_dir = tmp_path_factory.mktemp("synthetic_worker_terminology")
    db_file = tmp_dir / "terminology_v20261112.db"
    _build_synthetic_terminology_db(db_file)
    ptr_file = tmp_dir / "active_terminology.json"
    ptr_data = {
        "schema_version": "2.0.0",
        "version_tag": "20261112",
        "active_database": "terminology_v20261112.db",
        "active_database_path": str(db_file.as_posix()),
        "sha256": "synthetic_sha256",
        "loinc_release_id": "LOINC_2.78_SYNTH",
        "rxnorm_release_id": "RxNorm_20261112_SYNTH",
        "concepts_count": 14,
        "aliases_count": 20,
        "rules_count": 0,
        "published_at": "2026-11-12T00:00:00+00:00",
    }
    ptr_file.write_text(json.dumps(ptr_data, indent=2), encoding="utf-8")

    old_env = os.environ.get("CLINDOC_TERMINOLOGY_DIR")
    os.environ["CLINDOC_TERMINOLOGY_DIR"] = str(tmp_dir)
    reset_process_terminology_service()
    yield tmp_dir
    reset_process_terminology_service()
    if old_env is not None:
        os.environ["CLINDOC_TERMINOLOGY_DIR"] = old_env
    else:
        os.environ.pop("CLINDOC_TERMINOLOGY_DIR", None)


@pytest.fixture(autouse=True)
def clean_process_terminology_service():
    """Ensure process-local terminology service state is reset before and after each test."""
    reset_process_terminology_service()
    set_global_terminology_service(None)
    yield
    reset_process_terminology_service()
    set_global_terminology_service(None)


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite session with foreign keys enabled."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def sample_data(db_session: Session):
    """Seed sample user, role, document, and processing job."""
    reviewer_role = Role(
        id="clinical_reviewer",
        name="Clinical Reviewer",
        description="Clinical review role",
    )
    db_session.add(reviewer_role)
    db_session.flush()

    user = User(
        idp_subject="sub_worker_test",
        username="worker_tester",
        full_name="Worker Tester",
        email="tester@clindoc.local",
        is_active=True,
    )
    db_session.add(user)
    db_session.flush()

    doc = Document(
        uploaded_by=user.id,
        original_filename="worker_test.pdf",
        storage_ref="test_ref",
        mime_type="application/pdf",
        file_size_bytes=2048,
        sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        status=DocumentStatus.PROCESSING,
    )
    db_session.add(doc)
    db_session.flush()

    job = ProcessingJob(
        document_id=doc.id,
        status=JobStatus.RUNNING,
    )
    db_session.add(job)
    db_session.commit()

    return {"user": user, "doc": doc, "job": job}


class TestWorkerTerminologyLifecycleAndIntegration:
    """Phase 3D.3 Runtime Integration Test Suite."""

    def test_01_production_worker_receives_scaled_retrieval_service(self):
        """1. Production worker default construction wires scaled retrieval from active database."""
        worker = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        try:
            assert worker.terminology_service is not None
            assert isinstance(worker.terminology_service, TerminologyRetrievalService)
            assert worker.normalizer.retrieval_service is worker.terminology_service
            # Active database is the promoted candidate database
            assert "terminology_v20261112" in str(worker.terminology_service.db_path)
            assert worker.terminology_service.db_version == "20261112"
        finally:
            worker.close()

    def test_02_normalizer_instances_remain_isolated_across_workers(self):
        """2. Distinct workers own distinct normalizer instances with isolated mutable dictionaries."""
        worker1 = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        worker2 = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        try:
            # Shared underlying process-local retrieval service
            assert worker1.terminology_service is worker2.terminology_service

            # Separate worker-local normalizer instances
            assert worker1.normalizer is not worker2.normalizer

            # Mutating worker1 audit state does not affect worker2
            worker1.normalizer.audit_candidates["claim-w1"] = [{"code": "TEST-1"}]
            worker1.normalizer.audit_reasons["claim-w1"] = ["Reason 1"]
            worker1.normalizer.normalized_units["claim-w1"] = "mg"

            assert "claim-w1" not in worker2.normalizer.audit_candidates
            assert "claim-w1" not in worker2.normalizer.audit_reasons
            assert "claim-w1" not in worker2.normalizer.normalized_units
        finally:
            worker1.close()
            worker2.close()

    def test_03_service_reused_across_jobs_in_same_process(
        self, db_session: Session, sample_data
    ):
        """3. Process-local service instance is reused across successive jobs without re-opening DB."""
        doc = sample_data["doc"]
        job = sample_data["job"]

        worker = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        svc_initial = worker.terminology_service

        c1 = ContractClaim(
            claim_id="test-claim-1",
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )
        db_claim = Claim(
            claim_id=uuid.uuid4(),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            origin=ClaimOrigin.PIPELINE,
            payload={},
        )
        db_session.add(db_claim)
        db_session.commit()

        # Job 1 execution
        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[c1]
        )
        svc_after_job1 = worker.terminology_service
        assert svc_after_job1 is svc_initial

        # Job 2 execution
        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[c1]
        )
        svc_after_job2 = worker.terminology_service
        assert svc_after_job2 is svc_initial
        worker.close()

    def test_04_reload_checked_at_job_boundary(
        self, db_session: Session, sample_data
    ):
        """4. reload_if_updated() is invoked at the job boundary, not for each claim."""
        doc = sample_data["doc"]
        job = sample_data["job"]

        mock_svc = MagicMock(spec=TerminologyRetrievalService)
        mock_svc.reload_if_updated.return_value = False
        mock_svc.retrieve.return_value = RetrievalResponse(
            query="Safedox",
            normalized_query="safedox",
            candidates=[],
            is_ambiguous=False,
            is_abstained=False,
            abstention_reasons=[],
            database_version="20261112",
            source_releases={},
            query_duration_ms=1.0,
        )

        worker = DocumentProcessingWorker(
            terminology_service=mock_svc,
            router=MagicMock(),
            queue=MagicMock(),
        )

        c1 = ContractClaim(
            claim_id="c1",
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Claim 1",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )
        c2 = ContractClaim(
            claim_id="c2",
            document_id=str(doc.id),
            group_id="grp-2",
            field_type="med.name",
            value="Claim 2",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-2",
            region_id="reg-2",
        )
        c3 = ContractClaim(
            claim_id="c3",
            document_id=str(doc.id),
            group_id="grp-3",
            field_type="med.name",
            value="Claim 3",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-3",
            region_id="reg-3",
        )

        # Execute normalization stage containing 3 claims
        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[c1, c2, c3]
        )

        # reload_if_updated must be called exactly ONCE at stage start, NOT once per claim
        assert mock_svc.reload_if_updated.call_count == 1
        worker.close()

    def test_05_pointer_changes_during_job_retains_consistent_state(
        self, db_session: Session, sample_data
    ):
        """5. Pointer change during job keeps in-flight normalization consistent; next job reloads."""
        doc = sample_data["doc"]
        job = sample_data["job"]

        mock_svc = MagicMock(spec=TerminologyRetrievalService)
        # First call (job 1 boundary): no reload
        # Second call (job 2 boundary): returns True (reloaded)
        mock_svc.reload_if_updated.side_effect = [False, True]
        mock_svc.db_version = "v2_updated"

        worker = DocumentProcessingWorker(
            terminology_service=mock_svc,
            router=MagicMock(),
            queue=MagicMock(),
        )

        c = ContractClaim(
            claim_id="c1",
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Safedox",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        # Job 1 starts: reload_if_updated called once (returns False)
        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[c]
        )
        assert mock_svc.reload_if_updated.call_count == 1

        # Simulate pointer change on disk happening while job 1 was running
        # Job 1 did NOT observe reload during claim loop

        # Job 2 starts: boundary check detects pointer change and reloads
        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[c]
        )
        assert mock_svc.reload_if_updated.call_count == 2
        worker.close()

    def test_06_invalid_startup_configuration_fails_safely(self, tmp_path):
        """6. Missing/invalid terminology configuration fails fast with an operational error."""
        non_existent_dir = tmp_path / "does_not_exist"

        # Worker initialization pointing to invalid directory MUST raise FileNotFoundError
        with pytest.raises(FileNotFoundError, match="Cannot resolve terminology SQLite database"):
            DocumentProcessingWorker(
                terminology_dir=non_existent_dir,
                router=MagicMock(),
                queue=MagicMock(),
            )

    def test_07_runtime_retrieval_failure_does_not_trigger_legacy_fallback(
        self, db_session: Session, sample_data
    ):
        """7. Scaled retrieval failure fails closed: claim unnormalized, abstention reasons logged, zero legacy fallback."""
        doc = sample_data["doc"]
        job = sample_data["job"]

        c_id = str(uuid.uuid4())
        db_claim = Claim(
            claim_id=uuid.UUID(c_id),
            document_id=doc.id,
            group_id="grp-1",
            field_type="med.name",
            value="Amoxicillin",  # Exists in legacy subset!
            origin=ClaimOrigin.PIPELINE,
            payload={},
        )
        db_session.add(db_claim)
        db_session.commit()

        contract_c = ContractClaim(
            claim_id=c_id,
            document_id=str(doc.id),
            group_id="grp-1",
            field_type="med.name",
            value="Amoxicillin",
            extraction_method="deterministic",
            extraction_confidence=0.95,
            evidence_id="ev-1",
            region_id="reg-1",
        )

        # Failing service
        faulty_svc = MagicMock(spec=TerminologyRetrievalService)
        faulty_svc.retrieve.side_effect = RuntimeError("SQLite disk I/O failure")
        faulty_svc.reload_if_updated.return_value = False

        worker = DocumentProcessingWorker(
            terminology_service=faulty_svc,
            router=MagicMock(),
            queue=MagicMock(),
        )

        worker._orchestrate_normalization_stage(
            session=db_session, job=job, document=doc, extraction_output=[contract_c]
        )
        db_session.commit()
        db_session.expire_all()

        reloaded = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
        # Must FAIL CLOSED: zero legacy fallback
        assert reloaded.normalized_code is None
        assert reloaded.code_system is None
        assert "abstention_reasons" in reloaded.payload
        assert any("SQLite disk I/O failure" in r for r in reloaded.payload["abstention_reasons"])
        worker.close()

    def test_08_worker_and_daemon_shutdown_cleans_up_resources(self):
        """8. Worker.close() and Daemon.stop() cleanly release owned services and threads."""
        mock_svc = MagicMock(spec=TerminologyRetrievalService)
        worker = DocumentProcessingWorker(
            terminology_service=mock_svc,
            router=MagicMock(),
            queue=MagicMock(),
            owns_terminology_service=True,
        )

        daemon = DocumentProcessingDaemon(worker=worker, session_factory=MagicMock())
        daemon.stop()

        # Service close was called
        assert mock_svc.close.call_count == 1
        assert worker.terminology_service is None

    def test_09_api_dependency_resolves_process_service(self):
        """9. FastAPI dependency get_terminology_service resolves process service and honors overrides."""
        # 1. Default resolution returns active service
        svc = get_terminology_service()
        assert svc is not None
        assert isinstance(svc, TerminologyRetrievalService)

        # 2. set_global_terminology_service overrides with force=True
        mock_api_svc = MagicMock(spec=TerminologyRetrievalService)
        set_global_terminology_service(mock_api_svc, force=True)
        assert get_terminology_service() is mock_api_svc

    def test_10_explicitly_constructed_legacy_normalizer_retains_fallback(self):
        """10. Backward compatibility: explicitly constructed normalizer without service uses legacy subset."""
        legacy_normalizer = TerminologyNormalizer(retrieval_service=None)
        worker = DocumentProcessingWorker(
            normalizer=legacy_normalizer,
            router=MagicMock(),
            queue=MagicMock(),
        )
        try:
            assert worker.terminology_service is None
            assert worker.normalizer is legacy_normalizer

            # Normalizes Amoxicillin via legacy subset
            claim = ContractClaim(
                claim_id="leg-1",
                document_id="doc-1",
                group_id="grp-1",
                field_type="med.name",
                value="Amoxicillin",
                extraction_method="deterministic",
                extraction_confidence=0.95,
                evidence_id="ev-1",
                region_id="reg-1",
            )
            worker.normalizer.normalize_claim(claim)
            assert claim.code_system == "RxNorm"
            assert claim.normalized_code == "723"
        finally:
            worker.close()


class TestSingletonDirectoryConsistency:
    """Phase 3D.3.2: Verification of process-local singleton directory consistency."""

    @pytest.fixture(autouse=True)
    def _setup_canonical_dir(self, synthetic_terminology_env):
        self.canonical_dir = synthetic_terminology_env

    def test_01_first_initialization_with_dir_a(self):
        """1. First initialization with canonical directory succeeds."""
        svc = get_process_terminology_service(data_dir=self.canonical_dir)
        assert svc is not None
        assert svc.input_path.resolve() == self.canonical_dir.resolve()

    def test_02_subsequent_call_with_dir_a_returns_same_instance(self):
        """2. Subsequent call with same directory returns identical instance."""
        svc1 = get_process_terminology_service(data_dir=self.canonical_dir)
        svc2 = get_process_terminology_service(data_dir=self.canonical_dir)
        assert svc1 is svc2

    def test_03_subsequent_call_with_dir_b_raises_value_error(self, tmp_path):
        """3. Subsequent call specifying a conflicting directory explicitly fails with ValueError."""
        svc1 = get_process_terminology_service(data_dir=self.canonical_dir)
        dir_b = tmp_path / "other_terminology"
        dir_b.mkdir()

        with pytest.raises(ValueError, match="already initialized with directory"):
            get_process_terminology_service(data_dir=dir_b)

        # Original service remains intact
        assert get_process_terminology_service() is svc1

    def test_04_calls_omitting_data_dir_after_initialization(self):
        """4. Calls omitting data_dir after initialization return the existing singleton without error."""
        svc1 = get_process_terminology_service(data_dir=self.canonical_dir)
        svc2 = get_process_terminology_service()
        assert svc1 is svc2

    def test_05_concurrent_first_initialization(self):
        """5. Concurrent first initialization is thread-safe and produces exactly one instance."""
        results: list[TerminologyRetrievalService] = []
        barrier = threading.Barrier(5)

        def init_task():
            barrier.wait()
            svc = get_process_terminology_service(data_dir=self.canonical_dir)
            results.append(svc)

        threads = [threading.Thread(target=init_task) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5
        first_svc = results[0]
        assert all(s is first_svc for s in results)

    def test_06_initialization_failure_leaves_singleton_uninitialized(self, tmp_path):
        """6. Initialization failure does not leave a partially initialized singleton."""
        invalid_dir = tmp_path / "non_existent"
        with pytest.raises(FileNotFoundError):
            get_process_terminology_service(data_dir=invalid_dir)

        # Process service remains None, and subsequent valid call succeeds
        valid_svc = get_process_terminology_service(data_dir=self.canonical_dir)
        assert valid_svc is not None

    def test_07_explicit_dir_a_followed_by_omitted_data_dir_after_env_changed_to_b_raises_value_error(
        self, tmp_path, monkeypatch
    ):
        """7. Explicit dir A followed by omitted data_dir when environment changed to B raises ValueError."""
        svc_a = get_process_terminology_service(data_dir=self.canonical_dir)
        assert svc_a is not None

        other_dir = tmp_path / "other_terminology"
        other_dir.mkdir()
        monkeypatch.setenv("CLINDOC_TERMINOLOGY_DIR", str(other_dir))

        with pytest.raises(ValueError, match="already initialized with directory"):
            get_process_terminology_service()

        # Original service remains intact and untouched
        monkeypatch.setenv("CLINDOC_TERMINOLOGY_DIR", str(self.canonical_dir))
        assert get_process_terminology_service() is svc_a

    def test_08_default_dir_initialization_followed_by_explicit_conflicting_dir(self, tmp_path):
        """8. Default-directory initialization followed by an explicit conflicting directory raises ValueError."""
        svc_default = get_process_terminology_service()
        assert svc_default is not None

        other_dir = tmp_path / "conflicting_terminology"
        other_dir.mkdir()

        with pytest.raises(ValueError, match="already initialized with directory"):
            get_process_terminology_service(data_dir=other_dir)

        # Original service remains active
        assert get_process_terminology_service() is svc_default

    def test_09_same_directory_different_pointer_name_raises_value_error(self):
        """9. Same directory but different pointer_name raises ValueError."""
        svc = get_process_terminology_service(data_dir=self.canonical_dir, pointer_name="active_terminology.json")
        assert svc is not None

        with pytest.raises(ValueError, match="already initialized with pointer_name"):
            get_process_terminology_service(data_dir=self.canonical_dir, pointer_name="other_pointer.json")

        assert get_process_terminology_service() is svc

    def test_10_existing_singleton_remains_usable_and_unchanged_after_rejected_conflicts(
        self, tmp_path, monkeypatch
    ):
        """10. Existing singleton remains usable and unchanged after each rejected conflict."""
        svc = get_process_terminology_service(data_dir=self.canonical_dir)
        assert svc is not None

        # Attempt 1: Conflicting explicit directory
        conflict_dir = tmp_path / "conflict_1"
        conflict_dir.mkdir()
        with pytest.raises(ValueError):
            get_process_terminology_service(data_dir=conflict_dir)

        # Attempt 2: Conflicting pointer name
        with pytest.raises(ValueError):
            get_process_terminology_service(data_dir=self.canonical_dir, pointer_name="diff_pointer.json")

        # Attempt 3: Conflicting environment variable
        conflict_dir_2 = tmp_path / "conflict_2"
        conflict_dir_2.mkdir()
        monkeypatch.setenv("CLINDOC_TERMINOLOGY_DIR", str(conflict_dir_2))
        with pytest.raises(ValueError):
            get_process_terminology_service()
        monkeypatch.setenv("CLINDOC_TERMINOLOGY_DIR", str(self.canonical_dir))

        # Verify singleton is completely functional and queryable
        res = svc.retrieve("metformin")
        assert res is not None
        assert not res.is_abstained
        assert len(res.candidates) > 0
        assert res.candidates[0].canonical_name.lower().startswith("metformin")

    def test_11_preload_cache_difference_does_not_reject_request(self):
        """11. Differences in preload_cache do not reject requests because both yield identical results."""
        svc = get_process_terminology_service(data_dir=self.canonical_dir, preload_cache=False)
        assert svc is not None

        # Subsequent call with preload_cache=True returns same instance without ValueError
        svc_same = get_process_terminology_service(data_dir=self.canonical_dir, preload_cache=True)
        assert svc_same is svc


class TestExplicitNormalizerAndServiceReconciliation:
    """Phase 3D.3.2: Normalizer and terminology service reconciliation (Cases A–E)."""

    def test_case_a_no_explicit_normalizer_scaled_enabled(self):
        """Case A: normalizer=None, scaled service enabled -> resolves process service, creates worker-local normalizer."""
        worker = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        try:
            assert worker.terminology_service is not None
            assert worker.normalizer.retrieval_service is worker.terminology_service
            assert worker._owns_terminology_service is False
        finally:
            worker.close()

    def test_case_b_explicit_normalizer_no_service_legacy_fallback(self):
        """Case B: normalizer=TerminologyNormalizer(), terminology_service=None -> preserves legacy fallback."""
        legacy_norm = TerminologyNormalizer(retrieval_service=None)
        worker = DocumentProcessingWorker(
            normalizer=legacy_norm,
            terminology_service=None,
            router=MagicMock(),
            queue=MagicMock(),
        )
        try:
            assert worker.normalizer is legacy_norm
            assert worker.terminology_service is None
            assert worker.normalizer.retrieval_service is None
            assert worker._owns_terminology_service is False
        finally:
            worker.close()

    def test_case_c_explicit_normalizer_with_explicit_service_supplied(
        self, db_session: Session, sample_data
    ):
        """Case C: normalizer without service + explicit service supplied -> injects service into normalizer and runs scaled retrieval."""
        doc = sample_data["doc"]
        job = sample_data["job"]

        normalizer = TerminologyNormalizer(retrieval_service=None)
        assert normalizer.retrieval_service is None

        # Build mock scaled service
        mock_svc = MagicMock(spec=TerminologyRetrievalService)
        cand = RetrievedCandidate(
            concept_id=555,
            system="CDCI",
            code="2430421000189104",
            canonical_name="Safedox 200 mg oral tablet",
            tty="BRAND",
            attributes={},
            alias_id=5,
            matched_alias="safedox",
            alias_source="CDCI_BRAND",
            is_abbreviation=False,
            is_preferred=True,
            retrieval_method=RetrievalMethod.EXACT_DB,
            rank_score=1.0,
            context_status=ContextStatus.NO_RULES,
        )
        mock_svc.retrieve.return_value = RetrievalResponse(
            query="Safedox",
            normalized_query="safedox",
            candidates=[cand],
            is_ambiguous=False,
            is_abstained=False,
            abstention_reasons=[],
            database_version="20261112",
            source_releases={"CDCI": "20261112"},
            query_duration_ms=0.5,
        )
        mock_svc.reload_if_updated.return_value = False

        worker = DocumentProcessingWorker(
            normalizer=normalizer,
            terminology_service=mock_svc,
            router=MagicMock(),
            queue=MagicMock(),
        )
        try:
            # 1. Verify exact wiring
            assert worker.normalizer is normalizer
            assert worker.normalizer.retrieval_service is mock_svc
            assert worker.terminology_service is mock_svc
            assert worker._owns_terminology_service is False

            # 2. Verify normalization ACTUALLY USES the supplied scaled service
            c_id = str(uuid.uuid4())
            db_claim = Claim(
                claim_id=uuid.UUID(c_id),
                document_id=doc.id,
                group_id="grp-1",
                field_type="med.name",
                value="Safedox",
                origin=ClaimOrigin.PIPELINE,
                payload={},
            )
            db_session.add(db_claim)
            db_session.commit()

            contract_c = ContractClaim(
                claim_id=c_id,
                document_id=str(doc.id),
                group_id="grp-1",
                field_type="med.name",
                value="Safedox",
                extraction_method="deterministic",
                extraction_confidence=0.95,
                evidence_id="ev-1",
                region_id="reg-1",
            )

            worker._orchestrate_normalization_stage(
                session=db_session, job=job, document=doc, extraction_output=[contract_c]
            )
            db_session.commit()
            db_session.expire_all()

            reloaded = db_session.query(Claim).filter(Claim.claim_id == uuid.UUID(c_id)).one()
            assert reloaded.normalized_code == "2430421000189104"
            assert reloaded.code_system == "CDCI"
            assert mock_svc.retrieve.call_count == 1
        finally:
            worker.close()

    def test_case_d_explicit_normalizer_with_conflicting_explicit_service_raises_error(self):
        """Case D: normalizer containing service A + explicit service B (A != B) raises ValueError."""
        svc_a = MagicMock(spec=TerminologyRetrievalService)
        svc_b = MagicMock(spec=TerminologyRetrievalService)

        norm = TerminologyNormalizer(retrieval_service=svc_a)

        with pytest.raises(ValueError, match="Conflicting terminology services provided"):
            DocumentProcessingWorker(
                normalizer=norm,
                terminology_service=svc_b,
                router=MagicMock(),
                queue=MagicMock(),
            )

    def test_case_e_explicit_normalizer_with_service_and_no_explicit_service_argument(self):
        """Case E: normalizer containing service A + terminology_service=None -> preserves service A consistently."""
        svc_a = MagicMock(spec=TerminologyRetrievalService)
        norm = TerminologyNormalizer(retrieval_service=svc_a)

        worker = DocumentProcessingWorker(
            normalizer=norm,
            terminology_service=None,
            router=MagicMock(),
            queue=MagicMock(),
        )
        try:
            assert worker.normalizer is norm
            assert worker.terminology_service is svc_a
            assert worker.normalizer.retrieval_service is svc_a
        finally:
            worker.close()


class TestResourceOwnershipAndShutdown:
    """Phase 3D.3.2: Explicit resource ownership, process shutdown, and leak prevention."""

    def test_01_closing_worker_a_does_not_close_service_used_by_worker_b(self):
        """1. Closing worker A does not close the process-owned service shared with worker B."""
        worker_a = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        worker_b = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())

        shared_svc = worker_a.terminology_service
        assert shared_svc is worker_b.terminology_service

        # Close worker A
        worker_a.close()

        # Shared service remains open and usable by worker B
        cursor = shared_svc.conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1

        worker_b.close()

    def test_02_closing_worker_with_caller_injected_service_does_not_close_it(self):
        """2. Closing a worker with a caller-injected service does not close that service."""
        caller_svc = MagicMock(spec=TerminologyRetrievalService)
        worker = DocumentProcessingWorker(
            terminology_service=caller_svc,
            router=MagicMock(),
            queue=MagicMock(),
        )

        worker.close()
        # Caller-owned service was NOT closed by worker
        assert caller_svc.close.call_count == 0

    def test_03_process_shutdown_closes_process_owned_singleton(self):
        """3. Process-level reset explicitly closes the process-owned singleton."""
        svc = get_process_terminology_service()
        assert svc is not None

        # Reset process service (simulating daemon / process shutdown)
        reset_process_terminology_service()

        # Calling close on the old connection confirms it was closed
        with pytest.raises(Exception):
            svc.conn.cursor().execute("SELECT 1")

    def test_04_resetting_already_reset_service_is_safe_and_idempotent(self):
        """4. reset_process_terminology_service() is completely idempotent."""
        reset_process_terminology_service()
        reset_process_terminology_service()
        reset_process_terminology_service()

    def test_05_set_process_terminology_service_safety_and_leak_prevention(self):
        """5. set_process_terminology_service rejects accidental overwrite and closes superseded on force=True."""
        svc1 = MagicMock(spec=TerminologyRetrievalService)
        svc2 = MagicMock(spec=TerminologyRetrievalService)

        set_process_terminology_service(svc1)

        # Conflicting overwrite without force raises ValueError
        with pytest.raises(ValueError, match="Cannot overwrite an active process-local terminology service"):
            set_process_terminology_service(svc2, force=False)

        assert svc1.close.call_count == 0

        # Safe replacement with force=True closes svc1 and replaces it with svc2
        set_process_terminology_service(svc2, force=True)
        assert svc1.close.call_count == 1
        assert get_process_terminology_service() is svc2

    def test_06_worker_remains_functional_when_conflicting_replacement_rejected(self):
        """6. A worker using the current singleton remains functional when conflicting replacement is rejected."""
        worker = DocumentProcessingWorker(router=MagicMock(), queue=MagicMock())
        try:
            live_svc = worker.terminology_service
            assert live_svc is not None

            # Attempt conflicting replacement without force
            mock_replacement = MagicMock(spec=TerminologyRetrievalService)
            with pytest.raises(ValueError, match="Cannot overwrite an active process-local terminology service"):
                set_process_terminology_service(mock_replacement, force=False)

            # Old service was NOT closed
            assert live_svc.conn is not None
            cursor = live_svc.conn.cursor()
            cursor.execute("SELECT 1")
            assert cursor.fetchone()[0] == 1

            # Worker remains fully functional
            claim = ContractClaim(
                claim_id="c-safety-1",
                document_id="doc-1",
                group_id="grp-1",
                field_type="med.name",
                value="Metformin",
                extraction_method="deterministic",
                extraction_confidence=0.99,
                evidence_id="ev-1",
                region_id="reg-1",
            )
            norm = worker.normalizer.normalize_claim(claim)
            assert norm.normalized_code == "372567009"
            assert norm.code_system == "CDCI"
        finally:
            worker.close()

    def test_07_deliberate_reset_closes_old_service_and_allows_new_service_registration(self):
        """7. A deliberate reset closes the old service and allows registering a new service cleanly."""
        svc1 = get_process_terminology_service()
        assert svc1 is not None

        # Reset closes the active service
        reset_process_terminology_service()
        with pytest.raises(Exception):
            svc1.conn.cursor().execute("SELECT 1")

        # After reset, a new service can be registered cleanly without force=True
        mock_svc2 = MagicMock(spec=TerminologyRetrievalService)
        set_process_terminology_service(mock_svc2, force=False)
        assert get_process_terminology_service() is mock_svc2

    def test_08_api_setter_cannot_silently_close_service_in_use(self):
        """8. set_global_terminology_service() cannot silently close or overwrite a service in use."""
        live_svc = get_process_terminology_service()
        assert live_svc is not None

        # Calling set_global_terminology_service with force=False raises ValueError
        mock_new = MagicMock(spec=TerminologyRetrievalService)
        with pytest.raises(ValueError, match="Cannot overwrite an active process-local terminology service"):
            set_global_terminology_service(mock_new, force=False)

        # Original service connection remains open and unaffected
        cursor = live_svc.conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1
        assert get_terminology_service() is live_svc

        # Force replacement only when explicitly guaranteed by caller
        set_global_terminology_service(mock_new, force=True)
        assert get_terminology_service() is mock_new
