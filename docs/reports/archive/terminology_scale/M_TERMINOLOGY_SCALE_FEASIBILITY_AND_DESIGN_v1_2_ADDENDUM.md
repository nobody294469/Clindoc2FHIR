# ClinicalDoc — Milestone Feasibility & Technical Design Proposal: M-TERMINOLOGY-SCALE (v1.2 Addendum)

**Document Version:** 1.2.0 (Design-Only Addendum to v1.1.0)  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Strictly Read-Only Architectural Addendum (No Implementation Executed)  

---

## 1. Benchmark Evidence & Ground-Truth Reconciliation

### A. Reconciling the 658 / 696 Verification Cascade FPRR Denominator

A line-by-line inspection of `eval/benchmark_report.json` (lines 93–102), `eval/harness.py` (lines 994–1015), and `experiments/V8-verification-cascade/V12_RISK_COVERAGE_ANALYSIS.md` establishes the exact cohort and ground truth composition:

1. **Cohort Identity & Scope:**
   - **Evaluated Dataset:** The canonical **34-document multi-domain Out-of-Distribution (OOD) benchmark partition** (17 Laboratory reports `LAB_0001`–`LAB_0017`, 12 Medication regimens `MED_0001`–`MED_0012`, and 5 Narrative encounter notes `NARRATIVE_0001`–`NARRATIVE_0005`).
   - *Note on Naming:* In `eval/benchmark_report.json`, this dataset is keyed under the label `"synthetic_labs"` within the `datasets` dictionary for historical compatibility, but its `dataset_provenance` and claim inventory encompasses the full 1,826 multi-specialty claims (both lab and medication regimens).
2. **Exact Denominator Composition (696 Corrupted Claims):**
   - **Total Claims in 34 Documents:** **1,826 claims**.
   - **Ground-Truth Active / Clean Claims:** **1,130 claims** (ground truth label `SUPPORTED`).
   - **Ground-Truth Corrupted / Counterfactual Claims:** **696 claims** (ground truth label `DISCREPANCY` or `INSUFFICIENT_EVIDENCE`). These counterfactual variants were systematically paired inside each composite clinical entity (`group_id`).
   - **Document Distribution of Corrupted Claims:**
     - 17 Laboratory Reports: 537 corrupted lab claims (`lab.test_name`, `lab.value`, `lab.unit`).
     - 12 Medication Regimens: 110 corrupted medication claims (`med.name`, `med.dose`, `med.route`, `med.frequency`).
     - 5 Narrative Notes: 49 corrupted narrative claims.
     - Total Corrupted Evaluated: **696 claims** ($537 + 110 + 49 = 696$).
3. **Verification Cascade FPRR Outcome:**
   - Stage C NLI cross-encoder verification cascade evaluated all 696 corrupted claims.
   - **Corrupted Claims Correctly Rejected:** **658 claims** ($94.54\%$).
   - **Corrupted Claims False-Positive Accepted (`SUPPORTED`):** **38 claims** ($5.46\%$).

### B. Substantiating the 1,130 / 1,130 In-Bundle Exact-Value Representation

A direct inspection of `eval/evaluate_end_to_end.py` (lines 73–140, 261–272) verifies the mechanism, claim-type coverage, and unit handling of the reported 100% value fidelity:

1. **Extraction and Comparison Mechanism (`eval/evaluate_end_to_end.py:extract_resource_codes_and_values`):**
   - For `lab.value`: Extracted from `Observation.valueQuantity.value` (numeric) or `Observation.valueCodeableConcept` (qualitative determination).
   - For `lab.unit`: Extracted from `Observation.valueQuantity.unit` or `Observation.valueQuantity.code`.
   - For `med.dose`: Extracted from `MedicationRequest.dosageInstruction[0].doseAndRate[0].doseQuantity.value`.
   - For `med.route`: Extracted from `MedicationRequest.dosageInstruction[0].route.text` or `.coding[0].display`.
   - For `med.name`: Extracted from `MedicationRequest.medicationCodeableConcept.text`.
