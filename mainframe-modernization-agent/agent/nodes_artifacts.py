"""Artifact generation node.

Emits structured, paste-ready artifacts alongside the text response when the
SA's turn is imperative ("draft", "generate", "create", "outline", "build me").
The response generator calls pick_artifacts() to decide what to attach.

Four artifact kinds implemented:
  - wave_plan_csv          (CSV migration-wave plan)
  - mermaid_architecture   (Mermaid target-state diagram)
  - risk_register_md       (Markdown risk register)
  - tco_estimate_md        (Markdown TCO estimate — Iteration 2.1)

All are *deterministic templates* parameterized by the CustomerProfile plus
MCP results already fetched this turn. No extra LLM calls — fast and
auditable. The main response LLM can still rewrite/augment them in prose
if the SA asked for narrative.

Phase-gated triggering (Iteration 2.2):
  Each artifact kind declares a REQUIRED_INPUTS predicate. If the customer
  profile is missing the inputs required for meaningful output, the
  artifact is NOT emitted — the response prompt instead tells the SA
  "I can draft this once I know X and Y." Prevents a wave plan being
  emitted in discovery phase against a blank profile.
"""
from __future__ import annotations

import io
import csv
import logging
import re
from typing import Optional

from .customer_profile import CustomerProfile
from .state import AgentState, Artifact

logger = logging.getLogger(__name__)


ARTIFACT_TRIGGERS = {
    "wave_plan_csv": [
        r"\bwave\b", r"\bwave plan\b", r"\bmigration plan\b",
        r"\bphasing\b", r"\bsequence\b", r"\broadmap\b",
    ],
    "mermaid_architecture": [
        r"\barchitecture diagram\b", r"\btarget state\b", r"\bto-be\b",
        r"\bdraw\b", r"\bdiagram\b", r"\bmermaid\b",
    ],
    "risk_register_md": [
        r"\brisk register\b", r"\brisks\b", r"\brisk log\b", r"\bmitigations?\b",
    ],
    "tco_estimate_md": [
        r"\btco\b", r"\bcost estimate\b", r"\bcost model\b",
        r"\bcost comparison\b", r"\bcost analysis\b",
        r"\bbusiness case\b", r"\bcost breakdown\b",
        r"\brun-rate\b", r"\bannual cost\b", r"\bcurrent state vs\b",
    ],
}

IMPERATIVE_VERBS = re.compile(
    r"\b(draft|generate|create|outline|build|produce|give me|make me|write|estimate|model)\b",
    re.IGNORECASE,
)


# Phase-gated required inputs (Iteration 2.2). Each artifact kind maps to a
# predicate that returns True when the profile has enough substance to
# produce a defensible artifact. If the predicate returns False, the
# artifact is suppressed — the response prompt handles it verbally.
#
# Deliberately conservative: an artifact with fabricated numbers is worse
# than no artifact. If a required signal isn't in the profile, the SA
# gets "I can draft this once I know X and Y" instead of a hallucinated
# wave plan / TCO / diagram.

def _tco_inputs_present(profile) -> bool:
    """TCO needs SOMETHING to size against. Either explicit MIPS, or a
    program count paired with at least one workload type indicator."""
    w = profile.workload
    if w.mips_capacity:
        return True
    if w.num_cobol_programs and (w.has_cics or w.has_db2 or w.has_ims):
        return True
    return False


def _wave_plan_inputs_present(profile) -> bool:
    """Wave plan needs SOME workload signal — otherwise it's a hallucinated
    sequence of made-up applications."""
    w = profile.workload
    return bool(
        w.num_cobol_programs
        or w.has_cics or w.has_db2 or w.has_ims
        or w.databases or w.languages
    )


def _mermaid_inputs_present(profile) -> bool:
    """Architecture diagram needs to know what to draw — same signal as wave plan."""
    return _wave_plan_inputs_present(profile)


def _risk_inputs_present(profile) -> bool:
    """Risk register can render generic risks with no profile — first-line
    risks like "audit-trail discontinuity" apply universally. Always OK."""
    return True


REQUIRED_INPUTS = {
    "wave_plan_csv":       _wave_plan_inputs_present,
    "mermaid_architecture": _mermaid_inputs_present,
    "risk_register_md":    _risk_inputs_present,
    "tco_estimate_md":     _tco_inputs_present,
}


