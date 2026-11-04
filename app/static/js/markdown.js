import { escapeHtml } from "./ui.js";

function fallbackMarkdown(text) {
  return escapeHtml(text).replace(/\n/g, "<br>");
}

export function renderMarkdown(text) {
  const source = String(text ?? "");
  if (!source.trim()) return "";
  if (window.marked && typeof window.marked.parse === "function") {
    try {
      window.marked.setOptions({ gfm: true, breaks: true });
      const raw = window.marked.parse(source);
      if (window.DOMPurify) {
        return window.DOMPurify.sanitize(raw, {
          USE_PROFILES: { html: true },
          ADD_ATTR: ["target", "rel"],
        });
      }
      // Without the sanitizer, never inject marked's raw HTML.
      return fallbackMarkdown(source);
    } catch (error) {
      console.warn("markdown render failed", error);
    }
  }
  return fallbackMarkdown(source);
}

function linkCitations(container) {
  if (!container) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.nodeValue || !/\[\d+\]/.test(node.nodeValue)) continue;
    const parent = node.parentElement;
    if (!parent) continue;
    if (parent.closest("pre, code, a, .cite")) continue;
    nodes.push(node);
  }
  for (const node of nodes) {
    const parts = node.nodeValue.split(/(\[\d+\])/g);
    if (parts.length < 2) continue;
    const fragment = document.createDocumentFragment();
    for (const part of parts) {
      const match = part.match(/^\[(\d+)\]$/);
      if (match) {
        const span = document.createElement("span");
        span.className = "cite";
        span.dataset.index = match[1];
        span.textContent = part;
        fragment.appendChild(span);
      } else if (part) {
        fragment.appendChild(document.createTextNode(part));
      }
    }
    node.parentNode.replaceChild(fragment, node);
  }
}

export function enhanceMessage(container) {
  if (!container) return;
  container.querySelectorAll("pre code").forEach((block) => {
    if (window.hljs) {
      try {
        window.hljs.highlightElement(block);
      } catch (_) {
        /* ignore */
      }
    }
  });
  linkCitations(container);
}

export function setMessageHtml(container, text) {
  container.innerHTML = renderMarkdown(text);
  enhanceMessage(container);
}
