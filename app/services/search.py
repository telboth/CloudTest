import hashlib
import math
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from difflib import SequenceMatcher
from threading import Lock
from time import perf_counter

from sqlalchemy import Float, literal_column, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.logging import get_logger
from app.models.bug import Bug, BugSearchIndex
from app.models.user import User
from app.services.ai_provider import embed_text
from app.services.permissions import can_view_bug

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - optional dependency at runtime
    Vector = None  # type: ignore[assignment]

try:
    from pgvector import Vector as VectorValue
except ImportError:  # pragma: no cover - optional dependency at runtime
    VectorValue = None  # type: ignore[assignment]


logger = get_logger("app.search")

_SEARCH_TELEMETRY_LOCK = Lock()
_SEARCH_TELEMETRY: dict[str, float] = {
    "total_queries": 0.0,
    "exact_match_queries": 0.0,
    "hybrid_queries": 0.0,
    "keyword_fallback_queries": 0.0,
    "embedding_unavailable_queries": 0.0,
    "total_results": 0.0,
    "total_duration_ms": 0.0,
}
_PGVECTOR_NATIVE_SEARCH_DISABLED = False


def _sqlite_vec_lock_active() -> bool:
    explicit = getattr(settings, "sqlite_vec_lock_active", None)
    if explicit is not None:
        return bool(explicit)
    return bool(getattr(settings, "database_is_sqlite", False) and getattr(settings, "sqlite_vec_enabled", False))


def _sqlite_vec_embedding_provider() -> str:
    provider = str(getattr(settings, "sqlite_vec_embedding_provider", "openai") or "openai").strip().casefold()
    if provider not in {"openai", "local"}:
        provider = "openai"
    return provider


def _sqlite_vec_embedding_model(provider: str) -> str:
    provider = provider if provider in {"openai", "local"} else "openai"
    default_model = settings.local_embedding_model if provider == "local" else settings.embedding_model
    configured = str(getattr(settings, "sqlite_vec_embedding_model", default_model) or default_model).strip()
    return configured or default_model


def _tokenize_query(query: str) -> list[str]:
    tokens = [token for token in re.split(r"\W+", (query or "").casefold()) if token]
    # Keep token order but remove duplicates to avoid overweighting repeated words.
    return list(dict.fromkeys(tokens))


def _hybrid_weights_for_query(query: str) -> tuple[float, float, float, float]:
    tokens = _tokenize_query(query)
    if len(tokens) <= 2:
        # Short queries are typically better handled by lexical precision.
        return (0.75, 0.25, 0.18, 0.55)  # lexical_w, semantic_w, lexical_gate, semantic_gate
    return (0.45, 0.55, 0.12, 0.42)


def _similarity_weights_for_query(query: str) -> tuple[float, float, float, float]:
    tokens = _tokenize_query(query)
    if len(tokens) <= 2:
        return (0.35, 0.65, 0.10, 0.30)
    return (0.20, 0.80, 0.08, 0.26)


