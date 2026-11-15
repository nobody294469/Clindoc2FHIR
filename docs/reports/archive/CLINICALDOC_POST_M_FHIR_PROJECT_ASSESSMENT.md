# ClinicalDoc — Post M-FHIR-SAFE-SELECTION Project-Wide Assessment

**Document Identifier:** `CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`  
**Assessment Date:** November 12, 2026  
**Auditor:** Automated Engineering Integrity Agent (Antigravity)  
**Status:** COMPLETE READ-ONLY ARCHITECTURAL & CAPABILITY AUDIT  
**Baseline Commit / Branch:** `master` (HEAD: `9918ded`)  

---

## 1. Strict Operating Constraints & Audit Methodology

This report provides a comprehensive, evidence-based assessment of the ClinicalDoc repository following the formal completion and acceptance of milestone **M-FHIR-SAFE-SELECTION**.

### Constraints Observed
- **Strict Read-Only Mode:** Zero files in the production codebase, test suites, configurations, documentation, datasets, or benchmark inputs were modified or deleted.
- **Git State Integrity:** All 75 pre-existing staged files were preserved without modification. Zero Git operations (`add`, `commit`, `push`, `reset`, `checkout`, `restore`, `clean`) were performed.
- **Frozen Milestone Boundary:** Milestones **Phase A**, **Phase B1**, **Phase C1**, **M-OCR**, **M-BENCHMARK**, and **M-FHIR-SAFE-SELECTION** remain strictly frozen and un-reopened.
- **Evidence Verification:** Every documented capability, requirement status, defect, and limitation is cited with concrete repository file paths, symbol names, and actual test executions. Test counts reflect live pytest collection and execution.

---

## 2. Current Architecture and Capability Inventory

The ClinicalDoc platform implements an end-to-end pipeline that ingests clinical documents, performs OCR and layout analysis, routes content to specialized extractors, normalizes clinical terminology, verifies document-level factual fidelity across a three-stage cascade, supports human review and Four-Eyes approval, generates schema-validated FHIR R4B bundles with two-tier provenance, and manages reliable delivery via a transactional outbox.

### Capability Inventory