# Human-readable description of what's missing when a gate blocks an artifact.
# Used by the response prompt to tell the SA which facts to provide.
MISSING_INPUTS_MESSAGE = {
    "wave_plan_csv":
        "I can draft the wave plan once I know some workload shape — "
        "program count, whether CICS/DB2/IMS is in scope, or the "
        "languages/databases in play.",
    "mermaid_architecture":
        "I can draft the target-state diagram once I know the workload shape — "
        "which mainframe components are in scope (CICS/DB2/IMS/VSAM/MQ).",
    "risk_register_md": None,  # always safe to emit
    "tco_estimate_md":
        "I can build a directional TCO once I know either the mainframe "
        "MIPS capacity, or the program count paired with the workload "
        "type (CICS/DB2/IMS). Without one of those, any number would "
        "be pure fabrication.",
}


def pick_artifacts(query: str, profile=None) -> list[str]:
    """Return a list of artifact kinds to generate for this turn.

    Iteration 2.2: filters out artifacts whose required inputs aren't in
    the profile. The response prompt then tells the SA what's missing
    (see MISSING_INPUTS_MESSAGE).
    """
    if not IMPERATIVE_VERBS.search(query or ""):
        return []
    q = (query or "").lower()
    kinds = []
    for kind, patterns in ARTIFACT_TRIGGERS.items():
        if not any(re.search(p, q) for p in patterns):
            continue
        # Phase gate: check required inputs. If profile is None (test path),
        # let it through — tests can set up whatever inputs they want.
        if profile is not None:
            gate = REQUIRED_INPUTS.get(kind)
            if gate is not None and not gate(profile):
                logger.info(f"pick_artifacts: {kind} suppressed — required inputs missing")
                continue
        kinds.append(kind)
    return kinds


def get_missing_inputs_notes(query: str, profile) -> list[str]:
    """Return a list of "I can draft X once I know Y" messages for
    artifacts that WOULD have fired but were suppressed by phase-gate
    inputs check. Response prompt uses these to explain what's needed."""
    if profile is None or not IMPERATIVE_VERBS.search(query or ""):
        return []
    q = (query or "").lower()
    notes: list[str] = []
    for kind, patterns in ARTIFACT_TRIGGERS.items():
        if not any(re.search(p, q) for p in patterns):
            continue
        gate = REQUIRED_INPUTS.get(kind)
        if gate is None or gate(profile):
            continue
        msg = MISSING_INPUTS_MESSAGE.get(kind)
        if msg:
            notes.append(msg)
    return notes


# ---------------------------------------------------------------------------
# Wave plan CSV
# ---------------------------------------------------------------------------

def _wave_plan(profile: CustomerProfile) -> Artifact:
    """Template-driven wave plan sized to the customer's workload."""
    w = profile.workload
    n_pgm = w.num_cobol_programs or 100
    has_online = bool(w.has_cics or w.has_ims)
    has_db = bool(w.has_db2 or (w.databases and "DB2" in w.databases))
    has_vsam = (w.num_vsam_files or 0) > 0 or (w.databases and "VSAM" in w.databases)

    rows: list[list[str]] = [[
        "Wave", "Name", "Scope", "Pattern", "Duration",
        "Key AWS Services", "Dependencies", "FSI Considerations", "Exit Criteria"
    ]]

    rows.append([
        "0", "Discovery & Inventory",
        f"All {n_pgm} programs, JCL, data stores",
        "assessment",
        "6-8 weeks",
        "AWS Migration Hub; AWS Application Discovery Service",
        "—",
        "Baseline for SOX change-mgmt evidence",
        "Complete inventory + dependency graph + 6Rs per app",
    ])
    rows.append([
        "1", "Non-critical Batch Rehost",
        "~20% of batch workload, no online dependency",
        "rehost",
        "3-4 months",
        "AWS M2 (Micro Focus runtime); S3; DataSync",
        "Wave 0",
        "Immutable log shipping to CloudTrail + S3 Object Lock",
        "Batch runs parity vs mainframe for 30 days",
    ])

    if has_db:
        rows.append([
            "2", "Reference-data to Aurora",
            "DB2 reference tables (read-heavy)",
            "replatform (data-first)",
            "3-4 months",
            "AWS SCT; DMS CDC; Aurora PostgreSQL",
            "Wave 1",
            "Field-level encryption for PII; KMS CMKs per env",
            "CDC lag < 5s; read-traffic cutover",
        ])
    if has_vsam:
        rows.append([
            "3", "VSAM → DynamoDB / S3",
            "KSDS lookup files and flat sequentials",
            "refactor (data)",
            "4-6 months",
            "DynamoDB; S3; Glue; Lambda",
            "Wave 1",
            "Point-in-time recovery; access logging to CloudTrail",
            "Dual-write burn-in; lookup-lat <10ms p99",
        ])
    if has_online:
        rows.append([
            "4", "Online Transactions",
            "CICS/IMS customer-facing apps",
            "automated_refactor (Blu Age/Heirloom)" if n_pgm > 200 else "replatform",
            "9-12 months",
            "ECS Fargate; API Gateway; Cognito; ElastiCache",
            "Waves 1-3",
            "DR with pilot-light to second region; RPO<=15m",
            "Green-field UAT parity; gradual traffic shift 1%→100%",
        ])
    rows.append([
        str(len(rows) - 1), "Decommission & Exit",
        "Shutdown mainframe LPARs; archive tapes",
        "retire",
        "2-3 months",
        "S3 Glacier Deep Archive",
        "All prior waves",
        "Regulator notification; 7-10yr archival per record-retention policy",
        "Contract terminated; final audit sign-off",
    ])

    buf = io.StringIO()
    w2 = csv.writer(buf)
    w2.writerows(rows)

    return {
        "kind": "wave_plan_csv",
        "title": f"Migration Wave Plan — {profile.customer_display_name or profile.customer_id}",
        "content": buf.getvalue(),
        "mime_type": "text/csv",
    }


