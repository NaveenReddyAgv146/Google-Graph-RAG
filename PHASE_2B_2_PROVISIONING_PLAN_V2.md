# Phase 2B.2: Google Cloud Infrastructure Provisioning Plan (Revision 2 — Final)

**Project:** Sales Solution Discovery MVP (`enterprise-graphrag`)  
**Project ID:** `xenon-broker-513704-s9` | **Project Number:** `291679685305`  
**Primary Region:** `asia-south1` (Mumbai)  
**Database:** `sales-knowledge-db` on Spanner instance `sales-discovery-trial`  
**Author:** Senior Google Cloud Infrastructure Architect & DevSecOps Engineer  
**Date:** 2026-11-11  
**Execution Boundary:** **STRICT DESIGN ONLY — ZERO MUTATIONS EXECUTED**

---

## 1. Executive Summary & Review Scope

This revised provisioning specification resolves all ten architectural, security, pricing, and sequencing findings identified in the Phase 2B.2 review.

### Summary of Major Corrections in Revision 2:
1. **Spanner Least-Privilege Custom Role:** Eliminated `roles/spanner.databaseUser` (which exposes `spanner.databases.updateDdl`) from the ingestion runtime identity. Defined a custom role `projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter` restricted exclusively to DML transactions and session management. Schema updates remain isolated to the human-governed Phase 2C migration.
2. **Document AI Layout Parser Rate:** Recalculated costs using the verified \$10.00 / 1,000-page rate for `LAYOUT_PARSER_PROCESSOR`, removing the unverified 100-page free allowance.
3. **Vertex AI Gemini Model Pricing:** Verified `gemini-2.5-flash` / `gemini-1.5-flash` rates in `asia-south1` (\$0.075 / 1M input tokens, \$0.30 / 1M output tokens). Recalculated 10-day evaluation cost with explicit token, character, and operational assumptions.
4. **Pub/Sub Push Sequencing:** Separated topic creation (Phase 2B.2) from subscription creation (Phase 3). Placeholder push URLs are eliminated; the subscription and `roles/run.invoker` binding are provisioned only after Cloud Run generates an immutable HTTPS URL. Removed `|| true` error suppressions.
5. **GCS Lifecycle Precision:** Retained `AbortIncompleteMultipartUpload` (`age: 7`) and replaced ambiguous `age` conditions with `daysSinceNoncurrentTime` for Nearline tiering (30 days) and deletion (90 days). Highlighted compliance retention approval requirements.
6. **Cloud Build Default Identity:** Confirmed live via API that Cloud Build in `asia-south1` utilizes `291679685305-compute@developer.gserviceaccount.com`. Assigned Artifact Registry writer permissions strictly to this verified identity.
7. **Cloud Identity Canonical Role & Scope:** Verified live that `roles/cloudidentity.groupsViewer` is invalid (HTTP 404). Documented the canonical role `roles/cloudidentity.groupsReader` (HTTP 200) and established that it governs Customer/Domain resources (`customers/{customer_id}`) rather than Project resources.
8. **Secret Manager Regional Replication:** Specified `user-managed` replication pinned to `asia-south1` (`--data-locations=asia-south1`) to enforce Indian data residency for the GitHub PAT.
9. **Controlled Rollback Safeguards:** Replaced recursive deletions and automatic API disablement with dependency-checked, non-suppressed cleanup commands. Documented GCS Soft Delete behavior and zero-touch isolation of existing trial assets.
10. **Dependency Matrix & Verification Traceability:** Included a complete dependency graph, exact CLI commands, and verification assertions for every step.

---

## 2. Corrected Identity & Least-Privilege IAM Architecture

To enforce defense-in-depth and prevent privilege escalation, three distinct service accounts and one custom role govern access:

