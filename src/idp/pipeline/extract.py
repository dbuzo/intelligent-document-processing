"""Field extraction with Claude, then grounding: every value must be traceable to the source text."""

from __future__ import annotations

import re

from idp.adapters.llm import LLM, LLMError
from idp.adapters.textract import table_to_text
from idp.models import DocType, ExtractedDocument, ExtractedField, ExtractionResult
from idp.prompts import extract_system, fence_document
from idp.schemas import json_schema_for

# Well inside the model's context window; anything larger is a sign of a bad upload, not a document
# we should silently truncate.
MAX_CHARS = 600_000


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def render_for_llm(doc: ExtractedDocument) -> tuple[str, list[tuple[str, str]]]:
    by_page: dict[int, list[str]] = {}
    for line in doc.lines:
        by_page.setdefault(line.page, []).append(line.text)
    for t in doc.tables:
        by_page.setdefault(t.page, []).append("[table]\n" + table_to_text(t) + "\n[/table]")
    text = "\n\n".join(f"--- page {p} ---\n" + "\n".join(chunks) for p, chunks in sorted(by_page.items()))
    kvs = [(kv.key, kv.value) for kv in doc.key_values if kv.key and kv.value]
    return text, kvs


def ground(field: ExtractedField, doc: ExtractedDocument) -> ExtractedField:
    """Check the evidence quote exists in the OCR text and contains the value; attach OCR confidence."""
    if field.value is None:
        field.grounded = field.evidence is None
        return field
    if not field.evidence:
        field.grounded = False
        return field
    ev = _norm(field.evidence)
    source = _norm(doc.text) + _norm(" ".join(c for t in doc.tables for row in t.rows for c in row))
    # Values are normalized by the model (dates to ISO, money without symbols), so compare loosely:
    # every digit/letter run of the value should appear in the evidence.
    value_tokens = [tok for tok in re.findall(r"[a-z0-9]+", field.value.lower()) if len(tok) > 1]
    field.grounded = bool(ev) and ev in source and all(
        tok in ev or tok.lstrip("0") in ev for tok in value_tokens if not _is_date_part(tok, field.value)
    )
    matching = [
        line for line in doc.lines if (n := _norm(line.text)) and (n in ev or ev in n)
    ]
    if matching:
        field.ocr_confidence = min(line.confidence for line in matching)
        field.page = field.page or matching[0].page
    return field


def _is_date_part(token: str, value: str) -> bool:
    # ISO dates are reformatted from "March 3, 2026" etc.; don't require "2026-03-03" pieces verbatim.
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)) and token.isdigit() and len(token) <= 2


def extract(llm: LLM, doc: ExtractedDocument, doc_type: DocType) -> ExtractionResult:
    if doc_type == DocType.OTHER:
        return ExtractionResult(document_id=doc.document_id, doc_type=doc_type, fields=[])

    text, kvs = render_for_llm(doc)
    if len(text) > MAX_CHARS:
        raise LLMError(f"document text is {len(text)} chars; above the {MAX_CHARS} limit, needs manual handling")

    raw = llm.complete_json(
        system=extract_system(doc_type),
        content=fence_document(text, kvs) + "\n\nExtract the fields.",
        schema=json_schema_for(doc_type),
        effort="medium",
    )
    fields = []
    for name, f in raw["fields"].items():
        field = ExtractedField(
            name=name,
            value=f.get("value"),
            evidence=f.get("evidence"),
            page=f.get("page"),
            model_confidence=min(1.0, max(0.0, float(f.get("confidence", 0.0)))),
        )
        fields.append(ground(field, doc))
    return ExtractionResult(document_id=doc.document_id, doc_type=doc_type, fields=fields, summary=raw.get("summary"))
