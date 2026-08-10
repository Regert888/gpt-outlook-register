"""浏览器指纹随机化（多浏览器家族）。

每次注册调用 generate_fingerprint() 生成一套一致的指纹组合：
  - TLS impersonate（curl_cffi 用）
  - User-Agent
  - sec-ch-ua / sec-ch-ua-platform / sec-ch-ua-mobile（仅 Chrome）
  - 屏幕分辨率
  - Accept-Language
  - browser_type 标识（mac_safari / ios_safari / chrome / firefox）
  - fallback_impersonates 同家族回退列表
"""
from __future__ import annotations

import random

# ---------------------------------------------------------------------------
# macOS Safari（保留原有）
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
# 国家 → 时区/语言画像（IP 地理联动优化）
# ---------------------------------------------------------------------------
_COUNTRY_PROFILES = {
    # 亚洲
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
    # 北美
    "US": {
        "timezones": [
            ("America/New_York", 0.4),      # 东部（数据中心多）
            ("America/Los_Angeles", 0.3),   # 西部
            ("America/Chicago", 0.2),       # 中部
            ("America/Denver", 0.1),        # 山地
        ],
        "languages": ["en-US", "en", "es-US", "es", "zh-CN"],
    },
    "CA": {
        "timezones": [
            ("America/Toronto", 0.6),       # 东部（安大略）
            ("America/Vancouver", 0.3),     # 西部（BC）
            ("America/Edmonton", 0.1),      # 山地（阿尔伯塔）
        ],
        "languages": ["en-CA", "en-US", "en", "fr-CA", "fr"],
    },
    "MX": {
        "timezones": [("America/Mexico_City", 1.0)],
        "languages": ["es-MX", "es", "en-US", "en"],
    },
    # 南美
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
    # 欧洲
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
            ("Europe/Moscow", 0.7),         # 莫斯科（MSK，主要数据中心）
            ("Asia/Yekaterinburg", 0.15),   # 叶卡捷琳堡（+5）
            ("Asia/Novosibirsk", 0.15),     # 新西伯利亚（+7）
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
    # 大洋洲
    "AU": {
        "timezones": [
            ("Australia/Sydney", 0.5),      # 悉尼（NSW，数据中心多）
            ("Australia/Melbourne", 0.3),   # 墨尔本（VIC）
            ("Australia/Brisbane", 0.2),    # 布里斯班（QLD）
        ],
        "languages": ["en-AU", "en-US", "en", "zh-CN", "zh"],
    },
    "NZ": {
        "timezones": [("Pacific/Auckland", 1.0)],
        "languages": ["en-NZ", "en-US", "en"],
    },
    # 非洲
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

# 兜底策略（未知国家）
_DEFAULT_COUNTRY_PROFILE = {
    "timezones": [("UTC", 1.0)],
    "languages": ["en-US", "en"],
}

# ---------------------------------------------------------------------------
# 共享（旧的固定语言列表，保留兼容性）
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
# 硬件 / navigator 一致性画像（按浏览器家族绑定）
#
# 关键点：navigator.platform / vendor / deviceMemory 在不同引擎行为不同——
#   - vendor:       Safari/iOS="Apple Computer, Inc."，Chrome="Google Inc."，
#                   Firefox=""（空串，不是 undefined）
#   - deviceMemory: 仅 Chromium 暴露且 spec 封顶 8；Safari/Firefox 为 None(undefined)
#   - platform:     mac_safari=MacIntel, ios_safari=iPhone, chrome/firefox=Win32
#   - maxTouchPoints: 只有 iOS 触摸屏=5，其余=0
#   - devicePixelRatio: Retina=2.0/3.0，Windows 常见 1.0/1.25/1.5
# 这些值在一次注册内必须**固定**（真实浏览器同会话不会变），故在
# generate_fingerprint() 里用同一个 RNG 一次性定死，写进指纹 dict。
# ---------------------------------------------------------------------------
_HARDWARE_PROFILES = {
    "mac_safari": {
        "navigator_platform": "MacIntel",
        "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": [8, 10, 12, 16],
        "device_memory": [None],          # Safari 不暴露 deviceMemory
        "max_touch_points": [0],
        "device_pixel_ratio": [2.0],      # Retina 必定 2.0
    },
    "ios_safari": {
        "navigator_platform": "iPhone",
        "navigator_vendor": "Apple Computer, Inc.",
        "hardware_concurrency": [4, 6],   # A15/A16/A17
        "device_memory": [None],          # iOS Safari 不暴露
        "max_touch_points": [5],          # 触摸屏
        "device_pixel_ratio": [2.0, 3.0],
    },
    "chrome": {
        "navigator_platform": "Win32",
        "navigator_vendor": "Google Inc.",
        "hardware_concurrency": [4, 6, 8, 12, 16, 24],
        "device_memory": [4, 8],          # spec 封顶 8
        "max_touch_points": [0],
        "device_pixel_ratio": [1.0, 1.25, 1.5],
    },
    "firefox": {
        "navigator_platform": "Win32",
        "navigator_vendor": "",           # Firefox navigator.vendor 为空串
        "hardware_concurrency": [4, 6, 8, 12, 16],
        "device_memory": [None],          # Firefox 不暴露 deviceMemory
        "max_touch_points": [0],
        "device_pixel_ratio": [1.0, 1.5],
    },
}


def _apply_hardware(fp: dict, r: random.Random) -> None:
    """按 browser_type 从画像池抽一套一致的硬件参数写进指纹 dict。"""
    prof = _HARDWARE_PROFILES.get(fp["browser_type"], _HARDWARE_PROFILES["chrome"])
    fp["navigator_platform"] = prof["navigator_platform"]
    fp["navigator_vendor"] = prof["navigator_vendor"]
    fp["hardware_concurrency"] = r.choice(prof["hardware_concurrency"])
    fp["device_memory"] = r.choice(prof["device_memory"])
    fp["max_touch_points"] = r.choice(prof["max_touch_points"])
    fp["device_pixel_ratio"] = r.choice(prof["device_pixel_ratio"])


# ---------------------------------------------------------------------------
# 指纹生成
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
    # Client Hints 全套：full-version-list 带完整版本号
    sec_ch_ua_full_version_list = (
        f'"Chromium";v="{chrome["full_ver"]}", '
        f'"Google Chrome";v="{chrome["full_ver"]}", '
        f'{chrome["not_a_brand"]}'  # Not.A/Brand 保持主版本号
    )
    # Windows 10 的 NT 版本对应表：10.0.19041+ 对应不同 build
    # 常见 User-Visible 版本：21H2(19044)、22H2(19045)、Win11 21H2(22000)、22H2(22621)
    # 这里选择 Windows 10 22H2(19045) 和 Windows 11 23H2(22631) 的真实对应
    win_platform_versions = ["10.0.19045", "15.0.0"]  # Win10 22H2 / Win11 (UA 里说 10.0 但 CH 可报 15)
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
        "sec_ch_ua_model": '""',  # 桌面 Chrome model 为空串（引号包空串）
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
    """生成一套一致的浏览器指纹。

    参数:
        rng: 随机数生成器（传入可保证会话内一致性）
        country_code: IP 地理国家码（如 JP/US/DE），用于时区/语言联动优化

    返回 dict:
        browser_type: str     — 浏览器家族
        impersonate: str      — curl_cffi TLS 指纹名
        fallback_impersonates: list[str] — 同家族回退 impersonate 列表
        user_agent: str       — 完整 UA 字符串
        sec_ch_ua: str        — Client Hints（仅 Chrome 非空）
        sec_ch_ua_platform: str
        sec_ch_ua_mobile: str
        sec_ch_ua_full_version_list: str — 完整版本号列表（仅 Chrome）
        sec_ch_ua_arch: str              — CPU 架构（仅 Chrome）
        sec_ch_ua_bitness: str           — 位数（仅 Chrome）
        sec_ch_ua_model: str             — 设备型号（仅 Chrome，桌面为空串）
        sec_ch_ua_platform_version: str  — OS 版本号（仅 Chrome）
        screen: str           — 屏幕分辨率 (WxH)
        lang: str             — 主语言
        lang_full: str        — 完整 Accept-Language
        timezone: str         — IANA 时区名（如 Asia/Tokyo）
        navigator_platform: str  — navigator.platform（MacIntel/iPhone/Win32）
        navigator_vendor: str    — navigator.vendor（按引擎；Firefox 为空串）
        hardware_concurrency: int — CPU 逻辑核心数
        device_memory: int|None   — navigator.deviceMemory（仅 Chromium 有值）
        max_touch_points: int     — navigator.maxTouchPoints（iOS=5）
        device_pixel_ratio: float — window.devicePixelRatio
    """
    r = rng or random
    browser_type = r.choices(_BROWSER_TYPES, weights=_WEIGHTS, k=1)[0]
    fp = _GENERATORS[browser_type](r)

    # IP 地理联动：按国家码选择时区/语言
    country_code = (country_code or "").strip().upper()
    profile = _COUNTRY_PROFILES.get(country_code, _DEFAULT_COUNTRY_PROFILE)

    # 加权随机选择时区
    tz_choices = profile["timezones"]
    tz_list = [tz for tz, _ in tz_choices]
    tz_weights = [w for _, w in tz_choices]
    timezone = r.choices(tz_list, weights=tz_weights, k=1)[0]

    # 多语言优化：从池中随机选 3~5 个，保证第一语言是主语言
    lang_pool = profile["languages"].copy()
    # 目标 3~5 个语言；语言池不足 3 个时按池长度取（避免 randint 下界>上界）
    lo = min(3, len(lang_pool))
    hi = min(5, len(lang_pool))
    num_langs = r.randint(lo, hi)
    primary_lang = lang_pool[0]  # 主语言固定第一位
    other_langs = lang_pool[1:]
    r.shuffle(other_langs)  # 其他语言随机打乱
    selected = [primary_lang] + other_langs[:num_langs - 1]

    # 构建 Accept-Language header（带 q 值权重递减）
    # 真实浏览器格式：主语言无 q，第一个副语言 q=0.9，之后 0.8/0.7…
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

    # 非 Chrome 家族补齐空值键（保证调用方统一取值不报 KeyError）
    if browser_type != "chrome":
        fp.setdefault("sec_ch_ua_full_version_list", "")
        fp.setdefault("sec_ch_ua_arch", "")
        fp.setdefault("sec_ch_ua_bitness", "")
        fp.setdefault("sec_ch_ua_model", "")
        fp.setdefault("sec_ch_ua_platform_version", "")

    return fp


# ---------------------------------------------------------------------------
# impersonate → UA 映射（TLS 旋转用）
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
    """把指纹里**随 impersonate 版本变化**的字段同步到新版本，其余原样保留。

    TLS 旋转（_rotate_impersonate_session）换 impersonate 时，光换 UA 是不够的：
    _common_headers / _navigation_headers 的 sec-ch-ua* 全从指纹取，不同步就会出现
    「UA 说 Chrome/136、sec-ch-ua 说 v=146」，连 not_a_brand 都对不上
    （136:"Not.A/Brand";v="99" / 142:"Not/A)Brand";v="8" / 146:"Not?A_Brand";v="99"），
    是 CF 一抓一个准的自相矛盾特征。

    只动版本相关字段（sec_ch_ua / full_version_list / user_agent），
    屏幕、语言、时区、硬件等会话级属性保持不变 —— 那些跟浏览器版本无关，
    换了反而破坏"同一台机器"的一致性。

    未知 impersonate 或非 Chrome 家族：Safari/Firefox 本就不发 client hints
    （sec_ch_ua 为空串），无需同步，原样返回副本。
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
        # platform/mobile/arch/bitness/model/platform_version 只跟设备走，
        # 不随 Chrome 版本变，沿用原指纹即可（缺失时给桌面 Windows 默认值）
        fp.setdefault("sec_ch_ua_platform", '"Windows"')
        fp.setdefault("sec_ch_ua_mobile", "?0")
        fp.setdefault("sec_ch_ua_arch", '"x86"')
        fp.setdefault("sec_ch_ua_bitness", '"64"')
        fp.setdefault("sec_ch_ua_model", '""')
        fp.setdefault("sec_ch_ua_platform_version", '"10.0.19045"')
    else:
        # 非 Chromium：一个 client hint 都不发（真实浏览器行为）
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
    """根据 impersonate 名生成匹配的 UA。"""
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
