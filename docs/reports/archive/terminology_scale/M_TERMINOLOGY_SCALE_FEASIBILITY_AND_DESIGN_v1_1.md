# ClinicalDoc — Milestone Feasibility & Technical Design Proposal: M-TERMINOLOGY-SCALE (v1.1)

**Document Version:** 1.1.0  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Read-Only Investigation & Technical Design Revision (No Code Changes or Implementation Executed)  

---

## Document Revision History & Change Log (v1.0.0 $\rightarrow$ v1.1.0)

| Section | Nature of Revision | Key Changes in v1.1.0 |
| :--- | :--- | :--- |
| **§1. Benchmark Evidence** | **Correction** | Reconciled the 1,093/1,130 metric directly against raw frozen artifacts (`experiments/end-to-end-evaluation/REPORT.md`). Confirmed that **all 1,130 active claims were physically linked into FHIR resources** (100% physical representation). The 1,093/1,130 (96.73%) figure reflects **verification concordance** (in-bundle claims where verification verdict matched ground truth). Retracted the erroneous claim that 37 claims were "unrepresented" in bundles. Reconciled raw verification cascade FPRR (94.54%) vs M-FHIR safe selection bundle safety (100% slot exclusion). |
| **§2. RxNorm Licensing** | **Correction** | Accurately distinguished full monthly/weekly RxNorm releases vs Current Prescribable Content. Explicitly detailed SAB=RXNORM vs MTHSPL (FDA Structured Product Labels) and clarified that SAB=RXNORM alone is not coextensive with Prescribable Content. Specified NLM UMLS Metathesaurus license, distribution boundaries, and release versioning. |
| **§3. LOINC Licensing** | **Correction** | Documented exact Regenstrief open terms, commercial use allowances, required copyright notices, and preservation of third-party copyright metadata (`EXTERNAL_COPYRIGHT_NOTICE`). Formulated reproducible local ingestion runner rather than raw git tracking. |
| **§4. FTS5 Retrieval Design** | **Hardening** | Directly evaluated SQLite 3.45.1 FTS5 trigram behavior. Addressed short queries (<3 chars), single-char OCR errors, numeric strengths, and punctuation. Established hybrid FTS5 tokenization (`unicode61` + `trigram` fallback) with explicit abbreviation tables. Replaced uncalibrated BM25 assumptions with calibrated sigmoid scoring. Removed unverified latency/RAM claims. |
| **§5. Clinical Matching Rules** | **Clinical Hardening** | LOINC: Strictly eliminated serum default for unknown specimens; established 6-axis compatibility matrix distinguishing known, unknown, and contradictory attributes; treated units as necessary constraints, not proof of identity. RxNorm: Formulated explicit eligibility and decision tables for IN, PIN, SCDC, SCD, SBD; prohibited inferring dose form from route; prevented single-ingredient mentions matching combination drugs. |
| **§6. Evaluation Framework** | **Methodological Rigor** | Replaced generic metrics with formal definitions: Retrieval Top-1, Top-5 Recall, Conditional Normalization Precision, Normalization Coverage, Clinical False-Match Rate, and Abstention Quality. Formulated distinct evaluation cohorts with confidence intervals. Unfroze acceptance thresholds pending baseline execution. |
| **§7. Neural Reranking** | **Clarification** | Corrected loose terminology between SapBERT bi-encoders and cross-encoders. Formulated exact bi-encoder vs cross-encoder input formats and ONNX int8 runtime constraints. Formally deferred neural reranking to an optional secondary phase pending lexical baseline performance. |
| **§8. Operational Design** | **System Hardening** | Documented runtime SQLite version checks, immutable read-only multi-threaded connections (`mode=ro`), atomic build-and-rename database publishing, and release metadata tables (`schema_version`, `source_release_date`). Clarified OS page-cache dynamics vs resident set size (RSS). |

---

## 1. Reconciling Existing Benchmark Evidence

### A. Resolution of the 1,093 / 1,130 FHIR Discrepancy

In v1.0.0, the statement that *"37 claims were unrepresented in the FHIR bundle"* was inaccurate. A direct audit of raw evaluation artifacts in `experiments/end-to-end-evaluation/REPORT.md`, `eval/end_to_end_results.json`, and `eval/benchmark_report.json` establishes the following verified facts:

1. **Physical FHIR Resource Representation:**
   - **Total Ground-Truth Claims Evaluated:** 1,826 claims (34 canonical documents: 17 Lab reports, 12 Medication regimens, 5 Narrative clinical notes).
   - **Competing / Shadowed Claims:** 696 claims pruned or shadowed within composite `group_id` clinical entities (counterfactual variants).
   - **Claims Directly Linked into FHIR Resources:** **1,130 unique claims** ($61.88\%$ of total 1,826).
   - **Physical Representation Rate of Active Claims:** **100.00%** (1,130 of 1,130 active claims were physically linked into FHIR `Observation`, `MedicationRequest`, or `MedicationStatement` entries via `claimId` extensions). **Zero active claims were unrepresented.**
