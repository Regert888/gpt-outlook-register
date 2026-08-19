"""Abstract mail-provider layer: add new providers only in this directory.

Design goal:
    Adding a provider requires one new file and one registry import, with no
    changes to auth_flow, registrar, db, app, or auto_loop.

This mirrors BaseSmsProvider and create_sms_provider in sms_provider.py.

────────────────────────────────────────────────────────────
Two independent capability dimensions
────────────────────────────────────────────────────────────

    pooled     Accounts are purchased, finite resources that must be replaced
               when unusable. This controls fast-fail, mark_dead, claim,
               mark_done, and release behavior.

    ephemeral  Whether each run creates a fresh address. Fixed addresses may
               be recognized as existing accounts and routed to
               page_type='login_password', which requires a password.

    These dimensions are independent and all combinations are valid:

        provider            pooled  ephemeral   description
        ─────────────────── ─────── ─────────  ──────────────────────
        Outlook pool         True    False     imported fixed accounts
        CF catch-all         False   True      unlimited generated addresses
        Gmail / IMAP         True    False     same model as Outlook
        iCloud relay         False   False     fixed address without password

    Keeping both flags prevents a fixed iCloud relay address from bypassing
    pool logic while still being mistaken for a newly created account.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional


# ════════════════════════════════════════════════════════════
#  Exception types
# ════════════════════════════════════════════════════════════

class MailProviderError(Exception):
    """Common provider exception with structured severity.

    ``fatal=True`` means the account itself is unusable and registrar should
    mark it failed. ``fatal=False`` indicates an environmental or network
    problem, so registrar should release it back to the pool. This replaces
    brittle message sniffing in registrar.classify_error.
    """

    def __init__(self, message: str, *, fatal: bool = False, kind: str = ""):
        super().__init__(message)
        self.fatal = fatal
        self.kind = kind


class ImportValidationError(Exception):
    """Import text contains invalid lines.

    Includes each line number and reason so the WebUI can identify the exact
    problem. Rejecting the entire batch prevents silent partial imports.
    """

    def __init__(self, errors: list[dict]):
        self.errors = errors
        head = "; ".join(
            f"Line {e.get('line')}: {e.get('error')}" for e in errors[:5]
        )
        if len(errors) > 5:
            head += f"; …and {len(errors) - 5} more invalid lines"
        super().__init__(head or "Invalid import content")


# Per-import limits prevent accidentally pasting a huge file into the UI.
MAX_IMPORT_LINES = 5000
MAX_IMPORT_BYTES = 2 * 1024 * 1024


# ════════════════════════════════════════════════════════════
#  Self-describing configuration fields for dynamic WebUI forms
# ════════════════════════════════════════════════════════════

class ConfigField:
    """Metadata for one setting rendered dynamically by the WebUI."""

    def __init__(
        self,
        key: str,
        label: str,
        *,
        type: str = "text",          # text / password / number
        required: bool = True,
        placeholder: str = "",
        help: str = "",
    ):
        self.key = key
        self.label = label
        self.type = type
        self.required = required
        self.placeholder = placeholder
        self.help = help

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "placeholder": self.placeholder,
            "help": self.help,
        }


# ════════════════════════════════════════════════════════════
#  Shared OTP extraction utilities
# ════════════════════════════════════════════════════════════

_RE_SPAN_CODE = re.compile(r"<span[^>]*>\s*(\d{6})\s*</span>")
_RE_EMAIL_ADDR = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_RE_TS_BOUNDARY = re.compile(r"m=\+\d+\.\d+")
_RE_TS_PARAM = re.compile(r"\bt=\d+\b")
_RE_OTP6 = re.compile(r"(?<!#)(?<!\d)(\d{6})(?!\d)")


def extract_otp(raw: str, code_pattern: Optional[str] = None) -> Optional[str]:
    """Extract a six-digit OTP while avoiding common false matches.

    Prefer codes wrapped in ``<span>``; search after the MIME header; remove
    email addresses and timestamp parameters; and reject hexadecimal colors
    or digits embedded in longer numbers. All providers share these rules.
    """
    if not raw:
        return None

    m = _RE_SPAN_CODE.search(raw)
    if m:
        return m.group(1)

    body_start = raw.find("\r\n\r\n")
    text = raw[body_start:] if body_start != -1 else raw

    text = _RE_EMAIL_ADDR.sub("", text)
    text = _RE_TS_BOUNDARY.sub("", text)
    text = _RE_TS_PARAM.sub("", text)

    pattern = re.compile(code_pattern) if code_pattern else _RE_OTP6
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


# ════════════════════════════════════════════════════════════
#  Abstract base class
# ════════════════════════════════════════════════════════════

class MailProvider(ABC):
    """Base class for mail providers.

    Subclasses must implement create_mailbox() and wait_for_otp(); all other
    behavior has defaults that may be overridden.
    """

    # -- Identity ------------------------------------------------------------
    kind: str = "base"                   # Unique identifier stored as mail_source.
    display_name: str = "Unnamed"         # Label shown in WebUI selectors.

    # -- Capability declarations (see the module docstring) -----------------
    pooled: bool = False                 # Claim accounts from a finite pool.
    ephemeral: bool = False              # Create a fresh address for each run.

    # Whether an "already registered" response is acceptable. Purchased
    # accounts may use passwordless_login instead of being marked dead.
    accepts_existing_account: bool = False

    # -- Import format -------------------------------------------------------
    line_segments: int = 0               # Number of ---- fields; 0 disables import.
    import_hint: str = ""                # Format hint shown on the import page.
    import_placeholder: str = ""         # Example textarea placeholder.

    # -- Configuration fields used by the dynamic WebUI form ----------------
    config_fields: list[ConfigField] = []

    # ────────────────────────────────────────────────────
    # Required implementations
    # ────────────────────────────────────────────────────

    @abstractmethod
    def create_mailbox(self) -> str:
        """Return a fresh address when ephemeral, otherwise the held address."""
        ...

    @abstractmethod
    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """Block until a six-digit OTP arrives or raise TimeoutError.

        ``issued_after`` prevents cross-run code reuse by accepting only
        messages delivered after that time.
        """
        ...

    # ────────────────────────────────────────────────────
    # Optional non-destructive pre-read to avoid requesting an extra OTP
    # ────────────────────────────────────────────────────

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """Return this run's existing OTP without consuming its message.

        OpenAI may send a code as soon as it sees ``login_hint``, before the
        explicit email submission. A pre-read avoids requesting a duplicate.
        It must not block indefinitely, must return None rather than raise when
        no code exists, and must not add messages to the seen set. The default
        preserves request-then-wait behavior.
        """
        return None

    # ────────────────────────────────────────────────────
    #  Account-pool semantics; pooled subclasses may override
    # ────────────────────────────────────────────────────

    @property
    def exhausted(self) -> bool:
        """Whether the account is unusable due to delivery or credentials."""
        return getattr(self, "_dead", False)

    def mark_dead(self, reason: str = "") -> None:
        """Mark a pooled account unusable; non-pooled providers do nothing."""
        if self.pooled:
            self._dead = True

    # ────────────────────────────────────────────────────
    #  Import format; pooled providers may override
    # ────────────────────────────────────────────────────

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """Parse one import line into an account dictionary.

        Invalid input must raise ValueError with a reason so callers can attach
        the line number. The default splits by ``line_segments``, validates the
        email, and stores remaining values as seg1, seg2, and so on.
        """
        if cls.line_segments <= 0:
            raise ValueError(f"{cls.display_name} does not support account-pool imports")
        parts = [p.strip() for p in line.split("----")]
        if len(parts) != cls.line_segments:
            raise ValueError(
                f"Expected {cls.line_segments} fields separated by ----; got {len(parts)}"
            )
        validate_email(parts[0])
        out: dict[str, Any] = {"email": parts[0].lower(), "kind": cls.kind}
        for i, p in enumerate(parts[1:], start=1):
            out[f"seg{i}"] = p
        return out

    # ────────────────────────────────────────────────────
    #  Construction entry point
    # ────────────────────────────────────────────────────

    @classmethod
    def from_config(
        cls,
        settings: dict,
        account: Optional[dict] = None,
    ) -> "MailProvider":
        """Build from global settings and an optional claimed pool account.

        This is registrar's sole construction entry point. Subclasses must
        implement it; the default fails explicitly.
        """
        raise NotImplementedError(
            f"{cls.__name__} does not implement from_config()"
        )

    # ────────────────────────────────────────────────────
    #  Connectivity self-test used by the WebUI
    # ────────────────────────────────────────────────────

    def self_test(self) -> dict:
        """Return ``{"ok": bool, "message": str}``; default needs no test."""
        return {"ok": True, "message": f"{self.display_name} does not require a connectivity test"}

    # Class-level placeholder prevents AttributeError in future providers.
    last_persona = None

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} kind={self.kind} "
            f"pooled={self.pooled} ephemeral={self.ephemeral}>"
        )


# ════════════════════════════════════════════════════════════
#  Registry and factory
# ════════════════════════════════════════════════════════════

_PROVIDERS: dict[str, type[MailProvider]] = {}


def register(provider_cls: type[MailProvider]) -> type[MailProvider]:
    """Register a provider; may be used directly as a decorator::

        @register
        class MyMailProvider(MailProvider):
            kind = "my_mail"
    """
    key = (provider_cls.kind or "").strip().lower()
    if not key or key == "base":
        raise ValueError(f"{provider_cls.__name__} must define a unique kind")
    if key in _PROVIDERS and _PROVIDERS[key] is not provider_cls:
        raise ValueError(f"kind='{key}' is already registered by {_PROVIDERS[key].__name__}")
    _PROVIDERS[key] = provider_cls
    return provider_cls


def get_provider_class(kind: str) -> type[MailProvider]:
    """Return the provider class for kind and reject unknown values.

    Explicit failure avoids the old behavior where db.save_mail_config silently
    rewrote unknown providers to Outlook.
    """
    key = (kind or "").strip().lower()
    if key not in _PROVIDERS:
        known = ", ".join(sorted(_PROVIDERS)) or "(none)"
        raise MailProviderError(
            f"Unknown mail provider: '{kind}' (registered: {known})", fatal=True
        )
    return _PROVIDERS[key]


def create_mail_provider(
    kind: str,
    settings: dict,
    account: Optional[dict] = None,
) -> MailProvider:
    """Registrar's sole provider-construction entry point."""
    return get_provider_class(kind).from_config(settings, account)


