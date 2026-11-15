"""Phase 1 Unit and Integration Tests for Offline Terminology Database.

Tests schema creation, LOINC 6-axis parsing, RxNorm CPC filtering, FTS5 external-content
indexing, integrity validation, deterministic reproducibility, and atomic pointer publishing.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, Any

import pytest

from clindoc.terminology.compiler import (
    TerminologyDatabaseCompiler,
    compute_file_sha256,
    load_active_terminology_pointer,
)
from clindoc.terminology.loinc_builder import (
    LOINC_COPYRIGHT_NOTICE,
    LoincIngestionBuilder,
    ParsedLoincConcept,
    normalize_alias_text,
)
from clindoc.terminology.rxnorm_builder import (
    RXNORM_ATTRIBUTION_NOTICE,
    SUPPORTED_CPC_SABS,
    SUPPORTED_CPC_TTYS,
    RxNormIngestionBuilder,
    normalize_rx_text,
)
from clindoc.terminology.schema import (
    FTS5_DDL,
    SCHEMA_DDL,
    TERMINOLOGY_SCHEMA_VERSION,
)


@pytest.fixture
def memory_db() -> sqlite3.Connection:
    """Provides an in-memory SQLite database initialized with the terminology schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_DDL)
    conn.executescript(FTS5_DDL)
    yield conn
    conn.close()


class TestTerminologySchema:
    """Verifies relational schema and FTS5 external-content tables."""

    def test_schema_tables_exist(self, memory_db: sqlite3.Connection):
        cursor = memory_db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}

        assert "concepts" in tables
        assert "concept_aliases" in tables
        assert "alias_context_rules" in tables
        assert "terminology_metadata" in tables
        assert "fts_aliases_unicode" in tables
        assert "fts_aliases_trigram" in tables

    def test_foreign_key_enforcement(self, memory_db: sqlite3.Connection):
        cursor = memory_db.cursor()
        # Inserting alias for nonexistent concept must fail with FK violation
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO concept_aliases (concept_id, alias_text, alias_source)
                VALUES (9999, 'orphan alias', 'TEST')
                """
            )

    def test_unique_concept_constraint(self, memory_db: sqlite3.Connection):
        cursor = memory_db.cursor()
        cursor.execute(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, attributes_json)
            VALUES ('LOINC', '1234-5', 'Test Concept', 'Qn', '{}')
            """
        )
        # Re-inserting same system and code must fail
        with pytest.raises(sqlite3.IntegrityError):
            cursor.execute(
                """
                INSERT INTO concepts (system, code, canonical_name, tty, attributes_json)
                VALUES ('LOINC', '1234-5', 'Duplicate Concept', 'Qn', '{}')
                """
            )


