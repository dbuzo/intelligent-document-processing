# ADR 0003: The MCP server calls the REST API with the user's token

**Status:** accepted

**Context.** An MCP server could read DynamoDB, S3, and OpenSearch directly with its own IAM role. That's
fast to build, but it would have to re-implement every authorization rule, and a bug or a prompt-injected
agent could read across tenants.

**Decision.** The MCP server is a thin client of the REST API and forwards the end user's bearer token.
It has no AWS permissions.

**Consequences.**
+ One authorization path for people, scripts, and agents; tested once.
+ No confused-deputy risk: the agent can never see more than the signed-in user.
− An extra network hop. Token passthrough should become OAuth token exchange with audience-bound tokens
  for a multi-service production setup (see docs/MCP.md §5).
