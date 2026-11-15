# ClinicalDoc: Evidence-Grounded Clinical Document Conversion & Verification Architecture

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3119/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com/)
[![FHIR R4B](https://img.shields.io/badge/FHIR-R4B%207.1.0-orange.svg)](https://hl7.org/fhir/R4B/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-336791.svg)](https://www.postgresql.org/)

ClinicalDoc is an evidence-grounded clinical document-to-EHR conversion and verification platform. It ingests complex, heterogeneous clinical documents (consultation notes, lab panels, discharge summaries, prescription pads), extracts clinical entities linked to verbatim text spans and bounding boxes, validates factual grounding across an audited three-tier verification cascade, enforces **Four-Eyes clinician review** (Mode 0), and emits standards-compliant **HL7 FHIR R4B** resources backed by append-only cryptographic audit ledgers.

---

## 🔬 Audited Empirical Benchmarks

ClinicalDoc's extraction, normalization, and verification pipelines have been evaluated across synthetic counterfactual benchmarks and a real-world clinical cohort:

| Evaluation Dataset | Scope & Mentions | Extraction & Attribute Precision | Verification & Export Outcome |
| :--- | :--- | :--- | :--- |
| **Out-of-Domain Glaucoma Evaluation** | 309 ophthalmic medication mentions across 200 de-identified clinical notes | **74.73%** medication-name precision<br>**48.64%** micro attribute precision | **0/5** complete medication tuple exact matches on real-world cohort |
| **Phase 5 Counterfactual Generalization Benchmark** | 1,826 claims across 34 holdout summaries (1,130 clean, 696 corrupted counterfactual claims) | **94.54%** upstream cascade rejection (658/696 corrupted claims rejected) | **0** corrupted claims exported after downstream deterministic slot selection/pruning |

*Dataset Provenance: The real-world ophthalmic evaluation is derived from the de-identified Glaucoma Medication Dataset (Casey Eye Institute, Oregon Health & Science University; TVST Data Descriptor under BSD 3-Clause). See [Dataset provenance](external_eval/DATA_PROVENANCE.md).*

> [!NOTE]
> **Historical Dataset Context**: The historical 5,629-example dataset split (comprising 5,629 serialized examples across 120 synthetic clinical documents; see `reports/M2_V3_FULL_REGENERATION_REPORT.md`) reflects a historical Phase 4/5 calibration and development dataset split rather than a completed Milestone M-2 evaluation.

> [!IMPORTANT]
> **Research Prototype & Evaluation Boundaries**
> - **Research Prototype**: ClinicalDoc is a research prototype, not a clinically validated or certified production software medical device.
> - **Evaluation Boundaries**: Evaluated on structured synthetic/semi-synthetic templates and a 200-note de-identified ophthalmic clinical text cohort. Uncurated, messy handwritten clinical documents have not been processed or benchmarked.
> - **Fail-Closed EHR Transport Barrier**: Real HTTP live dispatch to external EHR endpoints fails closed (`HTTP 501 Not Implemented`) pending production transport integration; only controlled in-memory transport is active.

---

## 🏗️ System Architecture

ClinicalDoc is organized as a modular, containerized monolith structured around four safety boundaries:

```
[ Clinical Input Document (PDF / Image / Text) ]
                       │
                       ▼
   ┌───────────────────────────────────────────────┐
   │ 1. INGESTION & DOCUMENT SCREENING             │
   │    - MIME sniffing, PDF structural probe      │
   │    - Deduplication (SHA-256 content address)  │
   │    - Encrypted blob store (AES-256-GCM)       │
   └──────────────────────┬────────────────────────┘
                          │
                          ▼
   ┌───────────────────────────────────────────────┐
   │ 2. EXTRACTION & THREE-TIER CASCADE            │
   │    - ExtractionRouter (Qwen 2.5 7B grammar)   │
   │    - TerminologyNormalizer (RxNorm, LOINC)    │
   │    - Stage A: High-precision NLI screening    │
   │    - Stage B: ModernBERT temperature scaling  │
   │    - Stage C: Strict grammar-bounded judge    │
   └──────────────────────┬────────────────────────┘
                          │
                          ▼
   ┌───────────────────────────────────────────────┐
   │ 3. HUMAN REVIEW WORKBENCH (MODE 0)            │
   │    - Mandatory Four-Eyes principle (uploader   │
   │      cannot approve own document)             │
   │    - Explicit patient identity confirmation   │
   │    - Terminal claim transitions (ACCEPT/EDIT) │
   └──────────────────────┬────────────────────────┘
                          │
                          ▼
   ┌───────────────────────────────────────────────┐
   │ 4. ATTESTATION & EHR TRANSMISSION BOUNDARY    │
   │    - Document-scoped PostgreSQL advisory lock │
   │    - RFC 8785 canonical event projection      │
   │    - Append-only HMAC-SHA256 audit ledger     │
   │    - Fail-closed EHR transport barrier        │
   │    - Canonical HL7 FHIR R4B Bundle export     │
   │    - Phase B1 Transactional Outbox (Accepted) │
   │      (14-test suite: SQLite & PostgreSQL 18)  │
   └───────────────────────────────────────────────┘
```

---

## ⚡ Quick Start (Local Development)

### Prerequisites
- Python 3.11
- Virtual environment (`venv`)

### 1. Installation
```bash
git clone https://github.com/nobody294469/DAA.git
cd DAA

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell:
.venv\Scripts\Activate.ps1

pip install -e ".[dev,ocr,ml]"
```

### 2. Run Local Verification Suite
```bash
pytest tests/
```

### 3. Launch Development API Server
```bash
# Sets CLINDOC_PROFILE=dev to enable local testing fixtures
python -c "import os; os.environ['CLINDOC_PROFILE'] = 'dev'; from clindoc.api.app import create_app; import uvicorn; uvicorn.run(create_app(), host='127.0.0.1', port=8000)"
```
Interactive OpenAPI documentation will be accessible at: `http://127.0.0.1:8000/api/v1/docs`.

---

## 🛡️ Production Deployment & Profile Containment

ClinicalDoc enforces strict execution profiles via the `CLINDOC_PROFILE` environment variable:

- **`CLINDOC_PROFILE=dev`**: Permitted for local development, offline evaluations, unit testing, and sandbox experimentation. Mocks and synthetic fixtures are allowed.
- **`CLINDOC_PROFILE=production`**: Enforces strict containment. All mock providers and heuristic fallbacks are strictly prohibited. The system fails closed on startup if any production invariant is violated.

### Mandatory Production Invariants
1. `CLINDOC_ENV=production`
2. `CLINDOC_ENFORCE_FOUR_EYES=true`
3. `CLINDOC_ENFORCE_AUDIT=true`
4. `CLINDOC_AUDIT_HMAC_KEY`: Non-placeholder raw UTF-8 key ($\ge 32$ bytes). Rotated keys are resolved via `CLINDOC_AUDIT_HMAC_KEY_<ID>`.
5. `MODERNBERT_CHECKPOINT_PATH`: Valid qualified ModernBERT checkpoint file.
6. `VLLM_BASE_URL`: Valid `http://` or `https://` endpoint URL with non-empty model identifier.
7. `EHR Transport Barrier`: Real HTTP live dispatch in `PRODUCTION` profile fails closed with `HTTP 501 Not Implemented` pending Phase 9 migration; only controlled in-memory `MockFHIRTransport` is allowed.

See [`.env.example`](.env.example) and [`deploy/.env.example`](deploy/.env.example) for template configuration.

---

## 📚 Documentation Index

- **Specifications**:
  - [`PROJECT_SPEC.md`](docs/specifications/PROJECT_SPEC.md): Core platform requirements, evidence grounding schemas, and FHIR resource mappings.
  - [`PRODUCTIZATION_SPEC.md`](docs/specifications/PRODUCTIZATION_SPEC.md): Enterprise productization specifications, security boundaries, and threat models.
  - [`PRODUCTIZATION_DECISIONS.md`](docs/specifications/PRODUCTIZATION_DECISIONS.md): Architectural decision records (PD-001 through PD-019).
  - [`PROJECT_SPEC_REQUIREMENTS_MATRIX.md`](docs/specifications/PROJECT_SPEC_REQUIREMENTS_MATRIX.md): Traceability matrix of clinical requirements against implementation modules.
- **Architecture**:
  - [`PHASE0_ARCHITECTURE_RESOLUTION.md`](docs/architecture/PHASE0_ARCHITECTURE_RESOLUTION.md): Modular monolith subsystem boundaries and data contracts.
  - [`PHASE0_DECISION_RESOLUTION_PACKAGE.md`](docs/architecture/PHASE0_DECISION_RESOLUTION_PACKAGE.md): Architectural decision package.
  - [`CLINICALDOC_CURRENT_STATE_RECONCILIATION.md`](docs/architecture/CLINICALDOC_CURRENT_STATE_RECONCILIATION.md): Database schema and state machine reconciliation.
- **Security & Audits**:
  - [`docs/security/archive/`](docs/security/archive/): Historical security remediation designs and implementation plans.
  - [`reports/historical_phases/archive/`](reports/historical_phases/archive/): Historical milestone reports, adversarial testing audits, and grounding validation records.
