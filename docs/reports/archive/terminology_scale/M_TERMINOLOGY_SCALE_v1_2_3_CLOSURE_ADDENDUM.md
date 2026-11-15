# ClinicalDoc — M-TERMINOLOGY-SCALE v1.2.3 Final Architectural Closure Addendum

**Document Version:** 1.2.3 (Final Design Closure)  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Strictly Read-Only Architectural Closure (Zero Code, File, or Git Modifications)  

---

## 1. Reconciling `concept_aliases` Schema with Multi-Dimensional Abbreviation Contract

### Architectural Reconciliation
The previous draft in v1.2.2 introduced a flat `required_specimen` column in `concept_aliases`. This is insufficient for clinical abbreviations and multi-use terms, which require validation across multiple clinical axes simultaneously (e.g., domain, scale, route, and multiple allowable specimen types).

### Normalized Schema Design
To support rich, multi-dimensional clinical constraints without unneeded complexity or denormalization:

```sql
-- 1. Master Concepts Table (One row per unique standard concept)
CREATE TABLE concepts (
    concept_id INTEGER PRIMARY KEY,
    system TEXT NOT NULL,              -- 'LOINC' or 'RxNorm'
    code TEXT NOT NULL,                -- e.g. '2823-3', '723'
    canonical_name TEXT NOT NULL,      -- Formal release display string
    tty TEXT,                          -- RxNorm TTY (IN, SCD, etc.) or LOINC Scale (Qn, Ord, Nom)
    attributes_json TEXT NOT NULL      -- Structured JSON representing LOINC 6 axes or RxNorm components
);
CREATE UNIQUE INDEX idx_concepts_system_code ON concepts(system, code);
CREATE INDEX idx_concepts_system_tty ON concepts(system, tty);

-- 2. Master Aliases Table (Normalized alias strings and provenance)
CREATE TABLE concept_aliases (
    alias_id INTEGER PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(concept_id),
    alias_text TEXT NOT NULL,          -- Normalized alias string (e.g. "k", "potassium", "amoxil")
    alias_source TEXT NOT NULL,        -- 'LOINC_RELATEDNAMES2', 'MTHSPL_BRAND', 'ABBREVIATION_REGISTRY'
    is_abbreviation BOOLEAN NOT NULL DEFAULT 0,
    is_preferred BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX idx_aliases_concept_id ON concept_aliases(concept_id);
CREATE INDEX idx_aliases_text ON concept_aliases(alias_text);

-- 3. Normalized Contextual Restrictions Table (0..N restrictions per alias)
CREATE TABLE alias_context_rules (
    rule_id INTEGER PRIMARY KEY,
    alias_id INTEGER NOT NULL REFERENCES concept_aliases(alias_id),
    dimension TEXT NOT NULL,           -- 'SPECIMEN', 'SCALE', 'ROUTE', 'DOMAIN', 'DISALLOWED_CONTEXT'
    operator TEXT NOT NULL,            -- 'IN', 'EQUALS', 'NOT_IN'
    allowed_values_json TEXT NOT NULL  -- e.g. '["Ser/Plas", "Bld"]', '["Qn"]', '["Oral"]'
);
CREATE INDEX idx_rules_alias_id ON alias_context_rules(alias_id);

-- 4. External-Content FTS5 Virtual Table (BM25 Lexical Matching)
CREATE VIRTUAL TABLE fts_aliases_unicode USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- 5. Secondary Trigram FTS5 Virtual Table (OCR Typo Fallback)
CREATE VIRTUAL TABLE fts_aliases_trigram USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='trigram'
);
```

### Multi-Dimensional Context Evaluation Contract
When an alias $a$ is retrieved:
1. The engine joins `alias_context_rules WHERE alias_id = a.alias_id`.
2. For each rule:
   - If `dimension = 'SPECIMEN'`, verify `document_specimen IN allowed_values`. If `document_specimen` is **UNKNOWN** and the rule requires a specific specimen, **FAIL CLOSED AND ABSTAIN**.
   - If `dimension = 'DISALLOWED_CONTEXT'`, verify no matching negation token exists (e.g., `"VITAMIN_K"` token invalidates potassium match).
   - If any rule is violated, candidate $a$ is immediately dropped.

---

## 2. Independent 696-Group Regression Baseline

### A. Evidence Audit: Does a Machine-Readable Independent Oracle Exist?
- **Honest Finding:** **NO**, an independent pre-existing static file specifying the expected slot selection winner, suppression reason, and supporting rationale for each of the 696 groups does **not** currently exist on disk.
- **What Actually Exists:**
  - The 34 canonical audit reports (`data/fhir_phase5_v8/*_audit_report.json`) contain the raw extracted `claims` (1,826 claims) and their Stage C NLI `verifications` (1,826 verdicts).
  - The test `tests/test_fhir_safe_selection.py:TestBenchmarkCandidateSlotAudit` establishes an aggregate behavioral assertion: when `select_deterministic_slot_candidate` is executed over the 696 multi-candidate groups reconstructed from these audit reports, exactly **679 winners** and **17 suppressed slots** are produced.
  - The 34 serialized FHIR bundles (`data/fhir_phase5_v8/*_fhir_bundle.json`) reflect legacy Phase 5 serialization before `M-FHIR-SAFE-SELECTION` remediation.

