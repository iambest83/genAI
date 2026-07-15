"""Typed state definition for the LangGraph agent.

Identity fields (sa_id, customer_id) are set once by the WS Lambda / entrypoint
from the validated JWT and the session's customer binding. They MUST NOT be
mutated inside the graph — isolation depends on it.
"""
from typing import Annotated, Any, Literal, Optional, TypedDict
from typing_extensions import NotRequired
from langgraph.graph.message import add_messages

from .customer_profile import CustomerProfile


# Routing destinations after the router_node. Six routes today:
#   kb | mcp | both          — substantive turns; differ in retrieval shape
#   direct                   — bare greeting, terse bound-aware reply
#   help                     — explicit "what can you do" — full intro deck
#   summary                  — "what do you know" / "where are we"
#   defer                    — SA skipped the prior probe; drop it, move on
#   acknowledge              — short ack with no new content
RouteTarget = Literal[
    "kb", "mcp", "both", "direct", "summary", "help", "defer", "acknowledge",
]


class PendingContradiction(TypedDict):
    field_path: str
    old_value: Any
    new_value: Any
    rationale: str


class Artifact(TypedDict):
    kind: str          # "wave_plan_csv" | "mermaid_architecture" | "risk_register_md" | ...
    title: str
    content: str
    mime_type: str     # "text/csv" | "text/x-mermaid" | "text/markdown"


class AgentState(TypedDict, total=False):
    # -- message history (LangGraph reducer) --
    messages: Annotated[list, add_messages]

    # -- identity (set once, frozen for session) --
    sa_id: str
    customer_id: str
    lob_id: str                  # "default" if SA hasn't selected an LoB
    lob_display_name: NotRequired[str]
    session_id: str
    turn: int

    # -- inputs --
    user_query: str
    customer_display_name: NotRequired[str]

    # -- customer memory --
    customer_profile: CustomerProfile
    # Read-only sibling slice: every LoB profile this SA has for the bound
    # customer. Populated only when no LoB is selected (lob_id == "default")
    # so a customer-level question can answer with knowledge from prior
    # LoB-scoped turns. Never mutated or persisted from this field.
    customer_overview: NotRequired[list[CustomerProfile]]
    pending_contradictions: list[PendingContradiction]
    profile_dirty: bool
    phase: str  # discovery | assessment | recommendation | proposal | execution
    open_questions: list[str]   # gap questions the agent is queueing for future turns
    probe_muted: bool           # set when SA explicitly asks to skip probing

    # -- routing --
    route: RouteTarget
    mcp_tools: list
    routing_from_llm: bool
    intent: NotRequired[str]    # substantive | defer | acknowledge | meta_help | meta_summary | chat
    prior_probe: NotRequired[str]  # the agent's last "**To advance:**" question (if any)
    deferred_question: NotRequired[str]  # the question the SA just deferred this turn (cleared by updater)

    # -- retrieved context --
    kb_context: str
    mcp_context: str
    # Per-chunk attribution for KB retrieval. Each entry: {index, uri, score, label}.
    # Lets downstream nodes (response generator, traces, audit) cite which docs
    # the answer drew from. Populated by kb_node; empty when no KB chunks
    # survived the score floor.
    kb_sources: NotRequired[list[dict]]

    # -- outputs --
    response_text: str
    artifacts: list[Artifact]
    # Human-readable "I can draft this once I know X" notes emitted by
    # artifact_node when an imperative intent is detected but required
    # profile inputs are missing. Surfaced to the LLM so the response
    # explicitly names the gap instead of silently omitting the artifact.
    artifact_gap_notes: NotRequired[list[str]]

    # -- control --
    interrupt_reason: NotRequired[str]
    drift_detected: NotRequired[bool]   # set by drift_guard_node (item 1.12)
    drift_message: NotRequired[str]     # SA-facing message when drift_detected

    # -- listen-mode (Iteration 4.1) --
    # The agent runs different graphs for different payload kinds: chat,
    # meeting_notes (extract + preview, no merge), meeting_merge (apply
    # confirmed subset of a previous preview).
    payload_kind: NotRequired[str]       # "chat" | "meeting_notes" | "meeting_merge"
    meeting_notes_text: NotRequired[str] # raw pasted notes/transcript
    meeting_preview: NotRequired[dict]   # structured extraction result
    meeting_confirmed_ids: NotRequired[list[str]]  # row_ids the SA ticked
    meeting_merge_result: NotRequired[dict]        # what got applied
