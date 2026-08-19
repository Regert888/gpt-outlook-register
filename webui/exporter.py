"""Export registered credentials to CPA and SUB2API panels.

Based on the CPA and SUB2API upload shapes used by any-auto-register.

Before export, exchange refresh_token at the OpenAI OAuth endpoint for a
Codex-style access token. The main registration flow finishes with a ChatGPT
NextAuth-style token, which CPA and SUB2API do not accept.

Targets:
  1. CPA multipart POST /v0/management/auth-files with Bearer auth.
  2. SUB2API JSON POST /api/v1/admin/accounts with x-api-key auth.

Requests use curl_cffi browser impersonation for compatible TLS behavior.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# OpenAI and Codex constants.
OPENAI_TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_SCOPE = "openid email profile offline_access"

# Defaults.
DEFAULT_TIMEOUT = 30
DEFAULT_SUB2API_GROUP_IDS = [2]
SUB2API_DEFAULT_EXPIRES_IN = 863999  # Matches the target integration.
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = [3.0, 7.0]


# ----------------------------------- Utilities -----------------------------------


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload, returning {} on failure."""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        p = parts[1]
        pad = (4 - len(p) % 4) % 4
        data = json.loads(base64.urlsafe_b64decode(p + "=" * pad))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _b64url_json(d: dict) -> str:
    raw = json.dumps(d, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _get_auth(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _get_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    p = payload.get("https://api.openai.com/profile")
    return p if isinstance(p, dict) else {}


def _first(*values) -> str:
    for v in values:
        s = str(v or "").strip()
        if s:
            return s
    return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    """Parse comma-separated, iterable, or scalar group IDs into list[int]."""
    if isinstance(raw, str):
        candidates = [s.strip() for s in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    out: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            out.append(int(text))
        except ValueError:
            continue
    return out or list(fallback or DEFAULT_SUB2API_GROUP_IDS)


def _import_cffi():
    """Import curl_cffi lazily, raising RuntimeError when unavailable."""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests
    except ImportError as e:
        raise RuntimeError(
            f"curl_cffi is not installed; export is unavailable "
            f"(pip install curl-cffi): {e}"
        )


def _import_cffi_mime():
    """Import CurlMime lazily."""
    try:
        from curl_cffi import CurlMime
        return CurlMime
    except ImportError as e:
        raise RuntimeError(f"curl_cffi CurlMime is unavailable: {e}")


# ------------------------------ Refresh Codex token ------------------------------


def refresh_codex_token(refresh_token: str, *, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Exchange a Codex refresh token for a rotated token set.

    Return the original OpenAI response dictionary:
        {access_token, refresh_token, id_token, expires_in, token_type}
    Raise RuntimeError on failure.
    """
    rt = str(refresh_token or "").strip()
    if not rt:
        raise RuntimeError("A refresh_token is required to refresh the Codex access token")

    cffi = _import_cffi()
    body = {
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": rt,
        "scope": CODEX_SCOPE,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Origin": "https://auth.openai.com",
        "Referer": "https://auth.openai.com/",
    }

    resp = cffi.post(
        OPENAI_TOKEN_ENDPOINT,
        headers=headers,
        data=body,
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )

    if resp.status_code != 200:
        body_text = ""
        try:
            body_text = (resp.text or "")[:300]
        except Exception:
            pass
        raise RuntimeError(
            f"OpenAI token refresh failed with HTTP {resp.status_code}: {body_text}"
        )

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("OpenAI token refresh returned a non-JSON response")

    if not isinstance(data, dict) or not data.get("access_token"):
        raise RuntimeError(
            f"OpenAI token refresh returned no access_token: {str(data)[:200]}"
        )

    return data


# ------------------------------- Build CPA token JSON -------------------------------


def _build_compat_id_token(*, access_token: str, email: str) -> str:
    """Build a locally parseable compatibility token when id_token is absent.

    The fixed signature is only for local consumers that do not verify it.
    """
    payload = _decode_jwt_payload(access_token)
    if not payload:
        return ""

    auth_info = _get_auth(payload)
    profile = _get_profile(payload)
    email_from_token = (profile.get("email") or payload.get("email") or email or "").strip()
    email_verified = bool(profile.get("email_verified", payload.get("email_verified", True)))
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    user_id = str(
        auth_info.get("chatgpt_user_id")
        or auth_info.get("user_id")
        or payload.get("sub")
        or ""
    ).strip()
    iat = int(payload.get("iat") or 0)
    exp = int(payload.get("exp") or 0)
    auth_time = int(payload.get("pwd_auth_time") or payload.get("auth_time") or iat or 0)
    session_id = str(
        payload.get("session_id")
        or f"compat_session_{(account_id or user_id or 'unknown').replace('-', '')[:24]}"
    ).strip()
    plan_type = str(auth_info.get("chatgpt_plan_type") or "free").strip() or "free"
    organization_id = str(
        auth_info.get("organization_id")
        or f"org-{hashlib.sha1((account_id or email_from_token or user_id).encode('utf-8')).hexdigest()[:24]}"
    )
    project_id = str(
        auth_info.get("project_id")
        or f"proj_{hashlib.sha1((organization_id + ':' + (account_id or user_id)).encode('utf-8')).hexdigest()[:24]}"
    )

    compat_auth = {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan_type,
        "chatgpt_subscription_active_start": auth_info.get("chatgpt_subscription_active_start"),
        "chatgpt_subscription_active_until": auth_info.get("chatgpt_subscription_active_until"),
        "chatgpt_subscription_last_checked": auth_info.get("chatgpt_subscription_last_checked"),
        "chatgpt_user_id": user_id,
        "completed_platform_onboarding": bool(auth_info.get("completed_platform_onboarding", False)),
        "groups": auth_info.get("groups", []),
        "is_org_owner": bool(auth_info.get("is_org_owner", True)),
        "localhost": bool(auth_info.get("localhost", True)),
        "organization_id": organization_id,
        "organizations": auth_info.get("organizations") or [
            {"id": organization_id, "is_default": True, "role": "owner", "title": "Personal"}
        ],
        "project_id": project_id,
        "user_id": str(auth_info.get("user_id") or user_id or "").strip(),
    }

    compat_payload = {
        "amr": ["pwd", "otp", "mfa", "urn:openai:amr:otp_email"],
        "at_hash": hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:22],
        "aud": [CODEX_CLIENT_ID],
        "auth_provider": "password",
        "auth_time": auth_time,
        "email": email_from_token,
        "email_verified": email_verified,
        "exp": exp,
        "https://api.openai.com/auth": compat_auth,
        "iat": iat,
        "iss": payload.get("iss") or "https://auth.openai.com",
        "jti": f"compat-{hashlib.sha1(access_token.encode('utf-8')).hexdigest()[:32]}",
        "name": email_from_token or "OpenAI User",
        "rat": auth_time,
        "sid": session_id,
        "sub": payload.get("sub") or user_id,
    }

    header = {"alg": "RS256", "typ": "JWT", "kid": "compat"}
    signature = base64.urlsafe_b64encode(b"compat_signature_for_cpa_parsing_only").decode("ascii").rstrip("=")
    return f"{_b64url_json(header)}.{_b64url_json(compat_payload)}.{signature}"


def build_cpa_token_json(cred: dict) -> dict:
    """Build the multipart JSON file for CPA `/v0/management/auth-files`.

    The eight-field shape and UTC+8 timestamps match the target integration.
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("No exportable access_token was found")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()

    if not id_token:
        id_token = _build_compat_id_token(access_token=access_token, email=email)

    payload = _decode_jwt_payload(access_token)
    auth_info = _get_auth(payload)
    account_id = str(auth_info.get("chatgpt_account_id") or "").strip()

    tz_cn = timezone(timedelta(hours=8))
    expired_str = ""
    exp = payload.get("exp")
    if isinstance(exp, int) and exp > 0:
        expired_str = datetime.fromtimestamp(exp, tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    last_refresh = datetime.now(tz=tz_cn).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    return {
        "type": "codex",
        "email": email,
        "expired": expired_str,
        "id_token": id_token,
        "account_id": account_id,
        "access_token": access_token,
        "last_refresh": last_refresh,
        "refresh_token": refresh_token,
    }


# ----------------------------------- CPA upload -----------------------------------


def export_to_cpa(cred: dict, cfg: dict, *,
                    log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """Upload a CPA multipart credential file."""
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    if not api_url:
        raise RuntimeError("CPA URL is not configured")
    if not api_key:
        raise RuntimeError("CPA management key is not configured")

    cffi = _import_cffi()
    CurlMime = _import_cffi_mime()

    token_data = build_cpa_token_json(cred)
    email = token_data.get("email") or "unknown"
    filename = f"{email}.json"
    file_content = json.dumps(token_data, ensure_ascii=False, indent=2).encode("utf-8")
    upload_url = f"{api_url}/v0/management/auth-files"
    # Send both accepted authentication headers for deployment compatibility.
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Management-Key": api_key,
    }

    log(
        f"[CPA] Upload target: {upload_url}  "
        f"file={filename}  size={len(file_content)}B  "
        f"access_token={'yes' if token_data.get('access_token') else 'no'}  "
        f"refresh_token={'yes' if token_data.get('refresh_token') else 'no'}  "
        f"id_token={'yes' if token_data.get('id_token') else 'no'}",
        "info",
    )

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        mime = None
        try:
            log(
                f"[CPA] Multipart upload attempt {attempt}/{MAX_ATTEMPTS}: {filename}...",
                "info",
            )
            mime = CurlMime()
            mime.addpart(
                name="file",
                data=file_content,
                filename=filename,
                content_type="application/json",
            )
            resp = cffi.post(
                upload_url,
                multipart=mime,
                headers=headers,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
            # Log HTTP status and a bounded response preview.
            try:
                body_preview = (resp.text or "")[:400]
            except Exception:
                body_preview = "(response body unavailable)"
            log(
                f"[CPA] Server response: HTTP {resp.status_code}  body={body_preview!r}",
                "info" if resp.status_code in (200, 201) else "warn",
            )
            if resp.status_code in (200, 201):
                log(f"[CPA] ✅ Upload successful: {filename}", "ok")
                return {"ok": True, "email": email, "file_name": filename,
                        "message": f"CPA upload successful: {filename}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(detail.get("message") or detail.get("error") or detail.get("detail") or msg)
            except Exception:
                msg = f"{msg}: {body_preview}"
            last_err = msg
            # Log non-retryable 4xx failures as well as retryable errors.
            log(f"[CPA] ❌ Upload failed: {msg}", "error")
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(
                    f"[CPA] Attempt {attempt} failed ({msg}); retrying in {delay:.0f}s",
                    "warn",
                )
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email, "file_name": filename}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(
                    f"[CPA] Attempt {attempt} raised an error ({e}); "
                    f"retrying in {delay:.0f}s",
                    "warn",
                )
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email, "file_name": filename}
        finally:
            if mime is not None:
                try:
                    mime.close()
                except Exception:
                    pass
    return {
        "ok": False,
        "error": last_err or "Retry limit reached",
        "email": email,
        "file_name": filename,
    }


# ------------------------------- Build SUB2API payload -------------------------------


def build_sub2api_payload(cred: dict, group_ids: list[int]) -> dict:
    """Build the SUB2API POST /api/v1/admin/accounts body.

    Match the target integration's account payload shape.
    """
    access_token = str(cred.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("No exportable access_token was found")
    refresh_token = str(cred.get("refresh_token") or "").strip()
    id_token = str(cred.get("id_token") or "").strip()
    email = str(cred.get("email") or "").strip()

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _get_auth(access_payload)

    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + SUB2API_DEFAULT_EXPIRES_IN

    # Prefer organization_id from id_token, then fall back to access_token.
    id_auth = _get_auth(_decode_jwt_payload(id_token))
    organization_id = str(id_auth.get("organization_id") or "").strip()
    if not organization_id:
        orgs = id_auth.get("organizations") or []
        if isinstance(orgs, list):
            for o in orgs:
                if isinstance(o, dict):
                    organization_id = str(o.get("id") or "").strip()
                    if organization_id:
                        break
    if not organization_id:
        organization_id = str(access_auth.get("organization_id") or access_auth.get("poid") or "").strip()

    client_id = str(
        cred.get("client_id") or access_payload.get("client_id") or CODEX_CLIENT_ID
    ).strip() or CODEX_CLIENT_ID

    chatgpt_account_id = str(
        access_auth.get("chatgpt_account_id") or cred.get("account_id") or ""
    ).strip()
    chatgpt_user_id = str(access_auth.get("chatgpt_user_id") or "").strip()

    return {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": SUB2API_DEFAULT_EXPIRES_IN,
            "expires_at": expires_at,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "organization_id": organization_id,
            "client_id": client_id,
            "id_token": id_token,
        },
        "extra": {"email": email},
        "group_ids": list(group_ids) if group_ids else list(DEFAULT_SUB2API_GROUP_IDS),
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": True,
    }


# -------------------------------- SUB2API upload --------------------------------


def export_to_sub2api(cred: dict, cfg: dict, *,
                        log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """Upload directly to SUB2API with x-api-key and no login flow."""
    log = log_fn or (lambda m, lvl="info": logger.info(m))

    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API URL is not configured")
    if not api_key:
        raise RuntimeError("SUB2API API key is not configured")

    group_ids = _parse_group_ids(cfg.get("sub2api_group_ids"))
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    payload = build_sub2api_payload(cred, group_ids)
    email = payload.get("name") or "unknown"
    url = f"{api_url}/api/v1/admin/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url}/admin/accounts",
        "x-api-key": api_key,
    }

    last_err = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            log(
                f"[SUB2API] Upload attempt {attempt}/{MAX_ATTEMPTS}: {email} "
                f"(group_ids={group_ids})...",
                "info",
            )
            resp = cffi.post(
                url,
                headers=headers,
                json=payload,
                proxies=None,
                verify=False,
                timeout=timeout,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                new_id = ""
                try:
                    data = resp.json()
                    if isinstance(data, dict):
                        new_id = str(data.get("id") or data.get("ID") or "").strip()
                except Exception:
                    pass
                log(
                    f"[SUB2API] ✅ Upload successful: {email} "
                    f"(id={new_id or 'unknown'})",
                    "ok",
                )
                return {"ok": True, "email": email, "account_id": new_id,
                        "message": f"SUB2API upload successful #{new_id or 'unknown'}"}
            msg = f"HTTP {resp.status_code}"
            try:
                detail = resp.json()
                if isinstance(detail, dict):
                    msg = str(
                        detail.get("message") or detail.get("msg")
                        or detail.get("error") or msg
                    )
            except Exception:
                msg = f"{msg} - {(resp.text or '')[:200]}"
            last_err = msg
            if attempt < MAX_ATTEMPTS and resp.status_code >= 500:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(
                    f"[SUB2API] Attempt {attempt} failed ({msg}); "
                    f"retrying in {delay:.0f}s",
                    "warn",
                )
                time.sleep(delay)
                continue
            return {"ok": False, "error": msg, "email": email}
        except Exception as e:
            last_err = str(e)
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS_S[attempt - 1]
                log(
                    f"[SUB2API] Attempt {attempt} raised an error ({e}); "
                    f"retrying in {delay:.0f}s",
                    "warn",
                )
                time.sleep(delay)
                continue
            return {"ok": False, "error": str(e), "email": email}
    return {"ok": False, "error": last_err or "Retry limit reached", "email": email}


# ------------------------------- Connectivity tests -------------------------------


def test_cpa(cfg: dict) -> dict:
    """Test CPA connectivity and validate the Bearer key with a harmless GET.

    OPTIONS is only a CORS preflight and often skips authentication, which could
    falsely report a bad key as healthy.
    """
    api_url = (cfg.get("cpa_url") or "").rstrip("/").strip()
    api_key = (cfg.get("cpa_mgmt_key") or "").strip()
    if not api_url:
        raise RuntimeError("CPA URL is not configured")
    if not api_key:
        raise RuntimeError("CPA management key is not configured")
    timeout = int(cfg.get("cpa_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/v0/management/auth-files",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Management-Key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    if resp.status_code in (200, 201, 204):
        return {
            "ok": True,
            "message": f"CPA is reachable and the key is valid (HTTP {resp.status_code})",
        }
    if resp.status_code in (401, 403):
        body = ""
        try:
            body = (resp.text or "")[:200]
        except Exception:
            pass
        raise RuntimeError(
            f"CPA authentication failed (HTTP {resp.status_code}): "
            f"the management key is invalid. Response: {body}"
        )
    # HTTP 405 proves the URL is reachable even though GET cannot validate the key.
    if resp.status_code == 405:
        return {
            "ok": True,
            "message": (
                "CPA is reachable (HTTP 405), but GET cannot verify the key. "
                "Confirm it with an actual upload."
            ),
        }
    raise RuntimeError(f"CPA returned HTTP {resp.status_code}: {(resp.text or '')[:200]}")


def test_sub2api(cfg: dict) -> dict:
    """Test SUB2API connectivity and key validity through the account-list endpoint."""
    api_url = (cfg.get("sub2api_url") or "").rstrip("/").strip()
    api_key = (cfg.get("sub2api_api_key") or "").strip()
    if not api_url:
        raise RuntimeError("SUB2API URL is not configured")
    if not api_key:
        raise RuntimeError("SUB2API API key is not configured")
    timeout = int(cfg.get("sub2api_timeout") or DEFAULT_TIMEOUT)
    cffi = _import_cffi()

    resp = cffi.get(
        f"{api_url}/api/v1/admin/accounts",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{api_url}/admin/accounts",
            "x-api-key": api_key,
        },
        proxies=None,
        verify=False,
        timeout=timeout,
        impersonate="chrome110",
    )
    if resp.status_code in (200, 201):
        return {"ok": True, "message": f"SUB2API is reachable (HTTP {resp.status_code})"}
    if resp.status_code in (401, 403):
        raise RuntimeError(
            f"SUB2API authentication failed (HTTP {resp.status_code}); check the API key"
        )
    raise RuntimeError(
        f"SUB2API returned HTTP {resp.status_code}: {(resp.text or '')[:200]}"
    )


# ----------------------------- Post-registration entry -----------------------------


def run_exports(cred: dict, *,
                  cpa_cfg: Optional[dict] = None,
                  sub2api_cfg: Optional[dict] = None,
                  log_fn: Optional[Callable[[str, str], None]] = None) -> dict:
    """Run configured exports after registration.

    If a destination is enabled, refresh the NextAuth-style saved token into a
    Codex-style token, then send the refreshed credential to that destination.

    Return {"cpa": dict|None, "sub2api": dict|None, "any_attempted": bool}.
    """
    log = log_fn or (lambda m, lvl="info": logger.info(m))
    out: dict = {"cpa": None, "sub2api": None, "any_attempted": False}

    cpa_on = bool(cpa_cfg and cpa_cfg.get("enabled"))
    sub2_on = bool(sub2api_cfg and sub2api_cfg.get("enabled"))
    if not (cpa_on or sub2_on):
        return out

    # First exchange refresh_token for a Codex-style access token.
    try:
        log("[exporter] Refreshing the Codex access token...", "info")
        fresh = refresh_codex_token(cred.get("refresh_token", ""))
        cred = {
            **cred,
            "access_token":  fresh["access_token"],
            "refresh_token": fresh.get("refresh_token") or cred.get("refresh_token"),
            "id_token":      fresh.get("id_token") or cred.get("id_token", ""),
        }
        log(
            f"[exporter] ✅ Codex token refresh successful "
            f"(access_token len={len(fresh['access_token'])} "
            f"id_token len={len(fresh.get('id_token') or '')})",
            "ok",
        )
    except Exception as e:
        log(f"[exporter] ❌ Codex token refresh failed; export cannot continue: {e}", "error")
        if cpa_on:
            out["any_attempted"] = True
            out["cpa"] = {"ok": False, "error": f"Codex token refresh failed: {e}"}
        if sub2_on:
            out["any_attempted"] = True
            out["sub2api"] = {"ok": False, "error": f"Codex token refresh failed: {e}"}
        return out

    if cpa_on:
        out["any_attempted"] = True
        try:
            out["cpa"] = export_to_cpa(cred, cpa_cfg, log_fn=log)
        except Exception as e:
            log(f"[CPA] Export error: {e}", "error")
            out["cpa"] = {"ok": False, "error": str(e)}

    if sub2_on:
        out["any_attempted"] = True
        try:
            out["sub2api"] = export_to_sub2api(cred, sub2api_cfg, log_fn=log)
        except Exception as e:
            log(f"[SUB2API] Export error: {e}", "error")
            out["sub2api"] = {"ok": False, "error": str(e)}

    return out
