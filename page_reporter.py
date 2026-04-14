from __future__ import annotations

import json
from typing import Any


def render_reporter_page(user: dict[str, str], **deps: Any) -> None:
    st = deps["st"]
    _ensure_reporter_state = deps["_ensure_reporter_state"]
    _load_bugs_for_user_cached = deps["_load_bugs_for_user_cached"]
    _build_assignable_emails = deps["_build_assignable_emails"]
    _extract_text_from_uploaded_files = deps["_extract_text_from_uploaded_files"]
    _openai_reporter_draft = deps["_openai_reporter_draft"]
    _apply_reporter_ai_draft = deps["_apply_reporter_ai_draft"]
    _build_reporter_draft_query = deps["_build_reporter_draft_query"]
    _find_similar_bugs = deps["_find_similar_bugs"]
    _reset_reporter_form_state = deps["_reset_reporter_form_state"]
    _request_reporter_typeahead = deps["_request_reporter_typeahead"]
    _check_reporter_duplicates = deps["_check_reporter_duplicates"]
    _validate_reporter_create_input = deps["_validate_reporter_create_input"]
    _create_bug = deps["_create_bug"]
    _prepare_page_bug_list = deps["_prepare_page_bug_list"]
    render_bug_status_summary = deps["render_bug_status_summary"]
    render_bug_list_controls = deps["render_bug_list_controls"]
    build_bug_expander_title = deps["build_bug_expander_title"]
    _apply_pending_reporter_update_text_clear = deps["_apply_pending_reporter_update_text_clear"]
    _clear_bug_cache = deps["_clear_bug_cache"]
    format_datetime_display = deps["format_datetime_display"]
    status_label = deps["status_label"]
    _render_attachments = deps["_render_attachments"]
    _render_bug_thread = deps["_render_bug_thread"]
    _render_bug_history = deps["_render_bug_history"]
    _prefetch_bug_details = deps["_prefetch_bug_details"]
    _reporter_update_text_key = deps["_reporter_update_text_key"]
    normalize_bug_status = deps["normalize_bug_status"]
    _assignee_select_options = deps["_assignee_select_options"]
    _normalize_email = deps["_normalize_email"]
    _update_bug = deps["_update_bug"]
    _add_comment = deps["_add_comment"]
    _queue_clear_reporter_update_text = deps["_queue_clear_reporter_update_text"]
    _request_delete_confirmation = deps["_request_delete_confirmation"]
    _render_delete_confirmation = deps["_render_delete_confirmation"]
    _delete_bug = deps["_delete_bug"]
    _can_user_delete_bug = deps["_can_user_delete_bug"]
    _can_user_reopen_bug = deps["_can_user_reopen_bug"]
    CATEGORY_OPTIONS = deps["CATEGORY_OPTIONS"]
    SEVERITY_OPTIONS = deps["SEVERITY_OPTIONS"]
    STATUS_OPTIONS = deps["STATUS_OPTIONS"]
    REPORTER_SATISFACTION_OPTIONS = deps["REPORTER_SATISFACTION_OPTIONS"]
    MAX_ATTACHMENTS_PER_UPLOAD = deps["MAX_ATTACHMENTS_PER_UPLOAD"]
    MAX_ATTACHMENT_BYTES = deps["MAX_ATTACHMENT_BYTES"]

    st.subheader("Reporter")
    _ensure_reporter_state()
    all_bugs_for_similarity = _load_bugs_for_user_cached({"email": user["email"], "role": "admin"})
    assignable_emails = _build_assignable_emails()
    st.markdown(
        """
        <style>
        .st-key-reporter_create_submit button {
            background-color: #e5e7eb !important;
            color: #0f172a !important;
            border: 1px solid #cbd5e1 !important;
        }
        .st-key-reporter_create_submit button:hover {
            background-color: #d1d5db !important;
            border-color: #94a3b8 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("AI-hjelp for rapport", expanded=False):
        st.text_area(
            "Fortell hva som skjedde",
            key="reporter_ai_input",
            height=120,
            help="Lim inn råtekst eller notater. AI foreslår utfylling av feltene.",
        )
        ai_uploader_key = f"reporter_ai_attachments_{int(st.session_state.get('reporter_ai_uploader_nonce', 0))}"
        ai_attachments = st.file_uploader(
            "AI-vedlegg (valgfritt)",
            accept_multiple_files=True,
            key=ai_uploader_key,
            help="Støtter tekstfiler og PDF for utdrag til AI-utkast.",
        )
        c1, c2, c3 = st.columns([1, 1, 1.1])
        ai_clicked = c1.button("Bruk AI til å fylle ut felter", use_container_width=True, key="reporter_ai_fill")
        similar_clicked = c2.button("Finn lignende bugs", use_container_width=True, key="reporter_find_similar")
        with c3:
            with st.expander("Avanserte AI-detaljer", expanded=False):
                debug_text = str(st.session_state.get("reporter_ai_debug_details", "") or "").strip()
                extract_summary = str(st.session_state.get("reporter_ai_file_extract_summary", "") or "").strip()
                if debug_text or extract_summary:
                    st.text_area(
                        "AI-debug",
                        value="\n\n".join(
                            part
                            for part in [
                                debug_text,
                                f"Filuttrekk:\n{extract_summary}" if extract_summary else "",
                            ]
                            if part
                        ),
                        height=200,
                        disabled=True,
                        label_visibility="collapsed",
                    )
                else:
                    st.caption("Ingen AI-detaljer tilgjengelig ennå.")

        if ai_clicked:
            source_text = str(st.session_state.get("reporter_ai_input", "")).strip()
            extracted_file_text, extraction_messages = _extract_text_from_uploaded_files(list(ai_attachments or []))
            st.session_state["reporter_ai_file_extract_summary"] = "\n".join(extraction_messages)
            combined_source_text = "\n\n".join(part for part in [source_text, extracted_file_text] if part).strip()
            with st.spinner("Genererer AI-utkast..."):
                payload, error, debug_details = _openai_reporter_draft(combined_source_text)
            st.session_state["reporter_ai_debug_details"] = json.dumps(
                {
                    "model": (debug_details or {}).get("model"),
                    "prompt_chars": (debug_details or {}).get("prompt_chars"),
                    "response_chars": (debug_details or {}).get("response_chars"),
                    "exception": (debug_details or {}).get("exception"),
                },
                ensure_ascii=False,
                indent=2,
            )
            if error:
                st.session_state["reporter_ai_error"] = str(error)
                st.session_state["reporter_ai_status"] = ""
            else:
                st.session_state["reporter_ai_error"] = ""
                st.session_state["reporter_ai_status"] = "AI-utkast brukt i skjema."
                if isinstance(payload, dict):
                    _apply_reporter_ai_draft(payload, allowed_assignees=set(assignable_emails))
            st.rerun()

        if similar_clicked:
            query_text = str(st.session_state.get("reporter_ai_input", "")).strip() or _build_reporter_draft_query()
            matches = _find_similar_bugs(query_text, all_bugs_for_similarity, limit=5)
            st.session_state["reporter_similar_results"] = [
                {"id": match.id, "title": match.title, "status": match.status, "score": score}
                for score, match in matches
            ]
            st.session_state["reporter_similar_query"] = query_text

        if st.session_state.get("reporter_ai_error"):
            st.error(str(st.session_state.get("reporter_ai_error")))
        if st.session_state.get("reporter_ai_status"):
            st.success(str(st.session_state.get("reporter_ai_status")))

        similar_results = st.session_state.get("reporter_similar_results") or []
        if similar_results:
            st.caption(f"Lignende bugs for: {st.session_state.get('reporter_similar_query', '')}")
            for item in similar_results:
                st.write(
                    f"#{item['id']} - {item['title']} [{item['status']}] "
                    f"(likhet {round(float(item['score']) * 100)}%)"
                )

    find_similar_clicked = False
    suggest_description_clicked = False
    insert_suggestion_clicked = False
    clear_fields_clicked = False
    duplicate_check_clicked = False
    submitted = False

    with st.form("create_bug_form"):
        st.text_input(
            "Tittel",
            key="reporter_create_title",
            help="Kort oppsummering av problemet, for eksempel hva som feiler og hvor.",
        )
        uploader_key = f"reporter_new_attachments_{int(st.session_state.get('reporter_uploader_nonce', 0))}"
        st.markdown(
            f"""
            <style>
            .st-key-{uploader_key} [data-testid="stFileUploader"] {{
                min-height: 182px;
            }}
            </style>
            """,
            unsafe_allow_html=True,
        )
        desc_col, upload_col = st.columns([1.9, 1.1])
        with desc_col:
            st.text_area(
                "Beskrivelse",
                key="reporter_create_description",
                height=180,
                help="Beskriv hva som skjedde, hvordan feilen kan gjenskapes, og forventet resultat.",
            )
        with upload_col:
            new_attachments = st.file_uploader(
                "Vedlegg",
                accept_multiple_files=True,
                key=uploader_key,
                help=f"Maks {MAX_ATTACHMENTS_PER_UPLOAD} filer, opptil {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB per fil.",
            )

        if st.session_state.get("reporter_typeahead_error"):
            st.warning(str(st.session_state.get("reporter_typeahead_error")))
        if st.session_state.get("reporter_typeahead_suggestion"):
            source = str(st.session_state.get("reporter_typeahead_source") or "heuristic")
            st.caption(f"Forslag til fortsettelse ({source})")
            st.text_area(
                "Forslag",
                value=str(st.session_state.get("reporter_typeahead_suggestion") or ""),
                disabled=True,
                height=90,
                label_visibility="collapsed",
            )

        action_col_1, action_col_2, action_col_3, action_col_4 = st.columns(4)
        with action_col_1:
            find_similar_clicked = st.form_submit_button("Finn lignende bugs", use_container_width=True)
        with action_col_2:
            suggest_description_clicked = st.form_submit_button("AI-Foreslå fortsettelse", use_container_width=True)
        with action_col_3:
            insert_suggestion_clicked = st.form_submit_button(
                "Sett inn forslag",
                use_container_width=True,
                disabled=not bool(st.session_state.get("reporter_typeahead_suggestion")),
            )
        with action_col_4:
            clear_fields_clicked = st.form_submit_button("Tøm felter", use_container_width=True)

        c1, c2, c_notify = st.columns([1, 1, 1.2])
        with c1:
            st.selectbox("Kategori", CATEGORY_OPTIONS, key="reporter_create_category")
        with c2:
            st.selectbox("Alvorlighetsgrad", SEVERITY_OPTIONS, key="reporter_create_severity")
        with c_notify:
            st.text_input("Varsle e-post(er)", key="reporter_create_notify_emails")

        c3, c4, c5 = st.columns([1.2, 1.2, 1.6])
        with c3:
            create_assignee_options = _assignee_select_options(
                str(st.session_state.get("reporter_create_assignee", "")),
                assignable_emails,
            )
            st.selectbox(
                "Tildel bug til",
                options=create_assignee_options,
                key="reporter_create_assignee",
                format_func=lambda value: value if value else "Ikke tildelt",
            )
        with c4:
            st.text_input("Miljø", key="reporter_create_environment")
        with c5:
            st.text_input("Tagger (kommaseparert)", key="reporter_create_tags")

        create_col, duplicate_col, unique_col = st.columns([1, 1, 2.2])
        with create_col:
            submitted = st.form_submit_button("Opprett bug", use_container_width=True, key="reporter_create_submit")
        with duplicate_col:
            duplicate_check_clicked = st.form_submit_button("Sjekk duplikater", use_container_width=True)
        with unique_col:
            st.checkbox(
                "Denne bugen er forskjellig fra lignende bugs vist over",
                key="reporter_confirm_unique_bug",
                help="Kryss av hvis du har vurdert lignende bugs og fortsatt vil sende inn ny bug.",
            )

    if clear_fields_clicked:
        _reset_reporter_form_state()
        st.rerun()

    if find_similar_clicked:
        query_text = _build_reporter_draft_query()
        matches = _find_similar_bugs(query_text, all_bugs_for_similarity, limit=5)
        st.session_state["reporter_similar_results"] = [
            {"id": match.id, "title": match.title, "status": match.status, "score": score}
            for score, match in matches
        ]
        st.session_state["reporter_similar_query"] = query_text
        st.rerun()

    if suggest_description_clicked:
        suggestion, error, source = _request_reporter_typeahead(all_bugs_for_similarity)
        st.session_state["reporter_typeahead_suggestion"] = suggestion
        st.session_state["reporter_typeahead_error"] = error
        st.session_state["reporter_typeahead_source"] = source
        st.rerun()

    if insert_suggestion_clicked:
        suggestion = str(st.session_state.get("reporter_typeahead_suggestion", "")).strip()
        if suggestion:
            st.session_state["reporter_append_description_pending"] = suggestion
            st.session_state["reporter_typeahead_suggestion"] = ""
            st.session_state["reporter_typeahead_error"] = ""
        st.rerun()

    if duplicate_check_clicked or submitted:
        exact_id, duplicate_candidates = _check_reporter_duplicates(
            title=str(st.session_state.get("reporter_create_title", "")),
            description=str(st.session_state.get("reporter_create_description", "")),
            bugs=all_bugs_for_similarity,
            limit=5,
        )
        st.session_state["reporter_duplicate_exact_id"] = exact_id
        st.session_state["reporter_duplicate_candidates"] = duplicate_candidates
        st.session_state["reporter_duplicate_checked"] = True

    duplicate_exact_id = st.session_state.get("reporter_duplicate_exact_id")
    duplicate_candidates = st.session_state.get("reporter_duplicate_candidates") or []
    if duplicate_exact_id is not None:
        st.error(f"Denne bugen finnes allerede som bug #{duplicate_exact_id}.")
    elif duplicate_candidates:
        st.warning("Mulige duplikater funnet. Vurder disse før innsending:")
        for item in duplicate_candidates:
            st.write(
                f"#{item['id']} - {item['title']} [{item['status']}] "
                f"(likhet {round(float(item['score']) * 100)}%)"
            )
    elif st.session_state.get("reporter_duplicate_checked"):
        st.success("Ingen tydelige duplikater funnet.")

    if submitted:
        validation_error = _validate_reporter_create_input(assignable_emails=assignable_emails)
        if validation_error:
            st.warning(validation_error)
        elif duplicate_exact_id is not None:
            st.warning("Innsending stoppet fordi bugen ser ut som et eksakt duplikat.")
        elif duplicate_candidates and not bool(st.session_state.get("reporter_confirm_unique_bug", False)):
            st.warning("Bekreft avkryssingen om at bugen er unik før innsending.")
        else:
            error = _create_bug(
                user,
                title=str(st.session_state.get("reporter_create_title", "")),
                description=str(st.session_state.get("reporter_create_description", "")),
                severity=str(st.session_state.get("reporter_create_severity", "medium")),
                category=str(st.session_state.get("reporter_create_category", "software")),
                environment=str(st.session_state.get("reporter_create_environment", "")),
                tags=str(st.session_state.get("reporter_create_tags", "")),
                notify_emails=str(st.session_state.get("reporter_create_notify_emails", "")),
                assignee_id=str(st.session_state.get("reporter_create_assignee", "")),
                attachments=list(new_attachments or []),
                allowed_assignees=set(assignable_emails),
            )
            if error:
                st.error(error)
            else:
                st.success("Bug opprettet.")
                _reset_reporter_form_state()
                st.rerun()

    bugs = _prepare_page_bug_list(user=user, prefix="reporter")
    render_bug_status_summary(bugs=bugs, title="Reporter-oversikt")
    visible_count = render_bug_list_controls(prefix="reporter", total_count=len(bugs), default_visible=5)
    st.caption(f"Viser {min(len(bugs), visible_count)} av {len(bugs)} bugs.")
    visible_bugs = bugs[:visible_count]
    _prefetch_bug_details(visible_bugs)
    for bug in visible_bugs:
        header = build_bug_expander_title(bug)
        with st.expander(header, expanded=False):
            _apply_pending_reporter_update_text_clear(bug.id)
            is_resolved = normalize_bug_status(bug.status) == "resolved"
            top_left, top_right = st.columns([3, 1])
            with top_left:
                st.write(bug.description)
            with top_right:
                refresh_bug_clicked = st.button(
                    "Oppdater",
                    key=f"refresh_reporter_bug_{bug.id}",
                    use_container_width=True,
                    help="Laster inn siste versjon av denne bugen.",
                )
                if refresh_bug_clicked:
                    _clear_bug_cache()
                    st.rerun()

            display_reporting_date = format_datetime_display(bug.reporting_date or bug.created_at)
            st.caption(
                f"Rapportør: {bug.reporter_id} | Tildelt: {bug.assignee_id or '-'} | "
                f"Rapportert dato: {display_reporting_date} | Status: {status_label(bug.status)} | "
                f"Alvorlighetsgrad: {bug.severity} | Kategori: {bug.category or '-'} | "
                f"Miljø: {bug.environment or '-'} | Tagger: {bug.tags or '-'} | "
                f"Opprettet: {format_datetime_display(bug.created_at)}"
            )
            if bug.notify_emails:
                st.caption(f"Varsling: {bug.notify_emails}")
            if bug.reporter_satisfaction:
                st.caption(f"Rapportør-tilfredshet: {bug.reporter_satisfaction}")
            _render_attachments(bug, key_prefix=f"reporter_{bug.id}")

            _render_bug_thread(bug, title="Samtale", collapsed=False, dedupe_consecutive=True)
            _render_bug_history(bug, collapsed=True)

            new_comment = st.text_area(
                "Ny oppdatering fra rapportør",
                key=_reporter_update_text_key(bug.id),
                height=90,
                placeholder="Skriv nye endringer, presiseringer eller svar fra rapportør.",
                disabled=is_resolved,
            )
            c1, c2, c2b = st.columns([1, 1, 1.2])
            with c1:
                bug_status = st.selectbox(
                    "Status",
                    STATUS_OPTIONS,
                    index=STATUS_OPTIONS.index(normalize_bug_status(bug.status))
                    if normalize_bug_status(bug.status) in STATUS_OPTIONS
                    else 0,
                    key=f"reporter_status_{bug.id}",
                    format_func=status_label,
                    disabled=is_resolved,
                )
            with c2:
                bug_severity = st.selectbox(
                    "Alvorlighetsgrad",
                    SEVERITY_OPTIONS,
                    index=SEVERITY_OPTIONS.index(bug.severity) if bug.severity in SEVERITY_OPTIONS else 1,
                    key=f"reporter_severity_{bug.id}",
                    disabled=is_resolved,
                )
            with c2b:
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
                    key=f"reporter_satisfaction_{bug.id}",
                    disabled=is_resolved,
                )

            c3, c4 = st.columns(2)
            with c3:
                assignee_options = _assignee_select_options(bug.assignee_id, assignable_emails)
                normalized_current_assignee = _normalize_email(bug.assignee_id)
                bug_assignee = st.selectbox(
                    "Tildel bug til",
                    options=assignee_options,
                    index=assignee_options.index(normalized_current_assignee) if normalized_current_assignee in assignee_options else 0,
                    key=f"reporter_assignee_{bug.id}",
                    format_func=lambda value: value if value else "Ikke tildelt",
                    disabled=is_resolved,
                )
            with c4:
                bug_environment = st.text_input(
                    "Miljø",
                    value=bug.environment or "",
                    key=f"reporter_env_{bug.id}",
                    disabled=is_resolved,
                )

            c5, c6 = st.columns(2)
            with c5:
                bug_tags = st.text_input(
                    "Tagger",
                    value=bug.tags or "",
                    key=f"reporter_tags_{bug.id}",
                    disabled=is_resolved,
                )
            with c6:
                bug_notify = st.text_input(
                    "Varsle e-post(er)",
                    value="",
                    key=f"reporter_notify_{bug.id}",
                    disabled=is_resolved,
                )

            if is_resolved:
                st.info("Denne bugen er løst og kan ikke oppdateres. Sett den tilbake til Åpen for å gjøre endringer.")

            action_c1, action_c2, action_c3 = st.columns(3)
            with action_c1:
                save_clicked = st.button(
                    "Lagre endringer",
                    key=f"reporter_update_{bug.id}",
                    use_container_width=True,
                    disabled=is_resolved,
                )
            with action_c2:
                reopen_clicked = st.button(
                    "Sett tilbake til Åpen",
                    key=f"reporter_reopen_{bug.id}",
                    use_container_width=True,
                    disabled=(not is_resolved) or (not _can_user_reopen_bug(user)),
                )
            with action_c3:
                delete_clicked = st.button(
                    "Slett bug",
                    key=f"reporter_delete_{bug.id}",
                    use_container_width=True,
                    disabled=not _can_user_delete_bug(user),
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
                _request_delete_confirmation(prefix="reporter", item_key=f"bug_{bug.id}")
                st.rerun()
            if _render_delete_confirmation(
                prefix="reporter",
                item_key=f"bug_{bug.id}",
                message="Er du sikker på at du vil slette denne buggen?",
            ):
                delete_error = _delete_bug(user, bug.id)
                if delete_error:
                    st.error(delete_error)
                else:
                    st.success("Bug flyttet til papirkurv.")
                    st.rerun()

            if save_clicked:
                error = _update_bug(
                    user,
                    bug_id=bug.id,
                    status=bug_status,
                    severity=bug_severity,
                    assignee_id=bug_assignee,
                    environment=bug_environment,
                    tags=bug_tags,
                    notify_emails=bug_notify,
                    reporter_satisfaction=None if reporter_satisfaction == "ikke oppgitt" else reporter_satisfaction,
                )
                if error:
                    st.error(error)
                else:
                    if new_comment.strip():
                        c_error = _add_comment(user, bug.id, new_comment)
                        if c_error:
                            st.error(c_error)
                        else:
                            _queue_clear_reporter_update_text(bug.id)
                            st.success("Endringer lagret med oppdatering.")
                    else:
                        _queue_clear_reporter_update_text(bug.id)
                        st.success("Endringer lagret.")
                    st.rerun()
