# ClinicalDoc Documentation Map & Architecture Directory

This repository is strictly **document-driven**. Architectural intent, data contracts, and design resolutions govern all implementation code.

---

## 1. Specifications & Architecture Standards

| Directory / File | Description |
|---|---|
| [`docs/specifications/PROJECT_SPEC.md`](file:///d:/daa/docs/specifications/PROJECT_SPEC.md) | **Tier 1 Master Specification**: Defines platform objectives, input/output contracts, component responsibilities, schemas, metrics, and acceptance criteria. |
| [`docs/specifications/PRODUCTIZATION_SPEC.md`](file:///d:/daa/docs/specifications/PRODUCTIZATION_SPEC.md) | Enterprise productization specifications, security boundaries, and operational threat models. |
| [`docs/specifications/PRODUCTIZATION_DECISIONS.md`](file:///d:/daa/docs/specifications/PRODUCTIZATION_DECISIONS.md) | Productization decision log and architectural resolutions. |
| [`docs/specifications/PROJECT_SPEC_REQUIREMENTS_MATRIX.md`](file:///d:/daa/docs/specifications/PROJECT_SPEC_REQUIREMENTS_MATRIX.md) | Requirements traceability matrix linking clinical requirements to modules and tests. |
| [`docs/architecture/`](file:///d:/daa/docs/architecture/) | Foundational architecture resolutions, including `PHASE0_ARCHITECTURE_RESOLUTION.md`, `PHASE0_DECISION_RESOLUTION_PACKAGE.md`, `CLINICALDOC_CURRENT_STATE_RECONCILIATION.md`, and milestone reconciliation reports. |
| [`docs/decisions/`](file:///d:/daa/docs/decisions/) | Formal Architecture Decision Records (**DEC-001** through **DEC-019**) defining region classification, extraction thresholds, verification tiers, and FHIR provenance hierarchies. |

---

## 2. Security & Operational Runbooks

| Directory / File | Description |
|---|---|
| [`docs/security/archive/`](file:///d:/daa/docs/security/archive/) | P0 security remediation designs, audit trail specifications, and implementation execution plans. |
| [`deploy/runbooks/TESSERACT_OCR_PROVISIONING.md`](file:///d:/daa/deploy/runbooks/TESSERACT_OCR_PROVISIONING.md) | Operational runbook for deploying and verifying Tesseract 5.x OCR with Leptonica. |

---

## 3. Historical Reports & Milestone Archives

| Directory / Location | Description |
|---|---|
| [`docs/reports/archive/terminology_scale/`](file:///d:/daa/docs/reports/archive/terminology_scale/) | Feasibility, design, and acceptance records for scaling terminology lookups against LOINC and RxNorm SQLite databases (`M_TERMINOLOGY_SCALE_...`). |
| [`docs/reports/archive/CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md`](file:///d:/daa/docs/reports/archive/CLINICALDOC_POST_M_FHIR_PROJECT_ASSESSMENT.md) | Post-M-FHIR comprehensive project audit and safety assessment. |
| [`reports/historical_phases/archive/`](file:///d:/daa/reports/historical_phases/archive/) | Historical phase reports, grounding audits, sentence-clause segmentation audits, and Phase 5.2.3 milestone verification reports. |

---

## 4. Evaluation & Benchmarks

| Directory / Location | Description |
|---|---|
| [`external_eval/DATA_PROVENANCE.md`](file:///d:/daa/external_eval/DATA_PROVENANCE.md) | Data provenance and licensing descriptors for external clinical evaluation datasets (including the Glaucoma Medication Dataset). |
| [`external_eval/results_frozen_200.json`](file:///d:/daa/external_eval/results_frozen_200.json) | Frozen benchmark evaluation results across 200 clinical documents. |
| [`eval/benchmark_report.json`](file:///d:/daa/eval/benchmark_report.json) | Detailed performance and verification cascade metrics. |
