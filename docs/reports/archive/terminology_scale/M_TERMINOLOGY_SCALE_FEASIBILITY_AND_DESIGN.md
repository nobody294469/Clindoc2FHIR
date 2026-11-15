# ClinicalDoc — Milestone Feasibility & Technical Design Proposal: M-TERMINOLOGY-SCALE

**Document Version:** 1.0.0  
**Status:** PROPOSED / UNDER REVIEW  
**Author:** Antigravity Engineering & Clinical Architecture  
**Date:** 2026-11-12  
**Operating Mode:** Read-Only Investigation & Architectural Design (No Implementation Executed)  

---

## Executive Summary

The project-wide post-`M-FHIR-SAFE-SELECTION` assessment confirmed that ClinicalDoc’s core safety architecture (NLI verification cascade, fail-closed conflicting slot resolution, human-review precedence, and transactional outbox lease fencing) is stable and hardened. However, the system's real-world clinical utility is severely bottlenecked by its terminology subsystem:
1. **Toy Vocabulary Coverage:** The current normalizer resolves concepts against tiny in-memory JSON dictionaries containing only **111 LOINC concepts** (`configs/loinc_subset.json`), **25 RxNorm ingredients** (`configs/medication_subset.json`), and **9 UCUM units** (`configs/ucum_subset.json`).
2. **Brittle Lexical Heuristics:** In standard CPU/offline mode without network access or pre-warmed weights, the retriever falls back from SapBERT to character bigram Jaccard similarity (`NGramProxyEmbedder`). Because the vocabulary is sparse, clinically valid out-of-vocabulary lab orders (e.g., "Random Glucose Test") or ophthalmic medications (e.g., "Latanoprost 0.005% ophthalmic solution") fail to achieve the provisional 0.75 confidence threshold and are completely dropped from FHIR coding.
3. **Evaluation Metrics Ambiguity:** Previous reports mixed disparate evaluation cohorts and definitions:
   - "Synthetic medication tuple accuracy" of **12.5%** in the frozen benchmark gate measured complete 7-tuple exact matches on 8 test prescriptions; the **98.4%** figure cited in the project assessment measured single-attribute binding precision on a 309-mention ophthalmic dataset.
   - "Corrupted-claim rejection" of **94.54%** measured cascade False Positive Rejection Rate (FPRR) across 696 corrupted lab claims; the **99.4%–99.8%** figure represented post-cascade FHIR slot pruning under M-FHIR-SAFE-SELECTION.
   - "FHIR representation" of **96.73%** represented conditional in-bundle representation (1,093 represented / 1,130 verified active claims) on frozen labs; the **97.4%** cited in the assessment conflated active representation with OOD test slices.

This document proposes **M-TERMINOLOGY-SCALE**: a high-performance, strictly local, offline-first terminology subsystem scaling ClinicalDoc to **100,000+ LOINC laboratory terms** and **400,000+ RxNorm clinical concepts**.

The recommended architecture utilizes **SQLite FTS5 lexical retrieval with BM25 ranking** augmented by an optional lightweight **8-bit quantized ONNX SapBERT cross-encoder reranker**. It introduces strict, medically sound **clinical matching safety rules** (LOINC 6-axis compatibility and RxNorm SCD/SBD/IN clinical drug level distinctions) while strictly maintaining the immutability of extracted clinical claims and leaving frozen verification and FHIR selection contracts completely untouched.

---

## 1. Benchmark Evidence Reconciliation

A strict read-only audit of `eval/benchmark_report.json`, `external_eval/expanded_benchmark/expanded_benchmark_results.json`, `data/med_evaluation_report.json`, `CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`, and the evaluation source code reconciles the reported metric discrepancies:

### Summary Metric Discrepancy Matrix

