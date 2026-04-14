from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import streamlit as st

SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1}
STATUS_ORDER = {"open": 1, "resolved": 2}
STATUS_LABELS = {"open": "Åpen", "resolved": "Løst"}
LOGO_PATH = Path(__file__).resolve().parent / "logo-white.svg"


def normalize_bug_status(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized in {"resolved", "closed", "løst", "lost"}:
        return "resolved"
    return "open"


def status_label(value: str | None) -> str:
    return STATUS_LABELS.get(normalize_bug_status(value), "Åpen")


def apply_shared_app_style() -> None:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.2rem;
            padding-bottom: 1.2rem;
        }
        div[data-testid="stMetric"] {
            border: 1px solid rgba(49, 51, 63, 0.15);
            border-radius: 0.5rem;
            padding: 0.6rem 0.8rem;
            background: rgba(250, 250, 250, 0.4);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar_logo(app_title: str | None = None) -> None:
    if not LOGO_PATH.exists():
        if app_title:
            st.sidebar.markdown(f"### {app_title}")
        return
    try:
        logo_svg = LOGO_PATH.read_text(encoding="utf-8")
    except OSError:
        if app_title:
            st.sidebar.markdown(f"### {app_title}")
        return
    logo_data = base64.b64encode(logo_svg.encode("utf-8")).decode("ascii")
    st.sidebar.markdown(
        f"""
        <div style="
            background:#0f172a;
            border-radius:14px;
            padding:18px 14px;
            margin-bottom:14px;
            text-align:center;
        ">
            <div style="max-width:180px; margin:0 auto;">
                <a href="https://www.xlent.no" target="_blank" rel="noopener noreferrer" style="display:block;">
                    <img src="data:image/svg+xml;base64,{logo_data}" alt="Logo" style="width:100%; height:auto;" />
                </a>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if app_title:
        st.sidebar.markdown(
            f"""
            <div style="
                text-align:center;
                margin-top:-6px;
                margin-bottom:14px;
                font-size:1.05rem;
                font-weight:700;
                color:#0f172a;
            ">{app_title}</div>
            """,
            unsafe_allow_html=True,
        )


def format_datetime_display(value: datetime | None) -> str:
    if not value:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def sidebar_section(title: str, *, expanded: bool = False):
    return st.sidebar.expander(title, expanded=expanded)


def _cache_store() -> dict[str, dict]:
    if "_cloud_ui_cache" not in st.session_state:
        st.session_state["_cloud_ui_cache"] = {}
    return st.session_state["_cloud_ui_cache"]


def cached_value(cache_key: str, ttl_seconds: int, loader: Callable[[], object]):
    now = time.time()
    store = _cache_store()
    cached = store.get(cache_key)
    if cached and (now - float(cached.get("ts", 0))) < max(1, ttl_seconds):
        return cached.get("value")
    value = loader()
    store[cache_key] = {"ts": now, "value": value}
    return value


def clear_cached_value(cache_key_prefix: str) -> None:
    store = _cache_store()
    for key in list(store.keys()):
        if key.startswith(cache_key_prefix):
            store.pop(key, None)


def render_sidebar_search(prefix: str, *, label: str = "Søk i bugs") -> str:
    key = f"{prefix}_search_query"
    st.sidebar.text_input(label, key=key)
    return str(st.session_state.get(key, "")).strip()


def render_sidebar_refresh_button(prefix: str) -> bool:
    return st.sidebar.button("Oppdater visning", key=f"{prefix}_refresh_view", use_container_width=True)


def _tag_set(tags_value: str | None) -> set[str]:
    if not tags_value:
        return set()
    return {item.strip().casefold() for item in str(tags_value).split(",") if item.strip()}


def _search_blob(bug: object) -> str:
    fields = [
        str(getattr(bug, "title", "") or ""),
        str(getattr(bug, "description", "") or ""),
        str(getattr(bug, "reporter_id", "") or ""),
        str(getattr(bug, "assignee_id", "") or ""),
        str(getattr(bug, "status", "") or ""),
        str(getattr(bug, "severity", "") or ""),
        str(getattr(bug, "tags", "") or ""),
        str(getattr(bug, "environment", "") or ""),
    ]
    comments = []
    try:
        comments = getattr(bug, "comments", None) or []
    except Exception:
        comments = []
    fields.extend(str(getattr(comment, "body", "") or "") for comment in comments)
    return " ".join(fields).casefold()


def render_sidebar_bug_filters(prefix: str, bugs: list[object]) -> None:
    severities = sorted(
        {str(getattr(bug, "severity", "") or "").strip() for bug in bugs if str(getattr(bug, "severity", "") or "").strip()},
        key=lambda item: -SEVERITY_ORDER.get(item, 0),
    )
    tags = sorted({tag for bug in bugs for tag in _tag_set(getattr(bug, "tags", None))})

    with sidebar_section("Filtrering", expanded=False):
        st.selectbox(
            "Status",
            options=["all", "open", "resolved"],
            index=0,
            key=f"{prefix}_filter_status_mode",
            format_func=lambda value: {
                "all": "Begge",
                "open": "Åpne",
                "resolved": "Løste",
            }.get(str(value), str(value)),
            help="Velg om du vil se åpne, løste eller begge typer bugs.",
        )
        st.multiselect("Alvorlighetsgrad", options=severities, key=f"{prefix}_filter_severity")
        st.multiselect("Tagger", options=tags, key=f"{prefix}_filter_tags")
        st.selectbox(
            "Sortering",
            options=["Nyeste først", "Eldste først", "Alvorlighetsgrad", "Status"],
            key=f"{prefix}_sort_mode",
        )


def apply_sidebar_bug_filters(
    *,
    bugs: list[object],
    prefix: str,
    apply_query_filter: bool = True,
) -> list[object]:
    query = str(st.session_state.get(f"{prefix}_search_query", "")).strip().casefold()
    status_mode = str(st.session_state.get(f"{prefix}_filter_status_mode", "all") or "all").strip().casefold()
    severity_filter = {str(item).strip() for item in st.session_state.get(f"{prefix}_filter_severity", []) if str(item).strip()}
    tag_filter = {str(item).strip().casefold() for item in st.session_state.get(f"{prefix}_filter_tags", []) if str(item).strip()}
    sort_mode = str(st.session_state.get(f"{prefix}_sort_mode", "Nyeste først"))

    filtered: list[object] = []
    for bug in bugs:
        bug_status = normalize_bug_status(str(getattr(bug, "status", "") or "").strip())
        bug_severity = str(getattr(bug, "severity", "") or "").strip()
        bug_tags = _tag_set(getattr(bug, "tags", None))

        if status_mode in {"open", "resolved"} and bug_status != status_mode:
            continue
        if severity_filter and bug_severity not in severity_filter:
            continue
        if tag_filter and not (tag_filter & bug_tags):
            continue
        if apply_query_filter and query and query not in _search_blob(bug):
            continue
        filtered.append(bug)

    def _created(bug: object) -> datetime:
        value = getattr(bug, "created_at", None)
        if isinstance(value, datetime):
            return value
        return datetime.min.replace(tzinfo=timezone.utc)

    if sort_mode == "Eldste først":
        filtered.sort(key=_created)
    elif sort_mode == "Alvorlighetsgrad":
        filtered.sort(key=lambda bug: -SEVERITY_ORDER.get(str(getattr(bug, "severity", "") or ""), 0))
    elif sort_mode == "Status":
        filtered.sort(key=lambda bug: STATUS_ORDER.get(normalize_bug_status(str(getattr(bug, "status", "") or "")), 99))
    else:
        filtered.sort(key=_created, reverse=True)
    return filtered


def render_bug_list_controls(*, prefix: str, total_count: int, step: int = 5, default_visible: int = 10) -> int:
    if total_count <= 0:
        return 0
    if total_count == 1:
        st.session_state[f"{prefix}_visible_count"] = 1
        return 1

    options = sorted(
        {
            min(total_count, value)
            for value in range(step, max(step, total_count) + step, step)
        }
        | {min(total_count, max(step, default_visible)), total_count}
    )
    if len(options) <= 1:
        value = int(options[0]) if options else 1
        st.session_state[f"{prefix}_visible_count"] = value
        return value

    current = st.session_state.get(f"{prefix}_visible_count")
    if current not in options:
        st.session_state[f"{prefix}_visible_count"] = min(total_count, max(step, default_visible))
    st.select_slider(
        "Antall bugs vist",
        options=options,
        key=f"{prefix}_visible_count",
    )
    return int(st.session_state.get(f"{prefix}_visible_count", min(total_count, max(step, default_visible))))


def render_bug_status_summary(*, bugs: Iterable[object], title: str = "Statusoversikt") -> None:
    bug_list = list(bugs)
    if not bug_list:
        st.caption("Ingen bugs å vise.")
        return

    total = len(bug_list)
    open_count = sum(1 for bug in bug_list if normalize_bug_status(str(getattr(bug, "status", "") or "")) == "open")
    resolved_count = total - open_count
    critical_count = sum(1 for bug in bug_list if str(getattr(bug, "severity", "") or "") == "critical")

    st.caption(title)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Totalt", total)
    c2.metric("Åpne", open_count)
    c3.metric("Løste", resolved_count)
    c4.metric("Kritiske", critical_count)


def build_bug_expander_title(bug: object) -> str:
    bug_id = getattr(bug, "id", "?")
    title = str(getattr(bug, "title", "") or "Uten tittel")
    normalized_status = normalize_bug_status(str(getattr(bug, "status", "") or "-"))
    status = status_label(normalized_status)
    status_marker = "🔴" if normalized_status == "open" else "🟢"
    created = format_datetime_display(getattr(bug, "created_at", None))
    return f"{status_marker} #{bug_id} - {title} [{status}] | {created}"


def render_system_health_panel(*, oidc_configured: bool, local_login_enabled: bool) -> None:
    with sidebar_section("System og drift", expanded=False):
        if oidc_configured:
            st.success("Microsoft Entra: konfigurert")
        else:
            st.warning("Microsoft Entra: mangler konfigurasjon")

        if local_login_enabled:
            st.warning("Lokal fallback-login: aktiv")
        else:
            st.info("Lokal fallback-login: deaktivert")
