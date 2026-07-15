"""Hybrid router: keyword fast-path + Haiku LLM fallback.

For queries with clear mainframe/MCP keywords, routes instantly (0ms).
For ambiguous queries, calls Haiku to classify intent and select tools (~500ms).

Routing principle (LOCKED — see REFINEMENTS.md "Locked Decisions")
-----------------------------------------------------------------
The Knowledge Base is consulted on every substantive turn. The ONLY
exceptions are:
  - greetings / chat                    → route="direct"  (no retrieval)
  - short pure syntax-reference lookups → route="mcp"     (no KB)

Every other query (architecture, migration strategy, partner choice,
target service comparisons, compliance, sizing, customer-specific advice)
routes to "both" — KB + MCP run in parallel and the response generator
weaves them together.

Why this matters: the KB has 24 curated FSI / mainframe-modernization
documents (CoE materials, summit decks, partner research). Skipping it on
substantive queries dilutes the agent's value. Skipping it on pure
syntax lookups (e.g. "what is COBOL PIC S9 syntax") avoids irrelevant
retrieval results that distract the response.

If you change this rule, also update:
  - LLM_ROUTING_PROMPT below (must encode the same principle)
  - REFINEMENTS.md "Locked Decisions" section
  - Appendix A in REFINEMENTS.md if the prompt body is affected
"""
import json
import logging
import re

from .config import get_router_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP tool definitions (used by both keyword and LLM paths)
# ---------------------------------------------------------------------------
MCP_TOOLS = {
    # MainframeMCP: 5 tools with genuine differentiated value are wired.
    # 4 tools (lookup_cobol_pattern, lookup_jcl_reference,
    # map_mainframe_to_aws, compare_services) are DEPRECATED at the agent
    # layer because Sonnet + AWSMCP + KB retrieval cover the same ground
    # more accurately. Tools remain in the Lambda for reversibility.
    "get_migration_pattern": {
        "description": "Migration pattern details (reimagine, rehost, replatform, refactor, retire, automated_refactor)",
        "categories": [],
    },
    "estimate_complexity": {
        "description": "Estimate modernization complexity from workload characteristics",
        "categories": [],
    },
    "get_fsi_compliance_check": {
        "description": "FSI regulatory compliance requirements (SOX, PCI_DSS, FDIC, OCC, GLBA, FFIEC, FINRA_17a-4)",
        "categories": [],
    },
    "compare_partner_tools": {
        "description": "Rank mainframe modernization partner tools (Rocket, AWS Transform, Astadia, Heirloom, TCS, Kyndryl, DXC, etc.) for a workload",
        "categories": [],
    },
    "list_taxonomy": {
        "description": "Meta tool: discover valid enum values for any other tool's args",
        "categories": [],
    },
    # ---------------------------------------------------------------------
    # AWSMCP — managed Streamable HTTP MCP server at
    # https://aws-mcp.us-east-1.api.aws/mcp. Registered as Gateway
    # mcpServer target AWSMCP (2026-06-30). Tool names carry upstream
    # `aws___` prefix. Opt-in — router only emits when query clearly fits,
    # and prefers KB first. See ARCHITECTURE.md §8.4 / D11.
    # NOTE: aws___call_aws + aws___run_script are powerful (arbitrary AWS
    # API / script execution). Router LLM prompt instructs Sonnet to avoid
    # them by default; safety is also enforced by Gateway service role IAM.
    # ---------------------------------------------------------------------
    "aws___search_documentation": {
        "description": "Search docs.aws.amazon.com for AWS product/service documentation",
        "categories": [],
    },
    "aws___read_documentation": {
        "description": "Read a full AWS documentation page (use after search to fetch hits)",
        "categories": [],
    },
    "aws___list_regions": {
        "description": "List all AWS regions with their codes and long names",
        "categories": [],
    },
    "aws___get_regional_availability": {
        "description": "Check whether a specific AWS product/API/CFN resource is available in a region",
        "categories": [],
    },
    "aws___retrieve_skill": {
        "description": "Retrieve an AWS Agent Skill (domain workflow). Only after aws___search_documentation surfaces a skill_name.",
        "categories": [],
    },
    # ---------------------------------------------------------------------
    # WebSearchMCP — managed AgentCore web-search connector. One tool:
    # WebSearch(query, maxResults?). Registered 2026-06-30 (target id
    # <WEBSEARCH_TARGET_ID>). Use sparingly — adds latency and dilutes KB signal.
    # ---------------------------------------------------------------------
    "WebSearch": {
        "description": "Live web search via AWS-managed AgentCore web-search connector",
        "categories": [],
    },
    # ---------------------------------------------------------------------
    # AwsPricingMCP — awslabs pricing server hosted as our own AgentCore
    # Runtime (deploy/pricing_mcp_runtime). Registered 2026-07-07.
    # ---------------------------------------------------------------------
    "get_pricing": {
        "description": "Live AWS on-demand pricing lookup (service_code, region, filters)",
        "categories": [],
    },
    "get_pricing_service_codes": {
        "description": "List all AWS service codes usable in get_pricing",
        "categories": [],
    },
    "get_pricing_service_attributes": {
        "description": "List filterable attributes for a service before get_pricing",
        "categories": [],
    },
    "get_pricing_attribute_values": {
        "description": "List valid values for a specific attribute of a service",
        "categories": [],
    },
    "get_price_list_urls": {
        "description": "Get bulk price-list JSON URLs for offline TCO analysis",
        "categories": [],
    },
    "generate_cost_report": {
        "description": "Generate a structured cost report for a set of AWS services",
        "categories": [],
    },
    "get_bedrock_patterns": {
        "description": "Get common Bedrock architecture patterns with pricing",
        "categories": [],
    },
}