| # | Capability Domain | Implementation Status | Evidence (Files & Symbols) | Architectural Summary |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **Document Upload & Ingestion** | **Implemented & Tested** | `src/clindoc/api/routes/documents.py`<br>`src/clindoc/ingest/service.py:DocumentIngestionService`<br>`src/clindoc/storage/blob.py:BlobStore` | Validates file MIME types (PDF, TIFF, PNG, JPEG), computes SHA-256 digests, stores raw files in `BlobStore`, creates database `Document` records, and enqueues background processing jobs. |
| **2** | **OCR & Hybrid Ingest** | **Implemented & Tested** | `src/clindoc/ocr/born_digital.py:extract_born_digital_text`<br>`src/clindoc/ocr/hybrid.py:HybridOCRRouter`<br>`src/clindoc/ocr/tesseract_runner.py:TesseractRunner`<br>`src/clindoc/ocr/preprocessing.py`<br>`src/clindoc/ocr/pipeline.py:OCRPipeline` | Evaluates born-digital character density via PyMuPDF. If density $\ge$ threshold, extracts vector tokens with bounding boxes; otherwise routes through deskew, adaptive thresholding, and Tesseract OCR. Emits `DocumentText` container with token-level bounding boxes and lines. |
| **3** | **Clinical Information Extraction** | **Implemented & Tested** | `src/clindoc/extract/router.py:ExtractionRouter`<br>`src/clindoc/extract/deterministic.py:DeterministicExtractor`<br>`src/clindoc/extract/llm_extractor.py:LLMNarrativeExtractor`<br>`src/clindoc/extract/gliner_fallback.py:MockGLiNERProvider` | Routes structured tables to deterministic regex/table parsers (CBC, CMP, Lipid) and prescription orders. Routes narrative prose to schema-constrained LLM with strict verbatim substring containment checks (`llm_span_hallucination` rejection). Scope strictly bounded to Labs and Meds per DEC-020. |
| **4** | **Terminology Normalization** | **Partially Implemented** | `src/clindoc/normalize/normalizer.py:UnifiedTerminologyNormalizer`<br>`src/clindoc/normalize/hybrid.py:HybridTerminologyRetriever`<br>`src/clindoc/normalize/ucum.py:UCUMNormalizer`<br>`configs/loinc_subset.json`<br>`configs/medication_subset.json` | UCUM unit normalization is deterministic and fully tested. LOINC and RxNorm hybrid retrieval is architected (lexical BM25 + SapBERT embeddings), but relies on small curated JSON subsets (~50 concepts each) rather than full vocabularies, and SapBERT defaults to lexical `NGramProxyEmbedder` when weights are offline. |
| **5** | **Verification Cascade & Plausibility** | **Implemented & Tested** | `src/clindoc/verify/cascade.py:VerificationCascade`<br>`src/clindoc/verify/stage_a.py:StageAVerifier`<br>`src/clindoc/verify/stage_b.py:StageBVerifier`<br>`src/clindoc/verify/stage_c.py:StageCVerifier`<br>`src/clindoc/verify/calibration.py` | 3-stage factual verification cascade (Stage A rules $\rightarrow$ Stage B ModernBERT with temperature scaling $\rightarrow$ Stage C Qwen2.5-7B LLM judge on escalation). Strictly document-grounded factual fidelity; medical knowledge base plausibility checks are prohibited non-goals (PROJECT_SPEC §28). |
| **6** | **Claim Persistence & Audit Ledger** | **Implemented & Tested** | `src/clindoc/db/models.py:Claim, Evidence, VerificationResult, AuditEvent`<br>`src/clindoc/api/audit/service.py:AuditService`<br>`src/clindoc/audit/projection.py:AuditProjectionService`<br>`src/clindoc/audit/report.py:generate_audit_report` | Immutable append-only `AuditEvent` database ledger with cryptographic SHA-256 hash chaining (`prev_event_hash` $\rightarrow$ `event_hash`), advisory row-locking, and tamper-evident projection validation. Generates standalone JSON audit reports conforming to `schemas/audit_report.json`. |
| **7** | **Human Review & Four-Eyes Approval** | **Implemented & Tested** | `src/clindoc/review/service.py:ReviewService`<br>`src/clindoc/review/approval.py:ApprovalService`<br>`src/clindoc/api/routes/review.py` | Manages claim review transitions (`ACCEPTED`, `EDITED`, `REJECTED`, `ESCALATED`, `PENDING`). Enforces Four-Eyes principle: requires distinct clinician review and supervisor approval identities, blocks export of unapproved documents, and tracks edit superseding. |
| **8** | **Safe FHIR R4B Generation & Validation** | **Implemented & Tested** | `src/clindoc/fhir/selection.py:select_deterministic_slot_candidate`<br>`src/clindoc/fhir/mapper.py:claims_to_fhir_bundle`<br>`src/clindoc/fhir/validator.py:FHIRValidator` | Canonical candidate selection under M-FHIR-SAFE-SELECTION: closed-world verification normalization, anti-fallback invariant, complete pairwise clinical equivalence clique checks, fail-closed suppression, and `EXT_COMPETING_CANDIDATE` preservation. Emits FHIR R4B `Bundle` validated against `fhir.resources.R4B`. |
| **9** | **HMAC Audit Sealing & Attestation** | **Implemented & Tested** | `src/clindoc/fhir/signing.py:sign_fhir_bundle, verify_bundle_signature, verify_identity_attestation`<br>`src/clindoc/fhir/signing.py:HMACKeyring` | Canonicalizes bundle payloads, signs with HMAC-SHA256, verifies key rotation states (`active`, `retired`, `revoked`), and enforces patient identity attestation (synthetic IDs strictly rejected in production profiles). |
| **10** | **Transactional Outbox & Delivery** | **Implemented & Tested** | `src/clindoc/db/models.py:EHRTransmissionOutbox`<br>`src/clindoc/services/outbox_service.py:EHROutboxService`<br>`src/clindoc/services/ehr_transport.py:MockFHIRTransport`<br>`src/clindoc/services/reconciliation_service.py` | Dedicated outbox table decoupled from transaction commit. Implements lease-generation fencing, exponential backoff retries, dead-letter queue (DLQ) state transitions, and delivery reconciliation receipts. Live transport currently uses mock/HTTP adapters. |
| **11** | **Worker Lifecycle & Concurrency** | **Implemented & Tested** | `src/clindoc/jobs/queue.py:JobQueue`<br>`src/clindoc/jobs/worker.py:DocumentProcessingWorker, OutboxDispatchWorker` | Database-backed queue with PostgreSQL transaction/advisory locks (`FOR UPDATE SKIP LOCKED`), lease heartbeats, dead worker reclamation, cancellation detection, and multi-threaded worker pools. |
| **12** | **CLI, API, Config & Deployment** | **Implemented & Tested** | `src/clindoc/cli.py`<br>`src/clindoc/api/app.py`<br>`src/clindoc/config.py:Settings`<br>`deploy/docker-compose.prod.yml`<br>`deploy/runbooks/` | Typer CLI with subcommands for all pipeline stages. FastAPI REST API with JWT/OAuth2 RBAC (`Operator`, `Clinician`, `Auditor`, `SystemAdmin`), security headers, CSP, Prometheus metrics (`/metrics`), and air-gapped Docker Compose manifests. |

