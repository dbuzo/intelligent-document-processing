"""Amazon Textract: async document analysis (FORMS + TABLES) and block-graph normalization."""

from __future__ import annotations

from typing import Any

import boto3

from idp.models import ExtractedDocument, KeyValue, Table, TextBlock


class TextractClient:
    def __init__(self, client: Any | None = None):
        self._client = client or boto3.client("textract")

    def start_analysis(self, bucket: str, key: str, document_id: str) -> str:
        # Async API handles multi-page PDFs and TIFFs; the sync API is single-page only.
        resp = self._client.start_document_analysis(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
            FeatureTypes=["FORMS", "TABLES"],
            ClientRequestToken=document_id[:64],  # idempotent if Step Functions retries the task
            JobTag=document_id[:64],
        )
        return resp["JobId"]

    def get_status(self, job_id: str) -> str:
        return self._client.get_document_analysis(JobId=job_id, MaxResults=1)["JobStatus"]

    def get_blocks(self, job_id: str) -> list[dict]:
        blocks: list[dict] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"JobId": job_id, "MaxResults": 1000}
            if token:
                kwargs["NextToken"] = token
            resp = self._client.get_document_analysis(**kwargs)
            blocks.extend(resp.get("Blocks", []))
            token = resp.get("NextToken")
            if not token:
                return blocks


def _text_of(block: dict, by_id: dict[str, dict]) -> str:
    words: list[str] = []
    for rel in block.get("Relationships", []):
        if rel["Type"] != "CHILD":
            continue
        for child_id in rel["Ids"]:
            child = by_id.get(child_id, {})
            if child.get("BlockType") == "WORD":
                words.append(child.get("Text", ""))
            elif child.get("BlockType") == "SELECTION_ELEMENT" and child.get("SelectionStatus") == "SELECTED":
                words.append("[X]")
    return " ".join(words)


def parse_blocks(document_id: str, blocks: list[dict]) -> ExtractedDocument:
    """Turn Textract's block graph into lines, key-value pairs and tables, keeping confidence scores."""
    by_id = {b["Id"]: b for b in blocks}
    pages = max((b.get("Page", 1) for b in blocks), default=0)

    lines = [
        TextBlock(text=b["Text"], page=b.get("Page", 1), confidence=b.get("Confidence", 0.0))
        for b in blocks
        if b["BlockType"] == "LINE"
    ]

    key_values: list[KeyValue] = []
    for b in blocks:
        if b["BlockType"] != "KEY_VALUE_SET" or "KEY" not in b.get("EntityTypes", []):
            continue
        value_ids = [i for r in b.get("Relationships", []) if r["Type"] == "VALUE" for i in r["Ids"]]
        value_blocks = [by_id[i] for i in value_ids if i in by_id]
        value_text = " ".join(_text_of(v, by_id) for v in value_blocks).strip()
        confidences = [b.get("Confidence", 0.0)] + [v.get("Confidence", 0.0) for v in value_blocks]
        key_values.append(
            KeyValue(
                key=_text_of(b, by_id).rstrip(":").strip(),
                value=value_text,
                page=b.get("Page", 1),
                confidence=min(confidences),
            )
        )

    tables: list[Table] = []
    for b in blocks:
        if b["BlockType"] != "TABLE":
            continue
        cells = [
            by_id[i]
            for r in b.get("Relationships", [])
            if r["Type"] == "CHILD"
            for i in r["Ids"]
            if by_id.get(i, {}).get("BlockType") == "CELL"
        ]
        if not cells:
            continue
        n_rows = max(c["RowIndex"] for c in cells)
        n_cols = max(c["ColumnIndex"] for c in cells)
        grid = [["" for _ in range(n_cols)] for _ in range(n_rows)]
        for c in cells:
            grid[c["RowIndex"] - 1][c["ColumnIndex"] - 1] = _text_of(c, by_id)
        tables.append(
            Table(page=b.get("Page", 1), rows=grid, confidence=min(c.get("Confidence", 0.0) for c in cells))
        )

    return ExtractedDocument(
        document_id=document_id, pages=pages, lines=lines, key_values=key_values, tables=tables
    )


def table_to_text(table: Table) -> str:
    """Render a table as pipe-delimited rows so the LLM keeps row/column alignment."""
    return "\n".join(" | ".join(cell for cell in row) for row in table.rows)
