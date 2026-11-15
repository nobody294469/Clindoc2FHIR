# ClinicalDoc — M-TERMINOLOGY-SCALE v1.2.2 Final Architectural Correction Addendum

**Document Version:** 1.2.2 (Final Design Addendum)  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Strictly Read-Only Architectural Correction (Zero Code, File, or Git Modifications)  

---

## 1. Decoupling Exact-Match Lexical Status from Clinical Correctness

### Architectural Correction
In previous revisions, exact string matching was described as granting an unconditional confidence of `1.0000`. This is clinically hazardous: an exact lexical match to an analyte name (e.g., `"Glucose"`) or drug name (e.g., `"Metformin"`) does **not** prove clinical correctness if document context contradicts mandatory clinical dimensions (such as specimen, scale, or route).

### The Invariant Contract
1. **Separation of Properties:**
   - `is_exact_match: bool` — A strictly lexical property indicating that the normalized query string identically equals the candidate's canonical name or an authorized synonym.
   - `clinical_validity: bool` — An independent semantic property evaluated post-retrieval.
   - `calibrated_confidence: float` — A calibrated probability $P(\text{Match is Clinically Correct} \mid \text{Query, Context, Candidate})$ bounded in $[0.0, 1.0]$.
2. **Mandatory Post-Match Constraint Validation:**
   - Every candidate—**including exact matches**—MUST pass all applicable contextual constraints:
     - **LOINC:** Specimen compatibility (Rule L-SPEC-1/2), Scale compatibility (Rule L-SCALE-1/2), and Unit dimension compatibility (Rule L-UNIT-1).
     - **RxNorm:** Route/form compatibility (Rule R-LEVEL-1/3) and combination product exclusion (Rule R-LEVEL-4).
3. **Fail-Closed on Violation:**
   - If an exact lexical match violates any contextual constraint (e.g., `"Protein"` exact match to Serum Protein when document specifies Urine, or `"Glucose"` exact match to numeric code when document specifies qualitative strip result), the normalizer **MUST REJECT** the candidate and **ABSTAIN** (`normalized_code = None`). An exact lexical match never bypasses clinical safety checks.

---

## 2. Synonym & Alias Schema Architecture: Normalized Alias Table

### Schema Assessment & Recommendation
The space-separated `synonyms_text` string in v1.2.1 is sufficient for full-text search token matching, but it collapses distinct multi-word phrase boundaries (e.g., `"blood urea nitrogen"` vs `"urea blood"`), discards alias-level provenance (e.g., whether an alias originated from LOINC `RELATEDNAMES2`, RxNorm `SAB=MTHSPL`, or an internal abbreviation table), and prevents attaching alias-specific contextual constraints.

### Recommended Normalized Architecture
To maintain high performance without unneeded complexity, the v1.2.2 schema pairs a **normalized relational alias table** with FTS5 external content indexing:

```sql
-- 1. Master Concepts Table (One row per unique code)
CREATE TABLE concepts (
    concept_id INTEGER PRIMARY KEY,
    system TEXT NOT NULL,              -- 'LOINC' or 'RxNorm'
    code TEXT NOT NULL,                -- '2823-3', '723', etc.
    canonical_name TEXT NOT NULL,      -- Primary display string
    tty TEXT,                          -- RxNorm TTY or LOINC Scale
    attributes_json TEXT NOT NULL      -- Structured 6-axis LOINC or RxNorm strength/form JSON
);
CREATE UNIQUE INDEX idx_concepts_system_code ON concepts(system, code);

-- 2. Normalized Synonyms/Aliases Table (Preserves boundaries, source, and context)
CREATE TABLE concept_aliases (
    alias_id INTEGER PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(concept_id),
    alias_text TEXT NOT NULL,          -- Normalized alias string
    alias_source TEXT NOT NULL,        -- 'LOINC_RELATEDNAMES2', 'MTHSPL_BRAND', 'ABBREVIATION_RING'
    required_specimen TEXT,            -- Optional specimen restriction for this specific alias
    is_preferred BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX idx_aliases_concept_id ON concept_aliases(concept_id);
CREATE INDEX idx_aliases_lookup ON concept_aliases(alias_text);

-- 3. External-Content FTS5 Virtual Table (BM25 Lexical Matching)
-- Indexed on alias_text directly from concept_aliases
CREATE VIRTUAL TABLE fts_aliases_unicode USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- 4. Secondary Trigram FTS5 Virtual Table (OCR Typo Fallback)
CREATE VIRTUAL TABLE fts_aliases_trigram USING fts5(
    alias_text,
    content='concept_aliases',
    content_rowid='alias_id',
    tokenize='trigram'
);
```
- **Preservation Advantage:** Exact multi-word aliases are preserved as discrete rows in `concept_aliases`. Phrase boundaries and sources are fully auditable.
- **Query Efficiency:** Lexical search queries `fts_aliases_unicode`, joining back to `concept_aliases` and `concepts` via primary integer keys (`concept_id`).