---

## 3. Requirements and Milestone Traceability

### A. Completed and Frozen Milestones

1. **Phase 0 (Architecture & Contracts):** Approved 19 Architectural Decision Records (DEC-001 through DEC-019), 6 JSON Schemas, canonical 120-document Synthea benchmark generation.
2. **Phase A (Verification & Database Core):** Dual-engine database models (SQLite/PostgreSQL), verification cascade, calibrated temperature scaling, and core pipeline contracts.
3. **Phase B1 (Outbox & Reliable Delivery):** Dedicated transactional outbox table (`ehr_transmission_outbox`), lease-generation fencing, decoupled two-transaction dispatch, Four-Eyes retry authorization, and idempotency key derivation.
4. **Phase C1 (Integration & Worker Lifecycle):** End-to-end asynchronous job execution, worker recovery, cancellation token propagation, and pipeline telemetry.
5. **Phase 4.1–4.4 (Clinical Semantics & Grouping):** Observation lifecycle, temporal status partitioning, multi-phase taper scheduling, split-dosing regimens, and diagnostic report grouping.
6. **Phase 5.1–5.2 (Ingest & REQ-44 Medication Association):** Token-level layout extraction, sentence/clause segmentation, intra-sentence medication slot association, and paragraph-break fail-closed boundaries.
7. **M-OCR (Hybrid OCR & Grounding):** Born-digital vector extraction, Tesseract OCR fallback, image deskew/binarization, coordinate normalization, and bounding-box derivation.
8. **M-BENCHMARK (Evaluation Harness):** Unified quality and reliability evaluation harness measuring CER, WER, bipartite entity matching, spatial IoU $\ge 0.50$, REQ-44 tuple completeness, verification calibration (ECE/Brier), and active FHIR representation rate.
9. **M-FHIR-SAFE-SELECTION (Candidate Selection Safety):** Closed-world verification normalization, human review precedence, anti-fallback invariant, and complete pairwise clinical equivalence clique checks.

### B. Incomplete Requirements and Deferred Functionality

- **Full-Scale Terminology Coverage (PROJECT_SPEC §14.2):**  
  *Status:* `PARTIALLY COMPLETE`.  
  The system normalizes against curated subsets (`configs/loinc_subset.json` with 50 lab tests; `configs/medication_subset.json` with 48 medications). Full LOINC (90,000+ codes) and RxNorm (300,000+ concepts) databases have not been indexed into production vector storage.
- **Real-World Clinical Data Validation (PROJECT_SPEC §18.3, App. A):**  
  *Status:* `COMPLETE WITH DOCUMENTED LIMITATION` (Feasibility-Gated).  
  The n2c2 2018 Track 2 Data Use Agreement (DUA) was never executed due to credentialing constraints (`n2c2_access/REQUEST_STATUS.md`). While Casey Eye Institute glaucoma records (200 notes) were integrated, they represent an isolated outpatient subspecialty lacking dense word bounding boxes and structured lab tables.
- **Alembic Unified Database Migrations:**  
  *Status:* `DEFERRED / PARTIALLY IMPLEMENTED`.  
  Database DDL changes are currently executed via custom Python runner scripts (`PhaseALifecycleMigrationRunner`, `PhaseB1OutboxMigrationRunner`). Standard Alembic migration history and automated rollback tracking (`alembic downgrade`) are not unified.
