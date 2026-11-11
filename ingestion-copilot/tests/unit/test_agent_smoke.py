"""ADK agent initialization and FastAPI application startup smoke tests for Ingestion Copilot."""

from fastapi.testclient import TestClient
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.agent import app as adk_app
from app.agent import get_current_time, get_weather, root_agent
from app.fast_api_app import app as fastapi_app


def test_root_agent_initialization():
    """Verifies that the root agent initializes with correct ADK types and metadata."""
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "ingestion_copilot"
    assert len(root_agent.tools) == 2
    assert "helpful AI assistant" in root_agent.instruction


def test_placeholder_tools_execution():
    """Verifies that registered placeholder tools execute deterministically."""
    weather = get_weather("san francisco")
    assert "60 degrees and foggy" in weather

    time_sf = get_current_time("san francisco")
    assert "The current time" in time_sf

    time_unknown = get_current_time("unknown_city")
    assert "Sorry" in time_unknown


def test_adk_app_and_runner_initialization():
    """Verifies that ADK App and Runner initialize correctly with InMemorySessionService."""
    assert isinstance(adk_app, App)
    assert adk_app.root_agent == root_agent

    session_service = InMemorySessionService()
    session = session_service.create_session_sync(
        user_id="test_user", app_name="test_app"
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
    and exposes OpenAPI and A2A endpoints.
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
