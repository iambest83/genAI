/**
 * Recording modal (Iteration 4.2).
 *
 * Two paths to producing an audio file the agent can transcribe:
 *
 *   1. Live record — MediaRecorder API captures from the user's
 *      microphone, builds a single Blob on stop, and uploads it.
 *   2. Upload — user picks an existing audio file from disk.
 *
 * Both paths share the same upload + post-process pipeline:
 *
 *   POST /upload-url  → returns presigned S3 PUT URL
 *   PUT to S3         → triggers the Transcribe job
 *   (server-side)     → Transcribe → AgentCore extract
 *   meeting_preview   → arrives over the WebSocket → preview-card
 *
 * Consent gate (item 4.7 v1): a single checkbox the SA must tick before
 * either path enables. The acknowledgment is recorded in the S3 object
 * metadata server-side, not just on the client.
 */

const MAX_FILE_BYTES = 120 * 1024 * 1024;  // 120 MB
const PREFERRED_MIMES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];

export class RecordModal {
  /**
   * @param {HTMLElement}      rootEl       - the .rec-modal element
   * @param {HTMLButtonElement} openBtnEl   - header button that opens the modal
   * @param {AgentConnection}  agentConn    - WebSocket client
   * @param {string}           uploadEndpoint - "https://<api-id>.execute-api...../upload-url"
   * @param {string}           apiKey       - same key the WS uses
   */
  constructor(rootEl, openBtnEl, agentConn, uploadEndpoint, apiKey) {
    this.root = rootEl;
    this.openBtn = openBtnEl;
    this.conn = agentConn;
    this.uploadEndpoint = uploadEndpoint;
    this.apiKey = apiKey;

    this.consent = rootEl.querySelector("#recConsent");
    this.tabs = rootEl.querySelectorAll(".rec-tab");
    this.panels = rootEl.querySelectorAll(".rec-tab-panel");
    this.startBtn = rootEl.querySelector("#recStartBtn");
    this.stopBtn = rootEl.querySelector("#recStopBtn");
    this.liveStatus = rootEl.querySelector("#recLiveStatus");
    this.meter = rootEl.querySelector("#recMeter span");
    this.fileInput = rootEl.querySelector("#recFile");
    this.uploadBtn = rootEl.querySelector("#recUploadBtn");
    this.closeBtn = rootEl.querySelector("#recModalClose");

    this.recorder = null;
    this.recordedChunks = [];
    this.audioCtx = null;
    this.meterRaf = 0;
    this.meterStartedAt = 0;
    this.mascot = rootEl.querySelector("#recMascot");

    this._wire();
  }

  // --- Public ---------------------------------------------------------------

  open() {
    if (!this.conn.isOpen()) return;

    // Belt-and-suspenders gate: the header button is already disabled
    // until customer_bound fires, but if anything ever calls .open()
    // directly we still need to refuse. Recordings always belong to a
    // specific customer.
    const customerChip = document.getElementById("chipCustomerVal");
    const customerName = (customerChip && customerChip.textContent || "").trim();
    if (!customerName || customerName === "Choose…") {
      this._nudgeToBindCustomer();
      return;
    }

    this.consent.checked = false;
    this.fileInput.value = "";
    this.uploadBtn.disabled = true;
    this.startBtn.disabled = false;
    this.stopBtn.disabled = true;
    this.liveStatus.innerHTML = "Click <strong>Start recording</strong> to begin. Microphone access required.";
    this._setMeter(0);
    this.root.hidden = false;
    this._switchTab("record");
  }

  // Drop a polite inline note in the chat steering the SA to the
  // Customer chip. Then auto-open the customer dropdown so they don't
  // have to hunt for it.
  _nudgeToBindCustomer() {
    const chatRoot = document.getElementById("chat");
    if (chatRoot) {
      const note = document.createElement("div");
      note.className = "m sys";
      note.innerHTML = `
        <div class="b">
          🎤 To record or upload a customer call, pick a <strong>Customer</strong>
          first using the chip at the top right. Recordings always belong to a
          specific customer profile.
        </div>`;
      chatRoot.appendChild(note);
      chatRoot.scrollTop = chatRoot.scrollHeight;
    }
    // Auto-open the customer dropdown so the next click is one tap away.
    const chip = document.getElementById("chipCustomer");
    if (chip && !chip.disabled) chip.click();
  }

  close() {
    this._stopRecorder({ keep: false });
    if (this.mascot) this.mascot.classList.remove("is-listening");
    this.root.hidden = true;
  }

  // --- Internal -------------------------------------------------------------

