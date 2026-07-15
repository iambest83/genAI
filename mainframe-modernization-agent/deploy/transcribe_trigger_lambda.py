"""S3-trigger Lambda — kicks off a Transcribe job when audio lands (item 4.2).

Wired to the recordings bucket via `s3:ObjectCreated:*`. Reads the
object metadata (sa_id, customer_id, conn_id, etc.) and starts an Amazon
Transcribe job with diarization on. Job tags carry the same metadata so
the completion Lambda can recover it without re-reading S3.
"""
import json
import os
import time
import urllib.parse

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")

s3 = boto3.client("s3", region_name=REGION)
transcribe = boto3.client("transcribe", region_name=REGION)


def lambda_handler(event, context):
    for record in event.get("Records", []):
        s3rec = record.get("s3", {})
        bucket = s3rec.get("bucket", {}).get("name")
        key = s3rec.get("object", {}).get("key")
        if not bucket or not key:
            continue
        # S3 event notifications URL-encode the key (spaces → +, '=' → %3D, …).
        # Subsequent HeadObject / Transcribe calls need the literal key.
        key = urllib.parse.unquote_plus(key)

        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except Exception as e:
            print(f"[transcribe_trigger] head_object failed key={key} err={e}")
            continue

        meta = head.get("Metadata") or {}
        # Metadata keys are stored hyphen-cased (see upload_lambda.py — the
        # underscore form was breaking SigV4 because the canonical-request
        # header set didn't match what the browser sent). Helper handles
        # both spellings during the cutover.
        def _m(k):
            return meta.get(k.replace("_", "-")) or meta.get(k) or ""
        sa_id = _m("sa_id") or "anonymous"
        customer_id = _m("customer_id") or "default"
        conn_id = _m("conn_id") or ""

        if customer_id in ("default", "", None):
            print(f"[transcribe_trigger] skip (no customer bound) key={key}")
            continue

        # Job name must be unique per account+region; include a timestamp.
        job_name = f"mfmod-{int(time.time() * 1000)}-{key.replace('/', '-')[-60:]}"
        # Transcribe job names allow [a-zA-Z0-9._-]
        job_name = "".join(c if c.isalnum() or c in "._-" else "-" for c in job_name)
        if len(job_name) > 200:
            job_name = job_name[-200:]

        media_uri = f"s3://{bucket}/{key}"
        media_format = (key.rsplit(".", 1)[-1] or "webm").lower()
        if media_format not in ("mp3", "mp4", "wav", "flac", "ogg", "amr", "webm", "m4a"):
            media_format = "webm"

        try:
            transcribe.start_transcription_job(
                TranscriptionJobName=job_name,
                LanguageCode="en-US",
                MediaFormat=media_format,
                Media={"MediaFileUri": media_uri},
                Settings={
                    "ShowSpeakerLabels": True,
                    "MaxSpeakerLabels": 4,
                },
                # Redact PII at the source. RedactionOutput="redacted" means
                # Transcribe never produces a cleartext transcript at all —
                # SSNs, credit-card numbers, names, etc. are replaced with
                # [PII] tags before the JSON is written to S3. Required for
                # an FSI-facing workload; aligns with FIXES.md #4.
                ContentRedaction={
                    "RedactionType": "PII",
                    "RedactionOutput": "redacted",
                },
                Tags=[
                    {"Key": "sa_id", "Value": sa_id},
                    {"Key": "customer_id", "Value": customer_id},
                    {"Key": "lob_id", "Value": _m("lob_id") or "default"},
                    {"Key": "customer_display_name", "Value": _m("customer_display_name")},
                    {"Key": "lob_display_name", "Value": _m("lob_display_name")},
                    {"Key": "conn_id", "Value": conn_id},
                    {"Key": "source_key", "Value": key[-256:]},
                ],
            )
            print(f"[transcribe_trigger] started job={job_name} key={key}")
        except Exception as e:
            print(f"[transcribe_trigger] start_transcription_job failed: {e}")

    return {"statusCode": 200}
