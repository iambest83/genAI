"""Provision the recordings pipeline (item 4.2).

Idempotent: rerun safely. Creates / updates:

  • S3 bucket   `mfmod-recordings-<accountid>`
      - KMS-CMK-encrypted, BlockPublicAccess on, lifecycle: 30-day expiry
      - CORS for the SPA (S3 website endpoint)
      - PUT notification → transcribe-trigger Lambda
  • KMS key     `MfModAgent-RecordingsKey` (alias)
  • IAM role    `MfModAgent-UploadLambdaRole` for the upload Lambda
  • IAM role    `MfModAgent-TranscribeTriggerRole` for the S3-trigger
  • IAM role    `MfModAgent-TranscribeCompleteRole` for the EB rule target
  • Lambda      MfModAgent-Upload, MfModAgent-TranscribeTrigger,
                MfModAgent-TranscribeComplete
  • API GW v2 HTTP API with route POST /upload-url
  • EventBridge rule on Transcribe job state change → COMPLETED

Bedrock account: <ACCOUNT_ID>, us-east-1.
"""
import io
import json
import os
import time
import zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]

BUCKET = f"mfmod-recordings-{ACCOUNT}"
KMS_ALIAS = "alias/MfModAgent-RecordingsKey"
UPLOAD_FN = "MfModAgent-Upload"
TRIGGER_FN = "MfModAgent-TranscribeTrigger"
COMPLETE_FN = "MfModAgent-TranscribeComplete"
HTTP_API_NAME = "MfModAgent-UploadAPI"
EB_RULE = "MfModAgent-TranscribeJobComplete"

WS_API_ID = "<WS_API_ID>"
WS_ENDPOINT = f"https://{WS_API_ID}.execute-api.{REGION}.amazonaws.com/prod"
AGENT_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/MfModAgent-<RUNTIME_ID>"
)
API_KEY_ENV = os.environ.get("MFMOD_API_KEY") or ""  # set MFMOD_API_KEY before running this setup script
CONNECTIONS_TABLE = "MfModAgent-WsConnections"

s3 = boto3.client("s3", region_name=REGION)
kms = boto3.client("kms", region_name=REGION)
iam = boto3.client("iam")
lam = boto3.client("lambda", region_name=REGION)
apig = boto3.client("apigatewayv2", region_name=REGION)
events = boto3.client("events", region_name=REGION)


# ---------------------------------------------------------------------------
# KMS key + alias
# ---------------------------------------------------------------------------

def ensure_kms_key() -> str:
    try:
        alias = kms.describe_key(KeyId=KMS_ALIAS)
        print(f"  ✓ KMS key exists: {alias['KeyMetadata']['Arn']}")
        return alias["KeyMetadata"]["Arn"]
    except ClientError:
        pass

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "RootHasFullControl",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{ACCOUNT}:root"},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "AllowS3UseFromBedrockAccount",
                "Effect": "Allow",
                "Principal": {"Service": ["s3.amazonaws.com", "transcribe.amazonaws.com",
                                          "lambda.amazonaws.com"]},
                "Action": ["kms:Decrypt", "kms:GenerateDataKey", "kms:Encrypt"],
                "Resource": "*",
            },
        ],
    }
    resp = kms.create_key(
        Description="MfModAgent recordings — meeting audio at rest (item 4.2)",
        KeyUsage="ENCRYPT_DECRYPT", KeySpec="SYMMETRIC_DEFAULT",
        Policy=json.dumps(policy),
    )
    arn = resp["KeyMetadata"]["Arn"]
    kid = resp["KeyMetadata"]["KeyId"]
    kms.create_alias(AliasName=KMS_ALIAS, TargetKeyId=kid)
    print(f"  ✓ Created KMS key {arn} (alias {KMS_ALIAS})")
    return arn


# ---------------------------------------------------------------------------
# S3 bucket
# ---------------------------------------------------------------------------

