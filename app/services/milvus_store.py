"""Milvus access layer.

Uses Milvus' native BM25 function (Milvus >= 2.5; verified against 3.0.3).
If the server or client does not support the API, the error is surfaced
clearly so the setup script can report it instead of failing at chat time.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from loguru import logger

from app.config import settings


class MilvusError(RuntimeError):
    pass


def _import_milvus():
    try:
        from pymilvus import (
            AnnSearchRequest,
            DataType,
            Function,
            FunctionType,
            MilvusClient,
            RRFRanker,
        )
    except Exception as exc:  # noqa: BLE001
        raise MilvusError(f"pymilvus 导入失败：{exc}") from exc
    return {
        "MilvusClient": MilvusClient,
        "DataType": DataType,
        "Function": Function,
        "FunctionType": FunctionType,
        "AnnSearchRequest": AnnSearchRequest,
        "RRFRanker": RRFRanker,
    }


class MilvusStore:
    _instance: "MilvusStore | None" = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self._api = None

    @classmethod
    def instance(cls) -> "MilvusStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ------------------------------------------------------------------ client
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    api = _import_milvus()
                    self._api = api
                    self._client = api["MilvusClient"](
                        uri=settings.milvus_uri,
                        timeout=settings.milvus_timeout,
                    )
        return self._client

    @property
    def api(self):
        if self._api is None:
            self.client()
        return self._api

    def health(self) -> dict:
        try:
            client = self.client()
            collections = client.list_collections()
            version = None
            try:
                version = client.get_server_version()
            except Exception:  # noqa: BLE001
                pass
            exists = settings.milvus_collection in collections
            return {
                "ok": True,
                "uri": settings.milvus_uri,
                "version": version,
                "collection": settings.milvus_collection,
                "collection_exists": exists,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "uri": settings.milvus_uri, "error": str(exc)}

    # -------------------------------------------------------------- collection
    def ensure_collection(self, recreate: bool = False) -> None:
        api = self.api
        client = self.client()
        name = settings.milvus_collection
        collections = client.list_collections()
        if name in collections and recreate:
            client.drop_collection(name)
            collections.remove(name)
        if name in collections:
            try:
                client.load_collection(name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("load_collection failed: {}", exc)
            return

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", api["DataType"].VARCHAR, is_primary=True, max_length=64)
        schema.add_field("kb_id", api["DataType"].VARCHAR, max_length=64)
        schema.add_field("document_id", api["DataType"].VARCHAR, max_length=64)
        schema.add_field("parent_id", api["DataType"].VARCHAR, max_length=64)
        schema.add_field("level", api["DataType"].INT8)
        schema.add_field("chunk_index", api["DataType"].INT32)
        schema.add_field("page", api["DataType"].INT32)
        schema.add_field("heading_path", api["DataType"].VARCHAR, max_length=1024)
        schema.add_field(
            "text",
            api["DataType"].VARCHAR,
            max_length=65535,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field("dense", api["DataType"].FLOAT_VECTOR, dim=settings.embedding_dim)
        schema.add_field("sparse", api["DataType"].SPARSE_FLOAT_VECTOR)
        schema.add_field("embedding_version", api["DataType"].VARCHAR, max_length=64)
        schema.add_field("meta", api["DataType"].JSON)

        schema.add_function(
            api["Function"](
                name="bm25",
                function_type=api["FunctionType"].BM25,
                input_field_names=["text"],
                output_field_names=["sparse"],
            )
        )

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        index_params.add_index(
            field_name="sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )

        try:
            client.create_collection(
                collection_name=name,
                schema=schema,
                index_params=index_params,
                consistency_level="Strong",
            )
        except Exception as exc:  # noqa: BLE001
            raise MilvusError(
                "创建 Milvus 集合失败。请确认 Milvus >= 2.5 且支持 BM25 Function。"
                f"原始错误：{exc}"
            ) from exc
        client.load_collection(name)
        logger.info("Milvus collection {} created", name)

    def drop_collection(self) -> None:
        client = self.client()
        if settings.milvus_collection in client.list_collections():
            client.drop_collection(settings.milvus_collection)

    # ---------------------------------------------------------------- write/read
    def insert_chunks(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        self.ensure_collection()
        client = self.client()
        result = client.insert(settings.milvus_collection, data=rows)
        return int(result.get("insert_count", len(rows))) if isinstance(result, dict) else len(rows)

    def flush(self) -> None:
        try:
            self.client().flush(settings.milvus_collection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Milvus flush failed: {}", exc)

    def delete_document(self, document_id: str) -> None:
        self.ensure_collection()
        self.client().delete(
            settings.milvus_collection,
            filter=f'document_id == "{document_id}"',
        )
        self.flush()

    def delete_ids(self, ids: list[str]) -> None:
        if not ids:
            return
        self.ensure_collection()
        self.client().delete(settings.milvus_collection, ids=ids)
        self.flush()

    def get_by_ids(self, ids: list[str], output_fields: list[str] | None = None) -> list[dict]:
        if not ids:
            return []
        self.ensure_collection()
        fields = output_fields or [
            "id",
            "document_id",
            "kb_id",
            "parent_id",
            "level",
            "heading_path",
            "page",
            "text",
        ]
        return self.client().get(
            settings.milvus_collection,
            ids=ids,
            output_fields=fields,
        )

    # ------------------------------------------------------------------ search
    @staticmethod
    def build_filter(kb_ids: list[str] | None = None, document_ids: list[str] | None = None,
                     level: int | None = 1) -> str:
        parts: list[str] = []
        if level is not None:
            parts.append(f"level == {int(level)}")
        if kb_ids:
            parts.append(f"kb_id in {json.dumps(kb_ids, ensure_ascii=False)}")
        if document_ids:
            parts.append(f"document_id in {json.dumps(document_ids, ensure_ascii=False)}")
        return " and ".join(parts) if parts else ""

    def _output_fields(self) -> list[str]:
        return [
            "id",
            "kb_id",
            "document_id",
            "parent_id",
            "level",
            "chunk_index",
            "heading_path",
            "page",
            "text",
            "meta",
        ]

    @staticmethod
    def _normalize_results(raw: list) -> list[dict]:
        """Normalize pymilvus hit shapes to a flat list of dicts."""
        items: list[dict] = []
        for group in raw or []:
            for hit in group or []:
                if isinstance(hit, dict):
                    entity = hit.get("entity") or {}
                    merged = {**entity} if isinstance(entity, dict) else {}
                    merged["id"] = hit.get("id", merged.get("id"))
                    merged["score"] = hit.get("distance", hit.get("score", 0.0))
                    items.append(merged)
                    continue
                # Older pymilvus returns Hit objects.
                entity = getattr(hit, "entity", None)
                if entity is None:
                    continue
                if isinstance(entity, dict):
                    merged = dict(entity)
                elif hasattr(entity, "to_dict"):
                    merged = dict(entity.to_dict())
                else:
                    merged = {}
                    for field in ("id", "kb_id", "document_id", "parent_id", "level",
                                  "chunk_index", "heading_path", "page", "text", "meta"):
                        if hasattr(entity, field):
                            merged[field] = getattr(entity, field)
                merged["id"] = getattr(hit, "id", merged.get("id"))
                merged["score"] = getattr(hit, "distance", getattr(hit, "score", 0.0))
                items.append(merged)
        return items

    def search_dense(
        self,
        query_vector: list[float],
        limit: int,
        expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list[dict]:
        self.ensure_collection()
        raw = self.client().search(
            collection_name=settings.milvus_collection,
            data=[query_vector],
            anns_field="dense",
            search_params={"metric_type": "COSINE", "params": {"ef": max(64, limit)}},
            limit=limit,
            filter=expr or None,
            output_fields=output_fields or self._output_fields(),
            consistency_level="Strong",
        )
        return self._normalize_results(raw)

    def search_sparse(
        self,
        query_text: str,
        limit: int,
        expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list[dict]:
        self.ensure_collection()
        raw = self.client().search(
            collection_name=settings.milvus_collection,
            data=[query_text],
            anns_field="sparse",
            search_params={"metric_type": "BM25"},
            limit=limit,
            filter=expr or None,
            output_fields=output_fields or self._output_fields(),
            consistency_level="Strong",
        )
        return self._normalize_results(raw)

    def hybrid_search(
        self,
        query_vector: list[float],
        query_text: str,
        limit: int,
        expr: str = "",
        output_fields: list[str] | None = None,
    ) -> list[dict]:
        self.ensure_collection()
        api = self.api
        client = self.client()
        fields = output_fields or self._output_fields()
        dense_req = api["AnnSearchRequest"](
            data=[query_vector],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"ef": max(64, limit)}},
            limit=limit,
            expr=expr or None,
        )
        sparse_req = api["AnnSearchRequest"](
            data=[query_text],
            anns_field="sparse",
            param={"metric_type": "BM25"},
            limit=limit,
            expr=expr or None,
        )
        raw = client.hybrid_search(
            collection_name=settings.milvus_collection,
            reqs=[dense_req, sparse_req],
            ranker=api["RRFRanker"](settings.rrf_k),
            limit=limit,
            output_fields=fields,
            consistency_level="Strong",
        )
        return self._normalize_results(raw)


milvus_store = MilvusStore.instance()
