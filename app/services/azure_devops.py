from datetime import datetime, timedelta, timezone
import base64

import requests

from app.core.config import settings
from app.core.logging import get_logger
from app.models.bug import Bug

logger = get_logger("app.azure_devops")
ASSIGNABLE_USERS_CACHE_TTL = 300
_assignable_users_cache: dict[str, object] = {"expires_at": None, "users": []}


class AzureDevOpsError(Exception):
    pass


def azure_devops_enabled() -> bool:
    return bool(settings.azure_devops_org and settings.azure_devops_project)


def list_assignable_devops_users() -> list[dict[str, str]]:
    if not azure_devops_enabled() or not settings.azure_devops_pat:
        return []

    now = datetime.now(timezone.utc)
    expires_at = _assignable_users_cache.get("expires_at")
    cached_users = _assignable_users_cache.get("users")
    if isinstance(expires_at, datetime) and expires_at > now and isinstance(cached_users, list):
        return [dict(user) for user in cached_users]

    endpoint = f"https://vssps.dev.azure.com/{settings.azure_devops_org}/_apis/graph/users"
    headers = _authorization_headers(None)
    continuation_token: str | None = None
    seen_emails: set[str] = set()
    users: list[dict[str, str]] = []

    while True:
        params = {
            "api-version": "7.1-preview.1",
            "subjectTypes": "aad,msa",
        }
        if continuation_token:
            params["continuationToken"] = continuation_token

        response = requests.get(
            endpoint,
            headers=headers,
            params=params,
            timeout=60,
        )
        if not response.ok:
            logger.warning("Azure DevOps user lookup failed status=%s", response.status_code)
            raise AzureDevOpsError(response.text or "Azure DevOps user lookup failed.")

        payload = response.json()
        for item in payload.get("value", []):
            email = (item.get("mailAddress") or item.get("principalName") or "").strip()
            if not email or "@" not in email:
                continue
            normalized_email = email.casefold()
            if normalized_email in seen_emails:
                continue
            seen_emails.add(normalized_email)
            users.append(
                {
                    "email": email,
                    "full_name": (item.get("displayName") or email).strip(),
                }
            )

        continuation_token = response.headers.get("X-MS-ContinuationToken")
        if not continuation_token:
            break

    users.sort(key=lambda user: (user["full_name"].casefold(), user["email"].casefold()))
    _assignable_users_cache["expires_at"] = now + timedelta(seconds=ASSIGNABLE_USERS_CACHE_TTL)
    _assignable_users_cache["users"] = [dict(user) for user in users]
    logger.info("Azure DevOps user lookup succeeded count=%s", len(users))
    return users


def create_task_from_bug(bug: Bug, access_token: str | None = None) -> dict:
    if not azure_devops_enabled():
        raise AzureDevOpsError("Azure DevOps integration is not configured.")

    endpoint = (
        f"https://dev.azure.com/{settings.azure_devops_org}/"
        f"{settings.azure_devops_project}/_apis/wit/workitems/$Task?api-version=7.1"
    )
    payload = _build_work_item_payload(bug)
    headers = {
        "Content-Type": "application/json-patch+json",
        **_authorization_headers(access_token),
    }
    response = _post_work_item(endpoint, headers, payload)
    if _should_retry_with_pat(response, access_token):
        headers = {
            "Content-Type": "application/json-patch+json",
            **_authorization_headers(None),
        }
        response = _post_work_item(endpoint, headers, payload)
    if not response.ok and bug.assignee_id and _is_unknown_assignee_error(response):
        payload = _build_work_item_payload(bug, include_assignee=False)
        response = _post_work_item(endpoint, headers, payload)
    if not response.ok and bug.tags and _is_tag_permission_error(response):
        payload = _build_work_item_payload(bug, include_tags=False)
        response = _post_work_item(endpoint, headers, payload)
        if not response.ok and bug.assignee_id and _is_unknown_assignee_error(response):
            payload = _build_work_item_payload(bug, include_assignee=False, include_tags=False)
            response = _post_work_item(endpoint, headers, payload)
    if not response.ok:
        logger.warning("Azure DevOps task creation failed bug_id=%s status=%s", bug.id, response.status_code)
        raise AzureDevOpsError(response.text or "Azure DevOps task creation failed.")
    try:
        payload = response.json()
        logger.info("Azure DevOps task created bug_id=%s work_item_id=%s", bug.id, payload.get("id"))
        return payload
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown")
        response_text = response.text.strip()
        short_text = response_text[:500] if response_text else "No response body returned."
        raise AzureDevOpsError(
            f"Azure DevOps returned a non-JSON success response "
            f"(status {response.status_code}, content-type {content_type}): {short_text}"
        ) from exc