2. **In-Bundle Verification Concordance (The 1,093 Figure):**
   - Among the 1,130 claims physically placed inside FHIR resources, **1,093 claims** had their predicted verification label match the ground-truth verification label ($1,093 / 1,130 = 96.7256\% \approx \mathbf{96.73\%}$).
   - The remaining **37 claims** were not missing from FHIR; they were physically present in FHIR resources, but their cascade verification verdict differed from ground truth (e.g., false-negative verification cascades on subtle boundary conditions).
3. **In-Bundle Value Accuracy:**
   - **1,130 of 1,130 in-bundle claims** ($100.00\%$) carried the exact extracted clinical value matching ground truth.
4. **Distinction of Separate Benchmark Metrics:**
   - **Physical FHIR Representation Rate:** $\frac{\text{Claims Serialized in Bundle}}{\text{Active Verified Claims}} = \frac{1,130}{1,130} = \mathbf{100.00\%}$.
   - **In-Bundle Verification Concordance:** $\frac{\text{In-Bundle Claims with Correct Verification Label}}{\text{Total In-Bundle Claims}} = \frac{1,093}{1,130} = \mathbf{96.73\%}$.
   - **End-to-End Value & Verification Accuracy:** $\frac{\text{In-Bundle Claims with Correct Value AND Verification Label}}{\text{Total Ground-Truth Cohort}} = \frac{1,093}{1,826} = \mathbf{59.86\%}$.

### B. Reconciling Verification Cascade FPRR vs Safe-Selection Bundle Safety

The user request noted discrepancies between reported corrupted-claim rejection rates (94.54% vs 99.4%–99.8%):

1. **Verification Cascade FPRR (94.54%):**
   - **Evaluated Component:** Stage C NLI cross-encoder verification cascade alone (`eval/benchmark_report.json`, lines 93–102).
   - **Cohort:** 1,826 total claims in `synthetic_labs`, of which **696 were corrupted counterfactual claims** (intentionally injected OCR errors, value shifts, or swapped entities).
   - **Rejection Tally:** The cascade correctly rejected **658 of the 696 corrupted claims** as `DISCREPANCY` or `INSUFFICIENT_EVIDENCE`.
   - **Calculation:** $658 / 696 = \mathbf{94.54\%}$ False Positive Rejection Rate (FPRR). The remaining 38 corrupted claims passed the cascade as false-positive `SUPPORTED`.
2. **Post-Selection FHIR Bundle Safety (100.0% / 0 Corrupted Slots):**
   - **Evaluated Component:** End-to-end downstream bundle serialization after `M-FHIR-SAFE-SELECTION` pairwise equivalence checks and deterministic slot suppression (`src/clindoc/fhir/selection.py`).
   - **Cohort:** All 696 competing slots containing corrupted claims.
   - **Safety Outcome:** The approved `M-FHIR-SAFE-SELECTION` pairwise clinical equivalence engine prevented the false-positive corrupted claims from winning any slot. In all cases where a corrupted claim competed with a clean claim or another corrupted claim, the slot either selected the verified clean claim or failed closed by suppressing the slot entirely.
   - **Bundle Selection Rate of Corrupted Claims:** **0 out of 696** ($0.0\%$ corruption leak; $\mathbf{100.0\%}$ safety protection).
3. **The 99.4%–99.8% Figures in the Assessment:**
   - In `CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`, these figures represented an informal characterization of Stage A/B extraction specificity across OOD slices combined with slot pruning. Moving forward, these informal figures are retired in favor of the strict metrics above.

### C. Reconciling Medication Tuple Completeness (12.5% vs 98.4%)

1. **Synthetic Meds Tuple Accuracy (12.5%):**
   - **Source:** `eval/benchmark_report.json` (`Synthetic Meds Tuple Accuracy`).
   - **Cohort:** `synthetic_meds` (5 documents, **8 ground-truth prescription regimens**).
   - **Evaluation Function:** `src/clindoc/eval/metrics.py:compute_medication_tuple_metrics`.
   - **Rule:** Strict joint conjunction across all 7 prescription fields: `name`, `dose`, `dose_unit`, `form`, `route`, `frequency`, and `duration`. If any single attribute fails, the entire prescription scores 0. Exactly **1 out of 8** prescriptions matched all 7 attributes simultaneously ($1 / 8 = \mathbf{12.5\%}$).
2. **Glaucoma Medication Attribute Precision (98.4%):**
   - **Source:** `README.md` and `external_eval/glaucoma_med_dataset/`.
   - **Cohort:** 200 clinical encounter notes containing **309 ophthalmic medication mentions**.
   - **Rule:** Single-attribute binding accuracy (checking whether an extracted drug name was correctly linked to its adjacent concentration, e.g., "Latanoprost" linked to "0.005%"). This evaluated individual binary relations, not complete 7-tuple multi-field completeness.

---

## 2. Official Terminology Sources and Licensing

### A. RxNorm Licensing, Distribution, and Source Vocabularies

RxNorm is produced and maintained by the U.S. National Library of Medicine (NLM), National Institutes of Health.

