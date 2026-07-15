"""AgentCore eval runner — invokes the deployed AgentCore Runtime end-to-end.

Same dataset, same judge, same report shape as run_local.py. The
difference: instead of `graph.invoke(state)` in-process, this hits the
deployed AgentCore Runtime via boto3, parses the SSE stream into a
synthetic post-turn state, and grades that.

Why a separate runner: in-process eval (run_local) doesn't cross the
AgentCore boundary, doesn't go through Gateway, doesn't hit the deployed
Lambda. It catches prompt regressions but misses deploy-shape regressions
(packaging gaps, IAM / Gateway drift, Lambda cold-start surprises).

What can and can't be checked end-to-end:

  - response_text + route       → derived from SSE stream directly
  - persisted_facts             → read post-turn from the DDB profile
                                  snapshot (not in the SSE stream)
  - persisted_decisions         → same, decisions live on the snapshot
  - contradiction_surfaced      → contradictions are NOT persisted to
                                  the snapshot (that's the point — they
                                  await SA confirmation). They ARE
                                  written to the per-turn event row;
                                  we read the most recent event row for
                                  that turn to verify.

State-assertion rows (extract-01, extract-02, contradict-01) MUST run
in clean isolation — each row gets a unique sa_id so the AgentCore-side
profile is fresh and a previous row's writes never bleed in. After the
turn, we read DDB to materialize the post-turn state shape the judge
expects (customer_profile + pending_contradictions).

Cost: ~$0.05–0.15 per row (Bedrock LLM calls, KB retrieval,
gateway/Lambda). 25 rows ≈ $1–4 per full sweep — meaningfully more
than run_local.py, so this isn't a per-PR runner. Use after a deploy.

Usage:
    cd <repo root>
    python -m evals.run_agentcore                    # all rows
    python -m evals.run_agentcore --only defer-01    # one row
    python -m evals.run_agentcore --tag extraction   # state-assertion rows
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# Pin region BEFORE any boto3 client creation. Our DDB table + AgentCore
# Runtime live in us-east-1, so we override any ambient AWS_REGION the
# shell may have set. agent/memory.py reads AWS_REGION at module-load
# time, so this must happen before any agent imports.
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from agent.customer_profile import CustomerProfile  # noqa: E402
from agent.memory import _pk, _norm_lob, load_profile, upsert_profile  # noqa: E402
from evals.judge import judge_response  # noqa: E402
from evals.run_local import (  # noqa: E402
    DATASET_PATH, RUNS_DIR, _customer_id_from_display,
    filter_rows, load_dataset,
)

REGION = "us-east-1"
ACCOUNT_ID = "<ACCOUNT_ID>"
DEFAULT_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT_ID}:runtime/MfModAgent-<RUNTIME_ID>"
)
TABLE_NAME = os.environ.get("CUSTOMER_MEMORY_TABLE", "MfModAgent-CustomerMemory")
INVOKE_TIMEOUT_S = 120


# ---------------------------------------------------------------------------
# AgentCore invoke + SSE stream parsing
# ---------------------------------------------------------------------------

def _build_payload(row: dict, sa_id: str) -> dict:
    """Build the chat-kind payload AgentCore's entrypoint expects.

    Mirrors the shape ws_lambda.py:_stream_from_agentcore puts together
    in production — same fields, same defaults. Identity (sa_id) is the
    only field we deliberately diverge on: each row gets a fresh sa_id
    so AgentCore-side profile state is isolated.
    """
    bound_cust = row.get("bound_customer")
    bound_lob  = row.get("bound_lob")

    customer_id = _customer_id_from_display(bound_cust) if bound_cust else "default"
    lob_id = _norm_lob(bound_lob) if bound_lob else "default"

    return {
        "kind": "chat",
        "prompt": row["prompt"],
        "sa_id": sa_id,
        "customer_id": customer_id,
        "customer_display_name": bound_cust or "",
        "lob_id": lob_id,
        "lob_display_name": bound_lob or "",
        "session_id": f"eval-{row.get('id', '?')}-{sa_id}",
        "turn": 1,
    }


def _seed_prior_state(row: dict, sa_id: str) -> None:
    """For rows with prior_state_facts, write a fresh profile to DDB before
    the AgentCore turn so the deployed graph has something to contradict.

    Only used by contradict-* rows. The sa_id is unique-per-row, so this
    write doesn't pollute any other eval.
    """
    prior_facts = row.get("prior_state_facts") or []
    if not prior_facts:
        return

    bound_cust = row.get("bound_customer")
    bound_lob  = row.get("bound_lob")
    customer_id = _customer_id_from_display(bound_cust) if bound_cust else "default"
    lob_id = _norm_lob(bound_lob) if bound_lob else "default"

    profile = CustomerProfile(
        sa_id=sa_id,
        customer_id=customer_id,
        customer_display_name=bound_cust or "",
        lob_id=lob_id,
        lob_display_name=bound_lob or "",
    )
    for f in prior_facts:
        try:
            profile.apply_fact(f["field_path"], f["value"], turn=0)
        except Exception as e:
            print(f"[warn] {row.get('id')}: prior_state_facts apply failed: {e}")
    upsert_profile(profile)


def _invoke_and_collect(client, runtime_arn: str, payload: dict) -> dict:
    """Invoke the runtime; assemble a state-shaped dict from the SSE stream.

    Returns a dict with at least:
      - response_text: concatenated tokens
      - route:         from the terminal `done` event (or last status hint)
      - artifacts:     list of artifact event payloads
      - error:         non-empty if any `error` event arrived
      - tool_calls:    list of tool names emitted via `tool_call` events
    """
    out: dict = {
        "response_text": "",
        "route": "",
        "artifacts": [],
        "tool_calls": [],
        "error": "",
        "raw_event_count": 0,
    }
    body_bytes = json.dumps(payload).encode("utf-8")

    resp = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        payload=body_bytes,
    )
    content_type = (resp.get("contentType") or "").lower()
    body = resp.get("response") or resp.get("body") or resp.get("payload")

    if "text/event-stream" in content_type and hasattr(body, "iter_lines"):
        token_chunks: list[str] = []
        for line in body.iter_lines():
            if not line:
                continue
            try:
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            except Exception:
                continue
            if not line_str.startswith("data: "):
                continue
            try:
                event = json.loads(line_str[6:])
            except json.JSONDecodeError:
                continue
            out["raw_event_count"] += 1
            etype = event.get("type")
            if etype == "token":
                token_chunks.append(event.get("text", ""))
            elif etype == "tool_call":
                out["tool_calls"].append(event.get("tool", ""))
            elif etype == "artifact":
                out["artifacts"].append(event)
            elif etype == "done":
                out["route"] = event.get("route", "") or out["route"]
            elif etype == "error":
                out["error"] = event.get("message", "unknown error")
            elif etype == "status":
                # status hints look like "Route: kb" — opportunistic capture
                msg = event.get("message", "")
                if msg.startswith("Route: "):
                    out["route"] = msg[len("Route: "):].strip() or out["route"]
        out["response_text"] = "".join(token_chunks)
        return out

    # Non-streaming fallback: surface raw body so the failure mode is visible.
    raw = ""
    try:
        if hasattr(body, "read"):
            raw = body.read().decode("utf-8")
        elif body is not None:
            raw = str(body)
    except Exception as e:
        out["error"] = f"reading non-stream body failed: {e}"
        return out
    out["error"] = f"unexpected non-streaming response (content_type={content_type!r}): {raw[:300]}"
    return out


# ---------------------------------------------------------------------------
# Post-turn DDB reads (for state-assertion criteria)
# ---------------------------------------------------------------------------

def _read_profile_post_turn(sa_id: str, customer_id: str, lob_id: str) -> CustomerProfile | None:
    """Pull the post-turn profile snapshot. None if the turn didn't write one
    (which is itself meaningful — the judge will FAIL state-assertion rows
    that expected facts)."""
    try:
        return load_profile(sa_id, customer_id, _norm_lob(lob_id))
    except Exception as e:
        print(f"  [warn] load_profile failed: {e}")
        return None


def _read_pending_contradictions(sa_id: str, customer_id: str, lob_id: str) -> list[dict]:
    """Pull contradictions from the most recent per-turn event row.

    Pending contradictions are deliberately NOT persisted to the profile
    snapshot (they await SA confirmation). They ARE written into the
    event row by profile_updater_node — read that back to verify
    contradict-* rows.
    """
    sk_prefix = f"customer#{customer_id}#lob#{_norm_lob(lob_id)}#turn#"
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    try:
        resp = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={":pk": _pk(sa_id), ":prefix": sk_prefix},
            ScanIndexForward=False,  # newest turn rows first
            Limit=1,
        )
    except ClientError as e:
        print(f"  [warn] DDB query for turn events failed: {e}")
        return []
    items = resp.get("Items", [])
    if not items:
        return []
    body = items[0].get("body", {}) or {}
    return body.get("contradictions", []) or []


def _augment_state_for_judge(row: dict, sa_id: str, sse_state: dict) -> dict:
    """Build the post_turn_state dict the existing judge expects.

    Layered on top of what we got from SSE: customer_profile (for
    persisted_facts / persisted_decisions) and pending_contradictions
    (for contradiction_surfaced) — both read from DDB.

    Skips the DDB reads if no row criterion needs them (cheap rows
    like defer-01 don't warrant a roundtrip).
    """
    expected = row.get("expected", {})
    needs_profile = (
        "persisted_facts" in expected or "persisted_decisions" in expected
    )
    needs_contradictions = "contradiction_surfaced" in expected
    if not needs_profile and not needs_contradictions:
        return {"route": sse_state.get("route", "")}

    bound_cust = row.get("bound_customer")
    bound_lob  = row.get("bound_lob")
    customer_id = _customer_id_from_display(bound_cust) if bound_cust else "default"
    lob_id = _norm_lob(bound_lob) if bound_lob else "default"

    state: dict = {"route": sse_state.get("route", "")}
    if needs_profile:
        state["customer_profile"] = _read_profile_post_turn(sa_id, customer_id, lob_id)
    if needs_contradictions:
        state["pending_contradictions"] = _read_pending_contradictions(
            sa_id, customer_id, lob_id,
        )
    return state


# ---------------------------------------------------------------------------
# Per-row execution
# ---------------------------------------------------------------------------

def run_row(client, runtime_arn: str, row: dict) -> dict:
    rid = row.get("id", "?")
    print(f"  [{rid}] running…", end="", flush=True)
    t0 = time.time()

    # Unique sa_id per row → fresh AgentCore-side profile, no cross-row bleed.
    sa_id = f"eval-{rid}-{uuid.uuid4().hex[:8]}"

    err = ""
    sse_state: dict = {}
    try:
        _seed_prior_state(row, sa_id)
        payload = _build_payload(row, sa_id)
        sse_state = _invoke_and_collect(client, runtime_arn, payload)
        if sse_state.get("error"):
            err = sse_state["error"]
    except Exception as e:
        err = f"invoke failed: {e!s}"

    if err:
        elapsed = round(time.time() - t0, 2)
        print(f" ERROR ({elapsed}s) — {err}")
        return {
            "id": rid,
            "tag": row.get("tag", ""),
            "prompt": row["prompt"],
            "sa_id": sa_id,
            "error": err,
            "elapsed_s": elapsed,
            "verdicts": {},
            "overall": "FAIL",
            "response_excerpt": "",
        }

    response_text = sse_state.get("response_text", "")
    post_turn_state = _augment_state_for_judge(row, sa_id, sse_state)

    verdicts = judge_response(
        prompt=row["prompt"],
        response_text=response_text,
        expected=row.get("expected", {}),
        post_turn_state=post_turn_state,
    )
    overall = verdicts.get("overall", "FAIL")
    elapsed = round(time.time() - t0, 2)
    sym = "✓" if overall == "PASS" else "✗"
    print(f" {sym} {overall} ({elapsed}s, route={post_turn_state.get('route')!r}, "
          f"events={sse_state.get('raw_event_count')})")

    return {
        "id": rid,
        "tag": row.get("tag", ""),
        "prompt": row["prompt"],
        "sa_id": sa_id,
        "elapsed_s": elapsed,
        "route_actual": post_turn_state.get("route"),
        "response_excerpt": response_text[:400],
        "tool_calls": sse_state.get("tool_calls", []),
        "verdicts": verdicts,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AgentCore Runtime eval runner.")
    parser.add_argument("--only", help="Run a single row by id.")
    parser.add_argument("--tag",  help="Run only rows with this tag.")
    parser.add_argument("--runtime-arn", default=DEFAULT_RUNTIME_ARN,
                        help="AgentCore Runtime ARN to invoke.")
    parser.add_argument("--no-baseline", action="store_true",
                        help="Skip baseline diff.")
    parser.add_argument("--save-baseline", action="store_true",
                        help="Save this run as runs/baseline-agentcore.json.")
    args = parser.parse_args()

    rows = filter_rows(load_dataset(), args.only, args.tag)
    print(f"Loaded {len(rows)} row(s) from {DATASET_PATH}")
    print(f"Runtime: {args.runtime_arn}")

    from botocore.config import Config as BotoConfig
    client = boto3.client(
        "bedrock-agentcore",
        region_name=REGION,
        config=BotoConfig(read_timeout=INVOKE_TIMEOUT_S),
    )

    print(f"Running {len(rows)} row(s) against AgentCore:")
    t0 = time.time()
    results = [run_row(client, args.runtime_arn, r) for r in rows]
    elapsed = round(time.time() - t0, 1)

    passes = sum(1 for r in results if r["overall"] == "PASS")
    fails  = len(results) - passes
    print(f"\n{passes}/{len(results)} passed in {elapsed}s")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    report_path = RUNS_DIR / f"agentcore-{ts}.json"
    report = {
        "target":     "agentcore",
        "runtime_arn": args.runtime_arn,
        "timestamp":  ts,
        "elapsed_s":  elapsed,
        "totals":     {"pass": passes, "fail": fails, "n": len(results)},
        "results":    results,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Report: {report_path}")

    regressions = 0
    if not args.no_baseline:
        # Diff against the in-process baseline so deploy-shape divergence
        # is visible at a glance. (A separate baseline-agentcore.json
        # could be wired up later if we want a runtime-specific reference.)
        try:
            from evals.report import diff_against_baseline
            regressions = diff_against_baseline(report)
        except ImportError:
            print("(evals.report not available — skip baseline diff)")

    if args.save_baseline:
        baseline_path = RUNS_DIR / "baseline-agentcore.json"
        baseline_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Saved as agentcore baseline → {baseline_path}")

    sys.exit(1 if regressions or fails else 0)


if __name__ == "__main__":
    main()