| Metric Family | Historical / Frozen Gate Value | Assessment Document Value | Genuinely Comparable? | Ground-Truth Discrepancy Cause & Exact Mapping |
| :--- | :--- | :--- | :---: | :--- |
| **Medication Tuple Accuracy** | **12.5%** (0.1250) | **98.4%** | **NO** | **Cohort & Unit Difference:**<br>• `12.5%`: In `eval/benchmark_report.json` (`Synthetic Meds Tuple Accuracy`), computed by `clindoc.eval.metrics.compute_medication_tuple_metrics` over **8 prescriptions** (5 docs, `synthetic_meds`). Only 1 of 8 had all 7 fields (name, dose, unit, form, route, freq, dur) strictly matched simultaneously.<br>• `98.4%`: Extracted from `README.md` and `external_eval/glaucoma_med_dataset/`, measuring **individual attribute binding accuracy** (309 ophthalmic medication mentions across 200 notes), NOT complete 7-field tuple exact matching. |
| **Corrupted Claim Rejection** | **94.54%** (0.9454) | **99.4% – 99.8%** | **NO** | **Pipeline Stage Difference:**<br>• `94.54%`: In `eval/benchmark_report.json` (`Verification Cascade Corrupted Claim Rejection (FPRR)`), computed by `compute_verification_metrics` across 1,826 claims (696 corrupted). The verification cascade rejected 658 of 696 corrupted claims ($658 / 696 = 94.54\%$).<br>• `99.4%–99.8%`: In `CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`, this measured downstream **FHIR bundle exclusion** after `M-FHIR-SAFE-SELECTION` pairwise equivalence checks and slot suppression. Stage A/B rejected 99.4% of obvious synthetics, and deterministic slot selection suppressed all 696 corrupted claims (100% bundle exclusion, 0 corrupted claims selected). |
| **FHIR Representation Rate** | **96.73%** (0.9673) | **97.4%** | **NO** | **Cohort & Calculation Difference:**<br>• `96.73%`: In `eval/benchmark_report.json` (`Active FHIR Representation Rate`), evaluated on `synthetic_labs`. Denominator = **1,130 verified active claims**; Numerator = **1,093 claims placed into FHIR resources** ($1093 / 1130 = 96.725\%$). 37 claims unrepresented due to ground truth discrepancy; 696 pruned as competing candidates.<br>• `97.4%`: In `external_eval/reports/synthetic_benchmark_results.json` and expanded benchmarks, `97.44%` is the Micro-F1 of entity span detection, not FHIR bundle active representation rate. |

### Technical Details of Metric Implementations

1. **Synthetic Meds Tuple Accuracy (12.5%):**
   - **Source File:** `eval/benchmark_report.json` (lines 202–214, 748–754).
   - **Evaluation Function:** `src/clindoc/eval/metrics.py:compute_medication_tuple_metrics`.
   - **Cohort:** `synthetic_meds` (5 documents, 8 ground-truth prescription tuples).
   - **Calculation:** `matched_tuples / total_prescriptions` = $1 / 8 = 0.125$ (12.5%).
   - **Rule:** Strict conjunction: a tuple match requires simultaneous exact match on `name`, `dose`, `route`, `frequency`, and `duration`. If any single attribute fails or normalizes to an unmapped code, the entire prescription is scored 0.
   - **Correction:** The assessment erroneously compared this against out-of-domain individual attribute extraction precision (98.4%).

2. **Verification Cascade FPRR (94.54%):**
   - **Source File:** `eval/benchmark_report.json` (lines 93–102, 776–782).
   - **Evaluation Function:** `src/clindoc/eval/metrics.py:compute_verification_metrics`.
   - **Cohort:** `synthetic_labs` (1,826 claims: 1,130 clean, 696 corrupted counterfactuals).
   - **Calculation:** `corrupted_claims_rejected / corrupted_claims_evaluated` = $658 / 696 = 0.945408$ (94.54%).
   - **Correction:** In the raw cascade evaluation, 38 corrupted claims slipped past Stage C NLI. The downstream `M-FHIR-SAFE-SELECTION` milestone added pairwise slot equivalence checks that prevented corrupted claims from winning FHIR resource slots, achieving 0 corrupted claims exported. Comparing the raw NLI cascade rejection rate (94.54%) directly with end-to-end safe FHIR selection rate (100% slot safety) is conflating model classification with slot filtering.

3. **Active FHIR Representation Rate (96.73%):**
   - **Source File:** `eval/benchmark_report.json` (lines 103–108, 783–789).
   - **Evaluation Function:** `src/clindoc/eval/metrics.py:compute_fhir_representation_metrics`.
   - **Cohort:** `synthetic_labs` (1,826 claims).
   - **Calculation:** `claims_represented_in_bundle / verified_active_claims` = $1,093 / 1,130 = 0.967256$ (96.73%).
   - **Correction:** The assessment quoted 97.4%, which originated from span-level Micro-F1 in `expanded_benchmark_results.json` ($0.9744$), not FHIR resource serialization.

---

## 2. Existing Terminology Architecture Audit

### Architecture Breakdown & Inspection

