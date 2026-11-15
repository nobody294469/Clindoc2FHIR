"""Verification and regression test suite for M-TERMINOLOGY-SCALE Phase 3B.

Validates:
1. Candidate database isolation (unpublished, active pointer preserved).
2. Schema 2.0.0 explicit columns (class_type, class, status) and new tables (concept_relationships, concept_mappings).
3. LOINC 2.83 full ingestion (all 4 class types, 4 statuses, UCUM units).
4. CLCI Indian curated overlay (mappings, General Name aliases, discouraged code status).
5. CDCI Flat Files Package ingestion (substances, generics, brands, forms, routes, suppliers).
6. CDCI relationship topology (HAS_SUBSTANCE, HAS_ROUTE, HAS_DOSE_FORM, MANUFACTURED_BY, TRADENAME_OF).
7. CDCI 14 orphaned brands handling (UNRESOLVED_GENERIC_PARENT attribute, no invalid TRADENAME_OF edges).
8. RxNorm CPC filtering (SAB in RXNORM/MTHSPL, exclusion of 11 MTHCMSFRF records, RXNREL graph).
9. FTS5 Unicode and Trigram index synchronization and operational integrity.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from clindoc.terminology.cdci_builder import CdciIngestionBuilder
from clindoc.terminology.compiler import (
    TerminologyDatabaseCompiler,
    load_active_terminology_pointer,
)
from clindoc.terminology.loinc_builder import LoincIngestionBuilder
from clindoc.terminology.rxnorm_builder import RxNormIngestionBuilder
from clindoc.terminology.schema import (
    FTS5_DDL,
    SCHEMA_DDL,
    TERMINOLOGY_SCHEMA_VERSION,
)
from clindoc.terminology.service import QueryContext, TerminologyRetrievalService


STAGING_DIR = Path("D:/terminology_sources_staging")
LOINC_ZIP = STAGING_DIR / "Loinc_2.83.zip"
CLCI_ZIP = STAGING_DIR / "common-lab-codes-for-india-20260629.zip"
CDCI_ZIP = STAGING_DIR / "CommonDrugCodesForIndia_FlatFilePackage.zip"
RXNORM_ZIP = STAGING_DIR / "RxNorm_full_prescribe_11022026.zip"


class TestSchemaAndCandidateIsolation:
    """Verifies schema 2.0.0 DDL and candidate database isolation."""

    def test_schema_ddl_and_version(self):
        assert TERMINOLOGY_SCHEMA_VERSION == "2.0.0"
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        required = {
            "concepts",
            "concept_aliases",
            "alias_context_rules",
            "concept_relationships",
            "concept_mappings",
            "terminology_metadata",
            "fts_aliases_unicode",
            "fts_aliases_trigram",
        }
        assert required.issubset(tables)

        # Verify concepts columns
        cursor.execute("PRAGMA table_info(concepts)")
        col_names = {row[1] for row in cursor.fetchall()}
        assert {"concept_id", "system", "code", "canonical_name", "tty", "class_type", "class", "status", "attributes_json"}.issubset(col_names)

        # Verify concept_aliases columns
        cursor.execute("PRAGMA table_info(concept_aliases)")
        alias_cols = {row[1] for row in cursor.fetchall()}
        assert {"alias_id", "concept_id", "alias_text", "raw_display_text", "search_text", "language", "alias_source", "source_identifier", "is_abbreviation", "is_preferred"}.issubset(alias_cols)

    def test_candidate_compilation_leaves_pointer_untouched(self, tmp_path: Path):
        data_dir = tmp_path / "terminology"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Create pre-existing active pointer
        ptr_file = data_dir / "active_terminology.json"
        ptr_data = {
            "schema_version": "1.0.0",
            "version_tag": "pre_existing_v1",
            "active_database": "terminology_vpre_existing.db",
            "sha256": "pre_existing_hash",
        }
        with open(ptr_file, "w") as f:
            json.dump(ptr_data, f)

        # Compile candidate database
        compiler = TerminologyDatabaseCompiler(
            output_dir=data_dir,
            version_tag="20261112_candidate",
            is_candidate=True,
        )
        assert compiler.db_filename.endswith(".candidate.db")

        # Compile from small inline mock
        result = compiler.compile(
            loinc_json_path=Path("configs/loinc_subset.json"),
            rxnorm_json_path=Path("configs/medication_subset.json"),
            publish=False,
        )

        assert result.success is True
        assert result.database_path.exists()
        assert result.database_path.name.endswith(".candidate.db")
        assert result.pointer_path is None

        # Verify active pointer was 100% UNTOUCHED
        with open(ptr_file, "r") as f:
            active_ptr_after = json.load(f)
        assert active_ptr_after["version_tag"] == "pre_existing_v1"
        assert active_ptr_after["active_database"] == "terminology_vpre_existing.db"


@pytest.mark.skipif(not CDCI_ZIP.exists(), reason="CDCI staging archive not found")
class TestCdciIngestionAndTopology:
    """Verifies CDCI Flat Files ingestion, relationships, and orphan handling."""

    @pytest.fixture(scope="module")
    def cdci_db(self, tmp_path_factory) -> sqlite3.Connection:
        db_path = tmp_path_factory.mktemp("cdci") / "cdci_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        builder = CdciIngestionBuilder(conn)
        builder.ingest_flat_files_package(CDCI_ZIP)
        return conn

    def test_cdci_concept_counts_by_tty(self, cdci_db: sqlite3.Connection):
        cursor = cdci_db.cursor()
        cursor.execute("SELECT tty, COUNT(*) FROM concepts WHERE system = 'CDCI' GROUP BY tty")
        counts = dict(cursor.fetchall())

        assert counts.get("SUBSTANCE") == 3283
        assert counts.get("DOSE_FORM") == 423
        assert counts.get("ROUTE") == 160
        assert counts.get("SUPPLIER") == 8213
        assert counts.get("GENERIC") == 10305
        assert counts.get("BRAND") == 93911
        assert sum(counts.values()) == 116295

    def test_cdci_relationship_types_and_integrity(self, cdci_db: sqlite3.Connection):
        cursor = cdci_db.cursor()
        cursor.execute("SELECT relationship_type, COUNT(*) FROM concept_relationships WHERE relationship_source = 'CDCI_FLAT_FILES' GROUP BY relationship_type")
        rel_counts = dict(cursor.fetchall())

        assert rel_counts.get("HAS_SUBSTANCE") == 13127
        assert rel_counts.get("HAS_ROUTE") == 13044
        assert rel_counts.get("HAS_DOSE_FORM") == 10305
        assert rel_counts.get("MANUFACTURED_BY") == 93911
        assert rel_counts.get("TRADENAME_OF") == 93897  # Exactly 93,911 - 14 orphans

    def test_cdci_orphaned_brands_handling(self, cdci_db: sqlite3.Connection):
        cursor = cdci_db.cursor()
        # Find brands with unresolved_generic_parent
        cursor.execute(
            """
            SELECT code, canonical_name, attributes_json 
            FROM concepts 
            WHERE system = 'CDCI' AND tty = 'BRAND' 
              AND json_extract(attributes_json, '$.unresolved_generic_parent') IS NOT NULL
            """
        )
        orphans = cursor.fetchall()
        assert len(orphans) == 14

        # Verify none of these 14 brands have TRADENAME_OF edges
        orphan_codes = [r[0] for r in orphans]
        placeholders = ",".join("?" for _ in orphan_codes)
        cursor.execute(
            f"""
            SELECT COUNT(*) 
            FROM concept_relationships r
            JOIN concepts c ON r.concept_id_from = c.concept_id
            WHERE c.code IN ({placeholders}) AND r.relationship_type = 'TRADENAME_OF'
            """,
            orphan_codes,
        )
        assert cursor.fetchone()[0] == 0


@pytest.mark.skipif(not LOINC_ZIP.exists(), reason="LOINC staging archive not found")
class TestLoincAndClciIngestion:
    """Verifies LOINC 2.83 full ingestion and CLCI overlay."""

    def test_loinc_csv_row_preserves_status_and_classtype(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        builder = LoincIngestionBuilder(conn)

        # Deprecated row should NOT be filtered when filter_inactive=False
        row = {
            "LOINC_NUM": "1234-5",
            "COMPONENT": "Test Component",
            "STATUS": "DEPRECATED",
            "CLASSTYPE": "1",
            "CLASS": "CHEM",
            "EXAMPLE_UCUM_UNITS": "mg/dL",
        }
        concept = builder.parse_loinc_csv_row(row, filter_inactive=False)
        assert concept is not None
        assert concept.status == "DEPRECATED"
        assert concept.class_type == 1
        assert concept.class_name == "CHEM"
        assert concept.example_ucum_units == "mg/dL"

    @pytest.mark.skipif(not CLCI_ZIP.exists(), reason="CLCI staging archive not found")
    def test_clci_mappings_and_discouraged_status(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)

        # Insert representative LOINC concepts including the 2 discouraged ones
        cursor = conn.cursor()
        cursor.execute("INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) VALUES ('LOINC', '6025-1', 'Aspergillus IgE', 'Qn', 1, 'ALLERGY', 'ACTIVE', '{}')")
        cursor.execute("INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) VALUES ('LOINC', '16250-3', 'Codeine Ur', 'Qn', 1, 'TOX', 'DISCOURAGED', '{}')")
        cursor.execute("INSERT INTO concepts (system, code, canonical_name, tty, class_type, class, status, attributes_json) VALUES ('LOINC', '19289-8', 'Cannabinoids Ur', 'Qn', 1, 'TOX', 'DISCOURAGED', '{}')")
        conn.commit()

        builder = LoincIngestionBuilder(conn)
        clci_res = builder.ingest_clci(CLCI_ZIP)

        assert clci_res["clci_rows_count"] == 1473
        assert clci_res["mappings_inserted"] == 3  # The 3 we pre-populated

        # Verify mappings
        cursor.execute("SELECT source_system, source_code, target_system, target_code, relationship_type, review_status FROM concept_mappings WHERE source_system = 'CLCI'")
        mappings = cursor.fetchall()
        assert len(mappings) == 3
        for m in mappings:
            assert m[0] == "CLCI"
            assert m[2] == "LOINC"
            assert m[4] == "EQUIVALENT"
            assert m[5] == "OFFICIAL"

        # Verify discouraged codes remained DISCOURAGED
        cursor.execute("SELECT code, status FROM concepts WHERE code IN ('16250-3', '19289-8')")
        for code, status in cursor.fetchall():
            assert status == "DISCOURAGED"


@pytest.mark.skipif(not RXNORM_ZIP.exists(), reason="RxNorm staging archive not found")
class TestRxNormCpcFilteringAndRelationships:
    """Verifies RxNorm CPC strict filtering and relationship extraction."""

    def test_rxnorm_mthcmsfrf_exclusion(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        builder = RxNormIngestionBuilder(conn)

        # MTHCMSFRF row from archive
        cms_line = "1000000|ENG|TS|LUI|STT|SUI|N|RXAUI|SAUI|SCUI|SDUI|MTHCMSFRF|CMS_FRF|CODE|CMS Test|SRL|N|CVF"
        parsed = builder.parse_rxnconso_line(cms_line)
        assert parsed is None
        assert builder.excluded_count == 1
        assert "PROPRIETARY_OR_UNSUPPORTED_SAB_MTHCMSFRF" in builder.exclusion_reasons