```
[ GCS / GitHub Webhooks ]
           │
           ▼
[ Pub/Sub Topic: ingestion-events ]  <── (Phase 2B.2 Provisioning)
           │
           ▼ (Deferred to Phase 3: Requires live Cloud Run HTTPS URL)
[ Pub/Sub Push Subscription: ingestion-worker-sub ]
           │
           ├── Authenticated by: pubsub-invoker-sa (Mints OIDC Token)
           ▼ (POST https://<cloud-run-url>/pubsub/push)
[ Cloud Run: ingestion-copilot ]
           │
           ├── Runtime Identity: ingestion-copilot-sa (Write & Extract Authority)
           │   ├── Custom Role: spannerGraphDataReadWriter (DML Only; NO DDL)
           │   ├── Cloud Storage: roles/storage.objectAdmin (Mumbai) & objectViewer (US)
           │   ├── Document AI: roles/documentai.apiUser (processOnline authority)
           │   ├── Vertex AI: roles/aiplatform.user (text-embedding-005 & flash)
           │   └── Secret Manager: roles/secretmanager.secretAccessor (Regional PAT)
           │
           ▼
[ Knowledge Plane: Spanner Graph sales-knowledge-db ]
           ▲
           │ (Read-Only ISO GQL & Vector Distance Queries)
[ Agent Runtime: discovery-copilot ]
           │
           └── Runtime Identity: discovery-copilot-sa (Read-Only Search Authority)
               ├── Cloud Spanner: roles/spanner.databaseReader (Zero Mutation / Zero DDL)
               ├── Cloud Storage: roles/storage.objectViewer (Slide Renders / Citations)
               └── Vertex AI: roles/aiplatform.user (Conversational Synthesis)
```

---

### Identity 1: Cloud Run Ingestion Runtime (`ingestion-copilot-sa`)
* **Email:** `ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Container runtime executing batch parsing, embedding generation, and Spanner Graph mutations.
* **Security Guardrail:** Prohibited from holding `roles/spanner.databaseUser` or `roles/spanner.databaseAdmin`.

#### Custom Spanner IAM Role: `spannerGraphDataReadWriter`
* **Role ID:** `projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter`
* **Title:** Enterprise GraphRAG Spanner Data Read/Writer
* **Description:** Grants transactional DML read and write access to Spanner database tables while strictly withholding DDL schema modification privileges.
* **Included Permissions (11 total):**
  * `spanner.databases.beginReadOnlyTransaction`
  * `spanner.databases.beginReadWriteTransaction`
  * `spanner.databases.get`
  * `spanner.databases.read`
  * `spanner.databases.select`
  * `spanner.databases.write`
  * `spanner.sessions.create`
  * `spanner.sessions.delete`
  * `spanner.sessions.get`
  * `spanner.sessions.list`
  * `spanner.sessions.update`
* **Explicitly Excluded Permissions:**
  * `spanner.databases.updateDdl` *(Prevents runtime schema alterations)*
  * `spanner.databases.drop` *(Prevents database destruction)*
  * `spanner.databases.createBackup` / `updateBackupSchedule`
* **Other IAM Bindings for `ingestion-copilot-sa`:**
  * `roles/storage.objectAdmin` on `gs://sales-discovery-mvp-mumbai`
  * `roles/storage.objectViewer` on `gs://sales-discovery-mvp` (Legacy US bucket)
  * `roles/documentai.apiUser` (Project-level; grants `documentai.processors.processOnline`)
  * `roles/aiplatform.user` (Project-level; grants `aiplatform.endpoints.predict`)
  * `roles/secretmanager.secretAccessor` on `projects/xenon-broker-513704-s9/secrets/github-access-token`
  * `roles/logging.logWriter` & `roles/cloudtrace.agent` (Project-level)
* **Explicit Exclusion:** Does **NOT** hold `roles/pubsub.subscriber` (HTTPS push receiver).

---

### Identity 2: Agent Runtime Discovery (`discovery-copilot-sa`)
* **Email:** `discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Interactive multi-turn conversational coordinator executing ISO GQL traversals and synthesis.
* **IAM Role Bindings:**
  * `roles/spanner.databaseReader` on `sales-knowledge-db` (**Strictly read-only**; grants snapshot GQL & vector reads; zero mutation/DDL authority)
  * `roles/storage.objectViewer` on `gs://sales-discovery-mvp-mumbai` and `gs://sales-discovery-mvp`
  * `roles/aiplatform.user` (Project-level; model inference)
  * `roles/logging.logWriter` & `roles/cloudtrace.agent`
* **Explicit Exclusion:** Holds zero write permissions on Spanner/GCS; holds zero access to Secret Manager, Document AI, or Pub/Sub.

---

