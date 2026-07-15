"""Mainframe Modernization MCP Server.

Reference data tools for COBOL, JCL, mainframe-to-AWS mappings, migration
patterns, FSI compliance, partner-tool comparison, and AWS service comparison.

Iteration 1.5 refactor (2026-05-26):
  - Structured responses (prose summary + JSON payload + data_version)   [1.5.1]
  - Consistent argument shapes (`name` everywhere; list[str] for collections;
    Workload accepted as a flat dict for estimate_complexity)             [1.5.2]
  - Every response stamped with the data file's _version                  [1.5.3]
  - Exact/partial-match lookup helper (no more silent first-iteration
    false positives)                                                      [1.5.4]
  - New `list_taxonomy` meta-tool — discoverable categories               [1.5.5]
  - estimate_complexity scoring externalized to JSON config               [1.5.6]
  - Typed errors (ToolInputError) instead of 200-OK error strings         [1.5.7]
  - FSI tools accept optional customer_profile_summary for grounded
    responses                                                             [1.5.8]
"""
from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

# Dual-mode import: the installed console script (`uv run mfmod-mcp`) loads
# server.py as a TOP-LEVEL module — it has no parent package, so a relative
# import would raise ImportError. The test suite imports via
# `from mcp_server import server` which DOES have a parent package, so a
# top-level import would fail there. Prefer the relative form (cheap when it
# works) and fall back to top-level for the no-parent-package case. The
# proper fix is the single-shared-package consolidation in FIXES.md #5.
try:
    from ._helpers import (  # type: ignore[no-redef]
        ToolInputError,
        coerce_to_list,
        computation_result,
        constraints_from_profile,
        data_version_of,
        load_data,
        lookup_in_dict,
        normalize_name,
        payload_only,
        provenance_of,
        require_choice,
        score_subjects,
        source_tokens_from_workload,
        structured_response,
        workload_from_profile,
    )
except ImportError:
    from _helpers import (  # type: ignore[no-redef]
        ToolInputError,
        coerce_to_list,
        computation_result,
        constraints_from_profile,
        data_version_of,
        load_data,
        lookup_in_dict,
        normalize_name,
        payload_only,
        provenance_of,
        require_choice,
        score_subjects,
        source_tokens_from_workload,
        structured_response,
        workload_from_profile,
    )

mcp = FastMCP(
    "mainframe-reference",
    instructions=(
        "Mainframe modernization reference data for FSI customers migrating to AWS. "
        "Use `list_taxonomy` to discover valid arguments for any tool."
    ),
)


# ---------------------------------------------------------------------------
# Helper: shape a category-and-name lookup result
# ---------------------------------------------------------------------------

def _lookup_category_and_name(
    *,
    file: str,
    tool_name: str,
    category: str,
    name: str,
    category_choices: list[str],
) -> str:
    """Common shape used by lookup_cobol_pattern, lookup_jcl_reference, and
    map_mainframe_to_aws. Returns a structured_response string."""
    data = load_data(file)
    chosen_category = require_choice(category, category_choices, arg_name="category")
    cat_data = payload_only(data[chosen_category])

    if not name:
        # List the whole category
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
        summary = f"Exact match for {name!r} in '{chosen_category}': {match['key']}."
        return structured_response(
            summary=summary,
            payload={"category": chosen_category, "match": match,
                     "match_type": "exact"},
            data_version=data_version_of(file),
            tool_name=tool_name,
        )

    if mt == "partial":
        labels = ", ".join(m["key"] for m in result["matches"])
        summary = (
            f"No exact match for {name!r} in '{chosen_category}'. "
            f"Top {len(result['matches'])} partial matches: {labels}."
        )
        return structured_response(
            summary=summary,
            payload={"category": chosen_category, "match_type": "partial",
                     "matches": result["matches"]},
            data_version=data_version_of(file),
            tool_name=tool_name,
        )

    # miss
    summary = (
        f"No match for {name!r} in '{chosen_category}'. "
        f"Available: {', '.join(result['available_keys'])}."
    )
    return structured_response(
        summary=summary,
        payload={"category": chosen_category, "match_type": "miss",
                 "available_keys": result["available_keys"]},
        data_version=data_version_of(file),
        tool_name=tool_name,
    )


# ---------------------------------------------------------------------------
# Tool 1: COBOL patterns
# ---------------------------------------------------------------------------

@mcp.tool()
def lookup_cobol_pattern(category: str, name: str = "") -> str:
    """Look up COBOL syntax patterns and their AWS equivalents.

    Args:
        category: One of: data_types, file_handling, program_structure, common_verbs.
        name: Specific pattern name (e.g. 'PIC 9', 'PERFORM'). Empty → list category.
    """
    return _lookup_category_and_name(
        file="cobol_patterns.json",
        tool_name="lookup_cobol_pattern",
        category=category,
        name=name,
        category_choices=["data_types", "file_handling", "program_structure", "common_verbs"],
    )


# ---------------------------------------------------------------------------
# Tool 2: JCL reference
# ---------------------------------------------------------------------------