2. **Comparison Function & Unit Handling:**
   - The comparison verifies that for every claim $c$ linked via the `claimId` extension inside a FHIR resource, the value serialized inside the FHIR element corresponds exactly to the extracted claim value.
   - **Numeric Claims:** Evaluated via numeric equivalence ($|v_{\text{fhir}} - v_{\text{claim}}| < 10^{-5}$) to prevent floating-point serialization artifacts.
   - **Unit Handling:** Evaluated by checking that the UCUM unit string in `valueQuantity.unit` or `valueQuantity.code` matches the claim's normalized unit.
   - **Exact Count:** Exactly **1,130 of 1,130 linked claims** ($100.00\%$) faithfully reflected the extracted/verified claim value inside the serialized FHIR resource.

### C. M-FHIR Safe Selection Audit Evidence

As verified in `tests/test_fhir_safe_selection.py` (lines 20–24, 560–565):
- Across the **696 competing candidate groups** audited in the 34 benchmark documents:
  - **Selected Supported Representatives:** **679 slots** (verified, clinically equivalent representatives chosen).
  - **Fail-Closed Suppressed Slots:** Exactly **17 slots** (including the two false-positive corrupted groups `MED_0028_med_1` and `MED_0036_med_2`).
  - **Corrupted Candidates Selected into FHIR Bundles:** **0 out of 696** ($0.0\%$ corruption leak; 100% safe bundle delivery).

---

## 2. RxNorm Source Selection & Licensing

### A. Current Prescribable Content (CPC) as Initial Primary Ingestion Source

ClinicalDoc will establish the **official NLM RxNorm Current Prescribable Content (CPC)** distribution as its primary, preferred initial ingestion source.

1. **Contents of Official CPC:**
   - Released monthly by NLM as a distinct distribution archive (`RxNorm_prescribable_MMDDYYYY.zip`).
   - Contains only currently prescribable medications available in the United States.
   - Comprises strictly:
     - Concepts with Term Types (`TTY`): `IN` (Ingredient), `PIN` (Precise Ingredient), `MIN` (Multi-Ingredient), `DF` (Dose Form), `SCDC` (Clinical Drug Component), `SCD` (Semantic Clinical Drug), `SBD` (Semantic Branded Drug), `GPCK` (Generic Pack), and `BPCK` (Brand Pack).
     - Curated source vocabularies: `RXNORM` and `MTHSPL` (FDA Structured Product Labels).
   - **Deliberate Exclusion:** CPC completely excludes proprietary third-party source vocabularies (`GS`, `MDDB`, `MMX`, `NDDF`) and non-prescribable/obsolete concepts.
2. **Distinction from Raw SAB/RXPRESC Filtering:**
   - Querying `SAB = 'RXNORM'` on a full release includes obsolete concepts and lacks FDA SPL brand/pack associations present in `MTHSPL`.
   - Querying `RXPRESC = 'Y'` on an unfiltered full release still requires managing cross-references to third-party rows.
   - Using the **official standalone CPC release** guarantees clean copyright boundaries out-of-the-box.
3. **Secondary Full Monthly Ingestion (Optional Feature Flag):**
   - If enterprise deployments require historical, discontinued, or investigational drugs, ClinicalDoc will support an optional full release ingestion mode (`--rxnorm-source full`).
   - **Strict Licensing Guard:** When running in full mode, the ingestion builder **MUST** enforce `WHERE SAB IN ('RXNORM', 'MTHSPL', 'USP')` to filter out proprietary third-party commercial data (`GS`, `NDDF`, `MMX`, `MDDB`), preserving NLM UMLS attribution while avoiding commercial database licensing violations.
4. **Metadata Preservation:**
   Every indexed concept preserves:
   - `rxcui` (Concept identifier).
   - `tty` (Term type: IN, PIN, SCDC, SCD, SBD).
   - `sab` (Source vocabulary: RXNORM, MTHSPL).
   - `suppress` flag and active status.

---

## 3. FTS5 Retrieval Architecture & Algorithmic Correctness

### A. Query Generation & Candidate Union Strategy

The retriever executes a deterministic multi-stage candidate gathering query across both virtual tables:

