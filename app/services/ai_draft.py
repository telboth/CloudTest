import json
import re
from textwrap import shorten
from typing import TypedDict

from app.core.config import settings
from app.services.ai_provider import AIProviderError, generate_text, normalize_ai_provider
from app.schemas.bug import (
    BugAIDraftRequest,
    BugAIDraftResponse,
    BugDescriptionTypeaheadResponse,
)


ALLOWED_CATEGORIES = {"software", "hardware", "process", "documentation"}
ALLOWED_SEVERITIES = {"low", "medium", "high", "critical"}
EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
NOISE_NOTIFY_TOKENS = {
    "none", "n/a", "na", "ukjent", "unknown", "ingen", "ikkje", "ikke",
}
STOPWORD_NAMES = {
    "Begynn", "referansen", "Sporlogg", "ordre", "Order", "Invoice", "Faktura",
}
NORWEGIAN_HINTS = {
    "og", "ikke", "feil", "bruker", "rapport", "sporlogg", "ordre", "faktura",
    "haster", "innlogging", "vedlegg", "miljø", "saksbehandler", "bestiller",
    "meldt", "av", "til", "norsk", "appen", "mangler", "borte", "det",
    "som", "har", "fra", "med", "den", "dette", "skal", "kan", "ikke",
    "også", "ser", "ut", "på", "inn", "for", "må", "trenger", "gjelder",
}
ENGLISH_HINTS = {
    "the", "and", "not", "bug", "issue", "user", "report", "missing", "failed",
    "error", "from", "with", "this", "that", "need", "needs", "app", "browser",
    "steps", "reproduction", "office", "environment",
}
ALLOWED_ENVIRONMENT_LABELS = [
    "Windows",
    "Linux",
    "Frontend",
    "Backend",
    "Hardware",
    "Software",
    "Missing functionality",
    "GIS",
    "Other",
]
ALLOWED_SENTIMENT_LABELS = ["red", "yellow", "green"]
WEAK_TITLE_PATTERNS = {
    "help",
    "need help",
    "i need help",
    "problem",
    "issue",
    "bug",
    "error",
    "does not work",
    "not working",
    "please help",
    "feil",
    "hjelp",
    "trenger hjelp",
    "funker ikke",
    "virker ikke",
    "fungerer ikke",
}


class TitleReviewResult(TypedDict):
    title: str
    was_changed: bool
    reason: str


class SimilarBugContext(TypedDict):
    id: int
    title: str
    description: str
    repro_steps: str | None
    resolution_summary: str | None
    status: str | None


class CommentContext(TypedDict):
    author_role: str
    body: str


