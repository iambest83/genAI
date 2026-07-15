# Mainframe Modernization MCP — local server for Claude Desktop

Local MCP server exposing 10 mainframe-modernization reference tools to
Claude Desktop. Independent of the production agent — runs entirely on
your machine, no AWS calls, no auth.

## What you get

After connecting, Claude Desktop can call these tools in any conversation:

| Tool | What it does |
|---|---|
| `lookup_cobol_pattern`     | COBOL syntax / patterns by category + name |
| `lookup_jcl_reference`     | JCL statements / utilities reference |
| `map_mainframe_to_aws`     | CICS, DB2, VSAM, IMS, MQ, RACF → AWS service mapping |
| `get_migration_pattern`    | Rehost / replatform / refactor / retire / automated_refactor details |
| `estimate_complexity`      | **Deterministic** complexity score from a workload (programs, JCL, CICS, DB2, …) — returns factors, confidence, missing-inputs, and a `breakdown_hash` for audit |
| `get_fsi_compliance_check` | SOX / PCI-DSS / FDIC / OCC / GLBA / FFIEC / FINRA-17a-4 — typed control catalog + mandatory not-legal-advice disclaimer |
| `compare_partner_tools`    | Weighted multi-criteria ranking of Micro Focus / Blu Age / Heirloom / TCS / … with per-criterion `criterion_scores` |
| `compare_services`         | Weighted ranking of AWS target services for a given mainframe source component |
| `analyze_phase_gaps`       | **Deterministic** engagement-phase + the exact discovery questions blocking advancement (mirrors the agent's `derive_phase`) |
| `list_taxonomy`            | Discover the categories/argument shapes of every other tool |

### Iteration 1.6 — what's new

- **Profile seam.** `estimate_complexity`, `compare_partner_tools`,
  `compare_services`, and `analyze_phase_gaps` accept an optional
  `profile` argument (a `CustomerProfile.to_dict()`). They auto-seed their
  inputs from the profile so the SA never re-types known facts; explicit
  arguments always override. `compare_partner_tools` with an empty
  `source_systems` no longer scores every vendor a perfect 1.0 — it derives
  sources from the profile, or falls back to a neutral prior flagged
  `low_confidence`.
- **Auditable scoring.** All ranking weights live in externalized JSON
  (`partner_scoring.json`, `service_ranking_weights.json`,
  `complexity_scoring.json`) — tunable without a code change, and every
  response echoes the weights and a per-criterion breakdown. Pass
  `weight_profile` (e.g. `regulator_first`, `cost_sensitive`,
  `low_latency_first`) to see how the ranking shifts under different
  priorities.
- **Provenance.** Every response carries a one-line
  "Sources: …; last reviewed YYYY-MM-DD" footer so a recommendation is
  citable.

## Install

Already installed if you've run `uv sync` in this directory; if not:

```bash
cd <repo>/mcp_server
uv sync
```

That builds a `.venv` next to this file with FastMCP installed.

## Wire it into Claude Desktop

1. **Open Claude Desktop's config file**:

   ```
   ~/Library/Application\ Support/Claude/claude_desktop_config.json
   ```

   If it doesn't exist yet, create it.

2. **Add this `mcpServers` entry** (or merge with what's already there):

   ```json
   {
     "mcpServers": {
       "mainframe-modernization": {
         "command": "uv",
         "args": [
           "--directory",
           "<absolute path to repo>/mcp_server",
           "run",
           "mfmod-mcp"
         ]
       }
     }
   }
   ```

   The `--directory` flag pins `uv` to this project so it uses the local
   `.venv` regardless of where Claude Desktop launches us from.

3. **Quit and relaunch Claude Desktop.** (Important — the config is read
   once at startup.)

4. **Verify it loaded.** In a new conversation, click the 🔌 / tools
   icon. You should see `mainframe-modernization` listed with 10 tools.

## Try it

In any conversation, ask Claude something that fits a tool:

> Using the mainframe-modernization tools, look up the COBOL `PIC` clause syntax.

> Estimate the modernization complexity of a workload with 1500 COBOL programs, 600 JCL jobs, CICS, and DB2.

> Compare Micro Focus and Blu Age for a Cards replatform engagement.

Claude routes the call to the local server, gets back a structured response,
and weaves it into the answer.

## Troubleshooting

- **Tools don't show up** → check `~/Library/Logs/Claude/mcp-server-mainframe-modernization.log`. Most common cause: `uv` not on Claude Desktop's `$PATH`. Fix by passing the absolute path: `"command": "/opt/homebrew/bin/uv"`.

- **Tool calls error out** → run the server manually to see the trace:

  ```bash
  cd <repo>/mcp_server
  uv run mfmod-mcp
  # paste a JSON-RPC request into stdin, watch the response on stdout
  ```

- **Want to add a tool** → edit `server.py`, decorate with `@mcp.tool()`, restart Claude Desktop. No deploy.

- **Edited a JSON data file but the change isn't showing?** `load_data()` caches files for the process lifetime, so restart the server (relaunch Claude Desktop) after editing anything under `data/`.

## Running the tests

The tool logic has a test suite under `tests/`. It runs with or without
`pytest` installed:

```bash
# from the repo root, so both `mcp_server` and `agent` import cleanly:
mcp_server/.venv/bin/python mcp_server/tests/test_mcp_tools.py   # standalone runner
# or, if pytest is available:
pytest mcp_server/tests
```

The suite covers deterministic scoring + hashing, the profile seam, the
weighted-ranking engine, the compliance control catalog, and a fuzz test that
asserts `analyze_phase_gaps` reproduces the agent's `derive_phase()` exactly.

## What's NOT this server

The production agent (the one we deploy with `agentcore launch`) calls a
**different** copy of the same tools — a Lambda at
`MfModAgent-MainframeMCP` fronted by AgentCore Gateway, with IAM auth.

This local server and the production Lambda share tool LOGIC by virtue of
being copies of the same data + Python — but they're independent
deployments. Editing one doesn't touch the other.
