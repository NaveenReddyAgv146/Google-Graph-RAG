"""Unit tests for common configuration."""

from common.config import settings


def test_default_config():
    assert settings.project_id == "xenon-broker-513704-s9"
    assert settings.region == "asia-south1"
    assert settings.spanner_instance_id == "sales-discovery-trial"
    assert settings.spanner_database_id == "sales-knowledge-db"
    assert settings.embedding_dimensions == 768
    assert settings.documentai_location == "us"
    assert settings.documentai_processor_id == "d79bb791cfba8a30"
    assert settings.documentai_endpoint == "us-documentai.googleapis.com"
    assert (
        settings.documentai_processor_name
        == "projects/xenon-broker-513704-s9/locations/us/processors/d79bb791cfba8a30"
    )


def test_custom_settings():
    from common.config import Settings

    custom = Settings(
        documentai_location="us-central1",
        documentai_processor_id="test-proc-123",
        documentai_endpoint="us-central1-documentai.googleapis.com",
    )
    assert custom.documentai_location == "us-central1"
    assert custom.documentai_processor_id == "test-proc-123"
    assert custom.documentai_endpoint == "us-central1-documentai.googleapis.com"
    assert (
        custom.documentai_processor_name
        == "projects/xenon-broker-513704-s9/locations/us-central1/processors/test-proc-123"
    )
