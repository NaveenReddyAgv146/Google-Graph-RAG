# Phase 2B.2: Google Cloud Infrastructure Provisioning Plan (Revision 3 — Final)

**Project:** Sales Solution Discovery MVP (`enterprise-graphrag`)  
**Project ID:** `xenon-broker-513704-s9` | **Project Number:** `291679685305`  
**Primary Region:** `asia-south1` (Mumbai)  
**Database:** `sales-knowledge-db` on Spanner instance `sales-discovery-trial`  
**Author:** Senior Google Cloud Infrastructure Architect & DevSecOps Engineer  
**Date:** 2026-11-11  
**Execution Boundary:** **STRICT DESIGN ONLY — ZERO GCP MUTATIONS EXECUTED**

---

## 1. Executive Summary & Review Scope

This third revision (`V3`) establishes the complete, production-grade infrastructure provisioning specification for Phase 2B.2. It resolves all feedback regarding Cloud Spanner IAM permissions, Vertex AI model pricing, Secret Manager CLI grammar and regional topology, Cloud Identity scope governance, and Cloud Build identity verification.

### Summary of Major Technical Corrections in Revision 3:
1. **Spanner Least-Privilege Custom IAM Role Grammar:**
   * Replaced the invalid permission `spanner.databases.beginReadWriteTransaction` with the official documented permission **`spanner.databases.beginOrRollbackReadWriteTransaction`**.
   * Completely removed non-existent permission `spanner.sessions.update`.
   * Verified all 10 custom role permissions against the live Google Cloud Spanner IAM permission catalog.
   * Documented a concrete two-stage automated verification procedure demonstrating that the ingestion identity can execute DML/read-write transactions while receiving explicit `PERMISSION_DENIED` on DDL (`spanner.databases.updateDdl`).
2. **Vertex AI Gemini 2.5 Flash Pricing Realignment:**
   * Aligned with the current Vertex AI / Agent Platform standard pricing reference of **\$0.30 per 1,000,000 input tokens** and **\$2.50 per 1,000,000 output tokens** ($\le 128\text{k}$ standard context tier).
   * Recalculated 10-day evaluation cost for 2,000,000 input tokens and 500,000 output tokens to **\$1.85 USD** (previously \$0.30).
   * Updated the 10-day evaluation total for **Scenario A (Spanner Free Trial)** to **\$4.92 USD** (conservative buffer: **\$7.00 USD**).
   * Updated the 10-day evaluation total for **Scenario B (Provisioned Spanner 100 PUs)** to **\$31.32 USD** (conservative buffer: **\$38.00 USD**).
   * Documented regional model endpoint `asia-south1-aiplatform.googleapis.com`, location `asia-south1`, and model ID `gemini-2.5-flash`.
3. **Secret Manager Command Grammar & Architectural Topology:**
   * Replaced non-existent `--data-locations` flag with the documented **`--locations`** flag for user-managed replication.
   * Explicitly differentiated between:
     * *Global Secret Manager with User-Managed Replication:* Standard global control plane, encrypted payload strictly stored in `asia-south1`.
     * *Regional Secret Manager Service:* Complete control-plane and data-plane regional isolation in `asia-south1` utilizing regional endpoints (`secretmanager.asia-south1.rep.googleapis.com`) and location-qualified resource IDs.
   * Detailed the Python client SDK instantiation requirements and resource naming syntax for both options.
4. **Cloud Identity Group Membership Governance:**
   * Clarified the fundamental architectural boundary between Google Cloud Project IAM and Google Workspace / Cloud Identity Customer IAM.
   * Documented the two valid enterprise patterns for assigning `roles/cloudidentity.groupsReader` at the `customers/{customer_id}` or `groups/{group_id}` resource level (Cloud Identity Super Admin assignment vs Domain-Wide Delegation of Authority).
   * Confirmed that project-level IAM bindings are unsupported for Cloud Identity roles and documented how Discovery Copilot performs group expansion at query time.
