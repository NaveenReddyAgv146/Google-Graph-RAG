"""Structured Grounded Answer Synthesizer using Gemini (Phase 4.3).

Synthesizes structured, evidence-grounded answers strictly constrained to an
authoritative EvidencePack. Enforces discrete GroundedClaim generation,
fact vs inference classification, metric grounding, and insufficient evidence tracking.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from google.genai import types

from common.config import settings
from app.synthesis.models import (
    ClaimType,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
)

logger = logging.getLogger("discovery_copilot.synthesis.synthesizer")

SYNTHESIS_SYSTEM_PROMPT = """You are an Enterprise Architecture Grounded Answer Synthesizer.

Your role is to produce a 100% evidence-grounded synthesis to answer the user's architectural query based EXCLUSIVELY on the provided Authoritative Evidence Pack.

STRICT CONSTRAINTS (ZERO-HALLUCINATION POLICY):
1. FACTUAL GROUNDING:
   - Every factual assertion MUST be an item in `claims` with claim_type="VERIFIED_FACT".
   - Every VERIFIED_FACT MUST include at least one EvidenceReference specifying the exact card_id, chunk_id, document_id, and exact_source_quote.
   - `exact_source_quote` MUST be an exact, literal, character-for-character quotation from the chunk's text content.
2. NUMERIC & METRIC INTEGRITY:
   - Every metric, percentage (e.g. 99.999% SLA), latency (sub-10ms), cost ($500k, 31k), or revenue figure MUST have the exact quote containing that number.
   - NEVER convert projected, proposed, or target outcomes (e.g. "Realistic Success Scenario projects $20-30M") into achieved outcomes (e.g. "Achieved $20-30M"). Always preserve the original modality.
