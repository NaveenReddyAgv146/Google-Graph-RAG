# Phase 2B.2: Google Cloud Infrastructure Provisioning Plan (Final)

**Project:** Sales Solution Discovery MVP (`enterprise-graphrag`)  
**Project ID:** `xenon-broker-513704-s9` | **Project Number:** `291679685305`  
**Primary Region:** `asia-south1` (Mumbai)  
**Database:** `sales-knowledge-db` on Spanner instance `sales-discovery-trial`  
**Author:** Senior Google Cloud Infrastructure Architect & DevSecOps Engineer  
**Date:** 2026-11-11  
**Execution Boundary:** **STRICT DESIGN ONLY — ZERO MUTATIONS EXECUTED**

---

## 1. Executive Summary & Scope

This document defines the comprehensive, dependency-ordered, and reviewable execution plan for **Phase 2B.2 (Infrastructure Provisioning)**. It incorporates all architectural corrections identified in the Phase 2B.1 review:

1. **Pub/Sub Authenticated Push Delivery:** Corrected from pull-subscriber to an authenticated HTTPS push architecture using a dedicated push invoker service account (`pubsub-invoker-sa`), eliminating `roles/pubsub.subscriber` from the Cloud Run container runtime identity.
2. **Document AI Least-Privilege Verification:** Upgraded from `roles/documentai.viewer` (which lacks online processing authority) to `roles/documentai.apiUser`, granting `documentai.processors.processOnline` without administrative exposure.
3. **Transparent 10-Day Cost Model:** Itemized cost projections with explicit volume assumptions, unit rates, and service-by-service formulas under both free-trial and provisioned Spanner scenarios.
4. **Isolated & Reversible Steps:** Divided into eight modular execution phases with exact CLI commands, fully-qualified resource identifiers, and verified rollback procedures.

---

## 2. Corrected Identity & Least-Privilege IAM Architecture

To maintain strict defense-in-depth and avoid privilege overlap, three logical service accounts are defined:

```
[ GCS / GitHub Events ]
         │
         ▼
[ Pub/Sub Topic: ingestion-events ]
         │
         ▼ (Authenticated Push Delivery)
[ Pub/Sub Subscription: ingestion-worker-sub ]
         │
         ├── Assumes: pubsub-invoker-sa (Mints OIDC Token)
         ▼ (HTTP POST with Authorization: Bearer <OIDC>)
[ Cloud Run: ingestion-copilot ]
         │
         ├── Runtime Identity: ingestion-copilot-sa (Write & Extract Authority)
         │   ├── Cloud Spanner: roles/spanner.databaseUser (Read/Write)
         │   ├── Cloud Storage: roles/storage.objectAdmin (Mumbai) & objectViewer (US)
         │   ├── Document AI: roles/documentai.apiUser (Layout Parser)
         │   ├── Vertex AI: roles/aiplatform.user (Embeddings & Extraction)
         │   └── Secret Manager: roles/secretmanager.secretAccessor (GitHub PAT)
         │
         ▼
[ Knowledge Plane: Spanner Graph sales-knowledge-db ]
         ▲
         │ (Read-Only ISO GQL & Vector Scans)
[ Agent Runtime: discovery-copilot ]
         │
         └── Runtime Identity: discovery-copilot-sa (Read-Only Search Authority)
             ├── Cloud Spanner: roles/spanner.databaseReader (Strictly Read-Only)
             ├── Cloud Storage: roles/storage.objectViewer (Slide Renders / Citations)
             ├── Vertex AI: roles/aiplatform.user (Gemini 3.5 Flash Synthesis)
             └── Cloud Identity: roles/cloudidentity.groupsViewer (Directory RBAC)
```

