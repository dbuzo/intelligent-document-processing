"""Run the full workflow offline: register -> pipeline -> review queue -> approve -> index -> search -> ask.

Uses in-memory AWS stand-ins and the rule-based model in idp.local, so it needs no AWS account.

    python scripts/local_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path

from idp.api.app import handler
from idp.deps import set_deps
from idp.handlers.pipeline import index
from idp.local import LambdaContext, api_event, build_local_deps, run_state_machine

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
UPLOADER = {"sub": "alice", "custom:tenant_id": "acme", "cognito:groups": ["finance", "legal", "audit"]}
REVIEWER = {"sub": "ray", "custom:tenant_id": "acme", "cognito:groups": ["finance", "reviewers"]}


def call(method, path, claims, body=None):
    resp = handler(api_event(method, path, claims, body), LambdaContext())
    return resp["statusCode"], json.loads(resp["body"])


def show(title, obj):
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, default=str)[:1500])


def main():
    deps = build_local_deps()
    set_deps(deps)
    ids = {}
    for name, groups in [("invoice_acme.txt", ["finance"]), ("invoice_suspicious.txt", ["finance"]),
                         ("contract_msa.txt", ["legal"]), ("audit_evidence_access_review.txt", ["audit"])]:
        _, reg = call("POST", "/documents", UPLOADER,
                      {"filename": name.replace(".txt", ".pdf"), "content_type": "application/pdf",
                       "allowed_groups": groups})
        run_state_machine("raw", reg["upload"]["fields"]["key"], (SAMPLES / name).read_text())
        ids[name] = reg["document_id"]
        _, doc = call("GET", f"/documents/{reg['document_id']}", UPLOADER)
        print(f"{name:38s} -> {doc['status']:15s} {doc['doc_type']:18s} reasons={doc['review_reasons']}")

    _, queue = call("GET", "/reviews", REVIEWER)
    show("Review queue (reviewer view)", [{k: d[k] for k in ("document_id", "filename", "review_reasons")}
                                          for d in queue["documents"]])

    sus = ids["invoice_suspicious.txt"]
    _, fields = call("GET", f"/documents/{sus}/fields", REVIEWER)
    show("Issues on the suspicious invoice", fields["issues"])

    _, decision = call("POST", f"/documents/{sus}/review", REVIEWER,
                       {"decision": "approve", "corrections": {"total": "10800.00", "due_date": "2026-06-13"},
                        "comment": "Confirmed with vendor; total and due date corrected."})
    index({"document_id": sus}, LambdaContext())  # in AWS the API invokes this asynchronously
    show("Review decision", decision)

    _, hits = call("POST", "/search", UPLOADER, {"query": "renewal notice days", "k": 3})
    show("Search: 'renewal notice days'", [{k: h[k] for k in ("document_id", "page", "text")} for h in hits["hits"]])

    _, ans = call("POST", "/ask", UPLOADER, {"query": "What is the total on invoice INV-2026-0417?"})
    show("Ask", {"answer": ans["answer"], "citations": [c["chunk_id"] for c in ans["citations"]]})

    _, none = call("POST", "/search", REVIEWER, {"query": "Master Services Agreement liability"})
    show("Same search as a finance-only reviewer (no legal access)", none["hits"])


if __name__ == "__main__":
    main()