# ---------------------------------------------------------------------------
# Mermaid architecture
# ---------------------------------------------------------------------------

def _mermaid_architecture(profile: CustomerProfile) -> Artifact:
    """Target-state architecture diagram parameterized by workload."""
    w = profile.workload
    lines = [
        "flowchart LR",
        "  subgraph On-Prem [Remaining On-Prem]",
        "    MF[z/OS Mainframe<br/>during migration]",
        "  end",
        "  subgraph AWS [AWS — Target State]",
        "    APIGW[API Gateway]",
        "    Cognito[Amazon Cognito]",
    ]
    if w.has_cics or w.has_ims:
        lines.append("    ECS[ECS Fargate<br/>Refactored Online Apps]")
        lines.append("    APIGW --> ECS")
    lines.append("    M2[AWS Mainframe Modernization<br/>Micro Focus Runtime]")
    lines.append("    Batch[AWS Batch / EventBridge Scheduler]")

    if w.has_db2:
        lines.append("    Aurora[(Aurora PostgreSQL<br/>from DB2)]")
    if (w.num_vsam_files or 0) > 0 or "VSAM" in (w.databases or []):
        lines.append("    DDB[(DynamoDB<br/>from VSAM KSDS)]")
        lines.append("    S3flat[(S3<br/>from flat files)]")
    if w.has_mq:
        lines.append("    MQ[Amazon MQ / MSK]")

    lines.extend([
        "    KMS[[AWS KMS]]",
        "    CT[[CloudTrail + S3 Object Lock]]",
        "    CW[[CloudWatch + ADOT]]",
    ])
    lines.append("  end")

    # connections
    if w.has_cics or w.has_ims:
        lines.append("  Cognito --> APIGW")
        if w.has_db2:
            lines.append("  ECS --> Aurora")
        if (w.num_vsam_files or 0) > 0:
            lines.append("  ECS --> DDB")
    lines.append("  M2 --> Batch")
    if w.has_db2:
        lines.append("  Batch --> Aurora")
    lines.append("  MF -. DMS CDC .-> Aurora" if w.has_db2 else "  MF -. DataSync .-> S3flat")

    content = "\n".join(lines)

    return {
        "kind": "mermaid_architecture",
        "title": f"Target Architecture — {profile.customer_display_name or profile.customer_id}",
        "content": content,
        "mime_type": "text/x-mermaid",
    }


# ---------------------------------------------------------------------------
# Risk register
# ---------------------------------------------------------------------------

