"""ADK agent initialization, FastAPI application, and Agent Runtime compatibility smoke tests for Discovery Copilot."""

from agentplatform.frameworks.adk import AdkApp
from fastapi.testclient import TestClient
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from app.agent import app as adk_app
from app.agent import get_current_time, get_weather, root_agent
from app.app_utils import services
from app.fast_api_app import app as fastapi_app


def test_root_agent_initialization():
    """Verifies that the root agent initializes with correct ADK types and metadata."""
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "discovery_copilot"
    assert len(root_agent.tools) == 2
    assert "helpful AI assistant" in root_agent.instruction


def test_placeholder_tools_execution():
    """Verifies that registered placeholder tools execute deterministically."""
    weather = get_weather("san francisco")
    assert "60 degrees and foggy" in weather

    time_sf = get_current_time("san francisco")
    assert "The current time" in time_sf


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


def test_agent_runtime_compatibility():
    """
    Verifies that the agent satisfies the Agent Runtime / Reasoning Engine contract.
    Instantiates AdkApp and verifies registered operations.
    """
    runtime = AdkApp(
        app=adk_app,
        session_service_builder=services.get_session_service,
        artifact_service_builder=services.get_artifact_service,
    )
    runtime.set_up()
    operations = runtime.register_operations()
    assert isinstance(operations, dict)


def test_fastapi_application_startup_and_reasoning_engine_routes():
    """
    Verifies that FastAPI starts up, executes lifespan context manager,
    and exposes OpenAPI, A2A, and Reasoning Engine routes.
    """
    with TestClient(fastapi_app) as client:
        # Check docs endpoint
        res = client.get("/docs")
        assert res.status_code == 200

        # Check openapi.json
        openapi_res = client.get("/openapi.json")
        assert openapi_res.status_code == 200
        schema = openapi_res.json()
        assert schema["info"]["title"] == "discovery-copilot"

        # Verify Reasoning Engine routes exist in routes
        route_paths = [route.path for route in fastapi_app.routes]
        assert "/api/stream_reasoning_engine" in route_paths
        assert "/api/reasoning_engine" in route_paths