class TestLoincIngestion:
    """Verifies LOINC 6-axis parsing, synonym extraction, and rule generation."""

    def test_parse_loinc_csv_row_full(self, memory_db: sqlite3.Connection):
        builder = LoincIngestionBuilder(memory_db)
        row = {
            "LOINC_NUM": "2823-3",
            "COMPONENT": "Potassium",
            "PROPERTY": "SCnc",
            "TIME_ASPCT": "Pt",
            "SYSTEM": "Ser/Plas",
            "SCALE_TYP": "Qn",
            "METHOD_TYP": "",
            "STATUS": "ACTIVE",
            "LONG_COMMON_NAME": "Potassium [Moles/volume] in Serum or Plasma",
            "SHORTNAME": "K SerPl-sCnc",
            "RELATEDNAMES2": "K; Potass; Kalium",
            "EXAMPLE_UNITS": "mmol/L",
            "EXTERNAL_COPYRIGHT_NOTICE": "LOINC copyright Regenstrief",
        }
        concept = builder.parse_loinc_csv_row(row)
        assert concept is not None
        assert concept.code == "2823-3"
        assert concept.canonical_name == "Potassium [Moles/volume] in Serum or Plasma"
        assert concept.component == "Potassium"
        assert concept.system == "Ser/Plas"
        assert concept.scale == "Qn"
        assert concept.default_unit == "mmol/L"

        # Check aliases
        alias_texts = [a[0] for a in concept.aliases]
        assert "potassium [moles/volume] in serum or plasma" in alias_texts
        assert "potassium" in alias_texts
        assert "k serpl-scnc" in alias_texts
        assert "kalium" in alias_texts

        # Check rules
        rules_dims = [r[0] for r in concept.rules]
        assert "SPECIMEN" in rules_dims
        assert "SCALE" in rules_dims

    def test_inactive_loinc_filtering(self, memory_db: sqlite3.Connection):
        builder = LoincIngestionBuilder(memory_db)
        deprecated_row = {
            "LOINC_NUM": "9999-9",
            "STATUS": "DEPRECATED",
            "COMPONENT": "Old Test",
        }
        concept = builder.parse_loinc_csv_row(deprecated_row)
        assert concept is None
        assert builder.excluded_count == 1
        assert "INACTIVE_STATUS_DEPRECATED" in builder.exclusion_reasons

    def test_loinc_insert_and_idempotency(self, memory_db: sqlite3.Connection):
        builder = LoincIngestionBuilder(memory_db)
        row = {
            "LOINC_NUM": "718-7",
            "COMPONENT": "Hemoglobin",
            "PROPERTY": "MCnc",
            "TIME_ASPCT": "Pt",
            "SYSTEM": "Bld",
            "SCALE_TYP": "Qn",
            "STATUS": "ACTIVE",
            "LONG_COMMON_NAME": "Hemoglobin [Mass/volume] in Blood",
            "SHORTNAME": "Hgb Bld-mCnc",
        }
        concept = builder.parse_loinc_csv_row(row)
        concept_id_1 = builder.insert_concept(concept)
        assert concept_id_1 > 0
        assert builder.concepts_inserted == 1

        # Second insert of same concept should merge aliases idempotently
        concept_id_2 = builder.insert_concept(concept)
        assert concept_id_2 == concept_id_1
        assert builder.concepts_inserted == 1  # Not incremented


