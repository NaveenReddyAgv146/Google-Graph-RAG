"""Test writer buffering for exact 5 entities and 1 relationship."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from common.database.enrichment_writer import (
    EnrichmentWriter,
    EnrichmentPayload,
)
from common.parsers.metadata_extractor import (
    ExtractedMetadataResult,
    ValidatedEntity,
    ValidatedSolutionCard,
    ValidatedEntityRelationship,
)
from common.taxonomy import generate_entity_id

doc_id = "1trxQDZDEGA23IHBPHFt84HLggBUZNelE"
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

entities = [
    ValidatedEntity(
        entity_id=generate_entity_id("BUSINESS_UNIT", "Fusion Books"),
        name="Fusion Books",
        category="BUSINESS_UNIT",
        description=None,
        chunk_id="chunk-1120af6fa0f1282219bb538f1fe0223c2e9d026726ac43df",
        evidence_quote="Melanie Perkins and Cliff  Obrecht founded Fusion Books",
    ),
    ValidatedEntity(
        entity_id=generate_entity_id("BUSINESS_UNIT", "The Canva Team"),
        name="The Canva Team",
        category="BUSINESS_UNIT",
        description=None,
        chunk_id="chunk-2322bc9bef135f9e9074f73cf6d7af55ca8f6fa6306904af",
        evidence_quote="THE CANVA TEAM",
    ),
    ValidatedEntity(
        entity_id=generate_entity_id("CONCEPT", "Freemium model"),
        name="Freemium model",
        category="CONCEPT",
        description=None,
        chunk_id="chunk-dd0d4aed31a4f52c50b242bfcdbdc34d9ae980a52ed21421",
        evidence_quote="CANVA  uses the freemium model,  which ensures  clients  are hooked before",
    ),
    ValidatedEntity(
        entity_id=generate_entity_id("BUSINESS_UNIT", "CANVA"),
        name="CANVA",
        category="BUSINESS_UNIT",
        description=None,
        chunk_id="chunk-aafaa843411012e291b4e1f6314d4d3b8c26080e4470a5e3",
        evidence_quote="CANVA: ORDINARY PEOPLE,\nEXTRAORDINARY DESIGN",
    ),
    ValidatedEntity(
        entity_id=generate_entity_id("SUBSYSTEM", "CANVA editor"),
        name="CANVA editor",
        category="SUBSYSTEM",
        description=None,
        chunk_id="chunk-bce7f1f4de49cdfb59b6eabb66c25e18b84d7acb3de8a874",
        evidence_quote="the user will be lead straight\n\nto the  CANVA editor where the user can start customizing immediately",
    ),
]

solution_card = ValidatedSolutionCard(
    card_id="card_20dea10695fe2cde0f5c343231fa04af7885fccb3382b9b8",
    document_id=doc_id,
    project_name="Canva",
    business_problem="Frustrating design products in a $138.4 billion market.",
    proposed_solution="An easy-to-use design platform for ordinary people to create extraordinary designs.",
    implemented_solution="A freemium-based design platform containing a library of millions of photos and layouts.",
    outcome="Communities build their business and flourish using the platform.",
    chunk_id="chunk-aafaa843411012e291b4e1f6314d4d3b8c26080e4470a5e3",
    evidence_quote="CANVA: ORDINARY PEOPLE,\nEXTRAORDINARY DESIGN",
    technologies=[],
)

relationships = [
    ValidatedEntityRelationship(
        source_entity_id=generate_entity_id("BUSINESS_UNIT", "CANVA"),
        target_entity_id=generate_entity_id("CONCEPT", "Freemium model"),
        source_name="CANVA",
        target_name="Freemium model",
        relationship_type="USES",
        chunk_id="chunk-dd0d4aed31a4f52c50b242bfcdbdc34d9ae980a52ed21421",
        evidence_quote="CANVA  uses the freemium model,  which ensures  clients  are hooked before",
    )
]

text_cids = [
    'chunk-b3c8975e69d777630b3fa1965725079b29e7bae223495616',
    'chunk-6c14308c253144dc69a9dceb32f129a82ecf291a39dd18a1',
    'chunk-dd3a03b7f86bce60050ddccc6cd5996d64ce252af1dab165',
    'chunk-aafaa843411012e291b4e1f6314d4d3b8c26080e4470a5e3',
    'chunk-b1fca66bab77ecd1dde4e755f419b82894af08c68fedd09e',
    'chunk-5f9fde6f19dc3985472474fec18291eeae4b20b77744f5db',
    'chunk-9f3c1d4c4d4dc3af41fbfb84ef70b592b5a2b4109127289d',
    'chunk-bce7f1f4de49cdfb59b6eabb66c25e18b84d7acb3de8a874',
    'chunk-dd0d4aed31a4f52c50b242bfcdbdc34d9ae980a52ed21421',
    'chunk-1120af6fa0f1282219bb538f1fe0223c2e9d026726ac43df',
    'chunk-2322bc9bef135f9e9074f73cf6d7af55ca8f6fa6306904af',
    'chunk-f993d970a144417d2dc9b50a42f01a7c226f54273e57eb88',
    'chunk-12c9616df6e7d30ed7cc5813fa771f0b889fbd3077ab3e66'
]
chunk_embeddings = {cid: [0.01]*768 for cid in text_cids}

metadata_res = ExtractedMetadataResult(
    document_id=doc_id,
    entities=entities,
    technologies=[],
    solution_cards=[solution_card],
    relationships=relationships,
)

payload = EnrichmentPayload.from_metadata_result(
    document_id=doc_id,
    chunk_embeddings=chunk_embeddings,
    metadata_result=metadata_res,
    sidecar_uri=f"gs://sales-discovery-mvp-mumbai/metadata/{doc_id}.meta.json",
    sidecar_sha256="83fe67e0d416ca6f63b9274628b05a19106288138a8b430be4c89e9344642586",
)

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

print("=== WRITER BUFFERED MUTATIONS FOR 5 ENTITIES + 1 RELATIONSHIP ===")
print(f"Total Mutations: {writer_res.total_mutations}")
for tbl, cnt in mock_txn.buffered_mutations.items():
    print(f"  {tbl:<24}: {cnt}")

