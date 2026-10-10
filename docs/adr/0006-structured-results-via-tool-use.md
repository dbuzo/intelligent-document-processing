# ADR 0006: Structured model output through a result tool and local schema validation

**Status:** accepted

**Context.** Extraction needs machine-readable output. The Messages-API Bedrock endpoint (`bedrock-mantle`)
doesn't support structured outputs (`output_config.format`). Forced `tool_choice` (`any`/`tool`) is
rejected by current Claude models.

**Decision.** Give Claude one tool, `submit_result`, whose input schema is the result schema. Use
`tool_choice: auto` with an instruction to call it once, then validate the tool input with `jsonschema`.
A missing call, a schema violation, a refusal, or truncation raises `LLMError`, and the document goes to
human review.

**Consequences.** Works on Bedrock today and the guarantee is enforced in code. If the platform adds
structured outputs, swapping is a change confined to `adapters/llm.py`.
