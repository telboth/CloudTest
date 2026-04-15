from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import streamlit as st


def _oidc_available() -> bool:
    return hasattr(st, "login") and hasattr(st, "logout") and hasattr(st, "user")


def _cfg_get(cfg: Any, key: str, default: Any = "") -> Any:
    if cfg is None:
        return default
    try:
        getter = getattr(cfg, "get", None)
        if callable(getter):
            value = getter(key, default)
            return default if value is None else value
    except Exception:
        pass
    try:
        value = cfg[key]
        return default if value is None else value
    except Exception:
        return default


def _oidc_config_diagnostics() -> tuple[bool, list[str]]:
    try:
        auth_cfg = st.secrets.get("auth", {})
    except Exception:
        return False, ["Kunne ikke lese [auth] fra secrets.toml."]

    redirect_uri = str(_cfg_get(auth_cfg, "redirect_uri", "")).strip()
    cookie_secret = str(_cfg_get(auth_cfg, "cookie_secret", "")).strip()

    direct_client_id = str(_cfg_get(auth_cfg, "client_id", "")).strip()
    direct_client_secret = str(_cfg_get(auth_cfg, "client_secret", "")).strip()
    direct_metadata_url = str(_cfg_get(auth_cfg, "server_metadata_url", "")).strip()

    provider_configs: list[Any] = []
    for provider_name in ("microsoft", "default", "entra"):
        nested = _cfg_get(auth_cfg, provider_name, {})
        if nested:
            provider_configs.append(nested)

    if isinstance(auth_cfg, Mapping):
        for key, value in auth_cfg.items():
            if key in {"redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url"}:
                continue
            if hasattr(value, "get"):
                provider_configs.append(value)

    direct_configured = bool(direct_client_id and direct_client_secret and direct_metadata_url)
    provider_configured = False
    for provider_cfg in provider_configs:
        provider_client_id = str(_cfg_get(provider_cfg, "client_id", "")).strip()
        provider_client_secret = str(_cfg_get(provider_cfg, "client_secret", "")).strip()
        provider_metadata_url = str(_cfg_get(provider_cfg, "server_metadata_url", "")).strip()
        if provider_client_id and provider_client_secret and provider_metadata_url:
            provider_configured = True
            break

    issues: list[str] = []
    if "<" in redirect_uri or ">" in redirect_uri:
        issues.append("Ugyldig [auth].redirect_uri: placeholder '<...>' må erstattes med faktisk URL.")
    elif redirect_uri and not redirect_uri.startswith(("http://", "https://")):
        issues.append("Ugyldig [auth].redirect_uri: må starte med http:// eller https://")
    if not redirect_uri:
        issues.append("Mangler [auth].redirect_uri")
    if not cookie_secret:
        issues.append("Mangler [auth].cookie_secret")
    if not (direct_configured or provider_configured):
        issues.append(
            "Mangler OIDC-provider konfig (client_id/client_secret/server_metadata_url i [auth] "
            "eller [auth.microsoft])."
        )
    configured = not issues
    return configured, issues


def _oidc_configured() -> bool:
    configured, _issues = _oidc_config_diagnostics()
    return configured


def _resolve_oidc_provider_name() -> str | None:
    try:
        auth_cfg = st.secrets.get("auth", {})
    except Exception:
        return None

    if not isinstance(auth_cfg, Mapping):
        return None

    preferred = ("microsoft", "default", "entra")
    for provider_name in preferred:
        provider_cfg = auth_cfg.get(provider_name)
        if not isinstance(provider_cfg, Mapping):
            continue
        client_id = str(provider_cfg.get("client_id", "") or "").strip()
        client_secret = str(provider_cfg.get("client_secret", "") or "").strip()
        metadata_url = str(provider_cfg.get("server_metadata_url", "") or "").strip()
        if client_id and client_secret and metadata_url:
            return provider_name

    return None


def _oidc_login_sidebar(
    *,
    allow_local_login: Callable[[], bool],
    set_user: Callable[[str], None],
    logger: Any,
) -> None:
    if not _oidc_available():
        return
    configured, issues = _oidc_config_diagnostics()
    if not configured:
        if allow_local_login():
            st.sidebar.caption("Microsoft-innlogging er ikke konfigurert her. Bruk lokal innlogging.")
        else:
            st.sidebar.caption("Microsoft-innlogging er ikke konfigurert.")
        for issue in issues[:2]:
            st.sidebar.caption(f"- {issue}")
        return
    if bool(getattr(st.user, "is_logged_in", False)):
        email = str(getattr(st.user, "email", "") or "").strip()
        current_email = str(st.session_state.get("email") or "").strip().casefold()
        current_provider = str(st.session_state.get("auth_provider") or "").strip().casefold()
        if email and (current_email != email.casefold() or current_provider != "entra"):
            set_user(email.casefold())
            st.rerun()
        return
    if st.sidebar.button("Logg inn med Microsoft", use_container_width=True, key="oidc_login_btn"):
        try:
            provider_name = _resolve_oidc_provider_name()
            if provider_name:
                try:
                    st.login(provider=provider_name)
                except TypeError:
                    # Backward compatibility with older Streamlit signatures.
                    st.login()
            else:
                st.login()
        except Exception as exc:
            if logger is not None:
                logger.warning("OIDC login failed: %s", exc)
            st.sidebar.error(f"Microsoft-innlogging feilet: {exc}")


