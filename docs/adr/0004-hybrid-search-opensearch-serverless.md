# ADR 0004: Hybrid retrieval on OpenSearch Serverless

**Status:** accepted

**Context.** Users search with natural language ("contracts that auto-renew") and with exact identifiers
("INV-2026-0417", "AC-02.3"). Pure vector search is weak on identifiers; pure BM25 is weak on paraphrase.

**Decision.** Store chunks with Titan v2 embeddings in an OpenSearch Serverless vector collection. Run a
k-NN query and a BM25 query, both with the ACL filter inside the query, and merge them with reciprocal
rank fusion.

**Alternatives.** Bedrock Knowledge Bases (managed chunking and sync, but less control over ACL filtering
and chunk metadata); Aurora PostgreSQL + pgvector (cheaper at low volume, and SQL joins with metadata);
S3 Vectors (lowest cost, simpler queries). OpenSearch Serverless has a minimum OCU charge, so revisit at
low volume.
