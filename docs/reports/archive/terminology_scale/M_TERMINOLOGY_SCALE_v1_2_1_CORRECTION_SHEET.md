# ClinicalDoc — M-TERMINOLOGY-SCALE v1.2.1 Architectural Correction Sheet

**Document Version:** 1.2.1  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Strictly Read-Only Architectural Correction (Zero Code, File, or Git Modifications)  

---

## 1. Resolution of Acronym & Unknown Specimen Contradiction

### Verified Issue in v1.2
In Section 3.B of the v1.2 addendum, the Tier 1 acronym lookup stated:
> `"K" + (Specimen in {Ser/Plas, Unknown}) -> LOINC 2823-3 (Potassium Ser/Plas)`  
> `"Na" + (Specimen in {Ser/Plas, Unknown}) -> LOINC 2951-2 (Sodium Ser/Plas)`

This directly contradicted Section 4.A's mandatory safety rule: *"Unknown specimen MUST NEVER default to serum."*

### Corrected Policy & Hardening
1. **Strict Elimination of Unknown $\rightarrow$ Serum Default:**
   - If an acronym or token (e.g., `"K"`, `"Na"`, `"Ca"`, `"Cl"`, `"Mg"`, `"PO4"`) is extracted and the document context specimen is **UNKNOWN**, the normalizer is **STRICTLY PROHIBITED** from defaulting to Serum/Plasma LOINC codes (`2823-3`, `2951-2`, etc.).
2. **Deterministic Multi-Tier Acronym Resolution Rule:**
   - **Case A: Explicit Specimen Present in Context:**
     - `"K"` + Specimen `Ser/Plas` $\rightarrow$ LOINC `2823-3` (Potassium [Moles/volume] in Serum or Plasma).
     - `"K"` + Specimen `Urine` $\rightarrow$ LOINC `2828-2` (Potassium [Moles/volume] in Urine).
     - `"Na"` + Specimen `Ser/Plas` $\rightarrow$ LOINC `2951-2` (Sodium [Moles/volume] in Serum or Plasma).
     - `"Na"` + Specimen `Urine` $\rightarrow$ LOINC `2955-3` (Sodium [Moles/volume] in Urine).
   - **Case B: Unknown Specimen Context:**
     - The normalizer queries LOINC for an unassigned/broad specimen code (e.g., whole system).
     - Because LOINC does not provide clinically valid specimen-agnostic codes for electrolyte quantitative values, the normalizer **MUST ABSTAIN** (`normalized_code = None`, `code_system = None`, `code_confidence = None`).
     - The raw extracted entity (e.g. `"K"`, `"4.2"`, `"mEq/L"`) is preserved immutably with full provenance into the FHIR resource with `text = "K"`.
3. **Universal Safeguard for All Clinical Abbreviations:**
   - All entries in the static abbreviation dictionary must carry mandatory `required_specimen` or `required_domain` metadata tags. Any abbreviation entry whose required context is missing from the document header, section title, or local line **fails closed and abstains**.

---

## 2. Reconciled External-Content FTS5 Schema & Build DDL

### Verified Schema Discrepancy in v1.1 / v1.2
Earlier drafts left ambiguities between table column names (`synonyms`, `synonyms_text`, `synonyms_trigram`), making raw FTS5 external-content table synchronization invalid.

### Corrected, Verified Schema Architecture
The SQLite FTS5 subsystem uses an **External Content Table** pattern. The backing storage table is `concepts`, and two separate FTS5 virtual tables index its columns.

```sql
-- 1. Authoritative Backing Relational Table
CREATE TABLE concepts (
    concept_id INTEGER PRIMARY KEY,
    system TEXT NOT NULL,              -- 'LOINC' or 'RxNorm'
    code TEXT NOT NULL,                -- LOINC code or RxCUI
    canonical_name TEXT NOT NULL,      -- Formal release display string
    synonyms_text TEXT NOT NULL,       -- Space-separated unique alias/synonym tokens
    tty TEXT,                          -- RxNorm TTY (IN, SCD, etc.) or LOINC Scale
    attributes_json TEXT NOT NULL      -- Structured 6-axis LOINC or RxNorm strength/form JSON
);

CREATE UNIQUE INDEX idx_concepts_system_code ON concepts(system, code);
CREATE INDEX idx_concepts_system_tty ON concepts(system, tty);

-- 2. Primary Word-Level BM25 External-Content Table
-- Content table points to 'concepts', rowid maps to 'concept_id'
CREATE VIRTUAL TABLE fts_concepts_unicode USING fts5(
    canonical_name,
    synonyms_text,
    content='concepts',
    content_rowid='concept_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- 3. Secondary Substring / OCR Trigram External-Content Table
CREATE VIRTUAL TABLE fts_concepts_trigram USING fts5(
    synonyms_text,
    content='concepts',
    content_rowid='concept_id',
    tokenize='trigram'
);
```

