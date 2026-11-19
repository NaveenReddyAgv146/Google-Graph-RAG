"""Verify idempotency of Google Slides enrichment."""
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import google.auth
from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"

creds, _ = google.auth.default()
spanner_client = spanner.Client(project=settings.project_id, credentials=creds)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id, credentials=creds)

orch = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)
result = orch.run(doc_id, force_recompute=False)
print(f"Idempotency Test Result for {doc_id}:")
print(f"  Status: {result.status}")
print(f"  Projected Mutations: {result.projected_mutations}")
print(f"  Sidecar URI: {result.sidecar_uri}")
assert result.status.value == "SKIPPED", f"Expected SKIPPED, got {result.status}"
print("IDEMPOTENCY TEST PASSED 100%!")