3. CUSTOMER ISOLATION:
   - NEVER transfer technologies, metrics, or outcomes between customers (e.g. do NOT attribute Uber's Google Maps integration or radio dispatch to Canva or Dropbox).
4. ARCHITECTURAL INFERENCES:
   - Deductive reasoning must be classified as claim_type="ARCHITECTURAL_INFERENCE".
   - Inferences MUST list the exact statements of supporting verified facts under `supporting_fact_statements`.
   - Inferences MUST NOT introduce new ungrounded technologies, companies, or quantitative numbers.
5. INSUFFICIENT EVIDENCE & MISSING DETAILS:
   - If the user asks for information not present in the evidence pack (e.g. disaster recovery RTO/RPO, pricing details, or unmentioned technologies), do NOT guess.
   - Explicitly list these missing topics in `insufficient_evidence_notes` and `unsupported_aspects`.
6. PROVENANCE INTEGRITY:
   - Use ONLY card_id, chunk_id, and document_id values that exist in the provided Evidence Pack. NEVER invent IDs.
"""


class GroundedAnswerSynthesizer:
    """Invokes Gemini with structured output constraints to synthesize grounded answers."""

    def __init__(
        self,
        model_name: str | None = None,
        genai_client: Any | None = None,
        generator_override: Callable[[str, EvidencePack], SynthesisResult] | None = None,
    ) -> None:
        self._model_name = model_name or settings.reasoning_model
        self._genai_client = genai_client
        self._generator_override = generator_override

    def synthesize(
        self,
        query: str,
        evidence_pack: EvidencePack,
    ) -> SynthesisResult:
        """Synthesizes structured answer from authoritative evidence pack."""
        start_time = time.monotonic()

        # Handle empty evidence immediately (Fail-closed zero-hallucination contract)
        if not evidence_pack.cards or not evidence_pack.chunks_by_id:
            latency_ms = round((time.monotonic() - start_time) * 1000.0, 2)
            return SynthesisResult(
                summary=(
                    "No human-approved solutions or evidence chunks were found in the enterprise knowledge base "
                    f"matching the query: '{query}'. Access permissions and approved status are strictly enforced."
                ),
                claims=[],
                insufficient_evidence_notes=[
                    f"No verified solution cards or source chunks found matching '{query}' under current RBAC permissions."
                ],
                unsupported_aspects=[query],
                latency_ms=latency_ms,
            )

        # Support mock generator override for unit testing / benchmarks
        if self._generator_override:
            res = self._generator_override(query, evidence_pack)
            res.latency_ms = round((time.monotonic() - start_time) * 1000.0, 2)
            return res

        # Format evidence pack for model consumption
        evidence_context = self._format_evidence_context(evidence_pack)
        user_prompt = f"User Query: {query}\n\n{evidence_context}"

        try:
            client = self._get_client()
            response = client.models.generate_content(
                model=self._model_name,
                contents=user_prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYNTHESIS_SYSTEM_PROMPT,
                    temperature=0.1,  # Low temperature for deterministic grounding
                    response_mime_type="application/json",
                    response_schema=SynthesisResult,
                ),
            )

            latency_ms = round((time.monotonic() - start_time) * 1000.0, 2)

            if response.text:
                data = json.loads(response.text)
                result = SynthesisResult.model_validate(data)
                result.latency_ms = latency_ms
                return result
            else:
                raise ValueError("Gemini returned empty structured response text")

        except Exception as exc:
            logger.error("Synthesis failed with model '%s': %s", self._model_name, exc)
            latency_ms = round((time.monotonic() - start_time) * 1000.0, 2)
            # Deterministic fallback synthesis using verified card content
            return self._build_deterministic_fallback_synthesis(
                query=query,
                evidence_pack=evidence_pack,
                latency_ms=latency_ms,
                error_message=str(exc),
            )

    def _format_evidence_context(self, evidence_pack: EvidencePack) -> str:
        """Formats the authoritative EvidencePack into structured text for model input."""
        sections = [
            f"=== AUTHORITATIVE EVIDENCE PACK (Total Cards: {evidence_pack.total_cards}, Total Chunks: {evidence_pack.total_chunks}) ===",
            f"Pack ID: {evidence_pack.pack_id}",
            f"Intent: {evidence_pack.intent}",
            "",
        ]

        for idx, card in enumerate(evidence_pack.cards, 1):
            sections.append(f"--- CARD {idx}: {card.project_name} ---")
            sections.append(f"CardId: {card.card_id}")
            sections.append(f"Customer: {card.customer_name}")
            sections.append(f"DocumentId: {card.document_id}")
            sections.append(f"DocumentName: {card.document_name or 'N/A'}")
            sections.append(f"Technologies: {', '.join(card.technologies) if card.technologies else 'None specified'}")
            if card.business_problem:
                sections.append(f"Business Problem: {card.business_problem}")
            if card.proposed_solution:
                sections.append(f"Proposed Solution: {card.proposed_solution}")
            if card.implemented_solution:
                sections.append(f"Implemented Solution: {card.implemented_solution}")
            if card.outcome:
                sections.append(f"Outcome: {card.outcome}")
            sections.append("Verified Source Chunks:")

            for chk in card.evidence_chunks:
                anchor = f"Page {chk.page_number}" if chk.page_number else (f"Slide {chk.slide_number}" if chk.slide_number else "Section")
                sections.append(f"  * ChunkId: {chk.chunk_id} ({anchor}):")
                sections.append(f'    "{chk.text_content}"')
            sections.append("")

        return "\n".join(sections)

    def _build_deterministic_fallback_synthesis(
        self,
        query: str,
        evidence_pack: EvidencePack,
        latency_ms: float,
        error_message: str,
    ) -> SynthesisResult:
        """Constructs a deterministic, citation-verified fallback when model API fails."""
        claims: list[GroundedClaim] = []
        summary_lines: list[str] = [
            f"Architectural findings for query '{query}' compiled from verified customer solutions:",
            "",
        ]

        for card in evidence_pack.cards:
            summary_lines.append(f"### {card.customer_name} — {card.project_name}")
            if card.implemented_solution:
                summary_lines.append(f"**Implemented Solution:** {card.implemented_solution}")
            if card.outcome:
                summary_lines.append(f"**Outcome:** {card.outcome}")

            # Create grounded claim for first chunk
            if card.evidence_chunks:
                first_chunk = card.evidence_chunks[0]
                first_quote = first_chunk.text_content[:120].strip()
                if first_quote:
                    claims.append(
                        GroundedClaim(
                            statement=f"{card.customer_name} implemented {card.project_name}.",
                            claim_type=ClaimType.VERIFIED_FACT,
                            customer_name=card.customer_name,
                            card_id=card.card_id,
                            document_id=card.document_id,
                            evidence_refs=[
                                EvidenceReference(
                                    card_id=card.card_id,
                                    chunk_id=first_chunk.chunk_id,
                                    document_id=card.document_id,
                                    customer_name=card.customer_name,
                                    document_name=card.document_name,
                                    page_number=first_chunk.page_number,
                                    slide_number=first_chunk.slide_number,
                                    exact_source_quote=first_quote,
                                )
                            ],
                        )
                    )
            summary_lines.append("")

        return SynthesisResult(
            summary="\n".join(summary_lines).strip(),
            claims=claims,
            insufficient_evidence_notes=[
                f"Model generation encountered transient failure ({error_message}); assembled from verified records."
            ],
            unsupported_aspects=[],
            latency_ms=latency_ms,
        )

    def _get_client(self) -> Any:
        """Initializes or returns genai Client."""
        if self._genai_client is not None:
            return self._genai_client
        from google import genai
        return genai.Client()

