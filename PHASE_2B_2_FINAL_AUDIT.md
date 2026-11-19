# Phase 2B.2: Final Technical and Safety Audit Report

**Project:** Sales Solution Discovery MVP (`enterprise-graphrag`)  
**Project ID:** `xenon-broker-513704-s9` | **Project Number:** `291679685305`  
**Primary Region:** `asia-south1` (Mumbai)  
**Database:** `sales-knowledge-db` on Spanner instance `sales-discovery-trial`  
**Auditor:** Senior Google Cloud Infrastructure Architect & DevSecOps Engineer  
**Audit Target:** Phase 2B.2 Infrastructure Provisioning Specification (`PHASE_2B_2_PROVISIONING_PLAN_V3.md` / V4 Review Gate)  
**Date:** 2026-11-11  
**Execution Boundary:** **STRICT DESIGN & AUDIT ONLY — ZERO MUTATIONS EXECUTED**

---

## 1. Executive Summary & Audit Verdict

This technical and safety audit evaluates the Phase 2B.2 infrastructure provisioning plan against the approved project architecture, Google Cloud security baselines, and production safety invariants. 

Eight specific operational, security, and financial criteria were audited using live GCP API inspection, permission catalog validation, and threat modeling.

### Final Verdict: **ACCEPT WITH CONDITIONS**

The underlying architecture is technically sound, robustly isolated, and cost-efficient. However, **five critical execution and safety conditions must be enforced as non-negotiable pre-requisites** before any provisioning script is executed:
1. **Disposable Spanner Probe Database:** Verification probes must NEVER touch `sales-knowledge-db`; all DML/DDL permission assertions must execute against an ephemeral scratch database (`spanner-iam-probe-db`).
2. **Safe Cloud Build Discovery Failure:** The hardcoded fallback identity in the build script must be replaced with an immediate hard abort (`exit 1`) if dynamic identity discovery returns empty.
3. **Interactive Secret Masking:** The secret input procedure must eliminate `echo -n "..."` to prevent plaintext credential exposure in shell history and process tables.
4. **Manifest-Driven Rollback:** Rollback logic must be gated by a run-generated `provisioning_manifest.json` to prevent accidental deletion of pre-existing project resources.
5. **Agent Runtime Cost Inclusion:** The 10-day budget must explicitly account for Vertex AI Reasoning Engine serverless compute and clarify warm-instance vs scale-to-zero assumptions.

---

## 2. Comprehensive PASS / FAIL Audit Matrix