### B. Baseline Materialization Procedure (Phase 1 Prerequisite)
To prevent conflating "rerunning the current code" with an independent ground-truth oracle, Phase 1 implementation will formally materialize a frozen baseline artifact:
1. **Materialization Step:**
   Execute a deterministic snapshot script over the frozen `data/fhir_phase5_v8/*_audit_report.json` files to generate:
   `tests/fixtures/fhir_safe_selection_696_oracle.json`
2. **Recorded Structure per Group:**
   ```json
   {
     "group_key": "LAB_0005:group_11:lab.value",
     "document_id": "LAB_0005",
     "group_id": "group_11",
     "field_type": "lab.value",
     "candidate_ids": ["c1", "c2"],
     "baseline_verdict": "SUPPRESSED",
     "baseline_selected_claim_id": null,
     "suppression_reason": "INCOMPATIBLE_PAIRWISE_EQUIVALENCE",
     "evidence_summary": "Candidate AP 510 U/L conflicts with AP 51 U/L; pairwise check failed closed."
   }
   ```
3. **Review Governance for Terminology-Induced Shifts:**
   - Terminology normalization enriches claims with `normalized_code` prior to slot selection.
   - If a previously suppressed slot in the 696 oracle shifts to selecting a winner (e.g., two distinct brand/generic drug names are now recognized as clinically equivalent via matching RxCUIs), this change **MUST NOT OCCUR SILENTLY**.
   - The test harness will assert:
     $$\text{Winners} \ge 679, \quad \text{Corrupted Selected} == 0$$
   - Any diff against the frozen `oracle.json` will require an explicit automated diff report detailing the before/after CUI, the evidence of clinical equivalence, and manual architectural sign-off before acceptance.

---

## 3. Full Project Regression Execution Protocol

### Complete Suite Execution Protocol
Integration acceptance will not use a cherry-picked test subset or silently exclude tests. The regression protocol mandates running the complete test inventory and reporting isolated executions separately:

1. **Step 1: Complete Full-Suite Batch Execution:**
   ```powershell
   .\.venv\Scripts\pytest.exe -v
   ```
   - Executes the complete suite of all dynamically collected tests (currently ~1,700 tests across 119 files).
   - Captures all passes, skips, and failures.
2. **Step 2: Dedicated Independent Execution of `test_composite_foreign_key_wiring_sqlite`:**
   ```powershell
   .\.venv\Scripts\pytest.exe tests/test_phase_b_milestone1_outbox.py -k "test_composite_foreign_key_wiring_sqlite" -v
   ```
3. **Step 3: Dual Reporting Requirement:**
   - The final milestone report must document:
     a) Full-suite batch test results (including whether the SQLite foreign-key test succeeded or exhibited intermittent behavior during the batch run).
     b) Isolated test result for `test_composite_foreign_key_wiring_sqlite`.
   - **Causal Statement:** The batch failure is documented as an **unresolved test execution anomaly observed during large batch runs**, without presuming or claiming its root cause is definitively proven until dedicated database fixture isolation experiments are conducted under milestone `M-ALEMBIC-MIGRATIONS`.

---

## 4. Single-Candidate Calibration & Multi-Feature Logistic Scoring

### A. Mathematical Formulation of Multi-Feature Logistic Calibration
In v1.2.2, Platt scaling was incorrectly cited for a multi-variable model, and an arbitrary missing-margin constant was proposed. This is corrected:

1. **Feature Vector Representation $\vec{x}(q, c)$:**
   For a query $q$ and candidate concept $c$, define a 4-dimensional feature vector:
   $$\vec{x}(q, c) = \begin{bmatrix}
   \tilde{S}_{\text{BM25}}(c) \\
   \text{Coverage}(q, c) \\
   \Delta_{\text{margin}}(c) \\
   I_{\text{single}}
   \end{bmatrix}$$
   Where:
   - $\tilde{S}_{\text{BM25}}(c) = \frac{-S_{\text{raw}}(c)}{\max(1, |\text{Tokens}(q)|)}$ (query-length normalized BM25 magnitude $\ge 0$).
   - $\text{Coverage}(q, c) = \frac{|\text{Tokens}(q) \cap \text{Tokens}(c)|}{|\text{Tokens}(q)|} \in [0.0, 1.0]$.
   - $I_{\text{single}} \in \{0, 1\}$ is an **explicit binary missing-margin indicator**:
     $$I_{\text{single}} = \begin{cases} 1 & \text{if } |C| = 1 \text{ (only a single candidate retrieved)} \\ 0 & \text{if } |C| > 1 \end{cases}$$
   - $\Delta_{\text{margin}}(c)$ is the score margin separating rank 1 and rank 2:
     $$\Delta_{\text{margin}}(c) = \begin{cases} S_{\text{raw}}(c_1) - S_{\text{raw}}(c_2) & \text{if } |C| > 1 \\ 0.0 & \text{if } |C| = 1 \text{ (documented zero numeric representation)} \end{cases}$$
