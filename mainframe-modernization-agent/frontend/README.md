# `frontend/` — chat UI

A vanilla-JS single-page app. No build step, no bundler, no framework, no
`package.json` — everything loads as plain `<script>` tags or native ES
modules directly in the browser.

## Before this will work

Two values are hardcoded placeholders in [`app.js`](app.js) that you must
replace with your actual deployed values (see the [root README's setup
walkthrough](../README.md#setting-this-up-yourself), step 8):

```js
const WS_URL = "wss://<WS_API_ID>.execute-api.us-east-1.amazonaws.com/prod";
const API_KEY = window.__API_KEY || "REPLACE_WITH_DEPLOYED_API_KEY";
```

`WS_URL` comes from `deploy/setup_websocket.py`'s output. `API_KEY` must
match the `MFMOD_API_KEY` you set when running that script. Alternatively,
set `window.__API_KEY` before `app.js` loads (e.g. injected by your
hosting pipeline) instead of editing the literal.

There's also an upload endpoint for the optional recordings feature:

```js
const UPLOAD_ENDPOINT = (window.__UPLOAD_ENDPOINT
  || "https://<UPLOAD_API_ID>.execute-api.us-east-1.amazonaws.com/upload-url");
```

Only relevant if you deployed `deploy/setup_recordings_pipeline.py`.

## Running it

Any static file host works — this is genuinely just HTML/CSS/JS with no
server-side rendering. For local testing:

```bash
cd frontend
python -m http.server 8080
# open http://localhost:8080
```

For a real deployment, the repo's own scripts assume S3 + CloudFront
(`../deploy/setup_cloudfront.py`) — CloudFront specifically because live
microphone capture (`getUserMedia`) requires a secure (HTTPS) context,
and plain S3 website hosting is HTTP-only.

## Structure

| File | Purpose |
|---|---|
| [`index.html`](index.html) | The single page. CSP header restricts scripts to `'self'` + the three CDN origins below. |
| [`app.js`](app.js) | Entry point — wires the WebSocket connection to every UI component, owns the single `token`/`done`/`error` subscription (kept singular deliberately so streaming can't race between two listeners). |
| [`lib/ws.js`](lib/ws.js) | WebSocket client with auto-reconnect and a small typed event-bus (`conn.on(type, handler)`). |
| [`lib/markdown.js`](lib/markdown.js) | Thin wrapper over `marked` + `highlight.js` (both CDN-loaded, land on `window`) — one `render(rawMd)` function. |
| [`ui/chat.js`](ui/chat.js) | The message list — streaming token append, finalize. |
| [`ui/customer-picker.js`](ui/customer-picker.js) | Header chips for binding a customer + line-of-business. |
| [`ui/drift-prompt.js`](ui/drift-prompt.js) | Renders inline Yes/No buttons for agent confirmation prompts (customer-switch drift, meeting-notes merge confirmation). |
| [`ui/extraction-preview.js`](ui/extraction-preview.js) | The structured checkbox card shown after pasting meeting notes, before the SA confirms what to merge. |
| [`ui/notes-modal.js`](ui/notes-modal.js) | The "paste meeting notes" modal. |
| [`ui/probe-callout.js`](ui/probe-callout.js) | Pulls the `**To advance:**` question out of a streamed reply and pins it above the input box. |
| [`ui/record-modal.js`](ui/record-modal.js) | Audio recording/upload UI for the optional listen-mode pipeline — live `MediaRecorder` capture or file upload. |
| [`ui/status-bar.js`](ui/status-bar.js) | Connection state dot + status text. |
| [`styles.css`](styles.css) | All styling — no CSS framework. |

## External dependencies (CDN, no local install)

Loaded via `<script>` tag in `index.html`, allowlisted in the CSP header:

- [`marked`](https://cdn.jsdelivr.net/npm/marked/marked.min.js) — Markdown → HTML
- [`DOMPurify`](https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js) — sanitizes that HTML before it hits `innerHTML` (required — streamed LLM/transcript text is untrusted input)
- [`highlight.js`](https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js) — code-block syntax highlighting

## Known gap

Artifacts (TCO estimate, wave plan CSV, Mermaid architecture diagram, risk
register) are fully generated server-side and streamed as `artifact`-typed
WebSocket events (`agent/nodes_artifacts.py`) — but there is currently no
`conn.on("artifact", …)` handler anywhere in this folder. The event has no
listener, so it's silently dropped once it reaches the browser. Before
building the renderer, first confirm `../deploy/ws_lambda.py` actually
relays `type: artifact` frames rather than filtering them — that's the
place to check first, not this folder. See `ARCHITECTURE.md` for full
context.