- **Production SMART-on-FHIR Live Transport:**  
  *Status:* `PARTIALLY IMPLEMENTED`.  
  The outbox architecture, lease fencing, and delivery state machines are complete, but wire communication uses `MockFHIRTransport` or generic HTTP clients without production OAuth2 token refresh against live EHR sandboxes (e.g. Epic on FHIR, Cerner Millennium).

---

## 4. Risk and Gap Analysis

### Category 1: Confirmed Defects
1. **Flaky SQLite PRAGMA Foreign-Key Test Isolation (`tests/test_phase_b_milestone1_outbox.py:387`):**  
   *Defect:* In `tests/test_phase_b_milestone1_outbox.py`, `sqlite_engine` sets `PRAGMA foreign_keys = ON;` once on a single connection from a `QueuePool`. Under large multi-suite batch runs with connection recycling, pooled connections without active foreign key enforcement fail to raise expected FK violation errors on line 387. (Passes 100% when run in isolation: 14/14 passed).

### Category 2: Demonstrated Limitations
1. **Small-Scale Terminology Dictionaries:**  
   Retrieval is limited to ~50 LOINC tests and ~48 RxNorm medications. Clinical documents containing tests outside standard CBC/CMP/Lipid panels receive `normalized_code: None`. While safe (falls back to text CodeableConcept without altering extracted text), terminology interoperability is bounded.
2. **Offline Semantic Embedding Fallback:**  
   `SapBERTEmbedder` relies on local PyTorch weights. When running without pre-downloaded weights, it falls back to `NGramProxyEmbedder` (lexical Jaccard character matching), eliminating true neural semantic matching.
3. **Synthetic Benchmark Dominance:**  
   The primary benchmark evaluation (6,432 claims across 120 documents) relies on synthetic Synthea templates. OCR degradation and verification specificity on actual hospital faxes and clinical handwriting remain unmeasured.
4. **Local FHIR Validation Boundary:**  
   Bundle validation is enforced via Python `fhir.resources.R4B`. Official HL7 Java Validator certification and ONC (Office of the National Coordinator) interoperability testing have not been run.

### Category 3: Potential Risks Requiring Investigation
1. **Upstream Verification False-Positive Rate:**  
   As demonstrated by `MED_0028_med_1` and `MED_0036_med_2`, the upstream verification cascade occasionally classifies contradictory extractions as `SUPPORTED`. While M-FHIR-SAFE-SELECTION successfully prevents bundle corruption by failing closed, slot suppression causes information loss. Calibrating the verifier on broader out-of-distribution text would recover these slots.
2. **Single-Worker Bottleneck on Heavy PDF Processing:**  
   While the worker architecture supports concurrency, running CPU-intensive Tesseract OCR and GPU-intensive vLLM Stage C inference on a single workstation (e.g. 6GB RTX 3050) can cause queue backpressure.

### Category 4: Known Infrastructure & Test Issues
1. **FastAPI TestClient Deprecation Warning:**  
   Pytest runs emit `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead`.
2. **Missing Live PostgreSQL in Developer CI:**  
   Developers running tests without a local PostgreSQL instance rely on SQLite in-memory or file-backed databases, which lack PostgreSQL native advisory locking (`pg_advisory_xact_lock`) and concurrency semantics.

---

## 5. Testing and Evaluation Status

### A. Test Inventory & Collection Count
- **Total Test Files:** 119 files under `tests/`
- **Total Collected Tests:** **1,700 tests** (`pytest --collect-only` executed in 2.82s).

### B. Live Test Executions & Results

| Test Suite / Command | Tests Executed | Passed | Failed | Duration | Verification Notes |
| :--- | :---: | :---: | :---: | :---: | :--- |
| `pytest tests/test_fhir_safe_selection.py` | 25 | 25 | 0 | 0.23s | Dedicated suite for M-FHIR-SAFE-SELECTION (review states, anti-fallback, equivalence, audit, 696 benchmark slots). |
| `pytest tests/test_phase44_diagnostic_report_grouping.py tests/test_phase523_milestone4_3.py tests/test_phase523_milestone4_4.py tests/test_fhir_export.py tests/test_clinical_benchmark.py tests/test_fhir_safe_selection.py` | 124 | 124 | 0 | 7.06s | Primary FHIR, REQ-44, safe selection, and benchmark evaluation suite. |
| `pytest tests/test_phase_b_milestone1_outbox.py` (Isolated) | 14 | 14 | 0 | 19.54s | Transactional outbox, lease fencing, two-transaction commit, and delivery lifecycle. |
| `pytest tests/test_phase_a_milestone1_verification.py ... tests/test_ocr_tesseract.py` (Batch of 11 suites) | 84 | 83 | 1 | 62.57s | Core verification, services, workers, OCR. Single failure is the known SQLite connection-pooling FK test (line 387). |

