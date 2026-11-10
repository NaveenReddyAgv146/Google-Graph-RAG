"""Unit tests for common configuration."""

from common.config import settings

def test_default_config():
    assert settings.project_id == "xenon-broker-513704-s9"
    assert settings.region == "asia-south1"
    assert settings.spanner_instance_id == "sales-discovery-trial"
    assert settings.spanner_database_id == "sales-knowledge-db"
    assert settings.embedding_dimensions == 768