@mcp.tool()
def lookup_jcl_reference(category: str, name: str = "") -> str:
    """Look up JCL statements / utilities with AWS equivalents.

    Args:
        category: One of: statements, common_utilities.
        name: Specific JCL statement / utility (e.g. 'JOB', 'SORT', 'IDCAMS').
    """
    return _lookup_category_and_name(
        file="jcl_reference.json",
        tool_name="lookup_jcl_reference",
        category=category,
        name=name,
        category_choices=["statements", "common_utilities"],
    )


# ---------------------------------------------------------------------------
# Tool 3: mainframe → AWS mapping
# ---------------------------------------------------------------------------

@mcp.tool()
def map_mainframe_to_aws(category: str, name: str = "") -> str:
    """Map mainframe components to AWS service equivalents.

    Args:
        category: One of: compute, data, middleware, security, monitoring.
        name: Specific component (e.g. 'CICS', 'DB2', 'VSAM_KSDS', 'RACF', 'MQ_SERIES').
    """
    return _lookup_category_and_name(
        file="mainframe_aws_mapping.json",
        tool_name="map_mainframe_to_aws",
        category=category,
        name=name,
        category_choices=["compute", "data", "middleware", "security", "monitoring"],
    )


# ---------------------------------------------------------------------------
# Tool 4: migration patterns
# ---------------------------------------------------------------------------

