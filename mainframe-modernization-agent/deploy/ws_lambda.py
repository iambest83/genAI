"""WebSocket Lambda — thin SSE relay between WebSocket API and AgentCore.

After Iteration 1.1 (Fix Streaming Bypass), this Lambda has one job:
forward each SSE event from AgentCore to the WebSocket client.

Responsibilities:
  1. WebSocket connection management (connect/disconnect via DynamoDB)
  2. Optional API-key gate for sendMessage
  3. Pass session identity (sa_id, customer_id, turn) into the AgentCore
     payload so the graph's profile_loader / profile_updater work
  4. Stream AgentCore SSE events back to the client unchanged

All intelligence — routing, KB retrieval, MCP tool calls, response
generation, customer profile, artifacts — runs INSIDE AgentCore. The
Lambda no longer calls Bedrock directly and no longer holds a system
prompt.
"""
import json
import os
import boto3

CONNECTIONS_TABLE = os.environ.get("CONNECTIONS_TABLE", "MfModAgent-WsConnections")
API_KEY = os.environ.get("API_KEY", "")
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/MfModAgent-<RUNTIME_ID>",
)
REGION = os.environ.get("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(CONNECTIONS_TABLE)
agentcore = boto3.client("bedrock-agentcore", region_name=REGION)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    route_key = event.get("requestContext", {}).get("routeKey", "")
    conn_id = event.get("requestContext", {}).get("connectionId", "")
    domain = event.get("requestContext", {}).get("domainName", "")
    stage = event.get("requestContext", {}).get("stage", "")
    raw_body = event.get("body", "") or ""

    # API Gateway sends frames whose `action` doesn't match a named route
    # to the $default route. Recover the original action from the body so
    # the dispatch table below can match. (Named routes like sendMessage
    # still arrive with their literal route_key and bypass this.)
    if route_key == "$default":
        try:
            _body = json.loads(raw_body)
            route_key = _body.get("action", "$default") or "$default"
        except Exception:
            pass

    print(f"[ws_lambda] routeKey={route_key!r} conn={conn_id} body_len={len(raw_body)}")

    if route_key == "$connect":
        # Minimal connect record. sa_id is client-supplied, not derived from
        # a verified JWT — see ARCHITECTURE.md §11 for the tracked auth gap.
        # Optional ?customer=... and ?lob=... query-string preselection.
        qs = event.get("queryStringParameters") or {}
        customer_display = qs.get("customer", "") or ""
        lob_display = qs.get("lob", "") or ""

        item = {
            "connectionId": conn_id,
            "sa_id": "anonymous",
            "customer_id": "default",
            "customer_display_name": "",
            "lob_id": "default",
            "lob_display_name": "",
            "turn": 0,
        }
        if customer_display:
            cust_id, cust_disp = _make_customer_id(customer_display)
            item["customer_id"] = cust_id
            item["customer_display_name"] = cust_disp
        if lob_display:
            lob_id, lob_disp = _make_lob_id(lob_display)
            item["lob_id"] = lob_id
            item["lob_display_name"] = lob_disp

        table.put_item(Item=item)
        return {"statusCode": 200}

    if route_key == "$disconnect":
        table.delete_item(Key={"connectionId": conn_id})
        return {"statusCode": 200}

    if route_key == "whoami":
        # Return the connectionId so the frontend can include it on
        # out-of-band HTTP calls (e.g. the recording upload-URL endpoint
        # in item 4.2). The browser doesn't otherwise know its own
        # connection id.
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        _send(apigw, conn_id, {"type": "whoami", "conn_id": conn_id})
        return {"statusCode": 200}

    if route_key == "sendMessage":
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)

        try:
            body = json.loads(event.get("body", "{}"))
        except Exception:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid JSON"})
            return {"statusCode": 400}

        prompt = body.get("prompt", "")
        api_key = body.get("api_key", "")
        if API_KEY and api_key != API_KEY:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid API key"})
            return {"statusCode": 401}
        if not prompt:
            _send(apigw, conn_id, {"type": "error", "message": "Missing prompt"})
            return {"statusCode": 400}

        session = _resolve_session(conn_id)
        turn = _increment_turn(conn_id)

        _stream_from_agentcore(apigw, conn_id, prompt, session=session, turn=turn)
        return {"statusCode": 200}

    if route_key == "whatDoYouKnow":
        # Explicit WS action that mirrors the "what do you know" trigger
        # phrase. Useful for a frontend button. Re-enters the graph with
        # a canned prompt so the router classifies as route="summary".
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)

        session = _resolve_session(conn_id)
        turn = _increment_turn(conn_id)
        _stream_from_agentcore(apigw, conn_id, "what do you know", session=session, turn=turn)
        return {"statusCode": 200}

    if route_key == "selectCustomer":
        # Optional binding (per locked decision: customer selection is optional).
        # Selecting a customer also resets the LoB to "default" so the SA
        # gets a clean start unless they bind a LoB explicitly.
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        try:
            body = json.loads(event.get("body", "{}"))
        except Exception:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid JSON"})
            return {"statusCode": 400}

        display = (body.get("customer_display_name") or "").strip()
        if not display:
            _send(apigw, conn_id,
                  {"type": "error", "message": "customer_display_name required"})
            return {"statusCode": 400}

        binding = _rebind_customer(conn_id, display)
        _send(apigw, conn_id, {"type": "customer_bound", **binding})
        return {"statusCode": 200}

    if route_key == "selectLob":
        # Bind a Line of Business inside the currently bound customer. LoBs
        # are scoped to the customer; selecting a new LoB does NOT affect
        # the customer binding.
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        try:
            body = json.loads(event.get("body", "{}"))
        except Exception:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid JSON"})
            return {"statusCode": 400}

        display = (body.get("lob_display_name") or "").strip()
        if not display:
            _send(apigw, conn_id,
                  {"type": "error", "message": "lob_display_name required"})
            return {"statusCode": 400}

        binding = _rebind_lob(conn_id, display)
        _send(apigw, conn_id, {"type": "lob_bound", **binding})
        return {"statusCode": 200}

    if route_key == "submitMeetingNotes":
        # Listen-mode (4.1): SA pasted meeting notes / a transcript and is
        # asking the agent to extract a structured preview. Customer must
        # be bound. Notes only — no audio.
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        try:
            body = json.loads(raw_body)
        except Exception:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid JSON"})
            return {"statusCode": 400}

        notes_text = (body.get("notes_text") or "").strip()
        if not notes_text:
            _send(apigw, conn_id,
                  {"type": "error", "message": "notes_text required"})
            return {"statusCode": 400}

        session = _resolve_session(conn_id)
        # Refuse if no customer is bound — preview always belongs to a
        # specific customer.
        if (session.get("customer_id") or "default") in ("default", "", None):
            _send(apigw, conn_id, {
                "type": "error",
                "message": "Pick a Customer (and optionally a LoB) before "
                           "pasting meeting notes.",
            })
            return {"statusCode": 400}

        turn = _increment_turn(conn_id)
        _stream_from_agentcore(
            apigw, conn_id,
            session=session, turn=turn,
            extra={"kind": "meeting_notes", "notes_text": notes_text},
        )
        return {"statusCode": 200}

    if route_key == "confirmMeetingMerge":
        # Listen-mode (4.1): SA reviewed a preview, ticked some rows,
        # and is now asking us to merge the confirmed subset into the
        # bound profile.
        endpoint = f"https://{domain}/{stage}"
        apigw = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        try:
            body = json.loads(raw_body)
        except Exception:
            _send(apigw, conn_id, {"type": "error", "message": "Invalid JSON"})
            return {"statusCode": 400}

        preview = body.get("preview") or {}
        confirmed_ids = body.get("confirmed_ids") or []
        if not preview or not isinstance(confirmed_ids, list):
            _send(apigw, conn_id, {
                "type": "error",
                "message": "preview + confirmed_ids required",
            })
            return {"statusCode": 400}

        session = _resolve_session(conn_id)
        turn = _increment_turn(conn_id)
        _stream_from_agentcore(
            apigw, conn_id,
            session=session, turn=turn,
            extra={
                "kind": "meeting_merge",
                "preview": preview,
                "confirmed_ids": confirmed_ids,
            },
        )
        return {"statusCode": 200}

    return {"statusCode": 400}


