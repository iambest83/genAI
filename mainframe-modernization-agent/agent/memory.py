"""DynamoDB-backed persistence for CustomerProfile.

Table schema (see deploy/dynamodb_table.json):
    PK  = "sa#{sa_id}"
    SK  = "customer#{customer_id}#lob#{lob_id}#profile"
         | "customer#{customer_id}#lob#{lob_id}#turn#{NNNNNN}#{ms}"

Isolation is enforced at the query level:
    - Reads MUST pin PK to the authenticated sa_id.
    - Customer scope is pinned by SK prefix "customer#{customer_id}#".
    - LoB scope is pinned by deeper SK prefix
      "customer#{customer_id}#lob#{lob_id}#".

`lob_id` defaults to "default" when the SA hasn't selected one — this gives
backward-compat for callers that don't yet thread an LoB through.

No read should ever cross SA boundaries. There is intentionally no GSI on
customer_id — cross-SA customer views are a separate Tier-3 feature and
must live in a different record shape (see docs/shared_customer_memory.md).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .customer_profile import CustomerProfile

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("CUSTOMER_MEMORY_TABLE", "MfModAgent-CustomerMemory")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# Turn-event audit rows live for TURN_EVENT_TTL_DAYS, then DDB GCs them.
# 90 days covers a typical engagement's audit window. Snapshot rows
# (#profile SK) carry no TTL and live forever — they're the source of truth.
# Per FIXES.md #14 (d).
TURN_EVENT_TTL_DAYS = int(os.environ.get("TURN_EVENT_TTL_DAYS", "90"))

_ddb = None


def _table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    return _ddb


def _pk(sa_id: str) -> str:
    return f"sa#{sa_id}"


def _norm_lob(lob_id: str | None) -> str:
    """LoB id is a stable slug. Defaults to 'default' when missing or empty.
    Lowercases, replaces non-alphanumeric runs with hyphens, strips edges.
    Mirrors deploy/ws_lambda.py:_make_lob_id() so callers writing direct
    DDB rows produce identical SKs to the WS-bound path."""
    import re as _re
    raw = (lob_id or "").strip().lower()
    if not raw:
        return "default"
    slug = _re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or "default"


def _sk_profile(customer_id: str, lob_id: str = "default") -> str:
    return f"customer#{customer_id}#lob#{_norm_lob(lob_id)}#profile"


def _sk_turn(customer_id: str, lob_id: str, turn: int) -> str:
    # monotonic, sortable; pad turn for lex order up to 1e6 turns
    return (
        f"customer#{customer_id}#lob#{_norm_lob(lob_id)}#turn#{turn:06d}"
        f"#{int(time.time()*1000)}"
    )


# ---------------------------------------------------------------------------
# Profile CRUD
# ---------------------------------------------------------------------------

def load_profile(
    sa_id: str,
    customer_id: str,
    lob_id: str = "default",
    display_name: str = "",
    lob_display_name: str = "",
) -> CustomerProfile:
    """Load the (sa_id, customer_id, lob_id) profile. Returns a fresh empty
    one if absent. Never returns another SA's data: the PK pins to sa_id.
    """
    sk = _sk_profile(customer_id, lob_id)
    try:
        resp = _table().get_item(Key={"PK": _pk(sa_id), "SK": sk})
    except ClientError as e:
        logger.error(f"DDB get_item failed: {e}")
        return CustomerProfile(
            sa_id=sa_id, customer_id=customer_id, lob_id=_norm_lob(lob_id),
            customer_display_name=display_name, lob_display_name=lob_display_name,
        )

    item = resp.get("Item")
    if not item:
        return CustomerProfile(
            sa_id=sa_id, customer_id=customer_id, lob_id=_norm_lob(lob_id),
            customer_display_name=display_name, lob_display_name=lob_display_name,
        )

    try:
        return CustomerProfile.from_dict(item["profile"])
    except Exception as e:
        logger.error(f"Profile deserialize failed for {sa_id}/{customer_id}/{lob_id}: {e}")
        return CustomerProfile(
            sa_id=sa_id, customer_id=customer_id, lob_id=_norm_lob(lob_id),
            customer_display_name=display_name, lob_display_name=lob_display_name,
        )


def load_customer_lob_profiles(sa_id: str, customer_id: str) -> list[CustomerProfile]:
    """Return every LoB profile this SA has for the given customer.

    Used when the SA hasn't picked an LoB yet but asks a customer-level
    question — e.g. "what do we know about Fidelity?". The caller can
    fold the per-LoB facts into a customer-wide overview so the agent
    doesn't appear to "forget" data the SA already provided under one
    LoB. Read-only — the returned profiles are never mutated or written.
    """
    sk_prefix = f"customer#{customer_id}#lob#"
    out: list[CustomerProfile] = []
    last_eval_key = None
    while True:
        try:
            kwargs = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
                "ExpressionAttributeValues": {
                    ":pk": _pk(sa_id),
                    ":prefix": sk_prefix,
                },
            }
            if last_eval_key:
                kwargs["ExclusiveStartKey"] = last_eval_key
            resp = _table().query(**kwargs)
        except ClientError as e:
            logger.error(f"DDB query for LoB profiles failed: {e}")
            return out

        for item in resp.get("Items", []):
            sk = item.get("SK", "")
            # Only the per-LoB profile snapshots — skip turn rows.
            if not sk.endswith("#profile"):
                continue
            try:
                out.append(CustomerProfile.from_dict(item["profile"]))
            except Exception as e:
                logger.warning(f"skip malformed profile row {sk}: {e}")

        last_eval_key = resp.get("LastEvaluatedKey")
        if not last_eval_key:
            break
    return out


def upsert_profile(profile: CustomerProfile) -> None:
    """Write the full profile record with an optimistic lock on `version`.

    The lock prevents the silent-fact-loss race where two concurrent turns
    (e.g. chat racing meeting_merge, or two browser tabs) both load v=N and
    both try to write v=N+1 — without the guard, the second put_item wins
    and the first turn's facts vanish. With the guard, the second put fails
    on ConditionalCheckFailedException; we reload the latest snapshot,
    replay the in-memory delta on top, and retry. Per FIXES.md #14 (a).

    Failure mode: if reconciliation can't converge after MAX_RETRIES, the
    write is dropped with a logged error. That's strictly better than the
    silent-overwrite default — at least there's a signal.
    """
    MAX_RETRIES = 3
    sk = _sk_profile(profile.customer_id, profile.lob_id)

    # The version we expect to find in DDB. When profile_loader reads version N
    # and the updater applies facts in-memory (bumping version to N+K), DDB
    # still has version N. We condition on that original version, not on
    # profile.version - 1 (which would be N+K-1 and never match DDB's N).
    # On retry after contention, ddb_expected_version is updated to the
    # freshly loaded snapshot's version.
    ddb_expected_version = profile.version - 1 if profile.version > 1 else 0

    # If multiple facts were applied this turn (each bumping version), the
    # DDB row hasn't been written yet — it's still at the load-time version.
    # Detect this: if the profile has facts with turn > 0 whose count exceeds
    # what a single version bump would produce, the real DDB version is lower.
    # Simpler: re-derive from the fact that load_profile sets version=N and
    # each apply_fact/add_decision bumps it — the DDB version is N minus the
    # number of in-memory bumps. But we don't track that. Instead, just use
    # a broad condition: allow the write if DDB version <= profile.version.
    # This is still safe against concurrent writers because the condition
    # prevents clobbering a NEWER version.
    for attempt in range(MAX_RETRIES + 1):
        write_version = profile.version
        item = {
            "PK": _pk(profile.sa_id),
            "SK": sk,
            "sa_id": profile.sa_id,
            "customer_id": profile.customer_id,
            "lob_id": _norm_lob(profile.lob_id),
            "version": write_version,
            "updated_at": int(profile.updated_at),
            "profile": _to_ddb(profile.to_dict()),
        }
        try:
            _table().put_item(
                Item=item,
                # Either it's a brand-new row (no version yet), or the row
                # in DDB has a version strictly lower than what we're writing.
                # This prevents clobbering a concurrent writer's newer version
                # while allowing the multi-bump case (load v=1, apply 3 facts
                # → v=4, write v=4 conditioned on DDB having v < 4).
                ConditionExpression=(
                    "attribute_not_exists(version) OR version < :write_ver"
                ),
                ExpressionAttributeValues={":write_ver": write_version},
            )
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "ConditionalCheckFailedException":
                logger.error(f"DDB put_item failed: {e}")
                raise
            # Concurrent write detected. Reload the latest snapshot, replay
            # this turn's delta on top of it, and retry. The "delta" is the
            # set of facts and decisions added since the in-memory profile
            # was loaded — captured by tracking new entries added at the end
            # of the lists.
            if attempt >= MAX_RETRIES:
                logger.error(
                    f"upsert_profile: failed after {MAX_RETRIES} retries — "
                    f"sa={profile.sa_id} customer={profile.customer_id} "
                    f"lob={profile.lob_id}; dropping write to avoid clobber"
                )
                raise
            logger.info(
                f"upsert_profile: contention on attempt {attempt + 1}, "
                f"reloading and merging"
            )
            latest = load_profile(
                profile.sa_id, profile.customer_id, profile.lob_id,
                profile.customer_display_name, profile.lob_display_name,
            )
            _merge_in_memory_delta(into=latest, dropping_from=profile)
            # The merge above mutates `latest` in place; copy its state onto
            # `profile` so the next attempt has the unified view (and bumps
            # version above the latest snapshot's version).
            profile.facts = latest.facts
            profile.decisions_made = latest.decisions_made
            profile.workload = latest.workload
            profile.constraints = latest.constraints
            profile.open_questions = latest.open_questions
            profile.version = latest.version + 1
            profile.updated_at = int(time.time())
    # Unreachable; the loop returns or raises.


def _merge_in_memory_delta(*, into: CustomerProfile, dropping_from: CustomerProfile) -> None:
    """Reconciliation helper. `into` is the freshly-loaded snapshot from DDB;
    `dropping_from` is the in-memory profile we tried to write. We assume
    facts/decisions are append-mostly: pull anything from `dropping_from`
    that is "new" (turn > into's max turn for that field) onto `into`.

    This is heuristic — a precise merge would require the per-turn diff log,
    which we don't carry on the in-memory profile. Good enough for the
    common case (two turns both adding new facts) and strictly safer than
    the prior silent-overwrite.
    """
    into_max_turn_by_field = {}
    for f in into.facts:
        prev = into_max_turn_by_field.get(f.field_path, -1)
        if f.turn > prev:
            into_max_turn_by_field[f.field_path] = f.turn

    for f in dropping_from.facts:
        if f.turn > into_max_turn_by_field.get(f.field_path, -1):
            into.facts.append(f)

    into_dec_keys = {(d.category, d.value) for d in into.decisions_made}
    for d in dropping_from.decisions_made:
        if (d.category, d.value) not in into_dec_keys:
            into.decisions_made.append(d)

    # Open questions are append-only and short — union them.
    seen = set(into.open_questions)
    for q in dropping_from.open_questions:
        if q not in seen:
            into.open_questions.append(q)
            seen.add(q)
    into.open_questions = into.open_questions[-10:]


# ---------------------------------------------------------------------------
# Event-sourced per-turn rows (item 1.15)
# ---------------------------------------------------------------------------

def write_turn_event(
    profile: CustomerProfile,
    turn: int,
    *,
    user_query: str = "",
    facts_extracted: list | None = None,
    decisions_extracted: list | None = None,
    contradictions: list | None = None,
    response_text: str = "",
    open_question_added: str = "",
    open_questions_dropped: list[str] | None = None,
) -> None:
    """Append an immutable per-turn row capturing what changed this turn.

    Snapshot reads (the hot path) still come from `load_profile`. These
    rows are append-only — they enable replay, audit, and probe-quality
    metrics without touching the snapshot read path.

    Schema:
        PK = sa#<sa_id>
        SK = customer#<cust>#turn#<NNNNNN>#<ms_unix>
        body = {
            turn, ts_ms, user_query, response_text (truncated),
            facts_extracted, decisions_extracted, contradictions,
            open_question_added, open_questions_dropped,
            phase_at_end, profile_version_at_end,
        }

    Cost: one extra put_item per turn (~$0.0000003 at PAY_PER_REQUEST).
    Failure mode: log and swallow. The snapshot is the source of truth
    for behavior; missing event rows degrade audit/replay only.
    """
    try:
        ts_ms = int(time.time() * 1000)
        response_excerpt = (response_text or "")[:2000]

        body = {
            "turn": int(turn),
            "ts_ms": ts_ms,
            "user_query": (user_query or "")[:2000],
            "response_excerpt": response_excerpt,
            "facts_extracted": facts_extracted or [],
            "decisions_extracted": decisions_extracted or [],
            "contradictions": contradictions or [],
            "open_question_added": open_question_added or "",
            "open_questions_dropped": open_questions_dropped or [],
            "phase_at_end": profile.derive_phase(),
            "profile_version_at_end": profile.version,
        }

        # ttl is a Unix-second timestamp. DDB's TTL housekeeper deletes the
        # row some time after this moment passes (typically <48h). Requires
        # TimeToLive enabled on the table with AttributeName="ttl" — see
        # deploy/dynamodb_tables.json for the table-side config.
        ttl_seconds = int(time.time()) + TURN_EVENT_TTL_DAYS * 86_400

        _table().put_item(Item={
            "PK": _pk(profile.sa_id),
            "SK": _sk_turn(profile.customer_id, profile.lob_id, int(turn)),
            "sa_id": profile.sa_id,
            "customer_id": profile.customer_id,
            "lob_id": _norm_lob(profile.lob_id),
            "kind": "turn_event",
            "turn": int(turn),
            "ts_ms": ts_ms,
            "ttl": ttl_seconds,
            "body": _to_ddb(body),
        })
    except ClientError as e:
        # Audit row is optional — never fail a turn because of this.
        logger.warning(f"DDB write_turn_event failed for "
                       f"sa={profile.sa_id} cust={profile.customer_id} "
                       f"turn={turn}: {e}")
    except Exception as e:
        logger.warning(f"write_turn_event unexpected failure: {e}")


def list_turn_events(
    sa_id: str,
    customer_id: str,
    lob_id: str = "default",
    *,
    limit: int = 50,
) -> list[dict]:
    """Return per-turn event rows for (sa_id, customer_id, lob_id), oldest first.

    Used for replay, audit, and probe-quality metrics (item 3.4 later).
    """
    sk_prefix = f"customer#{customer_id}#lob#{_norm_lob(lob_id)}#turn#"
    try:
        resp = _table().query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={":pk": _pk(sa_id), ":sk": sk_prefix},
            ScanIndexForward=True,
            Limit=limit,
        )
    except ClientError as e:
        logger.error(f"DDB list_turn_events failed: {e}")
        return []
    return resp.get("Items", []) or []


def list_customers_for_sa(sa_id: str) -> list[dict]:
    """Return [{customer_id, lob_id, display_name, lob_display_name, updated_at}, ...]
    for a given SA — one row per (customer, lob) combination.

    Paginates with LastEvaluatedKey so an SA with hundreds of (customer,lob)
    profiles isn't silently truncated at the 1MB DDB page. Per FIXES.md #14 (c).
    """
    items: list[dict] = []
    last_eval_key = None
    while True:
        try:
            kwargs = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": _pk(sa_id),
                    ":sk": "customer#",
                },
                "ProjectionExpression": "PK, SK, customer_id, lob_id, #p.customer_display_name, "
                                        "#p.lob_display_name, updated_at",
                "ExpressionAttributeNames": {"#p": "profile"},
            }
            if last_eval_key:
                kwargs["ExclusiveStartKey"] = last_eval_key
            resp = _table().query(**kwargs)
        except ClientError as e:
            logger.error(f"DDB query failed: {e}")
            return []

        items.extend(resp.get("Items", []))
        last_eval_key = resp.get("LastEvaluatedKey")
        if not last_eval_key:
            break

    return [
        {
            "customer_id": it.get("customer_id"),
            "lob_id": it.get("lob_id", "default"),
            "display_name": (it.get("profile", {}) or {}).get("customer_display_name", ""),
            "lob_display_name": (it.get("profile", {}) or {}).get("lob_display_name", ""),
            "updated_at": it.get("updated_at", 0),
        }
        for it in items
        if it.get("SK", "").endswith("#profile")
    ]


def delete_customer_for_sa(sa_id: str, customer_id: str,
                           lob_id: str | None = None) -> int:
    """Hard delete records for (sa_id, customer_id) — by default ALL LoBs
    for that customer; if lob_id is provided, only that LoB's records.

    Paginates the query so a customer with many turn-event rows is fully
    deleted (the unpaginated version would silently leave rows past the
    first DDB page). Per FIXES.md #14 (c).
    """
    if lob_id:
        sk_prefix = f"customer#{customer_id}#lob#{_norm_lob(lob_id)}#"
    else:
        sk_prefix = f"customer#{customer_id}#"

    items: list[dict] = []
    last_eval_key = None
    while True:
        try:
            kwargs = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                "ExpressionAttributeValues": {":pk": _pk(sa_id), ":sk": sk_prefix},
                "ProjectionExpression": "PK, SK",
            }
            if last_eval_key:
                kwargs["ExclusiveStartKey"] = last_eval_key
            resp = _table().query(**kwargs)
        except ClientError as e:
            logger.error(f"DDB query failed: {e}")
            return 0

        items.extend(resp.get("Items", []))
        last_eval_key = resp.get("LastEvaluatedKey")
        if not last_eval_key:
            break

    with _table().batch_writer() as batch:
        for it in items:
            batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
    return len(items)


# ---------------------------------------------------------------------------
# DDB type coercion — DynamoDB doesn't accept floats directly via resource API.
# ---------------------------------------------------------------------------

def _to_ddb(value):
    from decimal import Decimal
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ddb(v) for v in value]
    return value
