"""Audit evidence and chunks for Dropbox - 2007 - Pre-Seed.pdf (1vInD5VR3LESLkb_-n4wu5uqu2716qIOT)."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from common.parsers.metadata_extractor import MetadataExtractor

doc_id = "1vInD5VR3LESLkb_-n4wu5uqu2716qIOT"
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

with db.snapshot() as s:
    rows = list(s.execute_sql("""
        SELECT ChunkId, PageNumber, TextContent
        FROM EvidenceChunks
        WHERE DocumentId = @d AND Modality = 'TEXT' AND TextContent IS NOT NULL AND TRIM(TextContent) != ''
        ORDER BY PageNumber, ChunkId
    """, params={'d': doc_id}, param_types={'d': spanner.param_types.STRING}))

    print(f"Total eligible non-empty TEXT chunks: {len(rows)}")
    chunks_dict = {r[0]: (r[1], r[2]) for r in rows}

for cid, (page, text) in chunks_dict.items():
    print(f"\n--- ChunkId: {cid} | Page: {page} | Len: {len(text)} ---")
    print(text.strip())

