"""Browser fingerprint randomization across multiple browser families.

Each registration calls generate_fingerprint() to create a consistent fingerprint set:
  - TLS impersonation (for curl_cffi)
  - User-Agent
  - sec-ch-ua / sec-ch-ua-platform / sec-ch-ua-mobile (Chrome only)
  - Screen resolution
  - Accept-Language
  - browser_type identifier (mac_safari / ios_safari / chrome / firefox)
  - fallback_impersonates list from the same browser family
"""
from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# macOS Safari (preserved from the original implementation)
# ---------------------------------------------------------------------------
_SAFARI_VERSIONS = [
    {
        "impersonate": "safari15_3",
        "safari_ver": "15.3",
        "webkit_ver": "605.1.15",
        "macos_versions": ["10_15_7", "12_0", "12_1"],
    },
    {
        "impersonate": "safari15_5",
        "safari_ver": "15.5",
        "webkit_ver": "605.1.15",
        "macos_versions": ["10_15_7", "12_4", "12_5"],
    },
    {
        "impersonate": "safari17_0",
        "safari_ver": "17.0",
        "webkit_ver": "605.1.15",
        "macos_versions": ["13_6", "14_0", "14_1"],
    },
    {
        "impersonate": "safari18_0",
        "safari_ver": "18.0",
        "webkit_ver": "605.1.15",
        "macos_versions": ["14_4", "14_5", "15_0", "15_1"],
    },
]

_MAC_SCREENS = [
    "1440x900",
    "1512x982",
    "1728x1117",
    "2560x1440",
    "1920x1080",
]

# ---------------------------------------------------------------------------
# iOS Safari
# ---------------------------------------------------------------------------
_IOS_SAFARI_VERSIONS = [
    {
        "impersonate": "safari17_2_ios",
        "safari_ver": "17.2",
        "webkit_ver": "605.1.15",
        "ios_versions": ["17_1_2", "17_2"],
    },
    {
        "impersonate": "safari18_0_ios",
        "safari_ver": "18.0",
        "webkit_ver": "605.1.15",
        "ios_versions": ["18_0", "18_1", "18_1_1"],
    },
]

_IPHONE_SCREENS = [
    "390x844",   # iPhone 13 / 14
    "393x852",   # iPhone 14 Pro / 15
    "428x926",   # iPhone 13 Pro Max / 14 Plus
    "430x932",   # iPhone 14 Pro Max / 15 Plus
]

# ---------------------------------------------------------------------------
# Chrome (Windows)
# ---------------------------------------------------------------------------
_CHROME_VERSIONS = [
    {
        "impersonate": "chrome136",
        "ver": "136",
        "full_ver": "136.0.0.0",
        "not_a_brand": '"Not.A/Brand";v="99"',
    },
    {
        "impersonate": "chrome142",
        "ver": "142",
        "full_ver": "142.0.0.0",
        "not_a_brand": '"Not/A)Brand";v="8"',
    },
    {
        "impersonate": "chrome146",
        "ver": "146",
        "full_ver": "146.0.0.0",
        "not_a_brand": '"Not?A_Brand";v="99"',
    },
]

_WIN_SCREENS = [
    "1920x1080",
    "1366x768",
    "2560x1440",
    "1536x864",
    "1440x900",
]

# ---------------------------------------------------------------------------
# Firefox (Windows)
# ---------------------------------------------------------------------------
_FIREFOX_VERSIONS = [
    {"impersonate": "firefox133", "ver": "133.0"},
    {"impersonate": "firefox144", "ver": "144.0"},
]

