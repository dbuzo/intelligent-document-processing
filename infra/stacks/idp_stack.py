"""Infrastructure for the IDP platform.

Flow: client -> API Gateway (Cognito) -> API Lambda -> presigned POST -> S3 raw bucket
      -> EventBridge "Object Created" -> Step Functions -> Textract / Bedrock / OpenSearch Serverless
"""

from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchserverless as aoss
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

ROOT = Path(__file__).resolve().parents[2]
COLLECTION = "idp-chunks"


class IdpStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, model_id: str, alarm_email: str | None, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ---------------- encryption & storage ----------------
        key = kms.Key(self, "DataKey", enable_key_rotation=True, alias="alias/idp-data",
                      removal_policy=RemovalPolicy.RETAIN)

        access_logs = s3.Bucket(
            self, "AccessLogs", encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, removal_policy=RemovalPolicy.RETAIN,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(365))],
        )

        def secure_bucket(name: str, **extra) -> s3.Bucket:
            return s3.Bucket(
                self, name, encryption=s3.BucketEncryption.KMS, encryption_key=key, bucket_key_enabled=True,
                enforce_ssl=True, block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
                server_access_logs_bucket=access_logs, server_access_logs_prefix=f"{name}/",
                removal_policy=RemovalPolicy.RETAIN, **extra,
            )

        raw = secure_bucket(
            "RawDocuments",
            event_bridge_enabled=True,
            cors=[s3.CorsRule(allowed_methods=[s3.HttpMethods.POST], allowed_origins=["*"], allowed_headers=["*"])],
            # Retention: originals are kept for the record-retention period, then expire.
            lifecycle_rules=[s3.LifecycleRule(
                expiration=Duration.days(7 * 365), noncurrent_version_expiration=Duration.days(30),
                abort_incomplete_multipart_upload_after=Duration.days(1),
            )],
        )
        processed = secure_bucket("Processed")

        table = ddb.Table(
            self, "Documents",
            partition_key=ddb.Attribute(name="document_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED, encryption_key=key,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        table.add_global_secondary_index(
            index_name="tenant-status",
            partition_key=ddb.Attribute(name="tenant_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="status", type=ddb.AttributeType.STRING),
        )

        review_topic = sns.Topic(self, "ReviewTopic", master_key=key, display_name="IDP review queue")
        alarm_topic = sns.Topic(self, "AlarmTopic", master_key=key)
        if alarm_email:
            sns.Subscription(self, "AlarmEmail", topic=alarm_topic, endpoint=alarm_email,
                             protocol=sns.SubscriptionProtocol.EMAIL)

        # ---------------- Lambda code & common config ----------------
        code = lambda_.Code.from_asset(
            str(ROOT),
            exclude=["*", "!src", "!src/**", "!requirements-lambda.txt"],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                command=["bash", "-c",
                         "pip install --no-cache-dir -r requirements-lambda.txt -t /asset-output "
                         "&& cp -r src/idp /asset-output/"],
            ),
        )

        dlq = sqs.Queue(self, "LambdaDLQ", encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                        retention_period=Duration.days(14), enforce_ssl=True)

        env = {
            "RAW_BUCKET": raw.bucket_name,
            "PROCESSED_BUCKET": processed.bucket_name,
            "TABLE_NAME": table.table_name,
            "KMS_KEY_ARN": key.key_arn,
            "MODEL_ID": model_id,
            "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
            "EMBEDDING_DIMS": "1024",
            "OPENSEARCH_INDEX": "idp-chunks",
            "REVIEW_TOPIC_ARN": review_topic.topic_arn,
            "POWERTOOLS_LOG_LEVEL": "INFO",
        }

        def fn(name: str, handler: str, timeout: int = 60, memory: int = 512) -> lambda_.Function:
            f = lambda_.Function(
                self, name,
                runtime=lambda_.Runtime.PYTHON_3_12,
                architecture=lambda_.Architecture.ARM_64,
                handler=handler,
                code=code,
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                environment={**env, "POWERTOOLS_SERVICE_NAME": name},
                tracing=lambda_.Tracing.ACTIVE,
                environment_encryption=key,
                log_group=logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH,
                                        removal_policy=RemovalPolicy.DESTROY),
                dead_letter_queue=dlq if handler.endswith(".index") else None,
            )
            cw.Alarm(
                self, f"{name}Errors", metric=f.metric_errors(period=Duration.minutes(5)), threshold=1,
                evaluation_periods=1, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ).add_alarm_action(cw_actions.SnsAction(alarm_topic))
            return f

        p = "idp.handlers.pipeline"
        start_fn = fn("StartExtraction", f"{p}.start_extraction")
        check_fn = fn("CheckExtraction", f"{p}.check_extraction", timeout=30)
        collect_fn = fn("CollectExtraction", f"{p}.collect_extraction", timeout=300, memory=1024)
        classify_fn = fn("Classify", f"{p}.classify", timeout=300)
        extract_fn = fn("Extract", f"{p}.extract", timeout=900, memory=1024)
        validate_fn = fn("Validate", f"{p}.validate")
        notify_fn = fn("NotifyReview", f"{p}.notify_review", timeout=30)
        index_fn = fn("Index", f"{p}.index", timeout=900, memory=1024)
        failed_fn = fn("MarkFailed", f"{p}.mark_failed", timeout=30)
        api_fn = fn("Api", "idp.api.app.handler", timeout=29, memory=1024)
        api_fn.add_environment("INDEX_FUNCTION_NAME", index_fn.function_name)

        # ---------------- least-privilege grants, per function ----------------
        for f in (start_fn, check_fn, collect_fn, classify_fn, extract_fn, validate_fn, notify_fn, index_fn,
                  failed_fn):
            table.grant_read_write_data(f)
        table.grant_read_write_data(api_fn)

        # Textract reads the object with the caller's permissions, so StartExtraction needs read + decrypt.
        raw.grant_read(start_fn)
        start_fn.add_to_role_policy(iam.PolicyStatement(actions=["textract:StartDocumentAnalysis"], resources=["*"]))
        for f in (check_fn, collect_fn):
            f.add_to_role_policy(iam.PolicyStatement(actions=["textract:GetDocumentAnalysis"], resources=["*"]))

        processed.grant_write(collect_fn)
        for f in (classify_fn, extract_fn, validate_fn, index_fn):
            processed.grant_read(f)
        processed.grant_read_write(extract_fn)
        processed.grant_read_write(validate_fn)
        processed.grant_read_write(api_fn)  # read results, write corrections and review audit records
        raw.grant_put(api_fn)  # needed to sign presigned POSTs for the raw bucket
        key.grant_encrypt_decrypt(api_fn)
        review_topic.grant_publish(notify_fn)
        index_fn.grant_invoke(api_fn)

        # Claude in Amazon Bedrock (Messages API endpoint) and Titan embeddings.
        claude_policy = iam.PolicyStatement(
            actions=["bedrock-mantle:CreateInference"],
            # Scope to the Claude model ARNs your account uses once confirmed in the Bedrock console.
            resources=["*"],
        )
        embed_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[f"arn:aws:bedrock:{self.region}::foundation-model/amazon.titan-embed-text-v2:0"],
        )
        for f in (classify_fn, extract_fn, api_fn):
            f.add_to_role_policy(claude_policy)
        for f in (index_fn, api_fn):
            f.add_to_role_policy(embed_policy)

        # ---------------- OpenSearch Serverless (vector collection) ----------------
        enc = aoss.CfnSecurityPolicy(
            self, "AossEncryption", name=f"{COLLECTION}-enc", type="encryption",
            policy=json.dumps({"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{COLLECTION}"]}],
                               "AWSOwnedKey": False, "KmsARN": key.key_arn}),
        )
        net = aoss.CfnSecurityPolicy(
            self, "AossNetwork", name=f"{COLLECTION}-net", type="network",
            # Production hardening: replace public access with an AOSS VPC endpoint (SourceVPCEs).
            policy=json.dumps([{"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{COLLECTION}"]}],
                                "AllowFromPublic": True}]),
        )
        collection = aoss.CfnCollection(self, "Collection", name=COLLECTION, type="VECTORSEARCH")
        collection.add_dependency(enc)
        collection.add_dependency(net)
        key.grant_encrypt_decrypt(iam.ServicePrincipal("aoss.amazonaws.com"))

        def data_access(name: str, principal: iam.IRole, permissions: list[str]) -> None:
            policy = aoss.CfnAccessPolicy(
                self, name, name=f"idp-{name.lower()}", type="data",
                policy=json.dumps([{
                    "Rules": [{"ResourceType": "index", "Resource": [f"index/{COLLECTION}/*"],
                               "Permission": permissions}],
                    "Principal": [principal.role_arn],
                }]),
            )
            policy.add_dependency(collection)

        data_access("IndexWriter", index_fn.role, ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:WriteDocument",
                                                    "aoss:ReadDocument", "aoss:UpdateIndex"])
        data_access("ApiReader", api_fn.role, ["aoss:DescribeIndex", "aoss:ReadDocument"])
        for f in (index_fn, api_fn):
            f.add_to_role_policy(iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[collection.attr_arn]))
            f.add_environment("OPENSEARCH_ENDPOINT", collection.attr_collection_endpoint)

        # ---------------- Step Functions ----------------
        def task(name: str, f: lambda_.IFunction, retries: bool = True) -> tasks.LambdaInvoke:
            t = tasks.LambdaInvoke(self, name, lambda_function=f, payload_response_only=True)
            if retries:
                # Throttling from Textract or Bedrock is expected under load: back off and retry.
                t.add_retry(
                    errors=["RetryableLLMError", "ThrottlingException", "ProvisionedThroughputExceededException",
                            "Lambda.TooManyRequestsException", "Lambda.ServiceException"],
                    interval=Duration.seconds(5), backoff_rate=2.0, max_attempts=5,
                    jitter_strategy=sfn.JitterType.FULL,
                )
            return t

        mark_failed = task("MarkFailedTask", failed_fn, retries=False)
        fail = sfn.Fail(self, "Failed")
        mark_failed.next(fail)

        start = task("StartExtractionTask", start_fn)
        wait = sfn.Wait(self, "WaitForTextract", time=sfn.WaitTime.duration(Duration.seconds(10)))
        check = task("CheckExtractionTask", check_fn)
        collect = task("CollectExtractionTask", collect_fn)
        classify = task("ClassifyTask", classify_fn)
        extract = task("ExtractTask", extract_fn)
        validate = task("ValidateTask", validate_fn)
        notify = task("NotifyReviewTask", notify_fn)
        index = tasks.LambdaInvoke(
            self, "IndexTask", lambda_function=index_fn, payload_response_only=True,
            payload=sfn.TaskInput.from_object({"document_id": sfn.JsonPath.string_at("$.document_id")}),
        )
        index.add_retry(interval=Duration.seconds(10), max_attempts=3, backoff_rate=2.0)

        for state in (start, check, collect, classify, extract, validate, notify, index):
            state.add_catch(mark_failed, result_path="$.error")

        textract_done = (
            sfn.Choice(self, "TextractDone?")
            .when(sfn.Condition.string_equals("$.textract_status", "IN_PROGRESS"), wait)
            .otherwise(collect)
        )
        route = (
            sfn.Choice(self, "NeedsReview?")
            .when(sfn.Condition.boolean_equals("$.needs_review", True),
                  notify.next(sfn.Succeed(self, "PendingReview")))
            .otherwise(index.next(sfn.Succeed(self, "Indexed")))
        )
        definition = (
            start.next(wait).next(check).next(textract_done)
        )
        collect.next(classify).next(extract).next(validate).next(route)

        sm_logs = logs.LogGroup(self, "StateMachineLogs", retention=logs.RetentionDays.ONE_MONTH,
                                removal_policy=RemovalPolicy.DESTROY)
        state_machine = sfn.StateMachine(
            self, "Pipeline",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.hours(2),
            tracing_enabled=True,
            # ERROR-level logs without execution data: document IDs are fine, document content is not.
            logs=sfn.LogOptions(destination=sm_logs, level=sfn.LogLevel.ERROR, include_execution_data=False),
        )
        cw.Alarm(
            self, "PipelineFailures", metric=state_machine.metric_failed(period=Duration.minutes(5)),
            threshold=1, evaluation_periods=1, treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        ).add_alarm_action(cw_actions.SnsAction(alarm_topic))

        events.Rule(
            self, "OnUpload",
            event_pattern=events.EventPattern(
                source=["aws.s3"], detail_type=["Object Created"],
                detail={"bucket": {"name": [raw.bucket_name]}, "object": {"key": [{"prefix": "uploads/"}]}},
            ),
            targets=[targets.SfnStateMachine(
                state_machine,
                input=events.RuleTargetInput.from_object({
                    "bucket": events.EventField.from_path("$.detail.bucket.name"),
                    "key": events.EventField.from_path("$.detail.object.key"),
                }),
                dead_letter_queue=dlq,
            )],
        )

        # ---------------- API Gateway + Cognito ----------------
        user_pool = cognito.UserPool(
            self, "Users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            mfa=cognito.Mfa.REQUIRED,
            mfa_second_factor=cognito.MfaSecondFactor(sms=False, otp=True),
            password_policy=cognito.PasswordPolicy(min_length=12, require_symbols=True),
            custom_attributes={"tenant_id": cognito.StringAttribute(mutable=False)},
            advanced_security_mode=cognito.AdvancedSecurityMode.ENFORCED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        for group in ("finance", "legal", "compliance", "audit", "reviewers", "admins"):
            cognito.CfnUserPoolGroup(self, f"Group{group.title()}", user_pool_id=user_pool.user_pool_id,
                                     group_name=group)
        client = user_pool.add_client(
            "WebClient", auth_flows=cognito.AuthFlow(user_srp=True),
            access_token_validity=Duration.hours(1), id_token_validity=Duration.hours(1),
            prevent_user_existence_errors=True,
        )

        api_logs = logs.LogGroup(self, "ApiAccessLogs", retention=logs.RetentionDays.ONE_MONTH,
                                 removal_policy=RemovalPolicy.DESTROY)
        api = apigw.LambdaRestApi(
            self, "IdpApi",
            handler=api_fn,
            proxy=True,
            default_method_options=apigw.MethodOptions(
                authorization_type=apigw.AuthorizationType.COGNITO,
                authorizer=apigw.CognitoUserPoolsAuthorizer(self, "Authorizer", cognito_user_pools=[user_pool]),
            ),
            deploy_options=apigw.StageOptions(
                stage_name="v1",
                throttling_rate_limit=50,
                throttling_burst_limit=100,
                tracing_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(api_logs),
                access_log_format=apigw.AccessLogFormat.json_with_standard_fields(
                    caller=False, http_method=True, ip=True, protocol=True, request_time=True,
                    resource_path=True, response_length=True, status=True, user=True),
            ),
        )

        CfnOutput(self, "ApiUrl", value=api.url)
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "RawBucket", value=raw.bucket_name)
        CfnOutput(self, "StateMachineArn", value=state_machine.state_machine_arn)
        CfnOutput(self, "CollectionEndpoint", value=collection.attr_collection_endpoint)
