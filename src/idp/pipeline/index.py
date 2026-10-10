"""Chunk approved documents, embed them, and write them to the search index with their ACLs."""

from __future__ import annotations

from idp.adapters.embeddings import Embedder
from idp.adapters.search import SearchIndex
from idp.adapters.textract import table_to_text
from idp.models import Chunk, DocumentRecord, ExtractedDocument, ExtractionResult


def chunk_document(doc: ExtractedDocument, max_words: int = 220, overlap: int = 40) -> list[tuple[int, str]]:
    """Page-aware sliding window. Chunks never span pages, so every hit can cite a page number.
    Tables are kept whole as their own chunk so rows are not split from their headers."""
    out: list[tuple[int, str]] = []
    pages: dict[int, list[str]] = {}
    for line in doc.lines:
        pages.setdefault(line.page, []).append(line.text)
    for page, lines in sorted(pages.items()):
        words = " ".join(lines).split()
        step = max(1, max_words - overlap)
        for start in range(0, len(words), step):
            out.append((page, " ".join(words[start : start + max_words])))
            if start + max_words >= len(words):
                break
    for t in doc.tables:
        out.append((t.page, table_to_text(t)))
    return out


def index_document(
    record: DocumentRecord,
    doc: ExtractedDocument,
    result: ExtractionResult,
    embedder: Embedder,
    index: SearchIndex,
) -> int:
    assert record.doc_type is not None
    pieces = chunk_document(doc)
    # The summary and the approved fields become a searchable "card" so structured questions
    # ("which invoices from Acme are over 10k") hit precise values, not just OCR text.
    card = [f"{record.filename} ({record.doc_type.value})"]
    if result.summary:
        card.append(result.summary)
    card.extend(f"{f.name}: {f.value}" for f in result.fields if f.value)
    pieces.insert(0, (1, "\n".join(card)))

    chunks = [
        Chunk(
            chunk_id=f"{record.document_id}#{i}",
            document_id=record.document_id,
            tenant_id=record.tenant_id,
            allowed_groups=record.allowed_groups,
            doc_type=record.doc_type,
            page=page,
            text=text,
            embedding=embedder.embed(text),
        )
        for i, (page, text) in enumerate(pieces)
    ]
    # Re-indexing (after a reviewer correction) replaces, never duplicates.
    index.delete_document(record.document_id)
    index.index_chunks(chunks)
    return len(chunks)