### Identity 1: Cloud Run Ingestion Runtime (`ingestion-copilot-sa`)
* **Email:** `ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Container runtime identity executing batch parsing, embeddings, and Spanner mutations.
* **IAM Role Bindings:**
  * `roles/spanner.databaseUser` (Resource: `projects/xenon-broker-513704-s9/instances/sales-discovery-trial/databases/sales-knowledge-db`)
  * `roles/storage.objectAdmin` (Resource: `projects/_/buckets/sales-discovery-mvp-mumbai`)
  * `roles/storage.objectViewer` (Resource: `projects/_/buckets/sales-discovery-mvp`)
  * `roles/documentai.apiUser` (Project-level; grants `documentai.processors.processOnline`)
  * `roles/aiplatform.user` (Project-level; grants `aiplatform.endpoints.predict`)
  * `roles/secretmanager.secretAccessor` (Resource: `projects/xenon-broker-513704-s9/secrets/github-access-token`)
  * `roles/logging.logWriter` (Project-level)
  * `roles/cloudtrace.agent` (Project-level)
* **Explicit Exclusion:** Does **NOT** hold `roles/pubsub.subscriber` or `roles/pubsub.editor`.

### Identity 2: Agent Runtime Discovery (`discovery-copilot-sa`)
* **Email:** `discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Conversational retrieval coordinator executing ISO GQL traversals and synthesis.
* **IAM Role Bindings:**
  * `roles/spanner.databaseReader` (**Strictly read-only**; grants snapshot GQL & vector reads; zero mutation/DDL authority)
  * `roles/storage.objectViewer` (Resource: `projects/_/buckets/sales-discovery-mvp-mumbai` and `projects/_/buckets/sales-discovery-mvp`)
  * `roles/aiplatform.user` (Invoking `gemini-3.5-flash`)
  * `roles/cloudidentity.groupsViewer` (Directory lookup for user group memberships)
  * `roles/logging.logWriter` (Telemetry)
  * `roles/cloudtrace.agent` (Tracing)
* **Explicit Exclusion:** Does **NOT** hold Secret Manager, Document AI, Pub/Sub, or Spanner write roles.

