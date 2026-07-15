"""Build and (optionally) deploy the MfModAgent-MainframeMCP Lambda zip.

The deployed Gateway-target Lambda imports `from _helpers import ...` and
calls `load_data("<file>.json")` for ~11 reference data files. Neither
deploy/_helpers.py nor deploy/data/ exists — the helpers and data live
under mcp_server/. Until this script existed, the Lambda artifact was
hand-assembled by zipping those files manually, so the deployed artifact
could not be reproduced from the repo. This is FIXES.md #5.

What this script does:
    1. Build a temp zip with the Lambda handler + a copy of mcp_server/
       _helpers.py at the zip root + every JSON under mcp_server/data/.
    2. With --deploy, call update_function_code on MfModAgent-MainframeMCP
       in the Bedrock account (<ACCOUNT_ID> / us-east-1).

Usage:
    # Build only (writes ./build/mainframe_mcp.zip, prints contents)
    python deploy/package_mcp_lambda.py

    # Build + deploy (state change — needs explicit flag)
    AWS_PROFILE=bedrock-agentcore python deploy/package_mcp_lambda.py --deploy

    # Build + deploy + run smoke invoke afterward
    AWS_PROFILE=bedrock-agentcore python deploy/package_mcp_lambda.py --deploy --smoke
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT     = Path(__file__).resolve().parent.parent
DEPLOY_DIR    = REPO_ROOT / "deploy"
MCP_SRC_DIR   = REPO_ROOT / "mcp_server"
DATA_DIR      = MCP_SRC_DIR / "data"
HELPERS_PATH  = MCP_SRC_DIR / "_helpers.py"
LAMBDA_HANDLER_PATH = DEPLOY_DIR / "mainframe_mcp_lambda.py"

BUILD_DIR     = REPO_ROOT / "build"
ZIP_PATH      = BUILD_DIR / "mainframe_mcp.zip"

LAMBDA_NAME   = "MfModAgent-MainframeMCP"
REGION        = "us-east-1"


def _expected_files() -> list[Path]:
    """Sources we package. Fail loudly if any expected file is missing —
    a silently-truncated zip would deploy and fail at cold-start with an
    obscure ImportError or FileNotFoundError."""
    files: list[Path] = [LAMBDA_HANDLER_PATH, HELPERS_PATH]
    if not DATA_DIR.is_dir():
        raise SystemExit(f"data directory missing: {DATA_DIR}")
    files.extend(sorted(DATA_DIR.glob("*.json")))
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise SystemExit(f"missing source file(s): {missing}")
    return files


def build_zip() -> Path:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    files = _expected_files()

    print(f"Packaging {LAMBDA_NAME}…")
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        # Handler at zip root — Lambda config has Handler=mainframe_mcp_lambda.lambda_handler
        z.write(LAMBDA_HANDLER_PATH, arcname="mainframe_mcp_lambda.py")
        # Helpers at zip root — matches the `from _helpers import ...` form in the handler
        z.write(HELPERS_PATH, arcname="_helpers.py")
        # Data files under data/ — _helpers.load_data resolves DATA_DIR = Path(__file__).parent/"data"
        for json_path in sorted(DATA_DIR.glob("*.json")):
            z.write(json_path, arcname=f"data/{json_path.name}")

    size_kb = ZIP_PATH.stat().st_size / 1024
    print(f"  → {ZIP_PATH} ({size_kb:.1f} KB)")

    # Print contents for traceability — so a CI run shows exactly what was packaged
    with zipfile.ZipFile(ZIP_PATH) as z:
        names = sorted(z.namelist())
    print(f"  Contents ({len(names)} files):")
    for n in names:
        print(f"    {n}")
    return ZIP_PATH


def deploy_zip(zip_path: Path) -> str:
    """Push the zip via update_function_code. Returns the new function version."""
    import boto3
    client = boto3.client("lambda", region_name=REGION)

    print(f"\nDeploying to {LAMBDA_NAME} ({REGION})…")
    with zip_path.open("rb") as f:
        zip_bytes = f.read()

    resp = client.update_function_code(
        FunctionName=LAMBDA_NAME,
        ZipFile=zip_bytes,
        Publish=False,  # we update $LATEST in place; aliases/versions are out of scope here
    )
    state = resp.get("State") or "?"
    last_status = resp.get("LastUpdateStatus") or "?"
    sha = (resp.get("CodeSha256") or "")[:16]
    print(f"  state={state} last_update_status={last_status} sha256={sha}…")

    # Wait for the function to finish updating before we let smoke fire
    waiter = client.get_waiter("function_updated_v2")
    print("  waiting for function_updated_v2…")
    waiter.wait(FunctionName=LAMBDA_NAME)
    print("  function ready.")
    return resp.get("Version", "$LATEST")


def smoke_invoke() -> None:
    """Quick sanity ping — list_taxonomy is argless and cheap."""
    import boto3
    client = boto3.client("lambda", region_name=REGION)

    payload = {
        "name": "list_taxonomy",
        "arguments": {},
    }
    print("\nSmoke: list_taxonomy()…")
    resp = client.invoke(
        FunctionName=LAMBDA_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = resp["Payload"].read().decode("utf-8")
    parsed = json.loads(body)
    is_error = parsed.get("isError", False)
    print(f"  isError={is_error}")
    if is_error:
        print(f"  body: {body[:600]}")
        raise SystemExit("smoke invoke FAILED — Lambda returned isError")
    # Print first 300 chars of the first text content so we see something sensible
    content = parsed.get("content", [])
    first_text = next((c.get("text", "") for c in content if c.get("type") == "text"), "")
    print(f"  first content text (300 chars): {first_text[:300]}")
    print("  smoke OK.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build / deploy the MfModAgent-MainframeMCP Lambda")
    ap.add_argument("--deploy", action="store_true",
                    help="After building, push to AWS via update_function_code. "
                         "Without this flag, only the zip is built (no AWS calls).")
    ap.add_argument("--smoke", action="store_true",
                    help="After deploy, invoke list_taxonomy() to confirm the new code runs. "
                         "Implies --deploy.")
    args = ap.parse_args()

    zip_path = build_zip()

    if args.smoke and not args.deploy:
        args.deploy = True

    if args.deploy:
        deploy_zip(zip_path)
        if args.smoke:
            smoke_invoke()
    else:
        print("\n(Build only. Pass --deploy to push to AWS.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
