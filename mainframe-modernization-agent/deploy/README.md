# `deploy/` — infra provisioning

Everything here is imperative boto3, not CDK/Terraform/CloudFormation.
Every script is idempotent (create-or-update — safe to rerun), but there
is no single bootstrap command. See the [root README's setup
walkthrough](../README.md#setting-this-up-yourself) for the order these
need to run in.

## Scripts, by concern

Every file here is active and used by the deployed system. (An earlier
pass through this folder found five superseded/broken scripts — a dead
duplicate MCP Lambda handler, two overlapping never-reconciled HTTP API
setup scripts, their paired proxy Lambda, and a written-but-never-wired
JWT session-binding helper — all removed. See git history if you need
the JWT auth starting point back; the gap it addressed is still tracked
in `ARCHITECTURE.md` §11.)

### Core agent runtime

The agent itself deploys via the `agentcore` CLI (`agentcore deploy --agent
MfModAgent`), reading config from [`../agentcore.toml`](../agentcore.toml).
Nothing in this folder deploys the runtime itself — these scripts wire
the infra *around* it.

### MCP tool Lambda

- **`mainframe_mcp_lambda.py`** — the Lambda handler. Reads its 11
  reference-data JSON files and `_helpers.py` from `mcp_server/` at
  package time (see next).
- **`package_mcp_lambda.py`** — builds the deployable zip (handler +
  copied `_helpers.py` + copied `mcp_server/data/*.json`) and optionally
  deploys it with `--deploy`. Run this any time `mcp_server/server.py`'s
  logic or data changes — the Lambda is a separate copy, not a live
  import, so nothing propagates automatically.
- **`gateway-lambda-policy.json`** — IAM policy for the Gateway's
  execution role to invoke the Lambda. **Check this matches your
  Lambda's actual function name before attaching** — it's been caught
  drifting from the real deployed function name before (see
  ARCHITECTURE.md's decision register).
- **`gateway-trust-policy.json`** — trust policy for the Gateway's
  service role (allows `bedrock-agentcore.amazonaws.com` to assume it).

### Gateway tool-schema sync

- **`gateway-tool-schema.json`** — single source of truth for what
  tools the Gateway advertises. See [`MCP_SCHEMA.md`](MCP_SCHEMA.md)
  for the full rationale.
- **`sync_gateway_schema.py`** — reconciles an *existing* Gateway
  target against the JSON file. Doesn't create the Gateway or target;
  run this after you've created both by hand (console or CLI) to push
  schema changes going forward.
  ```bash
  python deploy/sync_gateway_schema.py            # apply diff
  python deploy/sync_gateway_schema.py --check     # fail on drift, no changes (CI-friendly)
  ```

### Knowledge Base

- **`ingest_kb.py`** — triggers a Bedrock KB ingestion job. Doesn't
  create the KB or its data source (do that via console/CLI first).
  ```bash
  python deploy/ingest_kb.py --from ./your-docs-dir   # upload + re-ingest
  python deploy/ingest_kb.py --no-upload               # re-ingest only
  python deploy/ingest_kb.py --status                   # inspect, no changes
  ```

### WebSocket chat path (what's actually deployed)

- **`setup_websocket.py`** — creates the WebSocket API Gateway, the
  `MfModAgent-WsConnections` DynamoDB table, and the relay Lambda
  (`MfModAgent-WsHandler`). Set `AGENT_RUNTIME_ARN` (env var) before
  running, or patch the deployed Lambda's env var afterward. Also set
  `MFMOD_API_KEY` — this becomes the shared key the frontend and every
  WS request must present.
- **`ws_lambda.py`** — the relay Lambda's actual handler code (thin —
  forwards every SSE event from AgentCore to the WebSocket client
  1:1, no independent Bedrock calls).
- **`setup_cloudfront.py`** — fronts the chat UI's S3 bucket with
  CloudFront. Needed because live audio capture
  (`getUserMedia`) requires a secure context, and plain S3 website
  hosting is HTTP-only.

### Recordings pipeline (optional — "listen mode" via audio upload)

Independent of the text-chat path; skip entirely if you only want typed
chat.

- **`setup_recordings_pipeline.py`** — provisions the S3 recordings
  bucket (KMS-encrypted, 30-day lifecycle), upload Lambda, Transcribe
  trigger/complete Lambdas, and the HTTP API route for presigned
  uploads. One script, several resources — read the module docstring
  for the full list before running.
- **`upload_lambda.py`**, **`transcribe_trigger_lambda.py`**,
  **`transcribe_complete_lambda.py`** — the three Lambda handlers that
  script provisions.

### Pricing MCP Runtime (optional)

See [`pricing_mcp_runtime/`](pricing_mcp_runtime/) — deploys as its own
AgentCore Runtime, registered as a Gateway `mcpServer` target. Deploy
with `agentcore deploy --agent AwsPricingMcp` from the repo root (config
in the second `agents:` entry of your `.bedrock_agentcore.yaml`, which is
gitignored — you'll need to create/configure this yourself per the
`agentcore` CLI's conventions).

### Reference / config

- **`dynamodb_tables.json`** — **documentation, not a script.** Describes
  the key schema, TTL config, and access patterns for both DynamoDB
  tables this system uses. Create the tables yourself (CLI, console, or
  your own script) matching this spec.
- **`ws_config.json`** — output/reference file for the WebSocket path.
  `api_key` field is a placeholder — the real key is injected at deploy
  time via the `MFMOD_API_KEY` env var, never committed.
- **`chat.html`** — a minimal standalone HTML test harness for the WS
  API, independent of the full `frontend/` SPA. Useful for a quick
  connectivity check without the whole UI.
- **`MCP_SCHEMA.md`** — the "why" behind treating `gateway-tool-schema.json`
  as authoritative over direct Gateway API edits.

## Environment variables this folder's scripts read

| Var | Used by | Notes |
|---|---|---|
| `AGENT_RUNTIME_ARN` | `ws_lambda.py` | ARN of the deployed AgentCore Runtime. |
| `MFMOD_API_KEY` | `setup_websocket.py`, `setup_recordings_pipeline.py` | Shared secret gating WS + upload requests. Generate your own (e.g. `openssl rand -base64 32`) — do not reuse an example value. This is the *only* auth gate today — there is no per-SA identity check. See ARCHITECTURE.md §11 for the tracked Cognito JWT plan. |
| `CUSTOMER_MEMORY_TABLE` | (read by `agent/memory.py`, not this folder, but the table name must match) | Defaults to `MfModAgent-CustomerMemory`. |
| `RECORDINGS_BUCKET`, `RECORDINGS_KMS_KEY_ARN` | recordings pipeline scripts | Set if you customize bucket/key naming. |

All scripts default `REGION` to `us-east-1`. Account ID handling is
inconsistent across the folder: `setup_cloudfront.py` and
`setup_recordings_pipeline.py` derive it live via
`boto3.client("sts").get_caller_identity()`; `setup_websocket.py` has a
hardcoded `ACCOUNT_ID` constant near the top of the file (redacted to
`<ACCOUNT_ID>` in this repo) — **set that constant to your own account ID
before running it.**