def update_task_from_bug(
    bug: Bug,
    access_token: str | None = None,
    *,
    changed_fields: list[str] | None = None,
    comment_text: str | None = None,
) -> dict:
    if not azure_devops_enabled():
        raise AzureDevOpsError("Azure DevOps integration is not configured.")
    if not bug.ado_work_item_id:
        raise AzureDevOpsError("This bug does not have an Azure DevOps work item ID yet.")

    endpoint = (
        f"https://dev.azure.com/{settings.azure_devops_org}/"
        f"{settings.azure_devops_project}/_apis/wit/workitems/{bug.ado_work_item_id}?api-version=7.1"
    )
    headers = {
        "Content-Type": "application/json-patch+json",
        **_authorization_headers(access_token),
    }
    include_assignee = True
    include_tags = True
    include_state = True
    last_response: requests.Response | None = None

    for _ in range(4):
        payload = _build_work_item_payload(
            bug,
            include_assignee=include_assignee,
            include_tags=include_tags,
            include_state=include_state,
            history_note=_build_history_note(changed_fields=changed_fields, comment_text=comment_text),
        )
        response = _patch_work_item(endpoint, headers, payload)
        last_response = response
        if _should_retry_with_pat(response, access_token):
            headers = {
                "Content-Type": "application/json-patch+json",
                **_authorization_headers(None),
            }
            response = _patch_work_item(endpoint, headers, payload)
            last_response = response
        if response.ok:
            try:
                payload_json = response.json()
                logger.info("Azure DevOps task updated bug_id=%s work_item_id=%s", bug.id, bug.ado_work_item_id)
                return payload_json
            except ValueError as exc:
                content_type = response.headers.get("content-type", "unknown")
                response_text = response.text.strip()
                short_text = response_text[:500] if response_text else "No response body returned."
                raise AzureDevOpsError(
                    f"Azure DevOps returned a non-JSON success response "
                    f"(status {response.status_code}, content-type {content_type}): {short_text}"
                ) from exc

        retried = False
        if include_assignee and _is_assignee_update_error(response):
            include_assignee = False
            retried = True
        if include_tags and _is_tag_permission_error(response):
            include_tags = False
            retried = True
        if include_state and _is_state_update_error(response):
            include_state = False
            retried = True
        if not retried:
            break

    raise AzureDevOpsError((last_response.text if last_response else "") or "Azure DevOps task update failed.")


def delete_task_from_bug(bug: Bug, access_token: str | None = None) -> None:
    if not azure_devops_enabled():
        raise AzureDevOpsError("Azure DevOps integration is not configured.")
    if not bug.ado_work_item_id:
        raise AzureDevOpsError("This bug does not have an Azure DevOps work item ID yet.")

    endpoint = (
        f"https://dev.azure.com/{settings.azure_devops_org}/"
        f"{settings.azure_devops_project}/_apis/wit/workitems/{bug.ado_work_item_id}?api-version=7.1"
    )
    headers = {
        **_authorization_headers(access_token),
    }
    response = requests.delete(
        endpoint,
        headers=headers,
        params={"destroy": "false"},
        timeout=60,
    )
    if _should_retry_with_pat(response, access_token):
        headers = {
            **_authorization_headers(None),
        }
        response = requests.delete(
            endpoint,
            headers=headers,
            params={"destroy": "false"},
            timeout=60,
        )
    if response.status_code in {404, 410}:
        logger.info(
            "Azure DevOps task already absent bug_id=%s work_item_id=%s status=%s",
            bug.id,
            bug.ado_work_item_id,
            response.status_code,
        )
        return
    if response.ok:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error_code = payload.get("code")
            error_message = str(payload.get("message") or "").strip()
            if error_code not in {None, 0, "0"} or error_message:
                lowered_message = error_message.casefold()
                if "insufficient permissions" in lowered_message or "permission" in lowered_message:
                    logger.warning(
                        "Azure DevOps task deletion denied bug_id=%s work_item_id=%s message=%s",
                        bug.id,
                        bug.ado_work_item_id,
                        error_message,
                    )
                    raise AzureDevOpsError(error_message or "Azure DevOps denied deletion of the work item.")
    if response.status_code not in {200, 204}:
        logger.warning("Azure DevOps task deletion failed bug_id=%s work_item_id=%s status=%s", bug.id, bug.ado_work_item_id, response.status_code)
        raise AzureDevOpsError(response.text or "Azure DevOps task deletion failed.")
    logger.info("Azure DevOps task deleted bug_id=%s work_item_id=%s", bug.id, bug.ado_work_item_id)


