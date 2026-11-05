import { api } from "./api.js";
import { store, emit } from "./store.js";
import {
  $,
  $$,
  closeModal,
  confirmDialog,
  escapeHtml,
  formatTime,
  openModal,
  statusLabel,
  toast,
} from "./ui.js";
import { renderMarkdown, enhanceMessage } from "./markdown.js";

let abortController = null;
let sending = false;
let selectedKbIds = [];
let currentSessionId = null;
let pendingAssistant = null;
let pendingContent = "";
let pendingSources = [];
let renderTimer = null;
let lastMemoryPoll = 0;

function currentSession() {
  return store.sessions.find((session) => session.id === currentSessionId) || null;
}

function setEmptyVisible(visible) {
  const empty = $("#chat-empty");
  if (empty) empty.classList.toggle("hidden", !visible);
}

function scrollToBottom() {
  const body = $(".chat-body");
  if (body) body.scrollTop = body.scrollHeight;
}

function updateTitle(title) {
  const node = $("#chat-title");
  if (node) node.textContent = title || "新对话";
}

function updateKbCount() {
  const node = $("#kb-count");
  if (node) node.textContent = String(selectedKbIds.length);
  const button = $("#kb-btn");
  if (button) button.title = selectedKbIds.length ? `已选 ${selectedKbIds.length} 个知识库` : "未选择知识库";
}

function sessionPayload(extra = {}) {
  return {
    kb_ids: selectedKbIds,
    rag_enabled: $("#rag-toggle")?.checked ?? true,
    memory_enabled: $("#memory-toggle")?.checked ?? true,
    ...extra,
  };
}

async function patchSession(extra = {}) {
  if (!currentSessionId) return;
  try {
    const updated = await api.patch(`/api/sessions/${currentSessionId}`, sessionPayload(extra));
    const index = store.sessions.findIndex((session) => session.id === updated.id);
    if (index >= 0) store.sessions[index] = updated;
    else store.sessions.unshift(updated);
    renderSessionList();
  } catch (error) {
    console.warn("session patch failed", error);
  }
}

function openSessionFromList(sessionId) {
  // The session list lives in the sidebar and is visible from every view, so
  // let the app shell switch to chat before loading the conversation.
  window.dispatchEvent(
    new CustomEvent("insight:open-session", { detail: { sessionId } })
  );
}

export async function refreshSessions() {
  try {
    store.sessions = await api.get("/api/sessions");
  } catch (error) {
    console.error(error);
    store.sessions = [];
  }
  renderSessionList();
}

