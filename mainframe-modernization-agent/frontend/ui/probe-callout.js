/**
 * Pull the **To advance:** question out of the streamed reply and surface
 * it as a pinned callout above the input — but ONLY when there is a
 * question to show. The whole element stays hidden by default; we toggle
 * an `is-active` class to reveal.
 *
 * Includes a "Defer" button: clicking it sends the literal word "skip"
 * as a turn, which the intent classifier deterministically maps to the
 * defer route. This bypasses LLM guesswork entirely — the user explicitly
 * declared their intent.
 *
 * Behavior:
 *   - extractFromTurn(rawMd): if the assistant turn contained
 *     `**To advance:** ...`, show the callout with that question text.
 *   - clear(): called when the SA submits a new turn — hide again.
 *   - Clicking the callout (text area) focuses the input.
 *   - Clicking "Defer" fires a "skip" turn.
 */
const TO_ADVANCE_RE = /\*\*\s*To advance\s*:\s*\*\*\s*(.+?)(?:\n\n|$)/is;

export class ProbeCallout {
  constructor(rootEl, inputEl, agentConn) {
    this.root = rootEl;
    this.input = inputEl;
    this.conn = agentConn;
    this.current = null;
    this._render();
  }

  _render() {
    // Element is hidden whenever there's no current question.
    this.root.classList.add("probe-host");
    this.root.innerHTML = `
      <div class="probe-inner">
        <div class="probe-icon">→</div>
        <div class="probe-text" id="probeText"></div>
        <button class="probe-defer" id="probeDefer" type="button" title="Skip this question">Defer</button>
        <button class="probe-dismiss" id="probeDismiss" type="button" title="Hide">×</button>
      </div>
    `;
    this.textEl = this.root.querySelector("#probeText");

    // Click on the text area → focus the input.
    this.textEl.addEventListener("click", () => this.input.focus());
    this.root.querySelector(".probe-icon").addEventListener("click", () => this.input.focus());

    // Defer button — explicit user intent. Send "skip" as a real turn so
    // the agent's defer_node fires and the open question is dropped.
    this.root.querySelector("#probeDefer").addEventListener("click", (e) => {
      e.stopPropagation();
      if (window.__sendPrompt) {
        window.__sendPrompt("skip");
      }
      this.clear();
    });

    // Dismiss button — purely visual. Hides the callout without sending
    // anything; the agent still considers the question pending.
    this.root.querySelector("#probeDismiss").addEventListener("click", (e) => {
      e.stopPropagation();
      this.clear();
    });

    // Start hidden.
    this.clear();
  }

  // Call after a finalized assistant turn with the raw markdown.
  extractFromTurn(rawMd) {
    const m = TO_ADVANCE_RE.exec(rawMd || "");
    if (!m) return;            // no probe this turn — leave any prior alone
    const q = m[1].trim().replace(/\s+/g, " ").slice(0, 280);
    this.show(q);
  }

  show(question) {
    this.current = question;
    this.textEl.textContent = question;
    this.root.classList.add("is-active");
  }

  clear() {
    this.current = null;
    this.textEl.textContent = "";
    this.root.classList.remove("is-active");
  }
}
