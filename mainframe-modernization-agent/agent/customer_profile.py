"""CustomerProfile — the per-(SA, customer) memory record.

Design notes:
- Every field is optional; the profile is built turn-by-turn.
- Facts carry provenance (turn number they were stated in, optional rationale).
- Merging is explicit: new facts either extend lists, or surface contradictions
  rather than silently overwrite. The graph's profile-updater node is the only
  place that decides *how* to resolve a contradiction (ask the SA, or accept).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import time


@dataclass
class Workload:
    num_cobol_programs: Optional[int] = None
    num_jcl_jobs: Optional[int] = None
    num_vsam_files: Optional[int] = None
    num_copybooks: Optional[int] = None
    has_cics: Optional[bool] = None
    has_db2: Optional[bool] = None
    has_ims: Optional[bool] = None
    has_mq: Optional[bool] = None
    languages: list[str] = field(default_factory=list)     # ["COBOL", "PL/I", "Natural"]
    databases: list[str] = field(default_factory=list)     # ["DB2", "VSAM", "IMS-DB"]
    mainframe_vendor: Optional[str] = None                 # "IBM z/OS", "Unisys", etc.
    mips_capacity: Optional[int] = None                    # peak/installed MIPS
    online_tps_peak: Optional[int] = None                  # CICS/IMS online TPS at peak
    batch_window_hours: Optional[int] = None               # nightly batch window length


@dataclass
class Constraints:
    regulations: list[str] = field(default_factory=list)   # ["SOX", "FFIEC", "PCI_DSS"]
    data_residency: list[str] = field(default_factory=list)  # AWS regions
    target_date: Optional[str] = None                      # "2027-Q2"
    budget_band: Optional[str] = None                      # "under_5M", "5_to_20M", etc.
    risk_appetite: Optional[str] = None                    # "low", "medium", "high"
    downtime_tolerance: Optional[str] = None               # "zero", "weekend", "extended"


@dataclass
class Decision:
    turn: int
    category: str                 # "pattern", "partner", "target_service", etc.
    value: str                    # "replatform", "MicroFocus", "Aurora PostgreSQL"
    rationale: str = ""
    stated_at: float = field(default_factory=time.time)
    superseded_by_turn: Optional[int] = None


@dataclass
class Fact:
    """A raw stated fact with provenance. Used for audit + contradiction surfacing."""
    turn: int
    field_path: str               # e.g. "workload.num_cobol_programs"
    value: Any
    stated_at: float = field(default_factory=time.time)
    superseded_by_turn: Optional[int] = None


@dataclass
class CustomerProfile:
    sa_id: str
    customer_id: str
    customer_display_name: str = ""
    industry_segment: str = ""                              # "core-banking", "insurance", etc.

    # Line of Business — tertiary key for FSI customers where each LoB
    # (Cards, Wealth, Capital Markets, P&C, Life, etc.) typically has its
    # own mainframe estate, partners, regulations, and timeline.
    # Default to "default" when the SA hasn't selected one.
    lob_id: str = "default"
    lob_display_name: str = ""

    workload: Workload = field(default_factory=Workload)
    constraints: Constraints = field(default_factory=Constraints)

    decisions_made: list[Decision] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 1

    # -- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CustomerProfile":
        d = dict(d)
        # Filter unknown keys from nested dataclasses so a future schema
        # addition doesn't break deserialization of existing rows.
        wl_fields = {f.name for f in Workload.__dataclass_fields__.values()}
        c_fields = {f.name for f in Constraints.__dataclass_fields__.values()}
        d_fields = {f.name for f in Decision.__dataclass_fields__.values()}
        f_fields = {f.name for f in Fact.__dataclass_fields__.values()}

        d["workload"] = Workload(**{k: v for k, v in (d.get("workload") or {}).items() if k in wl_fields})
        d["constraints"] = Constraints(**{k: v for k, v in (d.get("constraints") or {}).items() if k in c_fields})
        d["decisions_made"] = [Decision(**{k: v for k, v in x.items() if k in d_fields}) for x in d.get("decisions_made", [])]
        d["facts"] = [Fact(**{k: v for k, v in x.items() if k in f_fields}) for x in d.get("facts", [])]
        # Backward-compat: rows written before LoB landed
        d.setdefault("lob_id", "default")
        d.setdefault("lob_display_name", "")
        # Filter top-level keys too
        cls_fields = {f.name for f in cls.__dataclass_fields__.values()}
        d = {k: v for k, v in d.items() if k in cls_fields}
        return cls(**d)

    # -- prompt rendering ---------------------------------------------------

    def _customer_label(self) -> str:
        """Customer + LoB header. LoB is shown only when explicitly set."""
        cust = self.customer_display_name or self.customer_id
        if self.lob_id and self.lob_id != "default":
            lob = self.lob_display_name or self.lob_id
            return f"{cust} / {lob}"
        return cust

    def render_for_prompt(self) -> str:
        """Compact human-readable view for injection into LLM prompts."""
        cust_id = (self.customer_id or "").strip().lower()
        unbound = (not cust_id) or cust_id == "default"
        if unbound:
            return (
                "Customer: (none bound — SA hasn't selected a customer yet). "
                "Answer generically; do NOT invent customer-specific facts."
            )
        label = self._customer_label()
        if self.is_empty():
            return (
                f"Customer: **{label}** is BOUND for this conversation. "
                f"This slice has no facts captured yet — check CUSTOMER-WIDE "
                f"OVERVIEW below for facts the SA shared in other LoBs. "
                f"Address answers to {label}; do NOT claim no customer is set."
            )

        lines = [f"Customer: **{label}** (BOUND)"]
        if self.industry_segment:
            lines.append(f"  Segment: {self.industry_segment}")

        w = self.workload
        wl = []
        if w.mips_capacity is not None: wl.append(f"{w.mips_capacity} MIPS")
        if w.online_tps_peak is not None: wl.append(f"{w.online_tps_peak} TPS peak")
        if w.batch_window_hours is not None: wl.append(f"{w.batch_window_hours}h batch window")
        if w.num_cobol_programs is not None: wl.append(f"{w.num_cobol_programs} COBOL pgms")
        if w.num_jcl_jobs is not None: wl.append(f"{w.num_jcl_jobs} JCL jobs")
        if w.num_vsam_files is not None: wl.append(f"{w.num_vsam_files} VSAM files")
        if w.has_cics: wl.append("CICS")
        if w.has_db2: wl.append("DB2")
        if w.has_ims: wl.append("IMS")
        if w.has_mq: wl.append("MQ")
        if w.languages: wl.append("langs=" + ",".join(w.languages))
        if wl:
            lines.append(f"  Workload: {'; '.join(wl)}")

        c = self.constraints
        cl = []
        if c.regulations: cl.append("regs=" + ",".join(c.regulations))
        if c.target_date: cl.append(f"target={c.target_date}")
        if c.data_residency: cl.append("residency=" + ",".join(c.data_residency))
        if c.downtime_tolerance: cl.append(f"downtime={c.downtime_tolerance}")
        if cl:
            lines.append(f"  Constraints: {'; '.join(cl)}")

        if self.decisions_made:
            active = [d for d in self.decisions_made if d.superseded_by_turn is None]
            if active:
                lines.append("  Decisions: " + "; ".join(f"{d.category}={d.value}" for d in active))

        if self.open_questions:
            lines.append("  Open questions: " + "; ".join(self.open_questions[:5]))

        return "\n".join(lines)

    def render_for_summary(self) -> str:
        """Markdown summary suitable for direct display to the SA.

        Where `render_for_prompt()` is compact (for LLM context windows),
        this is verbose and structured (for the SA's "what does the agent
        know" view). Used by the whatDoYouKnow capability (item 1.11).
        """
        name = self._customer_label()
        phase = self.derive_phase()

        if self.is_empty():
            return (
                f"### What I know about **{name}**\n\n"
                f"_Nothing yet — engagement phase: **{phase}**._\n\n"
                f"Tell me a bit about the customer (workload size, "
                f"regulations, target date) and I'll start building a profile."
            )

        sections: list[str] = []
        sections.append(f"### What I know about **{name}**")
        sections.append(f"_Engagement phase: **{phase}** "
                        f"(profile version {self.version})_")

        if self.industry_segment:
            sections.append(f"**Segment:** {self.industry_segment}")

        # Workload
        w = self.workload
        wl: list[str] = []
        if w.mips_capacity is not None:      wl.append(f"- {w.mips_capacity:,} MIPS")
        if w.online_tps_peak is not None:    wl.append(f"- {w.online_tps_peak:,} TPS peak (online)")
        if w.batch_window_hours is not None: wl.append(f"- {w.batch_window_hours}h nightly batch window")
        if w.num_cobol_programs is not None: wl.append(f"- {w.num_cobol_programs:,} COBOL programs")
        if w.num_jcl_jobs is not None:       wl.append(f"- {w.num_jcl_jobs:,} JCL jobs")
        if w.num_vsam_files is not None:     wl.append(f"- {w.num_vsam_files:,} VSAM files")
        if w.num_copybooks is not None:      wl.append(f"- {w.num_copybooks:,} copybooks")
        if w.has_cics:                       wl.append("- CICS online transactions")
        if w.has_db2:                        wl.append("- DB2")
        if w.has_ims:                        wl.append("- IMS")
        if w.has_mq:                         wl.append("- MQ Series")
        if w.languages:                      wl.append(f"- Languages: {', '.join(w.languages)}")
        if w.databases:                      wl.append(f"- Databases: {', '.join(w.databases)}")
        if w.mainframe_vendor:               wl.append(f"- Vendor: {w.mainframe_vendor}")
        if wl:
            sections.append("**Workload:**\n" + "\n".join(wl))

        # Constraints
        c = self.constraints
        cl: list[str] = []
        if c.regulations:         cl.append(f"- Regulations: {', '.join(c.regulations)}")
        if c.target_date:         cl.append(f"- Target date: {c.target_date}")
        if c.data_residency:      cl.append(f"- Data residency: {', '.join(c.data_residency)}")
        if c.budget_band:         cl.append(f"- Budget band: {c.budget_band}")
        if c.risk_appetite:       cl.append(f"- Risk appetite: {c.risk_appetite}")
        if c.downtime_tolerance:  cl.append(f"- Downtime tolerance: {c.downtime_tolerance}")
        if cl:
            sections.append("**Constraints:**\n" + "\n".join(cl))

        # Decisions (active first; superseded grouped at the end)
        active = [d for d in self.decisions_made if d.superseded_by_turn is None]
        superseded = [d for d in self.decisions_made if d.superseded_by_turn is not None]
        if active:
            dl = [f"- **{d.category}** = {d.value}"
                  + (f" — _{d.rationale}_" if d.rationale else "")
                  for d in active]
            sections.append("**Decisions:**\n" + "\n".join(dl))
        if superseded:
            sl = [f"- ~~{d.category} = {d.value}~~ _(superseded turn {d.superseded_by_turn})_"
                  for d in superseded]
            sections.append("**Earlier decisions (superseded):**\n" + "\n".join(sl))

        if self.open_questions:
            ql = [f"- {q}" for q in self.open_questions[:10]]
            sections.append("**Open questions queued for next turn:**\n" + "\n".join(ql))

        return "\n\n".join(sections)

    def render_customer_wide_summary(
        self,
        lob_profiles: list["CustomerProfile"],
    ) -> str:
        """Customer-level recap when no LoB is bound. Concatenates the
        per-LoB summaries so the SA can see what's known across the whole
        customer, not just the (currently empty) default-LoB slice.

        `lob_profiles` is the list of every non-default LoB profile this
        SA has for the customer (loaded by profile_loader_node into
        state["customer_overview"]). Bare facts only — no recommendation,
        no probe.
        """
        cust = self.customer_display_name or self.customer_id
        if not lob_profiles:
            return (
                f"### What I know about **{cust}**\n\n"
                f"_No facts captured yet across any Line of Business._\n\n"
                f"Pick a Line of Business at the top to start a focused "
                f"conversation, or just describe the workload here."
            )

        sections: list[str] = [
            f"### What I know about **{cust}**",
            f"_Customer-wide view across {len(lob_profiles)} "
            f"Line{'s' if len(lob_profiles) != 1 else ''} of Business._",
        ]

        for p in lob_profiles:
            lob_label = p.lob_display_name or p.lob_id
            inner = p.render_for_summary()
            # Strip the inner "### What I know about ..." header — we already
            # have the customer-level header; replace it with an LoB sub-header.
            inner_lines = inner.splitlines()
            stripped: list[str] = []
            skipping_intro = False
            for line in inner_lines:
                if line.startswith("### What I know about"):
                    continue
                if line.startswith("_Engagement phase:") or line.startswith("_Nothing yet"):
                    # Drop the per-LoB phase line; phase belongs at customer level
                    continue
                stripped.append(line)
            body = "\n".join(stripped).strip()
            sections.append(f"#### {lob_label}\n\n{body}" if body else
                            f"#### {lob_label}\n\n_No facts captured for this LoB yet._")

        return "\n\n".join(sections)

    def is_empty(self) -> bool:
        w = self.workload; c = self.constraints
        return (
            not self.industry_segment
            and all(v is None or v == [] or v is False for v in asdict(w).values())
            and all(v is None or v == [] for v in asdict(c).values())
            and not self.decisions_made
        )

    # -- phase derivation ---------------------------------------------------

    def derive_phase(self) -> str:
        """Return the engagement phase derived from profile completeness.

        Phases are gates, not categories — each requires the prior to be
        substantively filled. They're a hint to the response prompt about
        how to behave (probe hard in discovery, produce artifacts in
        proposal, stay tactical in execution).

        Per locked decision D2, this is a deterministic pure function — no
        LLM call. Reversible to LLM-classified phase later if controllability
        becomes a problem.
        """
        w = self.workload
        c = self.constraints

        # Workload signals (have we captured what's being modernized?)
        workload_known = any([
            w.num_cobol_programs is not None,
            w.num_jcl_jobs is not None,
            w.has_cics is True,
            w.has_db2 is True,
            w.has_ims is True,
            bool(w.languages),
            bool(w.databases),
        ])

        # Constraints signals (do we know the regulatory/business envelope?)
        constraints_known = any([
            bool(c.regulations),
            c.target_date is not None,
            c.budget_band is not None,
            c.downtime_tolerance is not None,
        ])

        # Decision signals
        active_decisions = [d for d in self.decisions_made
                            if d.superseded_by_turn is None]
        has_pattern = any(d.category == "pattern" for d in active_decisions)
        has_partner = any(d.category == "partner_tool" for d in active_decisions)
        has_target = any(d.category in ("target_service", "target_runtime")
                         for d in active_decisions)

        # Phase ladder — fall through from most-advanced to least
        if has_pattern and has_partner and (has_target or len(active_decisions) >= 3):
            # Decisions made, partner chosen, target picked or 3+ decisions —
            # treat tactical questions as execution-phase
            return "execution"
        if has_pattern and (has_partner or has_target):
            return "proposal"
        if workload_known and constraints_known and has_pattern:
            return "proposal"
        if workload_known and constraints_known:
            return "recommendation"
        if workload_known:
            return "assessment"
        return "discovery"

    # -- merging ------------------------------------------------------------

    def apply_fact(
        self,
        field_path: str,
        value: Any,
        turn: int,
    ) -> tuple[str, Optional[Any]]:
        """Apply a new fact. Returns (status, old_value).

        status is one of:
          - "set"         : field was empty, now populated
          - "extended"    : list-valued field, value appended
          - "unchanged"   : same value already stored
          - "contradicts" : different value already stored (NOT applied)
                            — graph's updater node must resolve
        """
        parts = field_path.split(".")
        target = self
        for p in parts[:-1]:
            target = getattr(target, p)
        attr = parts[-1]
        current = getattr(target, attr)

        if isinstance(current, list):
            if value in current:
                return "unchanged", current
            new_list = current + ([value] if not isinstance(value, list) else value)
            setattr(target, attr, new_list)
            self.facts.append(Fact(turn=turn, field_path=field_path, value=value))
            self.updated_at = time.time()
            self.version += 1
            self._prune_facts()
            return "extended", current

        if current is None or current == "":
            setattr(target, attr, value)
            self.facts.append(Fact(turn=turn, field_path=field_path, value=value))
            self.updated_at = time.time()
            self.version += 1
            self._prune_facts()
            return "set", None

        if current == value:
            return "unchanged", current

        # Contradiction — do NOT overwrite. Caller decides.
        return "contradicts", current

    def force_set(self, field_path: str, value: Any, turn: int) -> None:
        """Used by the updater node after the SA has confirmed an overwrite."""
        parts = field_path.split(".")
        target = self
        for p in parts[:-1]:
            target = getattr(target, p)
        attr = parts[-1]

        # Mark any previous fact for this path as superseded
        for f in self.facts:
            if f.field_path == field_path and f.superseded_by_turn is None:
                f.superseded_by_turn = turn

        setattr(target, attr, value)
        self.facts.append(Fact(turn=turn, field_path=field_path, value=value))
        self.updated_at = time.time()
        self.version += 1
        self._prune_facts()

    def add_decision(self, category: str, value: str, rationale: str, turn: int) -> None:
        # Supersede any prior active decision in the same category
        for d in self.decisions_made:
            if d.category == category and d.superseded_by_turn is None:
                d.superseded_by_turn = turn
        self.decisions_made.append(
            Decision(turn=turn, category=category, value=value, rationale=rationale)
        )
        self.updated_at = time.time()
        self.version += 1

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------
    # Caps per FIXES.md #14 (b). The whole profile lives in a single DDB
    # item with a 400KB hard limit; without these, a 6-month engagement
    # accumulates thousands of Fact entries and inflates every read/write
    # toward that ceiling.
    _MAX_FACTS_PER_FIELD = 5     # how many historical values to keep per field
    _MAX_TOTAL_FACTS = 200       # absolute cap across all fields

    def _prune_facts(self) -> None:
        """Drop superseded facts beyond _MAX_FACTS_PER_FIELD per field, and
        cap the total at _MAX_TOTAL_FACTS by dropping oldest superseded first.

        Active facts (superseded_by_turn is None) are NEVER dropped — they
        carry the live value the agent reads. Only history beyond the cap
        is trimmed.
        """
        if not self.facts:
            return

        # Group facts by field_path so we can keep the most recent N per field.
        by_field: dict[str, list[Fact]] = {}
        for f in self.facts:
            by_field.setdefault(f.field_path, []).append(f)

        kept: list[Fact] = []
        for field, facts in by_field.items():
            facts_sorted = sorted(facts, key=lambda f: f.turn, reverse=True)
            # Always keep active fact(s) — i.e. not superseded.
            active = [f for f in facts_sorted if f.superseded_by_turn is None]
            superseded = [f for f in facts_sorted if f.superseded_by_turn is not None]
            history_keep = max(0, self._MAX_FACTS_PER_FIELD - len(active))
            kept.extend(active)
            kept.extend(superseded[:history_keep])

        # Re-sort to original turn order so the visible history reads naturally.
        kept.sort(key=lambda f: f.turn)

        # Final absolute cap: if the per-field trim still leaves us over the
        # global ceiling, drop oldest superseded entries first.
        if len(kept) > self._MAX_TOTAL_FACTS:
            superseded_idxs = [
                i for i, f in enumerate(kept) if f.superseded_by_turn is not None
            ]
            superseded_idxs.sort(key=lambda i: kept[i].turn)  # oldest first
            drop_n = len(kept) - self._MAX_TOTAL_FACTS
            drop_set = set(superseded_idxs[:drop_n])
            kept = [f for i, f in enumerate(kept) if i not in drop_set]

        self.facts = kept
