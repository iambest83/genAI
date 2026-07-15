"""LangGraph node implementations for the mainframe modernization agent.

5 nodes (the graph also wires kb/mcp under both_kb/both_mcp aliases):
  1. router_node        — hybrid keyword + LLM routing (via agent/router.py)
  2. kb_node            — retrieve from Bedrock Knowledge Base
  3. mcp_node           — call MCP tools via AgentCore Gateway
  4. response_generator — LLM generates final answer (uses prompts.py)
  5. direct_node        — short non-retrieval reply for greetings/meta

The system prompt is owned exclusively by `agent/prompts.py:build_response_prompt`.
There is no fallback / duplicate prompt in this module.
"""
import json
import logging

from langchain_core.messages import HumanMessage, AIMessage

from .state import AgentState
from .config import get_response_llm, get_kb_config, get_gateway_config
from .customer_profile import CustomerProfile
from .router import route_query
from .prompts import build_response_prompt

logger = logging.getLogger(__name__)

# Module-level boto3 client singletons. Reused across warm invocations to
# avoid per-call connection negotiation (~50-100ms savings per turn).
_kb_client = None
_botocore_credentials = None


def _get_kb_client():
    global _kb_client
    if _kb_client is None:
        import boto3
        from .config import get_kb_config
        config = get_kb_config()
        _kb_client = boto3.client("bedrock-agent-runtime", region_name=config["region"])
    return _kb_client


def _get_mcp_credentials():
    global _botocore_credentials
    if _botocore_credentials is None:
        from botocore.session import Session as BotocoreSession
        session = BotocoreSession()
        _botocore_credentials = session.get_credentials()
    return _botocore_credentials


def _strip_decimals(obj):
    """Recursively convert Decimal values to int or float for JSON serialization.

    DynamoDB returns numeric fields as decimal.Decimal which json.dumps cannot
    handle. This coerces them to native Python types before the profile dict is
    passed into MCP tool args.
    """
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    if isinstance(obj, dict):
        return {k: _strip_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_decimals(v) for v in obj]
    return obj


def _profile_is_empty(profile_dict: dict) -> bool:
    """True iff the profile carries no SA-stated facts/decisions/constraints.

    For a fresh `default/default` profile we don't want to inject an empty
    object into MCP args — the Lambda would treat None/0 defaults as "stated"
    and inflate confidence. Keys outside the meaningful set (sa_id, version,
    customer_id, lob_id, *_display_name, updated_at, *facts/decisions arrays)
    are bookkeeping. This tracks the shape produced by CustomerProfile.to_dict.
    """
    if not isinstance(profile_dict, dict):
        return True
    workload = profile_dict.get("workload") or {}
    constraints = profile_dict.get("constraints") or {}
    decisions = profile_dict.get("decisions_made") or []
    open_qs = profile_dict.get("open_questions") or []
    if any(v not in (None, "", [], {}, False, 0) for v in workload.values()):
        return False
    if any(v not in (None, "", [], {}, False, 0) for v in constraints.values()):
        return False
    if decisions or open_qs:
        return False
    return True


# ---------------------------------------------------------------------------
# Node 1: Router (intent-aware)
# ---------------------------------------------------------------------------
def router_node(state: AgentState) -> dict:
    """Classify intent, then route. Returns route + mcp_tools + intent.

    The `prior_probe` we pass into the router is the most recent gap
    question the agent emitted (stored in profile.open_questions, head
    of the queue). With it, a short reply like "not now" or "Cards" is
    correctly classified as defer / substantive instead of chat.
    """
    messages = state["messages"]
    user_query = messages[-1].content if messages else ""

    # Pull the most recent open question (the one Sonnet asked last turn).
    open_qs = state.get("open_questions") or []
    profile = state.get("customer_profile")
    if not open_qs and profile is not None:
        open_qs = list(profile.open_questions or [])
    prior_probe = open_qs[-1] if open_qs else ""

    route, mcp_tools, intent = route_query(user_query, prior_probe=prior_probe)
    return {
        "route": route,
        "mcp_tools": mcp_tools,
        "user_query": user_query,
        "intent": intent,
        "prior_probe": prior_probe,
    }


