"""Retrieval-augmented answers over documents the caller is allowed to see."""

from __future__ import annotations

from pydantic import BaseModel

from idp.adapters.embeddings import Embedder
from idp.adapters.llm import LLM
from idp.adapters.search import SearchIndex
from idp.models import DocType, Principal, SearchHit
from idp.prompts import ANSWER_SYSTEM, fence_excerpts
from idp.schemas import ANSWER_SCHEMA


class Answer(BaseModel):
    answer: str
    citations: list[SearchHit]
    insufficient_context: bool


def search(
    principal: Principal, query: str, embedder: Embedder, index: SearchIndex, k: int = 5,
    doc_type: DocType | None = None,
) -> list[SearchHit]:
    return index.search(principal=principal, query=query, vector=embedder.embed(query), k=k, doc_type=doc_type)


def answer(
    principal: Principal, question: str, embedder: Embedder, index: SearchIndex, llm: LLM, k: int = 6,
    doc_type: DocType | None = None,
) -> Answer:
    hits = search(principal, question, embedder, index, k=k, doc_type=doc_type)
    if not hits:
        return Answer(answer="No accessible documents match this question.", citations=[], insufficient_context=True)
    raw = llm.complete_json(
        system=ANSWER_SYSTEM,
        content=fence_excerpts([(h.chunk_id, h.text) for h in hits]) + f"\n\nQuestion: {question}",
        schema=ANSWER_SCHEMA,
        effort="medium",
    )
    by_id = {h.chunk_id: h for h in hits}
    # Only keep citations that point at chunks we actually retrieved for this caller.
    cited = [by_id[c] for c in raw["citations"] if c in by_id]
    return Answer(answer=raw["answer"], citations=cited, insufficient_context=raw["insufficient_context"])
