"""Intent classification for incoming SA messages.

Replaces the old "direct vs kb vs mcp" routing decision with a richer
five-way intent. Most turns fall into `substantive`; the others handle
conversation flow correctly.

Intents:
  substantive   — Real question / fact / decision. Run the full graph.
  defer         — SA is skipping the agent's prior probe ("not now",
                  "skip", "later", "not at this point"). Drops the open
                  question, no retrieval, no probe.
  acknowledge   — SA is acknowledging without new content ("thanks",
                  "got it", "noted"). Tiny ack, no retrieval, no probe.
  meta_help     — SA is asking *about* the assistant ("what can you do",
                  "help", "how do you work"). Emits the intro deck.
  meta_summary  — SA is asking what the agent knows ("what do you know",
                  "where are we", "recap"). Emits the profile summary.
  chat          — Bare greeting only ("hi", "hello", "hola"). Tiny
                  acknowledgement that does NOT reproduce the intro.

Two-tier classification:
  1. Cheap-path heuristics catch the obvious cases (substantive question
     with domain keywords, exact-match greetings/help/summary triggers,
     short defer/ack matches).
  2. Anything that's still ambiguous (~5% of turns — short replies that
     could be defer, ack, or substantive depending on context) goes
     through Haiku with the prior-turn probe in context.
"""
from __future__ import annotations

import json
import logging
import re

from .config import get_router_llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cheap-path keyword sets
# ---------------------------------------------------------------------------

# Bare greetings — full message must be (just the greeting + optional punctuation
# + optional 1-word tail like "there"/"all"). Anything longer is treated as
# substantive (the greeting is just polite framing).
_GREETING_TOKENS = {
    "hi", "hello", "hola", "hey", "yo", "sup", "bye", "goodbye", "cya",
}

# Explicit ask for what the agent is / does. Only THESE trigger the intro.
_HELP_TRIGGERS = [
    "what can you do", "what do you do", "who are you", "how do you work",
    "help me get started", "how does this work", "what is this", "/help",
    "show me what you can do",
]

# Profile-summary triggers (already in router.py SUMMARY_TRIGGERS — duplicated
# here so this module is a single source for intent classification).
_SUMMARY_TRIGGERS = [
    "what do you know", "what does this profile", "what's in the profile",
    "show the profile", "show me the profile", "summarize what",
    "summarize the profile", "summary of what", "where are we", "recap",
    "what have we discussed", "what have we covered", "what we know so far",
]

# Deferral phrases — short and idiomatic. SAs use a wide variety of these
# in real conversations; expanding aggressively because the cost of a
# false-defer (drops a question that wasn't actually being deferred) is
# small (Sonnet will probe again), while the cost of a missed-defer
# (treating a deferral as substantive or as chat-greeting) is the bug
# users have been hitting.
_DEFER_PHRASES = [
    # explicit defers
    "not now", "not at this point", "not yet", "not right now",
    "skip", "skip that", "skip this", "skip it", "pass",
    "later", "for later", "come back to that", "park that",
    "move on", "next", "next question", "ignore that",
    "drop it", "drop that", "leave it", "leave that aside",
    "let's skip", "lets skip", "set that aside", "park it",
    # uncertainty / "I don't know" — equivalent to defer in this product
    "not sure", "not sure on that", "not sure about that",
    "no idea", "don't know", "dont know", "i don't know", "i dont know",
    "unclear", "unsure", "tbd", "to be determined",
    "haven't decided", "havent decided", "still deciding",
    "need to check", "need to confirm", "let me check", "let me get back",
    "i'll get back", "ill get back", "get back to you",
    # short negatives — when used alone or in tiny phrases, equivalent to defer
    "no", "nope", "nah", "na", "no clue",
]

# Acknowledgement phrases — short, no question, no new content.
# Per FIXES.md P21:
#   - Bare 'yes/yeah/yep/agreed' are removed: when they answer a probe like
#     "is this near-term?" they are SUBSTANTIVE (carry information). The
#     prompt's own rule says short probe-answers are substantive. The cheap
#     path runs before the LLM, so leaving them here drops them silently.
#     If the SA writes a bare 'yes' with no prior_probe, _llm_classify will
#     classify it (defaults to substantive on error, which is the safe fall).
#   - 'no/nope' removed: they live in _DEFER_PHRASES already. Keeping them
#     in both lists "worked" only because step 4 (defer) runs before step 5
#     (ack). Correctness should not depend on step ordering.
_ACK_PHRASES = [
    "thanks", "thank you", "thx", "ty", "cheers",
    "got it", "noted", "ok", "okay", "k", "kk", "great",
    "sounds good", "makes sense", "perfect", "nice",
]