### Deterministic Population & Integrity Check DDL
During database compilation (executed offline in a single atomic transaction):
```sql
BEGIN TRANSACTION;

-- [Ingestion scripts populate the 'concepts' table via parameterized inserts]

-- Synchronize external-content FTS indexes
INSERT INTO fts_concepts_unicode(rowid, canonical_name, synonyms_text)
    SELECT concept_id, canonical_name, synonyms_text FROM concepts;

INSERT INTO fts_concepts_trigram(rowid, synonyms_text)
    SELECT concept_id, synonyms_text FROM concepts;

-- Optimize B-trees for read-only access
INSERT INTO fts_concepts_unicode(fts_concepts_unicode) VALUES('optimize');
INSERT INTO fts_concepts_trigram(fts_concepts_trigram) VALUES('optimize');

COMMIT;

-- Verify index synchronization and structural integrity
PRAGMA integrity_check;
INSERT INTO fts_concepts_unicode(fts_concepts_unicode) VALUES('integrity-check');
INSERT INTO fts_concepts_trigram(fts_concepts_trigram) VALUES('integrity-check');
```
*Note on Runtime Immutability:* Because production deployment is strictly read-only (`mode=ro`), no runtime SQL triggers (`AFTER INSERT / UPDATE / DELETE`) are created on `concepts`. The FTS index is guaranteed 100% synchronized with `concepts` by construction.

---

## 3. Retrieval Trigger & Calibrated Score Definition

### Clarification of the "0.65" Ambiguity Threshold
In v1.2, a threshold of `0.65` was referenced as a trigger for trigram fallback. Raw BM25 scores returned by SQLite FTS5 are unbounded negative values (typically ranging from $-0.5$ to $-35.0$). A raw BM25 score cannot be compared against a decimal threshold.

### Formal Retrieval Scoring & Calibration Procedure
1. **Primary Index Lexical BM25:**
   For a query $q$ against `fts_concepts_unicode`, SQLite computes raw BM25: $S_{\text{raw}}(c) = -\text{bm25}(fts\_concepts\_unicode)$.
2. **Min-Max Relative Normalization:**
   For the retrieved candidate list $C = \{c_1, \dots, c_k\}$ with top raw score $S_{\text{raw}}(c_1)$:
   $$\text{Score}_{\text{norm}}(c_i) = \frac{S_{\text{raw}}(c_i)}{S_{\text{raw}}(c_1)} \in [0.0, 1.0]$$
3. **Platt Scaling / Logistic Calibration on Tuning Split:**
   The absolute confidence $P(\text{Correct} \mid q, c_1)$ is modeled via logistic regression fit on a dedicated **Tuning Partition** (distinct from the test set):
   $$\text{Confidence}(c_1) = \sigma\left( w_1 \cdot S_{\text{raw}}(c_1) + w_2 \cdot (S_{\text{raw}}(c_1) - S_{\text{raw}}(c_2)) + w_3 \cdot \text{Coverage}(q, c_1) + b \right)$$
   Where:
   - $S_{\text{raw}}(c_1) - S_{\text{raw}}(c_2)$ is the score margin separating rank 1 and rank 2.
   - $\text{Coverage}(q, c_1)$ is the fraction of query terms present in the candidate.
4. **Trigram Fallback Trigger Condition:**
   Trigram search is invoked if and only if:
   a) The primary Unicode search returns **zero candidates**; **OR**
   b) The query contains an out-of-vocabulary token and the top candidate's token coverage is $< 0.50$; **OR**
   c) All retrieved Unicode candidates violate hard clinical constraints (e.g. specimen or route mismatch).
5. **Exact-Match Fast Path:**
   Exact string equality against `canonical_name` or any verified synonym bypasses BM25 entirely and receives an exact-match confidence of $1.0000$ (`is_exact = True`).
6. **Removal of "Cohort 1b" Reference:**
   All references to "Cohort 1b" are formally struck. Data partitioning is strictly defined as:
   - **Tuning Partition (40%):** Used exclusively for fitting logistic calibration parameters $(w_1, w_2, w_3, b)$.
   - **Locked Evaluation Test Set (60%):** Kept blind and used only for final reporting.

