"""Reconcile orchestrator projection vs writer-buffered mutations via TrackingTransaction."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator
from common.database.enrichment_writer import EnrichmentWriter, EnrichmentPayload

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
timestamp = "2026-11-16T10:00:00+00:00"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)

orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)

res = orchestrator.run(
    document_id=doc_id,
    dry_run=True,
    force_recompute=True,
    enriched_at=timestamp,
)

print(f"Status: {res.status}")
print(f"Projected total: {res.projected_mutations}")
print(f"Projected table mutations:")
for tbl, cnt in res.projected_table_mutations.items():
    print(f"  {tbl}: {cnt}")

# TrackingTransaction implementation
class TrackingTransaction:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.buffered_mutations = {
            "EvidenceChunks": 0,
            "Entities": 0,
            "Technologies": 0,
            "SolutionCards": 0,
            "ChunkReferencesEntity": 0,
            "ChunkMentionsTech": 0,
            "SolutionSupportsTech": 0,
            "SolutionImplementsDoc": 0,
            "EntityReferencesEntity": 0,
            "EvidenceLinks": 0,
            "Documents": 0,
        }

    def execute_sql(self, sql, params=None, param_types=None):
        return self.snapshot.execute_sql(sql, params=params, param_types=param_types)

    def insert(self, table, columns, values):
        self.buffered_mutations[table] += len(values)

    def update(self, table, columns, values):
        self.buffered_mutations[table] += len(values)

eligible_chunks = orchestrator._read_document_chunks(doc_id)["eligible"]
chunk_texts = [c.text_content for c in eligible_chunks]
raw_embeddings = orchestrator.embeddings_client.embed_texts(chunk_texts)
chunk_embeddings = {chunk.chunk_id: emb for chunk, emb in zip(eligible_chunks, raw_embeddings)}

payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=chunk_embeddings,
    metadata_result=res.metadata_result,
    sidecar_uri=res.sidecar_uri,
    sidecar_sha256=res.sidecar_sha256,
)

writer = EnrichmentWriter(database=db)
with db.snapshot(multi_use=True) as snap:
    mock_txn = TrackingTransaction(snap)
    sim_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

print(f"\nWriter-buffered mutations total: {sim_res.total_mutations}")
for tbl, cnt in sorted(mock_txn.buffered_mutations.items()):
    print(f"  {tbl}: {cnt}")

print("\n--- Table-by-Table Diff ---")
diff = False
for tbl in sorted(mock_txn.buffered_mutations.keys()):
    p = res.projected_table_mutations.get(tbl, 0)
    w = mock_txn.buffered_mutations.get(tbl, 0)
    if p != w:
        print(f"  MISMATCH on {tbl}: Orchestrator={p}, Writer={w}")
        diff = True
    else:
        print(f"  MATCH on {tbl}: {p}")

if not diff and sim_res.total_mutations == res.projected_mutations:
    print(f"\nPERFECT RECONCILIATION: Exactly {sim_res.total_mutations} mutations match across all 11 tables!")
else:
    print(f"\nMISMATCH DETECTED: Orchestrator={res.projected_mutations}, Writer={sim_res.total_mutations}")
    sys.exit(1)
