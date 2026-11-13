"""Enterprise Agentic GraphRAG - Structured Gemini Metadata Extractor.

Extracts business entities, technologies, solution cards, and explicit entity relationships
from document chunks using Gemini with strict Pydantic JSON schemas, deterministic evidence
quote verification, taxonomy resolution, deduplication, and bounded retries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import time
from typing import Any, Callable, Iterable, Sequence

from pydantic import BaseModel, Field

from common.config import Settings, settings as global_settings
from common.models.schema import ChunkNode, EntityNode, SolutionCardNode, TechnologyNode
from common.taxonomy import (
    MAX_ENTITY_CATEGORY_LENGTH,
    MAX_ENTITY_NAME_LENGTH,
    MAX_TECH_NAME_LENGTH,
    TaxonomyMapper,
    generate_entity_id,
    normalize_category,
    normalize_name,
)

logger = logging.getLogger("common.parsers.metadata_extractor")

DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


# ============================================================================
# TYPED DOMAIN EXCEPTIONS
# ============================================================================


class MetadataExtractionError(Exception):
    """Base exception for metadata extraction operations."""

    pass


class ExtractionInvalidInputError(MetadataExtractionError, ValueError):
    """Raised when extraction input parameters or chunks are invalid."""

    pass


class ExtractionRateLimitError(MetadataExtractionError):
    """Raised when Gemini API rate limits (HTTP 429) are exhausted after retries."""

    pass


class ExtractionServiceError(MetadataExtractionError):
    """Raised when transient Gemini service errors (HTTP 5xx) persist after retries."""

    pass


class ExtractionResponseMalformedError(MetadataExtractionError):
    """Raised when Gemini returns malformed JSON, empty text, or schema-violating data."""

    pass


# ============================================================================
# RAW PYDANTIC SCHEMAS (GEMINI JSON OUTPUT)
# ============================================================================


class RawEntityCandidate(BaseModel):
    """Raw entity candidate emitted by Gemini."""

    name: str = Field(..., description="Business concept, subsystem, API, or organization name")
    category: str = Field(default="CONCEPT", description="Entity category: CONCEPT, SUBSYSTEM, API, BUSINESS_UNIT, DOMAIN, STANDARD")
    description: str | None = Field(default=None, description="Concise description of the entity in context")
    chunk_id: str = Field(..., description="ID of chunk where this entity is mentioned")
    evidence_quote: str = Field(..., description="Verbatim quote from chunk text containing the entity")


class RawTechnologyCandidate(BaseModel):
    """Raw technology candidate emitted by Gemini."""

    name: str = Field(..., description="Technology name or acronym as mentioned, e.g. BigQuery, Spanner, k8s")
    chunk_id: str = Field(..., description="ID of chunk where this technology is mentioned")
    evidence_quote: str = Field(..., description="Verbatim quote from chunk text mentioning the technology")


class RawSolutionCardCandidate(BaseModel):
    """Raw business solution card candidate emitted by Gemini."""

    project_name: str = Field(..., description="Name of the customer project, architecture, or initiative")
    business_problem: str | None = Field(default=None, description="The business problem or challenge")
    proposed_solution: str | None = Field(default=None, description="The proposed solution architecture")
    implemented_solution: str | None = Field(default=None, description="The implemented technical solution")
    outcome: str | None = Field(default=None, description="The business or technical outcome/result")
    chunk_id: str = Field(..., description="Primary chunk ID providing evidence for this solution")
    evidence_quote: str = Field(..., description="Verbatim quote from chunk text grounding the solution")
    technologies: list[str] = Field(default_factory=list, description="Technologies mentioned in this solution")


class RawEntityRelationshipCandidate(BaseModel):
    """Raw explicit entity relationship candidate emitted by Gemini."""

    source_entity_name: str = Field(..., description="Source entity name")
    source_entity_category: str = Field(default="CONCEPT", description="Source entity category")
    target_entity_name: str = Field(..., description="Target entity name")
    target_entity_category: str = Field(default="CONCEPT", description="Target entity category")
    relationship_type: str = Field(default="REFERENCES", description="Relationship type, e.g. REFERENCES, USES, CALLS")
    chunk_id: str = Field(..., description="ID of chunk where this relationship is stated")
    evidence_quote: str = Field(..., description="Verbatim quote from chunk text grounding the relationship")


class RawMetadataExtractionResponse(BaseModel):
    """Strict JSON schema required from Gemini."""

    entities: list[RawEntityCandidate] = Field(default_factory=list)
    technologies: list[RawTechnologyCandidate] = Field(default_factory=list)
    solution_cards: list[RawSolutionCardCandidate] = Field(default_factory=list)
    relationships: list[RawEntityRelationshipCandidate] = Field(default_factory=list)


# ============================================================================
# VALIDATED DOMAIN MODELS
# ============================================================================


class RejectedCandidate(BaseModel):
    """Audit log of candidates rejected during verification."""

    candidate_type: str = Field(..., description="entity, technology, solution_card, or relationship")
    candidate_name: str
    chunk_id: str
    evidence_quote: str
    rejection_reason: str


class ValidatedEntity(BaseModel):
    """Validated business entity grounded in evidence."""

    entity_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=MAX_ENTITY_NAME_LENGTH)
    category: str = Field(..., max_length=MAX_ENTITY_CATEGORY_LENGTH)
    description: str | None = None
    chunk_id: str = Field(..., max_length=64)
    evidence_quote: str

    def to_node(self) -> EntityNode:
        return EntityNode(
            entity_id=self.entity_id,
            name=self.name,
            category=self.category,
            description=self.description,
        )


class ValidatedTechnology(BaseModel):
    """Validated technology reference grounded in evidence."""

    raw_name: str
    chunk_id: str = Field(..., max_length=64)
    evidence_quote: str
    tech_id: str | None = Field(default=None, max_length=64)
    canonical_name: str | None = Field(default=None, max_length=MAX_TECH_NAME_LENGTH)
    category: str | None = Field(default=None, max_length=64)
    is_canonical: bool = False
    unresolved: bool = True

    def to_node(self) -> TechnologyNode | None:
        if not self.is_canonical or not self.tech_id or not self.canonical_name or not self.category:
            return None
        return TechnologyNode(
            tech_id=self.tech_id,
            name=self.canonical_name,
            category=self.category,
        )


class ValidatedSolutionCard(BaseModel):
    """Validated business solution card grounded in evidence."""

    card_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    project_name: str = Field(..., max_length=256)
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None
    chunk_id: str = Field(..., max_length=64)
    evidence_quote: str
    technologies: list[str] = Field(default_factory=list, description="Associated canonical TechIds")

    def to_node(self) -> SolutionCardNode:
        return SolutionCardNode(
            card_id=self.card_id,
            document_id=self.document_id,
            project_name=self.project_name,
            business_problem=self.business_problem,
            proposed_solution=self.proposed_solution,
            implemented_solution=self.implemented_solution,
            outcome=self.outcome,
        )


class ValidatedEntityRelationship(BaseModel):
    """Validated explicit relationship between two entities."""

    source_entity_id: str = Field(..., max_length=64)
    target_entity_id: str = Field(..., max_length=64)
    source_name: str
    target_name: str
    relationship_type: str = Field(default="REFERENCES", max_length=64)
    chunk_id: str = Field(..., max_length=64)
    evidence_quote: str


class ExtractedMetadataResult(BaseModel):
    """Complete structured metadata extracted from a document or chunk."""

    document_id: str
    entities: list[ValidatedEntity] = Field(default_factory=list)
    technologies: list[ValidatedTechnology] = Field(default_factory=list)
    solution_cards: list[ValidatedSolutionCard] = Field(default_factory=list)
    relationships: list[ValidatedEntityRelationship] = Field(default_factory=list)
    unresolved_technologies: list[str] = Field(default_factory=list)
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)


# ============================================================================
# DETERMINISTIC HELPERS
# ============================================================================


def generate_card_id(document_id: str, project_name: str) -> str:
    """Generate a deterministic canonical CardId <= 64 chars."""
    norm_doc = document_id.strip().lower()
    norm_proj = normalize_name(project_name).lower()
    raw = f"{norm_doc}:{norm_proj}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"card_{digest}"


def verify_evidence_quote(quote: str, chunk_text: str) -> bool:
    """Verify that an evidence quote is grounded in the chunk text using substring matching.

    Supports exact substring and normalized-whitespace substring matching.
    """
    if not quote or not isinstance(quote, str) or not quote.strip():
        return False
    if not chunk_text or not isinstance(chunk_text, str) or not chunk_text.strip():
        return False

    cleaned_quote = quote.strip()

    # Strip matching outer quotes if LLM enclosed them
    if (cleaned_quote.startswith('"') and cleaned_quote.endswith('"')) or (
        cleaned_quote.startswith("'") and cleaned_quote.endswith("'")
    ):
        cleaned_quote = cleaned_quote[1:-1].strip()

    if not cleaned_quote:
        return False

    # 1. Exact match
    if cleaned_quote in chunk_text:
        return True

    # 2. Whitespace-collapsed match
    norm_quote = re.sub(r"\s+", " ", cleaned_quote)
    norm_chunk = re.sub(r"\s+", " ", chunk_text)
    if norm_quote in norm_chunk:
        return True

    # 3. Case-insensitive whitespace-collapsed match as final fallback
    if norm_quote.lower() in norm_chunk.lower():
        return True

    return False


def _extract_status_code(exc: Exception) -> int | None:
    """Extract integer HTTP status code from generic exceptions."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    message = str(exc)
    for c in (400, 403, 404, 429, 500, 502, 503, 504):
        if str(c) in message:
            return c
    return None