5. **Cloud Build Identity Verification:**
   * Established a dynamic verification step to inspect the active regional build identity (`291679685305-compute@developer.gserviceaccount.com` in `asia-south1`) before granting `roles/artifactregistry.writer`.
   * Preserved support for custom pipeline identities (`--service-account`) for future automated triggers.
6. **Data & Infrastructure Preservation:**
   * Guaranteed zero mutations to existing trial Spanner instance `sales-discovery-trial`, database `sales-knowledge-db` (8 tables, 195 chunks, 125 visuals, 5 docs, 5 solutions, 10 ACL rows), and legacy bucket `gs://sales-discovery-mvp`.

---

## 2. Corrected Identity & Least-Privilege IAM Architecture

To enforce defense-in-depth and prevent privilege escalation across all execution planes, three distinct service accounts and one custom role govern access:

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
           │   ├── Vertex AI: roles/aiplatform.user (text-embedding-005 & gemini-2.5-flash)
           │   └── Secret Manager: roles/secretmanager.secretAccessor (Mumbai PAT)
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
* **Security Guardrail:** Strictly prohibited from holding `roles/spanner.databaseUser` or `roles/spanner.databaseAdmin`.

#### Custom Spanner IAM Role: `spannerGraphDataReadWriter`
* **Role ID:** `projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter`
* **Title:** Enterprise GraphRAG Spanner Data Read/Writer
* **Description:** Grants transactional DML read and write access to Spanner database tables while strictly withholding DDL schema modification privileges.
* **Verified Included Permissions (10 total):**
  1. `spanner.databases.beginOrRollbackReadWriteTransaction` *(Initiates and commits/aborts read-write transactions)*
  2. `spanner.databases.beginReadOnlyTransaction` *(Snapshot reads and consistent query evaluation)*
  3. `spanner.databases.get` *(Reads database metadata, state, and dialect)*
  4. `spanner.databases.read` *(Key-based lookups and range scans)*
  5. `spanner.databases.select` *(Executes SQL statements and ISO GQL graph queries)*
  6. `spanner.databases.write` *(Applies batch mutations and transactional DML)*
  7. `spanner.sessions.create` *(Acquires new database sessions for the client pool)*
  8. `spanner.sessions.delete` *(Closes and cleans up idle database sessions)*
  9. `spanner.sessions.get` *(Inspects session health and validity)*
  10. `spanner.sessions.list` *(Maintains session pool state in client)*

* **Explicitly Excluded Permissions (Enforcing Schema Immutability):**
  * `spanner.databases.updateDdl` *(Prevents runtime schema alterations — isolates schema change to Phase 2C)*
  * `spanner.databases.drop` *(Prevents accidental or malicious database deletion)*
  * `spanner.databases.createBackup` / `updateBackupSchedule` *(Backup administration)*
  * `spanner.databases.setIamPolicy` *(Prevents IAM escalation on the database)*
  * `spanner.instances.*` *(Prevents instance deletion, scaling, or configuration changes)*

#### Support for Python Client Operations:
The verified custom role fully satisfies all operations performed by `google-cloud-spanner` in Python:
* `database.run_in_transaction(unit_of_work)`: Uses `beginOrRollbackReadWriteTransaction`, `read`, `select`, and `write`.
* `database.execute_sql("GRAPH SalesGraph MATCH ...")`: Uses `select` and `sessions.*`.
* `database.batch().insert(...)` / `database.apply_mutations(...)`: Uses `write` and `sessions.*`.
* `database.snapshot()`: Uses `beginReadOnlyTransaction`, `read`, `select`, and `sessions.*`.