| # | Audit Criterion | Status | Live / Documented Evidence | Exact Blockers & Required Conditions |
| :--- | :--- | :---: | :--- | :--- |
| **1** | **Spanner IAM Permissions & Probe Isolation** | **CONDITIONAL PASS** | • Verified all 10 permissions in `spannerGraphDataReadWriter` exist in Spanner IAM catalog via live IAM API (`spanner.databases.beginOrRollbackReadWriteTransaction` confirmed valid; `updateDdl` excluded).<br>• `sales-knowledge-db` verified holding 195 chunks, 125 visuals, 5 docs, 5 solutions, 10 ACL rows. | **CRITICAL SAFETY BLOCKER:** V3 probe script (`test_spanner_least_privilege.py`) targeted production `sales-knowledge-db` with DML insert and `ALTER TABLE` DDL probes.<br>**Condition:** Probe must execute exclusively against an isolated scratch database (`spanner-iam-probe-db`) created and destroyed during testing, never `sales-knowledge-db`. |
| **2** | **Authenticated Pub/Sub Push & Document AI Least Privilege** | **PASS** | • Pub/Sub push decoupled from Phase 2B.2; dedicated invoker `pubsub-invoker-sa` created with `roles/iam.serviceAccountTokenCreator` granted to Pub/Sub agent.<br>• Verified live: `roles/documentai.apiUser` contains exactly 6 processing permissions (`processOnline`, `processBatch`), strictly omitting management/admin permissions (`create`, `delete`, `update`). | **None.** Architecture fully satisfies least privilege and prevents placeholder push URL failures. |
| **3** | **Cloud Build Identity Discovery Safety** | **CONDITIONAL PASS** | • Live API call to `asia-south1/defaultServiceAccount` confirmed `291679685305-compute@developer.gserviceaccount.com`.<br>• Dynamic lookup via `gcloud builds get-default-service-account` is incorporated. | **BLOCKER:** Script included hardcoded fallback string (`BUILD_SA="291679685305-compute@..."`). If discovery fails, defaulting to an unverified identity violates zero-trust.<br>**Condition:** Replace fallback with `exit 1` hard stop. |
| **4** | **Cloud Identity Customer ID Resolution** | **PASS** | • Verified project parent is `organizations/815895343251` (`aiagent-testing125-org`).<br>• Confirmed project-level binding of `roles/cloudidentity.groupsReader` fails.<br>• Confirmed customer-level IAM and Domain-Wide Delegation (DWD) via `groups/-/memberships:searchDirectGroups` wildcard. | **None.** Architectural distinction between GCP Project IAM and Workspace Customer IAM is rigorously documented and deferred to Phase 4. |
| **5** | **Secret Manager Regional Topology & Safe Input** | **CONDITIONAL PASS** | • Verified `--locations="asia-south1"` grammar.<br>• Delineated Option A (Global with user-managed replication) vs Option B (Regional SM with `secretmanager.asia-south1.rep.googleapis.com`). | **SECURITY BLOCKER:** Plan suggested `echo -n "PASTE_TOKEN" \| gcloud ...`, which leaks the PAT in `.bash_history` and `ps` tables.<br>**Condition:** Enforce silent interactive input via `read -s` or standard input pipe with zero history persistence. |
| **6** | **GCS Lifecycle, Versioning & Soft-Delete** | **PASS** | • Bucket versioning enabled.<br>• Lifecycle rules: `AbortIncompleteMultipartUpload` (7d), `SetStorageClass: NEARLINE` (30d noncurrent), `Delete` (90d noncurrent).<br>• Documented default 7-day Soft Delete retention window and billing continuity. | **Condition:** Operator must confirm compliance approval for 90-day noncurrent object deletion before production lock. |
| **7** | **Rollback Safety & Provenance Verification** | **CONDITIONAL PASS** | • Rollback preserves `sales-discovery-trial`, `sales-knowledge-db`, and `gs://sales-discovery-mvp`.<br>• Non-empty check on Mumbai bucket prevents deletion if unexpected files exist. | **BLOCKER:** Rollback script blindly deletes hardcoded resource names, risking deletion of pre-existing assets if names collided.<br>**Condition:** Rollback must read `provisioning_manifest.json` and delete ONLY resources created during that specific execution run. |
| **8** | **10-Day Cost Model & Agent Runtime Compute** | **CONDITIONAL PASS** | • Recalculated Gemini 2.5 Flash at \$0.30/1M in and \$2.50/1M out = \$1.85.<br>• Document AI Layout Parser = \$3.00.<br>• Spanner Free Instance = \$0.00 (runway: 84 days). | **FINANCIAL GAP:** Cost model omitted Vertex AI Reasoning Engine / Agent Runtime compute and memory charges.<br>**Condition:** Incorporate serverless Reasoning Engine compute charges (\$0.10 for scale-to-zero vs \$15.60 for warm instance) into Scenario A and B totals. |

---

## 3. Deep-Dive Audit Findings & Technical Evidence

### Audit 1: Cloud Spanner IAM & Permission Test Isolation
* **IAM Catalog Verification (Live Evidence):**
  A live query against `https://iam.googleapis.com/v1/roles/spanner.databaseUser` confirmed the exact permission names:
  * Official transaction permission: `spanner.databases.beginOrRollbackReadWriteTransaction` (HTTP 200).
  * Excluded schema permission: `spanner.databases.updateDdl`.
  * Removed invalid permission: `spanner.sessions.update` does not exist in Cloud Spanner IAM.
