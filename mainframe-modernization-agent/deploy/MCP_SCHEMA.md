# Gateway tool schema — single source of truth

[`gateway-tool-schema.json`](gateway-tool-schema.json) is the **single source
of truth** for what tools the AgentCore Gateway advertises to MCP clients
(including our agent in production). The deployed Gateway target is derived
from this file via [`sync_gateway_schema.py`](sync_gateway_schema.py); it is
never edited directly through the AWS console or API.

## Why this rule exists

In Iteration 1.5 we added three tools to the Lambda's `TOOLS` dict
(`compare_partner_tools`, `compare_services`, `list_taxonomy`) and updated
the agent's router to call them. The Gateway target was updated through
the AWS API at the time, but the JSON file in the repo wasn't — it sat at 6
tools and stale arg names while the actual deployed Gateway had 9 tools and
correct arg names. That divergence is invisible until someone re-runs a
setup script that overwrites the Gateway from the file, at which point the
Gateway silently regresses.

To prevent this happening again we made the file authoritative and
provided a reconciler script that keeps the Gateway in sync.

## The workflow

### Adding a tool

1. Add the function to [`mainframe_mcp_lambda.py`](mainframe_mcp_lambda.py)
   and to its `TOOLS` dict.
2. Add the tool's JSON Schema to
   [`gateway-tool-schema.json`](gateway-tool-schema.json).
3. Re-deploy the Lambda (whatever update path you normally use).
4. Run the reconciler:

   ```bash
   python deploy/sync_gateway_schema.py
   ```

5. Optionally update [`agent/router.py`](../agent/router.py) so the router
   knows when to fire the new tool.

### Changing a tool's signature

1. Update the function signature in `mainframe_mcp_lambda.py`.
2. Update the matching `inputSchema` in `gateway-tool-schema.json`.
3. Re-deploy the Lambda.
4. Run the reconciler.

### Removing a tool

1. Remove the function and the entry from the `TOOLS` dict in
   `mainframe_mcp_lambda.py`.
2. Remove the tool's entry from `gateway-tool-schema.json`.
3. Re-deploy the Lambda.
4. Run the reconciler — it will detect the live tool no longer in the file
   and remove it from the Gateway.

## The reconciler

`sync_gateway_schema.py` does three things:

| Mode | What it does |
|---|---|
| (default) | Reads the file, reads the live Gateway target, prints a human-readable diff, applies the update if there is drift, waits for `READY`. Idempotent — running twice is safe. |
| `--check` | Same diff, but never writes. Exits non-zero on drift. Use in CI. |

Examples:

```bash
# Apply any drift (or no-op if the live target already matches)
python deploy/sync_gateway_schema.py

# CI guard — fail the build if anyone edited the Gateway directly
python deploy/sync_gateway_schema.py --check
```

## Direct API edits are forbidden

Don't do:
- `aws bedrock-agentcore-control update-gateway-target ...` from a terminal
- AWS console "edit tools" inline
- A one-off Python script that calls `update_gateway_target`

If you need to test a change quickly, edit the file, run the reconciler,
test, and revert (in git) if needed. The Gateway is downstream of the file.
