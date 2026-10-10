"""Step 1 after OCR: decide which extraction schema applies."""

from __future__ import annotations

from idp.adapters.llm import LLM
from idp.models import Classification, ExtractedDocument
from idp.prompts import CLASSIFY_SYSTEM, fence_document
from idp.schemas import CLASSIFICATION_SCHEMA

# The first pages carry the title, letterhead and headings that identify a document's type.
CLASSIFY_PAGES = 3


def classify(llm: LLM, doc: ExtractedDocument) -> Classification:
    head = "\n".join(line.text for line in doc.lines if line.page <= CLASSIFY_PAGES)
    raw = llm.complete_json(
        system=CLASSIFY_SYSTEM,
        content=fence_document(head) + "\n\nClassify this document.",
        schema=CLASSIFICATION_SCHEMA,
        effort="low",  # a short, closed-set decision does not benefit from deep reasoning
        max_tokens=2000,
    )
    raw["confidence"] = min(1.0, max(0.0, float(raw["confidence"])))
    return Classification.model_validate(raw)
