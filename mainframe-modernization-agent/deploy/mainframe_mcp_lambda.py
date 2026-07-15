"""Gateway-target Lambda for the Mainframe Modernization MCP tools.

Iteration 1.5 deployment:
- Imports the same helpers + data files that mcp_server/ uses (single
  source of truth for tool logic).
- Returns Gateway-shaped responses ({"content": [...]} blocks).
- Uses the structured response shape (prose summary + JSON payload +
  data_version stamp) that the agent expects.

Function name in production: MfModAgent-MainframeMCP
Replaces the old AgentCoreLambdaTestFunction.
"""
from __future__ import annotations

import json
import logging

# We can't import mcp_server/server.py directly (FastMCP decorators bind to
# a server) — so we re-implement the dispatch surface here, calling into
# the shared helpers + data files. Keeping this file flat means the Lambda
# package stays small and we don't pull in fastmcp at runtime.
from _helpers import (  # noqa: E402
    ToolInputError,
    coerce_to_list,
    computation_result,
    constraints_from_profile,
    data_version_of,
    load_data,
    lookup_in_dict,
    payload_only,
    provenance_of,
    require_choice,
    score_subjects,
    source_tokens_from_workload,
    structured_response,
    workload_from_profile,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Tool implementations (parallel to mcp_server/server.py — same data, same
# response shape)
# ---------------------------------------------------------------------------

def _lookup_category_and_name(*, file, tool_name, category, name, category_choices):
    data = load_data(file)
    chosen_category = require_choice(category, category_choices, arg_name="category")
    cat_data = payload_only(data[chosen_category])

    if not name:
        summary = (
            f"Category '{chosen_category}' has {len(cat_data)} entries: "
            f"{', '.join(list(cat_data.keys())[:10])}"
            f"{'...' if len(cat_data) > 10 else ''}."
        )
        return structured_response(
            summary=summary,
            payload={"category": chosen_category, "entries": cat_data},
            data_version=data_version_of(file),
            tool_name=tool_name,
        )

    result = lookup_in_dict(name, cat_data)
    mt = result["match_type"]

    if mt == "exact":
        match = result["matches"][0]
        return structured_response(
            summary=f"Exact match for {name!r} in '{chosen_category}': {match['key']}.",
            payload={"category": chosen_category, "match": match, "match_type": "exact"},
            data_version=data_version_of(file),
            tool_name=tool_name,
        )

    if mt == "partial":
        labels = ", ".join(m["key"] for m in result["matches"])
        return structured_response(
            summary=(f"No exact match for {name!r} in '{chosen_category}'. "
                     f"Top {len(result['matches'])} partial matches: {labels}."),
            payload={"category": chosen_category, "match_type": "partial",
                     "matches": result["matches"]},
            data_version=data_version_of(file),
            tool_name=tool_name,
        )

    return structured_response(
        summary=(f"No match for {name!r} in '{chosen_category}'. "
                 f"Available: {', '.join(result['available_keys'])}."),
        payload={"category": chosen_category, "match_type": "miss",
                 "available_keys": result["available_keys"]},
        data_version=data_version_of(file),
        tool_name=tool_name,
    )


def lookup_cobol_pattern(category, name=""):
    return _lookup_category_and_name(
        file="cobol_patterns.json",
        tool_name="lookup_cobol_pattern",
        category=category, name=name,
        category_choices=["data_types", "file_handling",
                          "program_structure", "common_verbs"],
    )


def lookup_jcl_reference(category, name=""):
    return _lookup_category_and_name(
        file="jcl_reference.json",
        tool_name="lookup_jcl_reference",
        category=category, name=name,
        category_choices=["statements", "common_utilities"],
    )


def map_mainframe_to_aws(category, name=""):
    return _lookup_category_and_name(
        file="mainframe_aws_mapping.json",
        tool_name="map_mainframe_to_aws",
        category=category, name=name,
        category_choices=["compute", "data", "middleware", "security", "monitoring"],
    )


def get_migration_pattern(name=""):
    file = "migration_patterns.json"
    data = load_data(file)
    patterns = payload_only(data["patterns"])

    if not name:
        summary_table = {
            k: {"name": v["name"], "complexity": v["complexity"],
                "timeline": v["timeline"], "cost_savings": v["cost_savings"]}
            for k, v in patterns.items()
        }
        bullets = "\n".join(
            f"- **{v['name']}** ({k}): {v['complexity']} complexity, "
            f"{v['timeline']}, {v['cost_savings']} cost savings"
            for k, v in patterns.items()
        )
        return structured_response(
            summary=f"5 migration patterns available:\n{bullets}",
            payload={"patterns": summary_table},
            data_version=data_version_of(file),
            tool_name="get_migration_pattern",
        )

    chosen = require_choice(name, list(patterns.keys()), arg_name="name")
    p = patterns[chosen]
    return structured_response(
        summary=(f"**{p['name']}** — {p['complexity']} complexity, "
                 f"{p['timeline']}, {p['cost_savings']} cost savings. "
                 f"Best for: {p['best_for']}."),
        payload={"pattern": chosen, "details": p},
        data_version=data_version_of(file),
        tool_name="get_migration_pattern",
    )


# Single source of truth for the complexity inputs. None sentinels mean
# "the SA didn't state it this turn" → may be seeded from the profile.
_COMPLEXITY_INPUTS = {
    "num_cobol_programs": None, "num_jcl_jobs": None,
    "has_cics": False, "has_db2": False, "has_ims": False, "has_mq": False,
    "num_vsam_files": 0, "num_copybooks": 0,
}


def _merge_complexity_inputs(profile, overrides):
    """Resolve the 8 scorer inputs from (profile defaults) ← (explicit args).

    Returns (resolved, missing). An input is "missing" when it is one of the
    two required counts (num_cobol_programs / num_jcl_jobs) and NEITHER the
    profile nor an explicit arg supplied it — those drive the confidence band.
    Mirrors mcp_server/server.py:_merge_complexity_inputs after FIXES.md #13.
    """
    workload = workload_from_profile(profile)
    resolved = dict(_COMPLEXITY_INPUTS)

    for key in resolved:
        if workload.get(key) is not None:
            resolved[key] = workload[key]

    for key, val in overrides.items():
        if val is not None:
            resolved[key] = val

    missing = [k for k in ("num_cobol_programs", "num_jcl_jobs")
               if resolved.get(k) is None]
    return resolved, missing


def _score_complexity(inputs, cfg):
    score = 0
    factors = []

    for arg_name in ("num_cobol_programs", "num_jcl_jobs"):
        value = inputs.get(arg_name) or 0
        for bucket in cfg["buckets"][arg_name]:
            cap = bucket["max"]
            if cap is None or value <= cap:
                score += bucket["weight"]
                label = arg_name.replace("num_", "").replace("_", " ")
                factors.append(f"{value} {label} ({bucket['label']}, +{bucket['weight']})")
                break

    for flag in ("has_cics", "has_db2", "has_ims", "has_mq"):
        if inputs.get(flag) and flag in cfg["flags"]:
            spec = cfg["flags"][flag]
            score += spec["weight"]
            factors.append(f"{spec['factor']} (+{spec['weight']})")

    n_vsam = inputs.get("num_vsam_files") or 0
    if n_vsam > 0:
        spec = cfg["graduated"]["num_vsam_files"]
        w = min(n_vsam // spec["step_divisor"] + spec["step_weight"], spec["max_weight"])
        score += w
        factors.append(spec["factor_template"].format(n=n_vsam) + f" (+{w})")
    n_cpy = inputs.get("num_copybooks") or 0
    if n_cpy > 0:
        spec = cfg["graduated"]["num_copybooks"]
        if n_cpy > spec["threshold"]:
            score += spec["weight_above"]
            factors.append(spec["factor_template"].format(n=n_cpy)
                           + f" (+{spec['weight_above']})")

    chosen_level = cfg["levels"][-1]
    for lvl in cfg["levels"]:
        cap = lvl["max_score"]
        if cap is None or score <= cap:
            chosen_level = lvl
            break
    return score, factors, chosen_level


def estimate_complexity(num_cobol_programs=None, num_jcl_jobs=None,
                        has_cics=None, has_db2=None, has_ims=None, has_mq=None,
                        num_vsam_files=None, num_copybooks=None,
                        profile=None, **_unused):
    """Soft-missing + profile-seam complexity estimator (FIXES.md #11).

    Treats absent required counts as 0 with confidence='low' instead of
    hard-raising — so a sizing question without exact numbers returns a
    flagged-low estimate the agent can probe to firm up, rather than an
    isError block. Mirrors mcp_server/server.py.
    """
    cfg = load_data("complexity_scoring.json")

    overrides = {
        "num_cobol_programs": num_cobol_programs, "num_jcl_jobs": num_jcl_jobs,
        "has_cics": has_cics, "has_db2": has_db2,
        "has_ims": has_ims, "has_mq": has_mq,
        "num_vsam_files": num_vsam_files, "num_copybooks": num_copybooks,
    }
    resolved, missing = _merge_complexity_inputs(profile, overrides)
    score, factors, chosen_level = _score_complexity(resolved, cfg)

    inputs_echo = {k: v for k, v in resolved.items() if v not in (None, False, 0)}

    compute = computation_result(
        value=score,
        factors=factors,
        inputs_echo=inputs_echo,
        missing_inputs=missing,
        data_version=data_version_of("complexity_scoring.json"),
        extra={
            "max_score": cfg["max_score"],
            "complexity_level": chosen_level["level"],
            "recommended_pattern": chosen_level["recommended_pattern"],
            "estimated_timeline": chosen_level["timeline"],
            "scoring_criteria_used": {
                "buckets": cfg["buckets"], "flags": cfg["flags"],
                "graduated": cfg["graduated"], "levels": cfg["levels"],
            },
        },
    )

    conf_note = "" if not missing else (
        f" _Confidence {compute['confidence']}: missing {', '.join(missing)} "
        f"(treated as 0 — confirm to firm up the estimate)._"
    )
    summary = (
        f"Complexity score **{score}/{cfg['max_score']}** — **{chosen_level['level']}**. "
        f"Recommended pattern: **{chosen_level['recommended_pattern']}** "
        f"({chosen_level['timeline']}). "
        f"Top drivers: {'; '.join(factors[:4])}.{conf_note}"
    )
    return structured_response(
        summary=summary,
        payload=compute,
        data_version=data_version_of("complexity_scoring.json"),
        tool_name="estimate_complexity",
        provenance=provenance_of("complexity_scoring.json"),
    )


def get_fsi_compliance_check(regulation="", customer_profile_summary=""):
    file = "fsi_compliance.json"
    data = load_data(file)
    regs = payload_only(data["regulations"])

    if not regulation:
        summary = (
            f"General FSI best practices ({len(data['general_best_practices'])} items). "
            f"Available regulations: {', '.join(regs.keys())}."
        )
        if customer_profile_summary:
            summary = "For this customer:\n" + customer_profile_summary + "\n\n" + summary
        return structured_response(
            summary=summary,
            payload={"available_regulations": list(regs.keys()),
                     "general_best_practices": data["general_best_practices"]},
            data_version=data_version_of(file),
            tool_name="get_fsi_compliance_check",
        )

    chosen = require_choice(regulation, list(regs.keys()), arg_name="regulation")
    r = regs[chosen]
    summary = f"**{r['name']}** — applies to: {r['applies_to']}."
    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary
                   + f"\n\n**Regulatory focus:** {summary}")
    return structured_response(
        summary=summary,
        payload={"regulation": chosen, "details": r},
        data_version=data_version_of(file),
        tool_name="get_fsi_compliance_check",
    )


def compare_partner_tools(source_systems=None, pattern="",
                          aws_m2_managed_only=False, top_n=5,
                          customer_profile_summary=""):
    file = "partner_tools.json"
    data = load_data(file)
    tools = payload_only(data["tools"])

    wanted_sources = [s.upper() for s in coerce_to_list(source_systems)]
    pattern = (pattern or "").strip().lower()

    scored: list[dict] = []
    for key, t in tools.items():
        if aws_m2_managed_only and not t.get("aws_m2_managed"):
            continue
        if pattern and pattern not in [p.lower().replace(" ", "_")
                                       for p in t.get("pattern", [])]:
            continue
        fit_cov = [s for s in wanted_sources
                   if s in [x.upper() for x in t.get("source_fit", [])]]
        source_fit_score = (len(fit_cov) / len(wanted_sources)) if wanted_sources else 1.0
        managed_boost = 0.2 if t.get("aws_m2_managed") else 0.0
        total = round(source_fit_score + managed_boost, 2)

        scored.append({
            "tool_key": key, "name": t["name"],
            "pattern": t.get("pattern", []),
            "source_fit_coverage": fit_cov,
            "source_fit_missing": [s for s in wanted_sources
                                   if s not in [x.upper() for x in t.get("source_fit", [])]],
            "aws_m2_managed": t.get("aws_m2_managed", False),
            "typical_timeline": t.get("typical_timeline"),
            "strengths": t.get("strengths", []),
            "weaknesses": t.get("weaknesses", []),
            "fsi_considerations": t.get("fsi_considerations"),
            "reference_urls": t.get("reference_urls", []),
            "score": total,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[: int(top_n or 5)]

    if top:
        summary = (
            f"Top fit: **{top[0]['name']}** (score {top[0]['score']})."
            + (f" Coverage: {', '.join(top[0]['source_fit_coverage'])}."
               if top[0]['source_fit_coverage'] else "")
        )
        if len(top) > 1:
            summary += f" Runners-up: " + ", ".join(
                f"{t['name']} ({t['score']})" for t in top[1:3]
            ) + "."
    else:
        summary = "No partner tool matched the given filters."

    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary + "\n\n" + summary)

    return structured_response(
        summary=summary,
        payload={
            "filters": {
                "source_systems": wanted_sources,
                "pattern": pattern or "any",
                "aws_m2_managed_only": aws_m2_managed_only,
            },
            "criteria": data.get("decision_criteria", []),
            "ranked": top,
        },
        data_version=data_version_of(file),
        tool_name="compare_partner_tools",
    )


def compare_services(source, candidates=None, customer_profile_summary=""):
    file = "service_comparisons.json"
    data = load_data(file)
    comps = payload_only(data["comparisons"])

    src_key = require_choice(source, list(comps.keys()), arg_name="source")
    entry = comps[src_key]
    all_candidates = entry["candidates"]

    wanted = set(coerce_to_list(candidates))
    filtered = ({k: v for k, v in all_candidates.items() if k in wanted}
                if wanted else all_candidates)

    fit_rank = {"high": 4, "high_if_redesigned": 3, "medium": 2,
                "case_by_case": 2, "low": 1, "low_for_OLTP": 1}
    ranked = sorted(
        [{"service": k, **v} for k, v in filtered.items()],
        key=lambda x: fit_rank.get(x.get("fit", "low"), 0),
        reverse=True,
    )

    if ranked:
        primary = ranked[0]
        summary = (f"For {entry['source']}: **primary = {primary['service']}** "
                   f"(fit={primary['fit']}). "
                   f"When to choose: {primary.get('when_to_choose', '—')}")
    else:
        summary = "No candidates matched the filter."

    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary + "\n\n" + summary)

    return structured_response(
        summary=summary,
        payload={
            "source": entry["source"],
            "description": entry["description"],
            "candidates": ranked,
        },
        data_version=data_version_of(file),
        tool_name="compare_services",
    )


def list_taxonomy(tool_name=""):
    taxonomy = {
        "lookup_cobol_pattern": {
            "category": ["data_types", "file_handling", "program_structure", "common_verbs"],
            "name": "any key from the chosen category (or empty to list category)",
            "data_file": "cobol_patterns.json",
        },
        "lookup_jcl_reference": {
            "category": ["statements", "common_utilities"],
            "name": "any key from the chosen category",
            "data_file": "jcl_reference.json",
        },
        "map_mainframe_to_aws": {
            "category": ["compute", "data", "middleware", "security", "monitoring"],
            "name": "any component key from the chosen category",
            "data_file": "mainframe_aws_mapping.json",
        },
        "get_migration_pattern": {
            "name": list(payload_only(load_data("migration_patterns.json")["patterns"]).keys()),
            "data_file": "migration_patterns.json",
        },
        "estimate_complexity": {
            "required": ["num_cobol_programs (int)", "num_jcl_jobs (int)"],
            "optional_flags": ["has_cics", "has_db2", "has_ims", "has_mq"],
            "optional_counts": ["num_vsam_files", "num_copybooks"],
            "data_file": "complexity_scoring.json",
            "note": "Weights & thresholds in data file; response includes them.",
        },
        "get_fsi_compliance_check": {
            "regulation": list(payload_only(load_data("fsi_compliance.json")["regulations"]).keys()),
            "customer_profile_summary": "optional grounding string",
            "data_file": "fsi_compliance.json",
        },
        "compare_partner_tools": {
            "source_systems": "list[str] OR comma-separated string",
            "pattern": ["rehost", "replatform", "refactor", "retire", "automated_refactor"],
            "aws_m2_managed_only": "bool",
            "top_n": "int (default 5)",
            "data_file": "partner_tools.json",
        },
        "compare_services": {
            "source": list(payload_only(load_data("service_comparisons.json")["comparisons"]).keys()),
            "candidates": "list[str] OR comma-separated string (optional)",
            "data_file": "service_comparisons.json",
        },
    }

    if not tool_name:
        return structured_response(
            summary=f"{len(taxonomy)} tools available: {', '.join(taxonomy.keys())}.",
            payload={"tools": taxonomy},
            data_version="taxonomy",
            tool_name="list_taxonomy",
        )

    chosen = require_choice(tool_name, list(taxonomy.keys()), arg_name="tool_name")
    return structured_response(
        summary=f"Argument shape for {chosen}: see Data section.",
        payload={"tool": chosen, "argument_shape": taxonomy[chosen]},
        data_version="taxonomy",
        tool_name="list_taxonomy",
    )


# ---------------------------------------------------------------------------
# Lambda dispatch
# ---------------------------------------------------------------------------

TOOLS = {
    "lookup_cobol_pattern": lookup_cobol_pattern,
    "lookup_jcl_reference": lookup_jcl_reference,
    "map_mainframe_to_aws": map_mainframe_to_aws,
    "get_migration_pattern": get_migration_pattern,
    "estimate_complexity": estimate_complexity,
    "get_fsi_compliance_check": get_fsi_compliance_check,
    "compare_partner_tools": compare_partner_tools,
    "compare_services": compare_services,
    "list_taxonomy": list_taxonomy,
}

DELIMITER = "___"


def lambda_handler(event, context):
    """Dispatch a Gateway-targeted MCP tool call.

    Two formats supported:
      - Gateway:  tool name in context.client_context.custom['bedrockAgentCoreToolName']
                  (formatted as "<targetName>___<toolName>"), arguments == event
      - Legacy:   {"name": "<tool>", "arguments": {...}}
    """
    tool_name = None
    arguments = event if isinstance(event, dict) else {}

    # Gateway format
    try:
        original = context.client_context.custom.get("bedrockAgentCoreToolName", "")
        if original and DELIMITER in original:
            tool_name = original[original.index(DELIMITER) + len(DELIMITER):]
    except (AttributeError, TypeError):
        pass

    # Legacy format
    if not tool_name and isinstance(event, dict) and "name" in event:
        tool_name = event["name"]
        arguments = event.get("arguments", {}) or {}

    if not tool_name:
        return {
            "content": [{"type": "text", "text": "No tool name provided"}],
            "isError": True,
        }

    fn = TOOLS.get(tool_name)
    if not fn:
        return {
            "content": [{
                "type": "text",
                "text": f"Unknown tool: {tool_name}. "
                        f"Available: {', '.join(TOOLS.keys())}",
            }],
            "isError": True,
        }

    try:
        result = fn(**(arguments or {}))
        return {"content": [{"type": "text", "text": result}]}
    except ToolInputError as e:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": str(e),
                    "error_code": e.error_code,
                    "valid_options": e.valid_options,
                }),
            }],
            "isError": True,
        }
    except Exception as e:
        logger.exception("Tool call failed: %s", tool_name)
        return {
            "content": [{
                "type": "text",
                "text": f"Error calling {tool_name}: {str(e)}",
            }],
            "isError": True,
        }
