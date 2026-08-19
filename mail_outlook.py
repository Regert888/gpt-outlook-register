"""Compatibility wrapper for Outlook OTP retrieval.

The implementation moved to mail_providers/outlook.py and now inherits the
shared MailProvider base class. This module retains the old public names so
these legacy imports continue to work:

    auth_flow.py:25         from mail_outlook import OutlookMailProvider as MailProvider
    webui/registrar.py:23   from mail_outlook import OutlookMailProvider
    register_outlook.py:33  from mail_outlook import OutlookMailProvider

New code should use::

    from mail_providers import create_mail_provider
    mail = create_mail_provider("outlook", settings, account)
"""
from __future__ import annotations

from mail_providers.outlook import (  # noqa: F401
    GRAPH_BASE,
    GRAPH_FOLDERS,
    GRAPH_SCOPE,
    GRAPH_TOKEN_URL,
    IMAP_HOST,
    IMAP_SCOPE,
    IMAP_SERVERS,
    TOKEN_ENDPOINTS,
    FatalOutlookMailError,
    OutlookMailProvider,
    _check_from_domain,
    _extract_otp_from_html,
    _graph_list_messages,
    _is_fatal_imap_error,
    _is_hex_color_context,
    _request_access_token,
    fetch_otp_via_graph,
    fetch_otp_via_imap,
    get_outlook_access_token,
)

__all__ = [
    "OutlookMailProvider",
    "FatalOutlookMailError",
    "fetch_otp_via_graph",
    "fetch_otp_via_imap",
    "get_outlook_access_token",
    "TOKEN_ENDPOINTS",
    "GRAPH_SCOPE",
    "IMAP_SCOPE",
    "GRAPH_BASE",
    "GRAPH_FOLDERS",
    "IMAP_SERVERS",
    "GRAPH_TOKEN_URL",
    "IMAP_HOST",
]


if __name__ == "__main__":
    import logging
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(_sys.argv) < 2:
        print(
            "usage: python mail_outlook.py "
            "'email----password----client_id----refresh_token'"
        )
        _sys.exit(2)
    parts = _sys.argv[1].split("----")
    if len(parts) != 4:
        print(f"Invalid 4-field format: got {len(parts)} fields")
        _sys.exit(2)
    e, p, c, r = parts
    prov = OutlookMailProvider(e, p, c, r)
    try:
        otp = prov.wait_for_otp(e, timeout=180)
        print(f"OTP: {otp}")
    except Exception as ex:
        print(f"ERR: {ex}")
        _sys.exit(1)
