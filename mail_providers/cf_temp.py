"""Cloudflare Worker temporary-mail provider.

Uses the production flow from lxf746/any-auto-register: create an address with
``POST /admin/new_address`` and ``enablePrefix=True``, fetch its messages with
``GET /admin/mails?address=<email>``, and extract OTPs from the raw message
while filtering colors, addresses, and timestamps.

This provider generates unlimited fresh addresses, so it is unpooled and
ephemeral. It requires the Worker HTTPS URL, the configured ADMIN_PASSWORDS
value, and a catch-all domain. The implementation moved from mail_cf.py, which
remains as a compatibility wrapper for legacy imports.
"""
from __future__ import annotations

import json as _json
import logging
import random
import string
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .base import ConfigField, MailProvider, extract_otp, register

logger = logging.getLogger(__name__)


def _gen_local_part(rng: Optional[random.Random] = None, length: int = 10) -> str:
    """Generate a random lowercase-and-digit mailbox prefix."""
    r = rng or random
    return "".join(r.choices(string.ascii_lowercase + string.digits, k=length))


# OTP extraction is shared with Outlook through base.extract_otp.
# Retain the old name for potential external callers.
_extract_otp = extract_otp


@register
class CFTempEmailProvider(MailProvider):
    """Cloudflare Worker temporary-mail provider.

    Usage::
        mail = CFTempEmailProvider(
            api_url="https://mail.example.com",
            admin_token="<YOUR_ADMIN_PASSWORDS>",
            domain="example.com",
        )
        auth_flow.run_register(mail)
    """

    kind = "cf_temp"
    display_name = "Cloudflare Worker Temporary Email"
    pooled = False         # Generates unlimited addresses; no account pool.
    ephemeral = True       # Every run uses a fresh address.

    line_segments = 0      # Imports are not supported.
    import_hint = ""
    import_placeholder = ""

    config_fields = [
        ConfigField(
            "cf_api_url", "Worker URL",
            placeholder="https://mail.example.com",
            help="Cloudflare Worker HTTPS URL without a trailing slash",
        ),
        ConfigField(
            "cf_admin_token", "Admin Token", type="password",
            help="Value of the Worker's ADMIN_PASSWORDS environment variable",
        ),
        ConfigField(
            "cf_domain", "Receiving Domain",
            placeholder="example.com",
            help="Domain configured with catch-all email routing",
        ),
    ]

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        session=None,
    ):
        if not api_url:
            raise ValueError("api_url is required")
        if not domain:
            raise ValueError("domain is required")
        self.api_url = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = domain
        self._jwt: str = ""
        self._current_email: str = ""
        self._seen_mail_ids: set = set()
        self._rng = random.Random()
        self.last_persona = None

        # Use curl_cffi Chrome impersonation for Cloudflare Bot Fight Mode.
        if session is not None:
            self._session = session
        else:
            try:
                from curl_cffi.requests import Session as CffiSession
                self._session = CffiSession(impersonate="chrome136")
                self._session.trust_env = False
            except ImportError:
                self._session = None

    # ──────────────────────── Construction ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        api_url = (settings.get("cf_api_url") or "").strip()
        domain = (settings.get("cf_domain") or "").strip()
        token = (settings.get("cf_admin_token") or "").strip()
        if not api_url or not domain or not token:
            raise RuntimeError(
                "Cloudflare temporary email is not fully configured "
                "(missing api_url, domain, or admin_token). Complete the Mail Settings tab."
            )
        return cls(api_url=api_url, admin_token=token, domain=domain)

    # ──────────────────────── HTTP helpers ────────────────────────

    def _headers(self) -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }

    def _request(self, method: str, path: str, **kwargs):
        """Send a request with curl_cffi, falling back to urllib."""
        url = f"{self.api_url}{path}"
        m = method.upper()
        timeout = kwargs.get("timeout", 15)
        headers = dict(kwargs.get("headers") or self._headers())
        json_body = kwargs.get("json")
        params = kwargs.get("params")

        if self._session is not None:
            try:
                if m == "GET":
                    return self._session.get(url, headers=headers, params=params, timeout=timeout)
                if json_body is not None:
                    return self._session.post(
                        url, headers=headers,
                        data=_json.dumps(json_body, separators=(",", ":")),
                        timeout=timeout,
                    )
                return self._session.post(url, headers=headers, timeout=timeout)
            except Exception as e:
                logger.warning(f"[cf_temp] curl_cffi request failed; falling back to urllib: {e}")

        # urllib fallback
        if params:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        body = _json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=m)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.status_code = r.status
                r._text = r.read().decode("utf-8", errors="replace")
                r.text = r._text
                r.json = lambda: _json.loads(r._text)
                return r
        except urllib.error.HTTPError as e:
            e.status_code = e.code
            try:
                e._text = e.read().decode("utf-8", errors="replace")
            except Exception:
                e._text = ""
            e.text = e._text
            e.json = lambda: _json.loads(e._text or "{}")
            return e

    @staticmethod
    def _mail_epoch(mail: dict) -> Optional[float]:
        """Parse message ``created_at`` as epoch seconds, or return None.

        The Worker returns a timezone-free UTC value such as
        ``2026-08-08 05:51:41``. Interpret it explicitly as UTC so local time
        zones do not invalidate ``issued_after`` comparisons.
        """
        raw = (mail.get("created_at") or "").strip()
        if not raw:
            return None
        raw = raw.replace("T", " ").replace("Z", "").split(".")[0]
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        return dt.replace(tzinfo=timezone.utc).timestamp()

    @staticmethod
    def _parse_json(resp) -> dict:
        try:
            return resp.json() if callable(getattr(resp, "json", None)) else _json.loads(resp.text)
        except Exception:
            return {}

    # ──────────────────────── Public API ────────────────────────

    def create_mailbox(self) -> str:
        """Create a mailbox with POST /admin/new_address and obtain its JWT.

        ``enablePrefix=True`` is required by some deployments; ``name`` is a
        random ten-character prefix and ``domain`` is the catch-all domain.
        """
        local = _gen_local_part(self._rng, length=10)
        payload = {
            "enablePrefix": True,
            "name": local,
            "domain": self.domain,
        }
        resp = self._request("POST", "/admin/new_address", json=payload, timeout=15)
        status = getattr(resp, "status_code", 0)
        text = (getattr(resp, "text", "") or "")[:300]
        logger.debug(f"[cf_temp] new_address status={status} resp={text}")

        if status != 200:
            raise RuntimeError(
                f"CFTempEmail create_mailbox failed: status={status} body={text}"
            )

        data = self._parse_json(resp)
        # Accept both response schemas: email/address and token/jwt.
        email = (data.get("email") or data.get("address") or "").strip()
        token = (data.get("token") or data.get("jwt") or "").strip()

        if not email:
            raise RuntimeError(f"new_address response is missing the email field: {data}")

        self._jwt = token
        self._current_email = email
        self._seen_mail_ids = set()
        logger.info(
            f"[cf_temp] Created mailbox: {email} "
            f"jwt={'len='+str(len(token)) if token else 'NONE'}"
        )
        return email

    def _get_mails(self, email: str) -> list:
        """Fetch the latest messages for an address (default limit: 20)."""
        resp = self._request(
            "GET", "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            timeout=10,
        )
        status = getattr(resp, "status_code", 0)
        if status != 200:
            logger.debug(f"[cf_temp] /admin/mails returned {status}")
            return []
        data = self._parse_json(resp)
        if isinstance(data, dict):
            return data.get("results") or data.get("mails") or []
        if isinstance(data, list):
            return data
        return []

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """Return an existing current-run OTP without consuming its message.

        Do not modify ``_seen_mail_ids``. Reject messages older than
        ``issued_after`` and messages without a parseable timestamp. Return
        None without raising so callers can request a new code normally.
        """
        deadline = time.time() + max(0.0, float(wait))
        while True:
            try:
                for mail in sorted(
                    self._get_mails(email_addr),
                    key=lambda x: x.get("id", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    if issued_after is not None:
                        ts = self._mail_epoch(mail)
                        if ts is None or ts < issued_after - 2:
                            continue
                    otp = extract_otp(str(mail.get("raw") or ""))
                    if otp:
                        logger.info(
                            f"[cf_temp] 👀 Pre-read found OTP={otp} (mail id={mid}); skipping an extra code request"
                        )
                        return otp
            except Exception as e:
                logger.debug(f"[cf_temp] Peek failed; treating it as no OTP found: {e}")
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """Poll /admin/mails for a six-digit OTP.

        De-duplicate with ``_seen_mail_ids``, sort by descending message ID so
        new mail wins, and use the strict shared extraction rules.
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        logger.info(f"[cf_temp] Waiting for OTP -> {email_addr} (timeout={timeout}s)")

        # Seed seen IDs from messages that predate this OTP request.
        #
        # Honor issued_after rather than relying only on an entry snapshot.
        # OpenAI may deliver a code immediately during authorize/continue,
        # before polling begins. Treat only earlier messages as old; conservatively
        # treat messages with unparseable timestamps as old as well.
        try:
            initial_mails = self._get_mails(email_addr)
            kept = 0
            for m in initial_mails:
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                if issued_after is not None:
                    ts = self._mail_epoch(m)
                    if ts is not None and ts >= issued_after - 2:
                        # This is a current-run candidate; do not mark it old.
                        kept += 1
                        continue
                self._seen_mail_ids.add(mid)
            logger.debug(
                f"[cf_temp] Initial mailbox contains {len(initial_mails)} messages; "
                f"skipped {len(initial_mails) - kept} old messages and retained {kept} candidates"
            )
        except Exception as e:
            logger.warning(f"[cf_temp] Failed to load the initial message list: {e}")

        while time.time() < deadline:
            try:
                mails = self._get_mails(email_addr)
                # Newest messages first.
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    self._seen_mail_ids.add(mid)

                    raw = str(mail.get("raw") or "")
                    otp = extract_otp(raw)
                    if otp:
                        logger.info(
                            f"[cf_temp] ✅ OTP={otp} from mail id={mid} "
                            f"raw_len={len(raw)}"
                        )
                        return otp
                    # Log non-matches for diagnostics.
                    logger.debug(
                        f"[cf_temp] mail id={mid} did not contain an OTP "
                        f"(subject={mail.get('subject','')[:50]})"
                    )
            except Exception as e:
                logger.warning(f"[cf_temp] Poll failed; retrying: {e}")
            time.sleep(3)

        raise TimeoutError(f"CFTempEmail OTP timeout {timeout}s for {email_addr}")

    # ──────────────────────── Self-test ────────────────────────

    def self_test(self) -> dict:
        """Create a mailbox to test Worker connectivity from the WebUI."""
        try:
            email = self.create_mailbox()
            return {"ok": True, "message": f"Connection successful. Test mailbox: {email}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
