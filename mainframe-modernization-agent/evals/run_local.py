"""Local eval runner — invokes agent/graph.py in-process.

For each row in evals/dataset.jsonl:

  1. Build a fresh AgentState honoring the row's bound_customer / bound_lob /
     prior_probe / prior_state_facts overrides.
  2. Invoke the LangGraph in-process. Captures the full final state.
  3. Hand (prompt, response_text, expected, post_turn_state) to judge_response.
  4. Accumulate per-row results.
  5. Hand off to evals.report for baseline-diff + JSON report.

Why in-process: a fresh local run for each PR / prompt change is the fastest
possible feedback loop. No network round-trips, no Lambda cold-starts, no
AgentCore deploy. ~25 rows × ~3-5s/row including KB+Sonnet ≈ 1-2 minutes.

Environment: requires AWS creds (chained into the Bedrock account) so the
graph's KB retrieval, MCP gateway calls, and LLM invocations can run. The
runner does NOT spin up its own auth — relies on the ambient env vars set
by /tmp/bedrock_creds.sh.

Usage:
    cd <repo root>
    python -m evals.run_local                # run all rows
    python -m evals.run_local --only pat-01  # one specific row
    python -m evals.run_local --tag defer    # all rows with this tag
    python -m evals.run_local --no-baseline  # skip baseline-diff (first run)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Project root on sys.path so `from agent...` and `from evals...` both work.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage, AIMessage  # noqa: E402

from agent.customer_profile import CustomerProfile  # noqa: E402
from agent.graph import get_graph  # noqa: E402
from agent.state import AgentState  # noqa: E402
from evals.judge import judge_response  # noqa: E402

DATASET_PATH = ROOT / "evals" / "dataset.jsonl"
RUNS_DIR = ROOT / "evals" / "runs"


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def load_dataset() -> list[dict]:
    rows = []
    with DATASET_PATH.open() as f:
        for ln, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError as e:
                raise SystemExit(f"dataset.jsonl line {ln}: invalid JSON — {e}")
    return rows


def filter_rows(rows: list[dict], only: str | None, tag: str | None) -> list[dict]:
    if only:
        rows = [r for r in rows if r.get("id") == only]
    if tag:
        rows = [r for r in rows if r.get("tag") == tag]
    if not rows:
        raise SystemExit("no rows match the filter")
    return rows


# ---------------------------------------------------------------------------
# State construction
# ---------------------------------------------------------------------------

def _customer_id_from_display(display: str | None) -> str:
    """Mirror deploy/ws_lambda.py:_make_customer_id — slug + 8-char SHA suffix.
    Hashing the slug (not the raw display) so JPMC / jpmc / Jpmc collide to
    the same id, matching production behavior."""
    if not display:
        return "default"
    import hashlib
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", display.strip().lower()).strip("-") or "unknown"
    tail = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{tail}"


def build_initial_state(row: dict) -> AgentState:
    """Build a fresh AgentState for a single eval row.

    Honors:
      - bound_customer (display name) → customer_id + customer_display_name
      - bound_lob      (display name) → lob_id + lob_display_name
      - prior_probe                   → seeds open_questions queue (one entry)
      - prior_state_facts             → pre-applies facts to the in-memory
                                        CustomerProfile so contradiction-
                                        detection rows have something to
                                        contradict against
    """
    bound_cust_display = row.get("bound_customer")
    bound_lob_display  = row.get("bound_lob")
    prior_probe        = row.get("prior_probe", "")
    prior_facts        = row.get("prior_state_facts") or []

    customer_id = _customer_id_from_display(bound_cust_display) if bound_cust_display else "default"
    customer_display_name = bound_cust_display or ""

    if bound_lob_display:
        # Same _norm_lob convention as memory.py — slug only, no hash.
        import re as _re
        lob_id = _re.sub(r"[^a-z0-9]+", "-", bound_lob_display.lower()).strip("-") or "default"
    else:
        lob_id = "default"
    lob_display_name = bound_lob_display or ""

    # Build an in-memory CustomerProfile (we never persist during evals).
    profile = CustomerProfile(
        sa_id="eval-runner",
        customer_id=customer_id,
        customer_display_name=customer_display_name,
        lob_id=lob_id,
        lob_display_name=lob_display_name,
    )
    for f in prior_facts:
        try:
            profile.apply_fact(f["field_path"], f["value"], turn=0)
        except Exception as e:
            print(f"[warn] {row.get('id')}: prior_state_facts apply failed: {e}")

    state: AgentState = {
        "messages": [HumanMessage(content=row["prompt"])],
        "user_query": row["prompt"],
        "sa_id": "eval-runner",
        "customer_id": customer_id,
        "customer_display_name": customer_display_name,
        "lob_id": lob_id,
        "lob_display_name": lob_display_name,
        "session_id": f"eval-{row.get('id', '?')}",
        "turn": 1,
        "route": "both",
        "mcp_tools": [],
        "kb_context": "",
        "mcp_context": "",
        "artifacts": [],
        "pending_contradictions": [],
        "profile_dirty": False,
        "customer_profile": profile,
        "open_questions": [prior_probe] if prior_probe else [],
        "probe_muted": False,
    }
    return state


# ---------------------------------------------------------------------------
# Per-row execution
# ---------------------------------------------------------------------------

def _extract_response_text(final_state: dict) -> str:
    """Pull the assistant's final reply out of the post-turn state. Some
    routes set response_text directly; others land it as the last AIMessage
    in messages[]."""
    txt = final_state.get("response_text") or ""
    if txt:
        return txt
    msgs = final_state.get("messages") or []
    for m in reversed(msgs):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
            return str(content or "")
    return ""


def run_row(graph, row: dict) -> dict:
    """Run one row end-to-end. Returns the row record we'll write to the report."""
    rid = row.get("id", "?")
    print(f"  [{rid}] running…", end="", flush=True)
    t0 = time.time()

    state = build_initial_state(row)
    err = None
    final_state: dict = {}
    try:
        final_state = graph.invoke(state)
    except Exception as e:
        err = f"graph.invoke failed: {e!s}"

    if err:
        result = {
            "id": rid,
            "tag": row.get("tag", ""),
            "prompt": row["prompt"],
            "error": err,
            "elapsed_s": round(time.time() - t0, 2),
            "verdicts": {},
            "overall": "FAIL",
            "response_excerpt": "",
        }
        print(f" ERROR ({result['elapsed_s']}s) — {err}")
        return result

    response_text = _extract_response_text(final_state)
    verdicts = judge_response(
        prompt=row["prompt"],
        response_text=response_text,
        expected=row.get("expected", {}),
        post_turn_state=final_state,
    )
    overall = verdicts.get("overall", "FAIL")
    elapsed = round(time.time() - t0, 2)
    sym = "✓" if overall == "PASS" else "✗"
    print(f" {sym} {overall} ({elapsed}s, route={final_state.get('route')!r})")

    return {
        "id": rid,
        "tag": row.get("tag", ""),
        "prompt": row["prompt"],
        "elapsed_s": elapsed,
        "route_actual": final_state.get("route"),
        "response_excerpt": response_text[:400],
        "verdicts": verdicts,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Local in-process eval runner.")
    parser.add_argument("--only", help="Run a single row by id.")
    parser.add_argument("--tag",  help="Run only rows with this tag.")
    parser.add_argument("--no-baseline", action="store_true",
                        help="Skip baseline diff. Use on the first ever run.")
    parser.add_argument("--save-baseline", action="store_true",
                        help="After running, copy the report to runs/baseline.json.")
    args = parser.parse_args()

    rows = filter_rows(load_dataset(), args.only, args.tag)
    print(f"Loaded {len(rows)} row(s) from {DATASET_PATH}")

    print("Building graph…")
    graph = get_graph()

    print(f"Running {len(rows)} row(s):")
    t0 = time.time()
    results = [run_row(graph, r) for r in rows]
    elapsed = round(time.time() - t0, 1)

    passes = sum(1 for r in results if r["overall"] == "PASS")
    fails  = len(results) - passes
    print(f"\n{passes}/{len(results)} passed in {elapsed}s")

    # Write the report
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    report_path = RUNS_DIR / f"local-{ts}.json"
    report = {
        "target":     "local",
        "timestamp":  ts,
        "elapsed_s":  elapsed,
        "totals":     {"pass": passes, "fail": fails, "n": len(results)},
        "results":    results,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"Report: {report_path}")

    # Baseline diff
    if not args.no_baseline:
        try:
            from evals.report import diff_against_baseline
            regressions = diff_against_baseline(report)
        except ImportError:
            print("(evals.report not available yet — skip baseline diff)")
            regressions = 0
    else:
        regressions = 0

    if args.save_baseline:
        baseline_path = RUNS_DIR / "baseline.json"
        baseline_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"Saved as baseline → {baseline_path}")

    sys.exit(1 if regressions or fails else 0)


if __name__ == "__main__":
    main()