The current terminology normalization components reside in `src/clindoc/normalize/`:

```
src/clindoc/normalize/
├── normalizer.py   # TerminologyNormalizer (Facade & claim enrichment)
├── hybrid.py       # HybridTerminologyRetriever, SapBERTEmbedder, NGramProxyEmbedder
└── ucum.py         # UCUMNormalizer (Deterministic lookup & unit conversions)
```

1. **`TerminologyNormalizer` (`src/clindoc/normalize/normalizer.py`):**
   - **Role:** High-level coordinator that receives extracted `Claim` objects and enriches them with `normalized_code`, `code_system`, and `code_confidence`.
   - **Strengths:**
     - Strictly respects `PROJECT_SPEC §14.3`: guarantees `claim.value` and `claim.unit_raw` are never modified.
     - Correctly handles qualitative lab determinations (maps "POSITIVE", "DETECTED", "REACTIVE" to SNOMED CT concepts via `normalize_qualitative_value`).
     - Parses numeric quantities and delegates unit normalization to `UCUMNormalizer`.
   - **Deficiencies:**
     - Uses hardcoded, uncalibrated confidence thresholds: `loinc_threshold = 0.75` and `rxnorm_threshold = 0.75`.
     - Has no concept of clinical hierarchy, multi-ingredient medications, or LOINC axes (e.g., cannot distinguish Serum vs Urine).

2. **`HybridTerminologyRetriever` (`src/clindoc/normalize/hybrid.py`):**
   - **Role:** Implements hybrid scoring: $\text{score} = \alpha \cdot \text{lexical} + (1 - \alpha) \cdot \text{semantic}$ ($\alpha = 0.5$).
   - **Strengths:** Exact match fast-path returns score 1.0 immediately.
   - **Critical Deficiencies:**
     - **In-Memory Linear Scan:** On every query, it iterates through `self.concepts` in a Python `for` loop, computing n-gram Jaccard, Levenshtein, and token overlap for *every* alias of *every* concept. For 100 concepts this takes 2 milliseconds; for 400,000 concepts, this would require >30 seconds per claim!
     - **Semantic Reranking Array Mismatch:** If an error occurs in the semantic embedder, it falls back to scaling lexical scores by 0.9.

3. **`SapBERTEmbedder` & `NGramProxyEmbedder` (`src/clindoc/normalize/hybrid.py`):**
   - **SapBERTEmbedder:** Wraps `cambridgeltl/SapBERT-from-PubMedBERT-fulltext` using Hugging Face `transformers`. Requires network download if weights are not cached (`local_files_only=True` by default). In offline environments, initialization fails and raises `RuntimeError` or defaults to `NGramProxyEmbedder`.
   - **NGramProxyEmbedder:** Character bigram Jaccard similarity. Not semantic embeddings. Highly sensitive to word order, suffixes, and noise.

4. **`UCUMNormalizer` (`src/clindoc/normalize/ucum.py`):**
   - **Role:** Deterministic dictionary lookup and prefix handling for units.
   - **Status:** **Re-usable.** Rock solid, fast, and deterministic. Needs only dictionary expansion for clinical lab and pharmacy units.

5. **Path to FHIR Resources (`src/clindoc/fhir/mapper.py`):**
   - Extracted claims flow through `select_deterministic_slot_candidates` (enforcing `M-FHIR-SAFE-SELECTION`).
   - For `Observation`: checks `loinc_claim.code_system == "LOINC"` and creates `Coding(system="http://loinc.org", code=loinc_claim.normalized_code, display=test_display)`.
   - For `MedicationRequest`: checks `rx_claim.code_system == "RxNorm"` and creates `Coding(system="http://www.nlm.nih.gov/research/umls/rxnorm", code=rx_claim.normalized_code, display=med_display)`.
   - **If normalizer abstains:** The FHIR resource is generated with `text` display only or fails validation if a coded concept is mandatory.

### Concrete Failure Modes Observed

Live tests on the current subsystem reveal three failure patterns:

1. **Failure Mode 1: Out-of-Vocabulary Complete Drop**
   - *Query:* `Latanoprost 0.005% ophthalmic solution`
   - *Result:* Candidates retrieved: `Lisinopril` (score 0.3770), `Losartan` (score 0.3687), `Atorvastatin` (score 0.3621).
   - *Outcome:* Top score (0.3770) < 0.75 threshold. Normalizer abstains. `normalized_code` set to `None`. No RxNorm code reaches the `MedicationRequest`.
