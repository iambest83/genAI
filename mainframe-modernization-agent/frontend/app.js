/**
 * Entry point. Single-page app, one URL, no reloads.
 *
 * Token routing (single owner): there's exactly ONE subscription each for
 * "token" / "done" / "error" on the WebSocket — right here. A small flag
 * `tokenTarget` decides whether the current stream goes to the chat or
 * the slide-in profile panel. This makes races impossible.
 *
 * TODO(security): the API key is inlined here for now. Replace with
 * Cognito-issued JWT once Iteration 1.16 lands. See REFINEMENTS.md.
 */
import { AgentConnection } from "./lib/ws.js";
import { Chat } from "./ui/chat.js";
import { CustomerPicker } from "./ui/customer-picker.js";
import { maybeRenderDriftPrompt } from "./ui/drift-prompt.js";
import { ProbeCallout } from "./ui/probe-callout.js";
import { StatusBar } from "./ui/status-bar.js";
import { NotesModal } from "./ui/notes-modal.js";
import { RecordModal } from "./ui/record-modal.js";
import { renderMeetingPreview, renderMergeResult } from "./ui/extraction-preview.js";

const WS_URL = "wss://<WS_API_ID>.execute-api.us-east-1.amazonaws.com/prod";
// Shared API key set via deploy/setup_websocket.py's MFMOD_API_KEY env var.
// TODO(security): this is a shared secret, not per-SA auth. See
// ARCHITECTURE.md §11 for the tracked Cognito JWT replacement plan.
const API_KEY = window.__API_KEY || "REPLACE_WITH_DEPLOYED_API_KEY";
// Recording upload endpoint (item 4.2). Populated by setup_recordings_pipeline.py;
// override at runtime by setting window.__UPLOAD_ENDPOINT before app.js loads
// (the current value is also a sane default for the prod account).
const UPLOAD_ENDPOINT = (window.__UPLOAD_ENDPOINT
  || "https://<UPLOAD_API_ID>.execute-api.us-east-1.amazonaws.com/upload-url");

// --- Wire connection + UI ---------------------------------------------------

const conn = new AgentConnection(WS_URL, API_KEY);

const chat = new Chat(document.getElementById("chat"));
const statusBar = new StatusBar(document.getElementById("statusBar"), conn);
const picker = new CustomerPicker(document.getElementById("picker"), conn);
const probe = new ProbeCallout(
  document.getElementById("probeCallout"),
  document.getElementById("inp"),
  conn
);
const notesModal = new NotesModal(
  document.getElementById("notesModal"),
  document.getElementById("pasteNotesBtn"),
  conn
);
const recordModal = new RecordModal(
  document.getElementById("recModal"),
  document.getElementById("recordBtn"),
  conn,
  UPLOAD_ENDPOINT,
  API_KEY,
);

// --- Turn lifecycle ---------------------------------------------------------

// Whether a chat turn is currently in flight. Errant tokens are dropped
// when this is false.
let inFlight = false;

function startChatTurn(prompt) {
  inFlight = true;
  chat.addUserMessage(prompt);
  chat.startAssistantMessage();
  probe.clear();
  conn.sendPrompt(prompt);
}

// --- Input bar --------------------------------------------------------------

const inp = document.getElementById("inp");
const btn = document.getElementById("btn");

// Start disabled until the WebSocket actually opens.
inp.disabled = true;
btn.disabled = true;
inp.placeholder = "Connecting…";

function send() {
  const t = inp.value.trim();
  if (!t) return;
  if (!conn.isOpen()) {
    chat.showError(
      "Not connected to the agent yet. Wait for the green dot at the top, then try again."
    );
    return;
  }
  if (inFlight) {
    return; // mid-turn; defensive
  }

  inp.value = "";
  btn.disabled = true;
  startChatTurn(t);
}

// Enter → send. Shift+Enter → newline (textarea default behavior).
inp.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
btn.addEventListener("click", send);

