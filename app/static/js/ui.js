export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso).slice(0, 16).replace("T", " ");
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const time = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return time;
  return `${date.getMonth() + 1}/${date.getDate()} ${time}`;
}

export const STATUS_LABELS = {
  uploaded: "待处理",
  parsing: "解析中",
  chunking: "分块中",
  embedding: "向量化",
  ready: "就绪",
  failed: "失败",
  queued: "排队中",
  running: "处理中",
  succeeded: "完成",
  streaming: "生成中",
  complete: "完成",
  error: "错误",
  cancelled: "已停止",
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

let toastCounter = 0;

export function toast(message, { type = "", actionLabel = "", onAction = null, timeout = 4200 } = {}) {
  const container = $("#toast-container");
  if (!container) return;
  const node = document.createElement("div");
  node.className = `toast ${type}`.trim();
  node.dataset.id = String(++toastCounter);
  const text = document.createElement("span");
  text.textContent = message;
  node.appendChild(text);
  if (actionLabel && onAction) {
    const button = document.createElement("button");
    button.textContent = actionLabel;
    button.addEventListener("click", () => {
      onAction();
      node.remove();
    });
    node.appendChild(button);
  }
  container.appendChild(node);
  if (timeout > 0) {
    setTimeout(() => node.remove(), timeout);
  }
}

export function openModal({ title, body, wide = false, footer = "", onClose = null }) {
  closeModal();
  const root = $("#modal-root");
  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  const modal = document.createElement("div");
  modal.className = `modal ${wide ? "wide" : ""}`.trim();
  modal.innerHTML = `
    <div class="modal-head">
      <h3>${escapeHtml(title)}</h3>
      <button class="modal-close" type="button" aria-label="关闭">×</button>
    </div>
    <div class="modal-body"></div>
    <div class="modal-foot">${footer}</div>
  `;
  const bodyNode = modal.querySelector(".modal-body");
  if (typeof body === "string") bodyNode.innerHTML = body;
  else if (body) bodyNode.appendChild(body);
  const close = () => {
    backdrop.remove();
    if (onClose) onClose();
  };
  modal.querySelector(".modal-close").addEventListener("click", close);
  backdrop.addEventListener("click", (event) => {
    if (event.target === backdrop) close();
  });
  backdrop.appendChild(modal);
  root.appendChild(backdrop);
  return { modal, close, body: bodyNode };
}

export function closeModal() {
  const root = $("#modal-root");
  if (root) root.innerHTML = "";
}

export function confirmDialog(message, { title = "确认", danger = false, confirmText = "确定" } = {}) {
  return new Promise((resolve) => {
    openModal({
      title,
      body: `<p style="margin:0;font-size:13px;line-height:1.7;">${escapeHtml(message)}</p>`,
      footer: `<button class="ghost-btn" data-close>取消</button>
               <button class="${danger ? "danger-ghost" : "primary-btn"}" data-confirm>${escapeHtml(confirmText)}</button>`,
    });
    const root = $("#modal-root");
    const finish = (value) => {
      closeModal();
      resolve(value);
    };
    root.querySelector("[data-close]").addEventListener("click", () => finish(false));
    root.querySelector("[data-confirm]").addEventListener("click", () => finish(true));
  });
}

export function debounce(fn, delay = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
