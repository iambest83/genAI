/**
 * Customer + LoB pickers in the header. Two clickable chips:
 *
 *   [ Customer: <name or "Choose…"> ▾ ]  [ LoB: <name or "Choose…"> ▾ ]
 *
 * Clicking a chip opens a dropdown with the standard set + a "+ Custom"
 * input. Selecting fires the corresponding WS action (`selectCustomer` /
 * `selectLob`). The agent confirms with `customer_bound` / `lob_bound`
 * events; we update the chip from those confirmations (single source of
 * truth lives on the server).
 */

const COMMON_LOBS = [
  "Cards", "Wealth", "Capital Markets", "Retail Banking", "Wholesale Banking",
  "Payments", "P&C Insurance", "Life Insurance", "Specialty Insurance",
  "Investment Management", "Treasury",
];

export class CustomerPicker {
  constructor(rootEl, agentConn) {
    this.root = rootEl;
    this.conn = agentConn;
    this.customer = "";
    this.lob = "";
    this._render();
    this._wireServerEvents();
  }

  _render() {
    this.root.innerHTML = `
      <div class="picker-group">
        <button class="chip chip-cust" id="chipCustomer" type="button">
          <span class="chip-icon" aria-hidden="true">🏛️</span>
          <span class="chip-label">Customer:</span>
          <span class="chip-value" id="chipCustomerVal">Choose…</span>
          <span class="chip-caret">▾</span>
        </button>
        <button class="chip chip-lob" id="chipLob" type="button" disabled>
          <span class="chip-icon" aria-hidden="true">📂</span>
          <span class="chip-label">LoB:</span>
          <span class="chip-value" id="chipLobVal">—</span>
          <span class="chip-caret">▾</span>
        </button>
      </div>

      <div class="dropdown" id="ddCustomer" hidden>
        <div class="dd-title">Set customer</div>
        <input type="text" id="ddCustInput" placeholder="Type a customer name (e.g., JPMC)" />
        <button id="ddCustSubmit">Set customer</button>
        <div class="dd-hint">Customer binding is optional. Skip to use a generic profile.</div>
      </div>

      <div class="dropdown" id="ddLob" hidden>
        <div class="dd-title">Set Line of Business</div>
        <div class="dd-options" id="ddLobOptions"></div>
        <input type="text" id="ddLobInput" placeholder="Or type a custom LoB" />
        <button id="ddLobSubmit">Set LoB</button>
      </div>
    `;

    this.chipCustomer = this.root.querySelector("#chipCustomer");
    this.chipCustomerVal = this.root.querySelector("#chipCustomerVal");
    this.chipLob = this.root.querySelector("#chipLob");
    this.chipLobVal = this.root.querySelector("#chipLobVal");

    this.ddCustomer = this.root.querySelector("#ddCustomer");
    this.ddLob = this.root.querySelector("#ddLob");

    this.chipCustomer.onclick = () => this._toggle(this.ddCustomer, this.ddLob);
    this.chipLob.onclick = () => {
      if (!this.chipLob.disabled) this._toggle(this.ddLob, this.ddCustomer);
    };

    // Customer submit
    const custInput = this.root.querySelector("#ddCustInput");
    const custSubmit = this.root.querySelector("#ddCustSubmit");
    const submitCust = () => {
      const v = custInput.value.trim();
      if (!v) return;
      this.conn.invoke("selectCustomer", { customer_display_name: v });
      custInput.value = "";
      this._toggle(this.ddCustomer, null);
    };
    custInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitCust(); });
    custSubmit.onclick = submitCust;

    // LoB options + custom
    const ddLobOpts = this.root.querySelector("#ddLobOptions");
    ddLobOpts.innerHTML = COMMON_LOBS.map(l =>
      `<button type="button" class="dd-opt" data-lob="${this._escape(l)}">${this._escape(l)}</button>`
    ).join("");
    ddLobOpts.querySelectorAll(".dd-opt").forEach(btn => {
      btn.onclick = () => {
        this.conn.invoke("selectLob", { lob_display_name: btn.dataset.lob });
        this._toggle(this.ddLob, null);
      };
    });
    const lobInput = this.root.querySelector("#ddLobInput");
    const lobSubmit = this.root.querySelector("#ddLobSubmit");
    const submitLob = () => {
      const v = lobInput.value.trim();
      if (!v) return;
      this.conn.invoke("selectLob", { lob_display_name: v });
      lobInput.value = "";
      this._toggle(this.ddLob, null);
    };
    lobInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitLob(); });
    lobSubmit.onclick = submitLob;

    // Click outside closes dropdowns
    document.addEventListener("click", (e) => {
      if (!this.root.contains(e.target)) {
        this.ddCustomer.hidden = true;
        this.ddLob.hidden = true;
      }
    });
  }

  _toggle(toShow, toHide) {
    if (toHide) toHide.hidden = true;
    toShow.hidden = !toShow.hidden;
    if (!toShow.hidden) {
      const inp = toShow.querySelector("input[type=text]");
      if (inp) setTimeout(() => inp.focus(), 0);
    }
  }

  _wireServerEvents() {
    this.conn.on("customer_bound", (e) => {
      this.customer = e.customer_display_name || e.customer_id || "";
      this.chipCustomerVal.textContent = this.customer || "Choose…";
      this.chipCustomer.classList.toggle("is-bound", !!this.customer);
      // Selecting a customer resets the LoB on the server (default)
      this.lob = "";
      this.chipLobVal.textContent = "—";
      this.chipLob.disabled = !this.customer;
      this.chipLob.classList.remove("is-bound");
    });
    this.conn.on("lob_bound", (e) => {
      this.lob = e.lob_display_name || e.lob_id || "";
      this.chipLobVal.textContent = this.lob || "—";
      this.chipLob.classList.toggle("is-bound", !!this.lob);
    });
  }

  _escape(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
    }[c]));
  }
}
