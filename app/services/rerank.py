"""SiliconFlow rerank client."""
from __future__ import annotations

import httpx
from loguru import logger

from app.config import settings


class RerankError(RuntimeError):
    pass


class RerankService:
    @property
    def configured(self) -> bool:
        return bool(settings.siliconflow_api_key)

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        """Return (original_index, relevance_score), best first."""
        if not documents:
            return []
        if not self.configured:
            raise RerankError("未配置 SILICONFLOW_API_KEY")
        url = f"{settings.siliconflow_base_url.rstrip('/')}/rerank"
        payload = {
            "model": settings.rerank_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "return_documents": False,
        }
        try:
            response = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.siliconflow_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=settings.rerank_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise RerankError(f"重排序请求失败：{exc}") from exc
        if response.status_code != 200:
            raise RerankError(f"重排序返回 {response.status_code}: {response.text[:300]}")
        try:
            data = response.json()
            results = data.get("results") or []
            pairs = [(int(item["index"]), float(item["relevance_score"])) for item in results]
        except Exception as exc:  # noqa: BLE001
            raise RerankError(f"重排序响应解析失败：{exc}") from exc
        pairs.sort(key=lambda item: item[1], reverse=True)
        logger.info("Reranked {} documents -> {}", len(documents), len(pairs))
        return pairs

    def check(self) -> dict:
        if not self.configured:
            return {"ok": False, "configured": False, "error": "未配置 SILICONFLOW_API_KEY"}
        try:
            pairs = self.rerank("测试", ["这是一段测试文本", "这是另一段测试文本"], 2)
            return {"ok": True, "configured": True, "results": len(pairs)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "configured": True, "error": str(exc)}


rerank_service = RerankService()
