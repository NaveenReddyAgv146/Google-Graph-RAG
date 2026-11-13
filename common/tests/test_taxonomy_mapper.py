"""Offline unit tests for Deterministic Taxonomy & Technology Catalog.

Tests aliases, normalization, unknown values, duplicate inputs, ID stability,
collisions, Spanner constraints, and catalog extension.
"""

import pytest

from common.models.schema import EntityNode, TechnologyNode
from common.taxonomy import (
    MAX_CANONICAL_VERSION_LENGTH,
    MAX_ENTITY_CATEGORY_LENGTH,
    MAX_ENTITY_ID_LENGTH,
    MAX_ENTITY_NAME_LENGTH,
    MAX_TECH_CATEGORY_LENGTH,
    MAX_TECH_ID_LENGTH,
    MAX_TECH_NAME_LENGTH,
    CatalogCollisionError,
    InvalidNormalizationError,
    SpannerConstraintViolationError,
    TaxonomyMapper,
    TechnologyCatalog,
    TechnologyCatalogEntry,
    UnknownTechnologyError,
    build_default_catalog,
    generate_entity_id,
    generate_tech_id,
    normalize_category,
    normalize_lookup_key,
    normalize_name,
    validate_entity_node,
    validate_technology_entry,
)


# ============================================================================
# 1. DEFAULT CATALOG & INITIALIZATION
# ============================================================================


def test_default_catalog_initialization():
    """Verify default catalog contains curated entries and validates all constraints."""
    catalog = build_default_catalog()
    assert len(catalog) >= 50

    entries = catalog.all_entries()
    assert len(entries) == len(catalog)

    # Check deterministic sorting by tech_id
    tech_ids = [e.tech_id for e in entries]
    assert tech_ids == sorted(tech_ids)

    # Validate every entry against Spanner column limits
    for entry in entries:
        validate_technology_entry(entry)
        assert len(entry.tech_id) <= MAX_TECH_ID_LENGTH
        assert len(entry.canonical_name) <= MAX_TECH_NAME_LENGTH
        assert len(entry.category) <= MAX_TECH_CATEGORY_LENGTH
        if entry.canonical_version:
            assert len(entry.canonical_version) <= MAX_CANONICAL_VERSION_LENGTH

        # Check conversion to TechnologyNode
        node = entry.to_node()
        assert isinstance(node, TechnologyNode)
        assert node.tech_id == entry.tech_id
        assert node.name == entry.canonical_name


# ============================================================================
# 2. ALIAS RESOLUTION
# ============================================================================


@pytest.mark.parametrize(
    "raw_input,expected_tech_id,expected_canonical_name",
    [
        ("Cloud Spanner", "tech_cloud_spanner", "Cloud Spanner"),
        ("spanner", "tech_cloud_spanner", "Cloud Spanner"),
        ("google cloud spanner", "tech_cloud_spanner", "Cloud Spanner"),
        ("gcp spanner", "tech_cloud_spanner", "Cloud Spanner"),
        ("SPANNER", "tech_cloud_spanner", "Cloud Spanner"),
        ("BigQuery", "tech_bigquery", "BigQuery"),
        ("bq", "tech_bigquery", "BigQuery"),
        ("google bigquery", "tech_bigquery", "BigQuery"),
        ("BQ", "tech_bigquery", "BigQuery"),
        ("gcs", "tech_cloud_storage", "Google Cloud Storage"),
        ("cloud storage", "tech_cloud_storage", "Google Cloud Storage"),
        ("google cloud storage", "tech_cloud_storage", "Google Cloud Storage"),
        ("k8s", "tech_kubernetes", "Kubernetes"),
        ("kubernetes", "tech_kubernetes", "Kubernetes"),
        ("gke", "tech_gke", "Google Kubernetes Engine"),
        ("pub/sub", "tech_cloud_pubsub", "Cloud Pub/Sub"),
        ("pubsub", "tech_cloud_pubsub", "Cloud Pub/Sub"),
        ("pub-sub", "tech_cloud_pubsub", "Cloud Pub/Sub"),
        ("vertex ai", "tech_vertex_ai", "Vertex AI"),
        ("vertex", "tech_vertex_ai", "Vertex AI"),
        ("docai", "tech_document_ai", "Document AI"),
        ("document ai", "tech_document_ai", "Document AI"),
        ("postgres", "tech_postgresql", "PostgreSQL"),
        ("postgresql", "tech_postgresql", "PostgreSQL"),
        ("tf", "tech_terraform", "Terraform"),
        ("terraform", "tech_terraform", "Terraform"),
        ("c++", "tech_cpp", "C++"),
        ("c#", "tech_csharp", "C#"),
        ("node.js", "tech_nodejs", "Node.js"),
        ("next.js", "tech_nextjs", "Next.js"),
        ("fastapi", "tech_fastapi", "FastAPI"),
        ("spring boot", "tech_springboot", "Spring Boot"),
    ],
)
def test_alias_resolution(raw_input, expected_tech_id, expected_canonical_name):
    """Verify approved aliases resolve to stable canonical TechIds and names."""
    mapper = TaxonomyMapper()
    entry = mapper.resolve_technology(raw_input, strict=True)
    assert entry is not None
    assert entry.tech_id == expected_tech_id
    assert entry.canonical_name == expected_canonical_name

    tech_id = mapper.resolve_tech_id(raw_input, strict=True)
    assert tech_id == expected_tech_id