def verify_task_removed(bug: Bug, access_token: str | None = None) -> bool:
    if not azure_devops_enabled():
        raise AzureDevOpsError("Azure DevOps integration is not configured.")
    if not bug.ado_work_item_id:
        raise AzureDevOpsError("This bug does not have an Azure DevOps work item ID yet.")

    endpoint = (
        f"https://dev.azure.com/{settings.azure_devops_org}/"
        f"{settings.azure_devops_project}/_apis/wit/workitems/{bug.ado_work_item_id}?api-version=7.1"
    )
    headers = _authorization_headers(access_token)
    response = requests.get(endpoint, headers=headers, timeout=60)
    if _should_retry_with_pat(response, access_token):
        headers = _authorization_headers(None)
        response = requests.get(endpoint, headers=headers, timeout=60)

    if response.ok:
        logger.warning(
            "Azure DevOps task still exists after deletion bug_id=%s work_item_id=%s",
            bug.id,
            bug.ado_work_item_id,
        )
        return False

    response_text = response.text or ""
    lowered_response = response_text.casefold()
    if (
        "tf401232" in lowered_response
        or "does not exist" in lowered_response
        or "do not have permissions to read it" in lowered_response
    ):
        logger.info(
            "Azure DevOps task removal verified via missing/unreadable work item bug_id=%s work_item_id=%s status=%s",
            bug.id,
            bug.ado_work_item_id,
            response.status_code,
        )
        return True

    logger.warning(
        "Azure DevOps removal verification failed bug_id=%s work_item_id=%s status=%s",
        bug.id,
        bug.ado_work_item_id,
        response.status_code,
    )
    raise AzureDevOpsError(response.text or "Azure DevOps removal could not be verified.")


def _build_work_item_payload(
    bug: Bug,
    *,
    include_assignee: bool = True,
    include_tags: bool = True,
    include_state: bool = False,
    history_note: str | None = None,
) -> list[dict]:
    description_parts = [
        f"<p><strong>Local bug id:</strong> {bug.id}</p>",
        f"<p><strong>Description:</strong><br>{_escape_html(bug.description)}</p>",
    ]
    if bug.repro_steps:
        description_parts.append(f"<p><strong>Reproduction steps:</strong><br>{_escape_html(bug.repro_steps)}</p>")
    if bug.environment:
        description_parts.append(f"<p><strong>Environment:</strong> {_escape_html(bug.environment)}</p>")
    if bug.severity:
        description_parts.append(f"<p><strong>Severity:</strong> {_escape_html(bug.severity)}</p>")
    if bug.reporting_date:
        description_parts.append(
            f"<p><strong>Reporting date:</strong> {bug.reporting_date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>"
        )
    if bug.notify_emails:
        description_parts.append(f"<p><strong>Notify:</strong> {_escape_html(bug.notify_emails)}</p>")
    if bug.assignee_id:
        description_parts.append(f"<p><strong>Requested assignee:</strong> {_escape_html(bug.assignee_id)}</p>")

    payload = [
        {"op": "add", "path": "/fields/System.Title", "value": bug.title},
        {"op": "add", "path": "/fields/System.Description", "value": "".join(description_parts)},
    ]
    if include_tags:
        payload.append({"op": "add", "path": "/fields/System.Tags", "value": (bug.tags or "").replace(",", ";")})
    if include_assignee:
        payload.append({"op": "add", "path": "/fields/System.AssignedTo", "value": bug.assignee_id or ""})
    if include_state:
        payload.append({"op": "add", "path": "/fields/System.State", "value": _map_bug_status_to_ado_state(bug.status)})
    if history_note:
        payload.append({"op": "add", "path": "/fields/System.History", "value": history_note})
    return payload