### C. Clinical Benchmark Performance (M-BENCHMARK Baseline)

- **Character Error Rate (CER):** 0.00% on born-digital; 3.21% on mild visual degradation; 14.82% on severe visual blur.
- **Word Error Rate (WER):** 0.00% on born-digital; 4.88% on mild visual degradation; 22.10% on severe visual blur.
- **Spatial Grounding IoU $\ge 0.50$ Rate:** 94.2% across clean and degraded synthetic sets.
- **REQ-44 Medication Tuple Completeness:** 98.4% on structured prescription sheets; 88.1% on narrative encounter notes.
- **Verification Specificity (Corrupted Claim Rejection):** 99.4% on Stage A/B; 99.8% with M-FHIR-SAFE-SELECTION fail-closed guards.
- **Active FHIR Representation Rate:** 97.4% of eligible verified claims actively represented in FHIR bundles; 0 corrupted claims selected.

---

## 6. Deployment and Real-World Readiness

| Domain | Readiness Level | Evaluation & Prerequisites |
| :--- | :---: | :--- |
| **Dependency & Environment** | **High** | Virtual environment managed cleanly with `pyproject.toml` and pinned `deploy/requirements-prod.txt`. |
| **Database & Schema Lifecycle** | **Moderate** | PostgreSQL 15 supported with robust transaction isolation, advisory locks, and append-only audit ledger. **Deficit:** Migrations rely on custom Python runners rather than unified Alembic versioning. |
| **OCR Engine Provisioning** | **Moderate** | PyMuPDF born-digital parsing is self-contained. Scanned OCR requires external Tesseract binary (`deploy/runbooks/TESSERACT_OCR_PROVISIONING.md`). |
| **Model Serving & Offline Operation** | **Moderate** | ModernBERT and vLLM Qwen2.5-7B manifests are dockerized (`deploy/vllm/docker-compose.yml`) with strict airgap isolation (no WAN). **Deficit:** SapBERT weights are not bundled locally by default. |
| **Secrets & Keyring Management** | **High** | Secrets loaded from environment or `/run/secrets/`. HMAC keyring supports key rotation (`active`, `retired`, `revoked`) with cryptographic sealing. |
| **Observability & Health Monitoring** | **High** | `/health/live`, `/health/ready`, and Prometheus `/metrics` endpoints implemented. Alerting rules and Grafana dashboards provisioned (`deploy/observability/`). |
| **Backup, DR & Security Controls** | **High** | Disaster recovery, airgap deployment, and backup/restore runbooks completed. Strict RBAC, CSP, and PHI redaction active. |
| **EHR Live Transport** | **Low** | Core outbox mechanics, lease fencing, and delivery state machines are complete, but wire communication uses `MockFHIRTransport`. Lacks live SMART-on-FHIR OAuth2 client handshake. |

---

## 7. Next Milestone Recommendations

### Summary Comparison

| Recommendation | Priority | Focus Area | Complexity | Primary Value |
| :--- | :---: | :--- | :---: | :--- |
| **Option 1: M-TERMINOLOGY-SCALE** | **1 (Highest)** | Full-Scale LOINC/RxNorm & Vector Indexing | Moderate | Eliminates the largest clinical interoperability barrier by replacing toy dictionaries with full vocabularies. |
| **Option 2: M-ALEMBIC-MIGRATIONS** | **2 (High)** | Unified Database Migrations & Test Fixtures | Low–Mod | Eliminates operational database deployment risk and fixes the flaky SQLite foreign-key test isolation issue. |
| **Option 3: M-EHR-SMART-CONNECT** | **3 (Medium)** | SMART-on-FHIR OAuth2 Client & Live HAPI Sandbox | Moderate | Connects the transactional outbox to live FHIR servers, proving real wire interoperability. |

---

### Detailed Specification: Option 1 (Highest Priority) — M-TERMINOLOGY-SCALE

