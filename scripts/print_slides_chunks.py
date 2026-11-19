"""Print all 10 chunk contents with correct column name TextContent."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"

with db.snapshot() as s:
    rows = list(s.execute_sql("SELECT ChunkId, Modality, TextContent FROM EvidenceChunks WHERE DocumentId = @d ORDER BY ChunkId", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    for r in rows:
        print(f"=== CHUNK: {r[0]} ({r[1]}) ===")
        print(r[2])
        print()
