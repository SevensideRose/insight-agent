import { api } from "./api.js";
import { store } from "./store.js";
import {
  $,
  $$,
  closeModal,
  confirmDialog,
  escapeHtml,
  formatBytes,
  formatTime,
  openModal,
  statusLabel,
  toast,
} from "./ui.js";

let activeKbId = null;
let pendingFiles = [];
let documents = [];
let jobStates = new Map();
let pollTimers = new Set();
let previewState = null;

const STRATEGY_HINTS = {
  fixed: "固定大小：按字符硬切，可在 100–1024 之间选择。适合结构均匀的资料。",
  recursive: "递归字符：先段落再句子，适合大多数中文讲义。",
  semantic: "语义分块：按句子向量距离找断点，CPU 下较慢，适合概念密集的资料。",
  structure: "文档结构：按标题层级分组，适合有清晰章节目录的讲义。",
  ai: "AI 分块：由 DeepSeek 按你的要求决定边界，只移动边界、不改写正文。",
};

function activeKb() {
  return store.kbs.find((kb) => kb.id === activeKbId) || null;
}

function renderKbList() {
  const list = $("#kb-list");
  if (!list) return;
  list.innerHTML = "";
  if (!store.kbs.length) {
    list.innerHTML = `<div class="subtle" style="padding:8px 4px;">还没有知识库</div>`;
    return;
  }
  for (const kb of store.kbs) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `kb-item ${kb.id === activeKbId ? "active" : ""}`.trim();
    button.innerHTML = `<div class="kb-item-name">${escapeHtml(kb.name)}</div>
      <div class="kb-item-meta">${kb.document_count} 份文档</div>`;
    button.addEventListener("click", () => selectKb(kb.id));
    list.appendChild(button);
  }
}

async function selectKb(kbId) {
  const switched = activeKbId !== kbId;
  activeKbId = kbId;
  store.activeKbId = kbId;
  renderKbList();
  const kb = activeKb();
  if (!kb) return;
  $("#kb-empty")?.classList.add("hidden");
  $("#kb-detail-panel")?.classList.remove("hidden");
  $("#kb-name").textContent = kb.name;
  $("#kb-desc").textContent = kb.description || "暂无描述";
  if (switched) {
    documents = [];
    renderDocuments();
  }
  await loadDocuments();
}

async function loadKbs() {
  try {
    store.kbs = await api.get("/api/kbs");
  } catch (error) {
    toast(error.message, { type: "error" });
    store.kbs = [];
  }
  if (activeKbId && !store.kbs.some((kb) => kb.id === activeKbId)) {
    activeKbId = null;
    store.activeKbId = null;
    $("#kb-detail-panel")?.classList.add("hidden");
    $("#kb-empty")?.classList.remove("hidden");
  }
  renderKbList();
  if (!activeKbId && store.kbs.length) {
    await selectKb(store.kbs[0].id);
  }
}

function openCreateKb() {
  const { modal, close } = openModal({
    title: "新建知识库",
    body: `
      <label class="field" style="margin-bottom:10px;">
        <span>名称</span>
        <input id="new-kb-name" maxlength="120" placeholder="例如：线性代数" />
      </label>
      <label class="field">
        <span>描述（可选）</span>
        <textarea id="new-kb-desc" rows="2" maxlength="1000" placeholder="这门课用的教材、考试范围…"></textarea>
      </label>`,
    footer: `<button class="ghost-btn" data-close>取消</button>
             <button class="primary-btn" data-create>创建</button>`,
  });
  const nameInput = modal.querySelector("#new-kb-name");
  nameInput?.focus();
  modal.querySelector("[data-close]").addEventListener("click", close);
  modal.querySelector("[data-create]").addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) {
      toast("请输入知识库名称", { type: "error" });
      return;
    }
    try {
      const kb = await api.post("/api/kbs", {
        name,
        description: modal.querySelector("#new-kb-desc").value.trim(),
      });
      close();
      store.kbs.unshift(kb);
      activeKbId = kb.id;
      renderKbList();
      await selectKb(kb.id);
      toast("知识库已创建", { type: "success" });
    } catch (error) {
      toast(error.message, { type: "error" });
    }
  });
}

