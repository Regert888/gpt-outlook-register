"""Cloudflare Worker 自建临时邮箱 provider（dreamhunter2333/cloudflare_temp_email）。

借鉴 lxf746/any-auto-register 的 CFWorkerMailbox 实战逻辑：
  - POST /admin/new_address 创建邮箱（必须带 enablePrefix=True）
  - GET  /admin/mails?address=<email> 拉特定邮箱的邮件列表
  - 从 raw 字段抽 OTP，严格过滤 hex 颜色 / 邮箱地址 / 时间戳

能力：pooled=False（自己造地址，不走号池）
      ephemeral=True（每次新地址 → OpenAI 始终当新号，这是 CF 能跑通的根因）

主人需要：
    api_url          Worker HTTPS 地址（如 https://mail.example.com）
    admin_token      Worker 配置的 ADMIN_PASSWORDS
    domain           主人配的 catch-all 域名（如 example.com）

本文件由 mail_cf.py 迁移而来，取件逻辑逐字保留未改。
mail_cf.py 现为转发壳，旧 import 路径继续可用。
"""
from __future__ import annotations

import json as _json
import logging
import random
import string
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from .base import ConfigField, MailProvider, extract_otp, register

logger = logging.getLogger(__name__)


def _gen_local_part(rng: Optional[random.Random] = None, length: int = 10) -> str:
    """生成随机邮箱前缀。参考 any-auto-register 用 10 位 lowercase+digits。"""
    r = rng or random
    return "".join(r.choices(string.ascii_lowercase + string.digits, k=length))


# OTP 抽取已上提到 base.extract_otp（与 outlook 共用同一套防误判规则）。
# 保留旧名给可能的外部调用。
_extract_otp = extract_otp


