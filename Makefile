.PHONY: install test lint demo synth deploy mcp host

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .

demo:
	POWERTOOLS_LOG_LEVEL=WARNING python scripts/local_demo.py

# Validates the CloudFormation template without Docker bundling.
synth:
	cd infra && CDK_DEFAULT_ACCOUNT=123456789012 CDK_CONTEXT_JSON='{"aws:cdk:bundling-stacks": []}' \
		CDK_OUTDIR=cdk.out python app.py

deploy:
	cd infra && npx cdk deploy

mcp:
	idp-mcp

host:
	idp-host
