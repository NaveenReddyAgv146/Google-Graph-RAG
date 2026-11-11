"""Cloud Spanner Graph client and transaction management."""

import logging
from collections.abc import Callable
from typing import Any

from google.cloud import spanner

from common.config import settings

logger = logging.getLogger("common.spanner")

_client: spanner.Client | None = None
_database = None


def get_spanner_client() -> spanner.Client:
    """Returns singleton Spanner client instance."""
    global _client
    if _client is None:
        _client = spanner.Client(project=settings.project_id)
    return _client


def get_database():
    """Returns singleton Spanner database handle."""
    global _database
    if _database is None:
        client = get_spanner_client()
        instance = client.instance(settings.spanner_instance_id)
        _database = instance.database(settings.spanner_database_id)
    return _database


def execute_gql_query(
    database,
    gql_statement: str,
    params: dict[str, Any] | None = None,
    param_types: dict[str, Any] | None = None,
) -> list[list[Any]]:
    """
    Executes a read-only ISO GQL or SQL query against Spanner snapshot.
    Used by Discovery Copilot for graph traversals and vector distance queries.
    """
    with database.snapshot() as snapshot:
        results = snapshot.execute_sql(
            gql_statement, params=params, param_types=param_types
        )
        return list(results)


def execute_read_write_transaction(
    database, callable_fn: Callable[[spanner.Transaction], Any]
) -> Any:
    """
    Executes a unit of work inside a Cloud Spanner serializable read-write transaction.
    Automatically handles Wound-Wait lock conflicts and Aborted retries with exponential backoff.
    """
    return database.run_in_transaction(callable_fn)
