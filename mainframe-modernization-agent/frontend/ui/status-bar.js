/**
 * Minimal status bar:
 *   - Connection dot (green / yellow / red)
 *   - Connection / activity status text
 *
 * The previous "Activity ▾" trace pane was removed; SAs found it noisy
 * and the relevant signal (router decision, MCP calls) is already
 * implicit in the agent's reply. Streaming progress lives in the chat
 * bubble's blinking-LED thinking state.
 */

export class StatusBar {
  constructor(rootEl, agentConn) {
    this.root = rootEl;
    this.conn = agentConn;
    this._render();
    this._wire();
  }

  _render() {
    this.root.innerHTML = `
      <span class="dot off" id="connDot"></span>
      <span class="conn-text" id="connText">Connecting…</span>
    `;
    this.dot = this.root.querySelector("#connDot");
    this.text = this.root.querySelector("#connText");
  }

  _wire() {
    this.conn.onLifecycle(({ state }) => {
      if (state === "connecting" || state === "reconnecting") {
        this._setStatus("Connecting…", "off");
      } else if (state === "open") {
        this._setStatus("Connected", "");
      } else if (state === "error") {
        this._setStatus("Connection error", "off");
      }
    });

    this.conn.on("status", (e) => {
      const msg = e.message || "Working…";
      // Hide internal routing chatter — SAs don't need to see "Route: kb"
      if (typeof msg === "string" && msg.startsWith("Route: ")) return;
      this._setStatus(msg, "busy");
    });

    this.conn.on("done", () => {
      this._setStatus("Connected", "");
    });

    this.conn.on("error", () => {
      this._setStatus("Error — see chat", "off");
    });
  }

  _setStatus(text, dotKind) {
    this.text.textContent = text;
    this.dot.className = "dot " + (dotKind || "");
  }

  // No-op kept for backwards compatibility with app.js callers.
  resetTrace() {}
}
