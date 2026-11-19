"""Regression tests for EnrichmentWriter Invariant 6 protection.

Verifies that both APPROVED and REJECTED cards are completely protected
from automated overwrite or reactivation, including when force_recompute=True.
"""

import pytest
from common.database.enrichment_writer import (
    EnrichmentPayload,
    EnrichmentWriter,
)
from common.parsers.metadata_extractor import ValidatedSolutionCard


class MockSpannerTransaction:
    """Mock Spanner read-write transaction tracking buffered mutations and sql queries."""

    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state
        self.updates = {}
        self.inserts = {}

    def execute_sql(self, sql: str, params=None, param_types=None):
        if "FROM Documents" in sql:
            doc_id = params.get("doc_id")
            for row in self.state.get("Documents", []):
                if row[0] == doc_id:
                    return [row]
            return []
        if "FROM SolutionCards" in sql:
            doc_id = params.get("doc_id")
            rows = []
            for row in self.state.get("SolutionCards", []):
                if row[1] == doc_id:
                    rows.append(row)
            return rows
        if "FROM EvidenceChunks" in sql:
            return self.state.get("EvidenceChunks", [])
        if "FROM Entities" in sql:
            return self.state.get("Entities", [])
        if "FROM Technologies" in sql:
            return self.state.get("Technologies", [])
        return []

    def update(self, table, columns, values):
        self.updates.setdefault(table, []).extend(values)

    def insert(self, table, columns, values):
        self.inserts.setdefault(table, []).extend(values)

    def buffer_write(self, mutations):
        pass


class MockSpannerDatabase:
    def __init__(self, state: dict[str, list[tuple]]):
        self.state = state
        self.last_transaction = None

    def run_in_transaction(self, func):
        txn = MockSpannerTransaction(self.state)
        self.last_transaction = txn
        return func(txn)


def make_card_state(doc_id: str, card_status: str, card_id: str = "card_test"):
    return {
        "Documents": [(doc_id, "READY")],
        "EvidenceChunks": [
            ("chk_1", "TEXT", "Modernized using Cloud Spanner", 1, None, None, doc_id),
        ],
        "Entities": [],
        "Technologies": [],
        "SolutionCards": [
            (
                card_id,
                doc_id,
                "Audited Project Name",
                "Audited Business Problem",
                "Audited Proposed Solution",
                "Audited Implemented Solution",
                "Audited Outcome",
                card_status,
                f"gs://bucket/metadata/{doc_id}.json",
            )
        ],
        "SolutionImplementsDoc": [(card_id, doc_id)],
        "ChunkReferencesEntity": [],
        "ChunkMentionsTech": [],
        "SolutionSupportsTech": [],
        "EntityReferencesEntity": [],
        "EvidenceLinks": [],
    }


def test_approved_card_protected_from_force_recompute():
    """Verify APPROVED card cannot be overwritten even when force_recompute=True."""
    doc_id = "doc_eval"
    state = make_card_state(doc_id, card_status="APPROVED", card_id="card_appr")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id=doc_id,
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_appr",
                document_id=doc_id,
                project_name="Malicious Overwrite",
                business_problem="Overwritten Problem",
                proposed_solution="Overwritten Proposed",
                implemented_solution="Overwritten Implemented",
                outcome="Overwritten Outcome",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            )
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=True)
    assert result.solution_cards_protected == 1
    assert result.solution_cards_updated == 0
    assert result.solution_cards_inserted == 0

    txn = db.last_transaction
    assert "SolutionCards" not in txn.updates


def test_rejected_card_protected_from_automated_reactivation():
    """Verify REJECTED card is protected and cannot be silently reactivated or overwritten."""
    doc_id = "doc_eval"
    state = make_card_state(doc_id, card_status="REJECTED", card_id="card_rej")
    db = MockSpannerDatabase(state)
    writer = EnrichmentWriter(database=db)

    payload = EnrichmentPayload(
        document_id=doc_id,
        solution_cards=[
            ValidatedSolutionCard(
                card_id="card_rej",
                document_id=doc_id,
                project_name="Reactivation Attempt",
                business_problem="Reactivation Problem",
                proposed_solution="Reactivation Proposed",
                implemented_solution="Reactivation Implemented",
                outcome="Reactivation Outcome",
                chunk_id="chk_1",
                evidence_quote="Modernized using Cloud Spanner",
            )
        ],
    )

    result = writer.persist_enrichment(payload, force_recompute=True)
    assert result.solution_cards_protected == 1
    assert result.solution_cards_updated == 0
    assert result.solution_cards_inserted == 0

    txn = db.last_transaction
    assert "SolutionCards" not in txn.updates

