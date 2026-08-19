#!/usr/bin/env python3
"""HTTP-only ChatGPT registration using an Outlook mailbox.

Uses a four-field Outlook account and the HTTP protocol stack (curl_cffi,
Sentinel PoW, and IMAP) to drive OpenAI's authorization state machine without
a browser, Camoufox, or Playwright.

Four-field account format, separated by ----::
    email----password----client_id----microsoft_refresh_token

Usage::
    python register_outlook.py 'xxx@outlook.jp----<pwd>----<client_id>----M.C538_...'

Optional environment variables:
    PROXY                Egress proxy URL, e.g. socks5://user:pass@host:port
    OTP_TIMEOUT          OTP wait time in seconds (default 60, minimum 30)
    WEBUI_ALLOW_LOGIN    1 = use OTP login when OpenAI identifies the email as
                         already registered. Default 0 fails fast and moves
                         to the next account.
    SKIP_OAUTH_TOKEN_EXCHANGE  1 = skip OAuth refresh-token exchange
    AUTH_HTTP_TRACE      1 = print every HTTP request for debugging
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from mail_outlook import OutlookMailProvider  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: python register_outlook.py "
            "'email----password----client_id----refresh_token'",
            file=sys.stderr,
        )
        sys.exit(2)

    parts = sys.argv[1].split("----")
    if len(parts) != 4:
        print(f"Invalid 4-field format: got {len(parts)} fields", file=sys.stderr)
        sys.exit(2)
    email, password, client_id, refresh = parts
    logger.info(
        f"Account: {email}  client_id={client_id[:8]}…  refresh_token len={len(refresh)}"
    )

    cfg = Config()
    cfg.proxy = os.environ.get("PROXY") or None

    mail = OutlookMailProvider(
        email=email, password=password,
        client_id=client_id, refresh_token=refresh,
    )

    flow = AuthFlow(cfg)
    logger.info("[auth_flow] Starting run_register (HTTP protocol + Outlook IMAP)...")
    partial = False
    try:
        result = flow.run_register(mail)
        d = result.to_dict()
    except RuntimeError as e:
        # Preserve partial credentials when any token was obtained.
        d = flow.result.to_dict()
        if d.get("access_token") or d.get("refresh_token") or d.get("session_token"):
            partial = True
            logger.warning(f"[register] Registration flow failed: {e}")
            logger.warning("[register] Partial credentials were obtained and will be saved")
        else:
            raise

    logger.info(
        f"[register] Completed email={d.get('email')} "
        f"access_token=len{len(d.get('access_token') or '')} "
        f"session_token=len{len(d.get('session_token') or '')} "
        f"refresh_token=len{len(d.get('refresh_token') or '')}"
    )

    out_path = ROOT / f"account_{email.replace('@', '_at_')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    tag = " (partial credentials; session_token missing, possibly because phone verification is required)" if partial else ""
    print(f"\n=== DONE{tag} ===\nAccount credentials written to: {out_path}")


if __name__ == "__main__":
    main()
