from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "idp-test")
os.environ.setdefault("POWERTOOLS_LOG_LEVEL", "WARNING")

from idp.api.app import handler as api_handler  # noqa: E402
from idp.deps import set_deps  # noqa: E402
from idp.local import LambdaContext, api_event, build_local_deps, run_state_machine  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"

ALICE = {"sub": "alice", "custom:tenant_id": "t1", "cognito:groups": ["finance", "legal"]}  # uploader
RAY = {"sub": "ray", "custom:tenant_id": "t1", "cognito:groups": ["finance", "reviewers"]}  # finance reviewer
LEGAL_ONLY = {"sub": "lee", "custom:tenant_id": "t1", "cognito:groups": ["legal"]}
OTHER_TENANT = {"sub": "mallory", "custom:tenant_id": "t2", "cognito:groups": ["finance", "reviewers", "admins"]}


@pytest.fixture
def deps():
    d = build_local_deps()
    set_deps(d)
    yield d
    set_deps(None)


def sample(name: str) -> str:
    return (SAMPLES / name).read_text()


class Api:
    def __call__(self, method: str, path: str, claims: dict | None, body: dict | None = None, query=None):
        resp = api_handler(api_event(method, path, claims, body, query), LambdaContext())
        return resp["statusCode"], json.loads(resp["body"]) if resp.get("body") else None


@pytest.fixture
def api(deps):
    return Api()


@pytest.fixture
def ingest(api, deps):
    """Register via the API as `claims`, then run the pipeline on the sample text."""

    def _ingest(sample_name: str, claims=ALICE, groups=None, ocr_confidence: float = 99.0) -> str:
        status, body = api("POST", "/documents", claims, {
            "filename": sample_name.replace(".txt", ".pdf"), "content_type": "application/pdf",
            "allowed_groups": groups or ["finance"],
        })
        assert status == 201, body
        key = body["upload"]["fields"]["key"]
        run_state_machine("raw-bucket", key, sample(sample_name), ocr_confidence)
        return body["document_id"]

    return _ingest
