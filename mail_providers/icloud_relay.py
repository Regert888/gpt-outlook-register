"""iCloud Hide My Email relay provider.

Each imported account contains a fixed iCloud alias and its third-party relay
URL. Relay implementations vary: some render messages in HTML, while others
ship a single-page shell whose JavaScript calls a JSON inbox API. The provider
does not infer behavior from URL shape. It first checks the supplied page,
then discovers candidate API endpoints from that page and its same-origin
scripts, probes them, and remembers the first source that yields mail.

HTML relays may expose local wall-clock timestamps and rewritten sender
addresses. JSON relays are preferable because they provide real message IDs,
absolute ISO8601 timestamps, and original senders, although some expose only a
truncated preview. Message IDs are used only for identity; ordering and cutoff
checks always use message dates.

The provider is pooled and non-ephemeral: every account carries its own relay
URL and the address remains fixed. OpenAI may therefore identify it as an
existing account and use passwordless_login. That is a supported outcome, so
``accepts_existing_account`` is enabled below.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    extract_otp,
    register,
    validate_email,
)

logger = logging.getLogger(__name__)

# Relay services rewrite senders into underscore-form iCloud addresses, so
# match those forms as well as normal domains.
_FROM_HINTS = (
    "openai", "tm_openai", "chatgpt", "auth0", "tm.openai", "chatgpt.com",
)

# HTML parsing
#
# Avoid adding BeautifulSoup solely for this provider.
#
# Do not recognize fixed templates. Vendors and even individual deployments
# change markup frequently, which previously caused silent partial parsing.
# The generic scanner instead uses two language-independent signals.
_RE_TAG = re.compile(r"<[^>]+>")
# Strip numeric CSS/script metadata before OTP extraction.
_RE_STYLE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
_RE_SCRIPT = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_HEAD = re.compile(r"<head[^>]*>.*?</head>", re.S | re.I)


def _html_to_text(s: str, *, split_inline: bool = False) -> str:
    """Flatten a full HTML message to text while preserving line structure.

    Remove head, style, script, and conditional comments to prevent CSS values
    from becoming false OTPs. ``split_inline`` lets the generic scanner treat
    inline tags as line boundaries when enforcing its standalone-code rule.
    It remains disabled by default because splitting inline tags can separate
    labels such as ``code: <b>123456</b>`` from their values.
    """
    s = _RE_HEAD.sub(" ", s or "")
    s = _RE_STYLE.sub(" ", s)
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)          # Conditional comments may surround the OTP.
    tags = r"br|/p|/div|/tr|/td|/h[1-6]"
    if split_inline:
        tags += r"|/span|/a|/strong|/b|/li|/font|/em|/i"
    s = re.sub(rf"<({tags})[^>]*>", "\n", s, flags=re.I)
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)
    # Normalize each line and drop blanks so line structure stays unambiguous.
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _parse_date(s: str) -> Optional[float]:
    """Parse relay timestamps into epoch seconds.

    Timezone-free values are treated as local wall-clock time. RFC2822 values
    retain their explicit timezone after an optional parenthesized suffix is
    removed. Return None when parsing fails so callers can skip time filtering.
    """
    s = (s or "").strip()
    if not s:
        return None

    # Weekday or month abbreviations indicate RFC2822.
    if "," in s or re.search(r"\b[A-Z][a-z]{2}\b", s):
        cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*$", "", s)     # Remove '(UTC)'.
        try:
            dt = parsedate_to_datetime(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            pass

    # Parse timezone-free local wall-clock time.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# JSON API relays


def _parse_iso8601(s: str) -> Optional[float]:
    """Convert an ISO8601 UTC timestamp to epoch seconds.

    Fall back to the generic parser instead of losing the cutoff entirely.
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return _parse_date(s)