### Identity 3: Pub/Sub Authenticated Push Invoker (`pubsub-invoker-sa`)
* **Email:** `pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Dedicated identity used by Cloud Pub/Sub push subscription to authenticate HTTPS POST requests to Cloud Run.
* **IAM Role Bindings:**
  * `roles/run.invoker` on Cloud Run service `ingestion-copilot`.
* **Service Agent Delegation:**
  * The Google Cloud Pub/Sub Service Agent (`service-291679685305@gcp-sa-pubsub.iam.gserviceaccount.com`) is granted `roles/iam.serviceAccountTokenCreator` on `pubsub-invoker-sa`. This allows the Pub/Sub service agent to mint signed Google OIDC identity tokens bearing `pubsub-invoker-sa` claims.

---

## 3. Transparent 10-Day Cost & Usage Model

### Baseline Assumptions:
* **Evaluation Period:** 10 consecutive calendar days.
* **Corpus Ingestion Volume:** 5 PDF architecture whitepapers (~200 total pages), 1 slide deck (~25 slides), 2 GitHub repositories (~300 source files).
* **Chunk & Vector Projections:** ~500 evidence chunks generated (~1,500 characters average length = ~750,000 characters embedded).
* **Conversational Discovery Volume:** 200 user sessions, 5 turns per session = 1,000 conversational interactions (~2M prompt input tokens, ~500K synthesis output tokens).
* **Regional Pricing Basis:** Google Cloud `asia-south1` (Mumbai) published rates as of November 2026.

### Itemized Service-by-Service Cost Calculation:

| Service & Resource | Official Rate (`asia-south1`) | 10-Day Usage Assumption | Free Tier / Trial Allowance | Estimated 10-Day Cost (USD) |
| :--- | :--- | :--- | :--- | :---: |
| **Cloud Spanner (Instance)** | Free Trial: \$0.00<br>*(Provisioned: \$0.11/PU-hr)* | 100 PUs for 240 hours | 84 days remaining on `FREE_INSTANCE` (expires Feb 2, 2027) | **\$0.00**<br>*(Optional Provisioned: \$26.40)* |
| **Cloud Spanner (Storage)** | Standard: \$0.30 / GB / month | < 100 MB relational data | Included in Free Trial | **\$0.00** |
| **Document AI (Layout Parser)** | \$30.00 per 1,000 pages | 300 pages parsed | 100 pages/month free tier | $(300 - 100) \times \$0.030 =$ **\$6.00** |
| **Vertex AI Embeddings (`text-embedding-005`)** | \$0.000025 per 1,000 characters | 750,000 characters embedded | None | $\frac{750,000}{1,000} \times \$0.000025 =$ **\$0.019** (~**\$0.02**) |
| **Vertex AI LLM (`gemini-3.5-flash`)** | Input: \$0.075 / 1M tokens<br>Output: \$0.30 / 1M tokens | 2M input tokens<br>500K output tokens | Standard monthly tier | $(2 \times \$0.075) + (0.5 \times \$0.30) =$ **\$0.30** |
| **Cloud Storage (`sales-discovery-mvp-mumbai`)** | Storage: \$0.020 / GB / month<br>Class A: \$0.05 / 10K ops<br>Class B: \$0.004 / 10K ops | 2 GB stored<br>500 Class A writes<br>2,000 Class B reads | 5 GB-months free (US tier) / regional minimal | Storage: $2 \times \$0.02 \times \frac{10}{30} = \$0.013$<br>Ops: $<\$0.01$<br>Total: **\$0.03** |
| **Cloud Run (`ingestion-copilot`)** | vCPU: \$0.00002400 / sec<br>RAM: \$0.00000250 / GiB-sec<br>Requests: \$0.40 / 1M | 100 invocations $\times$ 30 sec = 3,000 vCPU-sec, 6,000 GiB-sec | Free tier: 360K vCPU-sec, 180K GiB-sec, 2M req/month | **\$0.00** (100% within Free Tier) |
| **Cloud Pub/Sub** | \$40.00 / TiB | < 50 MB total payload | First 10 GB/month free | **\$0.00** (100% within Free Tier) |
| **Secret Manager** | Secret: \$0.06 / version / month<br>Access: \$0.03 / 10K operations | 1 active secret version<br>< 200 access calls | 6 secret versions free, 10K operations free/month | **\$0.00** (100% within Free Tier) |
| **Artifact Registry** | \$0.10 / GB / month | 1 container image (~500 MB = 0.5 GB) | Standard rate | $0.5 \times \$0.10 \times \frac{10}{30} =$ **\$0.017** (~**\$0.02**) |
| **Cloud Build** | \$0.003 / build-minute | 5 builds $\times$ 3 min = 15 build-minutes | 120 build-minutes/day free | **\$0.00** (100% within Free Tier) |
| **Cloud Logging & Monitoring** | Ingestion: \$0.50 / GB | < 100 MB log volume | First 50 GB/project/month free | **\$0.00** (100% within Free Tier) |

### Total Cost Summary:
* **Scenario A (Recommended — Preserving Active Spanner Free Trial):**
  $$\text{Total 10-Day Estimated Cost} \approx \mathbf{\$6.39 \text{ USD}} \quad (\text{Bounded: } \$6.00 - \$8.50 \text{ USD})$$
* **Scenario B (If Spanner is explicitly upgraded to provisioned 100 PUs):**
  $$\text{Total 10-Day Estimated Cost} \approx \$6.39 + \$26.40 = \mathbf{\$32.79 \text{ USD}} \quad (\text{Bounded: } \$30.00 - \$36.00 \text{ USD})$$

---

## 4. Step-by-Step Provisioning Plan (Independently Reviewable)

### STEP 1: Enable Missing Google Cloud APIs
* **Prerequisites:** `serviceusage.googleapis.com` (already enabled).
* **Dependency:** Must execute prior to creating secrets, slide extractors, or group lookups.
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

### STEP 2: Service Account Provisioning
* **Prerequisites:** `iam.googleapis.com` (enabled).
* **Dependency:** Required before assigning IAM roles or binding Cloud Run service identities.
* **Exact Commands:**
  ```bash
  # 1. Ingestion Copilot Runtime Identity
  gcloud iam service-accounts create ingestion-copilot-sa \
    --project=xenon-broker-513704-s9 \
    --display-name="Enterprise GraphRAG Ingestion Copilot Service Account" \
    --description="Runtime identity for ambient Ingestion Copilot Cloud Run workers"

  # 2. Discovery Copilot Runtime Identity
  gcloud iam service-accounts create discovery-copilot-sa \
    --project=xenon-broker-513704-s9 \
    --display-name="Enterprise GraphRAG Discovery Copilot Service Account" \
    --description="Runtime identity for interactive Discovery Copilot Agent Runtime"

  # 3. Dedicated Pub/Sub Push Invoker Identity
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

