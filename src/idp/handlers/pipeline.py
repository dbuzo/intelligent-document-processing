"""Step Functions task handlers for the processing state machine.

    StartExtraction -> (Wait -> CheckExtraction)* -> CollectExtraction -> Classify -> Extract
      -> Validate -> Choice(needs_review) -> NotifyReview | Index
    Any failure -> MarkFailed
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote_plus

from aws_lambda_powertools import Logger

from idp.adapters.llm import LLMError
from idp.adapters.textract import parse_blocks
from idp.deps import get_deps
from idp.models import (
    Classification,
    DocType,
    ExtractedDocument,
    ExtractionResult,
    Status,
)
from idp.pipeline.classify import classify as classify_doc
from idp.pipeline.extract import extract as extract_fields
from idp.pipeline.index import index_document
from idp.pipeline.validate import validate as validate_result
from idp.security.redaction import redact

logger = Logger(service="idp-pipeline")


def _document_id_from_key(key: str) -> str:
    # uploads/{tenant_id}/{document_id}/{filename}
    parts = unquote_plus(key).split("/")
    if len(parts) < 4 or parts[0] != "uploads":
        raise ValueError(f"unexpected object key layout: {key!r}")
    return parts[2]


@logger.inject_lambda_context
def start_extraction(event: dict, context: Any) -> dict:
    d = get_deps()
    bucket, key = event["bucket"], event["key"]
    document_id = _document_id_from_key(key)
    record = d.repo.get(document_id)
    if record is None or record.s3_key != unquote_plus(key):
        # Objects can only be created via a presigned POST tied to a registered record.
        raise ValueError(f"no registered document for key {key!r}")
    job_id = d.textract.start_analysis(bucket, record.s3_key, document_id)
    d.repo.update(document_id, status=Status.PROCESSING)
    logger.info("textract started", extra={"document_id": document_id, "job_id": job_id})
    return {"document_id": document_id, "job_id": job_id}


@logger.inject_lambda_context
def check_extraction(event: dict, context: Any) -> dict:
    status = get_deps().textract.get_status(event["job_id"])
    if status == "FAILED":
        raise RuntimeError("Textract job failed")
    return {**event, "textract_status": status}  # IN_PROGRESS | SUCCEEDED | PARTIAL_SUCCESS


@logger.inject_lambda_context
def collect_extraction(event: dict, context: Any) -> dict:
    d = get_deps()
    document_id = event["document_id"]
    doc = parse_blocks(document_id, d.textract.get_blocks(event["job_id"]))
    key = f"extracted/{document_id}.json"
    d.blobs.put_json(key, doc.model_dump(mode="json"))
    d.repo.update(document_id, extraction_key=key)
    logger.info("extraction stored", extra={"document_id": document_id, "pages": doc.pages,
                                            "mean_ocr": round(doc.mean_ocr_confidence, 1)})
    return {"document_id": document_id, "extraction_key": key}


def _load_doc(key: str) -> ExtractedDocument:
    return ExtractedDocument.model_validate(get_deps().blobs.get_json(key))


@logger.inject_lambda_context
def classify(event: dict, context: Any) -> dict:
    d = get_deps()
    doc = _load_doc(event["extraction_key"])
    try:
        c = classify_doc(d.llm, doc)
    except LLMError as e:
        c = Classification(doc_type=DocType.OTHER, confidence=0.0, rationale=f"model error: {e}")
    d.repo.update(event["document_id"], doc_type=c.doc_type, classification_confidence=c.confidence)
    logger.info("classified", extra={"document_id": event["document_id"], "doc_type": c.doc_type.value,
                                     "confidence": c.confidence})
    return {**event, "classification": c.model_dump(mode="json")}


@logger.inject_lambda_context
def extract(event: dict, context: Any) -> dict:
    d = get_deps()
    doc = _load_doc(event["extraction_key"])
    c = Classification.model_validate(event["classification"])
    try:
        result = extract_fields(d.llm, doc, c.doc_type)
    except LLMError as e:
        result = ExtractionResult(
            document_id=doc.document_id, doc_type=c.doc_type, fields=[],
            needs_review=True, review_reasons=[f"extraction failed: {e}"],
        )
    key = f"results/{doc.document_id}.json"
    d.blobs.put_json(key, result.model_dump(mode="json"))
    return {**event, "result_key": key}


@logger.inject_lambda_context
def validate(event: dict, context: Any) -> dict:
    d = get_deps()
    doc = _load_doc(event["extraction_key"])
    c = Classification.model_validate(event["classification"])
    prior = ExtractionResult.model_validate(d.blobs.get_json(event["result_key"]))
    result = validate_result(prior, c, doc, d.settings)
    if prior.review_reasons:
        result = result.model_copy(update={"needs_review": True,
                                           "review_reasons": prior.review_reasons + result.review_reasons})
    d.blobs.put_json(event["result_key"], result.model_dump(mode="json"))
    d.repo.update(
        doc.document_id,
        result_key=event["result_key"],
        summary=result.summary,
        review_reasons=result.review_reasons,
        status=Status.PENDING_REVIEW if result.needs_review else Status.APPROVED,
    )
    logger.info("validated", extra={"document_id": doc.document_id, "needs_review": result.needs_review,
                                    "issue_codes": sorted({i.code for i in result.issues})})
    return {"document_id": doc.document_id, "needs_review": result.needs_review}


@logger.inject_lambda_context
def notify_review(event: dict, context: Any) -> dict:
    d = get_deps()
    record = d.repo.get(event["document_id"])
    d.notify_review(event["document_id"], record.review_reasons if record else [])
    return event


@logger.inject_lambda_context
def index(event: dict, context: Any) -> dict:
    """Also invoked asynchronously by the API after a reviewer approves a document."""
    d = get_deps()
    record = d.repo.get(event["document_id"])
    if record is None or record.status not in (Status.APPROVED, Status.INDEXED):
        raise ValueError("only approved documents are indexed")
    doc = _load_doc(record.extraction_key)
    result = ExtractionResult.model_validate(d.blobs.get_json(record.result_key))
    n = index_document(record, doc, result, d.embedder, d.index)
    d.repo.update(record.document_id, status=Status.INDEXED)
    logger.info("indexed", extra={"document_id": record.document_id, "chunks": n})
    return {"document_id": record.document_id, "chunks": n}


@logger.inject_lambda_context
def mark_failed(event: dict, context: Any) -> dict:
    d = get_deps()
    document_id = event.get("document_id") or _document_id_from_key(event.get("key", ""))
    cause = redact(str(event.get("error", {}).get("Cause", "unknown error")))
    if d.repo.get(document_id) is None:
        # e.g. an object written without a registered record; nothing to mark, but keep it visible.
        logger.error("pipeline failed for unregistered object", extra={"key": event.get("key"), "cause": cause})
        return {"document_id": document_id, "status": "FAILED"}
    d.repo.update(document_id, status=Status.FAILED, error=cause)
    logger.error("pipeline failed", extra={"document_id": document_id, "cause": cause})
    return {"document_id": document_id, "status": "FAILED"}
