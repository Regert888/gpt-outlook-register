"""SQLite account-pool and registration-result storage.

Tables:
  outlook_accounts: mixed-provider account pool, distinguished by kind and state
  registered:       successful registration results and credentials

The historical outlook_accounts name now covers Outlook, Gmail, iCloud, and
other providers. Renaming it would require a risky migration for no behavioral
benefit; the kind column is authoritative.

Provider credentials use a union of typed columns rather than extra_json.
Outlook/Gmail use password, client_id, and refresh_token; relay providers use
relay_url and leave unrelated columns empty. Typed columns remain indexable,
constrainable, and visible to SQL; add a column when a provider needs new data.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DB_PATH = Path(__file__).resolve().parent / "webui.db"

_lock = threading.Lock()  # Serialize SQLite writes.


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = _conn()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS outlook_accounts (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            client_id       TEXT,
            refresh_token   TEXT,
            relay_url       TEXT,       -- Relay inbox URL; blank for other providers
            kind            TEXT NOT NULL DEFAULT 'outlook',
                            -- Provider type matching the mail_providers registry kind
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);
        -- Create idx_outlook_kind only after old databases receive the kind column.

        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT
        );

        CREATE TABLE IF NOT EXISTS registered (
            email           TEXT PRIMARY KEY,
            password        TEXT,
            access_token    TEXT,
            session_token   TEXT,
            refresh_token   TEXT,
            id_token        TEXT,
            device_id       TEXT,
            csrf_token      TEXT,
            cookie_header   TEXT,
            totp_secret     TEXT,
            totp_factor_id  TEXT,
            extra_json      TEXT,
            created_at      REAL
        );

        CREATE TABLE IF NOT EXISTS runs (
            run_id          TEXT PRIMARY KEY,
            email           TEXT,
            status          TEXT,        -- running / done / failed
            started_at      REAL,
            finished_at     REAL,
            log_path        TEXT,
            error           TEXT,
            error_category  TEXT         -- network / account / unknown
        );
    """)
    con.commit()
    # Old-database migration: error_category was added after the original schema.
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()

    # Old-database migration: kind and relay_url were added for mixed providers.
    # Existing rows predate that change and are Outlook, so the default is correct.
    # The migration is idempotent and needs no follow-up UPDATE.
    cur = con.execute("PRAGMA table_info(outlook_accounts)")
    acc_cols = {r[1] for r in cur.fetchall()}
    if "kind" not in acc_cols:
        con.execute(
            "ALTER TABLE outlook_accounts ADD COLUMN kind TEXT NOT NULL DEFAULT 'outlook'"
        )
        con.commit()
    if "relay_url" not in acc_cols:
        con.execute("ALTER TABLE outlook_accounts ADD COLUMN relay_url TEXT")
        con.commit()
    # Create the index after adding kind so old databases do not fail.
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outlook_kind ON outlook_accounts(kind, status)"
    )
    con.commit()

    # Old-database migration: registered gained TOTP secret/factor columns later.
    # Persist the one-time secret explicitly; repeated migration is harmless.
    cur = con.execute("PRAGMA table_info(registered)")
    reg_cols = {r[1] for r in cur.fetchall()}
    if "totp_secret" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_secret TEXT")
        con.commit()
    if "totp_factor_id" not in reg_cols:
        con.execute("ALTER TABLE registered ADD COLUMN totp_factor_id TEXT")
        con.commit()
    con.close()


# ------------------------------- Outlook account pool -------------------------------


def parse_lines(text: str, kind: str = "") -> list[dict]:
    """Parse import text through the mail-provider registry.

    An explicit kind selects that provider's parser. Otherwise infer only when
    the field count is unique; Outlook and Gmail both use four fields.

    Invalid lines raise ImportValidationError with line numbers and reasons;
    silently skipping them would make a reported successful batch incomplete.
    """
    from mail_providers import parse_import_text

    return parse_import_text(text or "", kind)


