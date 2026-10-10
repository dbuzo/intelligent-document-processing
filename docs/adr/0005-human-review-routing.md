# ADR 0005: Route to human review with explicit reasons

**Status:** accepted

**Decision.** A document is auto-approved only when the classification is confident, OCR quality is
acceptable, every required field is present, every critical field is grounded and above the confidence
threshold, and all cross-field rules pass. Otherwise it goes to review, listing each reason. Reviewers can't
approve their own uploads. Corrections keep the model's original value for evaluation.

**Why.** The business risk sits in the documents the system *doesn't* send to a person. Thresholds are
calibrated against the escaped-error rate on a gold set (docs/EVALUATION.md), not set by intuition.