def _extract_credentials(url: str) -> dict:
    """Extract email and access-key values without identifying the vendor.

    Credentials may appear in any of three locations:
        fragment  https://flysms.xyz/icloud/pickup#email=xx&key=tok_xx
        query     https://ic.youyangai.top/pickup?email=xx&key=tok_xx
        path       https://xx.kdns.fr/pickup/<key>/<email>
                  https://icloud-api.top/s/<token>/<email>

    This reads parameters only and never infers a retrieval method. Endpoint
    discovery determines API paths and request shapes from the page itself.
    Return an empty dictionary when no credentials exist; HTML relays need none.
    """
    parts = urllib.parse.urlsplit(url or "")
    out: dict = {}

    # Key/value pairs in the fragment or query.
    for blob in (parts.fragment, parts.query):
        q = dict(urllib.parse.parse_qsl(blob or ""))
        for k, v in q.items():
            v = (v or "").strip()
            if not v:
                continue
            lk = k.lower()
            if "@" in v and not out.get("email"):
                out["email"] = v.lower()
            elif lk in ("key", "token", "access_key", "accesskey", "k") \
                    and not out.get("key"):
                out["key"] = v

    # In path-based URLs, the segment before the email is usually the key.
    segs = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    for i, s in enumerate(segs):
        if "@" in s:
            out.setdefault("email", s.lower())
            if i > 0:
                out.setdefault("key", segs[i - 1])
            break

    return out


# Discover API paths exposed by the page or JavaScript bundle. This supports
# new vendors without maintaining a domain allowlist.
#
# Do not require an opening quote because bundlers often concatenate paths.
#       `${`/icloud/`.replace(/\/$/,``)}/api/pickup/messages`
# The match requires a leading slash and closing quote/backtick; discovery
# restores any missing prefix from the page directory.
_RE_API_PATH = re.compile(
    r"""(/[A-Za-z0-9_\-./]{0,60}?"""
    r"""(?:messages|mails?|inbox|letters|pickup|codes?)"""
    r"""[A-Za-z0-9_\-./]{0,30})["'`]""",
    re.I,
)
# Ignore obvious non-inbox endpoints.
_API_SKIP = ("email-decode", "cloudflare", "cdn-cgi", "sentry", "analytics",
             "/static/", "/assets/", ".js", ".css", ".map", ".png", ".svg")


_RE_SCRIPT_SRC = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.I)


def _discover_endpoints(html: str, base_url: str, fetch=None) -> list[str]:
    """Discover and de-duplicate endpoints in their source order.

    Server-rendered pages expose endpoints directly in HTML. Single-page apps
    often expose them only in same-origin bundles, so follow referenced scripts
    when ``fetch`` is provided. Pure HTML relays may return no endpoints.
    """
    parts = urllib.parse.urlsplit(base_url or "")
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    # Page directory used to complete path fragments found in bundles.
    prefix = "/".join(parts.path.rstrip("/").split("/")[:-1])
    seen: set[str] = set()
    out: list[str] = []

    def add(path: str) -> None:
        path = "/" + path.strip("/")
        if any(x in path.lower() for x in _API_SKIP):
            return
        url = root + path
        if url not in seen:
            seen.add(url)
            out.append(url)

    def harvest(text: str) -> None:
        for m in _RE_API_PATH.finditer(text or ""):
            path = m.group(1).rstrip("/")
            add(path)
            # Bundlers may concatenate a directory and an API suffix.
            #     `${`/icloud/`.replace(/\/$/,``)}/api/pickup/messages`
            # Add a candidate prefixed by the page directory so a captured
            # suffix such as /api/pickup/messages resolves correctly.
            if prefix and not path.startswith(prefix + "/"):
                add(prefix + path)

    harvest(html)
    if out or fetch is None:
        return out

    # Follow a bounded number of same-origin scripts from skeleton pages.
    for src in _RE_SCRIPT_SRC.findall(html or "")[:8]:
        js_url = urllib.parse.urljoin(base_url, src)
        if urllib.parse.urlsplit(js_url).netloc != parts.netloc:
            continue        # Ignore third-party CDN scripts.
        try:
            harvest(fetch(js_url))
        except Exception as e:
            logger.debug("[icloud_relay] Failed to read script %s: %s", js_url, e)
        if out:
            break

    # Rank API-looking candidates first to minimize initial probe latency.
    # Parsing still determines which candidate actually yields mail.
    def score(u: str) -> tuple:
        low = u.lower()
        return (
            0 if "/api/" in low else 1,          # /api/ paths are most likely.
            0 if low.rstrip("/").split("/")[-1] in (
                "messages", "mails", "mail", "inbox", "codes") else 1,
            len(u),                               # Prefer shorter ties.
        )

    out.sort(key=score)
    return out


