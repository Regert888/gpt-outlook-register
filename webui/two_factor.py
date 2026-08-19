"""
two_factor.py — programmatic TOTP enrollment after registration
================================================================
Adapted from the validated bind_2fa.py script into registrar-callable functions.

registrar tries two paths in order:

  1. bind_totp_2fa_inline(flow, at), the fast path, reuses the registration
     session and access token. Validation completed in about 6.2 seconds.

  2. bind_totp_2fa(cfg, email, password, ...), the fallback, repeats login in a
     new AuthFlow and costs roughly 40 seconds, one PoW, and one email.

Earlier speculation that registration sessions always return recent_auth_required
was disproved by the validated fast path. Keep full login only as a fallback.

The secret appears once and cannot be retrieved; registrar persists it immediately.

Enrollment takes effect immediately; later logins require a six-digit TOTP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import sys
import time
import urllib.parse
from pathlib import Path

# Add the project root so config/auth_flow import when this module is standalone.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402

logger = logging.getLogger("two_factor")


# Internal implementation note.
def hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str) -> str:
    """Return the six-digit code for the current 30-second window."""
    return hotp(secret_b32, int(time.time()) // 30)


def verify_totp(secret_b32: str, code: str) -> bool:
    """Verify locally with one-window clock-skew tolerance."""
    c = int(time.time()) // 30
    return code in {hotp(secret_b32, c + d) for d in (-1, 0, 1)}


# Internal implementation note.
def _enroll_and_activate(flow: AuthFlow, at: str) -> dict | None:
    """Enroll and activate through flow.session and an access token.

    Both paths differ only in how they obtain the token.
    """
    # Idempotency: never re-enroll an existing TOTP factor.
    logger.info("[2fa] Checking existing enrollment...")
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa_info")
    hh["Authorization"] = f"Bearer {at}"
    r2 = flow.session.get(
        "https://chatgpt.com/backend-api/accounts/mfa_info", headers=hh, timeout=30
    )
    if r2.status_code == 200:
        info = r2.json() or {}
        if info.get("mfa_enabled") and (info.get("factors", {}) or {}).get("totp"):
            logger.info(
                "[2fa] TOTP is already enabled; skipping because the server cannot "
                "return the existing secret"
            )
            return None

    logger.info("[2fa] Enrolling TOTP (the secret appears only in this response)...")
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa/enroll")
    hh["Authorization"] = f"Bearer {at}"
    hh["Content-Type"] = "application/json"
    r3 = flow.session.post(
        "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        headers=hh, json={"factor_type": "totp"}, timeout=30,
    )
    if r3.status_code != 200:
        logger.warning(
            "[2fa] enroll %s: %s", r3.status_code, (r3.text or "")[:200]
        )
        return None
    en = r3.json() or {}
    secret = en.get("secret", "")
    session_id = en.get("session_id", "")
    factor_id = (en.get("factor", {}) or {}).get("id", "")
    if not secret or not session_id:
        logger.warning(
            "[2fa] Enrollment response is missing secret/session_id; skipping: %s",
            json.dumps(en)[:200],
        )
        return None

    logger.info("[2fa] Generating a code and activating TOTP...")
    code = totp_now(secret)
    hh = flow._common_headers(
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    )
    hh["Authorization"] = f"Bearer {at}"
    hh["Content-Type"] = "application/json"
    r4 = flow.session.post(
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
        headers=hh,
        json={"code": code, "factor_type": "totp", "session_id": session_id},
        timeout=30,
    )
    if r4.status_code != 200:
        logger.warning(
            "[2fa] activate_enrollment %s: %s (for 429, wait 60s and retry with a new code)",
            r4.status_code, (r4.text or "")[:200]
        )
        return None

    # A failed follow-up check does not undo successful enroll/activate responses.
    time.sleep(2)
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa_info")
    hh["Authorization"] = f"Bearer {at}"
    r5 = flow.session.get(
        "https://chatgpt.com/backend-api/accounts/mfa_info", headers=hh, timeout=30
    )
    if r5.status_code == 200 and (r5.json() or {}).get("mfa_enabled"):
        logger.info("[2fa] ✅ Enrollment successful; mfa_enabled=true")
    else:
        logger.warning(
            "[2fa] enroll/activate returned 200, but mfa_info verification failed: %s %s",
            r5.status_code, (r5.text or "")[:120]
        )

    return {"secret": secret, "factor_id": factor_id, "session_id": session_id}


# Internal implementation note.
def bind_totp_2fa_inline(flow: AuthFlow, access_token: str = "") -> dict | None:
    """Enroll through the just-completed registration flow.

    Validation showed that the recent registration session satisfies recent-auth
    requirements:
        GET  mfa_info -> 200 (mfa_enabled:false)
        POST enroll   -> 200 (32-character secret)
        POST activate -> 200 {"success":true}
        GET  mfa_info -> 200 mfa_enabled=true

    This takes about 6.2 seconds with no extra PoW or email. Return None on failure
    so the caller can use the fallback without propagating an exception.
    """
    try:
        at = access_token or getattr(getattr(flow, "result", None), "access_token", "")
        if not at:
            logger.warning(
                "[2fa] Fast path has no access_token from the registration session; "
                "falling back to the full login flow"
            )
            return None
        return _enroll_and_activate(flow, at)
    except Exception as e:  # noqa: BLE001 -- never invalidate a registered account
        logger.warning("[2fa] Fast path error; falling back to the full login flow: %s", e)
        return None


# Internal implementation note.
def bind_totp_2fa(
    cfg: Config,
    email: str,
    password: str,
    mail_provider=None,
    env_overrides: dict | None = None,
) -> dict | None:
    """
    Enroll TOTP 2FA after registration.

    Return secret/factor/session data on success, otherwise log and return None so
    a 2FA failure never invalidates an already registered account.

    The fallback uses an independent AuthFlow, device ID, and user agent.
    """
    if not email or not password:
        logger.warning(
            "[2fa] Email or password is missing; passwordless accounts cannot use "
            "the full login enrollment flow"
        )
        return None

    try:
        flow = AuthFlow(cfg, env_overrides=dict(env_overrides or {}))

        # Start the OTP watermark at the beginning because the server may send a
        # challenge during oauth_init, before authorize/continue. Resends within
        # one challenge carry the same code, while registration and enrollment use
        # distinct challenges separated by enough time to avoid cross-matching.
        chain_started_at = time.time()

        logger.info("[2fa] 1/11 Checking proxy and warming up...")
        flow.check_proxy()
        # Without oai-did enrollment returns 409; fail this optional post-step early.
        if not flow.warmup():
            raise RuntimeError(
                "Warmup failed: no oai-did cookie was received, so enrollment would "
                "fail with 409 invalid_state"
            )

        logger.info("[2fa] 2/11 Getting csrf_token...")
        csrf = flow.get_csrf_token()

        logger.info("[2fa] 3/11 Getting the OAuth authorization URL...")
        auth_url = flow.get_auth_url(csrf, email=email)

        logger.info("[2fa] 4/11 Initializing OAuth and obtaining device_id...")
        device_id = flow.auth_oauth_init(auth_url)

        logger.info("[2fa] 5/11 Getting the Sentinel token (PoW)...")
        flow.get_sentinel_token(device_id)

        logger.info(
            "[2fa] 6/11 Submitting the email to authorize/continue "
            "(full login flow)..."
        )
        step = flow.authorize_continue(
            email, flow._last_sentinel_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="bind_2fa",
        )
        page_type = flow._extract_page_type(step)
        continue_url = flow._normalize_continue_url(
            flow._extract_continue_url_from_step(step)
        )
        logger.info("[2fa] page.type = %r", page_type)

        # Step 7: password flow only when the server presents a password page.
        if page_type == "login_password" or "/log-in/password" in continue_url:
            logger.info("[2fa] 7/11 Opening the password page and verifying the password...")
            flow.session.get(
                f"https://auth.openai.com/log-in/password?email={urllib.parse.quote(email)}",
                headers=flow._common_headers("https://auth.openai.com/log-in/password"),
                timeout=30,
            )
            step = flow.login_password_verify(password)
            page_type = flow._extract_page_type(step)
            continue_url = flow._normalize_continue_url(
                flow._extract_continue_url_from_step(step)
            )
            logger.info("[2fa] page.type after password verification = %r", page_type)

        # Step 7.5: low-trust new accounts may require email OTP even with a password.
        need_otp = (page_type == "email_otp_verification") or (
            "/email-verification" in (continue_url or "")
        )
        if need_otp:
            if mail_provider is None:
                logger.warning(
                    "[2fa] Email OTP is required but no mail_provider was supplied; skipping"
                )
                return None
            try:
                otp_timeout = max(10, int(flow._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 60
            logger.info("[2fa] 7.5/11 Email OTP required (timeout=%ss)...", otp_timeout)

            # Peek before resending because OAuth/login_hint may already have sent
            # the challenge. Enrollment resends reuse the same state and code, so
            # an existing message is valid. If none appears within four seconds,
            # fall back to the original resend path.
            otp_code = None
            try:
                peek = getattr(mail_provider, "peek_otp", None)
                if callable(peek):
                    otp_code = peek(email, issued_after=chain_started_at, wait=4)
            except Exception as e:
                logger.debug("[2fa] OTP prefetch failed; falling back to resend: %s", e)

            if not otp_code:
                logger.info("[2fa] No code is in the inbox yet; requesting one...")
                # Existing accounts must reuse the challenge state; creating a new
                # challenge invalidates the already delivered code.
                if not flow.kickoff_otp_delivery("existing_bind_2fa"):
                    flow.send_otp(referer="https://auth.openai.com/email-verification")
                otp_code = mail_provider.wait_for_otp(
                    email, timeout=otp_timeout, issued_after=chain_started_at,
                )
            otp_resp = flow.verify_otp(otp_code)
            page_type = flow._extract_page_type(otp_resp)
            continue_url = flow._normalize_continue_url(
                flow._extract_continue_url_from_step(otp_resp)
            )
            logger.info("[2fa] OTP verified; page.type = %r", page_type)

        cu = continue_url
        if not cu:
            logger.warning(
                "[2fa] The login flow returned no continue_url (page.type=%r); skipping",
                page_type,
            )
            return None

        # Existing 2FA enters mfa-challenge directly; do not enroll again.
        if "/mfa-challenge/" in cu:
            logger.info("[2fa] 2FA is already enabled; skipping duplicate enrollment")
            return None

        logger.info("[2fa] 8/11 Consuming the callback and establishing a session...")
        if not flow._consume_callback_for_session(cu):
            logger.warning("[2fa] Failed to consume the callback; skipping")
            return None

        logger.info("[2fa] 9/11 Getting access_token...")
        _st, at = flow.get_auth_session()
        if not at:
            logger.warning("[2fa] No access_token was returned; skipping")
            return None

        # Steps 9.5-11 share the fast path's enrollment implementation.
        return _enroll_and_activate(flow, at)

    except Exception as e:  # noqa: BLE001 -- never invalidate a registered account
        logger.warning(
            "[2fa] Enrollment failed; the account remains valid without 2FA: %s", e
        )
        return None
