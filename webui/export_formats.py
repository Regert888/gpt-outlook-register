"""Registry of batch export formats.

Add formats only to FORMATS; backend routes and the frontend menu derive from it.

Modes:
  - `text`: one record per line, with preview/copy/download via `render`
  - `download`: a complete byte document from `render_all`, without preview

Rules:
- Never skip rows. Empty fields remain empty while delimiters are preserved.
- Row order follows the registration-results table (descending created_at).

Manual CPA/SUB2API export was removed because those APIs ingest one account at a
time and require a Codex-style token refreshed first. Use automatic push through
`exporter.run_exports` after registration.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportFormat:
    id: str                                       # Unique frontend command ID.
    label: str                                    # Dropdown label.
    filename: str                                 # Download filename.
    mode: str = "text"                            # "text" | "download"
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None          # One record -> one text line.
    render_all: Optional[Callable[[list], bytes]] = None    # Entire batch -> file bytes.
    note: str = ""                                # Secondary dropdown description.


def _s(row: dict, key: str) -> str:
    """Return a field as a stripped string, safely handling None/non-strings."""
    v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


# ----------------------------------- Registry -----------------------------------


FORMATS: list[ExportFormat] = [
    ExportFormat(
        id="at",
        label="access_token",
        filename="AT.txt",
        render=lambda r: _s(r, "access_token"),
    ),
    ExportFormat(
        id="email_pw",
        label="Email----Password",
        filename="email-password.txt",
        render=lambda r: f'{_s(r, "email")}----{_s(r, "password")}',
    ),
    # The one-time 2FA secret cannot be recovered, so provide an export that keeps
    # it. Accounts without 2FA retain a blank field and its delimiter.
    ExportFormat(
        id="email_pw_2fa",
        label="Email----Password----2FA",
        filename="email-password-2fa.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----{_s(r, "totp_secret")}'
        ),
        note="The secret is issued once and cannot be recovered; store it securely",
    ),
    # This adds the relay inbox URL, LEFT JOINed from outlook_accounts. Only relay
    # providers have a value; deleted pool rows remain blank. The embedded token
    # grants inbox access, so exported files must be protected like passwords.
    ExportFormat(
        id="email_pw_2fa_relay",
        label="Email----Password----2FA----Relay URL",
        filename="email-password-2fa-relay-url.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "password")}----'
            f'{_s(r, "totp_secret")}----{_s(r, "relay_url")}'
        ),
        note="The relay URL contains a token that grants inbox access; store it securely",
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    """Return frontend metadata without renderer callables."""
    return [
        {
            "id": f.id,
            "label": f.label,
            "filename": f.filename,
            "mode": f.mode,
            "mime": f.mime,
            "note": f.note,
        }
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[ExportFormat]:
    return _BY_ID.get((fmt_id or "").strip())


def render_text(rows: list, fmt: "ExportFormat | str") -> str:
    """Render one text record per line.

    A per-record rendering error leaves that line blank without failing the batch.
    """
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"Unknown export format: {fmt}")
    if not f.render:
        raise RuntimeError(f"Format {f.id} is not a text format")

    lines = []
    for r in rows or []:
        try:
            lines.append(f.render(r))
        except Exception:
            lines.append("")
    return "\n".join(lines)


def render_bytes(rows: list, fmt: "ExportFormat | str") -> bytes:
    """Render an entire downloadable document as bytes."""
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"Unknown export format: {fmt}")
    if not f.render_all:
        raise RuntimeError(f"Format {f.id} is not a download format")
    return f.render_all(rows or [])


# Backward-compatible function name.
def render(rows: list, fmt: "ExportFormat | str") -> str:
    return render_text(rows, fmt)