# ---------------------------------------------------------------------------
# Streaming relay
# ---------------------------------------------------------------------------

def _stream_from_agentcore(apigw, conn_id, prompt=None, *, session, turn, extra=None):
    """Invoke AgentCore and forward each SSE event to the WebSocket.

    `prompt` is used for chat turns. For non-chat payload kinds (e.g.
    meeting_notes, meeting_merge) the caller passes `extra={"kind": ...,
    ...payload_specific_fields}` instead and `prompt` is omitted.
    """
    base = {
        "sa_id": session.get("sa_id") or "anonymous",
        "customer_id": session.get("customer_id") or "default",
        "customer_display_name": session.get("customer_display_name") or "",
        "lob_id": session.get("lob_id") or "default",
        "lob_display_name": session.get("lob_display_name") or "",
        "turn": turn,
    }
    if prompt is not None:
        base["prompt"] = prompt
    if extra:
        base.update(extra)
    payload = json.dumps(base).encode("utf-8")

    try:
        response = agentcore.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            payload=payload,
        )
    except Exception as e:
        _send(apigw, conn_id, {"type": "error", "message": f"AgentCore invoke failed: {e}"})
        return

    content_type = (response.get("contentType") or "").lower()
    body = response.get("response") or response.get("body") or response.get("payload")

    # Streaming path — AgentCore returns text/event-stream when the
    # entrypoint is an async generator. Each line: "data: <json>\n\n".
    if "text/event-stream" in content_type and hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if not line:
                continue
            try:
                line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            except Exception:
                continue
            if not line_str.startswith("data: "):
                continue
            try:
                event_data = json.loads(line_str[6:])
            except json.JSONDecodeError:
                continue
            _send(apigw, conn_id, event_data)
            if event_data.get("type") in ("done", "error"):
                # Final event already forwarded; stop reading.
                return
        return

    # Non-streaming fallback (older AgentCore SDK or sync entrypoint):
    # read the whole body, surface as a single event so the UI doesn't hang.
    raw = ""
    try:
        if hasattr(body, "read"):
            raw = body.read().decode("utf-8")
        elif body is not None:
            raw = str(body)
    except Exception as e:
        _send(apigw, conn_id, {"type": "error", "message": f"Read response failed: {e}"})
        return

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"text": raw}

    if isinstance(parsed, dict) and parsed.get("type"):
        _send(apigw, conn_id, parsed)
    else:
        _send(apigw, conn_id, {"type": "token", "text": str(parsed)})
    _send(apigw, conn_id, {"type": "done"})


