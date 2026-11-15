# ClinicalDoc — M-TERMINOLOGY-SCALE Phase 3B Acceptance Report
## Full Offline Candidate Terminology Database Construction and Verification

**Date:** 2026-11-12  
**Database File:** `data/terminology/terminology_v20261112.candidate.db`  
**Candidate DB SHA-256:** `2b9a16736039e6a054e278cdedda223da650f41f5ca70b7d9d67466038904ad7`  
**Schema Version:** `2.0.0`  
**Evaluation Status:** **SUCCESS — ALL CONSTRUCTION AND VERIFICATION GATES SATISFIED**  
**Publication Status:** **UNPUBLISHED CANDIDATE** (`active_terminology.json` unmodified)

---

## 1. Executive Summary & Verification of Strict Boundaries

Phase 3B has successfully constructed, indexed, verified, and benchmarked a complete, offline, candidate terminology database (`terminology_v20261112.candidate.db`) incorporating all four verified official source packages:
1. **Full LOINC 2.83** (`Loinc_2.83.zip`): Ingested all 112,405 concepts across all four class types and all lifecycle statuses.
2. **NRCeS Common Lab Codes for India (CLCI)** (`common-lab-codes-for-india-20260629.zip`): 1,473 Indian curated lab codes mapped 100% to LOINC.
3. **NRCeS Common Drug Codes for India (CDCI)** (`CommonDrugCodesForIndia_FlatFilePackage.zip`): Ingested 116,295 concepts, 210,206 aliases, and 224,284 explicit ontological relationships.
4. **NLM RxNorm Current Prescribable Content (CPC)** (`RxNorm_full_prescribe_11022026.zip`): Ingested 82,763 prescribable concepts, 161,768 aliases, and 553,970 relationships strictly restricted to `SAB IN ('RXNORM', 'MTHSPL')`.

### Verification of Strict Boundaries
- **Candidate Isolation:** The new database was built exclusively as `data/terminology/terminology_v20261112.candidate.db`. The active production pointer `data/terminology/active_terminology.json` was **NOT modified** and continues pointing to `terminology_v20261112.db` (Phase 1 release).
- **Zero Runtime Pipeline Changes:** No modifications were made to `src/clindoc/fhir/selection.py`, extraction, review, or export pipelines.
- **Baseline Invariance:** All 696 benchmark groups in `tests/fixtures/fhir_safe_selection_696_oracle.json` pass with 100% parity (28/28 pipeline tests passed in 0.64s).
- **Zero Git Repository Mutations:** No git commits, pushes, checkouts, or resets were performed. Exactly 75 pre-existing staged files remain untouched.

---

## 2. Physical Source Archive Inventory & Hashes

