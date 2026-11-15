# M-TERMINOLOGY-SCALE — Phase 1 Acceptance Report
**Offline Terminology Database Construction, Indexing & Publishing**

- **Date:** 2026-11-12
- **Milestone:** M-TERMINOLOGY-SCALE (Phase 1)
- **Status:** CONDITIONALLY ACCEPTED (Offline Construction Framework & Fixture-Verified)
- **Git HEAD:** `9918ded` on `master`
- **Git State:** 75 pre-existing staged files preserved unmodified; 0 commits; 0 pushes; 0 branch changes

---

## 1. Executive Summary & Authorization Compliance

Under explicit authorization for **Phase 1 — Offline Terminology Database Construction only**, the offline terminology subsystem framework was implemented, tested, and conditionally accepted with fixture-level verification.

All work strictly adhered to the authorized scope boundaries:
- **No Phase 2+ Scope:** No runtime retrieval pipeline wiring, no confidence calibration changes, no modifications to live document processing.
- **Milestone Isolation:** Phase A, B1, C1, M-OCR, M-BENCHMARK, and M-FHIR-SAFE-SELECTION remain closed and unmodified.
- **FHIR Pipeline Preserved:** `src/clindoc/fhir/selection.py` and `tests/test_fhir_safe_selection.py` (25/25 passing) remain completely untouched.
- **Deterministic RxNorm Strict CPC Ingestion:** Ingestion restricted exclusively to official RxNorm Current Prescribable Content (CPC) specification (`SAB IN ('RXNORM', 'MTHSPL')`). Proprietary third-party sources (e.g. `NDDF`, `MSH`, `VANDF`) and non-prescribable sources (`USP`) are strictly rejected.
- **Licensing & Provenance Embedded:** Official attribution statements for Regenstrief Institute (LOINC) and NLM (RxNorm) embedded into database schema, metadata tables, and publisher manifests.
- **Windows File Lock Safety:** Versioned publishing pattern implemented with atomic JSON pointer file (`active_terminology.json`), completely preventing Windows NTFS `[WinError 32]` file handle contention.

---

## 2. Relational Schema & Virtual Table Architecture

The database architecture is implemented in `src/clindoc/terminology/schema.py` (`TERMINOLOGY_SCHEMA_VERSION = "1.0.0"`):

### 2.1 Authoritative Relational Tables
1. **`concepts`**:
   - `concept_id INTEGER PRIMARY KEY`
   - `system TEXT NOT NULL` (`'LOINC'` or `'RxNorm'`)
   - `code TEXT NOT NULL`
   - `canonical_name TEXT NOT NULL`
   - `tty TEXT` (RxNorm Term Type e.g., `IN`, `SCD` or LOINC scale e.g., `Qn`)
   - `attributes_json TEXT NOT NULL` (Structured JSON storing 6 LOINC axes or RxNorm components)
   - Unique Index: `(system, code)`
   - Index: `(system, tty)`
2. **`concept_aliases`**:
   - `alias_id INTEGER PRIMARY KEY`
   - `concept_id INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE`
   - `alias_text TEXT NOT NULL` (Normalized: lowercased, whitespace collapsed, trimmed)
   - `alias_source TEXT NOT NULL` (`LOINC_LONG_COMMON_NAME`, `LOINC_RELATEDNAMES2`, `RXNORM_IN`, etc.)
   - `is_abbreviation BOOLEAN NOT NULL DEFAULT 0`
   - `is_preferred BOOLEAN NOT NULL DEFAULT 0`
   - Indexes: `(concept_id)`, `(alias_text)`
3. **`alias_context_rules`**:
   - `rule_id INTEGER PRIMARY KEY`
   - `alias_id INTEGER NOT NULL REFERENCES concept_aliases(alias_id) ON DELETE CASCADE`
   - `dimension TEXT NOT NULL` (`SPECIMEN`, `SCALE`, `ROUTE`, `DOMAIN`, `DISALLOWED_CONTEXT`)
   - `operator TEXT NOT NULL` (`IN`, `EQUALS`, `NOT_IN`)
   - `allowed_values_json TEXT NOT NULL` (JSON array of permissible context values)
   - Index: `(alias_id)`