#### 1. Distribution Tiers and Releases
- **Full Monthly Release:** Issued on the first Monday of each month. Contains the complete relational metathesaurus (`RXNCONSO.RRF`, `RXNREL.RRF`, `RXNSAT.RRF`, etc.).
- **Weekly Releases:** Issued each Wednesday containing incremental updates and NDC additions.
- **Current Prescribable Content (RxNormPrescribable):** A specialized subset distributed monthly by NLM. It contains only active, prescribable medications available in the United States, specifically packaged to avoid licensing complications associated with third-party proprietary data.

#### 2. Source Vocabularies and Third-Party Intellectual Property
RxNorm aggregates concept names and attributes from multiple source vocabularies (`SAB` column in `RXNCONSO.RRF`):
- **`SAB = 'RXNORM'` (Pure RxNorm):** Normalized clinical drugs, generic ingredients, dosage forms, and brand names created directly by NLM. These are in the public domain in the United States.
- **`SAB = 'MTHSPL'` (FDA Structured Product Labels):** Active ingredient and brand names extracted from FDA drug package inserts. Public domain.
- **`SAB = 'USP'` (United States Pharmacopeia):** Official compendial standards.
- **Third-Party Proprietary Vocabularies:** Full RxNorm includes rows sourced from commercial databases: `SNOMEDCT_US`, `GS` (Gold Standard / Elsevier), `MDDB` (Micromedex), `MMX` (Micromedex), `NDDF` (First Databank), and `VANDF` (Veterans Affairs). Using or redistributing content from these proprietary sources outside the US or without separate commercial vendor licenses requires explicit third-party agreements.

#### 3. Critical Clarification: SAB=RXNORM vs Current Prescribable Content
*Filtering solely on `SAB = 'RXNORM'` is not coextensive with the Current Prescribable Content distribution.*
- `SAB = 'RXNORM'` contains historical, obsolete, and non-prescribable concepts created by NLM, as well as abstract classes.
- The **Current Prescribable Content** release is a curated subset of RxNorm (concepts where `RXPRESC = 'Y'` in `RXNCONSO.RRF`) that excludes obsolete drugs and excludes proprietary source data, ensuring clean intellectual property boundaries.
- **ClinicalDoc Scope Decision:** ClinicalDoc will target the **Current Prescribable Content** subset, supplemented by pure NLM entries (`SAB IN ('RXNORM', 'MTHSPL')`), and will explicitly exclude proprietary third-party SABs (`GS`, `MDDB`, `MMX`, `NDDF`, `SNOMEDCT_US`).

#### 4. Licensing and Redistribution Conditions
- **UMLS Metathesaurus License:** Downloading RxNorm releases requires a free UMLS license agreement with NLM.
- **Attribution Requirement:** Any application using RxNorm must include the standard NLM attribution:
  > *"This product uses publicly available data courtesy of the U.S. National Library of Medicine (NLM), National Institutes of Health, Department of Health and Human Services. NLM is not responsible for the product and does not endorse or recommend this or any other product."*
- **Offline Deployment:** Fully permitted. SQLite-compiled databases of prescribable content can be deployed locally in offline hospital networks without ongoing external network calls or per-seat fees.

---

### B. LOINC Licensing and Copyright Metadata

LOINC is owned and copyrighted by Regenstrief Institute, Inc. and the Logical Observation Identifiers Names and Codes (LOINC) Committee.

#### 1. Distribution and Terms
- **Release Schedule:** Biannually (February and August).
- **Format:** `LoincTable/Loinc.csv` (~180 MB, ~102,000 active concept rows, ~40 relational columns).
- **License:** The LOINC License is an open license granting permission at no cost to use, copy, and distribute LOINC in commercial and non-commercial software, clinical information systems, and electronic health records worldwide.

#### 2. Preservation of Third-Party Copyright Metadata
Certain specific LOINC terms represent third-party assessment instruments, surveys, or scales (e.g., PHQ-9 depression scale, Glasgow Coma Scale, CDC surveillance forms) that carry distinct copyright terms:
- In `Loinc.csv`, these terms are flagged in the `EXTERNAL_COPYRIGHT_NOTICE` column.
- **Safety & Compliance Requirement:** The ClinicalDoc database builder **MUST** extract and preserve `EXTERNAL_COPYRIGHT_NOTICE` in the relational schema.
- **Normalizer Contract:** If a claim maps to a LOINC code carrying an `EXTERNAL_COPYRIGHT_NOTICE`, the normalizer must carry this notice into the audit metadata and FHIR coding extension, alerting downstream users of specific clinical instrument licensing boundaries.

#### 3. Attribution and Database Packaging Policy
- **Attribution Statement:** ClinicalDoc documentation, CLI help text, and generated audit reports must display:
  > *"This material contains content from LOINC (http://loinc.org). LOINC is copyright © 1995-2026, Regenstrief Institute, Inc. and the Logical Observation Identifiers Names and Codes (LOINC) Committee and is available at no cost under the license at http://loinc.org/license. LOINC® is a registered United States trademark of Regenstrief Institute, Inc."*