# ---------------------------------------------------------------------------
# Node 2: KB Node (Bedrock Knowledge Base retrieval)
# ---------------------------------------------------------------------------
def kb_node(state: AgentState) -> dict:
    """Retrieve context from Bedrock Knowledge Base.

    Two-stage retrieval when `rerank_enabled` is true (the default):
      Stage 1 (vector ANN): pull `num_results` candidates from OpenSearch
        using Titan Text v2 embeddings.
      Stage 2 (Cohere Rerank 3.5): cross-encoder reranks those candidates
        and keeps the top `rerank_num_results`. Bedrock applies this via
        `retrievalConfiguration.vectorSearchConfiguration.rerankingConfiguration`
        so it happens server-side; we just get back the reranked list.

      The `score` field on each returned chunk is the *post-rerank*
      relevance score when rerank is enabled, and the raw vector cosine
      when disabled. The two scales are different (Cohere relevant ~0.5+,
      Titan cosine ~0.6-0.8 for in-domain) — `score_floor` is tuned for
      the post-rerank scale; raise/lower via KB_SCORE_FLOOR if eval drifts.

      Set KB_RERANK_ENABLED=false to skip Stage 2 and fall back to plain
      vector retrieval (rollback path; no code change required).

    Other retrieval-side behavior (chunking config stays server-side on
    the data source):
      - Capture each chunk's source S3 URI so a bad chunk can be traced back
        to its origin doc, and so the response can attribute (e.g. footer).
      - Drop chunks whose similarity score is below `score_floor`. Off-topic
        queries against a domain-specific KB now return nothing rather than
        chunks of low-similarity noise that confuses the response LLM.
      - Log per-call: which doc each chunk came from + its score. Lets us
        post-hoc diagnose "where did this answer come from?" without
        re-running the query.
    """
    from os.path import basename

    config = get_kb_config()
    if not config["kb_id"]:
        return {"kb_context": "", "kb_sources": []}

    try:
        client = _get_kb_client()

        vector_cfg: dict = {"numberOfResults": config["num_results"]}
        if config["rerank_enabled"]:
            vector_cfg["rerankingConfiguration"] = {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {
                        "modelArn": config["rerank_model_arn"],
                    },
                    "numberOfRerankedResults": config["rerank_num_results"],
                },
            }

        response = client.retrieve(
            knowledgeBaseId=config["kb_id"],
            retrievalQuery={"text": state["user_query"]},
            retrievalConfiguration={"vectorSearchConfiguration": vector_cfg},
        )

        raw_results = response.get("retrievalResults", []) or []

        # Filter by score floor BEFORE building context. This is what makes
        # off-topic queries return empty instead of weak-match noise.
        filtered = []
        for r in raw_results:
            score = float(r.get("score", 0) or 0)
            if score < config["score_floor"]:
                continue
            filtered.append(r)

        # Per-call telemetry. NOTE: kb_node runs as a parallel LangGraph
        # branch (both_kb fan-out) and the runtime's log shipping does NOT
        # always surface logger.info from parallel branches in CloudWatch
        # (sequential nodes — profile_loader, router, profile_updater — do
        # surface). The functional telemetry — which chunks fired, their
        # scores, their source URIs — is therefore exposed in state via
        # kb_sources rather than in logs. Downstream nodes / tests / audit
        # use kb_sources for traceability.
        logger.info(
            f"kb_node: query={state['user_query'][:80]!r} "
            f"returned={len(raw_results)} kept={len(filtered)} "
            f"floor={config['score_floor']:.2f}"
        )
        for r in raw_results:
            uri = (r.get("location", {})
                    .get("s3Location", {})
                    .get("uri", "") or "(no-uri)")
            score = float(r.get("score", 0) or 0)
            kept = "KEPT" if score >= config["score_floor"] else "DROP"
            logger.info(f"  kb_node: {kept} score={score:.3f} uri={uri}")

        if not filtered:
            return {"kb_context": "", "kb_sources": []}

        context_parts: list[str] = []
        sources: list[dict] = []  # exposed in state so callers can attribute
        for i, r in enumerate(filtered, 1):
            text = r.get("content", {}).get("text", "")
            score = float(r.get("score", 0) or 0)
            uri = (r.get("location", {})
                    .get("s3Location", {})
                    .get("uri", ""))
            label = basename(uri) if uri else f"Doc {i}"
            context_parts.append(
                f"[Doc {i} — {label}] (relevance: {score:.2f})\n{text}"
            )
            sources.append({"index": i, "uri": uri, "score": round(score, 3), "label": label})

        return {
            "kb_context": "\n\n".join(context_parts),
            "kb_sources": sources,
        }

    except Exception as e:
        logger.error(f"KB retrieval error: {e}")
        return {"kb_context": "", "kb_sources": []}


