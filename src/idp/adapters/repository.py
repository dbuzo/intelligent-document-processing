"""Document metadata in DynamoDB and large artifacts (Textract output, extraction results) in S3."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Protocol

import boto3
from boto3.dynamodb.conditions import Attr, Key

from idp.models import DocumentRecord, Status, utcnow


class DocumentRepository(Protocol):
    def put(self, doc: DocumentRecord) -> None: ...
    def get(self, document_id: str) -> DocumentRecord | None: ...
    def update(self, document_id: str, **changes: Any) -> DocumentRecord: ...
    def list_by_tenant(self, tenant_id: str, status: Status | None = None, limit: int = 50) -> list[DocumentRecord]: ...


class DynamoRepository:
    """Table key: document_id. GSI `tenant-status`: tenant_id + status, for queues and listings."""

    def __init__(self, table_name: str, resource: Any | None = None):
        self._table = (resource or boto3.resource("dynamodb")).Table(table_name)

    def put(self, doc: DocumentRecord) -> None:
        self._table.put_item(
            Item=_to_item(doc), ConditionExpression="attribute_not_exists(document_id)"
        )

    def get(self, document_id: str) -> DocumentRecord | None:
        item = self._table.get_item(Key={"document_id": document_id}, ConsistentRead=True).get("Item")
        return _from_item(item) if item else None

    def update(self, document_id: str, **changes: Any) -> DocumentRecord:
        changes["updated_at"] = utcnow()
        names = {f"#{k}": k for k in changes}
        values = {f":{k}": _dynamo_value(v) for k, v in changes.items()}
        expr = "SET " + ", ".join(f"#{k} = :{k}" for k in changes)
        resp = self._table.update_item(
            Key={"document_id": document_id},
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=Attr("document_id").exists(),
            ReturnValues="ALL_NEW",
        )
        return _from_item(resp["Attributes"])

    def list_by_tenant(self, tenant_id: str, status: Status | None = None, limit: int = 50) -> list[DocumentRecord]:
        cond = Key("tenant_id").eq(tenant_id)
        if status:
            cond = cond & Key("status").eq(status.value)
        resp = self._table.query(IndexName="tenant-status", KeyConditionExpression=cond, Limit=limit)
        return [_from_item(i) for i in resp.get("Items", [])]


def _dynamo_value(v: Any) -> Any:
    if hasattr(v, "value"):  # enums
        return v.value
    if isinstance(v, float):
        return Decimal(str(v))
    return v


def _to_item(doc: DocumentRecord) -> dict:
    # DynamoDB rejects Python floats; round-trip through JSON so nested floats become Decimals.
    return json.loads(doc.model_dump_json(exclude_none=True), parse_float=Decimal)


def _from_item(item: dict) -> DocumentRecord:
    return DocumentRecord.model_validate(json.loads(json.dumps(item, default=float)))


class InMemoryRepository:
    def __init__(self):
        self.items: dict[str, DocumentRecord] = {}

    def put(self, doc: DocumentRecord) -> None:
        if doc.document_id in self.items:
            raise ValueError("duplicate document_id")
        self.items[doc.document_id] = doc

    def get(self, document_id: str) -> DocumentRecord | None:
        doc = self.items.get(document_id)
        return doc.model_copy(deep=True) if doc else None

    def update(self, document_id: str, **changes: Any) -> DocumentRecord:
        doc = self.items[document_id]
        updated = doc.model_copy(update={**changes, "updated_at": utcnow()})
        self.items[document_id] = DocumentRecord.model_validate(updated.model_dump())
        return self.items[document_id]

    def list_by_tenant(self, tenant_id: str, status: Status | None = None, limit: int = 50) -> list[DocumentRecord]:
        docs = [d for d in self.items.values() if d.tenant_id == tenant_id and (not status or d.status == status)]
        return docs[:limit]


class BlobStore(Protocol):
    def put_json(self, key: str, data: dict) -> None: ...
    def get_json(self, key: str) -> dict: ...


class S3BlobStore:
    def __init__(self, bucket: str, kms_key_arn: str = "", client: Any | None = None):
        self.bucket = bucket
        self.kms_key_arn = kms_key_arn
        self._client = client or boto3.client("s3")

    def put_json(self, key: str, data: dict) -> None:
        extra: dict[str, str] = {}
        if self.kms_key_arn:
            extra = {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self.kms_key_arn}
        self._client.put_object(
            Bucket=self.bucket, Key=key, Body=json.dumps(data).encode(), ContentType="application/json", **extra
        )

    def get_json(self, key: str) -> dict:
        return json.loads(self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read())


class InMemoryBlobStore:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def put_json(self, key: str, data: dict) -> None:
        self.objects[key] = json.loads(json.dumps(data))

    def get_json(self, key: str) -> dict:
        return self.objects[key]