# ════════════════════════════════════════════════════════════
#  Generic scanning without templates, field names, or language assumptions
# ════════════════════════════════════════════════════════════
#
# Require both of these language-independent signals:
#
#   Gate 1: a standalone six-digit line. This is a layout signal; alone it
#   could match invoice or promotional numbers.
#
#   Gate 2: OpenAI or ChatGPT appears near the code. Brand names remain
#   untranslated across localized templates, but alone could match page chrome.
#
# Together the gates passed localized templates and common noise cases.

_RE_MAIL_ADDR = re.compile(r"[\w.+-]+@[\w.-]+")
_RE_CODE6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# Recognize observed ISO, wall-clock, and RFC2822 dates both to exclude digits
# inside timestamps and to associate each code with its nearest preceding date.
_RE_TS_ANY = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2},\s*\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[+-]\d{4}"
)

_BRAND = ("openai", "chatgpt")

# Search farther backward because branding usually appears above a code; keep
# the forward window small to avoid crossing into the next message.
_BRAND_BACK = 500
_BRAND_FWD = 200


def _scan_html(text: str) -> list[dict]:
    """Scan a full page and return codes accepted by both gates.

    Results match ``_scan_json`` plus an ``otp`` field. Callers must trust that
    field rather than rerun extract_otp with a different precedence rule.
    """
    clean = _html_to_text(text or "", split_inline=True)
    if not clean:
        return []
    # Remove rewritten sender addresses before applying the gates.
    # noreply_at_tm_openai_com_kyedc2002s707v_54tb7719@icloud.com，
# Their digit segments can otherwise satisfy both the standalone and brand gates.
    clean = _RE_MAIL_ADDR.sub(" ", clean)

    # Pre-scan timestamps to exclude their digits and associate code dates.
    spans: list[tuple[int, int]] = []
    stamps: list[tuple[int, str, float]] = []
    for g in _RE_TS_ANY.finditer(clean):
        spans.append((g.start(), g.end()))
        ts = _parse_iso8601(g.group(0)) if "T" in g.group(0) else _parse_date(g.group(0))
        if ts:
            stamps.append((g.start(), g.group(0), ts))

    out: list[dict] = []
    seen_codes: set[str] = set()
    for m in _RE_CODE6.finditer(clean):
        code, s, e = m.group(1), m.start(), m.end()
        if code in seen_codes:          # The same code may appear in HTML and text copies.
            continue

        # Reject only digits inside a timestamp span. Proximity is insufficient
        # because relays commonly render a valid code immediately below its date.
        if any(a <= s and e <= b for a, b in spans):
            continue

        # Gate 1: standalone line.
        ls = clean.rfind("\n", 0, s) + 1
        le = clean.find("\n", e)
        le = len(clean) if le < 0 else le
        if clean[ls:le].strip() != code:
            continue

        # Gate 2: nearby brand term.
        window = clean[max(0, s - _BRAND_BACK):e + _BRAND_FWD].lower()
        if not any(b in window for b in _BRAND):
            continue

        seen_codes.add(code)
        prev = [x for x in stamps if x[0] < s]
        date_str, ts = (prev[-1][1], prev[-1][2]) if prev else ("", None)
        out.append({
            # Gate 2 provides evidence for the synthetic OpenAI sender value.
            "sender": "openai (generic scan)",
            "subject": f"(generic scan) {code}",
            "body": clean[max(0, s - 200):e + 100],   # Diagnostic context only.
            "date_str": date_str,
            "ts": ts,
            "layout": "scan",
            "otp": code,
        })

    # Sort explicitly by descending time; page ordering is not contractual.
    # Place undated entries last so dated, cutoff-protected candidates win.
    out.sort(key=lambda x: -(x["ts"] or 0))
    return out


