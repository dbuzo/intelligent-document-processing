"""Evaluation against a human-verified gold set.

The numbers that matter for a "reduced manual review" claim:
  - field precision / recall per document type (is extraction right?)
  - review rate (what share of documents still go to a person?)
  - escaped error rate (of documents auto-approved without review, how many had a wrong field?)
A low review rate is worthless if the escaped error rate is high.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field


def normalize(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip().lower()
    s = re.sub(r"[$€£,]", "", s)
    s = re.sub(r"\s+", " ", s)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        s = f"{float(s):.2f}"
    return s or None


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0


@dataclass
class EvalReport:
    documents: int = 0
    classification_correct: int = 0
    reviewed: int = 0
    auto_approved: int = 0
    auto_approved_with_error: int = 0
    by_field: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))
    by_type: dict[str, Counts] = field(default_factory=lambda: defaultdict(Counts))

    @property
    def classification_accuracy(self) -> float:
        return self.classification_correct / self.documents if self.documents else 0.0

    @property
    def review_rate(self) -> float:
        return self.reviewed / self.documents if self.documents else 0.0

    @property
    def escaped_error_rate(self) -> float:
        return self.auto_approved_with_error / self.auto_approved if self.auto_approved else 0.0

    def to_dict(self) -> dict:
        return {
            "documents": self.documents,
            "classification_accuracy": round(self.classification_accuracy, 4),
            "review_rate": round(self.review_rate, 4),
            "auto_approved": self.auto_approved,
            "escaped_error_rate": round(self.escaped_error_rate, 4),
            "by_type": {k: _counts(v) for k, v in sorted(self.by_type.items())},
            "by_field": {k: _counts(v) for k, v in sorted(self.by_field.items())},
        }


def _counts(c: Counts) -> dict:
    return {"tp": c.tp, "fp": c.fp, "fn": c.fn, "precision": round(c.precision, 4),
            "recall": round(c.recall, 4), "f1": round(c.f1, 4)}


def score(gold: list[dict], predictions: dict[str, dict]) -> EvalReport:
    """gold: [{document_id, doc_type, fields: {name: value|null}}]
    predictions: {document_id: {doc_type, needs_review, fields: {name: value|null}}}"""
    report = EvalReport()
    for g in gold:
        report.documents += 1
        p = predictions.get(g["document_id"])
        if p is None:
            report.reviewed += 1  # a document the system failed on is manual work
            for name, value in g["fields"].items():
                if value is not None:
                    report.by_field[f"{g['doc_type']}.{name}"].fn += 1
                    report.by_type[g["doc_type"]].fn += 1
            continue
        if p["doc_type"] == g["doc_type"]:
            report.classification_correct += 1
        any_wrong = False
        for name, gold_value in g["fields"].items():
            gv, pv = normalize(gold_value), normalize(p["fields"].get(name))
            key = f"{g['doc_type']}.{name}"
            if pv is not None and pv == gv:
                report.by_field[key].tp += 1
                report.by_type[g["doc_type"]].tp += 1
            else:
                if pv is not None:
                    report.by_field[key].fp += 1
                    report.by_type[g["doc_type"]].fp += 1
                if gv is not None:
                    report.by_field[key].fn += 1
                    report.by_type[g["doc_type"]].fn += 1
                any_wrong = any_wrong or pv != gv
        if p.get("needs_review"):
            report.reviewed += 1
        else:
            report.auto_approved += 1
            report.auto_approved_with_error += int(any_wrong or p["doc_type"] != g["doc_type"])
    return report


def review_effort_reduction(
    report: EvalReport, baseline_minutes_per_doc: float, review_minutes_per_doc: float,
    spot_check_minutes_per_doc: float = 0.0,
) -> float:
    """Share of manual review time saved versus fully manual processing, from measured inputs.

    baseline: minutes a person spent per document before the system (time-study or ticket data)
    review:   minutes per document routed to the review queue (measured in the pilot)
    spot:     minutes per auto-approved document for QA sampling
    """
    n = report.documents
    if n == 0 or baseline_minutes_per_doc <= 0:
        return 0.0
    before = n * baseline_minutes_per_doc
    after = report.reviewed * review_minutes_per_doc + report.auto_approved * spot_check_minutes_per_doc
    return 1 - after / before