def import_accounts(text: str, kind: str = "") -> dict:
    """Import a batch, updating existing emails only when credentials change.

    Parse the entire batch before writing. Any invalid line rejects the whole
    import, preventing partially written batches.
    """
    rows = parse_lines(text, kind)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            row_kind = r.get("kind") or kind or "outlook"
            # Providers use different subsets of the credential union; others stay blank.
            password = r.get("password", "") or ""
            client_id = r.get("client_id", "") or ""
            refresh = r.get("refresh_token", "") or ""
            relay = r.get("relay_url", "") or ""

            cur = con.execute(
                "SELECT refresh_token, relay_url, kind FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "relay_url, kind, status, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'available', ?)",
                    (r["email"], password, client_id, refresh, relay, row_kind, now),
                )
                inserted += 1
            elif (
                (existing["refresh_token"] or "") != refresh
                or (existing["relay_url"] or "") != relay
                or (existing["kind"] or "") != row_kind
            ):
                # Credential or provider changes reset the account to available.
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "relay_url=?, kind=?, status='available', imported_at=?, fail_reason=NULL "
                    "WHERE email=?",
                    (refresh, password, client_id, relay, row_kind, now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "", kind: str = "") -> int:
    con = _conn()
    sql = "SELECT COUNT(*) FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    return con.execute(sql, args).fetchone()[0]


def list_accounts(
    status: str = "", limit: int = 50, offset: int = 0, kind: str = ""
) -> list[dict]:
    con = _conn()
    sql = "SELECT * FROM outlook_accounts"
    where, args = [], []
    if status:
        where.append("status=?")
        args.append(status)
    if kind:
        where.append("kind=?")
        args.append(kind.strip().lower())
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY imported_at DESC LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def stats_by_kind() -> dict:
    """Return account counts by provider kind for the WebUI summary."""
    con = _conn()
    cur = con.execute(
        "SELECT kind, status, COUNT(*) AS n FROM outlook_accounts GROUP BY kind, status"
    )
    out: dict[str, dict] = {}
    for r in cur.fetchall():
        k = r["kind"] or "outlook"
        slot = out.setdefault(
            k, {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
        )
        slot[r["status"]] = r["n"]
        slot["total"] += r["n"]
    return out


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """Atomically claim a specified available or failed account.

    Failed accounts remain manually retryable after transient risk-control or
    network errors. Done accounts cannot be reclaimed to protect credentials.

    A direct email claim does not filter by kind; callers inspect account["kind"].
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    with _lock:
        con = _conn()
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE email=? AND status IN ('available', 'failed')",
            (email,),
        )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE outlook_accounts SET status='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND status IN ('available', 'failed')",
            (time.time(), email),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def claim_next(kind: str = "") -> Optional[dict]:
    """Atomically claim the next available account.

    With kind, select only that provider. Without it, select the oldest import
    across the full pool.

    Provider filtering prevents mixed Outlook/Gmail pools from crossing sources.
    """
    k = (kind or "").strip().lower()
    with _lock:
        con = _conn()
        for _ in range(50):  # Bounded retries avoid recursion during contention.
            if k:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' AND kind=? "
                    "ORDER BY imported_at ASC LIMIT 1",
                    (k,),
                )
            else:
                cur = con.execute(
                    "SELECT * FROM outlook_accounts WHERE status='available' "
                    "ORDER BY imported_at ASC LIMIT 1"
                )
            row = cur.fetchone()
            if not row:
                return None
            rc = con.execute(
                "UPDATE outlook_accounts SET status='in_use', claimed_at=? "
                "WHERE email=? AND status='available'",
                (time.time(), row["email"]),
            )
            con.commit()
            if rc.rowcount == 1:
                return dict(row)
            # Another thread won the claim; try the next account.
        return None


def mark_done(email: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_failed(email: str, reason: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_unused(email: str) -> None:
    """Return an unregistered claim to available after cancellation or error."""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """Reset one done/failed account to available and clear outcome metadata.

    This supports rerunning an account whose registration completed without a
    refresh token.
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            "finished_at=NULL, fail_reason=NULL "
            "WHERE lower(email)=lower(?)",
            (email,),
        )
        con.commit()
        return rc.rowcount > 0


def bulk_reset_to_available(emails: list[str]) -> int:
    """Reset multiple accounts and return the number of changed rows."""
    if not emails:
        return 0
    with _lock:
        con = _conn()
        rc = con.execute(
            f"UPDATE outlook_accounts SET status='available', claimed_at=NULL, "
            f"finished_at=NULL, fail_reason=NULL "
            f"WHERE lower(email) IN ({','.join(['lower(?)'] * len(emails))})",
            emails,
        )
        con.commit()
        return rc.rowcount


