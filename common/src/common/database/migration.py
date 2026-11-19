"""Phase 2C Migration Lifecycle, Backfill, and Concurrency Engine."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

from google.cloud import spanner
from google.api_core.exceptions import Aborted

logger = logging.getLogger("common.database.migration")


class MigrationStateError(Exception):
    """Base exception for invalid migration state transitions."""
    pass


class RollbackRejectedError(MigrationStateError):
    """Raised when rollback is rejected due to batch state (e.g. ACCEPTED)."""
    pass


class AcceptanceRejectedError(MigrationStateError):
    """Raised when acceptance is rejected due to batch state (e.g. ROLLED_BACK)."""
    pass


def backfill_relationship_table(
    transaction,
    batch_id: str,
    target_edge_table: str,
    candidate_query: str,
    key_col1: str,
    key_col2: str,
) -> int:
    """
    Atomically executes inside a Spanner serializable read-write transaction:
    1. Reads candidate pairs.
    2. Reads existing edges.
    3. Filters for strictly missing pairs.
    4. Buffers insert mutations for missing edges.
    5. Buffers MigrationManifest audit records.
    """
    candidates = [tuple(r) for r in transaction.execute_sql(candidate_query)]
    if not candidates:
        logger.info(f"{target_edge_table}: No candidates returned.")
        return 0

    existing_sql = f"SELECT {key_col1}, {key_col2} FROM {target_edge_table}"
    existing_edges = set(tuple(r) for r in transaction.execute_sql(existing_sql))

    missing_edges = [pair for pair in candidates if pair not in existing_edges]
    if not missing_edges:
        logger.info(f"{target_edge_table}: All {len(candidates)} edges already exist.")
        return 0

    # Buffer edge mutations
    transaction.insert(
        target_edge_table,
        columns=[key_col1, key_col2],
        values=missing_edges,
    )

    # Buffer manifest rows strictly matching newly inserted edges
    manifest_rows = [
        (batch_id, target_edge_table, f"{k1}#{k2}", spanner.COMMIT_TIMESTAMP)
        for k1, k2 in missing_edges
    ]
    transaction.insert(
        "MigrationManifest",
        columns=["BatchId", "TableName", "RecordKey", "InsertedAt"],
        values=manifest_rows,
    )

    logger.info(
        f"{target_edge_table}: Buffered {len(missing_edges)} new edges and manifest records."
    )
    return len(missing_edges)


def execute_phase2_backfill(
    database,
    batch_id: str = "BATCH_BACKFILL_20261111_01",
) -> dict[str, Any]:
    """
    Stage 2 Migration Transaction:
    Atomically backfills missing relationships, logs individual edge keys in
    MigrationManifest, and creates MigrationBatches in 'PENDING_VALIDATION' state.
    
    Invariants Enforced:
    - Entire operation executes in a single serializable read-write transaction.
    - Zero existing rows are overwritten or deleted.
    - Re-verifies foreign key referential integrity inside transaction.
    - Leaves batch in 'PENDING_VALIDATION' state (NOT ACCEPTED).
    """
    def _backfill_txn(transaction):
        # 1. Verify BatchId does not already exist
        check_batch_sql = "SELECT State FROM MigrationBatches WHERE BatchId = @batch_id"
        existing_batch = list(transaction.execute_sql(
            check_batch_sql,
            params={"batch_id": batch_id},
            param_types={"batch_id": spanner.param_types.STRING},
        ))
        if existing_batch:
            raise MigrationStateError(
                f"Batch '{batch_id}' already exists in state '{existing_batch[0][0]}'. Aborting."
            )

        # 2. Referential integrity pre-checks inside transaction
        orphan_dcc = list(transaction.execute_sql("""
        SELECT COUNT(1) FROM EvidenceChunks c 
        WHERE c.DocumentId IS NOT NULL AND c.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """))[0][0]
        if orphan_dcc > 0:
            raise ValueError(f"Referential integrity failure: {orphan_dcc} orphan chunks found.")

        orphan_vabd = list(transaction.execute_sql("""
        SELECT COUNT(1) FROM VisualAssets v 
        WHERE v.DocumentId IS NOT NULL AND v.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """))[0][0]
        if orphan_vabd > 0:
            raise ValueError(f"Referential integrity failure: {orphan_vabd} orphan visual assets found.")

        orphan_sid = list(transaction.execute_sql("""
        SELECT COUNT(1) FROM SolutionCards s 
        WHERE s.DocumentId IS NOT NULL AND s.DocumentId NOT IN (SELECT DocumentId FROM Documents)
        """))[0][0]
        if orphan_sid > 0:
            raise ValueError(f"Referential integrity failure: {orphan_sid} orphan solution cards found.")

        # 3. Backfill DocumentContainsChunk (expected: 195)
        count_dcc = backfill_relationship_table(
            transaction,
            batch_id=batch_id,
            target_edge_table="DocumentContainsChunk",
            candidate_query="SELECT DISTINCT DocumentId, ChunkId FROM EvidenceChunks WHERE DocumentId IS NOT NULL",
            key_col1="DocumentId",
            key_col2="ChunkId",
        )

        # 4. Backfill VisualAssetBelongsToDoc (expected: 125)
        count_vabd = backfill_relationship_table(
            transaction,
            batch_id=batch_id,
            target_edge_table="VisualAssetBelongsToDoc",
            candidate_query="SELECT DISTINCT VisualId, DocumentId FROM VisualAssets WHERE VisualId IS NOT NULL AND DocumentId IS NOT NULL",
            key_col1="VisualId",
            key_col2="DocumentId",
        )

        # 5. Backfill SolutionImplementsDoc (expected: 5)
        count_sid = backfill_relationship_table(
            transaction,
            batch_id=batch_id,
            target_edge_table="SolutionImplementsDoc",
            candidate_query="SELECT DISTINCT CardId, DocumentId FROM SolutionCards WHERE CardId IS NOT NULL AND DocumentId IS NOT NULL",
            key_col1="CardId",
            key_col2="DocumentId",
        )

        total_inserted = count_dcc + count_vabd + count_sid

        # 6. Insert batch audit header in PENDING_VALIDATION state in exact same commit timestamp
        transaction.insert(
            "MigrationBatches",
            columns=["BatchId", "State", "TotalInsertedEdges", "CreatedAt", "AcceptedAt"],
            values=[(batch_id, "PENDING_VALIDATION", total_inserted, spanner.COMMIT_TIMESTAMP, None)],
        )

        logger.info(
            f"Buffered batch '{batch_id}' with {total_inserted} edges in PENDING_VALIDATION state."
        )
        return {
            "batch_id": batch_id,
            "DocumentContainsChunk": count_dcc,
            "VisualAssetBelongsToDoc": count_vabd,
            "SolutionImplementsDoc": count_sid,
            "total_inserted": total_inserted,
            "state": "PENDING_VALIDATION",
        }

    return database.run_in_transaction(_backfill_txn)


def accept_migration_batch(
    database,
    batch_id: str = "BATCH_BACKFILL_20261111_01",
) -> dict[str, Any]:
    """
    Stage 4 Acceptance Gate: Transitions batch state from 'PENDING_VALIDATION' to 'ACCEPTED'.

    Concurrency Invariants:
    - Runs in a serializable read-write transaction acquiring exclusive locks on MigrationBatches(batch_id).
    - If concurrent with rollback_migration_batch(), one transaction aborts and retries.
    - If rollback commits first, retry sees 'ROLLED_BACK' and raises AcceptanceRejectedError.
    - If acceptance commits first, state becomes 'ACCEPTED', permanently locking rollback.
    """
    def _accept_txn(transaction):
        # 1. Read current state inside transaction (acquires read lock on batch_id)
        res = list(transaction.execute_sql(
            "SELECT State, TotalInsertedEdges, CreatedAt FROM MigrationBatches WHERE BatchId = @batch_id",
            params={"batch_id": batch_id},
            param_types={"batch_id": spanner.param_types.STRING},
        ))
        if not res:
            raise MigrationStateError(f"Batch '{batch_id}' not found in MigrationBatches.")

        current_state, total_inserted, created_at = res[0]
        if current_state == "ACCEPTED":
            logger.info(f"Batch '{batch_id}' is already ACCEPTED.")
            return {
                "batch_id": batch_id,
                "state": "ACCEPTED",
                "total_inserted": total_inserted,
                "created_at": created_at,
                "already_accepted": True,
            }
        if current_state != "PENDING_VALIDATION":
            raise AcceptanceRejectedError(
                f"Cannot accept batch '{batch_id}': current state is '{current_state}', expected 'PENDING_VALIDATION'."
            )

        # 2. Update state to 'ACCEPTED' inside the same transaction (acquires exclusive lock)
        transaction.update(
            "MigrationBatches",
            columns=["BatchId", "State", "AcceptedAt"],
            values=[(batch_id, "ACCEPTED", spanner.COMMIT_TIMESTAMP)],
        )
        logger.info(f"Batch '{batch_id}' transitioning to ACCEPTED.")
        return {
            "batch_id": batch_id,
            "state": "ACCEPTED",
            "total_inserted": total_inserted,
            "created_at": created_at,
            "already_accepted": False,
        }

    result = database.run_in_transaction(_accept_txn)
    logger.info(f"Batch '{batch_id}' successfully ACCEPTED and committed.")
    return result


def rollback_migration_batch(
    database,
    batch_id: str = "BATCH_BACKFILL_20261111_01",
) -> int:
    """
    Fail-Safe Rollback Protocol:
    Atomically reads batch state, validates 'PENDING_VALIDATION', marks 'ROLLED_BACK',
    and deletes manifest-owned edges inside the EXACT SAME transaction.

    Race Prevention Guarantees:
    - Lock Contention: Competes directly for exclusive lock on MigrationBatches(batch_id).
    - If acceptance commits first, rollback retries, reads 'ACCEPTED', raises RollbackRejectedError,
      and performs ZERO deletions.
    - If rollback commits first, state becomes 'ROLLED_BACK', edges are deleted, and
      subsequent acceptance attempts are permanently rejected.
    """
    def _rollback_txn(transaction):
        # 1. Read current batch state inside transaction (acquires read lock on batch_id)
        res = list(transaction.execute_sql(
            "SELECT State FROM MigrationBatches WHERE BatchId = @batch_id",
            params={"batch_id": batch_id},
            param_types={"batch_id": spanner.param_types.STRING},
        ))
        if not res:
            raise MigrationStateError(f"Batch '{batch_id}' not found in MigrationBatches.")

        current_state = res[0][0]
        if current_state == "ACCEPTED":
            raise RollbackRejectedError(
                f"CRITICAL: Rollback rejected! Batch '{batch_id}' has already been ACCEPTED. "
                "Automated deletion is permanently locked to protect live ingestion graph integrity."
            )
        if current_state == "ROLLED_BACK":
            logger.warning(f"Batch '{batch_id}' has already been rolled back.")
            return 0
        if current_state != "PENDING_VALIDATION":
            raise RollbackRejectedError(
                f"Rollback rejected: batch '{batch_id}' is in state '{current_state}', expected 'PENDING_VALIDATION'."
            )

        # 2. Transition state to 'ROLLED_BACK' inside the SAME transaction (acquires exclusive lock)
        transaction.update(
            "MigrationBatches",
            columns=["BatchId", "State"],
            values=[(batch_id, "ROLLED_BACK")],
        )

        # 3. Read manifest keys recorded strictly for this batch inside the same transaction
        manifest_sql = (
            "SELECT TableName, RecordKey FROM MigrationManifest WHERE BatchId = @batch_id"
        )
        manifest_rows = list(transaction.execute_sql(
            manifest_sql,
            params={"batch_id": batch_id},
            param_types={"batch_id": spanner.param_types.STRING},
        ))

        if not manifest_rows:
            logger.info(f"No manifest records found for batch '{batch_id}'.")
            return 0

        # Group keys by target edge table
        edges_to_delete: dict[str, list[tuple[str, str]]] = {}
        for table_name, record_key in manifest_rows:
            k1, k2 = record_key.split("#", 1)
            edges_to_delete.setdefault(table_name, []).append((k1, k2))

        total_deleted = 0
        # 4. Delete edges matching exact manifest keys inside the same transaction
        for table_name, key_pairs in edges_to_delete.items():
            key_set = spanner.KeySet(keys=[list(pair) for pair in key_pairs])
            transaction.delete(table_name, key_set)
            total_deleted += len(key_pairs)

        # 5. Clean up manifest records for this batch
        manifest_keys = [
            [batch_id, t, f"{k1}#{k2}"]
            for t, pairs in edges_to_delete.items()
            for k1, k2 in pairs
        ]
        transaction.delete("MigrationManifest", spanner.KeySet(keys=manifest_keys))

        logger.info(f"Rollback buffered deletion of {total_deleted} edges and manifest records.")
        return total_deleted

    deleted_count = database.run_in_transaction(_rollback_txn)
    logger.info(f"Batch '{batch_id}' successfully rolled back ({deleted_count} edges deleted).")
    return deleted_count