# ---------------------------------------------------------------------------
# Country-to-timezone/language profiles (optimized to match IP geolocation)
# ---------------------------------------------------------------------------
_COUNTRY_PROFILES = {
    # Asia
    "JP": {
        "timezones": [("Asia/Tokyo", 1.0)],
        "languages": ["ja-JP", "ja", "en-US", "en", "zh-CN"],
    },
    "CN": {
        "timezones": [("Asia/Shanghai", 1.0)],
        "languages": ["zh-CN", "zh", "en-US", "en"],
    },
    "HK": {
        "timezones": [("Asia/Hong_Kong", 1.0)],
        "languages": ["zh-HK", "zh-CN", "zh", "en-US", "en"],
    },
    "TW": {
        "timezones": [("Asia/Taipei", 1.0)],
        "languages": ["zh-TW", "zh", "en-US", "en", "ja"],
    },
    "KR": {
        "timezones": [("Asia/Seoul", 1.0)],
        "languages": ["ko-KR", "ko", "en-US", "en", "ja"],
    },
    "SG": {
        "timezones": [("Asia/Singapore", 1.0)],
        "languages": ["zh-CN", "zh", "en-US", "en", "ms-MY", "ms"],
    },
    "MY": {
        "timezones": [("Asia/Kuala_Lumpur", 1.0)],
        "languages": ["ms-MY", "ms", "zh-CN", "zh", "en-US", "en"],
    },
    "TH": {
        "timezones": [("Asia/Bangkok", 1.0)],
        "languages": ["th-TH", "th", "en-US", "en"],
    },
    "VN": {
        "timezones": [("Asia/Ho_Chi_Minh", 1.0)],
        "languages": ["vi-VN", "vi", "en-US", "en"],
    },
    "IN": {
        "timezones": [("Asia/Kolkata", 1.0)],
        "languages": ["en-IN", "en-US", "en", "hi-IN", "hi"],
    },
    "ID": {
        "timezones": [("Asia/Jakarta", 1.0)],
        "languages": ["id-ID", "id", "en-US", "en"],
    },
    "PH": {
        "timezones": [("Asia/Manila", 1.0)],
        "languages": ["en-US", "en", "tl-PH", "tl"],
    },
    "PK": {
        "timezones": [("Asia/Karachi", 1.0)],
        "languages": ["en-US", "en", "ur-PK", "ur"],
    },
    "BD": {
        "timezones": [("Asia/Dhaka", 1.0)],
        "languages": ["bn-BD", "bn", "en-US", "en"],
    },
    "IL": {
        "timezones": [("Asia/Jerusalem", 1.0)],
        "languages": ["he-IL", "he", "en-US", "en", "ar"],
    },
    "TR": {
        "timezones": [("Europe/Istanbul", 1.0)],
        "languages": ["tr-TR", "tr", "en-US", "en"],
    },
    "SA": {
        "timezones": [("Asia/Riyadh", 1.0)],
        "languages": ["ar-SA", "ar", "en-US", "en"],
    },
    "AE": {
        "timezones": [("Asia/Dubai", 1.0)],
        "languages": ["ar-AE", "ar", "en-US", "en"],
    },
    # North America
    "US": {
        "timezones": [
            ("America/New_York", 0.4),      # Eastern (many data centers)
            ("America/Los_Angeles", 0.3),   # Pacific
            ("America/Chicago", 0.2),       # Central
            ("America/Denver", 0.1),        # Mountain
        ],
        "languages": ["en-US", "en", "es-US", "es", "zh-CN"],
    },
    "CA": {
        "timezones": [
            ("America/Toronto", 0.6),       # Eastern (Ontario)
            ("America/Vancouver", 0.3),     # Western (British Columbia)
            ("America/Edmonton", 0.1),      # Mountain (Alberta)
        ],
        "languages": ["en-CA", "en-US", "en", "fr-CA", "fr"],
    },
    "MX": {
        "timezones": [("America/Mexico_City", 1.0)],
        "languages": ["es-MX", "es", "en-US", "en"],
    },
    # South America
    "BR": {
        "timezones": [
            ("America/Sao_Paulo", 0.7),
            ("America/Manaus", 0.2),
            ("America/Fortaleza", 0.1),
        ],
        "languages": ["pt-BR", "pt", "en-US", "en", "es"],
    },
    "AR": {
        "timezones": [("America/Argentina/Buenos_Aires", 1.0)],
        "languages": ["es-AR", "es", "en-US", "en"],
    },
    "CL": {
        "timezones": [("America/Santiago", 1.0)],
        "languages": ["es-CL", "es", "en-US", "en"],
    },
    "CO": {
        "timezones": [("America/Bogota", 1.0)],
        "languages": ["es-CO", "es", "en-US", "en"],
    },
    # Europe
    "GB": {
        "timezones": [("Europe/London", 1.0)],
        "languages": ["en-GB", "en-US", "en", "fr", "de"],
    },
    "DE": {
        "timezones": [("Europe/Berlin", 1.0)],
        "languages": ["de-DE", "de", "en-US", "en", "fr"],
    },
    "FR": {
        "timezones": [("Europe/Paris", 1.0)],
        "languages": ["fr-FR", "fr", "en-US", "en", "de"],
    },
    "IT": {
        "timezones": [("Europe/Rome", 1.0)],
        "languages": ["it-IT", "it", "en-US", "en", "fr"],
    },
    "ES": {
        "timezones": [("Europe/Madrid", 1.0)],
        "languages": ["es-ES", "es", "en-US", "en", "fr"],
    },
    "NL": {
        "timezones": [("Europe/Amsterdam", 1.0)],
        "languages": ["nl-NL", "nl", "en-US", "en", "de"],
    },
    "BE": {
        "timezones": [("Europe/Brussels", 1.0)],
        "languages": ["nl-BE", "fr-BE", "nl", "fr", "en-US", "en"],
    },
    "CH": {
        "timezones": [("Europe/Zurich", 1.0)],
        "languages": ["de-CH", "fr-CH", "de", "fr", "it", "en-US", "en"],
    },
    "SE": {
        "timezones": [("Europe/Stockholm", 1.0)],
        "languages": ["sv-SE", "sv", "en-US", "en"],
    },
    "NO": {
        "timezones": [("Europe/Oslo", 1.0)],
        "languages": ["nb-NO", "nb", "en-US", "en"],
    },
    "DK": {
        "timezones": [("Europe/Copenhagen", 1.0)],
        "languages": ["da-DK", "da", "en-US", "en"],
    },
    "FI": {
        "timezones": [("Europe/Helsinki", 1.0)],
        "languages": ["fi-FI", "fi", "sv", "en-US", "en"],
    },
    "PL": {
        "timezones": [("Europe/Warsaw", 1.0)],
        "languages": ["pl-PL", "pl", "en-US", "en"],
    },
    "RU": {
        "timezones": [
            ("Europe/Moscow", 0.7),         # Moscow (MSK, major data centers)
            ("Asia/Yekaterinburg", 0.15),   # Yekaterinburg (UTC+5)
            ("Asia/Novosibirsk", 0.15),     # Novosibirsk (UTC+7)
        ],
        "languages": ["ru-RU", "ru", "en-US", "en"],
    },
    "UA": {
        "timezones": [("Europe/Kiev", 1.0)],
        "languages": ["uk-UA", "uk", "ru", "en-US", "en"],
    },
    "CZ": {
        "timezones": [("Europe/Prague", 1.0)],
        "languages": ["cs-CZ", "cs", "en-US", "en", "de"],
    },
    "AT": {
        "timezones": [("Europe/Vienna", 1.0)],
        "languages": ["de-AT", "de", "en-US", "en"],
    },
    "GR": {
        "timezones": [("Europe/Athens", 1.0)],
        "languages": ["el-GR", "el", "en-US", "en"],
    },
    "PT": {
        "timezones": [("Europe/Lisbon", 1.0)],
        "languages": ["pt-PT", "pt", "en-US", "en", "es"],
    },
    # Oceania
    "AU": {
        "timezones": [
            ("Australia/Sydney", 0.5),      # Sydney (NSW, many data centers)
            ("Australia/Melbourne", 0.3),   # Melbourne (VIC)
            ("Australia/Brisbane", 0.2),    # Brisbane (QLD)
        ],
        "languages": ["en-AU", "en-US", "en", "zh-CN", "zh"],
    },
    "NZ": {
        "timezones": [("Pacific/Auckland", 1.0)],
        "languages": ["en-NZ", "en-US", "en"],
    },
    # Africa
    "ZA": {
        "timezones": [("Africa/Johannesburg", 1.0)],
        "languages": ["en-ZA", "en-US", "en", "af"],
    },
    "EG": {
        "timezones": [("Africa/Cairo", 1.0)],
        "languages": ["ar-EG", "ar", "en-US", "en"],
    },
    "NG": {
        "timezones": [("Africa/Lagos", 1.0)],
        "languages": ["en-NG", "en-US", "en"],
    },
    "KE": {
        "timezones": [("Africa/Nairobi", 1.0)],
        "languages": ["sw-KE", "sw", "en-US", "en"],
    },
}

