"""SQLite 号池 + 注册结果存储。

表结构：
  outlook_accounts: 接码号池（4 段格式入库 + 状态机）
  registered:       注册成功结果（凭证 JSON）
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "webui.db"

_lock = threading.Lock()  # SQLite 写入串行化


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
            status          TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed
            imported_at     REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_outlook_status ON outlook_accounts(status);

        CREATE TABLE IF NOT EXISTS icloud_hme_addresses (
            email           TEXT PRIMARY KEY,
            label           TEXT,
            note            TEXT,
            anonymous_id    TEXT,
            is_active       INTEGER,
            state           TEXT NOT NULL DEFAULT 'available',
                            -- available / in_use / done / failed / trash
            source          TEXT,
            created_at      REAL,
            updated_at      REAL,
            synced_at       REAL,
            claimed_at      REAL,
            finished_at     REAL,
            fail_reason     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_icloud_hme_state ON icloud_hme_addresses(state);

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
    # 老 DB migrate：error_category 在后期才加，对已建表补列
    cur = con.execute("PRAGMA table_info(runs)")
    cols = {r[1] for r in cur.fetchall()}
    if "error_category" not in cols:
        con.execute("ALTER TABLE runs ADD COLUMN error_category TEXT")
        con.commit()


# ──────────────────────── outlook 号池 ────────────────────────


def parse_lines(text: str) -> list[dict]:
    """解析 4 段格式（每行一个）。无效行跳过。"""
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) != 4:
            continue
        email, password, client_id, refresh = (p.strip() for p in parts)
        if "@" not in email or len(refresh) < 20:
            continue
        out.append({
            "email": email.lower(),
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh,
        })
    return out


def import_accounts(text: str) -> dict:
    """批量入库。已存在的 email 仅在 refresh_token 不同时更新。"""
    rows = parse_lines(text)
    now = time.time()
    inserted = updated = skipped = 0
    with _lock:
        con = _conn()
        for r in rows:
            cur = con.execute(
                "SELECT refresh_token FROM outlook_accounts WHERE email=?",
                (r["email"],),
            )
            existing = cur.fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO outlook_accounts(email, password, client_id, refresh_token, "
                    "status, imported_at) VALUES (?, ?, ?, ?, 'available', ?)",
                    (r["email"], r["password"], r["client_id"], r["refresh_token"], now),
                )
                inserted += 1
            elif existing["refresh_token"] != r["refresh_token"]:
                con.execute(
                    "UPDATE outlook_accounts SET refresh_token=?, password=?, client_id=?, "
                    "status='available', imported_at=?, fail_reason=NULL WHERE email=?",
                    (r["refresh_token"], r["password"], r["client_id"], now, r["email"]),
                )
                updated += 1
            else:
                skipped += 1
        con.commit()
    return {"parsed": len(rows), "inserted": inserted, "updated": updated, "skipped": skipped}


def count_accounts(status: str = "") -> int:
    con = _conn()
    if status:
        cur = con.execute("SELECT COUNT(*) FROM outlook_accounts WHERE status=?", (status,))
    else:
        cur = con.execute("SELECT COUNT(*) FROM outlook_accounts")
    return cur.fetchone()[0]


def list_accounts(status: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    con = _conn()
    if status:
        cur = con.execute(
            "SELECT * FROM outlook_accounts WHERE status=? ORDER BY imported_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    else:
        cur = con.execute(
            "SELECT * FROM outlook_accounts ORDER BY imported_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [dict(r) for r in cur.fetchall()]


def get_account(email: str) -> Optional[dict]:
    con = _conn()
    cur = con.execute("SELECT * FROM outlook_accounts WHERE email=?", (email.lower(),))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_account(email: str) -> Optional[dict]:
    """原子 claim 指定邮箱（available / failed -> in_use）。

    failed 也允许重试 claim：之前 OpenAI 风控误判 / 网络抖动等导致 fail 的号
    应允许用户手动重试，已 done 的号才禁止重 claim（防误覆盖凭证）。
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


