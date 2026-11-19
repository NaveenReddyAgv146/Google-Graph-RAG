"""Inspect existing SolutionCards in Spanner."""
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
    cards = list(s.execute_sql("""
        SELECT sc.CardId, sc.DocumentId, d.FileName, sc.ProjectName, sc.VerificationStatus, sc.OkfUri,
               (SELECT COUNT(1) FROM EvidenceLinks el WHERE el.CardId = sc.CardId) as EvidenceCount
        FROM SolutionCards sc
        JOIN Documents d ON sc.DocumentId = d.DocumentId
        ORDER BY sc.DocumentId, sc.CardId
    """))
    print(f"=== CURRENT SOLUTION CARDS IN SPANNER ({len(cards)}) ===")
    for c in cards:
        print(f"CardId: {c[0]}")
        print(f"  DocId:          {c[1]} ({c[2]})")
        print(f"  ProjectName:    {c[3]}")
        print(f"  Status:         {c[4]}")
        print(f"  OkfUri:         {c[5]}")
        print(f"  Evidence Links: {c[6]}")
        print()
