from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import func, inspect, select, text


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
from app.core.database import SessionLocal, engine
from app.models.bug import Bug, BugSearchIndex


REQUIRED_TABLES = {
    "users",
    "bugs",
    "attachments",
    "bug_comments",
    "bug_history",
    "bug_search_index",
    "background_jobs",
    "bug_view_states",
    "app_runtime_meta",
    "alembic_version",
}

REQUIRED_COLUMNS = {
    "bugs": {
        "title",
        "description",
        "status",
        "severity",
        "reporter_id",
        "assignee_id",
        "notify_emails",
        "reporter_satisfaction",
        "sentiment_label",
        "sentiment_summary",
        "bug_summary",
        "reporting_date",
        "created_at",
        "updated_at",
        "deleted_at",
        "deleted_by",
    },
    "bug_search_index": {
        "bug_id",
        "content_hash",
        "search_text",
        "needs_reindex",
        "last_error",
        "indexed_at",
        "embedding",
        "updated_at",
    },
}


def _current_revision() -> str:
    with engine.connect() as conn:
        try:
            row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first()
        except Exception:
            return ""
    return str(row[0]).strip() if row and row[0] else ""


def _collect_schema_report() -> dict:
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())

    missing_tables = sorted(REQUIRED_TABLES - table_names)
    missing_columns: dict[str, list[str]] = {}
    for table_name, required in REQUIRED_COLUMNS.items():
        if table_name not in table_names:
            missing_columns[table_name] = sorted(required)
            continue
        existing = {c["name"] for c in inspector.get_columns(table_name)}
        missing = sorted(required - existing)
        if missing:
            missing_columns[table_name] = missing

    sqlite_special = {}
    if settings.database_is_sqlite:
        with engine.connect() as conn:
            master_rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
            ).fetchall()
        master_names = {str(row[0]) for row in master_rows}
        sqlite_special["fts_exists"] = "bug_search_fts" in master_names
        sqlite_special["vec_exists"] = str(getattr(settings, "sqlite_vec_table_name", "bug_search_vec")) in master_names

    with SessionLocal() as db:
        bug_count = int(db.scalar(select(func.count(Bug.id))) or 0)
        index_count = int(db.scalar(select(func.count(BugSearchIndex.bug_id))) or 0)
        dirty_count = int(
            db.scalar(select(func.count(BugSearchIndex.bug_id)).where(BugSearchIndex.needs_reindex == 1))
            or 0
        )

    revision = _current_revision()
    schema_ok = not missing_tables and not any(missing_columns.values()) and bool(revision)
    return {
        "database_backend": settings.database_backend,
        "database_url": settings.database_url,
        "alembic_revision": revision,
        "schema_ok": schema_ok,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "sqlite_special": sqlite_special,
        "counts": {
            "bugs": bug_count,
            "search_index_rows": index_count,
            "search_index_dirty_rows": dirty_count,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CloudTest DB schema + migration state.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero exit code if schema is not OK.")
    parser.add_argument("--json", action="store_true", help="Output JSON only.")
    args = parser.parse_args()

    report = _collect_schema_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f"Backend: {report['database_backend']}")
        print(f"Alembic revision: {report['alembic_revision'] or '-'}")
        print(f"Schema OK: {report['schema_ok']}")
        if report["missing_tables"]:
            print(f"Mangler tabeller: {', '.join(report['missing_tables'])}")
        missing_columns = report["missing_columns"] or {}
        for table_name, columns in missing_columns.items():
            if columns:
                print(f"Mangler kolonner i {table_name}: {', '.join(columns)}")
        counts = report["counts"] or {}
        print(
            "Counts: "
            f"bugs={counts.get('bugs', 0)} | "
            f"index_rows={counts.get('search_index_rows', 0)} | "
            f"index_dirty={counts.get('search_index_dirty_rows', 0)}"
        )
        sqlite_special = report.get("sqlite_special") or {}
        if sqlite_special:
            print(
                "SQLite search objects: "
                f"fts={sqlite_special.get('fts_exists')} | vec={sqlite_special.get('vec_exists')}"
            )

    if args.strict and not bool(report.get("schema_ok")):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
