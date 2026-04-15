from __future__ import annotations

import re
from typing import Any


def render_admin_page(user: dict[str, str], **deps: Any) -> None:
    st = deps["st"]
    _prepare_page_bug_list = deps["_prepare_page_bug_list"]
    _sidebar_render_once = deps["_sidebar_render_once"]
    _sidebar_should_render = deps["_sidebar_should_render"]
    _render_sidebar_work_queue_filters = deps["_render_sidebar_work_queue_filters"]
    _apply_sidebar_work_queue_filters = deps["_apply_sidebar_work_queue_filters"]
    _render_admin_sidebar_advanced_filters = deps["_render_admin_sidebar_advanced_filters"]
    _apply_admin_advanced_filters = deps["_apply_admin_advanced_filters"]
    _render_admin_sidebar_queue_summary = deps["_render_admin_sidebar_queue_summary"]
    _render_admin_sidebar_duplicates = deps["_render_admin_sidebar_duplicates"]
    _render_admin_access_management_sidebar = deps["_render_admin_access_management_sidebar"]
    _render_admin_devops_settings_sidebar = deps["_render_admin_devops_settings_sidebar"]
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
    _send_bug_to_devops = deps["_send_bug_to_devops"]
    _update_bug_in_devops = deps["_update_bug_in_devops"]
    _fetch_bug_from_devops = deps["_fetch_bug_from_devops"]
    _apply_devops_snapshot_to_local_bug = deps["_apply_devops_snapshot_to_local_bug"]
    _remove_bug_from_devops = deps["_remove_bug_from_devops"]
    _unlink_bug_from_devops_locally = deps["_unlink_bug_from_devops_locally"]
    _devops_access_state = deps["_devops_access_state"]
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
    show_queue_filters = _sidebar_should_render("admin", "Arbeidskø-filtre")
    if show_queue_filters and _sidebar_render_once("admin_sidebar_work_queue_filters"):
        _render_sidebar_work_queue_filters(prefix="admin", mode="admin")
    elif not show_queue_filters:
        st.session_state["admin_queue_status"] = "all"
        st.session_state["admin_queue_critical_only"] = False
        st.session_state["admin_queue_negative_only"] = False
        st.session_state["admin_queue_stale_only"] = False
        st.session_state["admin_queue_unassigned_only"] = False
    bugs = _apply_sidebar_work_queue_filters(bugs, prefix="admin", mode="admin")

    show_admin_advanced_filters = _sidebar_should_render("admin", "Admin-filtrering")
    if show_admin_advanced_filters and _sidebar_render_once("admin_sidebar_advanced_filters"):
        _render_admin_sidebar_advanced_filters()
    elif not show_admin_advanced_filters:
        st.session_state["admin_created_from"] = ""
        st.session_state["admin_sentiment_filter"] = "all"
        st.session_state["admin_only_unassigned"] = False
        st.session_state["admin_reporter_contains"] = ""
        st.session_state["admin_satisfaction_filter"] = "all"
    bugs = _apply_admin_advanced_filters(bugs)
    if _sidebar_should_render("admin", "Eksport") and _sidebar_render_once("admin_sidebar_export"):
        _render_bug_export_sidebar(prefix="admin", bugs=bugs)
    if _sidebar_should_render("admin", "Arbeidskø") and _sidebar_render_once("admin_sidebar_queue_summary"):
        _render_admin_sidebar_queue_summary(bugs)
    if _sidebar_should_render("admin", "Mulige duplikater") and _sidebar_render_once("admin_sidebar_duplicates"):
        _render_admin_sidebar_duplicates(user, bugs)
    if _sidebar_should_render("admin", "DevOps-innstillinger") and _sidebar_render_once("admin_sidebar_devops_settings"):
        _render_admin_devops_settings_sidebar(user)
    if _sidebar_should_render("admin", "Admin-tilganger") and _sidebar_render_once("admin_sidebar_access_management"):
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

            devops_allowed, devops_reason = _devops_access_state(user)
            devops_work_item_id = int(bug.ado_work_item_id or 0) if bug.ado_work_item_id else 0
            devops_work_item_url = str(bug.ado_work_item_url or "").strip()
            devops_snapshot_key = f"admin_devops_pull_snapshot_{bug.id}"
            devops_snapshot = st.session_state.get(devops_snapshot_key)
            if devops_work_item_id <= 0:
                st.session_state.pop(devops_snapshot_key, None)
                devops_snapshot = None
            devops_status_key = f"admin_status_{bug.id}"
            devops_severity_key = f"admin_severity_{bug.id}"
            devops_assignee_key = f"admin_assignee_{bug.id}"
            devops_tags_key = f"admin_tags_{bug.id}"
            devops_note_key = f"admin_note_{bug.id}"

            status_for_devops = st.session_state.get(devops_status_key, normalize_bug_status(bug.status))
            severity_for_devops = st.session_state.get(devops_severity_key, bug.severity or "medium")
            assignee_for_devops = st.session_state.get(devops_assignee_key, bug.assignee_id or "")
            tags_for_devops = st.session_state.get(devops_tags_key, bug.tags or "")
            note_for_devops = st.session_state.get(devops_note_key, "")

            devops_changed_fields: list[str] = []
            if normalize_bug_status(str(status_for_devops)) != normalize_bug_status(bug.status):
                devops_changed_fields.append("status")
            if str(severity_for_devops or "").strip().casefold() != str(bug.severity or "").strip().casefold():
                devops_changed_fields.append("severity")
            if _normalize_email(str(assignee_for_devops or "")) != _normalize_email(bug.assignee_id):
                devops_changed_fields.append("assignee")

            d1, d2, d3, d4, d5, d6 = st.columns(6)
            with d1:
                send_devops_clicked = st.button(
                    "Send bugen til DevOps",
                    key=f"admin_send_devops_{bug.id}",
                    use_container_width=True,
                    disabled=(not devops_allowed) or bool(devops_work_item_id),
                )
            with d2:
                update_devops_clicked = st.button(
                    "Oppdater i DevOps",
                    key=f"admin_update_devops_{bug.id}",
                    use_container_width=True,
                    disabled=(not devops_allowed) or (not bool(devops_work_item_id)),
                    help="Oppdaterer DevOps med status, alvorlighetsgrad, tildeling og eventuelt arbeidsnotat.",
                )
            with d3:
                fetch_devops_clicked = st.button(
                    "Hent fra DevOps",
                    key=f"admin_fetch_devops_{bug.id}",
                    use_container_width=True,
                    disabled=(not devops_allowed) or (not bool(devops_work_item_id)),
                    help="Henter siste data fra DevOps og viser forskjeller før du eventuelt oppdaterer lokal bug.",
                )
            with d4:
                if devops_work_item_url:
                    st.link_button(
                        "Åpne i DevOps",
                        url=devops_work_item_url,
                        use_container_width=True,
                    )
                elif devops_work_item_id:
                    st.caption(f"DevOps-ID: #{devops_work_item_id}")
            with d5:
                remove_devops_clicked = st.button(
                    "Fjerne fra DevOps",
                    key=f"admin_remove_devops_{bug.id}",
                    use_container_width=True,
                    disabled=(not devops_allowed) or (not bool(devops_work_item_id)),
                    help="Sletter work item i DevOps og fjerner lokal kobling dersom sletting er verifisert.",
                )
            with d6:
                unlink_devops_local_clicked = st.button(
                    "Koble fra lokalt",
                    key=f"admin_unlink_devops_local_{bug.id}",
                    use_container_width=True,
                    disabled=(not devops_allowed) or (not bool(devops_work_item_id)),
                    help="Fjerner kun koblingen i appen. Work item beholdes i DevOps.",
                )

            if devops_work_item_id:
                st.caption(f"Synket mot DevOps work item #{devops_work_item_id}.")
            elif not devops_allowed:
                st.caption(f"DevOps: {devops_reason}")

            if send_devops_clicked:
                devops_error, work_item_url = _send_bug_to_devops(user, bug_id=bug.id)
                if devops_error:
                    st.error(devops_error)
                else:
                    st.session_state.pop(devops_snapshot_key, None)
                    if work_item_url:
                        st.success("Bug sendt til DevOps.")
                    else:
                        st.success("Bug sendt til DevOps.")
                    st.rerun()
            if update_devops_clicked:
                devops_error, work_item_url = _update_bug_in_devops(
                    user,
                    bug_id=bug.id,
                    status=str(status_for_devops or ""),
                    severity=str(severity_for_devops or ""),
                    assignee_id=str(assignee_for_devops or ""),
                    tags=str(tags_for_devops or ""),
                    comment_text=str(note_for_devops or "").strip() or None,
                    changed_fields=devops_changed_fields,
                )
                if devops_error:
                    st.error(devops_error)
                else:
                    st.session_state.pop(devops_snapshot_key, None)
                    if work_item_url:
                        st.success("Bug oppdatert i DevOps.")
                    else:
                        st.success("Bug oppdatert i DevOps.")
                    st.rerun()
            if fetch_devops_clicked:
                devops_error, snapshot = _fetch_bug_from_devops(user, bug_id=bug.id)
                if devops_error:
                    st.error(devops_error)
                else:
                    st.session_state[devops_snapshot_key] = snapshot
                    st.success("Hentet siste data fra DevOps.")
                    st.rerun()
            if remove_devops_clicked:
                _request_delete_confirmation(prefix="admin", item_key=f"devops_remove_{bug.id}")
                st.rerun()
            if _render_delete_confirmation(
                prefix="admin",
                item_key=f"devops_remove_{bug.id}",
                message="Er du sikker på at du vil fjerne denne bugen fra DevOps?",
            ):
                devops_error, devops_notice = _remove_bug_from_devops(user, bug_id=bug.id)
                if devops_error:
                    st.error(devops_error)
                else:
                    st.session_state.pop(devops_snapshot_key, None)
                    notice_text = str(devops_notice or "").strip()
                    if "finnes fortsatt i devops" in notice_text.casefold():
                        st.warning(notice_text)
                    else:
                        st.success(notice_text or "Bug fjernet fra DevOps.")
                    st.rerun()
            if unlink_devops_local_clicked:
                _request_delete_confirmation(prefix="admin", item_key=f"devops_unlink_local_{bug.id}")
                st.rerun()
            if _render_delete_confirmation(
                prefix="admin",
                item_key=f"devops_unlink_local_{bug.id}",
                message="Er du sikker på at du vil koble fra DevOps lokalt (uten sletting i DevOps)?",
            ):
                devops_error = _unlink_bug_from_devops_locally(user, bug_id=bug.id)
                if devops_error:
                    st.error(devops_error)
                else:
                    st.session_state.pop(devops_snapshot_key, None)
                    st.success("Lokal DevOps-kobling fjernet.")
                    st.rerun()

            if isinstance(devops_snapshot, dict):
                pulled_at = str(devops_snapshot.get("pulled_at") or "").strip()
                pulled_caption = pulled_at if pulled_at else "-"
                changes = devops_snapshot.get("changes")
                if not isinstance(changes, list):
                    changes = []
                st.caption(f"DevOps-snapshot hentet: {pulled_caption}")
                if changes:
                    st.info(f"Fant {len(changes)} forskjell(er) mellom lokal bug og DevOps.")
                    for idx, change in enumerate(changes, start=1):
                        field_label = str(change.get("field") or f"Felt {idx}")
                        local_preview = re.sub(r"\s+", " ", str(change.get("local") or "-")).strip()
                        devops_preview = re.sub(r"\s+", " ", str(change.get("devops") or "-")).strip()
                        if len(local_preview) > 120:
                            local_preview = local_preview[:117] + "..."
                        if len(devops_preview) > 120:
                            devops_preview = devops_preview[:117] + "..."
                        st.caption(f"{field_label}: lokal='{local_preview}' | DevOps='{devops_preview}'")
                else:
                    st.caption("Ingen forskjeller funnet mellom lokal bug og DevOps.")

                sync_c1, sync_c2 = st.columns(2)
                with sync_c1:
                    apply_snapshot_clicked = st.button(
                        "Oppdater lokal bug",
                        key=f"admin_apply_devops_snapshot_{bug.id}",
                        use_container_width=True,
                        disabled=(not devops_allowed),
                    )
                with sync_c2:
                    clear_snapshot_clicked = st.button(
                        "Fjern snapshot",
                        key=f"admin_clear_devops_snapshot_{bug.id}",
                        use_container_width=True,
                    )
                if apply_snapshot_clicked:
                    apply_error = _apply_devops_snapshot_to_local_bug(user, bug_id=bug.id, snapshot=devops_snapshot)
                    if apply_error:
                        st.error(apply_error)
                    else:
                        st.session_state.pop(devops_snapshot_key, None)
                        st.success("Lokal bug oppdatert fra DevOps.")
                        st.rerun()
                if clear_snapshot_clicked:
                    st.session_state.pop(devops_snapshot_key, None)
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
