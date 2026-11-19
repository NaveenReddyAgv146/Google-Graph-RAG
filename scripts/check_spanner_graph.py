"""Check Spanner Graph schema."""
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
client = spanner.Client(project=settings.project_id, credentials=creds)
db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

with db.snapshot(multi_use=True) as s:
    rows = list(s.execute_sql("""
        SELECT PROPERTY_GRAPH_NAME
        FROM INFORMATION_SCHEMA.PROPERTY_GRAPHS
    """))
    print("=== PROPERTY GRAPHS ===")
    for r in rows:
        print(f"  {r[0]}")

    elem_tables = list(s.execute_sql("""
        SELECT ELEMENT_TABLE_NAME, ELEMENT_KIND, SOURCE_ELEMENT_TABLE_NAME, TARGET_ELEMENT_TABLE_NAME
        FROM INFORMATION_SCHEMA.GRAPH_ELEMENT_TABLES
        ORDER BY ELEMENT_KIND, ELEMENT_TABLE_NAME
    """))
    print("\n=== GRAPH ELEMENT TABLES ===")
    for r in elem_tables:
        print(f"  {r[1]:6} {r[0]:25} (Source: {r[2]}, Target: {r[3]})")
