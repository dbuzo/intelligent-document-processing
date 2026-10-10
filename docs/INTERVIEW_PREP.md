# Interview prep: Intelligent Document Processing

How to present this project and defend every layer: AWS pipeline, Claude in Bedrock, OpenSearch,
REST API, MCP, security, and evaluation. Every answer here points at code in this repo, so you can open
the file if asked.

---

## 0. Ground rules: say only what you can back up

Your prep notes flag two risks: an unverified **70% reduction** and a possible conflict between a
**deployed solution** and a **research prototype**. Keep two things separate:

| | What it is | What you can claim |
|---|---|---|
| **This repo** | A reference implementation you built and tested: 47 automated tests, an offline end-to-end demo, a CDK stack that synthesizes | Design, code, tests, trade-offs, how you'd measure it. **No accuracy or business-impact numbers** until you deploy it and run `idp-eval` on real documents. |
| **Any work project** | Whatever was actually built, with whoever built it | Only verified facts. Fill in the checklist below before the interview. |

**Fill this in from your real experience (don't guess):**

- [ ] Pilot or production? Since when? How many users?
- [ ] Document types and counts tested (for example, 400 invoices, 120 contracts)
- [ ] What counted as "manual review" (keying fields? checking every page? approval?)
- [ ] Baseline: minutes per document before, and how it was measured (time study, ticket timestamps, estimate)
- [ ] After: share of documents auto-approved; minutes per reviewed document; QA sampling of auto-approved ones
- [ ] Accuracy method and result (field-level precision/recall against a human-verified set?)
- [ ] Where documents landed (S3 bucket? SharePoint? an upload portal?) and the order of processing steps
- [ ] Security controls actually in place (KMS? VPC endpoints? who had access?)
- [ ] One real failure: symptom → how you detected it → root cause → fix → what changed afterward
- [ ] Your role versus the team's

If a number isn't verified, say so: *"Our pilot estimate was about 70% less review time; here's how it
was calculated, and here's what I'd measure more rigorously."* That holds up better than a confident number
you can't explain.

---

## 1. The main answer (about 2 minutes)

**Q: Tell me about an AI solution you built to reduce manual work.**

> **Situation.** Teams were manually reviewing contracts, invoices, audit evidence, and compliance
> records: keying in fields, checking totals, finding renewal dates. It was slow, inconsistent between
> reviewers, and didn't scale, and the documents were sensitive, so they had to stay inside a controlled
> AWS environment.
>
> **Task.** Automate extraction, classification, summarization, and search, while keeping people in
> control of anything uncertain and keeping data access as strict as it was before.
>
> **Action.** I split the work into separate steps, each with one job.
> - Documents land in an encrypted S3 bucket through presigned uploads, and an EventBridge event starts a
>   **Step Functions** workflow.
> - **Textract** does OCR with forms and tables and gives a confidence score for every line.
> - **Claude in Bedrock** classifies the document, then extracts a per-type schema. The key detail is that
>   each value has to come with a **verbatim quote** from the document.
> - Then **deterministic code** decides whether to trust it: does the quote actually appear in the OCR text,
>   do subtotal plus tax equal the total, are dates in order, are required fields present, is the scan
>   quality acceptable. Anything uncertain goes to a **human review queue** with the specific reasons.
> - Approved documents are chunked, embedded, and indexed in **OpenSearch** with their access-control
>   fields, so search and Q&A only return what that user is allowed to see.
> - I exposed it through a **REST API** with Cognito, and through an **MCP server** so AI assistants can
>   search and triage using the user's own permissions.
>
> **Result.** *[Use your verified numbers. For this repo:]* "In the reference implementation, the
> sample suspicious invoice (wrong total, due date before the issue date, and an embedded 'ignore previous
> instructions' line) is routed to review with all three reasons, and a clean invoice goes straight
> through. I built an evaluation harness that reports field-level precision and recall, the review rate,
> and the escaped-error rate, which is the number I'd use to back up any effort-reduction claim."
>
> **Lesson.** Document AI works when extraction, retrieval, generation, validation, and review are
> separate but connected, and every step makes its errors visible. A good summary isn't the goal. A
> verifiable field value is.

**30-second version:** "Textract for OCR with confidence, Claude in Bedrock for classification and
extraction with a quote for every value, deterministic grounding and business rules to decide what a person
needs to see, OpenSearch hybrid search with access filters in the query, orchestrated by Step Functions,
exposed through a Cognito-secured REST API and an MCP server that uses the caller's own permissions."

---

## 2. Whiteboard walkthrough (in the order to draw it)

1. **Client → API Gateway (Cognito) → API Lambda → presigned POST → S3 raw (KMS).** *Why presigned POST:*
   files never pass through Lambda (6 MB payload limit), and S3 enforces size and content type.
2. **S3 → EventBridge → Step Functions.** *Why:* async, retryable, and you can see where each document is.
3. **Textract async (FORMS, TABLES) + Wait/poll loop.** *Why async:* multi-page PDFs; the sync API
   handles single pages.
4. **Classify → Extract → Validate** (three Lambdas). Show the `Choice` state: **review** or **index**.
5. **Review path:** SNS notification → reviewer → `POST /review` → async Index.
6. **Index:** chunk (page-aware) → Titan embeddings → OpenSearch with `tenant_id` and `allowed_groups`.
7. **Read path:** `/search` (k-NN + BM25 → RRF) and `/ask` (cited answer).
8. **MCP:** host ↔ client ↔ MCP server → REST API with the user's token.
9. Draw **KMS** around the stores, **one IAM role per Lambda**, and **CloudWatch alarms → SNS**.

---

## 3. The follow-ups from your notes, with stronger answers

**Why combine Textract, Bedrock, and OpenSearch?**
Each does one job that the others do badly. Textract turns pixels into text and gives *per-line
confidence*, which is what lets me route bad scans to people. Claude turns varied layouts into a fixed
schema; templates and regex break on every new vendor. OpenSearch makes the approved content findable,
with vector search for paraphrases and BM25 for exact IDs like an invoice number. I don't let Claude read
the raw PDF for extraction because I need the OCR text to *check its quotes against*. (ADR 0002)

**How would you measure extraction accuracy?**
A human-verified gold set, stratified by document type and difficulty, double-labeled. Exact match after
normalizing money and dates, reported as precision and recall **per field and per type**, because averages
hide that, say, `liability_cap` is weak. Plus the **escaped-error rate**: of the documents the system
auto-approved, how many had a wrong field. That's the number that matters for risk. Then I calibrate the
confidence thresholds on half the set and report on the other half. (`src/idp/eval/metrics.py`,
`docs/EVALUATION.md`)

**How do you protect sensitive documents?**
Authorization is enforced in code on every request and *inside* every search query, never by asking the
model. Tenant isolation plus group ACLs; other tenants' IDs return 404 so they can't be probed. Uploaders
can't approve their own documents. A customer-managed KMS key across S3, DynamoDB, SNS, SQS, Lambda
environment, and OpenSearch. One IAM role per Lambda. No document content in logs or Step Functions history,
only IDs and S3 keys, with redacted error causes. Lifecycle retention, versioning, PITR, and an
append-only record of every review decision. (`src/idp/security/authz.py`, `infra/stacks/idp_stack.py`)

**What if a document contains prompt injection?**
Layers, because no single one is enough:
1. Document text is fenced as untrusted data and can't close its own fence.
2. The extraction model has **no tools that act on anything**, so injected text has nothing to trigger.
   It can only distort an output.
3. Outputs must match a JSON schema, quote real text, and pass business rules, so "mark this approved"
   can't become an approval.
4. Instruction-like text is flagged and sends the document to review.
5. On the MCP side, write actions need explicit human confirmation, and the API re-checks the role.

The sample `invoice_suspicious.txt` is the test case. (`src/idp/prompts.py`, `pipeline/validate.py`)

**What does a 70% reduction mean?**
`1 − (reviewed docs × review minutes + auto-approved docs × spot-check minutes) / (all docs × baseline
minutes)`, from measured times on a defined sample and period, *alongside* the escaped-error rate. It's not
"the AI processed 70% of documents." (`review_effort_reduction` in `eval/metrics.py`)

**What failed?** See §6. Use your own real failure if you have one; otherwise use the real ones from building this repo.

---

## 4. Deep-dive questions by layer

### Textract
- **Sync vs. async?** `AnalyzeDocument` is synchronous and single-page; `StartDocumentAnalysis` is async
  and multi-page, with results read through `GetDocumentAnalysis` pagination (`NextToken`). I use async with
  `ClientRequestToken = document_id` for idempotency.
- **What do FORMS and TABLES give you?** KEY_VALUE_SET blocks linked by `VALUE` relationships, and TABLE → CELL
  blocks with row and column indexes. I walk the block graph in `adapters/textract.py:parse_blocks`, take the
  *minimum* confidence of key and value, and render tables as pipe-delimited rows so the model keeps alignment.
- **Why not Queries or AnalyzeExpense?** Both are good options: AnalyzeExpense for invoices specifically,
  Queries for targeted questions. I'd benchmark them per type on the gold set. The general path keeps one
  pipeline for four document types.

### Claude in Bedrock
- **Which endpoint and client?** The Messages-API Bedrock endpoint through `anthropic.AnthropicBedrockMantle`,
  model ID `anthropic.claude-opus-5-5`, with SigV4 from the Lambda role. The IAM action is
  `bedrock-mantle:CreateInference`.
- **How do you get structured output?** That endpoint doesn't offer structured outputs, and forced
  `tool_choice` is rejected by current models. So the model gets one `submit_result` tool whose input schema
  is the result schema, and I validate the input with `jsonschema` myself. Failures go to review. (ADR 0006)
- **Thinking and effort?** Current Claude models think adaptively; I control depth with `effort`: `low`
  for classification (closed set, first 3 pages) and `medium` for extraction. No `temperature`; it's not
  accepted on current models.
- **Refusals and truncation?** I check `stop_reason`: `refusal` and `max_tokens` raise `LLMError` → review.
  Throttling and 5xx raise `RetryableLLMError`, which Step Functions retries with backoff and jitter.
- **Long documents?** 1M-token context, but I keep a hard character limit and send anything over it to
  manual handling instead of truncating silently.
- **Hallucination?** The model has to quote evidence. `pipeline/extract.py:ground` checks that the quote is in
  the OCR text and contains the value; otherwise the confidence is halved and critical fields go to review.
  There's a test for an invented total.

### Validation and human-in-the-loop
- Rules: required fields, critical-field confidence, ISO dates, parseable amounts, subtotal + tax = total
  (±0.01), due ≥ issue date, contract end > start, audit result in {pass, fail, exception}, failed
  controls flagged, low OCR, injection markers.
- **Why rules and not a second LLM as judge?** Rules are deterministic, explainable to auditors, and cheap.
  A model judge is a reasonable *addition* for fuzzy checks, but it shouldn't be the gate.
- **Reviewer UX?** Reasons are specific (`TOTAL_MISMATCH: subtotal 10000 + tax 800 != total 12800`).
  Corrections keep `original_value` so evaluation still scores the model and not the human fix.

### OpenSearch / RAG
- **Why hybrid?** Vector search misses exact identifiers and BM25 misses paraphrases. RRF merges the
  rankings without normalizing two incompatible score scales.
- **Where is the ACL enforced?** Inside the k-NN query (efficient filtering) and the BM25 filter. With
  post-filtering, you'd retrieve top-k and then drop unauthorized results, which leaks through result counts
  and can return nothing useful.
- **Chunking?** Page-aware, about 220 words with 40 overlap, tables kept whole, plus one "card" chunk per
  document with its approved fields and summary.
- **Only approved documents are indexed**, so unreviewed or suspicious content never reaches search answers.
- **Grounded answers?** `/ask` cites `chunk_id`s, and citations are filtered to chunks actually retrieved
  for that caller. `insufficient_context` is part of the schema, so the model can say it doesn't know.

### Step Functions and Lambda
- Standard workflow (Express caps at 5 min). State holds IDs only (256 KB limit, and history is visible).
- Catch on every task → `MarkFailed` → `FAILED` status + alarm. Retries only for retryable errors.
- One function per step: separate timeouts (Extract 15 min), memory, and IAM. Arm64 for cost.
  Powertools structured logging; X-Ray tracing.
- Next step at scale: Textract → SNS → `.waitForTaskToken` instead of polling; SQS buffering ahead of
  the workflow; reserved concurrency to protect Bedrock quotas.

### REST API, API Gateway, Cognito
- REST API with a Cognito user pool authorizer; the Lambda reads only verified claims. MFA required, no
  self sign-up, immutable `custom:tenant_id`.
- Powertools `APIGatewayRestResolver` routes. Pydantic validates bodies. Stage throttling 50 rps, burst 100.
  JSON access logs.
- **404 vs. 403:** a document you can't read looks the same as one that doesn't exist.
- **Why presigned POST over PUT?** POST policies enforce `content-length-range` and content type at S3.

### MCP (expect questions here)
- **Host vs. client vs. server?** The host is the app the user uses (Claude Desktop, Claude Code, or my
  `chat_host.py`). It owns the model, the conversation, and consent. A client is the protocol connection
  inside the host, one per server. The server exposes capabilities and doesn't know which model is calling.
- **Primitives?** Tools (model-controlled), resources (app-controlled context, here URI templates like
  `idp://documents/{id}`), prompts (user-invoked templates). Client features the server can request:
  elicitation (ask the user), sampling (borrow the host's model), roots (scope).
- **Transports?** stdio for local subprocesses; Streamable HTTP for remote. I run HTTP stateless with JSON
  responses so it scales horizontally.
- **How is MCP different from plain function calling?** Function calling is how one model API invokes
  tools you define in that request. MCP is a *standard protocol between applications and tool providers*:
  write the server once and any MCP host can discover and use it, with lifecycle, capability negotiation,
  resources, prompts, and user-facing features like elicitation.
- **Why not let the MCP server read the database?** Confused deputy: it would need to re-implement
  authorization, and a prompt-injected agent could read across tenants. It forwards the user's token to the
  REST API instead, so there's one authorization path. (ADR 0003)
- **How do you stop an agent from approving things?** Three independent layers: the tool is annotated
  `destructive_hint` and my host asks the user; the server elicits confirmation from the human; the API
  enforces the reviewer role and segregation of duties. A client without elicitation support gets a
  protocol error, so the write can't run unconfirmed.
- **Elicitation implementation detail:** in MCP Python SDK 2.x, I used a resolver-injected parameter.
  On newer protocol versions the question goes back in an `InputRequiredResult` and the call resumes when the
  client retries, which works over stateless HTTP. On older versions it's a mid-call `elicitation/create` request.
- **Risks:** tool poisoning (malicious descriptions), rug pulls (tools change after approval), and token
  passthrough. Mitigations: trusted servers only, pinned versions, audience-bound tokens with token exchange.
- **How did you test it?** A real MCP client connected in-process; the server's HTTP calls go through
  `httpx.MockTransport` into the real API handler. The same search returns results for a finance user and
  nothing for a legal-only user.

### Security and compliance
- Data stays in-account: S3, DynamoDB, OpenSearch Serverless, and Bedrock in your region. Claude in
  Amazon Bedrock runs on AWS-managed infrastructure.
- Production hardening I'd list honestly: VPC endpoints for AOSS, Bedrock, Textract, and S3; scope Bedrock
  IAM to specific model ARNs; WAF on API Gateway; Macie on the raw bucket; Bedrock invocation logging with
  restricted access; Object Lock if records need WORM retention.

### Observability and operations
- Alarms on every Lambda's errors and on failed executions → SNS. DLQ for async invocations and the
  EventBridge target. Structured logs keyed by `document_id`.
- Metrics to add: review rate by type, issue-code frequency, p95 time-to-index, cost per document, model
  refusal rate.

### Cost
- Main drivers: Textract per page (FORMS + TABLES costs more than plain text detection), Claude
  input tokens (long documents), OpenSearch Serverless minimum OCUs.
- Levers: low effort for classification, prompt caching for the repeated system prompt and schema,
  AnalyzeExpense for invoices, page selection, a cheaper vector store at low volume.

### Testing and delivery
- 47 tests: block-graph parser, extraction plus grounding, every validation rule, end-to-end API flows with
  ACLs and review, MCP client↔server, the host loop, DynamoDB and S3 against moto, the Bedrock request shape.
- CI: ruff, pytest, `cdk synth`. The offline mode (`idp.local`) runs the real handlers with fakes behind the
  same interfaces, which is why the whole workflow is testable without AWS.

---

## 5. Trade-offs to bring up yourself

- **Polling Textract** is simple; the callback pattern is better at scale.
- **Token passthrough** to the API is simple; OAuth token exchange is stricter.
- **OpenSearch Serverless** is capable but has a cost floor; pgvector or S3 Vectors for small volumes.
- **Rules are explicit and testable**, but each document type needs its own; that's ongoing work.
- **The regex injection flag** is a tripwire, not a defense. The real defenses are no-action tools,
  schema validation, grounding, and human approval.

---

## 6. "What failed?": real issues from building this repo

Use your own real failure if you have one. If you talk about this repo, these really happened during the build:

1. **The structured-output assumption was wrong for the target endpoint.**
   *Symptom:* the first version of the Claude adapter used `output_config.format` for guaranteed JSON.
   *Detection:* before deploying, I checked the platform's feature-support list for the Bedrock Messages
   endpoint; structured outputs aren't supported there.
   *Fix:* a result tool plus local `jsonschema` validation, with schema failures routed to review (ADR 0006).
   Tests now cover a missing tool call and schema violations.
   *Lesson:* check platform feature parity before designing around a feature, and keep provider specifics
   behind one adapter so the fix touched one file.

2. **A clean invoice went to review.**
   *Symptom:* the end-to-end test expected `INDEXED` but got `PENDING_REVIEW`.
   *Detection:* `review_reasons` said `MISSING_REQUIRED: vendor_name`.
   *Root cause:* the sample had the vendor in the letterhead but no labeled field, and the schema marks vendor
   as required. The validator did its job.
   *Lesson:* this is why reasons must be explicit. The same mechanism that catches bad documents
   tells you when the schema or the test data doesn't match reality.

3. **MCP confirmation broke under the newer SDK's in-process transport.**
   *Symptom:* `NoBackChannelError: Cannot send 'elicitation/create'`.
   *Root cause:* MCP Python SDK 2.x dispatches in-process calls without a server→client channel. Newer
   protocol versions carry the question inside the tool result (`InputRequiredResult`) instead of a mid-call
   request.
   *Fix:* moved the confirmation into a resolver, which works on both old and new protocol versions and over
   stateless HTTP. A client without elicitation now gets a protocol error, so the write can never run
   unconfirmed.

4. **The failure handler itself failed.**
   *Symptom:* an object uploaded without a registered record crashed `MarkFailed`, because it tried to
   update a record that didn't exist.
   *Fix:* handle the missing record, log the key, and still end the workflow in a failed state. A test covers it.
   *Lesson:* test the error paths, not only the happy path.

---

## 7. Numbers and limits to know

| Fact | Value |
|---|---|
| Step Functions state payload | 256 KB, which is why state carries only IDs |
| Lambda max timeout / sync payload | 15 min / 6 MB |
| Express workflow max duration | 5 min |
| Textract async | multi-page PDF/TIFF from S3; sync `AnalyzeDocument` handles one page |
| Bedrock request payload | 20 MB |
| Claude Opus 5.5 context | 1M tokens |
| Titan Text Embeddings V2 | 256 / 512 / 1024 dims (1024 used) |
| This repo's review thresholds | classification 0.80, critical field 0.85, mean OCR 80 (configurable) |
| Upload size limit | 50 MB (S3 POST policy) |

---

## 8. Questions to ask them

- What's your current error tolerance for auto-approved documents, and who owns that decision?
- How do reviewers work today, and where does their time actually go?
- Where do documents come from (email, portals, scanners), and how much do the formats vary?
- What are the data-residency and retention requirements?
- Are AI assistants already connected to internal systems, and how is their access controlled?