# ---------------------------------------------------------------------------
# Node 3: MCP Node (call tools via AgentCore Gateway)
# ---------------------------------------------------------------------------
#
# Gateway-target inventory. A single Gateway hosts multiple targets, each
# backed by its own Lambda. The Gateway prepends `<target>___` to every
# tool name to keep namespaces clean. The router can emit tool calls
# against any of these targets; mcp_node uses TOOL_TARGET to look up the
# right prefix per tool. Calls that don't specify a target (legacy
# routing code) fall through to MainframeMCP for backwards compat.
#
# Gateway targets in account <ACCOUNT_ID> (gateway mfmodagent-gateway-<GATEWAY_ID>):
#   MainframeMCP    → type=lambda → MfModAgent-MainframeMCP (deploy/mainframe_mcp_lambda.py)
#   AWSMCP          → type=mcpServer, endpoint=https://aws-mcp.us-east-1.api.aws/mcp
#                     (AWS-managed Streamable HTTP MCP server, IAM-auth via Gateway
#                     service role; target id <AWSMCP_TARGET_ID>, registered 2026-06-30).
#                     Superset of the earlier AwsKnowledgeMCP managed endpoint.
#   WebSearchMCP    → type=mcp.connector, connectorId=web-search
#                     (AWS-managed AgentCore connector; target id <WEBSEARCH_TARGET_ID>,
#                     registered 2026-06-30. Needs bedrock-agentcore:InvokeWebSearch
#                     IAM perm on the Gateway service role).
#
# AwsDocsMCP and AwsPricingMCP (D9 Lambda code in deploy/aws_*_mcp_lambda.py) are
# DEAD-ON-ARRIVAL — AWSMCP's tool surface covers the docs scope; pricing requires
# a separate self-hosted MCP server which is deferred. Those Lambda files will be
# deleted in a future cleanup.
#
# Adding a new target = (a) register target on Gateway (lambda / mcpServer /
# mcp.connector), (b) extend TOOL_TARGET below. No mcp_node change required.
DEFAULT_MCP_TARGET = "MainframeMCP"
TOOL_TARGET = {
    # Mainframe / FSI reference (curated JSON data, deploy/mainframe_mcp_lambda.py).
    #
    # Only tools with genuine differentiated value are wired here. The
    # MainframeMCP Lambda still SERVES 9 tools, but 4 of them are
    # redundant with Sonnet's training data + AWSMCP docs search + KB
    # retrieval, and are deliberately NOT routable from the agent:
    #   - lookup_cobol_pattern (Sonnet knows COBOL syntax cold)
    #   - lookup_jcl_reference (same, JCL)
    #   - map_mainframe_to_aws (AWSMCP + KB cover component mapping)
    #   - compare_services (AWSMCP docs search covers this better)
    # Deprecation is REVERSIBLE — tools remain in the Lambda, just
    # unrouted at the agent layer. Re-adding an entry restores routing.
    # See SESSION_STATE.md "Action 3 deprecation" for full rationale.
    "get_migration_pattern":    "MainframeMCP",
    "estimate_complexity":      "MainframeMCP",
    "get_fsi_compliance_check": "MainframeMCP",
    "compare_partner_tools":    "MainframeMCP",
    "list_taxonomy":            "MainframeMCP",
    # AWS managed Streamable HTTP MCP server (AWSMCP). Tool names carry
    # upstream `aws___` prefix; Gateway prepends `AWSMCP___`, so the wire
    # name is e.g. `AWSMCP___aws___search_documentation`. Keep upstream
    # names as TOOL_TARGET keys — mcp_node + router refer to them.
    # Only the 5 tools that (a) work in our env and (b) are relevant to an
    # SA advisory agent are wired. `call_aws`, `run_script`,
    # `get_presigned_url`, `get_tasks` need workload-identity federation
    # we haven't set up AND are out-of-scope for advisory use. `recommend`
    # is upstream-broken in our env. Live-verified 2026-07-07.
    "aws___search_documentation":      "AWSMCP",
    "aws___read_documentation":        "AWSMCP",
    "aws___list_regions":              "AWSMCP",
    "aws___get_regional_availability": "AWSMCP",
    "aws___retrieve_skill":            "AWSMCP",
    # AWS managed web-search connector. Wire name: WebSearchMCP___WebSearch.
    "WebSearch":                       "WebSearchMCP",
    # AwsPricingMCP — awslabs.aws-pricing-mcp-server v1.0.31 hosted as its
    # own AgentCore Runtime (AwsPricingMcp-<PRICING_RUNTIME_ID>). Registered as
    # Gateway mcpServer target `AwsPricingMCP` (id <PRICING_TARGET_ID>) on
    # 2026-07-07. Tools are the upstream awslabs pricing surface.
    "get_pricing":                     "AwsPricingMCP",
    "get_pricing_service_codes":       "AwsPricingMCP",
    "get_pricing_service_attributes":  "AwsPricingMCP",
    "get_pricing_attribute_values":    "AwsPricingMCP",
    "get_price_list_urls":             "AwsPricingMCP",
    "generate_cost_report":            "AwsPricingMCP",
    "get_bedrock_patterns":            "AwsPricingMCP",
    # `analyze_cdk_project` and `analyze_terraform_project` are IaC analyzers
    # that expect a project path — not relevant to an SA-conversation agent.
    # They're deliberately NOT wired here.
}


