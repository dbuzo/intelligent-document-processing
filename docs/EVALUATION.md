# Evaluation method

A claim like "we reduced manual review by 70%" only means something if you can say what was measured,
against what baseline, on which documents, and at what error rate. This harness is built to produce those numbers.

## 1. Build a gold set

1. Sample real documents **stratified by type and difficulty**: clean PDFs, phone photos, multi-page
   scans, tables that cross pages, handwritten annotations, non-standard vendors. Aim for at least 50 per
   type to start, and record where they came from and the time period.
2. Two people label the fields independently; a third resolves disagreements. Record inter-annotator
   agreement, because it's the ceiling on achievable accuracy.
3. Store one JSON object per line in `eval/gold/*.jsonl`:

```json
{"document_id": "…", "doc_type": "invoice", "fields": {"invoice_number": "INV-2026-0417", "total": "3208.13", "purchase_order": null}}
```

`null` means "not present in this document", and getting that right counts as correct.

## 2. Produce predictions

Run the documents through the deployed pipeline, then export with `scripts/export_predictions.py` (which
calls `GET /documents/{id}/fields`), or write
`{"document_id", "doc_type", "needs_review", "fields": {name: value}}` lines yourself.

## 3. Score

```bash
idp-eval --gold eval/gold/gold.jsonl --predictions eval/predictions.jsonl \
         --baseline-minutes 12 --review-minutes 4 --spot-minutes 0.5
```

| Metric | Meaning | Why it matters |
|---|---|---|
| Field precision / recall / F1 (per field, per type) | Exact match after normalizing money, case, and whitespace | Shows *where* extraction is weak (for example, `contract.liability_cap`) |
| Classification accuracy | Predicted type = gold type | A wrong type means the wrong schema |
| Review rate | Share of documents sent to a person | The cost side |
| **Escaped error rate** | Of auto-approved documents, the share with at least one wrong field | The risk side. Tune thresholds to keep this under an agreed limit (for example 1%) |
| Review effort reduction | `1 − (reviewed × review_min + auto × spot_min) / (all × baseline_min)` | The business claim, computed from measured times |

## 4. Calibrate thresholds

Sweep `MIN_FIELD_CONFIDENCE` and `MIN_CLASSIFICATION_CONFIDENCE` on a held-out half of the gold set.
Plot review rate against escaped error rate and pick the threshold where the escaped error rate meets the
limit. Report the final numbers on the *other* half.

## 5. How to state a result

> On a pilot of N documents (X invoices, Y contracts, …) from <period>, auto-approval covered A% of
> documents with an escaped error rate of E%. Measured reviewer time fell from B to C minutes per
> document for the rest, an overall reduction of R% in manual review time versus the fully manual
> baseline measured over <period>.

Say whether it was a pilot or production, how baseline times were measured (time study, ticket
timestamps, estimates), and what the quality controls were (QA sampling of auto-approved documents).

## 6. What this repo's offline demo does *not* tell you

The rule-based stand-in model only reads `Label: value` lines, so it scores perfectly on the samples. That
says nothing about Claude's accuracy on real documents. Only report numbers from step 3 run on real
predictions.
