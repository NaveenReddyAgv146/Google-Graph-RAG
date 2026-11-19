"""Deterministic entity and technology extraction for Discovery Copilot (Phase 4.2).

Integrates:
1. common.taxonomy.taxonomy_mapper.TaxonomyMapper in strict mode for technology normalization.
2. Canonical TechId lookup without hallucination or invention.
3. Separation of customer names, business domains, metrics, and technology entities.
4. Handling of unknown technologies as non-canonical keywords (safe for SQL/vector, blocked from GQL).
"""

from __future__ import annotations

import re
from typing import Sequence

from common.taxonomy.taxonomy_mapper import (
    TaxonomyMapper,
    build_default_catalog,
    normalize_lookup_key,
)
from app.planning.models import EntityCategory, ValidatedEntity

# Known enterprise customers and organizations
KNOWN_CUSTOMERS = {
    "uber": "Uber",
    "ubercab": "UberCab",
    "canva": "Canva",
    "dropbox": "Dropbox",
    "youtube": "YouTube",
    "netflix": "Netflix",
    "spotify": "Spotify",
}

# Known business domains
KNOWN_DOMAINS = {
    "banking": "Banking & Financial Services",
    "finance": "Banking & Financial Services",
    "financial": "Banking & Financial Services",
    "ride-hailing": "Transportation & Mobility",
    "mobility": "Transportation & Mobility",
    "transportation": "Transportation & Mobility",
    "media": "Digital Media & Design",
    "design": "Digital Media & Design",
    "video": "Digital Media & Streaming",
    "storage": "Data Storage & Sync",
    "sync": "Data Storage & Sync",
    "ecommerce": "E-Commerce & Retail",
    "retail": "E-Commerce & Retail",
    "healthcare": "Healthcare & Life Sciences",
}

# Metric / operational goals
METRIC_PATTERNS = [
    (r"\b(sub-\d+\s*ms|\d+\s*ms|\d+\s*milliseconds?)\b", "Latency Requirement"),
    (r"\b(99\.\d+%\s*(?:sla|availability|uptime)?)\b", "High Availability Requirement"),
    (r"\b(real-time|real time|sub-second|low latency)\b", "Performance Goal"),
    (r"\b(zero downtime|active-active|multi-region|disaster recovery)\b", "Reliability Architecture"),
]


class EntityExtractor:
    """Extracts, classifies, and validates entities from natural language queries."""

    def __init__(self, mapper: TaxonomyMapper | None = None) -> None:
        self._mapper = mapper or TaxonomyMapper(catalog=build_default_catalog())
        self._catalog = self._mapper.catalog

    def extract_entities(self, query: str) -> list[ValidatedEntity]:
        """Extracts and validates all entities, technologies, customers, and domains in a query.

        Enforces:
        - Technologies: mapped to canonical TechnologyCatalogEntry.
        - Aliases: resolved deterministically (e.g. 'docai' -> 'Document AI', 'k8s' -> 'Kubernetes').
        - Unknown technologies: flagged with is_canonical=False and tech_id=None (NEVER invented).
        - Customers and domains: tagged separately from technologies.
        """
        if not query or not query.strip():
            return []

        extracted: list[ValidatedEntity] = []
        seen_canonical_ids: set[str] = set()
        seen_raw_mentions: set[str] = set()

        text_lower = query.lower()

        # 1. Match Canonical Technologies from Catalog (longest alias first for greedy matching)
        all_entries = self._catalog.all_entries()
        # Sort catalog entries by length of matching alias descending to avoid partial word substring bugs
        all_patterns: list[tuple[str, str, Any]] = []
        for entry in all_entries:
            # Add canonical name
            all_patterns.append((normalize_lookup_key(entry.canonical_name), entry.canonical_name, entry))
            for alias in entry.aliases:
                all_patterns.append((normalize_lookup_key(alias), alias, entry))

        # Sort longest match first
        all_patterns.sort(key=lambda x: len(x[0]), reverse=True)

        for norm_alias, raw_repr, entry in all_patterns:
            # Word boundary regex matching to prevent accidental substrings (e.g. "go" in "google")
            escaped = re.escape(norm_alias)
            pattern = rf"(?:\b|\A){escaped}(?:\b|\Z)"
            if re.search(pattern, text_lower):
                if entry.tech_id not in seen_canonical_ids:
                    seen_canonical_ids.add(entry.tech_id)
                    seen_raw_mentions.add(norm_alias)
                    extracted.append(
                        ValidatedEntity(
                            raw_mention=raw_repr,
                            category=EntityCategory.TECHNOLOGY,
                            canonical_name=entry.canonical_name,
                            tech_id=entry.tech_id,
                            is_canonical=True,
                        )
                    )

        # 2. Match Customer Names
        for cust_key, cust_name in KNOWN_CUSTOMERS.items():
            pattern = rf"(?:\b|\A){re.escape(cust_key)}(?:\b|\Z)"
            if re.search(pattern, text_lower):
                if cust_key not in seen_raw_mentions:
                    seen_raw_mentions.add(cust_key)
                    extracted.append(
                        ValidatedEntity(
                            raw_mention=cust_name,
                            category=EntityCategory.CUSTOMER,
                            canonical_name=cust_name,
                            tech_id=None,
                            is_canonical=False,
                        )
                    )

        # 3. Match Business Domains
        for dom_key, dom_name in KNOWN_DOMAINS.items():
            pattern = rf"(?:\b|\A){re.escape(dom_key)}(?:\b|\Z)"
            if re.search(pattern, text_lower):
                if dom_key not in seen_raw_mentions:
                    seen_raw_mentions.add(dom_key)
                    extracted.append(
                        ValidatedEntity(
                            raw_mention=dom_key,
                            category=EntityCategory.BUSINESS_DOMAIN,
                            canonical_name=dom_name,
                            tech_id=None,
                            is_canonical=False,
                        )
                    )

        # 4. Match Metrics & Operational Goals
        for metric_regex, metric_label in METRIC_PATTERNS:
            match = re.search(metric_regex, text_lower)
            if match:
                raw = match.group(0)
                if raw not in seen_raw_mentions:
                    seen_raw_mentions.add(raw)
                    extracted.append(
                        ValidatedEntity(
                            raw_mention=raw,
                            category=EntityCategory.METRIC_OR_GOAL,
                            canonical_name=metric_label,
                            tech_id=None,
                            is_canonical=False,
                        )
                    )

        return extracted

    def validate_unknown_technology(self, candidate_name: str) -> ValidatedEntity:
        """Inspects a technology candidate not recognized in catalog.

        Ensures:
        - NEVER invents a TechId.
        - Sets is_canonical=False.
        - Safe for use in text search, blocked from graph traversal.
        """
        entry = self._mapper.resolve_technology(candidate_name, strict=False)
        if entry is not None:
            return ValidatedEntity(
                raw_mention=candidate_name,
                category=EntityCategory.TECHNOLOGY,
                canonical_name=entry.canonical_name,
                tech_id=entry.tech_id,
                is_canonical=True,
            )

        return ValidatedEntity(
            raw_mention=candidate_name,
            category=EntityCategory.TECHNOLOGY,
            canonical_name=None,
            tech_id=None,
            is_canonical=False,
        )

