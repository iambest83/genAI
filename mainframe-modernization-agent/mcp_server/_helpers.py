"""Shared helpers for the mainframe modernization MCP tools.

Owns the cross-cutting concerns introduced in Iteration 1.5:

  - load_data()          : load + cache JSON data files; strip metadata keys
  - data_version_of()    : read the file's `_version` for response stamping
  - normalize_name()     : canonicalize a lookup key (case + separators)
  - lookup_in_dict()     : exact + ranked partial matching, returns typed result
  - structured_response(): prose summary + JSON payload + version + status
  - tool_error()         : typed error shape (raised; FastMCP converts to MCP error)

Iteration 1.6 additions (profile-seam + auditability):
  - provenance_of()      : read a file's `_provenance` metadata for citation
  - structured_response now accepts an optional provenance block + footer
  - computation_result() : the canonical {value, factors, inputs_echo,
                           confidence, ...} shape every deterministic compute
                           tool returns, so the agent learns it once
  - score_subjects()     : a data-driven weighted multi-criteria scoring engine
                           (shared by compare_partner_tools / compare_services)
  - profile-seam helpers : workload_from_profile / constraints_from_profile /
                           source_tokens_from_workload pull typed inputs out of
                           a CustomerProfile.to_dict() so the SA never re-types
                           what the agent already knows
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@lru_cache(maxsize=16)
def load_data(filename: str) -> dict:
    """Load a JSON data file once and cache. The `_version` and any other
    underscore-prefixed metadata keys are KEPT in the returned dict — call
    `payload_only()` to strip them when shaping a response."""
    with open(DATA_DIR / filename, "r") as f:
        return json.load(f)


def data_version_of(filename: str) -> str:
    """Return the file's _version field, or 'unversioned' if absent."""
    return str(load_data(filename).get("_version", "unversioned"))


def provenance_of(filename: str) -> dict:
    """Return the file's `_provenance` metadata block, or a minimal fallback.

    The provenance block lets an SA answer "where did this number come from
    and how stale is it?" on the spot — the core of the curated-data moat.
    Convention (kept underscore-prefixed so `payload_only` strips it from
    the data body):

      "_provenance": {
        "last_reviewed": "2026-05-26",
        "reviewed_by": "mainframe-modernization CoE",
        "sources": [{"label": "...", "ref": "https://... or doc id"}]
      }
    """
    prov = load_data(filename).get("_provenance")
    if isinstance(prov, dict):
        # Always echo the file version alongside provenance for one-line citation.
        return {"data_version": data_version_of(filename), **prov}
    return {"data_version": data_version_of(filename),
            "last_reviewed": "unspecified", "sources": []}