def _call_mcp_tool(call: dict, gateway_url: str, region: str, credentials) -> str:
    """Single MCP tool invocation. Pure function — safe to fan out across
    a thread pool. Returns a labeled prose+JSON block (or an error label).

    Resolves the Gateway target from `call["target"]` if present, otherwise
    looks up TOOL_TARGET[tool], otherwise falls back to DEFAULT_MCP_TARGET.
    This keeps legacy routing emissions (no target field) working unchanged
    while letting new tools live on separate Lambdas.
    """
    import urllib.request
    import urllib.error
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    tool_name = call.get("tool", "")
    args = call.get("args", {})
    target = (
        call.get("target")
        or TOOL_TARGET.get(tool_name)
        or DEFAULT_MCP_TARGET
    )
    gateway_tool_name = f"{target}___{tool_name}"

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": "1",
        "method": "tools/call",
        "params": {"name": gateway_tool_name, "arguments": args},
    }).encode("utf-8")

    request = AWSRequest(
        method="POST", url=gateway_url,
        data=payload, headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(request)

    req = urllib.request.Request(gateway_url, data=payload, method="POST")
    for k, v in dict(request.headers).items():
        req.add_header(k, v)

    max_attempts = 2
    last_err = None
    for attempt in range(max_attempts):
        try:
            # timeout=30 on first attempt; shorter on retry to bound total wait.
            timeout = 30 if attempt == 0 else 10
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))

            # Surface JSON-RPC errors. When the Lambda raises ToolInputError or the
            # Gateway itself returns an error envelope, `result["error"]` is set
            # and `result["result"]` is absent.
            if isinstance(result, dict) and result.get("error"):
                err = result["error"]
                if isinstance(err, dict):
                    msg = err.get("message") or err.get("data") or json.dumps(err)
                else:
                    msg = str(err)
                return f"[{tool_name}] Error: {msg}"

            # Some MCP servers also return a successful envelope with isError=True
            # inside `result.content` — surface that the same way so downstream
            # sees a consistent error shape.
            rpc_result = result.get("result", {}) if isinstance(result, dict) else {}
            if rpc_result.get("isError"):
                content = rpc_result.get("content", [])
                err_text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                return f"[{tool_name}] Error: {err_text or 'tool reported isError'}"

            content = rpc_result.get("content", [])
            texts = [c.get("text", "") for c in content if c.get("type") == "text"]
            return f"[{tool_name}]\n" + "\n".join(texts)
        except urllib.error.HTTPError as e:
            if e.code >= 500 and attempt < max_attempts - 1:
                logger.warning(f"Gateway call {tool_name} got {e.code}, retrying")
                last_err = e
                continue
            logger.error(f"Gateway call {tool_name} failed: {e}")
            return f"[{tool_name}] Error: {str(e)}"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < max_attempts - 1:
                logger.warning(f"Gateway call {tool_name} transient error, retrying: {e}")
                last_err = e
                continue
            logger.error(f"Gateway call {tool_name} failed: {e}")
            return f"[{tool_name}] Error: {str(e)}"
        except Exception as e:
            logger.error(f"Gateway call {tool_name} failed: {e}")
            return f"[{tool_name}] Error: {str(e)}"
    return f"[{tool_name}] Error: {str(last_err)}"


