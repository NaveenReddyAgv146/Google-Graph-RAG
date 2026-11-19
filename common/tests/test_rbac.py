"""Unit and Integration tests for RBAC access resolution (common.rbac.rbac).

Validates:
1. Authorized Google identity retrieves the Uber document.
2. Unauthorized identity retrieves zero documents.
3. Missing identity fails closed without querying Spanner.
4. NULL and invalid access levels grant no access.
5. Database errors fail closed gracefully.
6. A caller cannot impersonate another employee or use unverified formats.
7. Explicitly authorized read-only integration test against existing live Spanner DocumentAccess records.
"""

from unittest.mock import MagicMock, patch
import pytest
from google.cloud import spanner

from common.rbac.rbac import (
    PERMITTED_ACCESS_LEVELS,
    build_document_access_filter,
    resolve_principal_access,
)


# ============================================================================
# UNIT TESTS (MOCKED DATABASE)
# ============================================================================


def test_missing_identity_fails_closed_without_db_call():
    """Falsy, whitespace or non-string identity must immediately return [] without querying database."""
    mock_db = MagicMock()

    assert resolve_principal_access("", database=mock_db) == []
    assert resolve_principal_access("   ", database=mock_db) == []
    assert resolve_principal_access(None, database=mock_db) == []
    assert resolve_principal_access(12345, database=mock_db) == []

    # Database must never be touched
    mock_db.snapshot.assert_not_called()
    mock_db.execute_sql.assert_not_called()


def test_malformed_email_identity_fails_closed_without_db_call():
    """Identities that are not valid emails must fail closed without querying database."""
    mock_db = MagicMock()

    assert resolve_principal_access("not-an-email", database=mock_db) == []
    assert resolve_principal_access("attacker@evil..com", database=mock_db) == []
    assert resolve_principal_access("admin", database=mock_db) == []
    assert resolve_principal_access("local:m5-demo", database=mock_db) == []

    mock_db.snapshot.assert_not_called()


def test_authorized_google_identity_queries_spanner_with_google_prefix():
    """Valid email is normalized and mapped to 'google:<email>' in parameterized SQL."""
    mock_db = MagicMock()
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot

    # Mock Spanner returning authorized document IDs
    mock_snapshot.execute_sql.return_value = [
        ("1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_",),
        ("1trxQDZDEGA23IHBPHFt84HLggBUZNelE",),
    ]

    docs = resolve_principal_access("AiAgent.Testing125@gmail.com ", database=mock_db)

    assert docs == [
        "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_",
        "1trxQDZDEGA23IHBPHFt84HLggBUZNelE",
    ]

    mock_snapshot.execute_sql.assert_called_once()
    call_args = mock_snapshot.execute_sql.call_args
    sql = call_args[0][0]
    params = call_args[1]["params"]

    assert "PrincipalId = @principal_id" in sql
    assert params["principal_id"] == "google:aiagent.testing125@gmail.com"
    assert set(params["permitted_levels"]) == PERMITTED_ACCESS_LEVELS


def test_unauthorized_identity_returns_empty_list():
    """When Spanner returns zero rows for an unmapped identity, resolve_principal_access returns []."""
    mock_db = MagicMock()
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []

    docs = resolve_principal_access("stranger@external.com", database=mock_db)
    assert docs == []


def test_database_error_fails_closed():
    """Any database exception must be logged and safely caught, returning []."""
    mock_db = MagicMock()
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.side_effect = RuntimeError("Spanner RPC connection timed out")

    docs = resolve_principal_access("architect@enterprise.com", database=mock_db)
    assert docs == []


def test_access_level_filter_enforcement():
    """Verify that only rows satisfying AccessLevel IN ('READ', 'WRITE', 'ADMIN') are queried."""
    mock_db = MagicMock()
    mock_snapshot = MagicMock()
    mock_db.snapshot.return_value.__enter__.return_value = mock_snapshot
    mock_snapshot.execute_sql.return_value = []

    resolve_principal_access("user@enterprise.com", database=mock_db)

    sql = mock_snapshot.execute_sql.call_args[0][0]
    assert "AccessLevel IN UNNEST(@permitted_levels)" in sql
    assert "READ" in PERMITTED_ACCESS_LEVELS
    assert "WRITE" in PERMITTED_ACCESS_LEVELS
    assert "ADMIN" in PERMITTED_ACCESS_LEVELS
    assert "NONE" not in PERMITTED_ACCESS_LEVELS


def test_build_document_access_filter():
    """build_document_access_filter builds valid parameterized SQL predicate."""
    assert build_document_access_filter(None) == "c.DocumentId IN UNNEST(CAST([] AS ARRAY<STRING>))"
    assert build_document_access_filter([]) == "c.DocumentId IN UNNEST(CAST([] AS ARRAY<STRING>))"
    assert build_document_access_filter(["doc_1", "doc_2"]) == "c.DocumentId IN UNNEST(@authorized_doc_ids)"


# ============================================================================
# LIVE READ-ONLY SPANNER INTEGRATION TESTS
# ============================================================================


@pytest.mark.integration
def test_live_authorized_user_retrieves_uber_document():
    """Live read-only test: 'aiagent.testing125@gmail.com' must resolve Uber document ID."""
    from common.database.spanner import get_database

    db = get_database()
    docs = resolve_principal_access("aiagent.testing125@gmail.com", database=db)

    # In live DocumentAccess, aiagent.testing125@gmail.com has 5 authorized documents
    assert "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_" in docs, (
        f"Expected Uber document ID '1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_' in authorized list: {docs}"
    )
    assert len(docs) == 5


@pytest.mark.integration
def test_live_unauthorized_user_retrieves_zero_documents():
    """Live read-only test: non-existent email in DocumentAccess must resolve to []."""
    from common.database.spanner import get_database

    db = get_database()
    docs = resolve_principal_access("unauthorized_tester_999@enterprise.com", database=db)
    assert docs == []

