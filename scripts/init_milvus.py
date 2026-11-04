"""Create the Milvus collection and optionally run a BM25 smoke test."""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db import init_db  # noqa: E402
from app.services.milvus_store import milvus_store  # noqa: E402


def smoke_test() -> None:
    dim = settings.embedding_dim
    vector = [random.uniform(-1, 1) for _ in range(dim)]
    norm = sum(v * v for v in vector) ** 0.5 or 1.0
    vector = [v / norm for v in vector]
    row = {
        "id": "smoke0000000000000000000000000001",
        "kb_id": "0" * 32,
        "document_id": "1" * 32,
        "parent_id": "",
        "level": 1,
        "chunk_index": 0,
        "page": 1,
        "heading_path": "冒烟测试",
        "text": "线性代数 矩阵 特征值 冒烟测试",
        "dense": vector,
        "embedding_version": settings.embedding_version,
        "meta": {"smoke": True},
    }
    milvus_store.insert_chunks([row])
    milvus_store.flush()
    expr = 'document_id == "' + "1" * 32 + '"'
    dense = milvus_store.search_dense(vector, 3, expr)
    sparse = milvus_store.search_sparse("矩阵 特征值", 3, expr)
    hybrid = milvus_store.hybrid_search(vector, "矩阵 特征值", 3, expr)
    print(f"dense hits: {len(dense)}")
    print(f"sparse hits: {len(sparse)}")
    print(f"hybrid hits: {len(hybrid)}")
    milvus_store.delete_ids([row["id"]])
    milvus_store.flush()
    print("smoke test cleaned up")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--recreate", action="store_true", help="drop existing collection first")
    parser.add_argument("--smoke", action="store_true", help="insert/search/delete a test row")
    args = parser.parse_args()

    settings.ensure_dirs()
    setup_logging(settings.log_path, settings.log_level)
    init_db()
    print(f"Milvus: {settings.milvus_uri}, collection: {settings.milvus_collection}")
    health = milvus_store.health()
    print("health:", health)
    if not health.get("ok"):
        raise SystemExit("Milvus 连接失败，请确认 Docker 容器已启动")
    milvus_store.ensure_collection(recreate=args.recreate)
    print("collection ready")
    if args.smoke:
        smoke_test()


if __name__ == "__main__":
    main()