def _risk_register(profile: CustomerProfile) -> Artifact:
    w = profile.workload
    c = profile.constraints
    rows: list[tuple[str, str, str, str, str, str]] = []
    # (ID, Risk, Likelihood, Impact, Mitigation, Owner)

    rows.append(("R1",
                 "Data divergence during parallel run",
                 "Medium", "High",
                 "Automated reconciliation jobs; daily diff reports; CDC lag alarms",
                 "Migration Lead"))
    if w.has_cics or w.has_ims:
        rows.append(("R2",
                     "Online-transaction regression post-cutover",
                     "Medium", "High",
                     "Shadow traffic + gradual canary (1%→10%→100%); rollback runbook",
                     "App Owner"))
    if "SOX" in (c.regulations or []) or "FFIEC" in (c.regulations or []):
        rows.append(("R3",
                     "Audit-trail discontinuity at cutover",
                     "Low", "High",
                     "Continuous CloudTrail + S3 Object Lock; cross-account log archive; pre-cutover audit rehearsal",
                     "Compliance Officer"))
    if "PCI_DSS" in (c.regulations or []):
        rows.append(("R4",
                     "Cardholder data exposure in transit to AWS",
                     "Low", "Critical",
                     "Direct Connect + MACsec; KMS envelope encryption; tokenization at source",
                     "Security Architect"))
    if w.has_db2:
        rows.append(("R5",
                     "DB2 stored-procedure semantics lost in Aurora",
                     "Medium", "Medium",
                     "AWS SCT assessment; re-implement in PL/pgSQL with parity tests",
                     "DBA Lead"))
    if (w.num_copybooks or 0) > 50:
        rows.append(("R6",
                     "Copybook coupling blocks independent deployment",
                     "High", "Medium",
                     "Copybook inventory + shared-library strategy; decouple before refactor waves",
                     "Dev Lead"))
    if not c.regulations:
        rows.append(("R7",
                     "Regulatory posture not yet confirmed",
                     "—", "—",
                     "Workshop with Compliance; lock FSI regs before Wave 1 design",
                     "Program Manager"))

    md = [f"# Risk Register — {profile.customer_display_name or profile.customer_id}", ""]
    md.append("| ID | Risk | Likelihood | Impact | Mitigation | Owner |")
    md.append("|---|---|---|---|---|---|")
    for r in rows:
        md.append("| " + " | ".join(r) + " |")

    return {
        "kind": "risk_register_md",
        "title": f"Risk Register — {profile.customer_display_name or profile.customer_id}",
        "content": "\n".join(md),
        "mime_type": "text/markdown",
    }


# ---------------------------------------------------------------------------
# TCO estimate (Iteration 2.1)
# ---------------------------------------------------------------------------
#
# Design principle: directional numbers with assumptions surfaced. Not a
# bill-of-materials. If we can't get to ±30% confidence from the profile,
# we say "insufficient inputs" and suppress via the phase gate. Any
# specific $-number the SA copy-pastes into a deck needs to survive being
# challenged by a customer procurement team, so we lean toward
# under-promising ranges and citing the reasoning inline.

# Industry-standard mainframe TCO benchmarks (annual $ per MIPS).
# Bands drawn from public analyst reports (Gartner, IBM's own competitive
# collateral, Rocket-cited studies) — genuine range, not a single number.
# The KB has richer partner-specific figures if the SA wants to dig deeper.
_MAINFRAME_ANNUAL_COST_PER_MIPS = {
    "low":  2000,   # highly optimized shops; older LPAR-heavy platforms
    "mid":  3500,   # typical FSI mainframe cost/MIPS
    "high": 5000,   # heavy ISV licensing (CA/Broadcom + IBM + DB2 licenses)
}

# AWS annualized run-rate per "unit" of workload. Deliberately coarse —
# these anchor a directional number, they don't replace the Pricing MCP
# for specific instance-type queries. Round numbers so it's obvious to
# the reader that these are order-of-magnitude estimates, not quotes.
_AWS_ANNUAL_COMPONENT_COSTS = {
    "cics_online_ecs":    120_000,   # ECS Fargate hosting refactored online apps (per app group)
    "batch_workload":      40_000,   # AWS Batch + EventBridge scheduling
    "aurora_db2":         180_000,   # Aurora PostgreSQL replacing DB2 (mid-size cluster, HA)
    "dynamodb_vsam":       36_000,   # DynamoDB replacing VSAM KSDS
    "s3_flat_files":       12_000,   # S3 for QSAM/flat file archives + working data
    "mq_replacement":      36_000,   # Amazon MQ or MSK for MQ Series replacement
    "shared_platform":     60_000,   # CloudTrail, KMS, IAM, CloudWatch, Backup — always applies
}


