"""Upload -> pipeline -> review -> index -> search, through the REST API with Cognito-style claims."""

from idp.handlers.pipeline import index
from idp.local import LambdaContext
from idp.models import Status

from .conftest import ALICE, LEGAL_ONLY, OTHER_TENANT, RAY


def test_requires_authentication(api):
    status, _ = api("GET", "/documents", None)
    assert status == 403


def test_rejects_unsupported_content_type(api):
    status, _ = api("POST", "/documents", ALICE, {"filename": "x.exe", "content_type": "application/x-msdownload"})
    assert status == 400


def test_cannot_share_with_groups_you_are_not_in(api):
    status, _ = api("POST", "/documents", ALICE, {"filename": "a.pdf", "content_type": "application/pdf",
                                                  "allowed_groups": ["executives"]})
    assert status == 403


def test_clean_invoice_flows_to_index_and_is_searchable(api, ingest, deps):
    doc_id = ingest("invoice_acme.txt")
    status, doc = api("GET", f"/documents/{doc_id}", ALICE)
    assert status == 200 and doc["status"] == Status.INDEXED
    assert "s3_key" not in doc  # storage locations are not exposed

    status, fields = api("GET", f"/documents/{doc_id}/fields", ALICE)
    assert {f["name"]: f["value"] for f in fields["fields"]}["total"] == "3208.13"

    status, res = api("POST", "/search", ALICE, {"query": "INV-2026-0417 nitrile gloves"})
    assert status == 200 and res["hits"][0]["document_id"] == doc_id

    status, ans = api("POST", "/ask", ALICE, {"query": "What is the total on invoice INV-2026-0417?"})
    assert status == 200 and ans["citations"] and not ans["insufficient_context"]


def test_access_control_on_read_and_search(api, ingest):
    doc_id = ingest("invoice_acme.txt", groups=["finance"])
    # same tenant, different group: looks exactly like "not found"
    assert api("GET", f"/documents/{doc_id}", LEGAL_ONLY)[0] == 404
    assert api("POST", "/search", LEGAL_ONLY, {"query": "INV-2026-0417"})[1]["hits"] == []
    # another tenant, even an admin there, sees nothing
    assert api("GET", f"/documents/{doc_id}", OTHER_TENANT)[0] == 404
    assert api("POST", "/search", OTHER_TENANT, {"query": "INV-2026-0417"})[1]["hits"] == []


def test_review_workflow_with_correction(api, ingest, deps):
    doc_id = ingest("invoice_suspicious.txt")
    assert deps.notify_review.calls and deps.notify_review.calls[0][0] == doc_id

    status, doc = api("GET", f"/documents/{doc_id}", ALICE)
    assert doc["status"] == Status.PENDING_REVIEW
    assert any("TOTAL_MISMATCH" in r for r in doc["review_reasons"])
    # not indexed while pending: nobody can retrieve unreviewed content
    assert api("POST", "/search", ALICE, {"query": "GX-559"})[1]["hits"] == []

    # uploader is not a reviewer
    assert api("GET", "/reviews", ALICE)[0] == 403
    queue = api("GET", "/reviews", RAY)[1]["documents"]
    assert [d["document_id"] for d in queue] == [doc_id]

    status, body = api("POST", f"/documents/{doc_id}/review", RAY,
                       {"decision": "approve", "corrections": {"total": "10800.00"}, "comment": "typo on total"})
    assert status == 200 and body["status"] == Status.APPROVED
    assert deps.trigger_index.calls == [(doc_id,)]

    fields = {f["name"]: f for f in api("GET", f"/documents/{doc_id}/fields", RAY)[1]["fields"]}
    assert fields["total"]["value"] == "10800.00" and fields["total"]["corrected_by"] == "ray"
    assert fields["total"]["original_value"] == "12800.00"  # kept so evaluation scores the model, not the fix

    # the async index invocation the API triggered
    index({"document_id": doc_id}, LambdaContext())
    assert api("GET", f"/documents/{doc_id}", RAY)[1]["status"] == Status.INDEXED
    assert any(k.startswith(f"reviews/{doc_id}/") for k in deps.blobs.objects)  # audit record


def test_reviewer_cannot_approve_own_upload(api, ingest):
    doc_id = ingest("invoice_suspicious.txt", claims=RAY)
    status, _ = api("POST", f"/documents/{doc_id}/review", RAY, {"decision": "approve"})
    assert status == 403


def test_unknown_correction_field_rejected(api, ingest):
    doc_id = ingest("invoice_suspicious.txt")
    status, _ = api("POST", f"/documents/{doc_id}/review", RAY, {"decision": "approve", "corrections": {"x": "1"}})
    assert status == 400


def test_upload_without_registration_fails_pipeline(deps):
    from idp.local import run_state_machine

    out = run_state_machine("raw", "uploads/t1/not-registered/file.pdf", "Invoice")
    assert out["status"] == "FAILED"
