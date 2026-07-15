"""AgentCore entrypoint — async streaming through the full LangGraph.

Single mode of operation: the entrypoint is an async generator that yields
SSE-friendly event dicts as the graph runs. Tokens stream out of any LLM
call inside any node (today: response_generator) via LangGraph's
`astream(stream_mode="messages")`.

The deployed WS Lambda is a thin SSE relay — it forwards every event we
yield here to the WebSocket client. There is no longer a `context_only`
mode, no second Bedrock call in WS Lambda, and no duplicate system prompt.

Payload shape (set by deploy/ws_lambda.py):
    {
      "prompt": "<user message>",
      "sa_id": "<from JWT sub>",                 # optional; used by profile_loader
      "customer_id": "<normalized>",             # optional; defaults to "default"
      "customer_display_name": "<raw>",          # optional
      "turn": <int>                              # optional; used by profile_updater
    }

Event shapes yielded:
    {"type": "status", "message": "..."}         # progress hint
    {"type": "token", "text": "..."}             # streamed token from response LLM
    {"type": "tool_call", "tool": "..."}         # MCP / KB activity (best-effort)
    {"type": "artifact", "kind": "...", ...}     # full artifact payload
    {"type": "done", "route": "..."}             # terminal
    {"type": "error", "message": "..."}          # terminal on failure
"""
import logging
import traceback
from typing import Any

from bedrock_agentcore import BedrockAgentCoreApp
from langchain_core.messages import HumanMessage, AIMessageChunk