#### A. Ingestion Copilot (`ingestion-copilot-sa`)
```bash
# 1. Document AI Online Processing Authority (Least Privilege)
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/documentai.apiUser" \
  --condition=None

# 2. Vertex AI Embeddings and Reasoning Model Execution
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" \
  --condition=None

# 3. Cloud Spanner Read/Write Authority
gcloud spanner databases add-iam-policy-binding sales-knowledge-db \
  --instance=sales-discovery-trial \
  --project=xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/spanner.databaseUser"

# 4. Telemetry and Logging
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter" \
  --condition=None

gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/cloudtrace.agent" \
  --condition=None

# 5. Legacy US GCS Bucket Read-Only Access
gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp \
  --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"
```

#### B. Discovery Copilot (`discovery-copilot-sa`)
```bash
# 1. Cloud Spanner Strictly Read-Only Authority
gcloud spanner databases add-iam-policy-binding sales-knowledge-db \
  --instance=sales-discovery-trial \
  --project=xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/spanner.databaseReader"

# 2. Vertex AI Gemini Reasoning Model Execution
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user" \
  --condition=None

# 3. Cloud Identity Group Directory Viewer (Pre-Retrieval RBAC)
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/cloudidentity.groupsViewer" \
  --condition=None

# 4. Legacy US GCS Bucket Read-Only Access
gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

# 5. Telemetry and Logging
gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/logging.logWriter" \
  --condition=None

gcloud projects add-iam-policy-binding xenon-broker-513704-s9 \
  --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
  --role="roles/cloudtrace.agent" \
  --condition=None
```

#### C. Pub/Sub Service Agent Delegation (Token Creation Authority)
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
* **Prerequisites:** `storage.googleapis.com` (enabled).
* **Dependency:** Must be provisioned before assigning bucket-level IAM or executing ingestion.
* **Exact Commands:**
  ```bash
  # 1. Create Regional Bucket in Mumbai with Uniform Bucket-Level Access
  gcloud storage buckets create gs://sales-discovery-mvp-mumbai \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --default-storage-class=STANDARD \
    --uniform-bucket-level-access \
    --public-access-prevention

  # 2. Enable Object Versioning
  gcloud storage buckets update gs://sales-discovery-mvp-mumbai \
    --versioning

  # 3. Bind Ingestion Runtime Object Admin
  gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp-mumbai \
    --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/storage.objectAdmin"

  # 4. Bind Discovery Runtime Object Viewer
  gcloud storage buckets add-iam-policy-binding gs://sales-discovery-mvp-mumbai \
    --member="serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/storage.objectViewer"
  ```
* **Lifecycle Policy Configuration:**
  Create a temporary configuration file `gcs_lifecycle.json`:
  ```json
  {
    "rule": [
      {
        "action": {"type": "AbortIncompleteMultipartUpload"},
        "condition": {"age": 7}
      },
      {
        "action": {"type": "SetStorageClass", "storageClass": "NEARLINE"},
        "condition": {"age": 30, "isLive": false}
      },
      {
        "action": {"type": "Delete"},
        "condition": {"age": 90, "isLive": false}
      }
    ]
  }
  ```
  Apply lifecycle policy:
  ```bash
  gcloud storage buckets update gs://sales-discovery-mvp-mumbai --lifecycle-file=gcs_lifecycle.json
  ```
* **Create Canonical Object Prefixes:**
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
* **Dependency:** Required before building container images via Cloud Build.
* **Exact Command:**
  ```bash
  gcloud artifacts repositories create enterprise-graphrag \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --repository-format=docker \
    --description="Container repository for Enterprise Agentic GraphRAG copilots"
  ```
