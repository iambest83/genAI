# Evals

Offline regression evals for the mainframe-modernization agent. Run before
every deploy. The dataset lives next to the prompts it grades — a
regression in either should be a one-PR change.

## Files

- `dataset.jsonl` — the test set. One row per line; never gets read by humans
  outside of grep, so JSONL keeps it diff-friendly.
- `runs/` — generated reports (one per run). Git-tracked so we can `diff`
  against the last green.
- `runs/baseline.json` — the most recently committed green run. Replaced
  whenever a green run becomes the new reference.
- `judge.py` — hybrid judge. Deterministic criteria (route equality, substring
  scans, word counts, post-turn state comparisons) run as plain Python;
  subjective criteria (`must_mention`, grounding checks) are batched into
  one Sonnet call per row, only when the row actually has subjective
  criteria. ~$0.002–0.005 per row when the Sonnet call fires.
- `run_local.py` — invokes `agent/graph.py` in-process. Fast inner loop,
  no AWS deploy needed (just AWS credentials for Bedrock + KB + DDB).
- `run_agentcore.py` — invokes the deployed runtime via
  `bedrock-agentcore.invoke_agent_runtime`, streaming SSE the same way
  the real WebSocket relay does. Also seeds `prior_state_facts` /
  `prior_probe` directly into DynamoDB before invoking, and reads back
  the persisted profile + `pending_contradictions` after, so
  extraction/contradiction rows can be graded end-to-end against the
  real deployment.
- `report.py` — diffs a run's report against `runs/baseline.json` and
  prints a colorized per-row diff (pass→fail, fail→pass, unchanged).

## Dataset row schema

Each line is a single JSON object. Required keys:

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Stable, unique. Used for diffs across runs. Convention: `<tag>-<NN>`. |
| `tag` | string | Behavior bucket: `patterns`, `partners`, `components`, `compliance`, `defer`, `acknowledge`, `chat`, `meta`, `summary`, `unbound`, `extraction`, `branding`, `substantive-short`. Used to group failures. |
| `prompt` | string | The literal SA message that gets sent to the agent. |
| `bound_customer` | string \| null | If non-null, the eval runner pre-binds this customer (sets `customer_id` + `customer_display_name`). Mirrors the chip in the UI. |
| `bound_lob` | string \| null | Same idea, for LoB. |
| `prior_probe` | string (opt) | If set, the runner injects this into `state["open_questions"]` before invocation. Required for defer/ack/short-reply rows so the intent classifier has prior context. |
| `prior_state_facts` | list (opt) | Pre-load the profile with these facts (each `{field_path, value}`) before the turn. Required for contradiction-detection rows. |
| `expected` | object | Rubric. See below. |

### The `expected` object

The keys here are the criteria the judge will score. Each key is independent —
the row passes overall iff every present key passes.

| Key | Type | Meaning |
|---|---|---|
| `expected_route` | string | Which graph route should fire: `kb` / `mcp` / `both` / `direct` / `summary` / `help` / `defer` / `acknowledge`. The runner reads this from the state delta — no judging needed. |
| `must_mention` | list[string] | Each term should appear in the response (paraphrasing OK; the judge handles synonyms). |
| `must_avoid` | list[string] | None of these strings should appear. Used for: citation markers (`[KB]`, `[MCP`), forbidden phrases, the intro deck leaking on a greeting, etc. |
| `must_end_with_probe` | bool | Response should end with a `**To advance:** …` question. Cheap regex check, not LLM-judged. |
| `no_customer_specific_claims` | bool | Response must not invent customer state (workload size, vendor, decisions). The judge looks for fabricated specifics. |
| `grounding_check` | string | Free-text rubric the judge uses for nuanced grounding decisions. |
| `max_response_words` | int | Word-count ceiling. Used for defer/ack rows that should be terse. Cheap check. |
| `persisted_facts` | list | After the turn, these facts should be in the merged profile. Used for extraction rows. Runner reads `customer_profile` post-turn. |
| `persisted_decisions` | list | Same idea for decisions. `[]` means "must NOT persist anything". |
| `contradiction_surfaced` | object | `{field_path, old_value, new_value}` should be in `pending_contradictions` after the turn. |
| `notes` | string | Author commentary — never used by the judge, just helps reviewers understand the row. |

## How to add a row

When you hit a real bug — in dev or in production — add a row that captures it:

```json
{
  "id": "drift-07",
  "tag": "unbound",
  "prompt": "Map components to AWS",
  "bound_customer": null,
  "bound_lob": null,
  "expected": {
    "must_avoid": ["set Map as the customer", "switch context"],
    "expected_route": "both",
    "notes": "Capitalized 'Map' must NOT trigger customer detection."
  }
}
```

Keep `expected` small. 3-4 properties is plenty. Long rubrics produce noisy
scores.

## Coverage map (current 31 rows)

| Tag | Count | What we're testing |
|---|---|---|
| `patterns` | 2 | Generic + bound pattern walkthroughs; no fake customer claims |
| `partners` | 2 | Partner comparison; nothing's "selected" until SA says so |
| `components` | 2 | Mainframe → AWS service maps |
| `compliance` | 2 | FFIEC + multi-reg one-liner |
| `defer` | 3 | "not now" / "na, not sure" / "skip" — must route defer, terse, no probe |
| `substantive-short` | 2 | "strategic" / "no it is a long term plan" — must NOT route to defer or chat |
| `acknowledge` | 1 | "thanks" → terse ack |
| `chat` | 1 | Bare "hi" — no intro deck |
| `meta` | 1 | "what can you do" → help deck |
| `summary` | 2 | "what do you know" / "where are we" — both LoB-bound and customer-wide |
| `unbound` | 2 | Generic prompts with no customer; capitalized verbs must not trigger drift |
| `extraction` | 3 | Quote-grounded fact extraction; questions must not extract; contradiction surfaced, not silently merged |
| `branding` | 2 | Greeting copy; bound-aware greeting names customer without reproducing intro |
| `aws-tools` | 2 | AWS-managed MCP tools (docs search, regions) route and fire correctly |
| `pricing` | 2 | Live AWS pricing tool calls, structured filter extraction |
| `web-search` | 2 | Web search tool fires on explicit "check the web" phrasing; no false "unavailable" claims |

New bug → new row, always. If you fork this for your own domain, treat
this dataset as a template shape to fill with your own regressions, not
literal content to keep.

## Running

```bash
# Fast inner loop, in-process (needs AWS creds: Bedrock + KB + DynamoDB)
python evals/run_local.py                          # full dataset
python evals/run_local.py --tag extraction          # just one tag
python evals/run_local.py --only pat-01             # just one row
python evals/run_local.py --no-baseline             # skip the diff (first-ever run)
python evals/run_local.py --save-baseline           # promote this run to the new baseline

# Against a deployed AgentCore Runtime
python evals/run_agentcore.py --runtime-arn <your-runtime-arn>
python evals/run_agentcore.py --runtime-arn <arn> --tag defer

# Either runner writes evals/runs/<local|agentcore>-<timestamp>.json,
# prints pass/fail counts, and diffs against evals/runs/baseline.json
# (or --save-baseline to replace it). Exit code is 1 if there are any
# fails or new regressions vs. baseline, 0 otherwise — wire this into CI.
```

`run_agentcore.py`'s default `--runtime-arn` is set from an
`ACCOUNT_ID`/`REGION` constant near the top of the file — update it to
your own account, or always pass `--runtime-arn` explicitly.
