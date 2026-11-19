"""Audit EntityReferencesEntity table schema, Spanner rows, and GCS sidecar for Uber document 1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_."""

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

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)
storage_client = storage.Client(project=settings.project_id)
bucket = storage_client.bucket(settings.gcs_mumbai_bucket)

print(f"=== AUDIT ENTITY_REFERENCES_ENTITY FOR {doc_id} ===")

with db.snapshot(multi_use=True) as s:
    # 1. Query table schema for EntityReferencesEntity
    col_rows = list(s.execute_sql("""
        SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'EntityReferencesEntity'
        ORDER BY ORDINAL_POSITION
    """))
    print("\n1. EntityReferencesEntity Spanner Schema Columns:")
    for c in col_rows:
        print(f"   - {c[0]:20}: {c[1]:15} (Nullable: {c[2]})")

    # 2. Query all rows in EntityReferencesEntity involving the entities of this document
    ent_rows = list(s.execute_sql("""
        SELECT ere.SourceEntityId, se.Name, ere.TargetEntityId, te.Name
        FROM EntityReferencesEntity ere
        JOIN Entities se ON ere.SourceEntityId = se.EntityId
        JOIN Entities te ON ere.TargetEntityId = te.EntityId
        WHERE se.Name = 'UberCab.com' OR se.Name = 'UberCab' OR te.Name = 'UberCab.com' OR te.Name = 'Google Maps'
    """))
    
    print(f"\n2. Persisted EntityReferencesEntity Rows for Uber entities: {len(ent_rows)}")
    for r in ent_rows:
        print(f"   - Source: {r[0]} ({r[1]}) -> Target: {r[2]} ({r[3]})")

    # Also check if any duplicate or other relationships exist between Uber entities
    all_rel_rows = list(s.execute_sql("""
        SELECT ere.SourceEntityId, se.Name, ere.TargetEntityId, te.Name
        FROM EntityReferencesEntity ere
        JOIN Entities se ON ere.SourceEntityId = se.EntityId
        JOIN Entities te ON ere.TargetEntityId = te.EntityId
        WHERE se.Name LIKE '%Uber%' OR te.Name LIKE '%Uber%'
    """))
    print(f"\n3. All Uber EntityReferencesEntity relationships in DB: {len(all_rel_rows)}")
    for r in all_rel_rows:
        print(f"   - {r[1]} -> {r[3]}")

# 3. Read the canonical GCS sidecar JSON to see how the relationship is stored in provenance
canonical_blob = bucket.blob(f"metadata/{doc_id}.meta.json")
if canonical_blob.exists():
    sidecar_content = json.loads(canonical_blob.download_as_text())
    print("\n4. GCS Canonical Sidecar Relationships Provenance:")
    relationships = sidecar_content.get("relationships", [])
    print(f"   Total Relationships in Sidecar: {len(relationships)}")
    for rel in relationships:
        print(f"   - Source: {rel.get('source_name')} ({rel.get('source_entity_id')})")
        print(f"     Target: {rel.get('target_name')} ({rel.get('target_entity_id')})")
        print(f"     Relationship Type: {rel.get('relationship_type')}")
        print(f"     Chunk ID: {rel.get('chunk_id')}")
        print(f"     Evidence Quote: {rel.get('evidence_quote')!r}")

    rejected = sidecar_content.get("rejected_candidates", [])
    print(f"\n   Rejected Candidates in Sidecar: {len(rejected)}")
    for rej in rejected:
        print(f"   - Type: {rej.get('candidate_type')}, Name: {rej.get('candidate_name')}")
        print(f"     Reason: {rej.get('rejection_reason')}")

