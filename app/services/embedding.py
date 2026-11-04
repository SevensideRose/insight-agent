"""Local bge-m3 embedding service (CPU).

The model is loaded lazily on first use. On a CPU-only machine the first
load can take tens of seconds; callers should show progress to the user.
"""
from __future__ import annotations

import os
import threading

from loguru import logger

from app.config import settings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingService:
    _instance: "EmbeddingService | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._model = None
        self._load_lock = threading.Lock()
        self._load_error: str | None = None

    @classmethod
    def instance(cls) -> "EmbeddingService":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                if settings.hf_endpoint:
                    os.environ["HF_ENDPOINT"] = settings.hf_endpoint
                os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

                import torch
                from sentence_transformers import SentenceTransformer

                # If huggingface_hub was imported before config was loaded, its
                # constants already captured the default endpoint. Patch them so
                # the mirror actually takes effect.
                try:
                    import huggingface_hub.constants as hf_constants

                    hf_constants.ENDPOINT = settings.hf_endpoint.rstrip("/")
                    hf_constants.HUGGINGFACE_CO_URL_TEMPLATE = (
                        hf_constants.ENDPOINT + "/{repo_id}/resolve/{revision}/{filename}"
                    )
                except Exception:  # noqa: BLE001
                    pass

                threads = max(1, (os.cpu_count() or 2) - 1)
                torch.set_num_threads(threads)
                logger.info(
                    "Loading embedding model {} on {} (threads={})",
                    settings.embedding_model,
                    settings.embedding_device,
                    threads,
                )
                model = SentenceTransformer(
                    settings.embedding_model,
                    device=settings.embedding_device,
                )
                model.max_seq_length = 8192
                self._model = model
                self._load_error = None
                logger.info("Embedding model loaded")
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                logger.exception("Failed to load embedding model")
                raise EmbeddingError(
                    "嵌入模型加载失败。请检查网络/HuggingFace 镜像配置，"
                    f"或提前下载 {settings.embedding_model}。原始错误：{exc}"
                ) from exc

    def encode(self, texts: list[str], batch_size: int | None = None) -> list[list[float]]:
        if not texts:
            return []
        self._load()
        assert self._model is not None
        try:
            vectors = self._model.encode(
                texts,
                batch_size=batch_size or settings.embedding_batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [list(map(float, vector)) for vector in vectors]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Embedding failed")
            raise EmbeddingError(f"文本向量化失败：{exc}") from exc

    def encode_query(self, query: str) -> list[float]:
        vectors = self.encode([query], batch_size=1)
        if not vectors:
            raise EmbeddingError("查询向量为空")
        return vectors[0]

    def health(self) -> dict:
        return {
            "loaded": self.loaded,
            "model": settings.embedding_model,
            "device": settings.embedding_device,
            "error": self._load_error,
        }


embedding_service = EmbeddingService.instance()
