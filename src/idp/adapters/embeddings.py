"""Text embeddings with Amazon Titan Text Embeddings V2 on Bedrock."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Protocol

import boto3


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class TitanEmbedder:
    def __init__(self, model_id: str, dims: int, region: str, client: Any | None = None):
        self.model_id = model_id
        self.dims = dims
        self._client = client or boto3.client("bedrock-runtime", region_name=region)

    def embed(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text[:30000], "dimensions": self.dims, "normalize": True})
        resp = self._client.invoke_model(modelId=self.model_id, body=body)
        return json.loads(resp["body"].read())["embedding"]


class HashEmbedder:
    """Bag-of-words hashing embedder for offline tests. Not semantic, but deterministic and cheap."""

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in text.lower().split():
            token = token.strip(".,;:()[]\"'")
            if token:
                h = int(hashlib.sha256(token.encode()).hexdigest(), 16)
                vec[h % self.dims] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
