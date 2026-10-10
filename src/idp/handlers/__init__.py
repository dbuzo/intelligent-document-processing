"""AWS Lambda entry points. Thin: parse the event, call pipeline logic, persist, return a small payload.

Step Functions state stays small (IDs and S3 keys only) because state payloads are capped at 256 KB
and document content should not sit in execution history.
"""