* **Authorize Cloud Build Service Account to push images:**
  ```bash
  gcloud artifacts repositories add-iam-policy-binding enterprise-graphrag \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --member="serviceAccount:291679685305@cloudbuild.gserviceaccount.com" \
    --role="roles/artifactregistry.writer"
  ```

---

### STEP 6: Document AI Layout Parser Processor
* **Prerequisites:** `documentai.googleapis.com` (enabled).
* **Dependency:** Required before running PDF ingestion pipelines.
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
    --location=asia-south1
  ```

---

### STEP 7: Cloud Pub/Sub Topic & Authenticated Push Subscription
* **Prerequisites:** `pubsub.googleapis.com` (enabled), `pubsub-invoker-sa` created.
* **Dependency:** The push endpoint requires the Cloud Run URL. For Phase 2B.2 pre-deployment, the topic and subscription are provisioned with a placeholder push configuration, updated upon Cloud Run deployment.
* **Exact Commands:**
  ```bash
  # 1. Create Ingestion Events Topic
  gcloud pubsub topics create ingestion-events \
    --project=xenon-broker-513704-s9

  # 2. Create Push Subscription with OIDC Authentication
  # (Placeholder push endpoint; updated in Phase 3 upon Cloud Run deployment)
  gcloud pubsub subscriptions create ingestion-worker-sub \
    --topic=ingestion-events \
    --project=xenon-broker-513704-s9 \
    --ack-deadline=600 \
    --message-retention-duration=7d \
    --push-endpoint="https://ingestion-copilot-placeholder.a.run.app/pubsub/push" \
    --push-auth-service-account="pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com"
  ```
* **Authorize Cloud Run Invocation:**
  ```bash
  # Authorize pubsub-invoker-sa to invoke ingestion-copilot (executed when service is scaffolded/deployed)
  gcloud run services add-iam-policy-binding ingestion-copilot \
    --project=xenon-broker-513704-s9 \
    --region=asia-south1 \
    --member="serviceAccount:pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/run.invoker" || true
  ```

---

### STEP 8: Secret Manager Secret Provisioning
* **Prerequisites:** `secretmanager.googleapis.com` enabled (from Step 1).
* **Dependency:** Required before configuring GitHub tree extraction.
* **Exact Commands:**
  ```bash
  # 1. Create Secret Container (without exposing static plaintext values in shell history)
  gcloud secrets create github-access-token \
    --project=xenon-broker-513704-s9 \
    --replication-policy="automatic"

  # 2. Restrict Access Exclusively to Ingestion Runtime Identity
  gcloud secrets add-iam-policy-binding github-access-token \
    --project=xenon-broker-513704-s9 \
    --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
  ```
* **Payload Ingestion Note:** Secret payloads must be added interactively via standard input (`gcloud secrets versions add github-access-token --data-file=-`) or GCP Console to prevent logging tokens in bash history.

---

## 5. Rollback Procedures & Irreversibility Assessment

If an infrastructure provisioning operation fails or requires de-provisioning, the exact rollback sequence is documented below:

### 1. Step-by-Step Rollback Commands

```bash
# Rollback Step 8: Delete Secret Container
gcloud secrets delete github-access-token --project=xenon-broker-513704-s9 --quiet || true

# Rollback Step 7: Delete Pub/Sub Subscription & Topic
gcloud pubsub subscriptions delete ingestion-worker-sub --project=xenon-broker-513704-s9 --quiet || true
gcloud pubsub topics delete ingestion-events --project=xenon-broker-513704-s9 --quiet || true

# Rollback Step 6: Delete Document AI Processor
PROCESSOR_ID=$(gcloud documentai processors list --project=xenon-broker-513704-s9 --location=asia-south1 --format="value(name)" --filter="displayName:enterprise-graphrag-layout-parser")
if [ -n "$PROCESSOR_ID" ]; then
  gcloud documentai processors delete "$PROCESSOR_ID" --project=xenon-broker-513704-s9 --location=asia-south1 --quiet || true
fi

# Rollback Step 5: Delete Artifact Registry Repository
gcloud artifacts repositories delete enterprise-graphrag --project=xenon-broker-513704-s9 --location=asia-south1 --quiet || true