4. **`terminology_metadata`**:
   - Key-value metadata table recording schema version, source releases, cryptographic hashes, row counts, build commit SHA, and license notices.

### 2.2 FTS5 External-Content Virtual Tables
To eliminate data duplication, external-content virtual tables reference `concept_aliases`:
1. **`fts_aliases_unicode`**:
   - `tokenize='unicode61 remove_diacritics 2'`
   - Indexed over `concept_aliases(alias_text)` with `content_rowid='alias_id'`
   - Used for full-word and stemmed lexical BM25 retrieval.
2. **`fts_aliases_trigram`**:
   - `tokenize='trigram'`
   - Indexed over `concept_aliases(alias_text)` with `content_rowid='alias_id'`
   - Used for character n-gram substring matching and OCR-corrupted query recovery.

---

## 3. Ingestion Builders & Licensing Safeguards

### 3.1 LOINC Ingestion Builder (`src/clindoc/terminology/loinc_builder.py`)
- **Axis Parsing:** Preserves all 6 official axes: Component, Property, Time Aspect, System (Specimen), Scale, and Method.
- **Status Filtering:** Rejects inactive terms (`STATUS IN ('DEPRECATED', 'DISCOURAGED', 'DELETED')`) and logs exclusion metrics.
- **Synonym Extraction:** Extracts Long Common Names, Component strings, Short Names, Consumer Names, and individual pipe/semicolon-delimited terms from `RELATEDNAMES2`.
- **Constraint Derivation:** Automatically generates `SPECIMEN` and `SCALE` rules from LOINC axis metadata.
- **Licensing Notice:** Embeds required Regenstrief Institute copyright statement (`LOINC_COPYRIGHT_NOTICE`).

### 3.2 RxNorm CPC Ingestion Builder (`src/clindoc/terminology/rxnorm_builder.py`)
- **Distribution Scope:** Ingests official NLM Current Prescribable Content (CPC) files (`RXNCONSO.RRF` / `RXNREL.RRF`).
- **Strict Non-Proprietary CPC Filtering:**
  - Allowed SABs: `RXNORM`, `MTHSPL`. (`USP` explicitly removed to enforce strict CPC prescribable scope).
  - Excluded SABs: Proprietary commercial datasets (e.g. `NDDF`, `MSH`, `VANDF`, `SNOMEDCT_US`) and compendial `USP`. Exclusions recorded in build stats (`PROPRIETARY_OR_UNSUPPORTED_SAB_<SAB>`).
- **Prescribable TTY Enforcement:**
  - Allowed TTYs: `IN`, `PIN`, `MIN`, `SCDC`, `SCD`, `SBD`, `DF`, `GPCK`, `BPCK`.
  - Excluded TTYs: Obsolete/unsupported types (`SY`, `PSN`, etc.) filtered and logged.
- **Language and Suppression:** Enforces `LAT = 'ENG'` and filters suppressed entries (`SUPPRESS IN ('O', 'E')`).
- **Attribution Notice:** Embeds NLM public-domain attribution statement (`RXNORM_ATTRIBUTION_NOTICE`).

---

## 4. Compiler, Publication & Safe Pointer Management

Implemented in `src/clindoc/terminology/compiler.py`:
- **Deterministic Pipeline:** Executes DDL $\rightarrow$ Ingests LOINC $\rightarrow$ Ingests RxNorm CPC $\rightarrow$ Populates FTS5 $\rightarrow$ Optimizes FTS5 $\rightarrow$ Verifies integrity $\rightarrow$ Writes metadata $\rightarrow$ WAL checkpoint (`TRUNCATE`) $\rightarrow$ Closes DB $\rightarrow$ Emits atomic pointer.
- **Windows File Lock Safety:**
  - Database file published under timestamped version: `data/terminology/terminology_v{YYYYMMDD}.db`.
  - Active database reference tracked via lightweight JSON pointer: `data/terminology/active_terminology.json`.
  - Pointer file updated atomically via temporary file replacement (`os.replace`).
  - Active readers open read-only SQLite connections to the versioned `.db` file, preventing Windows NTFS handle contention.