The candidate database was constructed directly by streaming from the four physically inspected archives in `D:\terminology_sources_staging\`:

| Source Package | Physical Archive Filename | Size (Bytes) | SHA-256 Checksum | Release Identifier |
| :--- | :--- | :--- | :--- | :--- |
| **LOINC Release 2.83** | `Loinc_2.83.zip` | 89,705,745 | `077a0718e87d8309ffe3a673f75b836a8e783dc36a646413ef97c71c12eab27e` | `LOINC_2.83` |
| **NRCeS CLCI** | `common-lab-codes-for-india-20260629.zip` | 275,343 | `d9db3941acdb7c5109b660e38a633076149230d35f0050a503ff156c927f3009` | `CLCI_20260629` |
| **NRCeS CDCI Flat Files**| `CommonDrugCodesForIndia_FlatFilePackage.zip` | 13,016,356 | `aebd30ac95d73accf573c86fd4dae1dc90eff03ae0fbcd312bc761011e88270a` | `CDCI_2019_FLAT_FILES` |
| **NLM RxNorm CPC** | `RxNorm_full_prescribe_11022026.zip` | 42,912,479 | `c742a6c4b2995194aa3479400e08a7c1ad707178bfc80fe9c8cd7afa693ae987` | `RxNorm_CPC_20261102` |

*Attribution & Licensing Notices:* Full statutory notices for Regenstrief Institute (LOINC), C-DAC Pune / NRCeS (CLCI & CDCI CC-BY 4.0), and the U.S. National Library of Medicine (RxNorm CPC) are recorded directly inside the `terminology_metadata` table.

---

## 3. Database Topology, Record Counts & Accounting

### 3.1 Global Entity Counts
- **Total Concepts:** `311,463`
- **Total Aliases:** `3,312,608`
- **FTS Unicode Search Entries:** `3,312,608` (100% sync)
- **FTS Trigram OCR Entries:** `3,312,608` (100% sync)
- **Total Relationships:** `778,254`
- **Total Cross-System Mappings:** `1,473`
- **Context Rules:** `3,324,424`

### 3.2 LOINC 2.83 Ingestion Verification
Official LOINC release census is verified with 100% precision:
- **Total LOINC Concepts:** `112,405`
- **Class Type Distribution:**
  - Class Type 1 (Laboratory): `69,651`
  - Class Type 2 (Clinical): `28,786`
  - Class Type 3 (Claims / Attachments): `1,161`
  - Class Type 4 (Surveys): `12,807`
  - **Total:** `112,405`
- **Lifecycle Status Distribution:**
  - `ACTIVE`: `99,737` (exact official active count)
  - `TRIAL`: `5,436`
  - `DEPRECATED`: `5,008`
  - `DISCOURAGED`: `2,224`
  - **Total:** `112,405`
- **Attributes Preserved:** Component, Property, Time Aspect, System (Specimen), Scale, Method, Class Name, Default Units, and `EXAMPLE_UCUM_UNITS`.

### 3.3 NRCeS CLCI Overlay Ingestion Verification
- **Total CLCI Codes Ingested:** `1,473`
- **Mappings Created:** `1,473` entries in `concept_mappings` (`source_system='CLCI'`, `target_system='LOINC'`, `relationship_type='EQUIVALENT'`, `review_status='OFFICIAL'`).
- **Overlay Flag:** `1,473` LOINC concepts tagged with `in_clci: 1` in `attributes_json`.
- **Discouraged Codes Preservation:** Both discouraged CLCI codes (`16250-3` - *Protein [Mass/volume] in Body fluid* and `19289-8` - *Cefuroxime axetil [Susceptibility] by Disk diffusion*) are preserved with `status='DISCOURAGED'` and retain their official CLCI equivalence mapping.

### 3.4 NRCeS CDCI Indian Medication Ingestion Verification
Ingested from the 7 master TSV files in `CommonDrugCodesForIndia_FlatFilePackage.zip`:
- **Concepts by Term Type (TTY):**
  - `BRAND`: `93,911`
  - `GENERIC`: `10,305`
  - `SUPPLIER`: `8,213`
  - `SUBSTANCE`: `3,283`
  - `DOSE_FORM`: `423`
  - `ROUTE`: `160`
  - **Total CDCI Concepts:** `116,295` (All with `status='ACTIVE'`)
- **Aliases:** `210,206` (including full `CDCI_PRODUCT_NAME` aliases from `ProductMaster.txt`).
- **Concept Relationships (224,284 total):**
  - `MANUFACTURED_BY` (Brand -> Supplier): `93,911`
  - `TRADENAME_OF` (Brand -> Generic): `93,897`
  - `HAS_SUBSTANCE` (Generic -> Substance): `13,127`
  - `HAS_ROUTE` (Generic -> Route): `13,044`
  - `HAS_DOSE_FORM` (Generic -> Dose Form): `10,305`
- **Accounted Orphaned Brands (Exactly 14):**
  All 14 orphaned brands identified in Phase 3A.3 are explicitly quarantined with `json_extract(attributes_json, '$.unresolved_generic_parent') IS NOT NULL` and zero `TRADENAME_OF` edges:
  - 6 missing Generic IDs: `B083070`, `B083071`, `B083072`, `B083073`, `B083074`, `B083075` (GenericID="").
  - 8 unresolvable SCTIDs: `B005896`, `B005897`, `B005898`, `B005899`, `B005900`, `B039148`, `B042460`, `B069275`.
  - Math check: 93,911 brands - 14 orphans = exactly 93,897 `TRADENAME_OF` relationships.

### 3.5 RxNorm CPC Supplemental Ingestion Verification
- **Total Concepts Ingested:** `82,763` prescribable concepts.
- **Aliases Ingested:** `161,768` (from `RXNCONSO.RRF`, non-suppressed, English).
- **Concept Relationships Ingested:** `553,970` (from `RXNREL.RRF`).
- **Exclusion Accounting:**
  - `PROPRIETARY_OR_UNSUPPORTED_SAB_MTHCMSFRF`: Exactly `11` rows excluded.
  - Non-prescribable Term Types filtered: `BN`: 8,806; `PSN`: 4,826; `ET`: 3,617; `SY`: 2,049; `DF`: 159.

---

## 4. Integrity and Reproducibility Verification

1. **Physical SQLite B-Tree Integrity:**
   `PRAGMA integrity_check;` executed across all 1.18 GB pages returning `[('ok',)]`.
2. **FTS5 Virtual Index Integrity:**
   `INSERT INTO fts_aliases_unicode(fts_aliases_unicode) VALUES('integrity-check');` -> **PASSED**  
   `INSERT INTO fts_aliases_trigram(fts_aliases_trigram) VALUES('integrity-check');` -> **PASSED**  
3. **Row Count Synchronization:**
   `concept_aliases` (3,312,608) == `fts_aliases_unicode` (3,312,608) == `fts_aliases_trigram` (3,312,608). Zero missing, orphaned, or desynchronized entries.
4. **Deterministic Bit-for-Bit Reproducibility:**
   Deterministic compiler compilation was verified in `tests/test_terminology_phase1.py` with identical SHA-256 output across separate runs on identical source files.

---

## 5. Performance Benchmarks vs. Acceptance Gates

| Benchmark Metric | Measured Value | Acceptance Target / Gate | Status | Operational Interpretation |
| :--- | :--- | :--- | :--- | :--- |
| **Candidate Database Size** | `1,125.09 MB` (1.18 GB) | Nominal (~1–2 GB) | **PASS** | Compact single-file SQLite database storing 311k concepts, 3.3M aliases, 778k relationships, and 2 full FTS5 indexes. |
| **Build Peak Memory (tracemalloc)** | `722.14 MB` | < 1,500 MB | **PASS** | Peak memory during full streaming extraction and indexing remained strictly bounded. |
| **Build Duration** | `2,055.13 s` (~34 min) | Offline one-time | **PASS** | Complete streaming ingestion, normalization, cross-table relationship construction, and 2 full FTS5 virtual table builds over 3.3 million rows. |
| **Unicode BM25 Search (p95)** | `12.150 ms` | < 20.0 ms | **PASS** | Full text BM25 ranking across 3.3 million aliases executes in ~12 ms. |
| **Trigram OCR Substring (p95)** | `58.361 ms` | < 100 ms (full scan) | **PASS** | Trigram fuzzy matching across 3.3 million aliases executes well within interactive bounds. |
| **Runtime Service Heap (No Cache Preload)** | `0.01 MB` | < 250 MB | **PASS** | Zero-heap footprint when utilizing SQLite's B-tree index on `concept_aliases(alias_text)` directly. |
| **Runtime Service Heap (Full Preload)** | `6,355.79 MB` | Informational | **NOTE** | Preloading all 3.3M alias rows into Python in-memory dictionaries consumes 6.3 GB. As designed in `service.py`, `get_exact_alias` seamlessly uses SQLite `EXACT_DB` via index, making full preload unnecessary in production. |
| **Exact Lookup Latency via EXACT_DB (p50)** | `5.092 ms` | < 10.0 ms | **PASS** | Direct B-Tree index lookup retrieves candidates (e.g. 65 matches for *paracetamol*) in ~5 ms without RAM overhead. |

---

## 6. Test Suite and Regression Verification

The complete project test suite was executed against the codebase:
- **Phase 1 Terminology Tests (`tests/test_terminology_phase1.py`):** 14/14 **PASSED**
- **Phase 2 Terminology Retrieval Tests (`tests/test_terminology_retrieval_phase2.py`):** 29/29 **PASSED**
- **Phase 3B Verification Tests (`tests/test_terminology_phase3b.py`):** 8/8 **PASSED**
  - `test_schema_ddl_and_version`: Verified Schema 2.0.0 DDL, tables, and indexes.
  - `test_candidate_compilation_leaves_pointer_untouched`: Verified candidate isolation and pointer preservation.
  - `test_cdci_concept_counts_by_tty`: Verified 116,295 CDCI concepts.
  - `test_cdci_relationship_types_and_integrity`: Verified 224,284 CDCI relationships.
  - `test_cdci_orphaned_brands_handling`: Verified exactly 14 orphaned brands quarantined.
  - `test_loinc_csv_row_preserves_status_and_classtype`: Verified LOINC 112,405 concepts and statuses.
  - `test_clci_mappings_and_discouraged_status`: Verified 1,473 CLCI mappings and discouraged codes.
  - `test_rxnorm_mthcmsfrf_exclusion`: Verified strict exclusion of 11 MTHCMSFRF records.
- **Combined Terminology Regression:** **51 / 51 PASSED in 20.85s**.
- **Runtime Pipeline Regression:**
  - `tests/test_fhir_safe_selection.py`: 25/25 **PASSED**
  - `tests/test_fhir_selection_baseline.py`: 3/3 **PASSED** (696/696 baseline parity verified in 0.64s).

---

## 7. Phase 3B Acceptance Summary & Standing by for Phase 3C Authorization

Phase 3B implementation and verification are complete:
- The full candidate database `terminology_v20261112.candidate.db` is compiled, indexed, physically validated, and resting in `data/terminology/`.
- `active_terminology.json` remains untouched pointing to the prior active database.
- Runtime selection and FHIR export pipelines remain completely untouched and 100% regression-free.

**Awaiting user authorization before initiating Phase 3C (candidate promotion, atomic pointer swap, and pipeline integration).**

