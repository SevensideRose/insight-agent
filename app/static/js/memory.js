import { api } from "./api.js";
import {
  $,
  $$,
  confirmDialog,
  debounce,
  escapeHtml,
  formatTime,
  toast,
} from "./ui.js";

const CATEGORY_LABELS = {
  learning_goal: "学习目标",
  weak_point: "薄弱点",
  mastered_concept: "已掌握",
  cognitive_preference: "解释偏好",
  progress_state: "进度",
  baseline_fact: "背景",
  other: "其他",
};

let category = "all";
let query = "";
let loading = false;

function categoryLabel(value) {
  return CATEGORY_LABELS[value] || value || "其他";
}

function renderCard(memory) {
  const card = document.createElement("article");
  card.className = "memory-card";
  card.innerHTML = `
    <div class="memory-card-head">
      <span class="memory-category ${escapeHtml(memory.category)}">${escapeHtml(categoryLabel(memory.category))}</span>
      <span class="confidence">置信度 ${Math.round((Number(memory.confidence) || 0) * 100)}%</span>
    </div>
    <div class="memory-content"></div>
    <div class="memory-meta">
      <span>${escapeHtml(formatTime(memory.updated_at || memory.created_at))}</span>
      ${memory.source_session_id ? `<button data-action="source">来源对话</button>` : ""}
      <button data-action="delete" title="删除这条记忆">删除</button>
    </div>`;
  card.querySelector(".memory-content").textContent = memory.content;
  card.querySelector('[data-action="delete"]')?.addEventListener("click", async () => {
    if (!(await confirmDialog(`删除记忆“${memory.content}”？`, { danger: true, confirmText: "删除" }))) {
      return;
    }
    try {
      await api.del(`/api/memories/${memory.id}`);
      card.remove();
      toast("记忆已删除", { type: "success" });
      if (!$("#memory-list").children.length) $("#memory-empty")?.classList.remove("hidden");
    } catch (error) {
      toast(error.message, { type: "error" });
    }
  });
  card.querySelector('[data-action="source"]')?.addEventListener("click", () => {
    window.dispatchEvent(
      new CustomEvent("insight:open-session", { detail: { sessionId: memory.source_session_id } })
    );
  });
  return card;
}

function renderMemories(memories) {
  const list = $("#memory-list");
  if (!list) return;
  list.innerHTML = "";
  const empty = $("#memory-empty");
  if (!memories.length) {
    empty?.classList.remove("hidden");
    if (query || category !== "all") {
      empty.querySelector("h2").textContent = "没有匹配的记忆";
      empty.querySelector("p").textContent = "换个关键词或分类试试。";
    } else {
      empty.querySelector("h2").textContent = "还没有记忆";
      empty.querySelector("p").textContent = "聊几轮后，考试时间、薄弱点、解释偏好会自动出现在这里。";
    }
    return;
  }
  empty?.classList.add("hidden");
  for (const memory of memories) list.appendChild(renderCard(memory));
}

async function loadMemories() {
  if (loading) return;
  loading = true;
  const list = $("#memory-list");
  if (list) list.innerHTML = `<div class="subtle">加载中…</div>`;
  try {
    const params = new URLSearchParams();
    if (category && category !== "all") params.set("category", category);
    if (query) params.set("q", query);
    const memories = await api.get(`/api/memories?${params.toString()}`);
    renderMemories(memories);
  } catch (error) {
    if (list) list.innerHTML = `<div class="subtle">加载失败：${escapeHtml(error.message)}</div>`;
    toast(error.message, { type: "error" });
  } finally {
    loading = false;
  }
}

const search = debounce(() => {
  query = $("#memory-search")?.value.trim() || "";
  loadMemories();
}, 280);

export function init() {
  $$("#memory-tabs .tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$("#memory-tabs .tab").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      category = tab.dataset.category || "all";
      loadMemories();
    });
  });
  $("#memory-search")?.addEventListener("input", search);
  $("#memory-refresh")?.addEventListener("click", loadMemories);
  loadMemories();
}

export async function reload() {
  await loadMemories();
}
