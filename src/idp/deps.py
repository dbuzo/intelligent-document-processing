"""Dependency container. Lambda handlers build AWS adapters once per container (cold start) and reuse
them across invocations; tests swap in fakes with `set_deps`."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import boto3

from idp.adapters.embeddings import Embedder, TitanEmbedder
from idp.adapters.llm import LLM, BedrockClaude
from idp.adapters.repository import BlobStore, DocumentRepository, DynamoRepository, S3BlobStore
from idp.adapters.search import OpenSearchIndex, SearchIndex
from idp.adapters.textract import TextractClient
from idp.config import Settings, get_settings

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class S3Uploader:
    def __init__(self, bucket: str, ttl: int, client: Any | None = None):
        self.bucket = bucket
        self.ttl = ttl
        self._client = client or boto3.client("s3")

    def presign(self, key: str, content_type: str) -> dict:
        # Presigned POST (not PUT) so S3 itself enforces size and content type. The bucket's default
        # encryption is SSE-KMS, so the client never handles keys.
        return self._client.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_UPLOAD_BYTES]],
            ExpiresIn=self.ttl,
        )


@dataclass
class Deps:
    settings: Settings
    repo: DocumentRepository
    blobs: BlobStore
    llm: LLM
    embedder: Embedder
    index: SearchIndex
    textract: Any
    uploader: Any
    notify_review: Callable[[str, list[str]], None]
    trigger_index: Callable[[str], None]


_deps: Deps | None = None


def _build() -> Deps:
    s = get_settings()
    sns = boto3.client("sns")
    lam = boto3.client("lambda")

    def notify_review(document_id: str, reasons: list[str]) -> None:
        # IDs and reason codes only; document content never leaves the secured stores via SNS.
        if s.review_topic_arn:
            sns.publish(
                TopicArn=s.review_topic_arn,
                Subject="Document needs review",
                Message=json.dumps({"document_id": document_id, "reasons": reasons}),
            )

    def trigger_index(document_id: str) -> None:
        lam.invoke(
            FunctionName=s.index_function_name,
            InvocationType="Event",
            Payload=json.dumps({"document_id": document_id}).encode(),
        )

    return Deps(
        settings=s,
        repo=DynamoRepository(s.table_name),
        blobs=S3BlobStore(s.processed_bucket, s.kms_key_arn),
        llm=BedrockClaude(s.model_id, s.region),
        embedder=TitanEmbedder(s.embedding_model_id, s.embedding_dims, s.region),
        index=OpenSearchIndex(s.opensearch_endpoint, s.opensearch_index, s.region, s.embedding_dims),
        textract=TextractClient(),
        uploader=S3Uploader(s.raw_bucket, s.upload_url_ttl_seconds),
        notify_review=notify_review,
        trigger_index=trigger_index,
    )


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        _deps = _build()
    return _deps


def set_deps(deps: Deps | None) -> None:
    global _deps
    _deps = deps
