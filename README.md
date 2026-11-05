# 知研 Insight-agent

一个单用户、本地运行的学习助手：把课程资料整理成可检索的知识库，在对话中流式回答并给出来源，同时自动提取长期学习记忆。

## 功能

- **知识库**：多个知识库，支持 docx / pdf / md / html；后台解析、分块、向量化，扫描 PDF 自动 OCR。
- **分块策略**：固定、递归、语义、文档结构、AI、父子分块。
- **混合检索**：向量检索 + BM25 稀疏检索 + RRF 融合，可选硅基流动重排和父块扩展。
- **对话**：流式输出、RAG 开关、多知识库选择、引用编号与来源卡片。
- **长期记忆**：每轮回答后自动提取学习目标、薄弱点、偏好等，下一轮及跨会话召回。
- **本地优先**：只监听 `127.0.0.1`，无登录；数据保存在本机 SQLite 和 Milvus 中。


## 快速开始

### 1. 环境要求

- Python 3.12+
- Docker 中的 Milvus **3.0.3**，监听 `localhost:19530`
- DeepSeek、硅基流动（SiliconFlow）、Mem0 的 API Key(也可自行修改成其他OpenAI接口的大模型，重排模型)
- 首次运行需下载 `BAAI/bge-m3`（约 2.3 GB），建议预留磁盘空间

### 2. 安装

```bash
git clone https://github.com/SevensideRose/insight-agent.git
cd insight-agent

python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
# Windows 可用：copy .env.example .env
```

编辑 `.env`，至少填写三个密钥：

```text
DEEPSEEK_API_KEY=...
SILICONFLOW_API_KEY=...
MEM0_API_KEY=...
```

其余配置可保持默认。国内网络可保留默认的 `HF_ENDPOINT=https://hf-mirror.com` 以加速模型下载。


### 4. 启动 Milvus

使用 Milvus 官方 standalone compose 启动 etcd、MinIO 和 Milvus（版本 3.0.3）：

```bash
# 在 Milvus 官方仓库的 deployments/docker/standalone 目录
docker compose up -d
```

确认 `19530` 端口可访问后继续。参考：[Milvus Standalone 安装文档](https://milvus.io/docs/install_standalone-docker.md)

### 5. 自检并启动

```bash
# 检查数据库、Milvus、DeepSeek、硅基流动、Mem0
python scripts/check_services.py

# 启动应用
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

首次上传文档时会加载嵌入模型，CPU 下可能需要等待几十秒；扫描件 OCR 也会增加处理时间。

### 6. 开始使用

1. 打开「知识库」，新建一个知识库。
2. 上传讲义、论文或笔记，等待状态变为「就绪」。
3. 打开「对话」，选中知识库后提问。
4. 回答会流式出现，引用编号可点开查看来源。
5. 对话中自动提取的记忆可在「记忆」页查看、搜索和删除。

