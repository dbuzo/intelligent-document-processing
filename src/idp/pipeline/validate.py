"""Deterministic validation and the review-routing decision.

The model proposes values; these rules decide whether a human must look. Anything uncertain is made
visible as an issue with a reason, never silently accepted.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from idp.config import Settings
from idp.models import Classification, DocType, ExtractedDocument, ExtractionResult, ValidationIssue
from idp.schemas import SCHEMAS

INJECTION_PATTERNS = re.compile(
    r"(ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(system|previous) prompt|"
    r"you are now|mark (this|the) (invoice|document) (as )?(approved|paid)|system prompt:)",
    re.IGNORECASE,
)


def _money(v: str | None) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(re.sub(r"[^0-9.\-]", "", v))
    except InvalidOperation:
        return None


def _date(v: str | None) -> date | None:
    if v is None:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def validate(
    result: ExtractionResult,
    classification: Classification,
    doc: ExtractedDocument,
    settings: Settings,
) -> ExtractionResult:
    issues: list[ValidationIssue] = []
    reasons: list[str] = []
    fields = result.field_map()

    if classification.confidence < settings.min_classification_confidence:
        reasons.append(f"classification confidence {classification.confidence:.2f} below threshold")
    if result.doc_type == DocType.OTHER:
        reasons.append("unrecognized document type")

    if doc.mean_ocr_confidence < settings.min_ocr_confidence:
        reasons.append(f"low OCR quality (mean {doc.mean_ocr_confidence:.0f})")
    elif doc.low_confidence_pages:
        issues.append(
            ValidationIssue(field=None, code="LOW_OCR_PAGES", severity="warning",
                            message=f"pages with low OCR confidence: {doc.low_confidence_pages}")
        )

    if INJECTION_PATTERNS.search(doc.text):
        issues.append(
            ValidationIssue(field=None, code="POSSIBLE_PROMPT_INJECTION",
                            message="document contains instruction-like text aimed at an AI system")
        )

    for spec in SCHEMAS[result.doc_type]:
        f = fields.get(spec.name)
        if f is None or f.value is None:
            if spec.required:
                issues.append(ValidationIssue(field=spec.name, code="MISSING_REQUIRED",
                                              message="required field not found"))
            continue
        if not f.grounded:
            issues.append(ValidationIssue(field=spec.name, code="UNGROUNDED",
                                          message="value not supported by a quote from the document"))
        if spec.critical and f.confidence < settings.min_field_confidence:
            issues.append(ValidationIssue(field=spec.name, code="LOW_CONFIDENCE",
                                          message=f"critical field confidence {f.confidence:.2f}"))
        elif f.confidence < settings.min_field_confidence:
            issues.append(ValidationIssue(field=spec.name, code="LOW_CONFIDENCE", severity="warning",
                                          message=f"confidence {f.confidence:.2f}"))
        if spec.kind == "date" and _date(f.value) is None:
            issues.append(ValidationIssue(field=spec.name, code="BAD_DATE", message=f"not an ISO date: {f.value!r}"))
        if spec.kind == "money" and _money(f.value) is None:
            issues.append(ValidationIssue(field=spec.name, code="BAD_AMOUNT", message=f"not an amount: {f.value!r}"))

    issues.extend(_cross_field_rules(result))

    errors = [i for i in issues if i.severity == "error"]
    reasons.extend(f"{i.code}: {i.field or 'document'}" for i in errors)
    return result.model_copy(update={"issues": issues, "needs_review": bool(reasons), "review_reasons": reasons})


def _cross_field_rules(result: ExtractionResult) -> list[ValidationIssue]:
    f = {k: v.value for k, v in result.field_map().items()}
    issues: list[ValidationIssue] = []
    if result.doc_type == DocType.INVOICE:
        subtotal, tax, total = _money(f.get("subtotal")), _money(f.get("tax")), _money(f.get("total"))
        amounts_present = subtotal is not None and tax is not None and total is not None
        if amounts_present and abs(subtotal + tax - total) > Decimal("0.01"):
            issues.append(ValidationIssue(field="total", code="TOTAL_MISMATCH",
                                          message=f"subtotal {subtotal} + tax {tax} != total {total}"))
        if total is not None and total < 0:
            issues.append(ValidationIssue(field="total", code="NEGATIVE_TOTAL", severity="warning",
                                          message="negative total; credit note?"))
        issued, due = _date(f.get("invoice_date")), _date(f.get("due_date"))
        if issued and due and due < issued:
            issues.append(ValidationIssue(field="due_date", code="DATE_ORDER", message="due date before invoice date"))
    if result.doc_type == DocType.CONTRACT:
        start, end = _date(f.get("effective_date")), _date(f.get("termination_date"))
        if start and end and end <= start:
            issues.append(ValidationIssue(field="termination_date", code="DATE_ORDER",
                                          message="termination date not after effective date"))
    if result.doc_type == DocType.AUDIT_EVIDENCE:
        res = (f.get("result") or "").lower()
        if res and res not in {"pass", "fail", "exception"}:
            issues.append(ValidationIssue(field="result", code="BAD_ENUM", message=f"unexpected result {res!r}"))
        if res in {"fail", "exception"}:
            issues.append(ValidationIssue(field="result", code="CONTROL_EXCEPTION",
                                          message="evidence records a failed control; needs auditor attention"))
    return issues