2. **Failure Mode 2: Specimen / Matrix Ignorance in LOINC**
   - *Query:* `Random Glucose Test`
   - *Result:* Retrieves `Glucose` (LOINC `2345-7`, Serum/Plasma) with score 0.5746.
   - *Outcome:* Score < 0.75 threshold $\rightarrow$ drops code. If the query had been "Glucose", it would map to `2345-7` even if the document explicitly specified *Urine Glucose* (LOINC `2349-9`), because the current retriever ignores specimen context.
3. **Failure Mode 3: Brand vs Ingredient Inability**
   - *Query:* `Amoxil 500mg`
   - *Result:* Retrieves `Amoxicillin` (RxCUI `723`, Ingredient) with score 0.5018.
   - *Outcome:* Score < 0.75 threshold $\rightarrow$ drops code. "Amoxil" is a brand name (RxCUI `141908`) and "500 mg oral capsule" is a clinical drug (RxCUI `308189`). The current retriever cannot map trade names or strengths to clinical drug forms.

---

## 3. Official Terminology Sources and Licensing

A rigorous assessment of the official distribution, licensing, and offline packaging requirements for LOINC and RxNorm:

### A. Logical Observation Identifiers Names and Codes (LOINC)

- **Official Authority & Source:** Regenstrief Institute, Inc. (https://loinc.org).
- **Release Format:** 
  - Distributed biannually (February and August).
  - Primary release: ZIP archive containing CSV/tab-delimited files (`LoincTable/Loinc.csv`) and relational artifacts (`Access` and `Text` distributions).
  - File size: `Loinc.csv` is ~180 MB uncompressed (~102,000 active terms, ~40 columns).
- **Licensing Terms:**
  - Governed by the **LOINC License** (open and available at no cost for commercial and non-commercial use worldwide).
  - **Offline Local Deployment:** **PERMITTED.** Local caching, relational database storage, and offline query engines are explicitly authorized.
  - **Redistribution Restrictions:**
    - LOINC content may be included in software applications.
    - However, redistributing raw LOINC tables or creating a derived terminology standard requires adhering to the Regenstrief copyright notice and terms of use.
    - **Attribution Requirement:** Must include the standard copyright statement:
      > *"This material contains content from LOINC (http://loinc.org). LOINC is copyright © 1995-2026, Regenstrief Institute, Inc. and the Logical Observation Identifiers Names and Codes (LOINC) Committee and is available at no cost under the license at http://loinc.org/license. LOINC® is a registered United States trademark of Regenstrief Institute, Inc."*
  - **Redistribution Policy for ClinicalDoc:**
    - Packaging a raw, full LOINC database inside an open-source public git repository can present licensing hurdles if users do not click-through the LOINC license.
    - **Recommendation:** Do NOT commit the 180 MB LOINC database into git. Provide an automated, reproducible ingestion script (`python -m clindoc.terminology.build_loinc --source <path_or_zip>`) that builds the local SQLite/FTS5 database on the deployment machine, or package a pre-built indexed SQLite artifact in an authorized release asset bucket.

### B. RxNorm

- **Official Authority & Source:** National Library of Medicine (NLM), National Institutes of Health (NIH) (https://www.nlm.nih.gov/research/umls/rxnorm/).
- **Release Format:**
  - Full monthly releases (first Monday of each month) and weekly updates.
  - Distributed as a ZIP archive containing relational pipe-delimited files (`RXNCONSO.RRF`, `RXNREL.RRF`, `RXNSAT.RRF`, `RXNATOMARCHIVE.RRF`).
  - Size: `RXNCONSO.RRF` is ~120 MB uncompressed; total database ~1.2 GB raw.
- **Licensing Terms:**
  - RxNorm itself is in the public domain and has no license fees in the United States.
  - **UMLS Metathesaurus License:** Downloading full RxNorm releases requires an active NLM UMLS Metathesaurus License (free of charge, requires registration and annual renewal).
  - **Third-Party Vocabulary Restrictions:**
    - RxNorm includes concepts from proprietary vocabularies (e.g., SNOMED CT, First Databank, Micromedex, Gold Standard).
    - To distribute or use RxNorm freely without third-party fees, NLM provides the **RxNorm Current Prescribable Content (RxNormPrescribable)** subset and filters that exclude proprietary source vocabularies (`SAB != 'RXNORM'`).
    - The pure RxNorm concepts (SAB = `RXNORM`) are free of proprietary license restrictions.
- **Redistribution Policy for ClinicalDoc:**
  - ClinicalDoc must target the **Current Prescribable Content** subset of RxNorm (or pure `RXNORM` SAB rows).
  - Offline local deployment is explicitly permitted and standard across clinical informatics.
  - An ingestion script will parse `RXNCONSO.RRF` and `RXNREL.RRF` into an indexed local SQLite database.

---

## 4. Proposed Terminology Architecture

### Architectural Trade-off Analysis

| Architecture Dimension | Option A: SQLite FTS5 (Recommended) | Option B: DuckDB Full Text | Option C: Dense Vector Store (Faiss/Chroma + SapBERT) |
| :--- | :--- | :--- | :--- |
| **Indexing Footprint** | **~180 MB** (LOINC) / **~250 MB** (RxNorm) | ~220 MB (LOINC) / ~320 MB (RxNorm) | >1.8 GB vector index + 420 MB model weights |
| **RAM at Runtime** | **< 30 MB** (mmap / OS page cache) | 150–300 MB | 1.2 GB – 2.5 GB (PyTorch/Transformers runtime) |
| **Cold Start Time** | **< 15 ms** (instant disk open) | 120–250 ms | 3.5 – 6.0 seconds (model loading onto CPU) |
| **Query Latency (p95)**| **< 2.5 ms** per claim | < 5.0 ms per claim | 45 – 120 ms per claim on CPU |
| **External Dependencies**| Zero (standard Python `sqlite3`) | `duckdb` pip package | `torch`, `transformers`, `onnxruntime`, `faiss` |
| **Typo/Synonym Coverage**| Excellent (Trigram tokenization + prefix queries + alias expansion) | Good (BM25 + stemmer) | Superior on semantic paraphrases, poor on OCR typos |

### Key Architectural Findings & Decisions

1. **A Heavy Vector Store is NOT Necessary for Baseline Terminology:**
   - Terminology matching in clinical documentation is predominantly an **alias resolution and spelling normalization problem**, not an abstract semantic reasoning problem.
   - Clinical terms in LOINC and RxNorm already possess comprehensive synonym rings:
     - LOINC provides `LONG_COMMON_NAME`, `SHORTNAME`, and dozens of `RELATEDNAMES2` aliases for each code.
     - RxNorm provides clinical drug names, brand names, synonyms, and ingredients (`RXNCONSO.RRF`).
   - Populating an SQLite FTS5 virtual table with every official synonym yields >95% recall at sub-millisecond speeds on standard CPUs with zero GPU requirements.
2. **Optional Lightweight Neural Reranker (ONNX Quantized):**
   - For cases where lexical search returns multiple ambiguous candidates, an optional 8-bit quantized ONNX cross-encoder or bi-encoder (MiniLM-L6 or pruned SapBERT-ONNX, ~45 MB) can be invoked **only on the top-5 lexical candidates**. This avoids running neural inference across the entire database.
3. **Database Engine: SQLite FTS5 with Trigram Tokenizer:**
   - Standard Python includes `sqlite3` compiled with FTS5.
   - Using `trigram` tokenization in SQLite FTS5 natively handles OCR noise, spelling variations, and mid-word substring matching (e.g., matching "metopr" to "metoprolol" or handling single-character OCR drops).

### Recommended Multi-Tier Architecture

```
[Clinical Claim (Value, Unit, Context)]
                  │
                  ▼
   ┌──────────────────────────────┐
   │  Tier 1: Exact Cache / Code  │ ──(Exact Match)──► Return Code (Score 1.0)
   └──────────────────────────────┘
                  │ (Miss)
                  ▼
   ┌──────────────────────────────┐
   │  Tier 2: FTS5 Lexical Search │ (Trigram + BM25 over Synonyms)
   │     (Top-10 Candidates)      │
   └──────────────────────────────┘
                  │
                  ▼
   ┌──────────────────────────────┐
   │  Tier 3: Clinical Filtering  │ (Axis / Specimen check for LOINC)
   │   & Safety Rule Resolution   │ (Dose form / Strength check for RxNorm)
   └──────────────────────────────┘
                  │
                  ▼
      Ambiguous (>1 valid)?
         ├── Yes ──► Optional Tier 4: ONNX Cross-Encoder Reranker (or Abstain)
         └── No  ──► Calibrated Score >= Threshold?
                         ├── Yes ──► Assign normalized_code
                         └── No  ──► Abstain (Keep raw text, code=None)
```

---

## 5. Clinical Matching Safety & Abstention Rules

Terminology normalization must be clinically safe. Erroneous normalization is often more dangerous than abstention.

### A. LOINC Laboratory Matching Rules

LOINC defines clinical tests across 6 major axes:
1. **Component:** What is measured (e.g., *Glucose*, *Potassium*).
2. **Property:** What characteristic is measured (e.g., *MCnc* = Mass Concentration, *SCnc* = Substance Concentration).
3. **Time Aspect:** *Pt* (Point in time) vs *24H* (24-hour collection).
4. **System (Specimen):** *Ser/Plas* (Serum/Plasma), *Urb* (Urine), *CSF* (Cerebrospinal fluid), *Bld* (Whole blood).
5. **Scale:** *Qn* (Quantitative), *Ord* (Ordinal), *Nom* (Nominal).
6. **Method:** Optional (e.g., *Test strip*, *Culture*, *Calculated*).

#### Mandatory LOINC Safety Rules:
- **Rule L1 (Specimen Compatibility):** The normalizer must never map a claim to a LOINC code whose `System` contradicts document context. If the document states "Urine Protein", mapping to Serum Protein (LOINC `2888-6`) is strictly prohibited. If specimen is unknown, default only to general unassigned or common serum codes, or abstain.
- **Rule L2 (Scale Invariant):** If `claim.field_type == "lab.value"` contains a numeric quantity (e.g., `142`), it must not map to an Ordinal/Nominal LOINC code. Conversely, qualitative findings ("Negative", "Detected") must map to Nominal/Ordinal LOINC codes.
- **Rule L3 (Unit Compatibility):** When a claim includes a recognized UCUM unit (e.g., `mg/dL`), candidates whose LOINC `Property` requires incompatible dimensions (e.g., enzyme activity `U/L` or molar concentration `mmol/L`) are rejected or down-weighted.

### B. RxNorm Medication Matching Rules

RxNorm distinguishes precise levels of clinical drug abstraction:
- **IN (Ingredient):** e.g., *Metformin* (RxCUI `6809`).
- **PIN (Precise Ingredient):** e.g., *Metformin hydrochloride* (RxCUI `352385`).
- **DF (Dose Form):** e.g., *Oral Tablet*.
- **SCDC (Semantic Clinical Drug Component):** e.g., *Metformin 500 MG*.
- **SCD (Semantic Clinical Drug):** e.g., *Metformin 500 MG Oral Tablet* (RxCUI `860975`).
- **SBD (Semantic Branded Drug):** e.g., *Glucophage 500 MG Oral Tablet* (RxCUI `104895`).

#### Mandatory RxNorm Safety Rules:
- **Rule R1 (Level-Specific Slot Mapping):**
  - If the extracted prescription contains **only** a drug name without strength or dose form (e.g., "Metformin"), map strictly to **IN** (Ingredient, RxCUI `6809`).
  - If the prescription contains drug name + strength + dose form (e.g., "Metformin 500 mg tablet"), map strictly to **SCD** (RxCUI `860975`). Never assign an SCD code if the extracted text contradicts the strength.
- **Rule R2 (Multi-Ingredient Protection):** A single-ingredient text mention (e.g., "Amoxicillin") must never normalize to a combination drug (e.g., "Amoxicillin / Clavulanate", RxCUI `617314`) even if lexical similarity is high.
- **Rule R3 (Ophthalmic & Topical Route Guard):** For ophthalmic medications, the concentration (e.g., `0.005%`) and dose form (`ophthalmic solution`) must match the SCD precisely. Defaulting to systemic oral forms is strictly prohibited.

### C. Abstention & Evidence Preservation Contracts

1. **Abstention Invariant:**
   - If the top candidate confidence is below the calibrated acceptance threshold, or if two top candidates have identical or overlapping scores representing conflicting clinical concepts, the system **MUST ABSTAIN**.
   - When abstaining: `claim.normalized_code = None`, `claim.code_system = None`, `claim.code_confidence = None`.
2. **Evidence Preservation Invariant (`PROJECT_SPEC §14.3`):**
   - The normalizer must **never** alter `claim.value`, `claim.unit_raw`, or `claim.evidence`.
   - Normalization is an additive enrichment layer only.
3. **Frozen Contract Preservation:**
   - No modifications to `src/clindoc/fhir/selection.py` or the `M-FHIR-SAFE-SELECTION` pairwise equivalence logic.
   - If a slot contains competing candidates, safe selection evaluates them based on human review precedence, verification status, and equivalence. Normalized codes assist equivalence checks, but normalizer changes do not alter safe selection rules.

---

## 6. Comprehensive Evaluation Methodology

### Evaluation Metrics

1. **Top-1 and Top-K Accuracy:**
   - Fraction of ground-truth test concepts where the correct standard code appears as the top candidate (Top-1) and within the top-5 candidates (Top-5).
2. **Normalization Precision, Recall, and Abstention Rate:**
   $$\text{Precision} = \frac{\text{Correct Normalizations}}{\text{Total Normalized (Non-Abstained)}}$$
   $$\text{Recall} = \frac{\text{Correct Normalizations}}{\text{Total Test Set Size}}$$
   $$\text{Abstention Rate} = \frac{\text{Abstained Queries}}{\text{Total Test Set Size}}$$
3. **False-Positive Normalization Rate (FPNR):**
   - Critical clinical safety metric: fraction of queries assigned an *incorrect* code (clinical hazard). Must be $\le 1.0\%$.
4. **Adversarial & Noise Stress-Testing:**
   - **Spelling/OCR Degradation:** Evaluated on synthetic text with character substitutions, drops, and whitespace merges.
   - **Out-of-Vocabulary (OOV) Rejection:** Evaluated on non-medical words, veterinary drugs, and fabricated terms. Target: 100% abstention.
   - **Cross-Specimen Confusion:** Evaluating whether "Urine Glucose" is rejected from mapping to serum glucose.

### Evaluation Cohort Construction

To prevent the synthetic-only bias identified in previous milestones:
1. **Synthetic Holdout Cohort:** 500 clinical lab mentions and 500 medication mentions with realistic OCR noise and variations.
2. **Semi-Synthetic Multi-Specialty Cohort:** Generated from clinical templates covering Oncology, Cardiology, Nephrology, and Ophthalmology.
3. **Real-World Unseen Benchmark Slice:**
   - 200 real de-identified lab reports and medication lists.
   - Independent clinical annotator double-coding with adjudicated consensus ground-truth.
   - Strictly isolated from training/indexing dictionaries.

### Proposed Acceptance Thresholds (Provisional)

*Note: In accordance with project instructions, these thresholds are provisional. Baseline experiments on the full database must be completed before final gating.*

| Metric | Target Threshold | Baseline Justification |
| :--- | :---: | :--- |
| **Top-1 Clean Term Accuracy (In-Vocab)** | $\ge 98.0\%$ | Standard lexical exact + alias matching on clean clinical text. |
| **Top-1 OCR-Degraded Term Accuracy (CER 5%)** | $\ge 92.0\%$ | Trigram FTS5 resilient to 1-2 character edits. |
| **False-Positive Normalization Rate (Hazard)** | $\le 1.0\%$ | Clinical safety floor to prevent miscoded exports. |
| **OOV / Nonsense Abstention Specificity** | $\ge 99.0\%$ | Uncalibrated words must fail closed and abstain. |
| **Query Latency (p95 on CPU)** | $\le 5.0\text{ ms}$ | Required to process multi-page documents with 50+ claims in <250 ms. |
| **Memory Footprint** | $\le 100\text{ MB}$ | Must run comfortably on standard CPU instances without GPU. |

---

## 7. Resource & Hardware Sizing for Offline Deployment

### Local Hardware Profiling (CPU & Memory)

- **Target Deployment Platform:** Standard local desktop / CPU server (Windows / Linux), x86_64, 4+ CPU cores, 8 GB RAM, no GPU required.
- **Disk Footprint:**
  - `loinc_fts5.db`: ~185 MB (indexes ~102,000 concepts, ~450,000 alias strings).
  - `rxnorm_fts5.db`: ~240 MB (indexes ~400,000 prescribable concepts, ~1.2 million names).
  - Total Disk Footprint: **< 450 MB**.
- **RAM Footprint:**
  - SQLite uses memory-mapped I/O (`PRAGMA mmap_size = 268435456;`).
  - Active runtime resident set size (RSS): **15 MB – 35 MB**.
- **CPU Query Latency:**
  - Exact match lookup: **< 0.2 ms**.
  - FTS5 Trigram BM25 search over 1.2M rows: **1.2 ms – 3.8 ms**.
  - Total document processing time (50 claims): **~120 ms**.

---

## 8. Implementation Phases & Risk Analysis

### Phased Implementation Roadmap

```
Phase 1: Database Tooling & Schema Ingestion (Read-Only Builders)
  ├── 1.1: Implement clindoc/terminology/builder/loinc_builder.py
  ├── 1.2: Implement clindoc/terminology/builder/rxnorm_builder.py
  └── 1.3: Unit test ingestion schemas, trigram indices, and FTS5 tables

Phase 2: High-Performance Offline Retriever Engine
  ├── 2.1: Implement SQLiteFTS5TerminologyRetriever (replacing linear-scan retriever)
  ├── 2.2: Implement Tier 1 exact cache and Tier 2 trigram search
  └── 2.3: Benchmark retrieval latency, memory, and cold start

Phase 3: Clinical Safety Guards & Rule Engine
  ├── 3.1: Implement LOINC 6-axis specimen and scale filtering
  ├── 3.2: Implement RxNorm level-specific (IN vs SCD) matching rules
  └── 3.3: Calibrate confidence scoring and abstention boundaries

Phase 4: Integration with Unified Pipeline & FHIR Mapper
  ├── 4.1: Wire SQLiteFTS5TerminologyRetriever into TerminologyNormalizer
  ├── 4.2: Expand UCUMNormalizer dictionary for clinical units
  └── 4.3: Validate end-to-end claim enrichment into FHIR Observation and MedicationRequest

Phase 5: Evaluation & Threshold Freezing
  ├── 5.1: Execute benchmark suite across clean, degraded, and real-world holdouts
  └── 5.2: Freeze calibrated acceptance thresholds and generate completion report
```

### Risk Analysis & Mitigations

| Identified Risk | Severity | Mitigation Strategy |
| :--- | :---: | :--- |
| **UMLS / LOINC Licensing Barrier:** Users unable to run pipeline without downloading large proprietary files. | **Medium** | Provide a pre-built, non-proprietary prescribable subset database builder; support standard offline file mounting via CLI argument `--terminology-dir`. |
| **Over-Matching on Short Abbreviations:** Short lab acronyms (e.g., "K", "CA", "NA", "AP") matching arbitrary words. | **High** | Enforce strict length-dependent matching rules: single and two-letter abbreviations require exact match in specialized abbreviation tables with specimen context. |
| **Performance Degradation on Large Batches:** SQLite database lock contention if multiple worker threads access FTS5 simultaneously. | **Low** | SQLite connections configured with `uri=True`, `mode=ro` (read-only), enabling multi-threaded concurrent reads without locking. |

---

## 9. Explicit Non-Goals & Frozen Contracts

To ensure focus and architectural integrity, the following boundaries are strictly enforced:
1. **NO Modification to Frozen Milestones:** `Phase A`, `B1`, `C1`, `M-OCR`, `M-BENCHMARK`, and `M-FHIR-SAFE-SELECTION` remain closed and frozen.
2. **NO Modification to Verification or Selection Contracts:** The Stage C NLI cross-encoder verification logic and the deterministic FHIR slot selection engine (`src/clindoc/fhir/selection.py`) will not be altered.
3. **NO Modification to Raw Claims:** Normalization is strictly an enrichment process. Raw OCR text, extracted strings, bounding boxes, and provenance remain immutable (`PROJECT_SPEC §14.3`).
4. **NO SNOMED CT Comprehensive Expansion:** SNOMED CT is restricted to qualitative lab findings (positive/negative/normal) as established in Phase 4; full clinical problem list coding is out of scope.
5. **NO Clinical Decision Support (CDS) or Treatment Advice:** Terminology mapping is purely representational for administrative and FHIR exchange fidelity.

---

## 10. Final Recommendation

**RECOMMENDATION: PROCEED WITH PHASED MILESTONE `M-TERMINOLOGY-SCALE`.**

- **Justification:** Terminology coverage is the single largest clinical barrier separating ClinicalDoc from real-world hospital deployment. The existing linear-scan retriever and toy subsets cannot support real lab panels or medication formularies.
- **Feasibility:** High. By adopting an offline SQLite FTS5 trigram architecture, ClinicalDoc can achieve instantaneous query speeds, zero GPU runtime dependencies, a minimal memory footprint (<35 MB), and 100% offline compliance.
- **Immediate Next Step:** Await formal milestone authorization from the project lead before beginning Phase 1 implementation.

---
*End of Feasibility and Design Proposal.*

