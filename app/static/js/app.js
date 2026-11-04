import { api } from "./api.js";
import * as chat from "./chat.js";
import * as kb from "./kb.js";
import * as memory from "./memory.js";
import * as settings from "./settings.js";
import { $, $$, toast } from "./ui.js";

const VIEWS = ["chat", "kb", "memory", "settings"];
const modules = { kb, memory, settings };
const initialized = new Set(["chat"]);
let activeView = "chat";

function viewFromHash() {
  const name = (window.location.hash || "").replace(/^#\/?/, "").trim();
  return VIEWS.includes(name) ? name : "chat";
}

function paint(name) {
  for (const view of VIEWS) {
    $(`#view-${view}`)?.classList.toggle("active", view === name);
  }
  $$(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === name);
  });
  activeView = name;
}

async function showView(name, { reload = true } = {}) {
  if (!VIEWS.includes(name)) name = "chat";
  const changed = name !== activeView;
  paint(name);
  const module = modules[name];
  if (module) {
    if (!initialized.has(name)) {
      initialized.add(name);
      module.init();
    } else if (reload && changed) {
      await module.reload?.();
    }
  } else if (changed && reload) {
    await chat.reload?.();
  }
}

function onHashChange() {
  showView(viewFromHash());
}

async function openSessionFromMemory(sessionId) {
  if (!sessionId) return;
  if (viewFromHash() !== "chat") window.location.hash = "#/chat";
  await showView("chat", { reload: false });
  await chat.refreshSessions();
  await chat.openSession(sessionId);
}

async function refreshHealthDot() {
  const dot = $("#health-dot");
  if (!dot) return;
  try {
    const health = await api.get("/api/health");
    const ok = Boolean(health.database?.ok && health.milvus?.ok);
    dot.className = `health-dot ${ok ? "ok" : "bad"}`;
    dot.title = ok ? "数据库与 Milvus 正常" : "数据库或 Milvus 不可用，去设置页检查";
  } catch (error) {
    dot.className = "health-dot bad";
    dot.title = "无法连接后端";
  }
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => {
      const name = button.dataset.view || "chat";
      if (viewFromHash() === name) showView(name);
      else window.location.hash = `#/${name}`;
    });
  });
  window.addEventListener("hashchange", onHashChange);
  window.addEventListener("insight:open-session", (event) => {
    openSessionFromMemory(event.detail?.sessionId);
  });
}

function bindGlobalErrors() {
  window.addEventListener("unhandledrejection", (event) => {
    const message = event.reason?.message || String(event.reason || "");
    if (message) toast(message, { type: "error", timeout: 7000 });
    console.error(event.reason);
  });
}

function boot() {
  bindNavigation();
  bindGlobalErrors();
  chat.init();
  if (!window.location.hash) {
    history.replaceState(null, "", "#/chat");
  }
  // Initialize the view named by the hash as well, so refreshing on #/kb or
  // #/memory does not leave a painted but empty page.
  showView(viewFromHash(), { reload: false });
  refreshHealthDot();
  window.setInterval(refreshHealthDot, 120_000);
}

boot();
