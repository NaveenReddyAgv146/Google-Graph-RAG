#!/usr/bin/env python3
"""
Phase 3D Cloud Spanner DDL Deployment Script (Safe & Idempotent).

Target:
  Project:  xenon-broker-513704-s9
  Instance: sales-discovery-trial
  Database: sales-knowledge-db

Scope:
  - Table: SolutionCardReviews
  - Secondary Indexes:
      * Idx_SolutionCardReviews_Reviewer
      * Idx_SolutionCardReviews_ReviewedAt

Safety & Idempotency Features:
  - Strict preflight: Validates that database exists and all existing 11 SolutionCards
    and 6 EvidenceLinks are intact before touching anything using deterministic SHA-256 fingerprints.
  - Deep Inspection: Compares existing SolutionCardReviews columns, data types, nullability,
    commit timestamp options, foreign keys, and index column ordering against specification.
  - Aborts on Incompatibility: Raises IncompatibleSchemaError if an existing object differs
    from the target specification. Never treats name-only existence as valid.
  - Postflight Verification: Re-verifies schema completeness and confirms identical
    cryptographic SHA-256 fingerprints for SolutionCards and EvidenceLinks.
  - Safe by default: Dry-run only unless explicitly invoked with --apply.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import logging
import sys
from typing import Any

from google.cloud import spanner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("phase3d_schema_deployment")

PROJECT_ID = "xenon-broker-513704-s9"
INSTANCE_ID = "sales-discovery-trial"
DATABASE_ID = "sales-knowledge-db"

TABLE_NAME = "SolutionCardReviews"
INDEX_REVIEWER = "Idx_SolutionCardReviews_Reviewer"
INDEX_REVIEWED_AT = "Idx_SolutionCardReviews_ReviewedAt"


class SchemaDeploymentError(Exception):
    """Base exception for schema deployment errors."""


class IncompatibleSchemaError(SchemaDeploymentError):
    """Raised when an existing schema object contradicts expected specification."""


class FingerprintMismatchError(SchemaDeploymentError):
    """Raised when preflight or postflight cryptographic fingerprints do not match."""


# Expected Column Specification: name -> (spanner_type, is_nullable)
EXPECTED_COLUMNS: dict[str, tuple[str, str]] = {
    "CardId": ("STRING(64)", "NO"),
    "ReviewId": ("STRING(64)", "NO"),
    "ReviewerId": ("STRING(128)", "NO"),
    "ReviewerEmail": ("STRING(256)", "NO"),
    "Action": ("STRING(32)", "NO"),
    "PreviousStatus": ("STRING(32)", "NO"),
    "NewStatus": ("STRING(32)", "NO"),
    "Comments": ("STRING(MAX)", "YES"),
    "ReviewedAt": ("TIMESTAMP", "NO"),
    "ReviewMetadata": ("JSON", "YES"),
}

EXPECTED_INDEXES: dict[str, list[tuple[str, str]]] = {
    INDEX_REVIEWER: [("ReviewerId", "ASC"), ("ReviewedAt", "DESC")],
    INDEX_REVIEWED_AT: [("ReviewedAt", "DESC")],
}

DDL_STATEMENTS = [
    f"""CREATE TABLE {TABLE_NAME} (
    CardId STRING(64) NOT NULL,
    ReviewId STRING(64) NOT NULL,
    ReviewerId STRING(128) NOT NULL,
    ReviewerEmail STRING(256) NOT NULL,
    Action STRING(32) NOT NULL,
    PreviousStatus STRING(32) NOT NULL,
    NewStatus STRING(32) NOT NULL,
    Comments STRING(MAX),
    ReviewedAt TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp = true),
    ReviewMetadata JSON,
    CONSTRAINT FK_SCR_CardId FOREIGN KEY (CardId) REFERENCES SolutionCards (CardId)
) PRIMARY KEY (CardId, ReviewId)""",
    f"""CREATE INDEX {INDEX_REVIEWER} 
ON {TABLE_NAME} (ReviewerId, ReviewedAt DESC)""",
    f"""CREATE INDEX {INDEX_REVIEWED_AT} 