# Fallback profile for unknown countries
_DEFAULT_COUNTRY_PROFILE = {
    "timezones": [("UTC", 1.0)],
    "languages": ["en-US", "en"],
}

# ---------------------------------------------------------------------------
# Shared legacy fixed-language list retained for compatibility
# ---------------------------------------------------------------------------
_LANGUAGES = [
    ("en-US", "en-US,en;q=0.9"),
    ("en-US", "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7"),
    ("en-GB", "en-GB,en;q=0.9,en-US;q=0.8"),
    ("en-US", "en-US,en;q=0.9,ja;q=0.8"),
]

_BROWSER_WEIGHTS = [
    ("mac_safari", 30),
    ("ios_safari", 15),
    ("chrome",     35),
    ("firefox",    20),
]

_BROWSER_TYPES = [t for t, _ in _BROWSER_WEIGHTS]
_WEIGHTS = [w for _, w in _BROWSER_WEIGHTS]


# ---------------------------------------------------------------------------
# Consistent hardware and navigator profiles bound to each browser family
#
# Key point: navigator.platform, vendor, and deviceMemory differ by engine:
#   - vendor:       Safari/iOS="Apple Computer, Inc.", Chrome="Google Inc.",
#                   Firefox="" (an empty string, not undefined)
#   - deviceMemory: exposed only by Chromium and capped at 8 by the specification;
#                   Safari/Firefox use None (undefined)
#   - platform:     mac_safari=MacIntel, ios_safari=iPhone, chrome/firefox=Win32
#   - maxTouchPoints: 5 only for iOS touchscreens; otherwise 0
#   - devicePixelRatio: Retina commonly uses 2.0/3.0; Windows commonly uses 1.0/1.25/1.5
# These values must remain fixed throughout a registration because a real browser does
# not change them within a session. generate_fingerprint() therefore selects them once
# with the same RNG and stores them in the fingerprint dictionary.
# ---------------------------------------------------------------------------
_HARDWARE_PROFILES = {
    "mac_safari": {
        "navigator_platform": "MacIntel",
        "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": [8, 10, 12, 16],
        "device_memory": [None],          # Safari does not expose deviceMemory
        "max_touch_points": [0],
        "device_pixel_ratio": [2.0],      # Retina is always 2.0
    },
    "ios_safari": {
        "navigator_platform": "iPhone",
        "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": [4, 6],   # A15/A16/A17
        "device_memory": [None],          # iOS Safari does not expose it
        "max_touch_points": [5],          # Touchscreen
        "device_pixel_ratio": [2.0, 3.0],
    },
    "chrome": {
        "navigator_platform": "Win32",
        "navigator_vendor": "Google Inc.",
        "hardware_concurrency": [4, 6, 8, 12, 16, 24],
        "device_memory": [4, 8],          # Specification caps the value at 8
        "max_touch_points": [0],
        "device_pixel_ratio": [1.0, 1.25, 1.5],
    },
    "firefox": {
        "navigator_platform": "Win32",
        "navigator_vendor": "",           # Firefox navigator.vendor is an empty string
        "hardware_concurrency": [4, 6, 8, 12, 16],
        "device_memory": [None],          # Firefox does not expose deviceMemory
        "max_touch_points": [0],
        "device_pixel_ratio": [1.0, 1.5],
    },
}