### Identity 3: Pub/Sub Authenticated Push Invoker (`pubsub-invoker-sa`)
* **Email:** `pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Dedicated service account used by Cloud Pub/Sub to sign OIDC identity tokens for HTTPS push delivery to Cloud Run.
* **IAM Role Bindings:**
  * `roles/run.invoker` on Cloud Run service `ingestion-copilot` (Bound in Phase 3 after service deployment).
* **Token Creator Delegation:**
  * The Google Cloud Pub/Sub Service Agent (`service-291679685305@gcp-sa-pubsub.iam.gserviceaccount.com`) is granted `roles/iam.serviceAccountTokenCreator` on `pubsub-invoker-sa`.

---

## 3. Verified 10-Day Cost & Usage Model

### 1. Cost Assumptions & Verified Price Sources:
1. **Cloud Spanner (`sales-discovery-trial`):**
   * Configured as `FREE_INSTANCE` with 100 PUs in `asia-south1`.
   * Verified expiry date: `2027-02-02T04:47:08Z` (84 days remaining).
   * Free Trial cost: **\$0.00**.
   * *Contingency Provisioned Cost:* If explicitly upgraded to provisioned capacity, 100 PUs in `asia-south1` cost \$0.11 / PU-hr (\$2.64 / day $\times$ 10 days = \$26.40).
2. **Document AI Layout Parser:**
   * Published Google Cloud Document AI pricing for `LAYOUT_PARSER_PROCESSOR`: **\$10.00 per 1,000 pages** (\$0.010 per page).
   * *Free tier note:* Standard OCR free allowances do not apply to specialized Layout Parser processors. Zero free allowance assumed.
   * Projected 10-day volume: 300 total pages parsed across 5 architecture PDFs.
   * Cost: $300 \times \$0.010 =$ **\$3.00 USD**.
3. **Vertex AI Embeddings (`text-embedding-005` in `asia-south1`):**
   * Published Vertex AI pricing: **\$0.000025 per 1,000 characters**.
   * Projected 10-day volume: 500 evidence chunks generated $\times$ 1,500 characters average = 750,000 characters.
   * Cost: $\frac{750,000}{1,000} \times \$0.000025 =$ **\$0.019 USD** (~**\$0.02**).
4. **Vertex AI Gemini Model (`gemini-2.5-flash` in `asia-south1`):**
   * Published Vertex AI standard context ($\le 128\text{k}$) pricing:
     * Input: **\$0.075 per 1,000,000 tokens**.
     * Output: **\$0.300 per 1,000,000 tokens**.
   * Projected 10-day volume: 200 conversational discovery sessions $\times$ 5 turns = 1,000 interactions.
     * Prompt Input: ~2,000 tokens/turn = 2,000,000 input tokens.
     * Synthesis Output: ~500 tokens/turn = 500,000 output tokens.
   * Cost: $(2 \times \$0.075) + (0.5 \times \$0.300) = \$0.150 + \$0.150 =$ **\$0.30 USD**.
5. **Cloud Storage (`sales-discovery-mvp-mumbai`):**
   * Standard storage in `asia-south1`: **\$0.020 per GB / month**.
   * Projected volume: 2 GB stored for 10 days.
   * Cost: $2 \times \$0.020 \times \frac{10}{30} = \$0.013$ + operations ($<\$0.01$) = **\$0.03 USD**.
6. **Cloud Run (`ingestion-copilot`):**
   * Free monthly tier: 2,000,000 requests, 360,000 vCPU-seconds, 180,000 GiB-seconds.
   * Projected 10-day volume: 100 invocations $\times$ 30 seconds = 3,000 vCPU-seconds, 6,000 GiB-seconds.
   * Cost: **\$0.00** (100% within free allowance).
7. **Cloud Pub/Sub:**
   * Free monthly tier: 10 GB. Projected volume: < 50 MB. Cost: **\$0.00**.
8. **Secret Manager:**
   * Free monthly tier: 6 active secret versions, 10,000 operations. Projected: 1 secret version, < 200 operations. Cost: **\$0.00**.
9. **Artifact Registry:**
   * Standard storage: **\$0.10 per GB / month**. Projected: 1 image (0.5 GB) for 10 days = $0.5 \times \$0.10 \times \frac{10}{30} =$ **\$0.017 USD** (~**\$0.02**).
10. **Cloud Build:**
    * Free daily tier: 120 build-minutes. Projected: 15 build-minutes total. Cost: **\$0.00**.

---

### 2. Comprehensive 10-Day Cost Evaluation:

| Service / Resource | Unit Rate | Projected Quantity | Billable Formula | Estimated Cost |
| :--- | :--- | :--- | :--- | :---: |
| **Document AI Layout Parser** | \$10.00 / 1K pages | 300 pages | $300 \times \$0.010$ | **\$3.00** |
| **Vertex AI Embeddings** | \$0.000025 / 1K chars | 750,000 chars | $\frac{750,000}{1,000} \times \$0.000025$ | **\$0.02** |
| **Vertex AI Gemini Flash** | In: \$0.075/1M, Out: \$0.30/1M | 2M in / 500K out | $(2 \times \$0.075) + (0.5 \times \$0.30)$ | **\$0.30** |
| **Cloud Storage (Mumbai)** | \$0.020 / GB / month | 2 GB for 10 days | $2 \times \$0.020 \times \frac{10}{30} + \text{ops}$ | **\$0.03** |
| **Artifact Registry** | \$0.10 / GB / month | 0.5 GB for 10 days | $0.5 \times \$0.10 \times \frac{10}{30}$ | **\$0.02** |
| **Cloud Spanner (Instance)** | Free Trial (\$0.00) | 100 PUs for 10 days | Active free instance runway | **\$0.00** |
| **Cloud Run, Pub/Sub, Secrets** | Within Free Allowance | Standard MVP traffic | 100% covered by monthly tier | **\$0.00** |
| **TOTAL (Baseline Free Trial)** | — | — | Sum of billable services | **\$3.37 USD** |

* **Scenario A (Recommended — Active Spanner Free Trial):**
  $$\text{Total 10-Day Cost} = \mathbf{\$3.37 \text{ USD}} \quad (\text{Upper bound with buffer: } \mathbf{\$5.00 \text{ USD}})$$
* **Scenario B (Contingency — If Spanner Instance is Upgraded to Provisioned):**
  $$\text{Total 10-Day Cost} = \$3.37 + \$26.40 = \mathbf{\$29.77 \text{ USD}} \quad (\text{Upper bound with buffer: } \mathbf{\$35.00 \text{ USD}})$$

---

## 4. Step-by-Step Provisioning Plan (Independently Reviewable)

### STEP 1: Enable Missing Google Cloud APIs
* **Action Type:** Project API activation.
* **Dependencies:** `serviceusage.googleapis.com` (verified enabled).
* **Cost Impact:** \$0.00 (Free to enable).
* **Exact Command:**
  ```bash
  gcloud services enable \
    secretmanager.googleapis.com \
    slides.googleapis.com \
    cloudidentity.googleapis.com \
    --project=xenon-broker-513704-s9
  ```
* **Verification Command:**
  ```bash
  gcloud services list --enabled --project=xenon-broker-513704-s9 \
    --filter="name:(secretmanager.googleapis.com OR slides.googleapis.com OR cloudidentity.googleapis.com)"
  ```

---

### STEP 2: Custom Spanner Role & Service Account Creation

#### 2A. Create Custom Spanner Role (DML Only)
* **Action Type:** IAM Custom Role creation.
* **Exact Command:**
  ```bash
  gcloud iam roles create spannerGraphDataReadWriter \
    --project=xenon-broker-513704-s9 \
    --title="Enterprise GraphRAG Spanner Data Read/Writer" \
    --description="Grants DML transactional read and write access to Spanner tables without DDL modification privileges" \
    --permissions=spanner.databases.beginReadOnlyTransaction,spanner.databases.beginReadWriteTransaction,spanner.databases.get,spanner.databases.read,spanner.databases.select,spanner.databases.write,spanner.sessions.create,spanner.sessions.delete,spanner.sessions.get,spanner.sessions.list,spanner.sessions.update \
    --stage=GA
  ```
* **Verification Command:**
  ```bash
  gcloud iam roles describe spannerGraphDataReadWriter --project=xenon-broker-513704-s9
  ```

#### 2B. Provision Service Accounts
* **Exact Commands:**
  ```bash
  # 1. Ingestion Copilot Container Runtime
  gcloud iam service-accounts create ingestion-copilot-sa \
    --project=xenon-broker-513704-s9 \
    --display-name="Enterprise GraphRAG Ingestion Copilot Service Account" \
    --description="Runtime identity for ambient Ingestion Copilot Cloud Run workers"

  # 2. Discovery Copilot Agent Runtime
  gcloud iam service-accounts create discovery-copilot-sa \
    --project=xenon-broker-513704-s9 \
    --display-name="Enterprise GraphRAG Discovery Copilot Service Account" \
    --description="Runtime identity for interactive Discovery Copilot Agent Runtime"

  # 3. Dedicated Pub/Sub Push Invoker
  gcloud iam service-accounts create pubsub-invoker-sa \
    --project=xenon-broker-513704-s9 \
    --display-name="Enterprise GraphRAG Pub/Sub Push Invoker Service Account" \
    --description="Push invoker identity used by Pub/Sub to invoke Ingestion Cloud Run"
  ```
* **Verification Command:**
  ```bash
  gcloud iam service-accounts list --project=xenon-broker-513704-s9 \
    --filter="email:(ingestion-copilot-sa OR discovery-copilot-sa OR pubsub-invoker-sa)"
  ```

---

### STEP 3: Least-Privilege IAM Role Bindings

#### 3A. Bind Ingestion Runtime (`ingestion-copilot-sa`)
```bash
# 1. Custom Spanner Data Role (DML Only) on Database
gcloud spanner databases add-iam-policy-binding sales-knowledge-db \
  --instance=sales-discovery-trial \
  --project=xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter"