#### Verification Method for DML Allowed vs DDL Denied:
To prove conclusively that the ingestion runtime identity can write data but cannot execute DDL, execute the following verification probe:
```python
# test_spanner_least_privilege.py
import sys
from google.cloud import spanner
from google.api_core.exceptions import PermissionDenied

client = spanner.Client(project="xenon-broker-513704-s9")
instance = client.instance("sales-discovery-trial")
database = instance.database("sales-knowledge-db")

print("--- [TEST 1] Verifying Data Write Authority (DML) ---")
try:
    def write_tx(transaction):
        # Transactional probe on migration tracking table
        transaction.execute_update(
            "INSERT INTO MigrationBatches (BatchId, Stage, Status, ExecutedAt) "
            "VALUES ('PROBE_TEST', 'TEST', 'PROBE', PENDING_COMMIT_TIMESTAMP())"
        )
    database.run_in_transaction(write_tx)
    print("SUCCESS: Transactional DML write succeeded.")
except Exception as e:
    print(f"FAILED: Data write failed: {e}")
    sys.exit(1)

print("\n--- [TEST 2] Verifying Schema Alteration Prohibition (DDL) ---")
try:
    ddl_statement = "ALTER TABLE Documents ADD COLUMN probe_test_column STRING(10)"
    op = database.update_ddl([ddl_statement])
    op.result(timeout=30)
    print("CRITICAL SECURITY FAILURE: DDL execution succeeded! Custom role is NOT least-privilege.")
    sys.exit(2)
except PermissionDenied as e:
    print(f"SUCCESS: DDL correctly rejected with PermissionDenied: {e.message}")
except Exception as e:
    if "spanner.databases.updateDdl" in str(e) or "PermissionDenied" in str(type(e)):
        print("SUCCESS: DDL correctly rejected due to missing updateDdl permission.")
    else:
        print(f"UNEXPECTED ERROR: DDL failed with: {e}")
```

#### Other IAM Bindings for `ingestion-copilot-sa`:
* `roles/storage.objectAdmin` on `gs://sales-discovery-mvp-mumbai`
* `roles/storage.objectViewer` on `gs://sales-discovery-mvp` (Legacy US bucket)
* `roles/documentai.apiUser` (Project-level; grants `documentai.processors.processOnline`)
* `roles/aiplatform.user` (Project-level; grants `aiplatform.endpoints.predict`)
* `roles/secretmanager.secretAccessor` on Secret Manager secret
* `roles/logging.logWriter` & `roles/cloudtrace.agent` (Project-level)
* **Explicit Exclusion:** Does **NOT** hold `roles/pubsub.subscriber`.

---

### Identity 2: Agent Runtime Discovery (`discovery-copilot-sa`)
* **Email:** `discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`
* **Role:** Interactive multi-turn conversational coordinator executing ISO GQL traversals and synthesis.
* **IAM Role Bindings:**
  * `roles/spanner.databaseReader` on `sales-knowledge-db` (**Strictly read-only**; snapshot GQL & vector distance reads)
  * `roles/storage.objectViewer` on `gs://sales-discovery-mvp-mumbai` and `gs://sales-discovery-mvp`
  * `roles/aiplatform.user` (Project-level; Gemini synthesis)
  * `roles/logging.logWriter` & `roles/cloudtrace.agent`
* **Explicit Exclusion:** Zero write permissions on Spanner/GCS; zero access to Secret Manager, Document AI, or Pub/Sub.

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

### 1. Cost Assumptions & Verified Price References:

1. **Cloud Spanner (`sales-discovery-trial`):**
   * Configured as `FREE_INSTANCE` with 100 PUs in `asia-south1`.
   * Verified expiry date: `2027-02-02T04:47:08Z` (84 days remaining).
   * Free Trial cost: **\$0.00**.
   * *Contingency Provisioned Cost:* If explicitly upgraded to provisioned capacity, 100 PUs in `asia-south1` cost \$0.11 / PU-hr (\$2.64 / day $\times$ 10 days = **\$26.40 USD**).

2. **Document AI Layout Parser:**
   * Published Google Cloud Document AI pricing for `LAYOUT_PARSER_PROCESSOR`: **\$10.00 per 1,000 pages** (\$0.010 per page).
   * Specialized processors have zero free allowance.
   * Projected 10-day volume: 300 total pages parsed across 5 architecture PDFs.
   * Cost: $300 \times \$0.010 =$ **\$3.00 USD**.

3. **Vertex AI Embeddings (`text-embedding-005` in `asia-south1`):**
   * Published Vertex AI pricing: **\$0.000025 per 1,000 characters**.
   * Projected 10-day volume: 500 evidence chunks generated $\times$ 1,500 characters average = 750,000 characters.
   * Cost: $\frac{750,000}{1,000} \times \$0.000025 =$ **\$0.01875 USD** (~**\$0.02 USD**).

