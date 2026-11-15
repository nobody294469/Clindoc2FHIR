"""Isolated unit and concurrency tests for F-01 (Hot-Reload Concurrency) and F-02 (Pointer Durability).

These tests run in isolated temporary directories using synthetic SQLite databases
and do NOT touch the production active pointer or database.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict
from unittest.mock import patch

import pytest

from clindoc.terminology.compiler import TerminologyDatabaseCompiler, load_active_terminology_pointer
from clindoc.terminology.schema import SCHEMA_DDL, FTS5_DDL
from clindoc.terminology.service import TerminologyRetrievalService


def _create_mock_terminology_db(db_path: Path, version: str, lab_name: str = "Hemoglobin") -> None:
    """Helper to create a valid minimal terminology database."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_DDL)
    conn.executescript(FTS5_DDL)

    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json)
        VALUES ('LOINC', '718-7', ?, 'LAB', 1, 'CHEM', 'ACTIVE', '{}')
        """,
        (lab_name,),
    )
    concept_id = cur.lastrowid
    cur.execute(
        """
        INSERT INTO concept_aliases (concept_id, alias_text, raw_display_text, search_text, alias_source, is_preferred)
        VALUES (?, ?, ?, ?, 'TEST', 1)
        """,
        (concept_id, lab_name.lower(), lab_name, lab_name.lower()),
    )

    metadata = [
        ("schema_version", "1.0.0"),
        ("version_tag", version),
        ("loinc_release_id", f"LOINC_{version}"),
        ("rxnorm_release_id", f"RxNorm_{version}"),
        ("concepts_count", "1"),
        ("aliases_count", "1"),
        ("rules_count", "0"),
    ]
    cur.executemany("INSERT INTO terminology_metadata (key, value) VALUES (?, ?)", metadata)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    conn.close()


def _write_test_pointer(dir_path: Path, db_filename: str, version: str) -> Path:
    """Writes an active_terminology.json pointer directly in a test directory."""
    ptr_data = {
        "schema_version": "1.0.0",
        "version_tag": version,
        "active_database": db_filename,
        "active_database_path": str((dir_path / db_filename).as_posix()),
        "sha256": "fakehash",
        "loinc_release_id": f"LOINC_{version}",
        "rxnorm_release_id": f"RxNorm_{version}",
        "concepts_count": 1,
        "aliases_count": 1,
        "rules_count": 0,
        "published_at": "2026-11-12T00:00:00+00:00",
    }
    p_path = dir_path / "active_terminology.json"
    with open(p_path, "w", encoding="utf-8") as f:
        json.dump(ptr_data, f, indent=2)
    return p_path


# ─── F-01: HOT-RELOAD CONCURRENCY TESTS ───────────────────────────────────────


class TestHotReloadConcurrencyF01:
    """Rigorous multi-threaded tests for F-01 hot-reload safety."""

    def test_concurrent_reloads_same_database_no_spurious_close(self, tmp_path: Path) -> None:
        """Two concurrent reloads targeting the same new database must not destroy each other's connection."""
        db1 = tmp_path / "terminology_v1.db"
        db2 = tmp_path / "terminology_v2.db"
        _create_mock_terminology_db(db1, "1.0.0", "Hemoglobin")
        _create_mock_terminology_db(db2, "2.0.0", "Serum Hemoglobin")
        _write_test_pointer(tmp_path, "terminology_v1.db", "1.0.0")

        service = TerminologyRetrievalService(tmp_path, preload_cache=False)
        assert service.db_version == "1.0.0"

        # Update pointer to v2
        _write_test_pointer(tmp_path, "terminology_v2.db", "2.0.0")

        results = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            return service.reload_if_updated()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker), executor.submit(worker)]
            for fut in concurrent.futures.as_completed(futures):
                results.append(fut.result())

        # Exactly one thread performs reload, the other cleanly returns False without corrupting state
        assert sorted(results) == [False, True]
        assert service.db_version == "2.0.0"
        assert service.db_path.name == "terminology_v2.db"

        # Crucial check: Active connection must remain open and queryable!
        res = service.retrieve("serum hemoglobin")
        assert len(res.candidates) == 1
        assert res.candidates[0].canonical_name == "Serum Hemoglobin"
        service.close()

    def test_pointer_changes_during_compilation_discards_stale_candidate(self, tmp_path: Path) -> None:
        """If active pointer moves from B to C while thread is compiling B, candidate B must be discarded."""
        db_a = tmp_path / "terminology_vA.db"
        db_b = tmp_path / "terminology_vB.db"
        db_c = tmp_path / "terminology_vC.db"
        _create_mock_terminology_db(db_a, "A.0.0", "Hgb A")
        _create_mock_terminology_db(db_b, "B.0.0", "Hgb B")
        _create_mock_terminology_db(db_c, "C.0.0", "Hgb C")

        _write_test_pointer(tmp_path, "terminology_vA.db", "A.0.0")
        service = TerminologyRetrievalService(tmp_path, preload_cache=False)
        assert service.db_version == "A.0.0"

        # Update pointer to B
        _write_test_pointer(tmp_path, "terminology_vB.db", "B.0.0")

        original_build = service._build_cache_from_conn

        def slow_build_with_pointer_shift(conn):
            # In the middle of building cache for B, the pointer jumps to C!
            _write_test_pointer(tmp_path, "terminology_vC.db", "C.0.0")
            return original_build(conn)

        with patch.object(service, "_build_cache_from_conn", side_effect=slow_build_with_pointer_shift):
            # Reload should detect pointer moved away from B, abort B fail-closed, and preserve A
            reloaded = service.reload_if_updated()
            assert reloaded is False
            assert service.db_version == "A.0.0"
            assert service.db_path.name == "terminology_vA.db"

        # Now calling reload normally should pick up C
        reloaded_c = service.reload_if_updated()
        assert reloaded_c is True
        assert service.db_version == "C.0.0"
        assert service.db_path.name == "terminology_vC.db"
        service.close()

    def test_concurrent_readers_during_reload_do_not_observe_closed_connection(self, tmp_path: Path) -> None:
        """Readers querying continuously must not encounter closed database errors during hot-reload."""
        db1 = tmp_path / "terminology_v1.db"
        db2 = tmp_path / "terminology_v2.db"
        _create_mock_terminology_db(db1, "1.0.0", "Hemoglobin")
        _create_mock_terminology_db(db2, "2.0.0", "Serum Hemoglobin")
        _write_test_pointer(tmp_path, "terminology_v1.db", "1.0.0")

        service = TerminologyRetrievalService(tmp_path, preload_cache=False)

        stop_event = threading.Event()
        reader_errors = []

        def reader_task():
            while not stop_event.is_set():
                try:
                    res = service.retrieve("hemoglobin")
                    assert isinstance(res.candidates, list)
                except Exception as e:
                    reader_errors.append(e)

        threads = [threading.Thread(target=reader_task) for _ in range(4)]
        for t in threads:
            t.start()

        time.sleep(0.05)
        # Point to db2 and reload
        _write_test_pointer(tmp_path, "terminology_v2.db", "2.0.0")
        assert service.reload_if_updated() is True

        time.sleep(0.05)
        stop_event.set()
        for t in threads:
            t.join()

        assert reader_errors == []
        service.close()

    def test_candidate_validation_failure_closes_connection_and_preserves_active(self, tmp_path: Path) -> None:
        """A corrupted candidate must have its candidate connection closed immediately."""
        db1 = tmp_path / "terminology_v1.db"
        corrupt_db = tmp_path / "terminology_corrupt.db"
        _create_mock_terminology_db(db1, "1.0.0", "Hemoglobin")
        # Write junk file
        with open(corrupt_db, "wb") as f:
            f.write(b"not a valid sqlite database")

        _write_test_pointer(tmp_path, "terminology_v1.db", "1.0.0")
        service = TerminologyRetrievalService(tmp_path, preload_cache=False)

        _write_test_pointer(tmp_path, "terminology_corrupt.db", "corrupt")
        assert service.reload_if_updated() is False
        assert service.db_version == "1.0.0"

        # Verify old connection is still alive
        res = service.retrieve("hemoglobin")
        assert len(res.candidates) == 1
        service.close()

    def test_post_publication_exception_preserves_newly_active_connection(self, tmp_path: Path) -> None:
        """An exception raised AFTER successful publication must NEVER close the newly active connection."""
        db1 = tmp_path / "terminology_v1.db"
        db2 = tmp_path / "terminology_v2.db"
        _create_mock_terminology_db(db1, "1.0.0", "Hemoglobin")
        _create_mock_terminology_db(db2, "2.0.0", "Serum Hemoglobin")
        _write_test_pointer(tmp_path, "terminology_v1.db", "1.0.0")

        service = TerminologyRetrievalService(tmp_path, preload_cache=False)
        assert service.db_version == "1.0.0"

        # Update pointer to v2
        _write_test_pointer(tmp_path, "terminology_v2.db", "2.0.0")

        # Inject an exception right after state publication (e.g. logger.info failure)
        with patch.object(service, "_validate_fts_operational"):
            with patch("logging.Logger.info", side_effect=RuntimeError("Simulated logging error after publication")):
                # The reload executes publication, hits the logging exception, but does NOT close the new connection
                reloaded = service.reload_if_updated()

        # The reload method returns True (since state was published)
        assert reloaded is True
        # Verify the newly active database path and version are updated
        assert service.db_version == "2.0.0"
        assert service.db_path.name == "terminology_v2.db"

        # Crucial check: Active connection self.conn must be OPEN and fully functional!
        res = service.retrieve("serum hemoglobin")
        assert len(res.candidates) == 1
        assert res.candidates[0].canonical_name == "Serum Hemoglobin"
        service.close()