def ensure_bucket(kms_arn: str) -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f"  ✓ Bucket exists: {BUCKET}")
    except ClientError:
        s3.create_bucket(Bucket=BUCKET)
        print(f"  ✓ Created bucket: {BUCKET}")

    s3.put_public_access_block(
        Bucket=BUCKET,
        PublicAccessBlockConfiguration=dict(
            BlockPublicAcls=True, IgnorePublicAcls=True,
            BlockPublicPolicy=True, RestrictPublicBuckets=True,
        ),
    )
    s3.put_bucket_encryption(
        Bucket=BUCKET,
        ServerSideEncryptionConfiguration={
            "Rules": [{
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms",
                    "KMSMasterKeyID": kms_arn,
                },
                "BucketKeyEnabled": True,
            }],
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [{
                "ID": "Expire recordings after 30 days",
                "Status": "Enabled",
                "Filter": {"Prefix": "recordings/"},
                "Expiration": {"Days": 30},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            }],
        },
    )
    s3.put_bucket_cors(
        Bucket=BUCKET,
        CORSConfiguration={
            "CORSRules": [{
                "AllowedHeaders": ["*"],
                "AllowedMethods": ["PUT", "POST", "GET", "HEAD"],
                "AllowedOrigins": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 3000,
            }],
        },
    )
    print("  ✓ Bucket public-block / KMS / lifecycle / CORS configured")


# ---------------------------------------------------------------------------
# IAM roles
# ---------------------------------------------------------------------------

LAMBDA_TRUST = {
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}],
}


def ensure_role(name: str, inline_policy: dict) -> str:
    try:
        role = iam.get_role(RoleName=name)
        arn = role["Role"]["Arn"]
        print(f"  ✓ Role exists: {name}")
    except ClientError:
        role = iam.create_role(
            RoleName=name, AssumeRolePolicyDocument=json.dumps(LAMBDA_TRUST),
        )
        arn = role["Role"]["Arn"]
        print(f"  ✓ Created role: {name}")
        # Wait for IAM to be consistent enough for Lambda to assume
        time.sleep(8)

    iam.attach_role_policy(
        RoleName=name,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=name, PolicyName=f"{name}-inline",
        PolicyDocument=json.dumps(inline_policy),
    )
    return arn


def upload_role_policy(kms_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["s3:PutObject"],
             "Resource": f"arn:aws:s3:::{BUCKET}/*"},
            {"Effect": "Allow",
             "Action": ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"],
             "Resource": kms_arn},
            {"Effect": "Allow",
             "Action": ["dynamodb:GetItem"],
             "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{CONNECTIONS_TABLE}"},
        ],
    }


def trigger_role_policy(kms_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["s3:GetObject", "s3:HeadObject"],
             "Resource": f"arn:aws:s3:::{BUCKET}/*"},
            {"Effect": "Allow",
             "Action": ["kms:Decrypt"],
             "Resource": kms_arn},
            {"Effect": "Allow",
             "Action": [
                 "transcribe:StartTranscriptionJob",
                 "transcribe:GetTranscriptionJob",
                 # Required when StartTranscriptionJob is called with Tags=[...]
                 "transcribe:TagResource",
             ],
             "Resource": "*"},
        ],
    }