def reset_failed_to_available() -> int:
    """Reset all failed accounts to available and clear fail_reason.

    Useful when a transient proxy outage incorrectly marks a batch failed.
    """
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', fail_reason=NULL, "
            "finished_at=NULL WHERE status='failed'"
        )
        con.commit()
        return rc.rowcount


def release_stale_in_use(stale_seconds: float = 1800) -> int:
    """Release accounts left in_use beyond the configured age.

    This recovers claims stranded by a crashed or forcibly stopped WebUI process.
    """
    with _lock:
        con = _conn()
        cutoff = time.time() - stale_seconds
        rc = con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE status='in_use' AND (claimed_at IS NULL OR claimed_at < ?)",
            (cutoff,),
        )
        con.commit()
        return rc.rowcount


def delete_account(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM outlook_accounts WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_accounts_by_status(status: str) -> int:
    """Delete by state (available/in_use/done/failed), or all; return row count."""
    valid = {"available", "in_use", "done", "failed", "all"}
    s = (status or "").strip().lower()
    if s not in valid:
        return 0
    with _lock:
        con = _conn()
        if s == "all":
            rc = con.execute("DELETE FROM outlook_accounts")
        else:
            rc = con.execute("DELETE FROM outlook_accounts WHERE status=?", (s,))
        con.commit()
        return rc.rowcount


def delete_accounts_by_emails(emails: list[str]) -> int:
    """Delete the listed emails and return the number of changed rows."""
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM outlook_accounts WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def stats() -> dict:
    con = _conn()
    cur = con.execute(
        "SELECT status, COUNT(*) AS n FROM outlook_accounts GROUP BY status"
    )
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "total": 0}
    for r in cur.fetchall():
        out[r["status"]] = r["n"]
        out["total"] += r["n"]
    return out


# ----------------------------- Registration-result storage -----------------------------


