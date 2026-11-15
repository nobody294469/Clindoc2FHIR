# M-TERMINOLOGY-SCALE — Phase 3A.2 Architecture Correction Sheet
**Refined India-First & Internationally Interoperable Terminology Design**

- **Date:** 2026-11-12  
- **Milestone:** M-TERMINOLOGY-SCALE (Phase 3A.2 — Architecture Correction)  
- **Status:** APPROVED ARCHITECTURAL DIRECTION — PREPARATION ONLY  
- **Git State:** Exactly 75 pre-existing staged files preserved unmodified; 0 commits; 0 branch mutations  
- **Execution Boundaries:** Design only; zero code, database, test, or dataset mutations.  

---

## 1. LOINC 2.83 Official Inventory and Concept Scope

### 1.1 Corrected Concept Inventory
Per the official Regenstrief Institute release distribution (`Loinc_2.83.zip`):
* **Total Concepts across all statuses:** **112,405**
  * `CLASSTYPE = 1` (Laboratory): **69,651** concepts
  * `CLASSTYPE = 2` (Clinical): **28,786** concepts
  * `CLASSTYPE = 3` (Claims / Attachments): **1,161** concepts
  * `CLASSTYPE = 4` (Surveys / Instruments): **12,807** concepts
* **Status Stratification (Across All 112,405 Concepts):**
  * **Active Concepts:** **99,737** (the official active subset)
  * **Trial Concepts:** **3,117**
  * **Discouraged Concepts:** **6,558**
  * **Deprecated Concepts:** **2,993**

### 1.2 Ingestion & Scoping Policy
* **Ingestion:** Ingest all 112,405 rows into the offline database to preserve comprehensive standard reference coverage.
* **Preservation of Source Status:** The source `STATUS` column must be preserved verbatim.
* **Retrieval Policy:** Runtime queries default to filtering for `status = 'ACTIVE'`, preventing deprecated or discouraged terms from silently satisfying clinical slots unless explicitly requested for historical record reconciliation.

---

## 2. Refined Relational Schema: Explicit Domain and Status Columns

To eliminate reliance on unstructured JSON parsing for high-volume domain filtering, `class_type`, `class`, and `status` are promoted to first-class indexed relational columns:

```sql
CREATE TABLE IF NOT EXISTS concepts (
    concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
    system TEXT NOT NULL,              -- 'LOINC', 'CDCI', 'RxNorm'
    code TEXT NOT NULL,                -- Official standard code (e.g. '2823-3', CDCI ID, RxCUI)
    canonical_name TEXT NOT NULL,      -- Official authoritative display string
    tty TEXT,                          -- RxNorm TTY ('IN', 'SCD'), CDCI TTY ('SUBSTANCE', 'GENERIC', 'BRAND'), or LOINC Scale ('Qn')
    class_type INTEGER,                -- LOINC: 1=Lab, 2=Clinical, 3=Claims, 4=Surveys; NULL for medications
    class TEXT,                        -- LOINC: 'CHEM', 'HEM/BC', 'MICRO', 'DOC', etc.
    status TEXT NOT NULL DEFAULT 'ACTIVE', -- 'ACTIVE', 'TRIAL', 'DISCOURAGED', 'DEPRECATED'
    attributes_json TEXT NOT NULL      -- Intrinsic scalar descriptors (e.g. CAS, UNII, 6 axes details)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_concepts_system_code ON concepts(system, code);
CREATE INDEX IF NOT EXISTS idx_concepts_system_domain ON concepts(system, class_type);
CREATE INDEX IF NOT EXISTS idx_concepts_system_status ON concepts(system, status);
CREATE INDEX IF NOT EXISTS idx_concepts_system_class ON concepts(system, class);
```

---

## 3. Structural Medication Model: Relational Graph & Multi-Attribute Equivalence

### 3.1 Eliminating Alias Rules for Route and Form
* **Correction:** Medication route, dose form, strength, and supplier are **intrinsic attributes and relational links of the medication concept**, not lexical rules attached to an alias text.
* `alias_context_rules` is restricted strictly to alias-level linguistic constraints (e.g., lab acronym specimen rules or disallowed homonym contexts).

### 3.2 Complex Relationship Topology (N:M Support)
In real-world pharmaceutical markets (especially in India), 1:1 assumptions fail:
* One **Brand** may have multiple manufacturers/suppliers (contract manufacturing, repackaging, regional distribution).
* One **Brand** name may encompass multiple generic formulations (e.g., combination kits, differing strength ratios, or pediatric vs adult formulations).
* Multi-ingredient combinations (e.g., *Amoxicillin 500 mg + Clavulanic Acid 125 mg*) must preserve multiple active moieties simultaneously.

### 3.3 Authoritative Relationship Table (`concept_relationships`)
Relational edges are stored in an authoritative relationship graph, eliminating duplicate, desynchronized attributes:

```sql
CREATE TABLE IF NOT EXISTS concept_relationships (
    relationship_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id_from INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    concept_id_to INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,    -- 'HAS_SUBSTANCE', 'HAS_DOSE_FORM', 'HAS_ROUTE', 'TRADENAME_OF', 'MANUFACTURED_BY'
    relationship_source TEXT NOT NULL,  -- 'CDCI_FLAT_FILES', 'RXNORM_RXNREL', 'NRCES_EXTENSION'
    source_relationship_id TEXT,        -- Original identifier from source archive (e.g. RXNREL row ID)
    attributes_json TEXT                -- Edge metadata (e.g. strength numerator/denominator, is_active_moiety)
);

CREATE INDEX idx_rel_from ON concept_relationships(concept_id_from, relationship_type);
CREATE INDEX idx_rel_to ON concept_relationships(concept_id_to, relationship_type);
```

### 3.4 Definition of Clinical Equivalence
Two medication concepts are clinically equivalent if and only if they match across **all clinically material product attributes**:
1. Exact active ingredients / moieties (including specific salt/base identification).
2. Exact strength / concentration and unit of measure (UCUM-standardized).
3. Physical dose form (e.g., capsule vs tablet vs oral suspension).
4. Intended route of administration (e.g., oral vs topical vs IV).
5. Release mechanism / kinetics (e.g., immediate-release vs extended-release `ER` vs gastro-resistant/enteric-coated `EC`).

> [!CAUTION]
> Products that share active ingredients and strengths but differ in release characteristics (e.g., Metformin 500 mg IR vs Metformin 500 mg SR) or route are **pharmacokinetically distinct** and must **never** be treated as clinically equivalent.

### 3.5 Partitioning CDCI and RxNorm
CDCI (`system = 'CDCI'`) and RxNorm (`system = 'RxNorm'`) remain strictly separated in the `concepts` table. They are linked only through explicit entries in `concept_mappings`.

---

## 4. Cross-Source Mapping Architecture

Cross-source mappings (CLCI to LOINC, CDCI generic to RxNorm, or CDCI to SNOMED CT) reside in a dedicated versioned table:

```sql
CREATE TABLE IF NOT EXISTS concept_mappings (
    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,          -- 'CDCI'
    source_code TEXT NOT NULL,            -- 'GEN_00123'
    target_system TEXT NOT NULL,          -- 'RxNorm'
    target_code TEXT NOT NULL,            -- '310965'
    relationship_type TEXT NOT NULL,      -- 'EQUIVALENT', 'BROADER', 'NARROWER', 'RELATED', 'UNRESOLVED'
    mapping_source TEXT NOT NULL,         -- 'NRCES_OFFICIAL_MAP', 'NLM_UMLS', 'EXPERT_CURATED'
    evidence_json TEXT NOT NULL,          -- Audit trail: basis of mapping, release comparison
    release_id TEXT NOT NULL,             -- Mapping release identifier
    review_status TEXT NOT NULL,          -- 'OFFICIAL', 'HUMAN_VERIFIED', 'PROVISIONAL'
    created_at TEXT NOT NULL
);

CREATE INDEX idx_map_src ON concept_mappings(source_system, source_code);
CREATE INDEX idx_map_tgt ON concept_mappings(target_system, target_code);
```

* **No Automated Lexical Equivalence:** Text similarity alone is strictly prohibited from creating `EQUIVALENT` mappings.
* **Hierarchy:** `BROADER`/`NARROWER` are used when granularity differs (e.g. multi-ingredient generic vs single ingredient). `UNRESOLVED` mappings remain flagged for clinical review.

---

## 5. Non-Destructive Alias Storage & FHIR Display Provenance

### 5.1 Three-Tier Display and Text Architecture
To guarantee clinical safety and strict regulatory traceability, the terminology and export layers distinguish three distinct string entities:

```
1. Extracted Document Evidence (source_text)
   └── "Tab. Augmentin 625 Duo 1 BD x 5 days"
       • Verbatim span from OCR / document text.
       • Stored in extraction provenance and FHIR CodeableConcept.text.

2. Matched Terminology Alias (concept_aliases.raw_display_text)
   └── "Augmentin® 625 Duo"
       • Exact surface string from the source vocabulary.
       • Preserves original casing, legal symbols, and punctuation.
       • Used for audit logging and clinical reviewer screens.

3. Canonical Release Name (concepts.canonical_name)
   └── "Amoxicillin 500 MG / Clavulanate Potassium 125 MG Oral Tablet"
   └── LOINC: "Hemoglobin [Mass/volume] in Blood"
       • Authoritative display string mandated by the coding authority.
       • Used strictly for FHIR Coding.display.
```