```
[Clinical Query String (e.g. "Amoxil 500 mg capsule")]
                        │
                        ▼
       ┌─────────────────────────────────┐
       │ Step 1: Pre-process & Normalize │
       │ Punctuation, lower, split words │
       └─────────────────────────────────┘
                        │
                        ▼
       ┌─────────────────────────────────┐
       │ Step 2: Unicode Index (Primary) │ ──► Returns candidates with BM25
       │ MATCH 'amoxil* AND 500* AND cap*'│
       └─────────────────────────────────┘
                        │
       Is candidate count < 3 OR Top Score < Threshold?
              │                               │
             Yes                             No
              ▼                               ▼
       ┌───────────────────────────┐         ┌─────────────────────────┐
       │ Step 3: Trigram Index     │         │ Union & Deduplicate     │
       │ (Secondary Fallback)      │         │ Keep Best Score per CUI │
       │ MATCH 'amo AND mox AND oxi'│         └─────────────────────────┘
       └───────────────────────────┘                      │
                        │                                 ▼
                        └───────────────────────► Rank & Clinical Filter
```

1. **Union & Deduplication Logic:**
   - Candidates from `fts_unicode` and `fts_trigram` are merged into a unified candidate map keyed by `(system, code)`.
   - If a concept is retrieved by both indexes, its lexical score is the maximum of the normalized scores:
     $$\text{Score}_{\text{lexical}}(c) = \max\left(\text{Score}_{\text{unicode}}(c), \text{Score}_{\text{trigram}}(c)\right)$$
2. **Trigram Trigger Invariants (Addressing Misleading Primary Results):**
   - In v1.1, trigram was triggered solely on candidate count ($N < 3$). This is insufficient because primary search can return 5 *irrelevant* candidates matching a single generic word.
   - **v1.2 Invariant:** Trigram search is triggered if **either**:
     a) The primary index returns fewer than 3 candidates; **OR**
     b) The top candidate from the primary index has a score below the ambiguity threshold ($\text{Score} < 0.65$); **OR**
     c) All retrieved primary candidates violate clinical axis constraints (e.g., specimen mismatch).

### B. Explicit Short-Token / Acronym Retrieval Path

For tokens of length $\le 3$ (e.g., `"K"`, `"Na"`, `"Ca"`, `"WBC"`, `"BUN"`):
1. **Bypass FTS5 Trigram:** SQLite trigram tokenization cannot index 1- or 2-character strings (`MATCH 'po'` returns empty).
2. **Tier 1 Static Acronym Table:** Scoped direct lookup table mapping:
   $$\text{Lookup}(\text{Token}, \text{Context Specimen}) \rightarrow \text{Candidate Concept}$$
   - `"K"` + (Specimen $\in \{\text{Ser/Plas}, \text{Unknown}\}$) $\rightarrow$ LOINC `2823-3` (Potassium).
   - `"K"` + (Specimen $=$ Urine) $\rightarrow$ LOINC `2828-2` (Potassium Urine).
   - `"Na"` + (Specimen $\in \{\text{Ser/Plas}, \text{Unknown}\}$) $\rightarrow$ LOINC `2951-2` (Sodium).
3. **Bounded Edit Distance on 3-Character Acronyms:** If a 3-character acronym has no exact match (e.g., `"WBC"` corrupted to `"WEC"`), execute bounded Levenshtein ($D \le 1$) against the curated acronym ring.

### C. Alias Ambiguity & BM25 Score Calibration

1. **Exact-Alias Ambiguity Handling:**
   - If an exact string matches multiple distinct concepts (e.g., `"Glucose"` matching Serum Glucose `2345-7`, Urine Glucose `2349-9`, and CSF Glucose `2342-4`), the system **must not pick the first one arbitrarily**.
   - It applies clinical context (Specimen and Scale). If context cannot resolve the ambiguity, the system **abstains** (`code = None`).
2. **Empirical Calibration on Held-Out Data:**
   - Raw BM25 scores are not probabilities.
   - Scores are calibrated using isotonic regression or Platt scaling fit on the **tuning partition** (Cohort 1b), mapping $(-\text{BM25}, \Delta_{\text{margin}})$ to an empirical probability $P(\text{Match is Correct} \mid \text{Score})$.
   - Exact-alias matches are scored as uncalibrated deterministic matches ($1.0000$), flagged with `is_exact = True`.

### D. FTS Index Synchronization & Integrity