---

## 4. Value-Fidelity Evidence & Field-by-Field Coverage

### Audit of the 1,130 Linked Claims
In `experiments/end-to-end-evaluation/REPORT.md` (lines 78–82) and `eval/evaluate_end_to_end.py` (lines 73–140), the pipeline evaluated **1,130 claims** that were directly linked into serialized FHIR resources across 34 benchmark documents.

### Field-by-Field Breakdown of Evaluated Claims

| Field Type (`field_type`) | In-Bundle Claims Count | Target FHIR Resource & Property | What the Evaluator Checked | What the Evaluator Did NOT Check |
| :--- | :---: | :--- | :--- | :--- |
| **`lab.value` (Quantitative)** | **648** | `Observation.valueQuantity.value` | Float equivalence ($|v_{\text{fhir}} - v_{\text{claim}}| < 10^{-5}$) | Does not re-evaluate laboratory clinical plausibility. |
| **`lab.value` (Qualitative)** | **81** | `Observation.valueCodeableConcept.coding[0].code` | Canonical string equality ("NEGATIVE", "NORMAL", "POSITIVE") | Does not check external EHR lab order mappings. |
| **`lab.unit`** | **98** | `Observation.valueQuantity.unit` / `.code` | String equality against UCUM code (`unit_raw` preserved) | Does not check non-UCUM legacy clinic abbreviations. |
| **`med.dose`** | **110** | `MedicationRequest.dosageInstruction[0].doseAndRate[0].doseQuantity.value` | Numeric quantity equivalence | Does not evaluate multi-stage tapering dose schedules. |
| **`med.route`** | **81** | `MedicationRequest.dosageInstruction[0].route.text` | Text match against extracted route string | Does not validate SNOMED CT route coding. |
| **`med.name`** | **112** | `MedicationRequest.medicationCodeableConcept.text` | Text display match against extracted drug string | Phase 5 Gap: `.coding[]` was unpopulated (0% RxCUI serialization). |
| **Total In-Bundle Claims** | **1,130** | — | **1,130 / 1,130 value fidelity ($100.0\%$)** | — |

### Precision Boundary on the "100% Value Fidelity" Claim
- **What is Substantiated:** Exactly 1,130 of 1,130 in-bundle claims had their raw extracted string or parsed numeric value faithfully serialized into the corresponding FHIR resource element without corruption or string drop.
- **What is NOT Claimed:** This was **NOT** an assertion of 100% terminology coding correctness (which was 14.02% in Phase 5 due to unpopulated LOINC/RxNorm `.coding[]` blocks), nor an assertion of 100% clinical correctness of the underlying text.

---

## 5. RxNorm Scope & Source Policy

1. **Initial Implementation Scope Limited Strictly to Official CPC:**
   - Milestone `M-TERMINOLOGY-SCALE` will ingest **exclusively** the official monthly **RxNorm Current Prescribable Content (CPC)** distribution archive (`RxNorm_prescribable_MMDDYYYY.zip`).
   - Full monthly release ingestion is formally **removed from Phase 1 scope**.
2. **Exclusion of Full Release Expansion Pending Legal Review:**
   - Any future ingestion of full RxNorm releases requires a separate, dedicated legal and data-lineage review.
   - *Technical Justification:* Applying `WHERE SAB IN ('RXNORM', 'MTHSPL')` on a full release is insufficient proof of redistribution safety because cross-relational tables (`RXNREL.RRF`, `RXNSAT.RRF`) contain foreign keys and attribute strings derived from commercial vocabularies (`NDDF`, `GS`). Using the standalone CPC package eliminates this legal and architectural risk.

---

## 6. SQLite Concurrency & Operational Realities

### Correction of Atomic Swapping on Windows vs POSIX
In v1.2, atomic file replacement was described generically using `os.replace`.
- **Operating System Difference:**
  - On Linux (POSIX), `rename()` atomically unlinks the old file while active processes hold open file descriptors to the old inode.
  - On **Windows (Win32 NTFS)**, `os.replace` fails with `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process` if any worker process holds an open file handle to the destination database.
- **Corrected Versioned Database Publication Architecture:**
  1. The database is published with an explicit versioned filename: `terminology_v20261112.db`.
  2. A pointer file (or registry key / environment variable) tracks the current active database: `active_terminology.json` contains `{"active_db": "terminology_v20261112.db", "version": "1.2.0"}`.
  3. Worker processes check the pointer file at document batch boundaries. When a new version is detected, workers gracefully open the new versioned file and close older connections.
  4. Older database files are unlinked only when all workers have completed connection retirement.

