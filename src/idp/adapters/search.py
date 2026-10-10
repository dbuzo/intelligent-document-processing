"""Amazon OpenSearch Serverless (vector search collection): chunk indexing and hybrid retrieval.

Retrieval runs two queries, k-NN (semantic) and BM25 (exact terms like invoice numbers), each with
the caller's ACL filter applied inside the query, then merges them with reciprocal rank fusion.
"""

from __future__ import annotations

import copy
import math
from typing import Any, Protocol

from idp.models import Chunk, DocType, Principal, SearchHit
from idp.security.authz import search_filter

INDEX_BODY = {
    "settings": {"index": {"knn": True}},
    "mappings": {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "tenant_id": {"type": "keyword"},
            "allowed_groups": {"type": "keyword"},
            "doc_type": {"type": "keyword"},
            "page": {"type": "integer"},
            "text": {"type": "text"},
            "embedding": {
                "type": "knn_vector",
                "dimension": 1024,
                "method": {"name": "hnsw", "engine": "faiss", "space_type": "innerproduct"},
            },
        }
    },
}


class SearchIndex(Protocol):
    def index_chunks(self, chunks: list[Chunk]) -> None: ...
    def delete_document(self, document_id: str) -> None: ...
    def search(
        self, *, principal: Principal, query: str, vector: list[float], k: int = 5, doc_type: DocType | None = None
    ) -> list[SearchHit]: ...


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


class OpenSearchIndex:
    def __init__(self, endpoint: str, index: str, region: str, dims: int = 1024, client: Any | None = None):
        self.index = index
        self.dims = dims
        if client is None:
            import boto3
            from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

            auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss")
            client = OpenSearch(
                hosts=[{"host": endpoint.replace("https://", ""), "port": 443}],
                http_auth=auth,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection,
                timeout=30,
            )
        self._client = client

    def ensure_index(self) -> None:
        if not self._client.indices.exists(index=self.index):
            body = copy.deepcopy(INDEX_BODY)
            body["mappings"]["properties"]["embedding"]["dimension"] = self.dims
            self._client.indices.create(index=self.index, body=body)

    def index_chunks(self, chunks: list[Chunk]) -> None:
        actions: list[dict] = []
        for c in chunks:
            # AOSS vector collections do not accept custom _id on index; chunk_id is a keyword field.
            actions.append({"index": {"_index": self.index}})
            actions.append(c.model_dump(mode="json"))
        if actions:
            resp = self._client.bulk(body=actions)
            if resp.get("errors"):
                raise RuntimeError("bulk indexing reported errors")

    def delete_document(self, document_id: str) -> None:
        self._client.delete_by_query(index=self.index, body={"query": {"term": {"document_id": document_id}}})

    def search(
        self, *, principal: Principal, query: str, vector: list[float], k: int = 5, doc_type: DocType | None = None
    ) -> list[SearchHit]:
        filters = search_filter(principal)
        if doc_type:
            filters.append({"term": {"doc_type": doc_type.value}})
        source = ["chunk_id", "document_id", "doc_type", "page", "text"]
        knn = self._client.search(
            index=self.index,
            body={
                "size": k * 2,
                "_source": source,
                # Efficient k-NN filtering: the ACL filter is applied during the vector search, so a
                # caller can never receive (or crowd out results with) chunks they cannot see.
                "query": {
                    "knn": {"embedding": {"vector": vector, "k": k * 2, "filter": {"bool": {"filter": filters}}}}
                },
            },
        )
        bm25 = self._client.search(
            index=self.index,
            body={
                "size": k * 2,
                "_source": source,
                "query": {"bool": {"must": [{"match": {"text": query}}], "filter": filters}},
            },
        )
        docs: dict[str, dict] = {}
        rankings = []
        for resp in (knn, bm25):
            ranking = []
            for hit in resp["hits"]["hits"]:
                src = hit["_source"]
                docs[src["chunk_id"]] = src
                ranking.append(src["chunk_id"])
            rankings.append(ranking)
        fused = sorted(rrf(rankings).items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [SearchHit(**docs[cid], score=round(score, 5)) for cid, score in fused]


class InMemoryIndex:
    """Same contract and ACL semantics as OpenSearchIndex, for tests and the offline demo."""

    def __init__(self):
        self.chunks: dict[str, Chunk] = {}

    def index_chunks(self, chunks: list[Chunk]) -> None:
        for c in chunks:
            self.chunks[c.chunk_id] = c

    def delete_document(self, document_id: str) -> None:
        self.chunks = {k: v for k, v in self.chunks.items() if v.document_id != document_id}

    def _visible(self, principal: Principal, c: Chunk) -> bool:
        if c.tenant_id != principal.tenant_id:
            return False
        return "admins" in principal.groups or bool(set(principal.groups) & set(c.allowed_groups))

    def search(
        self, *, principal: Principal, query: str, vector: list[float], k: int = 5, doc_type: DocType | None = None
    ) -> list[SearchHit]:
        candidates = [
            c for c in self.chunks.values() if self._visible(principal, c) and (not doc_type or c.doc_type == doc_type)
        ]

        def cosine(c: Chunk) -> float:
            if not c.embedding:
                return 0.0
            dot = sum(a * b for a, b in zip(vector, c.embedding, strict=False))
            na = math.sqrt(sum(a * a for a in vector)) or 1.0
            nb = math.sqrt(sum(b * b for b in c.embedding)) or 1.0
            return dot / (na * nb)

        terms = {t.lower() for t in query.split()}

        def bm25ish(c: Chunk) -> float:
            words = c.text.lower().split()
            return sum(words.count(t) for t in terms) / (1 + len(words) / 100)

        sem = [c.chunk_id for c in sorted(candidates, key=cosine, reverse=True) if cosine(c) > 0][: k * 2]
        lex = [c.chunk_id for c in sorted(candidates, key=bm25ish, reverse=True) if bm25ish(c) > 0][: k * 2]
        fused = sorted(rrf([sem, lex]).items(), key=lambda kv: kv[1], reverse=True)[:k]
        out = []
        for cid, score in fused:
            c = self.chunks[cid]
            out.append(
                SearchHit(
                    chunk_id=c.chunk_id, document_id=c.document_id, doc_type=c.doc_type, page=c.page, text=c.text,
                    score=round(score, 5),
                )
            )
        return out
