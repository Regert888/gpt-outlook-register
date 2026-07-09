"""Outlook 邮箱 OTP 取码（IMAP XOAUTH2 纯协议）。

从 4 段接码格式（email----password----client_id----refresh_token）出发：
  1. refresh_token + client_id → Microsoft v2 token endpoint 换 IMAP access_token
  2. IMAP XOAUTH2 登 outlook.office365.com:993，扫 INBOX/Junk/Spam
  3. 校验 From 必须是 OpenAI 域 + 排除 tm1.openai.com 影子发码域
  4. 正则抽 6 位 OTP，避开 hex 颜色 / tracking id 假阳性

适配 browser_register.py 的 MailProvider 接口：
  - create_mailbox()  → 返回固定邮箱地址
  - wait_for_otp()    → 阻塞拉 OTP
  - last_persona      → None（不算法生成 persona）
"""
from __future__ import annotations

import email as _email
import email.utils as _eu
import imaplib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
IMAP_HOST = "outlook.office365.com"


class FatalOutlookMailError(RuntimeError):
    """Non-retryable Outlook mail error.

    覆盖范围：IMAP XOAUTH2 鉴权失败 / mailbox 不可用（"authenticated but not
    connected"）/ refresh token invalid_grant / client 拒绝 等。命中即跳过
    resend retry，由调用方 fast-fail 跳到下一个号。
    """


_FATAL_IMAP_ERROR_PATTERNS = (
    "user is authenticated but not connected",
    "authentication failed",
    "authenticate failed",
    "imap xoauth2",
    "invalid_grant",
    "invalid_client",
)


def _is_fatal_imap_error(exc: Exception) -> bool:
    msg = str(exc or "").lower()
    return any(p in msg for p in _FATAL_IMAP_ERROR_PATTERNS)


# ──────────────────────── Microsoft OAuth refresh_token → access_token ────────────────────────


def get_outlook_access_token(refresh_token: str, client_id: str) -> str:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": IMAP_SCOPE,
    }).encode()
    req = urllib.request.Request(GRAPH_TOKEN_URL, data=body)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(text or "{}")
        except Exception:
            data = {"error": text[:500]}
        if e.code in (400, 401, 403):
            raise FatalOutlookMailError(f"outlook refresh failed: {data}") from e
        raise
    data = json.loads(resp.read())
    if not data.get("access_token"):
        raise FatalOutlookMailError(f"outlook refresh failed: {data}")
    return data


# ──────────────────────── OTP 抽取 ────────────────────────


def _is_hex_color_context(haystack: str, idx: int) -> bool:
    """排除 OpenAI 邮件里 #353740 / #10A37F 这类品牌色误判。"""
    if idx > 0 and haystack[idx - 1] == "#":
        return True
    before = haystack[max(0, idx - 30):idx]
    return bool(re.search(
        r"(?:color|background|bgcolor|fill|stroke)\s*[:=]\s*[\"']?#?\s*$",
        before, re.IGNORECASE,
    ))


def _extract_otp_from_html(body: str) -> Optional[str]:
    """先语义匹配 (code is 123456 / chatgpt / openai)，再 fallback \\b\\d{6}\\b。"""
    for pat in (
        r"(?:code(?:\s*is)?|verification|one[-\s]*time|verify|kode|verifikasi|代码|验证码|驗證碼)[^\d<>]{0,80}(\d{6})\b",
        r"chatgpt[^\d<>]{0,80}(\d{6})",
        r"openai[^\d<>]{0,80}(\d{6})",
    ):
        for m in re.finditer(pat, body, re.IGNORECASE | re.DOTALL):
            if not _is_hex_color_context(body, m.start(1)):
                return m.group(1)
    for m in re.finditer(r"\b(\d{6})\b", body):
        if not _is_hex_color_context(body, m.start(1)):
            return m.group(1)
    return None


# ──────────────────────── IMAP fetch ────────────────────────


