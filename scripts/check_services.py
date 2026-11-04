"""Check external services without printing secrets.

By default the embedding model is NOT loaded (it may download ~2 GB).
Use --embedding to test loading, or --embedding-load-only to only load.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.db import init_db  # noqa: E402
from app.services.embedding import embedding_service  # noqa: E402
from app.services.llm import llm_service  # noqa: E402
from app.services.memory import memory_service  # noqa: E402
from app.services.milvus_store import milvus_store  # noqa: E402
from app.services.rerank import rerank_service  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding", action="store_true", help="encode a short text")
    parser.add_argument("--embedding-load-only", action="store_true", help="only load the model")
    args = parser.parse_args()

    settings.ensure_dirs()
    setup_logging(settings.log_path, settings.log_level)
    init_db()

    print("== database ==")
    try:
        from app.db import SessionLocal

        with SessionLocal() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
        print("ok")
    except Exception as exc:  # noqa: BLE001
        print("FAILED:", exc)

    print("\n== milvus ==")
    print(milvus_store.health())

    print("\n== deepseek ==")
    print(llm_service.check())

    print("\n== siliconflow rerank ==")
    print(rerank_service.check())

    print("\n== mem0 ==")
    print(memory_service.check())

    print("\n== embedding ==")
    if args.embedding or args.embedding_load_only:
        try:
            if args.embedding_load_only:
                embedding_service._load()  # noqa: SLF001
                print(embedding_service.health())
            else:
                vector = embedding_service.encode_query("线性代数")
                print({"loaded": True, "dim": len(vector), "model": settings.embedding_model})
        except Exception as exc:  # noqa: BLE001
            print("FAILED:", exc)
    else:
        print(embedding_service.health(), "(not loaded; use --embedding to test)")


if __name__ == "__main__":
    main()
