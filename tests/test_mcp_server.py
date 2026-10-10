"""MCP server -> REST API -> pipeline, all in-process.

The MCP server talks HTTP to the API; here an httpx MockTransport turns each HTTP request into an API
Gateway event (with claims looked up from the bearer token, standing in for the Cognito authorizer).
"""

import asyncio
import json

import httpx
from mcp import Client
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError

from idp.api.app import handler as api_handler
from idp.local import LambdaContext, api_event
from idp.mcp_server.api_client import IdpApiClient
from idp.mcp_server.server import build_server

from .conftest import ALICE, LEGAL_ONLY, RAY

TOKENS = {"alice-token": ALICE, "ray-token": RAY, "lee-token": LEGAL_ONLY}


def _transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        body = json.loads(request.content) if request.content else None
        event = api_event(request.method, request.url.path, TOKENS.get(token), body)
        resp = api_handler(event, LambdaContext())
        return httpx.Response(resp["statusCode"], content=resp["body"] or b"",
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handle)


def _server(token: str):
    return build_server(IdpApiClient("https://api.test", transport=_transport()), default_token=token)


def _run(coro):
    return asyncio.run(coro)


def test_lists_primitives_with_annotations(deps):
    async def go():
        async with Client(_server("alice-token")) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            templates = [t.uri_template for t in (await c.list_resource_templates()).resource_templates]
            prompts = [p.name for p in (await c.list_prompts()).prompts]
            return tools, templates, prompts

    tools, templates, prompts = _run(go())
    assert set(tools) == {"search_documents", "ask_documents", "get_document", "get_extracted_fields",
                          "list_review_queue", "submit_review"}
    assert tools["search_documents"].annotations.read_only_hint is True
    assert tools["submit_review"].annotations.destructive_hint is True
    assert "idp://documents/{document_id}" in templates and "idp://schemas/{doc_type}" in templates
    assert set(prompts) == {"contract_risk_review", "invoice_exception_triage"}


def test_search_respects_the_callers_permissions(deps, ingest):
    doc_id = ingest("invoice_acme.txt", groups=["finance"])

    async def search(token):
        async with Client(_server(token)) as c:
            r = await c.call_tool("search_documents", {"query": "INV-2026-0417"})
            return json.loads(r.content[0].text)

    assert _run(search("alice-token"))["hits"][0]["document_id"] == doc_id
    assert _run(search("lee-token"))["hits"] == []


def test_api_errors_become_tool_errors(deps):
    async def go():
        async with Client(_server("alice-token")) as c:
            return await c.call_tool("get_document", {"document_id": "missing"})

    r = _run(go())
    assert r.is_error and "404" in r.content[0].text


def test_submit_review_requires_human_confirmation(deps, ingest):
    doc_id = ingest("invoice_suspicious.txt")

    async def review(answer: str | None):
        async def elicit(context, params):
            assert doc_id in params.message
            if answer == "accept":
                return mcp_types.ElicitResult(action="accept", content={"confirm": True})
            return mcp_types.ElicitResult(action="decline")

        kwargs = {"elicitation_callback": elicit} if answer else {}
        async with Client(_server("ray-token"), **kwargs) as c:
            try:
                return await c.call_tool("submit_review", {"document_id": doc_id, "decision": "approve"})
            except MCPError as e:
                return e

    # A client that cannot ask its user gets a protocol error, so the write can never run unconfirmed.
    no_support = _run(review(None))
    assert isinstance(no_support, MCPError) and "elicitation" in str(no_support)

    declined = _run(review("decline"))
    assert json.loads(declined.content[0].text)["status"] == "unchanged"
    assert deps.repo.get(doc_id).status == "PENDING_REVIEW"

    accepted = _run(review("accept"))
    assert json.loads(accepted.content[0].text)["status"] == "APPROVED"


def test_resources_and_prompts(deps, ingest):
    doc_id = ingest("contract_msa.txt", groups=["legal"])

    async def go():
        async with Client(_server("alice-token")) as c:
            res = await c.read_resource(f"idp://documents/{doc_id}")
            schema = await c.read_resource("idp://schemas/invoice")
            prompt = await c.get_prompt("contract_risk_review", {"document_id": doc_id})
            return res, schema, prompt

    res, schema, prompt = _run(go())
    assert json.loads(res.contents[0].text)["document"]["doc_type"] == "contract"
    assert any(f["name"] == "total" for f in json.loads(schema.contents[0].text))
    assert doc_id in prompt.messages[0].content.text
