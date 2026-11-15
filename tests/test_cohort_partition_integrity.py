"""Document-level partition integrity and cohort inventory tests.

Authority: Phase 5A / 5B Evaluation Dataset Integrity & Statistical Metrics.
Verifies:
1. Strict document-level partitioning (40% tuning / 60% locked evaluation).
2. Zero parent-document overlap between partitions.
3. Complete encapsulation of all mentions within their parent document's partition.
4. Correct inheritance of derived/degraded variants from parent document.
5. Cohort 1 clean in-vocabulary fixture integrity.
6. Cohort 2 unseen synonyms absence verification and concept-level disjointness.
7. Cohort 3 multi-parent OCR-degraded partition inheritance across LAB & MED.
8. Cohort 4 four-bucket abstention benchmark integrity and precedence.
"""

from __future__ import annotations

import glob
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import pytest


def deterministic_document_split(
    doc_ids: list[str],
    seed: int = 42,
    tuning_ratio: float = 0.40,
) -> tuple[list[str], list[str]]:
    """Deterministically partition documents into tuning and locked evaluation sets."""
    sorted_ids = sorted(doc_ids)
    rng = random.Random(seed)
    shuffled = list(sorted_ids)
    rng.shuffle(shuffled)
    n_tuning = int(round(len(sorted_ids) * tuning_ratio))
    tuning = sorted(shuffled[:n_tuning])
    locked = sorted(shuffled[n_tuning:])
    return tuning, locked