### Concurrency & Contention Realities
- Using `uri=file:...mode=ro&immutable=1` eliminates SQLite-level transaction lock acquisition (`BEGIN IMMEDIATE` / write locks).
- However, **memory bus contention, page cache misses, and OS-level I/O queueing** remain active under high concurrency. Claims of "zero contention" are retracted. Concurrency profiling will be measured under load during Phase 2.

---

## 7. Pipeline Execution Order & Selection Integration

### Pipeline Execution Order
A direct trace of `src/clindoc/jobs/worker.py` (lines 1150–1170) and `src/clindoc/fhir/mapper.py` (lines 1910–2730) proves the exact pipeline order:

```
[Ingest & OCR] 
      │
      ▼
[Stage A/B Extraction] ──► Produces raw Claim entities (field_type, value, unit_raw, bbox)
      │
      ▼
[Stage C NLI Verification] ──► Computes verification verdicts (SUPPORTED, DISCREPANCY, etc.)
      │
      ▼
[Terminology Normalizer] ──► Enriches Claim records with normalized_code, code_system, code_conf
      │                       (Persisted to DB via worker.py:1159)
      ▼
[FHIR Safe Selection] ──► select_deterministic_slot_candidate() in mapper.py
      │                    (Evaluates normalized claims to determine slot winners)
      ▼
[FHIR Bundle Serialization] ──► Builds final Observation & MedicationRequest resources
```

### Direct Proof: Safe Selector Consumes Normalized Codes
Inspection of `src/clindoc/fhir/selection.py` (lines 148–158) confirms that the safe selector **directly consumes** normalized terminology codes for equivalence evaluation:
```python
# src/clindoc/fhir/selection.py (Lines 148-154)
if field_slot == "med.name" or "name" in field_slot:
    # Check RxNorm code match
    if c1.normalized_code and c2.normalized_code:
        if c1.code_system == c2.code_system and c1.normalized_code == c2.normalized_code:
            return True
```
- **Preservation Contract:** `src/clindoc/fhir/selection.py` remains **100% frozen and unmodified**. Upgrading the terminology normalizer provides enriched, valid RxNorm codes to `c1.normalized_code`, allowing the existing selector to prove clinical equivalence between brand and generic mentions without altering the selector logic.

---

## 8. Evaluation Governance, Cohort 5 Authorization, and Regression Suites

1. **Cohort 5 (Real-World Glaucoma Dataset) Authorization:**
   - **Provenance:** `external_eval/glaucoma_med_dataset/` (200 clinical notes, 309 mentions).
   - **Data Governance:** De-identified clinical notes utilized under internal research evaluation protocols with zero direct patient identifiers.
   - **Role:** Used exclusively as a locked, read-only out-of-domain evaluation set for attribute extraction and terminology normalization precision.
2. **Per-Group Safe-Selection Evidence:**
   - The 679 selected / 17 suppressed slot outcomes across the 696 audited groups in `tests/test_fhir_safe_selection.py` are preserved intact. All new terminology integration tests must assert that slot suppression behavior on these 696 groups remains identical.
3. **Mandatory Full Regression Suite at Integration:**
   - Acceptance gating for `M-TERMINOLOGY-SCALE` integration will **NOT** be restricted to the 124-test subset.
   - Gating mandates execution and passing of the **entire project regression test suite** (all 1,700 collected tests across 119 test files).

---

## 9. Summary of Unresolved Risks

| Risk ID | Description | Severity | Management Strategy |
| :--- | :--- | :---: | :--- |
| **RISK-OP-01** | **Windows Handle Locks during Database Upgrade:** Inability to delete retired SQLite files on Windows while background worker processes have lingering connections. | Low | Mitigated via versioned filename publication (`terminology_vYYYYMMDD.db`) and delayed cleanup sweeps. |
| **RISK-ALG-01** | **Electrolyte Multi-Analyte Collisions:** Short acronyms matching multiple non-electrolyte concepts. | Medium | Enforced fail-closed rule: all acronym lookups require explicit specimen context or cleanly abstain. |
| **RISK-PERF-01** | **Cold-Start OS Page Cache Faults:** First query on cold boot incurring disk read latency. | Low | Implement an optional pre-warming query sweep on worker startup (`SELECT count(*) FROM fts_concepts_unicode`). |

---
*End of M-TERMINOLOGY-SCALE v1.2.1 Architectural Correction Sheet.*