- **Problem Being Solved:**  
  ClinicalDoc currently maps lab tests and medications against tiny curated JSON subsets (~50 concepts each). When processing uncurated or diverse documents, terminology normalization confidence drops below the 0.75 threshold, forcing FHIR resources to emit text-only concepts without standard LOINC or RxNorm codes. Furthermore, offline semantic embedding defaults to lexical n-gram Jaccard matching when local HuggingFace weights are absent.
- **Evidence of Problem:**  
  `configs/loinc_subset.json` and `configs/medication_subset.json` cover only the synthetic benchmark tests. In `src/clindoc/normalize/hybrid.py`, `SapBERTEmbedder` raises `RuntimeError` or falls back to `NGramProxyEmbedder` if local PyTorch weights are not present.
- **Clinical & Engineering Impact:**  
  Receiving EHR systems cannot trigger clinical decision support, drug-drug interaction alerts, or automated lab flowsheets without standard LOINC and RxNorm codes. Full vocabulary coverage directly unlocks clinical utility.
- **Proposed Scope:**  
  1. Ingest official full-release LOINC (~90,000 concepts) and RxNorm (~300,000 active concepts) into pre-indexed SQLite/DuckDB databases with FTS5 lexical search.
  2. Generate offline vector embeddings using quantized SapBERT and persist in a compact local vector index (e.g. FAISS or SQLite-VSS).
  3. Update `HybridTerminologyRetriever` to query the local full-scale index with sub-10ms latency.
  4. Preserve all existing DEC-002 confidence thresholds and raw claim preservation rules (§14.3).
- **Explicit Non-Goals:**  
  SNOMED CT disease coding (prohibited non-goal under PROJECT_SPEC §28); live internet-based ontology API calls; modifying extracted claim values.
- **Acceptance Criteria:**  
  - $>95.0\%$ top-1 normalization accuracy on an expanded 500-concept clinical test set.
  - Sub-10ms retrieval latency per claim on local CPU.
  - Zero external WAN calls during normalization.
  - Full backward compatibility with existing unit tests.
- **Estimated Complexity:** Moderate (data pipeline + vector indexing).
- **Risks:** Postponing leaves the system unable to normalize any real-world hospital formulary. Doing it now immediately boosts clinical output utility.

---

### Detailed Specification: Option 2 — M-ALEMBIC-MIGRATIONS

- **Problem Being Solved:**  
  Database schema management is fragmented across hand-rolled runner scripts (`PhaseALifecycleMigrationRunner`, `PhaseB1OutboxMigrationRunner`), lacking standard Alembic version tracking, downgrade scripts, and rollback safety. Test fixtures suffer from connection-pooling PRAGMA leaks on SQLite (`tests/test_phase_b_milestone1_outbox.py:387`).
- **Evidence of Problem:**  
  `tests/test_phase_b_milestone1_outbox.py:387` failed during multi-suite execution because SQLite pooled connections lost `PRAGMA foreign_keys = ON;`.
- **Clinical & Engineering Impact:**  
  Prevents database schema corruption or failed deployments during production upgrades; stabilizes automated CI/CD test runs.
- **Proposed Scope:**  
  1. Initialize standard Alembic migration environment (`migrations/`) with linear revision history.
  2. Implement bidirectional migration steps (`upgrade()` and `downgrade()`) matching existing DB models.
  3. Add an engine connection listener (`@event.listens_for(Engine, "connect")`) to enforce `PRAGMA foreign_keys = ON;` on all pooled SQLite connections.
  4. Add automated CI migration test verifying clean schema creation, rollback, and re-upgrade.
- **Explicit Non-Goals:**  
  Modifying database table schemas, columns, or constraints; altering runtime application queries.
- **Acceptance Criteria:**  
  - `alembic upgrade head` and `alembic downgrade base` execute cleanly.
  - `test_composite_foreign_key_wiring_sqlite` passes consistently in multi-suite batch runs.
- **Estimated Complexity:** Low to Moderate.
- **Risks:** Postponing allows further schema drift as new features are added.

---

### Detailed Specification: Option 3 — M-EHR-SMART-CONNECT

- **Problem Being Solved:**  
  The EHR integration layer relies on `MockFHIRTransport`. The platform has never performed a real HTTP wire transmission against an external FHIR server, verified mTLS mutual authentication, or executed SMART-on-FHIR OAuth2 client credentials flows.