def _apply_hardware(fp: dict, r: random.Random) -> None:
    """Select a consistent hardware profile for browser_type and add it to the fingerprint."""
    prof = _HARDWARE_PROFILES.get(fp["browser_type"], _HARDWARE_PROFILES["chrome"])
    fp["navigator_platform"] = prof["navigator_platform"]
    fp["navigator_vendor"] = prof["navigator_vendor"]
    fp["hardware_concurrency"] = r.choice(prof["hardware_concurrency"])
    fp["device_memory"] = r.choice(prof["device_memory"])
    fp["max_touch_points"] = r.choice(prof["max_touch_points"])
    fp["device_pixel_ratio"] = r.choice(prof["device_pixel_ratio"])


# ---------------------------------------------------------------------------
# Fingerprint generation
# ---------------------------------------------------------------------------

def _gen_mac_safari(r: random.Random) -> dict:
    safari = r.choice(_SAFARI_VERSIONS)
    macos_ver = r.choice(safari["macos_versions"])
    others = [s["impersonate"] for s in _SAFARI_VERSIONS if s["impersonate"] != safari["impersonate"]]
    return {
        "browser_type": "mac_safari",
        "impersonate": safari["impersonate"],
        "fallback_impersonates": [safari["impersonate"]] + r.sample(others, min(2, len(others))),
        "user_agent": (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {macos_ver}) "
            f"AppleWebKit/{safari['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{safari['safari_ver']} Safari/{safari['webkit_ver']}"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_MAC_SCREENS),
    }


