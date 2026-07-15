"""Listen-mode nodes — paste meeting notes → extract → preview → merge.

Iteration 4.1 + 4.3 + 4.6.

Flow (per item 4.1):
  meeting_notes_node    : runs the conversation-flavored extractor against
                          pasted notes/transcript text, returns a STRUCTURED
                          PREVIEW. Persists nothing — that's the safety
                          catch (item 4.6).
  meeting_merge_node    : applies the SA-confirmed subset of the preview
                          into the bound customer's profile via the same
                          apply_fact + add_decision paths the typed-input
                          updater uses.

Why a separate extractor (item 4.3): typed SA prompts are crisp ("they have
800 COBOL programs"). Meeting transcripts are messy — hedging ("we have,
what, around 800 programs?"), interruption, off-topic digressions, opposing
contradictions in the same conversation. The single-shot quote-grounded
extractor we use today loses too many true facts on this input. The
conversation extractor relaxes the strictness on quote-matching (since
speakers paraphrase mid-sentence) but keeps the "drop if unsourced" rule.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from .config import get_router_llm
from .customer_profile import CustomerProfile
from .memory import upsert_profile, write_turn_event
from .state import AgentState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversation extractor prompt
# ---------------------------------------------------------------------------

from .utils import allowed_regulation_tokens as _allowed_regulation_tokens


_CONVERSATION_EXTRACTION_TEMPLATE = """You are a fact-and-action extractor for a
mainframe modernization assistant. The text below is meeting notes or a
transcript from a customer conversation. Multiple speakers, paraphrased
statements, hedging ("around", "I think", "maybe"), interruptions.

Extract FOUR distinct lists (return as one JSON object).

  facts          : concrete current-state facts about the customer's
                   workload / constraints / posture. Only include things
                   STATED about the customer in the conversation. Drop
                   speculation, hypotheticals ("if we had..."), and
                   questions the SA asked the customer.

  decisions      : explicit commitments the customer (or AWS) has made.
                   Pattern, partner, target service, target runtime, data
                   strategy. Only when there is a clear declarative
                   commitment in the text.

  action_items   : concrete things someone owes back, with owner where
                   stated. "Customer to send DB2 schema export by EoW",
                   "AWS to draft a TCO model", etc.

  open_questions : questions raised but not answered, OR clarifications
                   the customer needs to come back on.

Each entry has these fields:

  facts[i]:
    - field_path : MUST be one of (or omit and use raw_note instead):
        industry_segment | workload.num_cobol_programs |
        workload.num_jcl_jobs | workload.num_vsam_files |
        workload.num_copybooks | workload.has_cics | workload.has_db2 |
        workload.has_ims | workload.has_mq | workload.languages |
        workload.databases | workload.mainframe_vendor |
        workload.mips_capacity | workload.online_tps_peak |
        workload.batch_window_hours |
        constraints.regulations (one of {regulation_enum}) |
        constraints.data_residency | constraints.target_date |
        constraints.budget_band | constraints.risk_appetite |
        constraints.downtime_tolerance
    - value      : appropriate type (int, bool, string, or list of strings)
    - quote      : a literal phrase from the conversation that supports
                   this fact (REQUIRED — at minimum 4-5 words verbatim)
    - speaker    : best-guess speaker label if available ("Customer",
                   "AWS SA", or empty)

  decisions[i]:
    - category   : one of "pattern" | "partner_tool" | "target_service"
                   | "target_runtime" | "data_strategy"
    - value      : e.g. "replatform", "Micro Focus", "Aurora PostgreSQL"
    - rationale  : short string, or empty
    - quote      : REQUIRED literal phrase from the conversation
    - speaker    : best-guess speaker if available

  action_items[i]:
    - owner      : "Customer" | "AWS" | named individual | "unspecified"
    - text       : what they're committing to (1-2 lines)
    - due        : ISO-ish date or relative phrase ("EoW", "by Q3"), or empty

  open_questions[i]:
    - text       : the question or clarification needed (1-2 lines)
    - blocks     : short note on what this blocks (e.g. "TCO model",
                   "pattern decision"), or empty

If a section has no items, return an empty list for that key.

CRITICAL — be conservative. False positives corrupt the profile. If you
can't quote a phrase or you're guessing, drop the entry.

Meeting notes / transcript:
{notes}