def payload_only(d: dict) -> dict:
    """Strip leading-underscore metadata keys (e.g. _version, _description)."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Name normalization + matching
# ---------------------------------------------------------------------------

def normalize_name(s: str) -> str:
    """Canonicalize a lookup key: uppercase, hyphens & spaces → underscore."""
    return (s or "").strip().upper().replace("-", "_").replace(" ", "_")


def lookup_in_dict(
    needle: str,
    haystack: dict,
) -> dict:
    """Look up a key in a dict with explicit exact/partial-match handling.

    Replaces the old bidirectional substring match (which silently returned
    whichever entry the dict iterated first when the needle matched several).

    Returns:
      {
        "match_type": "exact" | "partial" | "miss",
        "matches": [{"key": ..., "value": ...}, ...],   # ranked, max 5
        "available_keys": [...],                          # always present
      }
    """
    keys = [k for k in haystack.keys() if not k.startswith("_")]
    needle_n = normalize_name(needle)

    # 1. Exact match (case- and separator-insensitive)
    for k in keys:
        if normalize_name(k) == needle_n:
            return {
                "match_type": "exact",
                "matches": [{"key": k, "value": haystack[k]}],
                "available_keys": keys,
            }

    # 2. Partial — needle is a substring of the key, OR key is a substring
    #    of needle. Rank by length-ratio (closest size first).
    partials: list[tuple[float, str]] = []
    for k in keys:
        kn = normalize_name(k)
        if needle_n in kn or kn in needle_n:
            ratio = min(len(needle_n), len(kn)) / max(len(needle_n), len(kn))
            partials.append((ratio, k))
    partials.sort(reverse=True)

    if partials:
        return {
            "match_type": "partial",
            "matches": [{"key": k, "value": haystack[k]} for _, k in partials[:5]],
            "available_keys": keys,
        }

    return {"match_type": "miss", "matches": [], "available_keys": keys}


# ---------------------------------------------------------------------------
# Response shaping (1.5.1)
# ---------------------------------------------------------------------------

def structured_response(
    *,
    summary: str,
    payload: Any,
    data_version: str,
    tool_name: str,
    provenance: dict | None = None,
) -> str:
    """Build the canonical tool response — prose summary + JSON payload.

    Returns a single string with a clear two-section layout. Sonnet reads
    the prose efficiently; the JSON section is for downstream programmatic
    use (e.g. fact extraction in profile_updater).

    When `provenance` is supplied (from `provenance_of(file)`), it is folded
    into the JSON payload under `_provenance` and a one-line "Sources: …;
    last reviewed YYYY-MM-DD" footer is appended to the prose so the SA gets
    a citable, dated claim without digging into the JSON.
    """
    if provenance:
        # Don't mutate the caller's payload dict.
        if isinstance(payload, dict):
            payload = {**payload, "_provenance": provenance}
        summary = summary + "\n\n" + _provenance_footer(provenance)

    json_str = json.dumps(payload, indent=2, ensure_ascii=False)
    return (
        f"## Summary\n{summary}\n\n"
        f"## Data (data_version={data_version}, tool={tool_name})\n"
        f"```json\n{json_str}\n```"
    )


def _provenance_footer(provenance: dict) -> str:
    """One-line, SA-readable citation footer from a provenance block."""
    reviewed = provenance.get("last_reviewed", "unspecified")
    sources = provenance.get("sources", []) or []
    labels = ", ".join(
        s.get("label", "") for s in sources if isinstance(s, dict) and s.get("label")
    )
    src_part = f"Sources: {labels}. " if labels else ""
    return f"_{src_part}Last reviewed {reviewed}._"


# ---------------------------------------------------------------------------
# Typed errors (1.5.7)
# ---------------------------------------------------------------------------

class ToolInputError(ValueError):
    """Raised when a tool is called with invalid arguments. FastMCP
    converts to a proper MCP error response, which the agent can detect
    distinct from a 200-OK 'unknown category' string."""

    def __init__(self, message: str, *, error_code: str = "invalid_input",
                 valid_options: list[str] | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.valid_options = valid_options or []


def require_choice(
    value: str,
    choices: list[str],
    *,
    arg_name: str,
    allow_empty: bool = False,
) -> str:
    """Validate that `value` is one of `choices` (case-insensitive,
    separator-insensitive). Returns the canonicalized choice. Raises
    ToolInputError if no match."""
    if not value:
        if allow_empty:
            return ""
        raise ToolInputError(
            f"Argument '{arg_name}' is required.",
            error_code="missing_argument",
            valid_options=choices,
        )

    norm = normalize_name(value)
    for c in choices:
        if normalize_name(c) == norm:
            return c

    raise ToolInputError(
        f"Invalid {arg_name}={value!r}. Must be one of: {', '.join(choices)}.",
        error_code="invalid_choice",
        valid_options=choices,
    )


# ---------------------------------------------------------------------------
# List parsing (1.5.2)
# ---------------------------------------------------------------------------

def coerce_to_list(value: Any) -> list[str]:
    """Accept either a list[str] or a comma-separated string. Normalizes
    to a clean list of trimmed non-empty strings.

    This bridges old callers (comma-separated string) and the new
    standardized list-typed argument shape."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(s).strip() for s in value if str(s).strip()]
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    return [str(value).strip()] if str(value).strip() else []


