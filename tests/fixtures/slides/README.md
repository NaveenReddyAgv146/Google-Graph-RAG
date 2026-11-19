# Phase 3B Dedicated Google Slides Test Fixture

This directory contains the synthetic reference fixture and instructions for testing the Google Slides ingestion pipeline in Ingestion Copilot.

---

## 1. Fixture Overview

- **File**: `enterprise_architecture_sample.pptx`
- **Format**: Microsoft PowerPoint Presentation (16:9 widescreen layout)
- **Synthetic Guarantee**: 100% synthetic enterprise architecture content. Contains zero customer, personal, proprietary, or production data.

### Presentation Content Breakdown

| Slide # | Slide Title | Modalities & Elements | Expected Extractions |
| :---: | :--- | :--- | :--- |
| **1** | Enterprise Architecture Strategy | - Title & Subtitle text<br>- Executive Summary paragraph<br>- Speaker notes | - 1 Text chunk (`## Enterprise Architecture Strategy...`)<br>- 1 Notes chunk (`Speaker Notes:...`)<br>- 1 Visual asset (`slide-1-render.png`) |
| **2** | Core Graph Platform Pillars | - Title text<br>- 4 Bullet points / Paragraphs<br>- 1 Embedded architecture diagram (`image/png`, 600x300)<br>- Speaker notes | - 1 Text chunk (`## Core Graph Platform Pillars...`)<br>- 1 Notes chunk (`Speaker Notes:...`)<br>- 1 Visual asset (`slide-2-render.png`)<br>- 1 Embedded image visual asset (`slide-2-image-1.png`) |
| **3** | Performance & Scalability Targets | - Title text<br>- 1 Table (5 rows x 4 columns with header row)<br>- Explanatory benchmark footnote text<br>- Speaker notes | - 1 Table chunk (Markdown formatted table)<br>- 1 Notes chunk (`Speaker Notes:...`)<br>- 1 Visual asset (`slide-3-render.png`) |
| **4** | Phase 3 Ingestion Roadmap | - Title text<br>- 4 Multi-line phase milestone paragraphs<br>- Speaker notes | - 1 Text chunk (`## Phase 3 Ingestion Roadmap...`)<br>- 1 Notes chunk (`Speaker Notes:...`)<br>- 1 Visual asset (`slide-4-render.png`) |

---

## 2. Importing into Google Slides

To create a live test presentation from this fixture:

1. **Upload to Google Drive**:
   - Open [Google Drive](https://drive.google.com).
   - Click **+ New** &rarr; **File upload**.
   - Select `tests/fixtures/slides/enterprise_architecture_sample.pptx`.

2. **Convert to Native Google Slides**:
   - Locate the uploaded file in Google Drive.
   - Right-click &rarr; **Open with** &rarr; **Google Slides**.
   - In Google Slides, click **File** &rarr; **Save as Google Slides** (if not automatically converted).
   - *Alternative*: Open [Google Slides](https://slides.google.com) &rarr; **Blank Presentation** &rarr; **File** &rarr; **Import slides** &rarr; **Upload** tab &rarr; drag and drop `enterprise_architecture_sample.pptx` &rarr; click **Select all slides** &rarr; **Import slides**.

3. **Grant Access to the Agent**:
   - In the top-right corner of Google Slides, click the **Share** button.
   - Add the agent's identity:
     ```
     aiagent.testing125@gmail.com
     ```
   - Assign **Viewer** permissions (Read-only access is sufficient).
   - *(Optional alternative for testing)*: Under **General access**, select **Anyone with the link** with role **Viewer**.

---

## 3. Obtaining the Presentation ID

Inspect the browser address bar while viewing the Google Slides presentation:

```
https://docs.google.com/presentation/d/<PRESENTATION_ID>/edit#slide=id.p
```

The `<PRESENTATION_ID>` is the 44-character alphanumeric string between `/d/` and `/edit`.

**Example**:
- URL: `https://docs.google.com/presentation/d/10KaQ12JImPARhyWvZP7dAZLMqW62bke8/edit`
- Presentation ID: `10KaQ12JImPARhyWvZP7dAZLMqW62bke8`

---

## 4. Expected Extraction & Persistence Metrics

When ingested by `GoogleSlidesOrchestrator` or `scripts/ingest_slides_manual.py`, the following output should be observed:

| Metric | Expected Value | Details |
| :--- | :---: | :--- |
| **Status** | `SUCCESS` | Complete pipeline success |
| **Final Stage** | `COMPLETED` | All stages completed |
| **Document ID** | `doc_slide_<presentation_id>` | Deterministic Document ID |
| **Slide Count** | `4` | 4 total slides |
| **Total Chunks** | `~6 to 8` | Text, table, and speaker notes chunks |
| **Visual Assets** | `5` | 4 slide thumbnail renders + 1 embedded PNG diagram |
| **Documents Action** | `INSERTED` (Run 1) / `UNCHANGED` (Run 2) | Idempotent document row |
| **Spanner Total Mutations** | `~23 to 27` (Run 1) / `0` (Run 2) | Zero mutations on unchanged rerun |
| **Embedding768** | `None` (`NULL`) | Embeddings deferred to Phase 3D |

---

## 5. Manual CLI Testing Commands

### A. Read-Only Dry Run (Safe Test)
Validates API retrieval, layout extraction, table formatting, and verifies projected Spanner mutations without writing to GCS or Spanner:

```bash
uv run python scripts/ingest_slides_manual.py <PRESENTATION_ID> --dry-run
```

### B. Live Ingestion (When Authorized)
Commits slide renders to GCS Mumbai bucket (`sales-discovery-mvp-mumbai`) and commits all 5 Spanner tables:

```bash
uv run python scripts/ingest_slides_manual.py <PRESENTATION_ID>
```

### C. Idempotency Verification
Run the exact same command a second time:

```bash
uv run python scripts/ingest_slides_manual.py <PRESENTATION_ID>
```
Expected output:
- `Total Mutations: 0`
- `Document Action: UNCHANGED`
- `Skipped Existing: 5`
- `Newly Created: 0`