def save_registered(d: dict) -> None:
    """Save complete or partial credentials, replacing the same email's record.

    Store the three main tokens in dedicated columns and pack remaining fields,
    such as device_id, cookie_header, id_token, and custom metadata, into extra_json.
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    password = d.get("password", "") or ""
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
        "totp_secret", "totp_factor_id",
    }}
    with _lock:
        con = _conn()
        # INSERT OR REPLACE replaces the whole row, so preserve durable values that
        # a rerun may omit. A password set before an OTP timeout still exists even
        # when a later passwordless login returns no password. Likewise, a one-time
        # TOTP secret cannot be recovered and must never be overwritten with blank.
        # Tokens are intentionally replaced because each run may issue fresh values.
        totp_secret = (d.get("totp_secret") or "").strip()
        totp_factor_id = (d.get("totp_factor_id") or "").strip()
        if not password or not totp_secret:
            row = con.execute(
                "SELECT password, totp_secret, totp_factor_id FROM registered WHERE email=?",
                (email,),
            ).fetchone()
            if row:
                if not password and (row["password"] or "").strip():
                    password = row["password"]
                if not totp_secret and (row["totp_secret"] or "").strip():
                    totp_secret = row["totp_secret"]
                    # Preserve factor_id with its secret when this run did not enroll.
                    totp_factor_id = totp_factor_id or (row["totp_factor_id"] or "")
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                password,
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                totp_secret,
                totp_factor_id,
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()
        con.close()


def save_password_early(email: str, password: str) -> None:
    """Persist a password as soon as OpenAI accepts it.

    AuthFlow invokes this callback immediately after register_password returns
    HTTP 200. Later delivery, verification, or account-creation failures must not
    leave a valid remote password only in process memory.

    Initially write only email and password with a pending marker. A successful
    save_registered call fills the same primary-key row and clears pending state.

    For existing rows, update only password so reruns do not erase tokens.
    """
    email = (email or "").strip().lower()
    password = (password or "").strip()
    if not email or not password:
        return
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, '', '', '', '', '', '', '', ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET password=excluded.password",
            (
                email,
                password,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def save_totp_early(email: str, secret: str, factor_id: str = "") -> None:
    """Persist a TOTP secret as soon as enrollment returns it.

    registrar invokes this between session creation and later Codex/SMS steps.

    The secret is issued once and cannot be recovered. Persist it before later
    authorization or SMS work so process termination cannot permanently lose it.

    Update only the two TOTP columns for existing rows; preserve passwords/tokens.
    """
    email = (email or "").strip().lower()
    secret = (secret or "").strip()
    if not email or not secret:
        return
    factor_id = (factor_id or "").strip()
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, "
            "totp_secret, totp_factor_id, extra_json, created_at) "
            "VALUES (?, '', '', '', '', '', '', '', '', ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "totp_secret=excluded.totp_secret, "
            "totp_factor_id=excluded.totp_factor_id",
            (
                email,
                secret,
                factor_id,
                json.dumps({"pending": True}, ensure_ascii=False),
                time.time(),
            ),
        )
        con.commit()


def normalize_totp_secret(raw: str) -> str:
    """Normalize a user-entered TOTP secret to valid base32.

    The login path decodes without validation, so reject bad values before they
    enter storage and fail later with an opaque decoding error.

    Accepted input:
      - Raw base32: JBSWY3DPEHPK3PXP / jbswy3dp ehpk 3pxp / JBSW-Y3DP-EHPK
      - otpauth URI: otpauth://totp/ChatGPT:a@b.com?secret=JBSWY3DP&issuer=...
        (commonly pasted from an authenticator export or decoded QR code)
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # Extract the secret parameter from an otpauth URI.
    if s.lower().startswith("otpauth://"):
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(s).query)
            s = (qs.get("secret") or [""])[0]
        except Exception:
            raise ValueError(
                "Could not parse the otpauth URL; enter the secret directly"
            )
        if not s:
            raise ValueError("The otpauth URL does not contain a secret parameter")
    # Remove display separators and normalize to uppercase.
    s = s.replace(" ", "").replace("-", "").replace("_", "").upper()
    # Reject invalid base32 characters before decoding for a clearer error.
    if not s or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in s):
        raise ValueError(
            "TOTP secret contains invalid characters (base32 allows only A-Z and 2-7)"
        )
    try:
        # Add padding exactly as auth_flow does and validate by decoding.
        decoded = base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception:
        raise ValueError("TOTP secret is not valid base32")
    if len(decoded) < 10:
        raise ValueError(
            f"TOTP secret is too short ({len(decoded)} decoded bytes; usually 20 are expected)"
        )
    return s