@mcp.tool()
def get_migration_pattern(name: str = "") -> str:
    """Get mainframe migration pattern details (steps, timeline, FSI considerations).

    Args:
        name: One of: rehost, replatform, refactor, retire, automated_refactor.
              Empty → return a summary of all patterns.
    """
    file = "migration_patterns.json"
    data = load_data(file)
    patterns = payload_only(data["patterns"])

    if not name:
        summary_table = {
            k: {
                "name": v["name"],
                "complexity": v["complexity"],
                "timeline": v["timeline"],
                "cost_savings": v["cost_savings"],
            }
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

    chosen = require_choice(
        name,
        list(patterns.keys()),
        arg_name="name",
    )
    p = patterns[chosen]
    summary = (
        f"**{p['name']}** — {p['complexity']} complexity, {p['timeline']}, "
        f"{p['cost_savings']} cost savings. Best for: {p['best_for']}."
    )
    return structured_response(
        summary=summary,
        payload={"pattern": chosen, "details": p},
        data_version=data_version_of(file),
        tool_name="get_migration_pattern",
    )


# ---------------------------------------------------------------------------
# Tool 5: complexity estimation (1.5.6 — externalized scoring)
# ---------------------------------------------------------------------------

# Inputs the scorer reads, with their defaults. Single source of truth shared
# by the profile-seam merge, the missing_inputs computation, and the tests.
_COMPLEXITY_INPUTS: dict[str, Any] = {
    "num_cobol_programs": None,
    "num_jcl_jobs": None,
    "has_cics": False,
    "has_db2": False,
    "has_ims": False,
    "has_mq": False,
    "num_vsam_files": 0,
    "num_copybooks": 0,
}


def _score_complexity(inputs: dict, cfg: dict) -> tuple[int, list[str], dict]:
    """Pure scoring math — returns (score, factors, chosen_level).

    Factored out of the tool so it is unit-testable in isolation and reusable
    by other deterministic tools. No I/O, no response shaping.
    """
    score = 0
    factors: list[str] = []

    # Bucketed weights
    for arg_name in ("num_cobol_programs", "num_jcl_jobs"):
        value = inputs.get(arg_name) or 0
        for bucket in cfg["buckets"][arg_name]:
            cap = bucket["max"]
            if cap is None or value <= cap:
                score += bucket["weight"]
                label = arg_name.replace("num_", "").replace("_", " ")
                factors.append(f"{value} {label} ({bucket['label']}, +{bucket['weight']})")
                break

    # Boolean flags
    for flag in ("has_cics", "has_db2", "has_ims", "has_mq"):
        if inputs.get(flag) and flag in cfg["flags"]:
            spec = cfg["flags"][flag]
            score += spec["weight"]
            factors.append(f"{spec['factor']} (+{spec['weight']})")

    # Graduated weights
    num_vsam_files = inputs.get("num_vsam_files") or 0
    if num_vsam_files > 0:
        spec = cfg["graduated"]["num_vsam_files"]
        w = min(num_vsam_files // spec["step_divisor"] + spec["step_weight"],
                spec["max_weight"])
        score += w
        factors.append(spec["factor_template"].format(n=num_vsam_files) + f" (+{w})")
    num_copybooks = inputs.get("num_copybooks") or 0
    if num_copybooks > 0:
        spec = cfg["graduated"]["num_copybooks"]
        if num_copybooks > spec["threshold"]:
            score += spec["weight_above"]
            factors.append(spec["factor_template"].format(n=num_copybooks)
                           + f" (+{spec['weight_above']})")

    # Level — fall through from lowest cap to the open-ended top band
    chosen_level = cfg["levels"][-1]
    for lvl in cfg["levels"]:
        cap = lvl["max_score"]
        if cap is None or score <= cap:
            chosen_level = lvl
            break

    return score, factors, chosen_level


def _merge_complexity_inputs(profile: dict | None, overrides: dict) -> tuple[dict, list[str]]:
    """Resolve the 8 scorer inputs from (profile defaults) ← (explicit args).

    Returns (resolved_inputs, missing_inputs). An input is "missing" when it
    is one of the two required counts (num_cobol_programs / num_jcl_jobs) and
    neither the profile nor an explicit arg supplied it — those drive the
    confidence band. Explicit non-default args always win over the profile.
    """
    workload = workload_from_profile(profile)
    resolved: dict[str, Any] = dict(_COMPLEXITY_INPUTS)

    # 1. Seed from profile workload where present.
    for key in resolved:
        if workload.get(key) is not None:
            resolved[key] = workload[key]

    # 2. Explicit overrides win when the caller actually set them. With None
    #    sentinels for every signature default, "is not None" cleanly means
    #    "the SA stated it this turn" — including when they state the literal
    #    default value (e.g. "no, there's no CICS" → has_cics=False is a real
    #    correction, not an absent arg). Earlier logic used `val != default`
    #    which made an explicit-default indistinguishable from omitted, so a
    #    stale profile value won. Fixed per FIXES.md #13.
    for key, val in overrides.items():
        if val is not None:
            resolved[key] = val

    # 3. Required-input gap detection drives confidence.
    missing: list[str] = []
    for key in ("num_cobol_programs", "num_jcl_jobs"):
        if resolved.get(key) is None:
            missing.append(key)
    return resolved, missing


@mcp.tool()
def estimate_complexity(
    num_cobol_programs: int | None = None,
    num_jcl_jobs: int | None = None,
    has_cics: bool | None = None,
    has_db2: bool | None = None,
    has_ims: bool | None = None,
    has_mq: bool | None = None,
    num_vsam_files: int | None = None,
    num_copybooks: int | None = None,
    profile: dict | None = None,
) -> str:
    """Estimate mainframe modernization complexity from workload characteristics.

    All thresholds and weights are loaded from `data/complexity_scoring.json`
    so the scoring is auditable and tunable without code changes. The
    response includes a transparent breakdown of how the score was computed
    plus a `breakdown_hash` (identical inputs + data version → identical hash)
    for one-line audit citation.

    Args:
        num_cobol_programs / num_jcl_jobs / has_* / num_vsam_files /
        num_copybooks: explicit workload inputs.
        profile: optional CustomerProfile.to_dict() (or its `workload`
            sub-dict). When supplied, any input the SA did not pass explicitly
            is auto-seeded from the profile so the agent never re-states what
            it already knows. Explicit args always override the profile.

    The response is the canonical computation_result shape (value, factors,
    inputs_echo, missing_inputs, confidence, breakdown_hash). When the two
    required counts are absent from BOTH the args and the profile, the score
    is still returned (treating them as 0) but flagged low/medium confidence
    with `missing_inputs` populated so the agent can probe rather than present
    a falsely precise number.
    """
    cfg = load_data("complexity_scoring.json")

    overrides = {
        "num_cobol_programs": num_cobol_programs,
        "num_jcl_jobs": num_jcl_jobs,
        "has_cics": has_cics, "has_db2": has_db2,
        "has_ims": has_ims, "has_mq": has_mq,
        "num_vsam_files": num_vsam_files, "num_copybooks": num_copybooks,
    }
    resolved, missing = _merge_complexity_inputs(profile, overrides)

    score, factors, chosen_level = _score_complexity(resolved, cfg)

    # inputs_echo carries only the inputs that actually contributed a signal,
    # so the hash is stable and the SA sees exactly what drove the score.
    inputs_echo = {k: v for k, v in resolved.items()
                   if v not in (None, False, 0)}

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
                "buckets": cfg["buckets"],
                "flags": cfg["flags"],
                "graduated": cfg["graduated"],
                "levels": cfg["levels"],
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


# ---------------------------------------------------------------------------
# Tool 6: FSI compliance (1.5.8 — accepts customer_profile_summary)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_fsi_compliance_check(
    regulation: str = "",
    customer_profile_summary: str = "",
) -> str:
    """Get FSI regulatory compliance guidance for mainframe modernization.

    Returns a typed control catalog per regulation ({control_id,
    regulation_clause, intent, satisfied_by, evidence_required}) so downstream
    tooling can map controls deterministically — not just prose. Every
    response carries a mandatory not-legal-advice disclaimer.

    Args:
        regulation: One of the regulations defined in fsi_compliance.json
            (SOX, PCI_DSS, FDIC, OCC, GLBA, FFIEC, FINRA_17a-4). The valid set
            is sourced from the data file, not hardcoded. Empty → general best
            practices + the available-regulations list.
        customer_profile_summary: Optional. When provided, the response prepends
            a "for this customer" prose line — the agent supplies a compact
            profile rendering (e.g. CustomerProfile.render_for_prompt()).
    """
    file = "fsi_compliance.json"
    data = load_data(file)
    regs = payload_only(data["regulations"])
    disclaimer = data.get("compliance_disclaimer", "")

    if not regulation:
        summary = (
            f"General FSI best practices ({len(data['general_best_practices'])} items). "
            f"Available regulations: {', '.join(regs.keys())}."
        )
        if customer_profile_summary:
            summary = "For this customer:\n" + customer_profile_summary + "\n\n" + summary
        summary = f"_{disclaimer}_\n\n" + summary if disclaimer else summary
        return structured_response(
            summary=summary,
            payload={
                "compliance_disclaimer": disclaimer,
                "available_regulations": list(regs.keys()),
                "general_best_practices": data["general_best_practices"],
            },
            data_version=data_version_of(file),
            tool_name="get_fsi_compliance_check",
            provenance=provenance_of(file),
        )

    chosen = require_choice(regulation, list(regs.keys()), arg_name="regulation")
    r = regs[chosen]

    n_controls = len(r.get("controls", []))
    summary = (f"**{r['name']}** — applies to: {r['applies_to']}. "
               f"{n_controls} mapped controls.")
    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary
                   + f"\n\n**Regulatory focus:** {summary}")
    summary = f"_{disclaimer}_\n\n" + summary if disclaimer else summary
    return structured_response(
        summary=summary,
        payload={"compliance_disclaimer": disclaimer,
                 "regulation": chosen, "details": r},
        data_version=data_version_of(file),
        tool_name="get_fsi_compliance_check",
        provenance=provenance_of(file),
    )


