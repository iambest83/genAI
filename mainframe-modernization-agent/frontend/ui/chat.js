/**
 * Chat surface. Owns the message list and exposes:
 *   - addUserMessage(text)
 *   - startAssistantMessage()        → returns a handle
 *   - appendTokenToCurrent(text)
 *   - finalizeCurrent()              → returns the raw markdown that streamed in
 *   - showError(msg)
 *
 * Listens for "no message in flight" so probe-callout / drift-prompt can do
 * their own rendering without colliding with this stream.
 */
import { renderMarkdown, highlightCodeBlocks } from "../lib/markdown.js";

// Cap how often we re-render the entire markdown blob during streaming.
// The wire still delivers every token live; we just paint at most once
// per RENDER_INTERVAL_MS. Final paint at finalize() is full-fidelity.
const RENDER_INTERVAL_MS = 80;

export class Chat {
  constructor(rootEl) {
    this.root = rootEl;
    this.bubble = null;     // current assistant <div class="b">
    this.raw = "";          // accumulated markdown for the current bubble
    this._lastRenderAt = 0; // ms timestamp of last paint
    this._pendingRender = false;
    this.welcomeEl = rootEl.querySelector(".welcome");
    // Cache the original welcome markup so we can restore it on "New chat".
    this._welcomeHTML = this.welcomeEl ? this.welcomeEl.outerHTML : "";
  }

  _ensureWelcomeRemoved() {
    if (this.welcomeEl) {
      this.welcomeEl.remove();
      this.welcomeEl = null;
    }
  }

  // Public — clear the chat and re-render the welcome / starter cards.
  // Caller is responsible for re-wiring click handlers on the new cards
  // (we emit a custom event so app.js can do that without coupling).
  resetToWelcome() {
    this.bubble = null;
    this.raw = "";
    this.root.innerHTML = this._welcomeHTML;
    this.welcomeEl = this.root.querySelector(".welcome");
    this.root.dispatchEvent(new CustomEvent("welcome-rendered"));
  }

  addUserMessage(text) {
    this._ensureWelcomeRemoved();
    const m = document.createElement("div");
    m.className = "m u";
    const b = document.createElement("div");
    b.className = "b";
    b.textContent = text;
    m.appendChild(b);
    this.root.appendChild(m);
    this._scrollToBottom();
  }

  startAssistantMessage() {
    this._ensureWelcomeRemoved();
    const m = document.createElement("div");
    m.className = "m a";
    // Avatar — small AWS-themed mascot, same identity as the welcome hero.
    // .is-busy makes the mascot blink while we wait.
    const avatar = this._makeMascotAvatar();
    avatar.classList.add("is-busy");
    m.appendChild(avatar);
    const b = document.createElement("div");
    b.className = "b";
    // Word-at-a-time busy phrase. Picks one of a small pool, then reveals
    // it word-by-word until the first real token arrives, at which point
    // the bubble's innerHTML is rewritten by _renderStreaming().
    b.innerHTML =
      '<span class="thinking">' +
      '<span class="thinking-text" id="busyText"></span>' +
      "</span>";
    m.appendChild(b);
    this.root.appendChild(m);
    this.bubble = b;
    this._busyAvatar = avatar;
    this.raw = "";
    this._lastRenderAt = 0;
    this._pendingRender = false;
    this._startBusyPhrase();
    this._scrollToBottom();
  }

  // Busy state — single word "mainframing…" typed one letter at a time,
  // looping. Cleared the moment the first real token arrives.
  _startBusyPhrase() {
    const el = this.bubble && this.bubble.querySelector("#busyText");
    if (!el) return;
    const word = "mainframing";
    let i = 0;
    const tick = () => {
      if (!document.body.contains(el)) return;
      if (i <= word.length) {
        el.textContent = word.slice(0, i);
        i++;
        this._busyTimer = setTimeout(tick, 110);
      } else {
        // Brief pause holding the full word, then erase letter-by-letter
        // and replay so the animation never goes static.
        this._busyTimer = setTimeout(() => eraseTick(), 700);
      }
    };
    const eraseTick = () => {
      if (!document.body.contains(el)) return;
      const cur = el.textContent;
      if (cur.length > 0) {
        el.textContent = cur.slice(0, -1);
        this._busyTimer = setTimeout(eraseTick, 60);
      } else {
        i = 0;
        this._busyTimer = setTimeout(tick, 280);
      }
    };
    tick();
  }

  _stopBusyPhrase() {
    if (this._busyTimer) {
      clearTimeout(this._busyTimer);
      this._busyTimer = null;
    }
    if (this._busyAvatar) {
      this._busyAvatar.classList.remove("is-busy");
      this._busyAvatar = null;
    }
  }