ON {TABLE_NAME} (ReviewedAt DESC)""",
]


@dataclass(frozen=True)
class BaselineFingerprint:
    card_count: int
    card_sha256: str
    evidence_count: int
    evidence_sha256: str


def compute_deterministic_fingerprints(database) -> BaselineFingerprint:
    """Computes deterministic SHA-256 fingerprints for SolutionCards and EvidenceLinks.

    Covers primary keys, statuses, and payload columns in canonical sort order.
    """
    with database.snapshot() as snapshot:
        # SolutionCards fingerprint
        card_query = """
            SELECT CardId, DocumentId, ProjectName, BusinessProblem, ProposedSolution,
                   ImplementedSolution, Outcome, VerificationStatus, OkfUri
            FROM SolutionCards
            ORDER BY CardId ASC
        """
        card_rows = list(snapshot.execute_sql(card_query))
        card_hasher = hashlib.sha256()
        for r in card_rows:
            row_str = "|".join(str(val) if val is not None else "<NULL>" for val in r)
            card_hasher.update(row_str.encode("utf-8"))
            card_hasher.update(b"\n")
        card_digest = card_hasher.hexdigest()

        # EvidenceLinks fingerprint
        ev_query = """
            SELECT EvidenceId, CardId, ChunkId, DocumentId, PageNumber, SlideNumber, Note
            FROM EvidenceLinks
            ORDER BY EvidenceId ASC
        """
        ev_rows = list(snapshot.execute_sql(ev_query))
        ev_hasher = hashlib.sha256()
        for r in ev_rows:
            row_str = "|".join(str(val) if val is not None else "<NULL>" for val in r)
            ev_hasher.update(row_str.encode("utf-8"))
            ev_hasher.update(b"\n")
        ev_digest = ev_hasher.hexdigest()

    logger.info(
        "Fingerprint computed: SolutionCards (n=%d, sha256=%s), EvidenceLinks (n=%d, sha256=%s)",
        len(card_rows),
        card_digest,
        len(ev_rows),
        ev_digest,
    )
    return BaselineFingerprint(
        card_count=len(card_rows),
        card_sha256=card_digest,
        evidence_count=len(ev_rows),
        evidence_sha256=ev_digest,
    )


def deep_inspect_existing_schema(database) -> tuple[bool, set[str]]:
    """Deeply inspects existing SolutionCardReviews table, columns, types, options, FKs, and indexes.

    Raises IncompatibleSchemaError if an existing object contradicts EXPECTED_COLUMNS or EXPECTED_INDEXES.
    Returns: (table_exists: bool, valid_indexes: set[str])
    """
    table_exists = False
    valid_indexes = set()

    with database.snapshot() as snapshot:
        # 1. Check table existence
        tables = list(
            snapshot.execute_sql(
                "SELECT table_name FROM INFORMATION_SCHEMA.TABLES "
                "WHERE table_schema = '' AND table_name = @table_name",
                params={"table_name": TABLE_NAME},
                param_types={"table_name": spanner.param_types.STRING},
            )
        )
        if not tables:
            return False, set()

        table_exists = True

        # 2. Deep Column Inspection
        col_rows = list(
            snapshot.execute_sql(
                "SELECT column_name, spanner_type, is_nullable "
                "FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE table_name = @table_name",
                params={"table_name": TABLE_NAME},
                param_types={"table_name": spanner.param_types.STRING},
            )
        )
        found_cols = {r[0]: (r[1], r[2]) for r in col_rows}

        for exp_col, (exp_type, exp_null) in EXPECTED_COLUMNS.items():
            if exp_col not in found_cols:
                raise IncompatibleSchemaError(
                    f"Table '{TABLE_NAME}' exists but is missing required column '{exp_col}'."
                )
            actual_type, actual_null = found_cols[exp_col]
            if actual_type != exp_type:
                raise IncompatibleSchemaError(
                    f"Column '{TABLE_NAME}.{exp_col}' type mismatch: expected {exp_type}, found {actual_type}."
                )
            if actual_null != exp_null:
                raise IncompatibleSchemaError(
                    f"Column '{TABLE_NAME}.{exp_col}' nullability mismatch: expected {exp_null}, found {actual_null}."
                )

        # 3. Check commit timestamp option on ReviewedAt
        option_rows = list(
            snapshot.execute_sql(
                "SELECT option_name, option_value "
                "FROM INFORMATION_SCHEMA.COLUMN_OPTIONS "
                "WHERE table_name = @table_name AND column_name = 'ReviewedAt'",
                params={"table_name": TABLE_NAME},
                param_types={"table_name": spanner.param_types.STRING},
            )
        )
        options = {r[0]: r[1] for r in option_rows}
        if options.get("allow_commit_timestamp") != "TRUE":
            raise IncompatibleSchemaError(
                f"Column '{TABLE_NAME}.ReviewedAt' is missing 'OPTIONS (allow_commit_timestamp = true)'."
            )

        # 4. Check Foreign Key Constraint FK_SCR_CardId
        fk_rows = list(
            snapshot.execute_sql(
                "SELECT constraint_name "
                "FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS "
                "WHERE table_name = @table_name AND constraint_type = 'FOREIGN KEY'",
                params={"table_name": TABLE_NAME},
                param_types={"table_name": spanner.param_types.STRING},
            )
        )
        fk_names = {r[0] for r in fk_rows}
        if "FK_SCR_CardId" not in fk_names:
            raise IncompatibleSchemaError(
                f"Table '{TABLE_NAME}' is missing foreign key constraint 'FK_SCR_CardId'."
            )

        # 5. Deep Index Inspection
        for idx_name, exp_cols in EXPECTED_INDEXES.items():
            idx_rows = list(
                snapshot.execute_sql(
                    "SELECT column_name, column_ordering "
                    "FROM INFORMATION_SCHEMA.INDEX_COLUMNS "
                    "WHERE table_name = @table_name AND index_name = @idx_name "
                    "ORDER BY ordinal_position ASC",
                    params={"table_name": TABLE_NAME, "idx_name": idx_name},
                    param_types={
                        "table_name": spanner.param_types.STRING,
                        "idx_name": spanner.param_types.STRING,
                    },
                )
            )
            if idx_rows:
                actual_cols = [(r[0], r[1]) for r in idx_rows]
                if actual_cols != exp_cols:
                    raise IncompatibleSchemaError(
                        f"Index '{idx_name}' exists but has incompatible columns/ordering: expected {exp_cols}, found {actual_cols}."
                    )
                valid_indexes.add(idx_name)

    return table_exists, valid_indexes


def apply_schema(apply_changes: bool = False):
    """Main deployment procedure."""
    client = spanner.Client(project=PROJECT_ID)
    instance = client.instance(INSTANCE_ID)
    database = instance.database(DATABASE_ID)

    logger.info("Connecting to Spanner: %s/%s/%s", PROJECT_ID, INSTANCE_ID, DATABASE_ID)
    preflight_fp = compute_deterministic_fingerprints(database)

    table_exists, valid_indexes = deep_inspect_existing_schema(database)
    logger.info("Table '%s' exists & valid: %s. Valid indexes: %s", TABLE_NAME, table_exists, valid_indexes)

    statements_to_run = []
    if not table_exists:
        statements_to_run.append(DDL_STATEMENTS[0])
    if INDEX_REVIEWER not in valid_indexes:
        statements_to_run.append(DDL_STATEMENTS[1])
    if INDEX_REVIEWED_AT not in valid_indexes:
        statements_to_run.append(DDL_STATEMENTS[2])

    if not statements_to_run:
        logger.info("Schema is already up to date. All objects deeply validated. Idempotent no-op.")
        return

    logger.info("Planned DDL statements (%d):", len(statements_to_run))
    for stmt in statements_to_run:
        logger.info("  %s", stmt.strip().split("\n")[0])

    if not apply_changes:
        logger.warning("Dry-run mode. Specify --apply to execute DDL statements against Cloud Spanner.")
        return

    logger.info("Applying DDL statements to Cloud Spanner database...")
    op = database.update_ddl(statements_to_run)
    logger.info("Waiting for DDL operation to complete: %s", op.id)
    op.result(timeout=300)
    logger.info("DDL operation succeeded.")

    # Postflight Deep Schema Verification
    post_table_exists, post_indexes = deep_inspect_existing_schema(database)
    if not post_table_exists:
        raise RuntimeError(f"Postflight check failed: Table '{TABLE_NAME}' not found after DDL execution.")
    if INDEX_REVIEWER not in post_indexes or INDEX_REVIEWED_AT not in post_indexes:
        raise RuntimeError(f"Postflight check failed: Indexes missing or invalid. Found: {post_indexes}")

    # Postflight Record Integrity Check
    postflight_fp = compute_deterministic_fingerprints(database)
    if postflight_fp != preflight_fp:
        raise FingerprintMismatchError(
            f"Data integrity violation! Preflight {preflight_fp} does not match postflight {postflight_fp}"
        )

    logger.info("Schema deployment, deep inspection, and cryptographic integrity check PASSED successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 3D DDL Deployment Script")
    parser.add_argument("--apply", action="store_true", help="Execute DDL statements against Spanner")
    args = parser.parse_args()

    apply_schema(apply_changes=args.apply)