# ---------------------------------------------------------------------------
# Keyword fast-path
# ---------------------------------------------------------------------------
KEYWORD_RULES = [
    # NOTE: keyword rules for `lookup_cobol_pattern`, `lookup_jcl_reference`,
    # and `map_mainframe_to_aws` are REMOVED. Rationale: Sonnet's training
    # data + KB retrieval + AWSMCP docs search cover these three tools'
    # ground more accurately than the curated JSON. Removing the routing
    # means queries about COBOL syntax / JCL parameters / CICS-to-AWS
    # component mapping go to KB + Sonnet directly, not to stale JSON.
    # The tools remain in the MainframeMCP Lambda (reversible; re-adding
    # a keyword rule + TOOL_TARGET entry re-enables routing).
    {
        "keywords": ["migration", "rehost", "replatform", "refactor", "reimagine", "pattern", "moderniz"],
        "tool": "get_migration_pattern",
        "category_map": {},
        "default_category": None,
    },
    {
        "keywords": [
            "sox", "pci", "fdic", "occ", "glba", "ffiec", "finra",
            "compliance", "regulatory", "audit",
        ],
        "tool": "get_fsi_compliance_check",
        "category_map": {},
        "default_category": None,
    },
    {
        "keywords": [
            "micro focus", "blu age", "heirloom", "astadia", "unikix", "lzlabs",
            "kyndryl", "dxc", "tcs", "precisely", "partner", "vendor",
            "which tool", "compare tools", "tool comparison",
        ],
        "tool": "compare_partner_tools",
        "category_map": {},
        "default_category": None,
    },
    # compare_services is intentionally LLM-path-only. The keyword fast-path
    # cannot extract a `source` arg from arbitrary phrasing, and the Lambda
    # requires `source` to be one of the canonical keys (cics_online, db2_zos,
    # vsam_ksds, ims_db, jcl_batch, mq_search, mq_series). Routing without a
    # `source` would always return invalid_choice. Per FIXES.md #10.
    # ---------------------------------------------------------------------
    # AWSMCP keyword fast-paths — narrow rules for regions/availability;
    # everything else (docs search/read, recommend, retrieve_skill,
    # call_aws) routes via the LLM router. Paraphrased questions are
    # hard to keyword-match for those. See ARCHITECTURE.md §8.4 / D11.
    # ---------------------------------------------------------------------
    {
        "keywords": [
            "list aws regions", "list of regions", "all aws regions",
            "available regions",
        ],
        "tool": "aws___list_regions",
        "category_map": {},
        "default_category": None,
    },
    # WebSearchMCP keyword rule. Fires ADDITIVELY on both explicit
    # web-search phrasing AND implicit recency/current-events signals.
    # `query` is auto-populated from the full user query at line ~336.
    # Broadened 2026-07-07 to cover more natural SA phrasings — SAs
    # rarely type "search the web," they type "any news on X" or
    # "latest Blu Age features." Latency cost is bounded because
    # MCP calls run in parallel (ThreadPoolExecutor); adding WebSearch
    # to an already-firing turn adds ~0s to end-to-end latency.
    #
    # Note: `aws___get_regional_availability` is intentionally
    # LLM-router-only because its args (regions + resource_type +
    # filters) require structuring the keyword path can't do.
    {
        "keywords": [
            # Explicit web-search phrasing — broadened 2026-07-08 after SA
            # queries like "check the web for reimagine patterns" fell
            # through because the earlier list only had "search the web".
            # Any verb-of-lookup + "web" or "online" or "internet" should
            # fire this rule.
            "search the web", "search web", "web search", "search internet",
            "check the web", "check web", "on the web",
            "browse the web", "browse for", "look up on the web",
            "look up online", "find online", "search online",
            "find on the web", "find on web", "look on the web",
            "fact check", "fact-check",
            "latest online",
            # Implicit recency signals — SAs asking about current/recent
            # state should get live web content on top of KB.
            "latest", "recent news", "recent announcement", "recently launched",
            "recently announced", "just launched", "just released",
            "what's new", "whats new", "any news",
            "up to date", "as of today", "this year", "this quarter",
            "current state", "currently available", "current status",
            # Year mentions require an adjacent recency signal to avoid false
            # positives on decision dates ("they decided in 2025 to use X").
            "latest 2026", "latest 2025", "news 2026", "news 2025",
            "as of 2026", "as of 2025", "new in 2026", "new in 2025",
            "announced 2026", "announced 2025", "launched 2026", "launched 2025",
        ],
        "tool": "WebSearch",
        "category_map": {},
        "default_category": None,
    },
    # AWSMCP docs-search keyword rule. Fires ADDITIVELY when the SA
    # explicitly references AWS documentation. Skips the LLM router
    # step. `search_phrase` is auto-populated from the full query.
    {
        "keywords": [
            "aws documentation", "aws docs", "official docs",
            "documented in aws", "aws reference",
            "in the aws documentation", "per the aws docs",
            "check aws docs",
        ],
        "tool": "aws___search_documentation",
        "category_map": {},
        "default_category": None,
    },
    # AwsPricingMCP keyword rule. Fires on explicit pricing/cost signals.
    # Special: when the query mentions a specific instance type or service
    # + region, _infer_pricing_args (at line ~365) emits `get_pricing`
    # directly with structured args. Otherwise falls back to
    # `get_pricing_service_codes` so Sonnet can chain the follow-up call.
    #
    # This is the pricing "tool ladder": deterministic args when possible,
    # discovery call otherwise. Rationale: previously the response LLM
    # would hallucinate rates from training data because we only fired
    # `get_pricing_service_codes` (which returns just the enum of service
    # names, not any actual prices). Firing `get_pricing` directly for
    # recognizable patterns gives Sonnet ground-truth pricing to cite.
    {
        "keywords": [
            "pricing for", "price for", "how much is", "how much does",
            "cost per hour", "cost per month", "monthly cost",
            "on-demand price", "hourly rate", "tco", "$/hr",
            "cost estimate", "estimate cost", "estimate pricing",
            "cost of an", "cost of a", "price of an", "price of a",
        ],
        "tool": "get_pricing_service_codes",  # fallback; may be swapped for get_pricing
        "category_map": {},
        "default_category": None,
        "pricing_infer": True,  # marker: attempt _infer_pricing_args below
    },
]