def _classify_mips_band(mips: int) -> str:
    """Return the per-MIPS cost band label based on typical estate size."""
    # Small estates pay disproportionately more per MIPS (fixed license floors).
    # Large estates get scale discounts but often have deeper ISV entanglement.
    if mips < 1000:
        return "high"
    if mips < 8000:
        return "mid"
    return "low"


def _estimate_mainframe_cost(profile: CustomerProfile) -> tuple[Optional[int], Optional[int], list[str]]:
    """Return (low_estimate, high_estimate, assumptions) for annual mainframe cost.
    Returns (None, None, [...]) if MIPS unknown."""
    w = profile.workload
    assumptions: list[str] = []
    # Coerce numeric signals to int — DynamoDB deserializes numbers as Decimal
    # and `Decimal * float` raises TypeError. All downstream math here is
    # order-of-magnitude, so integer coercion is safe.
    mips_capacity = int(w.mips_capacity) if w.mips_capacity else 0
    num_cobol_programs = int(w.num_cobol_programs) if w.num_cobol_programs else 0
    if mips_capacity:
        mips = mips_capacity
        band = _classify_mips_band(mips)
        # Use the band as midpoint; widen ±25% for realistic range
        mid_rate = _MAINFRAME_ANNUAL_COST_PER_MIPS[band]
        low = int(mips * mid_rate * 0.75)
        high = int(mips * mid_rate * 1.25)
        assumptions.append(
            f"Annual mainframe cost estimated at ${mid_rate:,}/MIPS "
            f"({band} band for {mips:,}-MIPS estate) with ±25% range for licensing/ops variability."
        )
        assumptions.append(
            "Covers hardware lease, IBM z/OS + subsystem licensing (CICS, DB2, MQ), "
            "ISV licensing (CA/Broadcom, BMC), facilities, ops staff. Excludes application "
            "development staff (comparable across current-state and target-state)."
        )
        return low, high, assumptions
    # No MIPS — try to infer from program count. Rough proxy: 1000 programs
    # ≈ 500-2000 MIPS depending on estate density. Fall back to that range.
    if num_cobol_programs:
        n = num_cobol_programs
        # 500 MIPS per 1000 programs (low), 2000 MIPS per 1000 programs (high)
        low_mips = int(n * 0.5)
        high_mips = int(n * 2.0)
        low = int(low_mips * _MAINFRAME_ANNUAL_COST_PER_MIPS["mid"] * 0.75)
        high = int(high_mips * _MAINFRAME_ANNUAL_COST_PER_MIPS["mid"] * 1.25)
        assumptions.append(
            f"MIPS not stated — inferred {low_mips:,}-{high_mips:,} MIPS from "
            f"{n:,} COBOL programs using a 0.5-2 MIPS-per-program density band."
        )
        assumptions.append(
            f"Applied ${_MAINFRAME_ANNUAL_COST_PER_MIPS['mid']:,}/MIPS midpoint × ±25%. "
            "**High uncertainty** — please confirm actual MIPS to tighten this estimate."
        )
        return low, high, assumptions
    return None, None, assumptions