def _scan_json(raw: str) -> Optional[list[dict]]:
    """Scan JSON generically without relying on field names.

    Treat a dictionary as a message only when it contains a plausible date and
    its combined string values mention OpenAI or ChatGPT. Return None when the
    input is not JSON and an empty list when it is JSON with no messages;
    callers should continue to the HTML path in either case.
    """
    raw = (raw or "").strip()
    if not raw or raw[0] not in "[{":
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    nodes: list[tuple[float, str, dict]] = []

    def walk(o):
        if isinstance(o, dict):
            ts = None
            blob = []
            for v in o.values():
                if isinstance(v, str):
                    blob.append(v)
                    if ts is None and len(v) >= 10:
                        t = _parse_iso8601(v) if "T" in v else _parse_date(v)
                        # Bounds prevent IDs and sizes from becoming timestamps.
                        if t and 1.7e9 < t < 2.2e9:
                            ts = t
                elif isinstance(v, bool):
                    pass                      # bool is a subclass of int.
                elif isinstance(v, (int, float)) and 1.7e9 < float(v) < 2.2e9:
                    ts = float(v)
            txt = "\n".join(blob)
            if ts and any(b in txt.lower() for b in _BRAND):
                nodes.append((ts, txt, o))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    nodes.sort(key=lambda x: -x[0])           # Trust timestamps, not array order.

    out: list[dict] = []
    for ts, txt, node in nodes:
        # The body separator lets extract_otp skip headers and remove sender
        # addresses before scanning for digits.
        code = extract_otp("\r\n\r\n" + txt)
        if not code:
            continue
        uid = ""
        for k in ("uid", "id", "mid", "message_id", "msg_id"):
            v = node.get(k)
            if v not in (None, ""):
                uid = str(v)
                break
        out.append({
            "sender": "openai (JSON scan)",
            "subject": f"(JSON scan) {code}",
            "body": txt[:500],
            "date_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "ts": ts,
            "layout": "scan-json",
            "otp": code,
            "uid": uid,
        })
    return out


# Rate-limit fallback warnings because wait_for_otp polls every three seconds.
# Benign concurrent writes may emit at most one warning per worker.
_FALLBACK_WARN_INTERVAL = 60.0
_last_fallback_warn = 0.0


def _warn_fallback_once(text: str) -> None:
    """Warn when a branded page yields no code from the generic scanner.

    A page without OpenAI mail is normal and remains silent. A page that does
    mention OpenAI but yields no code suggests that the layout or brand-context
    gates no longer match and deserves a rate-limited warning.
    """
    global _last_fallback_warn
    if not re.search(r"openai|chatgpt", text or "", re.I):
        return
    now = time.time()
    if now - _last_fallback_warn < _FALLBACK_WARN_INTERVAL:
        logger.debug("[icloud_relay] Generic scan still found no code; warning suppressed by rate limit")
        return
    _last_fallback_warn = now
    logger.warning(
        "⚠️ [icloud_relay] The page mentions OpenAI/ChatGPT, but the generic scan "
        "did not find a verification code. Its code-line or brand-context checks may no "
        "longer match the relay layout. Falling back to a full-page scan, which cannot "
        "validate the time window or sender and may return an old code. "
        "Please report this relay layout. Page length=%d", len(text or ""),
    )


def _parse_fallback(text: str) -> list[dict]:
    """Treat the entire page as one message when structured scanning fails.

    This emergency fallback keeps registration usable after a relay changes
    templates. It cannot validate timestamps or senders, so it may select an
    old or unrelated six-digit value. A page-content hash prevents consuming
    an unchanged fallback result more than once.
    """
    body = _html_to_text(text)          # Strip non-content HTML first.
    if not body:                        # CSS values would otherwise look like codes.
        return []
    # Hash the page so unchanged fallback content is not consumed twice.
    digest = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]
    return [{
        "subject": "(full-page scan fallback)",
        # Use a known sender hint because fallback parsing cannot identify one.
        "sender": "openai (fallback: sender unknown)",
        "body": body,
        "date_str": f"fallback:{digest}",
        "ts": None,                     # No timestamp; cutoff checking is skipped.
        "layout": "fallback",
    }]


def parse_relay_html(text: str) -> list[dict]:
    """Parse a relay response without assuming a vendor template.

    Try generic JSON scanning, then the two-gate HTML scan, then the full-page
    fallback. Structured results are sorted by descending time. Both non-JSON
    and JSON-without-mail results continue to the HTML layers.
    """
    text = text or ""

    msgs = _scan_json(text)
    if msgs:
        return msgs

    msgs = _scan_html(text)
    if msgs:
        return msgs

    _warn_fallback_once(text)
    return _parse_fallback(text)


