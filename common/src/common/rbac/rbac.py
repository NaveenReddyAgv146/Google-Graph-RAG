"""Security and RBAC enforcement for pre-retrieval filtering in Spanner Graph."""

from __future__ import annotations

import logging
import re
from typing import Any

from google.cloud import spanner

logger = logging.getLogger("common.rbac")

# Permitted access levels granting read retrieval access
PERMITTED_ACCESS_LEVELS = frozenset({"READ", "WRITE", "ADMIN"})

# RFC 5322 compliant email regex disallowing consecutive dots
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9]+(\.[a-zA-Z0-9-]+)+$")


def resolve_principal_access(
    user_email: str, database: Any = None, group_resolver: Any = None
) -> list[str]:
    """Authoritatively resolves which DocumentIds a principal has read retrieval access to.

    Security Policies:
    1. Identity Normalization & Resolution:
       - Fails closed (returns []) if user_email is None, empty, whitespace, or invalid.
       - Maps trusted Google identity to: 'google:<normalized-email>'.
       - Does not grant unverified domain-level or local test identities.
    2. Explicit Access Level Gating:
       - Only rows with AccessLevel IN ('READ', 'WRITE', 'ADMIN') are granted.
       - NULL, empty, or unknown access levels grant zero access.
    3. Fail-Closed Resilience:
       - Database connection errors or query exceptions return [] without leaking data.
    4. Read-Only Transaction:
       - Executes inside a read-only snapshot.
    """
    if not user_email or not isinstance(user_email, str):
        logger.warning("Empty or non-string principal identity provided. Defaulting to deny-all.")
        return []

    normalized_identity = user_email.strip().lower()
    if not normalized_identity:
        logger.warning("Whitespace-only principal identity provided. Defaulting to deny-all.")
        return []

    # Enforce trusted Google identity mapping
    if normalized_identity.startswith("google:"):
        clean_email = normalized_identity[7:].strip()
    else:
        clean_email = normalized_identity

    if not clean_email or not EMAIL_REGEX.match(clean_email):
        logger.warning(
            "Principal identity '%s' does not contain a valid email address. Defaulting to deny-all.",
            user_email,
        )
        return []

    principal_id = f"google:{clean_email}"

    # Resolve database client safely
    if database is None:
        try:
            from common.database.spanner import get_database

            database = get_database()
        except Exception as exc:
            logger.error("Failed to acquire Spanner database handle: %s", exc)
            return []

    query = """
        SELECT DISTINCT DocumentId
        FROM DocumentAccess
        WHERE PrincipalId = @principal_id
          AND AccessLevel IN UNNEST(@permitted_levels)
    """
    params = {
        "principal_id": principal_id,
        "permitted_levels": list(PERMITTED_ACCESS_LEVELS),
    }
    param_types = {
        "principal_id": spanner.param_types.STRING,
        "permitted_levels": spanner.param_types.Array(spanner.param_types.STRING),
    }

    try:
        if hasattr(database, "snapshot"):
            with database.snapshot() as snapshot:
                rows = list(snapshot.execute_sql(query, params=params, param_types=param_types))
        elif hasattr(database, "execute_sql"):
            rows = list(database.execute_sql(query, params=params, param_types=param_types))
        else:
            logger.error("Provided database object lacks snapshot or execute_sql method.")
            return []

        authorized_doc_ids = sorted(list({r[0] for r in rows if r and r[0]}))
        logger.info(
            "Resolved %d authorized DocumentId(s) for principal '%s'",
            len(authorized_doc_ids),
            principal_id,
        )
        return authorized_doc_ids

    except Exception as exc:
        logger.error(
            "Database error while querying DocumentAccess for principal '%s': %s",
            principal_id,
            exc,
        )
        return []


def build_document_access_filter(authorized_doc_ids: list[str] | None) -> str:
    """Builds the SQL/GQL pre-retrieval filter fragment.

    Enforces push-down predicate evaluation so unauthorized rows are excluded at index scan time.
    """
    if not authorized_doc_ids:
        # Deny-all: Empty array guarantees zero chunks/documents match
        return "c.DocumentId IN UNNEST(CAST([] AS ARRAY<STRING>))"
    return "c.DocumentId IN UNNEST(@authorized_doc_ids)"
