from __future__ import annotations

import json
from typing import Any

from app.core.logging import get_logger

logger = get_logger("cloud_test.ai_client")


def extract_json_object(text: str) -> dict | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        return None
    return None


def _create_openai_client(api_key: str):
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None, "OpenAI-biblioteket er ikke tilgjengelig i dette miljøet."
    return OpenAI(api_key=api_key), None


def _call_openai_json(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    operation: str,
) -> tuple[dict | None, str | None, str]:
    client, import_error = _create_openai_client(api_key)
    if import_error:
        return None, import_error, ""

    try:
        response = client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        response_text = getattr(response, "output_text", "") or ""
    except Exception as exc:
        logger.warning("OpenAI call failed for %s: %s", operation, exc)
        return None, f"AI-kall feilet: {exc}", ""

    payload = extract_json_object(response_text)
    if not payload:
        return None, "AI svarte uten gyldig JSON.", response_text
    return payload, None, response_text


def request_reporter_draft(*, raw_text: str, api_key: str, model: str) -> tuple[dict | None, str | None, dict[str, Any]]:
    prompt_text = str(raw_text or "").strip()
    debug_details: dict[str, Any] = {"model": model, "prompt_chars": len(prompt_text)}
    if len(prompt_text) < 10:
        return None, "Skriv litt mer tekst før AI-utkast genereres.", debug_details
    if not api_key:
        return None, "OPENAI_API_KEY mangler. Legg den inn i secrets.", debug_details

    payload, error, response_text = _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "Du er en assistent som lager et bug-utkast. "
            "Svar KUN med gyldig JSON-objekt med feltene: "
            "title, description, severity, category, assignee_email, notify_emails, environment, tags."
        ),
        user_prompt=prompt_text,
        operation="reporter_draft",
    )
    debug_details["response_chars"] = len(response_text)
    if error:
        return None, error, debug_details
    return payload, None, debug_details


def request_assignee_solution(*, context: str, api_key: str, model: str) -> tuple[str, str, str | None]:
    if not api_key:
        return "", "", "OPENAI_API_KEY mangler. Legg den inn i secrets."

    payload, error, _ = _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "Du hjelper en saksbehandler med et kort løsningsforslag til en bug. "
            "Svar KUN med JSON-objekt: suggestion, source. "
            "suggestion skal være på norsk, konkret og maks 30 ord."
        ),
        user_prompt=context,
        operation="assignee_solution",
    )
    if error:
        return "", "", error

    suggestion = str(payload.get("suggestion", "") or "").strip() if isinstance(payload, dict) else ""
    source = str(payload.get("source", "") or "ai+conversation").strip() if isinstance(payload, dict) else "ai+conversation"
    if not suggestion:
        return "", "", "AI returnerte tomt løsningsforslag."
    words = suggestion.split()
    if len(words) > 30:
        suggestion = " ".join(words[:30]).strip()
    return suggestion, source, None


def request_bug_sentiment(*, context: str, api_key: str, model: str) -> tuple[str, str, str | None]:
    if not api_key:
        return "", "", "OPENAI_API_KEY mangler. Legg den inn i secrets."

    payload, error, _ = _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "Du analyserer sentiment i en bugsamtale. "
            "Svar KUN med JSON: label, summary. "
            "label må være en av: positive, neutral, negative. "
            "summary skal være kort (maks 30 ord) på norsk."
        ),
        user_prompt=context,
        operation="bug_sentiment",
    )
    if error:
        return "", "", error

    label = str(payload.get("label", "") or "").strip().casefold() if isinstance(payload, dict) else ""
    summary = str(payload.get("summary", "") or "").strip() if isinstance(payload, dict) else ""
    if label not in {"positive", "neutral", "negative"}:
        label = "neutral"
    words = summary.split()
    if len(words) > 30:
        summary = " ".join(words[:30]).strip()
    return label, summary, None


def request_bug_summary(*, context: str, api_key: str, model: str) -> tuple[str, str | None]:
    if not api_key:
        return "", "OPENAI_API_KEY mangler. Legg den inn i secrets."

    payload, error, _ = _call_openai_json(
        api_key=api_key,
        model=model,
        system_prompt=(
            "Du lager en kort, presis oppsummering av en bug basert på tittel, beskrivelse og samtalehistorikk. "
            "Svar KUN med JSON: summary. summary skal være norsk og maks 50 ord."
        ),
        user_prompt=context,
        operation="bug_summary",
    )
    if error:
        return "", error

    summary = str(payload.get("summary", "") or "").strip() if isinstance(payload, dict) else ""
    if not summary:
        return "", "AI returnerte tom oppsummering."
    words = summary.split()
    if len(words) > 50:
        summary = " ".join(words[:50]).strip()
    return summary, None