# ---------------------------------------------------------------------------
# Tool 7: partner-tool comparison
# ---------------------------------------------------------------------------

def _partner_scorers(scoring: dict) -> dict:
    """Build the per-criterion scorer functions for compare_partner_tools.

    Each scorer is fn(tool_dict, context) -> (raw_0_to_1, evidence_str). The
    rubrics that map categorical fields (cost_model, cloud_native_output) to
    a 0..1 score live in partner_scoring.json, NOT here, so they are auditable
    and tunable without a code deploy.
    """
    rubrics = scoring.get("rubrics", {})

    def cov(tool, ctx):
        wanted = ctx.get("wanted_sources", [])
        fit = [x.upper() for x in tool.get("source_fit", [])]
        if not wanted:
            # No customer sources known → neutral prior, NOT a perfect 1.0
            # (the old bug gave every vendor a perfect coverage score).
            return scoring.get("neutral_prior", 0.5), "no source systems supplied (neutral prior)"
        covered = [s for s in wanted if s in fit]
        raw = len(covered) / len(wanted)
        return raw, f"covers {len(covered)}/{len(wanted)}: {', '.join(covered) or 'none'}"

    def pattern_fit(tool, ctx):
        want = ctx.get("pattern", "")
        pats = [p.lower().replace(" ", "_") for p in tool.get("pattern", [])]
        if not want:
            return scoring.get("neutral_prior", 0.5), "no pattern preference (neutral)"
        hit = want in pats
        return (1.0 if hit else 0.0), ("matches " + want if hit else f"no {want} support")

    def m2(tool, ctx):
        on = bool(tool.get("aws_m2_managed"))
        return (1.0 if on else 0.0), ("AWS M2 managed" if on else "self-managed runtime")

    def _rubric_lookup(name, tool):
        r = rubrics.get(name, {})
        val = tool.get(name)
        score = r.get(val, r.get("_default", 0.5))
        return score, f"{name}={val}"

    def cost(tool, ctx):
        return _rubric_lookup("cost_model", tool)

    def cloud_native(tool, ctx):
        return _rubric_lookup("cloud_native_output", tool)

    def _scaled(name, tool):
        scale = rubrics.get(name, {}).get("_scale_max", 5)
        val = tool.get(name) or 0
        return min(1.0, val / scale), f"{name}={val}/{scale}"

    def regulator(tool, ctx):
        return _scaled("fsi_regulator_familiarity", tool)

    def ecosystem(tool, ctx):
        return _scaled("vendor_ecosystem_size", tool)

    return {
        "source_system_coverage": cov,
        "pattern_fit": pattern_fit,
        "aws_m2_managed": m2,
        "cost_model": cost,
        "cloud_native_output": cloud_native,
        "fsi_regulator_familiarity": regulator,
        "vendor_ecosystem_size": ecosystem,
    }


