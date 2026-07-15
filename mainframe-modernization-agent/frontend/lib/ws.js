/**
 * WebSocket client with auto-reconnect and a tiny event-bus.
 *
 * Server messages are JSON objects with a `type` field. Subscribers register
 * for a specific type via `on(type, handler)` and receive the parsed object.
 *
 * Example:
 *   const conn = new AgentConnection(url, apiKey);
 *   conn.on("token", (e) => { ... });
 *   conn.on("done",  (e) => { ... });
 *   conn.connect();
 *   conn.send({ action: "sendMessage", prompt: "..." });
 */
export class AgentConnection {
  constructor(url, apiKey) {
    this.url = url;
    this.apiKey = apiKey;
    this.ws = null;
    this.handlers = {};         // { type: [fn, fn, ...] }
    this.lifecycle = [];        // connect / disconnect / reconnecting listeners
    this._reconnectMs = 2000;
    this._connId = "";          // populated via whoami → "whoami" event
  }

  // Server-assigned WebSocket connection id, available after the server
  // answers `whoami`. Used by HTTP-side flows (e.g. /upload-url for
  // recordings) so they can correlate their result back to this WS.
  connectionId() { return this._connId; }

  on(type, fn) {
    (this.handlers[type] ||= []).push(fn);
    return this;
  }

  onLifecycle(fn) {
    this.lifecycle.push(fn);
    return this;
  }

  _emit(type, payload) {
    (this.handlers[type] || []).forEach((fn) => {
      try { fn(payload); } catch (e) { console.error(`handler ${type} threw`, e); }
    });
  }

  _emitLifecycle(state, detail) {
    this.lifecycle.forEach((fn) => {
      try { fn({ state, detail }); } catch (e) { console.error("lifecycle threw", e); }
    });
  }

  connect() {
    this._emitLifecycle("connecting");
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this._emitLifecycle("open");
      // Ask the server for our connection id so HTTP-side flows can
      // include it (e.g. /upload-url for recordings).
      try {
        this.ws.send(JSON.stringify({ action: "whoami", api_key: this.apiKey }));
      } catch {}
    };
    this.ws.onclose = () => {
      this._connId = "";
      this._emitLifecycle("reconnecting");
      setTimeout(() => this.connect(), this._reconnectMs);
    };
    this.ws.onerror = (e) => this._emitLifecycle("error", e);
    this.ws.onmessage = (e) => {
      let parsed;
      try { parsed = JSON.parse(e.data); }
      catch (err) { console.error("non-JSON ws frame", e.data); return; }
      // Capture the conn_id internally before fanning out to handlers
      if (parsed.type === "whoami" && parsed.conn_id) {
        this._connId = parsed.conn_id;
      }
      this._emit(parsed.type || "_unknown", parsed);
    };
  }

  isOpen() {
    return this.ws && this.ws.readyState === 1;
  }

  // Send a sendMessage (chat) request
  sendPrompt(prompt) {
    if (!this.isOpen()) return false;
    this.ws.send(JSON.stringify({ action: "sendMessage", prompt, api_key: this.apiKey }));
    return true;
  }

  // Generic action invocation (selectCustomer, selectLob, whatDoYouKnow, ...)
  invoke(action, body = {}) {
    if (!this.isOpen()) return false;
    this.ws.send(JSON.stringify({ action, ...body, api_key: this.apiKey }));
    return true;
  }
}
