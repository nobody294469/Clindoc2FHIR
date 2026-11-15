# M-TERMINOLOGY-SCALE — Phase 3A.3 Physical Archive Inspection Report

- **Date:** 2026-11-12  
- **Milestone:** M-TERMINOLOGY-SCALE (Phase 3A.3 — Physical Archive Inspection)  
- **Inspection Staging Location:** `D:\terminology_sources_staging\` (external to repository tree)  
- **Execution Mode:** Read-Only Physical Archive Inspection (Zero code/database mutations; zero Git mutations)  
- **Git State:** Exactly 75 pre-existing staged files preserved unmodified; 0 commits; 0 branch mutations  

---

## 1. Archive Inventory, Hashes, and Integrity Verification

All four official source distribution archives located in `D:\terminology_sources_staging\` were verified for archive integrity (CRC32 checksums via `zipfile.testzip()`) and cryptographic digests:

| Source Package | Staged Filename | File Size (Bytes) | MD5 Hash | SHA-256 Hash | Zip Integrity | Member Count |
|---|---|---|---|---|---|---|
| **LOINC 2.83** | `Loinc_2.83.zip` | 92,815,327 | `057ddf203164705d5a4c3604257060a4` | `077a0718e87d8309ffe3a673f75b836a8e783dc36a646413ef97c71c12eab27e` | **OK** (Valid) | 95 |
| **NRCeS CLCI** | `common-lab-codes-for-india-20260629.zip` | 43,797 | `55bc39f3d2cb12bb17499e7d6b1eebf1` | `d9db3941acdb7c5109b660e38a633076149230d35f0050a503ff156c927f3009` | **OK** (Valid) | 4 |
| **NRCeS CDCI (Flat Files)**| `CommonDrugCodesForIndia_FlatFilePackage.zip` | 4,148,530 | `cbee84f5a02da14bea53d43b085c623d` | `aebd30ac95d73accf573c86fd4dae1dc90eff03ae0fbcd312bc761011e88270a` | **OK** (Valid) | 10 |
| **NLM RxNorm CPC** | `RxNorm_full_prescribe_11022026.zip` | 75,032,750 | `cd240fc9655dcd2cb54b1e185f3d8702` | `c742a6c4b2995194aa3479400e08a7c1ad707178bfc80fe9c8cd7afa693ae987` | **OK** (Valid) | 16 |

### Verification Findings
* **RxNorm CPC Published Hash Match:** The calculated MD5 digest for `RxNorm_full_prescribe_11022026.zip` (`cd240fc9655dcd2cb54b1e185f3d8702`) matches the NLM published release checksum **identically**.
* **Distribution Authenticity:** Confirmed that `RxNorm_full_prescribe_11022026.zip` is strictly the non-proprietary Current Prescribable Content (CPC) release containing only non-proprietary source vocabularies (`RXNORM`, `MTHSPL`, `MTHCMSFRF`) and zero commercial third-party SABs.
* **Archive Non-Corruption:** All 4 archives passed 100% integrity validation with zero CRC32 errors or corrupted zip headers.

---

## 2. Physical Inspection Results: Source-by-Source

### 2.1 LOINC Version 2.83 (`Loinc_2.83.zip`)

* **Primary File:** `LoincTable/Loinc.csv` (84,205,426 bytes uncompressed)
* **Encoding & Format:** UTF-8 with BOM (`utf-8-sig`), comma-delimited (`","`), RFC 4180 compliant with quoted multiline strings.
* **Column Inventory (40 Headers):**
  `LOINC_NUM`, `COMPONENT`, `PROPERTY`, `TIME_ASPCT`, `SYSTEM`, `SCALE_TYP`, `METHOD_TYP`, `CLASS`, `VersionLastChanged`, `CHNG_TYPE`, `DefinitionDescription`, `STATUS`, `CONSUMER_NAME`, `CLASSTYPE`, `FORMULA`, `EXMPL_ANSWERS`, `SURVEY_QUEST_TEXT`, `SURVEY_QUEST_SRC`, `UNITSREQUIRED`, `RELATEDNAMES2`, `SHORTNAME`, `ORDER_OBS`, `HL7_FIELD_SUBFIELD_ID`, `EXTERNAL_COPYRIGHT_NOTICE`, `EXAMPLE_UNITS`, `LONG_COMMON_NAME`, `EXAMPLE_UCUM_UNITS`, `STATUS_REASON`, `STATUS_TEXT`, `CHANGE_REASON_PUBLIC`, `COMMON_TEST_RANK`, `COMMON_ORDER_RANK`, `HL7_ATTACHMENT_STRUCTURE`, `EXTERNAL_COPYRIGHT_LINK`, `PanelType`, `AskAtOrderEntry`, `AssociatedObservations`, `VersionFirstReleased`, `ValidHL7AttachmentRequest`, `DisplayName`.

#### Record Counts and Key Integrity
* **Total Concept Rows:** **112,405**
* **Primary Key (`LOINC_NUM`) Uniqueness:** **112,405 unique codes** (0 duplicates; 100% unique primary key).
* **Official Status Breakdown:**
  * `ACTIVE`: **99,737** (88.73%)
  * `TRIAL`: **5,436** (4.84%)
  * `DEPRECATED`: **5,008** (4.46%)
  * `DISCOURAGED`: **2,224** (1.98%)
* **Domain / Class Type (`CLASSTYPE`) Breakdown:**
  * `CLASSTYPE = 1` (Laboratory): **69,651** concepts
  * `CLASSTYPE = 2` (Clinical): **28,786** concepts
  * `CLASSTYPE = 3` (Claims / Attachments): **1,161** concepts
  * `CLASSTYPE = 4` (Surveys / Questionnaires): **12,807** concepts
* **Top Laboratory Classes (`CLASS`):**
  * `MICRO` (16,109), `CHEM` (10,872), `DRUG/TOX` (10,194), `ALLERGY` (6,169), `SERO` (3,171), `HEM/BC` (2,504), `COAG` (1,245).
* **Unit Standardization:** `EXAMPLE_UCUM_UNITS` is officially present as a distinct field on 24,198 laboratory concepts, providing an authoritative direct bridge to UCUM representations.

---

### 2.2 NRCeS Common Lab Codes for India (`common-lab-codes-for-india-20260629.zip`)

* **Primary File:** `common-lab-codes-for-india-20260629/common-lab-codes-for-india.csv` (215,903 bytes uncompressed)
* **Encoding & Format:** UTF-8, comma-delimited (`","`), 4 columns:
  1. `General Name`: Common Indian lab test name.
  2. `LOINC Code`: Referenced standard LOINC code.
  3. `Fully-Specified Name (FSN)`: 6-part formal LOINC term.
  4. `Long Common Name`: Human-readable display name.
* **Accompanying Documentation:** `NOTICE.txt` (1,338 bytes), `README.txt` (1,804 bytes).

#### Integrity & Cross-Validation against LOINC 2.83
* **Total Records:** **1,473**
* **Unique LOINC Codes:** **1,473**
* **Validation against Acquired LOINC 2.83:** **1,473 / 1,473 (100.00% valid)**. Every single LOINC code references a genuine, valid code in `Loinc.csv`.
* **LOINC Status in 2.83:**
  * `ACTIVE`: **1,471** (99.86%)
  * `DISCOURAGED`: **2** (codes `11283-9` and `14196-0` are flagged discouraged in 2.83, but retained in CLCI release notes).
  * `DEPRECATED`: **0**
* **LOINC ClassType in 2.83:**
  * `ClassType 1` (Laboratory): **1,468** (99.66%)
  * `ClassType 2` (Clinical): **4** (e.g. pulse rate / vital signs bundled in lab panels)
  * `ClassType 3` (Claims): **1**
* **Ambiguity & Alias Collision Findings:**
  * Unique Indian General Names: **1,264**.
  * **173 General Names map to multiple LOINC codes** (e.g., `'Calcium, Urine'` maps to 3 codes (`18488-7`, `6874-2`, `17862-4`); `'Albumin, Urine'` maps to 6 distinct LOINC codes depending on timing and collection method; `'Eosinophils, Blood'` maps to 2 codes).
  * **Architectural Confirmation:** This physical finding validates the Phase 3A.2 design requirement: the retrieval engine **must preserve ambiguity** and return candidate sets rather than forcing a lossy 1:1 resolution.

---

### 2.3 NRCeS CDCI Flat Files Package (`CommonDrugCodesForIndia_FlatFilePackage.zip`)

* **Package Inventory (7 TSV Data Files + Documentation):**
  * `SubstanceMaster.txt` (362,703 bytes)
  * `GenericMaster.txt` (2,598,230 bytes)
  * `BrandMaster.txt` (11,152,784 bytes)
  * `ProductMaster.txt` (1,997,407 bytes)
  * `SupplierMaster.txt` (410,366 bytes)
  * `DrugFormMaster.txt` (16,252 bytes)
  * `RouteOfAdministrationMaster.txt` (4,930 bytes)
  * `License.txt` (1,616 bytes), `README.txt` (1,108 bytes)
* **Encoding & Format:** UTF-8, Tab-Separated Values (`\t`).

#### Record Counts & Primary Keys
* **`SubstanceMaster`:** **3,283 rows** (3,283 unique `Identifier`s; 0 duplicates).
  * Headers: `Identifier`, `Substance Name`, `CAS Number`, `UNII`, `Substance Description`, `Molecular Weight`, `Toxicity`, `SMILE`, `InChI`, `IUPAC Name`, `Molecular Formula`, `last_updated_on`.
* **`GenericMaster`:** **10,305 rows** (10,305 unique `Identifier`s; 0 duplicates).
  * Headers: `Identifier`, `Generic Name`, `Substance Identifier`, `Route of Administration`, `Dose Form`, `Therapeutic Role`, `Indication`, `Contra Indication`, `Interaction with Drugs`, `Classification of Drugs`, `Source/ Regulatory`, `last_updated_on`.
* **`BrandMaster`:** **93,911 rows** (93,911 unique `Identifier`s; 0 duplicates).
  * Headers: `Identifier`, `Brand Name`, `Product Identifier`, `Supplier Identifier`, `Generic Identifier`, `License Number`, `License Status`, `Excipient`, `last_updated_on`.
* **`ProductMaster`:** **72,284 rows** (72,284 unique `Identifier`s; 0 duplicates).
  * Headers: `Identifier`, `Product Name`.
* **`SupplierMaster`:** **8,213 rows** (8,213 unique `Identifier`s; 0 duplicates).
  * Headers: `Identifier`, `Supplier Name`, `Country`.
* **`DrugFormMaster`:** **423 rows** (423 unique `Id`s). Headers: `Id`, `Dose Form`.
* **`RouteOfAdministrationMaster`:** **160 rows** (160 unique `Identifier`s). Headers: `Identifier`, `RouteOfAdministration`.

#### Relational Foreign-Key Integrity Verification
1. **`Generic -> Substance`:**
   * Single-substance generics: **8,141**
   * Multi-substance combination generics: **2,164** (substances joined by `+`, e.g. `'387362001+65741009'`)
   * Total referenced substance tokens: **13,127**
   * Unmatched substance foreign keys: **0** (**100.00% foreign key resolution**).
2. **`Generic -> Dose Form`:**
   * Unmatched dose form foreign keys: **0** (**100.00% resolution** against `DrugFormMaster`).
3. **`Generic -> Route`:**
   * Referenced routes (single and multi-route joined by `+`): **0 unmatched** (**100.00% resolution** against `RouteOfAdministrationMaster`).
4. **`Brand -> Generic`:**
   * Valid brand-to-generic mappings: **93,897** (99.985%)
   * Unmatched generic foreign keys: **14** (orphaned brands to be quarantined during ingestion).
   * Cardinality: 4,597 generics have at least 1 brand. Maximum brands for a single generic is **1,580** (e.g., Paracetamol oral formulations).
5. **`Brand -> Supplier`:**
   * Unmatched supplier foreign keys: **0** (**100.00% resolution** against `SupplierMaster`).
   * Maximum brands associated with a single supplier: **1,594**.
6. **`Brand -> Product`:**
   * Unmatched product foreign keys: **0** (**100.00% resolution** against `ProductMaster`).

---

### 2.4 NLM RxNorm Current Prescribable Content (`RxNorm_full_prescribe_11022026.zip`)

* **Distribution Contents:**
  * `rrf/RXNCONSO.RRF` (30,721,386 bytes uncompressed)
  * `rrf/RXNREL.RRF` (199,323,432 bytes uncompressed)
  * `rrf/RXNSAT.RRF` (284,405,614 bytes uncompressed)
  * `Readme_Full_Prescribe_11022026.txt` (3,075 bytes)
* **Encoding & Format:** UTF-8, pipe-delimited (`|`), Rich Release Format (RRF).

#### RXNCONSO.RRF Verification
* **Total Concept Rows:** **247,388**
* **Unique RxCUIs:** **82,773**
* **Source Vocabulary Breakdown (SAB):**
  * `RXNORM`: **141,195** rows
  * `MTHSPL` (FDA Structured Product Labels): **106,182** rows
  * `MTHCMSFRF`: **11** rows
  * **Commercial / Proprietary SABs: 0** (confirms 100% open CPC content).
* **Key Prescribable Term Types (TTY):**
  * Semantic Clinical Drug (`SCD`): 12,098
  * Semantic Branded Drug (`SBD`): 8,099
  * Clinical Drug Component (`SCDC`): 10,186
  * Branded Drug Component (`SBDC`): 7,030
  * Ingredient (`IN`): 5,886
  * Precise Ingredient (`PIN`): 1,961
  * Dose Form (`DF`): 120
  * Dose Form Group (`DFG`): 43
  * Generic / Brand Packs (`GPCK` / `BPCK`): 623 / 717
* **Quality Flags:** 100% of lines have `LAT = 'ENG'` and `SUPPRESS = 'N'`.

#### RXNREL.RRF Verification
* **Total Relationship Edges:** **2,594,942**
* **Foreign Key Integrity:** Unmatched `RXCUI1` = **0**; Unmatched `RXCUI2` = **0** (**100.00% endpoint resolution**).
* **Key Clinical Relationship Labels (`RELA`):**
  * `has_active_ingredient` / `active_ingredient_of`: **223,910**
  * `has_active_moiety` / `active_moiety_of`: **222,934**
  * `has_ingredient` / `ingredient_of`: **109,704**
  * `has_dose_form` / `dose_form_of`: **104,000+**
  * `tradename_of` / `has_tradename`: **90,000+**

#### RXNSAT.RRF Verification
* **Total Attribute Rows:** **3,384,661**
* **Key Attributes (`ATN`):** `SPL_SET_ID` (1,716,027), `NDC` (443,319), `LABELER` (95,248), `MARKETING_CATEGORY` (92,790), `RXN_VANDF_DOSE_FORM` (12,000+).

---

## 3. Cross-Source Artifact Finding: CDCI-to-RxNorm

* **Physical Inspection Finding:** Comprehensive search across all 4 archives confirms that **no official CDCI-to-RxNorm mapping artifact exists**.
  * `Loinc_2.83.zip`: Contains only IEEE and RSNA mappings.
  * `common-lab-codes-for-india-20260629.zip`: Contains only Indian General Names to LOINC.
  * `CommonDrugCodesForIndia_FlatFilePackage.zip`: Contains only internal CDCI foreign keys and SNOMED-derived identifiers.
  * `RxNorm_full_prescribe_11022026.zip`: Contains only US NLM/FDA data.
* **Architectural Impact:** This physical absence confirms that the Phase 3A.2 architectural mandate was correct:
  * **CDCI and RxNorm must remain strictly separate coding systems (`system='CDCI'` vs `system='RxNorm'`).**
  * Equivalence must **never** be synthesized from raw text similarity.
  * Cross-source mappings will be stored in `concept_mappings` only when official or clinician-adjudicated mapping datasets become available.

---

## 4. Licensing and Attribution Assessment (Verified from Physical Files)

| Source | Physical Document Inspected | Core Terms & Redistribution Rights | Mandatory Attribution Required |
|---|---|---|---|
| **LOINC 2.83** | `LoincLicense_5.8.txt` | Free worldwide for commercial and non-commercial use. Redistribution of unaltered tables permitted. Forbids using LOINC to establish a separate competing clinical standard. | `LOINC_COPYRIGHT_NOTICE` (mandates citation of Regenstrief Institute, Inc. and LOINC Committee). |
| **NRCeS CLCI** | `NOTICE.txt` & `README.txt` | Released by NRCeS / C-DAC Pune under MoHFW, Govt. of India. Free for adoption in healthcare systems in India. Cites underlying LOINC 2.82/2.83 copyright. | Must cite C-DAC Pune and NRCeS MoHFW as source. |
| **NRCeS CDCI (Flat Files)**| `License.txt` | Released by NRCeS / C-DAC Pune under **Creative Commons Attribution 4.0 International (CC BY 4.0)**. Explicitly authorizes redistribution and creation of derivative implementation products within India. Acknowledges SNOMED International trademark. | Must cite C-DAC Pune / NRCeS and CC BY 4.0 license. Brand names remain copyright of respective owners. |
| **NLM RxNorm CPC** | `Readme_Full_Prescribe_11022026.txt` | U.S. Government work (NLM, NIH). Open public prescribable content (`RXNORM`, `MTHSPL`). Direct open distribution. | `RXNORM_ATTRIBUTION_NOTICE` (must state data is courtesy of NLM, NIH, HHS and that NLM does not endorse product). |

---

## 5. Coverage Analysis Against Phase 3A.2 Architecture

| Architectural Component | Physical Source Support | Readiness Disposition |
|---|---|---|
| **LOINC Domain Columns (`class_type`, `class`)** | Direct physical columns `CLASSTYPE` (values 1, 2, 3, 4) and `CLASS` in `Loinc.csv`. | **READY** — 100% direct column alignment. |
| **LOINC Status Column (`status`)** | Direct physical column `STATUS` in `Loinc.csv` (`ACTIVE`, `TRIAL`, `DEPRECATED`, `DISCOURAGED`). | **READY** — 100% direct column alignment. |
| **CLCI Overlay on LOINC** | 1,473 codes in `common-lab-codes-for-india.csv` with 100% validity in LOINC 2.83. | **READY** — Ingest as Indian alias overlay (`CLCI_GENERAL_NAME`). |
| **CDCI Dedicated Builder (`cdci_builder.py`)** | 7 discrete TSV master files with clean integer foreign-key relationships. | **READY** — Clean relational architecture; does not touch RxNorm parser. |
| **Multi-Substance Combinations** | Represented in `GenericMaster.txt` via `+`-delimited SCTIDs (2,164 combination drugs). | **READY** — Split on `+` and generate multiple `HAS_SUBSTANCE` edges in `concept_relationships`. |
| **Medication Route & Dose Form Edges** | Represented in `GenericMaster.txt` referencing `DrugFormMaster` and `RouteOfAdministrationMaster`. | **READY** — Generate `HAS_DOSE_FORM` and `HAS_ROUTE` edges in `concept_relationships`. |
| **Supplier Attribution** | Represented in `BrandMaster.txt` referencing `SupplierMaster.txt` (8,213 suppliers). | **READY** — Generate `MANUFACTURED_BY` edges in `concept_relationships`. |
| **RxNorm Intrinsic Graph** | `RXNREL.RRF` contains 2.59M semantic edges (`has_active_ingredient`, `has_dose_form`). | **READY** — Ingest prescribable edges into `concept_relationships`. |
| **Cross-Source Mappings** | No official CDCI-to-RxNorm mapping artifact in archives. | **READY** — Maintain empty `concept_mappings` table pending curated bridges. |

---

## 6. Recommended Ingestion Transformations & Builder Actions

1. **`LoincIngestionBuilder`:**
   * Stream `Loinc.csv`.
   * Populate `concepts` with `system='LOINC'`, `code=LOINC_NUM`, `canonical_name=LONG_COMMON_NAME`, `class_type=CLASSTYPE`, `class=CLASS`, `status=STATUS`.
   * Ingest aliases: `raw_display_text` verbatim from `LONG_COMMON_NAME`, `SHORTNAME`, `CONSUMER_NAME`, and `RELATEDNAMES2`; generate normalized `search_text`.
2. **`ClciIngestionBuilder` (Overlay):**
   * Stream `common-lab-codes-for-india.csv`.
   * Match `LOINC Code` in `concepts`.
   * Insert `General Name` into `concept_aliases` (`raw_display_text=General Name`, `alias_source='CLCI_GENERAL_NAME'`).
   * Flag `attributes_json: {"in_clci": true}` on the concept.
3. **`CdciIngestionBuilder` (Dedicated Builder):**
   * Parse `SubstanceMaster.txt` $\rightarrow$ `concepts(system='CDCI', tty='SUBSTANCE')`.
   * Parse `GenericMaster.txt` $\rightarrow$ `concepts(system='CDCI', tty='GENERIC')`; generate `HAS_SUBSTANCE`, `HAS_DOSE_FORM`, `HAS_ROUTE` edges in `concept_relationships`.
   * Parse `BrandMaster.txt` $\rightarrow$ `concepts(system='CDCI', tty='BRAND')`; generate `TRADENAME_OF` (to Generic) and `MANUFACTURED_BY` (to Supplier) edges in `concept_relationships`. Quarantine the 14 orphaned brands.
   * Parse `ProductMaster.txt` $\rightarrow$ `concept_aliases(alias_source='CDCI_PRODUCT_NAME')`.
4. **`RxNormIngestionBuilder`:**
   * Ingest `RXNCONSO.RRF` with `SAB IN ('RXNORM', 'MTHSPL')` $\rightarrow$ `concepts(system='RxNorm')`.
   * Ingest `RXNREL.RRF` prescribable edges $\rightarrow$ `concept_relationships`.
5. **Post-Processing & Optimization:**
   * Execute batch transaction commits every 10,000 rows.
   * Populate FTS5 external tables (`fts_aliases_unicode`, `fts_aliases_trigram`) in bulk from `concept_aliases.search_text`.
   * Run `optimize` and `PRAGMA wal_checkpoint(TRUNCATE)`.

---

## 7. Outstanding Decisions & Prerequisites Before Phase 3B

Before Phase 3B implementation can be authorized:
1. **Approval of CDCI Orphan Policy:** Authorize discarding or flagging the **14 orphaned brands** in `BrandMaster.txt` that reference non-existent `Generic Identifier`s.
2. **Approval of Staging Location for Build:** Authorize the database compiler to read directly from `D:\terminology_sources_staging\` during compilation.
3. **Compilation Scale Acceptance:** Acknowledge that the compiled database will encompass ~205,000 concepts, ~600,000 aliases, and ~500,000 relationship edges (estimated SQLite size ~130–150 MB).

---

## 8. Scope Boundaries Honored

- **Inspection Only:** All analyses were executed via streaming read-only inspection directly from the archive files.
- **Repository Untouched:** 0 files modified in `src/`, `tests/`, or database files; zero Git mutations.
- **Staged Git State:** Exactly 75 pre-existing staged files preserved untouched.
- **Archive Preservation:** Downloaded archives in `D:\terminology_sources_staging\` remain completely unaltered and intact.

---

## 9. Phase 3A.3 — Final Evidence Reconciliation Addendum

### 9.1 RxNorm CPC — MTHCMSFRF Exception Reconciliation

During physical inspection of `rrf/RXNCONSO.RRF` in `RxNorm_full_prescribe_11022026.zip`, exactly **11 records** were identified with `SAB = 'MTHCMSFRF'`. 

#### A. Complete Inventory of MTHCMSFRF Records
| RxCUI | SAB | TTY | Source Code | Term Name (`STR`) | Overlaps RXNORM SAB? |
|---|---|---|---|---|:---:|
| **3686** | MTHCMSFRF | PT | NOCODE | `OTC product` | No |
| **126628** | MTHCMSFRF | PT | NOCODE | `Diabetic test strips` | No |
| **244822** | MTHCMSFRF | PT | NOCODE | `Disposable insulin syringe U-100 0.5 ML` | No |
| **244823** | MTHCMSFRF | PT | NOCODE | `Disposable insulin syringe U-100 1 ML` | No |
| **248458** | MTHCMSFRF | PT | NOCODE | `Disposable insulin syringe U-100 0.3 ML` | No |
| **313043** | MTHCMSFRF | PT | NOCODE | `Sodium fluoride 2.2 MG (fluoride ion 1 MG) Oral Tablet` | No |
| **636967** | MTHCMSFRF | PT | NOCODE | `Gauze pads and dressing - 2 X 2 Pads` | No |
| **637093** | MTHCMSFRF | PT | NOCODE | `Insulin pen needle` | No |
| **724217** | MTHCMSFRF | PT | NOCODE | `Disposable insulin safety needles` | No |
| **797463** | MTHCMSFRF | PT | NOCODE | `Prenatal Vitamins with Minerals and Folic Acid Greater than 0.8 MG Oral Tablet` | No |
| **797544** | MTHCMSFRF | PT | NOCODE | `isopropyl alcohol 70 % Medicated Pad` | **Yes** (also has `RXNORM` entries) |

#### B. Relationship and Attribute Linkage Analysis
* **`RXNREL.RRF` Linkage:**
  * Only RxCUI `797544` (isopropyl alcohol 70% Medicated Pad) participates in semantic relationship edges (10 edges, e.g. `has_dose_form`, `consists_of`, `isa`).
  * The remaining 10 RxCUIs have **zero** relationship rows in `RXNREL.RRF`.
* **`RXNSAT.RRF` Linkage:**
  * Only RxCUI `797544` has attribute entries in `RXNSAT.RRF` (4,547 attributes including FDA SPL IDs, NDC codes, and labelers).
  * The other 10 RxCUIs have **zero** attributes in `RXNSAT.RRF`.

#### C. Official Documented Provenance and Licensing
* **Definition:** `MTHCMSFRF` represents the Metathesaurus Centers for Medicare & Medicaid Services (CMS) Formulary Reference File.
* **Licensing:** Produced by the U.S. National Library of Medicine (NLM) in collaboration with CMS (U.S. Department of Health and Human Services). It is a public domain federal dataset and is non-proprietary (does not require commercial licensing).
* **Clinical Semantics:** 9 of the 11 records represent non-drug medical supplies, diabetic accessories (syringes, pen needles, test strips, dressings), or administrative CMS formulary categories (`"OTC product"`), rather than standardized clinical active ingredients or prescribable drugs.

#### D. Architectural Recommendation
* **Policy:** Maintain the core clinical medication allowlist strictly as `SUPPORTED_CPC_SABS = {'RXNORM', 'MTHSPL'}` and prescribable TTYs (`IN`, `PIN`, `MIN`, `SCD`, `SBD`, `SCDC`, `SBDC`, `DF`, `GPCK`, `BPCK`).
* **Disposition:** Exclude `MTHCMSFRF` rows (`TTY = 'PT'`) from the normalized clinical medication dictionary. Including them would pollute drug clinical concept space with non-drug supplies without providing dose, form, or route structures. If diabetic supplies or dressings require standardized coding in a future phase, they will be ingested into an auxiliary medical supplies table preserving source `SAB='MTHCMSFRF'` and `TTY='PT'`.

---

### 9.2 CDCI Orphaned Brands Reconciliation

Physical cross-referencing between `BrandMaster.txt` (93,911 rows) and `GenericMaster.txt` (10,305 rows) identified exactly **14 brand records** whose `Generic Identifier` fails to resolve against `GenericMaster.txt`.

#### A. Complete Inventory of Orphaned CDCI Brands
| Brand ID | Brand Name | Unresolved Generic ID | Supplier ID & Verified Name | Product ID |
|---|---|---|---|---|
| `2766071000189109` | Zedonac MR 250 mg + 50 mg + 325 mg oral tablet | `2759711000189104` | `382991000189106` (Alkem Laboratories Ltd) | `2755971000189106` |
| `1840691000189108` | Taregyl 1 mg oral tablet | `783333008` | `20911000189106` (Novartis India Ltd) | `1837611000189104` |
| `2839151000189103` | AMPHOLIP 5 mg/ml solution for injection | `785130004` | `9351000189105` (Bharat Serums & Vaccines Ltd) | `974411000189100` |
| `1842181000189104` | TAVEGYL 1 mg oral tablet | `783333008` | `20911000189106` (Novartis India Ltd) | `1838021000189103` |
| `2782201000189106` | Minidolo 250 mg + 50 mg + 325 mg oral tablet | `2759711000189104` | `51531000189100` (Winsome Laboratories Ltd) | `2760021000189108` |
| `2900561000189100` | Nimkind-P 100 mg + 325 mg oral tablet | `2741201000189105` | `20341000189106` (Elkos Healthcare Pvt Ltd) | `2872411000189104` |
| `2910381000189101` | Twins 100 mg + 325 mg oral tablet | `2741201000189105` | `85451000189104` (Zaneka Healthcare Pvt Ltd) | `2870981000189103` |
| `1845061000189101` | Clamist 1 mg oral tablet | `783333008` | `15691000189109` (Wanbury Ltd) | `1839641000189109` |
| `997201000189104` | Covaxin vaccine | `""` (Empty string) | `70961000189109` (Bharat Biotech International Ltd) | `997181000189103` |
| `997231000189107` | Covishield vaccine | `""` (Empty string) | `83291000189107` (Serum Institute of India Pvt Ltd) | `997211000189102` |
| `997261000189100` | Sputnik V vaccine | `""` (Empty string) | `1007921000189108` (Dr. Reddy's Laboratories Ltd) | `997241000189101` |
| `997271000189109` | Sputnik V 0.5 ml/dose injection | `""` (Empty string) | `21851000189108` (Panacea Biotec Ltd) | `997241000189101` |
| `1709021000189102` | Tetanus Vaccine Adsorbed 0.5 ml/dose | `""` (Empty string) | `83291000189107` (Serum Institute of India Pvt Ltd) | `1709001000189108` |
| `1709071000189103` | MVI 10 ml/1 dose solution for injection | `""` (Empty string) | `24421000189101` (USV Pvt Ltd) | `1709051000189109` |

#### B. Root-Cause Analysis
1. **Vaccines and Biologics (6 records):** Covaxin, Covishield, Sputnik V, Tetanus Vaccine, and MVI were introduced into CDCI under emergency protocols as branded commercial products with authorized manufacturers (`Supplier Identifier`). However, because NRCeS models vaccines and multi-vitamin infusions outside standard chemical generic drug hierarchies, NRCeS left `Generic Identifier` completely blank.
2. **Unlisted / International SNOMED Identifiers (8 records):**
   * Generic ID `783333008` (Clemastine 1 mg oral tablet) and `785130004` (Amphotericin B liposomal injection) are valid International SNOMED CT drug concept codes that were not packaged into NRCeS's domestic `GenericMaster.txt`.
   * Generic IDs `2759711000189104` (Aceclofenac + Paracetamol + Chlorzoxazone) and `2741201000189105` (Nimesulide + Paracetamol) are Indian national extension concepts unlisted in this release cycle of `GenericMaster.txt`.

#### C. Ingestion and Safety Preservation Policy
* **Retention in Master Concepts:** All 14 brands are ingested into `concepts(system='CDCI', tty='BRAND')` to preserve their brand names as retrievable Indian trade names and retain their manufacturer relationships (`MANUFACTURED_BY` linking to `SupplierMaster`).
* **Exclusion from Automated Generic Substitution:** 
  * Because the target generic concept does not exist in `GenericMaster.txt`, **no `TRADENAME_OF` relationship edge is written to `concept_relationships`** for these 14 brands.
  * Their `attributes_json` is tagged with:
    `{"generic_status": "UNRESOLVED_GENERIC_PARENT", "raw_generic_identifier": "<raw_id>"}`.
* **Safety Outcome:** Prevents automated generic substitution or clinical equivalence inference while allowing valid prescription recognition of trade brands like Covaxin, Covishield, and Zedonac MR.

---

### 9.3 CLCI Discouraged Concepts Reconciliation

Cross-validation of the 1,473 codes in `common-lab-codes-for-india.csv` against official LOINC 2.83 identified exactly **2 concepts with `STATUS = 'DISCOURAGED'`**:

#### A. Complete Inventory of Discouraged CLCI Concepts
| LOINC Code | CLCI General Name | CLCI Fully-Specified Name (FSN) | LOINC 2.83 Official Status Text |
|---|---|---|---|
| **`16250-3`** | `Codeine, Urine` | `Codeine:MCnc:Pt:Urine:Qn:Confirm` | *"The Method values 'Screen' and 'Confirm' reflect a regulatory testing requirement rather than an actual laboratory method. This term has been discouraged in favor of terms specifying the true method (Immunoassay or Mass Spectrometry) or a methodless term where appropriate. Please select the replacement term that matches your actual testing method."* |
| **`19289-8`** | `Cannabinoids, Urine` | `Cannabinoids:PrThr:Pt:Urine:Ord:Confirm` | *"The Method values 'Screen' and 'Confirm' reflect a regulatory testing requirement rather than an actual laboratory method. This term has been discouraged in favor of terms specifying the true method (Immunoassay or Mass Spectrometry) or a methodless term where appropriate. Please select the replacement term that matches your actual testing method."* |

#### B. Preservation and Runtime Safeguards
* **Concept Storage:** Ingested into `concepts` with their exact source metadata: `system = 'LOINC'`, `code = '16250-3'` / `'19289-8'`, `class_type = 1`, and `status = 'DISCOURAGED'`.
* **Alias Storage:** Ingested into `concept_aliases` with `raw_display_text = 'Codeine, Urine'` and `'Cannabinoids, Urine'`, tagged with `alias_source = 'CLCI_GENERAL_NAME'`.
* **Runtime Retrieval Safeguard:**
  * Default retrieval queries enforce indexed SQL predicate `WHERE status = 'ACTIVE'`.
  * Consequently, default runtime retrieval **will never automatically select** `16250-3` or `19289-8`.
  * If a query explicitly targets discouraged concepts (e.g. historical audit mode), the candidate is returned with `status = 'DISCOURAGED'` and `rank_score` depressed, forcing clinical review.
* **No Speculative Re-Mapping:** No unverified replacement codes are invented or substituted.

---

### 9.4 CDCI License Provisions — Verbatim Text and Legal Boundaries

Inspection of `CommonDrugCodesForIndia_FlatFilePackage/License.txt` establishes the exact legal terms governing the dataset.

#### A. Verbatim Text of Key Provisions

1. **License Identity & Creative Commons Foundation:**
   > *"The Common Drug Codes for India (Flat Files Package) is produced by National Resource Centre for EHR Standards (NRCeS) at Centre for Development of Advanced Computing (C-DAC), Pune under the terms of the Creative Commons Attribution 4.0 International Public License, https://creativecommons.org/licenses/by/4.0/"*

2. **Geographic Conditions & Derivative Works:**
   > *"The Licensee can redistribute this package within India. The Licensee can create derivatives or implementation-related products and services using this package within India."*

3. **SNOMED CT International Acknowledgment:**
   > *"The Common Drug Codes for India (Flat Files Package) is a managed list of Common Drug Codes for India covering a list of unique identifiers and terms. The Flat Files Package contain some SNOMED CT codes (identifiers) and Terms from its International Release. The usage of SNOMED CT International Release content is governed by relevant clauses of SNOMED CT Affiliate License extended to the user of this package.*  
   > *SNOMED CT® was originally created by the College of American Pathologists. 'SNOMED' and 'SNOMED CT' are registered trademarks of International Health Terminology Standards Development Organisation, trading as SNOMED International."*

4. **Trademarks & Disclaimers:**
   > *"The Brand names and any trade names are copyright of their respective owners.*  
   > *C-DAC does not warrant the accuracy, completeness, or adequacy of content. The content provided is intended but not promised or guaranteed to be current, complete and up-to-date."*

#### B. Legal Interpretation vs. Actual Text
* **Actual Text:** The dataset is released under CC BY 4.0, but C-DAC's specific license terms explicitly restrict redistribution and the creation of derivative products/services to **"within India"**.
* **Interpretation for ClinicalDoc Deployment:**
  * ClinicalDoc deployments, instances, and terminology builds operating for Indian healthcare systems, clinics, hospitals, and ABDM integrations are fully authorized to ingest, create derivative SQLite databases, and redistribute CDCI data.
  * International cross-border deployments must treat CDCI as an India-territory clinical extension and avoid re-hosting or redistributing raw CDCI flat-file tables outside India without separate authorization from NRCeS / C-DAC.
* **Attribution Requirement:** Ingestion compiler metadata must record:
  1. Primary Attribution: National Resource Centre for EHR Standards (NRCeS) / C-DAC Pune under CC BY 4.0.
  2. SNOMED CT Trademark notice as mandated by License.txt.
  3. Proprietary brand names disclaimer.


Phase 3A.3 physical archive inspection is complete. Awaiting your review and explicit authorization for Phase 3B.

