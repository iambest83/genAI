"""Hybrid judge for the offline eval dataset.

Each `expected` criterion is dispatched to one of two graders:

  - DETERMINISTIC criteria → a Python helper. No LLM, instant, free, exact.
    Used for substring scans, route equality, regex probes, word counts,
    and post-turn-state comparisons (persisted facts, decisions,
    pending contradictions).

  - SUBJECTIVE criteria → batched into one Sonnet call per row. Used for
    things that need reading comprehension and paraphrasing tolerance:
    "did the response actually mention the concept?", "did it avoid
    fabricating customer state?", "is every customer-specific claim
    grounded in the provided context?".

Public API:

    verdicts = judge_response(prompt, response_text, expected, post_turn_state)
    # → {criterion: {"verdict": "PASS"|"FAIL", "reason": "..."}, ...,
    #    "overall": "PASS"|"FAIL"}

Cost: ~$0.002–0.005 per row (one Sonnet call max, only if any subjective
criteria are present). 25 rows ≈ $0.05–0.13 per eval run.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dispatcher tables
# ---------------------------------------------------------------------------

# Each key is a criterion name authored in evals/dataset.jsonl's `expected`
# block. The dispatcher walks the row's expected dict, looks each key up
# here, and routes the criterion to the right grader. A typo in dataset.jsonl
# (e.g. `must_avoidd`) falls through to the unknown-criterion handler in
# judge_response() and produces a loud FAIL — never a silent skip.

DETERMINISTIC_CRITERIA = {
    "must_avoid",              # response_text must contain none of these
    "expected_route",          # final state["route"] equals this
    "must_end_with_probe",     # response_text ends with **To advance:**
    "max_response_words",      # word count cap
    "persisted_facts",         # facts present on profile after the turn
    "persisted_decisions",     # decisions present on profile after the turn
    "contradiction_surfaced",  # entry in pending_contradictions
    "expected_tool_calls",     # MCP tools that must have been routed
    "must_include_sources_footer",  # response must have a **Sources:** section
}

SUBJECTIVE_CRITERIA = {
    # name → human-readable rule that goes verbatim into the Sonnet prompt
    "must_mention":
        "The response must reference each listed term or its concept. "
        "Paraphrasing is OK — e.g. 'lift-and-shift' satisfies 'rehost'. "
        "FAIL only if a term is genuinely absent.",
    "no_customer_specific_claims":
        "The response must NOT invent or assert customer-specific state "
        "(workload size, vendor, regulations, decisions, target dates) "
        "when no customer is bound on the turn. Generic discussion is OK.",
    "grounding_check":
        "Every customer-specific claim in the response must be supported "
        "by the customer-context provided in the prompt. If the response "
        "asserts something specific about the customer that isn't in the "
        "context, that's a FAIL.",
}

# The dataset row may carry these keys that are NOT criteria — skip them
# silently rather than failing.
META_KEYS = {"notes", "id", "tag", "rationale_for_baseline"}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def judge_response(
    prompt: str,
    response_text: str,
    expected: dict,
    post_turn_state: dict | None = None,
) -> dict:
    """Grade a single dataset row.

    Args:
        prompt:           the SA message that was sent to the agent
        response_text:    the agent's final reply
        expected:         the dataset row's `expected` block
        post_turn_state:  the AgentState dict after .invoke() (or a similar
                          shape from the AgentCore runner). Required for
                          state-based criteria; pass {} if a row uses
                          response-only criteria.

    Returns:
        {criterion_name: {"verdict": "PASS"|"FAIL", "reason": str}, ...,
         "overall": "PASS"|"FAIL"}
    """
    if post_turn_state is None:
        post_turn_state = {}

    results: dict[str, dict] = {}
    subjective_to_grade: dict[str, Any] = {}

    for name, value in expected.items():
        if name in META_KEYS:
            continue
        if name in DETERMINISTIC_CRITERIA:
            results[name] = _DET_GRADERS[name](
                response_text=response_text,
                state=post_turn_state,
                expected_value=value,
            )
        elif name in SUBJECTIVE_CRITERIA:
            subjective_to_grade[name] = value
        else:
            results[name] = {
                "verdict": "FAIL",
                "reason": f"unknown criterion {name!r} — add it to "
                          f"DETERMINISTIC_CRITERIA or SUBJECTIVE_CRITERIA "
                          f"in evals/judge.py",
            }

    # One batched Sonnet call covers everything subjective for this row.
    if subjective_to_grade:
        try:
            sonnet_verdicts = _sonnet_batch_judge(
                prompt=prompt,
                response_text=response_text,
                criteria=subjective_to_grade,
            )
            results.update(sonnet_verdicts)
        except Exception as e:
            logger.warning(f"judge: Sonnet call failed: {e}")
            for name in subjective_to_grade:
                results[name] = {
                    "verdict": "FAIL",
                    "reason": f"Sonnet judge errored: {e}",
                }

    results["overall"] = (
        "PASS" if all(r["verdict"] == "PASS" for r in results.values())
        else "FAIL"
    )
    return results


# ---------------------------------------------------------------------------
# Deterministic graders
# ---------------------------------------------------------------------------

def _check_must_avoid(*, response_text: str, expected_value, **_) -> dict:
    banned = expected_value or []
    hits = [s for s in banned if s in response_text]
    if hits:
        return {"verdict": "FAIL", "reason": f"banned strings present: {hits}"}
    return {"verdict": "PASS", "reason": f"none of {banned} present"}


def _check_route(*, state, expected_value, **_) -> dict:
    actual = state.get("route", "") if isinstance(state, dict) else ""
    if actual == expected_value:
        return {"verdict": "PASS", "reason": f"route={actual!r}"}
    return {"verdict": "FAIL",
            "reason": f"route={actual!r}, expected {expected_value!r}"}


_PROBE_RE = re.compile(r"\*\*\s*to advance\s*:\s*\*\*", re.IGNORECASE)


def _check_probe(*, response_text: str, expected_value, **_) -> dict:
    has_probe = bool(_PROBE_RE.search(response_text))
    want = bool(expected_value)
    if has_probe == want:
        return {"verdict": "PASS",
                "reason": "probe present" if want else "probe absent"}
    return {"verdict": "FAIL",
            "reason": ("probe missing — should end with **To advance:**"
                       if want else "probe present but row expected none")}


def _check_word_count(*, response_text: str, expected_value, **_) -> dict:
    n = len(response_text.split())
    if n <= int(expected_value):
        return {"verdict": "PASS", "reason": f"{n} words (cap {expected_value})"}
    return {"verdict": "FAIL",
            "reason": f"{n} words exceeds cap {expected_value}"}


def _check_persisted_facts(*, state, expected_value, **_) -> dict:
    """Each entry in expected_value is {field_path, value}. Compare against
    the post-turn customer_profile (read via to_dict) along the dotted path."""
    expected_facts = expected_value or []
    profile = state.get("customer_profile") if isinstance(state, dict) else None
    profile_dict = (profile.to_dict() if profile is not None
                    and hasattr(profile, "to_dict") else None)
    if profile_dict is None:
        return {"verdict": "FAIL", "reason": "no customer_profile in state"}

    misses = []
    for ef in expected_facts:
        fp = ef.get("field_path")
        want = ef.get("value")
        actual = _walk_dotted(profile_dict, fp)
        if actual != want:
            misses.append(f"{fp}: got {actual!r}, want {want!r}")
    if misses:
        return {"verdict": "FAIL", "reason": "; ".join(misses)}
    return {"verdict": "PASS",
            "reason": f"all {len(expected_facts)} expected facts present"}


def _check_persisted_decisions(*, state, expected_value, **_) -> dict:
    """Each expected entry is {category, value}. Compare against
    profile.decisions_made (active only)."""
    expected_decs = expected_value or []
    profile = state.get("customer_profile") if isinstance(state, dict) else None
    if profile is None or not hasattr(profile, "decisions_made"):
        return {"verdict": "FAIL", "reason": "no customer_profile in state"}

    active = [d for d in profile.decisions_made
              if d.superseded_by_turn is None]

    # Special case: empty list means "must NOT persist anything new".
    # Used by extract-02 (questions don't extract decisions).
    if expected_decs == []:
        if active:
            return {"verdict": "FAIL",
                    "reason": f"expected no decisions but found "
                              f"{[(d.category, d.value) for d in active]}"}
        return {"verdict": "PASS", "reason": "no decisions persisted (correct)"}

    misses = []
    for ed in expected_decs:
        cat = ed.get("category")
        val = ed.get("value")
        if not any(d.category == cat and d.value == val for d in active):
            misses.append(f"{cat}={val!r} not present")
    if misses:
        return {"verdict": "FAIL", "reason": "; ".join(misses)}
    return {"verdict": "PASS",
            "reason": f"all {len(expected_decs)} decisions present"}


def _check_contradiction(*, state, expected_value, **_) -> dict:
    want = expected_value or {}
    pc = state.get("pending_contradictions", []) if isinstance(state, dict) else []
    matched = any(
        c.get("field_path") == want.get("field_path")
        and c.get("old_value") == want.get("old_value")
        and c.get("new_value") == want.get("new_value")
        for c in pc
    )
    if matched:
        return {"verdict": "PASS",
                "reason": f"contradiction surfaced for {want.get('field_path')}"}
    return {"verdict": "FAIL",
            "reason": f"expected contradiction {want} not in pending_contradictions={pc}"}


def _check_tool_calls(*, state, expected_value, **_) -> dict:
    """Verify that specific MCP tools were routed (present in state.mcp_tools).

    expected_value is a list of tool names, e.g. ["WebSearch", "get_pricing"].
    Each must appear as a tool name in the mcp_tools list on the final state.
    """
    expected_tools = expected_value or []
    actual_tools = [t.get("tool", "") for t in (state.get("mcp_tools") or [])]
    missing = [t for t in expected_tools if t not in actual_tools]
    if missing:
        return {"verdict": "FAIL",
                "reason": f"expected tools {missing} not in actual {actual_tools}"}
    return {"verdict": "PASS",
            "reason": f"all expected tools {expected_tools} present in {actual_tools}"}


def _check_sources_footer(*, response_text: str, expected_value, **_) -> dict:
    """Verify that the response includes a **Sources:** section with URLs.

    expected_value is a bool (True means the section must be present).
    """
    want = bool(expected_value)
    has_sources = bool(re.search(r"\*\*Sources:?\*\*", response_text, re.IGNORECASE))
    has_url = bool(re.search(r"https?://", response_text))
    present = has_sources and has_url
    if present == want:
        return {"verdict": "PASS",
                "reason": "Sources footer with URLs present" if want
                          else "No sources footer (correct)"}
    if want:
        return {"verdict": "FAIL",
                "reason": "expected a **Sources:** section with URLs but none found"}
    return {"verdict": "FAIL",
            "reason": "found a Sources footer but row expected none"}


# Map criterion name → grader function. Filled after the helpers are defined
# so we don't have to forward-declare.
_DET_GRADERS = {
    "must_avoid":              _check_must_avoid,
    "expected_route":          _check_route,
    "must_end_with_probe":     _check_probe,
    "max_response_words":      _check_word_count,
    "persisted_facts":         _check_persisted_facts,
    "persisted_decisions":     _check_persisted_decisions,
    "contradiction_surfaced":  _check_contradiction,
    "expected_tool_calls":     _check_tool_calls,
    "must_include_sources_footer": _check_sources_footer,
}


# ---------------------------------------------------------------------------
# Subjective grader — one Sonnet call per row, batched across criteria
# ---------------------------------------------------------------------------

_SONNET_PROMPT = """You are a strict grader for an LLM agent's response. The
agent assists AWS Solutions Architects with mainframe modernization for
Financial Services customers.

