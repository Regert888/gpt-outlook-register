"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import logging
import re
from typing import Optional
from urllib.parse import quote, unquote

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（fallback，优先使用 fingerprint.generate_fingerprint() 生成的值）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

_PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}


def normalize_proxy_url(proxy: Optional[str]) -> str:
    """校验并规范化代理 URL，支持带认证的 SOCKS5 URL。

    例如 ``socks5://用户名:密码@1.2.3.4:1080`` 会转换为
    ``socks5h://%E7%94%A8%E6%88%B7%E5%90%8D:%E5%AF%86%E7%A0%81@1.2.3.4:1080``。
    用户名和密码先解码再编码，避免已有百分号编码被重复编码。
    """
    value = str(proxy or "").strip()
    if not value:
        return ""

    match = re.match(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<rest>.+)$", value)
    if not match or match.group("scheme").lower() not in _PROXY_SCHEMES:
        raise ValueError("代理必须是 http://、https:// 或 socks5://host:port 格式")

    scheme = match.group("scheme").lower()
    rest = match.group("rest")
    if "@" in rest:
        credentials, hostport = rest.rsplit("@", 1)
        if ":" in credentials:
            username, password = credentials.split(":", 1)
        else:
            username, password = credentials, ""
        auth = quote(unquote(username), safe="")
        if ":" in credentials:
            auth += ":" + quote(unquote(password), safe="")
        rest = auth + "@" + hostport

    hostport = rest.rsplit("@", 1)[-1]
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0 or end + 1 >= len(hostport) or hostport[end + 1] != ":":
            raise ValueError("代理必须包含端口")
        host = hostport[1:end]
        port_text = hostport[end + 2:]
    else:
        if ":" not in hostport:
            raise ValueError("代理必须包含端口")
        host, port_text = hostport.rsplit(":", 1)
    if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("代理主机或端口无效")

    if scheme == "socks5":
        scheme = "socks5h"
    rendered_host = f"[{host}]" if ":" in host and not hostport.startswith("[") else hostport.split("@", 1)[-1].split(":", 1)[0]
    if hostport.startswith("["):
        rendered_host = f"[{host}]"
    return f"{scheme}://{rest.rsplit('@', 1)[0] + '@' if '@' in rest else ''}{rendered_host}:{port_text}"


def redact_proxy_url(proxy: Optional[str]) -> str:
    """隐藏代理认证信息，避免凭据进入日志或 WebUI 响应。"""
    value = normalize_proxy_url(proxy) if proxy else ""
    if "@" not in value or "://" not in value:
        return value
    scheme, rest = value.split("://", 1)
    return f"{scheme}://***:***@{rest.rsplit('@', 1)[1]}"


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。
    """
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        normalized_proxy = normalize_proxy_url(proxy) if proxy else ""
        if normalized_proxy:
            # curl_cffi 在 SOCKS 代理下建议使用 socks5h，让 DNS 走代理端解析。
            # 这能减少本地 DNS/链路导致的 TLS 握手异常。
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # 显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)
            session.proxies = {"https": "", "http": ""}
        return session
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        normalized_proxy = normalize_proxy_url(proxy) if proxy else ""
        if normalized_proxy:
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        session.headers["User-Agent"] = user_agent or USER_AGENT
        return session