  _wire() {
    this.openBtn.addEventListener("click", () => this.open());
    this.closeBtn.addEventListener("click", () => this.close());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.root.hidden) this.close();
    });

    this.tabs.forEach((t) =>
      t.addEventListener("click", () => this._switchTab(t.dataset.tab))
    );

    this.consent.addEventListener("change", () => this._refreshButtonStates());
    this.fileInput.addEventListener("change", () => this._refreshButtonStates());

    this.startBtn.addEventListener("click", () => this._startRecord());
    this.stopBtn.addEventListener("click", () => this._stopRecorder({ keep: true }));
    this.uploadBtn.addEventListener("click", () => this._uploadPicked());
  }

  _switchTab(name) {
    this.tabs.forEach((t) => t.classList.toggle("is-active", t.dataset.tab === name));
    this.panels.forEach((p) => (p.hidden = p.dataset.panel !== name));
    this._refreshButtonStates();
  }

  _refreshButtonStates() {
    const consented = this.consent.checked;
    this.startBtn.disabled = !consented || !!this.recorder;
    this.uploadBtn.disabled = !consented || !this.fileInput.files?.length;
  }

  // --- Live record (MediaRecorder) -----------------------------------------

  async _startRecord() {
    if (!this.consent.checked) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      this.liveStatus.innerHTML = `<span class="rec-error">Mic access denied: ${_esc(e.message || e)}</span>`;
      return;
    }

    const mime = PREFERRED_MIMES.find((m) => MediaRecorder.isTypeSupported(m)) || "";
    try {
      this.recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (e) {
      this.liveStatus.innerHTML = `<span class="rec-error">Recorder init failed: ${_esc(e.message || e)}</span>`;
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    this.recordedChunks = [];
    this.recorder.ondataavailable = (e) => {
      if (e.data && e.data.size) this.recordedChunks.push(e.data);
    };
    this.recorder.onstop = async () => {
      try {
        // Stop the underlying mic stream
        stream.getTracks().forEach((t) => t.stop());
      } catch {}
      if (this.mascot) this.mascot.classList.remove("is-listening");
      this._stopMeter();
      if (!this._keepRecording) {
        this.recordedChunks = [];
        return;
      }
      const type = this.recorder.mimeType || "audio/webm";
      const blob = new Blob(this.recordedChunks, { type });
      const filename = `live-${Date.now()}.${_extFromMime(type)}`;
      this.liveStatus.textContent = "Recording stopped — uploading…";
      try {
        await this._uploadBlob(blob, type, filename);
      } catch (e) {
        this.liveStatus.innerHTML = `<span class="rec-error">${_esc(e.message || e)}</span>`;
      }
      this.recorder = null;
      this._refreshButtonStates();
    };

    this.recorder.start(1000); // emit dataavailable every 1s
    this.startBtn.disabled = true;
    this.stopBtn.disabled = false;
    this.liveStatus.innerHTML = '<span class="rec-live">● Recording…</span>';
    if (this.mascot) this.mascot.classList.add("is-listening");
    this._startMeter(stream);
  }

  _stopRecorder({ keep }) {
    if (!this.recorder) return;
    this._keepRecording = !!keep;
    try { this.recorder.stop(); } catch {}
  }

  // Simple visual feedback — peak level meter
  _startMeter(stream) {
    try {
      this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = this.audioCtx.createMediaStreamSource(stream);
      const analyser = this.audioCtx.createAnalyser();
      analyser.fftSize = 1024;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.fftSize);
      this.meterStartedAt = performance.now();
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let max = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = Math.abs(buf[i] - 128);
          if (v > max) max = v;
        }
        this._setMeter(Math.min(1, max / 96));
        this.meterRaf = requestAnimationFrame(tick);
      };
      tick();
    } catch (e) {
      console.warn("meter init failed", e);
    }
  }
  _stopMeter() {
    cancelAnimationFrame(this.meterRaf);
    this.meterRaf = 0;
    if (this.audioCtx) {
      try { this.audioCtx.close(); } catch {}
      this.audioCtx = null;
    }
    this._setMeter(0);
  }
  _setMeter(p) { this.meter.style.transform = `scaleX(${p.toFixed(3)})`; }

  // --- File upload tab ------------------------------------------------------

  async _uploadPicked() {
    if (!this.consent.checked) return;
    const f = this.fileInput.files?.[0];
    if (!f) return;
    if (f.size > MAX_FILE_BYTES) {
      alert(`File too large (${(f.size / 1024 / 1024).toFixed(1)} MB). Cap is ${MAX_FILE_BYTES / 1024 / 1024} MB.`);
      return;
    }
    try {
      await this._uploadBlob(f, f.type || "audio/webm", f.name || "upload");
    } catch (e) {
      alert(`Upload failed: ${e.message || e}`);
    }
  }

  // --- Shared upload pipeline ----------------------------------------------

  async _uploadBlob(blob, contentType, filename) {
    const conn_id = this.conn.connectionId();
    if (!conn_id) throw new Error("WebSocket not yet connected — try again.");

    // 1. Ask the API for a presigned PUT
    const presignResp = await fetch(this.uploadEndpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: this.apiKey,
        consent_acknowledged: this.consent.checked,
        conn_id,
        filename,
        content_type: contentType,
      }),
    });
    if (!presignResp.ok) {
      const err = await presignResp.json().catch(() => ({}));
      throw new Error(err.error || `presign HTTP ${presignResp.status}`);
    }
    const presign = await presignResp.json();

    // 2. PUT the audio to S3 with the exact headers the presign requires
    if (window.__notifyUploadProgress) window.__notifyUploadProgress("uploading");
    const headers = presign.headers_required || {};
    const putResp = await fetch(presign.upload_url, {
      method: "PUT", body: blob, headers,
    });
    if (!putResp.ok) {
      throw new Error(`S3 PUT HTTP ${putResp.status}: ${await putResp.text()}`);
    }

    // 3. Done from the browser's POV — server takes over from here.
    if (window.__notifyUploadProgress) window.__notifyUploadProgress("uploaded");
    this.close();
  }
}


// --- Helpers ----------------------------------------------------------------

function _extFromMime(mime) {
  if (!mime) return "webm";
  if (mime.includes("webm")) return "webm";
  if (mime.includes("mp4") || mime.includes("m4a")) return "m4a";
  if (mime.includes("wav")) return "wav";
  if (mime.includes("mpeg") || mime.includes("mp3")) return "mp3";
  if (mime.includes("ogg")) return "ogg";
  return "webm";
}

function _esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}