KB_KEYWORDS = [
    "best practice", "case study", "whitepaper", "architecture", "guide",
    "documentation", "framework", "methodology", "togaf", "zachman", "blueprint",
    "governance", "risk management", "hybrid", "domain",
]

DIRECT_KEYWORDS = [
    "hello", "hi", "hey", "what can you do", "help", "who are you", "thanks",
]

# Trigger phrases for the "what does the agent know" capability (item 1.11).
# Match anywhere in the query (not just prefix) — these are usually whole
# sentences from the SA asking for a recap.
SUMMARY_TRIGGERS = [
    "what do you know",
    "what does this profile",
    "what's in the profile",
    "show the profile",
    "show me the profile",
    "summarize what",
    "summarize the profile",
    "summary of what",
    "where are we",
    "recap",
    "what have we discussed",
    "what have we covered",
    "what we know so far",
]

# Pure-reference syntax tools. A query that matches ONLY these tools and is
# short + free of advisory language is treated as a syntax lookup (skip KB).
# Since lookup_cobol_pattern and lookup_jcl_reference are deprecated at the
# agent layer (Action 3, 2026-07-08), this set is empty — no tool remains
# in MainframeMCP that qualifies as a "pure reference lookup" that should
# skip KB. All remaining wired tools produce substantive responses that
# benefit from KB grounding. Left as an empty set so `_is_pure_reference_lookup`
# always returns False without needing structural changes to the router.
PURE_LOOKUP_TOOLS: set[str] = set()