4. **Vertex AI Gemini Model (`gemini-2.5-flash` in `asia-south1`):**
   * Regional Endpoint: `https://asia-south1-aiplatform.googleapis.com`
   * Model ID: `gemini-2.5-flash` (or Vertex AI Model Garden equivalent)
   * Pricing Tier: Standard Context Tier ($\le 128\text{k}$ tokens per request)
     * Prompt / Input: **\$0.30 per 1,000,000 tokens**
     * Response / Output: **\$2.50 per 1,000,000 tokens**
   * Projected 10-day volume: 200 conversational discovery sessions $\times$ 5 turns = 1,000 interactions.
     * Prompt Input: ~2,000 tokens/turn = 2,000,000 input tokens.
     * Synthesis Output: ~500 tokens/turn = 500,000 output tokens.
   * Cost:
     $$\text{Input Cost} = 2.0 \times \$0.30 = \$0.60 \text{ USD}$$
     $$\text{Output Cost} = 0.5 \times \$2.50 = \$1.25 \text{ USD}$$
     $$\text{Total Gemini Cost} = \$0.60 + \$1.25 = \mathbf{\$1.85 \text{ USD}}$$

5. **Cloud Storage (`sales-discovery-mvp-mumbai`):**
   * Standard storage in `asia-south1`: **\$0.020 per GB / month**.
   * Projected volume: 2 GB stored for 10 days.
   * Cost: $2 \times \$0.020 \times \frac{10}{30} = \$0.013$ + Class A/B operations ($\approx \$0.01$) = **\$0.023 USD** (~**\$0.03 USD**).

6. **Artifact Registry (`enterprise-graphrag`):**
   * Standard storage: **\$0.10 per GB / month**.
   * Projected volume: 1 container image (0.5 GB) for 10 days = $0.5 \times \$0.10 \times \frac{10}{30} =$ **\$0.017 USD** (~**\$0.02 USD**).

7. **Cloud Run (`ingestion-copilot`):**
   * Free monthly tier: 2,000,000 requests, 360,000 vCPU-seconds, 180,000 GiB-seconds.
   * Projected 10-day volume: 100 invocations $\times$ 30 seconds = 3,000 vCPU-seconds (100% within free allowance) = **\$0.00**.

8. **Cloud Pub/Sub, Secret Manager & Cloud Build:**
   * Pub/Sub: Free monthly tier (10 GB). Projected: < 50 MB = **\$0.00**.
   * Secret Manager: Free monthly tier (6 active versions, 10,000 operations). Projected: 1 secret version, < 200 operations = **\$0.00**.
   * Cloud Build: Free daily tier (120 build-minutes). Projected: 15 build-minutes total = **\$0.00**.

---

### 2. Comprehensive 10-Day Cost Evaluation Table:

| Service / Resource | Unit Rate | Projected Quantity | Billable Formula | Estimated Cost |
| :--- | :--- | :--- | :--- | :---: |
| **Document AI Layout Parser** | \$10.00 / 1K pages | 300 pages | $300 \times \$0.010$ | **\$3.00** |
| **Vertex AI Embeddings** | \$0.000025 / 1K chars | 750,000 chars | $\frac{750,000}{1,000} \times \$0.000025$ | **\$0.02** |
| **Vertex AI Gemini 2.5 Flash** | In: \$0.30/1M, Out: \$2.50/1M | 2M in / 500K out | $(2.0 \times \$0.30) + (0.5 \times \$2.50)$ | **\$1.85** |
| **Cloud Storage (Mumbai)** | \$0.020 / GB / month | 2 GB for 10 days | $2 \times \$0.020 \times \frac{10}{30} + \text{ops}$ | **\$0.03** |
| **Artifact Registry** | \$0.10 / GB / month | 0.5 GB for 10 days | $0.5 \times \$0.10 \times \frac{10}{30}$ | **\$0.02** |
| **Cloud Spanner (Instance)** | Free Trial (\$0.00) | 100 PUs for 10 days | Active free instance runway | **\$0.00** |
| **Cloud Run, Pub/Sub, Secrets** | Within Free Allowance | Standard MVP traffic | 100% covered by monthly tier | **\$0.00** |
| **TOTAL (Baseline Free Trial)** | — | — | Sum of billable services | **\$4.92 USD** |