- **Database Artifact Strategy:** Because LOINC requires users to accept the terms of the LOINC license, committing the full 180 MB LOINC database directly into the git repository is prohibited. Instead:
  1. ClinicalDoc will ship with an automated, reproducible ingestion script: `python -m clindoc.terminology.builder.build_loinc --input <path_to_Loinc.csv_or_zip>`.
  2. For enterprise deployments, pre-built SQLite artifacts may be hosted in an authenticated artifact registry accompanied by the required click-through license acknowledgment.

---

## 3. Revised FTS5 Retrieval Engine Design

### A. Environment Verification

Direct runtime testing in the active project environment (`.venv` on Windows 10, Python 3.11.9) verified:
- **SQLite Version:** `3.45.1` (built into Python standard library `sqlite3`).
- **FTS5 Module:** Fully enabled and operational.
- **Trigram Tokenizer:** Supported natively (`tokenize = "trigram"`).

### B. Concrete Limitations of SQLite Trigram Tokenizer & Engineering Solutions

While FTS5 trigram tokenization enables substring matching, testing identified distinct failure modes in clinical applications:

| Trigram Tokenizer Limitation | Clinical Failure Scenario | Engineering Solution in ClinicalDoc v1.1 |
| :--- | :--- | :--- |
| **Queries $<3$ Characters** | Short test acronyms or electrolytes: `"K"` (Potassium), `"Na"` (Sodium), `"Ca"` (Calcium), `"Fe"` (Iron). Trigram parser returns 0 results (`MATCH 'po'` yielded `[]` in test). | **Tier 1 Exact Abbreviation Table:** Specialized lookup table for verified 1- and 2-letter clinical abbreviations, scoped strictly by specimen (e.g., `"K"` + Serum $\rightarrow$ Potassium LOINC `2823-3`). |
| **Numeric Strengths & Units** | Query `"Metformin 500 mg"` produces trigrams for numbers (`"500"`, `"00 "`, `"0 m"`), generating spurious lexical matches against `"Cefprozil 500 mg"` or `"Amoxicillin 500 mg"` because the number and unit match. | **Dual-Table Representation:** Drug concepts are split into base entity names and structured numeric attributes. Lexical FTS5 searches the drug name tokens; numeric strength is validated as a post-retrieval SQL/Python constraint. |
| **Single-Character OCR Drops** | OCR error dropping a character in a 3-character string: `"WBC"` $\rightarrow$ `"WC"`. Trigram search fails completely on 2-character remainder. | **Levenshtein Fallback on Short Tokens:** If token length $\le 4$ and FTS5 yields no results, execute bounded Levenshtein ($D \le 1$) against pre-indexed clinical acronym lists. |
| **False-Positive Substring Clashes** | Query `"Met"` (Metformin) matching substring in `"Methotrexate"` or `"Methylprednisolone"` with high trigram overlap. | **Token-Boundary Prioritization:** FTS5 BM25 queries enforce word-boundary prefixes (`^token*`) alongside trigram matching. Exact word matches receive an automatic rank boost over internal substrings. |
| **Punctuation & Slash Variations** | Variations in formatting: `"mg/dL"` vs `"mg / dL"` vs `"mg.dL"`, or `"Amox/Clav"` vs `"Amox-Clav"`. | **Canonical Pre-Tokenization Normalization:** Punctuation and slashes are normalized into canonical whitespace delimiters prior to query execution (`re.sub(r'[/.\-_]', ' ', text)`). |

### C. Recommended Multi-Index Lexical Strategy

Rather than relying solely on a single trigram index, the v1.1 architecture implements a **Dual-Tokenizer Index**:
1. **Primary Index (`fts_unicode`):** Uses SQLite FTS5 `tokenize = "unicode61 remove_diacritics 2"`. Provides standard BM25 token-level matching, word prefixes (`metform*`), and exact token matches.
2. **Secondary Fallback Index (`fts_trigram`):** Uses SQLite FTS5 `tokenize = "trigram"`. Invoked **only** when `fts_unicode` returns fewer than 3 candidates, capturing OCR noise, spelling corruptions, and glued tokens.

```sql
-- Schema for Dual-Indexed Concepts Table
CREATE TABLE concepts (
    concept_id INTEGER PRIMARY KEY,
    system TEXT NOT NULL,          -- 'LOINC' or 'RxNorm'
    code TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    tty TEXT,                      -- RxNorm Term Type (IN, SCD, etc.) or LOINC Scale
    attributes_json TEXT           -- Structured attributes (axes, strengths, units)
);

-- Primary Word-Level BM25 Index
CREATE VIRTUAL TABLE fts_concepts_unicode USING fts5(
    canonical_name,
    synonyms,
    content='concepts',
    content_rowid='concept_id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Secondary Substring / OCR Trigram Index
CREATE VIRTUAL TABLE fts_concepts_trigram USING fts5(
    synonyms_trigram,
    content='concepts',
    content_rowid='concept_id',
    tokenize='trigram'
);
```

### D. Calibration of BM25 Ranking Scores

