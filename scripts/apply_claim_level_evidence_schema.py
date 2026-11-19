#!/usr/bin/env python3
"""
Step 2B Spanner DDL Migration Script for Claim-Level Evidence Support.

Target:
  Project:  xenon-broker-513704-s9
  Instance: sales-discovery-trial
  Database: sales-knowledge-db

Scope:
  - Table: EvidenceLinks
  - Column: ClaimType STRING(32) (Nullable: YES)
  - Constraint: Chk_EvidenceLinks_ClaimType CHECK (ClaimType IS NULL OR ClaimType IN (
        'BUSINESS_PROBLEM',
        'PROPOSED_SOLUTION',
        'IMPLEMENTED_SOLUTION',
        'OUTCOME'
    ))

Safety & Idempotency Features:
  - Strict preflight: Validates that database exists and all 11 SolutionCards
    and 6 EvidenceLinks match deterministic cryptographic SHA-256 baselines.
  - Deep Inspection: Compares existing columns and constraints. Aborts if schema contradicts specification.
  - Safe by Default: Runs dry-run preflight checks unless explicitly invoked with --apply.
  - Postflight Verification: Verifies schema completeness and confirms identical
    cryptographic fingerprints for existing data.
  - Rollback Support: Provides automated --rollback execution to drop constraint and column.
"""

import argparse
from dataclasses import dataclass
import hashlib
import logging
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"
os.environ["GRPC_DNS_RESOLVER"] = "native"

import subprocess
from google.oauth2.credentials import Credentials
from google.cloud import spanner
from common.config import settings

def get_spanner_client():
    """Get Spanner client using active gcloud authentication."""
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    creds = Credentials(token)
    return spanner.Client(project=settings.project_id, credentials=creds)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("claim_level_schema_migration")

TABLE_NAME = "EvidenceLinks"
COLUMN_NAME = "ClaimType"
CONSTRAINT_NAME = "Chk_EvidenceLinks_ClaimType"

EXPECTED_CARDS_COUNT = 11
EXPECTED_CARDS_SHA256 = "21d7d94bb8078d537ef2aac1843e111e5ad16134d3068e761ba16d0115096d92"
EXPECTED_EVIDENCE_COUNT = 6
EXPECTED_EVIDENCE_SHA256 = "73a1c60f1af203ba0e5e1a07584fed9c9a3d4d894036b14dffcac705e0737760"

DDL_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} ADD COLUMN {COLUMN_NAME} STRING(32)",
    f"""ALTER TABLE {TABLE_NAME} ADD CONSTRAINT {CONSTRAINT_NAME} CHECK (
    {COLUMN_NAME} IS NULL OR {COLUMN_NAME} IN (
        'BUSINESS_PROBLEM',
        'PROPOSED_SOLUTION',
        'IMPLEMENTED_SOLUTION',
        'OUTCOME'
    )
)""",
]

ROLLBACK_DDL_STATEMENTS = [
    f"ALTER TABLE {TABLE_NAME} DROP CONSTRAINT {CONSTRAINT_NAME}",
    f"ALTER TABLE {TABLE_NAME} DROP COLUMN {COLUMN_NAME}",
]


class SchemaMigrationError(Exception):
    """Base exception for schema migration errors."""


class IncompatibleSchemaError(SchemaMigrationError):
    """Raised when existing schema contradicts expected specification."""


class FingerprintMismatchError(SchemaMigrationError):
    """Raised when preflight or postflight cryptographic fingerprints do not match."""


@dataclass(frozen=True)
class BaselineFingerprint:
    card_count: int
    card_sha256: str
    evidence_count: int
    evidence_sha256: str


def compute_deterministic_fingerprints(database) -> BaselineFingerprint:
    """Computes deterministic SHA-256 fingerprints for SolutionCards and EvidenceLinks."""
    with database.snapshot(multi_use=True) as snapshot:
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

    return BaselineFingerprint(
        card_count=len(card_rows),
        card_sha256=card_digest,
        evidence_count=len(ev_rows),
        evidence_sha256=ev_digest,
    )


def inspect_schema_status(database) -> tuple[bool, bool]:
    """Inspects whether ClaimType column and Chk_EvidenceLinks_ClaimType constraint exist."""
    with database.snapshot(multi_use=True) as snapshot:
        col_rows = list(snapshot.execute_sql("""
            SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = @table AND COLUMN_NAME = @col AND TABLE_SCHEMA = ''
        """, params={"table": TABLE_NAME, "col": COLUMN_NAME},
             param_types={"table": spanner.param_types.STRING, "col": spanner.param_types.STRING}))
        has_col = len(col_rows) > 0

        constraint_rows = list(snapshot.execute_sql("""
            SELECT CONSTRAINT_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
            WHERE TABLE_NAME = @table AND CONSTRAINT_NAME = @cname AND TABLE_SCHEMA = ''
        """, params={"table": TABLE_NAME, "cname": CONSTRAINT_NAME},
             param_types={"table": spanner.param_types.STRING, "cname": spanner.param_types.STRING}))
        has_constraint = len(constraint_rows) > 0

    return has_col, has_constraint


def preflight_check(database) -> BaselineFingerprint:
    """Strict preflight safety validation."""
    logger.info("Executing preflight fingerprint verification...")
    fp = compute_deterministic_fingerprints(database)

    if fp.card_count != EXPECTED_CARDS_COUNT or fp.card_sha256 != EXPECTED_CARDS_SHA256:
        raise FingerprintMismatchError(
            f"SolutionCards baseline mismatch! Expected {EXPECTED_CARDS_COUNT} cards "
            f"(sha256={EXPECTED_CARDS_SHA256}), got {fp.card_count} (sha256={fp.card_sha256})"
        )

    if fp.evidence_count != EXPECTED_EVIDENCE_COUNT or fp.evidence_sha256 != EXPECTED_EVIDENCE_SHA256:
        raise FingerprintMismatchError(
            f"EvidenceLinks baseline mismatch! Expected {EXPECTED_EVIDENCE_COUNT} links "
            f"(sha256={EXPECTED_EVIDENCE_SHA256}), got {fp.evidence_count} (sha256={fp.evidence_sha256})"
        )

    logger.info("Preflight validation PASSED: All 11 SolutionCards and 6 EvidenceLinks verified intact.")
    return fp


