"""
Registration/login flow using direct protocol calls.
Complete sequence:
  chatgpt_csrf -> chatgpt_signin_openai -> auth_oauth_init -> sentinel
  -> signup -> send_otp -> verify_otp -> create_account
  -> redirect_chain -> auth_session -> (optional) oauth_token_exchange
"""
import json
import base64
import hashlib
import hmac
import logging
import os
import random
import re
import secrets
import struct
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional, Any
from urllib.parse import urlparse, parse_qs, parse_qsl, urljoin, urlencode, urlunparse

from config import Config
from fingerprint import (
    generate_fingerprint,
    ua_for_impersonate,
    fingerprint_for_impersonate,
)
from mail_providers import MailProvider
from http_client import create_http_session, USER_AGENT

logger = logging.getLogger(__name__)


# ── RFC 6238 TOTP implementation for computing mfa-challenge codes ────────────
def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    """HOTP algorithm (RFC 4226)."""
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def _totp_now(secret_b32: str) -> str:
    """Return the six-digit TOTP code for the current 30-second window."""
    return _hotp(secret_b32, int(time.time()) // 30)


class AuthResult:
    """Authentication result."""

    def __init__(self):
        self.email: str = ""
        self.password: str = ""
        self.session_token: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""
        self.cookie_header: str = ""
        self.totp_secret: str = ""

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "cookie_header": self.cookie_header,
            "totp_secret": self.totp_secret,
        }


