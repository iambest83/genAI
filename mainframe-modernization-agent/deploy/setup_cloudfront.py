"""Front the SPA bucket with CloudFront so the site is served over HTTPS.

Why: live audio (`navigator.mediaDevices.getUserMedia`) is gated on a
secure context. S3 website endpoints are HTTP-only — without TLS, the
mic API is undefined and the Record tab fails with the misleading
"Cannot read properties of undefined" error.

This script is idempotent. It creates (or finds) an Origin Access
Control, a distribution that points at the SPA bucket via that OAC,
and a bucket policy that allows ONLY the distribution to read.

Output: the CloudFront domain. Use it in place of the S3 website URL.
"""
import json
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCOUNT = boto3.client("sts").get_caller_identity()["Account"]

BUCKET = f"mfmod-chat-ui-{ACCOUNT}"
DIST_COMMENT = "MfModAgent SPA — HTTPS in front of S3"
OAC_NAME = "MfModAgent-SPA-OAC"

cf = boto3.client("cloudfront")
s3 = boto3.client("s3", region_name=REGION)


# ---------------------------------------------------------------------------
# Origin Access Control (OAC) — replaces the legacy OAI
# ---------------------------------------------------------------------------

def ensure_oac() -> str:
    resp = cf.list_origin_access_controls()
    for item in resp.get("OriginAccessControlList", {}).get("Items", []):
        if item.get("Name") == OAC_NAME:
            print(f"  ✓ OAC exists: {item['Id']}")
            return item["Id"]
    cfg = {
        "Name": OAC_NAME,
        "Description": "OAC for the MfModAgent SPA bucket",
        "OriginAccessControlOriginType": "s3",
        "SigningBehavior": "always",
        "SigningProtocol": "sigv4",
    }
    out = cf.create_origin_access_control(OriginAccessControlConfig=cfg)
    oac_id = out["OriginAccessControl"]["Id"]
    print(f"  ✓ Created OAC: {oac_id}")
    return oac_id


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------

def find_distribution() -> dict | None:
    paginator = cf.get_paginator("list_distributions")
    for page in paginator.paginate():
        for d in page.get("DistributionList", {}).get("Items") or []:
            if d.get("Comment") == DIST_COMMENT:
                return d
    return None


def ensure_distribution(oac_id: str) -> dict:
    existing = find_distribution()
    if existing:
        print(f"  ✓ Distribution exists: {existing['Id']} → {existing['DomainName']}")
        return existing

    s3_origin_domain = f"{BUCKET}.s3.{REGION}.amazonaws.com"
    config = {
        "CallerReference": f"mfmod-spa-{int(time.time())}",
        "Comment": DIST_COMMENT,
        "Enabled": True,
        "PriceClass": "PriceClass_100",
        "DefaultRootObject": "index.html",
        "Origins": {
            "Quantity": 1,
            "Items": [{
                "Id": "spa-s3",
                "DomainName": s3_origin_domain,
                "S3OriginConfig": {"OriginAccessIdentity": ""},
                "OriginAccessControlId": oac_id,
                "CustomHeaders": {"Quantity": 0},
                "ConnectionAttempts": 3,
                "ConnectionTimeout": 10,
                "OriginShield": {"Enabled": False},
            }],
        },
        "DefaultCacheBehavior": {
            "TargetOriginId": "spa-s3",
            "ViewerProtocolPolicy": "redirect-to-https",
            "AllowedMethods": {
                "Quantity": 2, "Items": ["GET", "HEAD"],
                "CachedMethods": {"Quantity": 2, "Items": ["GET", "HEAD"]},
            },
            "Compress": True,
            # CachingDisabled managed policy — frontend assets ship with
            # `Cache-Control: no-cache,no-store,must-revalidate` so we want
            # CloudFront to honor that during dev.
            "CachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        },
        "ViewerCertificate": {
            "CloudFrontDefaultCertificate": True,
            "MinimumProtocolVersion": "TLSv1.2_2021",
            "SSLSupportMethod": "sni-only",
        },
        "HttpVersion": "http2",
        "IsIPV6Enabled": True,
        # SPA fallback: 403/404 from S3 → /index.html with a 200. Helps
        # if we ever add client-side routing; harmless today.
        "CustomErrorResponses": {
            "Quantity": 2,
            "Items": [
                {"ErrorCode": 403, "ResponsePagePath": "/index.html",
                 "ResponseCode": "200", "ErrorCachingMinTTL": 10},
                {"ErrorCode": 404, "ResponsePagePath": "/index.html",
                 "ResponseCode": "200", "ErrorCachingMinTTL": 10},
            ],
        },
    }
    out = cf.create_distribution(DistributionConfig=config)
    d = out["Distribution"]
    print(f"  ✓ Created distribution: {d['Id']} → {d['DomainName']}")
    return {"Id": d["Id"], "DomainName": d["DomainName"], "ARN": d["ARN"]}


# ---------------------------------------------------------------------------
# S3 bucket policy: only the distribution can read
# ---------------------------------------------------------------------------

def lock_bucket_to_distribution(distribution_arn: str) -> None:
    policy = {
        "Version": "2008-10-17",
        "Statement": [{
            "Sid": "AllowCloudFrontServicePrincipalReadOnly",
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{BUCKET}/*",
            "Condition": {
                "StringEquals": {
                    "AWS:SourceArn": distribution_arn,
                },
            },
        }],
    }
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(policy))
    print(f"  ✓ Bucket policy locked to distribution {distribution_arn}")


def main():
    print("\n=== OAC ===")
    oac_id = ensure_oac()

    print("\n=== Distribution ===")
    d = ensure_distribution(oac_id)

    # When we just created the distribution, ARN is in the response.
    # When it already existed, list_distributions gave us the ARN.
    arn = d.get("ARN") or f"arn:aws:cloudfront::{ACCOUNT}:distribution/{d['Id']}"

    print("\n=== Bucket policy ===")
    lock_bucket_to_distribution(arn)

    print("\n=== Done ===")
    print(f"HTTPS URL: https://{d['DomainName']}")
    print("(allow ~3-5 minutes for the distribution to fully deploy)")


if __name__ == "__main__":
    main()
