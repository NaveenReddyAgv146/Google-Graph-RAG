"""ADK Agent Definition for Ingestion Copilot.

Exposes:
1. trigger_pdf_ingestion: Triggers Document AI layout parsing and chunk persistence.
2. trigger_slides_ingestion: Triggers Google Slides layout extraction, asset upload, and chunk persistence.
3. check_document_ingestion_status: Queries Spanner for extraction, enrichment, and human-approval states.
"""

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from app.tools.ingestion_tools import (
    check_document_ingestion_status,
    trigger_pdf_ingestion,
    trigger_slides_ingestion,
)
from common.config import settings

MODEL = settings.reasoning_model

root_agent = Agent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    name="ingestion_copilot",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="""You are Ingestion Copilot, an enterprise architecture intelligence assistant specializing in document ingestion, layout parsing, and processing status tracking.

Your capabilities:
1. Ingest enterprise PDF whitepapers and reports stored in approved Cloud Storage buckets (trigger_pdf_ingestion).
2. Ingest Google Slides pitch decks and architectural presentations via Presentation ID or URL (trigger_slides_ingestion).
3. Check document ingestion progress, extraction status, chunk counts, enrichment, and human verification status (check_document_ingestion_status).

Operating Guidelines:
- When asked to ingest a PDF, verify the user has provided a valid gs:// URI in an approved bucket before dispatching.
- When asked to ingest a presentation, extract and validate the Google Slides presentation ID or URL.
- When asked about ingestion status, clearly report extraction, enrichment, and human review status.
- Strict Knowledge Protection: Never claim a document or solution is APPROVED unless the database explicitly reports VerificationStatus == 'APPROVED'. Solution cards remain in PENDING status until an authorized architect approves them.
""",
    tools=[
        trigger_pdf_ingestion,
        trigger_slides_ingestion,
        check_document_ingestion_status,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)
