/**
 * Detects two kinds of agent confirmation messages and renders inline
 * action buttons in the assistant bubble:
 *
 *   1. DRIFT prompt (existing) — bound customer profile, but the SA's message
 *      looks like a different customer. Buttons: Yes, switch / No, same.
 *
 *   2. CUSTOMER_DETECT prompt (new) — no customer is bound, but the SA named
 *      what looks like one. Buttons: Yes, set as customer / No, keep generic.
 *      On confirm we bind the customer (selectCustomer), drop a system note
 *      about grounding, and prompt for the LoB.
 *
 * Lightweight pattern match. The marker for CUSTOMER_DETECT is an HTML
 * comment the agent emits inline so we can extract the candidate name
 * even after markdown-to-HTML rendering strips the comment from view.
 */
const DRIFT_MARKER = /Quick check\s*—\s*the profile I have loaded is for\s*\*\*(.+?)\*\*/;
const CUSTOMER_DETECT_MARKER = /<!--\s*CUSTOMER_DETECT:\s*(.+?)\s*-->/;

export function maybeRenderDriftPrompt(rawMd, parentBubble, conn) {
  // 1. Customer-detect (unbound state) — preferred match
  const cd = CUSTOMER_DETECT_MARKER.exec(rawMd);
  if (cd) {
    _renderCustomerDetect(cd[1].trim(), parentBubble, conn);
    return true;
  }

  // 2. Drift (bound state)
  const m = DRIFT_MARKER.exec(rawMd);
  if (!m) return false;

  const wrap = document.createElement("div");
  wrap.className = "drift-actions";
  wrap.innerHTML = `
    <button type="button" class="btn btn-primary" data-act="switch">Yes, switch</button>
    <button type="button" class="btn btn-ghost"   data-act="same">No, same customer</button>
  `;
  parentBubble.appendChild(wrap);

  wrap.querySelector('[data-act="same"]').onclick = () => {
    conn.sendPrompt("No, same customer — please continue.");
    wrap.querySelectorAll("button").forEach(b => b.disabled = true);
  };
  wrap.querySelector('[data-act="switch"]').onclick = () => {
    const chip = document.getElementById("chipCustomer");
    if (chip) chip.click();
    wrap.querySelectorAll("button").forEach(b => b.disabled = true);
  };
  return true;
}


function _renderCustomerDetect(candidate, parentBubble, conn) {
  const wrap = document.createElement("div");
  wrap.className = "drift-actions";
  wrap.innerHTML = `
    <button type="button" class="btn btn-primary" data-act="bind">Yes, set as customer</button>
    <button type="button" class="btn btn-ghost"   data-act="skip">No, keep generic</button>
  `;
  parentBubble.appendChild(wrap);

  wrap.querySelector('[data-act="bind"]').onclick = () => {
    wrap.querySelectorAll("button").forEach(b => b.disabled = true);

    // 1. Bind the customer on the server (chip will update on customer_bound).
    conn.invoke("selectCustomer", { customer_display_name: candidate });

    // 2. Drop a clear, grounded system note in chat so the SA sees the
    //    behavior shift. The customer-picker chip will update via the
    //    customer_bound event from the server.
    const chatRoot = document.getElementById("chat");
    if (chatRoot) {
      const note = document.createElement("div");
      note.className = "m sys";
      note.innerHTML = `
        <div class="b">
          ✓ Set <strong>${_escape(candidate)}</strong> as the customer for this conversation.
          Every answer from here on will be grounded in this customer's
          context — workload shape, constraints, regulations, decisions, and
          prior turns — rather than a generic mainframe-modernization response.
          <br><br>
          If you know which <strong>Line of Business</strong> this conversation
          is about (Cards, Wealth, Capital Markets, Retail Banking, Wholesale,
          P&amp;C, Life, Specialty, Payments, Investment Management, Treasury),
          set it from the <strong>LoB</strong> chip at the top right — it'll
          sharpen the grounding further. You can skip this and just keep
          chatting if not.
        </div>
      `;
      chatRoot.appendChild(note);
      chatRoot.scrollTop = chatRoot.scrollHeight;
    }

    // 3. Open the LoB dropdown to make the next step obvious.
    setTimeout(() => {
      const chipLob = document.getElementById("chipLob");
      if (chipLob && !chipLob.disabled) chipLob.click();
    }, 350);
  };

  wrap.querySelector('[data-act="skip"]').onclick = () => {
    wrap.querySelectorAll("button").forEach(b => b.disabled = true);
    const note = document.createElement("div");
    note.className = "m sys";
    note.innerHTML = `<div class="b">Continuing with generic guidance — re-running your question.</div>`;
    const chatRoot = document.getElementById("chat");
    if (chatRoot) {
      chatRoot.appendChild(note);
      chatRoot.scrollTop = chatRoot.scrollHeight;
    }

    // The SA's original question was hijacked by the customer-detect
    // path and never actually answered. Replay it now so they get the
    // answer they asked for. We grab the most recent user bubble's
    // text (the message they typed before the customer-detect fired).
    const userBubbles = document.querySelectorAll(".chat .m.u .b");
    const last = userBubbles[userBubbles.length - 1];
    const originalQuestion = last ? last.textContent.trim() : "";
    if (originalQuestion && window.__sendPrompt) {
      // Tiny pause so the system note renders before the next turn starts
      setTimeout(() => window.__sendPrompt(originalQuestion), 200);
    }
  };
}


function _escape(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}
