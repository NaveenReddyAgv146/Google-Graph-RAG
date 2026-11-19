"""Independent post-enrichment verification script for enterprise_architecture_sample.gslides (doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ)."""

import hashlib
import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import google.auth
from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
expected_sha256 = "c379116a059b5f1383d117ecd187c833fbbb0a8a7727991cd99e0d2d031c9f9c"
expected_okf_uri = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json#sha256={expected_sha256}"

print(f"=== INDEPENDENT POST-ENRICHMENT VERIFICATION FOR {doc_id} ===")
creds, _ = google.auth.default()
spanner_client = spanner.Client(project=settings.project_id, credentials=creds)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id, credentials=creds)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

results = {}

with db.snapshot(multi_use=True) as s:
    # 1. Document Status == ENRICHED
    doc_rows = list(s.execute_sql("SELECT DocumentId, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    doc = doc_rows[0]
    print(f"1. Documents: Status={doc[1]}")
    results["doc_status_enriched"] = (doc[1] == "ENRICHED")

    # 2. EvidenceChunks: exactly 10 embedded chunks with 768 dims
    chunk_rows = list(s.execute_sql("SELECT ChunkId, Modality, ARRAY_LENGTH(Embedding768) FROM EvidenceChunks WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"2. EvidenceChunks embedded: {len([r for r in chunk_rows if r[2] == 768])} / {len(chunk_rows)}")
    results["all_chunks_embedded"] = (len(chunk_rows) == 10 and all(r[2] == 768 for r in chunk_rows))

    # 3. VisualAssets intact (5 rows)
    va_rows = list(s.execute_sql("SELECT VisualId FROM VisualAssets WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"3. VisualAssets intact: {len(va_rows)} (expected 5)")
    results["visual_assets_intact"] = (len(va_rows) == 5)

    # 4. Entities: 5 rows
    ent_rows = list(s.execute_sql("SELECT COUNT(1) FROM ChunkReferencesEntity cre JOIN EvidenceChunks ec ON cre.ChunkId = ec.ChunkId WHERE ec.DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"4. ChunkReferencesEntity count: {ent_rows[0][0]} (expected 5)")
    results["chunk_refs_entity_5"] = (ent_rows[0][0] == 5)

    # 5. Technologies: 3 rows mentioned
    cmt_rows = list(s.execute_sql("SELECT COUNT(1) FROM ChunkMentionsTech cmt JOIN EvidenceChunks ec ON cmt.ChunkId = ec.ChunkId WHERE ec.DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"5. ChunkMentionsTech count: {cmt_rows[0][0]} (expected 3)")
    results["chunk_mentions_tech_3"] = (cmt_rows[0][0] == 3)

    # 6. SolutionCards: 1 row with PENDING and OkfUri matching expected
    card_rows = list(s.execute_sql("SELECT CardId, VerificationStatus, OkfUri FROM SolutionCards WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    card = card_rows[0]
    print(f"6. SolutionCard: CardId={card[0]}, Status={card[1]}, OkfUri={card[2]}")
    results["card_count_1"] = (len(card_rows) == 1)
    results["card_status_pending"] = (card[1] == "PENDING")
    results["card_okf_uri_match"] = (card[2] == expected_okf_uri)

    # 7. SolutionImplementsDoc: 1 edge
    sid_rows = list(s.execute_sql("SELECT CardId, DocumentId FROM SolutionImplementsDoc WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"7. SolutionImplementsDoc count: {len(sid_rows)} (expected 1)")
    results["implements_doc_1"] = (len(sid_rows) == 1 and sid_rows[0][0] == card[0])

    # 8. SolutionSupportsTech: 3 edges
    sst_rows = list(s.execute_sql("SELECT TechId FROM SolutionSupportsTech WHERE CardId = @c", params={"c": card[0]}, param_types={"c": spanner.param_types.STRING}))
    print(f"8. SolutionSupportsTech count: {len(sst_rows)} (expected 3)")
    results["supports_tech_3"] = (len(sst_rows) == 3)

    # 9. EvidenceLinks: 1 record
    evl_rows = list(s.execute_sql("SELECT EvidenceId, CardId, ChunkId FROM EvidenceLinks WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"9. EvidenceLinks count: {len(evl_rows)} (expected 1)")
    results["evidence_links_1"] = (len(evl_rows) == 1 and evl_rows[0][1] == card[0])

# 10. Check GCS sidecars
can_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
stg_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
can_exists = can_blob.exists()
stg_exists = stg_blob.exists()
print(f"10. Canonical sidecar exists: {can_exists}")
print(f"    Staging sidecar exists: {stg_exists} (expected False)")
results["canonical_sidecar_exists"] = can_exists
results["staging_sidecar_deleted"] = (not stg_exists)

if can_exists:
    can_bytes = can_blob.download_as_bytes()
    can_hash = hashlib.sha256(can_bytes).hexdigest()
    print(f"    Canonical sidecar SHA-256: {can_hash}")
    results["canonical_hash_match"] = (can_hash == expected_sha256)

# 11. Run orchestrator four-point completion checkpoint
orchestrator = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)
cp_passed, cp_details = orchestrator.check_completion_checkpoint(doc_id)
print(f"\n11. Four-Point Checkpoint Result: {cp_passed}")
for pt, val in cp_details.items():
    print(f"    - {pt}: {val}")
results["four_point_checkpoint_passed"] = cp_passed

print("\n=== POST-ENRICHMENT VERIFICATION SUMMARY ===")
all_passed = True
for k, v in results.items():
    print(f"  {k}: {v}")
    if not v: all_passed = False

if not all_passed:
    print("\nVERIFICATION FAILED!")
    sys.exit(1)
print("\nALL POST-ENRICHMENT VERIFICATIONS PASSED 100%!")