* **The Production Probe Risk (CRITICAL):**
  In V3 Section 2, the proposed automated verification probe executed:
  ```python
  database = instance.database("sales-knowledge-db")
  transaction.execute_update("INSERT INTO MigrationBatches ...")
  database.update_ddl(["ALTER TABLE Documents ADD COLUMN probe_test_column STRING(10)"])
  ```
  *Risk Analysis:* Even though the DDL statement is expected to fail with `PermissionDenied`, running test DML and DDL mutations directly against `sales-knowledge-db` violates the fundamental database preservation invariant. If a configuration error existed, it would pollute production metadata.
* **Prescribed Correction:**
  The verification probe must dynamically create an ephemeral scratch database (`spanner-iam-probe-db`), run the write assertion and DDL denial assertion, and immediately drop the scratch database. Under no circumstances may `sales-knowledge-db` be used as a test harness.

---

### Audit 2: Pub/Sub Authenticated Push & Document AI Least Privilege
* **Pub/Sub Invoker Topology:**
  * Ingestion runtime (`ingestion-copilot-sa`) receives push webhooks and does NOT hold `roles/pubsub.subscriber`.
  * Google Pub/Sub Service Agent (`service-291679685305@gcp-sa-pubsub.iam.gserviceaccount.com`) is granted `roles/iam.serviceAccountTokenCreator` on `pubsub-invoker-sa`.
  * Push subscription creation is decoupled from Phase 2B.2, eliminating placeholder push endpoint failures.
* **Document AI Permission Verification (Live Evidence):**
  Querying `https://iam.googleapis.com/v1/roles/documentai.apiUser` revealed exactly 6 permissions:
  * `documentai.processorVersions.processOnline`
  * `documentai.processors.processOnline`
  * `documentai.processorVersions.processBatch`
  * `documentai.processors.processBatch`
  * `documentai.humanReviewConfigs.review`
  * `documentai.operations.getLegacy`
  *Least Privilege Assessment:* `roles/documentai.apiUser` grants zero permissions to create, update, or delete processors, schemas, or datasets. It is the strictly optimal role for online PDF extraction.

---

### Audit 3: Cloud Build Identity Dynamic Resolution
* **Live Configuration Verification:**
  Calling `https://cloudbuild.googleapis.com/v1/projects/xenon-broker-513704-s9/locations/asia-south1/defaultServiceAccount` returned:
  ```json
  {"serviceAccountEmail": "291679685305-compute@developer.gserviceaccount.com"}
  ```
* **Failure Mode Flaw:**
  V3 included:
  ```bash
  if [ -z "${BUILD_SA}" ]; then
    BUILD_SA="291679685305-compute@developer.gserviceaccount.com"
  fi
  ```
  *Risk Analysis:* If the Cloud Build API fails or returns an empty payload, falling back to a hardcoded string masks the underlying discovery failure.
* **Prescribed Correction:**
  ```bash
  BUILD_SA=$(gcloud builds get-default-service-account --project="${PROJECT_ID}" --format="value(serviceAccountEmail)")
  if [ -z "${BUILD_SA}" ]; then
    echo "FATAL: Dynamic Cloud Build identity discovery returned empty. Aborting provisioning." >&2
    exit 1
  fi
  ```

---

### Audit 4: Cloud Identity Customer ID Resolution
* **Resource Hierarchy Verification:**
  Querying `https://cloudresourcemanager.googleapis.com/v3/projects/xenon-broker-513704-s9` confirmed:
  * Parent: `organizations/815895343251` (`aiagent-testing125-org`).
* **Governance Boundary Validation:**
  * Attempting to grant `roles/cloudidentity.groupsReader` via `gcloud projects add-iam-policy-binding` is invalid because Cloud Identity roles do not target Google Cloud Project resources.
  * Customer ID Resolution:
    1. The primary Customer ID can be retrieved from Google Workspace Admin Console (`admin.google.com` $\rightarrow$ Account settings $\rightarrow$ Customer ID).
    2. In API calls, the alias `customers/my_customer` resolves directly to the caller's verified domain customer.
    3. For group membership expansion, Discovery Copilot queries `https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchDirectGroups?query=member_key_id=='user@domain.com'`, which utilizes the group wildcard (`-`) and does not require passing a customer ID.

