/**
 * Extraction preview card (Iteration 4.1 + 4.6).
 *
 * Renders the structured `meeting_preview` event from the agent as an
 * inline chat card with one checkbox per extracted item. Sections:
 *
 *   • Facts          — workload / constraints, with field path + value + quote
 *   • Decisions      — pattern / partner / target service commitments
 *   • Action items   — owner + text + optional due
 *   • Open questions — what still needs answering
 *
 * The SA ticks the rows they trust, hits "Confirm & merge", and we send
 * the chosen row_ids back to the agent via `confirmMeetingMerge`.
 *
 * Default behavior: every checkbox starts CHECKED. The SA's job is to
 * uncheck the ones they don't want — most extractions are mostly correct.
 */

export function renderMeetingPreview(preview, conn) {
  if (!preview || preview.error) {
    return _renderError(preview && preview.error);
  }

  const chatRoot = document.getElementById("chat");
  if (!chatRoot) return;

  // Outer system-style card
  const card = document.createElement("div");
  card.className = "m sys preview-card";
  card.dataset.previewId = preview.preview_id || "";

  const lobLabel = (preview.lob_id && preview.lob_id !== "default")
    ? `${_esc(preview.customer_display_name || preview.customer_id)} / ${_esc(preview.lob_display_name || preview.lob_id)}`
    : _esc(preview.customer_display_name || preview.customer_id);

  card.innerHTML = `
    <div class="b preview-body">
      <div class="preview-head">
        <span class="preview-title">📋 Meeting notes — preview</span>
        <span class="preview-target">${lobLabel}</span>
      </div>
      <div class="preview-counts">
        ${_pill("facts",          preview.counts && preview.counts.facts || 0)}
        ${_pill("decisions",      preview.counts && preview.counts.decisions || 0)}
        ${_pill("action items",   preview.counts && preview.counts.action_items || 0)}
        ${_pill("open questions", preview.counts && preview.counts.open_questions || 0)}
      </div>
      <p class="preview-help">
        Nothing has been merged yet. Untick anything you don't trust, then
        confirm to apply the rest into <strong>${lobLabel}</strong>'s profile.
      </p>
      ${_renderSection("Facts",          preview.facts,          _renderFact)}
      ${_renderSection("Decisions",      preview.decisions,      _renderDecision)}
      ${_renderSection("Action items",   preview.action_items,   _renderAction)}
      ${_renderSection("Open questions", preview.open_questions, _renderQuestion)}
      <div class="preview-actions">
        <button class="btn btn-ghost"   data-act="cancel">Cancel</button>
        <button class="btn btn-primary" data-act="confirm">Confirm & merge</button>
      </div>
    </div>
  `;
  chatRoot.appendChild(card);
  chatRoot.scrollTop = chatRoot.scrollHeight;

  // Inline-edit pencil on every row. Clicking the ✎ swaps the row's
  // editable span for an <input>; saving (Enter / blur) writes back to
  // both the DOM and the underlying preview object so a subsequent
  // Confirm & merge picks up the SA's edit.
  card.querySelectorAll(".preview-edit").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      const row = btn.closest(".preview-row");
      if (!row) return;
      const target = row.querySelector(".preview-editable");
      if (!target || row.dataset.editing === "1") return;
      row.dataset.editing = "1";
      const original = target.textContent;
      const field = target.dataset.edit;
      const rowId = row.dataset.rowId;
      let item = null;
      for (const k of ["facts","decisions","action_items","open_questions"]) {
        const hit = (preview[k] || []).find((it) => it.row_id === rowId);
        if (hit) { item = hit; break; }
      }

      const input = document.createElement("input");
      input.type = "text";
      input.className = "preview-edit-input";
      input.value = original;
      target.replaceWith(input);
      input.focus();
      input.select();

      const save = () => {
        const next = (input.value || "").trim();
        // Mutate the underlying preview object so confirm-merge sees it
        if (item && field) {
          if (field === "value" && Array.isArray(item.value)) {
            // Comma-separated list editing
            item.value = next ? next.split(",").map((s) => s.trim()).filter(Boolean) : [];
          } else {
            item[field] = next;
          }
        }
        // Restore the editable span with the new text
        const span = document.createElement(target.tagName.toLowerCase());
        span.className = target.className;
        span.dataset.edit = field;
        span.textContent = field === "value" && Array.isArray(item?.value)
          ? item.value.join(", ")
          : next;
        input.replaceWith(span);
        delete row.dataset.editing;
      };

      input.addEventListener("blur", save);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        else if (e.key === "Escape") {
          // Revert
          const span = document.createElement(target.tagName.toLowerCase());
          span.className = target.className;
          span.dataset.edit = field;
          span.textContent = original;
          input.replaceWith(span);
          delete row.dataset.editing;
        }
      });
    });
  });

  // Wire actions
  card.querySelector('[data-act="cancel"]').addEventListener("click", () => {
    card.querySelectorAll("button").forEach(b => b.disabled = true);
    const note = document.createElement("div");
    note.className = "preview-status";
    note.textContent = "Discarded — nothing was merged.";
    card.querySelector(".preview-body").appendChild(note);
  });

  card.querySelector('[data-act="confirm"]').addEventListener("click", () => {
    const ids = Array.from(card.querySelectorAll("input[type=checkbox]:checked"))
      .map(cb => cb.value);
    if (ids.length === 0) {
      const note = document.createElement("div");
      note.className = "preview-status";
      note.textContent = "Nothing ticked — nothing to merge.";
      card.querySelector(".preview-body").appendChild(note);
      return;
    }
    card.querySelectorAll("button").forEach(b => b.disabled = true);
    if (window.__confirmMeetingMerge) {
      window.__confirmMeetingMerge(preview, ids);
    }
    const note = document.createElement("div");
    note.className = "preview-status";
    note.textContent = `Merging ${ids.length} item${ids.length === 1 ? "" : "s"}…`;
    card.querySelector(".preview-body").appendChild(note);
  });
}

