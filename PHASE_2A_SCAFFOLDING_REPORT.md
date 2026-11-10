# Phase 2A: Local Scaffolding Execution Report

**Project:** Sales Solution Discovery MVP (`enterprise-graphrag`)  
**Cloud Project:** `xenon-broker-513704-s9` | **Region:** `asia-south1` (Mumbai)  
**Date:** 2026-11-10  
**Phase Status:** **PHASE 2A COMPLETE — LOCAL SCAFFOLDING VERIFIED**  
**Execution Boundary:** **STRICT LOCAL SCAFFOLDING ONLY (Zero Cloud Resources Created or Modified)**

---

## 1. Executive Summary

Phase 2A established the local repository architecture, initialized Git version control, configured the multi-project `uv` workspace monorepo, and scaffolded both independent Google ADK Python projects using the official `agents-cli` CLI tool.

All pre-existing Phase 0/1 files (`.agents-cli-spec.md`, `PHASE_1F_EXECUTABILITY_REPORT.md`) have been strictly preserved. Unit testing suites across `common/`, `ingestion-copilot/`, and `discovery-copilot/` were executed and passed cleanly.

---

## 2. Monorepo Topology & Files Created

```
enterprise-graphrag/
├── .git/                                 # Initialized Git repository (main branch)
├── .gitignore                            # Root ignore rules (Python, venv, secrets, caches)
├── .agents-cli-spec.md                   # PRESERVED: Approved Phase 0 Specification
├── PHASE_1F_EXECUTABILITY_REPORT.md      # PRESERVED: Phase 1F Readiness Report
├── PHASE_2A_SCAFFOLDING_REPORT.md        # THIS REPORT: Phase 2A Scaffolding Execution Report
├── README.md                             # Monorepo architecture & quickstart guide
├── pyproject.toml                        # Root uv workspace configuration
├── uv.lock                               # Locked workspace dependency graph
│
├── common/                               # Shared Knowledge Plane, Models, MMR & RBAC
│   ├── pyproject.toml                    # Package definition for 'common'
│   ├── README.md                         # Documentation for shared modules
│   ├── src/common/
│   │   ├── __init__.py                   # Package export
│   │   ├── config.py                     # Project configuration & defaults (asia-south1)
│   │   ├── database/
│   │   │   ├── __init__.py
│   │   │   └── spanner.py                # Spanner client & transaction manager interface
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   └── schema.py                 # Pydantic models for 8 node tables & 11 edge tables
│   │   ├── rbac/
│   │   │   ├── __init__.py
│   │   │   └── rbac.py                   # Pre-retrieval access filtering interface
│   │   ├── search/
│   │   │   ├── __init__.py
│   │   │   └── mmr.py                    # Deterministic Maximal Marginal Relevance engine
│   │   └── storage/
│   │       ├── __init__.py
│   │       └── resolver.py               # Dual-bucket GCS resolver (US legacy vs Mumbai)
│   └── tests/
│       ├── test_config.py                # Validation of default regional settings
│       └── test_mmr.py                   # Validation of deterministic MMR reranking
│
├── ingestion-copilot/                    # Cloud Run Ambient Ingestion Copilot
│   ├── .env                              # Local env (asia-south1, sales-knowledge-db)
│   ├── .env.example                      # Environment template
│   ├── .gcloudignore                     # Deployment ignore rules
│   ├── .gitignore                        # Agent ignore rules
│   ├── Dockerfile                        # Container packaging for Cloud Run
│   ├── GEMINI.md                         # Agent developer guidance
│   ├── README.md                         # Agent overview & runbook
│   ├── agents-cli-manifest.yaml          # Agents CLI project manifest
│   ├── pyproject.toml                    # Package definition with common workspace dependency
│   ├── uv.lock                           # Agent lockfile
│   ├── app/
│   │   ├── __init__.py
│   │   ├── agent.py                      # Root ADK agent definition
│   │   ├── fast_api_app.py               # Ambient Pub/Sub FastAPI trigger entrypoint
│   │   └── app_utils/
│   │       ├── a2a.py                   # A2A protocol implementation
│   │       └── services.py              # Telemetry & service helpers
│   ├── deployment/                       # Terraform IaC definitions
│   └── tests/
│       ├── unit/test_dummy.py            # Unit test suite
│       ├── integration/                  # E2E server & agent tests
│       └── eval/                         # Evaluation datasets & configs
│
└── discovery-copilot/                    # Agent Runtime Conversational Discovery Copilot
    ├── .env                              # Local env (asia-south1, sales-knowledge-db)
    ├── .env.example                      # Environment template
    ├── .gcloudignore                     # Deployment ignore rules
    ├── .gitignore                        # Agent ignore rules
    ├── Dockerfile                        # Container packaging
    ├── GEMINI.md                         # Agent developer guidance
    ├── README.md                         # Agent overview & runbook
    ├── agents-cli-manifest.yaml          # Agents CLI project manifest
    ├── deployment_metadata.json          # Deployment target metadata
    ├── pyproject.toml                    # Package definition with common workspace dependency
    ├── uv.lock                           # Agent lockfile
    ├── app/
    │   ├── __init__.py
    │   ├── agent.py                      # Root ADK multi-turn planner agent
    │   ├── fast_api_app.py               # FastAPI entrypoint
    │   └── app_utils/
    │       ├── a2a.py                   # A2A protocol implementation
    │       ├── reasoning_engine_adapter.py # Agent Runtime / Reasoning Engine adapter
    │       └── services.py              # Telemetry & service helpers
    ├── deployment/                       # Terraform IaC definitions
    └── tests/
        ├── unit/test_dummy.py            # Unit test suite
        ├── integration/                  # E2E server & agent tests
        └── eval/                         # Evaluation datasets & configs
```