function renderSessionList() {
  const list = $("#session-list");
  if (!list) return;
  list.innerHTML = "";
  if (!store.sessions.length) {
    const empty = document.createElement("div");
    empty.className = "subtle";
    empty.style.padding = "8px 9px";
    empty.textContent = "暂无对话";
    list.appendChild(empty);
    return;
  }
  for (const session of store.sessions) {
    const item = document.createElement("div");
    item.className = `session-item ${session.id === currentSessionId ? "active" : ""}`.trim();
    item.dataset.id = session.id;
    item.setAttribute("role", "button");
    item.tabIndex = 0;

    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = session.title || "新对话";
    item.appendChild(title);

    const time = document.createElement("span");
    time.className = "session-time";
    time.textContent = formatTime(session.updated_at);
    item.appendChild(time);

    const del = document.createElement("button");
    del.className = "session-del";
    del.type = "button";
    del.title = "删除对话";
    del.setAttribute("aria-label", "删除对话");
    del.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none"
      stroke="currentColor" stroke-width="2" stroke-linecap="round"
      stroke-linejoin="round" aria-hidden="true">
      <path d="M3 6h18" />
      <path d="M8 6V4h8v2" />
      <path d="M19 6l-1 14H6L5 6" />
      <path d="M10 11v6" />
      <path d="M14 11v6" />
    </svg>`;
    del.addEventListener("click", async (event) => {
      event.stopPropagation();
      const confirmed = await confirmDialog(
        `删除对话“${session.title || "新对话"}”？消息将一并删除。`,
        { danger: true, confirmText: "删除" }
      );
      if (!confirmed) return;
      try {
        await api.del(`/api/sessions/${session.id}`);
        if (currentSessionId === session.id) newSession();
        await refreshSessions();
        toast("对话已删除", { type: "success" });
      } catch (error) {
        toast(error.message, { type: "error" });
      }
    });
    item.appendChild(del);

    item.addEventListener("click", () => openSessionFromList(session.id));
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openSessionFromList(session.id);
    });
    list.appendChild(item);
  }
}

export function newSession() {
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
  currentSessionId = null;
  store.activeSessionId = null;
  pendingAssistant = null;
  pendingContent = "";
  pendingSources = [];
  const messages = $("#messages");
  if (messages) {
    messages.innerHTML = `
      <div class="empty-chat" id="chat-empty">
        <div class="empty-glyph">知</div>
        <h2>今天想弄懂什么？</h2>
        <p>上传讲义、论文或笔记，知研会带着引用回答，并在对话中记住你的学习状态。</p>
        <div class="starter-grid">
          <button class="starter" data-starter="帮我梳理这份讲义的核心脉络">梳理讲义脉络</button>
          <button class="starter" data-starter="根据我的薄弱点，先复习什么？">按薄弱点复习</button>
          <button class="starter" data-starter="用例子解释这个概念，再给定义">先例子后定义</button>
        </div>
      </div>`;
    bindStarters();
  }
  updateTitle("新对话");
  setEmptyVisible(true);
  renderSessionList();
  $("#composer-input")?.focus();
}

export async function openSession(sessionId) {
  try {
    const session = store.sessions.find((item) => item.id === sessionId);
    const messages = await api.get(`/api/sessions/${sessionId}/messages`);
    currentSessionId = sessionId;
    store.activeSessionId = sessionId;
    updateTitle(session?.title || "对话");
    if (session) {
      selectedKbIds = Array.isArray(session.kb_ids) ? [...session.kb_ids] : [];
      $("#rag-toggle").checked = Boolean(session.rag_enabled);
      $("#memory-toggle").checked = Boolean(session.memory_enabled);
      updateKbCount();
      renderKbOptions();
    }
    const container = $("#messages");
    container.innerHTML = "";
    setEmptyVisible(messages.length === 0);
    for (const message of messages) renderMessage(message);
    renderSessionList();
    scrollToBottom();
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

function renderMessage(message) {
  const container = $("#messages");
  if (!container) return null;
  const wrap = document.createElement("div");
  wrap.className = `message ${message.role === "user" ? "user" : "assistant"}`;
  wrap.dataset.id = message.id;

  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = message.role === "user" ? "你" : "知研";
  wrap.appendChild(meta);

  const content = document.createElement("div");
  content.className = "message-content";
  if (message.role === "user") {
    content.textContent = message.content;
  } else {
    content.innerHTML = renderMarkdown(message.content);
    enhanceMessage(content);
  }
  wrap.appendChild(content);

  if (message.role === "assistant" && message.sources?.length) {
    wrap._sources = message.sources;
    wrap.appendChild(buildSources(message.sources));
  }
  if (message.role === "assistant" && ["error", "cancelled"].includes(message.status)) {
    const status = document.createElement("div");
    status.className = `message-status ${message.status === "error" ? "error" : ""}`.trim();
    status.textContent = statusLabel(message.status);
    wrap.appendChild(status);
  }
  container.appendChild(wrap);
  return wrap;
}

function buildSources(sources) {
  const row = document.createElement("div");
  row.className = "sources";
  for (const source of sources) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "source-chip";
    const label = document.createElement("b");
    label.textContent = `[${source.index}]`;
    chip.appendChild(label);
    chip.append(` ${source.filename || "来源"}${source.page ? ` · 第${source.page}页` : ""}`);
    chip.title = source.heading_path || source.filename || "";
    chip.addEventListener("click", () => openSource(source));
    row.appendChild(chip);
  }
  return row;
}

function openSource(source) {
  const body = document.createElement("div");
  body.className = "source-view";
  const head = document.createElement("div");
  head.className = "source-view-head";
  head.innerHTML = `<span>《${escapeHtml(source.filename || "未知文档")}》</span>
    <span>${escapeHtml(source.heading_path || "正文")}</span>
    ${source.page ? `<span>第 ${source.page} 页</span>` : ""}
    <span>相关度 ${Number(source.score || 0).toFixed(3)}</span>`;
  const text = document.createElement("div");
  text.className = "source-view-text";
  text.textContent = source.text || "";
  body.appendChild(head);
  body.appendChild(text);
  const { modal } = openModal({
    title: `来源 [${source.index}]`,
    body,
    wide: true,
    footer: `<button class="ghost-btn" data-close>关闭</button>`,
  });
  modal.querySelector("[data-close]").addEventListener("click", () => closeModal());
}

function bindStarters() {
  $$(".starter").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $("#composer-input");
      input.value = button.dataset.starter || "";
      autoResize(input);
      send();
    });
  });
}

async function openSourceEvent(event) {
  const cite = event.target.closest(".cite");
  if (!cite) return;
  const message = cite.closest(".message");
  const index = Number(cite.dataset.index);
  const source = message?._sources?.find((item) => Number(item.index) === index);
  if (source) openSource(source);
}

function setBusy(busy) {
  sending = busy;
  $("#send-btn")?.classList.toggle("hidden", busy);
  $("#stop-btn")?.classList.toggle("hidden", !busy);
}

function autoResize(textarea) {
  if (!textarea) return;
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
}

function appendAssistantPlaceholder() {
  const container = $("#messages");
  setEmptyVisible(false);
  const wrap = document.createElement("div");
  wrap.className = "message assistant";
  const meta = document.createElement("div");
  meta.className = "message-meta";
  meta.textContent = "知研";
  wrap.appendChild(meta);
  const content = document.createElement("div");
  content.className = "message-content";
  content.innerHTML = `<span class="typing-dots"><i></i><i></i><i></i></span>`;
  wrap.appendChild(content);
  container.appendChild(wrap);
  pendingAssistant = { wrap, content, sourcesNode: null };
  pendingContent = "";
  pendingSources = [];
  scrollToBottom();
  return pendingAssistant;
}

function renderPending() {
  if (!pendingAssistant) return;
  if (!pendingContent) return;
  if (renderTimer) return;
  renderTimer = window.setTimeout(() => {
    renderTimer = null;
    if (!pendingAssistant) return;
    pendingAssistant.content.innerHTML = renderMarkdown(pendingContent);
    enhanceMessage(pendingAssistant.content);
    scrollToBottom();
  }, 55);
}

function finalizePending(status = "complete") {
  if (!pendingAssistant) return;
  if (renderTimer) {
    clearTimeout(renderTimer);
    renderTimer = null;
  }
  pendingAssistant.content.innerHTML = renderMarkdown(pendingContent);
  enhanceMessage(pendingAssistant.content);
  pendingAssistant.wrap._sources = pendingSources;
  if (pendingSources.length) {
    const existing = pendingAssistant.wrap.querySelector(".sources");
    const sourcesNode = buildSources(pendingSources);
    if (existing) existing.replaceWith(sourcesNode);
    else pendingAssistant.wrap.appendChild(sourcesNode);
  }
  if (status !== "complete") {
    const node = document.createElement("div");
    node.className = `message-status ${status === "error" ? "error" : ""}`.trim();
    node.textContent = statusLabel(status);
    pendingAssistant.wrap.appendChild(node);
  }
  pendingAssistant = null;
  scrollToBottom();
}

function memorySince() {
  return new Date(Date.now() - 5000).toISOString().replace("Z", "+00:00");
}

async function pollMemory(sessionId, startedAt) {
  const attempt = async () => {
    try {
      const events = await api.get(
        `/api/memories/recent?session_id=${encodeURIComponent(sessionId)}&since=${encodeURIComponent(startedAt)}`
      );
      const useful = (events || []).filter((event) => ["ADD", "UPDATE"].includes(event.action));
      const unique = new Map();
      for (const event of useful) unique.set(event.id, event);
      if (unique.size) {
        toast(`已记住 ${unique.size} 条学习状态`, {
          type: "success",
          actionLabel: "查看",
          onAction: () => {
            window.location.hash = "#/memory";
          },
        });
        return true;
      }
    } catch (error) {
      console.warn("memory poll failed", error);
    }
    return false;
  };
  for (const delay of [1800, 4000, 8000]) {
    setTimeout(async () => {
      if (currentSessionId !== sessionId) return;
      const found = await attempt();
      if (found) lastMemoryPoll = Date.now();
    }, delay);
  }
}

export async function send() {
  if (sending) return;
  const input = $("#composer-input");
  const text = (input?.value || "").trim();
  if (!text) return;
  if (!selectedKbIds.length && $("#rag-toggle")?.checked) {
    toast("先选择一个知识库，或关闭 RAG 进行普通对话", { type: "error" });
    return;
  }
  if (!currentSessionId) {
    try {
      const session = await api.post("/api/sessions", {
        title: text.slice(0, 30),
        ...sessionPayload(),
      });
      store.sessions.unshift(session);
      currentSessionId = session.id;
      store.activeSessionId = session.id;
      updateTitle(session.title);
    } catch (error) {
      toast(error.message, { type: "error" });
      return;
    }
  }

  input.value = "";
  autoResize(input);
  setBusy(true);
  const startedAt = memorySince();

  const userWrap = document.createElement("div");
  userWrap.className = "message user";
  const userMeta = document.createElement("div");
  userMeta.className = "message-meta";
  userMeta.textContent = "你";
  const userContent = document.createElement("div");
  userContent.className = "message-content";
  userContent.textContent = text;
  userWrap.appendChild(userMeta);
  userWrap.appendChild(userContent);
  $("#messages").appendChild(userWrap);
  setEmptyVisible(false);
  appendAssistantPlaceholder();

  abortController = new AbortController();
  const payload = {
    session_id: currentSessionId,
    message: text,
    kb_ids: selectedKbIds,
    rag_enabled: $("#rag-toggle")?.checked ?? true,
    memory_enabled: $("#memory-toggle")?.checked ?? true,
    retrieval_mode: $("#retrieval-mode")?.value || "hybrid",
  };

  try {
    await api.streamChat(
      payload,
      (event, data) => {
        if (event === "meta") {
          if (data.session_id) currentSessionId = data.session_id;
        } else if (event === "token") {
          pendingContent += data.text || "";
          renderPending();
        } else if (event === "sources") {
          pendingSources = data.sources || [];
          if (pendingSources.length && pendingAssistant) {
            const existing = pendingAssistant.wrap.querySelector(".sources");
            const node = buildSources(pendingSources);
            if (existing) existing.replaceWith(node);
            else pendingAssistant.wrap.appendChild(node);
            pendingAssistant.wrap._sources = pendingSources;
          }
        } else if (event === "memory") {
          if (data.count > 0 && pendingAssistant) {
            const note = document.createElement("div");
            note.className = "memory-note";
            note.textContent = `参考了 ${data.count} 条学习记忆`;
            pendingAssistant.wrap.insertBefore(note, pendingAssistant.content);
          }
        } else if (event === "warning") {
          toast(data.message || "检索提示", { type: "", timeout: 6000 });
        } else if (event === "error") {
          pendingContent = pendingContent || `生成失败：${data.message || "未知错误"}`;
          finalizePending("error");
          toast(data.message || "生成失败", { type: "error", timeout: 8000 });
        } else if (event === "done") {
          finalizePending("complete");
          pollMemory(currentSessionId, startedAt);
          refreshSessions();
        }
      },
      abortController.signal
    );
  } catch (error) {
    if (error.name === "AbortError") {
      finalizePending("cancelled");
    } else {
      finalizePending("error");
      toast(error.message || "请求失败", { type: "error", timeout: 8000 });
    }
  } finally {
    abortController = null;
    setBusy(false);
    refreshSessions();
  }
}

export function stop() {
  if (abortController) abortController.abort();
}

async function loadKbOptions() {
  try {
    store.kbs = await api.get("/api/kbs");
  } catch (error) {
    console.error(error);
    store.kbs = [];
  }
  renderKbOptions();
}

function renderKbOptions() {
  const panel = $("#kb-panel");
  if (!panel) return;
  panel.innerHTML = "";
  if (!store.kbs.length) {
    panel.innerHTML = `<div class="dropdown-empty">还没有知识库<br>去“知识库”页新建一个</div>`;
    updateKbCount();
    return;
  }
  for (const kb of store.kbs) {
    const label = document.createElement("label");
    label.className = "kb-option";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = kb.id;
    checkbox.checked = selectedKbIds.includes(kb.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) {
        if (!selectedKbIds.includes(kb.id)) selectedKbIds.push(kb.id);
      } else {
        selectedKbIds = selectedKbIds.filter((id) => id !== kb.id);
      }
      updateKbCount();
      selectedKbIds = selectedKbIds.filter((id) => store.kbs.some((item) => item.id === id));
      patchSession({ kb_ids: selectedKbIds });
    });
    label.appendChild(checkbox);
    const name = document.createElement("span");
    name.textContent = kb.name;
    label.appendChild(name);
    const count = document.createElement("span");
    count.className = "kb-option-count";
    count.textContent = `${kb.document_count} 份`;
    label.appendChild(count);
    panel.appendChild(label);
  }
  updateKbCount();
}

export function init() {
  $("#new-session-btn")?.addEventListener("click", newSession);
  $("#send-btn")?.addEventListener("click", send);
  $("#stop-btn")?.addEventListener("click", stop);
  $("#kb-btn")?.addEventListener("click", (event) => {
    event.stopPropagation();
    $("#kb-dropdown")?.classList.toggle("open");
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#kb-dropdown")) $("#kb-dropdown")?.classList.remove("open");
  });
  $("#rag-toggle")?.addEventListener("change", () => patchSession());
  $("#memory-toggle")?.addEventListener("change", () => patchSession());
  $("#retrieval-mode")?.addEventListener("change", () => patchSession());

  const input = $("#composer-input");
  if (input) {
    input.addEventListener("input", () => autoResize(input));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
  }
  bindStarters();
  $("#messages")?.addEventListener("click", openSourceEvent);

  loadKbOptions();
  refreshSessions();
}

export async function reload() {
  await loadKbOptions();
  await refreshSessions();
}