# If any of these appear, the query is substantive and the KB must be used.
SUBSTANTIVE_WORDS = [
    "how", "should", "strategy", "approach", "for our", "customer",
    "plan", "recommend", "best", "case", "why", "when to", "trade",
    "compare", "decide", "guidance", "design", "architect",
]


def _is_pure_reference_lookup(query: str, mcp_tools: list[dict]) -> bool:
    """True iff the query is a short, syntax-only reference lookup.

    Routing principle: KB is consulted on every substantive turn. The only
    queries that skip KB are greetings (handled separately) and pure syntax
    lookups like "what is the COBOL PIC clause syntax".
    """
    if not mcp_tools:
        return False
    if not all(t["tool"] in PURE_LOOKUP_TOOLS for t in mcp_tools):
        return False
    if len(query.split()) > 10:
        return False
    if any(w in query for w in SUBSTANTIVE_WORDS):
        return False
    return True


# Pricing arg inference lives in its own module for independent testability.
from .pricing_inference import infer_pricing_args as _infer_pricing_args


def _keyword_route(query: str) -> tuple[str, list[dict], bool]:
    """Keyword-based routing. Returns (route, mcp_tools, is_confident).

    is_confident=True means keywords matched clearly; no need for LLM.
    is_confident=False means we fell through to the default; LLM should refine.
    """
    q = query.lower().strip()

    # Greetings — only fire when the WHOLE message is just a greeting,
    # OR a greeting followed by punctuation. Prevents collisions like
    # "hi, the LoB is Cards" being misclassified as a chat greeting.
    # The agent has work to do on those — they should route to "both".
    for k in DIRECT_KEYWORDS:
        # Match: "hi" / "hi." / "hi!" / "hi there" (single short greeting only)
        if q == k:
            return "direct", [], True
        if q.startswith(k):
            tail = q[len(k):].lstrip()
            # Bare punctuation or empty → still a greeting
            if not tail or tail in {".", "!", "?", ",", "..."}:
                return "direct", [], True
            # "thanks!" / "hi there" / "hello!" — still greetings if very short
            if len(q.split()) <= 2 and tail.rstrip(".!?,") in {"there", "all", ""}:
                return "direct", [], True

    # Profile summary triggers — surface what the agent knows about the
    # customer (item 1.11). Always confident; deterministic short-circuit.
    if any(t in q for t in SUMMARY_TRIGGERS):
        return "summary", [], True

    # Scan for MCP tool matches
    mcp_tools = []
    for rule in KEYWORD_RULES:
        if any(k in q for k in rule["keywords"]):
            args = {}
            if rule["default_category"]:
                cat = rule["default_category"]
                for cat_name, cat_keywords in rule["category_map"].items():
                    if any(k in q for k in cat_keywords):
                        cat = cat_name
                        break
                args["category"] = cat

            # Special handling for compliance regulation. List mirrors the
            # data keys in mcp_server/data/fsi_compliance.json so any reg the
            # Lambda can serve is matchable on the keyword fast-path.
            if rule["tool"] == "get_fsi_compliance_check":
                reg = ""
                for r in ["SOX", "PCI_DSS", "FDIC", "OCC", "GLBA", "FFIEC", "FINRA_17a-4"]:
                    if r.lower().replace("_", "").replace("-", "") in q.replace("-", "").replace(" ", ""):
                        reg = r
                        break
                args["regulation"] = reg

            # WebSearch: pass the full user query verbatim. The connector
            # accepts natural language; no need to LLM-shape.
            if rule["tool"] == "WebSearch":
                args["query"] = query
                args["maxResults"] = 5

            # aws___search_documentation: pass the full user query as
            # search_phrase. `topics` and `limit` are optional — server
            # defaults to `general` topic with limit=5, which is fine.
            if rule["tool"] == "aws___search_documentation":
                args["search_phrase"] = query
                args["limit"] = 5

            # get_migration_pattern: when the query names a specific
            # pattern, set the `name` arg so the tool returns that
            # pattern's structured entry instead of a general summary.
            # Names must match keys in mcp_server/data/migration_patterns.json.
            if rule["tool"] == "get_migration_pattern":
                for name in ("reimagine", "automated_refactor", "rehost",
                             "replatform", "refactor", "retire"):
                    # `automated_refactor` matches "automated refactor" too
                    needle = name.replace("_", " ")
                    if needle in q or name in q:
                        args["name"] = name
                        break

            # Pricing tool ladder. When the rule is marked `pricing_infer`
            # and _infer_pricing_args can extract a service/region/instance
            # combo, swap the tool from get_pricing_service_codes (discovery)
            # to get_pricing (real prices). Response LLM then cites real
            # numbers instead of hallucinating from training data.
            tool_name = rule["tool"]
            if rule.get("pricing_infer"):
                inferred = _infer_pricing_args(query)
                if inferred is not None:
                    tool_name = "get_pricing"
                    args = inferred

            mcp_tools.append({"tool": tool_name, "args": args})

    has_mcp = len(mcp_tools) > 0

    if has_mcp:
        # Pure syntax lookups skip KB; every other MCP-matching query gets KB too.
        if _is_pure_reference_lookup(q, mcp_tools):
            return "mcp", mcp_tools, True
        return "both", mcp_tools, True

    # No MCP match — substantive queries always go to KB. Whether keyword
    # confidence is true or false is signaled to the caller; the LLM router
    # only refines when nothing recognizable matched.
    has_kb = any(k in q for k in KB_KEYWORDS)
    if has_kb:
        return "kb", [], True
    return "kb", [], False