def list_providers() -> list[dict]:
    """List registered providers and their WebUI-rendered capabilities."""
    out = []
    for key in sorted(_PROVIDERS):
        c = _PROVIDERS[key]
        out.append({
            "kind": c.kind,
            "display_name": c.display_name,
            "pooled": c.pooled,
            "ephemeral": c.ephemeral,
            "line_segments": c.line_segments,
            "import_hint": c.import_hint,
            "import_placeholder": c.import_placeholder,
            "config_fields": [f.to_dict() for f in c.config_fields],
        })
    return out


def list_pooled_providers() -> list[dict]:
    """List providers that are pooled and declare an import format.

    Both conditions matter: address-generating providers have nothing to
    import, while a non-pooled provider may still define a future parse format.
    """
    return [
        p for p in list_providers()
        if p["pooled"] and p["line_segments"] > 0
    ]


def validate_email(email: str) -> None:
    """Reject obvious email-format errors without full RFC validation."""
    em = (email or "").strip()
    if not em:
        raise ValueError("Email address is empty")
    if len(em) > 320:
        raise ValueError("Email address is too long")
    if em.count("@") != 1:
        raise ValueError(f"Invalid email address: {em[:60]}")
    local, domain = em.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError(f"Invalid email address: {em[:60]}")
    if any(ch.isspace() for ch in em):
        raise ValueError(f"Email address contains whitespace: {em[:60]}")


