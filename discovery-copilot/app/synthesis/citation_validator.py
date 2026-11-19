"""Deterministic Citation and Grounding Validator (Phase 4.3).

Enforces:
1. Strict provenance and ID verification (CardId, ChunkId, DocumentId must exist in EvidencePack).
2. Customer attribution match (no cross-customer contamination, e.g. Canva claim cited by Uber chunk).
3. Verbatim exact source quote matching under canonical whitespace normalization.
4. Numerical metric verification (metrics must have matching verbatim quotes).
5. Distinguishes verified facts from architectural inference.
6. Zero invalid citations leak to final user summary.
"""

from __future__ import annotations

import logging
import re

from app.synthesis.models import (
    ClaimType,
    ClaimValidationDetail,
    ClaimValidationStatus,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
    ValidatedSynthesisResponse,
)

logger = logging.getLogger("discovery_copilot.synthesis.citation_validator")

# Regex to detect numerical metrics (percentages, latencies, dollar amounts, multiples)
NUMERIC_METRIC_PATTERN = re.compile(
    r"\b(\$?\d+(?:\.\d+)?%?|\d+\s*(?:ms|milliseconds?|seconds?|mins?|minutes?|gb|tb|pb|k|m|b|billion|million))\b",
    re.IGNORECASE,
)


def normalize_whitespace_and_case(text: str) -> str:
    """Canonical string normalization: collapses all consecutive whitespace and converts to lowercase."""
    if not text:
        return ""
    return " ".join(text.strip().split()).lower()