# ---------------------------------------------------------------------------
# Session helpers (kept inline so the Lambda has zero internal-package deps)
# ---------------------------------------------------------------------------

def _resolve_session(conn_id: str) -> dict:
    resp = table.get_item(Key={"connectionId": conn_id})
    return resp.get("Item") or {}


def _increment_turn(conn_id: str) -> int:
    resp = table.update_item(
        Key={"connectionId": conn_id},
        UpdateExpression="ADD turn :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["turn"])


def _make_customer_id(display_name: str) -> tuple[str, str]:
    """Slug + short hash → stable customer_id. Returns (id, normalized display).

    The hash is taken over the SLUG (not the raw display) so that
    "JPMC", "jpmc", and "Jpmc " all collapse to the same customer_id.
    Without this, the SA would create a fresh empty profile every time
    they spelled a customer name with different casing or whitespace.
    """
    import hashlib
    import re
    disp = (display_name or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", disp.lower()).strip("-") or "unknown"
    tail = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{tail}", disp


def _make_lob_id(display_name: str) -> tuple[str, str]:
    """Slug-only LoB id (no hash — LoB names are short and human-friendly).
    Returns (id, normalized display)."""
    import re
    disp = (display_name or "").strip()
    slug = re.sub(r"[^a-z0-9]+", "-", disp.lower()).strip("-") or "default"
    return slug, disp


def _rebind_customer(conn_id: str, display_name: str) -> dict:
    """Bind/rebind the customer for this connection. Resets the LoB to
    'default' so the SA gets a clean start until they pick an LoB."""
    customer_id, disp = _make_customer_id(display_name)
    table.update_item(
        Key={"connectionId": conn_id},
        UpdateExpression=("SET customer_id = :c, customer_display_name = :n, "
                          "lob_id = :ld, lob_display_name = :le"),
        ExpressionAttributeValues={
            ":c": customer_id, ":n": disp,
            ":ld": "default", ":le": "",
        },
    )
    return {
        "customer_id": customer_id,
        "customer_display_name": disp,
        "lob_id": "default",
        "lob_display_name": "",
    }


def _rebind_lob(conn_id: str, display_name: str) -> dict:
    """Bind/rebind the LoB for this connection (within the bound customer)."""
    lob_id, disp = _make_lob_id(display_name)
    table.update_item(
        Key={"connectionId": conn_id},
        UpdateExpression="SET lob_id = :l, lob_display_name = :n",
        ExpressionAttributeValues={":l": lob_id, ":n": disp},
    )
    return {"lob_id": lob_id, "lob_display_name": disp}


def _send(apigw, conn_id, data):
    try:
        apigw.post_to_connection(
            ConnectionId=conn_id, Data=json.dumps(data).encode()
        )
    except Exception:
        # Connection may have closed; swallow so caller can keep streaming.
        pass