def fetch_otp_via_imap(
    email_addr: str,
    refresh_token: str,
    client_id: str,
    timeout: int = 240,
    threshold_ts: float = 0,
) -> str:
    """阻塞拉 outlook OTP（OpenAI 来的最新邮件）。返回 6 位 OTP 或抛 TimeoutError。

    扫描多 folder：INBOX、Junk、Junk Email、Spam。outlook 反垃圾经常把 OpenAI
    第一次发给陌生收件人的验证码邮件直接 route 到 Junk，单查 INBOX 会假装"未收到"。
    """
    deadline = time.time() + max(60, timeout)
    if not threshold_ts:
        threshold_ts = time.time() - 300  # 5min grace
    seen: set = set()
    cached_token: str = ""
    cached_refresh: str = refresh_token
    cached_at: float = 0.0
    folders_to_scan = ["INBOX", "Junk", "Junk Email", "Spam"]
    found_folders: list[str] | None = None  # LIST 探测一次就缓存

    while time.time() < deadline:
        try:
            if not cached_token or time.time() - cached_at > 3000:
                data = get_outlook_access_token(cached_refresh, client_id)
                cached_token = data["access_token"]
                cached_at = time.time()
                # outlook 滚动 refresh_token，更新缓存
                if data.get("refresh_token"):
                    cached_refresh = data["refresh_token"]

            M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
            auth_string = f"user={email_addr}\x01auth=Bearer {cached_token}\x01\x01"
            try:
                typ, _ = M.authenticate("XOAUTH2", lambda x: auth_string.encode())
                if typ != "OK":
                    raise RuntimeError("imap XOAUTH2 失败")
            except imaplib.IMAP4.error as e:
                # "User is authenticated but not connected." / "mailbox unavailable"
                # 等 fatal 模式由下方 _is_fatal_imap_error 统一识别为
                # FatalOutlookMailError，跳过 resend retry，调用方 fast-fail。
                if _is_fatal_imap_error(e):
                    try:
                        M.logout()
                    except Exception:
                        pass
                    raise FatalOutlookMailError(
                        f"outlook IMAP XOAUTH2 不可用 ({email_addr}): {e}"
                    ) from e
                raise

            # 探测真实 folder 名（不同 outlook 区域 Junk 命名不同）
            if found_folders is None:
                try:
                    typ, listing = M.list()
                    names_lower: dict[str, str] = {}
                    for raw in (listing or []):
                        if not raw:
                            continue
                        s = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
                        m = re.search(r'"([^"]+)"\s*$', s) or re.search(r"\s(\S+)\s*$", s)
                        if m:
                            nm = m.group(1).strip('"')
                            names_lower[nm.lower()] = nm
                    picked: list[str] = []
                    for cand in folders_to_scan:
                        real = names_lower.get(cand.lower())
                        if real and real not in picked:
                            picked.append(real)
                    for k, v in names_lower.items():
                        if any(x in k for x in ("junk", "spam", "bulk")) and v not in picked:
                            picked.append(v)
                    if "INBOX" not in picked:
                        picked.insert(0, "INBOX")
                    found_folders = picked
                    logger.info(f"[outlook-imap] {email_addr} folders to scan: {found_folders}")
                except Exception as e:
                    logger.warning(f"[outlook-imap] LIST 失败，回退默认列表: {e}")
                    found_folders = list(folders_to_scan)

            for folder in found_folders:
                try:
                    sel_arg = f'"{folder}"' if " " in folder else folder
                    typ, _ = M.select(sel_arg, readonly=True)
                    if typ != "OK":
                        continue
                except Exception:
                    continue
                try:
                    # SEARCH ALL + python 层 From 校验（嵌套 OR 查询在 O365 触发 BAD）
                    typ, data = M.search(None, "ALL")
                    ids = (data[0].split() if data and data[0] else [])
                except Exception as e:
                    logger.warning(f"[outlook-imap] SEARCH 失败 folder={folder}: {e}")
                    continue
                for mid in reversed(ids[-8:]):
                    key = (folder, mid)
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        typ, raw = M.fetch(mid, "(BODY.PEEK[])")
                        msg = _email.message_from_bytes(raw[0][1])
                    except Exception:
                        continue
                    date_str = msg.get("Date") or ""
                    try:
                        msg_ts = _eu.parsedate_to_datetime(date_str).timestamp()
                    except Exception:
                        msg_ts = 0
                    if msg_ts and msg_ts < threshold_ts:
                        continue
                    from_field = (msg.get("From") or "").lower()
                    if not any(d in from_field for d in (
                        "openai.com", "auth.openai", "tm.openai", "chatgpt.com",
                        "tm.open",  # SendGrid 中转子域 em*.tm.open
                    )):
                        continue
                    # tm1.openai.com 是坏掉的影子发码域，OTP 全是 493682 → 必失败
                    if "tm1.openai" in from_field:
                        logger.info(
                            f"[outlook-imap] skip tm1.openai.com 影子发码: id={mid.decode()} "
                            f"from={from_field[:60]}"
                        )
                        continue
                    text_body = ""
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                payload = part.get_payload(decode=True) or b""
                                text_body += payload.decode(
                                    part.get_content_charset() or "utf-8",
                                    errors="replace",
                                ) + "\n"
                            except Exception:
                                continue
                    otp = _extract_otp_from_html(text_body)
                    if otp:
                        logger.info(
                            f"[outlook-imap] {email_addr} OTP 命中 folder={folder!r} "
                            f"msg_ts={int(msg_ts)} otp={otp}"
                        )
                        try:
                            M.logout()
                        except Exception:
                            pass
                        return otp
            try:
                M.logout()
            except Exception:
                pass
        except FatalOutlookMailError:
            # 永久错误：不重试，直接冒泡给 wait_for_otp → mark dead
            raise
        except Exception as e:
            if _is_fatal_imap_error(e):
                raise FatalOutlookMailError(
                    f"outlook IMAP account unusable for {email_addr}: {e}"
                ) from e
            logger.warning(f"[outlook-imap] fetch_otp 异常 (吃掉重试): {e}")
        time.sleep(4)
    raise TimeoutError(f"outlook OTP timeout {timeout}s for {email_addr}")