# ---------------------------------------------------------------------------
# LLM router (Haiku) — called only for ambiguous queries
# ---------------------------------------------------------------------------
LLM_ROUTING_PROMPT = """You are a routing classifier for a mainframe modernization assistant.

Given the user query, decide which data sources and tools to use.

ROUTING PRINCIPLE: The knowledge base contains AWS documentation, FSI case
studies, architectural blueprints, and CoE materials. Consult it on every
substantive question. The ONLY times to skip the KB are:
  (a) greetings / chat → "direct"
  (b) pure syntax-reference lookups (e.g. "what is COBOL PIC S9 COMP-3 syntax",
      "JCL DD statement parameters") that need a dictionary answer with no
      architectural, advisory, or customer-context dimension → "mcp"
For anything else — architecture, migration strategy, partner choice, target
service comparisons, compliance, sizing, customer-specific advice — prefer
"both" so the response is grounded in both reference data and KB context.

Available routes:
- "direct" — greetings, general chat, no data lookup needed
- "kb"     — substantive question with no MCP reference need
- "mcp"    — short pure syntax/reference lookup only
- "both"   — DEFAULT for substantive questions that touch any MCP tool topic

Available MCP tools (only include if route is "mcp" or "both"). Each line shows
the args you may emit. Skip args you don't have a value for — do NOT invent
values. The Lambda treats absent inputs as low-confidence missing rather than
errors, so a sizing query with no exact counts is fine.

- get_migration_pattern(name?) — name ∈ {{reimagine, rehost, replatform, refactor, retire, automated_refactor}}; empty returns all. `reimagine` is the AWS Transform + Claude Code pattern (reverse-engineer + forward-engineer as microservices) — distinct from `refactor` (deterministic COBOL→Java preserving monolith).
- estimate_complexity(num_cobol_programs?, num_jcl_jobs?, has_cics?, has_db2?, has_ims?, has_mq?, num_vsam_files?, num_copybooks?) — emit only the inputs the SA stated. Missing required counts → low-confidence result, not an error.
- get_fsi_compliance_check(regulation?) — regulation ∈ {{SOX, PCI_DSS, FDIC, OCC, GLBA, FFIEC, FINRA_17a-4}}; empty → general overview
- compare_partner_tools(source_systems?, pattern?, aws_m2_managed_only?, top_n?) — source_systems is a list of mainframe stack tokens (e.g. ["COBOL","CICS","DB2","VSAM"]); pattern ∈ {{reimagine, rehost, replatform, refactor, automated_refactor, retire}}; top_n defaults to 5
- list_taxonomy(tool_name?) — meta tool. Use when you need to discover the valid enum values for any other tool's args. Empty → taxonomy for all tools.

NOTE — the following tools were previously in MainframeMCP but are DEPRECATED at the agent layer. Do NOT emit calls to them. If SA asks about COBOL/JCL syntax, mainframe→AWS component mapping, or AWS-service comparisons, answer from KB retrieval + your training data + AWSMCP docs search (aws___search_documentation). The deprecation is intentional — Sonnet + KB + AWS docs cover these topics more accurately than the curated JSON did.
- ~~lookup_cobol_pattern~~ (deprecated — use KB + training data)
- ~~lookup_jcl_reference~~ (deprecated — use KB + training data)
- ~~map_mainframe_to_aws~~ (deprecated — use aws___search_documentation + KB)
- ~~compare_services~~ (deprecated — use aws___search_documentation + KB)

AWSMCP tools (managed by AWS — use SELECTIVELY. These add latency and noise. Pick them ONLY when the query clearly fits, and prefer KB first.):
- aws___search_documentation(search_phrase, topics?, limit?) — Full-text search over AWS docs. topics ∈ {{reference_documentation, current_awareness, troubleshooting, amplify_docs, cdk_docs, cdk_constructs, cloudformation, agent_skills, strands_docs, general}}; pick up to 3. Use ONLY when the question is about a specific AWS service / feature / API the curated KB likely lacks (newer services, niche pages, "what's new" content). Do NOT use for general mainframe-modernization Q&A — KB covers that.
- aws___read_documentation(requests) — Fetch full AWS docs pages. requests is a list of {{url, max_length?, start_index?}}. Use AFTER aws___search_documentation returns URLs you want to read in full.
- aws___list_regions() — Full list of AWS regions with codes and long names.
- aws___get_regional_availability(regions, resource_type, filters?) — Check whether a product / API / CFN resource is available in specified regions. resource_type ∈ {{product, api, cfn}}. Max 10 regions per call. Use ONLY when SA asks a specific availability question with a named resource.
- aws___retrieve_skill(skill_name) — Retrieve an AWS Agent Skill. REQUIRED PREREQUISITE: aws___search_documentation must have already returned the exact skill_name. Never guess.

WebSearch tool (managed AWS AgentCore web-search connector — use SPARINGLY):
- WebSearch(query, maxResults?) — Live internet search. Use ONLY when SA explicitly asks to search the web / fact-check / look up online, or for very recent events the curated KB and AWS docs both won't have. Never as a substitute for KB or AWSMCP docs tools.

AwsPricingMCP tools (live AWS Pricing API via awslabs pricing MCP server on our own AgentCore Runtime):
- get_pricing_service_codes() — List all valid service_code values (e.g. `AmazonEC2`, `AmazonRDS`, `AWSMainframeModernization`). Cheap; call this first if you don't know the code.
- get_pricing_service_attributes(service_code) — List filterable attributes for a service (e.g. `instanceType`, `location`, `operatingSystem` for EC2). Call BEFORE get_pricing to know what to filter on.
- get_pricing_attribute_values(service_code, attribute_name) — Enum of valid values for one attribute of a service.
- get_pricing(service_code, region, filters?, output_options?) — The workhorse. `filters` is a list of `{{"Field": "...", "Value": "...", "Type": "TERM_MATCH"}}`. Pass `output_options={{"pricing_terms": ["OnDemand"]}}` to trim response size. Returns real pricing data.
- get_price_list_urls(service_codes, region) — Bulk JSON price-list URLs. Use for TCO / offline analysis.
- generate_cost_report(services) — Structured multi-service cost report.
- get_bedrock_patterns() — Common Bedrock architecture pricing patterns.
Use pricing tools when SA asks about cost / TCO / "how much" / instance rates / DMS pricing. Response LLM should cite prices as-of the timestamp Sonnet gets the tool output — pricing changes and stale numbers are worse than no number.

Return ONLY valid JSON:
{{"route": "...", "mcp_tools": [{{"tool": "tool_name", "args": {{...}}}}]}}

If route is "direct" or "kb", set mcp_tools to [].

User query: {query}"""