function addFiles(fileList) {
  const maxBytes = (store.settings.max_upload_mb || 100) * 1024 * 1024;
  const allowed = ["docx", "md", "markdown", "html", "htm", "pdf"];
  for (const file of Array.from(fileList || [])) {
    const extension = (file.name.split(".").pop() || "").toLowerCase();
    if (!allowed.includes(extension)) {
      toast(`不支持 ${file.name}，只接受 docx / md / html / pdf`, { type: "error" });
      continue;
    }
    if (file.size > maxBytes) {
      toast(`${file.name} 超过 ${store.settings.max_upload_mb || 100} MB`, { type: "error" });
      continue;
    }
    if (pendingFiles.some((item) => item.name === file.name && item.size === file.size)) continue;
    pendingFiles.push(file);
  }
  renderFileList();
}

function renderFileList() {
  const list = $("#file-list");
  if (!list) return;
  list.innerHTML = "";
  pendingFiles.forEach((file, index) => {
    const pill = document.createElement("span");
    pill.className = "file-pill";
    pill.innerHTML = `<span>${escapeHtml(file.name)}</span><span style="opacity:.55">${formatBytes(file.size)}</span>`;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "移除";
    remove.addEventListener("click", () => {
      pendingFiles.splice(index, 1);
      renderFileList();
    });
    pill.appendChild(remove);
    list.appendChild(pill);
  });
}

function readConfig() {
  return {
    strategy: $("#chunk-strategy").value,
    chunk_size: Number($("#chunk-size").value),
    chunk_overlap: Number($("#chunk-overlap").value),
    min_chunk_size: 80,
    semantic_percentile: Number($("#semantic-percentile").value),
    ai_instructions: $("#ai-instructions").value.trim(),
    parent_child: $("#parent-child").checked,
    parent_size: Number($("#parent-size").value),
  };
}

function updateStrategyUi() {
  const strategy = $("#chunk-strategy")?.value || "recursive";
  const size = $("#chunk-size");
  if (!size) return;
  if (strategy === "fixed") {
    size.min = "100";
    size.max = "1024";
    if (Number(size.value) > 1024) size.value = "1024";
    if (Number(size.value) < 100) size.value = "100";
  } else {
    size.min = "200";
    size.max = "4096";
    if (Number(size.value) < 200) size.value = "200";
  }
  $("#semantic-field")?.classList.toggle("hidden", strategy !== "semantic");
  $("#ai-field")?.classList.toggle("hidden", strategy !== "ai");
  const hint = $("#chunk-hint");
  if (hint) hint.textContent = STRATEGY_HINTS[strategy] || "";
}

async function upload() {
  const kb = activeKb();
  if (!kb) {
    toast("先选择一个知识库", { type: "error" });
    return;
  }
  if (!pendingFiles.length) {
    toast("先选择要上传的文件", { type: "error" });
    return;
  }
  const config = readConfig();
  if (config.parent_child && config.parent_size <= config.chunk_size) {
    toast("父块大小必须大于块大小", { type: "error" });
    return;
  }
  const button = $("#upload-btn");
  button.disabled = true;
  button.textContent = "上传中…";
  try {
    const results = await api.uploadFiles(kb.id, pendingFiles, config);
    let ok = 0;
    for (const result of results || []) {
      if (result.error) {
        toast(`${result.filename}：${result.error}`, { type: "error", timeout: 8000 });
      } else if (result.job_id) {
        ok += 1;
        pollJob(result.job_id, result.document_id);
      }
    }
    if (ok) toast(`已提交 ${ok} 个文件，后台开始索引`, { type: "success" });
    pendingFiles = [];
    renderFileList();
    await loadDocuments();
    await loadKbs();
  } catch (error) {
    toast(error.message, { type: "error", timeout: 8000 });
  } finally {
    button.disabled = false;
    button.textContent = "开始上传并索引";
  }
}