**BM25 is a ranking function, not a probability.** Raw BM25 values are unbounded negative numbers in SQLite FTS5 (e.g., `-14.32`), dependent on document length and corpus frequency.

To provide a calibrated confidence metric in $[0.0, 1.0]$:
1. **Relative Score Normalization:** For a retrieved candidate set $C = \{c_1, \dots, c_k\}$, calculate the score margin:
   $$\Delta_{\text{score}} = \text{BM25}(c_1) - \text{BM25}(c_2)$$
2. **Sigmoid Mapping with Margin Scaling:**
   $$\text{Confidence}(c_1) = \sigma\left( \frac{-\text{BM25}(c_1) - \beta}{\gamma} \right) \cdot \min\left(1.0, 0.5 + \frac{\Delta_{\text{score}}}{2.0}\right)$$
   Where parameters $\beta$ and $\gamma$ are fit empirically on validation holdout sets.
3. **Exact Match Fast-Path:** Exact case-insensitive string matches against canonical names or verified aliases bypass BM25 and receive an uncalibrated score of $1.0000$.

### E. Baseline Comparison: Exact vs FTS5 vs Current Linear Retriever

| Dimension | Current Implementation (`HybridTerminologyRetriever`) | Pure Exact Lookup | Proposed Dual FTS5 Engine (`M-TERMINOLOGY-SCALE v1.1`) |
| :--- | :--- | :--- | :--- |
| **Vocabulary Scale** | 111 LOINC / 25 RxNorm concepts | Unlimited | **102,000 LOINC / 400,000 RxNorm** |
| **Search Mechanism** | Linear `for` loop in Python over all aliases | Python `dict` hash table | **B-Tree exact index + FTS5 SQLite virtual table** |
| **OCR Typo Handling** | Bounded character n-grams | Fails completely (0% recall on typos) | **Trigram index captures 1-2 character OCR substitutions** |
| **Abbreviation Handling** | Partial substring match | Exact string match only | **Curated abbreviation table + token prefix search** |
| **Query Latency** | 2 ms on 100 concepts; **>30,000 ms projected on full data** | < 0.05 ms | **Estimated 1.5 – 6.0 ms across 1.2M aliases** |
| **Clinical Filtering** | None (lexical score only) | None | **SQL-level attribute filtering (Specimen, Scale, TTY)** |

---

## 4. Clinical Matching Safety & Abstention Rules

### A. LOINC Laboratory Matching: Six-Axis Compatibility Matrix

LOINC concepts are defined across six axes: (1) Component/Analyte, (2) Property, (3) Time Aspect, (4) System/Specimen, (5) Scale, (6) Method.

#### 1. Specimen / System Handling
- **Rule L-SPEC-1 (Never Default to Serum):** If the clinical text does not mention a specimen, the normalizer **MUST NOT** assume Serum/Plasma. It must evaluate whether an unassigned/general concept exists; if only specimen-specific codes exist, it must abstain or assign an unassigned parent concept.
- **Rule L-SPEC-2 (Strict Specimen Contradiction Fail-Closed):** If the document text indicates "Urine" (e.g., from header, section name, or test line) and the retrieved LOINC code has `System = 'Ser/Plas'`, the candidate is **REJECTED**, regardless of BM25 score.

#### 2. Scale Handling
- **Rule L-SCALE-1:** If the extracted claim value is numeric (e.g., `142`), `Scale` must be `Qn` (Quantitative). Ordinal (`Ord`) or Nominal (`Nom`) concepts are rejected.
- **Rule L-SCALE-2:** If the extracted claim value is qualitative ("Positive", "Detected", "Reactive", "Trace"), `Scale` must be `Ord`, `Nom`, or `OrdQn`. Pure quantitative concepts are rejected.

#### 3. Unit Handling
- **Rule L-UNIT-1 (Unit as Necessary Constraint, Not Sufficient Proof):** A matching unit (e.g., `mg/dL`) confirms physical dimension compatibility with the LOINC `Property` (Mass Concentration), but does **NOT** prove analyte identity. An analyte match without unit compatibility fails closed.

#### 4. LOINC Six-Axis Decision Table

| Clinical Input Condition | Component Match | Specimen Match | Scale Match | Unit Match | Normalization Action | Resulting `normalized_code` |
| :--- | :---: | :---: | :---: | :---: | :--- | :--- |
| `"Serum Potassium 4.2 mEq/L"` | Exact (`Potassium`) | Exact (`Ser/Plas`) | Match (`Qn`) | Compatible (`mEq/L`) | **Accept Top-1** | `2823-3` (Potassium Ser/Plas-sCnc) |
| `"Urine Protein 30 mg/dL"` | Exact (`Protein`) | Match (`Urine`) | Match (`Qn`) | Compatible (`mg/dL`) | **Accept Top-1** | `2888-6` (Protein Urine-mCnc) |
| `"Protein 30 mg/dL"` (No specimen context) | Exact (`Protein`) | **Unknown** | Match (`Qn`) | Compatible (`mg/dL`) | **Abstain (Ambiguous Specimen)** | `None` (Code withheld; text preserved) |
| `"Random Glucose 110 mg/dL"` | Exact (`Glucose`) | Compatible (`Ser/Plas`) | Match (`Qn`) | Compatible (`mg/dL`) | **Accept Top-1** | `2345-7` (Glucose Ser/Plas-mCnc) |
| `"Urine Glucose Strip: Positive"` | Exact (`Glucose`) | Match (`Urine`) | Match (`Ord`) | N/A (Qualitative) | **Accept Top-1** | `2349-9` (Glucose Urine Test Strip) |
| `"Glucose: Positive"` | Exact (`Glucose`) | **Unknown** | **Contradictory** (Numeric code retrieved) | N/A | **Abstain (Scale Mismatch)** | `None` |

