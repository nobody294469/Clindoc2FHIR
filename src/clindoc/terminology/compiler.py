"""Terminology database compiler and publisher for M-TERMINOLOGY-SCALE.

Compiles offline SQLite database from LOINC 2.83, NRCeS CLCI, NRCeS CDCI,
and NLM RxNorm Current Prescribable Content (CPC), populates and verifies FTS5
external-content virtual tables, validates integrity, records build metadata
and provenance notices, and optionally publishes versioned database files
with an atomic pointer file (`active_terminology.json`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from clindoc.terminology.cdci_builder import (
    CDCI_ATTRIBUTION_NOTICE,
    CdciIngestionBuilder,
)
from clindoc.terminology.loinc_builder import (
    CLCI_ATTRIBUTION_NOTICE,
    LOINC_COPYRIGHT_NOTICE,
    LoincIngestionBuilder,
)
from clindoc.terminology.rxnorm_builder import (
    RXNORM_ATTRIBUTION_NOTICE,
    RxNormIngestionBuilder,
)
from clindoc.terminology.schema import (
    FTS5_DDL,
    SCHEMA_DDL,
    TERMINOLOGY_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)


def compute_file_sha256(path: Optional[Path]) -> str:
    """Computes SHA-256 hash of a file on disk."""
    if not path or not Path(path).is_file():
        return ""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


@dataclass
class BuildResult:
    """Detailed summary of the compilation and publication process."""
    success: bool
    database_path: Path
    schema_version: str
    loinc_release_id: str
    rxnorm_release_id: str
    concepts_count: int
    aliases_count: int
    rules_count: int
    relationships_count: int
    mappings_count: int
    fts_unicode_count: int
    fts_trigram_count: int
    integrity_ok: bool
    build_duration_sec: float
    db_sha256: str
    exclusion_reasons: Dict[str, int] = field(default_factory=dict)
    pointer_path: Optional[Path] = None
    is_candidate: bool = False
    clci_release_id: Optional[str] = None
    cdci_release_id: Optional[str] = None


class TerminologyDatabaseCompiler:
    """Builds, indexes, validates, and publishes offline terminology databases."""

    def __init__(
        self,
        output_dir: Path,
        version_tag: Optional[str] = None,
        schema_version: str = TERMINOLOGY_SCHEMA_VERSION,
        is_candidate: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.version_tag = version_tag or datetime.now(timezone.utc).strftime("%Y%m%d")
        self.schema_version = schema_version
        self.is_candidate = is_candidate
        suffix = ".candidate.db" if is_candidate else ".db"
        self.db_filename = f"terminology_v{self.version_tag}{suffix}"
        self.target_db_path = self.output_dir / self.db_filename

    def compile(
        self,
        loinc_zip_path: Optional[Path] = None,
        clci_zip_path: Optional[Path] = None,
        cdci_zip_path: Optional[Path] = None,
        rxnorm_zip_path: Optional[Path] = None,
        loinc_csv_path: Optional[Path] = None,
        loinc_json_path: Optional[Path] = None,
        rxnorm_rrf_path: Optional[Path] = None,
        rxnorm_json_path: Optional[Path] = None,
        loinc_release_id: str = "LOINC_2.83",
        clci_release_id: str = "CLCI_20260629",
        cdci_release_id: str = "CDCI_2019_FLAT_FILES",
        rxnorm_release_id: str = "RxNorm_CPC_20261102",
        commit_sha: Optional[str] = None,
        publish: Optional[bool] = None,
    ) -> BuildResult:
        """Executes full database compilation pipeline:

        1. Creates relational schema and FTS5 external-content virtual tables.
        2. Ingests LOINC concepts, aliases, and axis-derived rules (from zip or CSV/JSON).
        3. Ingests CLCI Indian overlay (from zip or CSV).
        4. Ingests CDCI Flat Files Package (from zip).
        5. Ingests RxNorm CPC concepts, aliases, and relationships (from zip or RRF/JSON).
        6. Populates FTS5 Unicode and Trigram external-content indexes.
        7. Optimizes and verifies FTS5 indexes.
        8. Runs SQLite integrity check and row-count verification.
        9. Writes build metadata, source hashes, and licensing notices.
        10. Computes database SHA-256 and conditionally writes active_terminology.json pointer.
        """
        start_time = time.perf_counter()

        # By default: candidates are not published; release builds are published unless overridden
        should_publish = publish if publish is not None else (not self.is_candidate)

        if self.target_db_path.exists():
            self.target_db_path.unlink()

        conn = sqlite3.connect(str(self.target_db_path))
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = OFF;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA cache_size = -200000;")  # 200 MB cache

        loinc_hash = (
            compute_file_sha256(loinc_zip_path)
            or compute_file_sha256(loinc_csv_path)
            or compute_file_sha256(loinc_json_path)
            or "INLINE_OR_NONE"
        )
        clci_hash = compute_file_sha256(clci_zip_path) or "INLINE_OR_NONE"
        cdci_hash = compute_file_sha256(cdci_zip_path) or "INLINE_OR_NONE"
        rxnorm_hash = (
            compute_file_sha256(rxnorm_zip_path)
            or compute_file_sha256(rxnorm_rrf_path)
            or compute_file_sha256(rxnorm_json_path)
            or "INLINE_OR_NONE"
        )

        exclusion_stats: Dict[str, int] = {}
        loinc_active_cnt = 0
        loinc_trial_cnt = 0
        loinc_deprecated_cnt = 0
        loinc_discouraged_cnt = 0
        orphaned_brands_cnt = 0

        try:
            # 1. Initialize schema
            cursor = conn.cursor()
            cursor.executescript(SCHEMA_DDL)
            cursor.executescript(FTS5_DDL)
            conn.commit()

            # 2. Ingest LOINC
            loinc_builder = LoincIngestionBuilder(conn)
            if loinc_zip_path and Path(loinc_zip_path).exists():
                logger.info(f"Ingesting full LOINC distribution from zip: {loinc_zip_path}")
                loinc_stats = loinc_builder.ingest_full_release_zip(Path(loinc_zip_path))
                exclusion_stats.update(loinc_stats.get("exclusion_reasons", {}))
                by_status = loinc_stats.get("concepts_by_status", {})
                loinc_active_cnt = by_status.get("ACTIVE", 0)
                loinc_trial_cnt = by_status.get("TRIAL", 0)
                loinc_deprecated_cnt = by_status.get("DEPRECATED", 0)
                loinc_discouraged_cnt = by_status.get("DISCOURAGED", 0)
            elif loinc_csv_path and Path(loinc_csv_path).exists():
                logger.info(f"Ingesting LOINC from CSV: {loinc_csv_path}")
                loinc_stats = loinc_builder.ingest_csv(Path(loinc_csv_path))
                exclusion_stats.update(loinc_stats.get("exclusion_reasons", {}))
            elif loinc_json_path and Path(loinc_json_path).exists():
                logger.info(f"Ingesting LOINC from JSON: {loinc_json_path}")
                loinc_stats = loinc_builder.ingest_loinc_subset_json(Path(loinc_json_path))
                exclusion_stats.update(loinc_stats.get("exclusion_reasons", {}))
            conn.commit()

            # 3. Ingest CLCI Indian Overlay
            if clci_zip_path and Path(clci_zip_path).exists():
                logger.info(f"Ingesting NRCeS CLCI overlay from zip: {clci_zip_path}")
                clci_stats = loinc_builder.ingest_clci(Path(clci_zip_path))
                conn.commit()

            # 4. Ingest CDCI Flat Files Package
            if cdci_zip_path and Path(cdci_zip_path).exists():
                logger.info(f"Ingesting NRCeS CDCI Flat Files Package from zip: {cdci_zip_path}")
                cdci_builder = CdciIngestionBuilder(conn)
                cdci_stats = cdci_builder.ingest_flat_files_package(Path(cdci_zip_path))
                orphaned_brands_cnt = cdci_stats.get("orphaned_brands_count", 0)
                exclusion_stats.update(cdci_stats.get("exclusion_reasons", {}))
                conn.commit()

            # 5. Ingest RxNorm CPC
            rxnorm_builder = RxNormIngestionBuilder(conn)
            if rxnorm_zip_path and Path(rxnorm_zip_path).exists():
                logger.info(f"Ingesting full RxNorm CPC distribution from zip: {rxnorm_zip_path}")
                rx_stats = rxnorm_builder.ingest_full_release_zip(Path(rxnorm_zip_path))
                exclusion_stats.update(rx_stats.get("exclusion_reasons", {}))
            elif rxnorm_rrf_path and Path(rxnorm_rrf_path).exists():
                logger.info(f"Ingesting RxNorm from RRF: {rxnorm_rrf_path}")
                rx_stats = rxnorm_builder.ingest_rxnconso_file(Path(rxnorm_rrf_path))
                exclusion_stats.update(rx_stats.get("exclusion_reasons", {}))
            elif rxnorm_json_path and Path(rxnorm_json_path).exists():
                logger.info(f"Ingesting RxNorm from JSON: {rxnorm_json_path}")
                rx_stats = rxnorm_builder.ingest_medication_subset_json(Path(rxnorm_json_path))
                exclusion_stats.update(rx_stats.get("exclusion_reasons", {}))
            conn.commit()

            # 6. Populate FTS5 External Content Tables
            logger.info("Populating FTS5 external-content virtual tables...")
            cursor.execute(
                """
                INSERT INTO fts_aliases_unicode(rowid, alias_text)
                SELECT alias_id, alias_text FROM concept_aliases
                """
            )
            cursor.execute(
                """
                INSERT INTO fts_aliases_trigram(rowid, alias_text)
                SELECT alias_id, alias_text FROM concept_aliases
                """
            )
            conn.commit()

            # 7. Optimize FTS5 indexes
            logger.info("Optimizing FTS5 virtual tables...")
            cursor.execute("INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('optimize');")
            cursor.execute("INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('optimize');")
            conn.commit()

            # 8. Verify row counts and integrity
            counts = self._verify_integrity(conn)

            # 9. Record metadata
            build_timestamp = datetime.now(timezone.utc).isoformat()
            metadata_entries = [
                ("schema_version", self.schema_version),
                ("version_tag", self.version_tag),
                ("build_timestamp", build_timestamp),
                ("is_candidate", "true" if self.is_candidate else "false"),
                ("loinc_release_id", loinc_release_id),
                ("loinc_source_hash", loinc_hash),
                ("loinc_copyright_notice", LOINC_COPYRIGHT_NOTICE),
                ("loinc_active_count", str(loinc_active_cnt)),
                ("loinc_trial_count", str(loinc_trial_cnt)),
                ("loinc_deprecated_count", str(loinc_deprecated_cnt)),
                ("loinc_discouraged_count", str(loinc_discouraged_cnt)),
                ("clci_release_id", clci_release_id),
                ("clci_source_hash", clci_hash),
                ("clci_attribution_notice", CLCI_ATTRIBUTION_NOTICE),
                ("cdci_release_id", cdci_release_id),
                ("cdci_source_hash", cdci_hash),
                ("cdci_attribution_notice", CDCI_ATTRIBUTION_NOTICE),
                ("orphaned_brands_count", str(orphaned_brands_cnt)),
                ("rxnorm_release_id", rxnorm_release_id),
                ("rxnorm_source_hash", rxnorm_hash),
                ("rxnorm_attribution_notice", RXNORM_ATTRIBUTION_NOTICE),
                ("concepts_count", str(counts["concepts"])),
                ("aliases_count", str(counts["aliases"])),
                ("rules_count", str(counts["rules"])),
                ("relationships_count", str(counts["relationships"])),
                ("mappings_count", str(counts["mappings"])),
                ("build_commit_sha", commit_sha or "LOCAL_BUILD"),
            ]
            cursor.executemany(
                "INSERT OR REPLACE INTO terminology_metadata (key, value) VALUES (?, ?)",
                metadata_entries,
            )
            conn.commit()

        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            conn.close()

        # 10. Compute DB file hash and conditionally publish pointer
        db_hash = compute_file_sha256(self.target_db_path)
        pointer_path = None
        if should_publish:
            pointer_path = self._publish_pointer(
                db_hash=db_hash,
                loinc_release_id=loinc_release_id,
                rxnorm_release_id=rxnorm_release_id,
                counts=counts,
            )

        duration = time.perf_counter() - start_time

        return BuildResult(
            success=counts["integrity_ok"],
            database_path=self.target_db_path,
            schema_version=self.schema_version,
            loinc_release_id=loinc_release_id,
            rxnorm_release_id=rxnorm_release_id,
            concepts_count=counts["concepts"],
            aliases_count=counts["aliases"],
            rules_count=counts["rules"],
            relationships_count=counts["relationships"],
            mappings_count=counts["mappings"],
            fts_unicode_count=counts["fts_unicode"],
            fts_trigram_count=counts["fts_trigram"],
            integrity_ok=counts["integrity_ok"],
            build_duration_sec=duration,
            db_sha256=db_hash,
            exclusion_reasons=exclusion_stats,
            pointer_path=pointer_path,
            is_candidate=self.is_candidate,
            clci_release_id=clci_release_id,
            cdci_release_id=cdci_release_id,
        )

    def _verify_integrity(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """Runs integrity checks and verifies row counts across base and FTS5 tables."""
        cursor = conn.cursor()

        # SQLite base integrity
        cursor.execute("PRAGMA integrity_check;")
        pragma_rows = cursor.fetchall()
        pragma_ok = len(pragma_rows) == 1 and pragma_rows[0][0] == "ok"

        # FTS5 integrity checks
        cursor.execute("INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('integrity-check');")
        cursor.execute("INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('integrity-check');")

        # Counts
        cursor.execute("SELECT COUNT(*) FROM concepts")
        concepts_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM concept_aliases")
        aliases_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM alias_context_rules")
        rules_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM concept_relationships")
        rel_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM concept_mappings")
        map_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM fts_aliases_unicode")
        fts_u_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM fts_aliases_trigram")
        fts_t_count = cursor.fetchone()[0]

        # FTS5 external content must have exact matching count to concept_aliases
        sync_ok = (aliases_count == fts_u_count == fts_t_count)

        return {
            "concepts": concepts_count,
            "aliases": aliases_count,
            "rules": rules_count,
            "relationships": rel_count,
            "mappings": map_count,
            "fts_unicode": fts_u_count,
            "fts_trigram": fts_t_count,
            "integrity_ok": pragma_ok and sync_ok,
            "pragma_ok": pragma_ok,
            "sync_ok": sync_ok,
        }

    def _publish_pointer(
        self,
        db_hash: str,
        loinc_release_id: str,
        rxnorm_release_id: str,
        counts: Dict[str, Any],
    ) -> Path:
        """Durably and atomically publishes active_terminology.json pointing to the new version.

        Guarantees and Platform Limitations:
        1. Unique temporary filename:
           - Generated via `uuid.uuid4().hex` to prevent collisions across processes and threads.
        2. File data synchronization:
           - Calls `f.flush()` and `os.fsync(f.fileno())` before renaming. This commits user-space stdio
             and OS kernel page cache buffers to the underlying storage device.
        3. Atomic replacement:
           - Calls `os.replace(temp_pointer, pointer_path)`. Under POSIX (`rename(2)`) and Windows NTFS
             (`MoveFileExW` with `MOVEFILE_REPLACE_EXISTING`), this provides an atomic directory update
             where concurrent readers observe either the previous or the new pointer, never a partial file.
        4. Directory synchronization (POSIX vs Windows):
           - On POSIX platforms where directory file descriptors can be opened (`os.O_DIRECTORY`), the parent
             directory metadata is synced via `os.fsync(dir_fd)` to persist the directory entry itself.
           - Platform Limitation (Windows): Windows does not support `fsync` on directory handles. While NTFS
             maintains metadata consistency through journaling, user-space code cannot guarantee absolute
             directory-entry persistence across sudden hardware power loss or unbuffered hard resets.
        5. Storage controller caching limitation:
           - Drive controllers with volatile write caches enabled may reorder hardware writes unless
             backed by battery/capacitor or configured with write-through caching.
        6. Clean failure recovery:
           - Any exception cleans up the temporary file and leaves the existing active pointer undisturbed.
        """
        pointer_data = {
            "schema_version": self.schema_version,
            "version_tag": self.version_tag,
            "active_database": self.db_filename,
            "active_database_path": str(self.target_db_path).replace("\\", "/"),
            "sha256": db_hash,
            "loinc_release_id": loinc_release_id,
            "rxnorm_release_id": rxnorm_release_id,
            "concepts_count": counts["concepts"],
            "aliases_count": counts["aliases"],
            "rules_count": counts["rules"],
            "published_at": datetime.now(timezone.utc).isoformat(),
        }

        pointer_path = self.output_dir / "active_terminology.json"
        temp_pointer = self.output_dir / f"active_terminology.tmp.{uuid.uuid4().hex}.json"

        try:
            with open(temp_pointer, "w", encoding="utf-8") as f:
                json.dump(pointer_data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            # Atomic replace in destination directory with retry for transient Windows file locks
            max_retries = 10
            for attempt in range(max_retries):
                try:
                    os.replace(temp_pointer, pointer_path)
                    break
                except PermissionError:
                    if attempt == max_retries - 1:
                        raise
                    time.sleep(0.01 * (attempt + 1))

            # Directory metadata sync on POSIX platforms where directory fds are openable
            if hasattr(os, "O_DIRECTORY"):
                try:
                    dir_fd = os.open(str(self.output_dir), os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except Exception as dir_err:
                    logger.debug(f"Parent directory fsync not supported or permitted on this platform: {dir_err}")

            return pointer_path
        except Exception:
            if temp_pointer.exists():
                try:
                    temp_pointer.unlink()
                except Exception:
                    pass
            raise


def load_active_terminology_pointer(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Loads and validates the active_terminology.json pointer from a directory."""
    pointer_path = Path(data_dir) / "active_terminology.json"
    if not pointer_path.exists():
        return None

    with open(pointer_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    db_path = Path(data.get("active_database_path", ""))
    if not db_path.exists():
        db_path = Path(data_dir) / data.get("active_database", "")
        if not db_path.exists():
            return None

    return data
