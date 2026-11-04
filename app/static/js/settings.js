import { api } from "./api.js";
import { $, escapeHtml, toast } from "./ui.js";

const SERVICE_NAMES = {
  database: "SQLite",
  milvus: "Milvus 向量库",
  embedding: "本地嵌入模型",
  deepseek: "DeepSeek 对话",
  siliconflow: "SiliconFlow 重排",
  mem0: "Mem0 长期记忆",
};

const CONFIG_LABELS = {
  app_name: "应用名称",
  embedding_model: "嵌入模型",
  embedding_device: "运行设备",
  embedding_version: "索引版本",
  deepseek_model: "对话模型",
  rerank_model: "重排模型",
  rerank_enabled: "启用重排",
  retrieval_top_k: "返回片段数",
  retrieval_candidate_k: "召回候选数",
  memory_enabled: "长期记忆",
  memory_top_k: "记忆检索条数",
  milvus_uri: "Milvus 地址",
  milvus_collection: "Milvus 集合",
  data_dir: "数据目录",
  max_upload_mb: "上传上限 (MB)",
  ocr_enabled: "扫描 PDF OCR",
  deepseek_configured: "DeepSeek 密钥",
  siliconflow_configured: "SiliconFlow 密钥",
  mem0_configured: "Mem0 密钥",
};

function formatValue(value) {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function stateOf(key, data) {
  if (!data) return { level: "unknown", label: "未检测" };
  if (key === "embedding") {
    if (data.error) return { level: "bad", label: "加载失败" };
    if (data.loaded) return { level: "ok", label: "已加载" };
    return { level: "unknown", label: "待首次使用" };
  }
  if (data.ok === true) return { level: "ok", label: "正常" };
  if (data.ok === false) return { level: "bad", label: "不可用" };
  if (data.configured === true) return { level: "unknown", label: "已配置未检测" };
  if (data.configured === false) return { level: "bad", label: "未配置" };
  return { level: "unknown", label: "未检测" };
}

function detailOf(key, data) {
  if (!data) return "";
  if (data.error) return String(data.error);
  if (key === "milvus") {
    const parts = [];
    if (data.version) parts.push(`v${data.version}`);
    if (data.uri) parts.push(data.uri);
    if (data.collection) parts.push(`集合 ${data.collection}${data.collection_exists === false ? "（未创建）" : ""}`);
    return parts.join(" · ");
  }
  if (key === "embedding") {
    return `${data.model || ""} · ${data.device || "cpu"}`;
  }
  if (key === "database") return "data/app.db";
  if (data.status) return `HTTP ${data.status}`;
  return "";
}

function renderHealth(results) {
  const grid = $("#health-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (const key of Object.keys(SERVICE_NAMES)) {
    const data = results?.[key];
    const state = stateOf(key, data);
    const card = document.createElement("div");
    card.className = "health-card";
    card.innerHTML = `
      <div class="health-card-head">
        <span class="health-card-name">${escapeHtml(SERVICE_NAMES[key])}</span>
        <span class="health-state ${state.level}">${escapeHtml(state.label)}</span>
      </div>
      <div class="health-detail">${escapeHtml(detailOf(key, data))}</div>`;
    grid.appendChild(card);
  }
}

function updateHealthDot(results) {
  const dot = $("#health-dot");
  if (!dot) return;
  const essential = [results?.database?.ok, results?.milvus?.ok];
  if (essential.every((value) => value === true)) {
    dot.className = "health-dot ok";
    dot.title = "数据库与 Milvus 正常";
  } else {
    dot.className = "health-dot bad";
    dot.title = "数据库或 Milvus 不可用";
  }
}

function renderConfig(config) {
  const list = $("#config-list");
  if (!list) return;
  list.innerHTML = "";
  for (const [key, label] of Object.entries(CONFIG_LABELS)) {
    if (!(key in config)) continue;
    const row = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    let value = config[key];
    if (key.endsWith("_configured")) value = value ? "已配置" : "未配置";
    dd.textContent = formatValue(value);
    row.appendChild(dt);
    row.appendChild(dd);
    list.appendChild(row);
  }
}

async function loadConfig() {
  try {
    const config = await api.get("/api/settings/public");
    renderConfig(config);
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

async function loadHealth() {
  try {
    const health = await api.get("/api/health");
    renderHealth(health);
    updateHealthDot(health);
  } catch (error) {
    toast(error.message, { type: "error" });
  }
}

async function checkServices() {
  const button = $("#check-services");
  if (!button) return;
  button.disabled = true;
  button.textContent = "检查中…";
  try {
    const results = await api.post("/api/settings/check", {});
    renderHealth(results);
    updateHealthDot(results);
    const bad = Object.keys(SERVICE_NAMES).filter(
      (key) => stateOf(key, results?.[key]).level === "bad"
    );
    if (bad.length) {
      toast(`以下服务有问题：${bad.map((key) => SERVICE_NAMES[key]).join("、")}`, {
        type: "error",
        timeout: 8000,
      });
    } else {
      toast("检查完成", { type: "success" });
    }
  } catch (error) {
    toast(error.message, { type: "error", timeout: 8000 });
  } finally {
    button.disabled = false;
    button.textContent = "检查服务";
  }
}

async function initMilvus() {
  const button = $("#init-milvus");
  if (!button) return;
  button.disabled = true;
  button.textContent = "初始化中…";
  try {
    const result = await api.post("/api/settings/init-milvus", {});
    if (result.ok) {
      toast("Milvus 集合已就绪", { type: "success" });
      await loadHealth();
    } else {
      toast(result.error || "初始化失败", { type: "error", timeout: 9000 });
    }
  } catch (error) {
    toast(error.message, { type: "error", timeout: 9000 });
  } finally {
    button.disabled = false;
    button.textContent = "初始化 / 检查 Milvus 集合";
  }
}

export function init() {
  $("#check-services")?.addEventListener("click", checkServices);
  $("#init-milvus")?.addEventListener("click", initMilvus);
  $("#reload-config")?.addEventListener("click", async () => {
    await loadConfig();
    toast("已重新读取服务端配置；修改 .env 后需重启服务", { type: "", timeout: 6000 });
  });
  loadConfig();
  loadHealth();
}

export async function reload() {
  await Promise.all([loadConfig(), loadHealth()]);
}
