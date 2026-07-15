/**
 * Paste-notes modal (Iteration 4.1).
 *
 * Opens when the SA clicks the header "Paste notes" button. Captures a
 * chunk of meeting text, fires the WS `submitMeetingNotes` action, and
 * hands control off to the preview card (drawn by `renderMeetingPreview`
 * in extraction-preview.js) once the agent returns a `meeting_preview`
 * event.
 *
 * Preconditions enforced upstream (the WS Lambda also checks):
 *   - WebSocket open
 *   - A customer is bound (button is hidden otherwise)
 */

const SOFT_CHAR_CAP = 32_000;     // matches agent/nodes_listen.py cap

export class NotesModal {
  constructor(rootEl, openBtnEl, agentConn) {
    this.root = rootEl;
    this.openBtn = openBtnEl;
    this.conn = agentConn;
    this.textarea = rootEl.querySelector("#notesModalText");
    this.meta = rootEl.querySelector("#notesModalMeta");
    this.submitBtn = rootEl.querySelector("#notesModalSubmit");
    this.cancelBtn = rootEl.querySelector("#notesModalCancel");
    this.closeBtn = rootEl.querySelector("#notesModalClose");

    this.openBtn.addEventListener("click", () => this.open());
    this.closeBtn.addEventListener("click", () => this.close());
    this.cancelBtn.addEventListener("click", () => this.close());
    this.submitBtn.addEventListener("click", () => this._submit());
    this.textarea.addEventListener("input", () => this._updateMeta());

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !rootEl.hidden) this.close();
    });
  }

  open() {
    if (!this.conn.isOpen()) return;
    this.root.hidden = false;
    this.textarea.value = "";
    this._updateMeta();
    setTimeout(() => this.textarea.focus(), 50);
  }

  close() {
    this.root.hidden = true;
  }

  _updateMeta() {
    const len = (this.textarea.value || "").length;
    if (len === 0) {
      this.meta.textContent = "";
      this.submitBtn.disabled = true;
    } else if (len > SOFT_CHAR_CAP) {
      this.meta.textContent = `${len.toLocaleString()} chars — only the first ${SOFT_CHAR_CAP.toLocaleString()} will be processed.`;
      this.submitBtn.disabled = false;
    } else {
      this.meta.textContent = `${len.toLocaleString()} chars`;
      this.submitBtn.disabled = false;
    }
  }

  _submit() {
    const text = (this.textarea.value || "").trim();
    if (!text) return;
    if (!this.conn.isOpen()) return;
    // Hand off to app.js's listen-mode lifecycle so it can show the
    // busy state and route the response into the preview card.
    if (window.__submitMeetingNotes) {
      window.__submitMeetingNotes(text);
    }
    this.close();
  }
}