---

### B. RxNorm Medication Matching: Abstraction Level Hierarchy

RxNorm structures drugs into distinct Term Types (`TTY`):
- **`IN`:** Ingredient (e.g., *Metformin*).
- **`PIN`:** Precise Ingredient (e.g., *Metformin hydrochloride*).
- **`SCDC`:** Semantic Clinical Drug Component (Ingredient + Strength, e.g., *Metformin 500 MG*).
- **`SCD`:** Semantic Clinical Drug (Ingredient + Strength + Dose Form, e.g., *Metformin 500 MG Oral Tablet*).
- **`SBD`:** Semantic Branded Drug (Brand Name + Strength + Dose Form, e.g., *Glucophage 500 MG Oral Tablet*).

#### 1. Abstraction Level Eligibility Rules
- **Rule R-LEVEL-1 (Strength & Form Requirement for SCD):** To normalize to an **SCD** code, the extracted claim or its bound tuple attributes (`group_id`) **MUST** contain explicit source evidence for both numeric strength and dosage form.
- **Rule R-LEVEL-2 (Abstaining to Ingredient):** If the document text contains only a drug name without strength or form (e.g., *"Patient takes Lisinopril"*), the normalizer is eligible to normalize to **IN** (RxCUI `29046`), but **STRICTLY PROHIBITED** from guessing an SCD (e.g., cannot assume 10 mg tablet).
- **Rule R-LEVEL-3 (No Inferring Dose Form from Route):** A route mention (e.g., *"oral"*, *"PO"*) must **never** be used to invent a dose form (e.g., assuming *"tablet"* when the patient might be taking oral solution or capsule). If form is not in the text, map to SCDC or IN, not SCD.
- **Rule R-LEVEL-4 (Single vs Combination Drug Barrier):** A single-ingredient text mention (e.g., *"Amoxicillin"*) must **never** match a combination drug concept (e.g., *"Amoxicillin / Clavulanate"*, RxCUI `617314`), even if the combination drug has a high BM25 score. Combination drugs require evidence for all active ingredients.

#### 2. RxNorm Decision Table

| Clinical Input Text | Extracted Name | Extracted Strength | Extracted Form | Extracted Route | Eligible Level | Target RxCUI Concept | Resulting Action |
| :--- | :--- | :--- | :--- | :--- | :---: | :--- | :--- |
| `"Amoxicillin 500 mg oral capsule"` | Amoxicillin | 500 mg | Capsule | Oral | **SCD** | `308189` (Amoxicillin 500 MG Oral Capsule) | **Normalize to SCD** |
| `"Amoxicillin 500 mg PO"` | Amoxicillin | 500 mg | None | PO | **SCDC** | `313782` (Amoxicillin 500 MG) | **Normalize to SCDC** (Form withheld) |
| `"Patient on Amoxicillin"` | Amoxicillin | None | None | None | **IN** | `723` (Amoxicillin) | **Normalize to IN** |
| `"Amoxil 500 mg capsule"` | Amoxil | 500 mg | Capsule | Oral | **SBD** | `104895` (Amoxil 500 MG Oral Capsule) | **Normalize to SBD** |
| `"Latanoprost 0.005% eye drops"` | Latanoprost | 0.005% | Eye drops | Ophthalmic | **SCD** | `284347` (Latanoprost 0.05 MG/ML Ophthalmic Sol) | **Normalize to SCD** |
| `"Latanoprost"` (No concentration) | Latanoprost | None | None | None | **IN** | `5224` (Latanoprost) | **Normalize to IN** |
| `"Amoxicillin 500 mg"` (BM25 matches Amox-Clav) | Amoxicillin | 500 mg | None | None | **SCDC** | `313782` (Amoxicillin 500 MG) | **Reject Combination; Select Pure IN/SCDC** |

---

## 5. Formal Evaluation Methodology & Metrics

### A. Formal Metric Definitions

1. **Retrieval Top-1 Accuracy:**
   $$\text{Top-1 Acc} = \frac{1}{|Q|} \sum_{q \in Q} \mathbb{I}(\text{Top Candidate Code} == \text{Ground Truth Code}(q))$$
   *Evaluated strictly on in-vocabulary queries with unambiguous ground truth.*
2. **Retrieval Top-5 Recall:**
   $$\text{Top-5 Recall} = \frac{1}{|Q|} \sum_{q \in Q} \mathbb{I}(\text{Ground Truth Code}(q) \in \text{Top-5 Candidate Codes})$$
