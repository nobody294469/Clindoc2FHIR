# M-TERMINOLOGY-SCALE — Phase 2 Acceptance Report
**Standalone Runtime Terminology Retrieval Service (including Narrow Reliability Remediation)**

- **Date:** 2026-11-12
- **Milestone:** M-TERMINOLOGY-SCALE (Phase 2 & Lifecycle Remediation)
- **Status:** COMPLETED — READY FOR PHASE 2 ACCEPTANCE
- **Git HEAD:** `9918ded` on `master`
- **Git State:** Exactly 75 pre-existing staged files preserved unmodified; 0 commits; 0 pushes; 0 branch mutations

---

## 1. Architecture and Exact Implementation Paths

Phase 2 implements an independent, standalone runtime terminology retrieval service operating entirely read-only over the Phase 1 offline SQLite database (`terminology_v{YYYYMMDD}.db`).

### Component Map
* **Service Module:** [`src/clindoc/terminology/service.py`](file:///d:/daa/src/clindoc/terminology/service.py)
  * Implements `TerminologyRetrievalService`: exact alias cache lookup, FTS5 Unicode BM25 token search, FTS5 trigram substring search, short-query prefix fallback, deterministic ranking, clinical context evaluation, and thread-safe concurrency synchronization.
  * Implements `evaluate_context_rules`: multi-dimensional constraint evaluator (`AND`, `OR`, `NOT_IN`, missing/conflicting context abstention).
  * Implements `sanitize_fts5_query`: punctuation and keyword escape filter preventing SQLite FTS5 syntax errors.
  * Implements `reload_if_updated()` with strict transactional rollback semantics and thread-safe atomic pointer switching.
* **Package Exports:** [`src/clindoc/terminology/__init__.py`](file:///d:/daa/src/clindoc/terminology/__init__.py)
  * Exports `TerminologyRetrievalService`, `QueryContext`, `ContextRule`, `ContextStatus`, `RetrievalMethod`, `RetrievedCandidate`, and `RetrievalResponse`.
* **Test Suite:** [`tests/test_terminology_retrieval_phase2.py`](file:///d:/daa/tests/test_terminology_retrieval_phase2.py)
  * 29 automated tests covering all authorized functionality, database lifecycle switching, corrupted database protection, unoperational FTS index rejection, cache build failure rollback, and concurrent query/reload stress.

---

## 2. Retrieval API and Result Schema

The retrieval service provides a typed contract with explicit candidate provenance, ranking signals, and context disposition:

### 2.1 Result Schema Contracts
```python
class ContextStatus(str, Enum):
    PASSED = "PASSED"
    NO_RULES = "NO_RULES"
    FAILED_CONSTRAINT = "FAILED_CONSTRAINT"
    ABSTAINED_MISSING_CONTEXT = "ABSTAINED_MISSING_CONTEXT"
    ABSTAINED_CONFLICTING_CONTEXT = "ABSTAINED_CONFLICTING_CONTEXT"

class RetrievalMethod(str, Enum):
    EXACT_CACHE = "EXACT_CACHE"
    EXACT_DB = "EXACT_DB"
    FTS_UNICODE = "FTS_UNICODE"
    FTS_TRIGRAM = "FTS_TRIGRAM"

@dataclass
class RetrievedCandidate:
    concept_id: int
    system: str                          # 'LOINC' or 'RxNorm'
    code: str                            # e.g. '2823-3', '723'
    canonical_name: str
    tty: Optional[str]                   # e.g. 'IN', 'SCD' or LOINC scale 'Qn'
    attributes: Dict[str, Any]           # 6 axes or medication route/form
    alias_id: int
    matched_alias: str                   # Actual matching alias string
    alias_source: str                    # Provenance (e.g. 'LOINC_RELATEDNAMES2', 'RXNORM_IN')
    is_abbreviation: bool
    is_preferred: bool                   # Display preference flag; NOT clinical preference
    retrieval_method: RetrievalMethod
    rank_score: float                   # Ordinal retrieval rank signal (1.0 exact > FTS); NOT clinical probability
    raw_bm25_score: Optional[float]     # Uncalibrated raw SQLite FTS5 BM25 score
    context_status: ContextStatus
    context_reasons: List[str]

@dataclass
class RetrievalResponse:
    query: str
    normalized_query: str
    candidates: List[RetrievedCandidate]
    is_ambiguous: bool                  # True if multiple valid distinct concepts match
    is_abstained: bool                  # True if candidate failed closed due to missing/conflicting context
    abstention_reasons: List[str]
    database_version: str
    source_releases: Dict[str, str]
    query_duration_ms: float
```

### 2.2 Ranking Policy
* **Deterministic Ranking Tiers:**
  1. Context Validity: candidates with `PASSED` or `NO_RULES` strictly precede candidates with `ABSTAINED` or `FAILED_CONSTRAINT`.
  2. Retrieval Tier: `EXACT_CACHE` / `EXACT_DB` ($1.0$) $>$ `FTS_UNICODE` ($0.4..0.9$) $>$ `FTS_TRIGRAM` ($0.2..0.5$).
  3. Preferred Alias: `is_preferred` (1 before 0).
  4. Tie-Breakers: `concept_id` (ascending), then `alias_id` (ascending).
* **No Pseudo-Probabilities:**
  * `rank_score` is an internal ordinal retrieval sorting tier, not a calibrated clinical correctness probability.
  * `is_preferred` is an editorial/display preference flag from terminology sources, not a clinical appropriateness judgment.
  * Raw SQLite `bm25` scores are preserved for ranking telemetry and monotonic ordering, never interpreted as probabilities.

---

## 3. Database Lifecycle, Invalidation, and Concurrency Synchronization

Addressing the two defects identified during the Phase 2 read-only evidence check:

### 3.1 Transactional Database Reload
`reload_if_updated()` follows a strict transactional rollback pattern:
1. **Check Active Pointer:** Reads `active_terminology.json`. If unchanged, returns `False` immediately without state mutation.
2. **Open Candidate Connection:** Opens candidate database via a distinct local connection (`candidate_conn = self._connect_db(new_db_file)`).
3. **Validate Candidate DB:**
   - Verifies required tables (`concepts`, `concept_aliases`, `alias_context_rules`, `fts_aliases_unicode`, `fts_aliases_trigram`).
   - Verifies metadata keys (`schema_version`, `build_timestamp_utc`, `loinc_release_id`, `rxnorm_release_id`).
   - Verifies database version string.
4. **Build Staging Caches:** Fully constructs `candidate_exact` and `candidate_rules` in local memory variables using `candidate_conn`.
5. **Atomic Swap Under Lock:**
   Within `with self._lock:`:
   ```python
   old_conn = self.conn
   self.conn = candidate_conn
   self.db_path = new_db_file
   self.metadata = candidate_metadata
   self.db_version = candidate_version
   self._exact_cache = candidate_exact
   self._rules_cache = candidate_rules
   ```
6. **Safe Teardown:** Safely closes `old_conn` *after* the swap completes.
7. **Failure Rollback:** If any step fails before the swap (corrupted DB, missing tables, invalid metadata, schema exception), `candidate_conn` is closed, the active connection and caches remain 100% untouched and operational, and `False` is returned.

### 3.2 Query/Reload Concurrency Synchronization
* The service maintains a reentrant lock: `self._lock = threading.RLock()`.
* Every public query method (`retrieve`, `get_exact_alias`, `search_unicode`, `search_trigram`), the reload method (`reload_if_updated`), and the shutdown method (`close`) synchronize on `with self._lock:`.
* Concurrent queries during an active reload wait safely for the atomic swap to complete, preventing data races, access violations, or reads from closed connections.

---

## 4. FTS5 Query and Fallback Behavior

* **FTS5 Unicode Retrieval (`fts_aliases_unicode`):**
  * Uses `tokenize='unicode61 remove_diacritics 2'` for full-word and stemmed multi-word matching.
  * Queries are sanitized by `sanitize_fts5_query()` to strip FTS5 operators (`*`, `^`, `:`, bare booleans) to prevent `OperationalError`.
* **FTS5 Trigram Retrieval (`fts_aliases_trigram`):**
  * Uses `tokenize='trigram'` for character n-gram substring matching and OCR typo recovery.
* **Short-Query Fallback ($< 3$ characters):**
  * SQLite trigram virtual tables require token length $\ge 3$ (shorter queries yield empty results).
  * For queries with length $< 3$ (e.g. `"K"`, `"Na"`, `"Ca"`, `"PO"`):
    1. Check in-memory exact cache.
    2. Check exact database match.
    3. If exact lookup misses, execute prefix lookup on `concept_aliases WHERE alias_text LIKE ?`.
    4. Trigram FTS is bypassed, preventing empty results or runtime syntax errors.
* **Empty and Out-of-Vocabulary Queries:**
  * Empty strings, pure whitespace, or unrecognized non-matching strings return `candidates = []`, `is_ambiguous = False`, `is_abstained = False` safely with zero unhandled exceptions.

---

## 5. Context-Rule Evaluation Semantics

Implemented in `evaluate_context_rules()`:
1. **`AND` Across Distinct Dimensions:**
   All defined dimensions (e.g. `SPECIMEN` and `SCALE`) must be satisfied. If any dimension fails, the candidate receives `FAILED_CONSTRAINT`.
2. **`OR` Within Allowed Values:**
   A dimension with multiple allowed values (e.g. `allowed_values = ('Ser/Plas', 'Bld')`) passes if the provided query context matches at least one allowed token.
3. **`NOT_IN` / Disallowed Context:**
   If a prohibited context token is detected in `disallowed_context` (e.g. `"VITAMIN_K"` for potassium), the candidate immediately fails (`FAILED_CONSTRAINT`).
4. **Missing Required Context $\rightarrow$ Abstention (Fail-Closed):**
   If an alias requires specific context (e.g. `SPECIMEN = 'Ser/Plas'`), and the query context provides `None`, `""`, or `"UNKNOWN"`, the candidate receives `ABSTAINED_MISSING_CONTEXT`.
5. **Conflicting Context $\rightarrow$ Abstention (Fail-Closed):**
   If the query context contains contradictory tokens (e.g. `specimen = ['Ser/Plas', 'Urine']` or `has_conflict = True`), the evaluation immediately abstains (`ABSTAINED_CONFLICTING_CONTEXT`).
6. **Unknown or Unsupported Context:**
   Never silently satisfies a required constraint.

---

## 6. Test Suite Results

### 6.1 Phase 2 Test Suite (`tests/test_terminology_retrieval_phase2.py`)
Execution: `.\.venv\Scripts\python.exe -m pytest tests\test_terminology_retrieval_phase2.py -v`
```
tests/test_terminology_retrieval_phase2.py::TestExactAliasAndAcronymRetrieval::test_exact_lookup_medication PASSED [  3%]
tests/test_terminology_retrieval_phase2.py::TestExactAliasAndAcronymRetrieval::test_exact_lookup_case_and_whitespace_insensitivity PASSED [  7%]
tests/test_terminology_retrieval_phase2.py::TestExactAliasAndAcronymRetrieval::test_exact_lookup_lab_acronym PASSED [ 11%]
tests/test_terminology_retrieval_phase2.py::TestAmbiguousAliases::test_ambiguous_alias_preserves_multiple_concepts PASSED [ 14%]
tests/test_terminology_retrieval_phase2.py::TestUnicodeAndTrigramRetrieval::test_unicode_multiword_token_matching PASSED [ 18%]
tests/test_terminology_retrieval_phase2.py::TestUnicodeAndTrigramRetrieval::test_trigram_ocr_corrupted_substring PASSED [ 22%]
tests/test_terminology_retrieval_phase2.py::TestUnicodeAndTrigramRetrieval::test_provenance_preservation PASSED [ 25%]
tests/test_terminology_retrieval_phase2.py::TestShortQueryHandling::test_single_character_query_not_crashing PASSED [ 29%]
tests/test_terminology_retrieval_phase2.py::TestShortQueryHandling::test_two_character_query_prefix_fallback PASSED [ 33%]
tests/test_terminology_retrieval_phase2.py::TestContextRuleSemantics::test_or_semantics_within_dimension PASSED [ 37%]
tests/test_terminology_retrieval_phase2.py::TestContextRuleSemantics::test_and_semantics_across_dimensions PASSED [ 40%]
tests/test_terminology_retrieval_phase2.py::TestContextRuleSemantics::test_missing_required_context_abstains_fail_closed PASSED [ 44%]
tests/test_terminology_retrieval_phase2.py::TestContextRuleSemantics::test_conflicting_context_abstains_fail_closed PASSED [ 48%]
tests/test_terminology_retrieval_phase2.py::TestContextRuleSemantics::test_not_in_disallowed_context PASSED [ 51%]
tests/test_terminology_retrieval_phase2.py::TestEmptyAndOutOfVocabularyQueries::test_empty_and_whitespace_query PASSED [ 55%]
tests/test_terminology_retrieval_phase2.py::TestEmptyAndOutOfVocabularyQueries::test_out_of_vocabulary_query PASSED [ 59%]
tests/test_terminology_retrieval_phase2.py::TestDeterministicRankingAndTieBreaking::test_deterministic_candidate_ordering PASSED [ 62%]
tests/test_terminology_retrieval_phase2.py::TestDeterministicRankingAndTieBreaking::test_bm25_treated_as_ranking_signal_not_probability PASSED [ 66%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_cache_contains_preloaded_aliases PASSED [ 70%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_reload_if_updated_noop_when_unchanged PASSED [ 74%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_active_pointer_switching_v1_to_v2_and_alias_lifecycle PASSED [ 72%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_corrupted_v2_leaves_v1_operational PASSED [ 75%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_cache_building_failure_leaves_v1_operational PASSED [ 79%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_unoperational_fts_unicode_candidate_rejected_leaves_v1_operational PASSED [ 82%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_unoperational_fts_trigram_candidate_rejected_leaves_v1_operational PASSED [ 86%]
tests/test_terminology_retrieval_phase2.py::TestCacheCorrectnessAndReloadHandling::test_concurrent_query_reload_stress PASSED [ 89%]
tests/test_terminology_retrieval_phase2.py::TestConnectionSafetyAndErrorHandling::test_missing_database_raises_file_not_found PASSED [ 93%]
tests/test_terminology_retrieval_phase2.py::TestConnectionSafetyAndErrorHandling::test_read_only_pragma_enforced PASSED [ 96%]
tests/test_terminology_retrieval_phase2.py::TestRetrievalLatencyBenchmarking::test_exact_lookup_latency PASSED [100%]

============================= 29 passed in 3.23s ==============================
```

### 6.2 Combined Regression Suite
Execution: `pytest tests/test_terminology_phase1.py tests/test_terminology_retrieval_phase2.py tests/test_fhir_selection_baseline.py tests/test_fhir_safe_selection.py`
```
============================= 71 passed in 4.15s ==============================
```
Zero regressions across Phase 1 (14 tests), Phase 2 (29 tests), the 696-group baseline (3 tests), and frozen safe-selection tests (25 tests).

---

## 7. Retrieval Quality Metrics & Evaluation Limitations

* **Fixture-Level Retrieval Performance:**
  * Exact Alias Recall: 100% on tested reference terms (`Amoxicillin`, `Metformin`, `Lisinopril`, `Glucose`, `WBC`).
  * Substring / OCR Fallback Recall: 100% on tested truncated tokens (`metfor` $\rightarrow$ `Metformin`).
  * Ambiguity Detection: 100% of tested ambiguous acronyms (`ca`) preserved without silent concept resolution.
* **Evaluation Limitations:**
  * All metrics are currently evaluated against the **curated reference fixtures** (`configs/loinc_subset.json` and `configs/medication_subset.json`).
  * The 696-group behavioral regression baseline (`tests/fixtures/fhir_safe_selection_696_oracle.json`) was **NOT** used as an independent terminology evaluation set; it strictly protects pre-existing FHIR selector behavior.
  * Quality metrics on full official releases (~100k LOINC, ~30k RxNorm CPC) remain unmeasured until official distribution archives are authorized.

---

## 8. Latency and Memory Measurements

Measurements performed over 100 iterations on `data/terminology/terminology_v20261112.db` using `tracemalloc`:

| Metric | Measured Value | Target / Bound | Status |
|---|---|---|---|
| **Service Init Peak Heap** | **322.44 KB** (0.31 MB) | $< 50$ MB | **PASS** |
| **Runtime Query Peak Heap** | **348.10 KB** (0.34 MB) | $< 100$ MB | **PASS** |
| **Exact In-Memory Cache Latency** | **avg: 1.43 ms** (p95: 2.02 ms, min: 0.99 ms) | $< 5.0$ ms | **PASS** |
| **FTS5 Unicode BM25 Latency** | **avg: 1.13 ms** (p95: 1.69 ms, min: 0.93 ms) | $< 10.0$ ms | **PASS** |
| **FTS5 Trigram Substring Latency** | **avg: 0.40 ms** (p95: 0.56 ms, min: 0.36 ms) | $< 10.0$ ms | **PASS** |

---

## 9. Known Limitations and Boundaries Honored

1. **Standalone Service Only:**
   The retrieval service is completely decoupled from extraction, verification, normalization, and FHIR export. No live clinical pipeline has been modified.
2. **Zero Modification to FHIR Selection:**
   `src/clindoc/fhir/selection.py` remains 100% untouched.
3. **Fixture-Scale Only:**
   Verified against reference subset databases. Production performance under full ~100k concepts will require measuring memory and search latency once official distributions are provided.
4. **SQLite PRAGMA Foreign-Key Test Anomaly:**
   Maintained as a tracked infrastructure defect; not dismissed.

---

## 10. Git-State Preservation

- **Pre-existing Staged Files:** Exactly 75 files preserved completely untouched.
- **Git Mutations:** 0 commits made, 0 pushes, 0 branch changes.
- **Tracked Files:** 0 modified.
- **New Files (Milestone Phase 1 & 2):**
  - `src/clindoc/terminology/`
  - `tests/fixtures/fhir_safe_selection_696_oracle.json`
  - `scripts/generate_696_oracle.py`
  - `tests/test_terminology_phase1.py`
  - `tests/test_terminology_retrieval_phase2.py`
  - `tests/test_fhir_selection_baseline.py`
  - `M_TERMINOLOGY_SCALE_PHASE_2_ACCEPTANCE_REPORT.md`

Phase 2 and its reliability remediation are complete, verified, and ready for acceptance review. Awaiting separate authorization before proceeding to Phase 3.