def mcp_node(state: AgentState) -> dict:
    """Call MCP tools via the AgentCore Gateway, in parallel (item 1.14).

    Multiple tool calls fan out across a small thread pool so latency is
    determined by the slowest call rather than the sum. Order of results
    is preserved to match `state["mcp_tools"]` so traces stay readable.
    """
    from concurrent.futures import ThreadPoolExecutor

    config = get_gateway_config()
    mcp_tools = state.get("mcp_tools", [])

    if not mcp_tools:
        return {"mcp_context": ""}

    # Inject the bound customer profile into args for profile-aware tools.
    # Without this, Iteration 1.6 features (workload seeding for
    # estimate_complexity, source-systems / constraints for compare_*) stay
    # inert because the router never had reason to populate them. The Lambda
    # ignores `profile` for tools that don't accept it, so this is safe to
    # always emit. Per FIXES.md #10/#11.
    profile = state.get("customer_profile")
    if profile is not None and hasattr(profile, "to_dict"):
        try:
            profile_dict = _strip_decimals(profile.to_dict())
        except Exception as e:
            logger.warning(f"mcp_node: profile.to_dict failed: {e}")
            profile_dict = None
        if profile_dict and not _profile_is_empty(profile_dict):
            PROFILE_AWARE_TOOLS = {
                "estimate_complexity",
                "compare_partner_tools",
                "compare_services",
                "get_fsi_compliance_check",
                "analyze_phase_gaps",
            }
            for call in mcp_tools:
                if call.get("tool") in PROFILE_AWARE_TOOLS:
                    args = call.setdefault("args", {})
                    args.setdefault("profile", profile_dict)

    credentials = _get_mcp_credentials()

    # Cap concurrency at 8 to avoid hammering the Gateway in pathological cases.
    max_workers = min(8, max(1, len(mcp_tools)))
    results: list[str] = [""] * len(mcp_tools)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_idx = {
            ex.submit(_call_mcp_tool, call, config["gateway_url"],
                      config["region"], credentials): i
            for i, call in enumerate(mcp_tools)
        }
        for fut in future_to_idx:
            idx = future_to_idx[fut]
            try:
                results[idx] = fut.result()
            except Exception as e:
                tool_name = mcp_tools[idx].get("tool", "?")
                logger.error(f"mcp_node: future for {tool_name} raised: {e}")
                results[idx] = f"[{tool_name}] Error: {str(e)}"

    return {"mcp_context": "\n\n".join(r for r in results if r) or ""}