# ============================================================================
# SYSTEM PROMPTS
# ============================================================================

METADATA_EXTRACTION_SYSTEM_INSTRUCTION = """You are a precision enterprise knowledge graph extractor.
Analyze the provided text chunks and extract:
1. Business/Domain Entities (concepts, subsystems, APIs, business units, standards).
2. Technologies mentioned (databases, cloud services, tools, frameworks, programming languages).
3. Business Solution Cards (customer architectures, problems, solutions, outcomes).
4. Explicit Relationships between entities (e.g. REFERENCES, USES, CALLS, DEPENDS_ON, CONNECTS_TO).
   - Ensure the relationship type accurately reflects the semantic meaning grounded in the quote.
   - For network, routing, or physical connectivity, use CONNECTS_TO. Do NOT use CALLS unless explicit service calls, RPCs, or API requests are stated in the quote.

CRITICAL GROUNDING INVARIANTS:
- Every candidate MUST include the exact source ChunkId and an exact verbatim quote from that chunk.
- Do NOT hallucinate or assume facts outside the provided text.
- If no problem or outcome is stated in the text, omit or set to null.
- Output strictly in valid JSON matching the provided schema.
"""


# ============================================================================
# METADATA EXTRACTOR ENGINE
# ============================================================================


class MetadataExtractor:
    """Structured Gemini Metadata Extractor with deterministic evidence verification and taxonomy mapping.

    Invariants:
    1. Schema Enforcement: Strict Pydantic JSON schema output.
    2. Exact Grounding: Rejects any candidate whose quote cannot be found in the source chunk text.
    3. Taxonomy Integration: Resolves technologies against approved catalog; unknown technologies remain explicitly unresolved.
    4. Deterministic Deduplication: Deduplicates entities, technologies, solutions, and relationships by canonical IDs.
    5. Deterministic Sorting: All output collections are sorted deterministically.
    6. Bounded Retries: Retries transient 429 and 5xx errors with jitter.
    7. Offline Testability: Fully injectable client, taxonomy mapper, and sleeper.
    8. Zero Side Effects: Does not write to Spanner or GCS.
    """

    def __init__(
        self,
        client: Any | None = None,
        model_name: str | None = None,
        project_id: str | None = None,
        location: str | None = None,
        taxonomy_mapper: TaxonomyMapper | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or global_settings
        self.project_id = project_id or cfg.project_id
        self.location = location or cfg.region
        self.model_name = model_name or cfg.reasoning_model
        self.taxonomy_mapper = taxonomy_mapper or TaxonomyMapper()
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self._sleeper = sleeper
        self._client = client

    @property
    def client(self) -> Any:
        """Lazily initializes the google.genai Client configured for Vertex AI."""
        if self._client is None:
            from google import genai

            logger.info(
                "Initializing GenAI Client for MetadataExtractor (project=%s, location=%s)",
                self.project_id,
                self.location,
            )
            self._client = genai.Client(
                vertexai=True,
                project=self.project_id,
                location=self.location,
            )
        return self._client

    def extract_from_chunk(
        self,
        chunk_id: str,
        text: str,
        document_id: str = "",
    ) -> ExtractedMetadataResult:
        """Extract structured metadata from a single chunk."""
        if not chunk_id or not chunk_id.strip():
            raise ExtractionInvalidInputError("chunk_id cannot be empty.")
        if not text or not isinstance(text, str) or not text.strip():
            # Empty text chunk: return empty result with 0 API calls
            return ExtractedMetadataResult(document_id=document_id)

        chunks_map = {chunk_id.strip(): text.strip()}
        return self._extract_metadata(document_id=document_id, chunks_map=chunks_map)

    def extract_from_document(
        self,
        document_id: str,
        chunks: Sequence[ChunkNode | dict[str, Any] | tuple[str, str]],
    ) -> ExtractedMetadataResult:
        """Extract structured metadata from all non-empty text chunks of a document."""
        if not document_id or not document_id.strip():
            raise ExtractionInvalidInputError("document_id cannot be empty.")

        chunks_map: dict[str, str] = {}
        for item in chunks:
            if isinstance(item, ChunkNode):
                if item.text_content and item.text_content.strip():
                    chunks_map[item.chunk_id] = item.text_content.strip()
            elif isinstance(item, dict):
                cid = item.get("chunk_id") or item.get("ChunkId")
                txt = item.get("text_content") or item.get("TextContent")
                if cid and txt and isinstance(txt, str) and txt.strip():
                    chunks_map[str(cid).strip()] = txt.strip()
            elif isinstance(item, tuple) and len(item) == 2:
                cid, txt = item
                if cid and txt and isinstance(txt, str) and txt.strip():
                    chunks_map[str(cid).strip()] = txt.strip()
            else:
                raise ExtractionInvalidInputError(f"Unsupported chunk type: {type(item).__name__}")

        if not chunks_map:
            # Zero text chunks to process
            return ExtractedMetadataResult(document_id=document_id)

        return self._extract_metadata(document_id=document_id, chunks_map=chunks_map)

    def _extract_metadata(
        self,
        document_id: str,
        chunks_map: dict[str, str],
    ) -> ExtractedMetadataResult:
        """Internal worker executing model invocation, retry loop, and validation."""
        user_prompt = self._build_prompt(document_id, chunks_map)
        raw_response = self._call_model_with_retries(user_prompt)
        return self._validate_and_ground_candidates(document_id, chunks_map, raw_response)

    def _build_prompt(self, document_id: str, chunks_map: dict[str, str]) -> str:
        """Construct the prompt containing chunks labeled with ChunkId."""
        prompt_parts = [
            f"Extract structured metadata for Document ID '{document_id}'.",
            "Below are the text chunks available for extraction:\n",
        ]
        for cid, text in chunks_map.items():
            prompt_parts.append(f"--- START CHUNK [{cid}] ---")
            prompt_parts.append(text)
            prompt_parts.append(f"--- END CHUNK [{cid}] ---\n")

        prompt_parts.append("Extract all entities, technologies, solutions, and relationships with exact ChunkId and quotes.")
        return "\n".join(prompt_parts)

    def _call_model_with_retries(self, user_prompt: str) -> RawMetadataExtractionResponse:
        """Execute Gemini API call with bounded retries for transient errors."""
        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=RawMetadataExtractionResponse,
            system_instruction=METADATA_EXTRACTION_SYSTEM_INSTRUCTION,
        )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=user_prompt,
                    config=config,
                )
                return self._parse_model_response(response)
            except (ExtractionResponseMalformedError, ExtractionInvalidInputError):
                # Validation / malformed responses are not network-retried
                raise
            except Exception as exc:
                last_error = exc
                status_code = _extract_status_code(exc)

                # Non-retryable HTTP codes
                if status_code in {400, 401, 403, 404}:
                    logger.error("Non-retryable Gemini error (code=%s): %s", status_code, exc)
                    raise MetadataExtractionError(
                        f"Non-retryable Gemini error (code={status_code}): {exc}"
                    ) from exc

                # Check if retryable
                is_retryable = status_code in RETRYABLE_STATUS_CODES or "429" in str(exc) or "503" in str(exc)
                if not is_retryable or attempt >= self.max_retries:
                    break

                backoff = self.initial_backoff * (2**attempt) + random.uniform(0.0, 0.1)
                logger.warning(
                    "Transient Gemini error (attempt %d/%d, code=%s): %s. Backing off %.2fs",
                    attempt + 1,
                    self.max_retries,
                    status_code,
                    exc,
                    backoff,
                )
                self._sleeper(backoff)

        status_code = _extract_status_code(last_error) if last_error else None
        if status_code == 429:
            raise ExtractionRateLimitError(
                f"Gemini API rate limit exceeded after {self.max_retries} retries: {last_error}"
            ) from last_error
        raise ExtractionServiceError(
            f"Gemini service error after {self.max_retries} retries (code={status_code}): {last_error}"
        ) from last_error

    def _parse_model_response(self, response: Any) -> RawMetadataExtractionResponse:
        """Parse raw model response into RawMetadataExtractionResponse."""
        if response is None:
            raise ExtractionResponseMalformedError("Model response was None.")

        # Extract text content
        text = getattr(response, "text", None)
        if text is None:
            # Fallback to candidates inspection
            candidates = getattr(response, "candidates", None)
            if candidates and len(candidates) > 0:
                first_cand = candidates[0]
                content = getattr(first_cand, "content", None)
                parts = getattr(content, "parts", None) if content else None
                if parts and len(parts) > 0:
                    text = getattr(parts[0], "text", None)

        if not text or not isinstance(text, str) or not text.strip():
            raise ExtractionResponseMalformedError("Model returned empty or whitespace-only response text.")

        cleaned = text.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
            cleaned = re.sub(r"\n```$", "", cleaned).strip()

        try:
            return RawMetadataExtractionResponse.model_validate_json(cleaned)
        except Exception as exc:
            raise ExtractionResponseMalformedError(
                f"Failed to parse Gemini response into RawMetadataExtractionResponse: {exc}\nResponse: {cleaned[:200]}"
            ) from exc

    def _validate_and_ground_candidates(
        self,
        document_id: str,
        chunks_map: dict[str, str],
        raw: RawMetadataExtractionResponse,
    ) -> ExtractedMetadataResult:
        """Validate evidence quotes, resolve technologies, ground relationships, and deduplicate."""
        validated_entities: dict[str, ValidatedEntity] = {}
        validated_techs: dict[tuple[str, str], ValidatedTechnology] = {}  # (chunk_id, tech_key) -> ValidatedTechnology
        validated_cards: dict[str, ValidatedSolutionCard] = {}
        validated_rels: dict[tuple[str, str], ValidatedEntityRelationship] = {}
        unresolved_tech_set: set[str] = set()
        rejected_candidates: list[RejectedCandidate] = []

        # --------------------------------------------------------------------
        # 1. ENTITIES VALIDATION & GROUNDING
        # --------------------------------------------------------------------
        for cand in raw.entities:
            chunk_text = chunks_map.get(cand.chunk_id)
            if chunk_text is None:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="entity",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"ChunkId '{cand.chunk_id}' does not exist in document.",
                    )
                )
                continue

            if not verify_evidence_quote(cand.evidence_quote, chunk_text):
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="entity",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason="Evidence quote not found in source chunk text.",
                    )
                )
                continue

            try:
                norm_name = normalize_name(cand.name)
                norm_cat = normalize_category(cand.category)
            except Exception as e:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="entity",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"Invalid name or category normalization: {e}",
                    )
                )
                continue

            entity_id = generate_entity_id(norm_cat, norm_name)
            desc = cand.description.strip() if (cand.description and cand.description.strip()) else None

            # Deduplication: preserve longest description if already present
            if entity_id in validated_entities:
                existing = validated_entities[entity_id]
                if not existing.description and desc:
                    validated_entities[entity_id] = ValidatedEntity(
                        entity_id=entity_id,
                        name=norm_name,
                        category=norm_cat,
                        description=desc,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                    )
            else:
                validated_entities[entity_id] = ValidatedEntity(
                    entity_id=entity_id,
                    name=norm_name,
                    category=norm_cat,
                    description=desc,
                    chunk_id=cand.chunk_id,
                    evidence_quote=cand.evidence_quote,
                )

        # --------------------------------------------------------------------
        # 2. TECHNOLOGIES VALIDATION & TAXONOMY RESOLUTION
        # --------------------------------------------------------------------
        for cand in raw.technologies:
            chunk_text = chunks_map.get(cand.chunk_id)
            if chunk_text is None:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="technology",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"ChunkId '{cand.chunk_id}' does not exist in document.",
                    )
                )
                continue

            if not verify_evidence_quote(cand.evidence_quote, chunk_text):
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="technology",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason="Evidence quote not found in source chunk text.",
                    )
                )
                continue

            try:
                norm_raw_name = normalize_name(cand.name)
            except Exception as e:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="technology",
                        candidate_name=cand.name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"Invalid technology name normalization: {e}",
                    )
                )
                continue

            # Resolve through Phase 3C.3 Taxonomy Mapper
            entry = self.taxonomy_mapper.resolve_technology(norm_raw_name, strict=False)
            if entry is not None:
                tech_val = ValidatedTechnology(
                    raw_name=norm_raw_name,
                    chunk_id=cand.chunk_id,
                    evidence_quote=cand.evidence_quote,
                    tech_id=entry.tech_id,
                    canonical_name=entry.canonical_name,
                    category=entry.category,
                    is_canonical=True,
                    unresolved=False,
                )
                validated_techs[(cand.chunk_id, entry.tech_id)] = tech_val
            else:
                # Unknown technology: remains explicitly unresolved
                tech_val = ValidatedTechnology(
                    raw_name=norm_raw_name,
                    chunk_id=cand.chunk_id,
                    evidence_quote=cand.evidence_quote,
                    tech_id=None,
                    canonical_name=None,
                    category=None,
                    is_canonical=False,
                    unresolved=True,
                )
                validated_techs[(cand.chunk_id, norm_raw_name.lower())] = tech_val
                unresolved_tech_set.add(norm_raw_name)

        # --------------------------------------------------------------------
        # 3. SOLUTION CARDS VALIDATION & GROUNDING
        # --------------------------------------------------------------------
        for cand in raw.solution_cards:
            chunk_text = chunks_map.get(cand.chunk_id)
            if chunk_text is None:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="solution_card",
                        candidate_name=cand.project_name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"ChunkId '{cand.chunk_id}' does not exist in document.",
                    )
                )
                continue

            if not verify_evidence_quote(cand.evidence_quote, chunk_text):
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="solution_card",
                        candidate_name=cand.project_name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason="Evidence quote not found in source chunk text.",
                    )
                )
                continue

            try:
                norm_proj = normalize_name(cand.project_name)
            except Exception as e:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="solution_card",
                        candidate_name=cand.project_name,
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"Invalid project_name normalization: {e}",
                    )
                )
                continue

            card_id = generate_card_id(document_id, norm_proj)

            # Resolve associated technologies to canonical TechIds
            resolved_card_techs: list[str] = []
            for t_raw in cand.technologies:
                try:
                    t_entry = self.taxonomy_mapper.resolve_technology(t_raw, strict=False)
                    if t_entry:
                        resolved_card_techs.append(t_entry.tech_id)
                    else:
                        unresolved_tech_set.add(normalize_name(t_raw))
                except Exception:
                    continue

            resolved_card_techs = sorted(set(resolved_card_techs))

            card = ValidatedSolutionCard(
                card_id=card_id,
                document_id=document_id,
                project_name=norm_proj,
                business_problem=cand.business_problem.strip() if cand.business_problem else None,
                proposed_solution=cand.proposed_solution.strip() if cand.proposed_solution else None,
                implemented_solution=cand.implemented_solution.strip() if cand.implemented_solution else None,
                outcome=cand.outcome.strip() if cand.outcome else None,
                chunk_id=cand.chunk_id,
                evidence_quote=cand.evidence_quote,
                technologies=resolved_card_techs,
            )
            validated_cards[card_id] = card

        # --------------------------------------------------------------------
        # 4. RELATIONSHIPS VALIDATION & GROUNDING
        # --------------------------------------------------------------------
        for cand in raw.relationships:
            chunk_text = chunks_map.get(cand.chunk_id)
            if chunk_text is None:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="relationship",
                        candidate_name=f"{cand.source_entity_name} -> {cand.target_entity_name}",
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"ChunkId '{cand.chunk_id}' does not exist in document.",
                    )
                )
                continue

            if not verify_evidence_quote(cand.evidence_quote, chunk_text):
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="relationship",
                        candidate_name=f"{cand.source_entity_name} -> {cand.target_entity_name}",
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason="Evidence quote not found in source chunk text.",
                    )
                )
                continue

            try:
                src_name = normalize_name(cand.source_entity_name)
                src_cat = normalize_category(cand.source_entity_category)
                tgt_name = normalize_name(cand.target_entity_name)
                tgt_cat = normalize_category(cand.target_entity_category)
            except Exception as e:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="relationship",
                        candidate_name=f"{cand.source_entity_name} -> {cand.target_entity_name}",
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason=f"Invalid entity normalization in relationship: {e}",
                    )
                )
                continue

            src_id = generate_entity_id(src_cat, src_name)
            tgt_id = generate_entity_id(tgt_cat, tgt_name)

            if src_id == tgt_id:
                rejected_candidates.append(
                    RejectedCandidate(
                        candidate_type="relationship",
                        candidate_name=f"{src_name} -> {tgt_name}",
                        chunk_id=cand.chunk_id,
                        evidence_quote=cand.evidence_quote,
                        rejection_reason="Self-referential relationships are rejected.",
                    )
                )
                continue

            rel_type = cand.relationship_type.strip().upper() if cand.relationship_type else "REFERENCES"

            # Semantics check: normalize CALLS to CONNECTS_TO if evidence only describes connectivity/paths
            ev_lower = cand.evidence_quote.lower()
            if rel_type == "CALLS" and ("connect" in ev_lower or "path" in ev_lower) and not any(k in ev_lower for k in ("call", "invok", "rpc", "request", "api call")):
                rel_type = "CONNECTS_TO"

            # Register relationship
            validated_rels[(src_id, tgt_id)] = ValidatedEntityRelationship(
                source_entity_id=src_id,
                target_entity_id=tgt_id,
                source_name=src_name,
                target_name=tgt_name,
                relationship_type=rel_type,
                chunk_id=cand.chunk_id,
                evidence_quote=cand.evidence_quote,
            )

        # --------------------------------------------------------------------
        # 5. DETERMINISTIC SORTING OF ALL COLLECTIONS
        # --------------------------------------------------------------------
        sorted_entities = sorted(validated_entities.values(), key=lambda e: e.entity_id)
        sorted_techs = sorted(
            validated_techs.values(),
            key=lambda t: (t.tech_id or "", t.raw_name, t.chunk_id),
        )
        sorted_cards = sorted(validated_cards.values(), key=lambda c: c.card_id)
        sorted_rels = sorted(
            validated_rels.values(),
            key=lambda r: (r.source_entity_id, r.target_entity_id),
        )
        sorted_unresolved = sorted(unresolved_tech_set)
        sorted_rejected = sorted(
            rejected_candidates,
            key=lambda r: (r.candidate_type, r.candidate_name, r.chunk_id),
        )

        return ExtractedMetadataResult(
            document_id=document_id,
            entities=sorted_entities,
            technologies=sorted_techs,
            solution_cards=sorted_cards,
            relationships=sorted_rels,
            unresolved_technologies=sorted_unresolved,
            rejected_candidates=sorted_rejected,
        )
