# Mainframe Modernization Agent

An AI agent that helps AWS Solutions Architects run mainframe modernization
conversations with Financial Services customers. It combines a Bedrock
Knowledge Base, MCP-based tool calling, and per-customer persistent memory
behind a LangGraph orchestration flow, deployed on Amazon Bedrock AgentCore.

This repo is the actual code for a system that has been deployed and used
in real SA conversations — not a demo or a template. Resource identifiers
(account IDs, ARNs, endpoint URLs) have been replaced with `<PLACEHOLDER>`
tokens; see [Setting this up yourself](#setting-this-up-yourself) below.

## What it does

An SA opens a chat, optionally binds a customer + line of business, and
talks through a modernization engagement. The agent:

- **Remembers the customer.** Facts and decisions the SA states are
  extracted and persisted per (SA, customer, line-of-business) in
  DynamoDB — workload size, regulatory posture, target date, chosen
  pattern, etc. Every later turn is grounded in that profile.
- **Retrieves from a curated Knowledge Base** — AWS mainframe
  modernization blogs, partner research, FSI compliance guidance —
  reranked with Cohere Rerank 3.5 for precision.
- **Calls tools via MCP** — a Lambda-backed tool server (migration
  pattern lookup, complexity scoring, partner comparison, compliance
  checklists) plus AWS-managed MCP servers (AWS docs search, web search,
  live AWS pricing) — all wired as Gateway targets under one MCP
  namespace.
- **Generates paste-ready artifacts** — a wave plan (CSV), target-state
  architecture (Mermaid), risk register, and directional TCO estimate —
  but only when the customer profile actually has enough signal to make
  the artifact defensible. An empty profile gets "I can draft this once
  I know X," not a fabricated deliverable.
- **Probes for gaps.** Every substantive reply that isn't already
  information-dense ends with one targeted clarifying question, phrased
  against what's already known.

## Repository layout

| Path | What's in it |
|---|---|
| [`agent/`](agent/) | LangGraph orchestration — routing, retrieval, memory, artifact generation, the response prompt. This is the core of the system. |
| [`mcp_server/`](mcp_server/) | Mainframe-modernization MCP tools — source of truth for tool logic, runnable standalone (Claude Desktop) or as the Lambda deploy source. |
| [`deploy/`](deploy/) | Infra provisioning scripts (boto3, imperative, idempotent) for the WebSocket API, recordings pipeline, Gateway sync, KB ingestion. |
| [`frontend/`](frontend/) | Vanilla-JS chat UI — no build step, no framework. |
| [`evals/`](evals/) | Offline regression eval harness — dataset + hybrid judge (deterministic checks + Sonnet-as-judge) + baseline. |
| [`diagrams/`](diagrams/) | draw.io architecture diagram (6 pages: E2E overview, LangGraph flow, memory, retrieval, listen mode, decisions). |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The full design document — read this for the *why* behind every decision. Longer and more detailed than any README here. |

Each subfolder has its own README with setup/run instructions specific to
that piece. Start there once you know which part you're working on.

## Architecture at a glance

```
Browser (chat UI)
   │  wss://
   ▼
WebSocket API Gateway → WS Lambda (thin SSE relay)
   │  invoke_agent_runtime
   ▼
Bedrock AgentCore Runtime
   │
   ▼
LangGraph:  profile_loader → drift_guard → router
              → [direct | help | summary | defer | ack]
              → [kb_node ∥ mcp_node]  (parallel)
              → artifact_node (phase-gated)
              → response_generator (Sonnet, streamed)
              → profile_updater (extract facts, persist)
   │                              │
   ▼                              ▼
Bedrock Knowledge Base      AgentCore Gateway (MCP)
(Titan v2 + Cohere Rerank)    ├── MainframeMCP (Lambda)
                              ├── AWSMCP (AWS-managed)
                              ├── WebSearchMCP (AWS-managed)
                              └── AwsPricingMCP (own runtime)
                                     │
                                     ▼
                              DynamoDB (per-customer memory,
                              optimistic-locked profiles +
                              90-day turn-event audit log)
```

See [ARCHITECTURE.md §2](ARCHITECTURE.md#2-system-topology) for the
annotated version and [`diagrams/architecture.drawio`](diagrams/architecture.drawio)
for the visual.

## Setting this up yourself

**Read this section fully before starting — this is not a one-command
deploy.** The repo is the *code*, not a packaged product; provisioning
the AWS resources it depends on takes deliberate, sequential setup. If
you just want to read the code and understand the design, you don't need
any of this — go read [ARCHITECTURE.md](ARCHITECTURE.md) instead.

### What you need

- An AWS account with Bedrock model access enabled for Claude (Sonnet +
  Haiku) in your target region, and access to Amazon Bedrock AgentCore
  (Runtime + Gateway).
- Python 3.13, `uv` or `pip`.
- The [`agentcore` CLI](https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html)
  for deploying to AgentCore Runtime.
- Node is NOT required — the frontend is vanilla JS.

### Order of operations

1. **Knowledge Base** — create a Bedrock Knowledge Base yourself via the
   AWS console (this repo does not script KB creation, only re-ingestion
   into an existing one). Vector store: OpenSearch Serverless. Embeddings:
   Titan Text v2. Chunking: FIXED_SIZE, 512 tokens, 20% overlap. Once it
   exists, populate it with your own mainframe-modernization source
   documents (blogs, partner decks, compliance guides — whatever's
   relevant to your use case) and use [`deploy/ingest_kb.py`](deploy/ingest_kb.py)
   to trigger ingestion jobs going forward.

2. **DynamoDB tables** — create the two tables described in
   [`deploy/dynamodb_tables.json`](deploy/dynamodb_tables.json). This
   file is documentation, not an executable script — run
   `aws dynamodb create-table` yourself (or write a script) using the
   key schema, TTL, and billing mode it specifies.

3. **MCP tool Lambda** — package and deploy
   [`deploy/mainframe_mcp_lambda.py`](deploy/mainframe_mcp_lambda.py)
   (see [`deploy/package_mcp_lambda.py`](deploy/package_mcp_lambda.py))
   as a Lambda function. Note its ARN.

4. **AgentCore Gateway** — create a Gateway and register targets:
   your MCP Lambda from step 3, plus (optionally) the AWS-managed
   `aws-mcp` and web-search MCP servers as additional targets. Use
   [`deploy/gateway-tool-schema.json`](deploy/gateway-tool-schema.json)
   + [`deploy/sync_gateway_schema.py`](deploy/sync_gateway_schema.py)
   to keep the Gateway's advertised tool schema in sync with the file
   going forward — the script reconciles an *existing* target, it
   doesn't create the Gateway itself.

5. **AgentCore Runtime (main agent)** — set the environment variables
   in [`agentcore.toml`](agentcore.toml) (`KB_ID`, `GATEWAY_URL`, model
   IDs) to point at what you built in steps 1 and 4, then deploy:
   ```bash
   agentcore deploy --agent MfModAgent
   ```
   Note the resulting runtime ARN.

6. **(Optional) Pricing MCP Runtime** — if you want live AWS pricing as
   a tool, deploy [`deploy/pricing_mcp_runtime/`](deploy/pricing_mcp_runtime/)
   as a second AgentCore Runtime and register it as a Gateway target.
   See [ARCHITECTURE.md §8](ARCHITECTURE.md#8-mcp-tools-transport-and-the-gateway)
   for why this one runs as its own runtime instead of a Gateway-native
   `mcpServer` target.

7. **WebSocket API + relay Lambda** — run
   [`deploy/setup_websocket.py`](deploy/setup_websocket.py). It creates
   the WebSocket API, the connections table, and the relay Lambda, and
   is idempotent (safe to rerun). Set `AGENT_RUNTIME_ARN` to the ARN
   from step 5 before running, or update the deployed Lambda's env var
   afterward. Note the resulting `wss://` URL and generate/set an API
   key (see [`deploy/README.md`](deploy/README.md) for the exact
   env vars).

8. **Chat UI** — edit two lines in
   [`frontend/app.js`](frontend/app.js): `WS_URL` (from step 7) and
   `API_KEY`. Host the `frontend/` folder as a static site (S3 +
   CloudFront is what the deploy scripts assume — see
   [`deploy/setup_cloudfront.py`](deploy/setup_cloudfront.py) — but any
   static host works).

9. **(Optional) Recordings pipeline** — if you want the "paste meeting
   notes" / audio-upload listen mode, run
   [`deploy/setup_recordings_pipeline.py`](deploy/setup_recordings_pipeline.py).
   Independent of everything else; skip it if you only want text chat.

10. **Verify** — run the eval suite against your deployed runtime:
    ```bash
    python evals/run_agentcore.py --runtime-arn <your-arn-from-step-5>
    ```
    See [`evals/README.md`](evals/README.md) for the full eval workflow.

### The honest caveats

- Steps 1 and 4 (KB and Gateway *creation*, as opposed to update) are
  console/CLI work this repo does not automate. Everything else is
  scripted and idempotent.
- There is no CDK/Terraform/CloudFormation. All infra is imperative
  boto3 with create-or-update idempotency. See
  [`ARCHITECTURE.md §11`](ARCHITECTURE.md#11-open-decisions-deferrals)
  for the tracked decision to defer IaC.

## Local development (no AWS deploy required)

You can run the LangGraph agent locally against real AWS services (KB,
Bedrock models, Gateway) without deploying to AgentCore Runtime, as long
as your local AWS credentials have the right permissions:

```bash
pip install -r requirements.txt
export KB_ID=<your-kb-id>
export GATEWAY_URL=<your-gateway-url>
export AWS_REGION=us-east-1
python test_agent.py
```

See [`agent/README.md`](agent/README.md) for what each node does and
[`mcp_server/README.md`](mcp_server/README.md) for running the MCP tools
completely standalone (no AWS calls at all) inside Claude Desktop.

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — full design doc: every node,
  every decision, and the reasoning behind it. Read this before making
  non-trivial changes.
- [`agent/README.md`](agent/README.md), [`mcp_server/README.md`](mcp_server/README.md),
  [`deploy/README.md`](deploy/README.md), [`frontend/README.md`](frontend/README.md),
  [`evals/README.md`](evals/README.md), [`diagrams/README.md`](diagrams/README.md) —
  per-folder specifics.
