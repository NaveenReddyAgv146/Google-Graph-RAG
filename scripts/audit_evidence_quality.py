"""Evidence Quality Audit for Document 1S6i9SbkYHMAAEjmrliK-o5Z2CVjKGGXM.

Performs read-only inspection of source chunks, compares quotes,
checks taxonomy resolution, and audits SolutionCard grounding.
"""

from __future__ import annotations

import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from common.taxonomy import TaxonomyMapper

doc_id = "1S6i9SbkYHMAAEjmrliK-o5Z2CVjKGGXM"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

with db.snapshot() as s:
    rows = list(
        s.execute_sql(
            "SELECT ChunkId, PageNumber, TextContent, Modality FROM EvidenceChunks WHERE DocumentId = @d ORDER BY PageNumber, ChunkId",
            params={"d": doc_id},
            param_types={"d": spanner.param_types.STRING},
        )
    )

chunks = {r[0]: {"page": r[1], "text": r[2], "modality": r[3]} for r in rows}

print(f"Total chunks fetched: {len(chunks)}")
for cid, info in chunks.items():
    if info["modality"] == "TEXT":
        print(f"\n--- {cid} (Page {info['page']}) ---")
        print(info["text"])

# Taxonomy resolution check
mapper = TaxonomyMapper()
tech_queries = [
    "Flash Video",
    "Broadband Internet",
    "Digital video recording technology",
]
print("\n=== TAXONOMY RESOLUTION CHECK ===")
for t in tech_queries:
    entry = mapper.resolve_technology(t, strict=False)
    if entry:
        print(f"Query: '{t}' -> RESOLVED: TechId={entry.tech_id}, Canonical={entry.canonical_name}, Category={entry.category}")
    else:
        print(f"Query: '{t}' -> UNRESOLVED: TechId=None, Not in catalog")