# Substantive content signals — if any appear in the message, force substantive.
# (Domain-specific terms, anything that looks like a real ask.)
_SUBSTANTIVE_SIGNALS = re.compile(
    r"\b(cobol|jcl|cics|ims|db2|vsam|mq|racf|aws|"
    r"migrate|migration|rehost|replatform|refactor|modern|"
    r"customer|partner|sox|pci|fdic|occ|glba|ffiec|dora|"
    r"workload|pattern|architect|design|cost|tco|estimate|"
    r"how|why|when|where|which|compare|recommend|should|could|would|"
    r"draft|create|generate|outline|build|propose"
    r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Cheap-path classifier
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Lowercase + strip outer whitespace + collapse internal whitespace."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _strip_punct(s: str) -> str:
    return re.sub(r"[!?.,…]+$", "", s).strip()


def _cheap_classify(message: str) -> str | None:
    """Return an intent label or None if cheap-path is unsure.

    Returns one of: substantive | defer | acknowledge | meta_help |
    meta_summary | chat | None
    """
    if not message:
        return None

    norm = _normalize(message)
    bare = _strip_punct(norm)
    word_count = len(norm.split())

    # 1. Help triggers — must be explicit. Substring match is fine because
    #    these phrases are very specific.
    if any(t in norm for t in _HELP_TRIGGERS):
        return "meta_help"

    # 2. Summary triggers — same.
    if any(t in norm for t in _SUMMARY_TRIGGERS):
        return "meta_summary"

    # 3. Bare greeting — single-word OR greeting + tiny tail
    first_word = norm.split()[0] if norm else ""
    if word_count == 1 and first_word in _GREETING_TOKENS:
        return "chat"
    if word_count <= 3 and first_word in _GREETING_TOKENS:
        # "hi there" / "hello all" / "hola amigo" — still chat, not substantive
        # BUT if there's any question or domain word, treat as substantive
        if not _SUBSTANTIVE_SIGNALS.search(norm) and "?" not in norm:
            return "chat"

    # 4. Defer — careful three-tier rule. The challenge: single-word
    #    defer tokens like "no" / "nah" must not hijack longer messages
    #    that happen to START with them but actually carry substantive
    #    content (e.g. "no it is a long term plan" — that's a real answer,
    #    not a deferral).
    #
    #    4a: whole-message exact match against any defer phrase
    #    4b: message contains a MULTI-WORD defer phrase as a substring
    #        (e.g. "not sure on that", "no idea", "i don't know")
    #    4c: single-word defer head only if message is ≤3 words AND
    #        carries no substantive content
    if word_count <= 10 and not _SUBSTANTIVE_SIGNALS.search(norm) and "?" not in norm:
        if bare in _DEFER_PHRASES:
            return "defer"
        multi_word_defers = [p for p in _DEFER_PHRASES if " " in p]
        if any(p in bare for p in multi_word_defers):
            return "defer"
        if word_count <= 3:
            first_token = re.split(r"[\s,.!?;:]+", bare, maxsplit=1)[0]
            single_word_defers = {p for p in _DEFER_PHRASES if " " not in p}
            if first_token in single_word_defers:
                return "defer"

    # 5. Acknowledge — short + matches a known ack phrase
    if word_count <= 4:
        # whole-message exact match
        if bare in _ACK_PHRASES:
            return "acknowledge"
        # 2-word combo like "got it" / "thanks!" / "makes sense"
        if any(p == bare for p in _ACK_PHRASES):
            return "acknowledge"

    # 6. Has clear domain signals OR is long enough → substantive
    if _SUBSTANTIVE_SIGNALS.search(norm) or word_count >= 6 or "?" in norm:
        return "substantive"

    # Anything left is short and ambiguous — escalate
    return None


# ---------------------------------------------------------------------------
# LLM fallback (Haiku) — only for the genuinely ambiguous middle
# ---------------------------------------------------------------------------

_LLM_INTENT_PROMPT = """You are an intent classifier for a mainframe-modernization
assistant. The Solutions Architect (SA) just said something. Classify it as
exactly ONE of these intents.

  substantive  — A real question, fact, or decision the agent should engage with
                 (with retrieval). Even short answers to a prior probe
                 (e.g. "Cards", "P&C", "800 programs") are substantive — they
                 carry information. A bare "yes"/"yeah"/"yep" replying to a
                 yes/no probe is also substantive (it answers the question);
                 do NOT classify as acknowledge in that case.
  defer        — The SA is putting the prior question aside or admitting
                 they don't know yet. Examples:
                   "not now" / "skip" / "later" / "not at this point" /
                   "drop it" / "park it" /
                   "not sure" / "no idea" / "don't know" / "unclear" /
                   "tbd" / "haven't decided" / "need to check" /
                   "let me get back to you" / a bare "no" or "nope"
                 If the SA isn't ready to answer the prior question, it's
                 a defer — NOT chat, NOT acknowledge.
  acknowledge  — The SA is acknowledging the agent's prior reply without
                 adding content ("got it", "thanks", "noted", "ok",
                 "makes sense").
  meta_help    — The SA is asking ABOUT the assistant ("what can you do",
                 "help", "how do you work").
  meta_summary — The SA is asking what the agent knows so far ("where are
                 we", "recap", "summarize", "what do you know").
  chat         — VERY narrow: a bare greeting word with NO other content.
                 ONLY "hi" / "hello" / "hey" / "hola" alone qualify as chat.
                 Anything else — even a short hesitation, a "no", a
                 typo'd "nah" — is NOT chat.

CRITICAL RULES:
- If the SA's message is short and not a question, it is almost certainly
  one of: substantive, defer, acknowledge. NOT chat.
- If the AGENT'S PRIOR PROBE is non-empty, the SA's reply is responding
  to it. Never classify as chat in that case.
- "no" / "nope" / "nah" / "na" alone or as a sentence start almost always
  means defer (SA can't or won't answer the question).

PRIOR PROBE (the question the agent asked the SA on the previous turn):
{prior_probe_block}

SA message:
{message}

Reply with ONE word only — one of:
substantive | defer | acknowledge | meta_help | meta_summary | chat
"""


_VALID_INTENTS = {
    "substantive", "defer", "acknowledge", "meta_help", "meta_summary", "chat",
}


def _llm_classify(message: str, prior_probe: str = "") -> str:
    """Use Haiku to classify a short ambiguous message.

    Falls back to "substantive" on any error — the safest default since
    substantive runs the full graph and the SA gets a useful answer.
    """
    try:
        llm = get_router_llm()
        prior_block = (
            prior_probe.strip() if prior_probe and prior_probe.strip()
            else "(none — fresh turn, no pending question)"
        )
        prompt = _LLM_INTENT_PROMPT.format(
            message=message[:1500],   # cap defensively
            prior_probe_block=prior_block,
        )
        resp = llm.invoke(prompt).content.strip().lower()
        # Pull the first matching intent token from the response
        for token in _VALID_INTENTS:
            if token in resp:
                return token
        logger.warning(f"intent: LLM returned no valid token: {resp!r}; defaulting substantive")
        return "substantive"
    except Exception as e:
        logger.warning(f"intent: LLM classify failed: {e}; defaulting substantive")
        return "substantive"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Words/tokens that strongly suggest the SA is deferring or admitting they
# don't know. Used as a defensive coercion when the LLM mis-labels a short
# non-question reply as chat. Word-boundary regex so "north" doesn't match "no".
_DEFER_SIGNAL_RE = re.compile(
    r"\b(no|not|nope|nah|na|unsure|unclear|tbd|dont|don't|"
    r"haven't|havent|skip|later|pass|idk)\b",
    re.IGNORECASE,
)

# Chat is a VERY narrow category — the message must literally start with a
# greeting token. Anything else the LLM labeled "chat" is almost always a
# reply to the prior probe (substantive) or a deferral.
_GREETING_START_RE = re.compile(
    r"^\s*(hi|hello|hola|hey|yo|sup|bye|goodbye|cya)\b",
    re.IGNORECASE,
)


def classify_intent(message: str, prior_probe: str = "") -> str:
    """Top-level entry point.

    Returns one of: substantive | defer | acknowledge | meta_help |
    meta_summary | chat
    """
    cheap = _cheap_classify(message)
    if cheap is not None:
        logger.info(f"intent: cheap-path → {cheap}")
        return cheap

    intent = _llm_classify(message, prior_probe)

    # Defensive coercion: the LLM is the only path that can produce "chat",
    # and Haiku has a known failure mode of labeling short non-greeting
    # replies (single words like "strategic", short phrases like
    # "na, not sure on that") as chat. Chat should ONLY stick when the
    # message literally starts with a greeting token.
    norm = (message or "").strip().lower()
    word_count = len(norm.split())
    if intent == "chat" and not _GREETING_START_RE.match(message or ""):
        if "?" in (message or ""):
            coerced = "substantive"
        elif word_count <= 2 and _DEFER_SIGNAL_RE.search(message or ""):
            # Only treat as defer when the message is very short
            # (1-2 words, e.g. "nope", "tbd", "no idea"). Longer replies
            # like "no this is critical" or "no it is a long term plan"
            # carry content and should be substantive.
            coerced = "defer"
        else:
            # Replying to a probe, or a one-word fact like "strategic" /
            # "Cards" / "P&C" — substantive carries info, runs the full graph.
            coerced = "substantive"
        logger.info(
            f"intent: LLM said chat but message {message!r} isn't a greeting "
            f"— coercing to {coerced}"
        )
        intent = coerced

    logger.info(f"intent: LLM → {intent}")
    return intent