### 5.2 Alias Table Schema
```sql
CREATE TABLE IF NOT EXISTS concept_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL REFERENCES concepts(concept_id) ON DELETE CASCADE,
    raw_display_text TEXT NOT NULL,      -- Verbatim source string ("Augmentin® 625 Duo")
    search_text TEXT NOT NULL,           -- Normalized search token ("augmentin 625 duo")
    language TEXT NOT NULL DEFAULT 'en', -- ISO language code
    alias_source TEXT NOT NULL,          -- 'CDCI_BRAND_MASTER', 'LOINC_SHORTNAME'
    source_identifier TEXT,              -- Source atom ID (e.g. RXAUI, CDCI Trade ID)
    is_abbreviation BOOLEAN NOT NULL DEFAULT 0,
    is_preferred BOOLEAN NOT NULL DEFAULT 0
);
```

---

## 6. CDCI & CLCI Empirical Source Verification Protocol

* **No Premature Schema Hardcoding:** The CDCI Flat Files parser will not hardcode column names until the actual release archive from NRCeS is unpacked and inspected.
* **Terminology-Integrated Package Evaluation:** The India Drug Extension for SNOMED CT (RF2) will be inspected as a secondary resource:
  * If the Flat Files provide complete substance, generic, brand, supplier, form, and route TSVs, the Flat Files serve as primary.
  * If specific multi-ingredient relationships are only present in the RF2 relationship tables, those specific RF2 tables will be ingested into `concept_relationships` without deploying a full SNOMED logic engine.

---

## 7. Performance Benchmarks, Atomic Publication, and Rigorous Evaluation

### 7.1 Provisional Performance Benchmarks
All performance metrics are treated as provisional targets to be empirically measured on the full release:

| Metric | Provisional Target | Acceptance Gate | Verification Method |
|---|---|---|---|
| **Compilation Time** | $< 180$ seconds | $< 300$ seconds | Clean-room end-to-end build |
| **Active Service Heap** | $< 150$ MB | $< 250$ MB | `tracemalloc` peak memory during service load |
| **Exact Lookup (p95)** | $< 2.5$ ms | $< 5.0$ ms | 1,000 iterations over exact reference aliases |
| **Unicode BM25 (p95)** | $< 12.0$ ms | $< 20.0$ ms | 500 multi-word FTS queries |
| **Trigram Search (p95)** | $< 15.0$ ms | $< 25.0$ ms | 500 substring FTS queries |

### 7.2 Independent Evaluation Metrics
The pipeline evaluation separates extraction, retrieval, and selection into distinct independent metrics:
1. **Extraction Accuracy:** Measured independently by Character/Word Error Rate (CER/WER) and Entity Span $F_1$.
2. **Terminology Retrieval Accuracy:**
   $$\text{Top-}k\text{ Retrieval Recall} = \frac{\text{Queries with correct concept in top-}k}{\text{Total valid clinical queries}}$$
   $$\text{Exact Resolution Precision} = \frac{\text{Correct exact alias resolutions}}{\text{Total exact resolutions}}$$
   $$\text{Fail-Closed Safety Rate} = \frac{\text{Correctly abstained missing/ambiguous queries}}{\text{Total missing/ambiguous queries}}$$
3. **End-to-End Task Evaluation:** Evaluated on the 34 canonical benchmark documents.

### 7.3 Evaluation Role of the 696-Group Baseline
* **Behavioral Regression Guard:** The 696-group oracle (`tests/fixtures/fhir_safe_selection_696_oracle.json`) is strictly a **behavioral regression baseline** for the pre-existing selector, **NOT** independent clinical ground truth.
* **Independent Ground Truth Required:** A separate, clinician-adjudicated evaluation dataset of Indian lab panels and e-prescriptions will be constructed to evaluate clinical retrieval accuracy independently.

---

## 8. SNOMED CT India Extension Integration Conditions

SNOMED CT India Drug Extension integration remains **DEFERRED**. It will be authorized if and only if:
1. The organization holds a verified SNOMED CT Affiliate License in Good Standing via MLDS India.
2. A formal ABDM / regulatory milestone mandates SNOMED CT concept identifiers (SCTID) on exported FHIR bundles.
3. Complex ontological subsumption (e.g., querying all derivatives of a broad therapeutic class) is required that cannot be represented in flat relational hierarchies.

---

## Change Summary (Phase 3A.1 $\rightarrow$ Phase 3A.2)

1. **LOINC 2.83 Inventory:** Corrected class breakdown (type 1 = 69,651; type 2 = 28,786; type 3 = 1,161; type 4 = 12,807; total = 112,405 across all statuses). Documented official active count of 99,737.
2. **Relational LOINC Schema:** Added explicit `class_type`, `class`, and `status` columns with database indexes to `concepts`.
3. **Medication Data Model:** Removed route and form from `alias_context_rules`. Introduced `concept_relationships` table supporting N:M brand-generic-supplier mappings. Defined equivalence across all material clinical attributes.
4. **FHIR Display Provenance:** Established three-tier string architecture (`source_text` $\rightarrow$ `raw_display_text` $\rightarrow$ `canonical_name` for `Coding.display`).
5. **Evaluation Rigor:** Explicitly demarcated the 696-group oracle as a behavioral regression tool, requiring an independent adjudicated benchmark for clinical retrieval.

