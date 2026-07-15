"""Set up WebSocket API Gateway + DynamoDB + Lambda for streaming chat."""
import boto3
import json
import time
import zipfile
import io
import secrets
import os

REGION = "us-east-1"
ACCOUNT_ID = "<ACCOUNT_ID>"

iam = boto3.client("iam", region_name=REGION)
lambda_client = boto3.client("lambda", region_name=REGION)
apigw = boto3.client("apigatewayv2", region_name=REGION)
dynamodb = boto3.client("dynamodb", region_name=REGION)

API_KEY = os.environ.get("MFMOD_API_KEY") or ""  # set MFMOD_API_KEY before running this setup script
TABLE_NAME = "MfModAgent-WsConnections"
LAMBDA_NAME = "MfModAgent-WsHandler"
ROLE_NAME = "MfModAgent-WsLambdaRole"


def create_dynamodb_table():
    print("Creating DynamoDB table...")
    try:
        dynamodb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        print(f"  Created: {TABLE_NAME}")
        waiter = dynamodb.get_waiter("table_exists")
        waiter.wait(TableName=TABLE_NAME)
        print("  Table active")
    except dynamodb.exceptions.ResourceInUseException:
        print(f"  Exists: {TABLE_NAME}")


def create_lambda_role():
    print("Creating Lambda role...")
    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    })
    try:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=trust)
        print(f"  Created: {ROLE_NAME}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"  Exists: {ROLE_NAME}")

    iam.attach_role_policy(RoleName=ROLE_NAME, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="WsPermissions", PolicyDocument=json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeStreaming"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:GetItem"], "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/{TABLE_NAME}"},
            {"Effect": "Allow", "Action": ["execute-api:ManageConnections"], "Resource": "*"},
        ]
    }))
    print("  Policies attached")
    time.sleep(10)
    return f"arn:aws:iam::{ACCOUNT_ID}:role/{ROLE_NAME}"


def create_lambda(role_arn):
    print("Creating Lambda function...")
    code = open(os.path.join(os.path.dirname(__file__), "ws_lambda.py")).read()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("ws_lambda.py", code)
    buf.seek(0)
    zip_bytes = buf.read()

    env = {
        "Variables": {
            "AGENT_RUNTIME_ARN": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/MfModAgent-<RUNTIME_ID>",
            "CONNECTIONS_TABLE": TABLE_NAME,
            "API_KEY": API_KEY,
        }
    }

    try:
        resp = lambda_client.create_function(
            FunctionName=LAMBDA_NAME, Runtime="python3.13", Role=role_arn,
            Handler="ws_lambda.lambda_handler", Code={"ZipFile": zip_bytes},
            Timeout=300, MemorySize=256, Environment=env,
        )
        arn = resp["FunctionArn"]
        print(f"  Created: {arn}")
    except lambda_client.exceptions.ResourceConflictException:
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("ws_lambda.py", code)
        buf2.seek(0)
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=buf2.read())
        lambda_client.update_function_configuration(FunctionName=LAMBDA_NAME, Environment=env, Timeout=300)
        arn = f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:{LAMBDA_NAME}"
        print(f"  Updated: {arn}")

    time.sleep(5)
    return arn


def create_websocket_api(lambda_arn):
    print("Creating WebSocket API Gateway...")
    api = apigw.create_api(
        Name="MfModAgent-WebSocket",
        ProtocolType="WEBSOCKET",
        RouteSelectionExpression="$request.body.action",
    )
    api_id = api["ApiId"]
    print(f"  API ID: {api_id}")

    # Create integration
    integration = apigw.create_integration(
        ApiId=api_id, IntegrationType="AWS_PROXY",
        IntegrationUri=f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{lambda_arn}/invocations",
    )
    int_id = integration["IntegrationId"]

    # Create routes
    for route in ["$connect", "$disconnect", "sendMessage"]:
        apigw.create_route(ApiId=api_id, RouteKey=route, Target=f"integrations/{int_id}")
        print(f"  Route: {route}")

    # Deploy
    apigw.create_stage(ApiId=api_id, StageName="prod", AutoDeploy=True)

    # Grant API Gateway permission to invoke Lambda
    for sid, route in [("WsConnect", "$connect"), ("WsDisconnect", "$disconnect"), ("WsSendMessage", "sendMessage")]:
        try:
            lambda_client.add_permission(
                FunctionName=LAMBDA_NAME, StatementId=sid,
                Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
                SourceArn=f"arn:aws:execute-api:{REGION}:{ACCOUNT_ID}:{api_id}/*/{route}",
            )
        except lambda_client.exceptions.ResourceConflictException:
            pass

    ws_url = f"wss://{api_id}.execute-api.{REGION}.amazonaws.com/prod"
    print(f"  WebSocket URL: {ws_url}")
    return api_id, ws_url


if __name__ == "__main__":
    print("=" * 60)
    print("  Setting up WebSocket API for MfModAgent")
    print("=" * 60)

    create_dynamodb_table()
    role_arn = create_lambda_role()
    lambda_arn = create_lambda(role_arn)
    api_id, ws_url = create_websocket_api(lambda_arn)

    print()
    print("=" * 60)
    print("  SETUP COMPLETE!")
    print("=" * 60)
    print(f"  WebSocket URL: {ws_url}")
    print(f"  API Key: {API_KEY}")
    print()
    print("  Test with wscat:")
    print(f"  wscat -c '{ws_url}'")
    print(f'  > {{"action":"sendMessage","prompt":"Hello","api_key":"{API_KEY}"}}')
    print("=" * 60)

    config = {"api_id": api_id, "ws_url": ws_url, "api_key": API_KEY}
    with open(os.path.join(os.path.dirname(__file__), "ws_config.json"), "w") as f:
        json.dump(config, f, indent=2)
    print(f"\nConfig saved to deploy/ws_config.json")
