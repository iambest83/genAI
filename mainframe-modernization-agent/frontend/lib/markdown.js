/**
 * Thin wrapper over marked + highlight.js.
 *
 * Both libs are loaded as classic <script> tags (CDN) in index.html, so they
 * land on `window`. This module just exposes a single `render(rawMd)`
 * function that returns HTML and post-processes for syntax highlighting.
 *
 * Defining this as a module keeps the rest of the code free of `window.*`
 * references and makes it trivial to swap libraries later.
 */

let _configured = false;

function _configureOnce() {
  if (_configured) return;
  if (window.marked) {
    window.marked.setOptions({
      highlight: (code, lang) => {
        try { return window.hljs.highlight(code, { language: lang || "plaintext" }).value; }
        catch (e) { return code; }
      },
    });
  }
  _configured = true;
}

export function renderMarkdown(raw) {
  _configureOnce();
  if (!window.marked) return _escape(raw);
  const html = window.marked.parse(raw || "");
  // Sanitize before any caller assigns to innerHTML. LLM/transcript text is
  // untrusted — without DOMPurify, embedded <script> or onerror= payloads
  // would execute when rendered. Falls back to escaped text if DOMPurify
  // failed to load (CDN unreachable), so the page stays safe degraded.
  if (window.DOMPurify) return window.DOMPurify.sanitize(html);
  return _escape(raw);
}

export function highlightCodeBlocks(rootEl) {
  if (!rootEl || !window.hljs) return;
  rootEl.querySelectorAll("pre code").forEach((el) => window.hljs.highlightElement(el));
}

function _escape(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  }[c]));
}