---

## 5. Verification Metrics & Fixture Build Artifacts

A compilation was performed using the ClinicalDoc reference subsets (`configs/loinc_subset.json` and `configs/medication_subset.json`) into `data/terminology/`:

### 5.1 Compilation Metrics
| Metric | Value |
|---|---|
| **Database File** | `data/terminology/terminology_v20261112.db` |
| **Database Size** | 143,360 bytes (140 KB) |
| **Pointer File** | `data/terminology/active_terminology.json` (479 bytes) |
| **Schema Version** | `1.0.0` |
| **Build Time** | 0.035 seconds |
| **Peak Traced Python Memory** | 153.31 KB (0.15 MB) |
| **SHA-256 Hash** | `b10584a04a666f86e6f7358629a5ed9c02f8e916c547ab49f9a9b62a54639ef9` |
| **Concepts Inserted** | 103 (79 LOINC, 24 RxNorm) |
| **Aliases Inserted** | 136 (111 LOINC, 25 RxNorm) |
| **Context Rules Inserted** | 247 (222 LOINC, 25 RxNorm) |
| **FTS5 Unicode Rows** | 136 (100% sync) |
| **FTS5 Trigram Rows** | 136 (100% sync) |
| **SQLite `PRAGMA integrity_check`** | `ok` |
| **FTS5 `integrity-check`** | `ok` |

### 5.2 Recorded Metadata Verification
```json
{
  "schema_version": "1.0.0",
  "version_tag": "20261112",
  "build_timestamp": "2026-11-12T07:04:14.614248+00:00",
  "loinc_release_id": "LOINC_2.78",
  "loinc_source_hash": "c69424a6d390b03ae9dd63261f504c99f57a0d69feba822cdbf79bb310a4aef2",
  "loinc_copyright_notice": "This material contains content from LOINC (http://loinc.org)...",
  "rxnorm_release_id": "RxNorm_CPC_20261102",
  "rxnorm_source_hash": "0ccffacaa80aa577aa019879f3e7c47c8c3802c247ab2ee5033aff485da9e009",
  "rxnorm_attribution_notice": "This product uses publicly available data courtesy of the U.S. National Library of Medicine (NLM)...",
  "concepts_count": "103",
  "aliases_count": "136",
  "rules_count": "247",
  "build_commit_sha": "9918ded"
}
```

---

## 6. Test Suite Results

### 6.1 Phase 1 Test Execution (`tests/test_terminology_phase1.py`)
Execution command: `.\.venv\Scripts\python.exe -m pytest tests\test_terminology_phase1.py -v`
```
tests/test_terminology_phase1.py::TestTerminologySchema::test_schema_tables_exist PASSED [  7%]
tests/test_terminology_phase1.py::TestTerminologySchema::test_foreign_key_enforcement PASSED [ 14%]
tests/test_terminology_phase1.py::TestTerminologySchema::test_unique_concept_constraint PASSED [ 21%]
tests/test_terminology_phase1.py::TestLoincIngestion::test_parse_loinc_csv_row_full PASSED [ 28%]
tests/test_terminology_phase1.py::TestLoincIngestion::test_inactive_loinc_filtering PASSED [ 35%]
tests/test_terminology_phase1.py::TestLoincIngestion::test_loinc_insert_and_idempotency PASSED [ 42%]
tests/test_terminology_phase1.py::TestRxNormCpcIngestion::test_parse_valid_cpc_line PASSED [ 50%]
tests/test_terminology_phase1.py::TestRxNormCpcIngestion::test_usp_sab_rejected PASSED [ 57%]
tests/test_terminology_phase1.py::TestRxNormCpcIngestion::test_proprietary_sab_filtering PASSED [ 64%]
tests/test_terminology_phase1.py::TestRxNormCpcIngestion::test_unsupported_tty_filtering PASSED [ 71%]
tests/test_terminology_phase1.py::TestRxNormCpcIngestion::test_suppressed_line_filtering PASSED [ 78%]
tests/test_terminology_phase1.py::TestFts5IndexingAndIntegrity::test_fts5_sync_and_query PASSED [ 85%]
tests/test_terminology_phase1.py::TestCompilerEndToEndAndReproducibility::test_compiler_build_from_curated_subsets PASSED [ 92%]
tests/test_terminology_phase1.py::TestCompilerEndToEndAndReproducibility::test_deterministic_reproducibility PASSED [100%]

============================= 14 passed in 0.32s ==============================
```

