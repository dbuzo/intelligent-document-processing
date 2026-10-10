"""Offline mode: in-memory adapters, a fake Textract and a rule-based stand-in for Claude.

Used by the test suite and `scripts/local_demo.py` so the whole workflow (API -> pipeline -> review ->
index -> search -> MCP) runs without an AWS account. The rule-based "model" only understands the
`Label: value` layout of the files in `samples/`; it exists to exercise the plumbing, not to measure
extraction quality. Real accuracy numbers come from `idp-eval` against Claude on Bedrock.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from idp.adapters.embeddings import HashEmbedder
from idp.adapters.llm import FakeLLM
from idp.adapters.repository import InMemoryBlobStore, InMemoryRepository
from idp.adapters.search import InMemoryIndex
from idp.config import Settings
from idp.deps import Deps
from idp.models import DocType
from idp.schemas import SCHEMAS


def text_to_blocks(text: str, confidence: float = 99.0, lines_per_page: int = 60) -> list[dict]:
    """Build a minimal Textract block graph (PAGE, LINE, WORD) from plain text."""
    blocks: list[dict] = []
    for i, raw in enumerate(ln for ln in text.splitlines() if ln.strip()):
        page = i // lines_per_page + 1
        word_ids = []
        for w in raw.split():
            wid = str(uuid.uuid4())
            word_ids.append(wid)
            blocks.append({"Id": wid, "BlockType": "WORD", "Text": w, "Page": page, "Confidence": confidence})
        blocks.append({"Id": str(uuid.uuid4()), "BlockType": "LINE", "Text": raw.strip(), "Page": page,
                       "Confidence": confidence, "Relationships": [{"Type": "CHILD", "Ids": word_ids}]})
    return blocks


class FakeTextract:
    def __init__(self):
        self.jobs: dict[str, list[dict]] = {}
        self.pending: dict[str, str] = {}  # job_id -> s3 key, resolved by `complete`

    def start_analysis(self, bucket: str, key: str, document_id: str) -> str:
        job_id = f"job-{document_id}"
        self.pending[job_id] = key
        return job_id

    def complete(self, job_id: str, text: str, confidence: float = 99.0) -> None:
        self.jobs[job_id] = text_to_blocks(text, confidence)

    def get_status(self, job_id: str) -> str:
        return "SUCCEEDED" if job_id in self.jobs else "IN_PROGRESS"

    def get_blocks(self, job_id: str) -> list[dict]:
        return self.jobs[job_id]


_TYPE_HINTS = [
    (DocType.INVOICE, r"\binvoice\b"),
    (DocType.CONTRACT, r"\bagreement\b"),
    (DocType.AUDIT_EVIDENCE, r"\bcontrol id\b|\bevidence\b"),
    (DocType.COMPLIANCE_RECORD, r"\bpolicy\b|\battestation\b"),
]


def _doc_text(content: str) -> str:
    m = re.search(r"<document>\n(.*?)\n</document>", content, re.S)
    return m.group(1) if m else content


def _to_iso(value: str) -> str:
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return value


def rule_based_responder(system: str, content: str, schema: dict) -> dict:
    text = _doc_text(content)
    if "classify business documents" in system:
        for doc_type, pattern in _TYPE_HINTS:
            if re.search(pattern, text, re.I):
                return {"doc_type": doc_type.value, "confidence": 0.95, "rationale": f"matched /{pattern}/"}
        return {"doc_type": "other", "confidence": 0.4, "rationale": "no known markers"}

    if "You answer questions" in system:
        excerpts = re.findall(r'<excerpt chunk_id="([^"]+)">\n(.*?)\n</excerpt>', content, re.S)
        question = content.rsplit("Question:", 1)[-1].lower()
        terms = [t for t in re.findall(r"[a-z0-9-]+", question) if len(t) > 3]
        best = [(cid, body) for cid, body in excerpts if any(t in body.lower() for t in terms)]
        if not best:
            return {"answer": "The documents do not say.", "citations": [], "insufficient_context": True}
        return {"answer": best[0][1][:300], "citations": [best[0][0]], "insufficient_context": False}

    doc_type = next(t for t in DocType if f"from a {t.value.replace('_', ' ')}" in system)
    fields = {}
    for spec in SCHEMAS[doc_type]:
        label = spec.name.replace("_", " ")
        m = re.search(rf"^{label}:\s*(.+)$", text, re.I | re.M)
        if not m:
            fields[spec.name] = {"value": None, "evidence": None, "page": None, "confidence": 0.9}
            continue
        raw = m.group(1).strip()
        value = raw
        if spec.kind == "date":
            value = _to_iso(raw)
        elif spec.kind == "money":
            value = re.sub(r"[^0-9.\-]", "", raw)
        fields[spec.name] = {"value": value, "evidence": m.group(0).strip(), "page": 1, "confidence": 0.95}
    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.startswith("---")), "")
    return {"fields": fields, "summary": f"{doc_type.value.replace('_', ' ').title()}: {first.strip()}."}


class Recorder:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)


class FakeUploader:
    def presign(self, key: str, content_type: str) -> dict:
        return {"url": "https://example-bucket.s3.amazonaws.com/", "fields": {"key": key, "Content-Type": content_type}}


def build_local_deps(**overrides) -> Deps:
    deps = Deps(
        settings=Settings(),
        repo=InMemoryRepository(),
        blobs=InMemoryBlobStore(),
        llm=FakeLLM(rule_based_responder),
        embedder=HashEmbedder(),
        index=InMemoryIndex(),
        textract=FakeTextract(),
        uploader=FakeUploader(),
        notify_review=Recorder(),
        trigger_index=Recorder(),
    )
    for k, v in overrides.items():
        setattr(deps, k, v)
    return deps


class LambdaContext:
    function_name = "local"
    function_version = "$LATEST"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:local"
    aws_request_id = "local-request"


def run_state_machine(bucket: str, key: str, text: str, ocr_confidence: float = 99.0) -> dict:
    """Execute the pipeline handlers in the same order and with the same payloads as the Step Functions
    definition in infra/stacks/idp_stack.py (the Textract wait loop collapses to one check)."""
    from idp.deps import get_deps
    from idp.handlers import pipeline as h

    ctx = LambdaContext()
    state: dict = {"bucket": bucket, "key": key}
    try:
        state = h.start_extraction(state, ctx)
        get_deps().textract.complete(state["job_id"], text, ocr_confidence)
        state = h.check_extraction(state, ctx)
        state = h.collect_extraction(state, ctx)
        state = h.classify(state, ctx)
        state = h.extract(state, ctx)
        state = h.validate(state, ctx)
        if state["needs_review"]:
            return h.notify_review(state, ctx)
        return h.index({"document_id": state["document_id"]}, ctx)
    except Exception as e:  # mirrors the state machine's Catch -> MarkFailed
        return h.mark_failed({**state, "error": {"Error": type(e).__name__, "Cause": str(e)}}, ctx)


def api_event(method: str, path: str, claims: dict | None, body: dict | None = None,
              query: dict | None = None) -> dict:
    """An API Gateway REST proxy event as delivered after the Cognito authorizer has verified the JWT."""
    import json

    return {
        "resource": path,
        "path": path,
        "httpMethod": method,
        "headers": {"Content-Type": "application/json"},
        "multiValueHeaders": {},
        "queryStringParameters": query,
        "multiValueQueryStringParameters": None,
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "requestId": "local",
            "stage": "local",
            "path": path,
            "httpMethod": method,
            "resourcePath": path,
            "authorizer": {"claims": claims} if claims else {},
        },
        "body": json.dumps(body) if body is not None else None,
        "isBase64Encoded": False,
    }
