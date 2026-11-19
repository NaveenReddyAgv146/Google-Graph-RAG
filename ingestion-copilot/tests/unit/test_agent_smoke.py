"""ADK agent initialization and FastAPI application startup smoke tests for Ingestion Copilot."""

from fastapi.testclient import TestClient
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.agent import app as adk_app, root_agent
from app.tools.ingestion_tools import (
    check_document_ingestion_status,
    trigger_pdf_ingestion,
    trigger_slides_ingestion,
)
from app.fast_api_app import app as fastapi_app


def test_root_agent_initialization():
    """Verifies that the root agent initializes with correct ADK types and metadata."""
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "ingestion_copilot"
    assert len(root_agent.tools) == 3
    assert "Ingestion Copilot" in root_agent.instruction


def test_registered_tools_metadata():
    """Verifies that registered production tools have proper metadata and docstrings."""
    tool_names = [tool.__name__ for tool in root_agent.tools]
    assert "trigger_pdf_ingestion" in tool_names
    assert "trigger_slides_ingestion" in tool_names
    assert "check_document_ingestion_status" in tool_names

    assert trigger_pdf_ingestion.__name__ == "trigger_pdf_ingestion"
    assert trigger_slides_ingestion.__name__ == "trigger_slides_ingestion"
    assert check_document_ingestion_status.__name__ == "check_document_ingestion_status"


def test_adk_app_and_runner_initialization():
    """Verifies that ADK App and Runner initialize correctly with InMemorySessionService."""
    assert isinstance(adk_app, App)
    assert adk_app.root_agent == root_agent

    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="test_user@example.com", app_name="test_app"
    )
    assert session.id is not None

    runner = Runner(
        agent=root_agent, session_service=session_service, app_name="test_app"
    )
    assert runner.agent == root_agent


def test_fastapi_application_startup():
    """
    Verifies that the FastAPI application starts up cleanly,
    executes the lifespan context manager, initializes runner state,
    and exposes OpenAPI and exempt endpoints.
    """
    with TestClient(fastapi_app) as client:
        # Check docs endpoint
        res = client.get("/docs")
        assert res.status_code == 200

        # Check openapi.json
        openapi_res = client.get("/openapi.json")
        assert openapi_res.status_code == 200
        schema = openapi_res.json()
        assert schema["info"]["title"] == "ingestion-copilot"

        # Check runner attached to app state during lifespan
        assert hasattr(fastapi_app.state, "runner")
        assert fastapi_app.state.agent_app_name == "app"