// Expose a programmatic helper so child components (e.g., the DEFER
// button on the probe callout) can fire a turn without poking the input.
window.__sendPrompt = (text) => {
  if (!conn.isOpen() || inFlight) return;
  btn.disabled = true;
  startChatTurn(text);
};

// Mainframe fun facts — rotated on every page load. Pulled from public
// IBM Z / industry references so the framing stays credible (no made-up
// stats). Kept short on purpose so the hero stays scannable.
const FUN_FACTS = [
  "The IBM zSeries can process up to <b>30 billion transactions a day</b> on a single system — more than every Google search globally.",
  "About <b>70% of the world's enterprise transactions by value</b> still touch a mainframe — including 87% of credit card transactions.",
  "<b>220+ billion lines of COBOL</b> are estimated to still be in production worldwide — the language turned 65 in 2025.",
  "A modern z16 mainframe sustains <b>99.99999% (\"seven 9s\")</b> availability — about 3 seconds of downtime per year.",
  "Most mainframe workloads are billed in <b>MIPS (millions of instructions per second)</b> — a measure that's older than the internet.",
  "<b>43 of the top 50 banks worldwide</b> run on IBM Z — the platform is concentrated in FSI, healthcare, and government.",
  "JCL — Job Control Language — was first released in <b>1964</b>. SAs are still writing it.",
  "The average COBOL programmer is <b>over 55 years old</b>. Workforce risk is one of the strongest drivers of modernization.",
  "A single mainframe can host <b>thousands of LPARs</b> (logical partitions) — each running its own OS, isolated from the rest.",
  "<b>CICS turned 56 in 2025</b>, and still handles 1+ million transactions per second across customer estates.",
  "<b>VSAM</b> (Virtual Storage Access Method) predates Unix and is still the dominant non-relational store on z/OS.",
  "AWS Mainframe Modernization service supports two patterns: <b>replatform</b> (Micro Focus runtime) and <b>refactor</b> (Blu Age, COBOL → Java).",
  "<b>Hercules</b>, the open-source mainframe emulator, is what many modernization tools use to test workloads off-mainframe.",
  "A typical FSI mainframe migration moves <b>1,000–10,000 COBOL programs</b> in waves of 50–200 programs each.",
  "<b>RACF</b> (Resource Access Control Facility) was IBM's first commercial security product — released in 1976.",
  "The break-even on a mainframe modernization is typically <b>3–5 years</b>, but the strategic optionality unlocked is the bigger win.",
];

function renderFunFact() {
  const el = document.getElementById("welcomeFunFact");
  if (!el) return;
  const fact = FUN_FACTS[Math.floor(Math.random() * FUN_FACTS.length)];
  el.innerHTML = `<span class="funfact-bulb" aria-hidden="true">💡</span><b>Fun fact —</b> ${fact}`;
}
renderFunFact();
// Re-roll the fun fact each time the welcome screen is re-rendered (e.g.
// after "+ New chat"). Listens for the custom event Chat dispatches.
document.getElementById("chat").addEventListener("welcome-rendered", renderFunFact);

// (Pattern cycle was removed — the R-words now stream directly across
// the SVG flow line as part of the hero visual. No JS needed.)

// Welcome-card starter prompts. Each card has a data-prompt attribute;
// clicking sends it as a real chat turn (which clears the welcome card).
// Re-runnable so we can re-bind after Chat.resetToWelcome() rebuilds them.
function wireWelcomeCards() {
  document.querySelectorAll(".welcome-card[data-prompt]").forEach((card) => {
    card.addEventListener("click", () => {
      const t = card.getAttribute("data-prompt");
      if (t) window.__sendPrompt(t);
    });
  });
}
wireWelcomeCards();
document.getElementById("chat").addEventListener("welcome-rendered", wireWelcomeCards);