class CitationValidator:
    """Verifies synthesized claims against an authoritative EvidencePack."""

    def __init__(self, min_quote_length: int = 4) -> None:
        self._min_quote_length = min_quote_length

    def validate_synthesis(
        self,
        synthesis_result: SynthesisResult,
        evidence_pack: EvidencePack,
    ) -> ValidatedSynthesisResponse:
        """Validates all claims in a SynthesisResult against the authoritative EvidencePack."""
        validation_details: list[ClaimValidationDetail] = []
        validated_claims: list[GroundedClaim] = []
        rejected_claims: list[ClaimValidationDetail] = []

        total_quotes_checked = 0
        valid_quotes_count = 0
        misattributions_count = 0

        # Build lookup indices from authoritative EvidencePack
        known_cards = {c.card_id: c for c in evidence_pack.cards}
        known_chunks = evidence_pack.chunks_by_id

        for claim in synthesis_result.claims:
            claim_valid = True
            failure_status = ClaimValidationStatus.VALID
            failure_reason: str | None = None

            # Rule 1: VERIFIED_FACT requires at least one evidence reference
            if claim.claim_type == ClaimType.VERIFIED_FACT and not claim.evidence_refs:
                claim_valid = False
                failure_status = ClaimValidationStatus.MISSING_CITATION
                failure_reason = "VERIFIED_FACT claim lacks any evidence references."

            # Rule 2: ARCHITECTURAL_INFERENCE must link to supporting facts
            elif claim.claim_type == ClaimType.ARCHITECTURAL_INFERENCE:
                if not claim.supporting_fact_statements and not claim.evidence_refs:
                    claim_valid = False
                    failure_status = ClaimValidationStatus.UNSUPPORTED_INFERENCE
                    failure_reason = "ARCHITECTURAL_INFERENCE lacks supporting facts or direct evidence links."

            # Validate each evidence reference attached to this claim
            if claim_valid and claim.evidence_refs:
                for ref in claim.evidence_refs:
                    total_quotes_checked += 1

                    # Check CardId existence
                    if ref.card_id not in known_cards:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.INVALID_CARD_ID
                        failure_reason = f"CardId '{ref.card_id}' does not exist in authoritative EvidencePack."
                        break

                    owning_card = known_cards[ref.card_id]

                    # Check ChunkId existence in EvidencePack
                    if ref.chunk_id not in known_chunks:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.INVALID_CHUNK_ID
                        failure_reason = f"ChunkId '{ref.chunk_id}' does not exist in authoritative EvidencePack."
                        break

                    owning_chunk = known_chunks[ref.chunk_id]

                    # Check DocumentId match
                    if ref.document_id and ref.document_id != owning_chunk.document_id:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.DOCUMENT_MISMATCH
                        failure_reason = (
                            f"DocumentId mismatch: Ref says '{ref.document_id}', but chunk belongs to '{owning_chunk.document_id}'."
                        )
                        break

                    # Check Chunk-to-Card ownership
                    if owning_chunk.card_id != ref.card_id:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.INVALID_CARD_ID
                        failure_reason = (
                            f"Chunk '{ref.chunk_id}' belongs to card '{owning_chunk.card_id}', not '{ref.card_id}'."
                        )
                        break

                    # Check Customer attribution consistency (Anti-contamination rule)
                    # Check if claim or ref asserts customer X, but chunk is owned by customer Y
                    ref_customer = (ref.customer_name or claim.customer_name or "").strip().lower()
                    actual_customer = (owning_card.customer_name or "").strip().lower()

                    if ref_customer and actual_customer and ref_customer != actual_customer:
                        # Allow substring match (e.g. "uber" in "ubercab")
                        if ref_customer not in actual_customer and actual_customer not in ref_customer:
                            claim_valid = False
                            misattributions_count += 1
                            failure_status = ClaimValidationStatus.CUSTOMER_MISATTRIBUTION
                            failure_reason = (
                                f"Customer attribution mismatch: Claim attributes to '{ref.customer_name or claim.customer_name}', "
                                f"but evidence is owned by '{owning_card.customer_name}'."
                            )
                            break

                    # Check Verbatim Exact Source Quote Matching
                    quote_raw = ref.exact_source_quote or ""
                    if len(quote_raw.strip()) < self._min_quote_length:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.QUOTE_MISMATCH
                        failure_reason = f"Exact source quote is too short or empty (min {self._min_quote_length} chars)."
                        break

                    norm_quote = normalize_whitespace_and_case(quote_raw)
                    norm_chunk = normalize_whitespace_and_case(owning_chunk.text_content)

                    if norm_quote not in norm_chunk:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.QUOTE_MISMATCH
                        failure_reason = (
                            f"Quote mismatch: Normalized quote '{quote_raw}' was not found in chunk '{ref.chunk_id}'."
                        )
                        break

                    valid_quotes_count += 1

                    # Semantic Claim Compatibility Check
                    chunk_claim_t = owning_chunk.claim_type
                    ref_claim_t = getattr(ref, "claim_type", None)

                    # 1. Direct ClaimType mismatch check
                    if ref_claim_t and chunk_claim_t and ref_claim_t != chunk_claim_t:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.CLAIM_TYPE_MISMATCH
                        failure_reason = (
                            f"ClaimType mismatch: Reference requested '{ref_claim_t}', "
                            f"but cited chunk '{ref.chunk_id}' is tagged as '{chunk_claim_t}'."
                        )
                        break

                    # 2. Proposed vs. Implemented Protection
                    impl_keywords = ["implemented", "deployed", "in production", "shipped", "built and running"]
                    is_claiming_implementation = any(re.search(rf"\b{kw}\b", claim.statement, re.IGNORECASE) for kw in impl_keywords)
                    if is_claiming_implementation and chunk_claim_t == "PROPOSED_SOLUTION":
                        claim_valid = False
                        failure_status = ClaimValidationStatus.CLAIM_TYPE_MISMATCH
                        failure_reason = "Proposed architecture cannot be presented as implemented solution."
                        break

                    # 3. Projected vs. Observed Financial Outcome Protection
                    has_realized_action = any(
                        re.search(rf"\b{kw}\b", claim.statement, re.IGNORECASE)
                        for kw in ["generated", "achieved", "reached", "realized", "earned"]
                    )
                    has_financial_term = bool(re.search(r"(\$|revenue|profit)", claim.statement, re.IGNORECASE))
                    is_claiming_realized = has_realized_action and has_financial_term

                    projection_terms = ["projected", "scenario", "forecast", "target", "estimated", "potential"]
                    statement_marks_projection = any(
                        re.search(rf"\b{pt}\b", claim.statement, re.IGNORECASE) for pt in projection_terms
                    )

                    if is_claiming_realized and not statement_marks_projection and chunk_claim_t == "OUTCOME":
                        norm_chunk_text = owning_chunk.text_content.lower()
                        if any(pt in norm_chunk_text for pt in ["potential outcome", "best-case scenario", "realistic scenario", "worst-case scenario", "projected", "potential"]):
                            claim_valid = False
                            failure_status = ClaimValidationStatus.CLAIM_TYPE_MISMATCH
                            failure_reason = "Financial projections cannot be presented as verified historical outcomes."
                            break

            # Rule 3: Numeric Metrics Grounding check
            # If the claim statement mentions numbers/metrics, at least one quote must contain the metric
            if claim_valid and claim.claim_type == ClaimType.VERIFIED_FACT:
                metrics_in_claim = NUMERIC_METRIC_PATTERN.findall(claim.statement)
                if metrics_in_claim:
                    # Check that the numbers are backed by the quotes
                    combined_quotes = " ".join(
                        normalize_whitespace_and_case(r.exact_source_quote) for r in claim.evidence_refs
                    )
                    unsupported_metrics = [
                        m for m in metrics_in_claim if normalize_whitespace_and_case(m) not in combined_quotes
                    ]
                    if unsupported_metrics:
                        claim_valid = False
                        failure_status = ClaimValidationStatus.QUOTE_MISMATCH
                        failure_reason = (
                            f"Numeric metric(s) {unsupported_metrics} in claim '{claim.statement}' "
                            f"not supported by verbatim quotes."
                        )

            detail = ClaimValidationDetail(
                claim_id=claim.claim_id,
                statement=claim.statement,
                is_valid=claim_valid,
                status=failure_status,
                reason=failure_reason,
                evidence_ref_count=len(claim.evidence_refs),
            )
            validation_details.append(detail)

            if claim_valid:
                validated_claims.append(claim)
            else:
                rejected_claims.append(detail)

        # Compute evaluation metrics
        precision = (len(validated_claims) / len(synthesis_result.claims)) if synthesis_result.claims else 1.0
        quote_fidelity = (valid_quotes_count / total_quotes_checked) if total_quotes_checked > 0 else 1.0

        # Construct final validated summary:
        # Zero invalid citations may reach the user.
        # Summary must be constructed exclusively from validated claims.
        if validated_claims:
            clean_summary = self._build_summary_from_validated_claims(
                validated_claims=validated_claims,
                original_summary=synthesis_result.summary,
                has_rejected_claims=len(rejected_claims) > 0,
            )
            is_response_valid = len(rejected_claims) == 0
        else:
            clean_summary = (
                "Insufficient verified evidence was found to answer the query. "
                "Retrieved records did not meet grounding and citation integrity criteria."
            )
            is_response_valid = False

        return ValidatedSynthesisResponse(
            is_valid=is_response_valid,
            summary=clean_summary,
            validated_claims=validated_claims,
            rejected_claims=rejected_claims,
            insufficient_evidence_notes=synthesis_result.insufficient_evidence_notes,
            unsupported_aspects=synthesis_result.unsupported_aspects,
            validation_details=validation_details,
            precision=round(precision, 4),
            quote_fidelity=round(quote_fidelity, 4),
            misattribution_count=misattributions_count,
            latency_ms=synthesis_result.latency_ms,
        )

    def _build_summary_from_validated_claims(
        self,
        validated_claims: list[GroundedClaim],
        original_summary: str,
        has_rejected_claims: bool,
    ) -> str:
        """Reconstructs final answer strictly from validated claims if invalid claims were purged."""
        if not has_rejected_claims:
            return original_summary

        # Reconstruct clean markdown summary from validated claims
        lines: list[str] = [
            "### Verified Architectural Discovery Summary",
            "",
            "The following findings have been verified against authoritative knowledge base citations:",
            "",
        ]

        # Group by customer if present
        by_customer: dict[str, list[GroundedClaim]] = {}
        for c in validated_claims:
            cust = c.customer_name or "General Architecture"
            if cust not in by_customer:
                by_customer[cust] = []
            by_customer[cust].append(c)

        for cust, c_list in by_customer.items():
            lines.append(f"#### {cust}")
            for c in c_list:
                ref_tags = []
                for r in c.evidence_refs:
                    anchor = f"Page {r.page_number}" if r.page_number else (f"Slide {r.slide_number}" if r.slide_number else "")
                    tag_parts = [r.card_id]
                    if anchor:
                        tag_parts.append(anchor)
                    ref_tags.append(f"[{', '.join(tag_parts)}]")
                ref_str = " " + " ".join(ref_tags) if ref_tags else ""
                type_indicator = "[Inference] " if c.claim_type == ClaimType.ARCHITECTURAL_INFERENCE else ""
                lines.append(f"- {type_indicator}{c.statement}{ref_str}")
            lines.append("")

        return "\n".join(lines).strip()

