from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth

from app.core.logging import get_logger

logger = get_logger("cloud_test.devops")

ASSIGNABLE_USERS_CACHE_TTL_SECONDS = 300
_assignable_users_cache: dict[str, dict[str, Any]] = {}


@dataclass(frozen=True)
class DevOpsConfig:
    org: str
    project: str
    pat: str
    work_item_type: str = "Task"


def _auth(config: DevOpsConfig) -> HTTPBasicAuth:
    return HTTPBasicAuth("", config.pat)


def _assignable_cache_key(config: DevOpsConfig) -> str:
    org = str(config.org or "").strip().casefold()
    project = str(config.project or "").strip().casefold()
    # Keep PAT out of logs; use only lightweight in-memory keying.
    pat_fingerprint = f"{len(config.pat)}:{abs(hash(config.pat))}"
    return f"{org}:{project}:{pat_fingerprint}"


def _safe_json(response: requests.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _error_message_from_response(response: requests.Response, fallback: str) -> str:
    payload = _safe_json(response)
    if payload:
        message = str(payload.get("message") or payload.get("error") or "").strip()
        if message:
            return message
    raw = str(response.text or "").strip()
    if raw:
        return raw[:500]
    return fallback


def _forbidden_message(response: requests.Response, *, fallback: str) -> str:
    detail = _error_message_from_response(response, fallback).strip()
    if not detail:
        return fallback
    lowered = detail.casefold()
    if "autentisering feilet" in lowered or "invalid" in lowered and "pat" in lowered:
        return "Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps."
    if "sign in" in lowered or "login.microsoftonline.com" in lowered:
        return "Azure DevOps returnerte innloggingsside. Vanligvis ugyldig/utløpt PAT."
    if any(token in lowered for token in ("permission", "not authorized", "access denied", "vs403", "tf401")):
        return f"Azure DevOps avviste forespørselen (403). {detail}"
    return f"Azure DevOps avviste forespørselen (403). {detail}"


def _is_work_item_type_access_error(response: requests.Response) -> bool:
    text = str(response.text or "").casefold()
    if "vs402323" in text:
        return True
    return "work item type" in text and ("does not exist" in text or "do not have permission" in text)


def _is_unknown_assignee_error(response: requests.Response) -> bool:
    response_text = str(response.text or "").casefold()
    return "system.assignedto" in response_text and "unknown identity" in response_text


def _is_tag_permission_error(response: requests.Response) -> bool:
    response_text = str(response.text or "").casefold()
    return "tf401289" in response_text or ("does not have permissions to create tags" in response_text)


def _is_state_update_error(response: requests.Response) -> bool:
    response_text = str(response.text or "").casefold()
    return "system.state" in response_text


def _map_bug_status_to_ado_state(status: str) -> str:
    return {
        "open": "New",
        "in_progress": "Active",
        "resolved": "Resolved",
        "closed": "Closed",
    }.get(str(status or "").strip().casefold(), "New")


def _build_devops_history_note(*, changed_fields: list[str] | None = None, comment_text: str | None = None) -> str | None:
    parts: list[str] = []
    cleaned_fields = [str(item).strip() for item in (changed_fields or []) if str(item).strip()]
    if cleaned_fields:
        parts.append(f"Oppdatert fra CloudTest ({', '.join(sorted(set(cleaned_fields)))}).")
    cleaned_comment = str(comment_text or "").strip()
    if cleaned_comment:
        parts.append(f"Kommentar: {cleaned_comment}")
    if not parts:
        return None
    return "\n".join(parts)


def list_assignable_devops_users(
    config: DevOpsConfig,
    *,
    timeout_seconds: float = 20.0,
    force_refresh: bool = False,
) -> list[dict[str, str]]:
    org = str(config.org or "").strip()
    pat = str(config.pat or "").strip()
    if not org or not pat:
        return []

    cache_key = _assignable_cache_key(config)
    now = datetime.now(timezone.utc)
    if not force_refresh:
        cached_payload = _assignable_users_cache.get(cache_key)
        if isinstance(cached_payload, dict):
            expires_at = cached_payload.get("expires_at")
            users = cached_payload.get("users")
            if isinstance(expires_at, datetime) and expires_at > now and isinstance(users, list):
                return [dict(item) for item in users if isinstance(item, dict)]

    endpoint = f"https://vssps.dev.azure.com/{quote(org, safe='')}/_apis/graph/users"
    continuation_token: str | None = None
    seen_emails: set[str] = set()
    users: list[dict[str, str]] = []

    while True:
        params: dict[str, str] = {
            "api-version": "7.1-preview.1",
            "subjectTypes": "aad,msa",
        }
        if continuation_token:
            params["continuationToken"] = continuation_token

        try:
            response = requests.get(
                endpoint,
                auth=_auth(config),
                timeout=timeout_seconds,
                headers={"Accept": "application/json"},
                params=params,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Nettverksfeil ved henting av DevOps-brukere: {exc.__class__.__name__}: {exc}") from exc

        if response.status_code == 401:
            raise RuntimeError("Autentisering feilet ved henting av DevOps-brukere.")
        if response.status_code == 403:
            raise RuntimeError(
                _forbidden_message(
                    response,
                    fallback="Azure DevOps avviste henting av brukere (403).",
                )
            )
        if response.status_code == 203:
            raise RuntimeError("Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT.")
        if not response.ok:
            raise RuntimeError(_error_message_from_response(response, "Kunne ikke hente tildelbare DevOps-brukere."))

        payload = _safe_json(response) or {}
        values = payload.get("value")
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                email = str(item.get("mailAddress") or item.get("principalName") or "").strip()
                if "@" not in email:
                    continue
                normalized_email = email.casefold()
                if normalized_email in seen_emails:
                    continue
                seen_emails.add(normalized_email)
                full_name = str(item.get("displayName") or email).strip() or email
                users.append({"email": normalized_email, "full_name": full_name})

        continuation_token = str(response.headers.get("X-MS-ContinuationToken") or "").strip() or None
        if not continuation_token:
            break

    users.sort(key=lambda item: (str(item.get("full_name", "")).casefold(), str(item.get("email", "")).casefold()))
    _assignable_users_cache[cache_key] = {
        "expires_at": now + timedelta(seconds=ASSIGNABLE_USERS_CACHE_TTL_SECONDS),
        "users": [dict(item) for item in users],
    }
    logger.info("DevOps assignable user lookup succeeded count=%s", len(users))
    return users


def test_connection(config: DevOpsConfig, *, timeout_seconds: float = 12.0) -> tuple[bool, str]:
    project_path = quote(config.project, safe="")
    url = f"https://dev.azure.com/{quote(config.org, safe='')}/_apis/projects/{project_path}?api-version=7.1-preview.4"
    try:
        response = requests.get(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        return False, f"Nettverksfeil mot Azure DevOps: {exc.__class__.__name__}: {exc}"

    if response.status_code == 200:
        payload = _safe_json(response) or {}
        project_name = str(payload.get("name") or config.project).strip()
        type_names, type_error = list_work_item_type_names(config, timeout_seconds=timeout_seconds)
        requested_type = str(getattr(config, "work_item_type", "") or "auto").strip()
        if type_error:
            return True, (
                f"Tilkobling OK mot prosjekt '{project_name}'. "
                f"Kunne ikke lese work item-typer: {type_error}"
            )
        if not type_names:
            return False, f"Tilkobling OK mot prosjekt '{project_name}', men ingen work item-typer ble funnet."
        selected_type, select_error = resolve_work_item_type(type_names, requested_type=requested_type)
        if select_error:
            return False, select_error
        return True, (
            f"Tilkobling OK mot prosjekt '{project_name}'. "
            f"Work item type brukt: '{selected_type}'."
        )
    if response.status_code == 401:
        return False, "Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps."
    if response.status_code == 403:
        return False, _forbidden_message(
            response,
            fallback="Azure DevOps avviste tilkoblingen (403). Kontroller prosjekt-/API-rettigheter.",
        )
    if response.status_code == 404:
        return False, "Fant ikke org/prosjekt. Kontroller AZURE_DEVOPS_ORG og AZURE_DEVOPS_PROJECT."
    if response.status_code == 203:
        return False, "Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT."
    return False, _error_message_from_response(response, "Azure DevOps svarte med en uventet feil.")


def list_work_item_type_names(config: DevOpsConfig, *, timeout_seconds: float = 12.0) -> tuple[list[str], str | None]:
    encoded_project = quote(config.project, safe="")
    url = (
        f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}"
        "/_apis/wit/workitemtypes?api-version=7.1-preview.2"
    )
    try:
        response = requests.get(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers={"Accept": "application/json"},
        )
    except requests.RequestException as exc:
        return [], f"Nettverksfeil ved henting av work item-typer: {exc.__class__.__name__}: {exc}"

    if response.status_code == 401:
        return [], "Autentisering feilet ved henting av work item-typer."
    if response.status_code == 403:
        return [], _forbidden_message(
            response,
            fallback="Azure DevOps avviste henting av work item-typer (403).",
        )
    if response.status_code == 404:
        return [], "Fant ikke prosjekt ved henting av work item-typer."
    if response.status_code == 203:
        return [], "Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT."
    if response.status_code != 200:
        return [], _error_message_from_response(response, "Kunne ikke hente work item-typer.")

    payload = _safe_json(response) or {}
    values = payload.get("value")
    if not isinstance(values, list):
        return [], "Uventet respons ved henting av work item-typer."
    names: list[str] = []
    for row in values:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if name:
            names.append(name)
    # preserve order, remove duplicates
    deduped = list(dict.fromkeys(names))
    return deduped, None


def resolve_work_item_type(available_type_names: list[str], *, requested_type: str) -> tuple[str, str | None]:
    if not available_type_names:
        return "", "Ingen work item-typer er tilgjengelige i prosjektet."

    by_key = {str(name).strip().casefold(): str(name).strip() for name in available_type_names if str(name).strip()}
    requested = str(requested_type or "auto").strip()
    requested_key = requested.casefold()
    if requested and requested_key not in {"auto", "*"}:
        if requested_key in by_key:
            return by_key[requested_key], None
        available_display = ", ".join(available_type_names[:12])
        return "", (
            f"Work item type '{requested}' finnes ikke i prosjektet. "
            f"Tilgjengelige typer: {available_display}"
        )

    # Match the original, working integration behavior where $Task was used first.
    priority = ["Task", "Issue", "Bug", "User Story", "Product Backlog Item"]
    for candidate in priority:
        found = by_key.get(candidate.casefold())
        if found:
            return found, None
    return available_type_names[0], None


def create_bug_work_item(
    config: DevOpsConfig,
    *,
    title: str,
    description: str,
    severity: str,
    tags: str | None,
    assignee_email: str | None,
    reporter_email: str | None,
    local_bug_id: int,
    work_item_type: str = "Task",
    timeout_seconds: float = 20.0,
) -> tuple[int, str, str]:
    severity_value = {
        "critical": "1 - Critical",
        "high": "2 - High",
        "medium": "3 - Medium",
        "low": "4 - Low",
    }.get(str(severity or "").strip().casefold(), "3 - Medium")

    patch_ops: list[dict[str, Any]] = [
        {"op": "add", "path": "/fields/System.Title", "value": str(title or f"Bug {local_bug_id}").strip()},
        {
            "op": "add",
            "path": "/fields/System.Description",
            "value": str(description or "").strip() or f"Lokal bug-id: {local_bug_id}",
        },
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity_value},
    ]

    normalized_tags = [item.strip() for item in str(tags or "").split(",") if item.strip()]
    normalized_tags.append(f"CloudTest Bug {local_bug_id}")
    patch_ops.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(dict.fromkeys(normalized_tags))})

    if assignee_email:
        patch_ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": str(assignee_email).strip()})
    if reporter_email:
        patch_ops.append(
            {
                "op": "add",
                "path": "/fields/System.History",
                "value": f"Opprettet fra CloudTest av {str(reporter_email).strip()} (lokal bug #{local_bug_id}).",
            }
        )

    available_types, types_error = list_work_item_type_names(config, timeout_seconds=timeout_seconds)
    if types_error:
        raise RuntimeError(types_error)
    selected_work_item_type, type_resolution_error = resolve_work_item_type(
        available_types,
        requested_type=work_item_type,
    )
    if type_resolution_error:
        raise RuntimeError(type_resolution_error)
    by_key = {str(name).strip().casefold(): str(name).strip() for name in available_types if str(name).strip()}
    candidate_types: list[str] = [selected_work_item_type]
    # Keep requested type first, but always allow robust fallback on VS402323-like errors.
    fallback_priority = ["Task", "Issue", "Bug", "User Story", "Product Backlog Item"]
    for item in fallback_priority:
        resolved = by_key.get(item.casefold())
        if resolved and resolved not in candidate_types:
            candidate_types.append(resolved)
    for item in available_types:
        clean = str(item).strip()
        if clean and clean not in candidate_types:
            candidate_types.append(clean)

    encoded_project = quote(config.project, safe="")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json-patch+json",
    }

    def _post(candidate_type: str, payload: list[dict[str, Any]]) -> requests.Response:
        encoded_work_item_type = quote(f"${candidate_type}", safe="$")
        url = (
            f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}"
            f"/_apis/wit/workitems/{encoded_work_item_type}?api-version=7.1"
        )
        return requests.post(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers=headers,
            json=payload,
        )

    last_error: str | None = None
    for candidate_type in candidate_types:
        try:
            response = _post(candidate_type, patch_ops)
        except requests.RequestException as exc:
            raise RuntimeError(f"Nettverksfeil mot Azure DevOps: {exc.__class__.__name__}: {exc}") from exc

        if not response.ok and assignee_email and _is_unknown_assignee_error(response):
            payload_without_assignee = [item for item in patch_ops if item.get("path") != "/fields/System.AssignedTo"]
            response = _post(candidate_type, payload_without_assignee)
            if response.ok:
                patch_payload_used = payload_without_assignee
            else:
                patch_payload_used = patch_ops
        else:
            patch_payload_used = patch_ops

        if not response.ok and tags and _is_tag_permission_error(response):
            payload_without_tags = [item for item in patch_payload_used if item.get("path") != "/fields/System.Tags"]
            response = _post(candidate_type, payload_without_tags)

        if response.ok:
            payload = _safe_json(response)
            if not payload:
                raise RuntimeError("Azure DevOps returnerte ikke gyldig JSON-respons.")
            work_item_id = payload.get("id")
            if not isinstance(work_item_id, int):
                raise RuntimeError("Azure DevOps-respons manglet gyldig work item-id.")

            work_item_url = str(payload.get("url") or "").strip()
            try:
                html_url = str((payload.get("_links") or {}).get("html", {}).get("href") or "").strip()
                if html_url:
                    work_item_url = html_url
            except Exception:
                pass
            if not work_item_url:
                work_item_url = (
                    f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}/"
                    f"_workitems/edit/{work_item_id}"
                )
            return work_item_id, work_item_url, candidate_type

        if response.status_code == 401:
            raise RuntimeError("Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps.")
        if response.status_code == 403:
            raise RuntimeError(
                _forbidden_message(
                    response,
                    fallback="Azure DevOps avviste oppretting av work item (403).",
                )
            )
        if response.status_code == 203:
            raise RuntimeError("Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT.")

        last_error = _error_message_from_response(response, "Azure DevOps svarte med en uventet feil.")
        if _is_work_item_type_access_error(response):
            logger.warning(
                "DevOps work item type '%s' unavailable; trying fallback. detail=%s",
                candidate_type,
                last_error,
            )
            continue
        raise RuntimeError(last_error)

    raise RuntimeError(last_error or "Azure DevOps svarte med en uventet feil.")


