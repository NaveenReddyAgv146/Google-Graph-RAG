"""Role-based access control and pre-retrieval filtering utilities."""

from common.rbac.rbac import (
    build_document_access_filter,
    resolve_principal_access,
)

__all__ = [
    "build_document_access_filter",
    "resolve_principal_access",
]