# ---------------------------------------------------------------------------
# Node 4: Response Generator
# ---------------------------------------------------------------------------
def response_generator(state: AgentState) -> dict:
    """Generate the final response grounded in customer profile + retrieved context.

    Profile is expected to be present (set by profile_loader_node, which runs
    first in the graph). For tests / local dev that bypass the loader, fall
    back to an empty in-memory profile so the prompt path stays singular.
    """
    llm = get_response_llm()

    profile = state.get("customer_profile") or CustomerProfile(
        sa_id=state.get("sa_id", "anonymous"),
        customer_id=state.get("customer_id", "default"),
        customer_display_name=state.get("customer_display_name", ""),
    )

    prompt = build_response_prompt(
        query=state.get("user_query", ""),
        profile=profile,
        kb_context=state.get("kb_context", "") or "",
        mcp_context=state.get("mcp_context", "") or "",
        pending_contradictions=state.get("pending_contradictions", []) or [],
        artifact_titles=[a["title"] for a in state.get("artifacts", []) or []],
        phase=state.get("phase", profile.derive_phase()),
        route=state.get("route", "both"),
        probe_muted=bool(state.get("probe_muted", False)),
        open_questions=state.get("open_questions", []) or [],
        customer_overview=state.get("customer_overview") or [],
        artifact_gap_notes=state.get("artifact_gap_notes", []) or [],
    )

    response = llm.invoke([HumanMessage(content=prompt)])
    text = getattr(response, "content", str(response))
    return {"messages": [AIMessage(content=text)], "response_text": text}


def direct_node(state: AgentState) -> dict:
    """Bare-greeting reply.

    Two paths:
    1. drift_guard_node detected a customer switch — return the
       confirmation prompt (verbatim, with Yes/No semantics).
    2. SA said "hi" / "hello" / etc. — return a terse, bound-aware
       one-liner. Does NOT reproduce the intro deck (that's `help_node`'s
       job; this used to leak the intro on every greeting).
    """
    if state.get("drift_detected") and state.get("drift_message"):
        msg = state["drift_message"]
        return {"messages": [AIMessage(content=msg)], "response_text": msg}

    # Bound-aware greeting reply — references customer/LoB if set.
    profile = state.get("customer_profile")
    if profile and profile.customer_display_name:
        # Use _customer_label() for the "JPMC / Cards" rendering.
        target = profile._customer_label()
        msg = f"Hi — what would you like to dig into for **{target}**?"
    else:
        msg = "Hi — what would you like to dig into?"
    return {"messages": [AIMessage(content=msg)], "response_text": msg}


def help_node(state: AgentState) -> dict:
    """Explicit "what can you do" / "help" / "/help" — full intro deck.

    This is the only node that reproduces the introductory description.
    Fires only on intent=meta_help, not on bare greetings.
    """
    profile = state.get("customer_profile")
    bound_line = ""
    if profile and profile.customer_display_name:
        target = profile._customer_label()
        bound_line = f"\n\nYou're currently working on **{target}**."

    msg = (
        "**Mainframe Modernization Sherpa** — your AWS Solutions Architect "
        "co-pilot for Financial Services mainframe modernization."
        + bound_line
        + "\n\nWhat I can help with:\n"
        "- **Assessment** — workload sizing, complexity scoring, pattern selection\n"
        "- **Migration patterns** — rehost / replatform / refactor / retire / automated_refactor\n"
        "- **Partner & tool comparison** — Micro Focus, Blu Age, Heirloom, TCS, Deloitte, etc.\n"
        "- **AWS service mappings** — CICS, DB2, VSAM, IMS, MQ, RACF → AWS targets\n"
        "- **FSI compliance** — SOX, PCI-DSS, FDIC, OCC, GLBA, FFIEC, DORA implications\n"
        "- **Deliverables** — say *\"draft a wave plan\"* / *\"create an architecture diagram\"* "
        "/ *\"build a risk register\"* to get paste-ready artifacts\n\n"
        "**Useful commands:**\n"
        "- *\"what do you know\"* / *\"where are we\"* — see the customer profile snapshot\n"
        "- *\"skip\"* / *\"not now\"* — defer the agent's last clarifying question\n"
        "- Set the **Customer** and **LoB** chips at the top right to scope the conversation"
    )
    return {"messages": [AIMessage(content=msg)], "response_text": msg}


