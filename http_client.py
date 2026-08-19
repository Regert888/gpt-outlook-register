"""HTTP client with curl_cffi TLS fingerprint impersonation.

Supports Cloudflare-facing requests and falls back to requests when curl_cffi
is unavailable.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Prefer curl_cffi because it provides TLS fingerprint impersonation.
try:
    from curl_cffi.requests import Session as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi is available; using TLS fingerprint impersonation")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi is unavailable; falling back to requests")

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Generic fallback UA; prefer values from fingerprint.generate_fingerprint().
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

# Markers for transient TLS handshake failures, aligned with AuthFlow._is_tls_error.
_TLS_ERROR_MARKERS = ("curl: (35)", "tls connect error", "openssl_internal", "sslerror")


def _is_tls_handshake_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TLS_ERROR_MARKERS)


class _TlsRetrySession:
    """Retry transient TLS failures while forwarding every other attribute.

    Proxy links occasionally fail with curl error 35 before an HTTP request is
    sent. A 2026-08-10 sample observed this on 5.4% of attempts across multiple
    browser fingerprints and domains, which indicates a link-level transient
    failure rather than fingerprinting or risk-control rejection.

    Retries must reuse the original session. Later authorization stages depend
    on ``oai-did`` and CSRF cookies planted during warmup; rebuilding the
    session loses them and causes ``409 invalid_state``. Testing recovered all
    eight observed TLS35 events on the first same-session retry.

    Wrapping the session covers failures at any step, including Sentinel calls.
    Attribute forwarding has been checked for cookies, trust_env, proxies,
    mount, headers, iteration, and assignment. curl_cffi itself yields strings
    when iterating session.cookies; this wrapper does not change that behavior.
    """

    def __init__(self, inner, retries: int = 2, backoff: float = 1.5):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_retries", max(0, int(retries)))
        object.__setattr__(self, "_backoff", float(backoff))

    # Forward all reads and writes other than get/post to the real session.
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_inner"))

    def _call_with_retry(self, method: str, *args, **kwargs):
        import time

        inner = object.__getattribute__(self, "_inner")
        retries = object.__getattribute__(self, "_retries")
        backoff = object.__getattribute__(self, "_backoff")
        fn = getattr(inner, method)

        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                # Retry only transient TLS failures. Propagate HTTP status
                # errors, timeouts, and application errors unchanged.
                if not _is_tls_handshake_error(e) or attempt >= retries:
                    raise
                wait = backoff * (attempt + 1)
                url = args[0] if args else kwargs.get("url", "?")
                logger.warning(
                    "Transient TLS failure; retrying with the same session in %.1fs (%d/%d): %s",
                    wait, attempt + 1, retries, str(url)[:80],
                )
                time.sleep(wait)

    def get(self, *args, **kwargs):
        return self._call_with_retry("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call_with_retry("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call_with_retry("put", *args, **kwargs)


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
):
    """Create an HTTP session, preferring curl_cffi and falling back to requests."""
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # Use explicit proxy settings so system HTTP(S)_PROXY values do not leak in.
        session.trust_env = False
        if proxy:
            # With curl_cffi, socks5h resolves DNS at the proxy and reduces
            # TLS failures caused by the local DNS path.
            normalized_proxy = proxy
            if proxy.startswith("socks5://"):
                normalized_proxy = "socks5h://" + proxy[len("socks5://"):]
                logger.info("Normalized proxy scheme: socks5:// -> socks5h://")
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            # Explicitly clear proxies because trust_env=False alone is not enough for libcurl.
            session.proxies = {"https": "", "http": ""}
        # Same-session retries recovered all observed transient TLS failures.
        # Wrapping here covers both auth_flow and Sentinel calls.
        return _TlsRetrySession(session)
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
        if proxy:
            session.proxies = {"https": proxy, "http": proxy}
        session.headers["User-Agent"] = user_agent or USER_AGENT
        return session