class TestRxNormCpcIngestion:
    """Verifies RxNorm CPC filtering, proprietary exclusion, and TTY validation."""

    def test_parse_valid_cpc_line(self, memory_db: sqlite3.Connection):
        builder = RxNormIngestionBuilder(memory_db)
        # Valid RXNORM SAB, IN TTY
        line_rxnorm = "723|ENG|TS|LUI|STT|SUI|Y|RXAUI|SAUI|SCUI|SDUI|RXNORM|IN|723|Amoxicillin|SRL|N|CVF"
        parsed_rx = builder.parse_rxnconso_line(line_rxnorm)
        assert parsed_rx is not None
        rxcui, sab, tty, str_name, suppress, is_pref = parsed_rx
        assert rxcui == "723"
        assert sab == "RXNORM"
        assert tty == "IN"
        assert str_name == "Amoxicillin"
        assert is_pref == "Y"

        # Valid MTHSPL SAB, SBD TTY (FDA Structured Product Labeling brand)
        line_mthspl = "213169|ENG|TS|LUI|STT|SUI|N|RXAUI|SAUI|SCUI|SDUI|MTHSPL|SBD|213169|Amoxil 500 MG Oral Capsule|SRL|N|CVF"
        parsed_mth = builder.parse_rxnconso_line(line_mthspl)
        assert parsed_mth is not None
        assert parsed_mth[1] == "MTHSPL"
        assert parsed_mth[2] == "SBD"

    def test_usp_sab_rejected(self, memory_db: sqlite3.Connection):
        """USP compendial entries must be rejected to enforce strict pure CPC scope."""
        builder = RxNormIngestionBuilder(memory_db)
        line_usp = "723|ENG|TS|LUI|STT|SUI|N|RXAUI|SAUI|SCUI|SDUI|USP|IN|723|Amoxicillin USP|SRL|N|CVF"
        parsed = builder.parse_rxnconso_line(line_usp)
        assert parsed is None
        assert builder.excluded_count == 1
        assert "PROPRIETARY_OR_UNSUPPORTED_SAB_USP" in builder.exclusion_reasons

    def test_proprietary_sab_filtering(self, memory_db: sqlite3.Connection):
        builder = RxNormIngestionBuilder(memory_db)
        # Third-party proprietary commercial SAB (e.g., NDDF, MSH, VANDF)
        line = "723|ENG|TS|LUI|STT|SUI|Y|RXAUI|SAUI|SCUI|SDUI|NDDF|IN|723|Amoxicillin|SRL|N|CVF"
        parsed = builder.parse_rxnconso_line(line)
        assert parsed is None
        assert builder.excluded_count == 1
        assert "PROPRIETARY_OR_UNSUPPORTED_SAB_NDDF" in builder.exclusion_reasons

    def test_unsupported_tty_filtering(self, memory_db: sqlite3.Connection):
        builder = RxNormIngestionBuilder(memory_db)
        # Unsupported TTY (e.g. SY synonym, or PSN)
        line = "723|ENG|TS|LUI|STT|SUI|Y|RXAUI|SAUI|SCUI|SDUI|RXNORM|SY|723|Amoxicillin Syn|SRL|N|CVF"
        parsed = builder.parse_rxnconso_line(line)
        assert parsed is None
        assert builder.excluded_count == 1
        assert "UNSUPPORTED_TTY_SY" in builder.exclusion_reasons

    def test_suppressed_line_filtering(self, memory_db: sqlite3.Connection):
        builder = RxNormIngestionBuilder(memory_db)
        # Suppressed line with SUPPRESS='O'
        line = "723|ENG|TS|LUI|STT|SUI|Y|RXAUI|SAUI|SCUI|SDUI|RXNORM|IN|723|Amoxicillin|SRL|O|CVF"
        parsed = builder.parse_rxnconso_line(line)
        assert parsed is None
        assert builder.excluded_count == 1
        assert "SUPPRESSED_ROW_O" in builder.exclusion_reasons


class TestFts5IndexingAndIntegrity:
    """Verifies FTS5 external content synchronization, search, and integrity."""

    def test_fts5_sync_and_query(self, memory_db: sqlite3.Connection):
        cursor = memory_db.cursor()
        # Insert concept and alias
        cursor.execute(
            """
            INSERT INTO concepts (system, code, canonical_name, tty, attributes_json)
            VALUES ('LOINC', '2823-3', 'Potassium in Serum', 'Qn', '{}')
            """
        )
        cid = cursor.lastrowid
        cursor.execute(
            """
            INSERT INTO concept_aliases (concept_id, alias_text, alias_source, is_abbreviation, is_preferred)
            VALUES (?, 'potassium serum', 'LOINC_CORE', 0, 1)
            """,
            (cid,),
        )
        aid = cursor.lastrowid

        # Populate FTS external content
        cursor.execute("INSERT INTO fts_aliases_unicode(rowid, alias_text) VALUES (?, ?)", (aid, 'potassium serum'))
        cursor.execute("INSERT INTO fts_aliases_trigram(rowid, alias_text) VALUES (?, ?)", (aid, 'potassium serum'))
        memory_db.commit()

        # Unicode query
        cursor.execute("SELECT rowid FROM fts_aliases_unicode WHERE fts_aliases_unicode MATCH 'potassium'")
        u_results = cursor.fetchall()
        assert len(u_results) == 1
        assert u_results[0][0] == aid

        # Trigram substring query (e.g. 'tassi')
        cursor.execute("SELECT rowid FROM fts_aliases_trigram WHERE fts_aliases_trigram MATCH 'tassi'")
        t_results = cursor.fetchall()
        assert len(t_results) == 1
        assert t_results[0][0] == aid

        # Integrity checks
        cursor.execute("INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('integrity-check');")
        cursor.execute("INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('integrity-check');")