// Renders the merge-result event the agent emits after a confirm.
export function renderMergeResult(result) {
  const chatRoot = document.getElementById("chat");
  if (!chatRoot || !result) return;
  const card = document.createElement("div");
  card.className = "m sys";
  if (result.error) {
    card.innerHTML = `<div class="b"><span class="error-text">Merge failed: ${_esc(result.error)}</span></div>`;
  } else {
    const f  = result.applied_facts     || 0;
    const d  = result.applied_decisions || 0;
    const q  = result.applied_questions || 0;
    const cs = (result.contradictions || []).length;
    const parts = [];
    if (f)  parts.push(`${f} fact${f === 1 ? "" : "s"}`);
    if (d)  parts.push(`${d} decision${d === 1 ? "" : "s"}`);
    if (q)  parts.push(`${q} action/question item${q === 1 ? "" : "s"}`);
    const summary = parts.length ? parts.join(", ") : "nothing";
    let body = `✓ Merged <strong>${summary}</strong> into the profile.`;
    if (cs) {
      body += ` <em>${cs} contradiction${cs === 1 ? "" : "s"} flagged for review.</em>`;
    }
    card.innerHTML = `<div class="b">${body}</div>`;
  }
  chatRoot.appendChild(card);
  chatRoot.scrollTop = chatRoot.scrollHeight;
}

// --- Helpers ----------------------------------------------------------------

function _renderError(msg) {
  const chatRoot = document.getElementById("chat");
  if (!chatRoot) return;
  const card = document.createElement("div");
  card.className = "m sys";
  card.innerHTML = `<div class="b"><span class="error-text">${_esc(msg || "Extraction failed")}</span></div>`;
  chatRoot.appendChild(card);
  chatRoot.scrollTop = chatRoot.scrollHeight;
}

function _pill(label, count) {
  return `<span class="preview-pill"><b>${count}</b> ${label}</span>`;
}

function _renderSection(title, items, renderRow) {
  if (!items || items.length === 0) return "";
  const rows = items.map(renderRow).join("");
  return `
    <div class="preview-section">
      <div class="preview-section-h">${_esc(title)}</div>
      <ul class="preview-list">${rows}</ul>
    </div>
  `;
}

function _row(rowId, primary, secondary) {
  // The row is keyed by row_id (so edit + checkbox handlers can find it
  // and so the merge step can correlate with the preview object).
  return `
    <li class="preview-row" data-row-id="${_esc(rowId)}">
      <label>
        <input type="checkbox" value="${_esc(rowId)}" checked />
        <div class="preview-row-body">
          <div class="preview-row-primary">${primary}</div>
          ${secondary ? `<div class="preview-row-secondary">${secondary}</div>` : ""}
        </div>
      </label>
      <button class="preview-edit" type="button" title="Edit this item">✎</button>
    </li>
  `;
}

function _renderFact(f) {
  const fp = f.field_path ? `<code>${_esc(f.field_path)}</code>` : `<em>(unstructured)</em>`;
  const valTxt = f.value !== undefined ? _stringify(f.value) : "";
  const v  = `<strong class="preview-editable" data-edit="value">${_esc(valTxt)}</strong>`;
  const speaker = f.speaker ? `<span class="preview-speaker">${_esc(f.speaker)}</span> · ` : "";
  const quote = f.quote ? `<span class="preview-quote">${speaker}"${_esc(f.quote)}"</span>` : speaker;
  return _row(f.row_id, `${fp} = ${v}`, quote);
}

function _renderDecision(d) {
  const cat = d.category ? `<code>${_esc(d.category)}</code>` : "";
  const v   = `<strong class="preview-editable" data-edit="value">${_esc(d.value || "")}</strong>`;
  const r   = d.rationale ? ` — <em>${_esc(d.rationale)}</em>` : "";
  const speaker = d.speaker ? `<span class="preview-speaker">${_esc(d.speaker)}</span> · ` : "";
  const quote = d.quote ? `<span class="preview-quote">${speaker}"${_esc(d.quote)}"</span>` : speaker;
  return _row(d.row_id, `${cat} = ${v}${r}`, quote);
}

function _renderAction(a) {
  const owner = a.owner ? `<strong>${_esc(a.owner)}</strong>` : "<em>unspecified</em>";
  const due   = a.due ? ` <span class="preview-due">(${_esc(a.due)})</span>` : "";
  const text  = `<span class="preview-editable" data-edit="text">${_esc(a.text || "")}</span>`;
  return _row(a.row_id, `${owner}: ${text}${due}`, "");
}

function _renderQuestion(q) {
  const blocks = q.blocks ? `<span class="preview-secondary">blocks: ${_esc(q.blocks)}</span>` : "";
  const text   = `<span class="preview-editable" data-edit="text">${_esc(q.text || "")}</span>`;
  return _row(q.row_id, text, blocks);
}

function _stringify(v) {
  if (Array.isArray(v)) return v.join(", ");
  return String(v);
}

function _esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}
