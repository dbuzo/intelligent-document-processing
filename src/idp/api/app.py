"""REST API (API Gateway REST + Cognito authorizer -> this Lambda).

Every route resolves the caller from verified JWT claims and checks access in code. The MCP server is
a client of this API, so humans, scripts and AI agents all pass through the same authorization.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver, Response, content_types
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, ForbiddenError, NotFoundError
from aws_lambda_powertools.logging import correlation_paths
from pydantic import BaseModel, Field, ValidationError

from idp.deps import get_deps
from idp.models import DocType, DocumentRecord, ExtractionResult, Principal, Status, utcnow
from idp.pipeline.answer import answer as rag_answer
from idp.pipeline.answer import search as rag_search
from idp.security.authz import Forbidden, can_read, principal_from_claims, require_read, require_reviewer

logger = Logger(service="idp-api")
app = APIGatewayRestResolver(enable_validation=False)

ALLOWED_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/tiff"}
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


# ---------- request bodies ----------

class CreateDocument(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_type: str
    allowed_groups: list[str] = Field(default_factory=list, max_length=20)


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)
    doc_type: DocType | None = None


class ReviewRequest(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    corrections: dict[str, str | None] = Field(default_factory=dict)
    comment: str = Field(default="", max_length=2000)


# ---------- helpers ----------

def _principal() -> Principal:
    claims = app.current_event.request_context.authorizer.claims or {}
    try:
        return principal_from_claims(claims)
    except Forbidden as e:
        raise ForbiddenError(str(e)) from e


def _body(model: type[BaseModel]) -> Any:
    try:
        return model.model_validate(app.current_event.json_body or {})
    except ValidationError as e:
        raise BadRequestError("; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())) from e


def _get_doc(document_id: str, principal: Principal) -> DocumentRecord:
    doc = get_deps().repo.get(document_id)
    if doc is None:
        raise NotFoundError("document not found")
    try:
        require_read(principal, doc)
    except Forbidden as e:
        raise NotFoundError("document not found") from e
    return doc


@app.exception_handler(Forbidden)
def _forbidden(e: Forbidden) -> Response:
    return Response(status_code=403, content_type=content_types.APPLICATION_JSON, body={"message": str(e)})


# ---------- routes ----------

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/documents")
def create_document() -> tuple[dict, int]:
    """Register a document and return a presigned POST. Upload lands in S3 and starts the pipeline."""
    p = _principal()
    req: CreateDocument = _body(CreateDocument)
    if req.content_type not in ALLOWED_TYPES:
        raise BadRequestError(f"content_type must be one of {sorted(ALLOWED_TYPES)}")
    # Callers may only share with groups they belong to.
    groups = req.allowed_groups or p.groups
    if not set(groups) <= set(p.groups) and "admins" not in p.groups:
        raise ForbiddenError("allowed_groups must be a subset of your groups")

    d = get_deps()
    document_id = str(uuid.uuid4())
    filename = SAFE_NAME.sub("_", req.filename)
    key = f"uploads/{p.tenant_id}/{document_id}/{filename}"
    d.repo.put(
        DocumentRecord(
            document_id=document_id, tenant_id=p.tenant_id, owner=p.user_id, allowed_groups=groups,
            filename=filename, content_type=req.content_type, s3_key=key,
        )
    )
    upload = d.uploader.presign(key, req.content_type)
    logger.info("document registered", extra={"document_id": document_id, "tenant_id": p.tenant_id})
    return {"document_id": document_id, "upload": upload, "expires_in": d.settings.upload_url_ttl_seconds}, 201


@app.get("/documents")
def list_documents() -> dict:
    p = _principal()
    params = app.current_event.query_string_parameters or {}
    status = Status(params["status"]) if params.get("status") else None
    docs = [doc for doc in get_deps().repo.list_by_tenant(p.tenant_id, status) if can_read(p, doc)]
    return {"documents": [doc.public_view() for doc in docs]}


@app.get("/documents/<document_id>")
def get_document(document_id: str) -> dict:
    return _get_doc(document_id, _principal()).public_view()


@app.get("/documents/<document_id>/fields")
def get_fields(document_id: str) -> dict:
    doc = _get_doc(document_id, _principal())
    if not doc.result_key:
        return {"document_id": document_id, "status": doc.status, "fields": [], "issues": []}
    result = ExtractionResult.model_validate(get_deps().blobs.get_json(doc.result_key))
    return {
        "document_id": document_id,
        "status": doc.status,
        "doc_type": result.doc_type,
        "summary": result.summary,
        "fields": [{**f.model_dump(), "confidence": f.confidence} for f in result.fields],
        "issues": [i.model_dump() for i in result.issues],
        "review_reasons": result.review_reasons,
    }


@app.post("/search")
def search() -> dict:
    p = _principal()
    req: SearchRequest = _body(SearchRequest)
    d = get_deps()
    hits = rag_search(p, req.query, d.embedder, d.index, k=req.k, doc_type=req.doc_type)
    return {"hits": [h.model_dump() for h in hits]}


@app.post("/ask")
def ask() -> dict:
    p = _principal()
    req: SearchRequest = _body(SearchRequest)
    d = get_deps()
    return rag_answer(p, req.query, d.embedder, d.index, d.llm, doc_type=req.doc_type).model_dump()


@app.get("/reviews")
def review_queue() -> dict:
    p = _principal()
    require_reviewer(p)
    docs = get_deps().repo.list_by_tenant(p.tenant_id, Status.PENDING_REVIEW)
    return {"documents": [d.public_view() for d in docs if can_read(p, d)]}


@app.post("/documents/<document_id>/review")
def submit_review(document_id: str) -> dict:
    p = _principal()
    doc = _get_doc(document_id, p)
    require_reviewer(p, doc)
    if doc.status != Status.PENDING_REVIEW:
        raise BadRequestError(f"document is {doc.status}, not PENDING_REVIEW")
    req: ReviewRequest = _body(ReviewRequest)
    d = get_deps()

    result = ExtractionResult.model_validate(d.blobs.get_json(doc.result_key)) if doc.result_key else None
    if result is not None and req.corrections:
        fields = result.field_map()
        unknown = set(req.corrections) - set(fields)
        if unknown:
            raise BadRequestError(f"unknown fields: {sorted(unknown)}")
        for name, value in req.corrections.items():
            f = fields[name]
            if f.corrected_by is None:
                f.original_value = f.value
            f.value, f.corrected_by, f.grounded, f.model_confidence = value, p.user_id, True, 1.0
        d.blobs.put_json(doc.result_key, result.model_dump(mode="json"))

    # Append-only audit record of who decided what, kept alongside the result.
    d.blobs.put_json(
        f"reviews/{document_id}/{utcnow()}.json",
        {"reviewer": p.user_id, "decision": req.decision, "corrected_fields": sorted(req.corrections),
         "comment": req.comment, "at": utcnow()},
    )
    status = Status.APPROVED if req.decision == "approve" else Status.REJECTED
    d.repo.update(document_id, status=status, reviewed_by=p.user_id)
    if status == Status.APPROVED:
        d.trigger_index(document_id)
    logger.info("review submitted", extra={"document_id": document_id, "decision": req.decision})
    return {"document_id": document_id, "status": status}


@logger.inject_lambda_context(correlation_id_path=correlation_paths.API_GATEWAY_REST)
def handler(event: dict, context: Any) -> dict:
    return app.resolve(event, context)