def defer_node(state: AgentState) -> dict:
    """SA chose to skip the agent's prior probe.

    Behavior:
      - Acknowledge the deferral in one short line, naming the question
        that's being dropped (so the SA can see what changed).
      - Stash the deferred question in state so profile_updater_node
        can clear it from open_questions on this turn.
      - No retrieval, no probe, no intro.
    """
    open_qs = list(state.get("open_questions") or [])
    profile = state.get("customer_profile")
    if not open_qs and profile is not None:
        open_qs = list(profile.open_questions or [])

    deferred = open_qs[-1] if open_qs else ""
    if deferred:
        # Truncate the deferred question for display
        snippet = deferred if len(deferred) <= 120 else deferred[:117] + "..."
        msg = (
            f"Got it — I'll set that aside.\n\n"
            f"_Dropped: {snippet}_\n\n"
            f"What would you like to focus on instead?"
        )
    else:
        msg = "Got it — moving on. What would you like to focus on?"

    return {
        "messages": [AIMessage(content=msg)],
        "response_text": msg,
        "deferred_question": deferred,
    }


def acknowledge_node(state: AgentState) -> dict:
    """SA acknowledged without new content. Brief reply, no probe.

    No retrieval, no probe, no intro. profile_updater still runs to
    capture any incidentally extractable facts (rare with acks).
    """
    msg = "Acknowledged."
    return {"messages": [AIMessage(content=msg)], "response_text": msg}


def summary_node(state: AgentState) -> dict:
    """Emit the current customer profile as a structured summary.

    Triggered by SUMMARY_TRIGGERS in the router (e.g. "what do you know",
    "where are we", "recap"). Deterministic — calls the appropriate
    render method directly, no LLM. Fast (~10ms), cheap ($0), exact.

    Scoping rule:
      - LoB bound  → render_for_summary() of the bound profile slice.
      - LoB unbound but customer bound → render_customer_wide_summary()
        which concatenates every non-default LoB profile this SA has for
        the customer. Pure facts, no recommendation, no probe.
      - No customer bound → friendly hint to pick one.

    Item 1.11. Builds SA trust in the memory system.
    """
    profile = state.get("customer_profile")
    if profile is None:
        msg = (
            "I don't have a customer profile loaded for this session yet. "
            "Pick a Customer at the top and I'll show you what I know."
        )
        return {"messages": [AIMessage(content=msg)], "response_text": msg}

    cust_lc = (profile.customer_id or "").strip().lower()
    if not cust_lc or cust_lc == "default":
        msg = (
            "No customer is bound to this session yet. Pick a Customer at "
            "the top and I'll show you what's been captured for them."
        )
        return {"messages": [AIMessage(content=msg)], "response_text": msg}

    lob_lc = (profile.lob_id or "").strip().lower()
    if not lob_lc or lob_lc == "default":
        # Customer-wide view: aggregate every LoB profile we have
        overview = state.get("customer_overview") or []
        msg = profile.render_customer_wide_summary(overview)
    else:
        msg = profile.render_for_summary()

    return {"messages": [AIMessage(content=msg)], "response_text": msg}