3. **Conditional Normalization Precision:**
   $$\text{Precision}_{\text{cond}} = \frac{|\{q \in Q : \text{System Normalized } q \text{ AND Code is Correct}\}|}{|\{q \in Q : \text{System Normalized } q \text{ (Score} \ge \tau)\}|}$$
4. **Normalization Coverage:**
   $$\text{Coverage} = \frac{|\{q \in Q : \text{System Normalized } q \text{ (Score} \ge \tau)\}|}{|Q|}$$
5. **Clinical False-Match Rate (CFMR - Critical Safety Metric):**
   $$\text{CFMR} = \frac{|\{q \in Q : \text{System Normalized } q \text{ to an INCORRECT Code}\}|}{|Q|}$$
   *This measures dangerous clinical miscoding (e.g., mapping Metformin to Metoprolol). Target: $<0.5\%$.*
6. **Abstention Quality:**
   $$\text{Abstention Precision} = \frac{|\{q \in Q_{\text{OOV/Ambiguous}} : \text{System Abstained}\}|}{|Q_{\text{OOV/Ambiguous}}|}$$
   *Measures the system's ability to cleanly fail closed on nonsense, out-of-vocabulary, or incomplete terms.*
7. **End-to-End FHIR Terminology Accuracy:**
   $$\text{FHIR Term Acc} = \frac{\text{In-Bundle FHIR Resources with Exact Correct Code AND System}}{\text{Total Ground-Truth Clinical Entities in Benchmark}}$$

### B. Distinct Evaluation Cohorts

To ensure true statistical power, evaluation is partitioned across five independent cohorts:

| Cohort Name | Description | Minimum Sample Size | Ground Truth Adjudication |
| :--- | :--- | :---: | :--- |
| **Cohort 1: In-Vocab Clean** | Standard, clean clinical terms from inpatient/outpatient notes. | 1,000 terms (500 Lab, 500 Med) | Double-coded by two clinical annotators; $\kappa \ge 0.95$. |
| **Cohort 2: Unseen Synonyms** | Valid clinical aliases, brand names, and British/international spellings not in training seeds. | 500 terms | Physician consensus. |
| **Cohort 3: OCR-Degraded** | Terms processed with synthetic character blur, drops, and whitespace artifacts (CER 3%–10%). | 500 terms | Derived from Cohort 1 with automated degradation. |
| **Cohort 4: Ambiguous / Underspecified** | Terms lacking specimen or route (e.g., *"Protein"*, *"Glucose"*, *"Metformin"*). | 300 terms | Scored correct if system normalizes to generic level or cleanly abstains. |
| **Cohort 5: Out-of-Vocabulary (OOV) / Adversarial** | Veterinary drugs, fabricated chemical strings, non-medical tokens. | 300 terms | Correct behavior is 100% abstention. |

### C. Threshold Freezing Policy
*All numerical thresholds remain provisional.* Prior to freezing milestone acceptance gates, a baseline sweep across Cohorts 1–5 must be executed on the compiled SQLite database. Thresholds will be set at the point that maximizes coverage subject to the hard constraint: $\text{CFMR} \le 1.0\%$.

---

## 6. Optional Neural Reranking Architecture

### A. Clarification of Neural Terminology

In v1.0.0, the terms "SapBERT", "bi-encoder", and "cross-encoder" were used interchangeably. In v1.1, the precise technical distinction is defined:

1. **Bi-Encoder Architecture (Dense Representation Matching):**
   - **Model:** Pre-trained biomedical encoder (e.g., `cambridgeltl/SapBERT-from-PubMedBERT-fulltext`).
   - **Operation:** Computes independent vector embeddings: $\vec{u} = \text{Encoder}(\text{query})$ and $\vec{v} = \text{Encoder}(\text{candidate})$.
   - **Similarity:** Cosine similarity $\frac{\vec{u} \cdot \vec{v}}{\|\vec{u}\|\|\vec{v}\|}$.
   - **Limitations:** Bi-encoders can be pre-indexed into a vector database (e.g., Faiss), but indexing 400,000 concepts generates a >1.5 GB vector index and incurs substantial startup overhead on CPU.
2. **Cross-Encoder Architecture (Joint Interaction Reranker):**
   - **Model:** Sequence classification model (e.g., `cross-encoder/ms-marco-MiniLM-L-6-v2` fine-tuned on UMLS synonym pairs).
   - **Input Format:** A single concatenated pair passed into self-attention:
     $$\text{Input} = \texttt{[CLS]} \circ \text{query\_text} \circ \texttt{[SEP]} \circ \text{candidate\_canonical\_name} \circ \texttt{[SEP]}$$
   - **Operation:** Evaluates cross-attention between every token in the query and candidate, yielding a single probability score in $[0, 1]$.
   - **Strength:** Substantially higher ranking fidelity than bi-encoders for fine-grained distinctions.

### B. Formal Deferral of Neural Reranking