// "New chat" — clear the chat panel and re-render the welcome / starters.
const newChatBtn = document.getElementById("newChatBtn");
if (newChatBtn) {
  newChatBtn.addEventListener("click", () => {
    if (inFlight) return;
    chat.resetToWelcome();
    probe.clear();
  });
}

// "What I know" — pure facts recap for the bound customer. LoB-scoped if
// one is set, customer-wide if not. Routes to the deterministic
// summary_node (zero LLM calls, exact data). Hidden until a customer is
// bound, since unbound mode has nothing customer-specific to recap.
const whatIKnowBtn = document.getElementById("whatIKnowBtn");
if (whatIKnowBtn) {
  whatIKnowBtn.addEventListener("click", () => {
    if (inFlight) return;
    // The router's SUMMARY_TRIGGERS includes "what do you know", which
    // routes to summary_node (no retrieval, no probe, deterministic).
    window.__sendPrompt("what do you know");
  });
}

// "Generate report" — composite prompt: recap + gaps + recommendation +
// suggested artifacts. Enabled only with a bound customer.
const reportBtn = document.getElementById("reportBtn");
if (reportBtn) {
  reportBtn.addEventListener("click", () => {
    if (inFlight || reportBtn.disabled) return;
    const REPORT_PROMPT =
      "Generate a structured engagement report for the bound customer. " +
      "Include exactly these four sections, in order:\n" +
      "1. **What we know** — the captured profile (workload, constraints, " +
      "decisions, regulations, target dates) as a concise bulleted summary. " +
      "If nothing is captured for a dimension, say so explicitly.\n" +
      "2. **Open gaps** — the top 3-5 facts that are missing and would most " +
      "improve the recommendation. Be specific about why each gap matters.\n" +
      "3. **Recommendation** — given what you know, the modernization path " +
      "you'd lean toward (rehost / replatform / refactor / mixed) with a " +
      "short rationale and trade-offs. Cite assumptions explicitly.\n" +
      "4. **Suggested artifacts** — list 2-4 paste-ready artifacts you could " +
      "draft next (e.g. wave plan, target architecture, risk register, TCO " +
      "outline) and ask the SA which to generate first.\n\n" +
      "Keep prose tight; favor bullets and short paragraphs. Do not include " +
      "a **To advance:** probe at the end of this turn — section 4 is the ask.";
    window.__sendPrompt(REPORT_PROMPT);
  });
}

// Both buttons share the same gating rule: visible/enabled iff a customer
// is bound. customer_bound is fired on every selectCustomer ack from the
// server (single source of truth).
const pasteNotesBtn = document.getElementById("pasteNotesBtn");
const recordBtn = document.getElementById("recordBtn");

// All four customer-gated buttons stay VISIBLE so SAs can see what's
// available, but stay disabled until a customer is bound. Clicking a
// disabled button silently no-ops (browsers don't fire click on
// disabled buttons), and the title attribute tells them why.
function setCustomerBound(hasCustomer) {
  document.querySelectorAll('.hdr-btn[data-needs-customer="true"]')
    .forEach((b) => { b.disabled = !hasCustomer; });
  if (reportBtn) reportBtn.disabled = !hasCustomer;
}
// Initial state — no customer yet on first connect.
setCustomerBound(false);

conn.on("customer_bound", (e) => {
  const hasCustomer = !!(e.customer_display_name || e.customer_id);
  setCustomerBound(hasCustomer);
});

// --- Recording upload progress (item 4.2) ---------------------------------
// The RecordModal calls `window.__notifyUploadProgress("uploading"|"uploaded")`
// at upload milestones; we drop a friendly system note in chat so the SA
// can see that a recording is making its way through the pipeline.
window.__notifyUploadProgress = function (stage) {
  const chatRoot = document.getElementById("chat");
  if (!chatRoot) return;
  // Only emit ONE note in the chat — when the audio has landed and the
  // server is processing it. The mid-upload "Uploading…" note was just
  // noise; the modal closes the moment the upload completes anyway.
  if (stage !== "uploaded") return;
  const note = document.createElement("div");
  note.className = "m sys";
  note.innerHTML = '<div class="b">🎤 Transcribing the recording — preview will appear here when it\'s ready (typically 30–60 s).</div>';
  chatRoot.appendChild(note);
  chatRoot.scrollTop = chatRoot.scrollHeight;
};