def update_registered_manual(email: str, password: Optional[str] = None,
                             totp_secret: Optional[str] = None) -> bool:
    """Correct a registered account's locally stored password or TOTP secret.

    This changes only the local database, not the remote OpenAI credential. It
    records an externally known value or corrects a local record.

    None leaves a field unchanged; an empty string explicitly clears it.

    Validate totp_secret before writing so bad values do not fail during login.

    Return False when the email does not exist; this correction path never inserts.
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    sets, vals = [], []
    if password is not None:
        sets.append("password=?")
        vals.append(password)
    if totp_secret is not None:
        # Empty explicitly clears; non-empty values must validate.
        sets.append("totp_secret=?")
        vals.append(normalize_totp_secret(totp_secret) if totp_secret.strip() else "")
    if not sets:
        return False
    with _lock:
        con = _conn()
        row = con.execute("SELECT email FROM registered WHERE email=?", (email,)).fetchone()
        if not row:
            return False
        vals.append(email)
        con.execute(f"UPDATE registered SET {', '.join(sets)} WHERE email=?", vals)
        con.commit()
        return True


_ELIGIBILITY_KEYS = frozenset({"plus_check", "gcash_check"})
_SAFE_ELIGIBILITY_FIELDS = frozenset({
    "operation", "classification", "eligible", "decision", "conclusive",
    "retryable", "status", "label", "checked_at", "current_plan_type",
    "subscription_plan", "has_active_subscription", "campaign_id",
    "campaign_title", "discount_percentage", "duration_periods",
    "duration_unit", "method_available", "custom_method_id_discovered",
    "amount_minor", "currency",
})


def _safe_eligibility_result(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("eligibility result must be an object")
    return {
        key: item for key, item in value.items()
        if key in _SAFE_ELIGIBILITY_FIELDS
        and isinstance(item, (str, int, float, bool, type(None)))
    }


def update_eligibility_check(email: str, key: str, result: dict) -> None:
    """Persist a sanitized eligibility result while retaining the last verdict."""
    if key not in _ELIGIBILITY_KEYS:
        raise ValueError("unsupported eligibility result key")
    email = email.strip().lower()
    safe = _safe_eligibility_result(result)
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                "SELECT extra_json FROM registered WHERE email=?", (email,)
            ).fetchone()
            if not row:
                return
            extra = {}
            if row["extra_json"]:
                try:
                    extra = json.loads(row["extra_json"])
                except Exception:
                    extra = {}
            previous = extra.get(key) if isinstance(extra.get(key), dict) else {}
            last_conclusive = None
            if safe.get("conclusive"):
                last_conclusive = dict(safe)
            elif previous.get("conclusive"):
                last_conclusive = _safe_eligibility_result(previous)
            elif isinstance(previous.get("last_conclusive"), dict):
                last_conclusive = _safe_eligibility_result(previous["last_conclusive"])
            if last_conclusive:
                safe["last_conclusive"] = last_conclusive
            extra[key] = safe
            con.execute(
                "UPDATE registered SET extra_json=? WHERE email=?",
                (json.dumps(extra, ensure_ascii=False), email),
            )
            con.commit()
        finally:
            con.close()


def update_plus_check(email: str, plus_info: dict) -> None:
    """Backward-compatible wrapper for existing Plus-check callers."""
    update_eligibility_check(email, "plus_check", plus_info)


def _registered_where(filt: str) -> str:
    if filt == "has_rt":
        return "WHERE length(refresh_token) > 0"
    if filt == "no_rt":
        return "WHERE coalesce(length(refresh_token),0) = 0"
    if filt == "unchecked":
        return "WHERE (extra_json IS NULL OR extra_json NOT LIKE '%\"plus_check\"%')"
    if filt == "free":
        return "WHERE extra_json LIKE '%\"free\"%'"
    if filt == "plus":
        return "WHERE (extra_json LIKE '%\"plus_eligible\"%' OR extra_json LIKE '%\"plus_active\"%')"
    if filt == "banned":
        return "WHERE extra_json LIKE '%\"banned\"%'"
    if filt == "token_invalid":
        # token_invalid is conclusive and needs its own filter; it belongs neither
        # to unchecked nor to free/plus/banned.
        return "WHERE extra_json LIKE '%\"token_invalid\"%'"
    return ""


def count_registered(filter_rt: str = "all") -> int:
    con = _conn()
    cur = con.execute(f"SELECT COUNT(*) FROM registered {_registered_where(filter_rt)}")
    return cur.fetchone()[0]


def list_registered(limit: int = 20, offset: int = 0, filter_rt: str = "all") -> list[dict]:
    con = _conn()
    where = _registered_where(filter_rt)
    cur = con.execute(
        f"SELECT email, password, totp_secret, "
        f"length(access_token) AS at_len, length(session_token) AS st_len, "
        f"length(refresh_token) AS rt_len, extra_json, created_at FROM registered "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    fetched = cur.fetchall()
    con.close()
    rows = []
    for r in fetched:
        d = dict(r)
        plus = None
        gcash = None
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
                gcash = extra.get("gcash_check")
            except Exception:
                pass
        d["plus_check"] = plus
        d["gcash_check"] = gcash
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_registered_full(limit: int = 5000) -> list[dict]:
    """Return full credentials for batch export, including relay_url.

    relay_url lives on outlook_accounts because relay providers assign one
    tokenized inbox URL per pooled account. LEFT JOIN avoids migration, supports
    old registered rows while their pool row exists, and yields blank otherwise.
    """
    con = _conn()
    cur = con.execute(
        "SELECT r.*, a.relay_url AS relay_url "
        "FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
        "ORDER BY r.created_at DESC LIMIT ?",
        (limit,),
    )
    out = []
    for row in cur.fetchall():
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = {}
        d.pop("extra_json", None)
        out.append(d)
    return out


def list_registered_by_emails(emails: list[str]) -> list[dict]:
    """Return full credentials for selected emails during batch export.

    Results use descending created_at order, omit missing emails, batch below
    SQLite's variable limit, and LEFT JOIN relay_url as described above.
    """
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return []

    con = _conn()
    out = []
    CHUNK = 500
    for i in range(0, len(cleaned), CHUNK):
        part = cleaned[i:i + CHUNK]
        placeholders = ",".join("?" * len(part))
        cur = con.execute(
            f"SELECT r.*, a.relay_url AS relay_url "
            f"FROM registered r LEFT JOIN outlook_accounts a ON a.email = r.email "
            f"WHERE r.email IN ({placeholders})",
            part,
        )
        for row in cur.fetchall():
            d = dict(row)
            if d.get("extra_json"):
                try:
                    d["extra"] = json.loads(d["extra_json"])
                except Exception:
                    d["extra"] = {}
            d.pop("extra_json", None)
            out.append(d)

    out.sort(key=lambda d: d.get("created_at") or 0, reverse=True)
    return out


def get_registered(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM registered WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    if not row:
        return None
    out = dict(row)
    if out.get("extra_json"):
        try:
            out["extra"] = json.loads(out["extra_json"])
        except Exception:
            out["extra"] = {}
    out.pop("extra_json", None)
    return out


def delete_registered(email: str) -> bool:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def delete_registered_by_emails(emails: list[str]) -> int:
    cleaned = [e.strip().lower() for e in (emails or []) if e and e.strip()]
    if not cleaned:
        return 0
    with _lock:
        con = _conn()
        placeholders = ",".join("?" * len(cleaned))
        rc = con.execute(
            f"DELETE FROM registered WHERE email IN ({placeholders})",
            cleaned,
        )
        con.commit()
        return rc.rowcount


def delete_all_registered() -> int:
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM registered")
        con.commit()
        return rc.rowcount


# ----------------------------------- Run records -----------------------------------


def create_run(run_id: str, email: str, log_path: str) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO runs(run_id, email, status, started_at, log_path) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, email.lower(), time.time(), log_path),
        )
        con.commit()


def finish_run(run_id: str, status: str, error: str = "", category: str = "") -> None:
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE runs SET status=?, finished_at=?, error=?, error_category=? WHERE run_id=?",
            (status, time.time(), (error or "")[:500], category or None, run_id),
        )
        con.commit()


def list_runs(limit: int = 50) -> list[dict]:
    con = _conn()
    cur = con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────── settings (KV) ────────────────────────


def get_setting(key: str, default: str = "") -> str:
    con = _conn()
    cur = con.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_setting(key: str, value) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        con.commit()


# ---------------------------- Email source configuration ----------------------------


def get_mail_config() -> dict:
    """Return email-source settings with secret fields masked.

    Provider-declared config_fields appear automatically for new providers.
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if f.get("type") == "password":
                out[key] = "***" if get_setting(key) else ""
            else:
                out[key] = get_setting(key, "")
    return out