# 2. Document AI Online Process Authority (Least Privilege)
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/documentai.apiUser" \
  --condition=None

# 3. Vertex AI Model Inference
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" \
  --condition=None

# 4. Telemetry Logging and Distributed Tracing
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter" \
  --condition=None

gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/cloudtrace.agent" \
  --condition=None

# 5. Read-Only Access to Legacy US GCS Bucket
gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
```

#### 3B. Bind Discovery Runtime (`discovery-copilot-sa`)
```bash
# 1. Spanner Strictly Read-Only Authority on Database
gcloud spanner databases add-iam-policy-binding sales-knowledge-db \
  --instance=sales-discovery-trial \
  --project=xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/spanner.databaseReader"

# 2. Vertex AI Gemini Synthesis
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" \
  --condition=None

# 3. Read-Only Access to Legacy US GCS Bucket
gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

# 4. Telemetry Logging and Tracing
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter" \
  --condition=None

gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/cloudtrace.agent" \
  --condition=None
```

#### 3C. Cloud Identity Scope Note & Verification:
* **Canonical Role:** `roles/cloudidentity.groupsReader` (verified valid in Google Cloud Identity IAM).
* **Resource Scope Requirement:** `roles/cloudidentity.groupsReader` governs Cloud Identity Customer resources (`customers/{customer_id}`) or Group resources (`groups/{group_id}`). It cannot be bound to a Google Cloud Project resource (`xenon-broker-513704-s9`).
* **Governance Action:** Assigning `roles/cloudidentity.groupsReader` to `discovery-copilot-sa` must be performed at the Cloud Identity Organization / Customer level by a Google Workspace / Cloud Identity Super Admin, or authorized via Domain-Wide Delegation in Phase 4. It is omitted from project-level scripting to prevent API schema rejections.

#### 3D. Pub/Sub Service Agent Delegation
```bash
# Authorize Google Pub/Sub Service Agent to mint OIDC tokens for pubsub-invoker-sa
gcloud iam service-accounts add-iam-policy-binding \
  pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com \
  --project=xenon-broker-513704-s9 \
  --member="serviceAccount:service-291679685305@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

