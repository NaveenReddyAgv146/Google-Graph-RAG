"""Inspect Spanner schema for SolutionCards, EvidenceLinks, Reviews."""
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
    tables = [r[0] for r in s.execute_sql("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '' ORDER BY TABLE_NAME")]
    print(f"=== ALL TABLES IN SPANNER ({len(tables)}) ===")
    for t in tables:
        print(f"  {t}")

    print("\n=== COLUMNS FOR SolutionCards ===")
    cols = list(s.execute_sql("SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'SolutionCards' ORDER BY ORDINAL_POSITION"))
    for c in cols:
        print(f"  {c[0]:25} {c[1]:20} (Nullable: {c[2]})")

    print("\n=== COLUMNS FOR EvidenceLinks ===")
    cols = list(s.execute_sql("SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'EvidenceLinks' ORDER BY ORDINAL_POSITION"))
    for c in cols:
        print(f"  {c[0]:25} {c[1]:20} (Nullable: {c[2]})")

    # Let's inspect all SolutionCards currently in Spanner
    print("\n=== CURRENT SOLUTION CARDS IN SPANNER ===")
    cards = list(s.execute_sql("""
        SELECT CardId, DocumentId, ProjectName, VerificationStatus, OkfUri, ConfidenceScore
        FROM SolutionCards
        ORDER BY DocumentId, CardId
    """))
    for card in cards:
        print(f"  CardId: {card[0]}")
        print(f"    DocumentId:         {card[1]}")
        print(f"    ProjectName:        {card[2]}")
        print(f"    VerificationStatus: {card[3]}")
        print(f"    OkfUri:             {card[4]}")
        print(f"    ConfidenceScore:    {card[5]}")