from agent.graph import get_graph
from agent.state import AgentState

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload):
    """Run the full graph, streaming tokens from response_generator.

    Three payload kinds are supported (item 4.1 added the listen-mode
    branches):

      "chat" (default)  — `prompt` is the user message; full chat graph
      "meeting_notes"   — `notes_text` is pasted meeting text; runs the
                          conversation extractor and yields a structured
                          preview event (no merge, no chat reply)
      "meeting_merge"   — `preview` (full preview object) + `confirmed_ids`
                          (list of row_ids the SA ticked); applies the
                          subset to the bound profile
    """
    payload = payload or {}
    kind = (payload.get("kind") or "chat").strip().lower() or "chat"

    sa_id = payload.get("sa_id") or "anonymous"
    customer_id = payload.get("customer_id") or "default"
    customer_display_name = payload.get("customer_display_name") or ""
    lob_id = (payload.get("lob_id") or "default").strip().lower() or "default"
    lob_display_name = payload.get("lob_display_name") or ""
    try:
        turn = int(payload.get("turn") or 0)
    except (TypeError, ValueError):
        turn = 0

    # Build the state shape based on payload kind.
    if kind == "meeting_notes":
        notes_text = (payload.get("notes_text") or "").strip()
        if not notes_text:
            yield {"type": "error", "message": "No notes_text provided"}
            return
        state: AgentState = {
            "messages": [],
            "user_query": "",
            "payload_kind": "meeting_notes",
            "meeting_notes_text": notes_text,
        }
    elif kind == "meeting_merge":
        preview = payload.get("preview") or {}
        confirmed_ids = payload.get("confirmed_ids") or []
        if not preview or not isinstance(confirmed_ids, list):
            yield {"type": "error",
                   "message": "meeting_merge requires preview + confirmed_ids"}
            return
        state: AgentState = {
            "messages": [],
            "user_query": "",
            "payload_kind": "meeting_merge",
            "meeting_preview": preview,
            "meeting_confirmed_ids": confirmed_ids,
        }
    else:  # chat
        user_message = payload.get("prompt", "")
        if not user_message:
            yield {"type": "error", "message": "No prompt provided"}
            return
        state: AgentState = {
            "messages": [HumanMessage(content=user_message)],
            "user_query": user_message,
            "payload_kind": "chat",
        }

    # Identity + bookkeeping fields are set on every state regardless of kind.
    state["sa_id"] = sa_id
    state["customer_id"] = customer_id
    state["lob_id"] = lob_id
    state["lob_display_name"] = lob_display_name
    state["session_id"] = payload.get("session_id") or ""
    state["turn"] = turn
    state["route"] = "both"
    state["mcp_tools"] = []
    state["kb_context"] = ""
    state["mcp_context"] = ""
    state["artifacts"] = []
    state["pending_contradictions"] = []
    state["profile_dirty"] = False
    if customer_display_name:
        state["customer_display_name"] = customer_display_name

    graph = get_graph()

    # --- Stream the graph -----------------------------------------------------
    # stream_mode=["messages", "updates"] gives us:
    #   - messages: per-token chunks from any LLM call inside any node
    #   - updates : per-node state deltas (so we can surface route, artifacts, etc.)
    final_route = "both"
    final_artifacts: list[dict] = []
    streamed_any_token = False

    try:
        yield {"type": "status", "message": "Analyzing request..."}

        async for stream_mode, chunk in graph.astream(
            state,
            stream_mode=["messages", "updates"],
        ):
            if stream_mode == "messages":
                # chunk is (BaseMessageChunk, metadata_dict)
                msg, meta = chunk
                # Only stream tokens from the response_generator node.
                # Other nodes (router, fact-extractor) also call LLMs — we don't
                # want their internal tokens leaking to the user.
                node = (meta or {}).get("langgraph_node", "")
                if node != "response_generator":
                    continue
                if isinstance(msg, AIMessageChunk):
                    text = msg.content
                    if text:
                        streamed_any_token = True
                        yield {"type": "token", "text": _coerce_text(text)}

            elif stream_mode == "updates":
                # chunk is {node_name: {state_delta_keys: values}}
                for node_name, delta in (chunk or {}).items():
                    if not isinstance(delta, dict):
                        continue

                    if "route" in delta and delta["route"]:
                        final_route = delta["route"]
                        yield {"type": "status",
                               "message": f"Route: {final_route}"}

                    if "mcp_tools" in delta and delta["mcp_tools"]:
                        for tc in delta["mcp_tools"]:
                            yield {"type": "tool_call",
                                   "tool": tc.get("tool", "")}

                    if "artifacts" in delta and delta["artifacts"]:
                        for art in delta["artifacts"]:
                            final_artifacts.append(art)
                            yield {
                                "type": "artifact",
                                "kind": art.get("kind", ""),
                                "title": art.get("title", ""),
                                "mime_type": art.get("mime_type", ""),
                                "content": art.get("content", ""),
                            }

                    # Listen-mode (4.1) — preview object yielded by the
                    # meeting_notes_node terminates the graph; surface as
                    # a single event for the frontend to render its
                    # checkbox preview card.
                    if "meeting_preview" in delta and delta["meeting_preview"]:
                        yield {
                            "type": "meeting_preview",
                            "preview": delta["meeting_preview"],
                        }

                    # Listen-mode (4.1) — merge result yielded by
                    # meeting_merge_node. Tells the SA what got applied.
                    if "meeting_merge_result" in delta and delta["meeting_merge_result"]:
                        yield {
                            "type": "meeting_merge_result",
                            "result": delta["meeting_merge_result"],
                        }

                    # Non-LLM terminal nodes emit a non-streamed AIMessage.
                    # Deliver each as a single token block so the UI still
                    # renders it. (response_generator's tokens are streamed
                    # via the "messages" stream_mode above; these don't.)
                    NON_STREAMING_TERMINAL_NODES = {
                        "direct_node", "summary_node", "help_node",
                        "defer_node", "acknowledge_node",
                    }
                    if (
                        node_name in NON_STREAMING_TERMINAL_NODES
                        and not streamed_any_token
                        and "messages" in delta
                    ):
                        msgs = delta["messages"] or []
                        for m in msgs:
                            text = getattr(m, "content", "")
                            if text:
                                streamed_any_token = True
                                yield {"type": "token", "text": str(text)}

        yield {"type": "done", "route": final_route,
               "artifact_count": len(final_artifacts)}

    except Exception as e:
        logger.exception("graph.astream failed")
        yield {
            "type": "error",
            "message": str(e),
            "traceback": traceback.format_exc(),
        }


def _coerce_text(content: Any) -> str:
    """LLM message content can be a string OR a list of content blocks
    (Anthropic format). Coerce to a flat string for the SSE token shape."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text")
                if t:
                    out.append(t)
            elif isinstance(block, str):
                out.append(block)
        return "".join(out)
    return str(content)


if __name__ == "__main__":
    app.run()