# ──────────────────────── MailProvider 适配（browser_register 接口） ────────────────────────


class OutlookMailProvider:
    """auth_flow / browser_register 通用的 MailProvider 最小实现。

    构造时直接持有 4 段 outlook 凭证，无 DB / 池子。
    暴露 `_outlook_creds`、`mark_outlook_dead`、`outlook_exhausted` 字段供
    auth_flow.run_register / run_protocol_login 识别本邮箱为 outlook 池来源
    并在 OpenAI 反欺诈静默拒发 OTP 时 fast-fail。
    """

    def __init__(self, email: str, password: str, client_id: str, refresh_token: str):
        self.email = email
        self.password = password
        self.client_id = client_id
        self.refresh_token = refresh_token
        # browser_register 复用 last_persona 取密码 / 姓名；outlook 模式不算法生成 → None
        self.last_persona = None
        self.catch_all_domain = email.split("@", 1)[1]
        # auth_flow 用这两个判定：邮箱来自 outlook 池 → 静默拒发 OTP 时 fast-fail
        self._outlook_creds = {
            "email": email,
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        self.outlook_exhausted = False

    def mark_outlook_dead(self, reason: str = "") -> None:
        """auth_flow 在 OpenAI 静默拒发 OTP 时调用；纯协议版无 DB，仅打日志。"""
        logger.warning(f"[mail] outlook {self.email} mark dead: {reason}")
        self.outlook_exhausted = True

    def create_mailbox(self) -> str:
        logger.info(f"[mail] 使用 outlook 账号: {self.email}")
        return self.email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞拉 OTP。timeout 上调至 ≥90s（OpenAI → outlook 偶发延迟）。"""
        timeout = max(int(timeout), 90)
        strict_threshold = (issued_after - 5) if issued_after else (time.time() - 5)
        logger.info(
            f"[mail] outlook IMAP OAuth2 纯协议取 OTP -> {email_addr} "
            f"(timeout={timeout}s threshold>={int(strict_threshold)})"
        )
        try:
            return fetch_otp_via_imap(
                self.email, self.refresh_token, self.client_id,
                timeout=timeout, threshold_ts=strict_threshold,
            )
        except FatalOutlookMailError as e:
            # 邮箱后端不可用 / token 失效 → mark dead，让 auth_flow 的
            # `except TimeoutError` + outlook_exhausted 分支跳过 resend retry，
            # 整个流程秒级 fast-fail 而不是傻等 2×180s。
            self.mark_outlook_dead(f"IMAP 不可用 (不可重试): {e}")
            raise TimeoutError(
                f"outlook mail unusable for {self.email}: {e}"
            ) from e


if __name__ == "__main__":
    # 独立调试：python mail_outlook.py 'email----password----client_id----refresh_token'
    import sys as _sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(_sys.argv) < 2:
        print("usage: python mail_outlook.py 'email----password----client_id----refresh_token'")
        _sys.exit(2)
    parts = _sys.argv[1].split("----")
    if len(parts) != 4:
        print(f"4 段格式错: 拿到 {len(parts)} 段")
        _sys.exit(2)
    e, p, c, r = parts
    try:
        otp = fetch_otp_via_imap(e, r, c, timeout=180)
        print(f"OTP: {otp}")
    except Exception as ex:
        print(f"ERR: {ex}")
        _sys.exit(1)