def complete_role_policy(kms_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow",
             "Action": ["transcribe:GetTranscriptionJob"],
             "Resource": "*"},
            {"Effect": "Allow",
             "Action": ["s3:GetObject"],
             "Resource": f"arn:aws:s3:::{BUCKET}/*"},
            {"Effect": "Allow",
             "Action": ["kms:Decrypt"],
             "Resource": kms_arn},
            {"Effect": "Allow",
             "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
             # AgentCore traverses runtime-endpoint sub-resources, so the
             # resource ARN needs both the runtime and its child paths.
             "Resource": [AGENT_RUNTIME_ARN, f"{AGENT_RUNTIME_ARN}/*"]},
            {"Effect": "Allow",
             "Action": ["execute-api:ManageConnections"],
             "Resource": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{WS_API_ID}/*/*"},
        ],
    }


# ---------------------------------------------------------------------------
# Lambda packaging + deploy
# ---------------------------------------------------------------------------

def _zip_single(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_function.py", path.read_text())
    return buf.getvalue()


def ensure_lambda(name: str, role_arn: str, source: Path,
                  env: dict, timeout: int = 30, memory: int = 512) -> str:
    code = _zip_single(source)
    try:
        info = lam.get_function(FunctionName=name)
        # Update code + config
        lam.update_function_code(FunctionName=name, ZipFile=code, Publish=False)
        # Wait for code update to settle before pushing config
        for _ in range(20):
            cfg = lam.get_function_configuration(FunctionName=name)
            if cfg.get("LastUpdateStatus") != "InProgress":
                break
            time.sleep(0.5)
        lam.update_function_configuration(
            FunctionName=name, Role=role_arn,
            Timeout=timeout, MemorySize=memory,
            Handler="lambda_function.lambda_handler",
            Runtime="python3.13",
            Environment={"Variables": env},
        )
        print(f"  ✓ Lambda updated: {name}")
        return info["Configuration"]["FunctionArn"]
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
    resp = lam.create_function(
        FunctionName=name, Runtime="python3.13",
        Role=role_arn, Handler="lambda_function.lambda_handler",
        Code={"ZipFile": code}, Timeout=timeout, MemorySize=memory,
        Environment={"Variables": env}, Publish=False,
    )
    print(f"  ✓ Lambda created: {name}")
    return resp["FunctionArn"]


# ---------------------------------------------------------------------------
# S3 → Lambda notification
# ---------------------------------------------------------------------------

def ensure_s3_notification(trigger_fn_arn: str) -> None:
    # Allow S3 to invoke the trigger Lambda
    try:
        lam.add_permission(
            FunctionName=TRIGGER_FN, StatementId="S3InvokeRecordings",
            Action="lambda:InvokeFunction", Principal="s3.amazonaws.com",
            SourceArn=f"arn:aws:s3:::{BUCKET}",
            SourceAccount=ACCOUNT,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise

    s3.put_bucket_notification_configuration(
        Bucket=BUCKET,
        NotificationConfiguration={
            "LambdaFunctionConfigurations": [{
                "Id": "RecordingArrived",
                "LambdaFunctionArn": trigger_fn_arn,
                "Events": ["s3:ObjectCreated:*"],
                "Filter": {"Key": {"FilterRules": [
                    {"Name": "prefix", "Value": "recordings/"},
                ]}},
            }],
        },
    )
    print("  ✓ S3 → trigger-lambda notification wired")


# ---------------------------------------------------------------------------
# EventBridge rule (Transcribe completed → complete-lambda)
# ---------------------------------------------------------------------------

def ensure_eb_rule(complete_fn_arn: str) -> None:
    pattern = {
        "source": ["aws.transcribe"],
        "detail-type": ["Transcribe Job State Change"],
        "detail": {
            "TranscriptionJobStatus": ["COMPLETED", "FAILED"],
        },
    }
    events.put_rule(
        Name=EB_RULE,
        EventPattern=json.dumps(pattern),
        State="ENABLED",
        Description="Forward Transcribe job completion to MfModAgent-TranscribeComplete",
    )
    events.put_targets(
        Rule=EB_RULE,
        Targets=[{"Id": "1", "Arn": complete_fn_arn}],
    )
    try:
        lam.add_permission(
            FunctionName=COMPLETE_FN, StatementId="EbInvokeTranscribeComplete",
            Action="lambda:InvokeFunction", Principal="events.amazonaws.com",
            SourceArn=f"arn:aws:events:{REGION}:{ACCOUNT}:rule/{EB_RULE}",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
    print(f"  ✓ EventBridge rule {EB_RULE} → {COMPLETE_FN}")


# ---------------------------------------------------------------------------
# HTTP API (POST /upload-url)
# ---------------------------------------------------------------------------

def ensure_http_api(upload_fn_arn: str) -> str:
    # Find or create
    apis = apig.get_apis()["Items"]
    api = next((a for a in apis if a["Name"] == HTTP_API_NAME), None)
    if not api:
        api = apig.create_api(
            Name=HTTP_API_NAME, ProtocolType="HTTP",
            CorsConfiguration={
                "AllowOrigins": ["*"],
                "AllowMethods": ["POST", "OPTIONS"],
                "AllowHeaders": ["content-type", "authorization"],
                "MaxAge": 3000,
            },
        )
        print(f"  ✓ Created HTTP API: {HTTP_API_NAME}")
    api_id = api["ApiId"]

    # Integration
    integrations = apig.get_integrations(ApiId=api_id)["Items"]
    integ = next((i for i in integrations
                  if i.get("IntegrationUri") == upload_fn_arn), None)
    if not integ:
        integ = apig.create_integration(
            ApiId=api_id, IntegrationType="AWS_PROXY",
            IntegrationUri=upload_fn_arn,
            PayloadFormatVersion="2.0",
        )
        print("  ✓ Created upload Lambda integration")
    integ_id = integ["IntegrationId"]

    # Route
    routes = apig.get_routes(ApiId=api_id)["Items"]
    route = next((r for r in routes if r["RouteKey"] == "POST /upload-url"), None)
    if not route:
        apig.create_route(
            ApiId=api_id, RouteKey="POST /upload-url",
            Target=f"integrations/{integ_id}",
        )
        print("  ✓ Created route POST /upload-url")

    # Stage (autodeploy)
    stages = apig.get_stages(ApiId=api_id)["Items"]
    if not any(s["StageName"] == "$default" for s in stages):
        apig.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True)
        print("  ✓ Created $default stage (auto-deploy)")

    # Permission for API GW to invoke the Lambda
    try:
        lam.add_permission(
            FunctionName=UPLOAD_FN, StatementId="HttpApiInvokeUpload",
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/*/upload-url",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise

    endpoint = f"https://{api_id}.execute-api.{REGION}.amazonaws.com"
    print(f"  ✓ Upload endpoint: {endpoint}/upload-url")
    return endpoint


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    here = Path(__file__).parent

    print("\n=== KMS ===")
    kms_arn = ensure_kms_key()

    print("\n=== S3 bucket ===")
    ensure_bucket(kms_arn)

    print("\n=== IAM roles ===")
    upload_role = ensure_role("MfModAgent-UploadLambdaRole",
                              upload_role_policy(kms_arn))
    trigger_role = ensure_role("MfModAgent-TranscribeTriggerRole",
                               trigger_role_policy(kms_arn))
    complete_role = ensure_role("MfModAgent-TranscribeCompleteRole",
                                complete_role_policy(kms_arn))

    print("\n=== Lambdas ===")
    upload_arn = ensure_lambda(
        UPLOAD_FN, upload_role, here / "upload_lambda.py",
        env={
            "API_KEY": API_KEY_ENV,
            "RECORDINGS_BUCKET": BUCKET,
            "RECORDINGS_KMS_KEY_ARN": kms_arn,
            "CONNECTIONS_TABLE": CONNECTIONS_TABLE,
        },
    )
    trigger_arn = ensure_lambda(
        TRIGGER_FN, trigger_role, here / "transcribe_trigger_lambda.py",
        env={},
    )
    complete_arn = ensure_lambda(
        COMPLETE_FN, complete_role, here / "transcribe_complete_lambda.py",
        env={
            "AGENT_RUNTIME_ARN": AGENT_RUNTIME_ARN,
            "WS_ENDPOINT": WS_ENDPOINT,
        },
        timeout=120, memory=1024,
    )

    print("\n=== Wiring ===")
    ensure_s3_notification(trigger_arn)
    ensure_eb_rule(complete_arn)
    endpoint = ensure_http_api(upload_arn)

    print("\n=== Done ===")
    print(f"Upload endpoint: {endpoint}/upload-url")
    print(f"Bucket:          s3://{BUCKET}/")


if __name__ == "__main__":
    main()