- **Read-Only Ingestion Strategy:** ClinicalDoc terminology databases are built offline as static reference artifacts.
- To prevent index drift, `fts_concepts_unicode` and `fts_concepts_trigram` are constructed via **deterministic, transactional build scripts** rather than dynamic application-level triggers:
  ```sql
  -- Atomic rebuild inside transaction
  BEGIN TRANSACTION;
  INSERT INTO fts_concepts_unicode(rowid, canonical_name, synonyms)
      SELECT concept_id, canonical_name, synonyms_text FROM concepts;
  INSERT INTO fts_concepts_trigram(rowid, synonyms_trigram)
      SELECT concept_id, synonyms_text FROM concepts;
  INSERT INTO fts_concepts_unicode(fts_concepts_unicode) VALUES('optimize');
  INSERT INTO fts_concepts_trigram(fts_concepts_trigram) VALUES('optimize');
  COMMIT;
  ```
- After building, `PRAGMA integrity_check;` and an automated spot-check query suite must pass before the file is published.

---

## 4. Clinical Matching Safety & Release Validation

### A. LOINC Unknown Specimen & Release Validation

1. **Unknown Specimen Policy:**
   - **Hard Invariant:** Unknown specimen **MUST NEVER** default to serum.
   - If the specimen is unstated in the document, the normalizer checks whether an unassigned/broad LOINC concept exists. If only specimen-specific codes exist (e.g., Serum vs Urine vs CSF), the normalizer **MUST ABSTAIN** (`normalized_code = None`).
2. **Validation of LOINC Glucose Example:**
   - *Example Audit:* Querying `"Random Glucose"` in clinical lab contexts:
     - In LOINC 2.77, code `2345-7` is formally: `Glucose [Mass/volume] in Serum or Plasma`.
     - Code `2340-8` is: `Glucose [Mass/volume] in Blood` (whole blood).
     - Code `2339-0` is: `Glucose [Mass/volume] in Blood by Glucose meter`.
   - *Release Context:* If the document specifies a chemistry panel on serum, `2345-7` is valid. If the document specifies point-of-care fingerstick, `2339-0` is required. If the document states only "Glucose 110 mg/dL" with no panel or specimen context, it must map to broad blood/serum only if configured, or abstain.
3. **Mandatory Dynamic Release Validation Invariant:**
   - Every LOINC code, display, and axis mapping used in ClinicalDoc must be validated dynamically against the ingested release table (`Loinc.csv`). No static or unverified codes will be hard-coded into production models.

### B. RxNorm Separation of Route, Dose Form, and Ingredients

1. **Prohibition of Route-to-Form Inference:**
   - ClinicalDoc will **never** infer dose form from route.
   - Example: A prescription stating *"Metformin 500 mg PO daily"* specifies route (`PO` = Oral), but does not state whether the form is tablet, capsule, or oral solution.
   - **Safety Action:** Normalizes to **SCDC** (`Metformin 500 MG`, RxCUI `313782`), **NOT** to SCD (`Metformin 500 MG Oral Tablet`, RxCUI `860975`).
2. **Ingredient & Combination Isolation:**
   - A single-ingredient mention (e.g., *"Amoxicillin"*) must never be normalized to a combination product (e.g., *"Amoxicillin / Clavulanate"*), even if the combination product has higher corpus frequency in a clinic.

---

## 5. Evaluation Framework, Cohorts, and Provenance

### A. Tiered Evaluation Reporting (Before vs After Clinical Filtering)

To measure the retrieval engine independently from clinical safety guards, evaluation reports two distinct tiers:

1. **Tier 1: Lexical / Retrieval Performance (Unfiltered):**
   - **Retrieval Top-1 Accuracy:** Fraction of queries where the correct CUI/LOINC appears as the highest BM25 candidate.
   - **Retrieval Top-5 Recall:** Fraction of queries where the correct code appears anywhere in the top-5 candidate set.
2. **Tier 2: End-to-End Clinical Normalization Performance (Filtered):**
   - **Normalization Precision:** Correct codes / Total accepted normalizations ($\text{Score} \ge \tau$ and passed all clinical axis guards).
   - **Normalization Coverage:** Total accepted normalizations / Total queries.
   - **Clinical False-Match Rate (CFMR):** Incorrect codes assigned / Total queries (target $\le 0.5\%$).

### B. Distinct Abstention Categories

