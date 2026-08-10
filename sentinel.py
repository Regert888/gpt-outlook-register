"""
OpenAI Sentinel Token 生成入口。

唯一有效路径：QuickJS（Node 子进程跑 OpenAI 真实 sdk.js），
同时返回主 token 和 SO token。

公开 API：
  get_sentinel_token(session, device_id, flow, ...) -> (sentinel_token, so_token)
  失败时抛 RuntimeError，调用方应终止当前注册流程。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


def get_sentinel_token(
    session,
    device_id: str,
    flow: str = "authorize_continue",
    user_agent: str = DEFAULT_UA,
    sec_ch_ua: str = "",
    sec_ch_ua_platform: str = "",
    sec_ch_ua_mobile: str = "",
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str = "",
    sec_ch_ua_platform_version: str = "",
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    navigator_platform: str = "",
    navigator_vendor: str | None = None,
    hardware_concurrency: int = 0,
    device_memory: int | None = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone: str = "",
) -> tuple[str, str]:
    """返回 (sentinel_token, so_token) 元组。失败抛 RuntimeError。"""
    try:
        from sentinel_quickjs import get_sentinel_token_via_quickjs
        qresult = get_sentinel_token_via_quickjs(
            session,
            device_id=device_id,
            flow=flow,
            log=lambda m: logger.info(m),
            user_agent=user_agent,
            screen=screen,
            lang=lang,
            lang_full=lang_full,
            browser_type=browser_type,
            platform=navigator_platform,
            vendor=navigator_vendor,
            hardware_concurrency=hardware_concurrency,
            device_memory=device_memory,
            max_touch_points=max_touch_points,
            device_pixel_ratio=device_pixel_ratio,
            timezone=timezone,
            sec_ch_ua_full_version_list=sec_ch_ua_full_version_list,
            sec_ch_ua_arch=sec_ch_ua_arch,
            sec_ch_ua_bitness=sec_ch_ua_bitness,
            sec_ch_ua_model=sec_ch_ua_model,
            sec_ch_ua_platform_version=sec_ch_ua_platform_version,
        )
        if qresult:
            return qresult
        # 注意：SO token 为空**不一定**是失败 —— 服务端在 challenge 里没下发 so 块的
        # flow（如 username_password_create）本来就没有 SO token，那种情况
        # sentinel_quickjs 会正常返回 (token, "")，不会走到这里。
        raise RuntimeError("Sentinel QuickJS 失败（主 token 缺失或服务端要求的 SO token 未算出），中止注册以避免封号")
    except ImportError as e:
        raise RuntimeError(f"Sentinel QuickJS 模块缺失: {e}")
    except RuntimeError:
        raise
    except Exception as e:
        # 网络类异常原样上抛：包成 RuntimeError("QuickJS 异常") 会让日志看起来像
        # PoW 算不出来，实际是链路 TLS 瞬断（见 sentinel_quickjs 同处注释）。
        # 保留原异常类型，registrar 的 classify_error 也才能稳定判成 network。
        from http_client import _is_tls_handshake_error

        if _is_tls_handshake_error(e):
            raise
        raise RuntimeError(f"Sentinel QuickJS 异常: {e}")
