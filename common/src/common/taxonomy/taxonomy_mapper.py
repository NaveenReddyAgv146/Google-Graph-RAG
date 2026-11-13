"""Enterprise Agentic GraphRAG - Deterministic Taxonomy & Technology Catalog.

Provides deterministic normalization, canonical catalog lookup, alias resolution,
Spanner constraint validation, and deterministic EntityId/TechId generation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field

from common.models.schema import EntityNode, TechnologyNode


# ============================================================================
# SPANNER SCHEMA CONSTANTS & LIMITS
# ============================================================================

MAX_TECH_ID_LENGTH = 64
MAX_TECH_NAME_LENGTH = 256
MAX_TECH_CATEGORY_LENGTH = 64
MAX_CANONICAL_VERSION_LENGTH = 64

MAX_ENTITY_ID_LENGTH = 64
MAX_ENTITY_NAME_LENGTH = 256
MAX_ENTITY_CATEGORY_LENGTH = 64


# ============================================================================
# EXCEPTION HIERARCHY
# ============================================================================


class TaxonomyError(Exception):
    """Base exception for taxonomy and technology catalog operations."""

    pass


class UnknownTechnologyError(TaxonomyError):
    """Raised when one or more technologies cannot be resolved to the catalog."""

    def __init__(self, message: str, unknown_names: list[str] | None = None) -> None:
        super().__init__(message)
        self.unknown_names = unknown_names or []


class CatalogCollisionError(TaxonomyError):
    """Raised when registering an entry that collides with an existing ID or alias."""

    pass


class SpannerConstraintViolationError(TaxonomyError):
    """Raised when mapped entity/technology fields violate Spanner length constraints."""

    pass


class InvalidNormalizationError(TaxonomyError):
    """Raised when text provided for normalization is invalid or empty."""

    pass


# ============================================================================
# NORMALIZATION UTILITIES
# ============================================================================


def normalize_name(name: str) -> str:
    """Normalize a display/canonical name.

    Strips surrounding whitespace and outer quotes, collapses internal whitespace,
    and removes non-semantic trailing punctuation (such as trailing period/comma).
    Preserves meaningful technical characters like '+', '#', '.', '-', '/'.
    """
    if not isinstance(name, str):
        raise InvalidNormalizationError(f"Name must be a string, got {type(name).__name__}.")
    cleaned = name.strip()
    if not cleaned:
        raise InvalidNormalizationError("Name cannot be empty or whitespace-only.")

    # Strip matching outer quotes: "foo" -> foo, 'bar' -> bar, “baz” -> baz
    quote_pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]
    for q_open, q_close in quote_pairs:
        if cleaned.startswith(q_open) and cleaned.endswith(q_close) and len(cleaned) >= 2:
            cleaned = cleaned[1:-1].strip()
            break

    # Strip surrounding parentheses if wrapping the entire string: (GCP) -> GCP
    if cleaned.startswith("(") and cleaned.endswith(")") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1].strip()

    # Strip trailing punctuation if not part of a technical symbol like C++ or .NET
    while cleaned and cleaned[-1] in {",", ";", ":"} or (
        cleaned.endswith(".") and not cleaned.lower().endswith(".js") and not cleaned.lower().endswith(".net")
    ):
        cleaned = cleaned[:-1].strip()

    # Collapse internal whitespace (spaces, tabs, newlines)
    cleaned = re.sub(r"\s+", " ", cleaned)

    if not cleaned:
        raise InvalidNormalizationError("Name cannot be empty after punctuation stripping.")

    if len(cleaned) > MAX_TECH_NAME_LENGTH:
        raise SpannerConstraintViolationError(
            f"Normalized name exceeds maximum length of {MAX_TECH_NAME_LENGTH} chars (length={len(cleaned)})."
        )

    return cleaned


def normalize_category(category: str) -> str:
    """Normalize an entity or technology category.

    Trims, collapses internal whitespace, and validates length constraint.
    """
    if not isinstance(category, str):
        raise InvalidNormalizationError(f"Category must be a string, got {type(category).__name__}.")
    cleaned = category.strip()
    if not cleaned:
        raise InvalidNormalizationError("Category cannot be empty or whitespace-only.")

    cleaned = re.sub(r"\s+", " ", cleaned)

    if len(cleaned) > MAX_ENTITY_CATEGORY_LENGTH:
        raise SpannerConstraintViolationError(
            f"Normalized category exceeds maximum length of {MAX_ENTITY_CATEGORY_LENGTH} chars (length={len(cleaned)})."
        )

    return cleaned


def normalize_lookup_key(text: str) -> str:
    """Normalize text into an alias lookup key.

    Case-folded, trimmed, collapsed whitespace, with outer quotes and punctuation stripped.
    """
    if not isinstance(text, str):
        raise InvalidNormalizationError(f"Lookup text must be a string, got {type(text).__name__}.")
    norm = normalize_name(text).lower()

    # Normalize hyphens/underscores to spaces for consistent matching while keeping special chars
    # E.g. 'pub-sub' -> 'pub-sub' is preserved, but we collapse repeated hyphens
    norm = re.sub(r"-+", "-", norm)
    return norm


def generate_tech_id(canonical_name: str) -> str:
    """Generate a deterministic canonical TechId <= 64 chars.

    Uses SHA-256 digest to ensure collision-resistance and deterministic stability.
    Format: 'tech_' + 48 hex characters = 53 characters.
    """
    normalized = normalize_name(canonical_name).lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]
    return f"tech_{digest}"


def generate_entity_id(category: str, name: str) -> str:
    """Generate a deterministic canonical EntityId <= 64 chars.

    Matches enterprise knowledge graph convention: 'ent_' + SHA-256(CATEGORY:name)[:48].
    Format: 'ent_' + 48 hex characters = 52 characters.
    """
    norm_cat = normalize_category(category).upper()
    norm_name = normalize_name(name).lower()
    raw = f"{norm_cat}:{norm_name}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]
    return f"ent_{digest}"


# ============================================================================
# MODELS & CATALOG DEFINITIONS
# ============================================================================


class TechnologyCatalogEntry(BaseModel):
    """Canonical entry in the enterprise technology catalog."""

    tech_id: str = Field(..., max_length=MAX_TECH_ID_LENGTH, description="Immutable canonical TechId")
    canonical_name: str = Field(..., max_length=MAX_TECH_NAME_LENGTH, description="Official display name")
    category: str = Field(..., max_length=MAX_TECH_CATEGORY_LENGTH, description="Technology taxonomy category")
    aliases: list[str] = Field(default_factory=list, description="Approved alias strings resolving to this technology")
    canonical_version: str | None = Field(default=None, max_length=MAX_CANONICAL_VERSION_LENGTH)
    description: str | None = Field(default=None, description="Optional technology description")

    def to_node(self) -> TechnologyNode:
        """Convert to Spanner TechnologyNode model."""
        return TechnologyNode(
            tech_id=self.tech_id,
            name=self.canonical_name,
            category=self.category,
            canonical_version=self.canonical_version,
        )


class EntityEntry(BaseModel):
    """Discovered business entity or concept."""

    entity_id: str = Field(..., max_length=MAX_ENTITY_ID_LENGTH)
    name: str = Field(..., max_length=MAX_ENTITY_NAME_LENGTH)
    category: str = Field(..., max_length=MAX_ENTITY_CATEGORY_LENGTH)
    description: str | None = None

    def to_node(self) -> EntityNode:
        """Convert to Spanner EntityNode model."""
        return EntityNode(
            entity_id=self.entity_id,
            name=self.name,
            category=self.category,
            description=self.description,
        )


class TaxonomyMappingResult(BaseModel):
    """Outcome of mapping a batch of technology names or aliases."""

    matched_technologies: list[TechnologyCatalogEntry] = Field(
        default_factory=list, description="Deduplicated and deterministically sorted matched entries"
    )
    unmatched_names: list[str] = Field(
        default_factory=list, description="Deduplicated and deterministically sorted unrecognized names"
    )


# ============================================================================
# SPANNER VALIDATION FUNCTIONS
# ============================================================================


def validate_technology_entry(entry: TechnologyCatalogEntry) -> None:
    """Validate that a TechnologyCatalogEntry adheres strictly to Spanner column constraints."""
    if not entry.tech_id or not entry.tech_id.strip():
        raise SpannerConstraintViolationError("tech_id cannot be empty or whitespace.")
    if len(entry.tech_id) > MAX_TECH_ID_LENGTH:
        raise SpannerConstraintViolationError(
            f"tech_id '{entry.tech_id}' exceeds max length {MAX_TECH_ID_LENGTH} (length={len(entry.tech_id)})."
        )

    if not entry.canonical_name or not entry.canonical_name.strip():
        raise SpannerConstraintViolationError("canonical_name cannot be empty or whitespace.")
    if len(entry.canonical_name) > MAX_TECH_NAME_LENGTH:
        raise SpannerConstraintViolationError(
            f"canonical_name exceeds max length {MAX_TECH_NAME_LENGTH} (length={len(entry.canonical_name)})."
        )

    if not entry.category or not entry.category.strip():
        raise SpannerConstraintViolationError("category cannot be empty or whitespace.")
    if len(entry.category) > MAX_TECH_CATEGORY_LENGTH:
        raise SpannerConstraintViolationError(
            f"category exceeds max length {MAX_TECH_CATEGORY_LENGTH} (length={len(entry.category)})."
        )

    if entry.canonical_version is not None:
        if len(entry.canonical_version) > MAX_CANONICAL_VERSION_LENGTH:
            raise SpannerConstraintViolationError(
                f"canonical_version exceeds max length {MAX_CANONICAL_VERSION_LENGTH} (length={len(entry.canonical_version)})."
            )


def validate_entity_node(node: EntityNode) -> None:
    """Validate that an EntityNode adheres strictly to Spanner column constraints."""
    if not node.entity_id or not node.entity_id.strip():
        raise SpannerConstraintViolationError("entity_id cannot be empty or whitespace.")
    if len(node.entity_id) > MAX_ENTITY_ID_LENGTH:
        raise SpannerConstraintViolationError(
            f"entity_id exceeds max length {MAX_ENTITY_ID_LENGTH} (length={len(node.entity_id)})."
        )

    if not node.name or not node.name.strip():
        raise SpannerConstraintViolationError("name cannot be empty or whitespace.")
    if len(node.name) > MAX_ENTITY_NAME_LENGTH:
        raise SpannerConstraintViolationError(
            f"name exceeds max length {MAX_ENTITY_NAME_LENGTH} (length={len(node.name)})."
        )

    if not node.category or not node.category.strip():
        raise SpannerConstraintViolationError("category cannot be empty or whitespace.")
    if len(node.category) > MAX_ENTITY_CATEGORY_LENGTH:
        raise SpannerConstraintViolationError(
            f"category exceeds max length {MAX_ENTITY_CATEGORY_LENGTH} (length={len(node.category)})."
        )


# ============================================================================
# TECHNOLOGY CATALOG
# ============================================================================


class TechnologyCatalog:
    """In-memory deterministic catalog of canonical technologies and aliases."""

    def __init__(self) -> None:
        self._entries_by_id: dict[str, TechnologyCatalogEntry] = {}
        self._lookup_index: dict[str, str] = {}  # normalized_lookup_key -> tech_id

    def register(self, entry: TechnologyCatalogEntry) -> None:
        """Register a new canonical technology in the catalog.

        Validates Spanner constraints, verifies no collision with existing TechIds
        or aliases, and maps all approved aliases to the canonical TechId.
        """
        validate_technology_entry(entry)

        # Check TechId uniqueness
        if entry.tech_id in self._entries_by_id:
            existing = self._entries_by_id[entry.tech_id]
            if existing.canonical_name != entry.canonical_name:
                raise CatalogCollisionError(
                    f"TechId '{entry.tech_id}' collides with existing entry '{existing.canonical_name}'."
                )

        # Build keys to index: canonical name + all aliases
        lookup_keys = [normalize_lookup_key(entry.canonical_name)]
        for alias in entry.aliases:
            lookup_keys.append(normalize_lookup_key(alias))

        # Check for alias collisions against other TechIds
        for key in lookup_keys:
            if key in self._lookup_index and self._lookup_index[key] != entry.tech_id:
                colliding_id = self._lookup_index[key]
                colliding_entry = self._entries_by_id[colliding_id]
                raise CatalogCollisionError(
                    f"Alias/name '{key}' for '{entry.canonical_name}' collides with existing entry "
                    f"'{colliding_entry.canonical_name}' (TechId: '{colliding_id}')."
                )

        # Store entry and update index
        self._entries_by_id[entry.tech_id] = entry
        for key in lookup_keys:
            self._lookup_index[key] = entry.tech_id

    def get_by_id(self, tech_id: str) -> TechnologyCatalogEntry | None:
        """Retrieve entry by its exact canonical TechId."""
        return self._entries_by_id.get(tech_id)

    def lookup(self, name_or_alias: str) -> TechnologyCatalogEntry | None:
        """Lookup technology by name or alias."""
        try:
            key = normalize_lookup_key(name_or_alias)
        except (InvalidNormalizationError, SpannerConstraintViolationError):
            return None

        tech_id = self._lookup_index.get(key)
        if tech_id:
            return self._entries_by_id.get(tech_id)
        return None

    def all_entries(self) -> list[TechnologyCatalogEntry]:
        """Return all catalog entries sorted deterministically by TechId."""
        return sorted(self._entries_by_id.values(), key=lambda e: e.tech_id)

    def __len__(self) -> int:
        return len(self._entries_by_id)


# ============================================================================
# DEFAULT BUILT-IN ENTERPRISE CATALOG
# ============================================================================


def build_default_catalog() -> TechnologyCatalog:
    """Build the curated enterprise catalog covering Google Cloud, AI, databases, and devops."""
    catalog = TechnologyCatalog()

    entries = [
        # --- Cloud Platforms ---
        TechnologyCatalogEntry(
            tech_id="tech_gcp",
            canonical_name="Google Cloud Platform",
            category="Cloud Platform",
            aliases=["gcp", "google cloud", "google cloud platform"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_aws",
            canonical_name="Amazon Web Services",
            category="Cloud Platform",
            aliases=["aws", "amazon web services"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_azure",
            canonical_name="Microsoft Azure",
            category="Cloud Platform",
            aliases=["azure", "ms azure", "microsoft azure"],
        ),
        # --- Databases ---
        TechnologyCatalogEntry(
            tech_id="tech_cloud_spanner",
            canonical_name="Cloud Spanner",
            category="Database",
            aliases=["spanner", "google cloud spanner", "gcp spanner"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_sql",
            canonical_name="Cloud SQL",
            category="Database",
            aliases=["cloud sql", "gcp cloud sql", "google cloud sql"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_bigtable",
            canonical_name="Cloud Bigtable",
            category="Database",
            aliases=["bigtable", "cloud bigtable", "google cloud bigtable"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_firestore",
            canonical_name="Cloud Firestore",
            category="Database",
            aliases=["firestore", "cloud firestore", "google firestore"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_alloydb",
            canonical_name="AlloyDB",
            category="Database",
            aliases=["alloydb", "alloydb for postgresql", "google alloydb"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_postgresql",
            canonical_name="PostgreSQL",
            category="Database",
            aliases=["postgres", "postgresql", "pgsql"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_mysql",
            canonical_name="MySQL",
            category="Database",
            aliases=["mysql"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_redis",
            canonical_name="Redis",
            category="Database",
            aliases=["redis", "redis cache"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_mongodb",
            canonical_name="MongoDB",
            category="Database",
            aliases=["mongo", "mongodb"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cassandra",
            canonical_name="Apache Cassandra",
            category="Database",
            aliases=["cassandra", "apache cassandra"],
        ),
        # --- Data & Analytics ---
        TechnologyCatalogEntry(
            tech_id="tech_bigquery",
            canonical_name="BigQuery",
            category="Data & Analytics",
            aliases=["bq", "google bigquery", "google cloud bigquery"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_dataproc",
            canonical_name="Dataproc",
            category="Data & Analytics",
            aliases=["dataproc", "google dataproc", "cloud dataproc"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_dataflow",
            canonical_name="Dataflow",
            category="Data & Analytics",
            aliases=["dataflow", "google dataflow", "cloud dataflow"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_apache_spark",
            canonical_name="Apache Spark",
            category="Data & Analytics",
            aliases=["spark", "apache spark", "pyspark"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_apache_kafka",
            canonical_name="Apache Kafka",
            category="Data & Analytics",
            aliases=["kafka", "apache kafka"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_apache_beam",
            canonical_name="Apache Beam",
            category="Data & Analytics",
            aliases=["beam", "apache beam"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_apache_flink",
            canonical_name="Apache Flink",
            category="Data & Analytics",
            aliases=["flink", "apache flink"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_looker",
            canonical_name="Looker",
            category="Data & Analytics",
            aliases=["looker", "looker bi"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_dbt",
            canonical_name="dbt",
            category="Data & Analytics",
            aliases=["dbt", "data build tool"],
        ),
        # --- AI & Machine Learning ---
        TechnologyCatalogEntry(
            tech_id="tech_vertex_ai",
            canonical_name="Vertex AI",
            category="AI & Machine Learning",
            aliases=["vertex ai", "vertex", "google vertex ai"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_gemini",
            canonical_name="Gemini",
            category="AI & Machine Learning",
            aliases=["gemini", "google gemini", "gemini-1.5", "gemini-2.0", "gemini 1.5", "gemini 2.0"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_document_ai",
            canonical_name="Document AI",
            category="AI & Machine Learning",
            aliases=["document ai", "docai", "google document ai"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_tensorflow",
            canonical_name="TensorFlow",
            category="AI & Machine Learning",
            aliases=["tensorflow"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_pytorch",
            canonical_name="PyTorch",
            category="AI & Machine Learning",
            aliases=["pytorch", "torch"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_langchain",
            canonical_name="LangChain",
            category="AI & Machine Learning",
            aliases=["langchain"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_llamaindex",
            canonical_name="LlamaIndex",
            category="AI & Machine Learning",
            aliases=["llamaindex", "llama-index"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_scikit_learn",
            canonical_name="Scikit-learn",
            category="AI & Machine Learning",
            aliases=["scikit-learn", "sklearn", "scikitlearn"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_huggingface",
            canonical_name="Hugging Face",
            category="AI & Machine Learning",
            aliases=["hugging face", "huggingface", "hf"],
        ),
        # --- Compute & Containers ---
        TechnologyCatalogEntry(
            tech_id="tech_gke",
            canonical_name="Google Kubernetes Engine",
            category="Compute & Containers",
            aliases=["gke", "google kubernetes engine", "kubernetes engine"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_run",
            canonical_name="Cloud Run",
            category="Compute & Containers",
            aliases=["cloud run", "gcp cloud run", "google cloud run"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_functions",
            canonical_name="Cloud Functions",
            category="Compute & Containers",
            aliases=["cloud functions", "gcf", "google cloud functions"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_compute_engine",
            canonical_name="Compute Engine",
            category="Compute & Containers",
            aliases=["compute engine", "gce", "google compute engine"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_kubernetes",
            canonical_name="Kubernetes",
            category="Compute & Containers",
            aliases=["k8s", "kubernetes"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_docker",
            canonical_name="Docker",
            category="Compute & Containers",
            aliases=["docker", "docker container"],
        ),
        # --- Storage ---
        TechnologyCatalogEntry(
            tech_id="tech_cloud_storage",
            canonical_name="Google Cloud Storage",
            category="Storage",
            aliases=["gcs", "cloud storage", "google cloud storage"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_persistent_disk",
            canonical_name="Persistent Disk",
            category="Storage",
            aliases=["persistent disk", "gcp persistent disk"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_filestore",
            canonical_name="Cloud Filestore",
            category="Storage",
            aliases=["filestore", "cloud filestore"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_aws_s3",
            canonical_name="Amazon S3",
            category="Storage",
            aliases=["s3", "aws s3", "amazon s3"],
        ),
        # --- Integration & Messaging ---
        TechnologyCatalogEntry(
            tech_id="tech_cloud_pubsub",
            canonical_name="Cloud Pub/Sub",
            category="Integration & Messaging",
            aliases=["pub/sub", "pubsub", "cloud pubsub", "google cloud pub/sub", "pub-sub"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_tasks",
            canonical_name="Cloud Tasks",
            category="Integration & Messaging",
            aliases=["cloud tasks", "google cloud tasks"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_eventarc",
            canonical_name="Eventarc",
            category="Integration & Messaging",
            aliases=["eventarc", "google eventarc"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_rabbitmq",
            canonical_name="RabbitMQ",
            category="Integration & Messaging",
            aliases=["rabbitmq"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_grpc",
            canonical_name="gRPC",
            category="Integration & Messaging",
            aliases=["grpc"],
        ),
        # --- Networking & Security ---
        TechnologyCatalogEntry(
            tech_id="tech_cloud_armor",
            canonical_name="Cloud Armor",
            category="Networking & Security",
            aliases=["cloud armor", "armor"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_cdn",
            canonical_name="Cloud CDN",
            category="Networking & Security",
            aliases=["cloud cdn", "cdn"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_load_balancing",
            canonical_name="Cloud Load Balancing",
            category="Networking & Security",
            aliases=["clb", "cloud load balancer", "cloud load balancing", "gcp load balancing"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_iam",
            canonical_name="Cloud IAM",
            category="Networking & Security",
            aliases=["iam", "identity and access management", "cloud iam", "cloud identity"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_kms",
            canonical_name="Cloud KMS",
            category="Networking & Security",
            aliases=["kms", "key management service", "cloud key management"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_secret_manager",
            canonical_name="Secret Manager",
            category="Networking & Security",
            aliases=["secret manager", "cloud secret manager", "gsm"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_hashicorp_vault",
            canonical_name="HashiCorp Vault",
            category="Networking & Security",
            aliases=["vault", "hashicorp vault"],
        ),
        # --- DevOps & Observability ---
        TechnologyCatalogEntry(
            tech_id="tech_cloud_build",
            canonical_name="Cloud Build",
            category="DevOps & Observability",
            aliases=["cloud build", "gcb"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_monitoring",
            canonical_name="Cloud Monitoring",
            category="DevOps & Observability",
            aliases=["cloud monitoring", "stackdriver monitoring", "stackdriver"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_logging",
            canonical_name="Cloud Logging",
            category="DevOps & Observability",
            aliases=["cloud logging", "stackdriver logging"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cloud_trace",
            canonical_name="Cloud Trace",
            category="DevOps & Observability",
            aliases=["cloud trace", "stackdriver trace"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_terraform",
            canonical_name="Terraform",
            category="DevOps & Observability",
            aliases=["terraform", "tf", "hashicorp terraform"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_github",
            canonical_name="GitHub",
            category="DevOps & Observability",
            aliases=["github"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_github_actions",
            canonical_name="GitHub Actions",
            category="DevOps & Observability",
            aliases=["github actions", "gha", "gh actions"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_gitlab",
            canonical_name="GitLab",
            category="DevOps & Observability",
            aliases=["gitlab", "gitlab ci"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_prometheus",
            canonical_name="Prometheus",
            category="DevOps & Observability",
            aliases=["prometheus"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_grafana",
            canonical_name="Grafana",
            category="DevOps & Observability",
            aliases=["grafana"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_argocd",
            canonical_name="ArgoCD",
            category="DevOps & Observability",
            aliases=["argocd", "argo cd"],
        ),
        # --- Languages ---
        TechnologyCatalogEntry(
            tech_id="tech_python",
            canonical_name="Python",
            category="Programming Language",
            aliases=["python", "python3", "py"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_go",
            canonical_name="Go",
            category="Programming Language",
            aliases=["golang", "go"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_java",
            canonical_name="Java",
            category="Programming Language",
            aliases=["java", "java8", "java11", "java17", "java21"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_typescript",
            canonical_name="TypeScript",
            category="Programming Language",
            aliases=["typescript", "ts"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_javascript",
            canonical_name="JavaScript",
            category="Programming Language",
            aliases=["javascript", "js", "es6"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_rust",
            canonical_name="Rust",
            category="Programming Language",
            aliases=["rust"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_csharp",
            canonical_name="C#",
            category="Programming Language",
            aliases=["c#", "csharp", "c-sharp", ".net c#"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_cpp",
            canonical_name="C++",
            category="Programming Language",
            aliases=["c++", "cpp"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_sql",
            canonical_name="SQL",
            category="Programming Language",
            aliases=["sql"],
        ),
        # --- Frameworks ---
        TechnologyCatalogEntry(
            tech_id="tech_react",
            canonical_name="React",
            category="Web & Mobile Framework",
            aliases=["react", "reactjs", "react.js"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_angular",
            canonical_name="Angular",
            category="Web & Mobile Framework",
            aliases=["angular", "angularjs"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_vue",
            canonical_name="Vue",
            category="Web & Mobile Framework",
            aliases=["vue", "vuejs", "vue.js"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_nextjs",
            canonical_name="Next.js",
            category="Web & Mobile Framework",
            aliases=["next.js", "nextjs"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_nodejs",
            canonical_name="Node.js",
            category="Web & Mobile Framework",
            aliases=["node", "node.js", "nodejs"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_fastapi",
            canonical_name="FastAPI",
            category="Web & Mobile Framework",
            aliases=["fastapi", "fast-api"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_flask",
            canonical_name="Flask",
            category="Web & Mobile Framework",
            aliases=["flask"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_django",
            canonical_name="Django",
            category="Web & Mobile Framework",
            aliases=["django"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_springboot",
            canonical_name="Spring Boot",
            category="Web & Mobile Framework",
            aliases=["spring boot", "springboot", "spring"],
        ),
        TechnologyCatalogEntry(
            tech_id="tech_flutter",
            canonical_name="Flutter",
            category="Web & Mobile Framework",
            aliases=["flutter"],
        ),
    ]

    for entry in entries:
        catalog.register(entry)

    return catalog


# ============================================================================
# TAXONOMY MAPPER ENGINE
# ============================================================================


class TaxonomyMapper:
    """Deterministic taxonomy resolution, entity generation, and Spanner constraint validation."""

    def __init__(self, catalog: TechnologyCatalog | None = None) -> None:
        self.catalog = catalog or build_default_catalog()

    def register_technology(self, entry: TechnologyCatalogEntry) -> None:
        """Extend the catalog with an additional technology entry.

        Preserves existing canonical IDs and validates against collisions.
        """
        self.catalog.register(entry)

    def resolve_technology(self, name_or_alias: str, strict: bool = True) -> TechnologyCatalogEntry | None:
        """Resolve a single technology name or alias to its canonical TechnologyCatalogEntry.

        If strict=True and name is not found, raises UnknownTechnologyError.
        If strict=False and name is not found, returns None.
        Never silently invents a canonical entry.
        """
        entry = self.catalog.lookup(name_or_alias)
        if entry is None and strict:
            raise UnknownTechnologyError(
                f"Technology '{name_or_alias}' is not recognized in canonical catalog. "
                "Canonical mappings are never silently invented.",
                unknown_names=[name_or_alias],
            )
        return entry

    def resolve_tech_id(self, name_or_alias: str, strict: bool = True) -> str | None:
        """Resolve name or alias to canonical TechId string (<= 64 chars)."""
        entry = self.resolve_technology(name_or_alias, strict=strict)
        return entry.tech_id if entry else None

    def map_technologies(
        self,
        names_or_aliases: Iterable[str],
        allow_unknown: bool = False,
    ) -> list[TechnologyCatalogEntry]:
        """Resolve, deduplicate, and sort a batch of technology names/aliases.

        If allow_unknown=False (default) and any name is unrecognized, raises UnknownTechnologyError.
        If allow_unknown=True, silently excludes unrecognized names.
        Returns deduplicated entries deterministically sorted by TechId.
        """
        result = self.map_technologies_with_unmatched(names_or_aliases)
        if result.unmatched_names and not allow_unknown:
            raise UnknownTechnologyError(
                f"Unknown technologies detected: {result.unmatched_names}. "
                "Canonical mappings are never silently invented.",
                unknown_names=result.unmatched_names,
            )
        return result.matched_technologies

    def map_technologies_with_unmatched(
        self,
        names_or_aliases: Iterable[str],
    ) -> TaxonomyMappingResult:
        """Resolve a collection of technology names/aliases, capturing both matched and unmatched.

        Deduplicates matched entries by TechId and sorts deterministically by TechId.
        Deduplicates unmatched names and sorts them alphabetically.
        """
        matched_dict: dict[str, TechnologyCatalogEntry] = {}
        unmatched_set: set[str] = set()

        for raw_name in names_or_aliases:
            try:
                norm = normalize_name(raw_name)
            except (InvalidNormalizationError, SpannerConstraintViolationError):
                unmatched_set.add(str(raw_name).strip())
                continue

            entry = self.catalog.lookup(norm)
            if entry is not None:
                matched_dict[entry.tech_id] = entry
            else:
                unmatched_set.add(norm)

        # Deterministic sort
        sorted_matched = sorted(matched_dict.values(), key=lambda e: e.tech_id)
        sorted_unmatched = sorted(unmatched_set)

        # Final Spanner constraint sanity check
        for entry in sorted_matched:
            validate_technology_entry(entry)

        return TaxonomyMappingResult(
            matched_technologies=sorted_matched,
            unmatched_names=sorted_unmatched,
        )

    def create_entity_node(
        self,
        category: str,
        name: str,
        description: str | None = None,
    ) -> EntityNode:
        """Create a validated Spanner EntityNode with deterministic EntityId.

        EntityId is generated deterministically from (CATEGORY:name) via SHA-256 (52 chars).
        All fields are strictly validated against Spanner schema constraints.
        """
        norm_cat = normalize_category(category)
        norm_name = normalize_name(name)
        entity_id = generate_entity_id(norm_cat, norm_name)

        desc = description.strip() if (description and isinstance(description, str) and description.strip()) else None

        node = EntityNode(
            entity_id=entity_id,
            name=norm_name,
            category=norm_cat,
            description=desc,
        )
        validate_entity_node(node)
        return node

    def map_entities(
        self,
        entities: Iterable[tuple[str, str] | dict[str, Any] | EntityNode],
    ) -> list[EntityNode]:
        """Deduplicate and deterministically sort a collection of business/technical entities.

        Accepts:
        - tuple: (category, name) or (category, name, description)
        - dict: {"category": ..., "name": ..., "description": ...}
        - EntityNode instances

        Deduplicates by entity_id and sorts deterministically by entity_id.
        """
        nodes_by_id: dict[str, EntityNode] = {}

        for item in entities:
            if isinstance(item, EntityNode):
                validate_entity_node(item)
                node = item
            elif isinstance(item, tuple):
                if len(item) == 2:
                    cat, name = item
                    desc = None
                elif len(item) >= 3:
                    cat, name, desc = item[0], item[1], item[2]
                else:
                    raise InvalidNormalizationError(f"Entity tuple must have at least 2 items, got {item}.")
                node = self.create_entity_node(cat, name, desc)
            elif isinstance(item, dict):
                cat = item.get("category")
                name = item.get("name")
                desc = item.get("description")
                if not cat or not name:
                    raise InvalidNormalizationError(f"Entity dict must have 'category' and 'name', got {item}.")
                node = self.create_entity_node(cat, name, desc)
            else:
                raise InvalidNormalizationError(f"Unsupported entity specification type: {type(item).__name__}.")

            # If duplicate entity_id seen, preserve the one with description if current doesn't have one
            if node.entity_id in nodes_by_id:
                existing = nodes_by_id[node.entity_id]
                if not existing.description and node.description:
                    nodes_by_id[node.entity_id] = node
            else:
                nodes_by_id[node.entity_id] = node

        # Deterministic sort by entity_id
        return sorted(nodes_by_id.values(), key=lambda n: n.entity_id)
