"""Inspect entity relationships and compare writer vs orchestrator table-by-table."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from common.database.enrichment_writer import EnrichmentPayload, EnrichmentWriter
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator
from scripts.audit_slides_dryrun import metadata, embeddings, doc_id, timestamp

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage.Client(project=settings.project_id))

canonical_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json"
payload = EnrichmentPayload(
    document_id=doc_id,
    metadata_gcs_uri=canonical_uri,
    entities=metadata.entities,
    technologies=metadata.technologies,
    solution_cards=metadata.solution_cards,
    entity_relationships=metadata.entity_relationships,
    embeddings=embeddings,
)

writer = EnrichmentWriter(db)
mutations = writer.build_mutations(payload)

print(f"Total mutations in writer: {len(mutations)}")
table_counts = {}
for m in mutations:
    # m is a spanner.Mutation object
    # In google-cloud-spanner, table is attribute on mutation
    t = getattr(m, 'table', None) or m._table_name
    table_counts[t] = table_counts.get(t, 0) + 1

for t, c in sorted(table_counts.items()):
    print(f"  {t}: {c}")

print("\nOrchestrator breakdown:")
proj = orchestrator._project_spanner_mutations(doc_id, metadata, embeddings)
for k, v in proj.items():
    print(f"  {k}: {v}")

print("\nEntity relationships in metadata:")
for r in metadata.entity_relationships:
    print(f"  {r.source_entity_id} -> {r.target_entity_id} ({r.relationship_type})")
