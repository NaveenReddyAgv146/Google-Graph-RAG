"""Check foreign keys on EntityReferencesEntity."""
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner

client = spanner.Client(project=settings.project_id)
db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
with db.snapshot() as s:
    rows = list(s.execute_sql("""
        SELECT tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, tc.TABLE_NAME, kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
        WHERE tc.TABLE_NAME = 'EntityReferencesEntity'
    """))
    for r in rows:
        print(r)
