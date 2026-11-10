"""Common configuration and environment settings for Enterprise GraphRAG."""

import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    """Project-wide configuration settings."""

    # Google Cloud Core Settings
    project_id: str = os.getenv("GOOGLE_CLOUD_PROJECT", "xenon-broker-513704-s9")
    region: str = os.getenv("GOOGLE_CLOUD_REGION", "asia-south1")

    # Cloud Spanner Graph
    spanner_instance_id: str = os.getenv("SPANNER_INSTANCE_ID", "sales-discovery-trial")
    spanner_database_id: str = os.getenv("SPANNER_DATABASE_ID", "sales-knowledge-db")
    graph_name: str = os.getenv("SPANNER_GRAPH_NAME", "EnterpriseKnowledgeGraph")

    # Cloud Storage Buckets
    gcs_legacy_bucket: str = os.getenv("GCS_LEGACY_BUCKET", "sales-discovery-mvp")
    gcs_mumbai_bucket: str = os.getenv("GCS_MUMBAI_BUCKET", "sales-discovery-mvp-mumbai")

    # Vertex AI Foundation Models
    reasoning_model: str = os.getenv("REASONING_MODEL", "gemini-3.5-flash")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-005")
    embedding_dimensions: int = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))

    # Ingestion Copilot Pub/Sub Trigger
    ingestion_pubsub_topic: str = os.getenv(
        "INGESTION_PUBSUB_TOPIC", "projects/xenon-broker-513704-s9/topics/ingestion-events"
    )
    ingestion_pubsub_sub: str = os.getenv(
        "INGESTION_PUBSUB_SUB", "projects/xenon-broker-513704-s9/subscriptions/ingestion-worker-sub"
    )

settings = Settings()

