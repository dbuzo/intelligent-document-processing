"""AWS adapters against moto (DynamoDB, S3) and recorded request shapes (Bedrock)."""

from types import SimpleNamespace as NS

import boto3
import pytest
from moto import mock_aws

from idp.adapters.llm import BedrockClaude, LLMError
from idp.adapters.repository import DynamoRepository, S3BlobStore
from idp.models import DocumentRecord, Status


@pytest.fixture
def aws():
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="docs",
            KeySchema=[{"AttributeName": "document_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("document_id", "tenant_id", "status")],
            GlobalSecondaryIndexes=[{
                "IndexName": "tenant-status",
                "KeySchema": [{"AttributeName": "tenant_id", "KeyType": "HASH"},
                              {"AttributeName": "status", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="processed")
        yield ddb, s3


def test_dynamo_repository_round_trip(aws):
    ddb, _ = aws
    repo = DynamoRepository("docs", resource=ddb)
    repo.put(DocumentRecord(document_id="d1", tenant_id="t1", owner="u", allowed_groups=["g"],
                            filename="a.pdf", content_type="application/pdf", s3_key="k"))
    repo.update("d1", status=Status.PENDING_REVIEW, classification_confidence=0.91, review_reasons=["x"])
    doc = repo.get("d1")
    assert doc.status == Status.PENDING_REVIEW and doc.classification_confidence == 0.91
    assert [d.document_id for d in repo.list_by_tenant("t1", Status.PENDING_REVIEW)] == ["d1"]
    assert repo.list_by_tenant("t2") == []


def test_s3_blob_store(aws):
    _, s3 = aws
    store = S3BlobStore("processed", client=s3)
    store.put_json("results/d1.json", {"a": 1})
    assert store.get_json("results/d1.json") == {"a": 1}


class _Messages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def test_bedrock_claude_request_shape():
    thinking = NS(type="thinking", thinking="")
    call = NS(type="tool_use", id="t1", name="submit_result", input={"ok": True})
    client = NS(messages=_Messages(NS(stop_reason="tool_use", content=[thinking, call])))
    llm = BedrockClaude("anthropic.claude-opus-5-5", "us-east-1", client=client)
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    assert llm.complete_json(system="s", content="c", schema=schema, effort="low") == {"ok": True}
    sent = client.messages.kwargs
    assert sent["model"] == "anthropic.claude-opus-5-5"
    assert sent["tools"][0]["input_schema"] == schema and sent["tool_choice"] == {"type": "auto"}
    assert sent["output_config"] == {"effort": "low"}
    assert "thinking" not in sent and "temperature" not in sent


def test_bedrock_claude_rejects_schema_violations():
    bad = NS(type="tool_use", id="t1", name="submit_result", input={"ok": "yes"})
    client = NS(messages=_Messages(NS(stop_reason="tool_use", content=[bad])))
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    with pytest.raises(LLMError, match="schema"):
        BedrockClaude("m", "us-east-1", client=client).complete_json(system="s", content="c", schema=schema)


def test_bedrock_claude_requires_the_result_tool():
    client = NS(messages=_Messages(NS(stop_reason="end_turn", content=[NS(type="text", text="{}")])))
    with pytest.raises(LLMError, match="structured"):
        BedrockClaude("m", "us-east-1", client=client).complete_json(system="s", content="c", schema={})


@pytest.mark.parametrize("reason", ["refusal", "max_tokens"])
def test_bedrock_claude_unusable_stop_reasons(reason):
    client = NS(messages=_Messages(NS(stop_reason=reason, content=[])))
    with pytest.raises(LLMError):
        BedrockClaude("m", "us-east-1", client=client).complete_json(system="s", content="c", schema={})
