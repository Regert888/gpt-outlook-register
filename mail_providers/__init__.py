"""Mail provider package: add new providers only in this directory.

This is the only entry point used by registrar, app, and auto_loop::

    from mail_providers import create_mail_provider, list_providers

    mail = create_mail_provider("outlook", settings, account)
    mail = create_mail_provider("cf_temp", settings)

To add a new mail provider:

    1. Create mail_providers/xxx.py, inherit MailProvider, and implement
       create_mailbox() / wait_for_otp() / from_config()
    2. Add one import to the registration section at the bottom of this file.

No core-library changes are required in auth_flow, registrar, db, app, or
auto_loop.
"""
from __future__ import annotations

from .base import (  # noqa: F401
    ConfigField,
    ImportValidationError,
    MailProvider,
    MailProviderError,
    create_mail_provider,
    extract_otp,
    get_provider_class,
    list_pooled_providers,
    list_providers,
    parse_import_line,
    parse_import_text,
    register,
    validate_email,
)

# ════════════════════════════════════════════════════════════
#  Registration section: add one import here for each provider.
#  Importing the module triggers its @register decorator.
# ════════════════════════════════════════════════════════════

from . import outlook        # noqa: F401,E402  kind="outlook"
from . import cf_temp        # noqa: F401,E402  kind="cf_temp"
from . import icloud_relay   # noqa: F401,E402  kind="icloud_relay"

__all__ = [
    "MailProvider",
    "MailProviderError",
    "ImportValidationError",
    "ConfigField",
    "register",
    "get_provider_class",
    "create_mail_provider",
    "list_providers",
    "list_pooled_providers",
    "parse_import_line",
    "parse_import_text",
    "validate_email",
    "extract_otp",
]