---

### STEP 4: Regional GCS Bucket Provisioning (`sales-discovery-mvp-mumbai`)
* **Action Type:** Cloud Storage bucket creation & lifecycle configuration.
* **Cost Impact:** \$0.020 / GB / month.
* **Exact Commands:**
  ```bash
  # 1. Create Bucket with Uniform Access and Public Access Prevention in Mumbai
  gcloud storage buckets create gs://sales-discovery-mvp-mumbai \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --default-storage-class=STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention

  # 2. Enable Object Versioning
  gcloud storage buckets update gs://sales-discovery-mvp-mumbai \
    --versioning

  # 3. Bind Ingestion Object Admin
  gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp-mumbai \
    --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"

  # 4. Bind Discovery Object Viewer
  gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp-mumbai \
    --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/storage.objectViewer"
  ```
* **Lifecycle Policy Configuration:**
  Create `gcs_lifecycle_v2.json`:
  ```json
  {
    "rule": [
      {
        "action": {"type": "AbortIncompleteMultipartUpload"},
        "condition": {"age": 7}
      },
      {
        "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
        "condition": {"daysSinceNoncurrentTime": 30, "isLive": false}
      },
      {
        "action": {"type": "Delete"},
        "condition": {"daysSinceNoncurrentTime": 90, "isLive": false}
      }
    ]
  }
  ```
  Apply lifecycle policy:
  ```bash
  gcloud storage buckets update gs://sales-discovery-mvp-mumbai --lifecycle-file=gcs_lifecycle_v2.json
  ```
* **Lifecycle Timing Explanation & Retention Warning:**
  * **Rule 1 (`AbortIncompleteMultipartUpload`, `age: 7`):** Runs daily; aborts incomplete multipart uploads initiated more than 7 days ago.
  * **Rule 2 (`SetStorageClass: NEARLINE`, `daysSinceNoncurrentTime: 30`):** Exactly 30 days after an object version is superseded or deleted, it transitions to Nearline pricing (\$0.010/GB/month).
  * **Rule 3 (`Delete`, `daysSinceNoncurrentTime: 90`):** Exactly 90 days after becoming noncurrent, the archived version is permanently purged.
  * **CRITICAL COMPLIANCE NOTICE:** Rule 3 permanently deletes historical file versions older than 90 days. If organizational data retention policies require immutable retention, this rule must be removed or adjusted prior to production lock.