def update_bug_work_item(
    config: DevOpsConfig,
    *,
    work_item_id: int,
    title: str,
    description: str,
    severity: str,
    status: str,
    tags: str | None,
    assignee_email: str | None,
    changed_fields: list[str] | None = None,
    comment_text: str | None = None,
    timeout_seconds: float = 20.0,
) -> tuple[int, str]:
    if int(work_item_id) <= 0:
        raise RuntimeError("Ugyldig DevOps work item-id.")

    severity_value = {
        "critical": "1 - Critical",
        "high": "2 - High",
        "medium": "3 - Medium",
        "low": "4 - Low",
    }.get(str(severity or "").strip().casefold(), "3 - Medium")
    state_value = _map_bug_status_to_ado_state(status)
    history_note = _build_devops_history_note(changed_fields=changed_fields, comment_text=comment_text)
    normalized_tags = [item.strip() for item in str(tags or "").split(",") if item.strip()]
    tags_value = "; ".join(dict.fromkeys(normalized_tags))
    assignee_value = str(assignee_email or "").strip()

    encoded_project = quote(config.project, safe="")
    url = (
        f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}"
        f"/_apis/wit/workitems/{int(work_item_id)}?api-version=7.1"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json-patch+json",
    }

    include_assignee = True
    include_tags = True
    include_state = True
    last_error: str | None = None

    for _ in range(4):
        patch_ops: list[dict[str, Any]] = [
            {"op": "add", "path": "/fields/System.Title", "value": str(title or f"Bug {work_item_id}").strip()},
            {
                "op": "add",
                "path": "/fields/System.Description",
                "value": str(description or "").strip() or f"CloudTest bug #{work_item_id}",
            },
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Severity", "value": severity_value},
        ]
        if include_state:
            patch_ops.append({"op": "add", "path": "/fields/System.State", "value": state_value})
        if include_tags:
            patch_ops.append({"op": "add", "path": "/fields/System.Tags", "value": tags_value})
        if include_assignee:
            patch_ops.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assignee_value})
        if history_note:
            patch_ops.append({"op": "add", "path": "/fields/System.History", "value": history_note})

        try:
            response = requests.patch(
                url,
                auth=_auth(config),
                timeout=timeout_seconds,
                headers=headers,
                json=patch_ops,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Nettverksfeil mot Azure DevOps: {exc.__class__.__name__}: {exc}") from exc

        if response.ok:
            payload = _safe_json(response)
            if not payload:
                raise RuntimeError("Azure DevOps returnerte ikke gyldig JSON-respons.")
            returned_id = payload.get("id")
            if not isinstance(returned_id, int):
                raise RuntimeError("Azure DevOps-respons manglet gyldig work item-id.")

            work_item_url = str(payload.get("url") or "").strip()
            try:
                html_url = str((payload.get("_links") or {}).get("html", {}).get("href") or "").strip()
                if html_url:
                    work_item_url = html_url
            except Exception:
                pass
            if not work_item_url:
                work_item_url = (
                    f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}/"
                    f"_workitems/edit/{returned_id}"
                )
            return returned_id, work_item_url

        if response.status_code == 203:
            raise RuntimeError("Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT.")

        retried = False
        if include_assignee and _is_unknown_assignee_error(response):
            include_assignee = False
            retried = True
        if include_tags and _is_tag_permission_error(response):
            include_tags = False
            retried = True
        if include_state and _is_state_update_error(response):
            include_state = False
            retried = True

        if retried:
            last_error = _error_message_from_response(response, "Azure DevOps svarte med en uventet feil.")
            continue

        if response.status_code == 401:
            raise RuntimeError("Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps.")
        if response.status_code == 403:
            raise RuntimeError(
                _forbidden_message(
                    response,
                    fallback="Azure DevOps avviste oppdatering av work item (403).",
                )
            )

        last_error = _error_message_from_response(response, "Azure DevOps svarte med en uventet feil.")
        raise RuntimeError(last_error)

    raise RuntimeError(last_error or "Azure DevOps svarte med en uventet feil.")