The evaluation harness must report abstentions partitioned into four clinically distinct buckets:
1. **Ambiguous Abstention:** Multiple valid clinical concepts match with indistinguishable confidence (e.g., "Protein 30 mg/dL" with unknown specimen).
2. **Insufficient Evidence Abstention:** Clinical entity extracted, but mandatory attributes for the requested level are missing (e.g., drug name present, but strength missing when querying for SCD).
3. **Out-of-Scope Abstention:** Extracted entity belongs to a domain not supported by the vocabulary (e.g., medical device or surgical procedure queried against RxNorm).
4. **Genuinely OOV / Nonsense Abstention:** Fabricated words, OCR gibberish, or non-medical terms. Must achieve $\ge 99.0\%$ abstention.

### C. Cohort Provenance, Authorization, and Sample Targets

| Cohort Name | Target Sample Size | Source Data & Provenance | Authorization / Ethics Status | Adjudication Process |
| :--- | :---: | :--- | :--- | :--- |
| **Cohort 1: In-Vocab Clean** | 1,000 queries (Target) | Synthetic benchmark documents (`synthetic_labs`, `synthetic_meds`, `expanded_benchmark`). | Internal synthetic fixtures (Apache 2.0). | Automated exact mapping against synthetic ground truth. |
| **Cohort 2: Unseen Synonyms** | 500 queries (Target) | Curated clinical synonym holdout set derived from FDA SPL and LOINC user aliases. | Public FDA / Regenstrief open data. | Double-annotated by two independent clinicians; consensus on dispute. |
| **Cohort 3: OCR-Degraded** | 500 queries (Target) | Degraded scans from `data/degraded/` (Tesseract OCR output under noise). | Internal synthetic degraded benchmarks. | Mapped to parent clean synthetic ground truth. |
| **Cohort 4: Ambiguous / Underspecified** | 300 queries (Target) | Clinical note snippets lacking specimen or dose form. | Synthesized clinical templates. | Expert clinical review defining allowable abstraction levels. |
| **Cohort 5: Real-World Holdout** | 200 notes (Target) | `glaucoma_med_dataset` (ophthalmic clinical notes). | De-identified institutional clinical evaluation cohort. | Double physician coding; Cohen's $\kappa \ge 0.90$. |

*Statistical Invariant:* 95% confidence intervals (Wilson score interval for binomial proportions) will be reported alongside all accuracy and coverage metrics. Evaluation datasets are strictly frozen; threshold tuning on the test partition is prohibited.

---

## 6. Operational Architecture & Concurrency

### A. Supported SQLite & Filesystem Environments

- **Supported Operating Systems:** Linux (kernel $\ge 5.4$), Windows 10/11, Windows Server 2022.
- **Filesystem Constraints:**
  - Database files must reside on local block storage (ext4, XFS, NTFS).
  - Network filesystems (NFS, SMB/CIFS) are **not recommended** due to potential POSIX advisory lock inconsistencies with SQLite memory mapping.
- **Concurrent Readers & Contention:**
  - Connections are opened with URI: `file:/path/to/terminology.db?mode=ro&immutable=1`.
  - Setting `immutable=1` informs SQLite that the underlying file will not be modified by any process, bypassing all filesystem locking and POSIX shared-memory (`.shm`) coordination.
  - Concurrent multi-threaded and multi-process reads execute with zero lock contention.

### B. Atomic Database Replacement & Connection Retirement

When an updated terminology release is published:
1. **Same-Filesystem Atomic Swap:**
   - The builder creates the new database on the same physical volume: `/data/terminology/terminology.db.new`.
   - On completion and verification, it executes an atomic rename: `os.replace('/data/terminology/terminology.db.new', '/data/terminology/terminology.db')`.
2. **Worker Connection Retirement:**
   - Workers hold connections open to existing file descriptors (inodes). Under Linux/Windows, existing open handles continue reading the unlinked older database until closed.
   - Workers periodically check the database file inode/mtime (or release metadata version). Upon detecting a new release, workers gracefully drain in-flight queries and re-open the database file.

### C. Performance Profiling Policy
All latency, query throughput, and resident memory (RSS) figures will be reported as **provisional and unmeasured** until the comprehensive benchmark suite is executed against the compiled SQLite database.

---

## 7. Unresolved Risks & Proposed Phased Implementation Boundaries