# ============================================================================
# 3. NORMALIZATION UTILITIES
# ============================================================================


def test_normalization_whitespace_and_punctuation():
    """Verify whitespace trimming, collapsing, and quote stripping."""
    assert normalize_name("  Cloud Spanner  ") == "Cloud Spanner"
    assert normalize_name("Cloud \t\n  Spanner") == "Cloud Spanner"
    assert normalize_name('"Cloud Spanner"') == "Cloud Spanner"
    assert normalize_name("'BigQuery'") == "BigQuery"
    assert normalize_name("“Vertex AI”") == "Vertex AI"
    assert normalize_name("‘Document AI’") == "Document AI"
    assert normalize_name("(GCP)") == "GCP"
    assert normalize_name("Cloud Spanner,") == "Cloud Spanner"
    assert normalize_name("Cloud Spanner.") == "Cloud Spanner"
    assert normalize_name("Cloud Spanner;") == "Cloud Spanner"

    # Meaningful technical symbols preserved
    assert normalize_name("C++") == "C++"
    assert normalize_name("C#") == "C#"
    assert normalize_name("Node.js") == "Node.js"
    assert normalize_name("Next.js") == "Next.js"
    assert normalize_name("Cloud Pub/Sub") == "Cloud Pub/Sub"


def test_normalization_category():
    """Verify category normalization."""
    assert normalize_category("  Database  ") == "Database"
    assert normalize_category("AI \t & \n Machine Learning") == "AI & Machine Learning"


@pytest.mark.parametrize("invalid_input", ["", "   ", "\t\r\n"])
def test_normalization_empty_raises_error(invalid_input):
    """Verify empty or whitespace-only strings raise InvalidNormalizationError."""
    with pytest.raises(InvalidNormalizationError):
        normalize_name(invalid_input)
    with pytest.raises(InvalidNormalizationError):
        normalize_category(invalid_input)
    with pytest.raises(InvalidNormalizationError):
        normalize_lookup_key(invalid_input)


@pytest.mark.parametrize("invalid_type", [123, None, ["a"], {"k": "v"}])
def test_normalization_invalid_types_raise_error(invalid_type):
    """Verify non-string types raise InvalidNormalizationError."""
    with pytest.raises(InvalidNormalizationError):
        normalize_name(invalid_type)
    with pytest.raises(InvalidNormalizationError):
        normalize_category(invalid_type)
    with pytest.raises(InvalidNormalizationError):
        normalize_lookup_key(invalid_type)


# ============================================================================
# 4. UNKNOWN VALUES HANDLING
# ============================================================================


def test_unknown_technology_strict_mode_raises():
    """Verify strict resolution of unknown technologies raises UnknownTechnologyError."""
    mapper = TaxonomyMapper()
    with pytest.raises(UnknownTechnologyError) as exc_info:
        mapper.resolve_technology("CompletelyUnknownTech123", strict=True)
    assert "CompletelyUnknownTech123" in str(exc_info.value)
    assert exc_info.value.unknown_names == ["CompletelyUnknownTech123"]