// --- Listen-mode (paste meeting notes, item 4.1) ---------------------------
// Two helpers exposed on window so the modal + preview-card components can
// fire turns without holding onto a `conn` reference. Each one flips the
// shared `inFlight` flag the same way a chat turn does — that keeps the
// connection-state UI in sync (button enable/disable, status bar).
window.__submitMeetingNotes = function (notesText) {
  if (!conn.isOpen() || inFlight) return;
  inFlight = true;
  // Drop a system note in chat so the SA can see the action acknowledged
  // even before the agent starts streaming back.
  const chatRoot = document.getElementById("chat");
  if (chatRoot) {
    const note = document.createElement("div");
    note.className = "m sys";
    note.innerHTML = `<div class="b">📋 Sent meeting notes for extraction…</div>`;
    chatRoot.appendChild(note);
    chatRoot.scrollTop = chatRoot.scrollHeight;
  }
  conn.invoke("submitMeetingNotes", { notes_text: notesText });
};

window.__confirmMeetingMerge = function (preview, confirmedIds) {
  if (!conn.isOpen() || inFlight) return;
  inFlight = true;
  conn.invoke("confirmMeetingMerge", {
    preview: preview,
    confirmed_ids: confirmedIds,
  });
};

// AgentCore yields `meeting_preview` after extraction, and
// `meeting_merge_result` after a confirm-merge. Both are single-event
// terminal flows — no token streaming.
conn.on("meeting_preview", (e) => {
  renderMeetingPreview(e.preview, conn);
});
conn.on("meeting_merge_result", (e) => {
  renderMergeResult(e.result);
});

// --- Server events: single point of dispatch -------------------------------

conn.on("token", (e) => {
  const text = e.text || "";
  if (!text || !inFlight) return;
  chat.appendToken(text);
});

conn.on("done", () => {
  if (inFlight) {
    const finalRaw = chat.finalize();
    if (finalRaw) {
      const lastBubble = document.querySelector(".chat .m.a:last-child .b");
      if (lastBubble) maybeRenderDriftPrompt(finalRaw, lastBubble, conn);
      probe.extractFromTurn(finalRaw);
    }
  }
  inFlight = false;
  btn.disabled = false;
});

conn.on("error", (e) => {
  if (inFlight) chat.showError(e.message || "Unknown error");
  inFlight = false;
  btn.disabled = false;
});

// --- Connection lifecycle ---------------------------------------------------

conn.onLifecycle(({ state }) => {
  if (state === "open") {
    inp.disabled = false;
    btn.disabled = false;
    inp.placeholder = "How can I help?";
    inp.focus();
  } else if (state === "reconnecting" || state === "error") {
    inp.disabled = true;
    btn.disabled = true;
    inp.placeholder = "Reconnecting…";
  } else if (state === "connecting") {
    inp.disabled = true;
    btn.disabled = true;
    inp.placeholder = "Connecting…";
  }
});

// --- Global error surface ---------------------------------------------------

window.addEventListener("error", (e) => {
  console.error("[uncaught]", e.error || e.message);
  try { chat.showError("UI error: " + (e.message || "unknown")); }
  catch { /* chat may not exist yet */ }
});
window.addEventListener("unhandledrejection", (e) => {
  console.error("[unhandled rejection]", e.reason);
  try { chat.showError("UI error: " + (e.reason?.message || String(e.reason))); }
  catch { /* chat may not exist yet */ }
});

// --- Bootstrap --------------------------------------------------------------

conn.connect();