  // Compact AWS-themed mascot — same Squid Ink + Smile Orange palette as
  // the welcome hero. Sized 30×38; CSS scales it to fit beside bubbles.
  _makeMascotAvatar() {
    const wrap = document.createElement("span");
    wrap.className = "m-avatar";
    wrap.setAttribute("aria-hidden", "true");
    wrap.innerHTML = `
      <svg viewBox="0 0 60 76" xmlns="http://www.w3.org/2000/svg">
        <!-- antenna -->
        <line x1="30" y1="10" x2="30" y2="2" stroke="#FF9900" stroke-width="2"/>
        <circle cx="30" cy="2" r="2.4" fill="#FF9900"/>
        <!-- head -->
        <rect x="10" y="10" width="40" height="32" rx="9" fill="#232F3E" stroke="#FF9900" stroke-width="1.6"/>
        <!-- visor recess -->
        <rect x="14" y="20" width="32" height="14" rx="4" fill="#37475A"/>
        <!-- eyes -->
        <circle cx="22" cy="27" r="2.4" fill="#00A4DB"/>
        <circle cx="38" cy="27" r="2.4" fill="#00A4DB"/>
        <!-- AWS smile arrow under head -->
        <path d="M22 38 Q30 43 38 38" fill="none" stroke="#FF9900" stroke-width="1.6" stroke-linecap="round"/>
        <!-- chassis -->
        <rect x="8" y="46" width="44" height="24" rx="7" fill="#232F3E" stroke="#FF9900" stroke-width="1.6"/>
        <!-- chest "AWS" stripe -->
        <rect x="15" y="52" width="30" height="6" rx="1.5" fill="#FFFFFF"/>
        <text x="30" y="57" text-anchor="middle" font-family="Space Grotesk, sans-serif" font-size="4.6" font-weight="800" fill="#232F3E" letter-spacing="0.18em">AWS</text>
        <!-- LEDs -->
        <circle cx="14" cy="64" r="1.4" fill="#FF9900"/>
        <circle cx="19" cy="64" r="1.4" fill="#00A4DB"/>
        <circle cx="24" cy="64" r="1.4" fill="#7DD3FC"/>
      </svg>`;
    return wrap;
  }

  appendToken(text) {
    if (!this.bubble || !text) return;
    this._stopBusyPhrase();
    this.raw += text;

    const now = performance.now();
    const elapsed = now - this._lastRenderAt;

    if (elapsed >= RENDER_INTERVAL_MS) {
      this._renderStreaming();
    } else if (!this._pendingRender) {
      // Schedule one trailing paint so the last few tokens don't sit unrendered
      this._pendingRender = true;
      setTimeout(() => {
        this._pendingRender = false;
        this._renderStreaming();
      }, RENDER_INTERVAL_MS - elapsed);
    }
  }

  _renderStreaming() {
    if (!this.bubble) return;
    this.bubble.innerHTML =
      renderMarkdown(this.raw) + '<span class="cursor"></span>';
    highlightCodeBlocks(this.bubble);
    this._lastRenderAt = performance.now();
    this._scrollToBottom();
  }

  finalize() {
    if (!this.bubble) return "";
    this._stopBusyPhrase();
    const finalRaw = this.raw;
    // Full-fidelity final paint
    this.bubble.innerHTML = renderMarkdown(finalRaw);
    highlightCodeBlocks(this.bubble);
    // Append Copy INSIDE the bubble so it sits below the text. Appending
    // to the row parent would make it a sibling of the avatar/bubble
    // flex children, which pushes it off-screen to the right.
    this._appendCopyButton(this.bubble, finalRaw);
    this.bubble = null;
    this.raw = "";
    this._scrollToBottom();
    return finalRaw;
  }

  showError(msg) {
    this._stopBusyPhrase();
    if (this.bubble) {
      this.bubble.innerHTML = `<span class="error-text">${this._escape(msg)}</span>`;
      this.bubble = null;
      this.raw = "";
    } else {
      const m = document.createElement("div");
      m.className = "m a";
      m.innerHTML = `<div class="b"><span class="error-text">${this._escape(msg)}</span></div>`;
      this.root.appendChild(m);
    }
    this._scrollToBottom();
  }

  appendSystemNote(htmlString) {
    // Used for in-chat informational rendering (drift prompt buttons,
    // customer/LoB switch confirmations, etc.). Different visual style.
    this._ensureWelcomeRemoved();
    const m = document.createElement("div");
    m.className = "m sys";
    const b = document.createElement("div");
    b.className = "b";
    b.innerHTML = htmlString;
    m.appendChild(b);
    this.root.appendChild(m);
    this._scrollToBottom();
    return b;
  }

  _appendCopyButton(parent, text) {
    const btn = document.createElement("button");
    btn.className = "cp";
    btn.textContent = "Copy";
    btn.onclick = async () => {
      const ok = await _copyToClipboard(text);
      btn.textContent = ok ? "Copied" : "Copy failed";
      setTimeout(() => (btn.textContent = "Copy"), 1500);
    };
    parent.appendChild(btn);
  }

  _scrollToBottom() {
    this.root.scrollTop = this.root.scrollHeight;
  }

  _escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    }[c]));
  }
}


/**
 * Cross-context copy. Prefers the modern Clipboard API; falls back to the
 * legacy execCommand path for insecure contexts (HTTP S3 website endpoints
 * don't expose navigator.clipboard).
 */
async function _copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    // fall through to legacy path
  }
  // Legacy fallback — works over HTTP.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  ta.style.pointerEvents = "none";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}