def claim_next() -> Optional[dict]:
    """原子 claim 任一 available 号。"""
    with _lock:
        con = _conn()
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
        if rc.rowcount != 1:
            return claim_next()
        return dict(row)


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
    """claim 后没真注册（异常 / 用户取消）→ 还回 available。"""
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE outlook_accounts SET status='available', claimed_at=NULL "
            "WHERE email=? AND status='in_use'",
            (email.lower(),),
        )
        con.commit()


def reset_to_available(email: str) -> bool:
    """手动重置单个号：done / failed → available，清空时间戳和失败原因。

    场景：注册成功但 refresh_token 没拿到，主人想重新跑一遍这个号。
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
    """批量重置多个号。返回实际被改的行数。"""
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
    """把所有 failed 号一次性重置为 available（清掉 fail_reason）。返回受影响行数。

    场景：代理短暂抽风导致一波号被冤枉标 failed，主人想给它们一次机会。
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
    """把 claimed_at 超过 N 秒还在 in_use 的号释放回 available。

    场景：上次 webui 强退/进程崩溃，号卡在 in_use 永远不释放。默认 30 分钟。
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
    """按状态批量删除。status 必须是 available/in_use/done/failed 之一；
    传 'all' 删全部。返回受影响行数。"""
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
    """按 email 列表批量删除。返回受影响行数。"""
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


# ──────────────────────── 注册结果存储 ────────────────────────


def save_registered(d: dict) -> None:
    """保存注册成功（或部分成功）的凭证。覆盖同邮箱旧记录。

    凭证三件套（access_token / session_token / refresh_token）单独存列；
    其余字段（如 device_id / cookie_header / id_token / 自定义元数据）打包进 extra_json。
    """
    email = (d.get("email") or "").lower()
    if not email:
        return
    extra = {k: v for k, v in d.items() if k not in {
        "email", "password", "access_token", "session_token", "refresh_token",
        "id_token", "device_id", "csrf_token", "cookie_header",
    }}
    with _lock:
        con = _conn()
        con.execute(
            "INSERT OR REPLACE INTO registered "
            "(email, password, access_token, session_token, refresh_token, "
            "id_token, device_id, csrf_token, cookie_header, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                email,
                d.get("password", ""),
                d.get("access_token", ""),
                d.get("session_token", ""),
                d.get("refresh_token", ""),
                d.get("id_token", ""),
                d.get("device_id", ""),
                d.get("csrf_token", ""),
                d.get("cookie_header", ""),
                json.dumps(extra, ensure_ascii=False) if extra else None,
                time.time(),
            ),
        )
        con.commit()


def update_plus_check(email: str, plus_info: dict) -> None:
    """把 Plus 检查结果写入 extra_json.plus_check。"""
    email = email.lower()
    con = _conn()
    cur = con.execute("SELECT extra_json FROM registered WHERE email=?", (email,))
    row = cur.fetchone()
    if not row:
        return
    extra = {}
    if row["extra_json"]:
        try:
            extra = json.loads(row["extra_json"])
        except Exception:
            extra = {}
    extra["plus_check"] = plus_info
    with _lock:
        con.execute(
            "UPDATE registered SET extra_json=? WHERE email=?",
            (json.dumps(extra, ensure_ascii=False), email),
        )
        con.commit()


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
    return ""


def count_registered(filter_rt: str = "all") -> int:
    con = _conn()
    cur = con.execute(f"SELECT COUNT(*) FROM registered {_registered_where(filter_rt)}")
    return cur.fetchone()[0]


def list_registered(limit: int = 20, offset: int = 0, filter_rt: str = "all") -> list[dict]:
    con = _conn()
    where = _registered_where(filter_rt)
    cur = con.execute(
        f"SELECT email, password, "
        f"length(access_token) AS at_len, length(session_token) AS st_len, "
        f"length(refresh_token) AS rt_len, extra_json, created_at FROM registered "
        f"{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        plus = None
        if d.get("extra_json"):
            try:
                extra = json.loads(d["extra_json"])
                plus = extra.get("plus_check")
            except Exception:
                pass
        d["plus_check"] = plus
        d.pop("extra_json", None)
        rows.append(d)
    return rows


def list_registered_full(limit: int = 5000) -> list[dict]:
    """返回完整凭证（用于批量导出）。每行同 get_registered 的格式。"""
    con = _conn()
    cur = con.execute(
        "SELECT * FROM registered ORDER BY created_at DESC LIMIT ?",
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


# ──────────────────────── 运行记录 ────────────────────────


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


def update_run_email(run_id: str, email: str) -> None:
    """运行开始后才知道真实邮箱时更新 runs.email（例如 iCloud HME 自动 claim）。"""
    if not run_id or not email:
        return
    with _lock:
        con = _conn()
        con.execute("UPDATE runs SET email=? WHERE run_id=?", (email.lower(), run_id))
        con.commit()


# ──────────────────────── iCloud HME 地址池 ────────────────────────


ICLOUD_HME_STATES = {"available", "in_use", "done", "failed", "trash"}


def _clean_icloud_state(state: str, default: str = "available") -> str:
    s = (state or default).strip().lower()
    return s if s in ICLOUD_HME_STATES else default


def upsert_icloud_address(d: dict, preserve_terminal: bool = True) -> None:
    """新增/同步 iCloud HME 地址。

    preserve_terminal=True 时，同步不会覆盖 in_use/done/failed 等本地流程状态。
    """
    email = (d.get("email") or d.get("hme") or "").strip().lower()
    if not email:
        return
    now = time.time()
    incoming_state = _clean_icloud_state(d.get("state") or ("available" if d.get("is_active", True) else "trash"))
    is_active = 1 if bool(d.get("is_active", True)) else 0
    with _lock:
        con = _conn()
        cur = con.execute("SELECT state, created_at FROM icloud_hme_addresses WHERE email=?", (email,))
        existing = cur.fetchone()
        if existing:
            state = existing["state"] or "available"
            if not preserve_terminal or state in ("available", "trash"):
                state = incoming_state
            con.execute(
                "UPDATE icloud_hme_addresses SET label=?, note=?, anonymous_id=?, "
                "is_active=?, state=?, source=?, updated_at=?, synced_at=?, fail_reason=CASE "
                "WHEN ? IN ('available','trash') THEN NULL ELSE fail_reason END WHERE email=?",
                (
                    d.get("label", ""),
                    d.get("note", ""),
                    d.get("anonymous_id") or d.get("anonymousId") or "",
                    is_active,
                    state,
                    d.get("source") or "icloud",
                    now,
                    now,
                    state,
                    email,
                ),
            )
        else:
            con.execute(
                "INSERT INTO icloud_hme_addresses(email, label, note, anonymous_id, is_active, "
                "state, source, created_at, updated_at, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    email,
                    d.get("label", ""),
                    d.get("note", ""),
                    d.get("anonymous_id") or d.get("anonymousId") or "",
                    is_active,
                    incoming_state,
                    d.get("source") or "icloud",
                    d.get("created_at") or now,
                    now,
                    now,
                ),
            )
        con.commit()


def sync_icloud_addresses(rows: list[dict]) -> dict:
    """把 iCloud list 返回的 rows 同步到本地地址池。"""
    synced = active = inactive = 0
    for row in rows or []:
        email = (row.get("hme") or row.get("email") or "").strip().lower()
        if not email:
            continue
        is_active = bool(row.get("isActive", row.get("is_active", True)))
        created_at = None
        ts = row.get("createTimestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            created_at = ts / 1000.0
        upsert_icloud_address({
            "email": email,
            "label": row.get("label") or "",
            "note": row.get("note") or "",
            "anonymous_id": row.get("anonymousId") or row.get("anonymous_id") or "",
            "is_active": is_active,
            "state": "available" if is_active else "trash",
            "source": "icloud",
            "created_at": created_at,
        })
        synced += 1
        if is_active:
            active += 1
        else:
            inactive += 1
    return {"synced": synced, "active": active, "inactive": inactive}


def claim_icloud_available(email: str = "") -> Optional[dict]:
    """原子 claim iCloud HME 地址。"""
    email = (email or "").strip().lower()
    with _lock:
        con = _conn()
        if email:
            cur = con.execute(
                "SELECT * FROM icloud_hme_addresses WHERE email=? AND state IN ('available','failed') AND is_active != 0",
                (email,),
            )
        else:
            cur = con.execute(
                "SELECT * FROM icloud_hme_addresses WHERE state='available' AND is_active != 0 "
                "ORDER BY COALESCE(created_at, synced_at, updated_at, 0) ASC LIMIT 1"
            )
        row = cur.fetchone()
        if not row:
            return None
        rc = con.execute(
            "UPDATE icloud_hme_addresses SET state='in_use', claimed_at=?, fail_reason=NULL "
            "WHERE email=? AND state IN ('available','failed')",
            (time.time(), row["email"]),
        )
        con.commit()
        if rc.rowcount != 1:
            return None
        return dict(row)


def mark_icloud_done(email: str) -> None:
    if not email:
        return
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE icloud_hme_addresses SET state='done', finished_at=?, fail_reason=NULL WHERE email=?",
            (time.time(), email.lower()),
        )
        con.commit()


def mark_icloud_failed(email: str, reason: str = "") -> None:
    if not email:
        return
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE icloud_hme_addresses SET state='failed', finished_at=?, fail_reason=? WHERE email=?",
            (time.time(), (reason or "")[:500], email.lower()),
        )
        con.commit()


def release_icloud_unused(email: str) -> None:
    if not email:
        return
    with _lock:
        con = _conn()
        con.execute(
            "UPDATE icloud_hme_addresses SET state='available', claimed_at=NULL "
            "WHERE email=? AND state='in_use'",
            (email.lower(),),
        )
        con.commit()


def count_icloud_addresses(state: str = "") -> int:
    con = _conn()
    s = _clean_icloud_state(state, "") if state else ""
    if s:
        cur = con.execute("SELECT COUNT(*) FROM icloud_hme_addresses WHERE state=?", (s,))
    else:
        cur = con.execute("SELECT COUNT(*) FROM icloud_hme_addresses")
    return cur.fetchone()[0]


def list_icloud_addresses(state: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    con = _conn()
    s = _clean_icloud_state(state, "") if state else ""
    if s:
        cur = con.execute(
            "SELECT * FROM icloud_hme_addresses WHERE state=? "
            "ORDER BY COALESCE(updated_at, synced_at, created_at, 0) DESC LIMIT ? OFFSET ?",
            (s, limit, offset),
        )
    else:
        cur = con.execute(
            "SELECT * FROM icloud_hme_addresses "
            "ORDER BY COALESCE(updated_at, synced_at, created_at, 0) DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [dict(r) for r in cur.fetchall()]


def reset_icloud_address(email: str) -> bool:
    if not email:
        return False
    with _lock:
        con = _conn()
        rc = con.execute(
            "UPDATE icloud_hme_addresses SET state='available', claimed_at=NULL, finished_at=NULL, "
            "fail_reason=NULL WHERE email=? AND is_active != 0",
            (email.lower(),),
        )
        con.commit()
        return rc.rowcount > 0


def delete_icloud_address(email: str) -> bool:
    if not email:
        return False
    with _lock:
        con = _conn()
        rc = con.execute("DELETE FROM icloud_hme_addresses WHERE email=?", (email.lower(),))
        con.commit()
        return rc.rowcount > 0


def icloud_stats() -> dict:
    con = _conn()
    cur = con.execute("SELECT state, COUNT(*) AS n FROM icloud_hme_addresses GROUP BY state")
    out = {"available": 0, "in_use": 0, "done": 0, "failed": 0, "trash": 0, "total": 0}
    for r in cur.fetchall():
        out[r["state"]] = r["n"]
        out["total"] += r["n"]
    return out


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


# ──────────────────────── 邮箱来源配置 ────────────────────────


def get_mail_config() -> dict:
    """返回邮箱来源配置（密文字段隐藏明文）。"""
    return {
        "mail_source":   get_setting("mail_source", "outlook"),  # outlook / cf_temp / icloud_hme
        "cf_api_url":    get_setting("cf_api_url", ""),
        "cf_admin_token": "***" if get_setting("cf_admin_token") else "",
        "cf_domain":     get_setting("cf_domain", ""),
        "icloud_region":          get_setting("icloud_region", "global"),
        "icloud_cookie":          "***" if get_setting("icloud_cookie") else "",
        "icloud_maildomain_host": get_setting("icloud_maildomain_host", ""),
        "icloud_label_prefix":    get_setting("icloud_label_prefix", "gpt-register"),
        "icloud_note":            get_setting("icloud_note", "Generated by gpt-outlook-register"),
        "icloud_auto_generate":   get_setting("icloud_auto_generate", "1"),
        "icloud_imap_host":       get_setting("icloud_imap_host", ""),
        "icloud_imap_port":       get_setting("icloud_imap_port", "993"),
        "icloud_imap_username":   get_setting("icloud_imap_username", ""),
        "icloud_imap_password":   "***" if get_setting("icloud_imap_password") else "",
        "icloud_imap_folder":     get_setting("icloud_imap_folder", "INBOX"),
        "icloud_imap_use_ssl":    get_setting("icloud_imap_use_ssl", "1"),
    }


def save_mail_config(data: dict) -> None:
    """保存邮箱配置。密文字段传 '***' 表示不修改。"""
    if "mail_source" in data:
        src = str(data["mail_source"]).strip().lower()
        if src not in ("outlook", "cf_temp", "icloud_hme"):
            src = "outlook"
        set_setting("mail_source", src)
    if "cf_api_url" in data:
        set_setting("cf_api_url", str(data["cf_api_url"]).strip())
    if "cf_domain" in data:
        set_setting("cf_domain", str(data["cf_domain"]).strip())
    if data.get("cf_admin_token") and data["cf_admin_token"] != "***":
        set_setting("cf_admin_token", str(data["cf_admin_token"]).strip())
    if "icloud_region" in data:
        region = str(data["icloud_region"]).strip().lower()
        if region not in ("global", "china"):
            region = "global"
        set_setting("icloud_region", region)
    for key in (
        "icloud_maildomain_host", "icloud_label_prefix", "icloud_note",
        "icloud_imap_host", "icloud_imap_port", "icloud_imap_username",
        "icloud_imap_folder",
    ):
        if key in data:
            set_setting(key, str(data[key] or "").strip())
    for key in ("icloud_auto_generate", "icloud_imap_use_ssl"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    if data.get("icloud_cookie") and data["icloud_cookie"] != "***":
        set_setting("icloud_cookie", str(data["icloud_cookie"]).strip())
    if data.get("icloud_imap_password") and data["icloud_imap_password"] != "***":
        set_setting("icloud_imap_password", str(data["icloud_imap_password"]).strip())


def get_cf_admin_token() -> str:
    """内部用：拿明文 admin_token。"""
    return get_setting("cf_admin_token", "")


def get_icloud_config() -> dict:
    """内部用：拿 iCloud HME 明文配置。"""
    return {
        "icloud_region":          get_setting("icloud_region", "global"),
        "icloud_cookie":          get_setting("icloud_cookie", ""),
        "icloud_maildomain_host": get_setting("icloud_maildomain_host", ""),
        "icloud_label_prefix":    get_setting("icloud_label_prefix", "gpt-register"),
        "icloud_note":            get_setting("icloud_note", "Generated by gpt-outlook-register"),
        "icloud_auto_generate":   get_setting("icloud_auto_generate", "1") in ("1", "true"),
        "icloud_imap_host":       get_setting("icloud_imap_host", ""),
        "icloud_imap_port":       get_setting("icloud_imap_port", "993"),
        "icloud_imap_username":   get_setting("icloud_imap_username", ""),
        "icloud_imap_password":   get_setting("icloud_imap_password", ""),
        "icloud_imap_folder":     get_setting("icloud_imap_folder", "INBOX"),
        "icloud_imap_use_ssl":    get_setting("icloud_imap_use_ssl", "1") in ("1", "true"),
    }


# ──────────────────────── SMS 接码配置 ────────────────────────


def get_sms_config() -> dict:
    """返回 SMS 接码配置（api_key 隐藏明文）。

    sms_enabled:        '0'/'1' 是否启用接码（命中 add-phone 时才会用）
    sms_provider:       smsbower
    sms_country:        国家代码或 ID（推荐 '52' = Thailand，OpenAI 走 SMS 的唯一稳定国家）
    sms_service:        服务代码（OpenAI = 'dr'）
    sms_max_price:      号码最高单价（SmsBower / SmsBower 用，单位平台货币；空 / -1 = 不限）
    sms_reuse_phone:    '0'/'1' 同号复用（SmsBower / SmsBower 支持，省钱）
    sms_phone_success_max: 同号最多复用几次（默认 3）
    sms_auto_country:   '0'/'1' 自动选最优国家（按价格 + 库存）
    sms_auto_min_stock: 自动选国家最低库存（默认 20）
    sms_auto_max_price: 自动选国家最高单价（默认 0 = 不限）
    """
    return {
        "sms_enabled":             get_setting("sms_enabled", "0"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             "***" if get_setting("sms_api_key") else "",
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
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
    """保存 SMS 配置。sms_api_key 传 '***' 表示不修改。"""
    # 校验 provider
    valid_providers = {"smsbower", "herosms"}
    if "sms_provider" in data:
        p = str(data["sms_provider"]).strip().lower()
        if p not in valid_providers:
            p = "smsbower"
        set_setting("sms_provider", p)
    # 字符串字段直接落
    for key in (
        "sms_country", "sms_service", "sms_max_price",
        "sms_phone_success_max", "sms_auto_min_stock", "sms_auto_max_price",
        "sms_max_phone_attempts", "sms_per_phone_timeout",
        "sms_allowed_countries",
    ):
        if key in data:
            set_setting(key, str(data[key]).strip())
    # 布尔字段（前端传 '0'/'1' 或 bool）
    for key in ("sms_enabled", "sms_reuse_phone", "sms_auto_country", "sms_strict_whitelist"):
        if key in data:
            v = data[key]
            if isinstance(v, bool):
                set_setting(key, "1" if v else "0")
            else:
                s = str(v).strip().lower()
                set_setting(key, "1" if s in ("1", "true", "yes", "on") else "0")
    # API key（'***' 不修改）
    if data.get("sms_api_key") and data["sms_api_key"] != "***":
        set_setting("sms_api_key", str(data["sms_api_key"]).strip())


def get_sms_internal_config() -> dict:
    """内部用：拿明文 sms_api_key,供 sms_provider 实例化使用。"""
    return {
        "sms_enabled":             get_setting("sms_enabled", "0") in ("1", "true"),
        "sms_provider":            get_setting("sms_provider", "smsbower"),
        "sms_api_key":             get_setting("sms_api_key", ""),
        "sms_country":             get_setting("sms_country", "52"),
        "sms_service":             get_setting("sms_service", "dr"),
        "sms_max_price":           get_setting("sms_max_price", ""),
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


# ──────────────────────── 自动导出配置 (CPA / SUB2API) ────────────────────────


def get_export_config() -> dict:
    """返回导出配置（敏感字段做明文/'***' 占位）。

    给前端展示用：
      cpa_mgmt_key / sub2api_api_key 已设置时返回 '***'，未设置返回 ''。
      保存时传 '***' 代表不修改。
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
    """保存导出配置。密文字段传 '***' 表示不修改。"""
    # 布尔开关
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
    # 字符串字段（明文）
    for key_in, key_out in (
        ("cpa_url",            "export_cpa_url"),
        ("cpa_timeout",        "export_cpa_timeout"),
        ("sub2api_url",        "export_sub2api_url"),
        ("sub2api_group_ids",  "export_sub2api_group_ids"),
        ("sub2api_timeout",    "export_sub2api_timeout"),
    ):
        if key_in in data:
            set_setting(key_out, str(data[key_in] or "").strip())
    # 密文字段（'***' 不修改）
    if data.get("cpa_mgmt_key") and data["cpa_mgmt_key"] != "***":
        set_setting("export_cpa_mgmt_key", str(data["cpa_mgmt_key"]).strip())
    if data.get("sub2api_api_key") and data["sub2api_api_key"] != "***":
        set_setting("export_sub2api_api_key", str(data["sub2api_api_key"]).strip())


def get_export_internal_config() -> dict:
    """内部用：拿明文密钥 + 解析后的 enabled 布尔。供 registrar / app.test 调用。

    返回两个子配置 dict，可分别传给 exporter.export_to_cpa / export_to_sub2api。
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


# 模块加载时自动建表
init_db()