def parse_import_line(line: str, kind: str = "") -> dict:
    """Parse one import line or raise ValueError with a reason.

    A supplied kind selects one provider. Without it, field count is used only
    when it uniquely identifies a provider. The WebUI should request an
    explicit kind for ambiguous formats such as Outlook and Gmail.
    """
    line = (line or "").strip()
    if not line:
        raise ValueError("Empty line")

    if kind:
        return get_provider_class(kind).parse_line(line)

    seg_count = len(line.split("----"))
    candidates = [
        c for c in _PROVIDERS.values()
        if c.line_segments == seg_count and c.line_segments > 0
    ]
    if not candidates:
        known = sorted({
            c.line_segments for c in _PROVIDERS.values() if c.line_segments > 0
        })
        raise ValueError(
            f"Unrecognized {seg_count}-field format (known account-pool formats use {known} fields)"
        )
    if len(candidates) > 1:
        names = "/".join(c.display_name for c in candidates)
        raise ValueError(
            f"The {seg_count}-field format matches multiple providers ({names}); select the mail provider in the UI"
        )
    return candidates[0].parse_line(line)


def parse_import_text(text: str, kind: str = "") -> list[dict]:
    """Parse a batch atomically and reject it if any line is invalid.

    Duplicate addresses are detected here rather than left to a database-key
    conflict.
    """
    # Invalid kind is a batch-level error, so validate it before each line.
    if kind:
        cls = get_provider_class(kind)
        if cls.line_segments <= 0:
            raise MailProviderError(
                f"{cls.display_name} does not support account-pool imports because it creates its own addresses",
                fatal=True,
            )

    if not isinstance(text, str) or not text.strip():
        raise ImportValidationError([{"line": 0, "error": "Import content is empty"}])
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ImportValidationError([{"line": 0, "error": "Import content exceeds 2 MiB"}])

    # Strip BOM and skip blank/comment lines while preserving source line numbers.
    numbered = [
        (n, raw.strip())
        for n, raw in enumerate(text.replace("﻿", "", 1).splitlines(), 1)
        if raw.strip() and not raw.strip().startswith("#")
    ]
    if not numbered:
        raise ImportValidationError([{"line": 0, "error": "No importable content found"}])
    if len(numbered) > MAX_IMPORT_LINES:
        raise ImportValidationError(
            [{"line": 0, "error": f"A single import is limited to {MAX_IMPORT_LINES} lines"}]
        )

    errors: list[dict] = []
    rows: list[dict] = []
    seen: set[str] = set()
    for n, line in numbered:
        try:
            row = parse_import_line(line, kind)
            em = (row.get("email") or "").lower()
            if em in seen:
                raise ValueError(f"Duplicate email address: {em}")
            seen.add(em)
            rows.append(row)
        except (ValueError, MailProviderError) as e:
            errors.append({"line": n, "error": str(e)})

    if errors:
        raise ImportValidationError(errors)
    return rows
