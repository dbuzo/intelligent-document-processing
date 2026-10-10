"""Authorization. Enforced in code on every request and inside every search query, never delegated
to the model. A prompt can be manipulated; a query filter cannot."""

from __future__ import annotations

from typing import Any

from idp.models import DocumentRecord, Principal


class Forbidden(Exception):
    pass


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """Build the caller from Cognito claims that API Gateway has already verified."""
    groups = claims.get("cognito:groups", [])
    if isinstance(groups, str):  # API Gateway REST passes list claims as "[a b]" or "a,b"
        groups = [g for g in groups.strip("[]").replace(",", " ").split() if g]
    tenant = claims.get("custom:tenant_id")
    if not claims.get("sub") or not tenant:
        raise Forbidden("token is missing sub or tenant")
    return Principal(user_id=claims["sub"], tenant_id=tenant, groups=list(groups))


def can_read(principal: Principal, doc: DocumentRecord) -> bool:
    if principal.tenant_id != doc.tenant_id:
        return False
    if principal.user_id == doc.owner or "admins" in principal.groups:
        return True
    return bool(set(principal.groups) & set(doc.allowed_groups))


def require_read(principal: Principal, doc: DocumentRecord) -> None:
    if not can_read(principal, doc):
        # Same error as "not found" so callers cannot probe for document IDs in other tenants.
        raise Forbidden("document not found")


def require_reviewer(principal: Principal, doc: DocumentRecord | None = None) -> None:
    if not principal.is_reviewer:
        raise Forbidden("reviewer role required")
    if doc is not None:
        require_read(principal, doc)
        if doc.owner == principal.user_id and "admins" not in principal.groups:
            raise Forbidden("segregation of duties: uploader cannot approve their own document")


def search_filter(principal: Principal) -> list[dict]:
    """OpenSearch filter clauses applied to every retrieval query (pre-filter, not post-filter)."""
    clauses: list[dict] = [{"term": {"tenant_id": principal.tenant_id}}]
    if "admins" not in principal.groups:
        clauses.append({"terms": {"allowed_groups": principal.groups or ["__none__"]}})
    return clauses
