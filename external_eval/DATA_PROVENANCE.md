# External Evaluation Data Provenance & Upstream Licensing

## 1. Glaucoma Medication Dataset (`glaucoma_med_dataset`)

### Dataset Overview
- **Source**: Casey Eye Institute at Oregon Health & Science University (OHSU).
- **Domain**: Ophthalmic clinical encounter notes and medication prescriptions for patients with glaucoma.
- **Data Collection Period**: January 2019 – August 2020.
- **De-identification**: De-identified in accordance with HIPAA Safe Harbor regulations; protected health information (PHI) replaced with asterisks (`*`).
- **Entity Coverage**: Ophthalmic medication entities including medication name (`DRUG`), dosage (`DOSE`), route (`ROUTE`), and administration frequency (`FREQUENCY`).

### Upstream Citation
If utilizing this dataset or its derived subsets in academic or clinical evaluations, please cite the original data descriptor:

> **Citation**:
> *Translational Vision Science & Technology (TVST)*.
> "A De-Identified Dataset of Free-Text Clinical Notes and Annotations for Ophthalmic Medication Information Extraction."
> Casey Eye Institute, Oregon Health & Science University.

### Upstream License (BSD 3-Clause)
The `glaucoma_med_dataset` is redistributed under the terms of the BSD 3-Clause License:

```
BSD 3-Clause License

Copyright (c) 2022, jche253
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its
   contributors may be used to endorse or promote products derived from
   this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

## 2. Evaluation Subsets & Integrity

- **Frozen 200-Note Evaluation Subset** (`glaucoma_eval_subset_200.json`):
  A deterministic, reproducible sample of 200 de-identified notes drawn from the primary dataset for out-of-domain evaluation and numerical calibration auditing.
- **Evaluation Runners** (`eval_frozen_200.py`, `audit_numerical_integrity.py`):
  Benchmarked across 309 ophthalmic medication mentions to evaluate Stage A/B/C verification cascade generalizability on unseen clinical specialties without retraining.