def test_unknown_technology_lenient_mode_returns_none():
    """Verify non-strict resolution of unknown technologies returns None."""
    mapper = TaxonomyMapper()
    entry = mapper.resolve_technology("CompletelyUnknownTech123", strict=False)
    assert entry is None

    tech_id = mapper.resolve_tech_id("CompletelyUnknownTech123", strict=False)
    assert tech_id is None


def test_map_technologies_strict_rejects_unknown():
    """Verify map_technologies in strict mode fails when any entry is unknown."""
    mapper = TaxonomyMapper()
    with pytest.raises(UnknownTechnologyError) as exc_info:
        mapper.map_technologies(["Spanner", "FakeTechA", "BigQuery", "FakeTechB"], allow_unknown=False)
    assert "FakeTechA" in exc_info.value.unknown_names
    assert "FakeTechB" in exc_info.value.unknown_names


def test_map_technologies_lenient_filters_unknown():
    """Verify map_technologies with allow_unknown=True safely filters out unrecognized items."""
    mapper = TaxonomyMapper()
    mapped = mapper.map_technologies(["Spanner", "FakeTechA", "BigQuery", "FakeTechB"], allow_unknown=True)
    assert len(mapped) == 2
    tech_ids = [m.tech_id for m in mapped]
    assert tech_ids == ["tech_bigquery", "tech_cloud_spanner"]


def test_map_technologies_with_unmatched():
    """Verify map_technologies_with_unmatched captures both matched and unmatched."""
    mapper = TaxonomyMapper()
    res = mapper.map_technologies_with_unmatched(["Spanner", "FakeTechB", "BigQuery", "FakeTechA", "Spanner"])
    assert len(res.matched_technologies) == 2
    assert [m.tech_id for m in res.matched_technologies] == ["tech_bigquery", "tech_cloud_spanner"]
    assert res.unmatched_names == ["FakeTechA", "FakeTechB"]


# ============================================================================
# 5. DEDUPLICATION & DETERMINISTIC SORTING
# ============================================================================


def test_deduplication_of_identical_and_alias_inputs():
    """Verify different aliases of same tech deduplicate to single entry."""
    mapper = TaxonomyMapper()
    inputs = [
        "Cloud Spanner",
        "spanner",
        "google cloud spanner",
        "gcp spanner",
        "SPANNER",
        "Cloud Spanner",
    ]
    results = mapper.map_technologies(inputs)
    assert len(results) == 1
    assert results[0].tech_id == "tech_cloud_spanner"
    assert results[0].canonical_name == "Cloud Spanner"


def test_deterministic_sorting_invariance():
    """Verify output order is strictly deterministic regardless of input permutation."""
    mapper = TaxonomyMapper()
    inputs_order1 = ["Vertex AI", "BigQuery", "Cloud Spanner", "Python", "Kubernetes"]
    inputs_order2 = ["Kubernetes", "Cloud Spanner", "Python", "Vertex AI", "BigQuery"]
    inputs_order3 = ["Python", "BigQuery", "Kubernetes", "Vertex AI", "Cloud Spanner"]

    res1 = [e.tech_id for e in mapper.map_technologies(inputs_order1)]
    res2 = [e.tech_id for e in mapper.map_technologies(inputs_order2)]
    res3 = [e.tech_id for e in mapper.map_technologies(inputs_order3)]

    expected_sorted = [
        "tech_bigquery",
        "tech_cloud_spanner",
        "tech_kubernetes",
        "tech_python",
        "tech_vertex_ai",
    ]
    assert res1 == expected_sorted
    assert res2 == expected_sorted
    assert res3 == expected_sorted


# ============================================================================
# 6. ID GENERATION & STABILITY
# ============================================================================


