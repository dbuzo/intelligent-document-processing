# REST API

Base URL: `https://<api-id>.execute-api.<region>.amazonaws.com/v1`. Every request needs
`Authorization: Bearer <Cognito ID token>`. API Gateway's Cognito authorizer verifies the token; the
Lambda reads `sub`, `custom:tenant_id`, and `cognito:groups` from the verified claims.

Errors: `400` invalid input · `403` missing role or a forbidden action · `404` not found **or not
permitted** (same response on purpose) · `429` throttled (stage limit: 50 rps, burst 100).

## Register and upload

```http
POST /documents
{"filename": "acme-invoice.pdf", "content_type": "application/pdf", "allowed_groups": ["finance"]}
```

```json
201
{"document_id": "6f1c…", "expires_in": 900,
 "upload": {"url": "https://<raw-bucket>.s3.amazonaws.com/",
            "fields": {"key": "uploads/acme/6f1c…/acme-invoice.pdf", "Content-Type": "application/pdf",
                       "policy": "…", "x-amz-signature": "…"}}}
```

Upload with a multipart form POST to `upload.url`, sending every field in `upload.fields` plus `file`.
S3 enforces the content type and a 1 byte–50 MB size range. `allowed_groups` must be a subset of the
caller's groups and defaults to all of them.

```bash
curl -X POST "$URL" $(jq -r '.upload.fields|to_entries[]|"-F \(.key)=\(.value)"' reg.json) -F file=@acme-invoice.pdf
```

## Read

`GET /documents?status=PENDING_REVIEW` lists readable documents in your tenant.

`GET /documents/{id}`

```json
{"document_id":"6f1c…","tenant_id":"acme","owner":"alice","allowed_groups":["finance"],
 "filename":"acme-invoice.pdf","status":"INDEXED","doc_type":"invoice","classification_confidence":0.97,
 "summary":"Invoice INV-2026-0417 from Acme Industrial Supply for $3,208.13, due 2026-04-02.",
 "review_reasons":[],"reviewed_by":null,"created_at":"…","updated_at":"…"}
```

`GET /documents/{id}/fields`

```json
{"document_id":"6f1c…","status":"INDEXED","doc_type":"invoice","summary":"…",
 "fields":[{"name":"total","value":"3208.13","evidence":"Total: $3,208.13","page":1,
            "model_confidence":0.95,"ocr_confidence":99.1,"grounded":true,"corrected_by":null,
            "confidence":0.941}],
 "issues":[], "review_reasons":[]}
```

## Search and ask

```http
POST /search   {"query": "auto-renewal 90 days notice", "k": 5, "doc_type": "contract"}
POST /ask      {"query": "When can we terminate the Contoso MSA without renewing?"}
```

`/ask` returns `{"answer", "citations":[{chunk_id, document_id, page, text, score}], "insufficient_context"}`.

## Review

`GET /reviews` (reviewers only) lists `PENDING_REVIEW` documents with their reasons.

```http
POST /documents/{id}/review
{"decision": "approve", "corrections": {"total": "10800.00"}, "comment": "Confirmed with vendor"}
```

Rules: caller is in `reviewers` or `admins`; caller isn't the uploader (unless admin); the document is
`PENDING_REVIEW`; correction keys are existing field names. Approval triggers indexing asynchronously.
Every decision is saved as `reviews/{id}/{timestamp}.json`.
