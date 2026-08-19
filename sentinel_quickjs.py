"""QuickJS-driven Sentinel token generator.

Adapted from
https://github.com/zc-zhangchen/any-auto-register
platforms/chatgpt/sentinel_browser.py:`_get_sentinel_token_via_quickjs`
+ scripts/js/openai_sentinel_quickjs.js (MIT License).

Why this exists:
  Pure-Python `sentinel.py` computes a synthetic PoW that *passes* OpenAI's
  surface validation (200 OK on `/sentinel/req`, `/authorize/continue`, etc.)
  but the OTP-dispatch service runs the actual sentinel SDK JS server-side
  to verify the token. Our synthetic token fails the deeper check → email
  silent-drop. To pass, we must run OpenAI's real `sdk.js` (downloaded from
  `sentinel.openai.com/sentinel/<ver>/sdk.js`) inside a JS VM and emit the
  same token the real browser would.

Implementation:
  - Spawn `node -e <wrapper>` per token request
  - Wrapper loads OpenAI's sdk.js + `openai_sentinel_quickjs.js` (a thin
    adapter that exposes `requirements`/`solve` actions over stdin/stdout)
  - Two passes: action=requirements → `request_p`, then `/sentinel/req` →
    challenge, then action=solve → `final_p` + `t`
  - Returns the same JSON-string shape `{p, t, c, id, flow}` as our
    pure-Python `build_sentinel_token`, so callers don't need to change

Public API:
  - `get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> str | None`
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


SENTINEL_VERSION = "20260219f9f6"
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


_sdk_file_cache: Optional[Path] = None


def _ensure_sdk_file(session: Any, timeout_ms: int) -> Path:
    """Download OpenAI's actual sdk.js to /tmp cache (one-shot per version)."""
    global _sdk_file_cache
    if _sdk_file_cache and _sdk_file_cache.exists():
        return _sdk_file_cache

    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / SENTINEL_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        _sdk_file_cache = sdk_file
        return sdk_file

    resp = session.get(
        SENTINEL_SDK_URL,
        headers={
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": "https://auth.openai.com/",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"Failed to download sdk.js: HTTP {resp.status_code}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("Failed to download sdk.js: empty response")
    sdk_file.write_bytes(content)
    _sdk_file_cache = sdk_file
    return sdk_file


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    proc = subprocess.run(
        [_resolve_node_binary(), str(quickjs_script)],
        input=json.dumps(body, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=max(10, int(timeout_ms / 1000) + 5),
        env={
            **os.environ,
            "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
        },
    )
    if proc.returncode != 0:
        raise RuntimeError(f"QuickJS execution failed: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS returned empty output")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS output is not a JSON object")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN,zh;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge response is not a JSON object")
    return payload


def get_sentinel_token_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
    user_agent: str = "",
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    platform: str = "",
    vendor: Optional[str] = None,
    hardware_concurrency: int = 0,
    device_memory: Optional[int] = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone: str = "",  # IANA time-zone name, e.g. Asia/Tokyo
    # Accept the full Client Hints set to keep call signatures consistent,
    # even though the QuickJS path does not consume all of it directly.
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str = "",
    sec_ch_ua_platform_version: str = "",
) -> Optional[tuple[str, str]]:
    """Try the QuickJS path. Return JSON string on success, None on any failure.

    Caller is expected to fall back to pure-Python sentinel on None.

    Fingerprint consistency matters: feed ``platform``, ``vendor``,
    ``hardware_concurrency``, and related caller-supplied browser-family data
    into sdk.js navigator. This avoids contradictions such as a Windows Chrome
    UA paired with MacIntel/Apple navigator values. Infer sensible defaults
    from the UA when values are omitted.
    """
    log = log or (lambda m: logger.info(m))
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        log(f"Sentinel QuickJS script does not exist: {quickjs_script}")
        return None

    did = str(device_id or uuid.uuid4())

    screen_w, screen_h = "1920", "1080"
    if screen and "x" in screen:
        parts = screen.split("x", 1)
        screen_w, screen_h = parts[0], parts[1]

    lang_primary = lang or "en-US"
    languages = [lang_primary]
    if lang_full:
        for part in lang_full.split(","):
            tag = part.split(";")[0].strip()
            if tag and tag not in languages:
                languages.append(tag)

    # Infer platform and vendor from the UA rather than hard-coding MacIntel.
    ua_l = (user_agent or "").lower()
    if not platform:
        if "iphone" in ua_l:
            platform = "iPhone"
        elif "windows" in ua_l:
            platform = "Win32"
        elif "mac" in ua_l:
            platform = "MacIntel"
        else:
            platform = "Win32"
    if vendor is None:
        if "firefox" in ua_l:
            vendor = ""                       # Firefox exposes an empty navigator.vendor.
        elif "chrome" in ua_l:
            vendor = "Google Inc."
        else:
            vendor = "Apple Computer, Inc."   # Safari / iOS
    hw_conc = int(hardware_concurrency) if hardware_concurrency else 8

    env_payload = {
        "device_id": did,
        "user_agent": user_agent or "Mozilla/5.0",
        "screen_width": screen_w,
        "screen_height": screen_h,
        "language": lang_primary,
        "languages": languages,
        "platform": platform,
        "vendor": vendor,
        "hardware_concurrency": hw_conc,
        "browser_type": browser_type or "",
        "device_pixel_ratio": float(device_pixel_ratio) if device_pixel_ratio else 1.0,
        "max_touch_points": int(max_touch_points),
        "timezone": timezone or "UTC",  # IANA time-zone name
    }
    # Only Chromium exposes deviceMemory. Omit it for None so JS sees undefined.
    if device_memory is not None:
        env_payload["device_memory"] = int(device_memory)

    try:
        sdk_file = _ensure_sdk_file(session, timeout_ms)

        requirements = _run_quickjs_action(
            action="requirements",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=env_payload,
            timeout_ms=timeout_ms,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            log("Sentinel QuickJS failed: requirements did not return request_p")
            return None

        challenge = _fetch_sentinel_challenge(
            session, device_id=did, flow=flow, request_p=request_p, timeout_ms=timeout_ms,
        )
        c_value = str(challenge.get("token") or "").strip()
        if not c_value:
            log("Sentinel QuickJS failed: challenge token is empty")
            return None

        solve_payload = dict(env_payload)
        solve_payload.update({
            "request_p": request_p,
            "challenge": challenge,
            "flow": flow,
            "behavior_duration_ms": 4200,
        })
        solved = _run_quickjs_action(
            action="solve",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=solve_payload,
            timeout_ms=timeout_ms,
        )

        so_token_raw = str(solved.get("so_token") or "").strip()

        # The challenge determines whether an SO token is required; not every
        # flow includes one. The de-obfuscated sdk.js collector condition is:
        #     challenge.so.required === true && typeof challenge.so.collector_dx === 'string'
        # Observations from 2026-08-06: authorize_continue and
        # oauth_create_account required SO, while username_password_create
        # omitted the SO key entirely. Treating every empty SO token as failure
        # produced false alarms and risked reusing a token from another flow.
        so_required = bool((challenge.get("so") or {}).get("required") is True)

        sdk_token = str(solved.get("token") or "").strip()
        if not sdk_token:
            log("Sentinel QuickJS failed: SDK token is empty; stopping to protect the account")
            return None
        if so_required and not so_token_raw:
            # This is a real failure: the server explicitly required SO.
            log("Sentinel QuickJS failed: the server requires an SO token, but solving returned none; stopping to protect the account")
            return None
        log(f"Sentinel QuickJS OK (len={len(sdk_token)}, "
            f"so={'Y' if so_token_raw else 'N/A(not required by server)'})")
        return (sdk_token, so_token_raw)
    except Exception as e:
        # This used to catch every exception and return None, masking transient
        # TLS failures as missing-token or PoW failures. Propagate network
        # errors so registrar can classify them correctly and http_client can
        # apply its TLS retry policy. Only genuine JS/PoW failures return None.
        from http_client import _is_tls_handshake_error

        if _is_tls_handshake_error(e):
            log(f"Sentinel network error (not a PoW issue; propagating unchanged): {e}")
            raise
        log(f"Sentinel QuickJS error: {e}")
        return None
