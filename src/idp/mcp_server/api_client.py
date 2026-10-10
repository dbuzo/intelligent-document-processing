"""HTTP client for the IDP REST API, used by the MCP server.

The MCP server holds no AWS credentials and no data access of its own: it forwards the end user's
bearer token, so API Gateway's Cognito authorizer and the API's ACL checks apply to AI agents exactly
as they apply to people.
"""

from __future__ import annotations

from typing import Any

import httpx


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class IdpApiClient:
    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None, timeout: float = 60.0):
        self._http = httpx.Client(base_url=base_url.rstrip("/"), transport=transport, timeout=timeout)

    def _call(self, method: str, path: str, token: str, **kwargs: Any) -> dict:
        try:
            resp = self._http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.HTTPError as e:
            raise ApiError(503, f"IDP API unreachable ({type(e).__name__})") from e
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise ApiError(resp.status_code, message)
        return resp.json()

    def search(self, token: str, query: str, k: int = 5, doc_type: str | None = None) -> dict:
        return self._call("POST", "/search", token, json={"query": query, "k": k, "doc_type": doc_type})

    def ask(self, token: str, question: str, doc_type: str | None = None) -> dict:
        return self._call("POST", "/ask", token, json={"query": question, "doc_type": doc_type})

    def get_document(self, token: str, document_id: str) -> dict:
        return self._call("GET", f"/documents/{document_id}", token)

    def get_fields(self, token: str, document_id: str) -> dict:
        return self._call("GET", f"/documents/{document_id}/fields", token)

    def review_queue(self, token: str) -> dict:
        return self._call("GET", "/reviews", token)

    def submit_review(
        self, token: str, document_id: str, decision: str, corrections: dict[str, str | None], comment: str
    ) -> dict:
        return self._call(
            "POST", f"/documents/{document_id}/review", token,
            json={"decision": decision, "corrections": corrections, "comment": comment},
        )
