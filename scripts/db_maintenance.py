from __future__ import annotations

import argparse
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _prepare_path() -> None:
    script_path = Path(__file__).resolve()
    cloud_root = script_path.parents[1]
    project_root = cloud_root.parent
    ordered = [str(project_root), str(cloud_root)]
    for item in ordered:
        while item in sys.path:
            sys.path.remove(item)
    for item in ordered:
        sys.path.insert(0, item)


_prepare_path()

from app.core.config import settings
from app.core.database import SessionLocal
from app.models.bug import AppRuntimeMeta
from app.services.migrations import run_cloudtest_migrations
from app.services.search import rebuild_bug_search_index


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_reindex(*, dirty_only: bool, limit: int | None, with_embeddings: bool) -> int:
    with db_session() as db:
        processed = rebuild_bug_search_index(
            db,
            embedding_provider=settings.embedding_provider,
            embedding_model=settings.embedding_model,
            build_embedding=with_embeddings,
            dirty_only=dirty_only,
            limit=limit,
        )
        now = datetime.now(timezone.utc).isoformat()
        db.merge(AppRuntimeMeta(key="search.last_reindex_at", value=now))
        db.merge(AppRuntimeMeta(key="search.last_reindex_count", value=str(processed)))
        db.commit()
    return int(processed)


def main() -> int:
    parser = argparse.ArgumentParser(description="CloudTest DB maintenance: migrate + search reindex.")
    parser.add_argument("--migrate", action="store_true", help="Run Alembic upgrade head before other operations.")
    parser.add_argument("--reindex", action="store_true", help="Run search index rebuild.")
    parser.add_argument("--dirty-only", action="store_true", help="Reindex only dirty/missing rows.")
    parser.add_argument("--limit", type=int, default=0, help="Max number of bugs to reindex (0 = no limit).")
    parser.add_argument(
        "--without-embeddings",
        action="store_true",
        help="Populate search text/hash without generating embedding vectors.",
    )
    args = parser.parse_args()

    if not args.migrate and not args.reindex:
        args.migrate = True

    if args.migrate:
        run_cloudtest_migrations()
        print("Migrations: OK")

    if args.reindex:
        processed = _run_reindex(
            dirty_only=bool(args.dirty_only),
            limit=(int(args.limit) if int(args.limit or 0) > 0 else None),
            with_embeddings=not bool(args.without_embeddings),
        )
        print(f"Reindex: processed={processed}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
