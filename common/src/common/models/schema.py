"""Pydantic definitions for all 8 Node tables and 11 Edge tables in EnterpriseKnowledgeGraph."""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field

# ============================================================================
# NODE MODELS
# ============================================================================

class DocumentNode(BaseModel):
    """Document node in EnterpriseKnowledgeGraph (PDFs, Slide decks, Repositories)."""
    document_id: str = Field(..., max_length=256, description="Document identifier")
    file_name: str = Field(..., max_length=256)
    mime_type: str = Field(..., max_length=128)
    source_url: Optional[str] = Field(None, max_length=1024)
    gcs_uri: Optional[str] = Field(None, max_length=1024)
    status: Optional[str] = Field(None, max_length=32)
    repo_id: Optional[str] = Field(None, max_length=64)
    drive_file_id: Optional[str] = Field(None, max_length=256)

class ChunkNode(BaseModel):
    """Evidence chunk node (text, code, tables) with vector embedding and provenance."""
    chunk_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    file_id: Optional[str] = Field(None, max_length=64)
    modality: str = Field(default="TEXT", max_length=32)
    text_content: str
    page_number: Optional[int] = None
    slide_number: Optional[int] = None
    commit_sha: Optional[str] = Field(None, max_length=64)
    line_range: Optional[str] = Field(None, max_length=64)
    source_gcs_uri: Optional[str] = Field(None, max_length=1024)
    embedding_768: Optional[List[float]] = None

class SolutionCardNode(BaseModel):
    """Business solution card representing a customer architecture or project."""
    card_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    project_name: str = Field(..., max_length=256)
    business_problem: Optional[str] = None
    proposed_solution: Optional[str] = None
    implemented_solution: Optional[str] = None
    outcome: Optional[str] = None

class VisualAssetNode(BaseModel):
    """Visual asset (slide render, diagram, or figure)."""
    visual_id: str = Field(..., max_length=64)
    document_id: str = Field(..., max_length=256)
    page_number: Optional[int] = None
    slide_number: Optional[int] = None
    image_gcs_uri: str = Field(..., max_length=1024)
    description: Optional[str] = None
    asset_type: Optional[str] = Field("SLIDE", max_length=32)

class RepositoryNode(BaseModel):
    """GitHub repository node."""
    repo_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    url: str = Field(..., max_length=1024)
    default_branch: Optional[str] = Field("main", max_length=64)
    last_synced_commit: Optional[str] = Field(None, max_length=64)
    created_at: Optional[datetime] = None

class SourceFileNode(BaseModel):
    """Source file within a repository."""
    file_id: str = Field(..., max_length=64)
    repo_id: str = Field(..., max_length=64)
    file_path: str = Field(..., max_length=1024)
    language: Optional[str] = Field(None, max_length=64)
    commit_sha: str = Field(..., max_length=64)
    created_at: Optional[datetime] = None

class EntityNode(BaseModel):
    """Discovered business entity, concept, subsystem, or API."""
    entity_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    description: Optional[str] = None

class TechnologyNode(BaseModel):
    """Approved enterprise technology catalog entry."""
    tech_id: str = Field(..., max_length=64)
    name: str = Field(..., max_length=256)
    category: str = Field(..., max_length=64)
    canonical_version: Optional[str] = Field(None, max_length=64)

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