def _estimate_aws_cost(profile: CustomerProfile) -> tuple[int, int, list[str]]:
    """Return (low_estimate, high_estimate, breakdown_lines) for annual AWS cost."""
    w = profile.workload
    # Coerce numeric signals to int — DDB deserializes numbers as Decimal.
    num_cobol_programs = int(w.num_cobol_programs) if w.num_cobol_programs else 0
    num_vsam_files = int(w.num_vsam_files) if w.num_vsam_files else 0
    components: list[tuple[str, int]] = []

    # Shared platform is always present
    components.append(("Shared platform (CloudTrail, KMS, IAM, CloudWatch, Backup)",
                       _AWS_ANNUAL_COMPONENT_COSTS["shared_platform"]))

    if w.has_cics or w.has_ims:
        # Scale by program count roughly — 500 programs → 1 unit, up to 3 units for large
        units = 1
        if num_cobol_programs > 500:
            units = 2
        if num_cobol_programs > 2000:
            units = 3
        components.append((
            f"Online transactions (CICS/IMS) on ECS Fargate — {units} app-group unit(s)",
            _AWS_ANNUAL_COMPONENT_COSTS["cics_online_ecs"] * units,
        ))

    # Batch always assumed if there's ANY mainframe workload
    components.append((
        "Batch workload (AWS Batch + EventBridge Scheduler)",
        _AWS_ANNUAL_COMPONENT_COSTS["batch_workload"],
    ))

    if w.has_db2 or (w.databases and "DB2" in w.databases):
        components.append((
            "DB2 → Aurora PostgreSQL (Multi-AZ, mid-size cluster)",
            _AWS_ANNUAL_COMPONENT_COSTS["aurora_db2"],
        ))

    if num_vsam_files > 0 or (w.databases and "VSAM" in w.databases):
        components.append((
            "VSAM → DynamoDB (KSDS lookup files)",
            _AWS_ANNUAL_COMPONENT_COSTS["dynamodb_vsam"],
        ))
        components.append((
            "Flat files (QSAM) → S3 (archives + working data)",
            _AWS_ANNUAL_COMPONENT_COSTS["s3_flat_files"],
        ))

    if w.has_mq:
        components.append((
            "IBM MQ → Amazon MQ / MSK",
            _AWS_ANNUAL_COMPONENT_COSTS["mq_replacement"],
        ))

    mid_total = sum(c[1] for c in components)
    # AWS cost variance is tighter than mainframe (fewer license entanglements)
    low = int(mid_total * 0.75)
    high = int(mid_total * 1.35)
    breakdown = [f"- **{name}** — ~${cost:,}/year" for name, cost in components]
    return low, high, breakdown