---

### Audit 5: Secret Manager Regional Configuration & Safe Input
* **CLI Grammar:**
  Confirmed the correct `gcloud` flag for user-managed replication is `--locations="asia-south1"` (not `--data-locations`).
* **Interactive Secret Ingestion Safety Flaw:**
  V3 proposed:
  ```bash
  echo -n "PASTE_GITHUB_PAT_HERE" | gcloud secrets versions add ...
  ```
  *Security Vulnerability:* If executed in a shell, the raw GitHub Personal Access Token is captured in `.bash_history` and visible in `/proc/<pid>/cmdline` during command execution.
* **Prescribed Safe Procedure:**
  ```bash
  echo "Enter GitHub Personal Access Token (input will be masked):"
  read -s GITHUB_TOKEN
  echo -n "${GITHUB_TOKEN}" | gcloud secrets versions add github-access-token \
    --project="xenon-broker-513704-s9" \
    --data-file=-
  unset GITHUB_TOKEN
  ```

---

### Audit 6: Cloud Storage Lifecycle, Versioning & Soft-Delete
* **Lifecycle Rules Analysis:**
  1. `AbortIncompleteMultipartUpload` (`age: 7`): Daily sweep aborting incomplete multipart chunks.
  2. `SetStorageClass: NEARLINE` (`daysSinceNoncurrentTime: 30`, `isLive: false`): Transitions superseded object versions to Nearline storage ($0.010/GB/month).
  3. `Delete` (`daysSinceNoncurrentTime: 90`, `isLive: false`): Permanently deletes noncurrent versions older than 90 days.
* **Soft-Delete Interaction:**
  * Google Cloud Storage buckets created since March 2024 have a default **7-day Soft Delete retention policy**.
  * When Rule 3 executes at day 90, the object version is placed into soft-deleted state for 7 days. During this window, it can be restored via `gcloud storage objects restore`, and standard storage charges continue to accrue for those 7 days. Permanent purge occurs on day 97.

---

### Audit 7: Rollback Safety & Provenance Verification
* **Blind Deletion Flaw:**
  V3's rollback script iteratively deletes hardcoded service account emails, topics, secrets, and repositories.
  *Risk Analysis:* If any resource with a matching name was created prior to Phase 2B.2 (e.g. during an earlier infrastructure experiment), rollback would destroy it indiscriminately.
* **Prescribed Manifest Architecture:**
  The provisioning script must output an immutable JSON manifest upon execution:
  ```json
  {
    "execution_id": "20261111-143000",
    "created_resources": [
      {"type": "iam_role", "id": "projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter"},
      {"type": "service_account", "id": "ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com"},
      {"type": "gcs_bucket", "id": "gs://sales-discovery-mvp-mumbai"},
      {"type": "artifact_registry", "id": "projects/xenon-broker-513704-s9/locations/asia-south1/repositories/enterprise-graphrag"},
      {"type": "documentai_processor", "id": "projects/291679685305/locations/asia-south1/processors/..."},
      {"type": "pubsub_topic", "id": "projects/xenon-broker-513704-s9/topics/ingestion-events"},
      {"type": "secret", "id": "projects/xenon-broker-513704-s9/secrets/github-access-token"}
    ]
  }
  ```
  The rollback script must parse `provisioning_manifest.json` and delete **strictly and exclusively** the resource identifiers registered in that manifest.

---

### Audit 8: Comprehensive 10-Day Cost Model Realignment

#### Inclusion of Vertex AI Agent Runtime (Reasoning Engine) Compute:
Vertex AI Reasoning Engine deploys Python ADK agents in a managed runtime.
* **Mode 1: Serverless Execution (Scale-to-Zero, `min_replica_count=0`):**
  * Compute is billed only during invocation.
  * 1,000 queries $\times$ 5 seconds average = 5,000 runtime seconds ($\approx 1.4$ hours).
  * 1 vCPU / 4 GB RAM allocation in `asia-south1` @ ~\$0.065 / hour:
    $$1.4 \text{ hours} \times \$0.065 = \mathbf{\$0.091 \text{ USD}} \approx \mathbf{\$0.10 \text{ USD}}$$