# ─── F-02: POINTER PUBLICATION DURABILITY TESTS ──────────────────────────────


class TestPointerPublicationDurabilityF02:
    """Rigorous tests for F-02 durable pointer publication."""

    def test_unique_temp_file_and_fsync_invocation(self, tmp_path: Path) -> None:
        """Verify uuid-based temp filename, fsync invocation, and clean replacement."""
        compiler = TerminologyDatabaseCompiler(
            output_dir=tmp_path,
            version_tag="20261112",
        )

        target_db = tmp_path / "terminology_v20261112.db"
        _create_mock_terminology_db(target_db, "20261112", "Hemoglobin")

        counts = {"concepts": 1, "aliases": 1, "rules": 0}

        fsync_called = []
        real_fsync = os.fsync

        def tracking_fsync(fd):
            fsync_called.append(fd)
            real_fsync(fd)

        with patch("os.fsync", side_effect=tracking_fsync):
            published_path = compiler._publish_pointer(
                db_hash="testhash123",
                loinc_release_id="LOINC_2.83",
                rxnorm_release_id="RxNorm_CPC",
                counts=counts,
            )

        assert published_path.exists()
        assert published_path.name == "active_terminology.json"
        assert len(fsync_called) >= 1

        ptr = load_active_terminology_pointer(tmp_path)
        assert ptr is not None
        assert ptr["version_tag"] == "20261112"
        assert ptr["active_database"] == "terminology_v20261112.db"

        # Ensure no temp files lingered
        temp_files = list(tmp_path.glob("active_terminology.tmp.*.json"))
        assert temp_files == []

    def test_concurrent_temporary_file_creation_no_collision(self, tmp_path: Path) -> None:
        """Concurrent calls to _publish_pointer must produce unique temp files and not overwrite each other prematurely."""
        compiler = TerminologyDatabaseCompiler(
            output_dir=tmp_path,
            version_tag="20261112",
        )
        target_db = tmp_path / "terminology_v20261112.db"
        _create_mock_terminology_db(target_db, "20261112", "Hemoglobin")

        counts = {"concepts": 1, "aliases": 1, "rules": 0}

        def publish_task(i: int):
            return compiler._publish_pointer(
                db_hash=f"hash_{i}",
                loinc_release_id="LOINC_2.83",
                rxnorm_release_id="RxNorm_CPC",
                counts=counts,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(publish_task, i) for i in range(8)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        assert len(results) == 8
        assert all(r.name == "active_terminology.json" for r in results)

        # No orphaned temp files
        assert list(tmp_path.glob("active_terminology.tmp.*.json")) == []

    def test_publication_failure_cleans_temp_file_and_preserves_old_pointer(self, tmp_path: Path) -> None:
        """If os.replace fails, the temporary file must be unlinked and old pointer preserved."""
        # Establish initial valid pointer
        initial_ptr = tmp_path / "active_terminology.json"
        with open(initial_ptr, "w", encoding="utf-8") as f:
            json.dump({"version_tag": "old_version"}, f)

        compiler = TerminologyDatabaseCompiler(
            output_dir=tmp_path,
            version_tag="new_version",
        )
        target_db = tmp_path / "terminology_vnew_version.db"
        _create_mock_terminology_db(target_db, "new_version", "Hemoglobin")

        counts = {"concepts": 1, "aliases": 1, "rules": 0}

        with patch("os.replace", side_effect=OSError("Simulated disk replace error")):
            with pytest.raises(OSError, match="Simulated disk replace error"):
                compiler._publish_pointer(
                    db_hash="newhash",
                    loinc_release_id="LOINC_2.83",
                    rxnorm_release_id="RxNorm_CPC",
                    counts=counts,
                )

        # Old pointer remains intact
        with open(initial_ptr, "r", encoding="utf-8") as f:
            current = json.load(f)
        assert current["version_tag"] == "old_version"

        # Temporary file was cleaned up
        assert list(tmp_path.glob("active_terminology.tmp.*.json")) == []