def _tco_estimate(profile: CustomerProfile) -> Artifact:
    """Directional TCO comparison. Assumes required inputs are present
    (phase gate blocks otherwise per _tco_inputs_present)."""
    w = profile.workload
    c = profile.constraints

    mf_low, mf_high, mf_assumptions = _estimate_mainframe_cost(profile)
    aws_low, aws_high, aws_breakdown = _estimate_aws_cost(profile)

    display_name = profile.customer_display_name or profile.customer_id

    md: list[str] = [f"# TCO Estimate — {display_name}", ""]
    md.append("> Directional estimate. Numbers are ranges, not quotes. "
              "Confirm all assumptions with the customer before circulating.")
    md.append("")

    # Workload snapshot
    md.append("## Workload snapshot")
    md.append("")
    md.append("| Signal | Value |")
    md.append("|---|---|")
    if w.mips_capacity:
        md.append(f"| MIPS capacity | {int(w.mips_capacity):,} |")
    if w.num_cobol_programs:
        md.append(f"| COBOL programs | {int(w.num_cobol_programs):,} |")
    workload_types = []
    if w.has_cics: workload_types.append("CICS")
    if w.has_db2: workload_types.append("DB2")
    if w.has_ims: workload_types.append("IMS")
    if w.has_mq:  workload_types.append("MQ")
    if (int(w.num_vsam_files) if w.num_vsam_files else 0) > 0 or (w.databases and "VSAM" in w.databases):
        workload_types.append("VSAM")
    if workload_types:
        md.append(f"| Workload types | {', '.join(workload_types)} |")
    if c.regulations:
        md.append(f"| Regulatory scope | {', '.join(c.regulations)} |")
    md.append("")

    # Current-state (mainframe) cost
    md.append("## Current-state annual cost (mainframe)")
    md.append("")
    if mf_low is not None and mf_high is not None:
        md.append(f"**Estimated range: ${mf_low:,} — ${mf_high:,} per year**")
        md.append("")
        for a in mf_assumptions:
            md.append(f"- {a}")
    else:
        md.append("*Insufficient inputs to estimate current-state cost. "
                  "Confirm MIPS capacity or program count to enable this number.*")
    md.append("")

    # Target-state (AWS) cost
    md.append("## Target-state annual cost (AWS)")
    md.append("")
    md.append(f"**Estimated range: ${aws_low:,} — ${aws_high:,} per year**")
    md.append("")
    md.append("Component breakdown (midpoint):")
    for line in aws_breakdown:
        md.append(line)
    md.append("")
    md.append("- Ranges reflect ±25-35% variance on Reserved / Savings Plan commitments, "
              "region choice, and DR posture (single-region vs multi-region).")
    md.append("- Excludes migration project costs (assessment, refactor, cutover, "
              "parallel-run) — those are one-time and modeled separately.")
    md.append("- Uses AWS list pricing; a real customer engagement would apply "
              "EDP / private-pricing discounts on top.")
    md.append("")

    # Savings + break-even framing
    if mf_low is not None and mf_high is not None:
        # Midpoint-to-midpoint savings
        mf_mid = (mf_low + mf_high) // 2
        aws_mid = (aws_low + aws_high) // 2
        annual_savings = mf_mid - aws_mid
        if annual_savings > 0:
            savings_pct = (annual_savings / mf_mid) * 100
            md.append("## Directional savings")
            md.append("")
            md.append(f"**~${annual_savings:,}/year at midpoint** "
                      f"(~{savings_pct:.0f}% reduction vs current state).")
            md.append("")
            md.append("Additional non-cost benefits typically drive the business case:")
            md.append("- Time-to-market for new digital channels (weeks vs quarters)")
            md.append("- Talent availability (Java/Python vs shrinking mainframe pool)")
            md.append("- Elastic capacity for peak events (seasonal, product launches)")
        else:
            md.append("## Cost neutrality note")
            md.append("")
            md.append("**Migration is not primarily a cost play at this scale.** "
                      "The estimated AWS run-rate is within the same range as the "
                      "current mainframe cost. Business case should lean on "
                      "agility / talent / resilience benefits rather than pure infra savings.")
        md.append("")

    # Assumptions and next steps
    md.append("## Key assumptions and gaps")
    md.append("")
    gaps: list[str] = []
    if not w.mips_capacity:
        gaps.append("MIPS capacity — largest driver of current-state estimate accuracy")
    if not c.target_date:
        gaps.append("Target completion date — affects Reserved Instance vs Savings Plan mix")
    if not c.data_residency:
        gaps.append("Data residency requirements — determines eligible AWS regions and DR cost")
    if not c.regulations:
        gaps.append("Regulatory scope — determines mandatory controls and evidence tooling")
    if not w.online_tps_peak:
        gaps.append("Peak online TPS — determines ECS Fargate sizing precisely")
    if not w.batch_window_hours:
        gaps.append("Batch window duration — determines AWS Batch compute reservation")
    if gaps:
        for g in gaps:
            md.append(f"- {g}")
    else:
        md.append("- All primary inputs present. Proceed with detailed sizing workshop.")
    md.append("")

    md.append("---")
    md.append("")
    md.append("*Estimate generated deterministically from customer profile. "
              "For instance-level pricing, use the live AWS Pricing API "
              "(`get_pricing` tool). Numbers here are annualized run-rate for "
              "post-migration steady state.*")

    return {
        "kind": "tco_estimate_md",
        "title": f"TCO Estimate — {display_name}",
        "content": "\n".join(md),
        "mime_type": "text/markdown",
    }


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

_BUILDERS = {
    "wave_plan_csv": _wave_plan,
    "mermaid_architecture": _mermaid_architecture,
    "risk_register_md": _risk_register,
    "tco_estimate_md": _tco_estimate,
}


def artifact_node(state: AgentState) -> dict:
    """Generate artifacts if the turn is imperative and matches a trigger.

    Phase-gated (Iteration 2.2): artifacts whose required inputs are missing
    are suppressed and surface as `artifact_gap_notes` on state so the
    response prompt can tell the SA what facts to provide.
    """
    query = state.get("user_query", "") or ""
    profile: CustomerProfile = state.get("customer_profile")
    if profile is None:
        return {"artifacts": [], "artifact_gap_notes": []}

    kinds = pick_artifacts(query, profile=profile)
    gap_notes = get_missing_inputs_notes(query, profile)

    artifacts: list[Artifact] = []
    for kind in kinds:
        builder = _BUILDERS.get(kind)
        if not builder:
            continue
        try:
            artifacts.append(builder(profile))
        except Exception as e:
            logger.warning(f"artifact_node: {kind} build failed: {e}")
    logger.info(
        f"artifact_node: produced {[a['kind'] for a in artifacts]}"
        + (f"; gap notes: {len(gap_notes)}" if gap_notes else "")
    )
    return {"artifacts": artifacts, "artifact_gap_notes": gap_notes}