def _gen_ios_safari(r: random.Random) -> dict:
    safari = r.choice(_IOS_SAFARI_VERSIONS)
    ios_ver = r.choice(safari["ios_versions"])
    others = [s["impersonate"] for s in _IOS_SAFARI_VERSIONS if s["impersonate"] != safari["impersonate"]]
    fallbacks = [safari["impersonate"]] + others
    return {
        "browser_type": "ios_safari",
        "impersonate": safari["impersonate"],
        "fallback_impersonates": fallbacks,
        "user_agent": (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
            f"AppleWebKit/{safari['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{safari['safari_ver']} Mobile/15E148 Safari/604.1"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_IPHONE_SCREENS),
    }


def _gen_chrome(r: random.Random) -> dict:
    chrome = r.choice(_CHROME_VERSIONS)
    others = [c["impersonate"] for c in _CHROME_VERSIONS if c["impersonate"] != chrome["impersonate"]]
    sec_ch_ua = (
        f'"Chromium";v="{chrome["ver"]}", '
        f'"Google Chrome";v="{chrome["ver"]}", '
        f'{chrome["not_a_brand"]}'
    )
    # Complete Client Hints set: full-version-list includes full version numbers
    sec_ch_ua_full_version_list = (
        f'"Chromium";v="{chrome["full_ver"]}", '
        f'"Google Chrome";v="{chrome["full_ver"]}", '
        f'{chrome["not_a_brand"]}'  # Not.A/Brand retains its major version
    )
    # Windows NT version mapping: 10.0.19041+ corresponds to different builds.
    # Common user-visible versions include 21H2 (19044), 22H2 (19045),
    # Windows 11 21H2 (22000), and Windows 11 22H2 (22621).
    # Use the real mappings for Windows 10 22H2 (19045) and Windows 11 23H2 (22631).
    win_platform_versions = ["10.0.19045", "15.0.0"]  # Win10 22H2 / Win11 (UA says 10.0, but CH may report 15)
    platform_version = r.choice(win_platform_versions)

    return {
        "browser_type": "chrome",
        "impersonate": chrome["impersonate"],
        "fallback_impersonates": [chrome["impersonate"]] + r.sample(others, min(2, len(others))),
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome['full_ver']} Safari/537.36"
        ),
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_platform": '"Windows"',
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_full_version_list": sec_ch_ua_full_version_list,
        "sec_ch_ua_arch": '"x86"',
        "sec_ch_ua_bitness": '"64"',
        "sec_ch_ua_model": '""',  # Desktop Chrome model is a quoted empty string
        "sec_ch_ua_platform_version": f'"{platform_version}"',
        "screen": r.choice(_WIN_SCREENS),
    }


def _gen_firefox(r: random.Random) -> dict:
    ff = r.choice(_FIREFOX_VERSIONS)
    others = [f["impersonate"] for f in _FIREFOX_VERSIONS if f["impersonate"] != ff["impersonate"]]
    fallbacks = [ff["impersonate"]] + others
    return {
        "browser_type": "firefox",
        "impersonate": ff["impersonate"],
        "fallback_impersonates": fallbacks,
        "user_agent": (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{ff['ver']}) "
            f"Gecko/20100101 Firefox/{ff['ver']}"
        ),
        "sec_ch_ua": "",
        "sec_ch_ua_platform": "",
        "sec_ch_ua_mobile": "",
        "screen": r.choice(_WIN_SCREENS),
    }


_GENERATORS = {
    "mac_safari": _gen_mac_safari,
    "ios_safari": _gen_ios_safari,
    "chrome": _gen_chrome,
    "firefox": _gen_firefox,
}


