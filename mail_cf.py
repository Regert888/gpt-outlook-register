"""Cloudflare Worker 自建临时邮箱 provider（dreamhunter2333/cloudflare_temp_email）。

借鉴 lxf746/any-auto-register 的 CFWorkerMailbox 实战逻辑：
  - POST /admin/new_address 创建邮箱（必须带 enablePrefix=True）
  - GET  /admin/mails?address=<email> 拉特定邮箱的邮件列表
  - 从 raw 字段抽 OTP，严格过滤 hex 颜色 / 邮箱地址 / 时间戳

主人需要：
    api_url          Worker HTTPS 地址（如 https://mail.example.com）
    admin_token      Worker 配置的 ADMIN_PASSWORDS
    domain           主人配的 catch-all 域名（如 example.com）
"""
from __future__ import annotations

import json as _json
import logging
import random
import re
import string
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)
from http_client import create_http_session, normalize_proxy_url


def _gen_local_part(rng: Optional[random.Random] = None, length: int = 10) -> str:
    """生成随机邮箱前缀。参考 any-auto-register 用 10 位 lowercase+digits。"""
    r = rng or random
    return "".join(r.choices(string.ascii_lowercase + string.digits, k=length))


def _extract_otp(raw: str, code_pattern: Optional[str] = None) -> Optional[str]:
    """从邮件 raw 字段提取 6 位 OTP。

    借鉴 any-auto-register 的多层防误判逻辑：
      1. 优先匹配 <span>XXXXXX</span>（HTML 标签包裹的验证码）
      2. 跳过 MIME header（\r\n\r\n 前的部分）
      3. 排除邮箱地址、时间戳模式、hex 颜色
    """
    if not raw:
        return None

    # 1. 优先匹配 <span>123456</span>
    m = re.search(r'<span[^>]*>\s*(\d{6})\s*</span>', raw)
    if m:
        return m.group(1)

    # 2. 跳过 MIME header，只搜 body 部分
    body_start = raw.find('\r\n\r\n')
    search_text = raw[body_start:] if body_start != -1 else raw

    # 3. 排除邮箱地址（避免 user123456@x.com 误判）
    search_text = re.sub(
        r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
        '', search_text,
    )
    # 4. 排除时间戳模式（m=+XXXXXX. 和 t=XXXXXXXXXX）
    search_text = re.sub(r'm=\+\d+\.\d+', '', search_text)
    search_text = re.sub(r'\bt=\d+\b', '', search_text)

    # 5. 提取 6 位 OTP，排除 hex 颜色（前缀 # 或紧跟其他数字）
    pattern = code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)'
    m = re.search(pattern, search_text)
    if m:
        return m.group(1) if m.groups() else m.group(0)
    return None


class CFTempEmailProvider:
    """Cloudflare Worker 自建临时邮箱 provider。

    与 OutlookMailProvider 接口兼容，可直接传给 auth_flow.run_register。

    使用方式：
        mail = CFTempEmailProvider(
            api_url="https://mail.example.com",
            admin_token="<YOUR_ADMIN_PASSWORDS>",
            domain="example.com",
        )
        auth_flow.run_register(mail)
    """

    def __init__(
        self,
        api_url: str,
        admin_token: str = "",
        domain: str = "",
        proxy: str = "",
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
        # 兼容 auth_flow 接口
        self.last_persona = None
        self._outlook_creds = None
        self.outlook_exhausted = False

        # 用 curl_cffi 模拟 Chrome 指纹，过 CF Bot Fight Mode
        if session is not None:
            self._session = session
        else:
            try:
                self._session = create_http_session(
                    proxy=normalize_proxy_url(proxy) or None,
                    impersonate="chrome136",
                )
            except ImportError:
                self._session = None

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

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询 /admin/mails 等待 OTP（6 位数字）。

        - 用 self._seen_mail_ids 集合去重，避免重复消费历史邮件
        - 借鉴 any-auto-register：按 id desc 排序，新邮件优先
        - OTP 抽取规则严谨（见 _extract_otp）
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        logger.info(f"[cf_temp] 等待 OTP -> {email_addr} (timeout={timeout}s)")

        # 起始 seen_ids：当前邮箱里已有的邮件 id（避免被旧邮件污染）
        # issued_after=None 表示从现在开始等
        try:
            initial_mails = self._get_mails(email_addr)
            for m in initial_mails:
                mid = str(m.get("id", ""))
                if mid:
                    self._seen_mail_ids.add(mid)
            logger.debug(f"[cf_temp] 初始已有邮件 {len(self._seen_mail_ids)} 封，跳过")
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
                    otp = _extract_otp(raw)
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


if __name__ == "__main__":
    # 命令行测试：python mail_cf.py <api_url> <admin_token> <domain>
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) < 4:
        print("usage: python mail_cf.py <api_url> <admin_token> <domain>")
        sys.exit(2)
    p = CFTempEmailProvider(api_url=sys.argv[1], admin_token=sys.argv[2], domain=sys.argv[3])
    email = p.create_mailbox()
    print(f"创建邮箱: {email}")
    print(f"开始等待 OTP（120s）...")
    try:
        code = p.wait_for_otp(email, timeout=120)
        print(f"OTP: {code}")
    except TimeoutError as e:
        print(f"超时: {e}")
        sys.exit(1)