2. **Multi-Feature Logistic Calibration Function:**
   The calibrated probability of clinical correctness is modeled via standard multivariate logistic regression:
   $$P(\text{Correct} \mid \vec{x}) = \sigma\left( \vec{w}^T \vec{x} + b \right) = \frac{1}{1 + e^{-(\vec{w}^T \vec{x} + b)}}$$
   - **Removal of Positive Weight Constraints:** Weights $\vec{w} = [w_1, w_2, w_3, w_4]$ are estimated unconstrained via maximum likelihood estimation (L2-regularized logistic regression) on the training/tuning split. Coefficients are not artificially forced to be positive.
3. **Calibration Quality Evaluation:**
   Calibration quality will be evaluated using **Expected Calibration Error (ECE)** and **Brier Score** computed across reliability bins on the locked evaluation test set.

---

## 5. Calibration Data Partitioning & Specimen Inference Evidence

### A. Document/Encounter-Level Data Partitioning

To ensure zero information leakage, partitioning is performed strictly at the **Document Level**:

| Split Role | Document Count | Source Distribution | Total Queries (Est.) | Primary Function |
| :--- | :---: | :--- | :---: | :--- |
| **Tuning Partition (40%)** | **14 documents** | 7 Lab (`LAB_*`), 5 Med (`MED_*`), 2 Narrative (`NARRATIVE_*`) | ~730 queries | Fitting logistic calibration weights $\vec{w}, b$; determining optimal abstention threshold $\tau$. |
| **Locked Test Partition (60%)** | **20 documents** | 10 Lab (`LAB_*`), 7 Med (`MED_*`), 3 Narrative (`NARRATIVE_*`) | ~1,096 queries | Unbiased evaluation of Top-1/Top-5 accuracy, coverage, CFMR, and ECE with Wilson 95% CIs. |

### B. Contextual Specimen Evidence Rules: Section Headers vs Line Context

To maintain clinical safety while enabling realistic document parsing:

1. **When Section Headers Constitute Sufficient Specimen Evidence:**
   - A section heading provides valid specimen context **if and only if** the heading contains an unambiguous, standard specimen term:
     - `SECTION: URINALYSIS` $\rightarrow$ Specimen context = `Urine`.
     - `SECTION: CEREBROSPINAL FLUID ANALYSIS` $\rightarrow$ Specimen context = `CSF`.
     - `SECTION: COMPREHENSIVE METABOLIC PANEL` $\rightarrow$ Specimen context = `Ser/Plas` (established standard laboratory compendium practice; CMP is universally performed on serum/plasma).
2. **When Explicit Line Evidence is Required:**
   - If the section heading is generic, ambiguous, or absent:
     - `SECTION: LABORATORY RESULTS`
     - `SECTION: MISCELLANEOUS TESTS`
     - Header absent or unparsed.
   - **Rule:** Under generic headings, individual test lines **MUST contain explicit local specimen evidence** (e.g., `"Urine Sodium"`, `"Pleural Fluid Protein"`).
   - If neither the header nor the test line provides explicit specimen evidence, **Specimen Context = UNKNOWN**.
3. **The Unknown-Specimen Abstention Invariant (Re-Affirmed):**
   - If Specimen Context = `UNKNOWN`, the normalizer **MUST NEVER** default to serum. If the candidate LOINC concepts require specific specimens (Serum vs Urine vs CSF), the normalizer **MUST FAIL CLOSED AND ABSTAIN** (`normalized_code = None`).

---

## 6. Summary of Remaining Unresolved Risks

1. **RISK-TERM-07: Incomplete Specimen Ontologies in Legacy Notes:** Highly unstructured clinical notes that describe lab tests in narrative paragraphs without standard headers may suffer high abstention rates under the strict fail-closed specimen invariant.
2. **RISK-TERM-08: Database Compilation Memory on Constrained Hosts:** Building the full LOINC (~102k concepts, ~450k aliases) and RxNorm CPC databases in a single SQLite transaction requires allocating sufficient temporary buffer space (~1 GB disk during index compilation).

---

## 7. Confirmation of Zero Repository & Git Mutations

In strict adherence to instructions:
- **Zero Modifications:** No source code, tests, configurations, database files, or existing benchmark artifacts were modified.
- **Git State:** 100% untouched. All 75 pre-existing staged files remain preserved intact; zero commits, zero staging, zero reset operations.
- **Milestones:** Phase A, B1, C1, M-OCR, M-BENCHMARK, and M-FHIR-SAFE-SELECTION remain closed, accepted, and frozen.

*End of M-TERMINOLOGY-SCALE v1.2.3 Final Architectural Closure Addendum.*

