"""Independent post-enrichment verification script for Uber - 2008 (1).pdf (1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_)."""

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

from common.config import settings
from google.cloud import spanner, storage
from app.enrichment_orchestrator import EnrichmentOrchestrator

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
m2_doc_id = "10KaQ12JImPARhyWvZP7dAZLMqW62bke8"
yt_doc_id = "1S6i9SbkYHMAAEjmrliK-o5Z2CVjKGGXM"
canva_doc_id = "1trxQDZDEGA23IHBPHFt84HLggBUZNelE"
dropbox_doc_id = "1vInD5VR3LESLkb_-n4wu5uqu2716qIOT"
expected_sha256 = "e2ba34fa770c14809fb18620d40ab081f10c776310456ec70f7e2fdc9b77d3fc"

print(f"=== POST-ENRICHMENT INDEPENDENT VERIFICATION FOR {doc_id} ===")
spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

results = {}

with db.snapshot(multi_use=True) as s:
    # 1. Documents.Status = ENRICHED
    doc_row = list(s.execute_sql("SELECT DocumentId, Status FROM Documents WHERE DocumentId = @d", params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))[0]
    print(f"1. Documents: DocumentId={doc_row[0]}, Status={doc_row[1]}")
    results["doc_status_enriched"] = (doc_row[1] == "ENRICHED")

    # 2. Eligible Chunks (25 TEXT) have valid 768-dim embeddings
    # 3. Excluded Chunks (50 IMAGE) have NULL embeddings
    chunk_rows = list(s.execute_sql("""
        SELECT ChunkId, Modality, Embedding768 IS NOT NULL, 
               ARRAY_LENGTH(Embedding768)
        FROM EvidenceChunks 
        WHERE DocumentId = @d 
        ORDER BY Modality, ChunkId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    
    text_chunks = [r for r in chunk_rows if r[1] == "TEXT"]
    image_chunks = [r for r in chunk_rows if r[1] == "IMAGE"]
    
    print(f"2. TEXT Chunks count: {len(text_chunks)}")
    results["text_chunks_embedded"] = (len(text_chunks) == 25 and all(tc[2] is True and tc[3] == 768 for tc in text_chunks))

    print(f"3. IMAGE Chunks count: {len(image_chunks)}")
    results["image_chunks_null"] = (len(image_chunks) == 50 and all(ic[2] is False for ic in image_chunks))

    # 4. Both cards have matching SolutionImplementsDoc edges
    # 5. Both cards share canonical OkfUri and SHA-256
    cards = list(s.execute_sql("""
        SELECT CardId, ProjectName, BusinessProblem, ProposedSolution, ImplementedSolution, 
               Outcome, VerificationStatus, OkfUri 
        FROM SolutionCards 
        WHERE DocumentId = @d 
        ORDER BY CardId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"4 & 5. SolutionCards count: {len(cards)}")
    for c in cards:
        print(f"   - CardId={c[0]}")
        print(f"     ProjectName={c[1]}")
        print(f"     VerificationStatus={c[6]}")
        print(f"     OkfUri={c[7]}")

    sids = list(s.execute_sql("""
        SELECT CardId, DocumentId 
        FROM SolutionImplementsDoc 
        WHERE DocumentId = @d 
        ORDER BY CardId
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"SolutionImplementsDoc count: {len(sids)}")
    for sid in sids:
        print(f"   - Edge: CardId={sid[0]} -> DocId={sid[1]}")

    card_ids = {c[0] for c in cards}
    sid_card_ids = {s[0] for s in sids}
    results["both_cards_have_sid"] = (len(cards) == 2 and card_ids == sid_card_ids)

    # Legacy card preservation check
    legacy_card = next((c for c in cards if c[0] == "card-d3b68c3cfa3e49b7545cef9844356206f7bb9a9f9df5703c"), None)
    results["legacy_card_preserved"] = (legacy_card is not None and legacy_card[6] == "DRAFT")

    # Check OkfUris
    okf_uris = [c[7] for c in cards]
    expected_okf = f"gs://{settings.gcs_mumbai_bucket}/metadata/{doc_id}.meta.json#sha256={expected_sha256}"
    results["both_cards_share_okf_uri"] = (len(set(okf_uris)) == 1 and okf_uris[0] == expected_okf)

    # 6. Check Entities in Spanner
    ent_rows = list(s.execute_sql("""
        SELECT EntityId, Name, Category FROM Entities WHERE EntityId IN (
            'entity_ubercab', 'entity_ubercab_apps', 'entity_ubercab_com',
            'entity_google_maps', 'entity_medallion_system', 'entity_yellowcab'
        )
        ORDER BY Name
    """))
    print(f"6. Entities count: {len(ent_rows)} (expected 6)")
    results["entities_persisted"] = (len(ent_rows) == 6)

    # Check unresolved technologies (GPS, Paypal, SMS, blackberry, iphone, symbian) produce 0 in Technologies table
    unres_rows = list(s.execute_sql("""
        SELECT TechId, Name FROM Technologies
        WHERE LOWER(Name) IN ('gps', 'paypal', 'sms', 'blackberry', 'iphone', 'symbian')
    """))
    print(f"   Unresolved tech in Technologies table (should be 0): {len(unres_rows)}")
    results["unresolved_techs_not_canonical"] = (len(unres_rows) == 0)

    # 7. Check EntityReferencesEntity (1 USES relationship: UberCab.com -> Google Maps)
    ere_rows = list(s.execute_sql("""
        SELECT ere.SourceEntityId, se.Name, ere.TargetEntityId, te.Name
        FROM EntityReferencesEntity ere
        JOIN Entities se ON ere.SourceEntityId = se.EntityId
        JOIN Entities te ON ere.TargetEntityId = te.EntityId
        WHERE se.Name = 'UberCab.com' AND te.Name = 'Google Maps'
    """))
    print(f"7. EntityReferencesEntity count: {len(ere_rows)}")
    for er in ere_rows:
        print(f"   - {er[1]} --[REFERENCES]--> {er[3]}")
    results["uses_relationship_persisted"] = (len(ere_rows) == 1)

    # 8. EvidenceLinks
    ev_links = list(s.execute_sql("""
        SELECT EvidenceId, CardId, ChunkId, DocumentId, Note 
        FROM EvidenceLinks 
        WHERE DocumentId = @d
    """, params={"d": doc_id}, param_types={"d": spanner.param_types.STRING}))
    print(f"8. EvidenceLinks count: {len(ev_links)}")
    for el in ev_links:
        print(f"   - EvidenceId={el[0]}, CardId={el[1]}, ChunkId={el[2]}")
    results["evidence_links_persisted"] = (len(ev_links) == 1)

# 9. Canonical GCS object exists and its digest matches Spanner
canonical_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
staging_blob = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp")
can_exists = canonical_blob.exists()
stg_exists = staging_blob.exists()

print(f"9. Canonical GCS sidecar exists: {can_exists}")
print(f"   Staging GCS sidecar exists (should be False): {stg_exists}")
if can_exists:
    content = canonical_blob.download_as_bytes()
    actual_gcs_hash = hashlib.sha256(content).hexdigest()
    print(f"   GCS sidecar SHA-256:    {actual_gcs_hash}")
    print(f"   Expected SHA-256:       {expected_sha256}")
    results["gcs_hash_matches_expected"] = (actual_gcs_hash == expected_sha256)
    results["staging_cleaned"] = (not stg_exists)

    # Validate citations against manifest
    manifest_data = json.loads(content.decode("utf-8"))
    manifest_cards = manifest_data.get("solution_cards", [])
    print(f"   Manifest solution cards: {len(manifest_cards)}")
    for mc in manifest_cards:
        print(f"     Card in manifest: {mc.get('card_id')} -> Chunk: {mc.get('chunk_id')}")
    
    manifest_cids = {mc.get("card_id") for mc in manifest_cards}
    ev_cids = {el[1] for el in ev_links}
    results["citations_match_manifest"] = (manifest_cids.issubset(ev_cids) and len(ev_links) > 0)

# 10. Test Checkpoint directly via EnrichmentOrchestrator
orch = EnrichmentOrchestrator(spanner_database=db, storage_client=storage_client)
cp_passed, cp_details = orch.check_completion_checkpoint(doc_id)
print("\n=== CHECKPOINT DIRECT EVALUATION ===")
print(f"check_completion_checkpoint passed: {cp_passed}")
for k, v in cp_details.items():
    print(f"  {k}: {v}")
results["checkpoint_passed"] = cp_passed

# 11. Verify unrelated M2, YouTube, Canva, and Dropbox documents remain untouched
m2_passed, _ = orch.check_completion_checkpoint(m2_doc_id)
print(f"\n=== M2 REGRESSION CHECK: {m2_doc_id} -> {m2_passed} ===")
results["m2_unaffected"] = m2_passed

yt_passed, _ = orch.check_completion_checkpoint(yt_doc_id)
print(f"=== YOUTUBE REGRESSION CHECK: {yt_doc_id} -> {yt_passed} ===")
results["youtube_unaffected"] = yt_passed

canva_passed, _ = orch.check_completion_checkpoint(canva_doc_id)
print(f"=== CANVA REGRESSION CHECK: {canva_doc_id} -> {canva_passed} ===")
results["canva_unaffected"] = canva_passed

dropbox_passed, _ = orch.check_completion_checkpoint(dropbox_doc_id)
print(f"=== DROPBOX REGRESSION CHECK: {dropbox_doc_id} -> {dropbox_passed} ===")
results["dropbox_unaffected"] = dropbox_passed

print("\n=== SUMMARY OF VERIFICATION CHECKS ===")
for k, v in results.items():
    print(f"  {k}: {v}")

if not all(results.values()):
    print("\nVERIFICATION CHECKS FAILED!")
    sys.exit(1)
print("\nALL POST-ENRICHMENT VERIFICATION CHECKS PASSED!")

