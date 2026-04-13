from urllib.parse import urlencode
from urllib.parse import urlparse
import secrets
import time
import os
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.logging import get_logger
from app.core.security import create_access_token, create_signed_state_token, decode_signed_state_token, verify_password
from app.models.user import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.services.config_validation import validate_runtime_config

try:
    import msal
except ImportError:  # pragma: no cover - optional dependency at runtime
    msal = None  # type: ignore[assignment]

router = APIRouter()
logger = get_logger("app.auth")
ENTRA_AUTH_PLACEHOLDER = "__ENTRA_AUTH__"
ENTRA_LOGIN_SCOPES: list[str] = []
ENTRA_ACCESS_TOKENS: dict[str, str] = {}
ENTRA_LOGIN_TICKETS: dict[str, dict[str, object]] = {}
ENTRA_LOGIN_TICKET_TTL_SECONDS = 300
ENTRA_PENDING_APP_LOGINS: dict[str, dict[str, object]] = {}
ENTRA_PENDING_APP_LOGIN_TTL_SECONDS = 300
LOCAL_PENDING_LOGIN_DIR = Path(__file__).resolve().parents[2] / "CloudTest" / ".runtime" / "auth_pending"


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.get(User, str(payload.email))
    if not user or not verify_password(payload.password, user.password_hash):
        logger.warning("Local login failed email=%s", payload.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    token = create_access_token(subject=user.email, role=user.role)
    logger.info("Local login succeeded email=%s role=%s", user.email, user.role)
    return TokenResponse(access_token=token, email=user.email, role=user.role)


@router.get("/entra/status")
def entra_status() -> dict[str, object]:
    config_checks = validate_runtime_config()["checks"]
    entra_check = config_checks.get("entra", {})
    url_check = config_checks.get("urls", {})

    warnings: list[str] = []
    if isinstance(entra_check, dict) and entra_check.get("status") != "ok":
        warnings.append(str(entra_check.get("detail") or "Entra-konfigurasjonen er ikke komplett."))
    if isinstance(url_check, dict) and url_check.get("status") != "ok":
        warnings.append(str(url_check.get("detail") or "URL-oppsettet for auth ser ikke riktig ut."))

    return {
        "enabled": settings.entra_enabled,
        "config_status": entra_check.get("status", "unknown") if isinstance(entra_check, dict) else "unknown",
        "detail": entra_check.get("detail", "") if isinstance(entra_check, dict) else "",
        "redirect_uri": settings.entra_redirect_uri,
        "streamlit_urls": {
            "reporter": settings.streamlit_reporter_url,
            "assignee": settings.streamlit_assignee_url,
            "admin": settings.streamlit_admin_url,
        },
        "warnings": warnings,
    }


def _role_for_email(email: str) -> str:
    normalized = email.casefold()
    if normalized in settings.entra_admin_email_list:
        return "admin"
    if normalized in settings.entra_assignee_email_list:
        return "assignee"
    return "reporter"


def _msal_app() -> object:
    if not settings.entra_enabled:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Entra login is not configured")
    if msal is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MSAL is not installed")
    authority = f"https://login.microsoftonline.com/{settings.entra_tenant_id}"
    return msal.ConfidentialClientApplication(
        client_id=settings.entra_client_id,
        client_credential=settings.entra_client_secret,
        authority=authority,
    )


def _resolve_callback_app_name(state: str | None) -> str:
    if not state:
        return "reporter"
    decoded_state = decode_signed_state_token(state)
    if not decoded_state or "app" not in decoded_state:
        return "reporter"
    app_name = decoded_state["app"]
    return app_name if app_name in {"reporter", "assignee", "admin"} else "reporter"


def _cleanup_entra_login_tickets() -> None:
    now = time.time()
    expired_keys = [
        ticket
        for ticket, payload in ENTRA_LOGIN_TICKETS.items()
        if float(payload.get("expires_at", 0)) <= now
    ]
    for ticket in expired_keys:
        ENTRA_LOGIN_TICKETS.pop(ticket, None)


def _create_entra_login_ticket(*, access_token: str, email: str, role: str) -> str:
    _cleanup_entra_login_tickets()
    ticket = secrets.token_urlsafe(32)
    ENTRA_LOGIN_TICKETS[ticket] = {
        "access_token": access_token,
        "email": email,
        "role": role,
        "expires_at": time.time() + ENTRA_LOGIN_TICKET_TTL_SECONDS,
    }
    return ticket


def _cleanup_pending_app_logins() -> None:
    now = time.time()
    expired_apps = [
        app_name
        for app_name, payload in ENTRA_PENDING_APP_LOGINS.items()
        if float(payload.get("expires_at", 0)) <= now
    ]
    for app_name in expired_apps:
        ENTRA_PENDING_APP_LOGINS.pop(app_name, None)


def _store_pending_app_login(*, app_name: str, access_token: str, email: str, role: str) -> None:
    _cleanup_pending_app_logins()
    ENTRA_PENDING_APP_LOGINS[app_name] = {
        "access_token": access_token,
        "email": email,
        "role": role,
        "expires_at": time.time() + ENTRA_PENDING_APP_LOGIN_TTL_SECONDS,
    }


def _store_pending_app_login_file(*, app_name: str, access_token: str, email: str, role: str) -> None:
    try:
        LOCAL_PENDING_LOGIN_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "access_token": access_token,
            "email": email,
            "role": role,
            "expires_at": time.time() + ENTRA_PENDING_APP_LOGIN_TTL_SECONDS,
        }
        app_file_path = LOCAL_PENDING_LOGIN_DIR / f"{app_name}.json"
        shared_file_path = LOCAL_PENDING_LOGIN_DIR / "shared.json"
        serialized = json.dumps(payload)
        app_file_path.write_text(serialized, encoding="utf-8")
        shared_file_path.write_text(serialized, encoding="utf-8")
    except Exception:
        logger.exception("Failed writing pending login file app=%s", app_name)