---

## 3. BM25 Calibration, Score Comparability, and Leakage Prevention

### Formal Calibration Definition
1. **Raw Score Limitation:** SQLite FTS5 `bm25()` returns negative floats ($S_{\text{raw}} \in (-\infty, 0]$). Raw BM25 scores vary significantly with query length, term frequency, and document length, and cannot be interpreted as probabilities.
2. **Missing Competitor / Single Candidate Handling:**
   - If a query returns only a single candidate $c_1$, the score margin $\Delta_{\text{margin}} = S_{\text{raw}}(c_1) - S_{\text{raw}}(c_2)$ is undefined.
   - **Contract:** When $|C| = 1$, $\Delta_{\text{margin}}$ defaults to a fixed uncalibrated constant ($\Delta_{\text{default}} = 1.0$), and the model relies strictly on candidate query coverage and absolute BM25 magnitude.
3. **Platt Logistic Calibration Function:**
   $$\text{Confidence}(c_1) = \sigma\left( w_1 \cdot \tilde{S}(c_1) + w_2 \cdot \Delta_{\text{margin}} + w_3 \cdot \text{Coverage}(q, c_1) + b \right)$$
   Where:
   - $\tilde{S}(c_1) = \frac{-S_{\text{raw}}(c_1)}{\max(1.0, |q|)}$ (length-normalized BM25).
   - $\text{Coverage}(q, c_1) = \frac{|\text{Tokens}(q) \cap \text{Tokens}(c_1)|}{|\text{Tokens}(q)|} \in [0.0, 1.0]$.
   - Coefficients $(w_1, w_2, w_3, b)$ are strictly positive weights fit using cross-entropy loss.
4. **Leakage Prevention: Document-Level Partitioning:**
   - To prevent training/tuning leakage, the calibration model MUST NEVER be fit on queries from the evaluation test set.
   - **Partitioning Hierarchy:** Partitioning is performed at the **Document/Encounter Level**, not the individual mention level.
   - **Tuning Set (40% of documents):** Used exclusively for fitting logistic calibration weights and determining the optimal abstention threshold $\tau$.
   - **Locked Evaluation Set (60% of documents):** Completely isolated. Evaluated only once to report final unbiased metrics.

---

## 4. Structured Abbreviation Context Contract & Fail-Closed Enforcement

### Structured Abbreviation Context Contract
Clinical abbreviations (e.g., `"K"`, `"Na"`, `"AP"`, `"PO"`, `"QD"`) are intrinsically ambiguous without multi-dimensional clinical context.

The abbreviation subsystem defines an explicit, machine-readable contract:
```python
@dataclass(frozen=True)
class AbbreviationEntry:
    token: str                        # e.g., "K", "AP", "Na"
    target_system: str                # "LOINC" or "RxNorm"
    target_code: str                  # e.g., "2823-3", "2951-2"
    target_display: str               # e.g., "Potassium [Moles/volume] in Serum or Plasma"
    required_domain: str              # "LABORATORY" or "PHARMACY"
    required_specimen: Optional[str]  # e.g., "Ser/Plas", "Urine", "CSF"
    required_scale: Optional[str]     # e.g., "Qn", "Ord", "Nom"
    required_route: Optional[str]     # e.g., "Oral", "Ophthalmic"
    disallowed_contexts: tuple[str, ...]# Contexts that invalidate match (e.g. "VITAMIN_K")
```

### Retrieval vs Clinical Normalization Pipeline
1. **Candidate Retrieval Phase:**
   - The token `"K"` retrieves potential candidate matches across all domains (Serum Potassium, Urine Potassium, Vitamin K).
2. **Context Resolution Phase:**
   - The engine checks available document metadata:
     - Document type / Header (e.g., `SECTION: COMPREHENSIVE METABOLIC PANEL` $\rightarrow$ `required_domain = LABORATORY`, `specimen = Ser/Plas`).
     - Explicit line context (e.g., `"Urine Spot Test"` $\rightarrow$ `specimen = Urine`).
