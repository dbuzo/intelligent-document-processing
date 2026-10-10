"""Claude in Amazon Bedrock (the Messages-API endpoint, `bedrock-mantle`) via the Anthropic SDK.

Structured results come back through tool use: the model is given one tool whose input schema is the
result schema and told to call it. The Bedrock Messages endpoint does not offer structured outputs
(`output_config.format`), so the tool input is validated here against the JSON schema; anything that
fails validation is treated as a model error and the document goes to human review.
"""

from __future__ import annotations

from typing import Any, Protocol

import anthropic
import jsonschema
from anthropic import AnthropicBedrockMantle

RESULT_TOOL = "submit_result"


class LLMError(Exception):
    """Non-retryable model outcome (refusal, truncation, invalid result). Routed to human review."""


class RetryableLLMError(Exception):
    """Throttling or transient failure. Raised so Step Functions retries with backoff."""


class LLM(Protocol):
    def complete_json(
        self, *, system: str, content: str, schema: dict, effort: str = "medium", max_tokens: int = 16000
    ) -> dict[str, Any]: ...


class BedrockClaude:
    def __init__(self, model_id: str, region: str, client: Any | None = None):
        self.model_id = model_id
        # Credentials come from the Lambda execution role (SigV4); no API keys in the function.
        self._client = client or AnthropicBedrockMantle(aws_region=region, max_retries=2)

    def complete_json(
        self, *, system: str, content: str, schema: dict, effort: str = "medium", max_tokens: int = 16000
    ) -> dict[str, Any]:
        tool = {
            "name": RESULT_TOOL,
            "description": "Return the final result. Call this exactly once with the complete result.",
            "input_schema": schema,
        }
        try:
            response = self._client.messages.create(
                model=self.model_id,
                max_tokens=max_tokens,
                system=f"{system}\n\nReturn your result by calling the {RESULT_TOOL} tool exactly once.",
                messages=[{"role": "user", "content": content}],
                tools=[tool],
                # Forced tool_choice is rejected by current models; "auto" plus the instruction above.
                tool_choice={"type": "auto"},
                output_config={"effort": effort},
            )
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
            raise RetryableLLMError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                raise RetryableLLMError(str(e)) from e
            raise

        if response.stop_reason == "refusal":
            raise LLMError("model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError("response truncated at max_tokens")

        block = next((b for b in response.content if b.type == "tool_use" and b.name == RESULT_TOOL), None)
        if block is None:
            raise LLMError("model did not return a structured result")
        try:
            jsonschema.validate(block.input, schema)
        except jsonschema.ValidationError as e:
            raise LLMError(f"result failed schema validation: {e.message}") from e
        return dict(block.input)


class FakeLLM:
    """Deterministic stand-in for tests and the offline demo.

    `responder(system, content, schema)` returns the dict the model would have produced; it is
    schema-validated exactly like a real response.
    """

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, content: str, schema: dict, effort: str = "medium", max_tokens: int = 16000
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "content": content, "effort": effort})
        result = self._responder(system, content, schema)
        jsonschema.validate(result, schema)
        return result