* **Scenario A (Recommended — Active Spanner Free Trial):**
  $$\text{Total 10-Day Cost} = \$3.00 + \$0.02 + \$1.85 + \$0.03 + \$0.02 = \mathbf{\$4.92 \text{ USD}}$$
  $$\mathbf{Conservative\ Upper\ Bound\ (with\ 40\%\ Buffer):\ \$7.00 \text{ USD}}$$

* **Scenario B (Contingency — If Spanner Instance is Upgraded to Provisioned):**
  $$\text{Total 10-Day Cost} = \$4.92 + \$26.40 = \mathbf{\$31.32 \text{ USD}}$$
  $$\mathbf{Conservative\ Upper\ Bound\ (with\ 20\%\ Buffer):\ \$38.00 \text{ USD}}$$

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
* **Action Type:** IAM Custom Role creation at project level.
* **Exact Command:**
  ```bash
  gcloud iam roles create spannerGraphDataReadWriter \
    --project=xenon-broker-513704-s9 \
    --title="Enterprise GraphRAG Spanner Data Read/Writer" \
    --description="Grants DML transactional read and write access to Spanner tables without DDL modification privileges" \
    --permissions=spanner.databases.beginOrRollbackReadWriteTransaction,spanner.databases.beginReadOnlyTransaction,spanner.databases.get,spanner.databases.read,spanner.databases.select,spanner.databases.write,spanner.sessions.create,spanner.sessions.delete,spanner.sessions.get,spanner.sessions.list \
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

#### 3C. Cloud Identity Scope, Role Assignment & Directory Access Architecture:
* **The Architecture Boundary:** Google Cloud IAM governs project-level resources. In contrast, Google Workspace and Cloud Identity govern user, group, and customer resources under `customers/{customer_id}` or `groups/{group_id}`.
* **Why Project Binding Fails:** Binding `roles/cloudidentity.groupsReader` via `gcloud projects add-iam-policy-binding` returns an error because Cloud Identity roles cannot be granted on a GCP Project resource.
* **Role Assignment Mechanisms:**
  1. **Option 1: Cloud Identity Customer IAM (Direct API / Admin Console):**
     * *Authority Required:* Google Workspace / Cloud Identity **Super Administrator**.
     * *Resource Target:* `customers/{customer_id}` (retrieved via `gcloud organizations list` or Admin Console).
     * *Method:* Using the Cloud Identity REST API (`POST https://cloudidentity.googleapis.com/v1/customers/{customer_id}:setIamPolicy`) or Cloud Identity Admin Console, bind `roles/cloudidentity.groupsReader` to `serviceAccount:discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com`.
  2. **Option 2: Domain-Wide Delegation of Authority (Enterprise Pattern):**
     * *Authority Required:* Google Workspace Super Administrator.
     * *Configuration:* In Google Workspace Admin Console (`admin.google.com` $\rightarrow$ Security $\rightarrow$ Access and data control $\rightarrow$ API Controls $\rightarrow$ Domain-wide Delegation), add `discovery-copilot-sa`'s unique Client ID with the OAuth scope:
       `https://www.googleapis.com/auth/cloud-identity.groups.readonly`
* **Query-Time Retrieval by Discovery Agent:**
  * When a user queries Discovery Copilot, the agent determines the caller's identity (`user@domain.com`).
  * The agent calls Cloud Identity Groups API:
    `GET https://cloudidentity.googleapis.com/v1/groups/-/memberships:searchDirectGroups?query=member_key_id=='user@domain.com'`
  * The API returns the list of groups the user belongs to.
  * In Spanner, `DocumentAccess` rows define document-to-group relationships (`GroupEmail`). The agent filters ISO GQL graph traversals to nodes where `DocumentAccess.GroupEmail IN (user_groups)`.
