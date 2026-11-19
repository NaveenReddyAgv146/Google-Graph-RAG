"""Query status across all six corpus documents in Spanner."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import google.auth
from common.config import settings
from google.cloud import spanner

creds, _ = google.auth.default()
db = spanner.Client(project=settings.project_id, credentials=creds).instance(settings.spanner_instance_id).database(settings.spanner_database_id)
with db.snapshot() as snap:
    rows = list(snap.execute_sql("SELECT DocumentId, FileName, Status, SourceUrl FROM Documents ORDER BY DocumentId"))
    print("=== ALL SIX DOCUMENTS CURRENT STATUS ===")
    for r in rows:
        print(f"ID: {r[0]} | Status: {r[2]:<10} | File: {r[1]}")