For each criterion below, decide PASS or FAIL independently. Do not let
verdicts on one criterion influence another. Output a single JSON object
with one entry per criterion.

USER PROMPT (what the SA said to the agent):
{prompt}

AGENT RESPONSE (what the agent replied):
{response_text}

CRITERIA TO GRADE:
{criteria_block}

Return ONLY a JSON object — no prose, no markdown fences. Shape:
{{
  "<criterion_name>": {{"verdict": "PASS" | "FAIL", "reason": "<one sentence>"}},
  ...
}}
"""


def _sonnet_batch_judge(*, prompt: str, response_text: str,
                       criteria: dict) -> dict:
    """One Sonnet call covering every subjective criterion in `criteria`.

    `criteria` is name → criterion-value (the raw thing from `expected`).
    The criterion's grading rule comes from SUBJECTIVE_CRITERIA[name].
    """
    # Lazy import — keeps `from evals.judge import judge_response` cheap
    # for callers that only use the deterministic path (e.g. unit tests).
    from agent.config import get_response_llm
    from langchain_core.messages import HumanMessage

    blocks = []
    for name, value in criteria.items():
        rule = SUBJECTIVE_CRITERIA.get(name, "(no rule defined)")
        # `value` may be a list (must_mention) or a bool (no_customer_specific_claims)
        # or a free-text string (grounding_check). Render literally.
        blocks.append(
            f"- {name}\n"
            f"    rule: {rule}\n"
            f"    criterion_value: {json.dumps(value)}"
        )
    criteria_block = "\n".join(blocks)

    body = _SONNET_PROMPT.format(
        prompt=prompt[:4000],   # defensive caps
        response_text=response_text[:8000],
        criteria_block=criteria_block,
    )

    llm = get_response_llm()  # Sonnet (the response-tier model)
    raw = llm.invoke([HumanMessage(content=body)]).content
    if not isinstance(raw, str):
        raw = str(raw)

    parsed = _extract_json_object(raw)
    if not parsed:
        # Failure path — still return a verdict per criterion so the row
        # doesn't silently lose the criteria.
        return {
            name: {"verdict": "FAIL",
                   "reason": f"could not parse Sonnet response: {raw[:200]!r}"}
            for name in criteria
        }

    out: dict[str, dict] = {}
    for name in criteria:
        v = parsed.get(name)
        if not isinstance(v, dict) or "verdict" not in v:
            out[name] = {"verdict": "FAIL",
                         "reason": f"Sonnet omitted verdict for {name}"}
        else:
            verdict = str(v.get("verdict", "")).upper()
            if verdict not in ("PASS", "FAIL"):
                verdict = "FAIL"
            out[name] = {
                "verdict": verdict,
                "reason": str(v.get("reason", ""))[:500],
            }
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk_dotted(d: dict, path: str | None):
    """Walk a dotted-path through nested dicts. Returns None if any segment
    is missing. None vs missing is distinguished only by absence."""
    if not path:
        return None
    cur: Any = d
    for segment in path.split("."):
        if not isinstance(cur, dict) or segment not in cur:
            return None
        cur = cur[segment]
    return cur


def _extract_json_object(raw: str) -> dict:
    """Tolerate markdown fences and surrounding prose around the JSON blob."""
    if not isinstance(raw, str):
        return {}
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.startswith("json"):
            s = s[4:]
        s = s.strip()
    # Find the first {...} balanced block
    if not s.startswith("{"):
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if m:
            s = m.group(0)
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------------------
# Smoke test — run `python -m evals.judge` from the repo root
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Two small fixtures exercising the dispatcher end-to-end without
    # needing a real agent run. The first row only uses deterministic
    # criteria — exercises the no-Sonnet path. The second includes a
    # subjective criterion to exercise the batched Sonnet call (only
    # triggers if you've configured agent/config.py with creds).

    print("=" * 72)
    print("Fixture 1 — deterministic-only (no Sonnet call):")
    print("=" * 72)
    fixture1 = {
        "prompt": "skip",
        "response": "Got it — moving on. What would you like to focus on?",
        "expected": {
            "expected_route": "defer",
            "max_response_words": 40,
            "must_avoid": ["[KB]", "[MCP", "To advance:"],
        },
        "state": {"route": "defer"},
    }
    res = judge_response(
        prompt=fixture1["prompt"],
        response_text=fixture1["response"],
        expected=fixture1["expected"],
        post_turn_state=fixture1["state"],
    )
    print(json.dumps(res, indent=2))

    print()
    print("=" * 72)
    print("Fixture 2 — adds a subjective criterion (will hit Sonnet):")
    print("=" * 72)
    fixture2 = {
        "prompt": "Walk me through the migration patterns.",
        "response": (
            "The five major patterns are rehost (lift and shift), replatform, "
            "refactor, repurchase, and retire. Each makes different trade-offs "
            "between speed, cost, and target architecture flexibility.\n\n"
            "**To advance:** Which of these matters most to your customer — "
            "speed of migration or target-architecture freedom?"
        ),
        "expected": {
            "must_avoid": ["[KB]", "[MCP"],
            "expected_route": "kb",
            "must_end_with_probe": True,
            "must_mention": ["rehost", "replatform", "refactor", "retire"],
            "no_customer_specific_claims": True,
        },
        "state": {"route": "kb"},
    }
    try:
        res = judge_response(
            prompt=fixture2["prompt"],
            response_text=fixture2["response"],
            expected=fixture2["expected"],
            post_turn_state=fixture2["state"],
        )
        print(json.dumps(res, indent=2))
    except Exception as e:
        print(f"(skipped — Sonnet call needs AWS creds: {e})")
