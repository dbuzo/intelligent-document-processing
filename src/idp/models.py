"""Domain models shared by the pipeline, the REST API and the MCP server."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DocType(StrEnum):
    CONTRACT = "contract"
    INVOICE = "invoice"
    AUDIT_EVIDENCE = "audit_evidence"
    COMPLIANCE_RECORD = "compliance_record"
    OTHER = "other"


class Status(StrEnum):
    AWAITING_UPLOAD = "AWAITING_UPLOAD"
    PROCESSING = "PROCESSING"
    PENDING_REVIEW = "PENDING_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INDEXED = "INDEXED"
    FAILED = "FAILED"


class TextBlock(BaseModel):
    """A line of text from Textract with its page and OCR confidence (0-100)."""

    text: str
    page: int
    confidence: float


class KeyValue(BaseModel):
    key: str
    value: str
    page: int
    confidence: float


class Table(BaseModel):
    page: int
    rows: list[list[str]]
    confidence: float


class ExtractedDocument(BaseModel):
    """Normalized Textract output. Stored in S3; the state machine passes only its S3 key."""

    document_id: str
    pages: int
    lines: list[TextBlock] = Field(default_factory=list)
    key_values: list[KeyValue] = Field(default_factory=list)
    tables: list[Table] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def mean_ocr_confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)

    @property
    def low_confidence_pages(self) -> list[int]:
        by_page: dict[int, list[float]] = {}
        for line in self.lines:
            by_page.setdefault(line.page, []).append(line.confidence)
        return sorted(p for p, c in by_page.items() if sum(c) / len(c) < 80.0)


class Classification(BaseModel):
    doc_type: DocType
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class ExtractedField(BaseModel):
    name: str
    value: str | None
    # Verbatim quote from the document that supports the value; used for the grounding check.
    evidence: str | None = None
    page: int | None = None
    model_confidence: float = Field(ge=0.0, le=1.0)
    ocr_confidence: float | None = None
    grounded: bool = False
    corrected_by: str | None = None
    original_value: str | None = None  # model output before a reviewer correction

    @property
    def confidence(self) -> float:
        """Combined score: the model's own estimate, discounted by OCR quality and grounding."""
        score = self.model_confidence
        if self.ocr_confidence is not None:
            score *= min(1.0, self.ocr_confidence / 100.0)
        if self.value is not None and not self.grounded:
            score *= 0.5
        return round(score, 3)


class ValidationIssue(BaseModel):
    field: str | None
    code: str
    message: str
    severity: str = "error"  # "error" forces review; "warning" is informational


class ExtractionResult(BaseModel):
    document_id: str
    doc_type: DocType
    fields: list[ExtractedField]
    summary: str | None = None
    issues: list[ValidationIssue] = Field(default_factory=list)
    needs_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)

    def field_map(self) -> dict[str, ExtractedField]:
        return {f.name: f for f in self.fields}


class DocumentRecord(BaseModel):
    """Row in the DynamoDB documents table. ACL fields drive every authorization decision."""

    document_id: str
    tenant_id: str
    owner: str
    allowed_groups: list[str]
    filename: str
    content_type: str
    s3_key: str
    status: Status = Status.AWAITING_UPLOAD
    doc_type: DocType | None = None
    classification_confidence: float | None = None
    summary: str | None = None
    extraction_key: str | None = None
    result_key: str | None = None
    review_reasons: list[str] = Field(default_factory=list)
    reviewed_by: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utcnow)
    updated_at: str = Field(default_factory=utcnow)

    def public_view(self) -> dict[str, Any]:
        return self.model_dump(exclude={"s3_key", "extraction_key", "result_key"})


class Chunk(BaseModel):
    chunk_id: str
    document_id: str
    tenant_id: str
    allowed_groups: list[str]
    doc_type: DocType
    page: int | None
    text: str
    embedding: list[float] | None = None


class SearchHit(BaseModel):
    chunk_id: str
    document_id: str
    doc_type: DocType
    page: int | None
    text: str
    score: float


class Principal(BaseModel):
    """The authenticated caller, built from verified Cognito JWT claims."""

    user_id: str
    tenant_id: str
    groups: list[str] = Field(default_factory=list)

    @property
    def is_reviewer(self) -> bool:
        return "reviewers" in self.groups or "admins" in self.groups
