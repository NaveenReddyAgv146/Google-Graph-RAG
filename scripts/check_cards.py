"""Check existing SolutionCards in Spanner."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner

db = spanner.Client(project=settings.project_id).instance(settings.spanner_instance_id).database(settings.spanner_database_id)
with db.snapshot() as snap:
    rows = list(snap.execute_sql("SELECT CardId, DocumentId, VerificationStatus FROM SolutionCards ORDER BY DocumentId"))
    for r in rows:
        print(f"CardId: {r[0]} | DocId: {r[1]} | Status: {r[2]}")
