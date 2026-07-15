"""Local smoke tests for the mainframe modernization agent.

Two modes:
  - test_query()       : sync, full graph; prints final response and routing
  - test_stream()      : async, exercises the streaming path the deployed
                         AgentCore entrypoint takes. This is the canonical
                         test for Iteration 1.1 (Fix Streaming Bypass).

Both require AWS credentials with bedrock + bedrock-agent-runtime access
and a reachable Gateway / KB. To run without those, mock get_response_llm
and the boto3 clients in agent/nodes.py before importing.
"""
import asyncio
import sys

from langchain_core.messages import HumanMessage

from agent.graph import get_graph
from agent.state import AgentState


# ---------------------------------------------------------------------------
# Sync, end-to-end (uses graph.invoke)
# ---------------------------------------------------------------------------

def test_query(graph, query: str, sa_id: str = "tester", customer_id: str = "default"):
    print(f"\n{'=' * 60}")
    print(f"[sync] {query}")
    print("=" * 60)

    state: AgentState = {
        "messages": [HumanMessage(content=query)],
        "user_query": query,
        "sa_id": sa_id,
        "customer_id": customer_id,
        "turn": 1,
        "route": "both",
        "mcp_tools": [],
        "kb_context": "",
        "mcp_context": "",
        "artifacts": [],
        "pending_contradictions": [],
        "profile_dirty": False,
    }

    result = graph.invoke(state)
    print(f"Route          : {result.get('route')}")
    print(f"MCP tools      : {[t.get('tool') for t in result.get('mcp_tools', [])]}")
    print(f"KB context len : {len(result.get('kb_context', ''))}")
    print(f"MCP context len: {len(result.get('mcp_context', ''))}")
    print(f"Artifacts      : {[a['kind'] for a in result.get('artifacts', [])]}")
    last_msg = result["messages"][-1]
    print(f"\nResponse (truncated):\n{last_msg.content[:800]}...")


# ---------------------------------------------------------------------------
# Async streaming (mirrors what agentcore_app.py does in production)
# ---------------------------------------------------------------------------

async def test_stream(graph, query: str, sa_id: str = "tester", customer_id: str = "default"):
    print(f"\n{'=' * 60}")
    print(f"[stream] {query}")
    print("=" * 60)

    from langchain_core.messages import AIMessageChunk

    state: AgentState = {
        "messages": [HumanMessage(content=query)],
        "user_query": query,
        "sa_id": sa_id,
        "customer_id": customer_id,
        "turn": 1,
        "route": "both",
        "mcp_tools": [],
        "kb_context": "",
        "mcp_context": "",
        "artifacts": [],
        "pending_contradictions": [],
        "profile_dirty": False,
    }

    token_count = 0
    routes_seen = []
    artifacts_seen = []
    print("\nLive output:\n", end="", flush=True)
    async for stream_mode, chunk in graph.astream(
        state, stream_mode=["messages", "updates"]
    ):
        if stream_mode == "messages":
            msg, meta = chunk
            if (meta or {}).get("langgraph_node") != "response_generator":
                continue
            if isinstance(msg, AIMessageChunk) and msg.content:
                text = msg.content if isinstance(msg.content, str) else str(msg.content)
                print(text, end="", flush=True)
                token_count += 1
        elif stream_mode == "updates":
            for node, delta in (chunk or {}).items():
                if not isinstance(delta, dict):
                    continue
                if delta.get("route"):
                    routes_seen.append(delta["route"])
                if delta.get("artifacts"):
                    artifacts_seen.extend(a["kind"] for a in delta["artifacts"])

    print(f"\n\n[stream summary] tokens={token_count} routes={routes_seen} "
          f"artifacts={artifacts_seen}")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "stream"
    graph = get_graph()

    queries = [
        "How do I map CICS transactions to AWS services?",
        "What is the ROI of mainframe modernization for a mid-size bank?",
        "What SOX compliance considerations should we address during migration?",
    ]

    if mode == "sync":
        for q in queries:
            test_query(graph, q)
    else:
        for q in queries:
            asyncio.run(test_stream(graph, q))
