"""代理相关工具函数。"""
from __future__ import annotations

import urllib.parse


def mask_proxy_url(proxy: str | None) -> str:
    """日志中隐藏代理密码。

    支持格式：
      - http://user:pass@host:port
      - https://user:pass@host:port
      - socks5://user:pass@host:port
      - socks5h://user:pass@host:port
      - host:port（无认证信息则原样返回）

    解析失败或无密码时原样返回。
    """
    if not proxy:
        return proxy or ""
    try:
        parsed = urllib.parse.urlparse(str(proxy))
        if parsed.password:
            netloc = f"{parsed.username}:***@{parsed.hostname or ''}"
            if parsed.port:
                netloc += f":{parsed.port}"
            return parsed._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return str(proxy)


def parse_proxy_pool(text: str) -> list[str]:
    """把多行代理字符串拆成列表。空行 / # 开头注释跳过。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out
