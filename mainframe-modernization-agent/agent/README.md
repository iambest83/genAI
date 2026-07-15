# `agent/` — LangGraph orchestration

This is the core of the system: a LangGraph `StateGraph` that takes one SA
message, decides how to answer it, and updates persistent per-customer
memory. Everything else in this repo (deploy scripts, frontend, evals)
exists to run or test this package.

Read [ARCHITECTURE.md](../ARCHITECTURE.md) first for the full design
rationale — this README is a map of the files, not a replacement for that
document.

## Entry points

| File | Use case |
|---|---|
| [`main.py`](main.py) | Interactive CLI loop (`build_graph()`) — quickest way to talk to the agent locally. |
| [`graph.py`](graph.py) `get_graph()` | Used by [`agentcore_app.py`](../agentcore_app.py) (the deployed streaming entrypoint) and [`../test_agent.py`](../test_agent.py). |

Both build the same graph; `main.py` uses a slightly different assembly
path (`build_graph`) for synchronous CLI use, `get_graph` is what the
async streaming entrypoint and tests use.

## The graph, node by node

```
profile_loader → drift_guard → router_node
                                    │
        ┌───────────────┬──────────┼──────────┬─────────────┐
        ▼               ▼          ▼          ▼             ▼
     direct           help      summary     defer       [kb ∥ mcp]
        │               │          │          │              │
        └───────────────┴──────────┴──────────┴──────┐       ▼
                                                       │  artifact_node
                                                       ▼       │
                                              response_generator ◄┘
                                                       │
                                                       ▼
                                              profile_updater → END
```

| Node | File | What it does |
|---|---|---|
| `profile_loader_node` | [`nodes_memory.py`](nodes_memory.py) | Loads the `CustomerProfile` for `(sa_id, customer_id, lob_id)` from DynamoDB. If no LoB is bound, also loads sibling LoB profiles for customer-wide questions. |
| `drift_guard_node` | [`nodes_memory.py`](nodes_memory.py) | Checks whether the SA's message names a different customer than the one currently bound. Currently disabled (see ARCHITECTURE.md §11 for why) — the node exists and runs but its detection logic is inert. |
| `router_node` | [`router.py`](router.py) | Hybrid keyword + LLM (Haiku) routing. Decides: `direct` / `help` / `summary` / `defer` / `acknowledge` / `kb` / `mcp` / `both`, and which MCP tools to call. |
| `kb_node` | [`nodes.py`](nodes.py) | Two-stage retrieval against the Bedrock Knowledge Base: vector ANN (top 20) → Cohere Rerank 3.5 → score-floor filter → top 5. |
| `mcp_node` | [`nodes.py`](nodes.py) | Calls MCP tools via the AgentCore Gateway (SigV4-signed JSON-RPC), in parallel via a bounded thread pool. |
| `artifact_node` | [`nodes_artifacts.py`](nodes_artifacts.py) | Deterministic (no-LLM) generation of a wave plan (CSV), target architecture (Mermaid), risk register, or TCO estimate — gated by whether the customer profile actually has the inputs to make the artifact defensible. |
| `response_generator` | [`nodes.py`](nodes.py) + [`prompts.py`](prompts.py) | The one LLM call that produces the SA-facing reply (Sonnet, streamed). All prompt construction lives in `prompts.py` — this is the single source of the system prompt. |
| `profile_updater_node` | [`nodes_memory.py`](nodes_memory.py) | Extracts new facts/decisions from the SA's message (quote-grounded — nothing is extracted without a verbatim quote backing it), detects contradictions with the existing profile, and persists via optimistic locking. |

Two additional sub-graphs branch off `profile_loader` for non-chat payload
kinds — see [`nodes_listen.py`](nodes_listen.py):
`meeting_notes_node` (paste meeting notes → structured preview, no merge)
and `meeting_merge_node` (apply an SA-confirmed subset of a prior preview).

## Supporting modules

| File | Purpose |
|---|---|
| [`state.py`](state.py) | The `AgentState` TypedDict — every field that flows through the graph. Read this to understand what any node can read/write. |
| [`customer_profile.py`](customer_profile.py) | `CustomerProfile` dataclass (`Workload`, `Constraints`, `Decisions`, `Facts`) plus `derive_phase()` — the engagement-phase heuristic (discovery → assessment → recommendation → proposal → execution) that shapes prompt behavior. |
| [`memory.py`](memory.py) | DynamoDB persistence for `CustomerProfile` — load, optimistic-locked upsert, and the append-only turn-event audit log (90-day TTL). See ARCHITECTURE.md §6 for the schema. |
| [`config.py`](config.py) | All environment-variable-driven config: model IDs, KB retrieval tuning (`KB_NUM_RESULTS`, `KB_SCORE_FLOOR`, rerank on/off), Gateway URL. |
| [`intent.py`](intent.py) | Five-way intent classification (`substantive` / `defer` / `acknowledge` / `meta_help` / `meta_summary` / `chat`) that the router uses before deciding retrieval strategy. |
| [`prompts.py`](prompts.py) | `build_response_prompt()` — the single system prompt, phase-aware framing, untrusted-data sentinel fencing (Rule 0), URL-fabrication allowlist (Rule 2), probe-suppression logic. |
| [`pricing_inference.py`](pricing_inference.py) | Regex-based extraction of AWS pricing filters (instance type, region, service) from the SA's message, so the router can skip a discovery round-trip when the query is unambiguous. |
| [`utils.py`](utils.py) | Shared helpers (e.g. the allowed-regulation-token list used by the fact extractor). |

## Running it locally

Requires AWS credentials with Bedrock model invoke access, Bedrock KB
retrieve access, and DynamoDB access to the customer-memory table (see
[`../deploy/dynamodb_tables.json`](../deploy/dynamodb_tables.json) for the
schema). If DynamoDB is unreachable, `memory.load_profile` returns a fresh
empty profile and `upsert_profile` logs and swallows the error — so you
can run the graph with retrieval + generation working and only persistence
disabled, which is enough for most local dev.

```bash
export AWS_REGION=us-east-1
export KB_ID=<your-kb-id>
export GATEWAY_URL=<your-gateway-url>
python -m agent.main          # interactive CLI
# or
python ../test_agent.py       # scripted smoke test, exercises the streaming path
```

No customer bound (`customer_id="default"`) works out of the box — the
agent answers generically and skips all profile-specific probing.

## Design rules worth knowing before you edit this package

- **`prompts.py` is the only place the system prompt is built.** Don't
  add a second prompt string anywhere else — see the module docstring.
- **Artifacts never fabricate.** Every artifact kind in `nodes_artifacts.py`
  has a `REQUIRED_INPUTS` predicate; if the profile doesn't have enough
  signal, the artifact is suppressed and a gap note is surfaced instead.
  Don't relax this to "just generate something plausible."
- **Fact extraction is quote-grounded.** `profile_updater_node`'s
  extractor drops any fact whose supporting quote doesn't appear
  verbatim in the SA's message. Don't change this to allow inference —
  it's the main defense against a fabricated customer profile.
- **Contradictions are surfaced, never silently overwritten.** See
  `pending_contradictions` in `state.py`.
