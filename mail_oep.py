"""OutlookEmailPlus 平台接码 Provider（对接 outlookEmailPlus 项目的 /api/external/*）。

把远端 OutlookEmailPlus 服务当成"邮箱池 + 取码"黑盒：
  1. claim-random  → 从平台池 claim 一个 outlook 邮箱（Graph 读信，绕过本地 IMAP）
  2. verification-code → 轮询取 OTP（平台用 Graph/AI 抽码，对 "User is authenticated
     but not connected" 的号也能正常读，因为平台优先 Graph）
  3. claim-complete(result=success / provider_blocked) → 回传结果，平台据此回收/冻结
  4. claim-release → 中途放弃，把号还回 available

适配 auth_flow 的 MailProvider 接口：
  - create_mailbox()  → claim-random 拿 email
  - wait_for_otp()    → 轮询 verification-code
  - mark_outlook_dead() → claim-complete(provider_blocked) + 置 outlook_exhausted
  - complete_success() / release() → registrar 在结束时调用

`_outlook_creds` 设为非空，让 auth_flow 的 "outlook 池 + 已有账号" 分支生效
（这些号本质上仍是 outlook 池来源，OpenAI 对它们的已有账号判定逻辑不变）。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 平台默认不再用 IMAP，Graph 优先；这里只是为了让平台端知道我们要 outlook 邮箱
DEFAULT_PROVIDER = "outlook"
# verification-code 轮询间隔（秒）
POLL_INTERVAL = 4
# 平台 wait-message 单次最长 120s，我们用 verification-code 自己轮询更可控


class OEPError(Exception):
    """平台返回 success=false 且非"暂无邮件"类错误时抛出。"""

    def __init__(self, code: str, message: str = "", status: int = 0):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"OEP {code}: {message}")


def _parse_iso_utc(s: str) -> float:
    """ISO8601 (如 '2026-07-04T07:37:18Z') → UTC timestamp。失败返回 0。"""
    if not s:
        return 0.0
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _api(
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """调平台 /api/external/* 接口，返回解析后的 JSON dict。

    HTTP 层非 2xx 或 JSON 里 success=false 都视为失败；但"暂无邮件"类错误
    （MAIL_NOT_FOUND / VERIFICATION_CODE_NOT_FOUND）由调用方按 code 判断，
    这里统一返回 dict（含 success 字段），不抛异常，让调用方分支处理。
    """
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None and v != ""}
        )
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper())
    req.add_header("X-API-Key", api_key)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        raw = resp.read()
    except urllib.error.HTTPError as e:
        # 平台错误也是 JSON 体
        try:
            raw = e.read()
            return json.loads(raw) if raw else {}
        except Exception:
            raise OEPError("HTTP_ERROR", f"{e.code} {e.reason}", status=e.code)
    return json.loads(raw) if raw else {}


# ──────────────────────── Provider ────────────────────────


class OutlookEmailPlusProvider:
    """对接 outlookEmailPlus 平台的 MailProvider。

    构造时只存配置；create_mailbox() 才真正 claim。
    claim 后持有 account_id / claim_token，结束时由 registrar 调
    complete_success() / release() / mark_outlook_dead() 回传结果。
    """

    def __init__(
        self,
        api_url: str,
        api_key: str,
        *,
        caller_id: str = "gpt-outlook-register",
        project_key: str = "",
        provider: str = DEFAULT_PROVIDER,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.caller_id = caller_id or "gpt-outlook-register"
        self.project_key = project_key or ""
        self.provider = provider or DEFAULT_PROVIDER
        # 每次 claim 用唯一 task_id（平台要求 caller_id+task_id 一致即可）
        self._task_id = f"reg-{uuid.uuid4().hex[:12]}"
        # claim 结果
        self._account_id: Optional[int] = None
        self._claim_token: str = ""
        self.email: str = ""
        self.password: str = ""
        # auth_flow 用的字段：让 "outlook 池" 分支生效
        self.last_persona = None
        self.catch_all_domain = ""
        self._outlook_creds = {"api_url": api_url}  # 非空 → is_outlook_pool=True
        self.outlook_exhausted = False
        self._claim_done = False  # 防止 complete/release 重复调
        # 指定邮箱模式：非空时 create_mailbox 不走 claim-random，直接用此邮箱；
        # account_id 为空 → complete/release/mark_outlook_dead 全部 no-op
        self._fixed_email: str = ""

    def set_fixed_email(self, email: str) -> None:
        """跳过 claim-random，直接用指定邮箱（必须是当前 API key 可访问的账号）。

        场景：用户在 WebUI 填了具体邮箱 → 不希望平台随机换号；OTP 读取仍走
        /api/external/verification-code，受 API key 的 allowed_emails 约束。
        调用前建议先 check_accessible() 验证可访问性。
        """
        em = (email or "").strip()
        if not em:
            return
        self._fixed_email = em
        self.email = em
        self.catch_all_domain = em.split("@", 1)[-1] if "@" in em else ""
        logger.info(f"[oep] 使用指定邮箱: {em} (跳过 claim-random)")

    def check_accessible(self, email: str) -> tuple[bool, str]:
        """通过 /api/external/account-status 校验指定邮箱是否被当前 API key 可读。

        返回 (accessible, message)。accessible=True 表示平台存在且 can_read=True。
        任何调用异常都按不可访问处理，并把错误塞进 message 供上层展示。
        """
        em = (email or "").strip()
        if not em:
            return False, "邮箱为空"
        try:
            d = _api(self.api_url, self.api_key, "GET",
                     "/api/external/account-status",
                     params={"email": em}, timeout=20)
        except Exception as e:
            return False, f"account-status 调用失败: {e}"
        if not d.get("success"):
            code = d.get("code", "UNKNOWN")
            msg = d.get("message", "")
            return False, f"{code}: {msg}" if msg else code
        data = d.get("data") or {}
        exists = bool(data.get("exists"))
        can_read = bool(data.get("can_read"))
        if not exists:
            return False, "平台不存在该邮箱"
        if not can_read:
            return False, "邮箱存在但当前 API key 无读取权限"
        return True, "ok"

    # ─ 平台调用 ─

    def _claim(self) -> str:
        params = {
            "caller_id": self.caller_id,
            "task_id": self._task_id,
            "provider": self.provider,
        }
        if self.project_key:
            params["project_key"] = self.project_key
        d = _api(self.api_url, self.api_key, "POST",
                 "/api/external/pool/claim-random", body=params)
        if not d.get("success"):
            code = d.get("code", "UNKNOWN")
            if code == "no_available_account":
                raise RuntimeError("OutlookEmailPlus 池中没有可用邮箱")
            raise OEPError(code, d.get("message", ""))
        data = d["data"]
        self._account_id = int(data["account_id"])
        self._claim_token = data["claim_token"]
        self.email = data["email"]
        self.catch_all_domain = data.get("email_domain") or self.email.split("@", 1)[-1]
        return self.email

    def _claim_complete(self, result: str, detail: str = "") -> bool:
        if self._claim_done or not self._account_id:
            return False
        self._claim_done = True
        d = _api(self.api_url, self.api_key, "POST",
                 "/api/external/pool/claim-complete",
                 body={
                     "account_id": self._account_id,
                     "claim_token": self._claim_token,
                     "caller_id": self.caller_id,
                     "task_id": self._task_id,
                     "result": result,
                     "detail": detail,
                 })
        ok = bool(d.get("success"))
        status = (d.get("data") or {}).get("pool_status", "")
        logger.info(f"[oep] claim-complete result={result} ok={ok} pool_status={status}")
        return ok

    def _claim_release(self, reason: str = "") -> bool:
        if self._claim_done or not self._account_id:
            return False
        self._claim_done = True
        d = _api(self.api_url, self.api_key, "POST",
                 "/api/external/pool/claim-release",
                 body={
                     "account_id": self._account_id,
                     "claim_token": self._claim_token,
                     "caller_id": self.caller_id,
                     "task_id": self._task_id,
                     "reason": reason,
                 })
        ok = bool(d.get("success"))
        logger.info(f"[oep] claim-release ok={ok} reason={reason!r}")
        return ok

    def _fetch_code(self, since_ts: float) -> Optional[str]:
        """单次拉验证码。命中返回 OTP，否则 None。永久错误抛 OEPError。

        关键：平台 `since_minutes` 在大窗口(>60)时会 fallback 返回邮箱里
        最新一封匹配邮件(忽略时间窗)→ 可能拿到几小时前的旧码 → verify_otp 401。
        所以客户端必须用 `received_at` 二次校验：早于 since_ts 的当未命中。
        """
        since_minutes = max(1, int((time.time() - since_ts) / 60) + 1)
        d = _api(self.api_url, self.api_key, "GET",
                 "/api/external/verification-code",
                 params={
                     "email": self.email,
                     "since_minutes": since_minutes,
                     "from_contains": "openai",
                 })
        if d.get("success"):
            data = d.get("data") or {}
            code = data.get("verification_code") or ""
            if not code:
                return None
            # ─ 时间窗二次校验：防止平台 fallback 返回旧邮件 ─
            received_str = data.get("received_at") or ""
            received_ts = _parse_iso_utc(received_str)
            if received_ts and received_ts < since_ts:
                logger.warning(
                    f"[oep] 丢弃旧邮件 email={self.email} code={code} "
                    f"received_at={received_str} (< since_ts={int(since_ts)}), "
                    f"平台 since_minutes={since_minutes} fallback 返回了过期邮件"
                )
                return None
            frm = (data.get("from") or "")[:60]
            logger.info(
                f"[oep] OTP 命中 email={self.email} code={code} from={frm} "
                f"received_at={received_str}"
            )
            return str(code)
        code = d.get("code", "")
        # 暂无邮件 → 继续轮询
        if code in ("MAIL_NOT_FOUND", "VERIFICATION_CODE_NOT_FOUND"):
            return None
        # 账号不存在 / 不可读 → 永久错误
        if code in ("ACCOUNT_NOT_FOUND", "ACCOUNT_ACCESS_FORBIDDEN",
                    "UPSTREAM_READ_FAILED", "EMAIL_SCOPE_FORBIDDEN"):
            raise OEPError(code, d.get("message", ""))
        # 其他未知错误也当永久错误，避免死循环
        raise OEPError(code or "UNKNOWN", d.get("message", ""))

    # ─ MailProvider 接口 ─

    def create_mailbox(self) -> str:
        if self._fixed_email:
            logger.info(f"[oep] 使用指定邮箱: {self._fixed_email} (无 account_id，跳过 claim)")
            self.email = self._fixed_email
            return self._fixed_email
        email = self._claim()
        logger.info(f"[oep] claim 成功: {email} (account_id={self._account_id})")
        return email

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """阻塞轮询 verification-code 直到拿到 OTP 或超时。"""
        # 防串号：传入的 email 应与 claim 绑定的 self.email 一致
        if email_addr and email_addr.lower() != self.email.lower():
            logger.warning(
                f"[oep] wait_for_otp 传入 email={email_addr!r} 与 claim 绑定 "
                f"self.email={self.email!r} 不一致，强制用 self.email 防串号"
            )
        timeout = max(int(timeout), 60)
        since_ts = (issued_after - 5) if issued_after else (time.time() - 5)
        deadline = time.time() + timeout
        logger.info(
            f"[oep] 取 OTP -> {self.email} (timeout={timeout}s "
            f"since_minutes={max(1,int((time.time()-since_ts)/60)+1)})"
        )
        while time.time() < deadline:
            try:
                code = self._fetch_code(since_ts)
                if code:
                    return code
            except OEPError as e:
                # 永久错误 → mark dead + fast-fail（类似 mail_outlook 的处理）
                self.mark_outlook_dead(f"平台取码不可用 ({e.code}): {e.message}")
                raise TimeoutError(
                    f"oep mailbox unavailable for {self.email}: {e}"
                ) from e
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"oep OTP timeout {timeout}s for {self.email}")

    def mark_outlook_dead(self, reason: str = "") -> None:
        """OpenAI 静默拒发 / 平台取码失败 → 回传 provider_blocked 冻结该号。"""
        logger.warning(f"[oep] mark dead: {self.email} reason={reason}")
        self.outlook_exhausted = True
        try:
            self._claim_complete("provider_blocked", reason[:200])
        except Exception as e:
            logger.warning(f"[oep] mark_dead 回传失败: {e}")

    def mark_outlook_retired(self, reason: str = "") -> None:
        """OpenAI 账号已被删除/停用 (永久不可逆) → 回传 credential_invalid 退休该号。"""
        logger.warning(f"[oep] mark retired: {self.email} reason={reason}")
        self.outlook_exhausted = True
        try:
            self._claim_complete("credential_invalid", reason[:200])
        except Exception as e:
            logger.warning(f"[oep] mark_retired 回传失败: {e}")

    # ─ registrar 结束时调用的生命周期方法 ─

    def complete_success(self, detail: str = "注册成功") -> None:
        """注册成功 → 回传 success（长期邮箱+project_key 时回池可复用）。"""
        try:
            self._claim_complete("success", detail)
        except Exception as e:
            logger.warning(f"[oep] complete_success 失败: {e}")

    def release(self, reason: str = "放弃") -> None:
        """中途放弃 / 网络错误 → release 还回 available。"""
        try:
            self._claim_release(reason)
        except Exception as e:
            logger.warning(f"[oep] release 失败: {e}")


if __name__ == "__main__":
    # 独立调试：python mail_oep.py <api_url> <api_key>
    import sys as _sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if len(_sys.argv) < 3:
        print("usage: python mail_oep.py <api_url> <api_key>")
        _sys.exit(2)
    p = OutlookEmailPlusProvider(_sys.argv[1], _sys.argv[2])
    try:
        em = p.create_mailbox()
        print(f"claimed: {em}  (account_id={p._account_id})")
        print("等待 60s 看 OpenAI 来信（不会真有，仅验证连通）...")
        otp = p.wait_for_otp(em, timeout=60)
        print(f"OTP: {otp}")
    except Exception as ex:
        print(f"ERR: {ex}")
    finally:
        p.release("debug test")