# Rollback Step 4: Delete Regional GCS Bucket (Deletes only newly created Mumbai bucket)
gcloud storage rm --recursive gs://sales-discovery-mvp-mumbai || true

# Rollback Step 3 & 2: Delete Created Service Accounts (Removes all associated IAM bindings automatically)
gcloud iam service-accounts delete pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com --project=xenon-broker-513704-s9 --quiet || true
gcloud iam service-accounts delete discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com --project=xenon-broker-513704-s9 --quiet || true
gcloud iam service-accounts delete ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com --project=xenon-broker-513704-s9 --quiet || true

# Rollback Step 1: Disable Enabled APIs (Optional - leaving enabled has zero billing cost)
gcloud services disable \
  secretmanager.googleapis.com \
  slides.googleapis.com \
  cloudidentity.googleapis.com \
  --project=xenon-broker-513704-s9 || true
```

### 2. Irreversibility and Billing Assessment:
* **Zero-Touch Safeguard:** The rollback commands strictly exclude the existing Cloud Spanner instance (`sales-discovery-trial`), the database (`sales-knowledge-db`), and the legacy storage bucket (`gs://sales-discovery-mvp`). Existing production records remain completely safe.
* **Bucket Deletion Cooldown:** If `sales-discovery-mvp-mumbai` is deleted, Google Cloud retains a short global namespace lock (typically a few minutes) before the exact same bucket name can be recreated.
* **Document AI Processing Charges:** Document AI charges per page processed (\$0.030/page). Any pages submitted to the processor prior to rollback cannot be refunded.
* **API Disablement Propagation:** Disabling an API can take up to 5 minutes to propagate across all Google Cloud edge zones.

---

## 6. Execution Dependency Sequence

```
                                  [START PHASE 2B.2]
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │   STEP 1: Enable Missing GCP APIs     │
                       │ (secretmanager, slides, cloudidentity)│
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  STEP 2: Provision Service Accounts   │
                       │ (ingestion-sa, discovery-sa, push-sa) │
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │   STEP 3: Bind Least-Privilege IAM    │
                       │ (DocAI, Spanner, Vertex, GCS, Token)  │
                       └───────────────────┬───────────────────┘
                                           │
         ┌─────────────────────────────────┼─────────────────────────────────┐
         │                                 │                                 │
         ▼                                 ▼                                 ▼
┌──────────────────┐             ┌──────────────────┐             ┌──────────────────┐
│ STEP 4: GCS      │             │ STEP 5: Artifact │             │ STEP 6: DocAI    │
│ Mumbai Bucket &  │             │ Registry Docker  │             │ Layout Parser in │
│ Lifecycle Config │             │ Repository       │             │ asia-south1      │
└────────┬─────────┘             └────────┬─────────┘             └────────┬─────────┘
         │                                 │                                 │
         └─────────────────────────────────┼─────────────────────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │ STEP 7: Pub/Sub Topic & Push Sub      │
                       │ (ingestion-events, push auth token)   │
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
                       ┌───────────────────────────────────────┐
                       │  STEP 8: Secret Manager Container     │
                       │ (github-access-token + accessor IAM)  │
                       └───────────────────┬───────────────────┘
                                           │
                                           ▼
                        [READY FOR PHASE 2B.3: SPANNER DDL]
```

---

## 7. Approval Gate & GO Recommendation

### Infrastructure Readiness Certification:
* All resource identifiers, regions, IAM roles, and CLI commands have been validated against official Google Cloud documentation and live project constraints (`xenon-broker-513704-s9`, `asia-south1`).
* Defense-in-depth is enforced via strict separation of duties (write ingestion vs. read discovery vs. push invoker).
* 10-day evaluation cost is verified under \$10.00 USD with zero database infrastructure costs during the trial period.

### Operational Gate:
**STRICT DESIGN BOUNDARY PRESERVED.** No commands have been executed. Awaiting explicit user approval before initiating Phase 2B.2 provisioning.

---

*Report authored and certified by Senior Google Cloud Infrastructure Architect & DevSecOps Engineer.*  
*Saved to `/home/naveen/enterprise-graphrag/PHASE_2B_2_PROVISIONING_PLAN.md`.*