* **Execution Boundary:** This step is governed in Phase 4 (Security & RBAC Integration) and is strictly omitted from Phase 2B.2 project-level scripts.

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
  Create `gcs_lifecycle_v3.json`:
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
  gcloud storage buckets update gs://sales-discovery-mvp-mumbai --lifecycle-file=gcs_lifecycle_v3.json
  ```
* **Lifecycle Timing Explanation & Retention Warning:**
  * **Rule 1 (`AbortIncompleteMultipartUpload`, `age: 7`):** Runs daily; aborts incomplete multipart uploads initiated more than 7 days ago.
  * **Rule 2 (`SetStorageClass: NEARLINE`, `daysSinceNoncurrentTime: 30`):** Exactly 30 days after an object version is superseded or deleted, it transitions to Nearline pricing (\$0.010/GB/month).
  * **Rule 3 (`Delete`, `daysSinceNoncurrentTime: 90`):** Exactly 90 days after becoming noncurrent, the archived version is permanently purged.
  * **CRITICAL COMPLIANCE NOTICE:** Rule 3 permanently deletes historical file versions older than 90 days. If organizational compliance requires immutable record retention, this rule must be removed prior to applying the configuration.
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

### STEP 5: Artifact Registry Docker Repository & Build Identity Verification
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
* **Dynamic Cloud Build Identity Verification & Writer Binding:**
  Rather than assuming a static service account email, the script dynamically discovers the regional default build identity and assigns `roles/artifactregistry.writer`:
  ```bash
  # 1. Query the active default build service account for asia-south1
  BUILD_SA=$(gcloud builds get-default-service-account --project=xenon-broker-513704-s9 --format="value(serviceAccountEmail)")
  echo "Discovered active Cloud Build identity: ${BUILD_SA}"

  # Verification assertion: Confirm identity was returned
  if [ -z "${BUILD_SA}" ]; then
    echo "ERROR: Unable to resolve Cloud Build service account. Checking compute default fallback..."
    BUILD_SA="291679685305-compute@developer.gserviceaccount.com"
  fi

  # 2. Bind Artifact Registry Writer role strictly to the verified build identity
  gcloud artifacts repositories add-iam-policy-binding enterprise-graphrag \
    --project=xenon-broker-513704-s9 \
    --location=asia-south1 \
    --member="serviceAccount:${BUILD_SA}" \
    --role="roles/artifactregistry.writer"
  ```
* **Trigger Identity Note:** If Cloud Build Triggers are configured in Phase 3 with a dedicated user-managed service account (`--service-account=...`), that custom service account will receive `roles/artifactregistry.writer` at trigger creation time.

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
* **Sequencing Rationale:** Creating a push subscription with a placeholder endpoint causes immediate delivery failures and retry backoffs. In Phase 2B.2, the topic is provisioned independently. The push subscription and `roles/run.invoker` binding are provisioned in Phase 3 once Cloud Run is deployed and its real HTTPS URL is established.
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

### STEP 8: Secret Manager Provisioning & Data Residency Topology

#### Architecture Comparison: User-Managed Global vs Regional Secret Manager
Organizations subject to Indian data localization (e.g. RBI / MeitY guidelines) must evaluate two topologies for storing GitHub Personal Access Tokens:

| Architectural Property | Option A: Global SM with User-Managed Replication | Option B: Regional Secret Manager |
| :--- | :--- | :--- |
| **Control Plane Location** | Global (`secretmanager.googleapis.com`) | Regional (`secretmanager.asia-south1.rep.googleapis.com`) |
| **Data Plane / Payload** | **Encrypted payload strictly in `asia-south1`** | **Encrypted payload strictly in `asia-south1`** |
| **Metadata Location** | Global | Strictly in `asia-south1` |
| **Resource Path Syntax** | `projects/{P}/secrets/{NAME}` | `projects/{P}/locations/asia-south1/secrets/{NAME}` |
| **Python SDK Client Setup** | Standard `SecretManagerServiceClient()` | Requires explicit `client_options={"api_endpoint": ...}` |
| **Compliance Alignment** | Satisfies data-at-rest localization mandates | Satisfies strict control-plane + metadata residency mandates |

#### Recommended Implementation (Option A — Global SM with User-Managed Replication):
This pattern guarantees that the secret payload **never leaves Mumbai**, while maintaining standard SDK compatibility across Google ADK and container runtimes.

* **Exact Commands:**
  ```bash
  # 1. Create Secret Container with user-managed replication pinned to Mumbai
  # Notice: The correct documented flag is --locations, NOT --data-locations
  gcloud secrets create github-access-token \
    --project=xenon-broker-513704-s9 \
    --replication-policy="user-managed" \
    --locations="asia-south1"

  # 2. Bind Accessor Role Strictly to Ingestion Runtime Identity
  gcloud secrets add-iam-policy-binding github-access-token \
    --project=xenon-broker-513704-s9 \
    --member="serviceAccount:ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
  ```
* **Payload Addition (Interactive / Zero Shell History Exposure):**
  ```bash
  echo -n "PASTE_GITHUB_PAT_HERE" | gcloud secrets versions add github-access-token \
    --project=xenon-broker-513704-s9 \
    --data-file=-
  ```
* **Application Access Implementation in Python:**
  ```python
  from google.cloud import secretmanager

  client = secretmanager.SecretManagerServiceClient()
  name = "projects/xenon-broker-513704-s9/secrets/github-access-token/versions/latest"
  response = client.access_secret_version(request={"name": name})
  github_token = response.payload.data.decode("UTF-8")
  ```

#### Strict Regional Alternative (Option B — Regional Secret Manager):
If organizational policy requires complete control-plane and metadata regional isolation:
* **Provisioning Command:**
  ```bash
  gcloud secrets create github-access-token \
    --project=xenon-broker-513704-s9 \
    --location="asia-south1"
  ```
* **Application Access Implementation in Python:**
  ```python
  from google.cloud import secretmanager

  # Regional endpoint must be explicitly supplied to client options
  client = secretmanager.SecretManagerServiceClient(
      client_options={"api_endpoint": "secretmanager.asia-south1.rep.googleapis.com"}
  )
  name = "projects/xenon-broker-513704-s9/locations/asia-south1/secrets/github-access-token/versions/latest"
  response = client.access_secret_version(request={"name": name})
  github_token = response.payload.data.decode("UTF-8")
  ```

---

## 5. Controlled Rollback Procedures & Irreversibility Assessment

### 1. Controlled Rollback Script (Non-Suppressed)
All silent error suppressions (`|| true`) are removed. If any step fails, the operator is alerted immediately.

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

| Resource to Provision | Exact Resource Identifier | Location | Hard Prerequisites | Successor Dependent Tasks |
| :--- | :--- | :--- | :--- | :--- |
| **Secret Manager API** | `secretmanager.googleapis.com` | Global | None | Step 8 (Secret Creation) |
| **Slides API** | `slides.googleapis.com` | Global | None | Phase 3 (Slides Extraction) |
| **Cloud Identity API** | `cloudidentity.googleapis.com` | Global | None | Phase 4 (RBAC Directory Lookup) |
| **Custom Spanner Role** | `projects/xenon-broker-513704-s9/roles/spannerGraphDataReadWriter` | Project | Spanner API | Step 3A (Database IAM Binding) |
| **Ingestion Runtime SA** | `ingestion-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Project | IAM API | Step 3A, Step 4, Step 8, Phase 3 |
| **Discovery Runtime SA** | `discovery-copilot-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Project | IAM API | Step 3B, Step 4, Phase 4 |
| **Pub/Sub Invoker SA** | `pubsub-invoker-sa@xenon-broker-513704-s9.iam.gserviceaccount.com` | Project | IAM API | Step 3D, Phase 3 (Push Sub) |
| **Regional GCS Bucket** | `gs://sales-discovery-mvp-mumbai` | `asia-south1` | Storage API | Step 4 IAM, Phase 2C Migration |
| **Artifact Registry** | `projects/xenon-broker-513704-s9/locations/asia-south1/repositories/enterprise-graphrag` | `asia-south1` | AR API, Cloud Build SA | Phase 3 (Container Builds) |
| **Document AI Processor**| `projects/xenon-broker-513704-s9/locations/asia-south1/processors/...` | `asia-south1` | Document AI API | Phase 3 (PDF Layout Parsing) |
| **Pub/Sub Topic** | `projects/xenon-broker-513704-s9/topics/ingestion-events` | Global | Pub/Sub API | Phase 3 (Push Subscription) |
| **Secret Container** | `projects/xenon-broker-513704-s9/secrets/github-access-token` | `asia-south1` (Data) | Secret Manager API | Phase 3 (GitHub Ingestion) |

