"""Dual-bucket resolver: seamlessly resolves legacy multi-region and regional Mumbai assets."""

import logging

from common.config import settings

logger = logging.getLogger("common.storage")


def resolve_gcs_asset_uri(raw_uri: str, prefer_regional: bool = True) -> str:
    """
    Resolves GCS URIs across dual buckets:
    - Legacy multi-region assets: gs://sales-discovery-mvp/...
    - Target Mumbai regional assets: gs://sales-discovery-mvp-mumbai/...

    If prefer_regional is True and the asset has been synced to Mumbai, directs traffic regionally.
    """
    if not raw_uri or not raw_uri.startswith("gs://"):
        return raw_uri

    # If already regional, return as-is
    if raw_uri.startswith(f"gs://{settings.gcs_mumbai_bucket}/"):
        return raw_uri

    # Legacy multi-region reference
    if raw_uri.startswith(f"gs://{settings.gcs_legacy_bucket}/"):
        if prefer_regional:
            relative_path = raw_uri[len(f"gs://{settings.gcs_legacy_bucket}/") :]
            # Returns regional target URI
            return f"gs://{settings.gcs_mumbai_bucket}/{relative_path}"
        return raw_uri

    return raw_uri