@mcp.tool()
def compare_partner_tools(
    source_systems: Any = None,
    pattern: str = "",
    aws_m2_managed_only: bool = False,
    top_n: int = 5,
    customer_profile_summary: str = "",
    weight_profile: str = "default",
    profile: dict = None,
) -> str:
    """Rank mainframe modernization partner tools for a workload profile.

    Scoring is a deterministic weighted sum over the eight declared
    decision_criteria; all weights + rubrics live in
    `data/partner_scoring.json` (auditable / tunable without a code deploy).
    Each ranked tool carries a `criterion_scores` breakdown so an SA can see
    exactly WHY one tool out-ranked another.

    Args:
        source_systems: list[str] OR comma-separated string
            (e.g. ["COBOL","CICS","DB2","VSAM"]). When omitted, sources are
            derived from `profile.workload` so the SA never re-types them; if
            nothing is known the ranking is computed against a neutral prior
            and flagged `low_confidence`.
        pattern: Preferred migration pattern. Empty = derived from profile
            decisions, else any.
        aws_m2_managed_only: If true, only AWS M2 managed-runtime tools.
        top_n: Max ranked tools to return (default 5).
        customer_profile_summary: Optional grounding string (prose).
        weight_profile: Named weight set in partner_scoring.json — one of
            default / regulator_first / cost_sensitive / cloud_native_first.
        profile: Optional CustomerProfile.to_dict() to auto-seed source_systems
            (and the pattern, from active decisions).
    """
    file = "partner_tools.json"
    data = load_data(file)
    tools = payload_only(data["tools"])
    scoring = load_data("partner_scoring.json")

    weight_profiles = scoring["weight_profiles"]
    chosen_wp = weight_profile if weight_profile in weight_profiles else "default"
    weights = weight_profiles[chosen_wp]

    # --- Resolve inputs: explicit args win, else seed from the profile -------
    wanted_sources = [s.upper() for s in coerce_to_list(source_systems)]
    seeded_from_profile = False
    if not wanted_sources and profile is not None:
        wl = workload_from_profile(profile)
        wanted_sources = [s.upper() for s in source_tokens_from_workload(wl)]
        seeded_from_profile = bool(wanted_sources)

    pattern = (pattern or "").strip().lower()

    # Filter the candidate set, then score whatever survives.
    candidates = {
        key: t for key, t in tools.items()
        if (not aws_m2_managed_only or t.get("aws_m2_managed"))
        and (not pattern or pattern in [p.lower().replace(" ", "_") for p in t.get("pattern", [])])
    }

    context = {"wanted_sources": wanted_sources, "pattern": pattern}
    ranked_raw = score_subjects(
        candidates,
        weights=weights,
        scorers=_partner_scorers(scoring),
        context=context,
    )

    # Decorate with the human-facing fields the response needs.
    ranked: list[dict] = []
    for r in ranked_raw[:top_n]:
        t = tools[r["key"]]
        fit = [x.upper() for x in t.get("source_fit", [])]
        ranked.append({
            "tool_key": r["key"],
            "name": t["name"],
            "pattern": t.get("pattern", []),
            "source_fit_coverage": [s for s in wanted_sources if s in fit],
            "source_fit_missing": [s for s in wanted_sources if s not in fit],
            "aws_m2_managed": t.get("aws_m2_managed", False),
            "typical_timeline": t.get("typical_timeline"),
            "strengths": t.get("strengths", []),
            "weaknesses": t.get("weaknesses", []),
            "fsi_considerations": t.get("fsi_considerations"),
            "reference_urls": t.get("reference_urls", []),
            "score": r["score"],
            "criterion_scores": r["criterion_scores"],
        })

    low_confidence = not wanted_sources
    if ranked:
        summary = (
            f"Top fit: **{ranked[0]['name']}** (score {ranked[0]['score']}, "
            f"weights='{chosen_wp}')."
            + (f" Coverage: {', '.join(ranked[0]['source_fit_coverage'])}."
               if ranked[0]['source_fit_coverage'] else "")
        )
        if len(ranked) > 1:
            summary += " Runners-up: " + ", ".join(
                f"{t['name']} ({t['score']})" for t in ranked[1:3]
            ) + "."
        if low_confidence:
            summary += (" _Low confidence — no source systems supplied or "
                        "derivable from the profile; ranking uses a neutral "
                        "prior. Tell me the source stack to firm this up._")
        elif seeded_from_profile:
            summary += f" _(sources seeded from profile: {', '.join(wanted_sources)})_"
    else:
        summary = "No partner tool matched the given filters."

    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary + "\n\n"
                   + summary)

    payload = {
        "filters": {
            "source_systems": wanted_sources,
            "source_systems_origin": ("profile" if seeded_from_profile
                                      else "explicit" if wanted_sources else "none"),
            "pattern": pattern or "any",
            "aws_m2_managed_only": aws_m2_managed_only,
        },
        "weight_profile": chosen_wp,
        "weights_used": weights,
        "criteria": data.get("decision_criteria", []),
        "low_confidence": low_confidence,
        "ranked": ranked,
    }
    return structured_response(
        summary=summary,
        payload=payload,
        data_version=data_version_of(file),
        tool_name="compare_partner_tools",
        provenance=provenance_of(file),
    )


# ---------------------------------------------------------------------------
# Tool 8: AWS service comparison
# ---------------------------------------------------------------------------