# ---------------------------------------------------------------------------
# Deterministic computation result shape (1.6)
# ---------------------------------------------------------------------------

def stable_hash(obj: Any) -> str:
    """Deterministic short SHA-256 over a JSON-canonicalized object.

    Used to stamp a compute result with a one-line audit citation: identical
    inputs + data version → identical hash, so the agent can tell a genuine
    input change from silent LLM re-improvisation, and an SA can cite
    "score_breakdown_hash=ab12cd34" in a deliverable.
    """
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def computation_result(
    *,
    value: Any,
    factors: list[str],
    inputs_echo: dict,
    missing_inputs: list[str],
    data_version: str,
    extra: dict | None = None,
) -> dict:
    """The canonical payload shape for every deterministic compute tool.

    Keeping one shape across estimate_complexity (and future effort / cutover
    / residency tools) means the agent and the profile_updater learn the
    envelope once:

      {
        value, factors[], inputs_echo{}, missing_inputs[],
        confidence: "high"|"medium"|"low",
        inputs_source: "measured"|"estimated"|"mixed",
        breakdown_hash, data_version
      }

    `confidence` is derived purely from how many declared inputs were null,
    so a score computed from a sparse profile is honestly labeled rather than
    presented with false precision.
    """
    confidence = _confidence_from_missing(inputs_echo, missing_inputs)
    result = {
        "value": value,
        "factors": factors,
        "inputs_echo": inputs_echo,
        "missing_inputs": missing_inputs,
        "confidence": confidence,
        "inputs_source": _inputs_source(inputs_echo),
        "data_version": data_version,
    }
    if extra:
        result.update(extra)
    # Hash last, over everything that determines the value (not the hash itself).
    result["breakdown_hash"] = stable_hash(
        {"inputs_echo": inputs_echo, "value": value, "data_version": data_version}
    )
    return result


def _confidence_from_missing(inputs_echo: dict, missing_inputs: list[str]) -> str:
    """Confidence band from the fraction of declared inputs that were absent."""
    total = len(inputs_echo) + len(missing_inputs)
    if total == 0:
        return "low"
    missing_frac = len(missing_inputs) / total
    if missing_frac == 0:
        return "high"
    if missing_frac <= 0.4:
        return "medium"
    return "low"


def _inputs_source(inputs_echo: dict) -> str:
    """Whether the inputs were measured (from an ingestion parse) vs SA-estimated.

    Each echoed input may be a bare value or a {"value":..., "source":...}
    envelope. When sources are mixed we say so, so a memo can mark exactly
    which numbers are measured fact vs SA assertion.
    """
    sources = {
        v.get("source") for v in inputs_echo.values()
        if isinstance(v, dict) and "source" in v
    }
    if not sources:
        return "estimated"
    if sources == {"measured"}:
        return "measured"
    if "measured" in sources:
        return "mixed"
    return "estimated"


# ---------------------------------------------------------------------------
# Weighted multi-criteria scoring engine (1.6)
# ---------------------------------------------------------------------------

