# ADR 0002: Textract for OCR, Claude for interpretation

**Status:** accepted

**Context.** Claude can read PDFs and images directly. Textract adds a service and a step.

**Decision.** Textract does OCR (FORMS + TABLES) and Claude works on Textract's text, tables, and
key-value hints.

**Why.**
- Per-line OCR **confidence** is the input to routing poor scans to people. A vision model doesn't give
  calibrated per-word confidence.
- The **grounding check** needs source text to compare quotes against.
- Table structure (rows and cells) survives; totals checks rely on it.
- Large multi-page scans stay within predictable limits, and the same normalized text feeds the search index.

**Trade-off.** Two services to run and pay for. Visual cues (stamps, signatures, checkboxes outside
forms) can be lost. Mitigation: send the page image to Claude for specific fields where layout matters,
such as a signature present or a stamp.