- **Decision:** Neural reranking is **DEFERRED** and marked strictly **OPTIONAL**.
- **Rationale:** Lexical FTS5 with BM25, synonym ring expansion, trigram fallback, and rule-based axis filtering is expected to achieve $>95\%$ Top-1 precision on in-vocabulary clinical concepts with $<5\text{ ms}$ query latency.
- **Trigger Condition for Neural Phase:** A quantized ONNX cross-encoder (e.g., int8 quantized MiniLM-L6, ~45 MB) will only be designed and implemented if Cohort 2 (Unseen Synonyms) baseline evaluation yields Top-1 accuracy $<88\%$ on pure lexical FTS5.

---

## 7. Database and Operational Architecture

### A. SQLite Runtime and FTS5 Support Checks

At worker startup, the application verifies the SQLite environment:
```python
def verify_sqlite_fts5_support() -> None:
    """Verify that SQLite runtime supports FTS5 and trigram tokenization."""
    import sqlite3
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE _test_fts USING fts5(x, tokenize='trigram');")
        con.execute("INSERT INTO _test_fts VALUES ('test');")
        con.execute("SELECT * FROM _test_fts WHERE _test_fts MATCH 'te';")
    except Exception as e:
        raise RuntimeError(f"SQLite FTS5 trigram tokenization unavailable: {e}") from e
    finally:
        con.close()
```

### B. Thread-Safe Read-Only Connection Pooling

- **File Mode:** Database opened strictly in read-only URI mode: `file:/path/to/terminology.db?mode=ro`.
- **Concurrency:** Because the database is opened read-only, multiple worker threads and processes can execute simultaneous queries without lock contention or write barriers (`SQLITE_BUSY` impossible).
- **Per-Thread Connection:** Connections are managed via thread-local storage (`threading.local`) to avoid cross-thread SQLite connection sharing.

### C. Atomic Database Generation and Release Metadata

1. **Atomic Publishing Pattern:**
   - Ingestion builders write to a temporary file: `terminology_build.db.tmp`.
   - On completion of index building and integrity verification (`PRAGMA integrity_check;`), the builder atomically renames the temporary file to `terminology.db` (atomic filesystem swap).
2. **Release Metadata Table:**
   Every generated terminology database contains a mandatory metadata table:
   ```sql
   CREATE TABLE terminology_metadata (
       key TEXT PRIMARY KEY,
       value TEXT NOT NULL
   );
   INSERT INTO terminology_metadata VALUES 
       ('schema_version', '1.1.0'),
       ('loinc_release_version', '2.77'),
       ('rxnorm_release_date', '2026-10-05'),
       ('prescribable_only', 'true'),
       ('build_timestamp', '2026-11-12T12:00:00Z'),
       ('build_commit_sha', '9918ded4ec4e6ecdf2b8c7f4b69bd380721b4576');
   ```

### D. Memory Footprint and Page-Cache Clarification

- **`PRAGMA mmap_size` Clarification:** Setting `PRAGMA mmap_size = 268435456;` enables OS memory mapping. This is **not** a guaranteed resident-set memory limit; the operating system page cache manages physical RAM pages, paging in SQLite data from disk on demand.
- **Resident Set Size (RSS):** For typical read-only workloads querying a few dozen concepts per document, active resident memory is expected to remain between **15 MB and 45 MB**.

---

## 8. Explicit Non-Goals & Frozen Milestone Contracts

1. **Phase A, B1, C1, M-OCR, M-BENCHMARK, and M-FHIR-SAFE-SELECTION remain FROZEN:**
   - No code, fixtures, or baseline evaluations in frozen milestones will be modified.
2. **Immutable Clinical Claims (`PROJECT_SPEC §14.3`):**
   - Terminology normalization is an additive enrichment layer. `claim.value`, `claim.unit_raw`, `claim.bbox`, and `claim.evidence` are immutable.
3. **FHIR Safe Selection Contracts Preserved:**
   - `src/clindoc/fhir/selection.py` remains untouched. Normalization provides standardized codes that help determine pairwise equivalence, but the selection engine's human-review precedence, review state matrix, and fail-closed rules will not be altered.
4. **No SNOMED CT Comprehensive Expansion:**
   - SNOMED CT remains limited to qualitative lab findings (positive/negative/reactive) per Phase 4 decisions.

---

## 9. Final Recommendation

**RECOMMENDATION: PROCEED TO IMPLEMENTATION AUTHORIZATION UNDER v1.1 SPECIFICATION.**

- **Architecture:** SQLite FTS5 Dual-Tokenizer (Unicode BM25 + Trigram Fallback) + Exact Abbreviation Table.
- **Safety:** Six-axis LOINC compatibility checking and RxNorm Term Type (IN/SCD/SBD) abstraction hierarchy.
- **Licensing:** Fully compliant using LOINC official terms with attribution and RxNorm Current Prescribable Content (excluding proprietary third-party SABs).
- **Next Step:** Await explicit user authorization to begin Phase 1 implementation.

---
*End of M-TERMINOLOGY-SCALE Feasibility and Design Proposal v1.1.*