def suggest_bug_solution(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    comments: list[CommentContext] | None = None,
    similar_bugs: list[SimilarBugContext] | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> str:
    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            suggestion = _suggest_bug_solution_with_ai(
                title=title,
                description=description,
                repro_steps=repro_steps,
                environment=environment,
                tags=tags,
                comments=comments or [],
                similar_bugs=similar_bugs or [],
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            if suggestion:
                return suggestion
        except Exception:
            pass
    return _suggest_bug_solution_heuristic(
        title=title,
        description=description,
        repro_steps=repro_steps,
        environment=environment,
        tags=tags,
        comments=comments or [],
        similar_bugs=similar_bugs or [],
    )


def _response_language_name(detected_language: str) -> str:
    return "Norwegian Bokmal" if detected_language == "Norwegian" else "English"


def _same_language_instruction(detected_language: str) -> str:
    response_language = _response_language_name(detected_language)
    return (
        f"The input appears to be written in {detected_language}. "
        f"You must return all natural-language output in {response_language}. "
    )


def _localized_text(detected_language: str, norwegian: str, english: str) -> str:
    return norwegian if detected_language == "Norwegian" else english


def build_bug_draft(payload: BugAIDraftRequest) -> BugAIDraftResponse:
    provider = payload.ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) == "openai" and not (payload.openai_api_key or settings.openai_api_key):
        draft = _build_bug_draft_heuristic(payload)
        draft.debug_error = "OpenAI API key is missing."
        draft.assumptions = [
            _localized_text(
                draft.detected_language,
                "Generert med lokal heuristisk reservemodus fordi ingen OpenAI API-nokkel var oppgitt.",
                "Generated with local heuristic fallback because no OpenAI API key was provided.",
            )
        ]
        return draft

    try:
        return _build_bug_draft_with_ai(payload)
    except Exception as exc:
        draft = _build_bug_draft_heuristic(payload)
        draft.debug_error = f"{exc.__class__.__name__}: {exc}"
        draft.assumptions = [
            _localized_text(
                draft.detected_language,
                "Generert med lokal heuristisk reservemodus fordi AI-kallet feilet.",
                "Generated with local heuristic fallback because the AI request failed.",
            )
        ]
        return draft
    draft = _build_bug_draft_heuristic(payload)
    draft.debug_error = _localized_text(draft.detected_language, "Ukjent reservegrunn.", "Unknown fallback reason.")
    return draft


def review_bug_title(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> TitleReviewResult:
    cleaned_title = " ".join(title.split()).strip()
    if not cleaned_title:
        suggested = _heuristic_title_from_bug(
            description=description,
            repro_steps=repro_steps,
            environment=environment,
            tags=tags,
        )
        return {
            "title": suggested,
            "was_changed": True,
            "reason": _localized_text(_detect_input_language(" ".join(part for part in [description, repro_steps or ""] if part)), "Tittelen var tom.", "Title was empty."),
        }

    if not _title_needs_review(cleaned_title, description=description, repro_steps=repro_steps, tags=tags):
        return {
            "title": cleaned_title,
            "was_changed": False,
            "reason": _localized_text(_detect_input_language(" ".join(part for part in [cleaned_title, description, repro_steps or ""] if part)), "Tittelen virker allerede spesifikk nok.", "Title already appears specific enough."),
        }

    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            return _review_bug_title_with_ai(
                title=cleaned_title,
                description=description,
                repro_steps=repro_steps,
                environment=environment,
                tags=tags,
                category=category,
                severity=severity,
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
        except Exception:
            pass

    suggested = _heuristic_title_from_bug(
        description=description,
        repro_steps=repro_steps,
        environment=environment,
        tags=tags,
    )
    normalized_suggested = _normalize_title_candidate(suggested, fallback=cleaned_title)
    was_changed = normalized_suggested != cleaned_title
    return {
        "title": normalized_suggested,
        "was_changed": was_changed,
        "reason": _localized_text(
            _detect_input_language(" ".join(part for part in [cleaned_title, description, repro_steps or ""] if part)),
            "Heuristisk tittelvurdering ble brukt." if was_changed else "Heuristisk vurdering beholdt originaltittelen.",
            "Heuristic title review fallback applied." if was_changed else "Heuristic review kept the original title.",
        ),
    }


def infer_bug_environment(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> str:
    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            inferred = _infer_bug_environment_with_ai(
                title=title,
                description=description,
                repro_steps=repro_steps,
                tags=tags,
                category=category,
                severity=severity,
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            if inferred in ALLOWED_ENVIRONMENT_LABELS:
                return inferred
        except Exception:
            pass
    return _infer_bug_environment_heuristic(
        title=title,
        description=description,
        repro_steps=repro_steps,
        tags=tags,
        category=category,
    )


def infer_bug_severity(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> str:
    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            inferred = _infer_bug_severity_with_ai(
                title=title,
                description=description,
                repro_steps=repro_steps,
                environment=environment,
                tags=tags,
                category=category,
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            if inferred in ALLOWED_SEVERITIES:
                return inferred
        except Exception:
            pass
    return _infer_bug_severity_heuristic(
        title=title,
        description=description,
        repro_steps=repro_steps,
        environment=environment,
        tags=tags,
        category=category,
    )


def analyze_bug_sentiment(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    comments: list[str] | None = None,
    reporter_satisfaction: str | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> tuple[str, str]:
    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            label, summary = _analyze_bug_sentiment_with_ai(
                title=title,
                description=description,
                repro_steps=repro_steps,
                comments=comments or [],
                reporter_satisfaction=reporter_satisfaction,
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            if label in ALLOWED_SENTIMENT_LABELS:
                return label, summary
        except Exception:
            pass
    return _analyze_bug_sentiment_heuristic(
        title=title,
        description=description,
        repro_steps=repro_steps,
        comments=comments or [],
        reporter_satisfaction=reporter_satisfaction,
    )


def summarize_bug(
    *,
    title: str,
    description: str,
    repro_steps: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    status: str | None = None,
    comments: list[str] | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> str:
    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            summary = _summarize_bug_with_ai(
                title=title,
                description=description,
                repro_steps=repro_steps,
                environment=environment,
                tags=tags,
                status=status,
                comments=comments or [],
                ai_provider=ai_provider,
                ai_model=ai_model,
            )
            if summary:
                return summary
        except Exception:
            pass
    return _summarize_bug_heuristic(
        title=title,
        description=description,
        repro_steps=repro_steps,
        environment=environment,
        tags=tags,
        status=status,
        comments=comments or [],
    )


def suggest_bug_description_continuation(
    *,
    title: str | None,
    description: str,
    repro_steps: str | None = None,
    category: str | None = None,
    severity: str | None = None,
    environment: str | None = None,
    tags: str | None = None,
    similar_bugs: list[SimilarBugContext] | None = None,
    ai_provider: str | None = None,
    ai_model: str | None = None,
) -> BugDescriptionTypeaheadResponse:
    cleaned_description = description.strip()
    if not cleaned_description:
        return BugDescriptionTypeaheadResponse(
            suggestion="",
            source="heuristic",
        )

    provider = ai_provider or settings.ai_provider
    if normalize_ai_provider(provider) != "openai" or settings.openai_api_key:
        try:
            suggestion = _suggest_bug_description_continuation_with_ai(
                title=title,
                description=cleaned_description,
                repro_steps=repro_steps,
                category=category,
                severity=severity,
                environment=environment,
                tags=tags,
                similar_bugs=similar_bugs or [],
                ai_provider=ai_provider,
                ai_model=ai_model,
                openai_api_key=None,
            )
            if suggestion:
                return BugDescriptionTypeaheadResponse(
                    suggestion=suggestion,
                    source=normalize_ai_provider(provider),
                )
        except Exception:
            pass

    return BugDescriptionTypeaheadResponse(
        suggestion=_suggest_bug_description_continuation_heuristic(
            title=title,
            description=cleaned_description,
            repro_steps=repro_steps,
            category=category,
            severity=severity,
            environment=environment,
            tags=tags,
            similar_bugs=similar_bugs or [],
        ),
        source="heuristic",
    )


def normalize_notify_targets(value: object) -> str | None:
    return _normalize_email_list(value)


def normalize_tag_list(value: object) -> str | None:
    if value is None:
        return None
    parts = re.split(r"[,\n;]+", str(value))
    cleaned_parts: list[str] = []
    for part in parts:
        candidate = re.sub(r"\s+", " ", part).strip(" .,:;|-")
        if not candidate:
            continue
        cleaned_parts.append(candidate)
    unique_parts = list(dict.fromkeys(cleaned_parts))
    return ", ".join(unique_parts) or None


def _build_bug_draft_with_ai(payload: BugAIDraftRequest) -> BugAIDraftResponse:
    detected_language = _detect_input_language(payload.source_text)
    response_language = _response_language_name(detected_language)
    output_text = generate_text(
        instructions=(
            "You extract structured bug reports from text. "
            "The source text may be in Norwegian or English. "
            f"The source text appears to be written in {detected_language}. "
            f"Required output language: {response_language}. "
            f"You must write every natural-language output field in {response_language}. "
            "This requirement is mandatory for title, description, repro_steps, missing_info, assumptions, and confidence. "
            "Use conservative guesses. If assignee_id is not clearly matched to one of the allowed emails, return null for assignee_id. "
            "Capture order numbers, invoice numbers, PO numbers, faktura numbers, and similar identifiers in tags. "
            "If the text contains person names but no actual email addresses for notification, place the names in notify_emails for now. "
            "If the text is missing important information for a bug report, list the missing fields in missing_info. "
            "If you have to make assumptions to fill in the fields, list the assumptions in assumptions. "
            "Confidence should be low, medium, or high. "
            "Return strict JSON only with keys: title, description, repro_steps, category, severity, assignee_id, notify_emails, environment, tags, missing_info, assumptions, confidence."
        ),
        input_text=(
            "Create a structured bug draft from the text below.\n\n"
            f"Detected source language: {detected_language}\n"
            f"Required output language: {response_language}\n\n"
            f"Allowed assignee emails: {', '.join(str(email) for email in payload.assignable_emails) or 'None'}\n\n"
            f"Source text:\n{payload.source_text}"
        ),
        ai_provider=payload.ai_provider,
        ai_model=payload.ai_model or payload.openai_model,
        openai_api_key=payload.openai_api_key,
    )
    parsed = _parse_json_output(output_text)
    source = normalize_ai_provider(payload.ai_provider or settings.ai_provider)
    return _normalize_draft(parsed, source=source, assignable_emails=[str(email) for email in payload.assignable_emails])


def _review_bug_title_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    category: str | None,
    severity: str | None,
    ai_provider: str | None,
    ai_model: str | None,
) -> TitleReviewResult:
    detected_language = _detect_input_language(" ".join(part for part in [title, description, repro_steps or ""] if part))
    response_language = _response_language_name(detected_language)
    output_text = generate_text(
        instructions=(
            "You review bug titles. "
            "Check whether the provided title is specific and aligned with the rest of the bug report. "
            "Only suggest a different title when the current title is vague, generic, misleading, or inconsistent with the bug details. "
            "Keep the title concise, concrete, and factual. Do not invent facts. "
            + _same_language_instruction(detected_language)
            + f"Write the title and reason in {response_language}. "
            "Return strict JSON only with keys: title_is_good, suggested_title, reason."
        ),
        input_text=(
            f"Current title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Environment: {environment or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Category: {category or 'None'}\n"
            f"Severity: {severity or 'None'}\n"
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    parsed = _parse_json_output(output_text)
    title_is_good = bool(parsed.get("title_is_good"))
    suggested_title = _normalize_title_candidate(parsed.get("suggested_title") or title, fallback=title)
    if title_is_good:
        suggested_title = title
    return {
        "title": suggested_title,
        "was_changed": suggested_title != title,
        "reason": str(parsed.get("reason") or ""),
    }


def _infer_bug_environment_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    tags: str | None,
    category: str | None,
    severity: str | None,
    ai_provider: str | None,
    ai_model: str | None,
) -> str:
    detected_language = _detect_input_language(" ".join(part for part in [title, description, repro_steps or ""] if part))
    output_text = generate_text(
        instructions=(
            "You classify bug reports into exactly one environment label. "
            f"Allowed labels: {', '.join(ALLOWED_ENVIRONMENT_LABELS)}. "
            "Choose the single best label based only on the provided bug context. "
            "Do not invent facts. Return strict JSON only with key: environment."
        ),
        input_text=(
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Category: {category or 'None'}\n"
            f"Severity: {severity or 'None'}\n"
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    parsed = _parse_json_output(output_text)
    return str(parsed["environment"])


def _analyze_bug_sentiment_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    comments: list[str],
    reporter_satisfaction: str | None,
    ai_provider: str | None,
    ai_model: str | None,
) -> tuple[str, str]:
    detected_language = _detect_input_language(" ".join([title, description, repro_steps or "", " ".join(comments)]))
    output_text = generate_text(
        instructions=(
            "You analyze end-user sentiment in bug reports and bug conversations. "
            "Classify the user sentiment as one of: red, yellow, green. "
            "red means clearly frustrated/negative, yellow means mixed/uncertain/neutral, green means positive/satisfied. "
            "Base the decision on the bug text, the user-facing comments, and any satisfaction signal. "
            + _same_language_instruction(detected_language)
            + "Return strict JSON only with keys: sentiment_label, summary."
        ),
        input_text=(
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Reporter satisfaction: {reporter_satisfaction or 'None'}\n"
            f"Conversation:\n" + "\n".join(comments or ["None"])
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    parsed = _parse_json_output(output_text)
    fallback_summary = "Sentiment analysert." if detected_language == "Norwegian" else "Sentiment analyzed."
    return str(parsed["sentiment_label"]), _clean_optional_text(parsed.get("summary")) or fallback_summary


def _infer_bug_severity_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    category: str | None,
    ai_provider: str | None,
    ai_model: str | None,
) -> str:
    detected_language = _detect_input_language(" ".join(part for part in [title, description, repro_steps or "", environment or "", tags or "", category or ""] if part))
    output_text = generate_text(
        instructions=(
            "You classify bug severity into exactly one of: low, medium, high, critical. "
            "Use only the bug context. Do not invent business impact that is not stated. "
            "Return strict JSON only with key: severity."
        ),
        input_text=(
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Environment: {environment or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Category: {category or 'None'}\n"
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    parsed = _parse_json_output(output_text)
    return str(parsed["severity"])


def _summarize_bug_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    status: str | None,
    comments: list[str],
    ai_provider: str | None,
    ai_model: str | None,
) -> str:
    detected_language = _detect_input_language(" ".join([title, description, repro_steps or "", " ".join(comments)]))
    output_text = generate_text(
        instructions=(
            "Summarize the key points of a bug report and its conversation in 2 to 4 short lines. "
            "Focus on what the problem is, key actions or findings, and the current state. "
            "Do not include any dates or timestamps. "
            "Do not include bullet markers. "
            "Keep it concise and factual. "
            + _same_language_instruction(detected_language)
        ),
        input_text=(
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Environment: {environment or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Status: {status or 'None'}\n"
            f"Conversation:\n" + "\n".join(comments or ["None"])
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    return _clean_optional_text(output_text) or ""


def _suggest_bug_description_continuation_with_ai(
    *,
    title: str | None,
    description: str,
    repro_steps: str | None,
    category: str | None,
    severity: str | None,
    environment: str | None,
    tags: str | None,
    similar_bugs: list[SimilarBugContext],
    ai_provider: str | None,
    ai_model: str | None,
    openai_api_key: str | None,
    ) -> str:
    detected_language = _detect_input_language(" ".join(part for part in [title or "", description, repro_steps or ""] if part))
    response_language = _response_language_name(detected_language)
    similar_bug_text = "\n\n".join(
        _format_similar_bug_context_for_prompt(bug)
        for bug in similar_bugs[:3]
    ) or "None"
    output_text = generate_text(
        instructions=(
            "You suggest the next short continuation for a bug description draft using a hybrid approach. "
            + _same_language_instruction(detected_language)
            + f"Write in {response_language}. "
            "Return 1 to 3 short lines that the user can insert directly into the description. "
            "Improve clarity and completeness, but do not invent facts, root causes, or outcomes. "
            "Prefer concise factual continuation such as observed behavior, impact, expected result, or missing details that are implied by the draft. "
            "You may use the similar historical bugs only as inspiration for structure, wording, and likely missing details when they truly fit the current draft. "
            "Do not copy irrelevant details, IDs, names, or outcomes from the similar bugs. "
            "Do not use bullet markers. "
            "Do not repeat the existing text verbatim. "
            "Return plain text only."
        ),
        input_text=(
            f"Title: {title or 'None'}\n"
            f"Description draft: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Category: {category or 'None'}\n"
            f"Severity: {severity or 'None'}\n"
            f"Environment: {environment or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Similar historical bugs:\n{similar_bug_text}\n"
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
        openai_api_key=openai_api_key,
    )
    return _clean_optional_text(output_text) or ""


def _suggest_bug_solution_with_ai(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    comments: list[CommentContext],
    similar_bugs: list[SimilarBugContext],
    ai_provider: str | None,
    ai_model: str | None,
) -> str:
    conversation_text = _conversation_text_for_solution(comments)
    reporter_updates = _recent_reporter_updates_for_solution(comments)
    detected_language = _detect_input_language(
        " ".join(
            part
            for part in [title, description, repro_steps or "", environment or "", tags or "", conversation_text]
            if part
        )
    )
    response_language = _response_language_name(detected_language)
    similar_bug_text = "\n\n".join(_format_similar_bug_context_for_prompt(bug) for bug in similar_bugs) or "None"
    output_text = generate_text(
        instructions=(
            "You help an assignee suggest a likely solution or next investigation step for a bug. "
            + _same_language_instruction(detected_language)
            + f"Write in {response_language}. "
            "Return exactly one short practical solution note that can be inserted directly into internal work notes. "
            "Target about 20 to 30 words. "
            "Use a single sentence or at most two very short sentences. "
            "Prioritize the newest conversation updates over the original bug description when they add new information. "
            "If the reporter has replied with new details after the assignee responded, treat that reporter update as especially important. "
            "Prefer concrete next steps, likely cause, workaround, or verification steps. "
            "Use similar historical bugs as secondary guidance, especially if they are closed or already have a clear resolution summary. "
            "Do not let historical bugs override clear new information from the current conversation. "
            "Do not invent facts. If the historical bugs are only partially relevant, say so and keep the recommendation cautious. "
            "Do not use bullet markers, headings, or long explanations. Return plain text only."
        ),
        input_text=(
            f"Newest reporter updates:\n{reporter_updates}\n\n"
            f"Conversation timeline:\n{conversation_text}\n\n"
            f"Title: {title}\n"
            f"Description: {description}\n"
            f"Reproduction steps: {repro_steps or 'None'}\n"
            f"Environment: {environment or 'None'}\n"
            f"Tags: {tags or 'None'}\n"
            f"Similar historical bugs:\n{similar_bug_text}\n"
        ),
        ai_provider=ai_provider,
        ai_model=ai_model,
    )
    return _clean_optional_text(output_text) or ""


def _suggest_bug_solution_heuristic(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    comments: list[CommentContext],
    similar_bugs: list[SimilarBugContext],
) -> str:
    conversation_text = _conversation_text_for_solution(comments)
    detected_language = _detect_input_language(
        " ".join(part for part in [title, description, repro_steps or "", environment or "", tags or "", conversation_text] if part)
    )
    lines: list[str] = []
    best_match = next(
        (
            bug for bug in similar_bugs
            if (bug.get("resolution_summary") or "").strip()
        ),
        None,
    )
    if best_match and best_match.get("resolution_summary"):
        if detected_language == "Norwegian":
            lines.append(f"Mulig løsning fra lignende sak: {_normalize_summary_line(str(best_match['resolution_summary']))}")
        else:
            lines.append(f"Possible solution from a similar bug: {_normalize_summary_line(str(best_match['resolution_summary']))}")
    else:
        lines.append(
            _localized_text(
                detected_language,
                "Ingen tydelig tidligere løsning funnet; start med en kort teknisk verifisering av siste rapporterte symptom.",
                "No clear historical fix found; start with a short technical verification of the latest reported symptom.",
            )
        )

    latest_reporter_update = _first_meaningful_line(_recent_reporter_updates_for_solution(comments))
    if latest_reporter_update:
        lines.append(
            _localized_text(
                detected_language,
                f"Ta utgangspunkt i ny informasjon fra reporter: {latest_reporter_update}",
                f"Start from the reporter's new information: {latest_reporter_update}",
            )
        )
    else:
        latest_comment = _first_meaningful_line(conversation_text)
        if latest_comment:
            lines.append(
                _localized_text(
                    detected_language,
                    f"Sjekk siste spor i samtalen: {latest_comment}",
                    f"Check the latest conversation clue: {latest_comment}",
                )
            )
    summary = " ".join(" ".join(line.split()).strip() for line in lines if line.strip()).strip()
    if len(summary) > 220:
        summary = shorten(summary, width=220, placeholder="...")
    return summary


def _build_bug_draft_heuristic(payload: BugAIDraftRequest) -> BugAIDraftResponse:
    lines = [line.strip() for line in payload.source_text.splitlines() if line.strip()]
    title = shorten(lines[0], width=80, placeholder="...") if lines else _localized_text(detected_language, "Bugutkast", "Bug report draft")
    description = payload.source_text.strip()
    detected_language = _detect_input_language(payload.source_text)
    repro_lines = [
        line for line in lines
        if line.lower().startswith(("1.", "2.", "3.", "step", "-", "*"))
    ]
    repro_steps = "\n".join(repro_lines) if repro_lines else None
    lower_text = payload.source_text.lower()

    severity = "medium"
    if any(keyword in lower_text for keyword in [
        "crash", "outage", "data loss", "critical",
        "kritisk", "nedetid", "driftsstans", "tap av data",
    ]):
        severity = "critical"
    elif any(keyword in lower_text for keyword in [
        "urgent", "blocked", "high priority", "failure",
        "haster", "blokkert", "hoy prioritet", "høy prioritet", "feil",
    ]):
        severity = "high"
    elif any(keyword in lower_text for keyword in [
        "minor", "cosmetic", "typo",
        "mindre", "kosmetisk", "skrivefeil",
    ]):
        severity = "low"

    category = "software"
    if any(keyword in lower_text for keyword in [
        "printer", "laptop", "screen", "hardware",
        "skriver", "skjerm", "maskinvare",
    ]):
        category = "hardware"
    elif any(keyword in lower_text for keyword in [
        "process", "workflow", "approval",
        "prosess", "arbeidsflyt", "godkjenning",
    ]):
        category = "process"
    elif any(keyword in lower_text for keyword in [
        "manual", "document", "documentation", "guide",
        "dokument", "dokumentasjon", "brukerveiledning", "veiledning",
    ]):
        category = "documentation"

    notify_emails = _extract_notify_targets(payload.source_text)
    environment = _extract_environment(payload.source_text)
    tags = _extract_tags(payload.source_text)

    return BugAIDraftResponse(
        title=title,
        description=description,
        repro_steps=repro_steps,
        category=category,
        severity=severity,
        assignee_id=None,
        notify_emails=notify_emails,
        environment=environment,
        tags=tags,
        missing_info=(["assignee_id"] if not payload.assignable_emails else [])
        if detected_language == "English"
        else (["assignee_id"] if not payload.assignable_emails else []),
        assumptions=[
            _localized_text(
                detected_language,
                "Generert med lokal heuristisk reservemodus fordi en AI-provider ikke var tilgjengelig.",
                "Generated with local heuristic fallback because an AI provider was not available.",
            )
        ],
        confidence="low",
        source="heuristic",
        detected_language=detected_language,
        debug_error=None,
    )


def _normalize_draft(raw: dict, source: str, assignable_emails: list[str]) -> BugAIDraftResponse:
    category = raw.get("category", "software")
    if category not in ALLOWED_CATEGORIES:
        category = "software"

    severity = raw.get("severity", "medium")
    if severity not in ALLOWED_SEVERITIES:
        severity = "medium"

    assignee_id = raw.get("assignee_id")
    if assignee_id not in assignable_emails:
        assignee_id = None

    notify_emails = _normalize_email_list(raw.get("notify_emails"))
    return BugAIDraftResponse(
        title=(raw.get("title") or _localized_text(_detect_input_language(str(raw.get("description") or "")), "Bugutkast", "Bug report draft")).strip(),
        description=(raw.get("description") or "").strip(),
        repro_steps=_clean_optional_text(raw.get("repro_steps")),
        category=category,
        severity=severity,
        assignee_id=assignee_id,
        notify_emails=notify_emails,
        environment=_clean_optional_text(raw.get("environment")),
        tags=_clean_optional_text(raw.get("tags")),
        missing_info=[str(item) for item in raw.get("missing_info", [])],
        assumptions=[str(item) for item in raw.get("assumptions", [])],
        confidence=str(raw.get("confidence") or "medium"),
        source=source,
        detected_language=_detect_input_language(
            " ".join(
                part for part in [
                    str(raw.get("title") or ""),
                    str(raw.get("description") or ""),
                    str(raw.get("repro_steps") or ""),
                ] if part
            )
        ),
        debug_error=None,
    )


def _parse_json_output(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    candidate_starts = [index for index, char in enumerate(cleaned) if char in "{["]
    for start in candidate_starts:
        try:
            parsed, _end = decoder.raw_decode(cleaned[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    preview = cleaned[:100].replace("\n", "\\n").replace("\r", "\\r")
    raise json.JSONDecodeError(
        _localized_text(
            _detect_input_language(cleaned),
            f"Kunne ikke finne et gyldig JSON-objekt i modellsvar. Forhandsvisning: {preview}",
            f"Could not locate a valid JSON object in model output. Preview: {preview}",
        ),
        cleaned,
        0,
    )


def _clean_optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_email_list(value: object) -> str | None:
    if value is None:
        return None
    emails = EMAIL_PATTERN.findall(str(value))
    if emails:
        unique_emails = sorted(dict.fromkeys(email.lower() for email in emails))
        return ", ".join(unique_emails) or None
    parts = re.split(r"[,\n;]+", str(value))
    cleaned_parts: list[str] = []
    for part in parts:
        candidate = re.sub(r"\s+", " ", part).strip(" .,:;|-")
        if not candidate:
            continue
        if candidate.casefold() in NOISE_NOTIFY_TOKENS:
            continue
        cleaned_parts.append(candidate)
    unique_parts = list(dict.fromkeys(cleaned_parts))
    return ", ".join(unique_parts) or None


def _extract_environment(text: str) -> str | None:
    matches = []
    for keyword in [
        "windows", "mac", "linux", "chrome", "edge", "firefox", "ios", "android",
        "iphone", "ipad", "safari",
    ]:
        if keyword in text.lower():
            matches.append(keyword.title())
    return ", ".join(matches) or None


def _extract_tags(text: str) -> str | None:
    tags = []
    for keyword in [
        "login", "ui", "performance", "api", "notification", "attachment", "search",
        "innlogging", "ytelse", "varsling", "vedlegg", "sok", "søk",
    ]:
        if keyword in text.lower():
            tags.append(keyword)
    tags.extend(_extract_reference_tags(text))
    unique_tags = list(dict.fromkeys(tags))
    return ", ".join(unique_tags) or None


def _extract_reference_tags(text: str) -> list[str]:
    patterns = [
        ("order", r"\border(?:\s+number|\s+no\.?|nr\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
        ("ordre", r"\bordre(?:nummer|nr\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
        ("invoice", r"\binvoice(?:\s+number|\s+no\.?|nr\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
        ("faktura", r"\bfaktura(?:nummer|nr\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
        ("po", r"\bpo(?:\s+number|\s+no\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
        ("purchase-order", r"\bpurchase\s+order(?:\s+number|\s+no\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-\/]*)"),
    ]
    found: list[str] = []
    for prefix, pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            value = match.group(1).strip(".,;: ")
            if value:
                found.append(f"{prefix}:{value.upper()}")
    return list(dict.fromkeys(found))


def _extract_notify_targets(text: str) -> str | None:
    emails = sorted(dict.fromkeys(match.group(0).lower() for match in EMAIL_PATTERN.finditer(text)))
    if emails:
        return ", ".join(emails)

    names = _extract_names(text)
    return ", ".join(names) or None


def _extract_names(text: str) -> list[str]:
    names: list[str] = []
    patterns = [
        r"\b(?:contact|notify|attn|attention)\s*[:\-]?\s*([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){0,2})",
        r"\b(?:kontakt|varsle|til|att)\s*[:\-]?\s*([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){0,2})",
        r"\b(?:kontaktperson|bestiller|kunde|saksbehandler|referanseperson|mottaker)\s*[:\-]?\s*([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){0,2})",
        r"\b(?:requested by|reported by|owner)\s*[:\-]?\s*([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){0,2})",
        r"\b(?:bestilt av|meldt av|eier)\s*[:\-]?\s*([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){0,2})",
        r"\bav\s+([A-ZÆØÅ][a-zæøå]+(?:\s+[A-ZÆØÅ][a-zæøå]+){1,2})",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.MULTILINE):
            candidate = _clean_name_candidate(match.group(1))
            if candidate:
                names.append(candidate)

    for line in text.splitlines():
        candidate = _clean_name_candidate(line)
        if candidate:
            names.append(candidate)

    return list(dict.fromkeys(names))


def _clean_name_candidate(value: str) -> str | None:
    candidate = re.sub(r"\s+", " ", value).strip(" .,:;|-")
    if not candidate:
        return None
    if candidate.casefold() in NOISE_NOTIFY_TOKENS:
        return None

    words = candidate.split()
    if len(words) < 2 or len(words) > 3:
        return None
    if any(word in STOPWORD_NAMES for word in words):
        return None
    if not all(re.fullmatch(r"[A-ZÆØÅ][a-zæøå]+", word) for word in words):
        return None
    return candidate


def _detect_input_language(text: str) -> str:
    lower_text = text.casefold()
    norwegian_score = sum(1 for hint in NORWEGIAN_HINTS if f" {hint} " in f" {lower_text} ")
    english_score = sum(1 for hint in ENGLISH_HINTS if f" {hint} " in f" {lower_text} ")
    if any(char in lower_text for char in "æøå"):
        norwegian_score += 2
    if norwegian_score >= max(2, english_score):
        return "Norwegian"
    return "English"


def _title_needs_review(
    title: str,
    *,
    description: str,
    repro_steps: str | None,
    tags: str | None,
) -> bool:
    normalized_title = " ".join(title.split()).casefold().strip(" .,:;!-")
    if normalized_title in WEAK_TITLE_PATTERNS:
        return True
    if len(normalized_title) < 8:
        return True
    if len(normalized_title.split()) <= 2 and normalized_title in {"help", "issue", "problem", "bug", "error", "feil", "hjelp"}:
        return True

    context_text = " ".join(part for part in [description, repro_steps or "", tags or ""] if part).casefold()
    title_tokens = [token for token in re.split(r"\W+", normalized_title) if len(token) > 2]
    if not title_tokens:
        return True
    overlap = sum(1 for token in title_tokens if token in context_text)
    return overlap == 0


def _heuristic_title_from_bug(
    *,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
) -> str:
    candidates = []
    for source_text in [description, repro_steps or ""]:
        for line in source_text.splitlines():
            cleaned = " ".join(line.split()).strip(" .,:;!-")
            if len(cleaned) < 12:
                continue
            candidates.append(cleaned)
    if tags:
        tag_title = ", ".join(tag.strip() for tag in tags.split(",") if tag.strip())
        if tag_title:
            candidates.append(tag_title)
    detected_language = _detect_input_language(" ".join(part for part in [description, repro_steps or "", environment or "", tags or ""] if part))
    if environment:
        candidates.append(_localized_text(detected_language, f"Feil i {environment}", f"Issue in {environment}"))

    fallback_label = _localized_text(detected_language, "Bugrapport", "Bug report")
    fallback = candidates[0] if candidates else description.strip() or fallback_label
    return _normalize_title_candidate(fallback, fallback=fallback_label)


def _normalize_title_candidate(value: str, *, fallback: str) -> str:
    cleaned = " ".join(str(value).split()).strip(" .,:;!-")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 90:
        cleaned = shorten(cleaned, width=90, placeholder="...")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else fallback


def _infer_bug_environment_heuristic(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    tags: str | None,
    category: str | None,
) -> str:
    context = " ".join(part for part in [title, description, repro_steps or "", tags or "", category or ""] if part).casefold()

    if any(keyword in context for keyword in ["gis", "map", "kart", "sporlogg", "feltkultur", "geodata", "arcgis"]):
        return "GIS"
    if any(keyword in context for keyword in ["windows", "win11", "win10", "excel", "outlook", "desktop app"]):
        return "Windows"
    if any(keyword in context for keyword in ["linux", "ubuntu", "debian", "redhat", "bash", "terminal"]):
        return "Linux"
    if any(keyword in context for keyword in ["frontend", "ui", "button", "screen", "page", "browser", "css", "layout"]):
        return "Frontend"
    if any(keyword in context for keyword in ["backend", "api", "server", "database", "sql", "endpoint", "500 error"]):
        return "Backend"
    if any(keyword in context for keyword in ["printer", "screen", "device", "sensor", "keyboard", "mouse", "laptop", "hardware"]):
        return "Hardware"
    if any(keyword in context for keyword in ["missing", "not available", "cannot find", "should have", "would like", "feature request"]):
        return "Missing functionality"
    if category == "hardware":
        return "Hardware"
    if category == "software":
        return "Software"
    return "Other"


def _analyze_bug_sentiment_heuristic(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    comments: list[str],
    reporter_satisfaction: str | None,
) -> tuple[str, str]:
    text = " ".join([title, description, repro_steps or "", " ".join(comments)]).casefold()
    satisfaction = (reporter_satisfaction or "").casefold()

    if satisfaction in {"very satisfied", "satisfied"}:
        return "green", "Rapportoren er fornoyd." if _detect_input_language(text) == "Norwegian" else "Reporter satisfaction is positive."
    if satisfaction in {"dissatisfied", "very dissatisfied"}:
        return "red", "Rapportoren er misfornoyd." if _detect_input_language(text) == "Norwegian" else "Reporter satisfaction is negative."

    red_terms = ["angry", "frustrated", "urgent", "blocked", "broken", "hate", "terrible", "haster", "feil", "stuck"]
    green_terms = ["thanks", "thank you", "solved", "works now", "fixed", "great", "resolved", "takk", "løst"]

    red_hits = sum(1 for term in red_terms if term in text)
    green_hits = sum(1 for term in green_terms if term in text)

    if red_hits > green_hits and red_hits > 0:
        return "red", "Spraket i saken tyder pa frustrasjon eller hast." if _detect_input_language(text) == "Norwegian" else "Language in the report suggests frustration or urgency."
    if green_hits > red_hits and green_hits > 0:
        return "green", "Spraket i saken tyder pa en positiv eller avklart tone." if _detect_input_language(text) == "Norwegian" else "Language in the report suggests a positive or resolved tone."
    return "yellow", "Sentimentet virker blandet eller nøytralt.".replace("ø","o").replace("å","a") if _detect_input_language(text) == "Norwegian" else "Sentiment appears mixed or neutral."


def _infer_bug_severity_heuristic(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    category: str | None,
) -> str:
    lower_text = " ".join(
        part for part in [title, description, repro_steps or "", environment or "", tags or "", category or ""] if part
    ).casefold()

    if any(keyword in lower_text for keyword in [
        "crash", "outage", "data loss", "critical", "security", "payment failure",
        "kritisk", "nedetid", "driftsstans", "tap av data", "sikkerhet",
    ]):
        return "critical"
    if any(keyword in lower_text for keyword in [
        "urgent", "blocked", "high priority", "failure", "cannot login", "blank page", "500 error",
        "haster", "blokkert", "høy prioritet", "hoy prioritet", "feil", "innlogging feiler",
    ]):
        return "high"
    if any(keyword in lower_text for keyword in [
        "minor", "cosmetic", "typo", "small issue",
        "mindre", "kosmetisk", "skrivefeil",
    ]):
        return "low"
    return "medium"


def _summarize_bug_heuristic(
    *,
    title: str,
    description: str,
    repro_steps: str | None,
    environment: str | None,
    tags: str | None,
    status: str | None,
    comments: list[str],
) -> str:
    summary_lines: list[str] = []
    summary_lines.append(_normalize_summary_line(title))

    description_line = _first_meaningful_line(description)
    if description_line:
        summary_lines.append(description_line)

    detected_language = _detect_input_language(" ".join([title, description, " ".join(comments), environment or "", tags or "", status or ""]))

    if comments:
        latest_comment = _first_meaningful_line(comments[-1])
        if latest_comment:
            summary_lines.append(_localized_text(detected_language, f"Siste oppdatering: {latest_comment}", f"Latest update: {latest_comment}"))

    state_bits: list[str] = []
    if environment:
        state_bits.append(_localized_text(detected_language, f"Miljo: {environment}", f"Environment: {environment}"))
    if tags:
        state_bits.append(_localized_text(detected_language, f"Tagger: {tags}", f"Tags: {tags}"))
    if status:
        state_bits.append(_localized_text(detected_language, f"Status: {status}", f"Status: {status}"))
    if state_bits:
        summary_lines.append(" | ".join(state_bits))

    deduped: list[str] = []
    seen: set[str] = set()
    for line in summary_lines:
        normalized = line.casefold()
        if not line or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(line)
    return "\n".join(deduped[:4])


def _suggest_bug_description_continuation_heuristic(
    *,
    title: str | None,
    description: str,
    repro_steps: str | None,
    category: str | None,
    severity: str | None,
    environment: str | None,
    tags: str | None,
    similar_bugs: list[SimilarBugContext],
) -> str:
    lower_text = " ".join(
        part for part in [title or "", description, repro_steps or "", category or "", severity or "", environment or "", tags or ""] if part
    ).casefold()
    lines: list[str] = []
    detected_language = _detect_input_language(description)

    if not any(term in lower_text for term in ["expected", "forvent", "should", "skulle", "skal"]):
        if detected_language == "Norwegian":
            lines.append("Forventet resultat: beskriv kort hva som skulle skjedd.")
        else:
            lines.append("Expected result: briefly describe what should have happened.")

    if not any(term in lower_text for term in ["actual", "faktisk", "instead", "viser", "shows", "error", "feil"]):
        if detected_language == "Norwegian":
            lines.append("Faktisk resultat: beskriv hva som skjer i stedet.")
        else:
            lines.append("Actual result: describe what happens instead.")

    if not environment:
        if detected_language == "Norwegian":
            lines.append("Miljo: legg til relevant app, nettleser eller plattform hvis kjent.")
        else:
            lines.append("Environment: add the relevant app, browser, or platform if known.")

    if similar_bugs and not lines:
        pattern_line = _build_heuristic_similarity_line(similar_bugs[0], language=detected_language)
        if pattern_line:
            lines.append(pattern_line)

    if not lines:
        if detected_language == "Norwegian":
            lines.append("Kort konsekvens: beskriv hvem som blir paavirket og hva som blir blokkert.")
        else:
            lines.append("Impact: briefly describe who is affected and what is blocked.")

    return "\n".join(lines[:3])


def _format_similar_bug_context_for_prompt(bug: SimilarBugContext) -> str:
    parts = [
        f"Bug #{bug['id']}",
        f"Title: {bug['title']}",
        f"Description excerpt: {_normalize_summary_line(bug['description'])}",
    ]
    if bug.get("repro_steps"):
        parts.append(f"Reproduction excerpt: {_normalize_summary_line(str(bug['repro_steps']))}")
    if bug.get("resolution_summary"):
        parts.append(f"Resolution summary: {_normalize_summary_line(str(bug['resolution_summary']))}")
    if bug.get("status"):
        parts.append(f"Status: {bug['status']}")
    return "\n".join(parts)


def _conversation_text_for_solution(comments: list[CommentContext]) -> str:
    lines: list[str] = []
    for comment in comments[-6:]:
        body = _normalize_summary_line(comment.get("body") or "")
        if not body:
            continue
        role = str(comment.get("author_role") or "unknown").strip().lower()
        if role == "reporter":
            prefix = "Reporter"
        elif role == "assignee":
            prefix = "Assignee"
        elif role == "admin":
            prefix = "Admin"
        else:
            prefix = "Bruker"
        lines.append(f"{prefix}: {body}")
    return "\n".join(lines) or "None"


def _recent_reporter_updates_for_solution(comments: list[CommentContext]) -> str:
    reporter_lines: list[str] = []
    assignee_seen = False
    for comment in reversed(comments):
        role = str(comment.get("author_role") or "").strip().lower()
        body = _normalize_summary_line(comment.get("body") or "")
        if not body:
            continue
        if role == "assignee":
            assignee_seen = True
            continue
        if role == "reporter":
            reporter_lines.append(body)
            if assignee_seen:
                break
    reporter_lines.reverse()
    return "\n".join(reporter_lines) or "None"


def _build_heuristic_similarity_line(bug: SimilarBugContext, *, language: str) -> str | None:
    if language == "Norwegian":
        return f"Lignende saker pleier aa beskrive konkret symptom og konsekvens, for eksempel rundt: {_normalize_summary_line(bug['title']).casefold()}."
    return f"Similar bugs usually describe the concrete symptom and impact, for example around: {_normalize_summary_line(bug['title']).casefold()}."


def _first_meaningful_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        cleaned = _normalize_summary_line(line)
        if len(cleaned) >= 12:
            return cleaned
    cleaned = _normalize_summary_line(text)
    return cleaned or None


def _normalize_summary_line(text: str) -> str:
    cleaned = " ".join(str(text).split()).strip(" .,:;!-")
    if len(cleaned) > 140:
        cleaned = shorten(cleaned, width=140, placeholder="...")
    return cleaned
