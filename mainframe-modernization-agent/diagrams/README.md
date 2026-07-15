# `diagrams/`

[`architecture.drawio`](architecture.drawio) — open with
[draw.io](https://app.diagrams.net/) (File → Open From → Device) or the
draw.io desktop app / VS Code extension. Six pages, most-detailed first:

1. **E2E Overview** — the whole system, one page: browser → WebSocket →
   AgentCore Runtime → LangGraph → KB / Gateway / DynamoDB, plus the
   optional recordings pipeline. Start here.
2. **LangGraph Turn Flow** — every node in one chat turn, expanded with
   the actual decision logic (payload-kind branch, router's route
   options, artifact phase-gating).
3. **Memory & Persistence** — the DynamoDB schema, optimistic locking
   flow, and the profile-snapshot-vs-turn-event distinction.
4. **Retrieval — KB + MCP Gateway** — the two-stage KB retrieval
   pipeline (vector ANN → Cohere rerank → score floor) and the Gateway's
   MCP target topology.
5. **Listen Mode & Recordings** — the meeting-notes-paste and
   audio-upload-transcribe flows, independent of the main chat path.
6. **Decision Register (Summary)** — a condensed view of the "why" behind
   the major architecture decisions (each tagged `D1`, `D5`, `D6`, `D11`,
   etc. — same tags used in `ARCHITECTURE.md`'s decision register, §10).

This diagram and [`ARCHITECTURE.md`](../ARCHITECTURE.md) describe the
same system from two angles — the Markdown doc goes deeper on rationale,
the diagram is faster for orienting on topology. Keep both in sync when
the architecture changes; there's no automated check that they agree.
