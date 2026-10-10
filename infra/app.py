#!/usr/bin/env python3
import os

import aws_cdk as cdk
from stacks.idp_stack import IdpStack

app = cdk.App()
IdpStack(
    app,
    "IdpStack",
    model_id=app.node.try_get_context("model_id") or "anthropic.claude-opus-5-5",
    alarm_email=app.node.try_get_context("alarm_email"),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
)
cdk.Tags.of(app).add("project", "intelligent-document-processing")
cdk.Tags.of(app).add("data-classification", "confidential")
app.synth()