function pollJob(jobId, documentId) {
  const tick = async () => {
    try {
      const job = await api.get(`/api/jobs/${jobId}`);
      jobStates.set(documentId, job);
      renderDocuments();
      if (job.status === "succeeded" || job.status === "failed") {
        clearInterval(timer);
        pollTimers.delete(timer);
        if (job.status === "failed") {
          toast(`索引失败：${job.error || "未知错误"}`, { type: "error", timeout: 9000 });
        }
        await loadDocuments();
      }
    } catch (error) {
      clearInterval(timer);
      pollTimers.delete(timer);
    }
  };
  const timer = setInterval(tick, 1000);
  pollTimers.add(timer);
  tick();
}

async function loadDocuments() {
  if (!activeKbId) return;
  try {
    const docs = await api.get(`/api/kbs/${activeKbId}/documents`);
    if (Array.isArray(docs)) documents = docs;
    renderDocuments();
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

function renderDocuments(docs) {
  if (Array.isArray(docs)) documents = docs;
  const tbody = $("#doc-list");
  if (!tbody) return;
  tbody.innerHTML = "";
  $("#doc-empty")?.classList.toggle("hidden", documents.length > 0);
  for (const doc of documents) {
    const tr = document.createElement("tr");
    const job = jobStates.get(doc.id);
    const status = job && ["queued", "running"].includes(job.status) ? "running" : doc.status;
    const progress = job && job.status === "running" ? job.progress : null;
    const statusCell = document.createElement("td");
    statusCell.innerHTML = `<span class="status status-${escapeHtml(status)}">${statusLabel(status)}</span>`;
    if (progress !== null) {
      statusCell.insertAdjacentHTML(
        "beforeend",
        `<div class="progress-track"><div class="progress-bar" style="width:${Math.max(2, progress)}%"></div></div>
         <div class="progress-label">${escapeHtml(job.stage || "")} ${progress}%</div>`
      );
    }
    tr.innerHTML = `
      <td class="doc-name" title="${escapeHtml(doc.filename)}">${escapeHtml(doc.filename)}</td>
      <td></td>
      <td>${doc.chunk_count || 0}</td>
      <td>${formatBytes(doc.size_bytes)}</td>
      <td>${formatTime(doc.created_at)}</td>
      <td><div class="doc-actions">
        <button data-action="preview">预览</button>
        <button data-action="reindex">重建</button>
        <button data-action="delete" class="danger">删除</button>
      </div></td>`;
    tr.children[1].replaceWith(statusCell);
    if (doc.error) tr.title = doc.error;
    tr.querySelector('[data-action="preview"]').addEventListener("click", () => previewDoc(doc));
    tr.querySelector('[data-action="reindex"]').addEventListener("click", () => reindexDoc(doc));
    tr.querySelector('[data-action="delete"]').addEventListener("click", () => deleteDoc(doc));
    tbody.appendChild(tr);
  }
}

async function previewDoc(doc) {
  previewState = { doc, offset: 0, limit: 12 };
  const { modal } = openModal({
    title: `分块预览 · ${doc.filename}`,
    body: `<div id="chunk-preview" class="chunk-list"><div class="subtle">加载中…</div></div>`,
    wide: true,
    footer: `<button class="ghost-btn" data-close>关闭</button>`,
  });
  modal.querySelector("[data-close]").addEventListener("click", () => closeModal());
  await renderPreview(modal);
}

async function renderPreview(modal) {
  const container = modal.querySelector("#chunk-preview");
  if (!container || !previewState) return;
  container.innerHTML = `<div class="subtle">加载中…</div>`;
  try {
    const data = await api.get(
      `/api/documents/${previewState.doc.id}/chunks?level=1&limit=${previewState.limit}&offset=${previewState.offset}`
    );
    container.innerHTML = "";
    if (!data.items.length) {
      container.innerHTML = `<div class="subtle">没有子块。若启用了父子分块，可到数据库查看父块。</div>`;
      return;
    }
    for (const chunk of data.items) {
      const item = document.createElement("div");
      item.className = "chunk-item";
      item.innerHTML = `<div class="chunk-item-head">
          <span>#${chunk.chunk_index}</span>
          <span>${escapeHtml(chunk.heading_path || "正文")}</span>
          ${chunk.page ? `<span>第 ${chunk.page} 页</span>` : ""}
          <span>${chunk.char_count} 字</span>
          <span>${chunk.parent_id ? "子块" : "普通块"}</span>
        </div>
        <div class="chunk-item-text">${escapeHtml(chunk.text)}</div>`;
      container.appendChild(item);
    }
    const pager = document.createElement("div");
    pager.className = "chunk-pager";
    pager.innerHTML = `<button class="ghost-btn" data-prev ${previewState.offset <= 0 ? "disabled" : ""}>上一页</button>
      <span class="subtle">${previewState.offset + 1}–${Math.min(previewState.offset + previewState.limit, data.total)} / ${data.total}</span>
      <button class="ghost-btn" data-next ${previewState.offset + previewState.limit >= data.total ? "disabled" : ""}>下一页</button>`;
    pager.querySelector("[data-prev]").addEventListener("click", async () => {
      previewState.offset = Math.max(0, previewState.offset - previewState.limit);
      await renderPreview(modal);
    });
    pager.querySelector("[data-next]").addEventListener("click", async () => {
      previewState.offset += previewState.limit;
      await renderPreview(modal);
    });
    container.appendChild(pager);
  } catch (error) {
    container.innerHTML = `<div class="subtle">加载失败：${escapeHtml(error.message)}</div>`;
  }
}

async function reindexDoc(doc) {
  if (!(await confirmDialog(`用已保存的分块参数重新索引“${doc.filename}”？`, { confirmText: "重建" }))) return;
  try {
    const result = await api.post(`/api/documents/${doc.id}/reindex`);
    pollJob(result.job_id, doc.id);
    toast("已提交重建任务", { type: "success" });
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

async function deleteDoc(doc) {
  if (!(await confirmDialog(`删除“${doc.filename}”及其向量？`, { danger: true, confirmText: "删除" }))) return;
  try {
    await api.del(`/api/documents/${doc.id}`);
    jobStates.delete(doc.id);
    await loadDocuments();
    await loadKbs();
    toast("已删除", { type: "success" });
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

async function deleteKb() {
  const kb = activeKb();
  if (!kb) return;
  if (!(await confirmDialog(`删除知识库“${kb.name}”及其中所有文档？`, { danger: true, confirmText: "删除" }))) return;
  try {
    await api.del(`/api/kbs/${kb.id}`);
    activeKbId = null;
    store.activeKbId = null;
    $("#kb-detail-panel")?.classList.add("hidden");
    $("#kb-empty")?.classList.remove("hidden");
    await loadKbs();
    toast("知识库已删除", { type: "success" });
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

export function init() {
  $("#create-kb-btn")?.addEventListener("click", openCreateKb);
  $("#delete-kb-btn")?.addEventListener("click", deleteKb);
  $("#upload-btn")?.addEventListener("click", upload);
  $("#chunk-strategy")?.addEventListener("change", updateStrategyUi);
  updateStrategyUi();

  const dropzone = $("#dropzone");
  const fileInput = $("#file-input");
  dropzone?.addEventListener("click", () => fileInput?.click());
  fileInput?.addEventListener("change", () => {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  dropzone?.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone?.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone?.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragover");
    addFiles(event.dataTransfer.files);
  });

  loadKbs();
}

export async function reload() {
  await loadKbs();
}