def _service_scorers(weights_cfg: dict, low_latency_need: bool) -> dict:
    """Per-criterion scorers for compare_services. fn(candidate, ctx) ->
    (raw_0_to_1, evidence). The `fit` rank and latency base scores live in
    service_ranking_weights.json — the old hardcoded fit_rank dict is gone."""
    rubrics = weights_cfg.get("rubrics", {})

    def fit(cand, ctx):
        r = rubrics.get("fit", {})
        val = cand.get("fit", "low")
        return r.get(val, r.get("_default", 0.25)), f"fit={val}"

    def ops(cand, ctx):
        scale = rubrics.get("ops_burden", {}).get("_scale_max", 5)
        val = cand.get("ops_burden") or 3
        # Invert: burden 1 (managed) → 1.0, burden 5 (heavy) → 0.2
        raw = (scale - val + 1) / scale
        return max(0.0, min(1.0, raw)), f"ops_burden={val}/{scale}"

    def latency_fit(cand, ctx):
        base = rubrics.get("latency_fit", {}).get("base", {})
        cls = cand.get("latency_class", "")
        raw = base.get(cls, rubrics.get("latency_fit", {}).get("_default", 0.5))
        # When the customer declared a low-latency need, penalize non-OLTP-low.
        if low_latency_need and cls != "oltp_low":
            raw = raw * 0.7
        return raw, f"latency_class={cls or 'unknown'}" + (
            " (low-latency need applied)" if low_latency_need else "")

    return {"fit": fit, "ops_burden": ops, "latency_fit": latency_fit}


@mcp.tool()
def compare_services(
    source: str,
    candidates: Any = None,
    customer_profile_summary: str = "",
    weight_profile: str = "default",
    profile: dict = None,
) -> str:
    """Compare AWS target services for a given mainframe source component.

    Ranking is a deterministic weighted sum over `fit`, `ops_burden`, and
    `latency_fit`; all weights + the fit rubric live in
    `data/service_ranking_weights.json` (the old hardcoded fit_rank magic dict
    is gone). Each candidate carries a `criterion_scores` breakdown.

    Args:
        source: One of: vsam_ksds, vsam_esds_flat_sequential, db2_zos, ims_db,
            cics_online, jcl_batch, mq_series.
        candidates: list[str] OR comma-separated string. Empty = all candidates.
        customer_profile_summary: Optional grounding string (prose).
        weight_profile: Named weight set — default / low_latency_first /
            ops_light_first.
        profile: Optional CustomerProfile.to_dict(). A
            `constraints.downtime_tolerance == "zero"` up-weights low-latency
            OLTP targets automatically (the SA re-enters nothing).
    """
    file = "service_comparisons.json"
    data = load_data(file)
    comps = payload_only(data["comparisons"])
    weights_cfg = load_data("service_ranking_weights.json")

    src_key = require_choice(source, list(comps.keys()), arg_name="source")
    entry = comps[src_key]
    all_candidates = entry["candidates"]

    wanted = set(coerce_to_list(candidates))
    filtered = ({k: v for k, v in all_candidates.items() if k in wanted}
                if wanted else all_candidates)

    # Profile-driven latency sensitivity: zero downtime tolerance → low-latency need.
    constraints = constraints_from_profile(profile)
    low_latency_need = (constraints.get("downtime_tolerance") == "zero")

    weight_profiles = weights_cfg["weight_profiles"]
    chosen_wp = weight_profile if weight_profile in weight_profiles else "default"
    weights = weight_profiles[chosen_wp]

    ranked_raw = score_subjects(
        filtered,
        weights=weights,
        scorers=_service_scorers(weights_cfg, low_latency_need),
        context={},
    )

    ranked: list[dict] = []
    for r in ranked_raw:
        c = filtered[r["key"]]
        ranked.append({
            "service": r["key"],
            **c,
            "score": r["score"],
            "criterion_scores": r["criterion_scores"],
        })

    if ranked:
        primary = ranked[0]
        summary = (
            f"For {entry['source']}: **primary = {primary['service']}** "
            f"(score {primary['score']}, fit={primary['fit']}). "
            f"When to choose: {primary.get('when_to_choose', '—')}"
        )
        if low_latency_need:
            summary += " _(zero-downtime tolerance from profile up-weighted low-latency targets.)_"
    else:
        summary = "No candidates matched the filter."

    if customer_profile_summary:
        summary = ("For this customer:\n" + customer_profile_summary + "\n\n"
                   + summary)

    payload = {
        "source": entry["source"],
        "description": entry["description"],
        "weight_profile": chosen_wp,
        "weights_used": weights,
        "low_latency_need": low_latency_need,
        "candidates": ranked,
    }
    return structured_response(
        summary=summary,
        payload=payload,
        data_version=data_version_of(file),
        tool_name="compare_services",
        provenance=provenance_of(file),
    )


# ---------------------------------------------------------------------------
# Tool 9 (NEW 1.6): analyze_phase_gaps — deterministic phase-advancement gaps
# ---------------------------------------------------------------------------