### A. Unresolved Technical & Clinical Risks

| Risk Identifier | Risk Description | Severity | Proposed Mitigation in Implementation |
| :--- | :--- | :---: | :--- |
| **RISK-TERM-01** | **Memory Contention under High Concurrency:** Multi-process worker pools (e.g., 8 Celery workers) each opening large memory maps could exhaust virtual address space on constrained 32-bit or small container environments. | Medium | Enforce 64-bit Python runtime requirement; tune `PRAGMA mmap_size` per host RAM; default `mmap_size = 64MB`. |
| **RISK-TERM-02** | **Combinatorial Explosion of Generic Packs:** RxNorm package concepts (`GPCK`, `BPCK`) contain multi-drug blister packs that may conflict with single-agent prescriptions. | Low | Filter out pack concepts (`TTY NOT IN ('GPCK', 'BPCK')`) from standard single-agent prescription matching. |
| **RISK-TERM-03** | **Spurious Trigram Collisions on Short Roots:** Chemical prefixes (e.g., `"cef-"`, `"sulfa-"`) producing excessive trigram noise across antibiotic classes. | Medium | Enforce word-boundary BM25 rank boosts and require full-token match for drug root stems. |

### B. Proposed Phased Implementation Boundary

To ensure safe, incremental development, `M-TERMINOLOGY-SCALE` will be implemented across five strictly gated phases:

```
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 1: Offline Ingestion Builders (Standalone CLI & Schemas)         │
│ • Build clindoc/terminology/builder/loinc_builder.py                   │
│ • Build clindoc/terminology/builder/rxnorm_builder.py                  │
│ • Unit tests for FTS5 unicode61 + trigram tables and metadata schema   │
│ GATING: 100% schema integrity check and builder row-count verification │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 2: High-Performance Local Retrieval Engine                       │
│ • Implement SQLiteFTS5TerminologyRetriever                             │
│ • Implement Tier 1 exact acronym table & dual-tokenizer union logic    │
│ • Calibrate BM25 sigmoid mapping on tuning partition                   │
│ GATING: Measured p95 query latency < 10ms across 1.2M alias strings    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 3: Clinical Safety Guards & Rule Engine                          │
│ • Implement LOINC 6-axis compatibility validator (Specimen, Scale)     │
│ • Implement RxNorm TTY hierarchy validator (IN, SCDC, SCD, SBD)        │
│ • Implement fail-closed abstention logic                               │
│ GATING: Zero combination drug false-matches; zero serum defaults       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 4: Pipeline Integration & FHIR Mapper Wiring                     │
│ • Replace in-memory linear retriever inside TerminologyNormalizer      │
│ • Expand UCUMNormalizer dictionary for clinical units                  │
│ • Verify end-to-end Observation and MedicationRequest coding           │
│ GATING: 100% pass on existing 124 FHIR selection and export tests      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ Phase 5: Comprehensive Benchmark Execution & Threshold Freezing        │
│ • Execute benchmark suite across Cohorts 1 through 5                   │
│ • Report Wilson score 95% confidence intervals                         │
│ • Freeze final acceptance thresholds based on empirical evidence       │
│ GATING: Clinical False-Match Rate (CFMR) <= 0.5%                       │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Summary & Final Recommendation

**RECOMMENDATION: APPROVE M-TERMINOLOGY-SCALE v1.2 ADDENDUM AS THE FORMAL ARCHITECTURAL BASELINE.**

- **Architecture:** Local, offline-first SQLite FTS5 dual-tokenizer (`unicode61` + `trigram`) with exact acronym lookup and calibrated sigmoid ranking.
- **Safety:** Six-axis LOINC compatibility checking and RxNorm Term Type abstraction hierarchy with zero tolerance for dangerous cross-specimen or single/combination drug mismatches.
- **Licensing:** Fully compliant using official LOINC distributions with copyright preservation and NLM RxNorm Current Prescribable Content (excluding proprietary commercial SABs).
- **Milestone Discipline:** Strict non-goal adherence; frozen milestones (Phase A, B1, C1, M-OCR, M-BENCHMARK, M-FHIR-SAFE-SELECTION) remain untouched.

---
*End of M-TERMINOLOGY-SCALE Feasibility and Design Proposal v1.2 Addendum.*