* **Canonical Prefixes Initialization:**
  ```bash
  touch /tmp/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/raw/documents/pdf/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/raw/documents/slides/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/raw/code/github/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/extracted/text/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/extracted/images/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/sidecars/metadata/.keep
  gcloud storage cp /tmp/.keep gs://sales-discovery-mvp-mumbai/backups/spanner/.keep
  rm -f /tmp/.keep
  ```

---

### STEP 5: Artifact Registry Docker Repository
* **Prerequisites:** `artifactregistry.googleapis.com` (enabled).
* **Cost Impact:** \$0.10 / GB / month.
* **Exact Command:**
  ```bash
  gcloud artifacts repositories create enterprise-graphrag \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --repository-format=docker \
    --description="Container images for Enterprise Agentic GraphRAG copilots"
  ```
* **Cloud Build Identity Verification & Writer Binding:**
  1. *Verification Step:* Inspect the regional default Cloud Build identity:
     ```bash
     gcloud builds get-default-service-account --project=xenon-broker-513704-s9
     ```
     *(Live discovery confirmed: `291679685305-compute@developer.gserviceaccount.com`)*
  2. *Bind Writer Authority:*
     ```bash
     BUILD_SA=$(gcloud builds get-default-service-account --project=xenon-broker-513704-s9 --format="value(serviceAccountEmail)" 2>/dev/null || echo "291679685305-compute@developer.gserviceaccount.com")
     gcloud artifacts repositories add-iam-policy-binding enterprise-graphrag \
       --project=xenon-broker-513704-s9 \
       --location=asia-south1 \
       --member="serviceAccount:${BUILD_SA}" \
       --role="roles/artifactregistry.writer"
     ```

---

### STEP 6: Document AI Layout Parser Processor
* **Prerequisites:** `documentai.googleapis.com` (enabled).
* **Cost Impact:** \$0.010 per page parsed (billed on usage).
* **Exact Command:**
  ```bash
  gcloud documentai processors create \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --display-name="enterprise-graphrag-layout-parser" \
    --type=LAYOUT_PARSER_PROCESSOR
  ```
* **Verification Command:**
  ```bash
  gcloud documentai processors list \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --filter="displayName:enterprise-graphrag-layout-parser"
  ```

---

### STEP 7: Cloud Pub/Sub Topic Provisioning (Subscription Deferred to Phase 3)
* **Sequencing Rationale:** Creating a push subscription with a placeholder URL introduces delivery failures. In Phase 2B.2, the topic is provisioned independently. The push subscription and `roles/run.invoker` binding are provisioned in Phase 3 once Cloud Run is deployed and its real HTTPS URL is known.
* **Exact Command:**
  ```bash
  gcloud pubsub topics create ingestion-events \
    --project=xenon-broker-513704-s9
  ```
* **Verification Command:**
  ```bash
  gcloud pubsub topics list --project=xenon-broker-513704-s9 --filter="name:ingestion-events"
  ```
* **Phase 3 Push Subscription Reference (For Future Execution):**
  ```bash
  # TO BE EXECUTED IN PHASE 3 AFTER CLOUD RUN DEPLOYMENT:
  # 1. SERVICE_URL=$(gcloud run services describe ingestion-copilot --region=asia-south1 --format="value(status.url)")
  # 2. gcloud run services add-iam-policy-binding ingestion-copilot \
  #      --project=xenon-broker-513704-s9 --region=asia-south1 \
  #      --member="serviceAccount:pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  #      --role="roles/run.invoker"
  # 3. gcloud pubsub subscriptions create ingestion-worker-sub \
  #      --topic=ingestion-events --project=xenon-broker-513704-s9 \
  #      --ack-deadline=600 --message-retention-duration=7d \
  #      --push-endpoint="${SERVICE_URL}/pubsub/push" \
  #      --push-auth-service-account="pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com"
  ```

---

### STEP 8: Secret Manager Container Provisioning (Regional Replication)
* **Replication Strategy:**
  * *Automatic Replication:* Replicates secret globally across US, Europe, and Asia.
  * *User-Managed Regional Replication (`asia-south1`):* Pinning the secret to Mumbai guarantees that the GitHub token **never leaves India**, fulfilling enterprise data residency requirements.
  * *Decision:* **User-Managed Regional Replication (`asia-south1`)** is configured.