def test_entity_id_generation_stability_and_length():
    """Verify generate_entity_id is stable, deterministic, and <= 64 chars."""
    id1 = generate_entity_id("CONCEPT", "Microservices Architecture")
    id2 = generate_entity_id("concept", "  microservices architecture  ")
    id3 = generate_entity_id("CONCEPT", "Microservices Architecture")

    assert id1 == id2 == id3
    assert id1.startswith("ent_")
    assert len(id1) == 52  # "ent_" (4) + 48 hex = 52 <= 64
    assert len(id1) <= MAX_ENTITY_ID_LENGTH


def test_entity_id_collision_resistance():
    """Verify distinct entity inputs produce distinct EntityIds."""
    id_concept = generate_entity_id("CONCEPT", "Microservices Architecture")
    id_pattern = generate_entity_id("PATTERN", "Microservices Architecture")
    id_other = generate_entity_id("CONCEPT", "Event Sourcing")

    assert id_concept != id_pattern
    assert id_concept != id_other
    assert id_pattern != id_other


def test_tech_id_generation_stability_and_length():
    """Verify generate_tech_id is stable, deterministic, and <= 64 chars."""
    id1 = generate_tech_id("Custom AI Engine")
    id2 = generate_tech_id("  custom ai engine  ")
    assert id1 == id2
    assert id1.startswith("tech_")
    assert len(id1) == 53  # "tech_" (5) + 48 hex = 53 <= 64
    assert len(id1) <= MAX_TECH_ID_LENGTH


# ============================================================================
# 7. CATALOG COLLISIONS
# ============================================================================


def test_catalog_collision_on_tech_id():
    """Verify registering a conflicting TechId with different name raises CatalogCollisionError."""
    catalog = TechnologyCatalog()
    catalog.register(
        TechnologyCatalogEntry(
            tech_id="tech_custom",
            canonical_name="Custom Tool 1",
            category="Tool",
            aliases=["c1"],
        )
    )

    with pytest.raises(CatalogCollisionError) as exc_info:
        catalog.register(
            TechnologyCatalogEntry(
                tech_id="tech_custom",
                canonical_name="Custom Tool 2",
                category="Tool",
                aliases=["c2"],
            )
        )
    assert "collides with existing entry" in str(exc_info.value)


def test_catalog_collision_on_alias():
    """Verify registering an alias that collides with an existing alias raises CatalogCollisionError."""
    catalog = TechnologyCatalog()
    catalog.register(
        TechnologyCatalogEntry(
            tech_id="tech_alpha",
            canonical_name="Alpha Tech",
            category="Framework",
            aliases=["shared_alias", "alpha"],
        )
    )

    with pytest.raises(CatalogCollisionError) as exc_info:
        catalog.register(
            TechnologyCatalogEntry(
                tech_id="tech_beta",
                canonical_name="Beta Tech",
                category="Framework",
                aliases=["shared_alias", "beta"],
            )
        )
    assert "collides with existing entry" in str(exc_info.value)


def test_catalog_idempotent_re_registration():
    """Verify re-registering the exact same entry is idempotent and does not error."""
    catalog = TechnologyCatalog()
    entry = TechnologyCatalogEntry(
        tech_id="tech_delta",
        canonical_name="Delta Tech",
        category="Framework",
        aliases=["delta"],
    )
    catalog.register(entry)
    catalog.register(entry)  # Idempotent re-registration
    assert len(catalog) == 1


# ============================================================================
# 8. SPANNER FIELD-LENGTH CONSTRAINTS
# ============================================================================


def test_tech_field_length_violations():
    """Verify violations of Spanner length constraints for Technologies table raise errors."""
    # TechId > 64 chars
    with pytest.raises(SpannerConstraintViolationError):
        validate_technology_entry(
            TechnologyCatalogEntry.model_construct(
                tech_id="t" * 65,
                canonical_name="Valid Name",
                category="Database",
            )
        )

    # Name > 256 chars
    with pytest.raises(SpannerConstraintViolationError):
        validate_technology_entry(
            TechnologyCatalogEntry.model_construct(
                tech_id="tech_valid",
                canonical_name="N" * 257,
                category="Database",
            )
        )

    # Category > 64 chars
    with pytest.raises(SpannerConstraintViolationError):
        validate_technology_entry(
            TechnologyCatalogEntry.model_construct(
                tech_id="tech_valid",
                canonical_name="Valid Name",
                category="C" * 65,
            )
        )

    # CanonicalVersion > 64 chars
    with pytest.raises(SpannerConstraintViolationError):
        validate_technology_entry(
            TechnologyCatalogEntry.model_construct(
                tech_id="tech_valid",
                canonical_name="Valid Name",
                category="Database",
                canonical_version="V" * 65,
            )
        )


