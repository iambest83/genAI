"""Transcribe-complete Lambda — drives the post-transcription flow (item 4.2).

Triggered by EventBridge on `aws.transcribe / Transcribe Job State
Change → COMPLETED`. Reads the transcript, formats it with speaker
labels, calls AgentCore with `kind=meeting_notes`, and pushes the
resulting preview back to the originating WS connection.
"""
import json
import os
import urllib.request

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
AGENT_RUNTIME_ARN = os.environ.get(
    "AGENT_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-east-1:<ACCOUNT_ID>:runtime/MfModAgent-<RUNTIME_ID>",
)
WS_ENDPOINT = os.environ.get("WS_ENDPOINT", "")  # https://<WS_API_ID>.execute-api...

transcribe = boto3.client("transcribe", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
apigw = boto3.client("apigatewaymanagementapi", endpoint_url=WS_ENDPOINT) if WS_ENDPOINT else None


def lambda_handler(event, context):
    detail = event.get("detail") or {}
    job_name = detail.get("TranscriptionJobName")
    if not job_name:
        return {"statusCode": 400, "error": "missing TranscriptionJobName"}
    if detail.get("TranscriptionJobStatus") != "COMPLETED":
        print(f"[transcribe_complete] skip job={job_name} status={detail.get('TranscriptionJobStatus')}")
        return {"statusCode": 200}

    # Fetch the job + tags so we know who to route the result to
    job = transcribe.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
    tags = {t["Key"]: t["Value"] for t in job.get("Tags") or []}
    sa_id = tags.get("sa_id") or "anonymous"
    customer_id = tags.get("customer_id") or "default"
    lob_id = tags.get("lob_id") or "default"
    conn_id = tags.get("conn_id") or ""

    # Pull the transcript JSON. Transcribe gives us a public URI by default;
    # the Lambda has read access via IAM.
    transcript_uri = job.get("Transcript", {}).get("TranscriptFileUri", "")
    if not transcript_uri:
        return {"statusCode": 500, "error": "no TranscriptFileUri"}

    try:
        with urllib.request.urlopen(transcript_uri, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[transcribe_complete] fetch transcript failed: {e}")
        return {"statusCode": 500, "error": str(e)}

    notes_text = _format_transcript_with_speakers(payload)

    if not notes_text.strip():
        _ws_send(conn_id, {"type": "error",
                           "message": "Transcription completed but the transcript is empty."})
        return {"statusCode": 200}

    _ws_send(conn_id, {"type": "status",
                       "message": "Transcription complete — extracting facts…"})

    # Invoke AgentCore (kind=meeting_notes). It will yield a single
    # meeting_preview event; we forward that to the WS client.
    invoke_payload = json.dumps({
        "kind": "meeting_notes",
        "notes_text": notes_text,
        "sa_id": sa_id,
        "customer_id": customer_id,
        "customer_display_name": tags.get("customer_display_name", ""),
        "lob_id": lob_id,
        "lob_display_name": tags.get("lob_display_name", ""),
        "turn": 0,  # transcribe-side turns aren't tracked
    }).encode("utf-8")

    try:
        resp = agentcore.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            payload=invoke_payload,
        )
    except Exception as e:
        print(f"[transcribe_complete] invoke_agent_runtime failed: {e}")
        _ws_send(conn_id, {"type": "error", "message": f"Extraction failed: {e}"})
        return {"statusCode": 500}

    # Forward each event back to the WS client so the existing
    # meeting_preview UI handler picks it up.
    body = resp.get("response") or resp.get("body") or resp.get("payload")
    if hasattr(body, "iter_lines"):
        for line in body.iter_lines():
            if not line:
                continue
            text = line.decode("utf-8") if isinstance(line, (bytes, bytearray)) else line
            text = text.lstrip("data: ").strip()
            if not text:
                continue
            try:
                evt = json.loads(text)
            except Exception:
                continue
            _ws_send(conn_id, evt)
    else:
        # Non-streaming response — single JSON event
        try:
            evt = json.loads(body) if isinstance(body, (str, bytes)) else body
            _ws_send(conn_id, evt)
        except Exception:
            pass

    return {"statusCode": 200}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_transcript_with_speakers(payload: dict) -> str:
    """Convert Transcribe JSON to readable text with speaker labels."""
    results = payload.get("results") or {}
    items = results.get("items") or []
    speaker_segs = (results.get("speaker_labels") or {}).get("segments") or []

    if not items:
        return ""

    # Build a (start_ms, end_ms) → speaker map
    seg_index: list[tuple[float, float, str]] = []
    for seg in speaker_segs:
        start = float(seg.get("start_time", 0))
        end = float(seg.get("end_time", 0))
        speaker = seg.get("speaker_label", "spk_0")
        seg_index.append((start, end, speaker))

    def _speaker_for(t: float) -> str:
        for s, e, spk in seg_index:
            if s <= t <= e:
                return spk
        return "spk_0"

    out_lines: list[str] = []
    cur_speaker: str | None = None
    cur_buf: list[str] = []

    for it in items:
        if it.get("type") == "punctuation":
            if cur_buf:
                cur_buf[-1] = cur_buf[-1] + (it.get("alternatives", [{}])[0].get("content", ""))
            continue
        word = it.get("alternatives", [{}])[0].get("content", "")
        if not word:
            continue
        try:
            t = float(it.get("start_time", 0))
        except (TypeError, ValueError):
            t = 0.0
        spk = _speaker_for(t) if seg_index else "spk_0"
        if spk != cur_speaker:
            if cur_buf:
                out_lines.append(f"{cur_speaker}: {' '.join(cur_buf)}")
                cur_buf = []
            cur_speaker = spk
        cur_buf.append(word)

    if cur_buf:
        out_lines.append(f"{cur_speaker}: {' '.join(cur_buf)}")

    return "\n".join(out_lines)


def _ws_send(conn_id: str, data: dict) -> None:
    if not conn_id or not apigw:
        return
    try:
        apigw.post_to_connection(
            ConnectionId=conn_id, Data=json.dumps(data).encode("utf-8"),
        )
    except ClientError as e:
        # Connection probably closed (SA navigated away). Swallow so we
        # don't fail the EventBridge invocation.
        print(f"[transcribe_complete] post_to_connection failed: {e}")
