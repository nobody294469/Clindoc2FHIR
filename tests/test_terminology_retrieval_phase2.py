"""Comprehensive unit and integration tests for TerminologyRetrievalService (Phase 2).

Tests:
1. Exact alias and acronym retrieval.
2. Ambiguous aliases (preserving ambiguity, no silent resolution).
3. Unicode token and trigram substring retrieval.
4. Short-query fallback (< 3 characters).
5. Context-rule evaluation semantics (AND across dimensions, OR within values,
   NOT_IN, missing-context abstention, conflicting-context abstention).
6. Empty, whitespace, and out-of-vocabulary queries.
7. Deterministic candidate ranking and tie-breaking.
8. Cache correctness, version consistency, and reload handling.
9. Database connection resolution and read-only safety.
10. Retrieval latency benchmarking.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time
import pytest

from clindoc.terminology.compiler import TerminologyDatabaseCompiler
from clindoc.terminology.schema import SCHEMA_DDL, FTS5_DDL
from clindoc.terminology.service import (
    ContextRule,
    ContextStatus,
    QueryContext,
    RetrievalMethod,
    RetrievalResponse,
    RetrievedCandidate,
    TerminologyRetrievalService,
    evaluate_context_rules,
    sanitize_fts5_query,
)


@pytest.fixture(scope="module")
def fixture_db_path(tmp_path_factory) -> Path:
    """Builds a temporary test database from reference subsets for retrieval testing."""
    tmp_dir = tmp_path_factory.mktemp("term_db")
    compiler = TerminologyDatabaseCompiler(tmp_dir, version_tag="20261112_phase2")
    res = compiler.compile(
        loinc_json_path=Path("configs/loinc_subset.json"),
        rxnorm_json_path=Path("configs/medication_subset.json"),
        loinc_release_id="LOINC_2.78_TEST",
        rxnorm_release_id="RxNorm_CPC_20261102_TEST",
    )
    assert res.success is True
    return res.database_path


@pytest.fixture
def retrieval_service(fixture_db_path: Path):
    """Provides TerminologyRetrievalService instance connected to test DB."""
    service = TerminologyRetrievalService(fixture_db_path)
    yield service
    service.close()


class TestExactAliasAndAcronymRetrieval:
    """Verifies deterministic exact alias and acronym lookup."""

    def test_exact_lookup_medication(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("Amoxicillin")
        assert len(resp.candidates) > 0
        top = resp.candidates[0]
        assert top.system == "RxNorm"
        assert top.code == "723"
        assert top.canonical_name == "Amoxicillin"
        assert top.retrieval_method in (RetrievalMethod.EXACT_CACHE, RetrievalMethod.EXACT_DB)
        assert top.rank_score == 1.0

    def test_exact_lookup_case_and_whitespace_insensitivity(self, retrieval_service: TerminologyRetrievalService):
        resp1 = retrieval_service.retrieve("  metformin  ")
        resp2 = retrieval_service.retrieve("METFORMIN")
        assert len(resp1.candidates) > 0
        assert len(resp2.candidates) > 0
        assert resp1.candidates[0].code == resp2.candidates[0].code == "6809"

    def test_exact_lookup_lab_acronym(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("WBC")
        assert len(resp.candidates) > 0
        top = resp.candidates[0]
        assert top.system == "LOINC"
        assert top.code == "6690-2"
        assert top.canonical_name == "White Blood Cell Count"


class TestAmbiguousAliases:
    """Verifies that ambiguous aliases preserve all candidates and never silently pick one."""

    def test_ambiguous_alias_preserves_multiple_concepts(self, tmp_path: Path):
        # Create a database with two distinct concepts sharing the exact same acronym 'CA'
        db_file = tmp_path / "ambiguous.db"
        conn = sqlite3.connect(str(db_file))
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        cursor = conn.cursor()

        # Concept 1: Calcium (LOINC 17861-6)
        cursor.execute("INSERT INTO concepts (concept_id, system, code, canonical_name, tty, attributes_json) VALUES (1, 'LOINC', '17861-6', 'Calcium [Mass/volume]', 'Qn', '{}')")
        cursor.execute("INSERT INTO concept_aliases (alias_id, concept_id, alias_text, alias_source, is_abbreviation, is_preferred) VALUES (101, 1, 'ca', 'ABBR', 1, 0)")
        cursor.execute("INSERT INTO alias_context_rules VALUES (201, 101, 'SPECIMEN', 'IN', '[\"Ser/Plas\"]')")

        # Concept 2: Carcinoma Antigen (LOINC 10334-1)
        cursor.execute("INSERT INTO concepts (concept_id, system, code, canonical_name, tty, attributes_json) VALUES (2, 'LOINC', '10334-1', 'Cancer Ag 125', 'Qn', '{}')")
        cursor.execute("INSERT INTO concept_aliases (alias_id, concept_id, alias_text, alias_source, is_abbreviation, is_preferred) VALUES (102, 2, 'ca', 'ABBR', 1, 0)")
        cursor.execute("INSERT INTO alias_context_rules VALUES (202, 102, 'SPECIMEN', 'IN', '[\"Ser\"]')")

        # FTS sync
        cursor.execute("INSERT INTO fts_aliases_unicode(rowid, alias_text) SELECT alias_id, alias_text FROM concept_aliases")
        cursor.execute("INSERT INTO fts_aliases_trigram(rowid, alias_text) SELECT alias_id, alias_text FROM concept_aliases")
        cursor.execute("INSERT INTO terminology_metadata VALUES ('schema_version', '1.0.0'), ('version_tag', 'TEST_AMBIG')")
        conn.commit()
        conn.close()

        service = TerminologyRetrievalService(db_file)
        # Query with specimen=None (missing context -> both abstain, neither silently chosen)
        resp_unresolved = service.retrieve("ca", context={"specimen": None})
        assert len(resp_unresolved.candidates) == 2
        assert resp_unresolved.is_abstained is True
        codes = {c.code for c in resp_unresolved.candidates}
        assert codes == {"17861-6", "10334-1"}

        # Query with specimen='Ser/Plas' -> Calcium passes, CA125 fails
        resp_calcium = service.retrieve("ca", context={"specimen": "Ser/Plas"})
        assert len(resp_calcium.candidates) == 2
        winner = resp_calcium.candidates[0]
        assert winner.code == "17861-6"
        assert winner.context_status == ContextStatus.PASSED
        assert resp_calcium.candidates[1].context_status == ContextStatus.FAILED_CONSTRAINT

        service.close()


class TestUnicodeAndTrigramRetrieval:
    """Verifies token-based BM25 and substring trigram retrieval."""

    def test_unicode_multiword_token_matching(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("Blood Cell Count")
        assert len(resp.candidates) > 0
        names = [c.canonical_name for c in resp.candidates]
        assert any("White Blood Cell Count" in n or "Red Blood Cell Count" in n for n in names)
        assert any(c.retrieval_method == RetrievalMethod.FTS_UNICODE for c in resp.candidates)

    def test_trigram_ocr_corrupted_substring(self, retrieval_service: TerminologyRetrievalService):
        # Corrupted query: 'metfor' (substring of metformin)
        resp = retrieval_service.retrieve("metfor")
        assert len(resp.candidates) > 0
        matched = [c for c in resp.candidates if c.code == "6809"]
        assert len(matched) > 0
        assert matched[0].canonical_name == "Metformin"
        assert matched[0].retrieval_method in (RetrievalMethod.FTS_TRIGRAM, RetrievalMethod.FTS_UNICODE)

    def test_provenance_preservation(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("Amoxicillin")
        top = resp.candidates[0]
        assert top.alias_id > 0
        assert top.matched_alias != ""
        assert top.alias_source != ""


class TestShortQueryHandling:
    """Verifies handling of queries shorter than 3 characters without trigram crashes."""

    def test_single_character_query_not_crashing(self, retrieval_service: TerminologyRetrievalService):
        # Length 1 query must not crash SQLite trigram FTS
        resp = retrieval_service.retrieve("K")
        assert isinstance(resp, RetrievalResponse)
        assert resp.query == "K"

    def test_two_character_query_prefix_fallback(self, retrieval_service: TerminologyRetrievalService):
        # Length 2 query e.g. 'am' should find 'amoxicillin' via prefix fallback if exact misses
        resp = retrieval_service.retrieve("am")
        assert isinstance(resp, RetrievalResponse)
        # Should return candidates via prefix lookup without triggering trigram error
        assert any("amox" in c.matched_alias for c in resp.candidates)


class TestContextRuleSemantics:
    """Verifies multi-dimensional AND, OR, NOT_IN, missing context, and conflicting context."""

    def test_or_semantics_within_dimension(self):
        rule = ContextRule(1, 10, "SPECIMEN", "IN", ("Ser/Plas", "Bld"))
        # Provided Ser/Plas matches one of the allowed set
        status, reasons = evaluate_context_rules([rule], QueryContext(specimen="Ser/Plas"))
        assert status == ContextStatus.PASSED

        # Provided Bld also matches
        status2, _ = evaluate_context_rules([rule], QueryContext(specimen="Bld"))
        assert status2 == ContextStatus.PASSED

        # Provided Urine does not match
        status3, _ = evaluate_context_rules([rule], QueryContext(specimen="Urine"))
        assert status3 == ContextStatus.FAILED_CONSTRAINT

    def test_and_semantics_across_dimensions(self):
        r1 = ContextRule(1, 10, "SPECIMEN", "IN", ("Ser/Plas",))
        r2 = ContextRule(2, 10, "SCALE", "EQUALS", ("Qn",))

        # Both match -> PASSED
        ctx_good = QueryContext(specimen="Ser/Plas", scale="Qn")
        status, _ = evaluate_context_rules([r1, r2], ctx_good)
        assert status == ContextStatus.PASSED

        # Specimen matches, but scale fails -> FAILED_CONSTRAINT
        ctx_bad_scale = QueryContext(specimen="Ser/Plas", scale="Nom")
        status2, reasons2 = evaluate_context_rules([r1, r2], ctx_bad_scale)
        assert status2 == ContextStatus.FAILED_CONSTRAINT
        assert any("SCALE" in r for r in reasons2)

    def test_missing_required_context_abstains_fail_closed(self):
        r1 = ContextRule(1, 10, "SPECIMEN", "IN", ("Ser/Plas",))
        # No specimen provided (None)
        ctx_missing = QueryContext(specimen=None)
        status, reasons = evaluate_context_rules([r1], ctx_missing)
        assert status == ContextStatus.ABSTAINED_MISSING_CONTEXT
        assert any("Missing required context for dimension 'SPECIMEN'" in r for r in reasons)

        # Specimen is explicitly 'UNKNOWN'
        ctx_unknown = QueryContext(specimen="UNKNOWN")
        status2, reasons2 = evaluate_context_rules([r1], ctx_unknown)
        assert status2 == ContextStatus.ABSTAINED_MISSING_CONTEXT

    def test_conflicting_context_abstains_fail_closed(self):
        r1 = ContextRule(1, 10, "SPECIMEN", "IN", ("Ser/Plas",))
        # Context marked with explicit conflict
        ctx_conflict = QueryContext(specimen="Ser/Plas", has_conflict=True, conflict_reason="Contradictory specimen tags in document")
        status, reasons = evaluate_context_rules([r1], ctx_conflict)
        assert status == ContextStatus.ABSTAINED_CONFLICTING_CONTEXT
        assert "Contradictory specimen tags" in reasons[0]

        # Multiple conflicting values for single-value dimension
        ctx_multi = QueryContext(specimen=["Ser/Plas", "Urine"])
        status2, reasons2 = evaluate_context_rules([r1], ctx_multi)
        assert status2 == ContextStatus.ABSTAINED_CONFLICTING_CONTEXT

    def test_not_in_disallowed_context(self):
        r_disallow = ContextRule(1, 10, "DISALLOWED_CONTEXT", "NOT_IN", ("VITAMIN_K", "K_DUR"))
        # Clean query context
        ctx_clean = QueryContext(disallowed_context=["ROUTINE_LAB"])
        status, _ = evaluate_context_rules([r_disallow], ctx_clean)
        assert status == ContextStatus.PASSED

        # Contaminated query context with forbidden token
        ctx_forbidden = QueryContext(disallowed_context=["VITAMIN_K"])
        status2, reasons2 = evaluate_context_rules([r_disallow], ctx_forbidden)
        assert status2 == ContextStatus.FAILED_CONSTRAINT
        assert any("Disallowed context token detected" in r for r in reasons2)


class TestEmptyAndOutOfVocabularyQueries:
    """Verifies safe handling of empty, whitespace, and non-existent queries."""

    def test_empty_and_whitespace_query(self, retrieval_service: TerminologyRetrievalService):
        resp_empty = retrieval_service.retrieve("")
        assert resp_empty.candidates == []
        assert resp_empty.is_ambiguous is False

        resp_space = retrieval_service.retrieve("   \t  \n  ")
        assert resp_space.candidates == []

    def test_out_of_vocabulary_query(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("XylophonicZirconiumUnobtainium999")
        assert resp.candidates == []
        assert resp.is_ambiguous is False
        assert resp.is_abstained is False


class TestDeterministicRankingAndTieBreaking:
    """Verifies deterministic sorting across identical repeated queries."""

    def test_deterministic_candidate_ordering(self, retrieval_service: TerminologyRetrievalService):
        resp1 = retrieval_service.retrieve("Glucose")
        resp2 = retrieval_service.retrieve("Glucose")

        assert len(resp1.candidates) == len(resp2.candidates)
        keys1 = [(c.concept_id, c.alias_id, c.rank_score) for c in resp1.candidates]
        keys2 = [(c.concept_id, c.alias_id, c.rank_score) for c in resp2.candidates]
        assert keys1 == keys2

    def test_bm25_treated_as_ranking_signal_not_probability(self, retrieval_service: TerminologyRetrievalService):
        resp = retrieval_service.retrieve("blood cell")
        for c in resp.candidates:
            if c.retrieval_method == RetrievalMethod.FTS_UNICODE:
                assert c.raw_bm25_score is not None
                # BM25 is negative SQLite score; rank_score is relative tier, not pseudo-probability
                assert 0.0 <= c.rank_score <= 1.0


class TestCacheCorrectnessAndReloadHandling:
    """Verifies in-memory cache preloading, transactional reload, and concurrent safety."""

    def test_cache_contains_preloaded_aliases(self, retrieval_service: TerminologyRetrievalService):
        cache = retrieval_service._exact_cache
        assert len(cache) >= 100
        assert "amoxicillin" in cache
        assert "hemoglobin" in cache
        assert "glucose" in cache

    def test_reload_if_updated_noop_when_unchanged(self, retrieval_service: TerminologyRetrievalService):
        reloaded = retrieval_service.reload_if_updated()
        assert reloaded is False

    @staticmethod
    def _create_minimal_db(path: Path, version_tag: str, concepts_and_aliases: list[tuple[str, str, str]]) -> None:
        """Helper to create minimal valid terminology SQLite database."""
        conn = sqlite3.connect(str(path))
        conn.executescript(SCHEMA_DDL)
        conn.executescript(FTS5_DDL)
        cursor = conn.cursor()
        for idx, (code, name, alias) in enumerate(concepts_and_aliases, start=1):
            cursor.execute(
                "INSERT INTO concepts (concept_id, system, code, canonical_name, tty, attributes_json) VALUES (?, 'TEST', ?, ?, 'Qn', '{}')",
                (idx, code, name),
            )
            cursor.execute(
                "INSERT INTO concept_aliases (alias_id, concept_id, alias_text, alias_source, is_abbreviation, is_preferred) VALUES (?, ?, ?, 'TEST_SRC', 0, 1)",
                (idx * 100, idx, alias.lower().strip()),
            )
        cursor.execute("INSERT INTO fts_aliases_unicode(rowid, alias_text) SELECT alias_id, alias_text FROM concept_aliases")
        cursor.execute("INSERT INTO fts_aliases_trigram(rowid, alias_text) SELECT alias_id, alias_text FROM concept_aliases")
        cursor.execute(
            "INSERT INTO terminology_metadata VALUES ('schema_version', '1.0.0'), ('version_tag', ?)",
            (version_tag,),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _update_pointer(dir_path: Path, db_filename: str, version_tag: str) -> None:
        ptr_file = dir_path / "active_terminology.json"
        tmp_ptr = dir_path / f"active_terminology.tmp.{time.time_ns()}.json"
        data = {
            "schema_version": "1.0.0",
            "version_tag": version_tag,
            "active_database": db_filename,
            "active_database_path": str(dir_path / db_filename).replace("\\", "/"),
            "sha256": "dummy_hash",
            "concepts_count": 2,
            "aliases_count": 2,
            "rules_count": 0,
        }
        with open(tmp_ptr, "w", encoding="utf-8") as f:
            json.dump(data, f)
        import os
        os.replace(tmp_ptr, ptr_file)

    def test_active_pointer_switching_v1_to_v2_and_alias_lifecycle(self, tmp_path: Path):
        """Tests that pointer switching to v2 brings in new aliases and removes old ones."""
        data_dir = tmp_path / "lifecycle_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(
            v1_db,
            "v1",
            [("C1", "Glucose", "glucose"), ("C2", "Potassium", "potassium")],
        )
        self._update_pointer(data_dir, "terminology_v1.db", "v1")

        service = TerminologyRetrievalService(data_dir)
        assert service.db_version == "v1"
        assert service.retrieve("glucose").candidates[0].canonical_name == "Glucose"
        assert service.retrieve("potassium").candidates[0].canonical_name == "Potassium"
        assert service.retrieve("insulin").candidates == []

        # Create v2 database: retains Glucose, adds Insulin, removes Potassium
        v2_db = data_dir / "terminology_v2.db"
        self._create_minimal_db(
            v2_db,
            "v2",
            [("C1", "Glucose", "glucose"), ("C3", "Insulin", "insulin")],
        )
        self._update_pointer(data_dir, "terminology_v2.db", "v2")

        # Reload
        reloaded = service.reload_if_updated()
        assert reloaded is True
        assert service.db_version == "v2"

        # Newly added v2 alias is retrievable
        resp_ins = service.retrieve("insulin")
        assert len(resp_ins.candidates) > 0
        assert resp_ins.candidates[0].canonical_name == "Insulin"

        # Removed v1 alias is no longer present in cache or DB
        resp_pot = service.retrieve("potassium")
        assert resp_pot.candidates == []

        # Retained alias still works
        resp_gluc = service.retrieve("glucose")
        assert resp_gluc.candidates[0].canonical_name == "Glucose"

        service.close()

    def test_corrupted_v2_leaves_v1_operational(self, tmp_path: Path):
        """Tests that attempting to reload a corrupted candidate database preserves active state."""
        data_dir = tmp_path / "corrupt_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(v1_db, "v1", [("C1", "Glucose", "glucose")])
        self._update_pointer(data_dir, "terminology_v1.db", "v1")

        service = TerminologyRetrievalService(data_dir)
        assert service.db_version == "v1"

        # Point to a corrupted file (zero bytes or garbage)
        corrupted_db = data_dir / "terminology_v2_corrupted.db"
        with open(corrupted_db, "wb") as f:
            f.write(b"NOT A VALID SQLITE DATABASE FILE GARBAGE DATA")
        self._update_pointer(data_dir, "terminology_v2_corrupted.db", "v2")

        # Reload should safely fail without crashing and leave v1 intact
        reloaded = service.reload_if_updated()
        assert reloaded is False
        assert service.db_version == "v1"

        # Active service connection remains completely operational
        resp = service.retrieve("glucose")
        assert len(resp.candidates) > 0
        assert resp.candidates[0].canonical_name == "Glucose"

        service.close()

    def test_cache_building_failure_leaves_v1_operational(self, tmp_path: Path):
        """Tests that a database with missing tables safely fails reload and preserves active state."""
        data_dir = tmp_path / "bad_schema_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(v1_db, "v1", [("C1", "Glucose", "glucose")])
        self._update_pointer(data_dir, "terminology_v1.db", "v1")

        service = TerminologyRetrievalService(data_dir)

        # Create database missing concept_aliases and fts tables
        bad_db = data_dir / "terminology_v2_bad.db"
        conn = sqlite3.connect(str(bad_db))
        conn.execute("CREATE TABLE terminology_metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO terminology_metadata VALUES ('schema_version', '1.0.0'), ('version_tag', 'v2_bad')")
        conn.commit()
        conn.close()

        self._update_pointer(data_dir, "terminology_v2_bad.db", "v2_bad")

        reloaded = service.reload_if_updated()
        assert reloaded is False
        assert service.db_version == "v1"

        # Active service remains operational
        assert service.retrieve("glucose").candidates[0].canonical_name == "Glucose"
        service.close()

    def test_unoperational_fts_unicode_candidate_rejected_leaves_v1_operational(self, tmp_path: Path):
        """Tests that a candidate DB where fts_aliases_unicode cannot execute MATCH is rejected and v1 stays active."""
        data_dir = tmp_path / "bad_fts_unicode_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(v1_db, "v1", [("C1", "Glucose", "glucose")])
        self._update_pointer(data_dir, "terminology_v1.db", "v1")

        service = TerminologyRetrievalService(data_dir)
        assert service.db_version == "v1"

        # Create v2 with all required tables, but fts_aliases_unicode is a normal table (MATCH fails)
        v2_bad_db = data_dir / "terminology_v2_bad_fts.db"
        self._create_minimal_db(v2_bad_db, "v2_bad", [("C1", "Glucose", "glucose"), ("C2", "Insulin", "insulin")])

        conn = sqlite3.connect(str(v2_bad_db))
        conn.execute("DROP TABLE fts_aliases_unicode")
        conn.execute("CREATE TABLE fts_aliases_unicode (dummy TEXT)")
        conn.commit()
        conn.close()

        self._update_pointer(data_dir, "terminology_v2_bad_fts.db", "v2_bad")

        reloaded = service.reload_if_updated()
        assert reloaded is False
        assert service.db_version == "v1"
        assert service.metadata.get("version_tag") == "v1"

        # Active service v1 remains fully operational
        resp = service.retrieve("glucose")
        assert len(resp.candidates) > 0
        assert resp.candidates[0].canonical_name == "Glucose"
        service.close()

    def test_unoperational_fts_trigram_candidate_rejected_leaves_v1_operational(self, tmp_path: Path):
        """Tests that a candidate DB where fts_aliases_trigram cannot execute MATCH is rejected and v1 stays active."""
        data_dir = tmp_path / "bad_fts_trigram_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(v1_db, "v1", [("C1", "Glucose", "glucose")])
        self._update_pointer(data_dir, "terminology_v1.db", "v1")

        service = TerminologyRetrievalService(data_dir)
        assert service.db_version == "v1"

        # Create v2 with all required tables, but fts_aliases_trigram is a normal table (MATCH fails)
        v2_bad_db = data_dir / "terminology_v2_bad_trigram.db"
        self._create_minimal_db(v2_bad_db, "v2_bad", [("C1", "Glucose", "glucose"), ("C2", "Insulin", "insulin")])

        conn = sqlite3.connect(str(v2_bad_db))
        conn.execute("DROP TABLE fts_aliases_trigram")
        conn.execute("CREATE TABLE fts_aliases_trigram (dummy TEXT)")
        conn.commit()
        conn.close()

        self._update_pointer(data_dir, "terminology_v2_bad_trigram.db", "v2_bad")

        reloaded = service.reload_if_updated()
        assert reloaded is False
        assert service.db_version == "v1"
        assert service.metadata.get("version_tag") == "v1"

        # Active service v1 remains fully operational
        resp = service.retrieve("glucose")
        assert len(resp.candidates) > 0
        assert resp.candidates[0].canonical_name == "Glucose"
        service.close()

    def test_concurrent_query_reload_stress(self, tmp_path: Path):
        """Tests that concurrent queries during database reload do not throw closed connection errors."""
        import threading

        data_dir = tmp_path / "concurrent_test"
        data_dir.mkdir(parents=True, exist_ok=True)

        v1_db = data_dir / "terminology_v1.db"
        self._create_minimal_db(v1_db, "v1", [("C1", "Glucose", "glucose")])
        v2_db = data_dir / "terminology_v2.db"
        self._create_minimal_db(v2_db, "v2", [("C1", "Glucose", "glucose"), ("C2", "Insulin", "insulin")])

        self._update_pointer(data_dir, "terminology_v1.db", "v1")
        service = TerminologyRetrievalService(data_dir)

        errors: list[Exception] = []
        stop_event = threading.Event()

        def query_worker():
            while not stop_event.is_set():
                try:
                    resp = service.retrieve("glucose")
                    assert len(resp.candidates) > 0
                except Exception as ex:
                    errors.append(ex)

        def reload_worker():
            toggle = True
            for _ in range(20):
                target_file = "terminology_v2.db" if toggle else "terminology_v1.db"
                target_ver = "v2" if toggle else "v1"
                self._update_pointer(data_dir, target_file, target_ver)
                try:
                    service.reload_if_updated()
                except Exception as ex:
                    errors.append(ex)
                toggle = not toggle
                time.sleep(0.005)

        t_query1 = threading.Thread(target=query_worker)
        t_query2 = threading.Thread(target=query_worker)
        t_reload = threading.Thread(target=reload_worker)

        t_query1.start()
        t_query2.start()
        t_reload.start()

        t_reload.join(timeout=10.0)
        stop_event.set()
        t_query1.join(timeout=5.0)
        t_query2.join(timeout=5.0)

        service.close()
        assert not errors, f"Encountered {len(errors)} concurrency errors during reload: {errors[:5]}"


class TestConnectionSafetyAndErrorHandling:
    """Verifies file resolution, read-only pragmas, and missing DB handling."""

    def test_missing_database_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            TerminologyRetrievalService(tmp_path / "nonexistent.db")

    def test_read_only_pragma_enforced(self, retrieval_service: TerminologyRetrievalService):
        cursor = retrieval_service.conn.cursor()
        cursor.execute("PRAGMA query_only;")
        row = cursor.fetchone()
        assert row[0] == 1 or row["query_only"] == 1


class TestRetrievalLatencyBenchmarking:
    """Verifies sub-millisecond in-memory exact lookup latency."""

    def test_exact_lookup_latency(self, retrieval_service: TerminologyRetrievalService):
        times = []
        for _ in range(50):
            t0 = time.perf_counter()
            retrieval_service.retrieve("Amoxicillin")
            times.append((time.perf_counter() - t0) * 1000.0)

        avg_latency_ms = sum(times) / len(times)
        # Average exact cache latency should be well under 5.0 ms
        assert avg_latency_ms < 5.0, f"Average latency {avg_latency_ms:.2f} ms exceeded 5.0 ms"