3. **Fail-Closed Abstention Invariant:**
   - If `required_specimen` is non-null and the document context specimen is **UNKNOWN** or contradictory, the normalizer **MUST FAIL CLOSED** and abstain (`normalized_code = None`).
   - `"K"` with unknown specimen: **ABSTAIN**.
   - `"K"` under Serum chemistry panel: Normalize to LOINC `2823-3`.
   - `"K"` in medication list ("Vitamin K 5 mg"): Matches `disallowed_contexts` $\rightarrow$ **REJECT LOINC**, evaluate RxNorm.

---

## 5. Scope Boundary of Value-Fidelity Evidence

### Precision Boundary on the "100% Value Representation" Claim
In `experiments/end-to-end-evaluation/REPORT.md` (line 82) and `eval/evaluate_end_to_end.py`, the evaluation demonstrated that **1,130 of 1,130 in-bundle claims** matched the corresponding serialized FHIR elements.

To prevent any misleading claims of project-wide perfection, this metric is strictly bounded:

1. **Explicitly Evaluated Fields (The 6 Evaluated Categories):**
   - `lab.value` (Quantitative): Checked float equality $|v_{\text{fhir}} - v_{\text{claim}}| < 10^{-5}$ in `Observation.valueQuantity.value`.
   - `lab.value` (Qualitative): Checked canonical string equality in `Observation.valueCodeableConcept`.
   - `lab.unit`: Checked exact string equality in `Observation.valueQuantity.unit` / `.code`.
   - `med.dose`: Checked numeric equality in `MedicationRequest.dosageInstruction[0].doseAndRate[0].doseQuantity.value`.
   - `med.route`: Checked text string match in `MedicationRequest.dosageInstruction[0].route.text`.
   - `med.name`: Checked text display match in `MedicationRequest.medicationCodeableConcept.text`.
2. **Untested Claim Attributes & Evaluation Gaps:**
   - The 100% evaluation did **NOT** verify:
     - LOINC or RxNorm `.coding[]` blocks (Phase 5 serialization gap left concept coding at 14.02%).
     - Observation interpretation flags (`Observation.interpretation`).
     - Temporal clinical validity (`Observation.effectiveDateTime`, `MedicationRequest.authoredOn`).
     - Complex dosage structures: prn instructions, tapering dose schedules, or multi-ingredient dispense quantities.
     - Clinical truth or accuracy of the physician's source note.
   - **Formal Wording:** "100% value fidelity across the six evaluated quantitative and text field categories in serialized FHIR bundles", NOT "100% pipeline correctness".

---

## 6. Cohort 5 Governance: Documented Basis & Conditional Dependency

### Authoritative Governance Evidence
An audit of `external_eval/DATA_PROVENANCE.md` (lines 1–61) identifies the exact documented basis for the Glaucoma Medication Dataset (`glaucoma_med_dataset`):
- **Source Origin:** Casey Eye Institute, Oregon Health & Science University (OHSU).
- **Collection Window:** January 2019 – August 2020.
- **De-Identification Basis:** Formally documented as de-identified in accordance with **HIPAA Safe Harbor regulations (45 CFR § 164.514(b)(2))**, with all 18 identifiers removed or replaced with asterisks (`*`).
- **License:** Openly redistributed by upstream authors under the **BSD 3-Clause License** (Copyright (c) 2022, jche253).
- **Frozen Subset:** `glaucoma_eval_subset_200.json` (200 notes, 309 medication mentions).

### Governance Policy in M-TERMINOLOGY-SCALE
- While the dataset is licensed under BSD-3-Clause and de-identified under HIPAA Safe Harbor, ClinicalDoc establishes **Cohort 5 as a CONDITIONAL evaluation dependency**.
- **Rule:** Milestone implementation gating (Phases 1–4) will rely strictly on internal synthetic and open regulatory data (Cohorts 1–4). Cohort 5 will serve as an optional, secondary real-world generalization audit and will **not** block milestone completion if external evaluation restrictions are invoked.

---

## 7. Machine-Readable 696-Group Regression Oracle