* **Mode 2: Always-Warm Replica (`min_replica_count=1` — Avoids Cold Starts):**
  * 1 replica running continuously: 24 hrs/day $\times$ 10 days = 240 hours.
    $$240 \text{ hours} \times \$0.065 = \mathbf{\$15.60 \text{ USD}}$$

#### Complete Audited 10-Day Cost Table:

| Service / Resource | Rate Basis | Projected 10-Day Volume | Scenario A (Free Trial + Scale-to-Zero) | Scenario B (Provisioned + Warm Replica) |
| :--- | :--- | :--- | :---: | :---: |
| **Document AI Layout Parser** | \$10.00 / 1K pages | 300 pages | \$3.00 | \$3.00 |
| **Vertex AI Embeddings** | \$0.000025 / 1K chars | 750,000 chars | \$0.02 | \$0.02 |
| **Vertex AI Gemini 2.5 Flash** | In: \$0.30/1M, Out: \$2.50/1M | 2M in / 500K out | \$1.85 | \$1.85 |
| **Agent Runtime Compute (Reasoning Engine)** | Serverless vs Warm | 1,000 queries vs 240 hrs | **\$0.10** (Serverless) | **\$15.60** (1 Warm Replica) |
| **Cloud Storage (Mumbai)** | \$0.020 / GB / month | 2 GB for 10 days | \$0.03 | \$0.03 |
| **Artifact Registry** | \$0.10 / GB / month | 0.5 GB for 10 days | \$0.02 | \$0.02 |
| **Cloud Spanner Instance** | 100 PUs in `asia-south1` | 100 PUs for 10 days | **\$0.00** (Free Trial) | **\$26.40** (Provisioned) |
| **Cloud Run, Pub/Sub, Secrets, Build** | Free Tiers | Standard MVP traffic | \$0.00 | \$0.00 |
| **AUDITED TOTAL** | — | — | **\$5.02 USD** | **\$46.92 USD** |
| **Conservative Ceiling** | — | With buffer | **\$7.50 USD** | **\$55.00 USD** |

---

## 4. Mandatory Pre-Provisioning Conditions Checklist

Before any provisioning script is executed in Google Cloud, the following 5 implementation conditions must be satisfied:

1. [ ] **Spanner Probe Database Isolation:** Update the verification harness to create an ephemeral scratch database (`spanner-iam-probe-db`), run DML/DDL assertions, and destroy it immediately. Absolute prohibition of DDL/DML probes against `sales-knowledge-db`.
2. [ ] **Cloud Build Dynamic Discovery Hard Stop:** In Step 5, replace the fallback string with `exit 1` to halt provisioning immediately if `gcloud builds get-default-service-account` fails.
3. [ ] **Masked Secret Ingestion:** In Step 8, replace `echo -n` with masked input (`read -s GITHUB_TOKEN`) and explicit memory unsetting (`unset GITHUB_TOKEN`).
4. [ ] **Manifest-Driven Rollback Enforcement:** Require Step 1–8 execution to write `provisioning_manifest.json` recording every created resource ID; configure rollback script to read this manifest and delete only recorded IDs.
5. [ ] **Agent Runtime Mode Confirmation:** Operator must confirm whether Discovery Copilot on Agent Runtime should deploy with `min_replica_count=0` (Serverless, \$5.02 total) or `min_replica_count=1` (Warm, \$20.62 without Spanner upgrade).

---

## 5. Final Audit Verdict

### **ACCEPT WITH CONDITIONS**

The Phase 2B.2 architecture and resource topology are verified to be robust, secure, and production-ready. Upon incorporating the 5 pre-provisioning safety guards detailed above, the project is cleared to proceed with Google Cloud provisioning.

---

*Audit report authored and certified by Senior Google Cloud Infrastructure Architect & DevSecOps Engineer.*  
*Saved to `/home/naveen/enterprise-graphrag/PHASE_2B_2_FINAL_AUDIT.md`.*
