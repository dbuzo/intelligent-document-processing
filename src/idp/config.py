"""Runtime configuration, read once from environment variables set by the CDK stack."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    # Claude on Amazon Bedrock. Bedrock model IDs carry the "anthropic." prefix.
    model_id: str = field(default_factory=lambda: os.environ.get("MODEL_ID", "anthropic.claude-opus-5-5"))
    embedding_model_id: str = field(
        default_factory=lambda: os.environ.get("EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
    )
    embedding_dims: int = field(default_factory=lambda: int(os.environ.get("EMBEDDING_DIMS", "1024")))

    raw_bucket: str = field(default_factory=lambda: os.environ.get("RAW_BUCKET", "idp-raw"))
    processed_bucket: str = field(default_factory=lambda: os.environ.get("PROCESSED_BUCKET", "idp-processed"))
    table_name: str = field(default_factory=lambda: os.environ.get("TABLE_NAME", "idp-documents"))
    kms_key_arn: str = field(default_factory=lambda: os.environ.get("KMS_KEY_ARN", ""))

    opensearch_endpoint: str = field(default_factory=lambda: os.environ.get("OPENSEARCH_ENDPOINT", ""))
    opensearch_index: str = field(default_factory=lambda: os.environ.get("OPENSEARCH_INDEX", "idp-chunks"))

    review_topic_arn: str = field(default_factory=lambda: os.environ.get("REVIEW_TOPIC_ARN", ""))
    index_function_name: str = field(default_factory=lambda: os.environ.get("INDEX_FUNCTION_NAME", ""))

    # Below these thresholds a document goes to the human review queue instead of auto-approval.
    min_classification_confidence: float = field(
        default_factory=lambda: _float("MIN_CLASSIFICATION_CONFIDENCE", 0.80)
    )
    min_field_confidence: float = field(default_factory=lambda: _float("MIN_FIELD_CONFIDENCE", 0.85))
    min_ocr_confidence: float = field(default_factory=lambda: _float("MIN_OCR_CONFIDENCE", 80.0))

    upload_url_ttl_seconds: int = field(default_factory=lambda: int(os.environ.get("UPLOAD_URL_TTL", "900")))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
