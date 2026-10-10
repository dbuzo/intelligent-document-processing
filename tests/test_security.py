import pytest

from idp.models import DocumentRecord, Principal
from idp.security.authz import Forbidden, can_read, principal_from_claims, require_reviewer, search_filter
from idp.security.redaction import redact

DOC = DocumentRecord(document_id="d", tenant_id="t1", owner="alice", allowed_groups=["finance"],
                     filename="f.pdf", content_type="application/pdf", s3_key="k")


def test_group_claim_formats():
    assert principal_from_claims({"sub": "u", "custom:tenant_id": "t", "cognito:groups": "[a b]"}).groups == ["a", "b"]
    assert principal_from_claims({"sub": "u", "custom:tenant_id": "t", "cognito:groups": "a,b"}).groups == ["a", "b"]


def test_missing_tenant_rejected():
    with pytest.raises(Forbidden):
        principal_from_claims({"sub": "u"})


def test_can_read_rules():
    assert can_read(Principal(user_id="alice", tenant_id="t1"), DOC)
    assert can_read(Principal(user_id="bob", tenant_id="t1", groups=["finance"]), DOC)
    assert not can_read(Principal(user_id="bob", tenant_id="t1", groups=["legal"]), DOC)
    assert not can_read(Principal(user_id="alice", tenant_id="t2", groups=["admins"]), DOC)


def test_segregation_of_duties():
    with pytest.raises(Forbidden):
        require_reviewer(Principal(user_id="alice", tenant_id="t1", groups=["reviewers"]), DOC)


def test_search_filter_always_scopes_tenant_and_groups():
    f = search_filter(Principal(user_id="u", tenant_id="t1", groups=[]))
    assert {"term": {"tenant_id": "t1"}} in f
    assert {"terms": {"allowed_groups": ["__none__"]}} in f  # no groups -> matches nothing


def test_redaction():
    out = redact("SSN 123-45-6789 card 4111 1111 1111 1111 mail a.b@example.com")
    assert "123-45-6789" not in out and "4111" not in out and "example.com" not in out