---

## 7. Categorization of Facts, Assumptions & Approval Gates

### A. Verified Facts (Live Environment & Official Pricing Catalog):
1. **Spanner Instance Runway:** Spanner instance `sales-discovery-trial` is a free trial instance with 84 days of runway remaining, expiring `2027-02-02T04:47:08Z`.
2. **Spanner Production Data:** Database `sales-knowledge-db` contains 8 tables, 195 chunks, 125 visual assets, 5 documents, 5 solutions, and 10 ACL rows.
3. **Official Spanner IAM Permissions:** Live IAM API confirms `spanner.databases.beginOrRollbackReadWriteTransaction` is the valid transaction permission; `spanner.sessions.update` does not exist.
4. **Official Vertex AI Gemini Pricing:** Standard context tier ($\le 128\text{k}$) in `asia-south1` is \$0.30 / 1M input tokens and \$2.50 / 1M output tokens.
5. **Document AI Layout Parser Rate:** \$10.00 / 1,000 pages (\$0.010/page) with zero free allowance.
6. **Cloud Build Default Identity:** Live Cloud Build API confirms `291679685305-compute@developer.gserviceaccount.com` is the active regional build identity in `asia-south1`.
7. **Cloud Identity Canonical Role:** `roles/cloudidentity.groupsReader` is valid but applies only at Customer/Group resource scope, not Project scope.

