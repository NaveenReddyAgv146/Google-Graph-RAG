"""Security and RBAC enforcement for pre-retrieval filtering in Spanner Graph."""

import logging
from typing import List, Optional

logger = logging.getLogger("common.rbac")

def resolve_principal_access(
    user_email: str,
    database=None,
    group_resolver=None
) -> List[str]:
    """
    Authoritatively resolves which DocumentIds a principal has READ access to.
    
    Architecture (Phase 1E/1F Hardening):
    1. Direct user identity mapping: 'user:<email>'
    2. Enterprise transitive group mapping via Google Cloud Identity API: 'group:<group>@domain.com'
    3. Explicit domain policy mapping: 'domain:<domain>' (evaluated ONLY if explicit policy row exists)
    4. Default-deny: If zero matching policies found, returns an empty list [].
    """
    if not user_email:
        logger.warning("Empty principal email provided. Defaulting to deny-all.")
        return []

    # Placeholder interface for Phase 3 implementation
    # When fully wired, queries DocumentAccess table with resolved principal identifiers
    return []

def build_document_access_filter(authorized_doc_ids: Optional[List[str]]) -> str:
    """
    Builds the SQL/GQL pre-retrieval filter fragment.
    Enforces push-down predicate evaluation so unauthorized rows are excluded at index scan time.
    """
    if not authorized_doc_ids:
        # Deny-all: Empty array guarantees zero chunks/documents match
        return "c.DocumentId IN UNNEST(CAST([] AS ARRAY<STRING>))"
    return "c.DocumentId IN UNNEST(@authorized_doc_ids)"