def test_entity_field_length_violations():
    """Verify violations of Spanner length constraints for Entities table raise errors."""
    with pytest.raises(SpannerConstraintViolationError):
        validate_entity_node(
            EntityNode.model_construct(
                entity_id="e" * 65,
                name="Valid Name",
                category="Concept",
            )
        )

    with pytest.raises(SpannerConstraintViolationError):
        validate_entity_node(
            EntityNode.model_construct(
                entity_id="ent_valid",
                name="N" * 257,
                category="Concept",
            )
        )

    with pytest.raises(SpannerConstraintViolationError):
        validate_entity_node(
            EntityNode.model_construct(
                entity_id="ent_valid",
                name="Valid Name",
                category="C" * 65,
            )
        )


# ============================================================================
# 9. CATALOG EXTENSION (REQUIREMENT 8)
# ============================================================================


def test_catalog_extension_preserves_existing_ids():
    """Verify extending the catalog does not alter existing canonical entries or IDs."""
    mapper = TaxonomyMapper()
    baseline_count = len(mapper.catalog)

    # Verify existing entry before extension
    spanner_entry = mapper.resolve_technology("spanner")
    assert spanner_entry.tech_id == "tech_cloud_spanner"

    # Add a custom technology
    custom_entry = TechnologyCatalogEntry(
        tech_id="tech_custom_mesh",
        canonical_name="Custom Service Mesh",
        category="Networking & Security",
        aliases=["csm", "custom mesh"],
    )
    mapper.register_technology(custom_entry)

    # Verify new count
    assert len(mapper.catalog) == baseline_count + 1

    # Verify new entry resolves
    resolved_custom = mapper.resolve_technology("csm")
    assert resolved_custom is not None
    assert resolved_custom.tech_id == "tech_custom_mesh"
    assert resolved_custom.canonical_name == "Custom Service Mesh"

    # Verify existing entries remain identical
    assert mapper.resolve_technology("spanner").tech_id == "tech_cloud_spanner"
    assert mapper.resolve_technology("bq").tech_id == "tech_bigquery"
    assert mapper.resolve_technology("k8s").tech_id == "tech_kubernetes"


# ============================================================================
# 10. ENTITY MAPPING & DEDUPLICATION
# ============================================================================


def test_create_entity_node():
    """Verify create_entity_node validates constraints and generates deterministic node."""
    mapper = TaxonomyMapper()
    node = mapper.create_entity_node(
        category="CONCEPT",
        name="Event-Driven Architecture",
        description="Architecture pattern where state changes trigger events.",
    )
    assert isinstance(node, EntityNode)
    assert node.entity_id == generate_entity_id("CONCEPT", "Event-Driven Architecture")
    assert node.name == "Event-Driven Architecture"
    assert node.category == "CONCEPT"
    assert node.description == "Architecture pattern where state changes trigger events."


def test_map_entities_deduplication_and_sorting():
    """Verify map_entities deduplicates across representations and sorts by entity_id."""
    mapper = TaxonomyMapper()
    entities = [
        ("CONCEPT", "Microservices Architecture"),
        ("concept", "  microservices architecture  "),  # Duplicate tuple
        {"category": "SUBSYSTEM", "name": "Payment Gateway", "description": "Handles transactions"},
        {"category": "API", "name": "REST API v1"},
        mapper.create_entity_node("CONCEPT", "Microservices Architecture", "A variant with description"),
    ]

    mapped = mapper.map_entities(entities)
    assert len(mapped) == 3  # Microservices Architecture, Payment Gateway, REST API v1

    # Verify deterministic sort by entity_id
    ids = [n.entity_id for n in mapped]
    assert ids == sorted(ids)

    # Verify description preservation on deduplication
    microservices_node = next(n for n in mapped if n.name == "Microservices Architecture")
    assert microservices_node.description == "A variant with description"
