"""Upload-URL Lambda — presigned S3 PUT for meeting recordings (item 4.2).

Browser POSTs to this endpoint with:
  {
    "filename": "call-2026-06-01.webm",
    "content_type": "audio/webm",
    "consent_acknowledged": true,
    "conn_id": "<websocket connectionId>"   // so transcribe-complete can push back
  }

We return a presigned PUT URL plus the S3 key. The browser then uploads
the audio directly to S3 — Lambda doesn't proxy bytes (and shouldn't, at
audio sizes).

The S3 object's user-defined metadata carries the session identity:
sa_id, customer_id, lob_id, conn_id, sa_consent. The completion Lambda
reads these to know who to bill the work to and where to push the
result.

Authorization: API key (same key the WebSocket uses; lives in env var
API_KEY). Requires `consent_acknowledged: true` in the body — refuses
otherwise. Refuses without a bound customer.
"""
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config

API_KEY = os.environ.get("API_KEY", "")
BUCKET = os.environ.get("RECORDINGS_BUCKET", "")
KMS_KEY_ARN = os.environ.get("RECORDINGS_KMS_KEY_ARN", "")
CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "MfModAgent-WsConnections")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Force SigV4 + virtual-hosted addressing. KMS-encrypted S3 PUTs (which is
# every PUT here, since the bucket has SSE-KMS as default) REQUIRE SigV4 —
# boto3's default presigner can fall back to SigV2 and that fails with
# "Requests specifying Server Side Encryption with AWS KMS managed keys
# require AWS Signature Version 4." (See:
# https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html)
s3 = boto3.client(
    "s3",
    region_name=REGION,
    config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)
ddb = boto3.resource("dynamodb", region_name=REGION).Table(CONNECTIONS_TABLE)

ALLOWED_TYPES = {
    "audio/webm", "audio/wav", "audio/x-wav", "audio/mpeg", "audio/mp3",
    "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/aac", "audio/ogg",
}

EXT_BY_TYPE = {
    "audio/webm": "webm", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "m4a", "audio/m4a": "m4a", "audio/x-m4a": "m4a",
    "audio/aac": "aac", "audio/ogg": "ogg",
}


def lambda_handler(event, context):
    print(f"[upload_lambda] {event.get('routeKey') or event.get('rawPath')}")

    # CORS preflight
    method = (event.get("requestContext", {}).get("http", {}) or {}).get("method", "")
    if method == "OPTIONS":
        return _cors(204, {})

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _cors(400, {"error": "Invalid JSON"})

    api_key = body.get("api_key") or ""
    if API_KEY and api_key != API_KEY:
        return _cors(401, {"error": "Invalid API key"})

    if not body.get("consent_acknowledged"):
        return _cors(400, {"error": "consent_acknowledged required"})

    conn_id = (body.get("conn_id") or "").strip()
    if not conn_id:
        return _cors(400, {"error": "conn_id required"})

    # Resolve session — refuse if no customer is bound. Recordings always
    # belong to a specific customer.
    try:
        sess = ddb.get_item(Key={"connectionId": conn_id}).get("Item") or {}
    except Exception as e:
        return _cors(500, {"error": f"session lookup failed: {e}"})
    customer_id = sess.get("customer_id") or "default"
    if customer_id in ("default", "", None):
        return _cors(400, {
            "error": "Pick a Customer at the top before uploading a recording.",
        })
    sa_id = sess.get("sa_id") or "anonymous"
    lob_id = sess.get("lob_id") or "default"
    customer_display = sess.get("customer_display_name") or ""
    lob_display = sess.get("lob_display_name") or ""

    # Validate content type. Browsers attach codec parameters
    # (e.g. "audio/webm;codecs=opus") which would fail an exact-match
    # check; strip the parameters and compare on the base mime only.
    raw_ct = (body.get("content_type") or "").lower().strip()
    base_ct = raw_ct.split(";", 1)[0].strip()
    if base_ct not in ALLOWED_TYPES:
        return _cors(400, {
            "error": f"unsupported content_type {raw_ct!r}. "
                     f"Allowed: {sorted(ALLOWED_TYPES)}",
        })
    # Use the full content_type (codec params included) for the S3 PUT
    # signature so the browser's matching PUT header validates.
    content_type = raw_ct

    # Build a stable, namespaced S3 key. Include sa_id + customer_id +
    # lob_id in the prefix so lifecycle/retention can target subsets.
    ext = EXT_BY_TYPE.get(base_ct, "bin")
    today = time.strftime("%Y/%m/%d")
    obj_id = uuid.uuid4().hex[:12]
    safe_name = _slug((body.get("filename") or "recording") + "")
    key = f"recordings/{today}/sa={sa_id}/customer={customer_id}/lob={lob_id}/{obj_id}-{safe_name}.{ext}"

    # Presign a PUT for ~10 minutes. Object-level metadata carries the
    # session context so the transcribe-complete Lambda can push back to
    # the right WS connection without another DDB lookup.
    #
    # IMPORTANT: keys here are HYPHEN-CASE only. boto3's
    # generate_presigned_url(Metadata=…) canonicalizes the keys exactly
    # as it sees them, so any underscore key produces a signed-but-
    # mismatched header pair (the browser sends one form, the signature
    # demands the other). Sticking to hyphen-case keeps the signed
    # canonical-request and the browser's PUT headers identical.
    metadata = {
        "sa-id": sa_id,
        "customer-id": customer_id,
        "customer-display-name": customer_display,
        "lob-id": lob_id,
        "lob-display-name": lob_display,
        "conn-id": conn_id,
        "consent-acknowledged": "true",
        "uploaded-at-ms": str(int(time.time() * 1000)),
    }
    try:
        put_params = {
            "Bucket": BUCKET,
            "Key": key,
            "ContentType": content_type,
            "Metadata": metadata,
            "ServerSideEncryption": "aws:kms",
        }
        if KMS_KEY_ARN:
            put_params["SSEKMSKeyId"] = KMS_KEY_ARN
        url = s3.generate_presigned_url(
            "put_object",
            Params=put_params,
            ExpiresIn=600,
        )
    except Exception as e:
        return _cors(500, {"error": f"presign failed: {e}"})

    return _cors(200, {
        "upload_url": url,
        "key": key,
        "bucket": BUCKET,
        "headers_required": {
            "Content-Type": content_type,
            "x-amz-server-side-encryption": "aws:kms",
            **({"x-amz-server-side-encryption-aws-kms-key-id": KMS_KEY_ARN} if KMS_KEY_ARN else {}),
            # Echo each metadata key 1:1 with the form boto3 just signed.
            **{f"x-amz-meta-{k}": v for k, v in metadata.items()},
        },
    })


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "rec"
    return s[:60]


def _cors(status: int, body: dict):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",  # frontend + S3 website on different origin
            "Access-Control-Allow-Methods": "POST,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body),
    }
