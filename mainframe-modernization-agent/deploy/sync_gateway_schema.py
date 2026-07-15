"""Reconcile the AgentCore Gateway target with `gateway-tool-schema.json`.

This script is the deploy step for tool-surface changes. The flow is:

    1. Edit `deploy/gateway-tool-schema.json` (add a tool, change a description,
       update an inputSchema field).
    2. Run `python -m deploy.sync_gateway_schema` from the repo root, OR
       `python deploy/sync_gateway_schema.py` from anywhere.
    3. The script computes a diff between the file and the live Gateway target.
       If they match, it's a no-op and exits 0. If they differ, it calls
       `update_gateway_target` and waits for status=READY.

Idempotent — safe to run repeatedly. Failure modes report a non-zero exit.

Why this script exists:
    The Gateway target lives in AWS state; the JSON file lives in the repo.
    Without a reconciler, the two can drift (which is exactly what happened
    during Iteration 1.5: someone called update_gateway_target via the API
    but never updated the file). This script makes the file the single
    source of truth — every Gateway change MUST go through `git`.

Run with `--check` to fail (non-zero) on any drift without applying changes.
Use that mode in CI to guard against direct API edits.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

REGION       = "us-east-1"
GATEWAY_ID   = "mfmodagent-gateway-<GATEWAY_ID>"
TARGET_ID    = "<MAINFRAME_TARGET_ID>"
LAMBDA_ARN   = "arn:aws:lambda:us-east-1:<ACCOUNT_ID>:function:MfModAgent-MainframeMCP"
SCHEMA_FILE  = Path(__file__).parent / "gateway-tool-schema.json"


def load_file_schema() -> list[dict]:
    with SCHEMA_FILE.open() as f:
        schema = json.load(f)
    if not isinstance(schema, list):
        raise SystemExit(f"{SCHEMA_FILE} must be a JSON array of tool schemas")
    return schema


def load_live_schema(client) -> tuple[list[dict], dict]:
    """Returns (tools_list, full_target_response). The latter is needed
    when calling update so we can preserve fields we didn't intend to change
    (target name, credential providers, etc.)."""
    target = client.get_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        targetId=TARGET_ID,
    )
    inline = (
        target["targetConfiguration"]["mcp"]["lambda"]["toolSchema"]["inlinePayload"]
    )
    return inline, target


def normalize(schema: list[dict]) -> list[dict]:
    """Sort by name + recursively sort dict keys so equality is order-independent."""
    def _sort(obj):
        if isinstance(obj, dict):
            return {k: _sort(obj[k]) for k in sorted(obj)}
        if isinstance(obj, list):
            return [_sort(x) for x in obj]
        return obj
    return [_sort(t) for t in sorted(schema, key=lambda t: t.get("name", ""))]


def diff_summary(file_schema: list[dict], live_schema: list[dict]) -> list[str]:
    """Human-readable diff lines. Empty list means no drift."""
    file_by_name = {t["name"]: t for t in file_schema}
    live_by_name = {t["name"]: t for t in live_schema}

    lines: list[str] = []
    only_file = sorted(set(file_by_name) - set(live_by_name))
    only_live = sorted(set(live_by_name) - set(file_by_name))
    common = sorted(set(file_by_name) & set(live_by_name))

    for n in only_file:
        lines.append(f"  + {n}  (in file, not live — will be ADDED)")
    for n in only_live:
        lines.append(f"  - {n}  (live, not in file — will be REMOVED)")
    for n in common:
        f, l = normalize([file_by_name[n]])[0], normalize([live_by_name[n]])[0]
        if f != l:
            lines.append(f"  ~ {n}  (drift in description / inputSchema — will be UPDATED)")
    return lines


def apply_update(client, file_schema: list[dict], live_target: dict) -> str:
    """Call update_gateway_target and wait for READY. Returns final status."""
    resp = client.update_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        targetId=TARGET_ID,
        name=live_target["name"],
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": LAMBDA_ARN,
                    "toolSchema": {"inlinePayload": file_schema},
                }
            }
        },
        credentialProviderConfigurations=live_target["credentialProviderConfigurations"],
    )
    print(f"  update_gateway_target → status={resp['status']}, waiting for READY…")
    for _ in range(30):
        cur = client.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=TARGET_ID)
        if cur["status"] == "READY":
            return "READY"
        if cur["status"] in ("UPDATE_FAILED", "DELETE_FAILED"):
            raise SystemExit(f"Update failed: {cur.get('statusReasons')}")
        time.sleep(2)
    raise SystemExit("Timed out waiting for READY (>60s)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconcile Gateway target with schema file")
    ap.add_argument(
        "--check", action="store_true",
        help="Only print the diff. Exit non-zero on drift, zero on clean. "
             "Use in CI to guard against direct API edits.",
    )
    args = ap.parse_args()

    file_schema = load_file_schema()
    print(f"File:  {SCHEMA_FILE}  ({len(file_schema)} tools)")

    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    live_schema, live_target = load_live_schema(client)
    print(f"Live:  Gateway={GATEWAY_ID} Target={TARGET_ID}  ({len(live_schema)} tools)")

    diff = diff_summary(file_schema, live_schema)

    if not diff:
        print("No drift. File and Gateway target match.")
        return 0

    print("Drift detected:")
    for line in diff:
        print(line)

    if args.check:
        print("\n--check mode: not applying. Exiting non-zero.")
        return 1

    print("\nApplying update…")
    final_status = apply_update(client, file_schema, live_target)
    print(f"Done. Final status: {final_status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