def score_subjects(
    subjects: dict[str, dict],
    *,
    weights: dict[str, float],
    scorers: dict[str, Any],
    context: dict | None = None,
) -> list[dict]:
    """Score a set of subjects against named criteria with explicit weights.

    Shared by compare_partner_tools and compare_services so both produce the
    identical, auditable ranking shape ("why did X beat Y?").

    Args:
      subjects: {key: subject_dict}. Each subject_dict holds the raw fields a
                scorer reads (e.g. a partner tool's source_fit / cost_model).
      weights:  {criterion: weight}. Weights are normalized to sum to 1 so the
                total is always a comparable 0-1 score regardless of how many
                criteria are active.
      scorers:  {criterion: fn(subject_dict, context) -> (raw_0_to_1, evidence_str)}.
                A criterion present in `weights` but absent from `scorers` is
                skipped (and its weight redistributed) rather than crashing.
      context:  optional dict passed to every scorer (e.g. the customer's
                wanted source systems / constraints).

    Returns a list of {key, score, criterion_scores{criterion:{raw,weight,
    weighted,evidence}}} sorted by score descending. Deterministic: same
    inputs → same order (ties broken by key name).
    """
    context = context or {}
    active = {c: w for c, w in weights.items() if c in scorers and w > 0}
    total_w = sum(active.values()) or 1.0

    scored: list[dict] = []
    for key, subject in subjects.items():
        criterion_scores: dict[str, dict] = {}
        total = 0.0
        for crit, w in active.items():
            raw, evidence = scorers[crit](subject, context)
            raw = max(0.0, min(1.0, float(raw)))
            norm_w = w / total_w
            weighted = round(raw * norm_w, 4)
            total += weighted
            criterion_scores[crit] = {
                "raw": round(raw, 3),
                "weight": round(norm_w, 3),
                "weighted": weighted,
                "evidence": evidence,
            }
        scored.append({
            "key": key,
            "score": round(total, 4),
            "criterion_scores": criterion_scores,
        })

    scored.sort(key=lambda x: (-x["score"], x["key"]))
    return scored


# ---------------------------------------------------------------------------
# Profile seam — pull typed inputs out of a CustomerProfile.to_dict() (1.6)
# ---------------------------------------------------------------------------

def workload_from_profile(profile: dict | None) -> dict:
    """Extract the Workload sub-dict from a CustomerProfile.to_dict().

    Accepts either the full profile dict (with a "workload" key) or a bare
    workload dict, so callers can pass whichever they have. Returns {} when
    nothing usable is supplied."""
    if not isinstance(profile, dict):
        return {}
    if "workload" in profile and isinstance(profile["workload"], dict):
        return dict(profile["workload"])
    # Maybe they passed the workload sub-dict directly.
    workload_keys = {"num_cobol_programs", "num_jcl_jobs", "has_cics", "has_db2"}
    if workload_keys & set(profile.keys()):
        return dict(profile)
    return {}


def constraints_from_profile(profile: dict | None) -> dict:
    """Extract the Constraints sub-dict from a CustomerProfile.to_dict()."""
    if not isinstance(profile, dict):
        return {}
    if "constraints" in profile and isinstance(profile["constraints"], dict):
        return dict(profile["constraints"])
    constraint_keys = {"regulations", "data_residency", "downtime_tolerance"}
    if constraint_keys & set(profile.keys()):
        return dict(profile)
    return {}


# Map the workload boolean/list signals to the canonical source-system tokens
# the comparison tools rank against.
_SOURCE_TOKEN_MAP = {
    "has_cics": "CICS",
    "has_db2": "DB2",
    "has_ims": "IMS",
    "has_mq": "MQ",
}


def source_tokens_from_workload(workload: dict) -> list[str]:
    """Derive canonical source-system tokens (COBOL, CICS, DB2, VSAM, …) from
    a workload dict, so compare_partner_tools can seed `source_systems` from
    the profile instead of awarding every vendor a perfect score on empty
    input."""
    tokens: list[str] = []
    if workload.get("num_cobol_programs"):
        tokens.append("COBOL")
    if workload.get("num_jcl_jobs"):
        tokens.append("JCL")
    if workload.get("num_vsam_files"):
        tokens.append("VSAM")
    for flag, token in _SOURCE_TOKEN_MAP.items():
        if workload.get(flag):
            tokens.append(token)
    for lang in workload.get("languages", []) or []:
        u = normalize_name(lang).replace("_", "/")
        if u and u not in tokens:
            tokens.append(str(lang).strip().upper())
    for db in workload.get("databases", []) or []:
        d = str(db).strip().upper()
        if d and d not in tokens:
            tokens.append(d)
    # De-dupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out
