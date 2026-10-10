"""Export pipeline output from the deployed API into the predictions format used by `idp-eval`.

    IDP_API_URL=... IDP_API_TOKEN=... python scripts/export_predictions.py eval/gold/gold.jsonl > eval/predictions.jsonl

The gold file's document_ids must be the IDs the documents were registered under.
"""

from __future__ import annotations

import json
import os
import sys

import httpx


def main(gold_path: str) -> None:
    base = os.environ["IDP_API_URL"].rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['IDP_API_TOKEN']}"}
    with httpx.Client(base_url=base, headers=headers, timeout=30) as http, open(gold_path) as gold:
        for line in gold:
            if not line.strip():
                continue
            doc_id = json.loads(line)["document_id"]
            resp = http.get(f"/documents/{doc_id}/fields")
            if resp.status_code != 200:
                print(f"skip {doc_id}: HTTP {resp.status_code}", file=sys.stderr)
                continue
            body = resp.json()
            # Score what the system produced *before* any human correction.
            fields = {f["name"]: f["original_value"] if f.get("corrected_by") else f["value"] for f in body["fields"]}
            needs_review = bool(body.get("review_reasons"))
            print(json.dumps({"document_id": doc_id, "doc_type": body.get("doc_type"),
                              "needs_review": needs_review, "fields": fields}))


if __name__ == "__main__":
    main(sys.argv[1])