@register
class CFTempEmailProvider(MailProvider):
    """Cloudflare Worker 自建临时邮箱 provider。

    使用方式：
        mail = CFTempEmailProvider(
            api_url="https://mail.example.com",
            admin_token="<YOUR_ADMIN_PASSWORDS>",
            domain="example.com",
        )
        auth_flow.run_register(mail)
    """

    kind = "cf_temp"
    display_name = "CF Worker 临时邮箱"
    pooled = False         # 地址自己造，无限量，不走号池
    ephemeral = True       # 每次新地址 → OpenAI 永远当新号

    line_segments = 0      # 不支持导入
    import_hint = ""
    import_placeholder = ""

    config_fields = [
        ConfigField(
            "cf_api_url", "Worker 地址",
            placeholder="https://mail.example.com",
            help="Cloudflare Worker 的 HTTPS 地址，末尾不带斜杠",
        ),
        ConfigField(
            "cf_admin_token", "Admin Token", type="password",
            help="Worker 环境变量 ADMIN_PASSWORDS 的值",
        ),
        ConfigField(
            "cf_domain", "收件域名",
            placeholder="example.com",
            help="已配置 catch-all 的域名",
        ),
    ]

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        session=None,
    ):
        if not api_url:
            raise ValueError("api_url 不能为空")
        if not domain:
            raise ValueError("domain 不能为空")
        self.api_url = api_url.rstrip("/")
        self.admin_token = admin_token
        self.domain = domain
        self._jwt: str = ""
        self._current_email: str = ""
        self._seen_mail_ids: set = set()
        self._rng = random.Random()
        self.last_persona = None

        # 用 curl_cffi 模拟 Chrome 指纹，过 CF Bot Fight Mode
        if session is not None:
            self._session = session
        else:
            try:
                from curl_cffi.requests import Session as CffiSession
                self._session = CffiSession(impersonate="chrome136")
                self._session.trust_env = False
            except ImportError:
                self._session = None

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        api_url = (settings.get("cf_api_url") or "").strip()
        domain = (settings.get("cf_domain") or "").strip()
        token = (settings.get("cf_admin_token") or "").strip()
        if not api_url or not domain or not token:
            raise RuntimeError(
                "CF Temp Email 未配置完整（缺 api_url / domain / admin_token），"
                "请去「邮箱配置」Tab 填写"
            )
        return cls(api_url=api_url, admin_token=token, domain=domain)

    # ──────────────────────── HTTP 工具 ────────────────────────

    def _headers(self) -> dict:
        return {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "x-admin-auth": self.admin_token,
        }

    def _request(self, method: str, path: str, **kwargs):
        """统一请求：curl_cffi 优先，urllib 兜底。"""
        url = f"{self.api_url}{path}"
        m = method.upper()
        timeout = kwargs.get("timeout", 15)
        headers = dict(kwargs.get("headers") or self._headers())
        json_body = kwargs.get("json")
        params = kwargs.get("params")

        if self._session is not None:
            try:
                if m == "GET":
                    return self._session.get(url, headers=headers, params=params, timeout=timeout)
                if json_body is not None:
                    return self._session.post(
                        url, headers=headers,
                        data=_json.dumps(json_body, separators=(",", ":")),
                        timeout=timeout,
                    )
                return self._session.post(url, headers=headers, timeout=timeout)
            except Exception as e:
                logger.warning(f"[cf_temp] curl_cffi 请求异常，回退 urllib: {e}")

        # urllib 兜底
        if params:
            import urllib.parse
            qs = urllib.parse.urlencode(params)
            url = f"{url}?{qs}"
        body = _json.dumps(json_body).encode() if json_body is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=m)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.status_code = r.status
                r._text = r.read().decode("utf-8", errors="replace")
                r.text = r._text
                r.json = lambda: _json.loads(r._text)
                return r
        except urllib.error.HTTPError as e:
            e.status_code = e.code
            try:
                e._text = e.read().decode("utf-8", errors="replace")
            except Exception:
                e._text = ""
            e.text = e._text
            e.json = lambda: _json.loads(e._text or "{}")
            return e

    @staticmethod
    def _mail_epoch(mail: dict) -> Optional[float]:
        """把邮件的 created_at 解析成 epoch 秒；解析不出来返回 None。

        CF Worker 给的是【UTC】裸时间串（'2026-08-08 05:51:41'，不带时区），
        主人本地是 UTC+8 —— 当成本地时间解析会整整差 8 小时，那 issued_after
        就永远比不过了。所以必须显式按 UTC 解释。
        """
        raw = (mail.get("created_at") or "").strip()
        if not raw:
            return None
        raw = raw.replace("T", " ").replace("Z", "").split(".")[0]
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        return dt.replace(tzinfo=timezone.utc).timestamp()

    @staticmethod
    def _parse_json(resp) -> dict:
        try:
            return resp.json() if callable(getattr(resp, "json", None)) else _json.loads(resp.text)
        except Exception:
            return {}

    # ──────────────────────── 公共 API ────────────────────────

    def create_mailbox(self) -> str:
        """创建一个新邮箱：POST /admin/new_address，拿 JWT。

        关键参数（来自 any-auto-register 实战）：
          enablePrefix=True   必须，否则部分部署会返回 400
          name=<10位随机>     邮箱前缀
          domain=<主人域名>   catch-all 收件域
        """
        local = _gen_local_part(self._rng, length=10)
        payload = {
            "enablePrefix": True,
            "name": local,
            "domain": self.domain,
        }
        resp = self._request("POST", "/admin/new_address", json=payload, timeout=15)
        status = getattr(resp, "status_code", 0)
        text = (getattr(resp, "text", "") or "")[:300]
        logger.debug(f"[cf_temp] new_address status={status} resp={text}")

        if status != 200:
            raise RuntimeError(
                f"CFTempEmail create_mailbox 失败: status={status} body={text}"
            )

        data = self._parse_json(resp)
        # any-auto-register 双源兼容：email 或 address；token 或 jwt
        email = (data.get("email") or data.get("address") or "").strip()
        token = (data.get("token") or data.get("jwt") or "").strip()

        if not email:
            raise RuntimeError(f"new_address 响应缺 email 字段: {data}")

        self._jwt = token
        self._current_email = email
        self._seen_mail_ids = set()
        logger.info(
            f"[cf_temp] 创建邮箱: {email} "
            f"jwt={'len='+str(len(token)) if token else 'NONE'}"
        )
        return email

    def _get_mails(self, email: str) -> list:
        """拉指定邮箱的最新邮件列表（默认 limit=20）。"""
        resp = self._request(
            "GET", "/admin/mails",
            params={"limit": 20, "offset": 0, "address": email},
            timeout=10,
        )
        status = getattr(resp, "status_code", 0)
        if status != 200:
            logger.debug(f"[cf_temp] /admin/mails 返回 {status}")
            return []
        data = self._parse_json(resp)
        if isinstance(data, dict):
            return data.get("results") or data.get("mails") or []
        if isinstance(data, list):
            return data
        return []

    def peek_otp(
        self,
        email_addr: str,
        issued_after: Optional[float] = None,
        wait: float = 0.0,
    ) -> Optional[str]:
        """非破坏性预读：收件箱里已经躺着本轮的码就直接返回，没有返回 None。

        语义见 base.MailProvider.peek_otp。这里三条铁律：
          - **不碰 self._seen_mail_ids**。探完没探到，后面 wait_for_otp 还要
            靠这几封信；标记成已读它就永远看不见了。
          - issued_after 之前的信一律不认（旧 challenge 的废码）；时间戳读不
            出来的也不认 —— 宁可让调用方多发一封，也不能把上一轮的码当成本轮的。
          - 拿不到不抛异常，让调用方安静地回退到原来的发码流程。
        """
        deadline = time.time() + max(0.0, float(wait))
        while True:
            try:
                for mail in sorted(
                    self._get_mails(email_addr),
                    key=lambda x: x.get("id", 0),
                    reverse=True,
                ):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    if issued_after is not None:
                        ts = self._mail_epoch(mail)
                        if ts is None or ts < issued_after - 2:
                            continue
                    otp = extract_otp(str(mail.get("raw") or ""))
                    if otp:
                        logger.info(
                            f"[cf_temp] 👀 预读命中 OTP={otp} (mail id={mid})，省掉一次发码"
                        )
                        return otp
            except Exception as e:
                logger.debug(f"[cf_temp] peek 异常（当作没探到）: {e}")
            if time.time() >= deadline:
                return None
            time.sleep(1)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询 /admin/mails 等待 OTP（6 位数字）。

        - 用 self._seen_mail_ids 集合去重，避免重复消费历史邮件
        - 借鉴 any-auto-register：按 id desc 排序，新邮件优先
        - OTP 抽取规则严谨（见 base.extract_otp）
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        logger.info(f"[cf_temp] 等待 OTP -> {email_addr} (timeout={timeout}s)")

        # 起始 seen_ids：当前邮箱里已有的邮件 id（避免被旧邮件污染）
        #
        # ★ issued_after 必须当真（2026-08-08 修）：以前这个参数收下就扔了，只靠
        #   「进来时拍张快照、之后的才算新信」。碰上【邮件比我们开始等更早落地】的
        #   场景就必死 —— 绑 2FA 时 OpenAI 在 authorize/continue 那一刻当场就把码发了
        #   （实测 run 4067171c0a62：信 05:51:40 到，我们 05:51:40 才开始等），
        #   于是这封正主被快照当成旧信吞掉，干等到超时。
        #   现在：只有【早于 issued_after】的信才算旧信；issued_after 之后到的信
        #   哪怕已经躺在收件箱里，也照样认。时间戳读不出来的按旧信处理（保守，
        #   宁可多等一封重发，也不要把上一轮的废码当成新码用）。
        try:
            initial_mails = self._get_mails(email_addr)
            kept = 0
            for m in initial_mails:
                mid = str(m.get("id", ""))
                if not mid:
                    continue
                if issued_after is not None:
                    ts = self._mail_epoch(m)
                    if ts is not None and ts >= issued_after - 2:
                        # 这封是我们要等的信，别标记成旧信
                        kept += 1
                        continue
                self._seen_mail_ids.add(mid)
            logger.debug(
                f"[cf_temp] 初始已有邮件 {len(initial_mails)} 封，"
                f"跳过 {len(initial_mails) - kept} 封旧信，保留 {kept} 封候选"
            )
        except Exception as e:
            logger.warning(f"[cf_temp] 初始邮件列表拉取异常: {e}")

        while time.time() < deadline:
            try:
                mails = self._get_mails(email_addr)
                # 按 id 倒序：最新的邮件优先
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in self._seen_mail_ids:
                        continue
                    self._seen_mail_ids.add(mid)

                    raw = str(mail.get("raw") or "")
                    otp = extract_otp(raw)
                    if otp:
                        logger.info(
                            f"[cf_temp] ✅ OTP={otp} from mail id={mid} "
                            f"raw_len={len(raw)}"
                        )
                        return otp
                    # 没匹配到也记日志便于排查
                    logger.debug(
                        f"[cf_temp] mail id={mid} 未匹配到 OTP "
                        f"(subject={mail.get('subject','')[:50]})"
                    )
            except Exception as e:
                logger.warning(f"[cf_temp] poll 异常 (吃掉重试): {e}")
            time.sleep(3)

        raise TimeoutError(f"CFTempEmail OTP timeout {timeout}s for {email_addr}")

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """WebUI「测试」按钮：造一个邮箱验证 Worker 连通性。"""
        try:
            email = self.create_mailbox()
            return {"ok": True, "message": f"连接成功，测试邮箱: {email}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}