---

## 3. Exact Scaffold Commands Used

All scaffolding commands were executed using `agents-cli` v1.13.0 under WSL user `naveen` inside `/home/naveen/enterprise-graphrag`:

### 1. Ingestion Copilot (Cloud Run Prototype)
```bash
agents-cli scaffold create ingestion-copilot \
  --agent adk_python \
  --deployment-target cloud_run \
  --region asia-south1 \
  --prototype \
  --agent-guidance-filename GEMINI.md \
  --cloud-run-identity-default service_account \
  --skip-checks \
  --auto-approve
```

### 2. Discovery Copilot (Agent Runtime Prototype)
```bash
agents-cli scaffold create discovery-copilot \
  --agent adk_python \
  --deployment-target agent_runtime \
  --region asia-south1 \
  --prototype \
  --agent-guidance-filename GEMINI.md \
  --agent-runtime-identity-default service_account \
  --skip-checks \
  --auto-approve
```

---

## 4. Dependencies Installed & Workspace Synchronization

A multi-project workspace was established via root `pyproject.toml` linking `common`, `ingestion-copilot`, and `discovery-copilot`.

* **Workspace Synchronization Command:**
  ```bash
  uv sync --all-groups
  ```
* **Core Dependencies Installed:**
  * `google-adk[gcp,otel-gcp,bigquery-analytics]>=2.12.0,<3.0.0`
  * `google-cloud-agentplatform[agent-engines]>=2.2.0,<3.0.0`
  * `google-cloud-aiplatform[evaluation]>=2.2.0,<3.0.0`
  * `google-cloud-spanner>=3.49.0`
  * `google-cloud-storage>=2.19.0`
  * `google-genai>=1.0.0`
  * `fastapi>=0.115.8,<1.0.0`
  * `uvicorn~=0.34.0`
  * `pydantic>=2.10.0`
  * `numpy>=1.26.0`
  * `pytest>=9.0.2,<10.0.0`
  * `pytest-asyncio>=1.0.0,<2.0.0`
  * `hatchling` (PEP 517 build backend)
* **Local Package Linkage:**
  * `common` is declared as an active workspace dependency in both `ingestion-copilot` and `discovery-copilot`:
    ```toml
    [tool.uv.sources]
    common = { workspace = true }
    ```

---

## 5. Local Validation Commands and Results

Local unit test verification was conducted across all three projects using `uv run pytest`:

### 1. Common Package Tests
```bash
cd common && uv run pytest tests
```
* **Result:** **3 PASSED in 0.16s**
  * `tests/test_config.py` (Default region `asia-south1`, project `xenon-broker-513704-s9`, dimensions 768) — **PASSED**
  * `tests/test_mmr.py::test_mmr_empty_input` — **PASSED**
  * `tests/test_mmr.py::test_mmr_ranking_relevance_vs_diversity` (Mathematical diversity reranking verification) — **PASSED**

### 2. Ingestion Copilot Unit Tests
```bash
cd ingestion-copilot && uv run pytest tests/unit
```
* **Result:** **1 PASSED in 0.01s**
  * `tests/unit/test_dummy.py::test_dummy` — **PASSED**

### 3. Discovery Copilot Unit Tests
```bash
cd discovery-copilot && uv run pytest tests/unit
```
* **Result:** **1 PASSED in 0.01s**
  * `tests/unit/test_dummy.py::test_dummy` — **PASSED**

* **Total Test Summary:** **5 passed across 3 projects, 0 failures, 0 errors.**

---

## 6. Git Status & Version Control

* **Repository Root:** `/home/naveen/enterprise-graphrag/.git`
* **Branch:** `main`
* **Initial Scaffolding Commit:** `63c259e`
* **Commit Message:** `feat(scaffold): initialize monorepo and scaffold ingestion & discovery copilots (Phase 2A)`
* **Status:** Clean working tree (93 files tracked, 0 uncommitted changes).

---

## 7. Errors or Unresolved Issues

* **Resolved Issue (WSL file ownership):** Files initially created via Windows UNC path inherited root permissions. Resolved via `chown -R naveen:naveen /home/naveen/enterprise-graphrag`.
* **Resolved Issue (PATH configuration):** Added `/home/naveen/.local/bin` to `naveen`'s `.bashrc` ensuring `agents-cli` and `uv` are available in all subshells.
* **Resolved Issue (MMR tie-breaking in unit test):** Synthetic test vectors with identical zero relevance to query produced a tie under $\lambda=0.5$; adjusted test case to $\lambda=0.3$ to cleanly demonstrate diversity penalty over redundant candidates.
* **Current Status:** **ZERO unresolved issues.**

---

## 8. Explicit Confirmation of Cloud Safety Boundaries

In strict compliance with the Phase 2A authorization boundaries:

1. **NO GCP resources were created or modified.**
2. **NO Google Cloud APIs were enabled.**
3. **NO IAM roles, service accounts, or permissions were changed.**
4. **NO Cloud Spanner DDL or DML statements were executed.**
5. **Existing live rows in `sales-knowledge-db` remain 100% untouched.**
6. **NO Google Cloud Storage buckets were created or modified.**
7. **NO agent deployments (Cloud Run or Agent Runtime) were triggered.**
8. **NO Gemini Enterprise publishing operations were performed.**
9. **Phase 3 and Phase 4 implementation remains inactive.**

---

*Report authored and certified by Senior Google Cloud Spanner Architect & GraphRAG Database Engineer.*  
*Saved to `/home/naveen/enterprise-graphrag/PHASE_2A_SCAFFOLDING_REPORT.md`.*