def fetch_bug_work_item(
    config: DevOpsConfig,
    *,
    work_item_id: int,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    if int(work_item_id) <= 0:
        raise RuntimeError("Ugyldig DevOps work item-id.")

    encoded_project = quote(config.project, safe="")
    url = (
        f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}"
        f"/_apis/wit/workitems/{int(work_item_id)}?api-version=7.1"
    )
    headers = {"Accept": "application/json"}

    try:
        response = requests.get(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers=headers,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Nettverksfeil mot Azure DevOps: {exc.__class__.__name__}: {exc}") from exc

    if response.status_code == 401:
        raise RuntimeError("Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps.")
    if response.status_code == 403:
        raise RuntimeError(
            _forbidden_message(
                response,
                fallback=f"Azure DevOps avviste lesing av work item #{int(work_item_id)} (403).",
            )
        )
    if response.status_code == 404:
        raise RuntimeError(f"Fant ikke work item #{int(work_item_id)} i Azure DevOps.")
    if response.status_code == 203:
        raise RuntimeError("Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT.")
    if not response.ok:
        raise RuntimeError(_error_message_from_response(response, "Azure DevOps svarte med en uventet feil."))

    payload = _safe_json(response)
    if not payload:
        raise RuntimeError("Azure DevOps returnerte ikke gyldig JSON-respons.")
    returned_id = payload.get("id")
    if not isinstance(returned_id, int):
        raise RuntimeError("Azure DevOps-respons manglet gyldig work item-id.")

    work_item_url = str(payload.get("url") or "").strip()
    try:
        html_url = str((payload.get("_links") or {}).get("html", {}).get("href") or "").strip()
        if html_url:
            work_item_url = html_url
    except Exception:
        pass
    if not work_item_url:
        work_item_url = (
            f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}/"
            f"_workitems/edit/{returned_id}"
        )

    fields = payload.get("fields")
    if not isinstance(fields, dict):
        fields = {}
    return {
        "id": returned_id,
        "url": work_item_url,
        "fields": fields,
    }