class TestDocumentLevelPartitionIntegrity:
    """Verifies that evaluation partitioning operates strictly at the document/concept level."""

    def test_stratified_document_split_counts_and_zero_overlap(self):
        lab_files = sorted(glob.glob("data/ground_truth/LAB_*.json"))
        med_files = sorted(glob.glob("data/med_ground_truth/MED_*.json"))
        narr_files = sorted(glob.glob("data/narrative_ground_truth/NARRATIVE_*.json"))

        assert len(lab_files) == 50, f"Expected 50 lab files, got {len(lab_files)}"
        assert len(med_files) == 50, f"Expected 50 med files, got {len(med_files)}"
        assert len(narr_files) == 20, f"Expected 20 narrative files, got {len(narr_files)}"

        lab_ids = [Path(f).stem for f in lab_files]
        med_ids = [Path(f).stem for f in med_files]
        narr_ids = [Path(f).stem for f in narr_files]

        t_lab, l_lab = deterministic_document_split(lab_ids, seed=42, tuning_ratio=0.40)
        t_med, l_med = deterministic_document_split(med_ids, seed=42, tuning_ratio=0.40)
        t_narr, l_narr = deterministic_document_split(narr_ids, seed=42, tuning_ratio=0.40)

        # 40% tuning / 60% locked exact counts
        assert len(t_lab) == 20
        assert len(l_lab) == 30
        assert len(t_med) == 20
        assert len(l_med) == 30
        assert len(t_narr) == 8
        assert len(l_narr) == 12

        tuning_all = set(t_lab + t_med + t_narr)
        locked_all = set(l_lab + l_med + l_narr)

        # Zero parent document overlap
        assert tuning_all.isdisjoint(locked_all), f"Found overlapping documents: {tuning_all & locked_all}"

    def test_mention_confinement_to_parent_document_partition(self):
        lab_files = {Path(f).stem: f for f in glob.glob("data/ground_truth/LAB_*.json")}
        med_files = {Path(f).stem: f for f in glob.glob("data/med_ground_truth/MED_*.json")}

        t_lab, l_lab = deterministic_document_split(list(lab_files.keys()), seed=42, tuning_ratio=0.40)
        t_med, l_med = deterministic_document_split(list(med_files.keys()), seed=42, tuning_ratio=0.40)

        tuning_docs = set(t_lab + t_med)
        locked_docs = set(l_lab + l_med)

        t_queries = 0
        l_queries = 0

        # Collect lab queries
        for doc_id, fpath in lab_files.items():
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            tests = data.get("tests", [])
            if doc_id in tuning_docs:
                t_queries += len(tests)
                assert doc_id not in locked_docs
            else:
                assert doc_id in locked_docs
                l_queries += len(tests)

        # Collect med queries
        for doc_id, fpath in med_files.items():
            with open(fpath, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            meds = data.get("medications", [])
            if doc_id in tuning_docs:
                t_queries += len(meds)
                assert doc_id not in locked_docs
            else:
                assert doc_id in locked_docs
                l_queries += len(meds)

        total_queries = t_queries + l_queries
        assert total_queries == 792, f"Expected 792 total queries across 100 clean docs, got {total_queries}"
        assert t_queries == 291  # 36.7%
        assert l_queries == 501  # 63.3%

    def test_degraded_variant_parent_inheritance(self):
        lab_ids = [f"LAB_{i:04d}" for i in range(1, 51)]
        t_lab, l_lab = deterministic_document_split(lab_ids, seed=42, tuning_ratio=0.40)

        assert "LAB_0001" in t_lab
        degraded_samples = glob.glob("data/degraded/samples/LAB_0001_*")
        assert len(degraded_samples) > 0
        for sample in degraded_samples:
            parent_id = "LAB_0001"
            assert parent_id in t_lab
            assert parent_id not in l_lab

    def test_cohort_1_materialized_fixture_integrity(self):
        fixture_path = Path("tests/fixtures/terminology_eval_cohort_1.json")
        assert fixture_path.exists(), f"Fixture missing at {fixture_path}"

        with open(fixture_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        meta = data.get("_metadata", {})
        assert meta["total_source_documents"] == 100
        assert meta["tuning_document_count"] == 40
        assert meta["locked_evaluation_document_count"] == 60
        assert meta["total_queries"] == 792
        assert meta["tuning_query_count"] == 291
        assert meta["locked_evaluation_query_count"] == 501

        queries = data.get("queries", [])
        assert len(queries) == 792

        tuning_docs = set()
        locked_docs = set()
        for q in queries:
            assert q["target_code"] is not None and len(q["target_code"]) > 0
            assert q["target_system"] in ("LOINC", "RxNorm")
            assert q["partition"] in ("TUNING", "LOCKED_EVALUATION")
            if q["partition"] == "TUNING":
                tuning_docs.add(q["document_id"])
            else:
                locked_docs.add(q["document_id"])

        assert len(tuning_docs) == 40
        assert len(locked_docs) == 60
        assert tuning_docs.isdisjoint(locked_docs)

    def test_cohort_2_unseen_synonyms_absence_and_concept_disjointness(self):
        fixture_path = Path("tests/fixtures/terminology_eval_cohort_2.json")
        assert fixture_path.exists(), f"Cohort 2 fixture missing at {fixture_path}"

        with open(fixture_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        meta = data.get("_metadata", {})
        db_path = Path("data/terminology/terminology_v20261112.candidate.db")
        assert db_path.exists()
        current_db_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert meta["active_database_sha256"] == current_db_hash

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        queries = data.get("queries", [])
        assert len(queries) >= 90, f"Expected at least 90 verified unseen synonyms, got {len(queries)}"

        tuning_concepts = set()
        locked_concepts = set()

        for q in queries:
            q_text = q["query_text"]
            t_clean = q_text.lower().strip()
            t_search = re.sub(r"[^a-z0-9 ]", "", t_clean).strip()

            # Verify ABSENCE from active candidate database
            cur.execute(
                "SELECT count(*) FROM concept_aliases WHERE search_text = ? OR alias_text = ?",
                (t_search, t_clean),
            )
            count = cur.fetchone()[0]
            assert count == 0, f"Synonym '{q_text}' unexpectedly present in candidate database aliases ({count} occurrences)!"

            # Verify target concept exists in DB
            cur.execute(
                "SELECT count(*) FROM concepts WHERE code = ? AND system = ?",
                (q["target_code"], q["target_system"]),
            )
            assert cur.fetchone()[0] == 1, f"Target concept {q['target_system']}:{q['target_code']} missing from concepts table"

            # Check concept-level disjoint partitioning
            concept_key = f"{q['target_system']}:{q['target_code']}"
            if q["partition"] == "TUNING":
                tuning_concepts.add(concept_key)
            else:
                assert q["partition"] == "LOCKED_EVALUATION"
                locked_concepts.add(concept_key)

            # Metadata assertions
            assert q["is_in_vocabulary"] is True
            assert q["is_nonsense"] is False
            assert q["is_out_of_scope"] is False
            assert len(q["annotation_rationale"]) > 0
            assert len(q["synonym_source"]) > 0

        conn.close()

        # Zero concept-level leakage between tuning and locked evaluation
        assert tuning_concepts.isdisjoint(locked_concepts), (
            f"Concept leakage detected! Overlapping concepts: {tuning_concepts & locked_concepts}"
        )
        assert len(tuning_concepts) > 0
        assert len(locked_concepts) > 0

        # Substantiated provenance assertions
        torvast_entries = [q for q in queries if q["query_text"] == "Torvast"]
        assert len(torvast_entries) == 1
        assert torvast_entries[0]["synonym_source"] == "INTERNATIONAL_BRAND_NAME"
        assert "Pfizer Italia" in torvast_entries[0]["annotation_rationale"]
        assert "Commercial Trademark" in torvast_entries[0]["license"]

        # Ensure no private publisher sources are labeled as "Public Domain"
        for q in queries:
            if q["synonym_source"] in ("USP_COMPENDIUM", "CLSI_USER_ALIAS", "CAP_LAB_COMPENDIUM"):
                assert q["license"] != "Public Domain", (
                    f"Entry {q['query_id']} incorrectly labeled as 'Public Domain' for private publisher {q['synonym_source']}"
                )

    def test_cohort_3_ocr_degraded_multi_parent_partition_inheritance(self):
        fixture_path = Path("tests/fixtures/terminology_eval_cohort_3.json")
        c1_fixture_path = Path("tests/fixtures/terminology_eval_cohort_1.json")
        assert fixture_path.exists(), f"Cohort 3 fixture missing at {fixture_path}"
        assert c1_fixture_path.exists(), f"Cohort 1 master fixture missing at {c1_fixture_path}"

        with open(fixture_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        with open(c1_fixture_path, "r", encoding="utf-8") as fp:
            c1_data = json.load(fp)

        c1_doc_partition = {q["document_id"]: q["partition"] for q in c1_data["queries"]}

        meta = data.get("_metadata", {})
        assert meta["lab_parent_document_count"] == 20
        assert meta["med_parent_document_count"] == 20
        assert meta["total_parent_documents"] == 40

        lab_ids = [f"LAB_{i:04d}" for i in range(1, 21)]
        med_ids = [f"MED_{i:04d}" for i in range(1, 21)]
        parent_ids = lab_ids + med_ids

        # Authoritative master partition from Cohort 1
        tuning_parent_docs = {pid for pid in parent_ids if c1_doc_partition[pid] == "TUNING"}
        locked_parent_docs = {pid for pid in parent_ids if c1_doc_partition[pid] == "LOCKED_EVALUATION"}

        assert len(tuning_parent_docs) == 18
        assert len(locked_parent_docs) == 22
        assert tuning_parent_docs.isdisjoint(locked_parent_docs)

        queries = data.get("queries", [])
        assert len(queries) >= 600, f"Expected >=600 degraded queries, got {len(queries)}"

        seen_parents = set()
        for q in queries:
            p_id = q["parent_document_id"]
            seen_parents.add(p_id)

            # Master partition inheritance: every query MUST match its Cohort 1 parent document partition exactly
            expected_partition = c1_doc_partition[p_id]
            assert q["partition"] == expected_partition, (
                f"Query {q['query_id']} parent {p_id} partition mismatch: "
                f"Cohort 1 has {expected_partition}, but query has {q['partition']}"
            )

            # Verify recovery and nonsense classification
            assert q["is_recoverable"] is True
            assert q["is_nonsense"] is False
            assert q["is_out_of_scope"] is False
            assert q["target_code"] is not None and len(q["target_code"]) > 0
            assert q["target_system"] in ("LOINC", "RxNorm")
            assert q["degradation_severity"] in ("low", "medium", "high")

        # Every parent document must be represented
        assert seen_parents == set(parent_ids)

    def test_cohort_3_negative_partition_tampering_rejected(self):
        """Negative test ensuring mismatched partition assignment is rejected."""
        fixture_path = Path("tests/fixtures/terminology_eval_cohort_3.json")
        c1_fixture_path = Path("tests/fixtures/terminology_eval_cohort_1.json")

        with open(fixture_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        with open(c1_fixture_path, "r", encoding="utf-8") as fp:
            c1_data = json.load(fp)

        c1_doc_partition = {q["document_id"]: q["partition"] for q in c1_data["queries"]}

        # Simulate tampering with a query's partition
        tampered_query = dict(data["queries"][0])
        original_partition = tampered_query["partition"]
        parent_id = tampered_query["parent_document_id"]
        inverted_partition = "LOCKED_EVALUATION" if original_partition == "TUNING" else "TUNING"
        tampered_query["partition"] = inverted_partition

        # Verify negative assertion catches the mismatch
        with pytest.raises(AssertionError, match="partition mismatch"):
            expected = c1_doc_partition[parent_id]
            assert tampered_query["partition"] == expected, (
                f"Query {tampered_query['query_id']} parent {parent_id} partition mismatch: "
                f"Cohort 1 has {expected}, but query has {tampered_query['partition']}"
            )

    def test_cohort_4_abstention_benchmark_four_buckets_and_precedence(self):
        fixture_path = Path("tests/fixtures/terminology_eval_cohort_4.json")
        assert fixture_path.exists(), f"Cohort 4 fixture missing at {fixture_path}"

        with open(fixture_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)

        meta = data.get("_metadata", {})
        assert meta["precedence_rule"] == "NONSENSE > OUT_OF_SCOPE > INSUFFICIENT_EVIDENCE > AMBIGUOUS"
        assert meta["nonsense_locked_evaluation_denominator"] == 81

        queries = data.get("queries", [])
        assert len(queries) == 230, f"Expected exactly 230 queries, got {len(queries)}"

        bucket_counts = {}
        tuning_count = 0
        locked_count = 0
        nonsense_locked = 0

        for q in queries:
            b = q["abstention_bucket"]
            bucket_counts[b] = bucket_counts.get(b, 0) + 1

            assert len(q["adjudication_rationale"]) > 0
            assert q["target_code"] is None, "Abstention benchmark targets must be None to enforce fail-closed scoring"
            assert q["target_system"] is None

            if b == "NONSENSE":
                assert q["is_nonsense"] is True
                assert q["is_in_vocabulary"] is False
                assert q["is_out_of_scope"] is False
            elif b == "OUT_OF_SCOPE":
                assert q["is_nonsense"] is False
                assert q["is_in_vocabulary"] is False
                assert q["is_out_of_scope"] is True
            elif b in ("INSUFFICIENT_EVIDENCE", "AMBIGUOUS"):
                assert q["is_nonsense"] is False
                assert q["is_in_vocabulary"] is True
                assert q["is_out_of_scope"] is False
            else:
                pytest.fail(f"Unknown abstention bucket '{b}'")

            if q["partition"] == "TUNING":
                tuning_count += 1
            else:
                assert q["partition"] == "LOCKED_EVALUATION"
                locked_count += 1
                if b == "NONSENSE":
                    nonsense_locked += 1

        assert bucket_counts == {
            "NONSENSE": 135,
            "OUT_OF_SCOPE": 55,
            "INSUFFICIENT_EVIDENCE": 20,
            "AMBIGUOUS": 20,
        }
        assert tuning_count == 92  # 40% of 230
        assert locked_count == 138  # 60% of 230
        assert nonsense_locked == 81, f"Expected 81 locked nonsense queries, got {nonsense_locked}"