### 6.2 Regression & Safety Isolation Verification
Execution command: `.\.venv\Scripts\python.exe -m pytest tests\test_fhir_safe_selection.py`
```
============================= 25 passed in 0.15s ==============================
```
Zero regressions in frozen milestones; zero changes to `src/clindoc/fhir/selection.py`.

---

## 7. Workspace and Git Invariance

- **Git Commit Count:** 0 commits made.
- **Git Push / Fetch:** 0 remote operations performed.
- **Pre-existing Staged Changes:** Exactly 75 staged files preserved unmodified.
- **Pre-existing Unstaged Changes:** Preserved unmodified.
- **New Files (Untracked):**
  - `src/clindoc/terminology/schema.py`
  - `src/clindoc/terminology/loinc_builder.py`
  - `src/clindoc/terminology/rxnorm_builder.py`
  - `src/clindoc/terminology/compiler.py`
  - `src/clindoc/terminology/__init__.py`
  - `tests/test_terminology_phase1.py`
  - `data/terminology/` (`terminology_v20261112.db`, `active_terminology.json`)
  - `M_TERMINOLOGY_SCALE_PHASE_1_ACCEPTANCE_REPORT.md`

---

## 8. Explicit Limitations and Carry-Forward Requirements

1. **Curated Fixture Verification Only:**
   - Full official LOINC and RxNorm CPC distribution ingestion has **not** been performed. The 103-concept database represents curated test fixtures (`configs/loinc_subset.json` and `configs/medication_subset.json`).
   - Official release ingestion and multi-gigabyte build reproducibility must be verified when authorized source distribution files become available.
2. **Context-Rule Evaluation Engine Deferred:**
   - Context rules are stored in `alias_context_rules` (247 rows populated), but rule *evaluation* is not implemented in Phase 1.
   - Phase 2/3 must implement and test multi-dimensional `AND` across dimensions, `OR` within allowed values, `NOT_IN` exclusion, missing-context abstention, and conflicting-context abstention.
3. **696-Group Behavioral Baseline Carry-Forward:**
   - The 696-group baseline is currently established by aggregate behavioral tests in `tests/test_fhir_safe_selection.py` (679 selected, 17 suppressed), but a static machine-readable file (`fhir_safe_selection_696_oracle.json`) is not yet materialized on disk.
   - It must be generated and reviewed from the frozen pre-terminology selector before terminology-enabled selection integration, labeled strictly as a **behavioral regression baseline**, not independent clinical ground truth.
4. **SQLite PRAGMA Foreign-Key Test Isolation Anomaly:**
   - The previously documented SQLite PRAGMA test-isolation issue remains a separate tracked infrastructure defect; it must not be silently dismissed during subsequent testing.

---

## 9. Final Phase 1 Status & Strict Phase 2 Boundary

Phase 1 is formally recorded as **CONDITIONALLY ACCEPTED** as an offline terminology database construction framework with fixture-level verification.

**Strict Operating Boundary:**
- No Phase 2 implementation or authorization.
- No runtime retrieval wiring.
- No clinical pipeline or FHIR selector modifications.
- No changes to frozen milestones.
- No Git staging, commits, pushes, resets, or working tree state alterations.
- All work is paused pending explicit user authorization for Phase 2.