def remove_bug_work_item(
    config: DevOpsConfig,
    *,
    work_item_id: int,
    timeout_seconds: float = 20.0,
) -> None:
    if int(work_item_id) <= 0:
        raise RuntimeError("Ugyldig DevOps work item-id.")

    encoded_project = quote(config.project, safe="")
    url = (
        f"https://dev.azure.com/{quote(config.org, safe='')}/{encoded_project}"
        f"/_apis/wit/workitems/{int(work_item_id)}?api-version=7.1"
    )
    headers = {"Accept": "application/json"}

    try:
        response = requests.delete(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers=headers,
            params={"destroy": "false"},
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Nettverksfeil mot Azure DevOps: {exc.__class__.__name__}: {exc}") from exc

    if response.status_code in {404, 410}:
        return
    if response.status_code == 401:
        raise RuntimeError("Autentisering feilet. Kontroller PAT/rettigheter i Azure DevOps.")
    if response.status_code == 403:
        raw = str(response.text or "")
        if "VS403145" in raw:
            raise RuntimeError(
                "VS403145: Mangler rettighet til å slette work item i DevOps "
                "(Delete work items). Bruk lokal frakobling hvis du kun vil fjerne linken i appen."
            )
        raise RuntimeError(
            _forbidden_message(
                response,
                fallback="Azure DevOps avviste sletting av work item (403).",
            )
        )
    if response.status_code == 203:
        raise RuntimeError("Azure DevOps returnerte innloggingsside (203). Vanligvis ugyldig/utløpt PAT.")
    if not response.ok:
        raise RuntimeError(_error_message_from_response(response, "Azure DevOps kunne ikke slette work item."))

    payload = _safe_json(response)
    if isinstance(payload, dict):
        error_code = payload.get("code")
        error_message = str(payload.get("message") or "").strip()
        if error_code not in {None, 0, "0"} or error_message:
            lowered_message = error_message.casefold()
            if "insufficient permissions" in lowered_message or "permission" in lowered_message:
                raise RuntimeError(error_message or "Azure DevOps nektet sletting av work item.")

    try:
        verify_response = requests.get(
            url,
            auth=_auth(config),
            timeout=timeout_seconds,
            headers=headers,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Nettverksfeil ved verifisering av sletting: {exc.__class__.__name__}: {exc}") from exc

    if verify_response.ok:
        raise RuntimeError(
            "Arbeidselementet ser fortsatt ut til å eksistere i Azure DevOps etter sletting. "
            "Lokal kobling blir derfor beholdt."
        )

    verify_text = str(verify_response.text or "").casefold()
    if verify_response.status_code in {404, 410}:
        return
    if (
        "tf401232" in verify_text
        or "does not exist" in verify_text
        or "do not have permissions to read it" in verify_text
    ):
        return

    raise RuntimeError(
        _error_message_from_response(
            verify_response,
            "Kunne ikke verifisere sletting i Azure DevOps. Lokal kobling blir beholdt.",
        )
    )
