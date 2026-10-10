import json

from idp.eval.metrics import normalize, review_effort_reduction, score
from idp.eval.run_eval import main


def test_normalize():
    assert normalize("$3,208.13") == normalize("3208.13") == "3208.13"
    assert normalize(" Acme  Corp ") == "acme corp"
    assert normalize(None) is None


GOLD = [
    {"document_id": "a", "doc_type": "invoice", "fields": {"total": "100.00", "po": None}},
    {"document_id": "b", "doc_type": "invoice", "fields": {"total": "50.00", "po": "PO-1"}},
    {"document_id": "c", "doc_type": "contract", "fields": {"title": "MSA"}},
]
PREDS = {
    "a": {"doc_type": "invoice", "needs_review": False, "fields": {"total": "$100", "po": None}},
    "b": {"doc_type": "invoice", "needs_review": False, "fields": {"total": "60.00", "po": "PO-1"}},
    # "c" missing: the system failed on it
}


def test_score_counts_escaped_errors():
    r = score(GOLD, PREDS)
    assert r.documents == 3 and r.auto_approved == 2 and r.reviewed == 1
    assert r.auto_approved_with_error == 1 and r.escaped_error_rate == 0.5
    total = r.by_field["invoice.total"]
    assert (total.tp, total.fp, total.fn) == (1, 1, 1)


def test_review_effort_reduction_uses_measured_inputs():
    r = score(GOLD, PREDS)
    # before: 3 docs x 10 min = 30; after: 1 reviewed x 5 + 2 auto x 1 = 7 -> 76.7% less
    assert round(review_effort_reduction(r, 10, 5, 1), 3) == 0.767


def test_cli(tmp_path, capsys):
    g, p = tmp_path / "g.jsonl", tmp_path / "p.jsonl"
    g.write_text("\n".join(json.dumps(x) for x in GOLD))
    p.write_text("\n".join(json.dumps({"document_id": k, **v}) for k, v in PREDS.items()))
    out = main(["--gold", str(g), "--predictions", str(p), "--baseline-minutes", "10", "--review-minutes", "5"])
    assert out["documents"] == 3 and "review_effort_reduction" in out
