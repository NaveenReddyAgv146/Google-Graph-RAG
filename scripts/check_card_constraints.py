"""Check constraints on SolutionCards and EvidenceLinks."""
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
        SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        LEFT JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE tc.TABLE_NAME IN ('SolutionCards', 'EvidenceLinks', 'SolutionImplementsDoc', 'SolutionSupportsTech')
        ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME
    """))
    print("=== CONSTRAINTS ===")
    for r in rows:
        print(f"  {r[0]:22} {r[1]:30} {r[2]:15} {r[3]}")
