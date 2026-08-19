"""Compatibility wrapper for the Cloudflare Worker temporary-mail provider.

The implementation moved to mail_providers/cf_temp.py and now inherits the
shared MailProvider base class. This module retains the old public names so
these legacy imports continue to work:

    webui/app.py:379        from mail_cf import CFTempEmailProvider
    webui/registrar.py:166  from mail_cf import CFTempEmailProvider

New code should use::

    from mail_providers import create_mail_provider
    mail = create_mail_provider("cf_temp", settings)
"""
from __future__ import annotations

from mail_providers.cf_temp import (  # noqa: F401
    CFTempEmailProvider,
    _extract_otp,
    _gen_local_part,
)

__all__ = ["CFTempEmailProvider"]


if __name__ == "__main__":
    # Command-line test: python mail_cf.py <api_url> <admin_token> <domain>
    import logging
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 4:
        print("usage: python mail_cf.py <api_url> <admin_token> <domain>")
        sys.exit(2)
    p = CFTempEmailProvider(api_url=sys.argv[1], admin_token=sys.argv[2], domain=sys.argv[3])
    email = p.create_mailbox()
    print(f"Created mailbox: {email}")
    print("Waiting for OTP (120s)...")
    try:
        code = p.wait_for_otp(email, timeout=120)
        print(f"OTP: {code}")
    except TimeoutError as e:
        print(f"Timed out: {e}")
        sys.exit(1)
