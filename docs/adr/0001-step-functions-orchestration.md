# ADR 0001: Orchestrate with Step Functions, one Lambda per step

**Status:** accepted

**Context.** Processing a document means an async OCR job (seconds to minutes), several model calls, rules,
and a branch to human review. A single Lambda would hit the 15-minute limit on large documents, mix
retry policies, and hide where a document got stuck.

**Decision.** Use a Standard Step Functions workflow. Each step is its own Lambda with its own IAM role
and timeout. State carries only IDs and S3 keys. Textract completion is polled with a 10 s Wait loop.

**Consequences.**
+ Per-step retries (throttling backs off; model refusals go to review instead of retrying), a visual
  execution history, one catch-all failure path, least privilege per step.
− Polling costs a state transition every 10 s per document. At high volume, switch to the Textract → SNS →
  `SendTaskSuccess` callback pattern (`.waitForTaskToken`).
− Standard workflows bill per state transition; Express workflows are cheaper but cap at 5 minutes, too
  short for large scans.