def _signal_satisfied(signal_key: str, signal: dict, profile: dict) -> bool:
    """Evaluate one phase-gate signal against a CustomerProfile.to_dict().

    Mirrors the gate semantics of CustomerProfile.derive_phase():
      - `any_of` field_paths: satisfied if ANY resolves truthy (a non-empty
        list, a non-None scalar, or — for has_* booleans — True).
      - `decision_category`: satisfied if an ACTIVE (non-superseded) decision
        of that category (or any of a list of categories) exists.
    The parity test in tests/ asserts this reproduces derive_phase() exactly.
    """
    # Active-decision-count signal (e.g. >=3 active decisions)
    threshold = signal.get("active_decision_count_gte")
    if threshold is not None:
        active = [d for d in profile.get("decisions_made", []) or []
                  if d.get("superseded_by_turn") is None]
        return len(active) >= threshold

    # Decision-backed signal
    cats = signal.get("decision_category")
    if cats:
        cats = [cats] if isinstance(cats, str) else list(cats)
        for d in profile.get("decisions_made", []) or []:
            if d.get("category") in cats and d.get("superseded_by_turn") is None:
                return True
        return False

    # Field-backed signal (any_of)
    for path in signal.get("any_of", []):
        val = _resolve_path(profile, path)
        # has_* booleans only count when explicitly True (matches derive_phase,
        # which treats has_cics is True — not just truthy — as the signal).
        leaf = path.rsplit(".", 1)[-1]
        if leaf.startswith("has_"):
            if val is True:
                return True
        elif isinstance(val, list):
            if len(val) > 0:
                return True
        elif val is not None:
            return True
    return False


def _resolve_path(profile: dict, path: str):
    """Resolve a dotted field_path against a profile dict (e.g.
    'workload.num_cobol_programs'). Returns None if any segment is absent."""
    cur: Any = profile
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _derive_phase_from_signals(satisfied: dict, gates: dict) -> str:
    """Return the engagement phase via the ordered, first-match-wins
    `phase_rules` in the data file. This reproduces
    CustomerProfile.derive_phase() EXACTLY — including its decision-driven
    shortcut branches (pattern+partner+target → execution even with no
    workload captured), which a simple sequential ladder cannot express.
    The parity test asserts the match.
    """
    for rule in gates["phase_rules"]:
        all_ok = all(satisfied.get(s) for s in rule.get("all_of", []))
        any_list = rule.get("any_of")
        any_ok = (any(satisfied.get(s) for s in any_list)) if any_list else True
        if all_ok and any_ok:
            return rule["phase"]
    return gates["phase_order"][0]


@mcp.tool()
def analyze_phase_gaps(profile: dict, target_phase: str = "") -> str:
    """Compute the engagement phase and the EXACT gaps blocking advancement.

    Deterministic discovery driver: given a CustomerProfile.to_dict(), this
    reports the current phase, the next phase, and the precise set of missing
    profile field_paths that gate advancement — each mapped to a curated FSI
    discovery question from `data/phase_gates.json`. Because the phase ladder
    is a locked deterministic gate function (D2), an LLM asked "what's missing
    to advance?" gives non-reproducible answers and re-asks already-filled
    fields; this does not.

    Args:
        profile: CustomerProfile.to_dict() (workload, constraints,
            decisions_made, …).
        target_phase: Optional — compute the gaps to reach a SPECIFIC phase
            (e.g. "proposal") instead of just the next one. One of
            discovery / assessment / recommendation / proposal / execution.

    The `gap_questions` in the response are shaped to feed
    CustomerProfile.open_questions (the agent phrases the final probe).
    """
    if not isinstance(profile, dict) or not profile:
        raise ToolInputError(
            "analyze_phase_gaps requires a profile dict (CustomerProfile.to_dict()).",
            error_code="missing_argument",
            valid_options=["profile (dict)"],
        )

    gates = load_data("phase_gates.json")
    signals = gates["signals"]
    order = gates["phase_order"]
    transitions = {t["from"]: t for t in gates["transitions"]}

    # Evaluate every signal once.
    satisfied = {key: _signal_satisfied(key, sig, profile)
                 for key, sig in signals.items()}

    current_phase = _derive_phase_from_signals(satisfied, gates)

    # Determine which transition we're solving for.
    if target_phase:
        target_phase = require_choice(target_phase, order, arg_name="target_phase")
        # Collect the union of requirements across every transition from the
        # current phase up to (and including) the one that reaches target_phase.
        needed_signals: list[str] = []
        cursor = current_phase
        reached = current_phase
        while cursor in transitions:
            t = transitions[cursor]
            for req in t["requires"]:
                if req not in needed_signals:
                    needed_signals.append(req)
            reached = t["to"]
            cursor = t["to"]
            if reached == target_phase:
                break
        next_phase = target_phase
    else:
        t = transitions.get(current_phase)
        needed_signals = list(t["requires"]) if t else []
        next_phase = t["to"] if t else current_phase  # execution = terminal

    # The gaps are the needed signals that are NOT yet satisfied.
    gap_questions: list[dict] = []
    for sig_key in needed_signals:
        if satisfied.get(sig_key):
            continue
        sig = signals[sig_key]
        gap_questions.append({
            "signal": sig_key,
            "field_path": sig.get("primary_field_path", ""),
            "question": sig.get("question", ""),
            "inspects": sig.get("any_of") or sig.get("decision_category"),
        })

    at_terminal = (current_phase == order[-1]) or (
        not target_phase and current_phase not in transitions
    )

    if at_terminal and not gap_questions:
        summary = (f"Phase: **{current_phase}** (most-advanced phase reached). "
                   f"No advancement gaps — drive execution-level detail.")
    elif not gap_questions:
        summary = (f"Phase: **{current_phase}** → ready to advance to "
                   f"**{next_phase}**: all gate signals are satisfied.")
    else:
        labels = ", ".join(g["signal"] for g in gap_questions)
        summary = (f"Phase: **{current_phase}** → to reach **{next_phase}**, "
                   f"{len(gap_questions)} gap(s) remain: {labels}. "
                   f"Ask the queued discovery question(s) to advance.")

    payload = {
        "current_phase": current_phase,
        "next_phase": next_phase,
        "signals_satisfied": satisfied,
        "required_signals": needed_signals,
        "gap_questions": gap_questions,
        "at_terminal_phase": at_terminal,
    }
    return structured_response(
        summary=summary,
        payload=payload,
        data_version=data_version_of("phase_gates.json"),
        tool_name="analyze_phase_gaps",
        provenance=provenance_of("phase_gates.json"),
    )