def _local_login_sidebar(
    *,
    allow_local_login: Callable[[], bool],
    db_session: Callable[[], Any],
    verify_password: Callable[[str, str], bool],
    user_model: Any,
    default_email: str = "",
    default_password: str = "",
    enable_test_login: bool = False,
) -> None:
    local_login_allowed = allow_local_login()
    if not local_login_allowed and not enable_test_login:
        return
    
    def _authenticate_local_user(email: str, password: str) -> bool:
        normalized_email = str(email or "").strip().casefold()
        with db_session() as db:
            user = db.get(user_model, normalized_email) if normalized_email else None
            auth_provider = str(getattr(user, "auth_provider", "") or "").strip().casefold() if user else ""
            is_local_user = auth_provider == "local"
            if not user or not is_local_user or not verify_password(password, user.password_hash):
                st.sidebar.error("Ugyldig e-post eller passord.")
                return False
            st.session_state["email"] = user.email
            st.session_state["role"] = user.role
            st.session_state["auth_provider"] = "local"
            return True

    if enable_test_login:
        st.sidebar.caption("Test-innlogging")
        quick_email = (
            st.sidebar.text_input(
                "Test e-post",
                key="quick_local_login_email",
                value=(default_email or "").strip(),
            )
            .strip()
            .casefold()
        )
        quick_password = st.sidebar.text_input(
            "Test passord",
            type="password",
            key="quick_local_login_password",
            value=default_password or "",
        )
        if st.sidebar.button("Logg inn (test)", use_container_width=True, key="quick_local_login_btn"):
            if _authenticate_local_user(quick_email, quick_password):
                st.rerun()

    if not local_login_allowed:
        return

    with st.sidebar.expander("Lokal innlogging", expanded=False):
        email = (
            st.text_input(
                "E-post",
                key="local_login_email",
                value=(default_email or "").strip(),
            )
            .strip()
            .casefold()
        )
        password = st.text_input(
            "Passord",
            type="password",
            key="local_login_password",
            value=default_password or "",
        )
        if st.button("Logg inn lokalt", use_container_width=True, key="local_login_btn"):
            if _authenticate_local_user(email, password):
                st.rerun()


def _logout_sidebar(*, logger: Any) -> None:
    if st.sidebar.button("Logg ut", use_container_width=True):
        for key in ("email", "role", "auth_provider"):
            st.session_state.pop(key, None)
        if _oidc_available() and bool(getattr(st.user, "is_logged_in", False)):
            try:
                st.logout()
            except Exception as exc:
                if logger is not None:
                    logger.warning("OIDC logout failed: %s", exc)
        st.rerun()


def render_auth_gate(
    *,
    allow_local_login: Callable[[], bool],
    current_user: Callable[[], dict[str, str] | None],
    set_user: Callable[[str], None],
    db_session: Callable[[], Any],
    verify_password: Callable[[str, str], bool],
    user_model: Any,
    local_default_email: str = "",
    local_default_password: str = "",
    enable_test_login: bool = False,
    logger: Any = None,
) -> bool:
    st.sidebar.subheader("Innlogging")
    oidc_is_configured = _oidc_configured()
    _oidc_login_sidebar(allow_local_login=allow_local_login, set_user=set_user, logger=logger)
    # Safety fallback: when OIDC is not configured we still expose test-login
    # so the app is never locked out in local/dev environments.
    effective_enable_test_login = bool(enable_test_login or (not oidc_is_configured))
    _local_login_sidebar(
        allow_local_login=allow_local_login,
        db_session=db_session,
        verify_password=verify_password,
        user_model=user_model,
        default_email=local_default_email,
        default_password=local_default_password,
        enable_test_login=effective_enable_test_login,
    )
    user = current_user()
    if not user:
        if allow_local_login():
            st.info("Innlogging kreves. Bruk sidebaren for Microsoft eller lokal innlogging.")
        else:
            st.info("Innlogging kreves. Bruk sidebaren for Microsoft-innlogging. - Eller bruk default test-innlogging")
        return False
    st.sidebar.success(f"Innlogget: {user['email']}")
    _logout_sidebar(logger=logger)
    return True