def save_mail_config(data: dict) -> None:
    """Save email settings; '***' leaves password-like fields unchanged.

    Validate mail_source against the provider registry so unknown values fail
    explicitly instead of silently reverting to Outlook.
    """
    from mail_providers import get_provider_class, list_providers

    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        get_provider_class(src)  # Unregistered kinds raise MailProviderError.
        set_setting("mail_source", src)

    # Save provider-declared fields so new providers need no changes here.
    for p in list_providers():
        for f in p["config_fields"]:
            key = f["key"]
            if key not in data:
                continue
            val = data[key]
            if f.get("type") == "password":
                if not val or val == "***":
                    continue  # Missing or masked: keep the current value.
            set_setting(key, str(val).strip())


def get_secret_setting(key: str) -> str:
    """Return an unmasked secret setting for internal use."""
    return get_setting(key, "")


def get_mail_settings() -> dict:
    """Return unmasked settings for create_mail_provider.

    Unlike get_mail_config, this is server-only and must never be returned directly.
    """
    from mail_providers import list_providers

    out = {"mail_source": get_setting("mail_source", "outlook")}
    for p in list_providers():
        for f in p["config_fields"]:
            out[f["key"]] = get_setting(f["key"], "")
    return out


def get_cf_admin_token() -> str:
    """Return the unmasked admin_token for internal use."""
    return get_setting("cf_admin_token", "")


# ----------------------------- SMS verification settings -----------------------------