# ---------------------------------------------------------------------------
# Tool 10 (NEW): list_taxonomy meta-tool (1.5.5)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_taxonomy(tool_name: str = "") -> str:
    """Discover valid arguments for any tool without making a "real" call.

    Args:
        tool_name: Empty → list all tools and their argument shapes.
                   Otherwise: one of the tool names below.
    """
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
            "optional_counts": ["num_cobol_programs", "num_jcl_jobs",
                                "num_vsam_files", "num_copybooks"],
            "optional_flags": ["has_cics", "has_db2", "has_ims", "has_mq"],
            "profile": "optional CustomerProfile.to_dict() — auto-seeds all of "
                       "the above from profile.workload (explicit args override)",
            "profile_autofill": {
                "num_cobol_programs": "workload.num_cobol_programs",
                "num_jcl_jobs": "workload.num_jcl_jobs",
                "has_cics/db2/ims/mq": "workload.has_*",
                "num_vsam_files/copybooks": "workload.num_*",
            },
            "data_file": "complexity_scoring.json",
            "note": "Deterministic: returns value/factors/confidence/missing_inputs/"
                    "breakdown_hash. All weights & thresholds are in the data file.",
        },
        "get_fsi_compliance_check": {
            "regulation": list(payload_only(load_data("fsi_compliance.json")["regulations"]).keys()),
            "customer_profile_summary": "optional grounding string",
            "data_file": "fsi_compliance.json",
            "note": "Returns typed control catalog + mandatory not-legal-advice disclaimer.",
        },
        "compare_partner_tools": {
            "source_systems": "list[str] OR comma-separated string (auto-seeded "
                              "from profile.workload when omitted)",
            "pattern": ["rehost", "replatform", "refactor", "retire", "automated_refactor"],
            "aws_m2_managed_only": "bool",
            "top_n": "int (default 5)",
            "weight_profile": list(load_data("partner_scoring.json")["weight_profiles"].keys()),
            "profile": "optional CustomerProfile.to_dict() — seeds source_systems",
            "data_file": "partner_tools.json",
            "scoring_file": "partner_scoring.json",
            "note": "Weighted multi-criteria ranking; each result carries criterion_scores.",
        },
        "compare_services": {
            "source": list(payload_only(load_data("service_comparisons.json")["comparisons"]).keys()),
            "candidates": "list[str] OR comma-separated string (optional)",
            "weight_profile": list(load_data("service_ranking_weights.json")["weight_profiles"].keys()),
            "profile": "optional CustomerProfile.to_dict() — constraints.downtime_tolerance "
                       "drives latency weighting",
            "data_file": "service_comparisons.json",
            "scoring_file": "service_ranking_weights.json",
        },
        "analyze_phase_gaps": {
            "profile": "REQUIRED — CustomerProfile.to_dict()",
            "target_phase": list(load_data("phase_gates.json")["phase_order"]),
            "data_file": "phase_gates.json",
            "note": "Deterministic discovery driver — returns current/next phase + "
                    "exact gap_questions to feed open_questions. Mirrors derive_phase().",
        },
    }

    if not tool_name:
        summary = (f"{len(taxonomy)} tools available: {', '.join(taxonomy.keys())}. "
                   f"Tools accepting `profile` auto-seed their args from the "
                   f"CustomerProfile so the SA never re-states known facts.")
        return structured_response(
            summary=summary,
            payload={"tools": taxonomy,
                     "note": "lookup_cobol_pattern / lookup_jcl_reference / "
                             "map_mainframe_to_aws / get_migration_pattern remain "
                             "callable but are no longer in the agent's reflexive "
                             "router tool-map (the response model already knows that "
                             "reference content); they're for explicit syntax lookups."},
            data_version="taxonomy",
            tool_name="list_taxonomy",
        )

    chosen = require_choice(tool_name, list(taxonomy.keys()), arg_name="tool_name")
    summary = f"Argument shape for {chosen}: see Data section."
    return structured_response(
        summary=summary,
        payload={"tool": chosen, "argument_shape": taxonomy[chosen]},
        data_version="taxonomy",
        tool_name="list_taxonomy",
    )


def main() -> None:
    """Entry point used by the `mfmod-mcp` console script (see pyproject.toml).

    Claude Desktop launches us as a stdio MCP subprocess; FastMCP.run() picks
    up that transport automatically.
    """
    mcp.run()


if __name__ == "__main__":
    main()
