"""LangGraph StateGraph for the mainframe modernization agent.

Flow (per turn):

    START
      │
      ▼
    profile_loader      (load CustomerProfile for (sa_id, customer_id))
      │
      ▼
    drift_guard         (item 1.12: confirm switch if SA names a different customer)
      │
      ├── drift_detected ──► direct_node (with confirmation prompt) ──┐
      │                                                                │
      └── normal ──► router                                             │
                       │                                                │
                       ├── direct  ──► direct_node ────────────────────┤
                       ├── summary ──► summary_node ───────────────────┤
                       ├── kb      ──► kb_node ────────────────────────┤
                       ├── mcp     ──► mcp_node ───────────────────────┤
                       └── both    ──► both_kb ─┐  (parallel branches  │
                                     both_mcp ─┤   per item 1.14)      │
                                               │                       │
                                                ─► artifact_node ──────┤
                                                                       ▼
                                                              response_generator
                                                                       │
                                                                       ▼
                                                              profile_updater
                                                                       │
                                                                       ▼
                                                                      END

Invariants:
- sa_id and customer_id enter via State from ws_lambda.py and are never mutated.
- profile_updater runs LAST so persistence happens after the reply is built.
- profile_updater bails out when drift_detected — we don't merge facts that
  may belong to a different customer.
- direct_node, summary_node, and the drift confirmation path skip retrieval
  AND artifact generation.
- For route="both", `both_kb` and `both_mcp` run concurrently. LangGraph
  waits for both to complete before firing `artifact_node` (natural join
  via shared downstream node). Inside `mcp_node`, multiple tool calls
  also fan out across a thread pool. Net: latency is max(kb, slowest_mcp)
  rather than kb + sum(mcp_calls).
"""
from langgraph.graph import StateGraph, START, END

from .state import AgentState
from .nodes import (
    router_node, kb_node, mcp_node, response_generator,
    direct_node, summary_node, help_node, defer_node, acknowledge_node,
)
from .nodes_artifacts import artifact_node
from .nodes_memory import (
    profile_loader_node, profile_updater_node, drift_guard_node,
)
from .nodes_listen import meeting_notes_node, meeting_merge_node


def route_decision(state: AgentState):
    """Conditional-edge selector. Returns either a single node name (str)
    or a list of node names — LangGraph fans out on a list, running the
    branches concurrently before fanning back in at any shared downstream
    node (here: artifact_node)."""
    route = state.get("route", "both")
    if route == "direct":
        return "direct_node"
    if route == "help":
        return "help_node"
    if route == "summary":
        return "summary_node"
    if route == "defer":
        return "defer_node"
    if route == "acknowledge":
        return "acknowledge_node"
    if route == "kb":
        return "kb_node"
    if route == "mcp":
        return "mcp_node"
    # "both" — fan out KB + MCP in parallel (item 1.14)
    return ["both_kb", "both_mcp"]


def post_drift_decision(state: AgentState) -> str:
    """If drift_guard set drift_detected, route directly to direct_node so
    the SA sees the confirmation prompt with no retrieval. Otherwise
    proceed to the normal router."""
    if state.get("drift_detected"):
        return "direct_node"
    return "router_node"


def post_loader_decision(state: AgentState) -> str:
    """Choose the sub-graph based on payload kind (item 4.1).

      "meeting_notes"  → run the conversation extractor and return a
                         preview (no merge, no chat reply)
      "meeting_merge"  → apply the SA-confirmed subset of an earlier
                         preview to the profile
      anything else    → normal chat flow (drift_guard → router → ...)
    """
    kind = state.get("payload_kind") or "chat"
    if kind == "meeting_notes":
        return "meeting_notes_node"
    if kind == "meeting_merge":
        return "meeting_merge_node"
    return "drift_guard"


def build_graph():
    workflow = StateGraph(AgentState)

    # Memory
    workflow.add_node("profile_loader", profile_loader_node)
    workflow.add_node("drift_guard", drift_guard_node)
    workflow.add_node("profile_updater", profile_updater_node)

    # Core pipeline
    workflow.add_node("router_node", router_node)
    workflow.add_node("kb_node", kb_node)
    workflow.add_node("mcp_node", mcp_node)
    workflow.add_node("both_kb", kb_node)
    workflow.add_node("both_mcp", mcp_node)
    workflow.add_node("artifact_node", artifact_node)
    workflow.add_node("response_generator", response_generator)
    workflow.add_node("direct_node", direct_node)
    workflow.add_node("summary_node", summary_node)
    workflow.add_node("help_node", help_node)
    workflow.add_node("defer_node", defer_node)
    workflow.add_node("acknowledge_node", acknowledge_node)

    # Listen-mode (4.1) — separate sub-graphs that don't go through
    # router/retrieval/response_generator.
    workflow.add_node("meeting_notes_node", meeting_notes_node)
    workflow.add_node("meeting_merge_node", meeting_merge_node)

    # Wiring
    workflow.add_edge(START, "profile_loader")
    workflow.add_conditional_edges(
        "profile_loader",
        post_loader_decision,
        {
            "drift_guard": "drift_guard",
            "meeting_notes_node": "meeting_notes_node",
            "meeting_merge_node": "meeting_merge_node",
        },
    )
    # Both listen-mode nodes terminate without going through profile_updater
    # (4.1's whole point is that nothing persists until the SA confirms).
    # meeting_merge_node persists internally before returning.
    workflow.add_edge("meeting_notes_node", END)
    workflow.add_edge("meeting_merge_node", END)

    workflow.add_conditional_edges(
        "drift_guard",
        post_drift_decision,
        {
            "direct_node": "direct_node",
            "router_node": "router_node",
        },
    )

    # router_node fan-out. The "both" route returns a list
    # ["both_kb", "both_mcp"]; LangGraph treats list returns as parallel
    # fan-out and waits for all named branches before firing the next node.
    workflow.add_conditional_edges(
        "router_node",
        route_decision,
        {
            "direct_node": "direct_node",
            "help_node": "help_node",
            "summary_node": "summary_node",
            "defer_node": "defer_node",
            "acknowledge_node": "acknowledge_node",
            "kb_node": "kb_node",
            "mcp_node": "mcp_node",
            "both_kb": "both_kb",
            "both_mcp": "both_mcp",
        },
    )

    workflow.add_edge("kb_node", "artifact_node")
    workflow.add_edge("mcp_node", "artifact_node")
    # both_kb and both_mcp converge at artifact_node — natural join.
    workflow.add_edge("both_kb", "artifact_node")
    workflow.add_edge("both_mcp", "artifact_node")
    workflow.add_edge("artifact_node", "response_generator")

    workflow.add_edge("direct_node", "profile_updater")
    workflow.add_edge("summary_node", "profile_updater")
    workflow.add_edge("help_node", "profile_updater")
    workflow.add_edge("defer_node", "profile_updater")
    workflow.add_edge("acknowledge_node", "profile_updater")
    workflow.add_edge("response_generator", "profile_updater")
    workflow.add_edge("profile_updater", END)

    return workflow.compile()


import functools


@functools.lru_cache(maxsize=1)
def get_graph():
    return build_graph()