def apply_ado_sync_to_bug(bug: Bug, work_item: dict, *, sync_status: str = "submitted") -> None:
    bug.ado_work_item_id = work_item.get("id")
    bug.ado_work_item_url = _build_work_item_web_url(work_item.get("id"))
    bug.ado_sync_status = sync_status
    bug.ado_synced_at = datetime.now(timezone.utc)


def clear_ado_sync_from_bug(bug: Bug, *, sync_status: str = "removed") -> None:
    bug.ado_work_item_id = None
    bug.ado_work_item_url = None
    bug.ado_sync_status = sync_status
    bug.ado_synced_at = datetime.now(timezone.utc)


def _authorization_headers(access_token: str | None) -> dict[str, str]:
    if access_token:
        return {"Authorization": f"Bearer {access_token}"}
    if settings.azure_devops_pat:
        token_bytes = f":{settings.azure_devops_pat}".encode("utf-8")
        encoded = base64.b64encode(token_bytes).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}
    raise AzureDevOpsError(
        "Azure DevOps authentication is not configured. Set AZURE_DEVOPS_PAT or re-enable delegated Entra access later."
    )


def _post_work_item(endpoint: str, headers: dict[str, str], payload: list[dict]) -> requests.Response:
    return requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=60,
    )


def _patch_work_item(endpoint: str, headers: dict[str, str], payload: list[dict]) -> requests.Response:
    return requests.patch(
        endpoint,
        headers=headers,
        json=payload,
        timeout=60,
    )


def _should_retry_with_pat(response: requests.Response, access_token: str | None) -> bool:
    if not access_token or not settings.azure_devops_pat:
        return False
    content_type = (response.headers.get("content-type") or "").casefold()
    response_text = (response.text or "").lstrip().casefold()
    is_html_login = "text/html" in content_type and "azure devops services | sign in" in response_text[:500]
    return response.status_code in {203, 401, 302} or is_html_login


def _is_unknown_assignee_error(response: requests.Response) -> bool:
    response_text = response.text.casefold()
    return "system.assignedto" in response_text and "unknown identity" in response_text


def _is_assignee_update_error(response: requests.Response) -> bool:
    response_text = response.text.casefold()
    return "system.assignedto" in response_text


def _is_tag_permission_error(response: requests.Response) -> bool:
    response_text = response.text.casefold()
    return "tf401289" in response_text or (
        "does not have permissions to create tags" in response_text
    )


def _is_state_update_error(response: requests.Response) -> bool:
    response_text = response.text.casefold()
    return "system.state" in response_text


def _map_bug_status_to_ado_state(status: str) -> str:
    return {
        "open": "New",
        "in_progress": "Active",
        "resolved": "Resolved",
        "closed": "Closed",
    }.get(status, "New")


def _build_work_item_web_url(work_item_id: int | None) -> str | None:
    if not work_item_id:
        return None
    return (
        f"https://dev.azure.com/{settings.azure_devops_org}/"
        f"{settings.azure_devops_project}/_workitems/edit/{work_item_id}"
    )


def _build_history_note(
    *,
    changed_fields: list[str] | None = None,
    comment_text: str | None = None,
) -> str | None:
    parts: list[str] = []
    if changed_fields:
        formatted_fields = ", ".join(sorted(set(changed_fields)))
        parts.append(f"<p><strong>Synced from Bug Ticket System.</strong> Updated fields: {_escape_html(formatted_fields)}</p>")
    if comment_text:
        parts.append(f"<p><strong>Comment from Bug Ticket System:</strong><br>{_escape_html(comment_text)}</p>")
    if not parts:
        return None
    return "".join(parts)


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
