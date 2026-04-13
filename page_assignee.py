from __future__ import annotations

from typing import Any


def render_assignee_page(user: dict[str, str], **deps: Any) -> None:
    st = deps["st"]
    _prepare_page_bug_list = deps["_prepare_page_bug_list"]
    _render_sidebar_work_queue_filters = deps["_render_sidebar_work_queue_filters"]
    _apply_sidebar_work_queue_filters = deps["_apply_sidebar_work_queue_filters"]
    _prioritize_assignee_bugs = deps["_prioritize_assignee_bugs"]
    _render_assignee_sidebar_queue_summary = deps["_render_assignee_sidebar_queue_summary"]
    _render_assignee_sidebar_duplicates = deps["_render_assignee_sidebar_duplicates"]
    _build_assignable_emails = deps["_build_assignable_emails"]
    render_bug_status_summary = deps["render_bug_status_summary"]
    render_bug_list_controls = deps["render_bug_list_controls"]
    _sentiment_symbol = deps["_sentiment_symbol"]
    _normalize_email = deps["_normalize_email"]
    build_bug_expander_title = deps["build_bug_expander_title"]
    _apply_pending_assignee_note_clear = deps["_apply_pending_assignee_note_clear"]
    _apply_pending_assignee_solution_to_note = deps["_apply_pending_assignee_solution_to_note"]
    _get_tracked_job = deps["_get_tracked_job"]
    _get_background_job = deps["_get_background_job"]
    _clear_tracked_job = deps["_clear_tracked_job"]
    _finalize_background_job = deps["_finalize_background_job"]
    _clear_bug_cache = deps["_clear_bug_cache"]
    format_datetime_display = deps["format_datetime_display"]
    status_label = deps["status_label"]
    _render_bug_thread = deps["_render_bug_thread"]
    _assignee_solution_state_key = deps["_assignee_solution_state_key"]
    STATUS_OPTIONS = deps["STATUS_OPTIONS"]
    normalize_bug_status = deps["normalize_bug_status"]
    SEVERITY_OPTIONS = deps["SEVERITY_OPTIONS"]
    _assignee_select_options = deps["_assignee_select_options"]
    _assignee_note_key = deps["_assignee_note_key"]
    MAX_ATTACHMENTS_PER_UPLOAD = deps["MAX_ATTACHMENTS_PER_UPLOAD"]
    MAX_ATTACHMENT_BYTES = deps["MAX_ATTACHMENT_BYTES"]
    _openai_assignee_solution_suggestion = deps["_openai_assignee_solution_suggestion"]
    _queue_apply_assignee_solution_to_note = deps["_queue_apply_assignee_solution_to_note"]
    _start_background_job = deps["_start_background_job"]
    _run_bug_sentiment_analysis = deps["_run_bug_sentiment_analysis"]
    _wait_for_background_job_completion = deps["_wait_for_background_job_completion"]
    _run_bug_summary = deps["_run_bug_summary"]
    _request_delete_confirmation = deps["_request_delete_confirmation"]
    _render_delete_confirmation = deps["_render_delete_confirmation"]
    _delete_bug = deps["_delete_bug"]
    _update_bug = deps["_update_bug"]
    _upload_attachments_for_bug = deps["_upload_attachments_for_bug"]
    _add_comment = deps["_add_comment"]
    _queue_clear_assignee_note = deps["_queue_clear_assignee_note"]
    _clear_assignee_solution_state = deps["_clear_assignee_solution_state"]
    _render_attachments = deps["_render_attachments"]
    _render_bug_history = deps["_render_bug_history"]
    _prefetch_bug_details = deps["_prefetch_bug_details"]

    st.subheader("Assignee")
    st.markdown(
        """
        <style>
        [class*="st-key-assignee_update_"] button {
            background-color: #e5e7eb !important;
            color: #0f172a !important;
            border: 1px solid #cbd5e1 !important;
        }
        [class*="st-key-assignee_update_"] button:hover {
            background-color: #d1d5db !important;
            border-color: #94a3b8 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    bugs = _prepare_page_bug_list(user=user, prefix="assignee")
    _render_sidebar_work_queue_filters(prefix="assignee", mode="assignee")
    bugs = _apply_sidebar_work_queue_filters(bugs, prefix="assignee", mode="assignee")
    bugs = _prioritize_assignee_bugs(bugs, user_email=user["email"])
    _render_assignee_sidebar_queue_summary(bugs)
    _render_assignee_sidebar_duplicates(user, bugs)
    assignable_emails = _build_assignable_emails()
    st.caption("Du ser alle bugs. Bugs tildelt deg vises øverst og er merket i listen.")
    render_bug_status_summary(bugs=bugs, title="Assignee-oversikt")
    if not bugs:
        st.info("Ingen bugs å vise med gjeldende assignee-filtre.")
        return
    visible_count = render_bug_list_controls(prefix="assignee", total_count=len(bugs), default_visible=10)
    st.caption(f"Viser {min(len(bugs), visible_count)} av {len(bugs)} bugs.")
    visible_bugs = bugs[:visible_count]
    _prefetch_bug_details(visible_bugs)
    for bug in visible_bugs:
        sentiment_badge = _sentiment_symbol(bug.sentiment_label)
        is_mine = _normalize_email(bug.assignee_id) == _normalize_email(user["email"])
        is_resolved = normalize_bug_status(bug.status) == "resolved"
        mine_badge = "      |      Tildelt meg" if is_mine else ""
        sentiment_section = f"      |      Sentiment {sentiment_badge}" if sentiment_badge else "      |      Sentiment -"
        header = f"{build_bug_expander_title(bug)}{mine_badge}{sentiment_section}"
        with st.expander(header, expanded=False):
            _apply_pending_assignee_note_clear(bug.id)
            _apply_pending_assignee_solution_to_note(bug.id)

            for job_key, running_message in (
                ("sentiment", "Sentimentanalyse behandles i bakgrunnen."),
                ("summarize", "Bugoppsummering behandles i bakgrunnen."),
            ):
                tracked = _get_tracked_job("assignee", bug.id, job_key)
                if not tracked:
                    continue
                tracked_job_id = int(tracked.get("job_id", 0) or 0)
                job_payload = _get_background_job(tracked_job_id)
                if job_payload is None:
                    _clear_tracked_job("assignee", bug.id, job_key)
                    continue
                job_status = str(job_payload.get("status") or "unknown")
                if job_status in {"pending", "running"}:
                    st.info(running_message)
                    if st.button(
                        "Oppdater jobbstatus",
                        key=f"assignee_refresh_job_{bug.id}_{job_key}",
                        use_container_width=True,
                    ):
                        st.rerun()
                    continue

                if job_key in {"sentiment", "summarize"}:
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

                _clear_tracked_job("assignee", bug.id, job_key)
                _finalize_background_job(tracked_job_id)
                _clear_bug_cache()
                st.rerun()

            top_left, top_right = st.columns([3, 1])
            with top_left:
                st.write(bug.description)
            with top_right:
                if st.button(
                    "Oppdater",
                    key=f"refresh_assignee_bug_{bug.id}",
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
                f"Tagger: {bug.tags or '-'} | Sentiment: {bug.sentiment_label or '-'}"
            )
            if bug.reporter_satisfaction:
                st.caption(f"Rapportør-tilfredshet: {bug.reporter_satisfaction}")
            if bug.sentiment_summary:
                st.caption(f"Sentimentoppsummering: {bug.sentiment_summary}")
            if bug.bug_summary:
                st.info(f"Oppsummering: {bug.bug_summary}")

            _render_bug_thread(bug, title="Samtale", collapsed=False, dedupe_consecutive=True)

            if is_resolved:
                st.info("Denne bugen er løst og kan ikke oppdateres. Sett den tilbake til Åpen for å gjøre endringer.")

            solution_key = _assignee_solution_state_key(bug.id, "text")
            error_key = _assignee_solution_state_key(bug.id, "error")
            source_key = _assignee_solution_state_key(bug.id, "source")
            suggestion_text = str(st.session_state.get(solution_key, "") or "").strip()
            suggestion_error = str(st.session_state.get(error_key, "") or "").strip()
            suggestion_source = str(st.session_state.get(source_key, "") or "").strip()

            a1, a2, a3, a4 = st.columns(4)
            with a1:
                suggest_solution_clicked = st.button(
                    "AI: Foreslå løsning",
                    key=f"assignee_suggest_solution_{bug.id}",
                    use_container_width=True,
                    help="Bruker AI + samtalehistorikk til å foreslå et kort løsningsnotat.",
                    disabled=is_resolved,
                )
            with a2:
                insert_solution_clicked = st.button(
                    "Sett inn forslag",
                    key=f"assignee_insert_solution_{bug.id}",
                    use_container_width=True,
                    disabled=(not bool(suggestion_text)) or is_resolved,
                    help="Legger AI-forslaget inn i arbeidsnotatet.",
                )
            with a3:
                sentiment_clicked = st.button(
                    "Sentiment - analyse",
                    key=f"assignee_sentiment_{bug.id}",
                    use_container_width=True,
                    help="Analyserer sentiment i samtalen for denne bugen.",
                    disabled=is_resolved,
                )
            with a4:
                summarize_clicked = st.button(
                    "Oppsummer bug",
                    key=f"assignee_summarize_{bug.id}",
                    use_container_width=True,
                    help="Genererer en kort AI-oppsummering av bug og samtale.",
                    disabled=is_resolved,
                )

            if suggestion_error:
                st.warning(suggestion_error)
            if suggestion_text:
                st.caption(f"Løsningsforslag ({suggestion_source or 'AI'})")
                st.text_area(
                    "Løsningsforslag",
                    value=suggestion_text,
                    key=f"assignee_solution_preview_{bug.id}",
                    disabled=True,
                    label_visibility="collapsed",
                    height=90,
                )

            c1, c2 = st.columns(2)
            with c1:
                status = st.selectbox(
                    "Status",
                    STATUS_OPTIONS,
                    index=STATUS_OPTIONS.index(normalize_bug_status(bug.status))
                    if normalize_bug_status(bug.status) in set(STATUS_OPTIONS)
                    else 0,
                    key=f"assignee_status_{bug.id}",
                    format_func=status_label,
                    disabled=is_resolved,
                )
            with c2:
                severity = st.selectbox(
                    "Alvorlighetsgrad",
                    SEVERITY_OPTIONS,
                    index=SEVERITY_OPTIONS.index(bug.severity) if bug.severity in set(SEVERITY_OPTIONS) else 1,
                    key=f"assignee_severity_{bug.id}",
                    disabled=is_resolved,
                )

            c3, c4 = st.columns(2)
            with c3:
                assignee_options = _assignee_select_options(bug.assignee_id, assignable_emails)
                current_assignee = _normalize_email(bug.assignee_id)
                assignee = st.selectbox(
                    "Tildel bug til",
                    options=assignee_options,
                    index=assignee_options.index(current_assignee) if current_assignee in assignee_options else 0,
                    key=f"assignee_owner_{bug.id}",
                    format_func=lambda value: value if value else "Ikke tildelt",
                    disabled=is_resolved,
                )
            with c4:
                environment = st.text_input(
                    "Miljø",
                    value=bug.environment or "",
                    key=f"assignee_env_{bug.id}",
                    disabled=is_resolved,
                )

            c5, c6 = st.columns(2)
            with c5:
                tags = st.text_input(
                    "Tagger",
                    value=bug.tags or "",
                    key=f"assignee_tags_{bug.id}",
                    disabled=is_resolved,
                )
            with c6:
                notify_emails = st.text_input(
                    "Varsle e-post(er)",
                    value="",
                    key=f"assignee_notify_{bug.id}",
                    disabled=is_resolved,
                )

            note = st.text_area(
                "Arbeidsnotater og forslag til løsning",
                key=_assignee_note_key(bug.id),
                height=90,
                disabled=is_resolved,
            )
            new_attachments = st.file_uploader(
                "Last opp vedlegg",
                accept_multiple_files=True,
                key=f"assignee_new_attachments_{bug.id}",
                help=f"Maks {MAX_ATTACHMENTS_PER_UPLOAD} filer, opptil {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB per fil.",
                disabled=is_resolved,
            )

            if suggest_solution_clicked:
                with st.spinner("Genererer løsningsforslag..."):
                    suggestion, source, error = _openai_assignee_solution_suggestion(bug)
                if error:
                    st.session_state[_assignee_solution_state_key(bug.id, "error")] = str(error)
                    st.session_state[_assignee_solution_state_key(bug.id, "text")] = ""
                    st.session_state[_assignee_solution_state_key(bug.id, "source")] = ""
                else:
                    st.session_state[_assignee_solution_state_key(bug.id, "error")] = ""
                    st.session_state[_assignee_solution_state_key(bug.id, "text")] = str(suggestion or "").strip()
                    st.session_state[_assignee_solution_state_key(bug.id, "source")] = str(source or "ai").strip()
                st.rerun()

            if insert_solution_clicked:
                _queue_apply_assignee_solution_to_note(bug.id)
                st.rerun()

            if sentiment_clicked:
                job_id = _start_background_job(
                    prefix="assignee",
                    bug_id=bug.id,
                    job_key="sentiment",
                    job_label="Sentimentanalyse",
                    target=lambda: {"error": _run_bug_sentiment_analysis(user, bug.id)},
                )
                quick_state = _wait_for_background_job_completion(job_id, timeout_seconds=6, poll_seconds=0.5)
                if quick_state == "timeout":
                    st.info("Sentimentanalyse fortsetter i bakgrunnen.")
                st.rerun()

            if summarize_clicked:
                job_id = _start_background_job(
                    prefix="assignee",
                    bug_id=bug.id,
                    job_key="summarize",
                    job_label="Bugoppsummering",
                    target=lambda: {"error": _run_bug_summary(user, bug.id)},
                )
                quick_state = _wait_for_background_job_completion(job_id, timeout_seconds=6, poll_seconds=0.5)
                if quick_state == "timeout":
                    st.info("Bugoppsummering fortsetter i bakgrunnen.")
                st.rerun()

            action_b1, action_b2, action_b3 = st.columns(3)
            with action_b1:
                update_clicked = st.button(
                    "Oppdater bug",
                    key=f"assignee_update_{bug.id}",
                    use_container_width=True,
                    disabled=is_resolved,
                )
            with action_b2:
                reopen_clicked = st.button(
                    "Sett tilbake til Åpen",
                    key=f"assignee_reopen_{bug.id}",
                    use_container_width=True,
                    disabled=not is_resolved,
                )
            with action_b3:
                delete_clicked = st.button(
                    "Slett bug",
                    key=f"assignee_delete_{bug.id}",
                    use_container_width=True,
                )

            if reopen_clicked:
                reopen_error = _update_bug(
                    user,
                    bug_id=bug.id,
                    status="open",
                    severity=bug.severity,
                    assignee_id=bug.assignee_id,
                    environment=bug.environment,
                    tags=bug.tags,
                    notify_emails=bug.notify_emails,
                )
                if reopen_error:
                    st.error(reopen_error)
                else:
                    st.success("Bug satt tilbake til Åpen.")
                    st.rerun()

            if delete_clicked:
                _request_delete_confirmation(prefix="assignee", item_key=f"bug_{bug.id}")
                st.rerun()
            if _render_delete_confirmation(
                prefix="assignee",
                item_key=f"bug_{bug.id}",
                message="Er du sikker på at du vil slette denne buggen?",
            ):
                delete_error = _delete_bug(user, bug.id)
                if delete_error:
                    st.error(delete_error)
                else:
                    st.success("Bug slettet.")
                    st.session_state.pop("assignee_duplicate_candidates", None)
                    st.rerun()

            if update_clicked:
                error = _update_bug(
                    user,
                    bug_id=bug.id,
                    status=status,
                    severity=severity,
                    assignee_id=assignee,
                    environment=environment,
                    tags=tags,
                    notify_emails=notify_emails,
                )
                if error:
                    st.error(error)
                else:
                    upload_errors = _upload_attachments_for_bug(user, bug.id, list(new_attachments or []))
                    if note.strip():
                        c_error = _add_comment(user, bug.id, note.strip())
                        if c_error:
                            st.error(c_error)
                        else:
                            st.success("Bug oppdatert med notat.")
                    else:
                        st.success("Bug oppdatert.")
                    if upload_errors:
                        st.warning("Noen vedlegg kunne ikke lastes opp:")
                        for item in upload_errors:
                            st.write(f"- {item}")
                    _queue_clear_assignee_note(bug.id)
                    _clear_assignee_solution_state(bug.id)
                    st.rerun()

            _render_attachments(bug, key_prefix=f"assignee_{bug.id}")
            _render_bug_history(bug, collapsed=True)
