"""Per-document-type field definitions.

`required` fields must be present for auto-approval; `critical` fields always need high confidence
because a wrong value has business impact (paying the wrong amount, missing a renewal date).
"""

from __future__ import annotations

from dataclasses import dataclass

from idp.models import DocType


@dataclass(frozen=True)
class FieldSpec:
    name: str
    description: str
    kind: str = "string"  # string | date | money | number
    required: bool = False
    critical: bool = False


SCHEMAS: dict[DocType, list[FieldSpec]] = {
    DocType.INVOICE: [
        FieldSpec("invoice_number", "Invoice identifier as printed", required=True, critical=True),
        FieldSpec("vendor_name", "Legal name of the issuing vendor", required=True),
        FieldSpec("invoice_date", "Date of issue, ISO 8601", kind="date", required=True),
        FieldSpec("due_date", "Payment due date, ISO 8601", kind="date"),
        FieldSpec("currency", "ISO 4217 currency code"),
        FieldSpec("subtotal", "Amount before tax", kind="money"),
        FieldSpec("tax", "Total tax amount", kind="money"),
        FieldSpec("total", "Total amount due", kind="money", required=True, critical=True),
        FieldSpec("purchase_order", "Referenced purchase order number"),
    ],
    DocType.CONTRACT: [
        FieldSpec("title", "Agreement title", required=True),
        FieldSpec("parties", "Contracting parties, semicolon separated", required=True, critical=True),
        FieldSpec("effective_date", "Effective date, ISO 8601", kind="date", required=True, critical=True),
        FieldSpec("termination_date", "End or expiry date, ISO 8601", kind="date", critical=True),
        FieldSpec("renewal_terms", "Auto-renewal and notice terms"),
        FieldSpec("governing_law", "Governing law / jurisdiction"),
        FieldSpec("liability_cap", "Limitation of liability amount or formula"),
        FieldSpec("termination_notice_days", "Days of notice required to terminate", kind="number"),
    ],
    DocType.AUDIT_EVIDENCE: [
        FieldSpec("control_id", "Control identifier the evidence supports", required=True, critical=True),
        FieldSpec("evidence_date", "Date the evidence was produced, ISO 8601", kind="date", required=True),
        FieldSpec("system", "System or application in scope", required=True),
        FieldSpec("performed_by", "Person or role that performed the control"),
        FieldSpec("result", "Outcome: pass, fail, or exception", required=True, critical=True),
        FieldSpec("period", "Audit period covered"),
    ],
    DocType.COMPLIANCE_RECORD: [
        FieldSpec("regulation", "Regulation or framework (e.g. SOC 2, HIPAA)", required=True),
        FieldSpec("record_type", "Policy, attestation, training record, etc.", required=True),
        FieldSpec("owner", "Accountable owner"),
        FieldSpec("effective_date", "Effective date, ISO 8601", kind="date", required=True),
        FieldSpec("review_date", "Next review date, ISO 8601", kind="date", critical=True),
        FieldSpec("status", "Current status: active, expired, draft"),
    ],
    DocType.OTHER: [],
}


def field_names(doc_type: DocType) -> list[str]:
    return [f.name for f in SCHEMAS[doc_type]]


def _nullable(kind: str) -> dict:
    return {"anyOf": [{"type": kind}, {"type": "null"}]}


def json_schema_for(doc_type: DocType) -> dict:
    """JSON schema for the result tool Claude calls (validated again in `adapters/llm.py`).

    Every field is required in the schema and nullable: the model must state "not found" explicitly
    (null) rather than silently dropping a field, which keeps missing data visible.
    """
    field_obj = {
        "type": "object",
        "properties": {
            "value": _nullable("string"),
            "evidence": _nullable("string"),
            "page": _nullable("integer"),
            "confidence": {"type": "number", "description": "0.0-1.0"},
        },
        "required": ["value", "evidence", "page", "confidence"],
        "additionalProperties": False,
    }
    names = field_names(doc_type)
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "properties": {name: field_obj for name in names},
                "required": names,
                "additionalProperties": False,
            },
            "summary": {"type": "string"},
        },
        "required": ["fields", "summary"],
        "additionalProperties": False,
    }


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": [t.value for t in DocType]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["doc_type", "confidence", "rationale"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}, "description": "chunk_ids used"},
        "insufficient_context": {"type": "boolean"},
    },
    "required": ["answer", "citations", "insufficient_context"],
    "additionalProperties": False,
}