@register
class ICloudRelayProvider(MailProvider):
    """iCloud Hide My Email account retrieved through a third-party relay.

    Usage::
        mail = ICloudRelayProvider(
            email="wariest-grimier.33@icloud.com",
            relay_url="https://mail.ai1998.xyz/messages/<token>/<email>",
        )
    """

    kind = "icloud_relay"
    display_name = "iCloud Hide My Email (Relay)"
    pooled = True           # Each imported account carries its own relay URL.
    ephemeral = False       # The address is fixed; see the module docstring.

    # Two fields: email----relay_url. The tokenized URL belongs to one account.
    line_segments = 2
    import_hint = "email----relay_url"
    import_placeholder = (
        "wariest-grimier.33@icloud.com----https://mail.example.com/messages/TOKEN/"
        "wariest-grimier.33%40icloud.com"
    )

    # Pool records contain all credentials; no global configuration is needed.
    config_fields = []

    # Relay accounts may already exist and legitimately use passwordless_login.
    accepts_existing_account = True

    def __init__(self, email: str, relay_url: str, timeout: int = 20):
        email = (email or "").strip().lower()
        relay_url = (relay_url or "").strip()
        if not email:
            raise ValueError("iCloud email address is required")
        validate_email(email)
        if not relay_url.lower().startswith(("http://", "https://")):
            raise ValueError("Relay URL must be a complete URL beginning with http:// or https://")

        self.email = email
        self.relay_url = relay_url
        self.http_timeout = timeout
        self._dead = False
        self.last_persona = None

        # Credentials are parsed for requests, not to infer a retrieval method.
        # The first _load probes and remembers the actual source.
        self._cred = _extract_credentials(relay_url)
        self._source: Optional[str] = None      # None=unprobed, "html", or API URL.
        self._host = urllib.parse.urlsplit(relay_url).netloc
        if self._cred.get("email") and self._cred["email"] != email:
            # Prefer the URL-embedded address because its key is address-specific.
            logger.warning(
                "[icloud_relay] Pool email %s does not match %s in the relay URL; "
                "using the address embedded in the URL because its key is URL-specific",
                email, self._cred["email"],
            )

        # Fingerprints prevent consuming the same message twice.
        self._seen: set[str] = set()
        # Capture the starting snapshot only once; see wait_for_otp.
        self._snapshot_done = False

    # Construction entry point

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        """Build from a pool record containing its email and relay URL.

        There is no global fallback because every tokenized relay URL belongs
        to one account. Fail explicitly when the account record is missing.
        """
        if not account:
            raise MailProviderError(
                "iCloud relay email uses the account pool. Import accounts on the "
                "Import Email page using email----relay_url.",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        relay = (account.get("relay_url") or "").strip()
        if not relay:
            raise MailProviderError(
                f"The pool entry for {email} has no relay URL. It may use an old "
                "import format; re-import it as email----relay_url.",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, relay_url=relay)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    # ──────────────────────── HTTP ────────────────────────

    def _fetch(self) -> str:
        """Fetch relay HTML with both known "show all" parameters.

        Different relays use ``all=1`` or ``n=N`` and ignore the unknown one,
        so sending both avoids an extra detection request.
        """
        parts = urllib.parse.urlsplit(self.relay_url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q["all"] = "1"
        q.setdefault("n", "20")
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(q), parts.fragment)
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # These statuses indicate an expired token or invalid URL.
            if e.code in (401, 403, 404, 410):
                raise MailProviderError(
                    f"Invalid relay URL (HTTP {e.code}); its token may have expired or the URL may be incorrect",
                    fatal=True, kind=self.kind,
                ) from e
            raise

    # Public API

    def create_mailbox(self) -> str:
        """Return the fixed configured address."""
        return self.email

    def _fetch_text(self, url: str) -> str:
        """GET raw text for endpoint discovery, including JavaScript bundles."""
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Accept": "*/*",
            "Referer": self.relay_url,
        })
        with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
            return r.read().decode("utf-8", errors="replace")

    def _try_api(self, api_url: str, limit: int = 50) -> str:
        """Request raw text from a candidate API without parsing it.

        Probe the observed GET/POST and credential combinations because relay
        vendors differ. ``parse_relay_html`` decides whether a response contains
        mail. Candidate failures return an empty string and are never fatal;
        the HTML page may still be usable.
        """
        cred = self._cred
        email = cred.get("email") or self.email
        key = cred.get("key") or ""
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
        base_hdr = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": ua,
            "Referer": self.relay_url,
            "Cache-Control": "no-cache",
        }

        attempts: list[tuple[str, dict, Optional[bytes]]] = []
        # GET with a Bearer header.
        q = urllib.parse.urlencode({"limit": limit})
        attempts.append((
            f"{api_url}?{q}",
            {**base_hdr, "Authorization": f"Bearer {key}", "X-Mailbox-Email": email},
            None,
        ))
        # POST with a JSON body containing email, access_key, and limit.
        if key:
            body = json.dumps(
                {"email": email, "access_key": key, "key": key, "limit": limit}
            ).encode()
            attempts.append((
                api_url, {**base_hdr, "Content-Type": "application/json"}, body,
            ))
        # GET with credentials in the query string.
        if key:
            q2 = urllib.parse.urlencode(
                {"email": email, "key": key, "access_key": key, "limit": limit}
            )
            attempts.append((f"{api_url}?{q2}", dict(base_hdr), None))

        for url, hdr, body in attempts:
            try:
                req = urllib.request.Request(url, data=body, headers=hdr)
                with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                    txt = r.read().decode("utf-8", errors="replace")
                if txt.strip():
                    return txt
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # Rate limiting is temporary; do not reject the endpoint.
                    logger.warning(
                        "[icloud_relay] Relay inbox API rate-limited the request with 429 (Retry-After=%s)",
                        (e.headers.get("Retry-After") if e.headers else None) or "?",
                    )
                    return ""
                logger.debug("[icloud_relay] Candidate API %s -> HTTP %s", url, e.code)
            except Exception as e:
                logger.debug("[icloud_relay] Candidate API %s failed: %s", url, e)
        return ""

    def _load(self) -> list[dict]:
        """Fetch and parse once while discovering the actual inbox source.

        Try the supplied page first, then endpoints exposed by the page. Cache
        the first source that yields mail so later polls avoid repeated probes.
        All response types use ``parse_relay_html`` as the single parser.
        """
        # Use an already-discovered source directly.
        if self._source:
            if self._source == "html":
                return parse_relay_html(self._fetch())
            raw = self._try_api(self._source)
            msgs = parse_relay_html(raw) if raw else []
            if msgs:
                return msgs
            # A temporarily empty known source is not grounds for reprobe.
            return []

        # First probe: the supplied page.
        #
        # A source succeeds only when the scanner recognizes an OTP. The
        # full-page fallback always emits a record, so non-empty is insufficient.
        html = self._fetch()
        msgs = parse_relay_html(html)
        if any(m.get("otp") for m in msgs):
            self._source = "html"
            logger.info("[icloud_relay] Inbox source=relay page (%s)", self._host)
            return msgs

        # Probe endpoints exposed by the page and, when needed, its JS bundles.
        for api in _discover_endpoints(html, self.relay_url, fetch=self._fetch_text):
            raw = self._try_api(api)
            if not raw:
                continue
            got = parse_relay_html(raw)
            if any(m.get("otp") for m in got):
                self._source = api
                logger.info("[icloud_relay] Inbox source=API %s", api)
                return got

        # If no API yields mail, retain any page fallback and keep polling.
        return msgs

        # No mail may simply mean delivery is pending; never mark the account dead here.
        return []

    def _messages(self) -> list[dict]:
        """Return an empty poll result for transient fetch failures."""
        try:
            return self._load()
        except MailProviderError:
            raise                      # Fatal errors must not look like an empty inbox.
        except Exception as e:
            logger.warning(f"[icloud_relay] Failed to fetch messages; retrying: {e}")
            return []

    @staticmethod
    def _fp(m: dict) -> str:
        """Build a fingerprint that prevents duplicate message consumption.

        Prefer a real JSON UID. HTML relays fall back to date and subject,
        which may collide when OpenAI sends similar messages close together.
        """
        uid = m.get("uid")
        if uid not in (None, ""):
            return f"uid:{uid}"
        return f"{m.get('date_str','')}|{m.get('subject','')[:80]}"

    def _looks_like_openai(self, m: dict) -> bool:
        blob = f"{m.get('sender','')} {m.get('subject','')}".lower()
        return any(h in blob for h in _FROM_HINTS)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """Poll the relay for an OTP.

        ``issued_after`` filters persistent history. Allow a 90-second margin
        because page timestamps have only second precision and clocks may drift.
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        cutoff = (issued_after - 90) if issued_after else None
        logger.info(
            f"[icloud_relay] Waiting for OTP -> {email_addr} "
            f"(timeout={timeout}s, cutoff={cutoff})"
        )

        # Mark messages present before the run as seen.
        #
        # Capture this snapshot only once. Resend retries reuse the provider;
        # another snapshot would hide codes that arrived during the first wait.
        if not self._snapshot_done:
            try:
                for m in self._messages():
                    self._seen.add(self._fp(m))
                self._snapshot_done = True
                logger.debug(f"[icloud_relay] Skipped {len(self._seen)} messages from the initial snapshot")
            except MailProviderError:
                raise
            except Exception as e:
                logger.warning(f"[icloud_relay] Failed to capture the initial inbox snapshot: {e}")

        while time.time() < deadline:
            for m in self._messages():
                fp = self._fp(m)
                if fp in self._seen:
                    continue
                self._seen.add(fp)

                if cutoff and m.get("ts") and m["ts"] < cutoff:
                    logger.debug(
                        f"[icloud_relay] Skipping old message {m.get('date_str')} "
                        f"({m.get('subject','')[:40]})"
                    )
                    continue
                if not self._looks_like_openai(m):
                    logger.debug(
                        f"[icloud_relay] Skipping non-OpenAI message: "
                        f"{m.get('sender','')[:60]}"
                    )
                    continue

                # Trust OTPs accepted by the generic scanner's two gates.
                # Older JSON/fallback records without otp use the legacy extractor.
                otp = m.get("otp") or extract_otp(
                    f"{m.get('subject','')}\r\n\r\n{m.get('body','')}"
                )
                if otp:
                    if m.get("layout") == "fallback":
                        # Fallback codes lack sender and time validation; warn clearly.
                        logger.warning(
                            f"⚠️ [icloud_relay] OTP={otp} came from the full-page scan fallback "
                            "without time-window or sender validation. Retry once if validation fails."
                        )
                    elif m.get("ts") is None:
                        # An undated generic-scan code lacks cutoff protection.
                        logger.warning(
                            f"⚠️ [icloud_relay] OTP={otp} came from the generic scan, but the "
                            "page has no parseable timestamp. It may be an old code."
                        )
                    else:
                        logger.info(
                            f"[icloud_relay] ✅ OTP={otp} "
                            f"({m.get('date_str')} {m.get('subject','')[:40]})"
                        )
                    return otp
                logger.debug(
                    f"[icloud_relay] Message has no OTP: {m.get('subject','')[:50]}"
                )
            time.sleep(3)

        raise TimeoutError(
            f"iCloud relay OTP timed out after {timeout}s ({email_addr}). "
            "Confirm that the relay can receive messages for this address."
        )

    # Import format

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """email----relay_url

        The parser remains useful if this provider's pooling policy changes.
        """
        parts = [p.strip() for p in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"Expected 2 fields (email----relay_url); got {len(parts)}"
            )
        email, relay = parts
        validate_email(email)
        if not relay.lower().startswith(("http://", "https://")):
            raise ValueError("The second field must be a relay URL beginning with http:// or https://")
        return {
            "email": email.lower(),
            "kind": cls.kind,
            "relay_url": relay,
        }

    # Self-test

    def self_test(self) -> dict:
        """Probe once and report the actual source and visible messages.

        The source is discovered dynamically, so running ``_load`` is the only
        reliable way for the WebUI connectivity test to report it.
        """
        try:
            msgs = self._load()
        except MailProviderError as e:
            return {"ok": False, "message": f"[{self._host}] {e}"}
        except Exception as e:
            return {"ok": False, "message": f"[{self._host}] Fetch failed: {e}"}
        way = {None: "no usable source detected", "html": "relay page"}.get(
            self._source, f"API {self._source}"
        )

        if not msgs:
            return {
                "ok": True,
                "message": (
                    f"[{way}] The URL is reachable, but the inbox is empty ({self.email}). "
                    "Send a test message to confirm delivery."
                ),
            }
        newest = msgs[0]
        if newest.get("layout") == "fallback":
            # A fallback result often means no OpenAI code has arrived yet,
            # while the relay link itself is healthy.
            return {
                "ok": True,
                "message": (
                    f"[{way}] The URL is reachable ({self.email}), but no OpenAI "
                    "verification-code message was found. This is normal if no code has arrived yet."
                ),
            }
        return {
            "ok": True,
            "message": (
                f"[{way}] Connection successful. Found {len(msgs)} verification codes "
                f"for {self.email}; newest: "
                f"{newest.get('otp') or newest.get('subject','(no subject)')[:40]} "
                f"({newest.get('date_str') or 'time unknown'})"
            ),
        }