class AuthFlow:
    """Direct-protocol registration and login flow."""

    def __init__(
        self,
        config: Config,
        sms_callback: Optional[Any] = None,
        env_overrides: Optional[dict] = None,
        on_password: Optional[Any] = None,
        on_session_ready: Optional[Any] = None,
        account_callback: Optional[Any] = None,
    ):
        # Per-flow configuration overrides (WEBUI_ALLOW_LOGIN, OTP_TIMEOUT, OAuth flags, and so on).
        # Previously registrar wrote directly to os.environ and restored it in finally.
        # auto_loop can run multiple workers concurrently, so B could see OTP_TIMEOUT written by A;
        # when B restored the older value, A could read another worker's configuration later in its run.
        # Overrides now live only on the instance and never mutate the process-wide environment.
        self._env_overrides = dict(env_overrides or {})
        self.config = config
        self._country_code = ""  # IP geolocation country code, populated by check_proxy()
        self._fingerprint = generate_fingerprint()  # Generate the default fingerprint first
        self._ua = self._fingerprint["user_agent"]
        self._impersonate_candidates = self._fingerprint.get(
            "fallback_impersonates",
            [self._fingerprint["impersonate"], "safari17_0", "safari15_5"],
        )
        self._impersonate_idx = 0
        self.session = create_http_session(
            proxy=config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
            user_agent=self._ua,
        )
        self.result = AuthResult()
        # Optional SMS controller (a sms_provider.PhoneCallbackController instance).
        # On add-phone, rent a number and receive the SMS code automatically;
        # otherwise fall back to the environment-variable path.
        self._sms_callback = sms_callback
        # Invoke the callback as soon as the password takes effect on OpenAI; the caller persists it immediately.
        # Signature: (email: str, password: str) -> None. register_password suppresses callback errors.
        # The protocol layer does not know about webui.db, so it exposes only a callback and lets the caller
        # decide where to persist data. CLI use can omit it, preserving the previous behavior.
        self._on_password = on_password
        # Hook invoked after obtaining the session (access_token) but before Codex authorization.
        # Signature: (flow: AuthFlow, access_token: str) -> None. The call site suppresses errors.
        # This inserts 2FA binding in the requested order:
        #     create account -> redirect chain -> get session -> bind 2FA -> Codex authorization -> receive code
        # Supplying this hook also disables the Codex attempt before the callback in run_register
        # (:3051 OAUTH_CODEX_RT_BEFORE_CALLBACK); otherwise Codex would run before the hook.
        # Omitting the hook preserves the old behavior exactly.
        self._on_session_ready = on_session_ready
        # Account-credentials callback: load password and totp_secret for an existing-account login.
        # Signature: (email: str) -> dict returning {"password": "...", "totp_secret": "..."}.
        # The mfa-challenge path uses it to load the secret needed after password verification.
        self._account_callback = account_callback
        self._http_trace_enabled = str(os.getenv("AUTH_HTTP_TRACE", "0")).lower() in ("1", "true", "yes", "on")
        # signup() sets this within its branches, but an existing-account run_protocol_login path skips signup.
        # Initialize it here so kickoff_otp_delivery cannot read an unset attribute.
        self._is_existing_account = False
        self._existing_email_verification_mode = ""
        self._existing_page_type = ""
        self._manual_login_verifier = (os.getenv("LOGIN_VERIFIER", "") or "").strip()
        self._captured_login_verifier = ""
        self._oauth_client_secret = (os.getenv("OAUTH_CLIENT_SECRET", "") or "").strip()
        self._oauth_client_id = "YOUR_OPENAI_WEB_CLIENT_ID"
        self._oauth_redirect_uri = "https://chatgpt.com/api/auth/callback/openai"
        self._oauth_scope = ""
        self._oauth_state = ""
        self._oauth_auth_url = ""
        self._client_auth_session_dump: dict[str, Any] = {}
        self._client_auth_session_id: str = ""
        self._dump_login_verifier: str = ""
        self._codex_rt_attempted: bool = False
        self._trace_dump_enabled = str(os.getenv("AUTH_TRACE_DUMP", "0")).lower() in ("1", "true", "yes", "on")
        self._trace_include_cookie = str(os.getenv("AUTH_TRACE_INCLUDE_COOKIE", "0")).lower() in (
            "1", "true", "yes", "on"
        )
        self._trace_dump_path = ""
        logger.debug(
            f"Fingerprint: impersonate={self._fingerprint['impersonate']} "
            f"screen={self._fingerprint['screen']} lang={self._fingerprint['lang']} "
            f"ua={self._ua}"
        )

    def _build_chatgpt_cookie_header(self) -> str:
        """
        Export chatgpt.com cookies from the current session.

        Notes:
        - The modern/custom `/backend-api/payments/checkout` entry point depends not only on
          `__Secure-next-auth.session-token`, but also validates several same-domain cookies,
          such as csrf, oai-sc, and Cloudflare cookies.
        - Therefore this method must return as many chatgpt.com cookies already collected by
          the session as possible, rather than returning only session_token.
        """
        cookie_pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        try:
            jar_iter = list(self.session.cookies)
        except Exception:
            jar_iter = []

        for cookie in jar_iter:
            try:
                name = (getattr(cookie, "name", "") or "").strip()
                value = getattr(cookie, "value", "") or ""
                domain = (getattr(cookie, "domain", "") or "").strip().lower()
            except Exception:
                continue
            if not name or not value:
                continue
            if domain and "chatgpt.com" not in domain:
                continue
            if name in seen:
                continue
            seen.add(name)
            cookie_pairs.append((name, value))

        # Add critical cookies as a fallback in case cookie-jar iteration omits them
        critical_names = [
            "__Secure-next-auth.session-token",
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "oai-did",
            "oai-sc",
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__stripe_mid",
            "__stripe_sid",
            "oai-client-auth-info",
            "oai-gn",
            "oai-nav-state",
            "oai-hlib",
            "_account_is_fedramp",
            "oai_consent_analytics",
            "oai_consent_marketing",
            "oai-allow-ne",
            "_ga",
            "_ga_9SHBSK2D9J",
            "_gcl_au",
            "_fbp",
            "_puid",
            "_dd_s",
            "g_state",
        ]
        for name in critical_names:
            if name in seen:
                continue
            try:
                value = self.session.cookies.get(name, "")
            except Exception:
                value = ""
            if value:
                seen.add(name)
                cookie_pairs.append((name, value))

        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)
        if self._trace_dump_enabled:
            try:
                os.makedirs("outputs", exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                self._trace_dump_path = os.path.join("outputs", f"auth_trace_{ts}_{os.getpid()}.jsonl")
                logger.info(f"Plaintext HTTP capture enabled: {self._trace_dump_path}")
            except Exception as e:
                logger.warning(f"Failed to initialize HTTP capture file: {e}")
                self._trace_dump_enabled = False

    def _trace_http(self, step: str, resp, extra_request: dict | None = None):
        """Optional fine-grained HTTP tracing for protocol debugging."""
        if (not self._http_trace_enabled and not self._trace_dump_enabled) or resp is None:
            return
        try:
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "") if req else ""
            req_url = getattr(req, "url", "") if req else ""
            req_body = ""
            req_headers = {}
            if req is not None:
                raw_req_body = getattr(req, "body", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "content", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "data", None)
                if isinstance(raw_req_body, bytes):
                    req_body = raw_req_body.decode("utf-8", errors="replace")
                elif raw_req_body is not None:
                    req_body = str(raw_req_body)
                try:
                    req_headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    req_headers = {}

            # Supplement request details when curl_cffi leaves request.body or headers empty
            if isinstance(extra_request, dict):
                if not method:
                    method = str(extra_request.get("method", "") or "")
                if not req_url:
                    req_url = str(extra_request.get("url", "") or "")
                if not req_body:
                    maybe_body = extra_request.get("body", "")
                    if isinstance(maybe_body, bytes):
                        req_body = maybe_body.decode("utf-8", errors="replace")
                    else:
                        req_body = str(maybe_body or "")
                extra_headers = extra_request.get("headers", {})
                if isinstance(extra_headers, dict):
                    merged = dict(req_headers or {})
                    merged.update(extra_headers)
                    req_headers = merged

            status = getattr(resp, "status_code", "N/A")
            final_url = str(getattr(resp, "url", "") or "")
            req_cookie = (req_headers.get("Cookie", "") or "")
            location = (resp.headers.get("Location", "") or "")[:180]
            req_id = (resp.headers.get("x-request-id", "") or "")[:120]
            ctype = (resp.headers.get("Content-Type", "") or "")[:120]
            # Preserve complete Set-Cookie values because critical cookies may appear in later entries
            set_cookie_list: list[str] = []
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(resp.headers, "getlist", None)
                if callable(get_list):
                    vals = get_list("Set-Cookie")
                    if isinstance(vals, list):
                        set_cookie_list = [str(x) for x in vals if x]
            except Exception:
                set_cookie_list = []
            if not set_cookie_list:
                one = (resp.headers.get("Set-Cookie", "") or "")
                if one:
                    set_cookie_list = [one]
            set_cookie_raw = " || ".join(set_cookie_list)
            set_cookie = set_cookie_raw[:260]
            body = (resp.text or "").replace("\n", " ").replace("\r", " ")
            body = body[:260]
            req_headers_lc = {(str(k).lower()): v for k, v in (req_headers or {}).items()}

            if self._http_trace_enabled:
                logger.info(
                    "[HTTP TRACE] %s | %s %s -> %s | url=%s | location=%s | req_id=%s | ctype=%s | set_cookie=%s | body=%s",
                    step,
                    method,
                    req_url[:180],
                    status,
                    final_url[:180],
                    location,
                    req_id,
                    ctype,
                    set_cookie,
                    body,
                )
                if self._trace_include_cookie and req_cookie:
                    logger.info("[HTTP TRACE] %s | req_cookie=%s", step, req_cookie[:360])

            # Capture login_verifier/code_verifier from multiple sources
            self._sniff_login_verifier(req_url, f"{step}:req_url")
            self._sniff_login_verifier(req_body, f"{step}:req_body")
            self._sniff_login_verifier(final_url, f"{step}:final_url")
            self._sniff_login_verifier(location, f"{step}:location")
            raw_text = resp.text or ""
            self._sniff_login_verifier(raw_text, f"{step}:resp_body")

            # Persist plaintext HTTP captures as JSONL
            if self._trace_dump_enabled and self._trace_dump_path:
                try:
                    include_req_cookie = self._env_flag("AUTH_TRACE_INCLUDE_REQ_COOKIE", "0")
                    record = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "step": step,
                        "request": {
                            "method": method,
                            "url": req_url,
                            "body": req_body[:120000],
                            "headers": {
                                "Content-Type": (req_headers_lc.get("content-type", "") or "")[:240],
                                "Accept": (req_headers_lc.get("accept", "") or "")[:240],
                                "Referer": (req_headers_lc.get("referer", "") or "")[:500],
                                "Origin": (req_headers_lc.get("origin", "") or "")[:120],
                                **(
                                    {
                                        "Cookie": (req_headers_lc.get("cookie", "") or "")[:6000],
                                    }
                                    if include_req_cookie
                                    else {}
                                ),
                            },
                        },
                        "response": {
                            "status_code": status,
                            "url": final_url,
                            "location": resp.headers.get("Location", ""),
                            "x_request_id": resp.headers.get("x-request-id", ""),
                            "content_type": resp.headers.get("Content-Type", ""),
                            "set_cookie": set_cookie_raw,
                            "set_cookie_list": set_cookie_list,
                            "body": raw_text[:120000],
                        },
                        "captured_login_verifier": self._captured_login_verifier,
                    }
                    if self._trace_include_cookie and req_cookie:
                        record["request"]["headers"]["Cookie"] = req_cookie[:8000]
                    with open(self._trace_dump_path, "a", encoding="utf-8") as fw:
                        fw.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.debug(f"Failed to write HTTP capture: {e}")
        except Exception as e:
            logger.debug(f"Failed to emit HTTP trace: {e}")

    def _sniff_login_verifier(self, text: str, source: str = ""):
        """Extract login_verifier/code_verifier from arbitrary text."""
        if not text:
            return
        try:
            patterns = [
                r"(?:login_verifier|code_verifier|verifier)=([A-Za-z0-9._~-]{8,})",
                r'"(?:login_verifier|code_verifier|verifier)"\s*:\s*"([^"]{8,})"',
            ]
            for p in patterns:
                m = re.search(p, text)
                if not m:
                    continue
                v = (m.group(1) or "").strip()
                if not v:
                    continue
                if v != self._captured_login_verifier:
                    self._captured_login_verifier = v
                    logger.info("Captured login_verifier source=%s len=%s", source or "unknown", len(v))
                return
        except Exception:
            return

    @staticmethod
    def _walk_collect_str_fields(obj: Any, wanted_keys: set[str], out: dict[str, str], depth: int = 0, max_depth: int = 6):
        """Recursively collect string values for the requested fields."""
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = (str(k) or "").strip().lower()
                if kk in wanted_keys and isinstance(v, str) and v.strip():
                    out[kk] = v.strip()
                AuthFlow._walk_collect_str_fields(v, wanted_keys, out, depth + 1, max_depth)
        elif isinstance(obj, list):
            for it in obj:
                AuthFlow._walk_collect_str_fields(it, wanted_keys, out, depth + 1, max_depth)

    def fetch_client_auth_session_dump(self, stage: str = "") -> dict:
        """
        Try to read auth.openai's client_auth_session_dump:
        - It may contain additional session_id/client_auth_session state.
        - If verifier or refresh fields appear, inject them into the current flow automatically.
        """
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Accept"] = "application/json"
        try:
            resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers=headers,
                timeout=30,
            )
            self._trace_http(f"client_auth_session_dump_{stage or 'default'}", resp)
        except Exception as e:
            logger.debug(f"client_auth_session_dump request error ({stage}): {e}")
            return {}

        if resp.status_code != 200:
            logger.info(
                "client_auth_session_dump(%s) returned non-200 status: %s",
                stage or "default",
                resp.status_code,
            )
            return {}

        try:
            data = resp.json()
        except Exception:
            logger.warning(f"Failed to parse client_auth_session_dump({stage}) JSON")
            return {}

        if not isinstance(data, dict):
            return {}

        self._client_auth_session_dump = data
        cas = data.get("client_auth_session", {}) if isinstance(data.get("client_auth_session"), dict) else {}

        sid = (data.get("session_id", "") or "").strip() or (cas.get("session_id", "") or "").strip()
        if sid:
            self._client_auth_session_id = sid

        # Synchronize OAuth client_id if the dump provides a more accurate value
        dump_client_id = (cas.get("openai_client_id", "") or data.get("openai_client_id", "") or "").strip()
        if dump_client_id:
            self._oauth_client_id = dump_client_id

        wanted = {
            "login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier",
            "refresh_token", "oauth_refresh_token", "access_token", "id_token",
        }
        found: dict[str, str] = {}
        self._walk_collect_str_fields(data, wanted, found)

        # Verifier candidates
        for key in ("login_verifier", "code_verifier", "verifier", "pkce_verifier", "oauth_code_verifier"):
            v = (found.get(key, "") or "").strip()
            if v and len(v) >= 8:
                self._dump_login_verifier = v
                self._captured_login_verifier = v
                logger.info("client_auth_session_dump captured verifier: key=%s len=%s", key, len(v))
                break

        # Token candidates are rare, but accept them when present
        refresh = (found.get("refresh_token", "") or found.get("oauth_refresh_token", "")).strip()
        if refresh:
            self.result.refresh_token = refresh
        acc = (found.get("access_token", "") or "").strip()
        if acc:
            self.result.access_token = acc
        idt = (found.get("id_token", "") or "").strip()
        if idt:
            self.result.id_token = idt

        logger.debug(
            "client_auth_session_dump(%s) succeeded: top_keys=%s cas_keys=%s session_id=%s refresh=%s verifier=%s",
            stage or "default",
            list(data.keys())[:12],
            list(cas.keys())[:18] if isinstance(cas, dict) else [],
            (self._client_auth_session_id[:24] if self._client_auth_session_id else ""),
            "present" if self.result.refresh_token else "absent",
            "present" if self._dump_login_verifier else "absent",
        )
        return data

    @staticmethod
    def _is_tls_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        markers = ["curl: (35)", "tls connect error", "openssl_internal", "sslerror"]
        return any(m in msg for m in markers)

    @staticmethod
    def _is_registration_disallowed_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "registration_disallowed" in msg

    def _get_cookie_value_by_name(self, name: str) -> str:
        """Get a cookie value by name while ignoring domain conflicts."""
        try:
            jar = getattr(self.session.cookies, "jar", None)
            if jar is None:
                return ""
            target = (name or "").strip().lower()
            for c in jar:
                if (getattr(c, "name", "") or "").strip().lower() == target:
                    return (getattr(c, "value", "") or "").strip()
        except Exception:
            pass
        return ""

    def _extract_login_challenge_from_cookie(self) -> str:
        """
        Extract login_challenge from the login_session cookie.
        The first login_session segment is usually base64url-encoded JSON.
        """
        raw = self._get_cookie_value_by_name("login_session")
        if not raw:
            return ""
        try:
            p0 = raw.split(".")[0]
            p0 += "=" * (-len(p0) % 4)
            payload = json.loads(base64.urlsafe_b64decode(p0.encode("utf-8")).decode("utf-8"))
            return (payload.get("login_challenge", "") or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _extract_query_first(url: str, keys: list[str]) -> str:
        if not url:
            return ""
        try:
            qs = parse_qs(urlparse(url).query)
        except Exception:
            return ""
        for k in keys:
            val = qs.get(k, [None])[0]
            if val:
                return val
        return ""

    @staticmethod
    def _extract_page_type(resp_json: dict | None) -> str:
        if not isinstance(resp_json, dict):
            return ""
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        return (page.get("type", "") or "").strip()

    @staticmethod
    def _extract_continue_url_from_step(resp_json: dict | None) -> str:
        """
        Extract continue_url from an auth-step response:
        - Top-level continue_url.
        - payload.url when page.type=external_url.
        """
        if not isinstance(resp_json, dict):
            return ""
        continue_url = (resp_json.get("continue_url", "") or "").strip()
        if continue_url:
            return continue_url
        page = resp_json.get("page", {})
        if not isinstance(page, dict):
            return ""
        if (page.get("type", "") or "").strip() != "external_url":
            return ""
        payload = page.get("payload", {})
        if not isinstance(payload, dict):
            return ""
        return (payload.get("url", "") or "").strip()

    def _get_env(self, name: str, default: str = "") -> str:
        """Read configuration, preferring this flow's env_overrides over process variables.

        registrar passes AuthFlow(env_overrides=...) instead of writing os.environ, so
        concurrent account runs cannot interfere. The register_outlook.py CLI passes no
        overrides and therefore retains its previous behavior.
        """
        v = self._env_overrides.get(name)
        return os.getenv(name, default) if v is None else str(v)

    def _env_flag(self, name: str, default: str = "0") -> bool:
        # Converted from @staticmethod to an instance method so it can read self._env_overrides.
        # Every call site already uses self._env_flag(...), so the signature remains unchanged.
        return self._get_env(name, default).lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")

    def _remember_oauth_params(self, auth_url: str):
        """Remember OAuth parameters from an authorize URL for a later token exchange."""
        if not auth_url:
            return
        self._oauth_auth_url = auth_url
        try:
            qs = parse_qs(urlparse(auth_url).query)
            self._oauth_client_id = (qs.get("client_id", [self._oauth_client_id])[0] or self._oauth_client_id).strip()
            self._oauth_redirect_uri = (
                qs.get("redirect_uri", [self._oauth_redirect_uri])[0] or self._oauth_redirect_uri
            ).strip()
            self._oauth_scope = (qs.get("scope", [""])[0] or "").strip()
            self._oauth_state = (qs.get("state", [""])[0] or "").strip()
        except Exception:
            return

    def _build_pkce_pair(self, raw_bytes: int = 64) -> tuple[str, str]:
        """Generate a (code_verifier, code_challenge) pair."""
        verifier = self._b64url_no_pad(secrets.token_bytes(max(32, int(raw_bytes))))
        if len(verifier) < 43:
            verifier = (verifier + ("A" * 43))[:43]
        if len(verifier) > 128:
            verifier = verifier[:128]
        challenge = self._b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
        return verifier, challenge

    def _build_codex_authorize(self, prompt_override: Optional[str] = None) -> tuple[str, str, str, str, str]:
        """
        Build the Codex OAuth authorization URL used to obtain a refresh_token.
        Following any-auto-register, use a dedicated client_id, redirect_uri, and controlled PKCE.
        """
        client_id = (os.getenv("OAUTH_CODEX_CLIENT_ID", "") or "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
        redirect_uri = (os.getenv("OAUTH_CODEX_REDIRECT_URI", "") or "").strip() or "http://localhost:1455/auth/callback"
        scope = (os.getenv("OAUTH_CODEX_SCOPE", "") or "").strip() or "openid email profile offline_access"
        state = self._b64url_no_pad(secrets.token_bytes(24))
        verifier, challenge = self._build_pkce_pair()
        prompt = (
            (os.getenv("OAUTH_CODEX_PROMPT", "login") or "").strip()
            if prompt_override is None
            else (prompt_override or "").strip()
        )
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        if prompt:
            params["prompt"] = prompt
        auth_url = f"https://auth.openai.com/oauth/authorize?{urlencode(params)}"
        return auth_url, state, verifier, redirect_uri, client_id

    @staticmethod
    def _callback_has_code(url: str, redirect_uri: str) -> bool:
        if not url:
            return False
        try:
            cb_base = (redirect_uri or "").split("?", 1)[0].rstrip("/")
            target = url.split("?", 1)[0].rstrip("/")
            if cb_base and target == cb_base:
                qs = parse_qs(urlparse(url).query)
                return bool((qs.get("code", [""])[0] or "").strip())
        except Exception:
            return False
        return False

    def _follow_authorize_for_callback(self, start_url: str, redirect_uri: str, trace_prefix: str) -> tuple[str, str]:
        """
        Follow the auth.openai.com authorization chain and capture, but do not consume, the callback.
        Return (callback_url, final_url).
        """
        current = start_url
        callback_url = ""
        chose_account = False  # Select once per /choose-an-account chain to prevent a 200/same-URL loop
        for i in range(12):
            if self._callback_has_code(current, redirect_uri):
                callback_url = current
                break
            resp = self.session.get(
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": self._ua,
                },
                timeout=30,
                allow_redirects=False,
            )
            self._trace_http(f"{trace_prefix}_hop_{i+1}", resp)

            # When workspace/consent returns 200, select a workspace to obtain the next continue_url
            if resp.status_code == 200:
                is_workspace_like = (
                    ("/workspace" in current)
                    or ("/sign-in-with-chatgpt/" in current)
                    or ("/consent" in current)
                )
                if is_workspace_like:
                    workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                    if workspace_id:
                        next_url = self._workspace_select(workspace_id)
                        if next_url:
                            if next_url.startswith("/"):
                                next_url = urljoin("https://auth.openai.com", next_url)
                            current = next_url
                            continue

                # /choose-an-account is the react-router SSR account picker for multiple OpenAI sessions.
                # streamController.enqueue injects unified_sessions[].id (us_*) and authsess_* into the HTML.
                # The protocol client must select the first us_* or it cannot obtain the Codex callback.
                if "/choose-an-account" in current and not chose_account:
                    chose_account = True
                    next_url = self._choose_account_select(resp.text or "", current)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current = next_url
                        continue

            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = (resp.headers.get("Location", "") or "").strip()
            if not loc:
                break
            if loc.startswith("/"):
                loc = urljoin(current, loc)
            if self._callback_has_code(loc, redirect_uri):
                callback_url = loc
                current = loc
                break
            current = loc
        return callback_url, current

    @staticmethod
    def _drop_query_keys(url: str, drop_keys: set[str]) -> str:
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            params = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for (k, v) in params if (k or "").strip() not in drop_keys]
            return urlunparse(parsed._replace(query=urlencode(kept)))
        except Exception:
            return url

    def _exchange_codex_callback_code(
        self,
        callback_url: str,
        expected_state: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> bool:
        qs = parse_qs(urlparse(callback_url).query)
        code = (qs.get("code", [""])[0] or "").strip()
        got_state = (qs.get("state", [""])[0] or "").strip()
        if not code:
            logger.warning("Codex callback is missing code")
            return False
        if expected_state and got_state and got_state != expected_state:
            logger.warning("Codex callback state mismatch: expected=%s actual=%s", expected_state[:20], got_state[:20])
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "User-Agent": self._ua,
        }
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        encoded_form = urlencode(form)
        resp = self.session.post(
            "https://auth.openai.com/oauth/token",
            headers=headers,
            data=encoded_form,
            timeout=30,
        )
        self._trace_http(
            "oauth_token_exchange_codex_pkce",
            resp,
            extra_request={
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            },
        )
        if resp.status_code != 200:
            logger.warning("Codex oauth/token failed: %s - %s", resp.status_code, (resp.text or "")[:220])
            return False
        data = resp.json() if resp is not None else {}
        self.result.id_token = data.get("id_token", self.result.id_token)
        self.result.access_token = data.get("access_token", self.result.access_token)
        self.result.refresh_token = data.get("refresh_token", self.result.refresh_token)
        logger.info(
            "Codex OAuth exchange succeeded: access=%s refresh=%s",
            "present" if self.result.access_token else "absent",
            "present" if self.result.refresh_token else "absent",
        )
        return True

    def _codex_drive_login_from_log_in(self, mail_provider: Optional[MailProvider] = None) -> str:
        """
        When Codex authorization falls back to /log-in, advance the state machine with a
        direct-protocol login. Return a continue_url that can be followed, or an empty string.
        """
        email = (self.result.email or "").strip()
        if not email:
            logger.warning("Cannot advance Codex login without an email address")
            return ""
        password, pw_is_real = self._resolve_login_password(email)
        if pw_is_real:
            self.result.password = password
        else:
            # Use a guessed password only to test for 401; do not store it in result as a real password
            logger.info("No known password for this account; trying the default password rule (likely to return 401)")

        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if not device_id:
            device_id = str(uuid.uuid4())
            self.result.device_id = device_id

        sentinel = self.get_sentinel_token(device_id)
        step = self.authorize_continue(
            email=email,
            sentinel_token=sentinel,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="authorize_continue_login_codex",
        )
        page_type = self._extract_page_type(step)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        if page_type == "login_password" or "/log-in/password" in continue_url:
            step = self.login_password_verify(password)
            page_type = self._extract_page_type(step)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        # mfa-challenge path requiring TOTP 2FA after password verification
        if self._is_mfa_challenge_state(page_type, continue_url):
            totp_secret = (self.result.totp_secret or "").strip()
            if not totp_secret and self._account_callback:
                # Load credentials from the database
                try:
                    cred = self._account_callback(email)
                    if cred and cred.get("totp_secret"):
                        totp_secret = cred["totp_secret"]
                        self.result.totp_secret = totp_secret
                        logger.info("Loaded totp_secret from the database")
                except Exception as e:
                    logger.warning(f"account_callback failed: {e}")
            if not totp_secret:
                logger.warning("Cannot continue mfa-challenge without totp_secret")
                return continue_url or ""
            # Extract challenge_id from continue_url
            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
            if not challenge_id:
                logger.warning("Could not extract challenge_id from continue_url")
                return continue_url or ""
            # Compute and submit the current TOTP code
            totp_code = _totp_now(totp_secret)
            logger.info(f"Submitting TOTP code for 2FA verification (challenge_id={challenge_id[:16]}...)")
            mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(mfa_resp))

        need_otp = (page_type == "email_otp_verification") or ("/email-verification" in (continue_url or ""))
        if need_otp:
            if mail_provider is None:
                logger.warning("Codex login requires an OTP, but no mail_provider was provided")
                return continue_url or ""
            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_sent_at = time.time()
            if not self.kickoff_otp_delivery("codex_login_need_otp"):
                self.send_otp()
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            otp_resp = self.verify_otp(otp_code)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(otp_resp))

        # Optional add-phone path; advance automatically only when a phone number and code source are configured
        if self._is_add_phone_state(page_type="", continue_url=continue_url):
            next_url = self._handle_add_phone_verification(continue_url=continue_url)
            if next_url:
                continue_url = self._normalize_continue_url(next_url)

        return continue_url or ""

    @staticmethod
    def _is_add_phone_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_phone") or ("add-phone" in cu)

    @staticmethod
    def _is_mfa_challenge_state(page_type: str = "", continue_url: str = "") -> bool:
        """Return whether an existing 2FA account entered mfa-challenge after password verification."""
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "mfa_challenge") or ("/mfa-challenge/" in cu)

    def _phone_headers(self, referer: str) -> dict:
        headers = self._common_headers(referer)
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://auth.openai.com"
        device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
        if device_id:
            headers["oai-device-id"] = device_id
        return headers

    def _add_phone_send(self, phone_number: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/add-phone")
        try:
            resp = self.session.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                headers=headers,
                json={"phone_number": phone_number},
                timeout=30,
            )
        except Exception as e:
            logger.warning("[add-phone] Network error: %s (phone=%s)", e, phone_number)
            raise
        self._trace_http("add_phone_send", resp)

        if resp.status_code != 200:
            # Parse error.message when available
            try:
                data = resp.json()
                msg = data.get("error", {}).get("message", "")
                code = data.get("error", {}).get("code", "")
            except Exception:
                msg = resp.text[:150]
                code = ""
            # Raise only the message, not the full JSON, to keep upper-level logs concise
            raise RuntimeError(msg or f"HTTP {resp.status_code}")

        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def _phone_otp_resend(self) -> bool:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("phone_otp_resend", resp)
        return resp.status_code == 200

    def _phone_otp_validate(self, code: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            headers=headers,
            json={"code": code},
            timeout=30,
        )
        self._trace_http("phone_otp_validate", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"phone-otp/validate failed: {resp.status_code} - {(resp.text or '')[:220]}")
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    @staticmethod
    def _extract_otp6(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        return (m.group(1) if m else "").strip()

    def _read_phone_otp_from_cmd(self) -> str:
        """
        Read a phone verification code from stdout of the command specified by OPENAI_PHONE_OTP_CMD.
        Treat any six-digit sequence in the command output as a match.
        """
        cmd = (os.getenv("OPENAI_PHONE_OTP_CMD", "") or "").strip()
        if not cmd:
            return ""
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, timeout=20)
            return self._extract_otp6(out or "")
        except Exception:
            return ""

    def _wait_phone_otp(self, timeout: int = 180) -> str:
        static_otp = self._extract_otp6(os.getenv("OPENAI_PHONE_OTP", "") or "")
        if static_otp:
            return static_otp

        deadline = time.time() + max(20, int(timeout))
        while time.time() < deadline:
            code = self._read_phone_otp_from_cmd()
            if code:
                return code
            time.sleep(4)
        raise TimeoutError(f"Timed out waiting for phone OTP ({timeout}s)")

    def _handle_add_phone_verification(self, continue_url: str = "") -> str:
        """
        Handle the add-phone verification path:
        - Prefer self._sms_callback, which rents a number and receives the code automatically.
        - Fall back to OPENAI_PHONE_NUMBER plus OPENAI_PHONE_OTP_CMD/OPENAI_PHONE_OTP.
        """
        if self._sms_callback is not None:
            try:
                return self._handle_add_phone_via_sms(continue_url)
            except Exception as e:
                logger.warning("SMS provider flow failed; falling back to environment variables: %s", e)
                try:
                    self._sms_callback.cleanup()
                except Exception:
                    pass
        return self._handle_add_phone_via_env(continue_url)

    def _handle_add_phone_via_sms(self, continue_url: str = "") -> str:
        """Use the SMS controller: rent number -> add-phone/send -> wait for SMS -> validate.

        Supported platform: SmsBower (smsbower.page).
        Each number has an 80-second window; OpenAI resend is triggered three times at 20-second
        intervals. Failures automatically cancel the number and rent a replacement. The default
        maximum is three numbers and can be configured in WebUI or OPENAI_PHONE_MAX_ATTEMPTS.
        """
        ctrl = self._sms_callback
        try:
            ctrl.set_resend_callback(self._phone_otp_resend)
        except Exception:
            pass

        # Ensure the lock is released and final cleanup runs even if the loop raises
        try:
            return self._do_sms_loop(ctrl)
        finally:
            # Always release the lock and clean up the final number, if any
            try:
                ctrl.cleanup()
            except Exception:
                pass
            try:
                ctrl._release_lock()
            except Exception:
                pass

    def _do_sms_loop(self, ctrl) -> str:
        """Run the SMS verification loop through max_attempts."""
        # Provider information; currently only SmsBower is supported
        provider_key = (getattr(ctrl, "provider_key", "") or "").lower()

        # Prefer controller.config from the frontend, then environment variables, then defaults
        ctrl_cfg = getattr(ctrl, "config", None) or {}

        def _read_int(cfg_key: str, env_key: str, default: str, min_v: int = 1) -> int:
            raw = (str(ctrl_cfg.get(cfg_key) or "")).strip()
            if not raw:
                raw = os.getenv(env_key, default)
            try:
                return max(min_v, int(raw))
            except Exception:
                return int(default)

        # Per-number wait window in seconds; default 80 = 20×3 plus a 20-second buffer
        per_phone_timeout = max(40, _read_int(
            "sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT", "80", min_v=40
        ))
        # Maximum number attempts, default 3
        max_phone_attempts = _read_int(
            "sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS", "3"
        )
        # Validation retries for one number while time remains
        max_code_retries_per_phone = _read_int(
            "sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES", "2"
        )

        logger.info(
            "[sms] Configuration: provider=%s per-number window=%ds max number attempts=%d validation retries per number=%d",
            provider_key, per_phone_timeout, max_phone_attempts, max_code_retries_per_phone,
        )

        # Keywords for OpenAI errors indicating a used or disallowed phone number
        _PHONE_REJECTED_PATTERNS = (
            "phone_number_already_in_use", "already_in_use", "already_taken",
            "phone_already_verified", "already_verified",
            "disallowed_phone", "invalid_phone_number", "phone_number_invalid",
            "blocked_phone", "phone_number_blocked",
            "suspicious behavior from phone",  # OpenAI risk control flags a suspicious number range
        )
        def _is_phone_rejected(s: str) -> bool:
            sl = (s or "").lower()
            return any(p in sl for p in _PHONE_REJECTED_PATTERNS)

        last_err: Optional[Exception] = None

        for phone_attempt in range(1, max_phone_attempts + 1):
            logger.info("[sms] 🔁 Phone number attempt %d/%d...", phone_attempt, max_phone_attempts)

            # Stage 1: rent a number; after the first, cleanup has cleared the SmsBower cache
            try:
                phone = ctrl.get_phone()
            except Exception as e:
                last_err = e
                logger.warning("[sms] Failed to rent phone number on attempt %d: %s", phone_attempt, e)
                continue
            if not phone:
                last_err = RuntimeError("SMS controller did not return a phone number")
                continue

            # Stage 2: ask OpenAI to send a code to this number
            send_resp = None
            try:
                logger.info("[sms] 📤 Preparing POST add-phone/send (phone=%s)...", phone)
                send_resp = self._add_phone_send(phone)
                logger.info("[sms] ✅ POST add-phone/send succeeded (phone=%s)", phone)
            except Exception as e:
                err_text = str(e)
                if "too many phone verification" in err_text.lower() \
                        or "phone_verification_rate_limit" in err_text.lower():
                    logger.warning(
                        "⚠️ OpenAI rate limit: this Outlook account or IP has made too many add-phone requests. "
                        "Try another Outlook account or proxy IP. Skipping add-phone for this run (the session_token remains usable)."
                    )
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    break
                if _is_phone_rejected(err_text):
                    logger.warning("[sms] Phone number %s was rejected by OpenAI (already used or not allowed): %s",
                                   phone, err_text[:200])
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    continue
                # Log other unrecognized errors in detail without treating them as number-specific
                logger.warning("[sms] POST add-phone/send failed for %s with an unrecognized error: %s",
                               phone, err_text[:300])
                ctrl.mark_send_failed(err_text)
                last_err = e
                continue

            send_page_type = self._extract_page_type(send_resp)
            send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
            if send_page_type not in ("phone_otp_verification", "external_url") \
                    and "phone-verification" not in (send_continue or ""):
                logger.warning(
                    "add-phone/send did not reach the phone verification page: page=%s continue=%s",
                    send_page_type or "(empty)",
                    (send_continue or "")[:180],
                )
                ctrl.mark_send_failed("did not enter phone-verification page")
                last_err = RuntimeError(f"add-phone/send did not reach phone-verification: page={send_page_type}")
                continue

            ctrl.mark_send_succeeded()

            # Stage 3: wait for SMS; SmsBower calls OpenAI resend three times at 20-second intervals
            phone_start = time.time()
            seen_codes: set[str] = set()
            code_attempt = 0
            phone_used = False

            while time.time() - phone_start < per_phone_timeout and code_attempt < max_code_retries_per_phone:
                remaining = per_phone_timeout - (time.time() - phone_start)
                if remaining < 10:
                    break
                code_attempt += 1
                logger.info(
                    "[sms] Waiting for SMS for %s, attempt %d/%d (%ds remaining)",
                    phone, code_attempt, max_code_retries_per_phone, int(remaining),
                )
                code = ctrl.get_code(timeout=int(remaining))
                if not code:
                    break  # Timed out; switch numbers
                if code in seen_codes:
                    logger.warning("[sms] Received duplicate code=%s; skipping", code)
                    continue
                seen_codes.add(code)
                phone_used = True

                try:
                    validate_resp = self._phone_otp_validate(code)
                    next_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(validate_resp)
                    )
                    logger.info("[sms] ✅ phone-otp/validate succeeded (phone=%s code=%s) next=%s",
                                phone, code, (next_url or "")[:160])
                    ctrl.report_success()
                    return next_url or continue_url or ""
                except Exception as e:
                    last_err = e
                    err_text = str(e)
                    logger.warning("[sms] Validation failed (phone=%s code=%s): %s",
                                   phone, code, err_text[:200])
                    ctrl.mark_code_failed(err_text)
                    # Continue waiting for another code on the same number

            # The per-number window ended; cancel this number
            logger.warning("[sms] Phone number %s exhausted its %ds window", phone, per_phone_timeout)
            try:
                ctrl.cleanup()
            except Exception:
                pass
            # cleanup clears controller.activation so the next get_phone rents a new number

        # All numbers failed
        if last_err:
            raise last_err
        raise RuntimeError(f"SMS verification failed for all {max_phone_attempts} phone numbers")

    def _handle_add_phone_via_env(self, continue_url: str = "") -> str:
        """
        Handle add-phone verification using the legacy environment-variable path:
        - Environment variables must provide the number and verification-code source:
          - OPENAI_PHONE_NUMBER=+1...
          - OPENAI_PHONE_OTP_CMD='...returns SMS content...' or OPENAI_PHONE_OTP=123456
        """
        phone_raw = (os.getenv("OPENAI_PHONE_NUMBER", "") or "").strip()
        phone_candidates = [x.strip() for x in phone_raw.split(",") if x.strip()]
        if not phone_candidates:
            logger.warning("Reached add-phone, but no SMS provider or OPENAI_PHONE_NUMBER is configured")
            return continue_url or ""

        try:
            otp_timeout = max(30, int(os.getenv("OPENAI_PHONE_OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        last_err = ""
        for idx, phone in enumerate(phone_candidates, 1):
            try:
                logger.info("add-phone phone number attempt %s/%s: %s", idx, len(phone_candidates), phone)
                send_resp = self._add_phone_send(phone)
                send_page_type = self._extract_page_type(send_resp)
                send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
                if send_page_type not in ("phone_otp_verification", "external_url") and "phone-verification" not in (send_continue or ""):
                    logger.warning(
                        "add-phone/send did not reach the phone verification page: page=%s continue=%s",
                        send_page_type or "(empty)",
                        (send_continue or "")[:180],
                    )
                    continue

                phone_code = self._wait_phone_otp(timeout=otp_timeout)
                validate_resp = self._phone_otp_validate(phone_code)
                next_url = self._normalize_continue_url(self._extract_continue_url_from_step(validate_resp))
                logger.info("add-phone verification succeeded, next=%s", (next_url or "")[:180])
                return next_url or continue_url or ""
            except Exception as e:
                last_err = str(e)
                logger.warning("add-phone failed for phone number %s: %s", phone, e)
                try:
                    self._phone_otp_resend()
                except Exception:
                    pass

        if last_err:
            logger.warning("add-phone stage did not succeed: %s", last_err)
        return continue_url or ""

    def _codex_refresh_retry_after_add_phone(
        self,
        auth_url: str,
        redirect_uri: str,
        attempts: int = 3,
        sleep_seconds: float = 1.2,
    ) -> tuple[str, str]:
        """
        When add-phone is reached, repeatedly issue authorize requests using a refresh-retry
        strategy in hopes of reaching a path that does not require add-phone and returns a callback code.
        """
        callback_url = ""
        final_url = ""
        start_url = self._drop_query_keys(auth_url, {"prompt"}) or auth_url
        rounds = max(1, int(attempts))
        wait_s = max(0.0, float(sleep_seconds))

        for i in range(rounds):
            callback_url, final_url = self._follow_authorize_for_callback(
                start_url,
                redirect_uri,
                f"codex_add_phone_refresh_retry_{i+1}",
            )
            if callback_url:
                return callback_url, final_url
            if i < rounds - 1 and wait_s > 0:
                time.sleep(wait_s)

        return callback_url, final_url

    def oauth_codex_rt_exchange(self, mail_provider: Optional[MailProvider] = None) -> bool:
        """
        Obtain a refresh token through direct protocol calls, following any-auto-register:
        - Reauthorize with dedicated Codex OAuth parameters and controlled PKCE.
        - Capture the callback code without consuming it.
        - Exchange it directly at /oauth/token for access_token and refresh_token.
        """
        allow_retry = self._env_flag("OAUTH_CODEX_RT_ALLOW_RETRY", "0")
        if self._codex_rt_attempted and (not allow_retry):
            logger.debug("Codex RT was already attempted in this run; skipping duplicate attempt")
            return False
        self._codex_rt_attempted = True

        logger.info("Attempting direct Codex OAuth exchange for refresh_token...")
        try:
            auth_url, state, verifier, redirect_uri, client_id = self._build_codex_authorize()
            self._oauth_auth_url = auth_url
            self._oauth_client_id = client_id
            self._oauth_redirect_uri = redirect_uri
            self._oauth_state = state
            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            callback_url, final_url = self._follow_authorize_for_callback(
                auth_url, redirect_uri, "codex_authorize"
            )

            # If redirected to /log-in, complete one protocol-login step before continuing authorization
            if (not callback_url) and "/log-in" in (final_url or ""):
                logger.info("Codex authorization returned to /log-in; attempting to advance the login flow...")
                continue_url = ""
                try:
                    continue_url = self._codex_drive_login_from_log_in(mail_provider=mail_provider)
                except Exception as e:
                    logger.warning(f"Failed to advance Codex login; falling back to no-prompt authorization: {e}")
                if continue_url:
                    # On add-phone, retry authorization instead of giving up immediately
                    if self._is_add_phone_state(page_type="", continue_url=continue_url) and self._env_flag(
                        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "1"
                    ):
                        try:
                            retry_count = max(1, int(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY_COUNT", "3")))
                        except Exception:
                            retry_count = 3
                        try:
                            retry_sleep = max(0.0, float(os.getenv("OAUTH_CODEX_ADD_PHONE_REFRESH_SLEEP", "1.2")))
                        except Exception:
                            retry_sleep = 1.2
                        logger.info("Reached add-phone; retrying authorize: count=%s sleep=%.1fs", retry_count, retry_sleep)
                        callback_url, final_url = self._codex_refresh_retry_after_add_phone(
                            auth_url=auth_url,
                            redirect_uri=redirect_uri,
                            attempts=retry_count,
                            sleep_seconds=retry_sleep,
                        )
                    else:
                        callback_url, final_url = self._follow_authorize_for_callback(
                            continue_url,
                            redirect_uri,
                            "codex_post_login",
                        )

            # If Codex authorize reaches /add-phone directly, use the SMS controller to bind
            # a phone number before authorizing again
            if (not callback_url) and self._is_add_phone_state(page_type="", continue_url=final_url or "") \
                    and self._sms_callback is not None:
                logger.info("Codex authorization reached /add-phone; attempting phone verification through the SMS provider...")
                try:
                    self._handle_add_phone_via_sms(continue_url=final_url)
                    # Reauthorize for a callback code after successful phone binding
                    callback_url, final_url = self._follow_authorize_for_callback(
                        auth_url, redirect_uri, "codex_authorize_after_add_phone"
                    )
                    if not callback_url:
                        no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                        if no_prompt_url and no_prompt_url != auth_url:
                            callback_url, final_url = self._follow_authorize_for_callback(
                                no_prompt_url,
                                redirect_uri,
                                "codex_authorize_noprompt_after_add_phone",
                            )
                except Exception as e:
                    logger.warning(f"SMS phone verification failed: {e}")

            # Fallback: remove prompt=login and authorize once more
            if not callback_url:
                no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                if no_prompt_url and no_prompt_url != auth_url:
                    callback_url, final_url = self._follow_authorize_for_callback(
                        no_prompt_url,
                        redirect_uri,
                        "codex_authorize_noprompt",
                    )

            if not callback_url:
                logger.debug("Codex OAuth did not capture a callback code, final=%s", (final_url or "")[:180])
                return False
            return self._exchange_codex_callback_code(
                callback_url=callback_url,
                expected_state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        except Exception as e:
            logger.warning(f"Codex OAuth exchange error: {e}")
            return False

    def _inject_pkce_into_auth_url(self, auth_url: str) -> str:
        """Optionally inject PKCE parameters into an authorize URL."""
        if not auth_url:
            return auth_url
        if not self._env_flag("OAUTH_SECONDARY_PKCE", "0"):
            return auth_url

        try:
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            if params.get("code_challenge") and params.get("code_challenge_method"):
                return auth_url

            verifier, challenge = self._build_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            new_url = urlunparse(parsed._replace(query=urlencode(params)))
            # Inject this run's verifier if the user did not supply one manually
            if not self._manual_login_verifier:
                self._manual_login_verifier = verifier
            logger.info(
                "Secondary PKCE injection enabled: verifier_len=%s challenge=%s...",
                len(verifier),
                challenge[:16],
            )
            return new_url
        except Exception as e:
            logger.warning(f"Failed to inject PKCE parameters; using the original auth_url: {e}")
            return auth_url

    @staticmethod
    def _safe_b64url_decode_text(data: str) -> str:
        if not data:
            return ""
        try:
            s = data + "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(s.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_hydra_redirect_values(self) -> list[str]:
        """Extract possible session values from the hydra_redirect cookie."""
        raw = self._get_cookie_value_by_name("hydra_redirect")
        if not raw:
            return []
        out: list[str] = []
        try:
            p0 = (raw.split(".", 1)[0] or "").strip()
            text = self._safe_b64url_decode_text(p0)
            if text:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    for v in obj.values():
                        if isinstance(v, str) and v.strip():
                            vv = v.strip()
                            out.append(vv)
                            if "|" in vv:
                                out.extend([x for x in vv.split("|") if isinstance(x, str) and x.strip()])
        except Exception:
            return out
        return out

    def _collect_code_verifier_candidates(self, callback_url: str, continue_url: str) -> list[tuple[str, str]]:
        """Collect code_verifier candidates as source/value pairs."""
        raw_candidates: list[tuple[str, str]] = [
            ("query", self._extract_query_first(continue_url, ["login_verifier", "code_verifier", "verifier"])),
            ("query_callback", self._extract_query_first(callback_url, ["login_verifier", "code_verifier", "verifier"])),
            ("dump", self._dump_login_verifier),
            ("captured", self._captured_login_verifier),
            ("manual", self._manual_login_verifier),
            ("cookie_login_verifier", self._get_cookie_value_by_name("login_verifier")),
            ("cookie_code_verifier", self._get_cookie_value_by_name("code_verifier")),
            ("cookie_login_challenge", self._extract_login_challenge_from_cookie()),
            ("cookie_nextauth_state", self._get_cookie_value_by_name("__Secure-next-auth.state")),
        ]

        # hydra_redirect may contain an encoded csrf/session string; include it as an experimental candidate
        for i, hv in enumerate(self._extract_hydra_redirect_values()):
            raw_candidates.append((f"hydra_{i}", hv))

        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        max_len = max(128, int(os.getenv("OAUTH_MAX_VERIFIER_LEN", "4096")))
        for src, val in raw_candidates:
            v = (val or "").strip()
            if not v:
                continue
            if len(v) > max_len:
                v = v[:max_len]
            if v not in seen:
                seen.add(v)
                out.append((src, v))
            # Standard PKCE length is 43-128; add a truncated form for oversized candidates
            if len(v) > 128:
                v128 = v[:128]
                if v128 not in seen:
                    seen.add(v128)
                    out.append((f"{src}_trunc128", v128))

        return out

    def _rotate_impersonate_session(self) -> bool:
        """Retry with another UA fingerprint version within the curl_cffi family and update the UA.

        Client hints in self._fingerprint must change with it. The old implementation updated
        only self._ua and the session, while _common_headers and _navigation_headers read all
        sec-ch-ua* values from self._fingerprint. That produced a UA reporting Chrome/136 while
        sec-ch-ua reported v=146, including mismatched not_a_brand variants
        ("Not.A/Brand";v="99" / "Not/A)Brand";v="8" / "Not?A_Brand";v="99").
        Cloudflare can easily detect this contradiction. It previously remained hidden only
        because this path was rarely reached.

        fallback_impersonates contains only members of the same family (see each _gen_* in
        fingerprint.py), so rotation remains chrome-to-chrome or safari-to-safari. Even within
        a family, changing versions still requires synchronized headers.
        """
        if self._impersonate_idx >= len(self._impersonate_candidates) - 1:
            return False
        self._impersonate_idx += 1
        imp = self._impersonate_candidates[self._impersonate_idx]
        self._ua = ua_for_impersonate(imp, self._ua)
        # Update client hints to match the new version and keep the UA and headers consistent
        try:
            self._fingerprint = fingerprint_for_impersonate(imp, self._fingerprint)
        except Exception as e:  # Prefer the old fingerprint over breaking the flow
            logger.warning(f"Failed to synchronize client hints; keeping the previous fingerprint: {e}")
        logger.warning(f"TLS error; retrying with another fingerprint: impersonate={imp}, ua={self._ua[:60]}...")
        self.session = create_http_session(
            proxy=self.config.proxy, impersonate=imp, user_agent=self._ua,
        )
        return True

    @staticmethod
    def _datadog_trace_headers() -> dict:
        """Generate Datadog RUM trace headers matching the gptfree-register format."""
        tid = f"{random.getrandbits(64):016x}"
        sid = str(random.getrandbits(63))
        pid = str(random.getrandbits(63))
        ts_hex = f"{int(time.time()):08x}"
        return {
            "traceparent": f"00-0000000000000000{tid}-{random.getrandbits(64):016x}-01",
            "x-datadog-trace-id": sid,
            "x-datadog-parent-id": pid,
            "x-datadog-sampling-priority": "1",
            "x-datadog-origin": "rum",
            "x-datadog-tags": f"_dd.p.id={tid},_dd.p.tid={ts_hex}00000000,_dd.b.sr=1",
        }

    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        Build common request headers.

        Key points:
        - Origin must share an origin with Referer, especially for auth.openai.com state-machine
          endpoints, or invalid_state and risk-control paths may trigger.
        - Add oai-device-id on auth.openai.com whenever possible to preserve state continuity.
        - Add Datadog trace headers to every request to avoid silent OTP drops.
        """
        origin = "https://chatgpt.com"
        try:
            parsed = urlparse(referer or "")
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        fp = self._fingerprint
        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "User-Agent": self._ua,
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "priority": "u=1, i",
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            # Complete Client Hints set; populated only for Chromium and omitted for other browsers
            if fp.get("sec_ch_ua_full_version_list"):
                headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
            if fp.get("sec_ch_ua_arch"):
                headers["sec-ch-ua-arch"] = fp["sec_ch_ua_arch"]
            if fp.get("sec_ch_ua_bitness"):
                headers["sec-ch-ua-bitness"] = fp["sec_ch_ua_bitness"]
            if fp.get("sec_ch_ua_model"):
                headers["sec-ch-ua-model"] = fp["sec_ch_ua_model"]
            if fp.get("sec_ch_ua_platform_version"):
                headers["sec-ch-ua-platform-version"] = fp["sec_ch_ua_platform_version"]

        # Add the device identifier to auth.openai.com requests when available
        try:
            host = (urlparse(origin).netloc or "").lower()
        except Exception:
            host = ""
        if "auth.openai.com" in host:
            device_id = (self.result.device_id or "").strip() or (self.session.cookies.get("oai-did", "") or "").strip()
            if device_id:
                headers["oai-device-id"] = device_id

        headers.update(self._datadog_trace_headers())
        return headers

    def _navigation_headers(self) -> dict:
        """Build document-navigation headers, including client hints.

        It differs from _common_headers only in Sec-Fetch-*: that method represents XHR
        (empty/cors/same-origin), while this one represents full-page navigation
        (document/navigate/none plus user and UIR). Client hints must match in both and come
        from self._fingerprint. Chrome sends the complete set; Safari and Firefox send none,
        matching real browsers. The old warmup headers omitted these values, causing four of
        five Chrome requests to return 403; all five passed after adding them.
        """
        fp = self._fingerprint
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "priority": "u=0, i",
            "User-Agent": self._ua,
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            for key, name in (
                ("sec_ch_ua_full_version_list", "sec-ch-ua-full-version-list"),
                ("sec_ch_ua_arch", "sec-ch-ua-arch"),
                ("sec_ch_ua_bitness", "sec-ch-ua-bitness"),
                ("sec_ch_ua_model", "sec-ch-ua-model"),
                ("sec_ch_ua_platform_version", "sec-ch-ua-platform-version"),
            ):
                if fp.get(key):
                    headers[name] = fp[key]
        return headers

    def warmup(self) -> bool:
        """GET chatgpt.com to set all cookies, including oai-did; return True on success.

        Why this step cannot fail (26 runs across more than 40 exit IPs on 2026-08-10):
        `POST /api/auth/signin/openai` chooses its response from chatgpt.com cookies. With oai-did,
        it returns an auth.openai.com/authorize URL; without it, it returns a NextAuth page that
        inevitably produces 409 invalid_state at authorize/continue. All five runs without oai-did
        returned 409, while only three of seventeen runs with oai-did did so.

        The old implementation had two problems, each accounting for roughly half the failures:
        1. One attempt with timeout=15. Cookie setup failed 19% of the time through TLS curl(35)
           disconnects, 15-second timeouts, or CF 403. Successful runs took 3.4-10.9 seconds, so
           15 seconds was marginal and 40 seconds provides adequate headroom.
        2. The return value did not match the result because the code caught exceptions without
           checking status_code. A 403 still returned True in three runs with no cookie, while one
           timed-out run had already set the cookie but returned False. The cookie jar is therefore
           the only reliable success signal.

        3. Claiming Chrome without client hints or sec-ch-ua is visibly inconsistent to Cloudflare
           and caused the 403s. Real Chrome sends sec-ch-ua/-mobile/-platform on every navigation.
           The old hand-built warmup headers omitted all of them, although _common_headers included
           them. Five tests per impersonation with a new IP on 2026-08-10 produced:

               impersonate   bare headers   full CH set
               chrome146      1/5        5/5
               chrome136      1/5        5/5
               chrome142      4/5        4/5   <- the only failure was an SSL disconnect, not 403

           Adding the headers eliminated every 403. The earlier conclusion that Cloudflare blocked
           the Chrome family was wrong. Safari and Firefox passed 4/4 because they correctly send
           no client hints. The fix is complete headers, not switching to Safari; changing families
           would make self._fingerprint inconsistent with self._ua.

        Retries change only the exit IP, not the fingerprint. The proxy pool assigns exits by session,
        so a new session approximates a new IP. Clearing cookies with a failed session is correct.

        Keep the URL at `/`. Tests against `/auth/login` showed no meaningful difference.

        Final verification on 2026-08-10: after all three navigation paths used _navigation_headers,
        three of three full run_register attempts succeeded with no 409 responses.
        """
        headers = self._navigation_headers()

        for attempt in range(4):
            if attempt:
                # Change only the exit IP and preserve the fingerprint; missing client hints caused the 403
                time.sleep(3 + attempt * 2)
                self.session = create_http_session(
                    proxy=self.config.proxy,
                    impersonate=self._impersonate_candidates[self._impersonate_idx],
                    user_agent=self._ua,
                )
            try:
                resp = self.session.get(
                    "https://chatgpt.com", headers=headers, timeout=40,
                )
                status = resp.status_code
            except Exception as e:
                status = None
                logger.warning(f"Warmup request {attempt + 1}/4 failed: {e}")

            # The only success criterion is whether the cookie was set; status and exceptions are inconclusive
            try:
                cookies = self.session.cookies.get_dict()
            except Exception:
                cookies = {}
            if "oai-did" in cookies:
                logger.info(
                    f"chatgpt.com warmup completed on attempt {attempt + 1}; oai-did was set "
                    f"with {len(cookies)} cookies total"
                )
                return True

            logger.warning(
                f"Warmup attempt {attempt + 1}/4 did not set oai-did"
                + (f" (HTTP {status})" if status is not None else "")
                + (f"; existing cookies: {sorted(cookies)}" if cookies else "; no cookies present")
            )

        logger.error("All 4 warmup attempts failed to set the oai-did cookie; continuing will result in 409 invalid_state")
        return False

    # ── Step 1: Check proxy connectivity ──
    def check_proxy(self) -> bool:
        logger.info("Checking network connectivity...")
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                country_code = loc.group(1) if loc else ""
                logger.info(f"Network is available - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"region: {country_code or 'N/A'}")

                # Regenerate the fingerprint after detecting a country code to align timezone and language
                if country_code and country_code != self._country_code:
                    self._country_code = country_code
                    import random
                    session_seed = id(self.session) % (2**32)
                    rng = random.Random(session_seed)
                    self._fingerprint = generate_fingerprint(rng=rng, country_code=country_code)
                    self._ua = self._fingerprint["user_agent"]
                    new_imp = self._fingerprint["impersonate"]
                    self._impersonate_candidates = self._fingerprint.get(
                        "fallback_impersonates",
                        [new_imp, "safari17_0", "safari15_5"],
                    )
                    self._impersonate_idx = 0
                    self.session = create_http_session(
                        proxy=self.config.proxy,
                        impersonate=new_imp,
                        user_agent=self._ua,
                    )
            else:
                logger.warning(f"Network probe returned an unexpected response: Cloudflare trace {resp.status_code}")

            return True
        except Exception as e:
            logger.error(f"Network check failed: {e}")
        return False

    # ── Step 2: Get the CSRF token ──
    def get_csrf_token(self) -> str:
        logger.info("[1/10] Getting CSRF token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare may return 403 after rapid requests; retry up to three times
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "The TLS handshake with chatgpt.com failed, so the current network cannot establish an HTTPS "
                        "connection to /api/auth/csrf. Switch to a network that can reach chatgpt.com directly, or "
                        "configure a working proxy in the UI and retry."
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare returned 403; retrying in {wait}s ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        self._trace_http("chatgpt_csrf", resp)
        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("Failed to get CSRF token")
        self.result.csrf_token = csrf
        logger.debug(f"CSRF Token: {csrf[:20]}...")
        return csrf

    # ── Step 3: Get the authorization URL ──
    def get_auth_url(self, csrf_token: str, email: str = "") -> str:
        logger.info("[2/10] Getting OpenAI authorization URL...")
        headers = self._common_headers("https://chatgpt.com/auth/login")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if not self.result.device_id:
            self.result.device_id = str(uuid.uuid4())
        query_params: dict[str, str] = {
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "ext-oai-did": self.result.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "1111",
        }
        if email:
            query_params["login_hint"] = email
        signin_url = f"https://chatgpt.com/api/auth/signin/openai?{urlencode(query_params)}"
        resp = self.session.post(
            signin_url,
            headers=headers,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._trace_http("chatgpt_signin_openai", resp)
        auth_url = resp.json().get("url", "")
        if not auth_url:
            raise RuntimeError("Failed to get authorization URL")
        # Remember OAuth parameters and optionally inject PKCE according to configuration
        self._remember_oauth_params(auth_url)
        auth_url = self._inject_pkce_into_auth_url(auth_url)
        self._remember_oauth_params(auth_url)
        logger.debug(f"Auth URL: {auth_url[:80]}...")
        return auth_url

    # ── Step 4: Initialize OAuth and get device_id ──
    def auth_oauth_init(self, auth_url: str) -> str:
        """Follow the authorize chain, establish session state, and retrieve oai-did.

        This establishes the state later consumed by authorize/continue. Headers that do not
        resemble a real browser fail to establish valid state and cause 409 invalid_state.

        The old implementation sent only Accept/Referer/UA and omitted client hints and every
        Sec-Fetch-* header, although real navigation includes document/navigate/cross-site.
        Six-run A/B tests on 2026-08-10 treated 400 invalid_username as valid session state:

            A: bare headers       valid session 2/6, 409 = 3
            B: CH + SecFetch      valid session 5/6, 409 = 0

        This was the same defect as warmup, but the earlier fix covered only warmup. Deriving
        headers from _navigation_headers keeps client hints consistent with self._fingerprint and self._ua.
        """
        logger.info("[3/10] Initializing OAuth...")
        headers = self._navigation_headers()
        headers["Referer"] = "https://chatgpt.com/"
        # chatgpt.com -> auth.openai.com is a cross-site redirect, not initial direct navigation
        headers["sec-fetch-site"] = "cross-site"
        # A browser does not send sec-fetch-user for automatic 302 following
        headers.pop("sec-fetch-user", None)
        resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
        self._trace_http("auth_oauth_init", resp)

        # Read oai-did from cookies
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self.session.cookies.get("oai-did", "")
                break

        # curl_cffi cookie access path
        if not device_id:
            try:
                device_id = self.session.cookies.get("oai-did", "")
            except Exception:
                pass

        # Fallback: extract from HTML
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text)
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"Response did not include device_id; using generated value: {device_id}")

        self.result.device_id = device_id
        logger.debug(f"Device ID: {device_id}")
        return device_id

    # ── Step 5: Get the Sentinel token ──
    def _sentinel_fp_kwargs(self) -> dict:
        """Extract fingerprint and hardware fields required by Sentinel.

        Ensure all Sentinel calls use one consistent UA/platform/vendor/hardware profile throughout.
        """
        fp = self._fingerprint or {}
        return {
            "user_agent": self._ua,
            "sec_ch_ua": fp.get("sec_ch_ua", ""),
            "sec_ch_ua_platform": fp.get("sec_ch_ua_platform", ""),
            "sec_ch_ua_mobile": fp.get("sec_ch_ua_mobile", ""),
            # Complete Client Hints set, populated only for Chromium
            "sec_ch_ua_full_version_list": fp.get("sec_ch_ua_full_version_list", ""),
            "sec_ch_ua_arch": fp.get("sec_ch_ua_arch", ""),
            "sec_ch_ua_bitness": fp.get("sec_ch_ua_bitness", ""),
            "sec_ch_ua_model": fp.get("sec_ch_ua_model", ""),
            "sec_ch_ua_platform_version": fp.get("sec_ch_ua_platform_version", ""),
            "screen": fp.get("screen", ""),
            "lang": fp.get("lang", ""),
            "lang_full": fp.get("lang_full", ""),
            "browser_type": fp.get("browser_type", ""),
            "navigator_platform": fp.get("navigator_platform", ""),
            "navigator_vendor": fp.get("navigator_vendor"),
            "hardware_concurrency": fp.get("hardware_concurrency", 0),
            "device_memory": fp.get("device_memory"),
            "max_touch_points": fp.get("max_touch_points", 0),
            "device_pixel_ratio": fp.get("device_pixel_ratio", 0.0),
            "timezone": fp.get("timezone", ""),  # Timezone aligned with the IP
        }

    def get_sentinel_token(self, device_id: str) -> str:
        logger.info("[4/10] Getting Sentinel token (PoW)...")
        from sentinel import get_sentinel_token
        result = get_sentinel_token(
            self.session,
            device_id=device_id,
            flow="authorize_continue",
            **self._sentinel_fp_kwargs(),
        )
        token, so_token = result
        self._last_sentinel_token = token or ""
        self._last_sentinel_so_token = so_token or ""
        logger.debug("Sentinel token acquired")
        return token

    # ── Step 6: Submit the registration email ──
    def authorize_continue(
        self,
        email: str,
        sentinel_token: str,
        screen_hint: str = "signup",
        referer: str = "https://auth.openai.com/create-account",
        trace_step: str = "",
    ) -> dict:
        """Call /api/accounts/authorize/continue and return its JSON response."""
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        payload = {
            "username": {"value": email, "kind": "email"},
            "screen_hint": screen_hint,
        }
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._trace_http(trace_step or f"authorize_continue_{screen_hint}", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:360]
            # Log headers/req_id to help diagnose IP-based risk controls
            req_id = (resp.headers.get("x-request-id", "") or "")[:80]
            ct = (resp.headers.get("Content-Type", "") or "")[:60]
            logger.error(
                "authorize/continue returned non-200 status: status=%s screen_hint=%s req_id=%s content_type=%s body=%r",
                resp.status_code, screen_hint, req_id, ct, body,
            )
            raise RuntimeError(
                f"authorize/continue failed (screen_hint={screen_hint}): "
                f"HTTP {resp.status_code} req_id={req_id} body={body}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}

    def signup(self, email: str, sentinel_token: str) -> bool:
        """Submit an email; return True for signup or False for an existing-account OTP login."""
        logger.info("[5/10] Submitting registration email...")
        data = self.authorize_continue(
            email=email,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            trace_step="authorize_continue_signup",
        )

        # Use page_type/continue_url to distinguish new and existing accounts
        try:
            page = (data.get("page") or {}) if isinstance(data, dict) else {}
            page_type = (page.get("type") or "").strip()
            payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
            continue_url = (data.get("continue_url") or "").strip()

            # Standard new-account path
            if page_type == "create_account_password" or "/create-account/password" in continue_url:
                self._is_existing_account = False
                self._existing_email_verification_mode = ""
                self._existing_page_type = page_type
                logger.info("Registration email submitted")
                return True

            # OTP verification path for passwordless signup or an existing-account login
            if page_type == "email_otp_verification":
                mode = (payload.get("email_verification_mode", "") or "").strip()
                self._existing_email_verification_mode = mode
                self._existing_page_type = page_type
                if mode == "passwordless_signup":
                    logger.info("Server selected passwordless signup for a new account; waiting for OTP")
                    self._is_existing_account = False
                else:
                    logger.info("Existing account detected; switching to OTP login flow")
                    self._is_existing_account = True
                return False

            # Treat unknown page types as existing accounts to avoid register_password invalid_state
            self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
            self._existing_page_type = page_type
            self._is_existing_account = True
            logger.warning(
                "authorize/continue returned a nonstandard signup page: page_type=%s continue_url=%s; treating it as an existing account",
                page_type or "(empty)",
                continue_url[:180] or "(empty)",
            )
            return False
        except Exception:
            # Conservatively treat JSON parsing failure as a new registration
            self._is_existing_account = False
            self._existing_email_verification_mode = ""
            self._existing_page_type = ""
            logger.info("Registration email submitted")
            return True

    # ── Step 6.5: Register the password ──
    def register_password(self, email: str) -> bool:
        logger.info("[5.5/10] Registering password...")
        password = self._random_password()
        self.result.password = password

        # Visit create-account/password first; HAR confirms this establishes required server state
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._common_headers("https://auth.openai.com/create-account"),
                timeout=15,
            )
            logger.info(f"create-account/password page: {pw_page.status_code}")
        except Exception as e:
            logger.warning(f"Failed to open create-account/password page: {e}")

        # Refresh the Sentinel token before registration using flow=username_password_create
        #
        # Decide whether to include the SO token only for this request; never overwrite the instance's
        # _last_sentinel_so_token. Later send_otp/verify_otp steps reuse the instance field without
        # refreshing Sentinel. This flow needs no SO token, so writing back its empty value would
        # remove the header from later flows that require it.
        so_token_for_request = getattr(self, "_last_sentinel_so_token", "")
        if self.result.device_id:
            try:
                from sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="username_password_create",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                so_token_for_request = so_token or ""
                if so_token:
                    self._last_sentinel_so_token = so_token
                logger.debug("Sentinel token acquired")
            except Exception as e:
                # The server does not issue an SO block for username_password_create (verified
                # 2026-08-06; see sentinel_quickjs.py), so an empty so_token is normal. This exception
                # catches genuine network or subprocess errors. Reaching it means reusing a token
                # from a different flow, which is a risk signal, but continuing is preferable to
                # preventing the POST entirely.
                logger.warning(
                    f"Failed to refresh the Sentinel token before registration; submitting with the existing token from a different flow: {e}"
                )

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        # If this flow has no SO block, omit the header to match a real browser; do not substitute
        # an SO token from another flow stored on the instance
        if so_token_for_request:
            headers["openai-sentinel-so-token"] = so_token_for_request
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": email},
            timeout=30,
        )
        self._trace_http("register_password", resp)
        if resp.status_code != 200:
            logger.warning(f"Password registration returned {resp.status_code}: {resp.text[:200]}")
            return False
        logger.info("Password registration succeeded")
        # OpenAI has now created the account with this password, but send-code, verify-OTP, and
        # create_account still remain before save_registered. Any failure would lose the generated
        # in-memory password when the process exits, leaving an inaccessible account. Persist it
        # immediately. The callback intentionally runs after POST 200 because a failed POST means
        # the generated password never took effect.
        if self._on_password is not None:
            try:
                self._on_password(email, password)
            except Exception as e:
                logger.warning(f"Password persistence callback failed (registration can continue): {e}")
        return True

    # ── Step 7: Send OTP ──
    def send_otp(self, referer: str = "https://auth.openai.com/create-account/password"):
        logger.info(f"[6/10] Sending OTP (referer={referer.split('/')[-1]})...")
        headers = self._common_headers(referer)
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        # zhuce6 uses GET /api/accounts/email-otp/send
        resp = self.session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to send OTP: {resp.status_code} - {resp.text[:200]}")
        logger.info("OTP sent to email")

    def send_passwordless_otp(self, referer: str = "https://auth.openai.com/create-account/password") -> bool:
        """
        Send a passwordless OTP; create-account/password can trigger this path.
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_passwordless_otp", resp)
        if resp.status_code == 200:
            logger.info("Passwordless OTP sent")
            return True
        logger.warning(f"Failed to send passwordless OTP: {resp.status_code} - {(resp.text or '')[:220]}")
        return False

    def resend_otp(self, referer: str = "https://auth.openai.com/email-verification") -> bool:
        """
        Resend an OTP for an existing-account passwordless/login_challenge path.
        Return True when the request succeeds.
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("resend_email_otp", resp)
        if resp.status_code == 200:
            logger.info("OTP resent")
            return True
        logger.warning(f"Failed to resend OTP: {resp.status_code} - {(resp.text or '')[:200]}")
        return False

    def kickoff_otp_delivery(self, mode: str = "") -> bool:
        """
        Unified OTP delivery strategy, selecting the referer from the mode hint:

        - New signup (create-account/password state): passwordless/send-otp -> email-otp/send.
        - Existing account / passwordless_login / existing_*: send_otp with email-verification
          referer -> resend_otp, avoiding passwordless/send-otp's 409 invalid_state.
        """
        mode_lc = (mode or "").strip().lower()
        is_existing = (
            "existing" in mode_lc
            or "passwordless_login" in mode_lc
            or "passwordless_signup" in mode_lc  # OpenAI assigns this mode to Outlook mailbox pools
            or self._is_existing_account
        )

        if is_existing:
            # authorize/continue already triggered delivery for existing-account passwordless paths.
            # Only resend may be used because it preserves the challenge state. send_otp creates a
            # new challenge and invalidates the code already being delivered, causing wrong_email_otp_code.
            if self.resend_otp("https://auth.openai.com/email-verification"):
                return True
            # If resend fails, create a new challenge with send_otp because the old state is unusable
            logger.warning(f"Existing-account resend failed; falling back to send_otp with a new challenge (the previous email code will expire)")
            try:
                self.send_otp(referer="https://auth.openai.com/email-verification")
                return True
            except Exception as e:
                logger.warning(f"All existing-account OTP delivery methods failed: {e}")
                return False

        # New registration, original sequence
        if self.send_passwordless_otp("https://auth.openai.com/create-account/password"):
            return True
        if self.resend_otp("https://auth.openai.com/email-verification"):
            return True
        try:
            self.send_otp()
            return True
        except Exception as e:
            logger.warning(f"send_otp fallback failed (mode={mode_lc or 'unknown'}): {e}")
            return False

    @staticmethod
    def _default_password_from_email(email: str) -> str:
        """Return a guessed password, not the account's known password.

        Use it only when no real password is available, in case an early account used this rule.
        Callers must use _resolve_login_password rather than calling this method directly.
        """
        pwd = (email or "").replace("@", "")
        if len(pwd) < 8:
            pwd = f"{pwd}2026OpenAI"
        return pwd

    def _resolve_login_password(self, email: str) -> tuple[str, bool]:
        """Resolve the login password and return (password, is_known_password).

        Known-password sources in priority order:
            1. self.result.password, set by register_password in this run.
            2. LOGIN_PASSWORD, manually supplied through the environment.
            3. account_callback, loaded from the database and required for rerunning old accounts.
        Only when all are empty does it fall back to _default_password_from_email and return False.

        Never write a guessed password to self.result.password. to_dict() would persist it as real,
        and registrar's error fallback would tell the user to keep it. A passwordless legacy account
        once received an email-derived guess despite never having a password. Callers must assign
        the result only when is_known_password is true.
        """
        pwd = (self.result.password or "").strip()
        if pwd:
            return pwd, True
        pwd = (os.getenv("LOGIN_PASSWORD", "") or "").strip()
        if pwd:
            return pwd, True
        if self._account_callback:
            try:
                cred = self._account_callback(email) or {}
                pwd = (cred.get("password") or "").strip()
                if pwd:
                    logger.info("Loaded password from the database")
                    return pwd, True
            except Exception as e:
                logger.warning(f"account_callback failed to load password: {e}")
        return self._default_password_from_email(email), False

    @staticmethod
    def _random_password(length: int = 16) -> str:
        import string
        upper = string.ascii_uppercase
        lower = string.ascii_lowercase
        digits = string.digits
        special = "!@#$%^&*"
        must = [
            random.choice(upper),
            random.choice(lower),
            random.choice(digits),
            random.choice(special),
        ]
        all_chars = upper + lower + digits + special
        rest = random.choices(all_chars, k=length - len(must))
        pwd_list = must + rest
        random.shuffle(pwd_list)
        return "".join(pwd_list)

    def login_password_verify(self, password: str) -> dict:
        """Perform the /password/verify step for an existing account."""
        headers = self._common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": password},
            timeout=30,
        )
        self._trace_http("login_password_verify", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"Password login failed: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 7.5: Submit the TOTP 2FA code ──
    def submit_mfa_totp(self, totp_code: str, challenge_id: str) -> dict:
        """Submit a TOTP code after an existing-account login enters mfa-challenge.

        Args:
            totp_code: Six-digit TOTP code.
            challenge_id: Challenge ID extracted from continue_url, such as /mfa-challenge/6a76f2e8...

        Returns:
            Server response containing the continue_url that points to the callback.
        """
        headers = self._common_headers("https://auth.openai.com/mfa-challenge")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/mfa/verify",
            headers=headers,
            json={"code": totp_code, "type": "totp", "id": challenge_id},
            timeout=30,
        )
        self._trace_http("submit_mfa_totp", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:260]
            raise RuntimeError(f"TOTP verification failed: {resp.status_code} - {body}")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 8: Verify OTP ──
    def verify_otp(self, otp_code: str) -> dict:
        logger.info("[7/10] Verifying OTP...")
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": otp_code},
            timeout=30,
        )
        self._trace_http("validate_email_otp", resp)
        if resp.status_code != 200:
            body = (resp.text or "")
            logger.warning(f"verify_otp FULL body ({resp.status_code}): {body[:2000]}")
            raise RuntimeError(f"OTP verification failed: {resp.status_code} - {body[:260]}")
        logger.info("OTP verification succeeded")
        try:
            return resp.json()
        except Exception:
            return {}

    # ── Step 9: Create the account ──
    def create_account(self) -> str:
        logger.info("[8/10] Creating account...")
        # Refresh the Sentinel token with the create_account flow before account creation
        if self.result.device_id:
            try:
                from sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="oauth_create_account",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                self._last_sentinel_so_token = so_token or ""
                logger.debug("Sentinel token acquired")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"Failed to refresh the Sentinel token before account creation: {e}")
        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        _FIRST = ["James", "John", "Robert", "Michael", "William", "David", "Richard",
                  "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
                  "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
        _LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
                 "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas"]
        name = f"{random.choice(_FIRST)} {random.choice(_LAST)}"
        birthdate = f"{random.randint(1985, 2000)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=headers,
            json={"name": name, "birthdate": birthdate},
            timeout=30,
        )
        self._trace_http("create_account", resp)
        if resp.status_code != 200:
            body = (resp.text or "")[:500]
            logger.error("Account creation failed: http=%s body=%s", resp.status_code, body)
            raise RuntimeError(f"Account creation failed: {resp.status_code} - {body[:260]}")
        data = resp.json()
        continue_url = data.get("continue_url", "")
        self._sniff_login_verifier(continue_url, "create_account_continue_url")

        # Attempt workspace selection
        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            raise RuntimeError("Account creation response did not include continue_url")

        logger.info("Account created successfully")
        return continue_url

    def _extract_workspace_id(self) -> str:
        """Extract workspace_id from cookies."""
        try:
            auth_session = self.session.cookies.get("oai-client-auth-session", "")
            if auth_session:
                parts = auth_session.split(".")
                # Support cookie variants with workspace_id in either segment or workspaces[0].id
                for idx in range(min(2, len(parts))):
                    segment = (parts[idx] or "").strip()
                    if not segment:
                        continue
                    payload_b64 = segment + "=" * (-len(segment) % 4)
                    decoded = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                    if not isinstance(decoded, dict):
                        continue
                    wid = (decoded.get("workspace_id", "") or "").strip()
                    if wid:
                        return wid
                    workspaces = decoded.get("workspaces", [])
                    if isinstance(workspaces, list):
                        for it in workspaces:
                            if isinstance(it, dict):
                                wid = (it.get("id", "") or "").strip()
                                if wid:
                                    return wid
        except Exception:
            pass
        return ""

    def _workspace_select(self, workspace_id: str) -> str:
        logger.info("Selecting workspace...")
        headers = self._common_headers("https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=30,
        )
        self._trace_http("workspace_select", resp)
        return resp.json().get("continue_url", "") if resp.status_code == 200 else ""

    def _choose_account_select(self, html_text: str, current_url: str) -> str:
        """Handle the react-router SSR /choose-an-account page for multiple accounts.

        streamController.enqueue injects `unified_sessions[].id` (us_*) and `session_id`
        (authsess_*) into the HTML. Extract us_* with a regex, POST it using react-router action
        conventions, and try candidate JSON endpoints as fallbacks. Return the next continue_url
        or an empty string.
        """
        m = re.search(r"us_[A-Za-z0-9]{16,}", html_text or "")
        if not m:
            logger.warning("No us_* session ID found in /choose-an-account HTML; skipping")
            return ""
        session_id = m.group(0)
        logger.debug(f"/choose-an-account selected session_id={session_id}")
        headers = self._common_headers("https://auth.openai.com/choose-an-account")
        headers["Origin"] = "https://auth.openai.com"

        # The actual endpoint was recovered from nextStepHandler-*.js:
        #   const {path, method} = r.data.intent === "select"
        #     ? {path: "/session/select", method: "POST"}
        #     : {path: "/session/remove", method: "DELETE"};
        #   fetch(`${authapi_base}/session/select`, {method, body: JSON.stringify({session_id})})
        # This means POST https://auth.openai.com/api/accounts/session/select with JSON {session_id}.
        # intent selects the path and is not in the body. Posting /choose-an-account directly passes
        # through a react-router action loader and nextStepHandler, whose server-side layer appears
        # sensitive to content type/form fields and returns 500. Call the lower-level endpoint directly.
        candidates = [
            ("POST", "https://auth.openai.com/api/accounts/session/select",
             {"session_id": session_id}, "json"),
            # Fall back to the react-router path and zod fields if risk controls block the endpoint
            ("POST", "https://auth.openai.com/choose-an-account",
             {"intent": "select", "session_id": session_id}, "form"),
        ]
        for method, url, body, kind in candidates:
            try:
                h = dict(headers)
                if kind == "json":
                    h["Content-Type"] = "application/json"
                    h["Accept"] = "application/json"
                    resp = self.session.post(url, headers=h, json=body, timeout=30)
                else:
                    h["Content-Type"] = "application/x-www-form-urlencoded"
                    h["Accept"] = "application/json, text/html;q=0.9"
                    body_str = "&".join(f"{k}={v}" for k, v in body.items())
                    resp = self.session.post(url, headers=h, data=body_str, timeout=30)
                self._trace_http(f"choose_account_try_{kind}_{url.rsplit('/', 1)[-1][:30]}", resp)
                status = getattr(resp, "status_code", 0)
                snippet = (getattr(resp, "text", "") or "")[:240].replace("\n", " ")
                loc = (getattr(resp, "headers", {}) or {}).get("Location", "") or \
                      (getattr(resp, "headers", {}) or {}).get("location", "") or ""
                # Print each candidate result to stdout so WebUI SSE can display it
                print(
                    f"[choose-an-account] {method} {url} [{kind}] -> "
                    f"status={status} loc={loc[:120]} body={snippet}",
                    flush=True,
                )
                if status in (200, 201, 302, 303):
                    next_url = ""
                    try:
                        j = resp.json() if resp is not None else {}
                        next_url = j.get("continue_url", "") if isinstance(j, dict) else ""
                    except Exception:
                        pass
                    if not next_url and loc:
                        next_url = loc
                    if next_url:
                        logger.debug(f"choose-an-account selection succeeded endpoint={url} next={next_url[:120]}")
                        return next_url
                    # A 200 without continue_url may have set a cookie; let the caller GET authorize again
                    if status == 200:
                        logger.debug(f"choose-an-account POST {url} returned 200 without continue_url; assuming the cookie was set")
                        return current_url  # Let the outer layer GET again after the server set the cookie
            except Exception as e:
                print(f"[choose-an-account] {method} {url} [{kind}] -> EXC {e}", flush=True)
                continue
        logger.warning("All /choose-an-account endpoint candidates failed")
        return ""

    def _normalize_continue_url(self, continue_url: str) -> str:
        """
        Normalize continue_url:
        1. Convert relative paths to absolute URLs.
        2. On a workspace page, call workspace/select for the next hop.
        """
        if not continue_url:
            return ""
        out = continue_url.strip()
        if out.startswith("/"):
            out = urljoin("https://auth.openai.com", out)
        if "/workspace" in out:
            workspace_id = self._extract_workspace_id() or self._extract_query_first(out, ["workspace_id", "id"])
            if workspace_id:
                logger.info("Workspace page detected; trying workspace/select: workspace_id=%s", workspace_id)
                next_url = self._workspace_select(workspace_id)
                if next_url:
                    out = next_url
        return out

    @staticmethod
    def _extract_workspace_id_from_html(html_text: str) -> str:
        """Extract workspace_id from workspace-page HTML as a fallback."""
        if not html_text:
            return ""
        try:
            # Restore escaped quotes before regex matching
            text = html_text.replace('\\"', '"')
            patterns = [
                r'workspaces".{0,1600}?"id","([0-9a-fA-F-]{36})"',
                r'"workspace_id"\s*:\s*"([0-9a-fA-F-]{36})"',
                r'"workspaceId"\s*:\s*"([0-9a-fA-F-]{36})"',
            ]
            for p in patterns:
                m = re.search(p, text, flags=re.DOTALL | re.IGNORECASE)
                if m:
                    return (m.group(1) or "").strip()
        except Exception:
            return ""
        return ""

    # ── Step 10: Follow the redirect chain ──
    def follow_redirect_chain(self, start_url: str) -> tuple[str, str]:
        """Follow redirects manually and return (callback_url, final_url)."""
        logger.info("[9/10] Following redirect chain...")
        current_url = start_url
        callback_url = ""
        max_hops = 12
        referer = "https://auth.openai.com/"

        for i in range(max_hops):
            # Each full-page navigation hop needs browser-like headers. The old Accept/Referer/UA-only
            # headers omitted Client Hints and Sec-Fetch-*, causing 409 invalid_state.
            headers = self._navigation_headers()
            headers["Referer"] = referer
            headers.pop("sec-fetch-user", None)   # Following a 302 is not a user click
            # Mark chatgpt.com/auth.openai.com transitions cross-site and same-host requests same-origin
            try:
                headers["sec-fetch-site"] = (
                    "same-origin"
                    if urlparse(current_url).netloc == urlparse(referer).netloc
                    else "cross-site"
                )
            except Exception:
                headers["sec-fetch-site"] = "cross-site"
            resp = self.session.get(
                current_url, headers=headers, timeout=30, allow_redirects=False
            )
            self._trace_http(f"redirect_hop_{i+1}", resp)
            referer = current_url

            if "/api/auth/callback/openai" in current_url:
                callback_url = current_url
                self._sniff_login_verifier(current_url, f"redirect_hop_{i+1}_callback_url")

            # Workspace pages often return 200 and require workspace/select for the next hop
            if "/workspace" in current_url and resp.status_code == 200:
                workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                if workspace_id:
                    logger.info("Extracted workspace_id=%s from workspace page; continuing authorization", workspace_id)
                    next_url = self._workspace_select(workspace_id)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current_url = next_url
                        continue

            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if not location:
                    break
                if location.startswith("/"):
                    parsed = urlparse(current_url)
                    location = f"{parsed.scheme}://{parsed.netloc}{location}"
                # Do not GET the callback because that would consume the code
                if "/api/auth/callback/openai" in location and "code=" in location:
                    callback_url = location
                    current_url = location
                    self._sniff_login_verifier(location, f"redirect_hop_{i+1}_location_callback")
                    logger.info("Captured callback URL without consuming it")
                    break
                current_url = location
                logger.debug(f"  Redirect {i + 1}: {current_url[:80]}...")
            else:
                break

        # Add one final homepage hop
        if (not callback_url) and (not current_url.rstrip("/").endswith("chatgpt.com")):
            self.session.get(
                "https://chatgpt.com/",
                headers={"Referer": current_url},
                timeout=30,
            )

        logger.info(f"Redirect chain complete, callback: {'present' if callback_url else 'absent'}")
        return callback_url, current_url

    def _reauthorize_for_session(self, original_auth_url: str) -> str | None:
        """Reauthorize for a callback URL after existing-account OTP verification."""
        logger.info("[9.5/10] Reauthorizing to get a session...")
        try:
            # Remove prompt=login and reuse the existing auth-session cookie
            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(original_auth_url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            params.pop("prompt", None)
            # Rebuild the URL
            new_query = urlencode({k: v[0] for k, v in params.items()})
            authorize_url = urlunparse(parsed._replace(query=new_query))

            resp = self.session.get(
                authorize_url,
                allow_redirects=False,
                timeout=15,
            )
            self._trace_http("reauthorize_start", resp)
            logger.info(f"reauthorize status={resp.status_code}")

            # Follow the redirect chain to find the callback URL
            current_url = resp.headers.get("Location", "")
            logger.info(f"reauthorize Location: {current_url[:150]}")
            if resp.status_code in (301, 302, 303, 307, 308) and current_url:
                for hop in range(10):
                    logger.debug(f"reauthorize redirect hop {hop+1}: {current_url[:100]}")
                    if "code=" in current_url and "state=" in current_url:
                        logger.info("Reauthorization found callback URL")
                        return current_url
                    try:
                        hop_resp = self.session.get(
                            current_url,
                            allow_redirects=False,
                            timeout=15,
                        )
                        self._trace_http(f"reauthorize_hop_{hop+1}", hop_resp)
                        next_loc = hop_resp.headers.get("Location", "")
                        if hop_resp.status_code not in (301, 302, 303, 307, 308) or not next_loc:
                            # Check the final URL
                            final_url = str(getattr(hop_resp, 'url', current_url))
                            if "code=" in final_url:
                                return final_url
                            break
                        current_url = next_loc
                        if not current_url.startswith("http"):
                            from urllib.parse import urljoin
                            current_url = urljoin(authorize_url, current_url)
                    except Exception:
                        break
            logger.warning("Reauthorization could not get a callback URL")
            return None
        except Exception as e:
            logger.warning(f"Reauthorization failed: {e}")
            return None

    # ── Step 11: Get the session ──
    def _extract_session_cookie(self) -> str:
        """Extract the __Secure-next-auth.session-token cookie through multiple fallback paths.

        curl_cffi sometimes isolates cookies by domain so session.cookies.get(name) misses them.
        Iterate through all cookies and match the name exactly as a fallback.
        """
        target = "__Secure-next-auth.session-token"
        # Path 1: direct get
        try:
            v = self.session.cookies.get(target, "")
            if v:
                return v
        except Exception:
            pass
        # Path 2: iterate through the jar
        try:
            for c in self.session.cookies:
                name = getattr(c, "name", "") if hasattr(c, "name") else str(c)
                if name == target:
                    val = getattr(c, "value", "") or ""
                    if val:
                        return val
        except Exception:
            pass
        # Path 3: use domain-agnostic _get_cookie_value_by_name
        try:
            return self._get_cookie_value_by_name(target)
        except Exception:
            return ""

    def get_auth_session(self) -> tuple[str, str]:
        """Get session_token and access_token.

        session_token fallback order:
          1. `__Secure-next-auth.session-token` cookie for NextAuth database sessions.
          2. `sessionToken` in JSON for some NextAuth JWT-session paths.
          3. Case and underscore variants.
        Read access_token from the JSON `accessToken` field.
        """
        first_call = not getattr(self, "_auth_session_fetched", False)
        self._auth_session_fetched = True
        if first_call:
            logger.info("[10/10] Getting authentication session...")
        headers = self._common_headers("https://chatgpt.com/")
        resp = self.session.get(
            "https://chatgpt.com/api/auth/session",
            headers=headers,
            timeout=30,
        )
        self._trace_http("chatgpt_auth_session", resp)
        resp.raise_for_status()

        try:
            sess_json = resp.json() if resp is not None else {}
        except Exception:
            sess_json = {}
        if not isinstance(sess_json, dict):
            sess_json = {}

        cookie_st = self._extract_session_cookie()
        json_st = (
            sess_json.get("sessionToken", "")
            or sess_json.get("session_token", "")
            or ""
        )
        session_token = cookie_st or json_st
        access_token = sess_json.get("accessToken", "") or sess_json.get("access_token", "") or ""

        if session_token:
            self.result.session_token = session_token
        if access_token:
            self.result.access_token = access_token
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        _log = logger.info if first_call else logger.debug
        _log(f"session: st={'present' if session_token else 'absent'} at={'present' if access_token else 'absent'}")
        return session_token, access_token

    def _consume_callback_for_session(self, callback_url: str) -> bool:
        """GET the callback URL so chatgpt.com NextAuth sets the session cookie.

        follow_redirect_chain deliberately leaves the callback unconsumed for a later OAuth token
        exchange, but then NextAuth never writes __Secure-next-auth.session-token. If session_token
        is missing, consume the callback once and follow it to chatgpt.com so the server sets the cookie.
        """
        if not callback_url or "code=" not in callback_url:
            return False
        try:
            current = callback_url
            for hop in range(8):
                resp = self.session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://auth.openai.com/",
                        "User-Agent": self._ua,
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"consume_callback_hop_{hop+1}", resp)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                loc = (resp.headers.get("Location", "") or "").strip()
                if not loc:
                    break
                if loc.startswith("/"):
                    loc = urljoin(current, loc)
                current = loc
                # Reaching the chatgpt.com homepage is sufficient
                parsed = urlparse(current)
                if "chatgpt.com" in (parsed.netloc or "") and "/api/auth/callback" not in current:
                    # GET the homepage once more to persist all cookies
                    try:
                        self.session.get(current, timeout=20, allow_redirects=True)
                    except Exception:
                        pass
                    break
            return bool(self.session.cookies.get("__Secure-next-auth.session-token", ""))
        except Exception as e:
            logger.warning(f"Failed to consume callback: {e}")
            return False

    # ── Optional: OAuth token exchange ──
    def oauth_token_exchange(self, callback_url: str, continue_url: str) -> bool:
        """
        Exchange OAuth tokens on a best-effort basis:
        1. Try code_verifier values from query, cookie, dump, and hydra sources.
        2. Fall back to no verifier.
        """
        auth_code = self._extract_query_first(callback_url, ["code"]) or self._extract_query_first(continue_url, ["code"])

        if not auth_code:
            logger.info("auth_code is missing; skipping token exchange")
            return False

        verifier_candidates = self._collect_code_verifier_candidates(callback_url, continue_url)
        if not verifier_candidates:
            logger.info("No usable code_verifier was found; trying exchange without a verifier first")
        else:
            show = ", ".join([f"{src}:{len(v)}" for src, v in verifier_candidates[:8]])
            logger.info("code_verifier candidate count=%s sample=%s", len(verifier_candidates), show)

        logger.info("Performing OAuth token exchange...")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
        }
        base_form = {
            "grant_type": "authorization_code",
            "client_id": self._oauth_client_id or "YOUR_OPENAI_WEB_CLIENT_ID",
            "code": auth_code,
            "redirect_uri": self._oauth_redirect_uri or "https://chatgpt.com/api/auth/callback/openai",
        }
        logger.info(
            "Token exchange parameters: client_id=%s redirect_uri=%s",
            base_form["client_id"],
            base_form["redirect_uri"],
        )

        candidates: list[tuple[str, dict]] = []
        if self._oauth_client_secret:
            d = dict(base_form)
            d["client_secret"] = self._oauth_client_secret
            candidates.append(("with_client_secret", d))

        try:
            max_verifier_try = max(1, int(os.getenv("OAUTH_MAX_VERIFIER_TRY", "18")))
        except Exception:
            max_verifier_try = 18

        for src, verifier in verifier_candidates[:max_verifier_try]:
            d = dict(base_form)
            d["code_verifier"] = verifier
            candidates.append((f"with_verifier_{src}", d))
            if self._oauth_client_secret:
                d2 = dict(d)
                d2["client_secret"] = self._oauth_client_secret
                candidates.append((f"with_verifier_{src}_and_client_secret", d2))

        # Experimental candidates for servers that may require extra parameters
        audience = self._extract_query_first(self._oauth_auth_url, ["audience"])
        if audience:
            d = dict(base_form)
            d["audience"] = audience
            candidates.append(("without_verifier_with_audience", d))
        if self._oauth_scope:
            d = dict(base_form)
            d["scope"] = self._oauth_scope
            candidates.append(("without_verifier_with_scope", d))

        candidates.append(("without_verifier", dict(base_form)))

        seen_fingerprints: set[str] = set()
        for mode, form in candidates:
            fp = json.dumps(form, sort_keys=True, ensure_ascii=False)
            if fp in seen_fingerprints:
                continue
            seen_fingerprints.add(fp)
            try:
                self._sniff_login_verifier(urlencode(form), f"oauth_token_exchange_{mode}:form")
            except Exception:
                pass
            encoded_form = urlencode(form)
            extra_request = {
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            }

            resp = self.session.post(
                "https://auth.openai.com/oauth/token",
                headers=headers,
                data=encoded_form,
                timeout=30,
            )
            self._trace_http(f"oauth_token_exchange_{mode}", resp, extra_request=extra_request)
            if resp.status_code == 200:
                data = resp.json()
                self.result.id_token = data.get("id_token", "")
                self.result.access_token = data.get("access_token", self.result.access_token)
                self.result.refresh_token = data.get("refresh_token", "")
                logger.info(
                    "Token exchange succeeded (mode=%s): refresh_token=%s",
                    mode,
                    "present" if self.result.refresh_token else "absent",
                )
                return True

            body = (resp.text or "")[:240]
            logger.warning("Token exchange failed (mode=%s): status=%s body=%s", mode, resp.status_code, body)

        return False

    def oauth_secondary_authorize_exchange(self) -> bool:
        """
        Secondary-authorization experiment:
        - Issue another PKCE authorize request on the current authenticated session.
        - Extract the callback code without consuming the callback.
        - Exchange it through oauth/token.
        """
        logger.info("Trying secondary authorize + PKCE exchange for refresh_token...")
        try:
            csrf = self.get_csrf_token()
            auth_url = self.get_auth_url(csrf)
        except Exception as e:
            logger.warning(f"Failed to initialize secondary authorization: {e}")
            return False

        try:
            verifier, challenge = self._build_pkce_pair()
            parsed = urlparse(auth_url)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"
            if not params.get("state"):
                params["state"] = self._b64url_no_pad(os.urandom(16))
            sec_url = urlunparse(parsed._replace(query=urlencode(params)))

            self._manual_login_verifier = verifier
            self._captured_login_verifier = verifier
            self._remember_oauth_params(sec_url)

            current = sec_url
            callback_url = ""
            max_hops = 10
            for i in range(max_hops):
                resp = self.session.get(
                    current,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://chatgpt.com/",
                        "User-Agent": self._ua,
                    },
                    timeout=30,
                    allow_redirects=False,
                )
                self._trace_http(f"secondary_authorize_hop_{i+1}", resp)

                loc = (resp.headers.get("Location", "") or "").strip()
                if loc and loc.startswith("/"):
                    loc = urljoin(current, loc)

                if loc and "/api/auth/callback/openai" in loc and "code=" in loc:
                    callback_url = loc
                    break
                if resp.status_code not in (301, 302, 303, 307, 308) or not loc:
                    break
                current = loc

            if not callback_url:
                logger.warning("Secondary authorization did not capture a callback code")
                return False

            ok = self.oauth_token_exchange(callback_url, callback_url)
            logger.info("Secondary authorization exchange result: %s", "success" if ok else "failure")
            return ok
        except Exception as e:
            logger.warning(f"Secondary authorization exchange error: {e}")
            return False

    # ── Complete registration flow ──
    def run_register(self, mail_provider: MailProvider) -> AuthResult:
        """Run the complete registration flow."""
        # Check the network
        if not self.check_proxy():
            logger.warning("Network pre-check failed; continuing the registration flow to get the exact error...")
        # Warmup failure means no oai-did and an inevitable authorize/continue 409 (five of five tests).
        # Stop before create_mailbox so a paid mailbox is not wasted on a doomed attempt.
        if not self.warmup():
            raise RuntimeError(
                "Warmup failed: none of 4 attempts obtained the chatgpt.com oai-did cookie. "
                "Continuing registration would result in 409 invalid_state, usually because the proxy exit IP is "
                "unreachable or blocked by Cloudflare. Check the proxy and retry."
            )

        # Create a mailbox
        email = mail_provider.create_mailbox()
        self.result.email = email

        # Login/registration chain
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, email=email)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)
        is_new = self.signup(email, sentinel)

        # Strategy when OpenAI marks a pooled email as an existing account:
        #   WEBUI_ALLOW_LOGIN=1 (modes such as promo-link need access_token): use OTP login.
        #   Without WEBUI_ALLOW_LOGIN (register-only mode): fail fast, mark dead, and use the next account.
        # This avoids honeypots in register-only mode while allowing promo-link to reuse accounts.
        #
        # This branch applies to every pooled provider, including Outlook, iCloud Relay, and future ones.
        # Use the provider's kind in log prefixes rather than hard-coding Outlook.
        pool_tag = getattr(mail_provider, "kind", "pool")
        is_pooled_existing = not is_new and getattr(mail_provider, "pooled", False)
        if is_pooled_existing:
            _allow_login = self._get_env("WEBUI_ALLOW_LOGIN", "").strip() in (
                "1", "true", "yes",
            )
            if _allow_login:
                logger.info(
                    f"[{pool_tag}] Existing account detected with WEBUI_ALLOW_LOGIN=1; using OTP login to get credentials ({email})"
                )
            else:
                logger.warning(
                    f"[{pool_tag}] Existing pooled email detected ({email}); marking it dead so register() can claim "
                    f"the next account (set WEBUI_ALLOW_LOGIN=1 to use OTP login instead)"
                )
                try:
                    mail_provider.mark_dead(
                        "OpenAI identified the pooled email as an existing account (reused address or honeypot); protocol flow stopped early"
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"OpenAI silently refused to send an OTP after identifying {email} as an existing account; stopping the {pool_tag} pool attempt"
                )

        # passwordless_signup is also a new account; the server merely chose an OTP-first signup.
        # signup() compresses three states into a bool, sharing False with existing accounts. Without
        # this special case, new accounts never called register_password and depended permanently on
        # a temporary mailbox. Tests on 2026-08-06 confirmed POST user/register can set their password.
        if is_new or self._existing_email_verification_mode == "passwordless_signup":
            # New account: register password -> resend code -> verify OTP -> create account
            password_registered = self.register_password(email)
            if password_registered:
                # POST user/register switches to email_otp_send and invalidates the signup OTP.
                # Resend and use the new delivery time without the -8 offset. Record the timestamp
                # before sending so issued_after does not filter an email timestamped during the request.
                otp_sent_at = time.time()
                try:
                    self.send_otp()
                except Exception as e:
                    # Do not abort registration on delivery errors such as 429; fall back to resend
                    logger.warning(f"Failed to send OTP after password registration; falling back to resend: {e}")
                    self.kickoff_otp_delivery("post_register_password_send_failed")
            else:
                logger.warning("Password registration failed; falling back to the existing-account OTP path")
                self.fetch_client_auth_session_dump("post_register_password_failed_new")
                # If password registration fails, fall back to actively sending a code
                if not self.kickoff_otp_delivery("register_password_failed_fallback"):
                    self.send_otp()
                otp_sent_at = time.time()

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_new")
            except RuntimeError as e:
                # On an occasional 401 wrong-code response, resend once and retry
                if "401" in str(e):
                    logger.warning(f"Initial OTP verification failed; resending and retrying: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("verify_otp_retry_new"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_new")
                else:
                    raise

            try:
                continue_url = self.create_account()
            except Exception as e:
                # On registration_disallowed, try reauthorization before re-raising
                if self._is_registration_disallowed_error(e):
                    logger.warning("create_account was rejected; attempting reauthorization to get a session...")
                    continue_url = self._reauthorize_for_session(auth_url) or ""
                    if not continue_url:
                        raise
                else:
                    raise
        else:
            # Existing account: send OTP -> verify -> get session
            mode = (self._existing_email_verification_mode or "").lower()
            page_type = (self._existing_page_type or "").lower()
            continue_url = ""

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180

            if page_type == "login_password":
                logger.info("Existing account entered login_password; verifying the password before OTP")
                login_password, pw_is_real = self._resolve_login_password(email)
                if pw_is_real:
                    self.result.password = login_password
                else:
                    logger.info("No known password for this account; trying the default password rule (likely to return 401)")
                login_resp = self.login_password_verify(login_password)
                login_page_type = self._extract_page_type(login_resp)
                continue_url = self._normalize_continue_url(
                    (login_resp or {}).get("continue_url", "") if isinstance(login_resp, dict) else ""
                )

                # mfa-challenge path requiring TOTP 2FA after password verification
                if self._is_mfa_challenge_state(login_page_type, continue_url):
                    totp_secret = (self.result.totp_secret or "").strip()
                    if not totp_secret and self._account_callback:
                        # Load credentials from the database
                        try:
                            cred = self._account_callback(email)
                            if cred and cred.get("totp_secret"):
                                totp_secret = cred["totp_secret"]
                                self.result.totp_secret = totp_secret
                                logger.info("Loaded totp_secret from the database")
                        except Exception as e:
                            logger.warning(f"account_callback failed: {e}")
                    if not totp_secret:
                        logger.warning("Cannot continue mfa-challenge without totp_secret")
                    else:
                        challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                        if challenge_id:
                            totp_code = _totp_now(totp_secret)
                            logger.info(f"Submitting TOTP code for 2FA verification (challenge_id={challenge_id[:16]}...)")
                            mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                            continue_url = self._normalize_continue_url(
                                (mfa_resp or {}).get("continue_url", "") if isinstance(mfa_resp, dict) else ""
                            )
                        else:
                            logger.warning("Could not extract challenge_id from continue_url")

                # Some accounts still require email OTP after password verification
                elif not continue_url or "/email-verification" in continue_url:
                    # Prefer resend after password/verify rather than /email-otp/send
                    otp_sent_at = time.time()
                    self.kickoff_otp_delivery("existing_login_password")
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    continue_url = self._normalize_continue_url(
                        (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                    )
            else:
                need_send_otp = mode not in ("passwordless_signup", "passwordless_login")
                if need_send_otp:
                    otp_sent_at = time.time()
                    self.send_otp()
                else:
                    # Some modes already triggered delivery in /authorize/continue; avoid another
                    # /email-otp/send that would break state. Try resend first, then inspect a short window.
                    forced_resend = self._env_flag("OTP_FORCE_RESEND", "0")
                    if forced_resend and self.kickoff_otp_delivery("existing_forced_resend"):
                        otp_sent_at = time.time()
                        logger.debug(f"Existing-account verification mode={mode}; OTP was resent")
                    else:
                        # Use a short lookback while avoiding an older code from the previous run
                        otp_sent_at = time.time() - 8
                        logger.info(f"Existing-account verification mode={mode}; skipping an extra send_otp call and waiting for email")

                try:
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                except TimeoutError:
                    # A provider sets exhausted=True and marks dead when no receiving path remains.
                    # Raise without retrying an invalid path. Outlook uses IMAP-only failure criteria;
                    # other providers define their own.
                    if getattr(mail_provider, "exhausted", False):
                        logger.warning(
                            f"[{getattr(mail_provider, 'kind', 'mail')}] "
                            f"OTP delivery path is unavailable and the account was marked dead; skipping resend retry"
                        )
                        raise
                    # For non-pooled providers such as catch_all CF KV, allow one resend retry
                    logger.warning("Timed out waiting for an existing-account OTP; resending before retrying")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("existing_timeout_retry"):
                        self.send_otp()
                    try:
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                    except TimeoutError:
                        # Two timeouts on a pooled existing-account path indicate OpenAI silently
                        # refused delivery. Mark the email dead so the pool skips it next time.
                        # mark_dead is a no-op for non-pooled providers such as temporary CF mail.
                        if getattr(mail_provider, "pooled", False):
                            try:
                                mail_provider.mark_dead(
                                    "OpenAI silently refused to send an OTP (existing account reported, but no email arrived)"
                                )
                            except Exception:
                                pass
                        raise
                try:
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_existing")
                except RuntimeError as e:
                    if any(code in str(e) for code in ("401", "409")):
                        logger.warning(f"Initial OTP verification failed; resending and retrying: {e}")
                        otp_sent_at = time.time()
                        if not self.kickoff_otp_delivery("existing_verify_retry"):
                            self.send_otp()
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                        otp_resp = self.verify_otp(otp_code)
                        self.fetch_client_auth_session_dump("post_verify_otp_retry_existing")
                    else:
                        raise
                continue_url = (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                continue_url = self._normalize_continue_url(continue_url)
                if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                    continue_url = self._normalize_continue_url(
                        self._handle_add_phone_verification(continue_url=continue_url)
                    )

            # Some existing accounts enter about-you after OTP and require create_account
            if continue_url and "/about-you" in continue_url:
                try:
                    continue_url = self.create_account()
                except Exception as e:
                    if self._is_registration_disallowed_error(e):
                        logger.warning("about-you create_account was rejected; attempting reauthorization to get a session...")
                        continue_url = self._reauthorize_for_session(auth_url) or ""
                        if continue_url:
                            logger.info("Reauthorization fallback succeeded; continuing session retrieval")
                            # Downstream processing uses follow_redirect_chain and get_auth_session
                            pass
                        else:
                            raise
                    else:
                        logger.warning(f"Failed to complete about-you for the existing account; falling back to reauthorization: {e}")
                        continue_url = ""

            # Fall back to reauthorization if the OTP response has no usable continue_url
            if not continue_url:
                # The auth.openai.com session cookie is set, so request the code directly
                continue_url = self._reauthorize_for_session(auth_url)

        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            # Try Codex OAuth before consuming the chatgpt callback to preserve auth.openai login state.
            # Skip this early attempt when on_session_ready is installed because 2FA binding requires
            # access_token after get_auth_session. Codex must then run later to preserve the sequence:
            # create account -> redirects -> session -> bind 2FA -> Codex authorization -> receive code.
            if (
                (not self.result.refresh_token)
                and self._on_session_ready is None
                and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1")
            ):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            # Optionally exchange tokens before consuming the callback; disabled because it may affect the callback
            refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)
        else:
            callback_url, final_url = None, None

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")

        # ─ Critical ordering fix ─
        # Old order: get_auth_session -> oauth_token_exchange -> _consume_callback fallback.
        # oauth_token_exchange consumed the callback code at /oauth/token, so a later callback GET
        # used an expired code and NextAuth refused to set __Secure-next-auth.session-token.
        # New order: let NextAuth consume the code and set all cookies first, then get access_token.
        # Codex refresh-token exchange uses a separate authorize chain and does not conflict.
        if (not refresh_only_mode) and callback_url:
            logger.debug("Consuming callback to trigger NextAuth Set-Cookie (session-token)")
            self._consume_callback_for_session(callback_url)

        if not refresh_only_mode:
            self.get_auth_session()

        # ── Hook after session retrieval and before Codex authorization ──
        # This is the only point with access_token available before Codex runs, preserving the
        # registration -> 2FA -> Codex -> code sequence. Suppress failures so an already registered
        # account can continue through Codex.
        if self._on_session_ready is not None and self.result.access_token:
            try:
                self._on_session_ready(self, self.result.access_token)
            except Exception as e:
                logger.warning(f"session_ready callback failed (registration can continue): {e}")

        # Codex OAuth refresh_token exchange uses an independent authorize chain
        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_register")
            # oauth_token_exchange(callback_url) competes with NextAuth for the same code, so it is
            # disabled unless OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK=1 is explicitly set.
            if self._env_flag("OAUTH_TOKEN_EXCHANGE_FROM_CALLBACK", "0") \
                    and not self._env_flag("SKIP_OAUTH_TOKEN_EXCHANGE", "0"):
                self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            # Fetch the session once more because Codex may update cookies or access_token
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("Flow completed without a refresh_token or access_token")
        elif not self.result.is_valid():
            raise RuntimeError("Registration completed without valid credentials")

        logger.info("Registration flow completed!")
        return self.result

    # ── Direct-protocol existing-account login for callback/session/refresh ──
    def run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """
        Direct-protocol login without creating a random mailbox:
        - Supports passwordless and login_password existing-account entry points.
        - OAUTH_EXCHANGE_BEFORE_CALLBACK / OAUTH_REFRESH_ONLY can prioritize refresh_token.
        """
        if not (email or "").strip():
            raise RuntimeError("run_protocol_login requires an email address")

        if not self.check_proxy():
            logger.warning("Network pre-check failed; continuing the login flow to get the exact error...")
        # As in run_register, authorization cannot work without oai-did. Fail early so the IP can
        # be changed, and report the cause clearly instead of misdiagnosing a wrong password.
        if not self.warmup():
            raise RuntimeError(
                "Warmup failed: none of 4 attempts obtained the chatgpt.com oai-did cookie. "
                "Continuing login would result in 409 invalid_state, usually because the proxy exit IP is unreachable "
                "or blocked by Cloudflare. Check the proxy and retry."
            )

        # This method logs into an existing account. kickoff_otp_delivery uses _is_existing_account
        # to select resend rather than send_passwordless_otp; the latter creates new server state and
        # invalidates the code read by IMAP. Set True at entry even for unusual passwordless page types;
        # the signup fallback safely replaces it from the real OpenAI response.
        self._is_existing_account = True

        email = email.strip()
        self.result.email = email
        login_password = (password or "").strip()
        if login_password:
            self.result.password = login_password
        else:
            login_password, pw_is_real = self._resolve_login_password(email)
            if pw_is_real:
                self.result.password = login_password
            else:
                logger.info("No password was provided or found in the database; trying the default password rule")

        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, email=email)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        continue_url = ""
        try:
            otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
        except Exception:
            otp_timeout = 180

        page_type = ""
        mode = ""
        prefer_login_screen_first = str(
            os.getenv("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
        ).lower() in ("1", "true", "yes", "on")

        if prefer_login_screen_first:
            try:
                logger.info("Existing-account protocol login: probing password/OTP path with login screen_hint")
                login_step = self.authorize_continue(
                    email=email,
                    sentinel_token=sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    trace_step="authorize_continue_login_protocol",
                )
                page_type = (self._extract_page_type(login_step) or "").lower()
                continue_url = self._normalize_continue_url(
                    self._extract_continue_url_from_step(login_step)
                )
                page = (login_step.get("page") or {}) if isinstance(login_step, dict) else {}
                payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
                mode = (payload.get("email_verification_mode", "") or "").lower()
                self._existing_page_type = page_type
                self._existing_email_verification_mode = mode

                if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
                    logger.info("Login path: login_password -> password/verify")
                    # Mark the existing-account password path so kickoff_otp_delivery uses resend
                    self._is_existing_account = True
                    login_resp = self.login_password_verify(login_password)
                    page_type = (self._extract_page_type(login_resp) or "").lower()
                    continue_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(login_resp)
                    )

                    # mfa-challenge path requiring TOTP 2FA after password verification
                    if self._is_mfa_challenge_state(page_type, continue_url):
                        totp_secret = (self.result.totp_secret or "").strip()
                        if not totp_secret and self._account_callback:
                            # Load credentials from the database
                            try:
                                cred = self._account_callback(email)
                                if cred and cred.get("totp_secret"):
                                    totp_secret = cred["totp_secret"]
                                    self.result.totp_secret = totp_secret
                                    logger.info("Loaded totp_secret from the database")
                            except Exception as e:
                                logger.warning(f"account_callback failed: {e}")
                        if not totp_secret:
                            logger.warning("Cannot continue mfa-challenge without totp_secret")
                        else:
                            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                            if challenge_id:
                                totp_code = _totp_now(totp_secret)
                                logger.info(f"Submitting TOTP code for 2FA verification (challenge_id={challenge_id[:16]}...)")
                                mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                                page_type = (self._extract_page_type(mfa_resp) or "").lower()
                                continue_url = self._normalize_continue_url(
                                    self._extract_continue_url_from_step(mfa_resp)
                                )
                            else:
                                logger.warning("Could not extract challenge_id from continue_url")

                elif page_type == "email_otp_verification" or "/email-verification" in (continue_url or ""):
                    logger.info("Login path: email_otp_verification")
                    # authorize/continue already triggered delivery, so kickoff_otp_delivery must only resend
                    self._is_existing_account = True
                else:
                    logger.info(
                        "login screen_hint did not directly reach a completed existing-account state: page_type=%s continue_url=%s",
                        page_type or "(empty)",
                        (continue_url or "")[:180] or "(empty)",
                    )
            except Exception as e:
                logger.warning(f"login screen_hint probe failed; falling back to signup probe: {e}")
                continue_url = ""
                page_type = ""
                mode = ""

        if not continue_url and page_type not in ("login_password", "email_otp_verification"):
            is_new = self.signup(email, sentinel)
            if is_new:
                logger.warning("Target email did not reach the existing-account path; falling back to registration")
                self.register_password(email)
                otp_sent_at = time.time()
                self.send_otp()
                otp_code = mail_provider.wait_for_otp(
                    email,
                    timeout=otp_timeout,
                    issued_after=otp_sent_at,
                )
                self.verify_otp(otp_code)
                continue_url = self.create_account()
            else:
                page_type = (self._existing_page_type or "").lower()
                mode = (self._existing_email_verification_mode or "").lower()
        else:
            page_type = (page_type or self._existing_page_type or "").lower()
            mode = (mode or self._existing_email_verification_mode or "").lower()

        if not continue_url or "/email-verification" in continue_url:
            # OTP is still required; prefer resend for a fresh code
            otp_sent_at = time.time()
            resend_ok = self.kickoff_otp_delivery("protocol_need_otp")
            if not resend_ok and mode not in ("passwordless_signup", "passwordless_login"):
                self.send_otp()
                otp_sent_at = time.time()

            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                otp_resp = self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_protocol")
            except RuntimeError as e:
                if any(code in str(e) for code in ("401", "409")):
                    logger.warning(f"Initial OTP verification failed; resending and retrying: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("protocol_verify_retry"):
                        self.send_otp()
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_protocol")
                else:
                    raise
            continue_url = self._extract_continue_url_from_step(otp_resp)
            continue_url = self._normalize_continue_url(continue_url)
            if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                continue_url = self._normalize_continue_url(
                    self._handle_add_phone_verification(continue_url=continue_url)
                )

        continue_url = self._normalize_continue_url(continue_url)
        # Some edge states return no callback after OTP; fall back to reauthorization
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            pre_exchange_default = "1" if refresh_only_mode else "0"
            pre_exchange = self._env_flag("OAUTH_EXCHANGE_BEFORE_CALLBACK", pre_exchange_default)
            if pre_exchange:
                self.oauth_token_exchange(continue_url, continue_url)
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if not refresh_only_mode:
            self.get_auth_session()

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_protocol")
            self.oauth_token_exchange(callback_url or "", continue_url or "")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_SECONDARY_AUTHORIZE_EXCHANGE", "0"):
                self.oauth_secondary_authorize_exchange()
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("Protocol login completed without a refresh_token or access_token")
        elif not self.result.is_valid():
            raise RuntimeError("Protocol login completed without a valid session or access token")

        logger.info("Protocol login flow completed")
        return self.result

    # ── Initialize from existing credentials ──
    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """Use existing credentials and skip registration."""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")
        detected_email = ""

        # If session_token exists, use it to refresh a potentially expired access_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("Refreshing access_token with session_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                new_access_token = session_data.get("accessToken", "")
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token refreshed successfully")
                else:
                    logger.warning(f"Failed to refresh access_token (status={resp.status_code}); using the original token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"Failed to refresh access_token: {e}; using the original token")
        elif access_token:
            # Without session_token, try to obtain one through access_token
            logger.info("No session_token provided; attempting to obtain one with access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("Obtained session_token with access_token")
                else:
                    logger.warning("Could not obtain session_token; it may need to be provided manually")
            except Exception as e:
                logger.warning(f"Failed to obtain session_token: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        # Backfill email, often used as the billing email in skip-register mode
        if not detected_email and access_token and access_token.count(".") >= 2:
            try:
                payload_b64 = access_token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                prof = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
                if isinstance(prof, dict):
                    detected_email = detected_email or (prof.get("email", "") or "")
            except Exception:
                pass
        self.result.email = detected_email or ""
        logger.info("Initialization with existing credentials completed")
        return self.result