def generate_fingerprint(rng: random.Random | None = None, country_code: str = "") -> dict:
    """Generate a consistent browser fingerprint.

    Args:
        rng: Random-number generator. Supplying one preserves consistency within a session.
        country_code: IP geolocation country code (for example, JP/US/DE), used to align
            timezone and language settings.

    Returns:
        browser_type: Browser family.
        impersonate: curl_cffi TLS fingerprint name.
        fallback_impersonates: Fallback impersonation list from the same family.
        user_agent: Complete user-agent string.
        sec_ch_ua: Client Hints value, non-empty only for Chrome.
        sec_ch_ua_platform: str
        sec_ch_ua_mobile: str
        sec_ch_ua_full_version_list: Full-version list, Chrome only.
        sec_ch_ua_arch: CPU architecture, Chrome only.
        sec_ch_ua_bitness: CPU bitness, Chrome only.
        sec_ch_ua_model: Device model, Chrome only; empty on desktop.
        sec_ch_ua_platform_version: OS version, Chrome only.
        screen: Screen resolution in WxH format.
        lang: Primary language.
        lang_full: Complete Accept-Language value.
        timezone: IANA timezone name, for example Asia/Tokyo.
        navigator_platform: navigator.platform (MacIntel/iPhone/Win32).
        navigator_vendor: Engine-specific navigator.vendor; empty for Firefox.
        hardware_concurrency: Logical CPU count.
        device_memory: navigator.deviceMemory; populated only for Chromium.
        max_touch_points: navigator.maxTouchPoints; 5 for iOS.
        device_pixel_ratio: window.devicePixelRatio.
    """
    r = rng or random
    browser_type = r.choices(_BROWSER_TYPES, weights=_WEIGHTS, k=1)[0]
    fp = _GENERATORS[browser_type](r)

    # Align timezone and language with the IP geolocation country code
    country_code = (country_code or "").strip().upper()
    profile = _COUNTRY_PROFILES.get(country_code, _DEFAULT_COUNTRY_PROFILE)

    # Select a timezone using weighted randomness
    tz_choices = profile["timezones"]
    tz_list = [tz for tz, _ in tz_choices]
    tz_weights = [w for _, w in tz_choices]
    timezone = r.choices(tz_list, weights=tz_weights, k=1)[0]

    # Select 3-5 languages while keeping the primary language first
    lang_pool = profile["languages"].copy()
    # If fewer than three languages are available, use the pool size to keep randint bounds valid
    lo = min(3, len(lang_pool))
    hi = min(5, len(lang_pool))
    num_langs = r.randint(lo, hi)
    primary_lang = lang_pool[0]  # Keep the primary language first
    other_langs = lang_pool[1:]
    r.shuffle(other_langs)  # Randomize the remaining languages
    selected = [primary_lang] + other_langs[:num_langs - 1]

    # Build Accept-Language with descending q-value weights
    # Real browsers omit q for the primary language, then use 0.9, 0.8, 0.7, and so on
    lang_parts = []
    for i, lang in enumerate(selected):
        if i == 0:
            lang_parts.append(lang)
        else:
            q = round(1.0 - i * 0.1, 1)  # i=1→0.9, i=2→0.8, ...
            lang_parts.append(f"{lang};q={q}")
    lang_full = ",".join(lang_parts)

    fp["lang"] = primary_lang
    fp["lang_full"] = lang_full
    fp["timezone"] = timezone
    _apply_hardware(fp, r)

    # Add empty keys for non-Chrome families so callers can read them without KeyError
    if browser_type != "chrome":
        fp.setdefault("sec_ch_ua_full_version_list", "")
        fp.setdefault("sec_ch_ua_arch", "")
        fp.setdefault("sec_ch_ua_bitness", "")
        fp.setdefault("sec_ch_ua_model", "")
        fp.setdefault("sec_ch_ua_platform_version", "")

    return fp


# ---------------------------------------------------------------------------
# impersonate-to-UA mapping used during TLS rotation
# ---------------------------------------------------------------------------

_ALL_IMPERSONATES: dict[str, dict] = {}

for s in _SAFARI_VERSIONS:
    _ALL_IMPERSONATES[s["impersonate"]] = {"type": "mac_safari", "data": s}
for s in _IOS_SAFARI_VERSIONS:
    _ALL_IMPERSONATES[s["impersonate"]] = {"type": "ios_safari", "data": s}
