"""Verification script comparing projected mutations with writer buffered mutations.

Simulates Spanner read-write transaction without committing.
"""

import os
import sys
import json

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from app.enrichment_orchestrator import EnrichmentOrchestrator
from common.database.enrichment_writer import (
    EnrichmentWriter,
    EnrichmentPayload,
    EnrichmentWriteResult,
    EXPECTED_EMBEDDING_DIMENSIONS,
)

doc_id = "1trxQDZDEGA23IHBPHFt84HLggBUZNelE"
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
orch = EnrichmentOrchestrator(spanner_database=db)

# 1. Run dry-run via orchestrator to get metadata_result and projected_table_mutations
res = orch.run(document_id=doc_id, dry_run=True)

print("=== ORCHESTRATOR DRY RUN RESULT ===")
print(f"Projected Total: {res.projected_mutations}")
for tbl, cnt in res.projected_table_mutations.items():
    print(f"  {tbl:<24}: {cnt}")
print(f"Entities: {[e.name for e in res.metadata_result.entities]}")
print(f"Relationships: {[(r.source_name, r.relationship_type, r.target_name) for r in res.metadata_result.relationships]}")

# 2. Build EnrichmentPayload exactly as orchestrator would
payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings={c.chunk_id: [0.01]*768 for c in res.metadata_result.solution_cards + [type('Chunk', (), {'chunk_id': cid}) for cid in ['chunk-b3c8975e69d777630b3fa1965725079b29e7bae223495616', 'chunk-6c14308c253144dc69a9dceb32f129a82ecf291a39dd18a1', 'chunk-dd3a03b7f86bce60050ddccc6cd5996d64ce252af1dab165', 'chunk-aafaa843411012e291b4e1f6314d4d3b8c26080e4470a5e3', 'chunk-b1fca66bab77ecd1dde4e755f419b82894af08c68fedd09e', 'chunk-5f9fde6f19dc3985472474fec18291eeae4b20b77744f5db', 'chunk-9f3c1d4c4d4dc3af41fbfb84ef70b592b5a2b4109127289d', 'chunk-bce7f1f4de49cdfb59b6eabb66c25e18b84d7acb3de8a874', 'chunk-dd0d4aed31a4f52c50b242bfcdbdc34d9ae980a52ed21421', 'chunk-1120af6fa0f1282219bb538f1fe0223c2e9d026726ac43df', 'chunk-2322bc9bef135f9e9074f73cf6d7af55ca8f6fa6306904af', 'chunk-f993d970a144417d2dc9b50a42f01a7c226f54273e57eb88', 'chunk-12c9616df6e7d30ed7cc5813fa771f0b889fbd3077ab3e66']]},
    metadata_result=res.metadata_result,
    sidecar_uri=res.sidecar_uri,
    sidecar_sha256=res.sidecar_sha256,
)

# 3. Simulate writer persistence using a Mock transaction capturing buffered calls against real snapshot reads
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

writer = EnrichmentWriter(database=db)

with db.snapshot(multi_use=True) as snap:
    mock_txn = TrackingTransaction(snap)
    writer_res = writer._enrichment_transaction(mock_txn, payload, force_recompute=False)

print("\n=== WRITER BUFFERED MUTATIONS ===")
print(f"Writer Total: {writer_res.total_mutations}")
for tbl, cnt in mock_txn.buffered_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

print("\n=== COMPARISON PER TABLE ===")
all_match = True
for tbl in res.projected_table_mutations:
    proj = res.projected_table_mutations[tbl]
    buff = mock_txn.buffered_mutations[tbl]
    match = (proj == buff)
    if not match:
        all_match = False
    print(f"  {tbl:<24}: Projected={proj}, Buffered={buff}, Match={match}")

print(f"\nAll Table Mutation Counts Agree: {all_match}")