### Existence & Verification of the 696-Group Oracle
- **Does a single monolithic file named `oracle_696.json` exist?** **NO.**
- **How the 696 Groups are Authoritatively Stored:**
  The ground-truth oracle for all 696 multi-candidate groups is distributed across the **34 canonical audit reports** located in `data/fhir_phase5_v8/*_audit_report.json` (17 `LAB_*`, 12 `MED_*`, 5 `NARRATIVE_*`).
- **Reproducible Extraction Procedure (`tests/test_fhir_safe_selection.py:TestBenchmarkCandidateSlotAudit`):**
  1. Iterate across all 34 files matching `data/fhir_phase5_v8/*_audit_report.json`.
  2. Parse every claim $c$ and index into slots keyed by the 3-tuple: `(c.document_id, c.group_id, c.field_type)`.
  3. Filter slots where `len(candidates) > 1`. This yields **exactly 696 multi-candidate slots**.
  4. Load the authoritative verification map from each audit report's `verifications` array.
  5. Execute `select_deterministic_slot_candidate()` on each group.
  6. The deterministic baseline yields:
     - **679 selected winners** (supported clean candidates).
     - **17 suppressed slots** (failed closed on clinical discrepancy or unreviewed conflict).
     - **0 corrupted claims selected**.

### Regression Invariant under Terminology Upgrades
- Upgrading terminology normalization enriches `c.normalized_code` on claims.
- **Regression Oracle Rule:** In Phase 4 integration, the 696 slots will be re-evaluated against the frozen audit reports.
- Any change to the 679/17 outcome (e.g., if a previously suppressed slot now resolves because a newly normalized RxNorm code proves equivalence between two brand/generic mentions) **MUST BE EXPLICITLY LOGGED AND CLINICALLY REVIEWED**. Under no circumstances may a corrupted candidate win a slot.

---

## 8. Full Project Regression Suite & SQLite Foreign-Key Isolation

### Actual Test Collection & Execution Procedure
- Rather than assuming an unverified constant, test execution is governed by dynamic pytest collection:
  ```powershell
  # Dynamic Collection
  .\.venv\Scripts\pytest.exe --collect-only -q
  ```
- **Current Baseline:** Exactly **1,700 tests collected across 119 test files** in the active repository.
- **Integration Test Execution Scope:**
  Acceptance of `M-TERMINOLOGY-SCALE` Phase 4 requires running:
  1. All 1,700 existing unit, service, verification, and FHIR export tests.
  2. All newly authored terminology builder, FTS5 retriever, and clinical axis validation tests.
  3. The end-to-end multi-candidate 696-slot benchmark audit.

### Isolation of the SQLite Foreign-Key Infrastructure Flake
- In large multi-file batch test runs, `tests/test_phase_b_milestone1_outbox.py:387` (`test_composite_foreign_key_wiring_sqlite`) intermittently fails due to SQLite's per-connection `PRAGMA foreign_keys = ON;` scoping under SQLAlchemy `QueuePool`.
- **Handling Contract:**
  - This is tracked as an **infrastructure test-isolation issue**, not an M-TERMINOLOGY-SCALE defect.
  - When running full batch suites, this test is isolated via `-k "not test_composite_foreign_key_wiring_sqlite"` or executed in an isolated pytest sub-process, where it consistently passes (14/14).

---

## 9. Summary of Remaining Unresolved Risks

1. **RISK-TERM-04: Terminology DB Storage on Container Mounts:** Deploying the compiled ~450 MB SQLite database on ephemeral Docker containers requires ensuring volume mounts or local image baking does not exceed container disk limits.
2. **RISK-TERM-05: Cold-Start OS Page Ingestion:** Initial access to the FTS5 B-tree on spinning disks or cold cloud nodes may cause p99 latency spikes on the first clinical document.
3. **RISK-TERM-06: Acronym Over-Suppression:** The strict fail-closed unknown-specimen rule will cause high abstention rates on abbreviated hospital notes that omit section headers.

---

## 10. Confirmation of Zero Repository & Git Mutations

In strict adherence to instructions:
- **Source Code, Tests, Configurations, Benchmarks:** Zero modifications.
- **Git Staging & Working Tree:** Exactly **75 staged files preserved untouched**; zero commits, zero staging, zero reset operations.
- **Milestone State:** Phase A, B1, C1, M-OCR, M-BENCHMARK, and M-FHIR-SAFE-SELECTION remain closed, accepted, and frozen.

*End of M-TERMINOLOGY-SCALE v1.2.2 Final Architectural Correction Addendum.*

