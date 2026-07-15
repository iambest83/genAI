"""Re-ingest the mainframe-modernization KB from its S3 source bucket.

Why this script exists: dropping a doc into the S3 bucket does NOT auto-ingest
into the KB. Bedrock Knowledge Bases require an explicit `start_ingestion_job`
call against the data source. Without a script, that's a manual console click —
easy to forget, and the agent's KB silently lags reality. Failure mode #4 in
the "KB chunking" review.

What this script does:
    1. Optionally sync local files from a path to s3://<KB_BUCKET>/docs/ (skip
       with --no-upload if files are already in S3).
    2. Start a Bedrock `start_ingestion_job` against the KB's data source.
    3. Poll until the job is COMPLETE or FAILED (typical: 1-3 min for a
       small corpus).
    4. Print per-doc ingestion stats so you can spot doc-level failures.

Usage:
    # Full flow: upload from local dir + re-ingest
    AWS_PROFILE=bedrock-agentcore python deploy/ingest_kb.py --from ./kb-docs

    # Re-ingest only (files already in S3)
    AWS_PROFILE=bedrock-agentcore python deploy/ingest_kb.py --no-upload

    # Inspect the current data source config without doing anything
    AWS_PROFILE=bedrock-agentcore python deploy/ingest_kb.py --status
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import boto3

REGION         = "us-east-1"
KB_ID          = "<KB_ID>"
DATA_SOURCE_ID = "<KB_DATASOURCE_ID>"
KB_BUCKET      = "mainframe-modernization-kb-<ACCOUNT_ID>"
S3_PREFIX      = "docs/"

POLL_INTERVAL_S = 10
POLL_TIMEOUT_S  = 600   # 10-min ceiling; small corpora finish in ~1-3 min


# ---------------------------------------------------------------------------
# S3 upload
# ---------------------------------------------------------------------------

def upload_local_dir(source_path: Path) -> int:
    """Upload every file under source_path to s3://KB_BUCKET/docs/<filename>.

    Flattens directories — the KB chunker doesn't care about S3 prefixes
    beyond the inclusionPrefix; metadata mappings can carry source path
    later if useful. Returns the count of files uploaded.
    """
    if not source_path.is_dir():
        raise SystemExit(f"--from path is not a directory: {source_path}")

    s3 = boto3.client("s3", region_name=REGION)
    files = [p for p in source_path.rglob("*") if p.is_file() and not p.name.startswith(".")]
    if not files:
        print(f"  (no files under {source_path} — nothing to upload)")
        return 0

    print(f"Uploading {len(files)} file(s) from {source_path} to s3://{KB_BUCKET}/{S3_PREFIX}…")
    for p in sorted(files):
        key = f"{S3_PREFIX}{p.name}"
        size_kb = p.stat().st_size / 1024
        print(f"  → {key} ({size_kb:.1f} KB)")
        s3.upload_file(str(p), KB_BUCKET, key)
    print(f"  uploaded {len(files)} file(s).")
    return len(files)


# ---------------------------------------------------------------------------
# Ingestion job
# ---------------------------------------------------------------------------

def start_and_wait() -> dict:
    """Kick off start_ingestion_job and poll until terminal. Returns the
    final ingestion job description."""
    client = boto3.client("bedrock-agent", region_name=REGION)

    print(f"\nStarting ingestion job on KB={KB_ID} dataSource={DATA_SOURCE_ID}…")
    resp = client.start_ingestion_job(
        knowledgeBaseId=KB_ID,
        dataSourceId=DATA_SOURCE_ID,
        description=f"Triggered by deploy/ingest_kb.py at {int(time.time())}",
    )
    job = resp["ingestionJob"]
    job_id = job["ingestionJobId"]
    print(f"  jobId={job_id} status={job['status']}")

    deadline = time.time() + POLL_TIMEOUT_S
    last_status = job["status"]
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        cur = client.get_ingestion_job(
            knowledgeBaseId=KB_ID,
            dataSourceId=DATA_SOURCE_ID,
            ingestionJobId=job_id,
        )["ingestionJob"]
        status = cur["status"]
        if status != last_status:
            print(f"  status: {last_status} → {status}")
            last_status = status
        if status in ("COMPLETE", "FAILED", "STOPPED"):
            return cur
    raise SystemExit(f"Ingestion job {job_id} did not terminate within {POLL_TIMEOUT_S}s")


def print_job_summary(job: dict) -> None:
    stats = job.get("statistics") or {}
    print("\nIngestion job summary:")
    print(f"  status                   : {job['status']}")
    print(f"  documents scanned        : {stats.get('numberOfDocumentsScanned', '?')}")
    print(f"  new documents indexed    : {stats.get('numberOfNewDocumentsIndexed', '?')}")
    print(f"  modified docs re-indexed : {stats.get('numberOfModifiedDocumentsIndexed', '?')}")
    print(f"  documents deleted        : {stats.get('numberOfDocumentsDeleted', '?')}")
    print(f"  documents failed         : {stats.get('numberOfDocumentsFailed', '?')}")
    print(f"  metadata docs modified   : {stats.get('numberOfMetadataDocumentsModified', '?')}")
    failure_reasons = job.get("failureReasons") or []
    if failure_reasons:
        print("\n  failureReasons:")
        for fr in failure_reasons[:10]:
            print(f"    - {fr}")


# ---------------------------------------------------------------------------
# Status-only inspection
# ---------------------------------------------------------------------------

def print_status() -> None:
    """Print KB + data source config + the last 5 ingestion jobs."""
    client = boto3.client("bedrock-agent", region_name=REGION)
    ds = client.get_data_source(
        knowledgeBaseId=KB_ID, dataSourceId=DATA_SOURCE_ID,
    )["dataSource"]
    cfg = ds.get("vectorIngestionConfiguration", {}).get("chunkingConfiguration", {})

    print(f"KB:           {KB_ID}")
    print(f"data source:  {DATA_SOURCE_ID}  ({ds.get('name')})  status={ds.get('status')}")
    print(f"S3 bucket:    {KB_BUCKET}  prefix={S3_PREFIX!r}")
    print(f"chunking:     strategy={cfg.get('chunkingStrategy')} "
          f"maxTokens={cfg.get('fixedSizeChunkingConfiguration', {}).get('maxTokens')} "
          f"overlap%={cfg.get('fixedSizeChunkingConfiguration', {}).get('overlapPercentage')}")

    jobs = client.list_ingestion_jobs(
        knowledgeBaseId=KB_ID, dataSourceId=DATA_SOURCE_ID, maxResults=5,
    ).get("ingestionJobSummaries", [])
    print(f"\nlast {len(jobs)} ingestion job(s):")
    for j in jobs:
        stats = j.get("statistics") or {}
        scanned = stats.get("numberOfDocumentsScanned", "?")
        failed  = stats.get("numberOfDocumentsFailed", "?")
        started = str(j.get("startedAt", ""))[:19]  # YYYY-MM-DD HH:MM:SS
        print(f"  {started:<22} {j['status']:<10} scanned={scanned} failed={failed}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Re-ingest the mainframe-modernization KB")
    ap.add_argument("--from", dest="src", type=Path,
                    help="Local directory whose files should be uploaded to S3 before re-ingest")
    ap.add_argument("--no-upload", action="store_true",
                    help="Skip the S3 upload step (files are already in the bucket).")
    ap.add_argument("--status", action="store_true",
                    help="Print KB + data source + last ingestion jobs and exit. No state change.")
    args = ap.parse_args()

    if args.status:
        print_status()
        return 0

    if args.src and not args.no_upload:
        upload_local_dir(args.src)
    elif not args.src and not args.no_upload:
        print("(no --from given; pass --no-upload if files are already in S3, "
              "or --from <dir> to upload first)")
        return 1

    job = start_and_wait()
    print_job_summary(job)
    return 0 if job["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
