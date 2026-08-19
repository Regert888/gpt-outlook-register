"""OpenAI Sentinel token generation entry point.

The only supported path runs OpenAI's real sdk.js through QuickJS in a Node
subprocess and returns both the main token and the SO token.

Public API:
  get_sentinel_token(session, device_id, flow, ...) -> (sentinel_token, so_token)
  Raises RuntimeError on failure; callers should stop the current registration.
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
    """Return ``(sentinel_token, so_token)`` or raise RuntimeError."""
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
        # An empty SO token is not necessarily a failure. Flows whose
        # challenge omits the SO block (such as username_password_create)
        # legitimately return (token, "") from sentinel_quickjs.
        raise RuntimeError(
            "Sentinel QuickJS failed (main token missing or the required SO token "
            "could not be generated); registration was stopped to protect the account"
        )
    except ImportError as e:
        raise RuntimeError(f"Sentinel QuickJS module is missing: {e}")
    except RuntimeError:
        raise
    except Exception as e:
        # Propagate network errors unchanged. Wrapping them as QuickJS errors
        # would make transient TLS failures look like PoW failures and would
        # prevent registrar.classify_error from classifying them as network.
        from http_client import _is_tls_handshake_error

        if _is_tls_handshake_error(e):
            raise
        raise RuntimeError(f"Sentinel QuickJS error: {e}")