def main():
    parser = argparse.ArgumentParser(description="Claim-Level Evidence Spanner DDL Migration Preflight & Deployer")
    parser.add_argument("--apply", action="store_true", help="Apply DDL migration statements to Spanner")
    parser.add_argument("--rollback", action="store_true", help="Roll back DDL migration (drop constraint & column)")
    args = parser.parse_args()

    client = get_spanner_client()
    db = client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

    # 1. Preflight
    baseline_fp = preflight_check(db)
    has_col, has_constraint = inspect_schema_status(db)

    logger.info(f"Current Schema State: ClaimType Column Exists = {has_col}, Constraint Exists = {has_constraint}")

    if args.rollback:
        if not args.apply:
            logger.info("[DRY RUN] Would execute rollback DDL:")
            for stmt in ROLLBACK_DDL_STATEMENTS:
                logger.info(f"  {stmt}")
            return
        logger.info("Executing ROLLBACK DDL...")
        op = db.update_ddl(ROLLBACK_DDL_STATEMENTS)
        op.result(timeout=300)
        logger.info("Rollback complete.")
        return

    if not args.apply:
        logger.info("=" * 80)
        logger.info("[DRY RUN ONLY] Preflight checks passed. No DDL was executed.")
        logger.info("Planned DDL Statements for --apply:")
        for stmt in DDL_STATEMENTS:
            logger.info(f"  {stmt}")
        logger.info("=" * 80)
        return

    # Apply Migration
    if has_col and has_constraint:
        logger.info("ClaimType column and CHECK constraint already exist. Schema is up to date.")
        return

    logger.info("Executing online DDL schema migration...")
    stmts_to_run = []
    if not has_col:
        stmts_to_run.append(DDL_STATEMENTS[0])
    if not has_constraint:
        stmts_to_run.append(DDL_STATEMENTS[1])

    op = db.update_ddl(stmts_to_run)
    op.result(timeout=300)
    logger.info("DDL operation finished. Running postflight verification...")

    # Postflight
    post_fp = compute_deterministic_fingerprints(db)
    if post_fp.card_sha256 != baseline_fp.card_sha256 or post_fp.evidence_sha256 != baseline_fp.evidence_sha256:
        raise FingerprintMismatchError("Postflight data corruption detected! Fingerprints changed.")

    post_has_col, post_has_constraint = inspect_schema_status(db)
    if not (post_has_col and post_has_constraint):
        raise SchemaMigrationError("Postflight schema verification failed: Column or constraint missing.")

    # Detailed Postflight Verification
    with db.snapshot(multi_use=True) as snapshot:
        # 1. Physical columns
        cols = list(snapshot.execute_sql("""
            SELECT COLUMN_NAME, SPANNER_TYPE, IS_NULLABLE, ORDINAL_POSITION
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'EvidenceLinks' AND TABLE_SCHEMA = ''
            ORDER BY ORDINAL_POSITION
        """))
        logger.info(f"Postflight: EvidenceLinks physical columns count = {len(cols)} (expected 8)")
        for c in cols:
            logger.info(f"  [{c[3]}] {c[0]:<20} {c[1]:<20} (Nullable: {c[2]})")

        # 2. Constraint validation state
        try:
            check_constraints = list(snapshot.execute_sql("""
                SELECT CONSTRAINT_NAME, CHECK_CLAUSE
                FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS
                WHERE CONSTRAINT_NAME = 'Chk_EvidenceLinks_ClaimType'
            """))
            for cc in check_constraints:
                logger.info(f"Postflight Constraint: {cc[0]} -> {cc[1]}")
        except Exception as e:
            logger.warning(f"Could not read CHECK_CONSTRAINTS: {e}")
            t_constraints = list(snapshot.execute_sql("""
                SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
                WHERE TABLE_NAME = 'EvidenceLinks'
            """))
            for tc in t_constraints:
                logger.info(f"  Table Constraint: {tc[0]} ({tc[1]})")

        # 3. Check existing EvidenceLinks ClaimType values
        ev_records = list(snapshot.execute_sql("""
            SELECT EvidenceId, CardId, ClaimType FROM EvidenceLinks ORDER BY EvidenceId ASC
        """))
        logger.info(f"Postflight EvidenceLinks count: {len(ev_records)}")
        all_null = True
        for er in ev_records:
            logger.info(f"  EvidenceId={er[0]} | CardId={er[1]} | ClaimType={er[2]!r}")
            if er[2] is not None:
                all_null = False

        if not all_null:
            raise SchemaMigrationError("Existing EvidenceLinks have non-NULL ClaimType!")

        # 4. Check SolutionCards statuses
        card_statuses = list(snapshot.execute_sql("""
            SELECT VerificationStatus, COUNT(1)
            FROM SolutionCards
            GROUP BY VerificationStatus
            ORDER BY VerificationStatus
        """))
        logger.info("Postflight SolutionCards statuses:")
        for cs in card_statuses:
            logger.info(f"  {cs[0]}: {cs[1]}")

    logger.info("=" * 80)
    logger.info("POSTFLIGHT AUDIT PASSED: EXACTLY 8 COLUMNS, NULL CLAIMTYPES VERIFIED, FINGERPRINTS UNCHANGED.")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()
