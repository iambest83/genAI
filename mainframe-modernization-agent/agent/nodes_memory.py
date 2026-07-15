"""Memory-related graph nodes.

Three nodes:
- profile_loader_node : first node in the graph; loads CustomerProfile
  for the authenticated (sa_id, customer_id) into State.

- drift_guard_node    : runs after profile_loader, before router. If the
  user's message names a customer that doesn't match the bound profile,
  asks the SA to confirm before proceeding (item 1.12).

- profile_updater_node: last node in the graph before response emission;
  extracts new facts from the user turn and applies them. On contradiction,
  does NOT overwrite — queues the conflict in state.pending_contradictions
  so the response generator can surface it as a clarifying question in the
  SAME turn. No mid-turn interrupt.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .customer_profile import CustomerProfile
from .memory import (
    load_profile, load_customer_lob_profiles, upsert_profile, write_turn_event,
)
from .state import AgentState
from .config import get_router_llm  # re-use the fast/cheap model

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def profile_loader_node(state: AgentState) -> dict:
    sa_id = state.get("sa_id")
    customer_id = state.get("customer_id")
    lob_id = state.get("lob_id") or "default"
    customer_display_name = state.get("customer_display_name") or ""
    lob_display_name = state.get("lob_display_name") or ""

    # No customer bound (or sa_id missing): never touch DynamoDB. Synthesize
    # an empty in-memory profile so the rest of the graph runs cleanly with
    # generic-only answers. Persistence resumes when the SA binds a customer.
    cust_lc = (customer_id or "").strip().lower()
    if not sa_id or not customer_id or cust_lc == "default":
        logger.info("profile_loader: no customer bound — skipping DDB read, using empty profile")
        empty = CustomerProfile(
            sa_id=sa_id or "unknown",
            customer_id=customer_id or "default",
            lob_id=lob_id,
            customer_display_name=customer_display_name,
            lob_display_name=lob_display_name,
        )
        return {
            "customer_profile": empty,
            "phase": empty.derive_phase(),
            "open_questions": list(empty.open_questions),
            "pending_contradictions": [],
            "profile_dirty": False,
        }

    profile = load_profile(
        sa_id, customer_id, lob_id,
        display_name=customer_display_name,
        lob_display_name=lob_display_name,
    )
    # If the row didn't have an LoB display name but the request did, pick it up
    if lob_display_name and not profile.lob_display_name:
        profile.lob_display_name = lob_display_name
    if customer_display_name and not profile.customer_display_name:
        profile.customer_display_name = customer_display_name

    # Customer-wide overview — pull every LoB profile this SA has for the
    # customer. Used by the response prompt so a customer-level question
    # ("what do we know about Fidelity?") gets the union of LoB knowledge,
    # not just the currently bound LoB. Read-only (we never mutate or write
    # these). Skipped when an LoB is already bound — the bound profile is
    # the authoritative slice.
    overview: list[CustomerProfile] = []
    lob_norm = (lob_id or "default").strip().lower() or "default"
    if lob_norm == "default":
        overview = load_customer_lob_profiles(sa_id, customer_id)
        # Filter out the currently loaded slice to avoid duplicate facts
        overview = [
            p for p in overview
            if (p.lob_id or "default") != "default"
        ]

    phase = profile.derive_phase()
    logger.info(
        f"profile_loader: loaded sa={sa_id} customer={customer_id} lob={profile.lob_id} "
        f"version={profile.version} empty={profile.is_empty()} phase={phase} "
        f"overview_lobs={len(overview)}"
    )
    return {
        "customer_profile": profile,
        "customer_overview": overview,
        "phase": phase,
        "open_questions": list(profile.open_questions),
        "pending_contradictions": [],
        "profile_dirty": False,
    }


# ---------------------------------------------------------------------------
# Drift guard — prevent silent cross-customer fact merging (item 1.12)
# ---------------------------------------------------------------------------

# Hard signal that the SA is talking about the bound customer (cheap path).
# We don't want to ask "are you switching?" when they're just talking about
# the customer we already know about.

DRIFT_LLM_PROMPT = """You are a binary classifier for a mainframe modernization
assistant. The agent has a customer profile loaded for "{bound_name}". The SA
just said:

{message}

Question: does the SA's message refer to a DIFFERENT customer (a real
organization name distinct from "{bound_name}")? Treat references to
"the customer", "they", "this customer" as the SAME customer. Look for
proper-noun company / bank / fintech / firm names that don't match.

Reply with ONE word only:
- DRIFT       — clearly different customer named
- SAME        — clearly same customer or no other customer named
- AMBIGUOUS   — unclear

One-word answer:"""


# Tech / mainframe / AWS terms that look like proper nouns but aren't customers.
_TECH_NON_CUSTOMER_TOKENS = {
    "I", "AWS", "FSI", "COBOL", "JCL", "CICS", "DB2", "VSAM", "IMS",
    "MQ", "RACF", "SOX", "PCI", "PCI-DSS", "FDIC", "OCC", "GLBA",
    "FFIEC", "DORA", "BCBS", "NYDFS", "APRA", "MAS",
    "S3", "EC2", "RDS", "DynamoDB", "Aurora", "Lambda", "ECS", "Fargate",
    "EKS", "KMS", "IAM", "VPC", "API",
}

# Partners, vendors, regulators, products: SAs mention these constantly when
# discussing modernization. They are NOT customers. Suppress drift on any of
# these to avoid false positives.
_PARTNER_AND_VENDOR_TOKENS = {
    # AWS partner SIs / GSIs
    "TCS", "Tata", "Tata Consultancy Services",
    "Deloitte", "Accenture", "Capgemini", "Cognizant", "DXC",
    "HCL", "HCLTech", "HCL Tech", "Wipro", "Infosys", "Cobalt",
    "NTT", "NTT DATA", "NTT Data", "UniKix",
    "Kyndryl", "IBM", "IBM Consulting",
    "Atos", "Astadia", "Heirloom", "Heirloom Computing",
    "Stromasys", "Precisely", "Syncsort", "LzLabs",
    # Mainframe modernization vendors / products
    "Micro Focus", "MicroFocus", "Micro", "Focus", "OpenText",
    "Blu Age", "BluAge", "Mainframe Modernization", "M2",
    "MasterCraft", "TransformPlus", "innoWake", "Innowake",
    "ADvantage", "Modernize", "Prizm", "AppLens", "CloudSteps",
    "QTE", "Quick Transformation Engine",
    # Hyperscalers / cloud / well-known software
    "Azure", "Google", "GCP", "Oracle", "SAP", "Salesforce",
    # AWS programs / docs that look like proper nouns
    "Transform", "AWS Transform", "Skill Builder", "ProServe",
    "Bedrock", "AgentCore", "Highspot", "Q Developer",
    "Mainframe Assessment Tool", "MPA", "MAP",
    # Generic FSI / market terms that look like names
    "Banking", "Insurance", "Capital Markets", "Wealth", "Cards",
    "Financial Services", "Wall Street",
}


# Common English sentence-starter words that get capitalized but aren't names.
_SENTENCE_STARTER_WORDS = {
    "what", "when", "where", "why", "how", "who", "which",
    "should", "would", "could", "can", "do", "does", "did",
    "is", "are", "was", "were", "will",
    "tell", "show", "give", "let", "let's", "lets",
    "compare", "draft", "create", "generate", "build",
    "i", "if", "the", "a", "an",
}


def _strip_to_tokens(phrase: str) -> set[str]:
    """Split a candidate proper-noun phrase into individual lowercase tokens
    so we can intersection-test against the allowlists token-by-token."""
    return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", phrase)}


# NOTE: the unbound-state customer-detect heuristic was removed. It kept
# misfiring on capitalized verbs in starter prompts ("Note", "Highlight",
# "Map", "Walk", etc.) and disrupting the conversation with bogus
# confirmation popups. Customer binding now flows exclusively through the
# Customer chip (selectCustomer WS action). Until that fires, the agent
# answers generically and persists nothing.


def _quick_drift_check(message: str, bound_name: str) -> str | None:
    """Cheap heuristic. Returns 'SAME' / 'DRIFT' / None (= unclear, escalate).

    Rules (in order):
      1. Empty / default bound → SAME (drift-guard is moot)
      2. Full bound-name appears in message → SAME
      3. Any non-trivial bound-name token appears in message → SAME
         (catches "Acme is..." when bound is "Acme Bank")
      4. No capitalized noun phrases at all → SAME
      5. Strip sentence-starter capitals; for remaining phrases,
         all tokens fall in the tech / partner / vendor allowlist → SAME
      6. Otherwise → None (escalate to LLM)
    """
    if not bound_name or bound_name in ("default", "unknown", ""):
        return "SAME"

    msg_lower = message.lower()
    if bound_name.lower() in msg_lower:
        return "SAME"

    # Token-level bound-name match: catches "Acme is targeting" when bound is
    # "Acme Bank". Skip tokens that are too short or generic.
    bound_tokens = {
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", bound_name.lower())
        if len(t) >= 4 and t not in {"bank", "banc", "corp", "inc", "ltd", "llc",
                                     "group", "holdings", "company"}
    }
    if any(re.search(rf"\b{re.escape(tok)}\b", msg_lower) for tok in bound_tokens):
        return "SAME"

    # Find capitalized noun phrases (greedy: "Micro Focus", "IBM Consulting").
    candidates = re.findall(r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\b", message)
    if not candidates:
        return "SAME"

    allowlist_lower = {t.lower() for t in
                       (_TECH_NON_CUSTOMER_TOKENS | _PARTNER_AND_VENDOR_TOKENS)}
    # Allowlist tokenized: e.g. "Tata Consultancy Services" → {"tata","consultancy","services"}
    allowlist_tokens: set[str] = set()
    for t in (_TECH_NON_CUSTOMER_TOKENS | _PARTNER_AND_VENDOR_TOKENS):
        allowlist_tokens |= _strip_to_tokens(t)

    real_candidates: list[str] = []
    for raw in candidates:
        # Split on whitespace; first word might be a sentence starter.
        words = raw.split()
        # Drop a leading sentence-starter token; e.g. "Compare Deloitte" → "Deloitte".
        if words and words[0].lower() in _SENTENCE_STARTER_WORDS:
            words = words[1:]
        if not words:
            continue
        phrase = " ".join(words)
        if len(phrase) < 3:
            continue

        # Whole phrase or all its tokens in allowlist? → suppress.
        if phrase.lower() in allowlist_lower:
            continue
        phrase_tokens = _strip_to_tokens(phrase)
        if phrase_tokens and phrase_tokens.issubset(allowlist_tokens):
            continue

        real_candidates.append(phrase)

    if not real_candidates:
        return "SAME"

    return None  # unclear; escalate to LLM


def _llm_drift_check(message: str, bound_name: str) -> str:
    """Single-token Haiku call. Returns 'DRIFT' / 'SAME' / 'AMBIGUOUS'."""
    try:
        llm = get_router_llm()
        prompt = DRIFT_LLM_PROMPT.format(message=message, bound_name=bound_name)
        resp = llm.invoke(prompt).content.strip().upper()
        for token in ("DRIFT", "SAME", "AMBIGUOUS"):
            if token in resp:
                return token
        return "AMBIGUOUS"
    except Exception as e:
        logger.warning(f"drift_guard: LLM check failed, defaulting to SAME: {e}")
        return "SAME"


def drift_guard_node(state: AgentState) -> dict:
    """Placeholder for customer-drift detection (item 1.12).

    Currently a no-op. The heuristic approach (proper-noun detection) was
    removed because it misfired on partner names, capitalized verbs, and
    domain nouns. The replacement architecture — an allowlist-based match
    against the SA's own customer list from DDB — is parked in
    REFINEMENTS.md and will be implemented when SA auth lands.

    Cross-customer fact merging is mitigated in the meantime by:
      - the strict quote-grounding extractor (drops anything that doesn't
        echo the SA's literal phrase)
      - the explicit Customer chip being the only binding mechanism
    """
    return {}


# ---------------------------------------------------------------------------
# Updater — fact extraction + contradiction surfacing
# ---------------------------------------------------------------------------

from .utils import allowed_regulation_tokens as _allowed_regulation_tokens


_FACT_EXTRACTION_TEMPLATE = """You are a fact extractor for a mainframe modernization assistant.

Given a Solutions Architect's message about a customer, extract ONLY concrete facts
the SA has *explicitly stated* about THE CUSTOMER'S CURRENT STATE.

CRITICAL — DO NOT extract:
  - Anything from a question (e.g. "what about CICS?" does NOT mean has_cics=true)
  - Anything hypothetical (e.g. "if they have DB2…")
  - Anything the SA is asking ABOUT (asking is not stating)
  - Generic discussion of mainframe technologies the SA is curious about
  - Lists of things the SA wants to learn about

ONLY extract a fact when the SA makes a declarative statement of fact about
the specific customer at hand (e.g. "they have 800 COBOL programs",
"the workload runs on CICS", "they're regulated under SOX").

Return a JSON array of facts. Each fact has:
  - field_path : one of the exact paths listed below
  - value      : appropriate type (int, bool, string, or list of strings)
  - quote      : the exact SA phrase supporting the fact (REQUIRED; if you can't
                 quote a phrase, the fact is not stated — drop it)

Allowed field paths:
  industry_segment                      (string: e.g. "core-banking", "insurance", "payments")
  workload.num_cobol_programs           (int)
  workload.num_jcl_jobs                 (int)
  workload.num_vsam_files               (int)
  workload.num_copybooks                (int)
  workload.has_cics                     (bool)
  workload.has_db2                      (bool)
  workload.has_ims                      (bool)
  workload.has_mq                       (bool)
  workload.languages                    (list of strings, e.g. ["COBOL","PL/I"])
  workload.databases                    (list of strings, e.g. ["DB2","VSAM"])
  workload.mainframe_vendor             (string)
  workload.mips_capacity                (int — peak/installed MIPS, e.g. "40K MIPS"=40000)
  workload.online_tps_peak              (int — peak online TPS, e.g. "5K TPS"=5000)
  workload.batch_window_hours           (int — nightly batch window length in hours)
  constraints.regulations               (list: {regulation_enum})
  constraints.data_residency            (list of AWS region codes)
  constraints.target_date               (string like "2027-Q2")
  constraints.budget_band               (string)
  constraints.risk_appetite             (string: low|medium|high)
  constraints.downtime_tolerance        (string: zero|weekend|extended)

If no facts are present, return [].

SA message:
{message}

Return only the JSON array, no prose."""


# Substitute regulation_enum at module load. We use a placeholder swap rather
# than .format() to avoid escaping every other curly brace in the template.
FACT_EXTRACTION_PROMPT = _FACT_EXTRACTION_TEMPLATE.replace(
    "{regulation_enum}", _allowed_regulation_tokens()
)


DECISION_EXTRACTION_PROMPT = """Extract DECISIONS from the SA's message — that is,
explicit commitments the SA has made or reported the customer making.

CRITICAL — DO NOT extract a decision unless ALL of these are true:
  1. The SA's message contains a clear declarative commitment (e.g. "we're going
     with replatform", "the customer chose Micro Focus", "they decided to target
     Aurora PostgreSQL").
  2. You can quote the literal SA phrase that establishes the commitment.
  3. The decision is about THIS engagement / customer — not a general comment
     about mainframe modernization, not the SA's preferred default.

DO NOT extract decisions from:
  - Questions ("should we replatform?")
  - Hypotheticals ("if we replatform…")
  - Discussion of options ("the patterns are rehost, replatform, refactor…")
  - Generic mentions of technologies in passing
  - Anything a user-message starter prompt asks the agent to discuss

Return JSON array of:
  - category   (one of: pattern, partner_tool, target_service, target_runtime, data_strategy)
  - value      (e.g. "replatform", "Micro Focus", "Aurora PostgreSQL")
  - rationale  (short string, or empty)
  - quote      (REQUIRED — exact SA phrase establishing the commitment; if you
                cannot quote a phrase, the decision is not stated, drop it)

If unsure, drop the decision. False positives stick to the profile and corrupt
every future answer — being conservative is mandatory.

SA message:
{message}

Return only the JSON array, no prose."""


def _quote_supports_message(quote: str, message: str) -> bool:
    """True iff the `quote` field returned by the extractor actually appears
    in the SA's message. Used to reject hallucinated facts/decisions the
    SA never stated.

    Two passes:
      1. Strict — first 5 words of the quote appear verbatim in the message.
      2. Lenient — strip non-alphanumerics + normalize numeric shorthand
         ("40K" → "40000", "1.5M" → "1500000"), then look for the head.
         Catches the common case where Haiku echoes "40,000 MIPS" while the
         SA typed "40K MIPS" — both are stating the same fact.
    """
    import re
    if not quote or not isinstance(quote, str):
        return False
    norm_msg = re.sub(r"\s+", " ", message or "").strip().lower()
    norm_q = re.sub(r"\s+", " ", quote).strip().lower().strip('"\'')
    if not norm_q:
        return False

    head = " ".join(norm_q.split()[:5])
    if head in norm_msg:
        return True

    # Lenient pass: collapse "40k"→"40000", "1.5m"→"1500000", strip commas /
    # punctuation, then re-test.
    def _expand_numeric_shorthand(s: str) -> str:
        def _repl(m):
            num = float(m.group(1))
            mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2).lower()]
            return str(int(num * mult))
        return re.sub(r"(\d+(?:\.\d+)?)\s*([kmb])\b", _repl, s, flags=re.IGNORECASE)

    def _flatten(s: str) -> str:
        s = _expand_numeric_shorthand(s)
        s = re.sub(r"[,\.\-_/]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    flat_msg = _flatten(norm_msg)
    flat_head = _flatten(head)
    return flat_head and flat_head in flat_msg


def _extract_json_array(raw: str) -> list:
    raw = raw.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        # Last-ditch: find first [...] block
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return []
        return []


def _extract_to_advance_question(text: str) -> str:
    """Pull the gap question Sonnet emitted via the **To advance:** prefix.

    Returns the question text (without the prefix) or "" if none found.
    Match is case-insensitive and tolerates surrounding whitespace.
    """
    if not text:
        return ""
    m = re.search(
        r"\*\*\s*To advance\s*:\*\*\s*(.+?)(?:\n\n|$)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    q = m.group(1).strip()
    # Collapse internal whitespace; cap length defensively.
    q = re.sub(r"\s+", " ", q)
    return q[:280]


def _which_open_questions_did_facts_answer(facts: list, open_qs: list[str]) -> set[int]:
    """Heuristic: any open question that mentions a field path that just got
    populated is considered answered. Returns indices to drop.

    This isn't perfect; it's a cheap mechanism so we don't ask the same
    question turn after turn. Worst case we drop a question that wasn't
    fully answered — Sonnet will probe again next turn naturally.
    """
    if not facts or not open_qs:
        return set()

    populated_fields = {f.get("field_path", "") for f in facts if f.get("field_path")}
    field_keywords = set()
    for fp in populated_fields:
        # field_path like "workload.num_cobol_programs" → "cobol", "programs"
        parts = re.split(r"[._]", fp.lower())
        field_keywords.update(p for p in parts if len(p) > 3)

    drop = set()
    for i, q in enumerate(open_qs):
        ql = q.lower()
        if any(kw in ql for kw in field_keywords):
            drop.add(i)
    return drop


def profile_updater_node(state: AgentState) -> dict:
    """Extract facts from user_query and merge into the profile.

    Never overwrites on contradiction — queues them for the response generator.
    Only persists to DynamoDB if any mutations were applied.

    Also manages the open_questions queue:
      - Pulls the new gap question Sonnet emitted (**To advance:** ...)
        and appends to profile.open_questions
      - Heuristically drops queued questions that this turn's facts answered
    """
    query = state.get("user_query", "")
    profile: CustomerProfile = state.get("customer_profile")
    turn = state.get("turn", 0)
    response_text = state.get("response_text", "") or ""
    intent = state.get("intent", "")

    if not query or profile is None:
        return {}

    # No real customer bound (the SA hasn't picked one in the header chip)
    # → never persist anything. This keeps the conversation usable for ad-hoc
    # browsing while guaranteeing we don't pollute the shared "default"
    # profile with facts from one SA's session that another SA would later
    # see. Persistence resumes the moment the SA binds a customer.
    cust_id = (profile.customer_id or "").strip().lower()
    if not cust_id or cust_id == "default":
        logger.info("profile_updater: no customer bound — skipping all profile writes")
        return {
            "customer_profile": profile,
            "phase": profile.derive_phase(),
            "open_questions": list(profile.open_questions),
        }

    # If drift was detected, skip fact extraction entirely — we'd be merging
    # the wrong customer's facts into the bound profile. Wait for SA to
    # confirm the switch.
    if state.get("drift_detected"):
        logger.info("profile_updater: drift_detected — skipping fact extraction")
        return {}

    # --- Defer intent: drop the matching open_question, no fact extraction ---
    # The SA explicitly skipped the agent's prior probe. Pull the deferred
    # question (set by defer_node into state) and remove it from the queue.
    # Persist if the queue actually changed.
    if intent == "defer":
        deferred = (state.get("deferred_question") or "").strip()
        if deferred and profile.open_questions:
            before = list(profile.open_questions)
            profile.open_questions = [
                q for q in profile.open_questions if q.strip() != deferred
            ]
            if profile.open_questions != before:
                profile.version += 1
                profile.updated_at = time.time()
                try:
                    upsert_profile(profile)
                except Exception as e:
                    logger.error(f"profile_updater(defer): persist failed: {e}")
                logger.info(f"profile_updater: defer dropped 1 open question; "
                            f"{len(profile.open_questions)} remaining")
        # Always log the turn event so audits show the defer
        write_turn_event(
            profile, turn,
            user_query=query,
            facts_extracted=[],
            decisions_extracted=[],
            contradictions=[],
            response_text=response_text,
            open_question_added="",
            open_questions_dropped=[deferred] if deferred else [],
        )
        return {
            "customer_profile": profile,
            "phase": profile.derive_phase(),
            "open_questions": list(profile.open_questions),
        }

    # --- Acknowledge / chat / help / summary: skip fact extraction ---------
    # These intents don't carry SA-stated facts. Cost savings = 2 Haiku
    # calls per turn. Still log the turn event for audit completeness.
    if intent in ("acknowledge", "chat", "meta_help", "meta_summary"):
        write_turn_event(
            profile, turn,
            user_query=query,
            facts_extracted=[],
            decisions_extracted=[],
            contradictions=[],
            response_text=response_text,
            open_question_added="",
            open_questions_dropped=[],
        )
        logger.info(f"profile_updater: intent={intent} — skipped extraction")
        return {
            "customer_profile": profile,
            "phase": profile.derive_phase(),
            "open_questions": list(profile.open_questions),
        }

    # Run extraction (facts + decisions) concurrently — two independent Haiku
    # calls that share no state. Parallel execution saves one full round-trip
    # (~400-600ms) on every substantive turn.
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        llm = get_router_llm()
        fact_prompt = FACT_EXTRACTION_PROMPT.format(message=query)
        decision_prompt = DECISION_EXTRACTION_PROMPT.format(message=query)

        with ThreadPoolExecutor(max_workers=2) as ex:
            fact_future = ex.submit(lambda: llm.invoke(fact_prompt).content)
            decision_future = ex.submit(lambda: llm.invoke(decision_prompt).content)
            facts_raw = fact_future.result()
            decisions_raw = decision_future.result()
    except Exception as e:
        logger.warning(f"profile_updater: LLM extraction failed: {e}")
        return {}

    facts = _extract_json_array(facts_raw)
    decisions = _extract_json_array(decisions_raw)

    # Drop any fact/decision whose `quote` is missing or doesn't actually
    # appear in the SA message. This is the cheapest defense against the
    # extractor inventing facts the SA never stated (a known Haiku failure
    # mode that was poisoning every downstream answer).
    facts = [f for f in facts if _quote_supports_message(f.get("quote", ""), query)]
    decisions = [d for d in decisions if _quote_supports_message(d.get("quote", ""), query)]
    if not facts and not decisions:
        logger.info("profile_updater: extraction produced nothing groundable; skipping writes")

    pending: list[dict] = list(state.get("pending_contradictions", []) or [])
    dirty = False

    for f in facts:
        fp = f.get("field_path")
        val = f.get("value")
        if not fp or val is None:
            continue
        try:
            status, old = profile.apply_fact(fp, val, turn)
        except AttributeError:
            logger.warning(f"profile_updater: bad field_path {fp}")
            continue

        if status in ("set", "extended"):
            dirty = True
        elif status == "contradicts":
            pending.append({
                "field_path": fp,
                "old_value": old,
                "new_value": val,
                "rationale": f"Previously stated {old}; now stated {val}.",
            })

    for d in decisions:
        cat = d.get("category")
        val = d.get("value")
        if not cat or not val:
            continue
        profile.add_decision(cat, val, d.get("rationale", ""), turn)
        dirty = True

    # --- Open-question queue management (probe-and-guide) ---------------
    # 1. Drop any queued questions that this turn's facts probably answered
    dropped_qs: list[str] = []
    if profile.open_questions and facts:
        drop_idxs = _which_open_questions_did_facts_answer(facts, profile.open_questions)
        if drop_idxs:
            dropped_qs = [profile.open_questions[i] for i in sorted(drop_idxs)]
            profile.open_questions = [
                q for i, q in enumerate(profile.open_questions) if i not in drop_idxs
            ]
            dirty = True

    # 2. Append the new gap question Sonnet emitted this turn
    new_q = _extract_to_advance_question(response_text)
    if new_q and new_q not in profile.open_questions:
        # Cap the queue so we don't accumulate forever
        profile.open_questions = (profile.open_questions + [new_q])[-10:]
        dirty = True

    # --- Persist (snapshot + per-turn event) ----------------------------
    # Snapshot persists only if anything mutated.  Event row persists on
    # every non-trivial turn so we have a full audit/replay trail
    # (item 1.15, locked decision D1 = event-sourced).
    if dirty:
        try:
            upsert_profile(profile)
        except Exception as e:
            logger.error(f"profile_updater: persist snapshot failed: {e}")

    # Write the per-turn event row whenever we did real extraction work
    # (i.e. we got past the drift_detected / empty-query early returns).
    # Even "no facts extracted" turns are worth recording — they show
    # what the SA asked and what was discussed.
    write_turn_event(
        profile,
        turn,
        user_query=query,
        facts_extracted=facts,
        decisions_extracted=decisions,
        contradictions=pending,
        response_text=response_text,
        open_question_added=new_q,
        open_questions_dropped=dropped_qs,
    )

    return {
        "customer_profile": profile,
        "pending_contradictions": pending,
        "profile_dirty": dirty,
        "phase": profile.derive_phase(),     # phase may have advanced this turn
        "open_questions": list(profile.open_questions),
    }
