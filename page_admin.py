from __future__ import annotations

from typing import Any


def render_admin_page(user: dict[str, str], **deps: Any) -> None:
    st = deps["st"]
    _prepare_page_bug_list = deps["_prepare_page_bug_list"]
    _sidebar_render_once = deps["_sidebar_render_once"]
    _render_sidebar_work_queue_filters = deps["_render_sidebar_work_queue_filters"]
    _apply_sidebar_work_queue_filters = deps["_apply_sidebar_work_queue_filters"]
    _render_admin_sidebar_advanced_filters = deps["_render_admin_sidebar_advanced_filters"]
    _apply_admin_advanced_filters = deps["_apply_admin_advanced_filters"]
    _render_admin_sidebar_queue_summary = deps["_render_admin_sidebar_queue_summary"]
    _render_admin_sidebar_duplicates = deps["_render_admin_sidebar_duplicates"]
    _render_admin_access_management_sidebar = deps["_render_admin_access_management_sidebar"]
    _build_assignable_emails = deps["_build_assignable_emails"]
    render_bug_status_summary = deps["render_bug_status_summary"]
    _render_admin_dashboard_cards = deps["_render_admin_dashboard_cards"]
    _render_admin_trend_report = deps["_render_admin_trend_report"]
    _render_admin_audit_log_panel = deps["_render_admin_audit_log_panel"]
    _render_admin_operations_panel = deps["_render_admin_operations_panel"]
    _render_bug_export_sidebar = deps["_render_bug_export_sidebar"]
    render_bug_list_controls = deps["render_bug_list_controls"]
    _sentiment_symbol = deps["_sentiment_symbol"]
    build_bug_expander_title = deps["build_bug_expander_title"]
    _get_tracked_job = deps["_get_tracked_job"]
    _get_background_job = deps["_get_background_job"]
    _clear_tracked_job = deps["_clear_tracked_job"]
    _finalize_background_job = deps["_finalize_background_job"]
    _clear_bug_cache = deps["_clear_bug_cache"]
    format_datetime_display = deps["format_datetime_display"]
    status_label = deps["status_label"]
    _start_background_job = deps["_start_background_job"]
    _allow_ai_action = deps["_allow_ai_action"]
    _run_bug_sentiment_analysis = deps["_run_bug_sentiment_analysis"]
    _wait_for_background_job_completion = deps["_wait_for_background_job_completion"]
    _request_delete_confirmation = deps["_request_delete_confirmation"]
    _render_delete_confirmation = deps["_render_delete_confirmation"]
    _delete_bug = deps["_delete_bug"]
    _can_user_delete_bug = deps["_can_user_delete_bug"]
    _can_user_reopen_bug = deps["_can_user_reopen_bug"]
    _run_bug_summary = deps["_run_bug_summary"]
    normalize_bug_status = deps["normalize_bug_status"]
    _render_bug_thread = deps["_render_bug_thread"]
    _assignee_select_options = deps["_assignee_select_options"]
    _normalize_email = deps["_normalize_email"]
    STATUS_OPTIONS = deps["STATUS_OPTIONS"]
    SEVERITY_OPTIONS = deps["SEVERITY_OPTIONS"]
    CATEGORY_OPTIONS = deps["CATEGORY_OPTIONS"]
    REPORTER_SATISFACTION_OPTIONS = deps["REPORTER_SATISFACTION_OPTIONS"]
    MAX_ATTACHMENTS_PER_UPLOAD = deps["MAX_ATTACHMENTS_PER_UPLOAD"]
    MAX_ATTACHMENT_BYTES = deps["MAX_ATTACHMENT_BYTES"]
    _update_bug = deps["_update_bug"]
    _upload_attachments_for_bug = deps["_upload_attachments_for_bug"]
    _add_comment = deps["_add_comment"]
    _render_attachments = deps["_render_attachments"]
    _render_bug_history = deps["_render_bug_history"]
    _prefetch_bug_details = deps["_prefetch_bug_details"]
    _sla_brief_label = deps["_sla_brief_label"]

    bugs = _prepare_page_bug_list(user=user, prefix="admin")
    if _sidebar_render_once("admin_sidebar_work_queue_filters"):
        _render_sidebar_work_queue_filters(prefix="admin", mode="admin")
    bugs = _apply_sidebar_work_queue_filters(bugs, prefix="admin", mode="admin")
    if _sidebar_render_once("admin_sidebar_advanced_filters"):
        _render_admin_sidebar_advanced_filters()
    bugs = _apply_admin_advanced_filters(bugs)
    if _sidebar_render_once("admin_sidebar_export"):
        _render_bug_export_sidebar(prefix="admin", bugs=bugs)
    if _sidebar_render_once("admin_sidebar_queue_summary"):
        _render_admin_sidebar_queue_summary(bugs)
    if _sidebar_render_once("admin_sidebar_duplicates"):
        _render_admin_sidebar_duplicates(user, bugs)
    if _sidebar_render_once("admin_sidebar_access_management"):
        _render_admin_access_management_sidebar(current_admin_email=user["email"])
    assignable_emails = _build_assignable_emails()
    render_bug_status_summary(bugs=bugs, title="Admin-oversikt")
    _render_admin_dashboard_cards(bugs)
    _render_admin_trend_report(bugs)
    _render_admin_audit_log_panel()
    _render_admin_operations_panel(user)

    if not bugs:
        st.info("Ingen bugs å vise med gjeldende admin-filtre.")
        return

    visible_count = render_bug_list_controls(prefix="admin", total_count=len(bugs), default_visible=8)
    st.caption(f"Viser {min(len(bugs), visible_count)} av {len(bugs)} bugs.")
    visible_bugs = bugs[:visible_count]
    _prefetch_bug_details(visible_bugs)
    for bug in visible_bugs:
        sentiment_badge = _sentiment_symbol(bug.sentiment_label)
        is_resolved = normalize_bug_status(bug.status) == "resolved"
        sentiment_section = f"      |      Sentiment {sentiment_badge}" if sentiment_badge else "      |      Sentiment -"
        header = f"{build_bug_expander_title(bug)}{sentiment_section}"
        with st.expander(header, expanded=False):
            for job_key, running_message in (
                ("sentiment", "Sentimentanalyse behandles i bakgrunnen."),
                ("summarize", "Bugoppsummering behandles i bakgrunnen."),
            ):
                tracked = _get_tracked_job("admin", bug.id, job_key)
                if not tracked:
                    continue
                tracked_job_id = int(tracked.get("job_id", 0) or 0)
                job_payload = _get_background_job(tracked_job_id)
                if job_payload is None:
                    _clear_tracked_job("admin", bug.id, job_key)
                    continue
                status = str(job_payload.get("status") or "unknown")
                if status in {"pending", "running"}:
                    st.info(running_message)
                    if st.button(
                        "Oppdater jobbstatus",
                        key=f"admin_refresh_job_{bug.id}_{job_key}",
                        use_container_width=True,
                    ):
                        st.rerun()
                    continue

                result = job_payload.get("result")
                if isinstance(result, dict):
                    result_error = str(result.get("error") or "").strip()
                    if result_error:
                        st.error(result_error)
                    else:
                        success_text = "Sentimentanalyse fullført." if job_key == "sentiment" else "Bugoppsummering fullført."
                        st.success(success_text)
                elif str(job_payload.get("error") or "").strip():
                    st.error(str(job_payload.get("error")))

                _clear_tracked_job("admin", bug.id, job_key)
                _finalize_background_job(tracked_job_id)
                _clear_bug_cache()
                st.rerun()

            top_left, top_right = st.columns([3, 1])
            with top_left:
                st.write(bug.description)
            with top_right:
                if st.button(
                    "Oppdater",
                    key=f"refresh_admin_bug_{bug.id}",
                    use_container_width=True,
                    help="Laster inn siste versjon av denne bugen.",
                ):
                    _clear_bug_cache()
                    st.rerun()

            display_reporting_date = format_datetime_display(bug.reporting_date or bug.created_at)
            st.caption(
                f"Rapportør: {bug.reporter_id} | Tildelt: {bug.assignee_id or '-'} | "
                f"Rapportert dato: {display_reporting_date} | Status: {status_label(bug.status)} | "
                f"Alvorlighetsgrad: {bug.severity} | Kategori: {bug.category or '-'} | Miljø: {bug.environment or '-'} | "
                f"Tagger: {bug.tags or '-'} | Sentiment: {bug.sentiment_label or '-'} | {_sla_brief_label(bug)}"
            )
            if bug.reporter_satisfaction:
                st.caption(f"Rapportør-tilfredshet: {bug.reporter_satisfaction}")
            if bug.sentiment_summary:
                st.caption(f"Sentimentoppsummering: {bug.sentiment_summary}")
            if bug.bug_summary:
                st.info(f"Oppsummering: {bug.bug_summary}")

            a1, a2, a3 = st.columns(3)
            with a1:
                sentiment_clicked = st.button(
                    "Sentiment - analyse",
                    key=f"admin_sentiment_{bug.id}",
                    use_container_width=True,
                    help="Analyserer sentiment i samtalen for denne bugen.",
                    disabled=is_resolved,
                )
            with a2:
                delete_clicked = st.button(
                    "Slett bug",
                    key=f"admin_delete_{bug.id}",
                    use_container_width=True,
                    help="Flytter bugen til papirkurv.",
                    disabled=not _can_user_delete_bug(user),
                )
            with a3:
                summarize_clicked = st.button(
                    "AI-Oppsummer bug",
                    key=f"admin_summarize_{bug.id}",
                    use_container_width=True,
                    help="Genererer en kort AI-oppsummering av bug og samtale.",
                    disabled=is_resolved,
                )

            if sentiment_clicked:
                allowed, throttle_message = _allow_ai_action(f"admin:sentiment:{bug.id}")
                if not allowed:
                    st.warning(str(throttle_message or "AI-knappen er midlertidig sperret. Prøv igjen om litt."))
                else:
                    job_id = _start_background_job(
                        prefix="admin",
                        bug_id=bug.id,
                        job_key="sentiment",
                        job_label="Sentimentanalyse",
                        target=lambda: {"error": _run_bug_sentiment_analysis(user, bug.id)},
                    )
                    quick_state = _wait_for_background_job_completion(job_id, timeout_seconds=6, poll_seconds=0.5)
                    if quick_state == "timeout":
                        st.info("Sentimentanalyse fortsetter i bakgrunnen.")
                    st.rerun()

            if delete_clicked:
                _request_delete_confirmation(prefix="admin", item_key=f"bug_{bug.id}")
                st.rerun()
            if _render_delete_confirmation(
                prefix="admin",
                item_key=f"bug_{bug.id}",
                message="Er du sikker på at du vil slette denne buggen?",
            ):
                error = _delete_bug(user, bug.id)
                if error:
                    st.error(error)
                else:
                    st.success("Bug flyttet til papirkurv.")
                    st.session_state.pop("admin_duplicate_candidates", None)
                    st.rerun()

            if summarize_clicked:
                allowed, throttle_message = _allow_ai_action(f"admin:summarize:{bug.id}")
                if not allowed:
                    st.warning(str(throttle_message or "AI-knappen er midlertidig sperret. Prøv igjen om litt."))
                else:
                    job_id = _start_background_job(
                        prefix="admin",
                        bug_id=bug.id,
                        job_key="summarize",
                        job_label="Bugoppsummering",
                        target=lambda: {"error": _run_bug_summary(user, bug.id)},
                    )
                    quick_state = _wait_for_background_job_completion(job_id, timeout_seconds=6, poll_seconds=0.5)
                    if quick_state == "timeout":
                        st.info("Bugoppsummering fortsetter i bakgrunnen.")
                    st.rerun()

            quick_col1, quick_col2 = st.columns(2)
            with quick_col1:
                close_clicked = st.button(
                    "Sett som løst",
                    key=f"admin_close_bug_{bug.id}",
                    use_container_width=True,
                    disabled=normalize_bug_status(bug.status) == "resolved",
                )
            with quick_col2:
                reopen_clicked = st.button(
                    "Gjenåpne bug",
                    key=f"admin_reopen_bug_{bug.id}",
                    use_container_width=True,
                    disabled=(normalize_bug_status(bug.status) != "resolved") or (not _can_user_reopen_bug(user)),
                )

            if close_clicked or reopen_clicked:
                target_status = "resolved" if close_clicked else "open"
                error = _update_bug(
                    user,
                    bug_id=bug.id,
                    status=target_status,
                    severity=bug.severity,
                    assignee_id=bug.assignee_id,
                    category=bug.category,
                    environment=bug.environment,
                    tags=bug.tags,
                    notify_emails=bug.notify_emails,
                    reporter_satisfaction=bug.reporter_satisfaction,
                )
                if error:
                    st.error(error)
                else:
                    st.success("Status oppdatert.")
                    st.rerun()

            _render_bug_thread(bug, title="Samtale", collapsed=True, dedupe_consecutive=True)

            if is_resolved:
                st.info("Denne bugen er løst og kan ikke oppdateres. Sett den tilbake til Åpen for å gjøre endringer.")

            c1, c2 = st.columns(2)
            with c1:
                assignee_options = _assignee_select_options(bug.assignee_id, assignable_emails)
                current_assignee = _normalize_email(bug.assignee_id)
                assignee = st.selectbox(
                    "Tildel bug til",
                    options=assignee_options,
                    index=assignee_options.index(current_assignee) if current_assignee in assignee_options else 0,
                    key=f"admin_assignee_{bug.id}",
                    format_func=lambda value: value if value else "Ikke tildelt",
                    disabled=is_resolved,
                )
            with c2:
                status = st.selectbox(
                    "Status",
                    STATUS_OPTIONS,
                    index=STATUS_OPTIONS.index(normalize_bug_status(bug.status))
                    if normalize_bug_status(bug.status) in set(STATUS_OPTIONS)
                    else 0,
                    key=f"admin_status_{bug.id}",
                    format_func=status_label,
                    disabled=is_resolved,
                )
            c3, c4 = st.columns(2)
            with c3:
                severity = st.selectbox(
                    "Alvorlighetsgrad",
                    SEVERITY_OPTIONS,
                    index=SEVERITY_OPTIONS.index(bug.severity) if bug.severity in set(SEVERITY_OPTIONS) else 1,
                    key=f"admin_severity_{bug.id}",
                    disabled=is_resolved,
                )
            with c4:
                environment = st.text_input(
                    "Miljø",
                    value=bug.environment or "",
                    key=f"admin_env_{bug.id}",
                    disabled=is_resolved,
                )

            c5, c6 = st.columns(2)
            with c5:
                tags = st.text_input(
                    "Tagger",
                    value=bug.tags or "",
                    key=f"admin_tags_{bug.id}",
                    disabled=is_resolved,
                )
            with c6:
                notify_emails = st.text_input(
                    "Varsle e-post(er)",
                    value="",
                    key=f"admin_notify_{bug.id}",
                    disabled=is_resolved,
                )

            c7, c8 = st.columns(2)
            with c7:
                category = st.selectbox(
                    "Kategori",
                    CATEGORY_OPTIONS,
                    index=CATEGORY_OPTIONS.index(bug.category) if bug.category in CATEGORY_OPTIONS else 0,
                    key=f"admin_category_{bug.id}",
                    disabled=is_resolved,
                )
            with c8:
                satisfaction_options = ["ikke oppgitt", *REPORTER_SATISFACTION_OPTIONS]
                current_satisfaction = (
                    bug.reporter_satisfaction
                    if bug.reporter_satisfaction in REPORTER_SATISFACTION_OPTIONS
                    else "ikke oppgitt"
                )
                reporter_satisfaction = st.selectbox(
                    "Rapportør-tilfredshet",
                    options=satisfaction_options,
                    index=satisfaction_options.index(current_satisfaction),
                    key=f"admin_satisfaction_{bug.id}",
                    disabled=is_resolved,
                )

            note_col, upload_col = st.columns([1.8, 1.2])
            with note_col:
                admin_note = st.text_area(
                    "Arbeidsnotat",
                    key=f"admin_note_{bug.id}",
                    height=90,
                    placeholder="Skriv intern oppdatering som publiseres i samtalen.",
                    disabled=is_resolved,
                )
            with upload_col:
                new_attachments = st.file_uploader(
                    "Last opp vedlegg",
                    accept_multiple_files=True,
                    key=f"admin_new_attachments_{bug.id}",
                    help=f"Maks {MAX_ATTACHMENTS_PER_UPLOAD} filer, opptil {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB per fil.",
                    disabled=is_resolved,
                )

            if st.button("Lagre endringer", key=f"admin_save_{bug.id}", use_container_width=True, disabled=is_resolved):
                error = _update_bug(
                    user,
                    bug_id=bug.id,
                    status=status,
                    severity=severity,
                    assignee_id=assignee,
                    category=category,
                    environment=environment,
                    tags=tags,
                    notify_emails=notify_emails,
                    reporter_satisfaction=None if reporter_satisfaction == "ikke oppgitt" else reporter_satisfaction,
                )
                if error:
                    st.error(error)
                else:
                    upload_errors = _upload_attachments_for_bug(user, bug.id, list(new_attachments or []))
                    if admin_note.strip():
                        c_error = _add_comment(user, bug.id, admin_note.strip())
                        if c_error:
                            st.error(c_error)
                    if upload_errors:
                        st.warning("Noen vedlegg kunne ikke lastes opp:")
                        for item in upload_errors:
                            st.write(f"- {item}")
                    st.success("Endringer lagret.")
                    st.rerun()
            _render_attachments(bug, key_prefix=f"admin_{bug.id}")
            _render_bug_history(bug, collapsed=True)
