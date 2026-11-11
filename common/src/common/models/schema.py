"""Pydantic definitions for all 8 Node tables and 11 Edge tables in EnterpriseKnowledgeGraph."""

from datetime import datetime

from pydantic import BaseModel, Field

# ============================================================================
# NODE MODELS
# ============================================================================


class DocumentNode(BaseModel):
    """Document node in EnterpriseKnowledgeGraph (PDFs, Slide decks, Repositories)."""

    document_id: str = Field(..., max_length=256, description="Document identifier")
    file_name: str = Field(..., max_length=256)
    mime_type: str = Field(..., max_length=128)
    source_url: str | None = Field(None, max_length=1024)
    gcs_uri: str | None = Field(None, max_length=1024)
    status: str | None = Field(None, max_length=32)
    repo_id: str | None = Field(None, max_length=64)
    drive_file_id: str | None = Field(None, max_length=256)


class ChunkNode(BaseModel):
    """Evidence chunk node (text, code, tables) with vector embedding and provenance."""

    chunk_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    file_id: str | None = Field(None, max_length=64)
    modality: str = Field(default="TEXT", max_length=32)
    text_content: str
    page_number: int | None = None
    slide_number: int | None = None
    commit_sha: str | None = Field(None, max_length=64)
    line_range: str | None = Field(None, max_length=64)
    source_gcs_uri: str | None = Field(None, max_length=1024)
    embedding_768: list[float] | None = None


class SolutionCardNode(BaseModel):
    """Business solution card representing a customer architecture or project."""

    card_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    project_name: str = Field(..., max_length=256)
    business_problem: str | None = None
    proposed_solution: str | None = None
    implemented_solution: str | None = None
    outcome: str | None = None


class VisualAssetNode(BaseModel):
    """Visual asset (slide render, diagram, or figure)."""

    visual_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    page_number: int | None = None
    slide_number: int | None = None
    image_gcs_uri: str = Field(..., max_length=1024)
    description: str | None = None
    asset_type: str | None = Field("SLIDE", max_length=32)


class RepositoryNode(BaseModel):
    """GitHub repository node."""

    repo_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    url: str = Field(..., max_length=1024)
    default_branch: str | None = Field("main", max_length=64)
    last_synced_commit: str | None = Field(None, max_length=64)
    created_at: datetime | None = None


class SourceFileNode(BaseModel):
    """Source file within a repository."""

    file_id: str = Field(..., max_length=64)
    repo_id: str = Field(..., max_length=64)
    file_path: str = Field(..., max_length=1024)
    language: str | None = Field(None, max_length=64)
    commit_sha: str = Field(..., max_length=64)
    created_at: datetime | None = None


class EntityNode(BaseModel):
    """Discovered business entity, concept, subsystem, or API."""

    entity_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    description: str | None = None


class TechnologyNode(BaseModel):
    """Approved enterprise technology catalog entry."""

    tech_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    canonical_version: str | None = Field(None, max_length=64)


# ============================================================================
# EDGE MODELS
# ============================================================================


class DocumentContainsChunkEdge(BaseModel):
    document_id: str
    chunk_id: str


class RepoContainsFileEdge(BaseModel):
    repo_id: str
    file_id: str


class SourceFileContainsChunkEdge(BaseModel):
    file_id: str
    chunk_id: str


class ChunkReferencesEntityEdge(BaseModel):
    chunk_id: str
    entity_id: str


class EntityReferencesEntityEdge(BaseModel):
    source_entity_id: str
    target_entity_id: str


class ChunkMentionsTechEdge(BaseModel):
    chunk_id: str
    tech_id: str


class VisualAssetBelongsToDocEdge(BaseModel):
    visual_id: str
    document_id: str


class SolutionSupportsTechEdge(BaseModel):
    card_id: str
    tech_id: str


class SourceFileDefinesEntityEdge(BaseModel):
    file_id: str
    entity_id: str


class SourceFileCallsFileEdge(BaseModel):
    caller_file_id: str
    callee_file_id: str


class SolutionImplementsDocEdge(BaseModel):
    card_id: str
    document_id: str