Return ONLY the JSON object, no prose, no markdown fences.
"""


# Substitute regulation_enum at module load. Use replace() rather than format()
# to avoid escaping every other curly brace; {notes} stays a placeholder for
# downstream .format(notes=...).
CONVERSATION_EXTRACTION_PROMPT = _CONVERSATION_EXTRACTION_TEMPLATE.replace(
    "{regulation_enum}", _allowed_regulation_tokens()
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json_object(raw: str) -> dict:
    """Pull a JSON object out of an LLM response that may have markdown
    fences or trailing prose."""
    raw = raw.strip()
    if "```" in raw:
        # Strip the first fenced block
        parts = raw.split("```")
        if len(parts) >= 3:
            raw = parts[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
    # Find the first {...} balanced block
    if not raw.startswith("{"):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"meeting_notes: JSON decode failed: {e}; raw[:200]={raw[:200]!r}")
        return {}


# Quote-grounding constants. Per FIXES.md P3, the prior threshold of "first 3
# words" was too lenient — a transcript phrase like "as we discussed" was
# enough to wave through any value/field_path the LLM picked. Longer minimum
# spans (and a span over the value, not just the quote) close the
# stored-injection sink that flowed extracted values into render_for_prompt.
_MIN_QUOTE_WORDS = 5     # minimum contiguous span we require to match
_VALUE_MIN_CHARS = 3     # tiny values (single token like "DB2") match leniently


def _normalize_for_match(s: str) -> str:
    """Lowercase + collapse whitespace + drop common separators + expand the
    K/M/B numeric shorthand. Same normalization for both quote and notes."""
    if not s:
        return ""
    s = s.strip().lower().strip('"\'')
    # Numeric-shorthand expansion: "40k" / "40 k" -> "40000".
    def _repl(m):
        num = float(m.group(1))
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[m.group(2).lower()]
        return str(int(num * mult))
    s = re.sub(r"(\d+(?:\.\d+)?)\s*([kmb])\b", _repl, s, flags=re.IGNORECASE)
    # Drop common separators that vary between transcripts and quotes.
    s = re.sub(r"[,.\-_/()\[\]]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _quote_in_notes(quote: str, notes: str) -> bool:
    """Quote must echo the notes for at least _MIN_QUOTE_WORDS contiguous
    words. Stricter than the prior 3-word threshold so a casual transcript
    phrase doesn't wave through arbitrary structured values. Per FIXES.md P3.
    """
    if not quote or not isinstance(quote, str) or not notes:
        return False
    norm_n = _normalize_for_match(notes)
    norm_q = _normalize_for_match(quote)
    if not norm_q:
        return False
    words = norm_q.split()
    if len(words) < _MIN_QUOTE_WORDS:
        # Reject quotes shorter than the minimum span outright. Short quotes
        # like "we currently run" or "as we discussed" are common transcript
        # filler and provide no real grounding signal — letting them through
        # was the prior loophole. The model is explicitly instructed to
        # quote >=5 words verbatim; if it can't, drop the row.
        return False
    # Slide a window of N consecutive words over the quote. Any window
    # appearing in the notes is enough — the model can paraphrase the
    # bookends but the load-bearing core must be verbatim.
    for i in range(len(words) - _MIN_QUOTE_WORDS + 1):
        window = " ".join(words[i:i + _MIN_QUOTE_WORDS])
        if window in norm_n:
            return True
    return False


def _value_in_notes(value, notes: str) -> bool:
    """The persisted value itself must have substring support in the notes.

    Per FIXES.md P3, prior code only validated the `quote` field while the
    `value` was persisted unchecked — a transcript could carry a 3-word
    quote-prefix but the value could be attacker-chosen. Now both must
    ground.

    For numeric and boolean values, presence-check against normalized notes.
    For string values, require a normalized substring match. List values
    (e.g. regulations) ground if EVERY entry grounds.
    """
    if value in (None, "", []):
        # No value to validate (caller decides whether to drop).
        return True
    norm_n = _normalize_for_match(notes)
    if isinstance(value, bool):
        # Booleans don't ground textually — they're a distillation. Allow
        # them through; caller's quote-grounding still applies.
        return True
    if isinstance(value, (int, float)):
        if str(int(value) if isinstance(value, float) and value.is_integer() else value) in norm_n:
            return True
        # Allow K/M/B shorthand to match: norm_n already expanded shorthand,
        # but the value comes in as a plain int — try the as-stated value.
        return False
    if isinstance(value, str):
        nv = _normalize_for_match(value)
        if not nv:
            return True
        if len(nv) < _VALUE_MIN_CHARS:
            # Tiny tokens are too easy to spuriously match; require exact
            # word boundary match in normalized notes.
            return f" {nv} " in f" {norm_n} "
        return nv in norm_n
    if isinstance(value, list):
        return all(_value_in_notes(v, notes) for v in value)
    return False


# Length/charset caps for free-string profile fields. These don't change
# correctness — they just neutralize newline-injection / oversized payloads
# before persistence. Per FIXES.md P3.
_FREE_STRING_FIELDS = {
    "industry_segment", "workload.mainframe_vendor",
    "constraints.budget_band", "constraints.target_date",
    "constraints.risk_appetite", "constraints.downtime_tolerance",
}
_FREE_STRING_MAX_LEN = 80


def _sanitize_free_string(value):
    """Strip newlines/control chars and cap length on free-string fields."""
    if not isinstance(value, str):
        return value
    # Strip control chars + collapse whitespace + cap length.
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:_FREE_STRING_MAX_LEN]


# ---------------------------------------------------------------------------
# Node 1: extract preview from pasted notes
# ---------------------------------------------------------------------------

def meeting_notes_node(state: AgentState) -> dict:
    """Run the conversation extractor on `state["meeting_notes_text"]` and
    return a structured preview into `state["meeting_preview"]`. Does NOT
    persist anything — the SA reviews and confirms via meeting_merge_node.

    Refuses to run when no customer is bound: meeting notes always belong
    to a customer.
    """
    notes = (state.get("meeting_notes_text") or "").strip()
    profile: CustomerProfile = state.get("customer_profile")
    if not notes or profile is None:
        return {"meeting_preview": {"error": "missing notes or profile"}}

    cust_lc = (profile.customer_id or "").strip().lower()
    if not cust_lc or cust_lc == "default":
        return {
            "meeting_preview": {
                "error": "no customer bound — paste-notes requires a "
                         "customer. Set one with the Customer chip first.",
            }
        }

    if len(notes) > 32_000:
        # Cap input to avoid runaway token cost. 32K chars ~= a 1-hr call.
        notes = notes[:32_000]

    try:
        llm = get_router_llm()
        prompt = CONVERSATION_EXTRACTION_PROMPT.format(notes=notes)
        raw = llm.invoke(prompt).content
    except Exception as e:
        logger.warning(f"meeting_notes_node: LLM call failed: {e}")
        return {"meeting_preview": {"error": f"extraction failed: {e}"}}

    obj = _extract_json_object(raw)
    facts = obj.get("facts") or []
    decisions = obj.get("decisions") or []
    action_items = obj.get("action_items") or []
    open_questions = obj.get("open_questions") or []

    # Grounding pass — three layers per FIXES.md P3:
    #   (1) Quote must echo a >=5-word span of the source notes.
    #   (2) The persisted value/decision-value must ALSO have substring
    #       support — otherwise an attacker-chosen value could ride a
    #       legitimate quote field into the profile.
    #   (3) Free-string fields are sanitized (newlines stripped, length
    #       capped) before any later persistence.
    def _ground_fact(f: dict) -> bool:
        if not _quote_in_notes(f.get("quote", ""), notes):
            return False
        if not _value_in_notes(f.get("value"), notes):
            return False
        # Sanitize the value for free-string fields in place
        if f.get("field_path") in _FREE_STRING_FIELDS:
            f["value"] = _sanitize_free_string(f.get("value"))
        return True

    def _ground_decision(d: dict) -> bool:
        if not _quote_in_notes(d.get("quote", ""), notes):
            return False
        if not _value_in_notes(d.get("value"), notes):
            return False
        # Decision value/rationale are user-visible prose — sanitize length/charset
        d["value"] = _sanitize_free_string(d.get("value")) or d.get("value")
        if d.get("rationale"):
            d["rationale"] = _sanitize_free_string(d.get("rationale"))
        return True

    facts = [f for f in facts if _ground_fact(f)]
    decisions = [d for d in decisions if _ground_decision(d)]

    # Action items + open_questions don't have a quote/value to ground (they're
    # the model's synthesis), but their text reaches the prompt unfenced. At
    # minimum sanitize newlines/length so an injected payload can't ride along.
    for a in action_items:
        if a.get("text"):
            a["text"] = _sanitize_free_string(a.get("text"))
    for q in open_questions:
        if q.get("text"):
            q["text"] = _sanitize_free_string(q.get("text"))

    # Tag each preview row with a stable id so the frontend can confirm
    # subsets of the preview by id.
    preview_id = str(uuid.uuid4())
    for i, f in enumerate(facts):
        f["row_id"] = f"f-{i}"
    for i, d in enumerate(decisions):
        d["row_id"] = f"d-{i}"
    for i, a in enumerate(action_items):
        a["row_id"] = f"a-{i}"
    for i, q in enumerate(open_questions):
        q["row_id"] = f"q-{i}"

    preview = {
        "preview_id": preview_id,
        "customer_id": profile.customer_id,
        "customer_display_name": profile.customer_display_name,
        "lob_id": profile.lob_id,
        "lob_display_name": profile.lob_display_name,
        "notes_excerpt": notes[:600] + ("…" if len(notes) > 600 else ""),
        "facts": facts,
        "decisions": decisions,
        "action_items": action_items,
        "open_questions": open_questions,
        "counts": {
            "facts": len(facts),
            "decisions": len(decisions),
            "action_items": len(action_items),
            "open_questions": len(open_questions),
        },
    }

    logger.info(
        f"meeting_notes_node: preview {preview_id} "
        f"facts={len(facts)} decisions={len(decisions)} "
        f"actions={len(action_items)} qs={len(open_questions)}"
    )
    return {"meeting_preview": preview}


# ---------------------------------------------------------------------------
# Node 2: merge confirmed subset into profile
# ---------------------------------------------------------------------------

def meeting_merge_node(state: AgentState) -> dict:
    """Apply the SA-confirmed subset of a meeting-notes preview into the
    bound customer profile.

    Inputs (set by AgentCore entrypoint when payload kind == meeting_merge):
      state["meeting_preview"]   : full preview object from a previous
                                   meeting_notes_node run
      state["meeting_confirmed_ids"] : list of row_ids the SA ticked
                                       (e.g. ["f-0","f-2","d-1","a-0"])

    Anything not in confirmed_ids is dropped. Nothing else changes about
    the conversation or the profile.
    """
    preview = state.get("meeting_preview") or {}
    confirmed: set[str] = set(state.get("meeting_confirmed_ids") or [])
    profile: CustomerProfile = state.get("customer_profile")
    turn = state.get("turn", 0)

    if not preview or profile is None:
        return {"meeting_merge_result": {"error": "no preview or profile"}}
    if not confirmed:
        return {"meeting_merge_result": {"applied": 0, "note": "nothing confirmed"}}

    # Apply confirmed facts
    applied_facts = 0
    contradictions: list[dict] = []
    for f in preview.get("facts", []):
        if f.get("row_id") not in confirmed:
            continue
        fp = f.get("field_path")
        val = f.get("value")
        if not fp or val is None:
            continue
        try:
            status, old = profile.apply_fact(fp, val, turn)
        except AttributeError:
            logger.warning(f"meeting_merge: bad field_path {fp}")
            continue
        if status in ("set", "extended"):
            applied_facts += 1
        elif status == "contradicts":
            contradictions.append({
                "field_path": fp, "old_value": old, "new_value": val,
                "rationale": f"Meeting notes claim {val}; profile had {old}.",
            })

    # Apply confirmed decisions
    applied_decisions = 0
    for d in preview.get("decisions", []):
        if d.get("row_id") not in confirmed:
            continue
        cat = d.get("category")
        val = d.get("value")
        if not cat or not val:
            continue
        profile.add_decision(cat, val, d.get("rationale", ""), turn)
        applied_decisions += 1

    # Action items + open questions: track as open_questions in profile
    # (no separate action-item store yet — that's item 4.4)
    new_qs: list[str] = []
    for a in preview.get("action_items", []):
        if a.get("row_id") not in confirmed:
            continue
        owner = a.get("owner") or "unspecified"
        text = a.get("text") or ""
        if text:
            new_qs.append(f"[Action — {owner}] {text}".strip())
    for q in preview.get("open_questions", []):
        if q.get("row_id") not in confirmed:
            continue
        text = q.get("text") or ""
        if text:
            new_qs.append(text)
    if new_qs:
        # Cap the queue to avoid unbounded growth
        profile.open_questions = (profile.open_questions + new_qs)[-20:]

    # Persist
    import time
    if applied_facts or applied_decisions or new_qs:
        profile.version += 1
        profile.updated_at = time.time()
        try:
            upsert_profile(profile)
        except Exception as e:
            logger.error(f"meeting_merge: persist failed: {e}")
            return {"meeting_merge_result": {"error": f"persist failed: {e}"}}

    # Audit row — meeting events get tagged so audit reviewers can spot them
    write_turn_event(
        profile, turn,
        user_query="(meeting notes merged)",
        facts_extracted=[f for f in preview.get("facts", []) if f.get("row_id") in confirmed],
        decisions_extracted=[d for d in preview.get("decisions", []) if d.get("row_id") in confirmed],
        contradictions=contradictions,
        response_text=f"Merged {applied_facts} fact(s), {applied_decisions} decision(s), {len(new_qs)} question(s) from meeting notes.",
        open_question_added="; ".join(new_qs)[:1000],
        open_questions_dropped=[],
    )

    result = {
        "applied_facts": applied_facts,
        "applied_decisions": applied_decisions,
        "applied_questions": len(new_qs),
        "contradictions": contradictions,
        "preview_id": preview.get("preview_id", ""),
    }
    logger.info(f"meeting_merge_node: {result}")
    return {
        "meeting_merge_result": result,
        "customer_profile": profile,
        "phase": profile.derive_phase(),
        "open_questions": list(profile.open_questions),
    }