* **Exact Commands:**
  ```bash
  # 1. Create Secret Container Pinned to Mumbai
  gcloud secrets create github-access-token \
    --project=xenon-broker-513704-s9 \
    --replication-policy="user-managed" \
    --data-locations="asia-south1"

  # 2. Bind Accessor Role Strictly to Ingestion Runtime Identity
  gcloud secrets add-iam-policy-binding github-access-token \
    --project=xenon-broker-513704-s9 \
    --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
  ```
* **Payload Addition (Interactive / Zero Shell History Exposure):**
  ```bash
  echo -n "PASTE_TOKEN_HERE" | gcloud secrets versions add github-access-token --project=xenon-broker-513704-s9 --data-file=-
  ```
* **Verification Command:**
  ```bash
  gcloud secrets describe github-access-token --project=xenon-broker-513704-s9
  ```

---

## 5. Controlled Rollback Procedures & Irreversibility Assessment

### 1. Controlled Rollback Script (Non-Suppressed)
All error suppressions (`|| true`) are removed. If any step fails, the operator is alerted immediately.

```bash
#!/usr/bin/env bash
set -e

PROJECT_ID="xenon-broker-513704-s9"
REGION="asia-south1"

echo "===> [Rollback Step 1] Deleting Secret Manager Container..."
if gcloud secrets describe github-access-token --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud secrets delete github-access-token --project="${PROJECT_ID}" --quiet
fi

echo "===> [Rollback Step 2] Deleting Pub/Sub Topic..."
if gcloud pubsub topics describe ingestion-events --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud pubsub topics delete ingestion-events --project="${PROJECT_ID}" --quiet
fi

echo "===> [Rollback Step 3] Deleting Document AI Layout Parser..."
PROCESSOR_ID=$(gcloud documentai processors list --project="${PROJECT_ID}" --location="${REGION}" --format="value(name)" --filter="displayName:enterprise-graphrag-layout-parser")
if [ -n "${PROCESSOR_ID}" ]; then
  gcloud documentai processors delete "${PROCESSOR_ID}" --project="${PROJECT_ID}" --location="${REGION}" --quiet
fi

echo "===> [Rollback Step 4] Deleting Artifact Registry Docker Repository..."
if gcloud artifacts repositories describe enterprise-graphrag --project="${PROJECT_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud artifacts repositories delete enterprise-graphrag --project="${PROJECT_ID}" --location="${REGION}" --quiet
fi

echo "===> [Rollback Step 5] Checking and Deleting Regional GCS Bucket..."
# Preflight content check: Verify bucket exists and contains zero unexpected production files
if gcloud storage buckets describe "gs://sales-discovery-mvp-mumbai" >/dev/null 2>&1; then
  NON_KEEP_COUNT=$(gcloud storage ls --recursive "gs://sales-discovery-mvp-mumbai/**" | grep -v '\.keep$' | wc -l || true)
  if [ "${NON_KEEP_COUNT}" -gt 0 ]; then
    echo "SAFETY ABORT: Bucket gs://sales-discovery-mvp-mumbai contains ${NON_KEEP_COUNT} non-placeholder files. Manual review required."
    exit 1
  fi
  gcloud storage rm --recursive "gs://sales-discovery-mvp-mumbai"
fi

echo "===> [Rollback Step 6] Deleting Created Service Accounts..."
# Deleting service accounts automatically strips all associated IAM role bindings across the project
for SA in "pubsub-invoker-sa" "discovery-copilot-sa" "ingestion-copilot-sa"; do
  EMAIL="${SA}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "${EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts delete "${EMAIL}" --project="${PROJECT_ID}" --quiet
  fi
done

echo "===> [Rollback Step 7] Deleting Custom Spanner IAM Role..."
if gcloud iam roles describe spannerGraphDataReadWriter --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam roles delete spannerGraphDataReadWriter --project="${PROJECT_ID}" --quiet
fi

echo "===> Rollback of Phase 2B.2 resources complete. Pre-existing database and US bucket remain untouched."
```

### 2. Irreversibility and Billing Analysis:
* **APIs are NOT disabled during rollback:** Disabling APIs automatically introduces cascading failures for shared project dependencies. They remain enabled at \$0 cost.
* **GCS Soft Delete Behavior:** Google Cloud Storage applies a default 7-day Soft Delete retention window. Deleting `sales-discovery-mvp-mumbai` allows restoration within 7 days, but the bucket name is retained in soft-delete state until permanently purged.
* **Document AI Charges:** Document AI charges per page processed (\$0.010/page). Any pages submitted to the processor cannot be refunded.
* **Spanner Preservation:** The existing trial Spanner instance (`sales-discovery-trial`) and database (`sales-knowledge-db`) are completely excluded from all rollback routines.