def search_visible_bugs(
    db: Session,
    *,
    current_user: User,
    query: str,
    limit: int = 50,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[Bug]:
    started = perf_counter()
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    bugs = db.scalars(
        select(Bug)
        .options(
            selectinload(Bug.attachments),
            selectinload(Bug.comments),
            selectinload(Bug.history),
            selectinload(Bug.view_states),
        )
        .order_by(Bug.created_at.desc())
    ).all()
    visible_bugs = [
        bug
        for bug in bugs
        if can_view_bug(current_user, bug) and getattr(bug, "deleted_at", None) is None
    ]
    if not visible_bugs:
        _record_search_telemetry(mode="keyword_fallback", results_count=0, duration_ms=(perf_counter() - started) * 1000, embedding_available=False)
        return []

    resolved_provider, resolved_model = _resolve_embedding_selection(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    exact_matches = _exact_keyword_matches(cleaned_query, visible_bugs)
    if exact_matches:
        results = exact_matches[:limit]
        duration_ms = (perf_counter() - started) * 1000
        _record_search_telemetry(mode="exact", results_count=len(results), duration_ms=duration_ms, embedding_available=False)
        logger.info(
            "Bug search mode=exact results=%s visible=%s duration_ms=%.2f provider=%s model=%s",
            len(results),
            len(visible_bugs),
            duration_ms,
            resolved_provider,
            resolved_model,
        )
        return results

    query_embedding = _build_embedding(
        cleaned_query,
        embedding_provider=resolved_provider,
        embedding_model=resolved_model,
    )
    semantic_scores = _semantic_scores_for_bugs(
        db,
        visible_bugs=visible_bugs,
        query_embedding=query_embedding,
        embedding_provider=resolved_provider,
        embedding_model=resolved_model,
    )

    lexical_weight, semantic_weight, lexical_gate, semantic_gate = _hybrid_weights_for_query(cleaned_query)
    if not query_embedding or not semantic_scores:
        lexical_weight, semantic_weight = 1.0, 0.0
        semantic_gate = 1.1

    ranked: list[tuple[float, Bug]] = []
    for bug in visible_bugs:
        search_text = _build_bug_search_text(bug)
        keyword_score = _keyword_score(cleaned_query, bug, search_text)
        semantic_score = semantic_scores.get(bug.id, 0.0)
        final_score = (semantic_score * semantic_weight) + (keyword_score * lexical_weight)
        if keyword_score < lexical_gate and semantic_score < semantic_gate:
            continue
        ranked.append((final_score, bug))

    ranked.sort(key=lambda item: (item[0], item[1].updated_at or item[1].created_at), reverse=True)
    results = [bug for _score, bug in ranked[:limit]]
    duration_ms = (perf_counter() - started) * 1000
    has_semantic_scores = bool(semantic_scores)
    mode = "hybrid" if query_embedding and has_semantic_scores else "keyword_fallback"
    _record_search_telemetry(
        mode=mode,
        results_count=len(results),
        duration_ms=duration_ms,
        embedding_available=bool(query_embedding),
    )
    logger.info(
        "Bug search mode=%s results=%s visible=%s duration_ms=%.2f provider=%s model=%s semantic_scores=%s",
        mode,
        len(results),
        len(visible_bugs),
        duration_ms,
        resolved_provider,
        resolved_model,
        len(semantic_scores),
    )
    return results


def retrieve_similar_visible_bugs(
    db: Session,
    *,
    current_user: User,
    query: str,
    limit: int = 5,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[Bug]:
    cleaned_query = query.strip()
    if not cleaned_query:
        return []

    bugs = db.scalars(
        select(Bug)
        .options(
            selectinload(Bug.attachments),
            selectinload(Bug.comments),
            selectinload(Bug.history),
            selectinload(Bug.view_states),
        )
        .order_by(Bug.created_at.desc())
    ).all()
    visible_bugs = [
        bug
        for bug in bugs
        if can_view_bug(current_user, bug) and getattr(bug, "deleted_at", None) is None
    ]
    if not visible_bugs:
        return []

    resolved_provider, resolved_model = _resolve_embedding_selection(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    query_embedding = _build_embedding(
        cleaned_query,
        embedding_provider=resolved_provider,
        embedding_model=resolved_model,
    )
    semantic_scores = _semantic_scores_for_bugs(
        db,
        visible_bugs=visible_bugs,
        query_embedding=query_embedding,
        embedding_provider=resolved_provider,
        embedding_model=resolved_model,
    )

    lexical_weight, semantic_weight, lexical_gate, semantic_gate = _similarity_weights_for_query(cleaned_query)
    if not query_embedding or not semantic_scores:
        lexical_weight, semantic_weight = 1.0, 0.0
        semantic_gate = 1.1

    ranked: list[tuple[float, Bug]] = []
    for bug in visible_bugs:
        search_text = _build_bug_search_text(bug)
        keyword_score = _keyword_score(cleaned_query, bug, search_text)
        semantic_score = semantic_scores.get(bug.id, 0.0)
        final_score = (semantic_score * semantic_weight) + (keyword_score * lexical_weight)
        if keyword_score < lexical_gate and semantic_score < semantic_gate:
            continue
        ranked.append((final_score, bug))

    ranked.sort(key=lambda item: (item[0], item[1].updated_at or item[1].created_at), reverse=True)
    return [bug for _score, bug in ranked[:limit]]


def _exact_keyword_matches(query: str, bugs: list[Bug]) -> list[Bug]:
    lower_query = query.casefold()
    matched: list[Bug] = []
    for bug in bugs:
        search_text = _build_bug_search_text(bug).casefold()
        if lower_query == str(bug.id) or lower_query in search_text:
            matched.append(bug)
    matched.sort(key=lambda bug: bug.updated_at or bug.created_at, reverse=True)
    return matched


def _semantic_scores_for_bugs(
    db: Session,
    *,
    visible_bugs: list[Bug],
    query_embedding: list[float] | None,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> dict[int, float]:
    global _PGVECTOR_NATIVE_SEARCH_DISABLED
    if not query_embedding:
        return {}

    visible_ids = [bug.id for bug in visible_bugs]
    if not visible_ids:
        return {}

    current_provider, current_model = _resolve_embedding_selection(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    current_dimensions = len(query_embedding)

    rows = db.scalars(select(BugSearchIndex).where(BugSearchIndex.bug_id.in_(visible_ids))).all()
    indexed_by_bug_id = {row.bug_id: row for row in rows}
    needs_update: list[Bug] = []
    for bug in visible_bugs:
        row = indexed_by_bug_id.get(bug.id)
        if _search_index_row_is_stale(
            row,
            bug=bug,
            embedding_provider=current_provider,
            embedding_model=current_model,
            embedding_dimensions=current_dimensions,
            require_embedding=True,
        ):
            needs_update.append(bug)

    if needs_update:
        for bug in needs_update:
            _ensure_search_index(
                db,
                bug,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
            )
        db.commit()

    if settings.database_is_postgresql and Vector is not None and not _PGVECTOR_NATIVE_SEARCH_DISABLED:
        try:
            native_scores = _semantic_scores_for_bugs_postgres(
                db,
                visible_ids=visible_ids,
                query_embedding=query_embedding,
                embedding_provider=current_provider,
                embedding_model=current_model,
                embedding_dimensions=current_dimensions,
            )
            if native_scores:
                return native_scores
        except SQLAlchemyError as exc:
            _PGVECTOR_NATIVE_SEARCH_DISABLED = True
            logger.warning(
                "Disabling native pgvector search for this runtime after DB error: %s",
                exc.__class__.__name__,
            )
        except Exception as exc:  # pragma: no cover - defensive fallback
            _PGVECTOR_NATIVE_SEARCH_DISABLED = True
            logger.warning(
                "Disabling native pgvector search for this runtime after unexpected error: %s",
                exc.__class__.__name__,
            )

    rows = db.scalars(select(BugSearchIndex).where(BugSearchIndex.bug_id.in_(visible_ids))).all()
    indexed_by_bug_id = {row.bug_id: row for row in rows}
    scores: dict[int, float] = {}
    for bug in visible_bugs:
        row = indexed_by_bug_id.get(bug.id)
        if _search_index_row_is_stale(
            row,
            bug=bug,
            embedding_provider=current_provider,
            embedding_model=current_model,
            embedding_dimensions=current_dimensions,
            require_embedding=True,
        ):
            continue
        if row and row.embedding:
            scores[bug.id] = _cosine_similarity(query_embedding, row.embedding)

    return scores


def _ensure_search_index(
    db: Session,
    bug: Bug,
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embedding: bool = True,
) -> BugSearchIndex:
    search_text = _build_bug_search_text(bug)
    current_provider, current_model = _resolve_embedding_selection(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    content_hash = _search_text_content_hash(
        search_text=search_text,
        embedding_provider=current_provider,
        embedding_model=current_model,
    )
    row = db.get(BugSearchIndex, bug.id)
    if row and row.content_hash == content_hash and row.embedding_provider == current_provider and row.embedding_model == current_model:
        if build_embedding and bool(row.embedding) and int(getattr(row, "needs_reindex", 0) or 0) == 0:
            return row
        if not build_embedding and int(getattr(row, "needs_reindex", 0) or 0) == 1:
            return row

    embedding: list[float] | None = None
    embedding_dimensions: int | None = None
    needs_reindex = 1
    indexed_at: datetime | None = None
    if build_embedding:
        embedding = _build_embedding(
            search_text,
            embedding_provider=current_provider,
            embedding_model=current_model,
        )
        embedding_dimensions = len(embedding) if embedding else None
        needs_reindex = 0
        indexed_at = datetime.now(timezone.utc)
    if row is None:
        row = BugSearchIndex(
            bug_id=bug.id,
            content_hash=content_hash,
            embedding_provider=current_provider,
            embedding_model=current_model,
            embedding_dimensions=embedding_dimensions,
            search_text=search_text,
            needs_reindex=needs_reindex,
            last_error=None,
            indexed_at=indexed_at,
            embedding=embedding,
        )
        db.add(row)
    else:
        row.content_hash = content_hash
        row.embedding_provider = current_provider
        row.embedding_model = current_model
        row.embedding_dimensions = embedding_dimensions
        row.search_text = search_text
        row.needs_reindex = needs_reindex
        row.last_error = None
        row.indexed_at = indexed_at
        row.embedding = embedding
    db.flush()
    return row


def _semantic_scores_for_bugs_postgres(
    db: Session,
    *,
    visible_ids: list[int],
    query_embedding: list[float],
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
) -> dict[int, float]:
    if VectorValue is None:
        return {}

    query_vector_text = VectorValue(query_embedding).to_text()
    query_vector = literal_column(f"'{query_vector_text}'::vector({embedding_dimensions})")
    embedding_cast = literal_column(f"(bug_search_index.embedding::vector({embedding_dimensions}))")
    distance_expr = embedding_cast.op("<=>", return_type=Float())(query_vector)
    score_expr = (1 - distance_expr).label("score")
    ranked_rows = db.execute(
        select(BugSearchIndex.bug_id, score_expr)
        .where(
            BugSearchIndex.bug_id.in_(visible_ids),
            BugSearchIndex.embedding.is_not(None),
            BugSearchIndex.embedding_provider == embedding_provider,
            BugSearchIndex.embedding_model == embedding_model,
            BugSearchIndex.embedding_dimensions == embedding_dimensions,
        )
        .order_by(distance_expr.asc())
    ).all()
    return {bug_id: max(0.0, min(1.0, float(score))) for bug_id, score in ranked_rows}


def _build_embedding(
    text: str,
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> list[float] | None:
    resolved_provider, resolved_model = _resolve_embedding_selection(
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    return embed_text(
        text=text,
        embedding_provider=resolved_provider,
        embedding_model=resolved_model,
    )


def get_search_telemetry_snapshot() -> dict[str, float]:
    with _SEARCH_TELEMETRY_LOCK:
        snapshot = dict(_SEARCH_TELEMETRY)

    total_queries = snapshot["total_queries"]
    if total_queries > 0:
        snapshot["avg_latency_ms"] = snapshot["total_duration_ms"] / total_queries
        snapshot["avg_results_per_query"] = snapshot["total_results"] / total_queries
        snapshot["fallback_rate"] = snapshot["keyword_fallback_queries"] / total_queries
        snapshot["embedding_unavailable_rate"] = snapshot["embedding_unavailable_queries"] / total_queries
    else:
        snapshot["avg_latency_ms"] = 0.0
        snapshot["avg_results_per_query"] = 0.0
        snapshot["fallback_rate"] = 0.0
        snapshot["embedding_unavailable_rate"] = 0.0
    return snapshot


def _record_search_telemetry(
    *,
    mode: str,
    results_count: int,
    duration_ms: float,
    embedding_available: bool,
) -> None:
    with _SEARCH_TELEMETRY_LOCK:
        _SEARCH_TELEMETRY["total_queries"] += 1
        _SEARCH_TELEMETRY["total_results"] += max(0, results_count)
        _SEARCH_TELEMETRY["total_duration_ms"] += max(0.0, duration_ms)
        if mode == "exact":
            _SEARCH_TELEMETRY["exact_match_queries"] += 1
        elif mode == "hybrid":
            _SEARCH_TELEMETRY["hybrid_queries"] += 1
        else:
            _SEARCH_TELEMETRY["keyword_fallback_queries"] += 1
        if not embedding_available:
            _SEARCH_TELEMETRY["embedding_unavailable_queries"] += 1


def _resolve_embedding_selection(
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> tuple[str, str]:
    if _sqlite_vec_lock_active():
        configured_provider = _sqlite_vec_embedding_provider()
        configured_model = _sqlite_vec_embedding_model(configured_provider)
    else:
        configured_provider = (settings.embedding_provider or "openai").strip().casefold()
        configured_model = (
            settings.local_embedding_model if configured_provider == "local" else settings.embedding_model
        )

    if settings.embedding_lock_enabled:
        return configured_provider, configured_model

    requested_provider = (embedding_provider or configured_provider).strip().casefold() or configured_provider
    if requested_provider not in {"openai", "local"}:
        requested_provider = configured_provider

    requested_model = (embedding_model or "").strip()
    if not requested_model:
        requested_model = (
            settings.local_embedding_model if requested_provider == "local" else settings.embedding_model
        )
    return requested_provider, requested_model


def upsert_bug_search_index(
    db: Session,
    bug: Bug,
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embedding: bool = False,
) -> BugSearchIndex:
    return _ensure_search_index(
        db,
        bug,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        build_embedding=build_embedding,
    )


def upsert_bug_search_index_by_id(
    db: Session,
    *,
    bug_id: int,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embedding: bool = False,
) -> BugSearchIndex | None:
    bug = db.scalar(
        select(Bug)
        .options(
            selectinload(Bug.attachments),
            selectinload(Bug.comments),
            selectinload(Bug.history),
            selectinload(Bug.view_states),
        )
        .where(Bug.id == bug_id, Bug.deleted_at.is_(None))
    )
    if not bug:
        return None
    return upsert_bug_search_index(
        db,
        bug,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        build_embedding=build_embedding,
    )


def mark_bug_search_index_dirty_by_id(
    db: Session,
    *,
    bug_id: int,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
) -> BugSearchIndex | None:
    return upsert_bug_search_index_by_id(
        db,
        bug_id=bug_id,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        build_embedding=False,
    )


def rebuild_bug_search_index(
    db: Session,
    *,
    embedding_provider: str | None = None,
    embedding_model: str | None = None,
    build_embedding: bool = True,
    dirty_only: bool = False,
    limit: int | None = None,
) -> int:
    query = (
        select(Bug)
        .options(
            selectinload(Bug.attachments),
            selectinload(Bug.comments),
            selectinload(Bug.history),
            selectinload(Bug.view_states),
        )
        .where(Bug.deleted_at.is_(None))
        .order_by(Bug.id.asc())
    )
    if dirty_only:
        query = query.outerjoin(BugSearchIndex, BugSearchIndex.bug_id == Bug.id).where(
            (BugSearchIndex.bug_id.is_(None))
            | (BugSearchIndex.needs_reindex == 1)
            | (BugSearchIndex.updated_at.is_(None))
            | (BugSearchIndex.updated_at < Bug.updated_at)
        )
    if limit is not None and limit > 0:
        query = query.limit(limit)
    bugs = db.scalars(query).all()
    for bug in bugs:
        upsert_bug_search_index(
            db,
            bug,
            embedding_provider=embedding_provider,
            embedding_model=embedding_model,
            build_embedding=build_embedding,
        )
    return len(bugs)


def _build_bug_search_text(bug: Bug) -> str:
    parts = [
        f"Bug ID: {bug.id}",
        f"Title: {bug.title}",
        f"Description: {bug.description}",
        f"Category: {bug.category}",
        f"Severity: {bug.severity}",
        f"Status: {bug.status}",
        f"Reporter: {bug.reporter_id}",
        f"Assignee: {bug.assignee_id or ''}",
        f"Environment: {bug.environment or ''}",
        f"Reproduction steps: {bug.repro_steps or ''}",
        f"Tags: {bug.tags or ''}",
        f"Notify: {bug.notify_emails or ''}",
    ]
    if bug.comments:
        parts.append("Comments:")
        parts.extend(comment.body for comment in bug.comments if comment.body)
    if bug.attachments:
        parts.append("Attachments:")
        parts.extend(attachment.filename for attachment in bug.attachments if attachment.filename)
    return "\n".join(part for part in parts if part.strip())


def _search_text_content_hash(*, search_text: str, embedding_provider: str, embedding_model: str) -> str:
    hash_key = f"{embedding_provider}|{embedding_model}|{search_text}"
    return hashlib.sha256(hash_key.encode("utf-8")).hexdigest()


def _search_index_row_is_stale(
    row: BugSearchIndex | None,
    *,
    bug: Bug,
    embedding_provider: str,
    embedding_model: str,
    embedding_dimensions: int,
    require_embedding: bool,
) -> bool:
    if row is None:
        return True
    if int(getattr(row, "needs_reindex", 0) or 0) == 1:
        return True
    if row.embedding_provider != embedding_provider or row.embedding_model != embedding_model:
        return True
    expected_hash = _search_text_content_hash(
        search_text=_build_bug_search_text(bug),
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
    )
    if row.content_hash != expected_hash:
        return True
    if not require_embedding:
        return False
    if not row.embedding:
        return True
    if row.embedding_dimensions != embedding_dimensions:
        return True
    return False


def _keyword_score(query: str, bug: Bug, search_text: str) -> float:
    lower_query = query.casefold()
    lower_text = search_text.casefold()
    lower_title = bug.title.casefold()
    query_tokens = _tokenize_query(lower_query)

    if lower_query == str(bug.id):
        return 1.0

    if not query_tokens:
        return 0.0

    title_tokens = set(_tokenize_query(lower_title))
    text_tokens = set(_tokenize_query(lower_text))
    title_coverage = _fuzzy_token_coverage(query_tokens, title_tokens)
    text_coverage = _fuzzy_token_coverage(query_tokens, text_tokens)

    phrase_title = 1.0 if lower_query and lower_query in lower_title else 0.0
    phrase_text = 1.0 if lower_query and lower_query in lower_text else 0.0

    score = (
        (0.55 * title_coverage)
        + (0.30 * text_coverage)
        + (0.12 * phrase_title)
        + (0.08 * phrase_text)
    )
    return max(0.0, min(1.0, score))


def _fuzzy_token_coverage(query_tokens: list[str], candidate_tokens: set[str]) -> float:
    if not query_tokens or not candidate_tokens:
        return 0.0

    matched_weight = 0.0
    for token in query_tokens:
        if token in candidate_tokens:
            matched_weight += 1.0
            continue
        # Fuzzy fallback for minor typos like repeated/omitted characters.
        if len(token) < 4:
            continue
        best_ratio = 0.0
        for candidate in candidate_tokens:
            if not candidate:
                continue
            if candidate[0] != token[0]:
                continue
            if abs(len(candidate) - len(token)) > 8:
                continue
            ratio = SequenceMatcher(None, token, candidate).ratio()
            if len(candidate) > len(token):
                ratio = max(ratio, SequenceMatcher(None, token, candidate[: len(token)]).ratio())
            if ratio > best_ratio:
                best_ratio = ratio
            if best_ratio >= 0.92:
                break
        if best_ratio >= 0.84:
            matched_weight += 0.75

    return matched_weight / float(len(query_tokens))


def _cosine_similarity(left: Iterable[float], right: Iterable[float]) -> float:
    left_values = list(left)
    right_values = list(right)
    if not left_values or not right_values or len(left_values) != len(right_values):
        return 0.0
    dot_product = sum(a * b for a, b in zip(left_values, right_values))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return max(0.0, min(1.0, dot_product / (left_norm * right_norm)))