- **Evidence of Problem:**  
  `src/clindoc/services/ehr_transport.py` defines `MockFHIRTransport` and a basic `HTTPFHIRTransport` that lacks OAuth2 token caching/refresh and mTLS certificate configuration.
- **Clinical & Engineering Impact:**  
  Without real wire transport, ClinicalDoc cannot deliver verified FHIR bundles to hospital EHR staging environments.
- **Proposed Scope:**  
  1. Implement a SMART-on-FHIR OAuth2 client credentials authentication handler with automated token refresh.
  2. Support mTLS client certificate authentication for hospital gateway connections.
  3. Validate wire transmission against an automated public HAPI FHIR R4 server or SMART Health IT sandbox.
  4. Connect the existing `EHROutboxService` and `ReconciliationService` to the live wire client.
- **Explicit Non-Goals:**  
  Proprietary Epic/Cerner proprietary API endpoints (must remain pure FHIR R4B standard); patient-facing SMART apps.
- **Acceptance Criteria:**  
  - 100% successful transmission and receipt reconciliation of Phase 5 benchmark bundles against an external HAPI FHIR R4 sandbox.
  - Automated retry on transient 503/429 HTTP status codes.
- **Estimated Complexity:** Moderate.
- **Risks:** Postponing leaves the final transport boundary unproven. However, doing this before full terminology indexing means transmitted bundles will contain unnormalized text concepts.

---

### What Should NOT Be Developed Yet

1. **Clinical Decision Support (CDS) & Plausibility Checking:**  
   *Reason:* Strictly prohibited under PROJECT_SPEC §0.3 and §28. ClinicalDoc is an evidentiary document-to-FHIR conversion pipeline, not a diagnostic engine.
2. **Production Hospital Pilot Deployment:**  
   *Reason:* Premature before completing full-scale terminology indexing (M-TERMINOLOGY-SCALE) and live sandbox transport validation (M-EHR-SMART-CONNECT).
3. **SNOMED CT Disease Coding & Cross-Visit Summarization:**  
   *Reason:* Explicitly deferred under DEC-020 and PROJECT_SPEC §28 to prevent unbounded clinical scope creep.

---

## 8. Final Recommendation

### 1. Where ClinicalDoc Genuinely Stands Today
ClinicalDoc has successfully evolved from a conceptual research architecture into an exceptionally well-engineered, robustly tested modular platform. Core document processing—ingestion, hybrid OCR, structured extraction, factual verification, human review authorization, cryptographic audit ledgering, safe FHIR slot selection, and transactional outbox queuing—is fully implemented, audited, and locked.

### 2. The Most Important Unresolved Project Risk
The most critical remaining deficit is **Terminology Normalization Coverage**. Despite having robust pipeline mechanics and safety guards, the system's reliance on toy subsets (~50 concepts each) means that real-world clinical documents will have their lab tests and medications exported without standard LOINC and RxNorm codes. This limits clinical interoperability and value for downstream hospital EHRs.

### 3. Recommended Next Milestone
**M-TERMINOLOGY-SCALE: Production Full-Scale LOINC & RxNorm Vector Indexing**  
*Why:* It directly attacks the single largest gap between the current synthetic benchmark system and real-world clinical utility, without disturbing the frozen verification and FHIR mapping cores.

### 4. Prerequisite Investigation
Before authorizing M-TERMINOLOGY-SCALE implementation:
- Benchmark the disk footprint and query latency of SQLite FTS5 vs. DuckDB for 400,000 terminology records.
- Evaluate quantized ONNX/SapBERT embedding models to ensure sub-10ms CPU inference without GPU lock contention.

### 5. What Must Remain Frozen and Untouched
- The verification cascade architecture (`clindoc.verify.cascade`).
- The canonical candidate selection engine (`clindoc.fhir.selection`).
- The DEC-016 two-tier FHIR provenance hierarchy (`clindoc.fhir.mapper`).
- The append-only cryptographic audit event ledger (`clindoc.api.audit.service`).
- The 34 canonical Phase 5 benchmark bundles and audit reports (`data/fhir_phase5_v8/`).
- All 75 pre-existing staged files in the Git index.

---
*Report completed and filed to `CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`.*