def _llm_route(query: str) -> tuple[str, list[dict]]:
    """Use Haiku to classify an ambiguous query."""
    try:
        llm = get_router_llm()
        prompt = LLM_ROUTING_PROMPT.format(query=query)
        response = llm.invoke(prompt)
        content = response.content.strip()

        # Extract JSON from response (handle markdown code blocks)
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        result = json.loads(content)
        route = result.get("route", "kb")
        mcp_tools = result.get("mcp_tools", [])

        # Validate route
        if route not in ("direct", "kb", "mcp", "both"):
            route = "kb"

        return route, mcp_tools

    except Exception as e:
        logger.warning(f"LLM router failed, falling back to KB: {e}")
        return "kb", []


# ---------------------------------------------------------------------------
# Public API — intent-aware routing
# ---------------------------------------------------------------------------

# Maps intent (from agent.intent.classify_intent) to a graph route.
# `substantive` is special — it falls through to keyword/LLM topic routing.
_INTENT_TO_ROUTE = {
    "chat":          "direct",
    "meta_help":     "help",
    "meta_summary":  "summary",
    "defer":         "defer",
    "acknowledge":   "acknowledge",
    # "substantive" → resolved via _keyword_route below
}


def route_query(query: str, prior_probe: str = "") -> tuple[str, list[dict], str]:
    """Route a user query. Returns (route, mcp_tools, intent).

    Pipeline:
      1. Classify the SA's INTENT (substantive | defer | acknowledge |
         meta_help | meta_summary | chat). Cheap heuristics first; Haiku
         falls back for the ambiguous middle. `prior_probe` is the last
         **To advance:** question — passed into the LLM context so a
         short reply gets correctly classified as substantive/defer.
      2. Non-substantive intents map directly to a flow route (direct /
         help / summary / defer / acknowledge).
      3. Substantive intents go through topic routing (keyword fast-path
         + LLM fallback) to pick kb/mcp/both and the MCP tool selection.
    """
    # Lazy import to avoid circular: intent uses get_router_llm from config
    from .intent import classify_intent

    intent = classify_intent(query, prior_probe=prior_probe)

    if intent in _INTENT_TO_ROUTE:
        route = _INTENT_TO_ROUTE[intent]
        logger.info(f"router: intent={intent} → route={route} (no retrieval)")
        return route, [], intent

    # intent == "substantive" — resolve the topic route
    route, mcp_tools, is_confident = _keyword_route(query)
    if is_confident:
        # Substantive messages must never short-circuit to "direct" (the
        # bound-aware greeting node). If keyword routing said "direct"
        # for a substantive message, that's an over-fire — upgrade to KB.
        if route == "direct":
            logger.info(f"router: substantive but keyword said direct — upgrading to kb")
            route = "kb"
        logger.info(f"router: intent=substantive keyword-path → route={route}, tools={len(mcp_tools)}")
        return route, mcp_tools, intent

    # Ambiguous topic — let Haiku pick kb/mcp/both
    route, mcp_tools = _llm_route(query)
    # Same guard: substantive must never collapse to direct.
    if route == "direct":
        logger.info(f"router: substantive but LLM said direct — upgrading to kb")
        route = "kb"
    logger.info(f"router: intent=substantive LLM-path → route={route}, tools={len(mcp_tools)}")
    return route, mcp_tools, intent