def get_sms_config() -> dict:
    """Return SMS verification settings with api_key masked.

    sms_enabled:        '0'/'1'; used only when the flow reaches add-phone
    sms_provider:       smsbower
    sms_country:        country code or provider ID ('52' = Thailand)
    sms_service:        service code (OpenAI = 'dr')
    sms_max_price:      maximum unit price; blank/-1 means unlimited
    sms_reuse_phone:    '0'/'1' number reuse
    sms_phone_success_max: maximum successful reuse count (default 3)
    sms_auto_country:   '0'/'1' choose by price and inventory
    sms_auto_min_stock: minimum inventory for automatic choice (default 20)
    sms_auto_max_price: maximum automatic-choice price (0 means unlimited)
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


def save_sms_config(data: dict) -> None:
    """Save SMS settings; '***' leaves sms_api_key unchanged."""
    # Validate provider.
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # Store string fields directly.
    for key in (
        "sms_country", "sms_service", "sms_max_price", "sms_fixed_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # Boolean fields accept frontend '0'/'1' strings or bool values.
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # '***' preserves the existing API key.
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """Return unmasked SMS settings for internal provider construction."""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
        "sms_fixed_price":         get_setting("sms_fixed_price", ""),
        "sms_reuse_phone":         get_setting("sms_reuse_phone", "0") in ("1", "true"),
        "sms_phone_success_max":   get_setting("sms_phone_success_max", "3"),
        "sms_auto_country":        get_setting("sms_auto_country", "0") in ("1", "true"),
        "sms_strict_whitelist":    get_setting("sms_strict_whitelist", "0") in ("1", "true"),
        "sms_allowed_countries":   get_setting("sms_allowed_countries", ""),
        "sms_auto_min_stock":      get_setting("sms_auto_min_stock", "20"),
        "sms_auto_max_price":      get_setting("sms_auto_max_price", ""),
        "sms_max_phone_attempts":  get_setting("sms_max_phone_attempts", ""),
        "sms_per_phone_timeout":   get_setting("sms_per_phone_timeout", "80"),
    }


# -------------------------- Automatic export configuration --------------------------


def get_export_config() -> dict:
    """Return export settings with configured secrets represented by '***'.

    The frontend receives '***' for configured keys and '' otherwise; sending
    '***' when saving leaves a key unchanged.
    """
    return {
        # CPA
        "cpa_enabled":     get_setting("export_cpa_enabled", "0"),
        "cpa_url":         get_setting("export_cpa_url", ""),
        "cpa_mgmt_key":    "***" if get_setting("export_cpa_mgmt_key") else "",
        "cpa_timeout":     get_setting("export_cpa_timeout", "30"),
        # SUB2API
        "sub2api_enabled":    get_setting("export_sub2api_enabled", "0"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    "***" if get_setting("export_sub2api_api_key") else "",
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }


def save_export_config(data: dict) -> None:
    """Save export settings; '***' leaves secret fields unchanged."""
    # Boolean switches.
    for key_in, key_out in (
        ("cpa_enabled",     "export_cpa_enabled"),
        ("sub2api_enabled", "export_sub2api_enabled"),
    ):
        if key_in in data:
            v = data[key_in]
            if isinstance(v, bool):
                set_setting(key_out, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key_out, "1" if s in ("1", "true", "yes", "on") else "0")
    # Plaintext string fields.
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # Secret fields preserve their values when masked.
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """Return unmasked keys and parsed enabled flags for internal callers.

    The two child dictionaries can be passed to the respective exporter functions.
    """
    cpa = {
        "enabled":      get_setting("export_cpa_enabled", "0") in ("1", "true"),
        "cpa_url":      get_setting("export_cpa_url", ""),
        "cpa_mgmt_key": get_setting("export_cpa_mgmt_key", ""),
        "cpa_timeout":  get_setting("export_cpa_timeout", "30"),
    }
    sub2api = {
        "enabled":            get_setting("export_sub2api_enabled", "0") in ("1", "true"),
        "sub2api_url":        get_setting("export_sub2api_url", ""),
        "sub2api_api_key":    get_setting("export_sub2api_api_key", ""),
        "sub2api_group_ids":  get_setting("export_sub2api_group_ids", "2"),
        "sub2api_timeout":    get_setting("export_sub2api_timeout", "30"),
    }
    return {"cpa": cpa, "sub2api": sub2api}


# Initialize tables when the module loads.
init_db()