class TestCompilerEndToEndAndReproducibility:
    """Verifies database compilation, reproducibility, and versioned pointer publishing."""

    def test_compiler_build_from_curated_subsets(self, tmp_path: Path):
        compiler = TerminologyDatabaseCompiler(
            output_dir=tmp_path,
            version_tag="20261112_test",
        )
        loinc_json = Path("configs/loinc_subset.json")
        med_json = Path("configs/medication_subset.json")

        assert loinc_json.exists(), "configs/loinc_subset.json missing"
        assert med_json.exists(), "configs/medication_subset.json missing"

        res = compiler.compile(
            loinc_json_path=loinc_json,
            rxnorm_json_path=med_json,
            loinc_release_id="LOINC_2.78_SUBSET",
            rxnorm_release_id="RxNorm_CPC_20261102_SUBSET",
            commit_sha="test_commit_sha",
        )

        assert res.success is True
        assert res.integrity_ok is True
        assert res.concepts_count > 0
        assert res.aliases_count >= res.concepts_count
        assert res.fts_unicode_count == res.aliases_count
        assert res.fts_trigram_count == res.aliases_count
        assert res.database_path.exists()
        assert res.pointer_path is not None and res.pointer_path.exists()

        # Verify pointer loading
        pointer_data = load_active_terminology_pointer(tmp_path)
        assert pointer_data is not None
        assert pointer_data["schema_version"] == TERMINOLOGY_SCHEMA_VERSION
        assert pointer_data["version_tag"] == "20261112_test"
        assert pointer_data["sha256"] == res.db_sha256
        assert pointer_data["concepts_count"] == res.concepts_count

        # Inspect database contents
        conn = sqlite3.connect(str(res.database_path))
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM terminology_metadata")
        meta = dict(cursor.fetchall())
        conn.close()

        assert meta["schema_version"] == TERMINOLOGY_SCHEMA_VERSION
        assert "LOINC" in meta["loinc_copyright_notice"]
        assert "NLM" in meta["rxnorm_attribution_notice"]

    def test_deterministic_reproducibility(self, tmp_path: Path):
        """Two compilations from identical source inputs must produce identical row counts and data hashes."""
        loinc_json = Path("configs/loinc_subset.json")
        med_json = Path("configs/medication_subset.json")

        dir1 = tmp_path / "build1"
        dir2 = tmp_path / "build2"

        c1 = TerminologyDatabaseCompiler(dir1, version_tag="repro1")
        res1 = c1.compile(loinc_json_path=loinc_json, rxnorm_json_path=med_json)

        c2 = TerminologyDatabaseCompiler(dir2, version_tag="repro2")
        res2 = c2.compile(loinc_json_path=loinc_json, rxnorm_json_path=med_json)

        # Check identical content metrics
        assert res1.concepts_count == res2.concepts_count
        assert res1.aliases_count == res2.aliases_count
        assert res1.rules_count == res2.rules_count
        assert res1.fts_unicode_count == res2.fts_unicode_count
        assert res1.fts_trigram_count == res2.fts_trigram_count

        # Check exact concept table row contents
        conn1 = sqlite3.connect(str(res1.database_path))
        conn2 = sqlite3.connect(str(res2.database_path))

        rows1 = conn1.execute("SELECT system, code, canonical_name, tty FROM concepts ORDER BY system, code").fetchall()
        rows2 = conn2.execute("SELECT system, code, canonical_name, tty FROM concepts ORDER BY system, code").fetchall()
        assert rows1 == rows2

        aliases1 = conn1.execute("SELECT concept_id, alias_text, alias_source FROM concept_aliases ORDER BY alias_id").fetchall()
        aliases2 = conn2.execute("SELECT concept_id, alias_text, alias_source FROM concept_aliases ORDER BY alias_id").fetchall()
        assert aliases1 == aliases2

        conn1.close()
        conn2.close()

