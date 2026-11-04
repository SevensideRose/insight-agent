"""Application settings loaded from .env.

Only non-secret values are ever returned to the browser.
"""
from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app ---
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_name: str = "知研 Insight-agent"
    app_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    # --- paths ---
    data_dir: str = str(BASE_DIR / "data")
    log_dir: str = str(BASE_DIR / "logs")
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'app.db').as_posix()}"

    # --- Milvus ---
    milvus_uri: str = "http://localhost:19530"
    milvus_collection: str = "insight_chunks"
    milvus_timeout: float = 10.0

    # --- embedding (local, CPU) ---
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: str = "cpu"
    embedding_batch_size: int = 8
    embedding_dim: int = 1024
    embedding_version: str = "bge-m3-1024"
    hf_endpoint: str = "https://hf-mirror.com"

    # --- DeepSeek ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    llm_timeout: float = 120.0
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096

    # --- SiliconFlow rerank ---
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_timeout: float = 30.0

    # --- Mem0 ---
    mem0_api_key: str = ""
    mem0_user_id: str = "owner"
    memory_enabled: bool = True
    memory_top_k: int = 8
    memory_min_score: float = 0.0
    memory_worker_retries: int = 2
    memory_event_timeout: float = 120.0
    memory_event_poll_interval: float = 1.5
    memory_reconcile_interval: float = 60.0

    # --- retrieval ---
    retrieval_top_k: int = 8
    retrieval_candidate_k: int = 30
    rerank_enabled: bool = True
    rrf_k: int = 60
    max_context_chars: int = 6000

    # --- upload / parsing ---
    max_upload_mb: int = 100
    ocr_enabled: bool = True
    ocr_min_chars: int = 30
    ocr_dpi: int = 200
    pdf_merge_cross_page_tables: bool = True

    # --- chat ---
    history_max_messages: int = 12
    sse_heartbeat_seconds: float = 15.0

    # --- tests ---
    testing: bool = False

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def log_path(self) -> Path:
        return Path(self.log_dir)

    @property
    def upload_path(self) -> Path:
        return self.data_path / "uploads"

    @property
    def parsed_path(self) -> Path:
        return self.data_path / "parsed"

    def ensure_dirs(self) -> None:
        for path in (self.data_path, self.log_path, self.upload_path, self.parsed_path):
            path.mkdir(parents=True, exist_ok=True)

    def public_config(self) -> dict:
        """Configuration safe to expose to the local frontend."""
        return {
            "app_name": self.app_name,
            "embedding_model": self.embedding_model,
            "embedding_device": self.embedding_device,
            "embedding_version": self.embedding_version,
            "deepseek_model": self.deepseek_model,
            "rerank_model": self.rerank_model,
            "rerank_enabled": self.rerank_enabled,
            "retrieval_top_k": self.retrieval_top_k,
            "retrieval_candidate_k": self.retrieval_candidate_k,
            "memory_enabled": self.memory_enabled,
            "memory_top_k": self.memory_top_k,
            "milvus_uri": self.milvus_uri,
            "milvus_collection": self.milvus_collection,
            "data_dir": str(self.data_path),
            "max_upload_mb": self.max_upload_mb,
            "ocr_enabled": self.ocr_enabled,
            "deepseek_configured": bool(self.deepseek_api_key),
            "siliconflow_configured": bool(self.siliconflow_api_key),
            "mem0_configured": bool(self.mem0_api_key),
        }


settings = Settings()

# huggingface_hub reads these at import time, so they must be set before any
# service imports sentence-transformers / huggingface_hub. Doing it in
# EmbeddingService._load() is too late for the endpoint URL.
if settings.hf_endpoint:
    os.environ["HF_ENDPOINT"] = settings.hf_endpoint
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