@router.get("/entra/exchange-ticket", response_model=TokenResponse)
def entra_exchange_ticket(ticket: str = Query(..., min_length=8)) -> TokenResponse:
    _cleanup_entra_login_tickets()
    payload = ENTRA_LOGIN_TICKETS.pop(ticket, None)
    if not payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Innloggingsticket er ugyldig eller utløpt.")
    return TokenResponse(
        access_token=str(payload["access_token"]),
        email=str(payload["email"]),
        role=str(payload["role"]),
    )


@router.get("/entra/pending", response_model=TokenResponse)
def entra_consume_pending_login(app: str = Query(default="reporter")) -> TokenResponse:
    app_name = app if app in {"reporter", "assignee", "admin"} else "reporter"
    _cleanup_pending_app_logins()
    payload = ENTRA_PENDING_APP_LOGINS.pop(app_name, None)
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ingen ventende Entra-innlogging for appen.")
    return TokenResponse(
        access_token=str(payload["access_token"]),
        email=str(payload["email"]),
        role=str(payload["role"]),
    )


def _auth_error_response(*, title: str, message: str, app_name: str = "reporter", status_code: int = 400) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <html>
        <body style="font-family:Segoe UI, Arial, sans-serif; padding:32px; max-width:720px; margin:auto;">
            <h2>{title}</h2>
            <p>{message}</p>
            <p><strong>Tilbake til app:</strong> <a href="{settings.streamlit_url_for_app(app_name)}">{settings.streamlit_url_for_app(app_name)}</a></p>
            <p>Hvis problemet fortsetter, kontroller Entra-oppsettet, redirect URI og at lokal stack kjører.</p>
        </body>
        </html>
        """,
        status_code=status_code,
    )


@router.get("/entra/login")
def entra_login(app: str = Query(default="reporter")) -> RedirectResponse:
    try:
        app_name = app if app in {"reporter", "assignee", "admin"} else "reporter"
        logger.info("Starting Entra login flow app=%s", app_name)
        msal_app = _msal_app()
        state = create_signed_state_token({"app": app_name})
        auth_url = msal_app.get_authorization_request_url(
            scopes=ENTRA_LOGIN_SCOPES,
            state=state,
            redirect_uri=settings.entra_redirect_uri,
            prompt="select_account",
        )
        return RedirectResponse(auth_url)
    except Exception as exc:  # pragma: no cover - local debug aid
        logger.exception("Entra login failed app=%s", app)
        return _auth_error_response(
            title="Feil under Entra-innlogging",
            message=f"{exc.__class__.__name__}: {exc}",
            app_name=app if app in {"reporter", "assignee", "admin"} else "reporter",
            status_code=500,
        )


@router.get("/entra/callback", response_model=None)
def entra_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    try:
        app_name = _resolve_callback_app_name(state)
        if error:
            logger.warning("Entra callback returned error error=%s description=%s", error, error_description or "")
            return _auth_error_response(
                title="Microsoft-innlogging feilet",
                message=f"{error}: {error_description or ''}",
                app_name=app_name,
                status_code=400,
            )
        if not code or not state:
            logger.warning("Entra callback missing code or state")
            return _auth_error_response(
                title="Manglende innloggingsdata",
                message="Authorization code eller state mangler i callbacken.",
                app_name=app_name,
                status_code=400,
            )

        decoded_state = decode_signed_state_token(state)
        if not decoded_state or "app" not in decoded_state:
            logger.warning("Entra callback received invalid state")
            return _auth_error_response(
                title="Ugyldig innloggingsstatus",
                message="State-verdien kunne ikke valideres. Prøv å starte innloggingen på nytt fra appen.",
                app_name=app_name,
                status_code=400,
            )

        app_name = decoded_state["app"]
        msal_app = _msal_app()
        result = msal_app.acquire_token_by_authorization_code(
            code=code,
            scopes=ENTRA_LOGIN_SCOPES,
            redirect_uri=settings.entra_redirect_uri,
        )
        if not result:
            logger.warning("Entra token exchange returned no result app=%s", app_name)
            return _auth_error_response(
                title="Tokenutveksling feilet",
                message="MSAL returnerte ikke noe svar under tokenutvekslingen.",
                app_name=app_name,
                status_code=400,
            )
        if "error" in result:
            logger.warning("Entra token exchange failed app=%s error=%s", app_name, result.get("error") or "unknown")
            return _auth_error_response(
                title="Tokenutveksling feilet",
                message=str(result.get("error_description") or result["error"]),
                app_name=app_name,
                status_code=400,
            )

        claims = result.get("id_token_claims") or {}
        email = claims.get("preferred_username") or claims.get("email") or claims.get("upn")
        if not email:
            logger.warning("Entra callback returned no email")
            return _auth_error_response(
                title="Ingen e-post returnert",
                message="Entra ID returnerte ingen e-postadresse. Kontoen må ha en e-post knyttet til seg.",
                app_name=app_name,
                status_code=400,
            )
        user = db.get(User, email)
        role = _role_for_email(email)
        full_name = claims.get("name") or email
        entra_oid = claims.get("oid")
        if user is None:
            user = User(
                email=email,
                full_name=full_name,
                password_hash=ENTRA_AUTH_PLACEHOLDER,
                role=role,
                auth_provider="entra",
                entra_oid=entra_oid,
            )
            db.add(user)
        else:
            user.full_name = full_name
            user.role = role
            user.auth_provider = "entra"
            user.entra_oid = entra_oid
            if not user.password_hash:
                user.password_hash = ENTRA_AUTH_PLACEHOLDER
        db.commit()
        delegated_access_token = result.get("access_token")
        if delegated_access_token:
            ENTRA_ACCESS_TOKENS[email.casefold()] = delegated_access_token

        token = create_access_token(subject=email, role=role)
        logger.info("Entra login succeeded email=%s role=%s app=%s", email, role, app_name)
        cloud_mode = str(os.getenv("STREAMLIT_CLOUD_TEST_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
        cloud_pending_mode = str(os.getenv("CLOUD_TEST_ENTRA_PENDING_MODE", "")).strip().lower() in {"1", "true", "yes", "on"}
        logger.info(
            "Entra callback redirect decision cloud_mode=%s cloud_pending_mode=%s target_app_url=%s",
            cloud_mode,
            cloud_pending_mode,
            settings.streamlit_url_for_app(app_name),
        )
        if cloud_mode:
            _store_pending_app_login_file(app_name=app_name, access_token=token, email=email, role=role)
        if cloud_mode and cloud_pending_mode:
            _store_pending_app_login(app_name=app_name, access_token=token, email=email, role=role)
            logger.info("Entra callback using pending-login redirect for app=%s", app_name)
            return RedirectResponse(settings.streamlit_url_for_app(app_name))

        if cloud_mode:
            login_ticket = _create_entra_login_ticket(access_token=token, email=email, role=role)
            redirect_query = urlencode({"entra_ticket": login_ticket, "auth_provider": "entra"})
            logger.info("Entra callback using ticket redirect for app=%s", app_name)
            return RedirectResponse(f"{settings.streamlit_url_for_app(app_name)}?{redirect_query}")

        login_ticket = _create_entra_login_ticket(access_token=token, email=email, role=role)
        redirect_query = urlencode({"entra_ticket": login_ticket, "auth_provider": "entra"})
        logger.info("Entra callback using ticket redirect for app=%s", app_name)
        return RedirectResponse(f"{settings.streamlit_url_for_app(app_name)}?{redirect_query}")
    except Exception as exc:  # pragma: no cover - local debug aid
        logger.exception("Entra callback failed")
        return _auth_error_response(
            title="Intern feil under Entra-callback",
            message=f"{exc.__class__.__name__}: {exc}",
            app_name=_resolve_callback_app_name(state),
            status_code=500,
        )


def get_entra_access_token_for_email(email: str) -> str | None:
    return ENTRA_ACCESS_TOKENS.get(email.casefold())