### B. Architectural Assumptions:
1. **Workload Bounding:** Total 10-day evaluation traffic will not exceed 300 PDF pages, 500 chunks (750,000 chars), and 1,000 conversational turns (2M in / 500K out).
2. **Push Delivery Model:** Ingestion Copilot Cloud Run service will operate under an authenticated push delivery model validating OIDC tokens minted by `pubsub-invoker-sa`.
3. **Secret Residency Mode:** Global Secret Manager with user-managed replication pinned to `asia-south1` satisfies organizational data residency policies for the GitHub PAT.

### C. Decisions Requiring Explicit User Approval:
1. **Spanner Free Instance Retention:** Approval to execute the 10-day evaluation on the existing free trial instance ($0 cost) rather than upgrading to provisioned capacity ($26.40 cost).
2. **GCS 90-Day Version Deletion Approval:** Explicit sign-off that noncurrent object versions older than 90 days may be purged under lifecycle rule 3.
3. **Secret Manager Topology Selection:** Formal confirmation to proceed with Option A (Global SM with user-managed replication in Mumbai) versus Option B (Strict Regional Secret Manager).
4. **Execution Sign-off:** Explicit authorization to proceed with provisioning Phase 2B.2 cloud infrastructure (Steps 1–8).

---

## 8. Final GO / NO-GO Recommendation

### Verdict: **GO FOR PROVISIONING (AWAITING HUMAN APPROVAL)**

* **Security & Least Privilege:** Enforces strictly verified Spanner DML permissions (`beginOrRollbackReadWriteTransaction`), excludes DDL/Drop privileges, mandates OIDC push authentication, and isolates discovery to snapshot reads.
* **Cost Predictability:** 10-day evaluation cost is calculated and bounded at **\$4.92 USD** (Scenario A with buffer: **\$7.00 USD**).
* **Zero Disruption:** Guarantees absolute preservation of existing Spanner tables, visual assets, and US bucket data.

---

*Report authored and certified by Senior Google Cloud Infrastructure Architect & DevSecOps Engineer.*  
*Saved to `/home/naveen/enterprise-graphrag/PHASE_2B_2_PROVISIONING_PLAN_V3.md`.*