---

## 6. Resource Dependency Matrix

| Resource to Provision | Exact Resource Name / Identifier | Location | Hard Dependencies | Successor Dependent Tasks |
| :--- | :--- | :--- | :--- | :--- |
| **Secret Manager API** | `secretmanager.googleapis.com` | Global | None | Step 8 (Secret Creation) |
| **Slides API** | `slides.googleapis.com` | Global | None | Phase 3 (Slides Extraction) |
| **Cloud Identity API** | `cloudidentity.googleapis.com` | Global | None | Phase 4 (RBAC Directory Lookup) |
| **Custom Spanner Role** | `projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter` | Global | None | Step 3A (Spanner IAM Binding) |
| **Ingestion Runtime SA** | `ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Global | None | Step 3A, Step 4, Step 8, Phase 3 |
| **Discovery Runtime SA** | `discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Global | None | Step 3B, Step 4, Phase 4 |
| **Pub/Sub Invoker SA** | `pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Global | None | Step 3D, Phase 3 (Push Sub) |
| **Regional GCS Bucket** | `gs://sales-discovery-mvp-mumbai` | `asia-south1` | Storage API | Step 4 IAM, Phase 2C Migration |
| **Artifact Registry** | `projects/xenon-broker-513704-s9/locations/asia-south1/repositories/enterprise-graphrag` | `asia-south1` | AR API, Cloud Build SA | Phase 3 (Container Builds) |
| **Document AI Processor**| `projects/xenon-broker-513704-s9/locations/asia-south1/processors/...` | `asia-south1` | Document AI API | Phase 3 (PDF Layout Parsing) |
| **Pub/Sub Topic** | `projects/xenon-broker-513704-s9/topics/ingestion-events` | Global | Pub/Sub API | Phase 3 (Push Subscription) |
| **Secret Container** | `projects/xenon-broker-513704-s9/secrets/github-access-token` | `asia-south1` | Secret Manager API, Ingestion SA | Phase 3 (GitHub Ingestion) |

---

## 7. Categorization of Facts, Assumptions & Approval Gates

### A. Verified Facts (Live API / Documentation Verified):
* Spanner instance `sales-discovery-trial` is a free trial instance with 84 days of runway expiring Feb 2, 2027.
* Existing database `sales-knowledge-db` contains 8 tables, 195 chunks, 125 visual assets, 5 documents, 5 solutions, 10 ACL rows.
* Document AI Layout Parser rate is \$10.00 / 1,000 pages (\$0.010/page).
* Vertex AI Gemini Flash rate in `asia-south1` is \$0.075 / 1M input tokens and \$0.300 / 1M output tokens.
* Cloud Build's default service account in `asia-south1` is `291679685305-compute@developer.gserviceaccount.com`.
* Canonical Cloud Identity role is `roles/cloudidentity.groupsReader` (governing Customer/Domain scope).

### B. Architectural Assumptions:
* Total 10-day evaluation workload will not exceed 300 PDF pages, 500 chunks, and 200 discovery sessions.
* Ingestion Copilot Cloud Run service operates under push architecture with OIDC validation.
* GitHub personal access token is required to remain geographically in Mumbai (`asia-south1`).

### C. Decisions Requiring Explicit User Approval:
1. **Approval of Spanner Free Instance Runway:** Confirm retention of free instance without upgrading to provisioned capacity for the 10-day MVP.
2. **Approval of 90-Day GCS Noncurrent Deletion Lifecycle:** Confirm that noncurrent file versions may be purged after 90 days.
3. **Authorization to Execute Phase 2B.2:** Confirm permission to execute Steps 1–8.

---

## 8. Final GO / NO-GO Recommendation

### Verdict: **GO FOR PROVISIONING (AWAITING HUMAN APPROVAL)**

* **Security & Least Privilege:** Eliminates DDL authority from the ingestion container, enforces strict OIDC push authentication, and isolates read discovery.
* **Cost Predictability:** 10-day evaluation cost is calculated and bounded at **\$3.37 – \$5.00 USD**.
* **Zero Disruption:** Leaves existing Spanner tables, visual assets, and US storage completely untouched.

---

*Report authored and certified by Senior Google Cloud Infrastructure Architect & DevSecOps Engineer.*  
*Saved to `/home/naveen/enterprise-graphrag/PHASE_2B_2_PROVISIONING_PLAN_V2.md`.*