for c in _CHROME_VERSIONS:
    _ALL_IMPERSONATES[c["impersonate"]] = {"type": "chrome", "data": c}
for f in _FIREFOX_VERSIONS:
    _ALL_IMPERSONATES[f["impersonate"]] = {"type": "firefox", "data": f}


def fingerprint_for_impersonate(impersonate: str, current_fp: dict) -> dict:
    """Synchronize version-dependent fingerprint fields for a new impersonation.

    Changing impersonation during TLS rotation (_rotate_impersonate_session) requires more
    than changing the UA. _common_headers and _navigation_headers obtain every sec-ch-ua*
    value from the fingerprint. Without synchronization, the UA could report Chrome/136
    while sec-ch-ua reports v=146, including a mismatched not_a_brand value
    (136:"Not.A/Brand";v="99" / 142:"Not/A)Brand";v="8" / 146:"Not?A_Brand";v="99").
    Cloudflare can readily identify this contradiction.

    Only version-dependent fields (sec_ch_ua, full_version_list, and user_agent) change.
    Session-level screen, language, timezone, and hardware properties stay fixed because
    they are unrelated to browser version and changing them would break device consistency.

    Unknown impersonations and non-Chrome families need no synchronization because
    Safari and Firefox do not send client hints (sec_ch_ua is empty); return a copy unchanged.
    """
    entry = _ALL_IMPERSONATES.get(impersonate)
    fp = dict(current_fp or {})
    if not entry:
        return fp

    t, d = entry["type"], entry["data"]
    fp["impersonate"] = impersonate
    fp["browser_type"] = t
    fp["user_agent"] = ua_for_impersonate(impersonate, fp.get("user_agent", ""))

    if t == "chrome":
        fp["sec_ch_ua"] = (
            f'"Chromium";v="{d["ver"]}", '
            f'"Google Chrome";v="{d["ver"]}", '
            f'{d["not_a_brand"]}'
        )
        fp["sec_ch_ua_full_version_list"] = (
            f'"Chromium";v="{d["full_ver"]}", '
            f'"Google Chrome";v="{d["full_ver"]}", '
            f'{d["not_a_brand"]}'
        )
        # platform/mobile/arch/bitness/model/platform_version belong to the device,
        # not the Chrome version. Retain them, using desktop Windows defaults if missing.
        fp.setdefault("sec_ch_ua_platform", '"Windows"')
        fp.setdefault("sec_ch_ua_mobile", "?0")
        fp.setdefault("sec_ch_ua_arch", '"x86"')
        fp.setdefault("sec_ch_ua_bitness", '"64"')
        fp.setdefault("sec_ch_ua_model", '""')
        fp.setdefault("sec_ch_ua_platform_version", '"10.0.19045"')
    else:
        # Non-Chromium browsers send no client hints, matching real browser behavior
        fp["sec_ch_ua"] = ""
        fp["sec_ch_ua_platform"] = ""
        fp["sec_ch_ua_mobile"] = ""
        fp["sec_ch_ua_full_version_list"] = ""
        fp["sec_ch_ua_arch"] = ""
        fp["sec_ch_ua_bitness"] = ""
        fp["sec_ch_ua_model"] = ""
        fp["sec_ch_ua_platform_version"] = ""
    return fp


def ua_for_impersonate(impersonate: str, current_ua: str) -> str:
    """Generate a UA that matches the impersonation name."""
    entry = _ALL_IMPERSONATES.get(impersonate)
    if not entry:
        return current_ua

    t, d = entry["type"], entry["data"]

    if t == "mac_safari":
        macos_ver = random.choice(d["macos_versions"])
        return (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X {macos_ver}) "
            f"AppleWebKit/{d['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{d['safari_ver']} Safari/{d['webkit_ver']}"
        )
    elif t == "ios_safari":
        ios_ver = random.choice(d["ios_versions"])
        return (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {ios_ver} like Mac OS X) "
            f"AppleWebKit/{d['webkit_ver']} (KHTML, like Gecko) "
            f"Version/{d['safari_ver']} Mobile/15E148 Safari/604.1"
        )
    elif t == "chrome":
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{d['full_ver']} Safari/537.36"
        )
    elif t == "firefox":
        return (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{d['ver']}) "
            f"Gecko/20100101 Firefox/{d['ver']}"
        )
    return current_ua
