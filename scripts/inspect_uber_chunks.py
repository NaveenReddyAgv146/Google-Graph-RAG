"""Inspect all text chunks of Uber document 1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
s_client = spanner.Client(project=settings.project_id)
db = s_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

with db.snapshot(multi_use=True) as s:
    rows = list(s.execute_sql(
        "SELECT ChunkId, PageNumber, TextContent FROM EvidenceChunks WHERE DocumentId = @d AND Modality = 'TEXT' ORDER BY PageNumber, ChunkId",
        params={"d": doc_id},
        param_types={"d": spanner.param_types.STRING}
    ))
    for r in rows:
        print(f"=== CHUNK {r[0]} (Page {r[1]}) ===")
        print(r[2])
        print()

