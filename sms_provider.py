"""SMS 接码 provider 抽象 + SmsBower 实现。

设计参考：asz798838958/GeniusFKoai 的 core/base_sms.py，但裁剪掉浏览器回调相关代码、
仅保留纯协议注册需要的两段流程：
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) 成功/失败       → provider.report_success / cancel / mark_code_failed

⚠️ 关键事实：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证，**纯 SMS 路径目前只有
泰国（country_id=52）确认可用**。其它国家可能抽到 WhatsApp 号导致拿不到 SMS。
SmsBower 的 `auto_select_country=True` 会按价格 + 库存自动选号。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""
    activation_id: str
    phone_number: str          # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到 code 即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180,
                 resend_interval: Optional[int] = None,
                 resend_max: Optional[int] = None) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台可能据此结算/允许复用。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到 code 但 validate 失败 → 请求 resend。"""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 停止复用。"""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """注册 resend 钩子（SmsBower 长等待时回调业务侧重新触发 OTP）。"""
        return None


# ---------------------------------------------------------------------------
# 国家 ID → 中文名映射（sms-activate.org 协议系，SmsBower 共用）
# ---------------------------------------------------------------------------

SMS_COUNTRY_NAMES_CN: dict[str, str] = {
    "0": "俄罗斯", "1": "乌克兰", "2": "哈萨克斯坦", "3": "中国", "4": "菲律宾",
    "5": "缅甸", "6": "印度尼西亚", "7": "马来西亚", "8": "肯尼亚", "9": "坦桑尼亚",
    "10": "越南", "11": "吉尔吉斯斯坦", "12": "美国(虚拟)", "13": "以色列", "14": "香港",
    "15": "波兰", "16": "英国", "17": "马达加斯加", "18": "刚果(布)", "19": "尼日利亚",
    "20": "澳门", "21": "埃及", "22": "印度", "23": "爱尔兰", "24": "柬埔寨",
    "25": "老挝", "26": "海地", "27": "科特迪瓦", "28": "冈比亚", "29": "塞尔维亚",
    "30": "也门", "31": "南非", "32": "罗马尼亚", "33": "哥伦比亚", "34": "爱沙尼亚",
    "35": "阿塞拜疆", "36": "加拿大", "37": "摩洛哥", "38": "加纳", "39": "阿根廷",
    "40": "乌兹别克斯坦", "41": "喀麦隆", "42": "乍得", "43": "德国", "44": "立陶宛",
    "45": "克罗地亚", "46": "瑞典", "47": "伊拉克", "48": "荷兰", "49": "拉脱维亚",
    "50": "奥地利", "51": "白俄罗斯", "52": "泰国", "53": "沙特阿拉伯", "54": "墨西哥",
    "55": "台湾", "56": "西班牙", "57": "伊朗", "58": "阿尔及利亚", "59": "斯洛文尼亚",
    "60": "孟加拉国", "61": "塞内加尔", "62": "土耳其", "63": "捷克", "64": "斯里兰卡",
    "65": "秘鲁", "66": "巴基斯坦", "67": "新西兰", "68": "几内亚", "69": "马里",
    "70": "委内瑞拉", "71": "埃塞俄比亚", "72": "蒙古", "73": "巴西", "74": "阿富汗",
    "75": "乌干达", "76": "安哥拉", "77": "塞浦路斯", "78": "法国", "79": "巴布亚新几内亚",
    "80": "莫桑比克", "81": "尼泊尔", "82": "比利时", "83": "保加利亚", "84": "匈牙利",
    "85": "摩尔多瓦", "86": "意大利", "87": "巴拉圭", "88": "洪都拉斯", "89": "突尼斯",
    "90": "尼加拉瓜", "91": "东帝汶", "92": "玻利维亚", "93": "哥斯达黎加", "94": "危地马拉",
    "95": "阿联酋", "96": "津巴布韦", "97": "波多黎各", "98": "苏丹", "99": "多哥",
    "100": "科威特", "101": "萨尔瓦多", "102": "利比亚", "103": "牙买加", "104": "特立尼达和多巴哥",
    "105": "厄瓜多尔", "106": "斯威士兰", "107": "阿曼", "108": "波黑", "109": "多米尼加",
    "110": "叙利亚", "111": "卡塔尔", "112": "巴拿马", "113": "古巴", "114": "毛里塔尼亚",
    "115": "塞拉利昂", "116": "约旦", "117": "葡萄牙", "118": "巴巴多斯", "119": "布隆迪",
    "120": "贝宁", "121": "文莱", "122": "巴哈马", "123": "博茨瓦纳", "124": "伯利兹",
    "125": "中非", "126": "多米尼克", "127": "格林纳达", "128": "格鲁吉亚", "129": "希腊",
    "130": "几内亚比绍", "131": "圭亚那", "132": "冰岛", "133": "科摩罗", "134": "利比里亚",
    "135": "莱索托", "136": "马拉维", "137": "纳米比亚", "138": "尼日尔", "139": "卢旺达",
    "140": "斯洛伐克", "141": "苏里南", "142": "塔吉克斯坦", "143": "摩纳哥", "144": "巴林",
    "145": "留尼汪岛", "146": "赞比亚", "147": "亚美尼亚", "148": "索马里", "149": "刚果(金)",
    "150": "智利", "151": "布基纳法索", "152": "黎巴嫩", "153": "加蓬", "154": "阿尔巴尼亚",
    "155": "乌拉圭", "156": "毛里求斯", "157": "不丹", "158": "马尔代夫", "159": "瓜德罗普岛",
    "160": "土库曼斯坦", "161": "法属圭亚那", "162": "芬兰", "163": "圣卢西亚", "164": "卢森堡",
    "165": "圣文森特", "166": "赤道几内亚", "167": "吉布提", "168": "安提瓜和巴布达", "169": "开曼群岛",
    "170": "黑山", "171": "丹麦", "172": "瑞士", "173": "挪威", "174": "澳大利亚",
    "175": "厄立特里亚", "176": "南苏丹", "177": "圣多美", "178": "阿鲁巴岛", "179": "蒙特塞拉特",
    "180": "安圭拉岛", "181": "北马其顿", "182": "塞舌尔", "183": "新喀里多尼亚", "184": "佛得角",
    "185": "美国(实体)", "186": "巴勒斯坦", "187": "美国", "188": "中国", "189": "韩国",
    "190": "科特迪瓦", "191": "日本",
}


def country_label(country_id) -> str:
    """返回 '52 泰国' 这样的展示标签。"""
    cid = str(country_id or "").strip()
    name = SMS_COUNTRY_NAMES_CN.get(cid, "")
    return f"{cid} {name}".strip()


# ---------------------------------------------------------------------------
# SmsBower / SMSBower —— 共享 API 协议
# ---------------------------------------------------------------------------

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # Thailand —— OpenAI 走 SMS 的稳定国家
SMS_PHONE_LIFETIME = 20 * 60  # 号码租用窗口（秒）
_SMS_CACHE_LOCK = threading.Lock()
_SMS_VERIFY_LOCK = threading.RLock()
_SMS_CACHE: Optional[dict] = None  # 跨线程共享的号码复用缓存

# OpenAI 走纯 SMS 的国家白名单（截至 2025-2026 实测；其它国家会抽到 WhatsApp 号）
OPENAI_SMS_COUNTRIES = {"52"}  # Thailand only


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _project_cache_dir() -> Path:
    root = Path(__file__).resolve().parent
    cache = root / "data"
    cache.mkdir(parents=True, exist_ok=True)
    return cache


def _smsbower_cache_file() -> Path:
    return _project_cache_dir() / ".smsbower_phone_cache.json"


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": text}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(
            f"{activation_id}:{code}".encode("utf-8")
        ).hexdigest(),
    }


class SmsBowerProvider(BaseSmsProvider):
    """SmsBower (smsbower.page) —— 支持号码复用 + resend + V2 API。"""

    provider_name = "smsbower"
    BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    auto_report_success_on_code = False  # 等业务侧确认才报成功（便于号码复用）

    def __init__(
        self,
        api_key: str,
        *,
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        proxy: Optional[str] = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self._proxy = (proxy or "").strip() or None
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self._resend_callback: Optional[Callable[[], None]] = None
        self.last_code_result: Optional[dict] = None
        self.current_activation: Optional[SmsActivation] = None

    # ---- HTTP ----

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30) -> requests.Response:
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.BASE_URL, params=payload, timeout=timeout, proxies=self._proxies)
        resp.raise_for_status()
        return resp

    # ---- 余额 / 价格 / 国家 ----

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"{self.provider_name} getBalance 失败: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError(f"{self.provider_name} getPrices 返回结构异常")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """按价格 + 库存排序返回国家列表。"""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        # 策略1：使用专用排名 API
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue
        # 策略2：从 getPrices 解析
        try:
            prices = self.get_prices(service=service_code)
            rows = []
            for country_id, services in prices.items():
                if not isinstance(services, dict):
                    continue
                svc = services.get(service_code)
                if not isinstance(svc, dict):
                    continue
                price = svc.get("cost") or svc.get("price")
                count = svc.get("count") or svc.get("qty") or svc.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None and count > 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
            rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
            return rows
        except Exception:
            return []

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        rows = []
        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data
        if isinstance(items, dict):
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = value.get("price") or value.get("cost") or value.get("retail_price")
                count = value.get("count") or value.get("qty") or value.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": country_id, "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = item.get("country") or item.get("countryId") or item.get("country_id") or item.get("id")
                if country_id is None:
                    continue
                price = item.get("price") or item.get("cost")
                count = item.get("count") or item.get("qty") or item.get("available") or 0
                try:
                    price = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price = None
                try:
                    count = int(count) if count is not None else 0
                except (TypeError, ValueError):
                    count = 0
                if price is not None:
                    rows.append({"country": str(country_id), "price": price, "count": count})
        return rows

    def get_best_country(self, service: Optional[str] = None, *,
                         min_stock: int = 20, max_price: float = 0,
                         strict_whitelist: bool = False,
                         allowed_countries: Optional[list[str]] = None) -> Optional[str]:
        """自动选最优国家。

        allowed_countries 优先级最高（用户自定义 = 从这些国家里挑最便宜+库存足的）
        strict_whitelist  = True → 只从 OPENAI_SMS_COUNTRIES 选（即 52 泰国）
        都没设 → 全部国家自由选（默认；用户自行承担"OpenAI 让用 WhatsApp"的风险）
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("%s get_best_country 查询失败: %s", self.provider_name, exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                # 优先用 user-supplied 白名单
                if allowed_set is not None:
                    if cid not in allowed_set:
                        continue
                elif strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    continue
                price = row.get("price") or 0
                count = row.get("count") or 0
                if count < stock_threshold:
                    continue
                if max_price > 0 and price > max_price:
                    continue
                # 非白名单国家 → warn 一下（不阻止）
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    logger.warning(
                        "%s 自动选了非 OpenAI-SMS 白名单国家 country=%s price=%s "
                        "（OpenAI 可能让此号用 WhatsApp 验证 → 收不到 SMS）",
                        self.provider_name, cid, price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # ---- 号码复用缓存 ----

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "provider": getattr(self, "provider_name", self.__class__.__name__),
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> Optional[dict]:
        global _SMS_CACHE
        cache = _SMS_CACHE
        if cache is None:
            path = _smsbower_cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        identity = self._cache_identity(service, country)
        if any(str(cache.get(k) or "") != str(v) for k, v in identity.items()):
            return None
        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"success max reached ({self.phone_success_max})"
            self._save_cache(cache)
            return None
        cache["used_codes"] = set(cache.get("used_codes") or [])
        _SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: Optional[dict]) -> None:
        global _SMS_CACHE
        _SMS_CACHE = cache
        path = _smsbower_cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    # ---- 租号 ----

    def _request_number_single_action(self, action: str, service: str, country: str) -> dict:
        """单次调用 getNumberV2 或 getNumber（不自己 fallback，由调用方双重 for 控制）。

        借鉴 GuJumpgate：每个国家分别试 V2 / V1，而不是内部自动 fallback。
        """
        common = {"action": action, "service": service, "country": country}
        # 用户配了 max_price 才传，空 / <=0 时根本不传（让平台用默认）
        if self.max_price > 0:
            common["maxPrice"] = self.max_price
        logger.info("%s %s: service=%s country=%s maxPrice=%s",
                    self.provider_name, action, service, country, common.get("maxPrice", "未设置"))

        try:
            resp = self._request(common)
            resp_text = resp.text.strip()
            logger.info("%s %s resp: status=%s text=%s", self.provider_name, action, resp.status_code, resp_text[:500])

            # V2 返回 JSON
            if action == "getNumberV2":
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("activationId"):
                        return data
                except ValueError:
                    pass
                raise RuntimeError(resp_text[:200] or "empty response")

            # V1 返回纯文本 ACCESS_NUMBER:id:phone
            if resp_text.startswith("ACCESS_NUMBER:"):
                parts = resp_text.split(":", 2)
                if len(parts) == 3:
                    return {
                        "activationId": parts[1],
                        "phoneNumber": parts[2],
                        "countryPhoneCode": "",
                    }
            raise RuntimeError(resp_text[:200] or "empty response")
        except Exception as e:
            # 不在这里 fallback，让调用方的 for action 循环去试下个 action
            raise

    @staticmethod
    def _format_phone(info: dict) -> str:
        raw = str(info.get("phoneNumber") or "").strip()
        cc = str(info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if cc and raw.startswith(cc):
            return f"+{raw}"
        if cc:
            return f"+{cc}{raw}"
        return f"+{raw}"

    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        """租号。支持多国家候选依次尝试（按入参顺序）。

        country_candidates: 候选国家 ID 列表，按这个顺序依次尝试；空时只用 country 单个。

        借鉴 GuJumpgate: 双重 for 循环 —— 外层遍历国家，内层每个国家先试 getNumberV2，
        失败才 fallback getNumber（V1）。
        """
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        # 单一 country 兜底
        if not country_candidates:
            country_candidates = [str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()]

        with _SMS_VERIFY_LOCK:
            with _SMS_CACHE_LOCK:
                # 复用 cache（仅当用户许可且 cache 国家在候选列表里）
                cache = self._load_cache(service_code, country_candidates[0]) if self.reuse_phone_to_max else None
                if cache and str(cache.get("country") or "") in country_candidates:
                    activation = SmsActivation(
                        activation_id=str(cache["activation_id"]),
                        phone_number=str(cache["phone_number"]),
                        country=str(cache.get("country") or country_candidates[0]),
                        metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                    )
                    self.current_activation = activation
                    return activation

                # 双重 for：外层国家 × 内层 action（V2 / V1）
                failures: list[str] = []
                last_exc: Optional[Exception] = None
                for cid in country_candidates:
                    cid = str(cid).strip()
                    if not cid:
                        continue
                    for action in ("getNumberV2", "getNumber"):
                        try:
                            info = self._request_number_single_action(action, service_code, cid)
                            aid = str(info.get("activationId") or "")
                            phone = self._format_phone(info)
                            if not aid or not phone.strip("+"):
                                failures.append(f"{cid}: {action} 返回信息不完整")
                                continue  # 同国家试下个 action
                            # 成功 → 立刻保存 cache + 返回
                            cache = {
                                **self._cache_identity(service_code, cid),
                                "country": cid,
                                "activation_id": aid,
                                "phone_number": phone,
                                "acquired_at": time.time(),
                                "use_count": 0,
                                "used_codes": set(),
                                "reuse_stopped": False,
                                "stop_reason": "",
                            }
                            self._save_cache(cache)
                            activation = SmsActivation(
                                activation_id=aid,
                                phone_number=phone,
                                country=cid,
                                metadata={"reused": False},
                            )
                            self.current_activation = activation
                            if len(country_candidates) > 1:
                                logger.info("%s 在国家 %s 租到号 %s (action=%s)", self.provider_name, cid, phone, action)
                            return activation
                        except Exception as e:
                            msg = str(e)[:120]
                            failures.append(f"{cid}: {action}={msg}")
                            last_exc = e
                            continue  # 同国家试下个 action

                detail = " | ".join(failures) if failures else "未知"
                raise RuntimeError(f"{self.provider_name} 依次尝试 {len(country_candidates)} 个候选国家全失败: {detail}") from last_exc

    # ---- 等 code / 状态查询 ----

    def get_status(self, activation_id: str) -> dict:
        text = self._request({"action": "getStatus", "id": activation_id}).text
        return _parse_sms_status_text(text)

    def get_status_v2(self, activation_id: str) -> dict:
        resp = self._request({"action": "getStatusV2", "id": activation_id})
        text = resp.text.strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_sms_status_text(text)
        if isinstance(data, str):
            return _parse_sms_status_text(data)
        if not isinstance(data, dict):
            return {"status": "unknown"}
        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_sms_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(activation_id, f"getStatusV2.{channel}", item.get("code"))
                if candidate:
                    return candidate
        return {"status": "wait_code"}

    def request_resend_sms(self, activation_id: str) -> bool:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 3})
            return True
        except Exception:
            return False

    def wait_for_code(self, activation_id: str, *, timeout: int = 80, poll: int = 3,
                       openai_resend_interval: int = 20,
                       openai_resend_max: int = 3) -> Optional[dict]:
        """等 SMS 验证码：每 `openai_resend_interval` 秒触发一次 OpenAI 端 resend，
        最多 `openai_resend_max` 次。超过 timeout 仍没收到 → 返回 None（由上层 cancel 换号）。
        """
        deadline = time.time() + timeout
        start = time.time()
        openai_resend_count = 0
        last_smsbower_resend = start
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE or {}
            used_codes = set(cache.get("used_codes") or [])

        while time.time() < deadline:
            for src in ("v2", "v1"):
                try:
                    if src == "v2":
                        result = self.get_status_v2(activation_id)
                    else:
                        result = self.get_status(activation_id)
                    if result.get("status") == "cancel":
                        return None
                    if result.get("status") == "ok":
                        code = str(result.get("code") or "")
                        if code and code not in used_codes:
                            return {"status": "ok", "code": code,
                                    "sms_key": result.get("sms_key") or ""}
                except Exception as e:
                    logger.debug("%s status %s 失败: %s", self.provider_name, src, e)

            elapsed = time.time() - start
            # OpenAI 端 resend：固定间隔触发，最多 N 次
            expected_resend_count = min(openai_resend_max, int(elapsed // openai_resend_interval))
            if expected_resend_count > openai_resend_count and self._resend_callback:
                try:
                    self._resend_callback()
                    openai_resend_count = expected_resend_count
                    logger.info(
                        "%s: 已请求 OpenAI 端 resend (第 %d/%d 次, elapsed=%ds)",
                        self.provider_name, openai_resend_count, openai_resend_max, int(elapsed),
                    )
                except Exception as e:
                    logger.warning("OpenAI resend callback 失败: %s", e)
                # 同步请求 SmsBower 端 resend
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()
            elif time.time() - last_smsbower_resend >= openai_resend_interval:
                # 平时也间歇请求 SmsBower 端 resend，跟 OpenAI 同节奏
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()

            time.sleep(poll)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 180,
                 resend_interval: Optional[int] = None,
                 resend_max: Optional[int] = None) -> str:
        # ⚠️ 不再用 cache.remaining 延长 timeout：
        # 用户给的 timeout 就是真 timeout，超时就让上层换号或换 attempt。
        # （旧逻辑会被拉到 20 分钟号码生命周期，OpenAI 端 phone-otp challenge 等不了那么久）
        kwargs: dict = {"timeout": timeout}
        if resend_interval is not None:
            kwargs["openai_resend_interval"] = resend_interval
        if resend_max is not None:
            kwargs["openai_resend_max"] = resend_max
        candidate = self.wait_for_code(activation_id, **kwargs)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # ---- 状态报告 ----

    @staticmethod
    def _is_cancel_response_ok(resp: requests.Response) -> bool:
        """判断 cancel/setStatus 响应是否表示退款成功。

        实测两平台的成功指纹（唯一精确匹配，不做模糊判断）：
          - hero-sms  cancelActivation  → HTTP 204 + 空 body
          - smsbower  setStatus=8       → HTTP 200 + 纯文本 "ACCESS_CANCEL"

        其它一律判失败（包括 200 + BAD_ACTION/BAD_STATUS 这类错误响应）。
        """
        try:
            if resp.status_code >= 400:
                return False
            if resp.status_code == 204:
                return True
            text = (resp.text or "").strip()
            if "ACCESS_CANCEL" in text:
                return True
            return False
        except Exception:
            return False

    @staticmethod
    def _is_early_cancel_denied(status_code: int, text: str) -> bool:
        """识别 SmsBower/HeroSMS 的 EARLY_CANCEL_DENIED（号未到最小激活时长）。"""
        if status_code != 409:
            return False
        return "EARLY_CANCEL_DENIED" in (text or "").upper()

    def _raw_request(self, params: dict) -> requests.Response:
        """不走 raise_for_status 的原始请求，用于 cancel/debug 时拿到真实响应。"""
        payload = dict(params)
        payload["api_key"] = self.api_key
        return requests.get(self.BASE_URL, params=payload, timeout=30, proxies=self._proxies)

    def cancel(self, activation_id: str) -> bool:
        activation_id = str(activation_id)
        ok = False
        last_status = 0
        last_text = ""
        # 1) 先尝试 cancelActivation（不走 raise_for_status，拿到真实响应）
        try:
            resp = self._raw_request({"action": "cancelActivation", "id": activation_id})
            last_status, last_text = resp.status_code, resp.text
            ok = self._is_cancel_response_ok(resp)
            logger.info("%s cancel cancelActivation (id=%s) -> %s %s",
                        self.provider_name, activation_id, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("%s cancel cancelActivation 异常 (id=%s): %s",
                           self.provider_name, activation_id, e)
        # 1.5) EARLY_CANCEL_DENIED：号还没到最小激活时长，丢后台队列等够再重试，不阻塞主流程
        if not ok and self._is_early_cancel_denied(last_status, last_text):
            _enqueue_bg_cancel(
                api_key=self.api_key, base_url=self.BASE_URL, proxies=self._proxies,
                activation_id=activation_id, provider_name=self.provider_name,
            )
            logger.info("%s ⏳ 号未到最小激活时长，已转入后台取消队列 (id=%s, 满 120s 后取消)",
                        self.provider_name, activation_id)
            with _SMS_CACHE_LOCK:
                cache = _SMS_CACHE
                if cache and str(cache.get("activation_id")) == str(activation_id):
                    self._clear_cache()
            return True  # 已安排后台取消，对主流程而言算"已处理"
        # 2) fallback setStatus=8
        if not ok:
            try:
                resp = self._raw_request({"action": "setStatus", "id": activation_id, "status": 8})
                last_status, last_text = resp.status_code, resp.text
                ok = self._is_cancel_response_ok(resp)
                logger.info("%s cancel setStatus=8 (id=%s) -> %s %s",
                            self.provider_name, activation_id, resp.status_code, resp.text[:200])
            except Exception as e:
                logger.warning("%s cancel setStatus=8 异常 (id=%s): %s",
                               self.provider_name, activation_id, e)
        if not ok:
            logger.warning("%s cancel 退款失败 (activation_id=%s) last_status=%s last_text=%s",
                           self.provider_name, activation_id, last_status, last_text)
        logger.info("%s cancel 退款判定 ok=%s (activation_id=%s) status=%s body=%s",
                    self.provider_name, ok, activation_id, last_status, last_text[:200])
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._clear_cache()
        return ok

    def report_success(self, activation_id: str) -> bool:
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            should_finish = False
            should_clear = False
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                remaining = SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    should_finish = True
                    cache["reuse_stopped"] = True
                elif remaining <= 30:
                    should_finish = True
                    should_clear = True
                    cache["reuse_stopped"] = True
                self._save_cache(cache)
                if should_clear:
                    self._clear_cache()
        try:
            if should_finish or not (cache and str(cache.get("activation_id")) == str(activation_id)):
                resp = self._request({"action": "finishActivation", "id": activation_id})
                return resp.status_code in (200, 204) or "ACCESS" in resp.text
        except Exception:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                return "ACCESS" in resp.text
            except Exception:
                return False
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                self._save_cache(cache)
        if self._resend_callback:
            try:
                self._resend_callback()
            except Exception:
                pass
        self.request_resend_sms(activation_id)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # 业务侧拒了这个号 → 立即 cancel 退款（号根本没用上，不能让主人白花钱）
        # 统一走 cancel()：先 cancelActivation，失败再 fallback setStatus=8
        try:
            cancel_ok = self.cancel(activation_id)
        except Exception as e:
            cancel_ok = False
            logger.warning("mark_send_failed cancel 异常 (activation_id=%s): %s", activation_id, e)
        logger.info("%s 号 activation_id=%s cancel 退款 %s (原因: %s)",
                    self.provider_name, activation_id, "✅" if cancel_ok else "❌", reason or "未知原因")
        # 同时清掉复用缓存（避免下次注册又拿到这个被拒的号）
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "phone rejected"
                self._save_cache(cache)
                self._clear_cache()

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._resend_callback = callback



class HeroSmsProvider(SmsBowerProvider):
    """HeroSMS (hero-sms.com) —— 与 SmsBower 使用同一套 SMS-Activate 兼容协议。

    支持 getNumber / getStatus / setStatus / getPrices / getTopCountriesByService，
    号码复用、resend、自动选国家等能力直接继承自 SmsBowerProvider。
    """

    provider_name = "herosms"
    BASE_URL = "https://hero-sms.com/stubs/handler_api.php"


# ---------------------------------------------------------------------------
# 后台取消队列：EARLY_CANCEL_DENIED（号未到最小激活时长）时不阻塞主流程，
# 把 cancel 任务丢到后台线程，等够 minActivationTime 再重试 cancelActivation/setStatus=8。
# ---------------------------------------------------------------------------

import queue as _queue

_BG_CANCEL_Q: "_queue.Queue[dict]" = _queue.Queue()
_BG_CANCEL_THREAD: Optional[threading.Thread] = None
_BG_CANCEL_LOCK = threading.Lock()


def _enqueue_bg_cancel(*, api_key: str, base_url: str, proxies: Optional[dict],
                       activation_id: str, provider_name: str) -> None:
    """把一个 cancel 任务丢到后台队列；主流程立即返回。

    入队时存上时间戳 ts；worker FIFO 一个个处理，取出后算「距 ts 是否满 120s」，
    不够就补睡剩余时间，再 cancel。队列先进先出，无需 pending/批量。
    """
    _BG_CANCEL_Q.put({
        "api_key": api_key, "base_url": base_url, "proxies": proxies,
        "activation_id": str(activation_id),
        "provider_name": provider_name,
        "ts": time.time(),
    })
    _ensure_bg_cancel_thread()


def _ensure_bg_cancel_thread() -> None:
    global _BG_CANCEL_THREAD
    if _BG_CANCEL_THREAD and _BG_CANCEL_THREAD.is_alive():
        return
    with _BG_CANCEL_LOCK:
        if _BG_CANCEL_THREAD and _BG_CANCEL_THREAD.is_alive():
            return
        t = threading.Thread(target=_bg_cancel_worker, daemon=True, name="sms-bg-cancel")
        t.start()
        _BG_CANCEL_THREAD = t


def _bg_cancel_once(api_key: str, base_url: str, proxies: Optional[dict],
                    activation_id: str, provider_name: str) -> tuple:
    """执行一次 cancelActivation + fallback setStatus=8，返回 (ok, last_status, last_text)。"""
    ok = False
    last_status = 0
    last_text = ""
    base_params = {"api_key": api_key}
    try:
        resp = requests.get(base_url, params={**base_params, "action": "cancelActivation",
                                              "id": activation_id}, timeout=30, proxies=proxies)
        last_status, last_text = resp.status_code, resp.text
        ok = SmsBowerProvider._is_cancel_response_ok(resp)
        logger.info("[bg-cancel] %s cancelActivation (id=%s) -> %s %s",
                    provider_name, activation_id, resp.status_code, resp.text[:200])
    except Exception as e:
        logger.warning("[bg-cancel] %s cancelActivation 异常 (id=%s): %s", provider_name, activation_id, e)
    if not ok:
        try:
            resp = requests.get(base_url, params={**base_params, "action": "setStatus",
                                                  "id": activation_id, "status": 8}, timeout=30, proxies=proxies)
            last_status, last_text = resp.status_code, resp.text
            ok = SmsBowerProvider._is_cancel_response_ok(resp)
            logger.info("[bg-cancel] %s setStatus=8 (id=%s) -> %s %s",
                        provider_name, activation_id, resp.status_code, resp.text[:200])
        except Exception as e:
            logger.warning("[bg-cancel] %s setStatus=8 异常 (id=%s): %s", provider_name, activation_id, e)
    logger.info("[bg-cancel] %s 判定 ok=%s (id=%s) status=%s body=%s",
                provider_name, ok, activation_id, last_status, last_text[:200])
    return ok, last_status, last_text


_BG_CANCEL_MIN_WAIT = 120  # 距入队时间戳至少满 120s 才取消（号要过 minActivationTime）


def _bg_cancel_worker() -> None:
    while True:
        item = _BG_CANCEL_Q.get()  # FIFO，阻塞等任务
        try:
            aid = item["activation_id"]
            tag = item.get("provider_name", "sms")
            # 距入队时间戳还差多少到 120s；不够就补睡
            elapsed = time.time() - float(item.get("ts") or 0)
            remaining = _BG_CANCEL_MIN_WAIT - elapsed
            if remaining > 0:
                logger.info("[bg-cancel] %s activation_id=%s 还需等 %.0fs",
                            tag, aid, remaining)
                time.sleep(remaining)
            ok, status, text = _bg_cancel_once(
                item["api_key"], item["base_url"], item.get("proxies"), aid, tag
            )
            if ok:
                logger.info("[bg-cancel] ✅ %s 后台取消成功 activation_id=%s", tag, aid)
            else:
                logger.warning("[bg-cancel] ❌ %s 后台取消失败 activation_id=%s last=%s %s",
                               tag, aid, status, text[:200])
        except Exception as e:
            logger.warning("[bg-cancel] worker 异常: %s", e)
        finally:
            _BG_CANCEL_Q.task_done()


# ---------------------------------------------------------------------------
# 工厂 + 回调控制器（注入到 auth_flow）
# ---------------------------------------------------------------------------


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    provider_key: smsbower / herosms
    config 字段：sms_api_key / sms_country / sms_service / sms_max_price /
                sms_reuse_phone / sms_phone_success_max
    """
    pk = (provider_key or "").lower().strip()
    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{pk} 未配置 API Key")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # 接码 API 请求走的代理：复用全局 proxy（registrar 注入注册流程的代理），
    # 也允许调用方显式传 sms_proxy 覆盖（保留扩展点，目前 WebUI 不暴露）。
    proxy = (str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)
    reuse = _safe_bool(config.get("sms_reuse_phone"), True)
    succ_max = max(0, _safe_int(config.get("sms_phone_success_max"), 3))

    common_kwargs = dict(
        api_key=api_key,
        default_service=service,
        default_country=country or SMS_DEFAULT_COUNTRY,
        max_price=max_price,
        proxy=proxy,
        reuse_phone_to_max=reuse,
        phone_success_max=succ_max,
    )

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(**common_kwargs)
    if pk in ("herosms", "hero_sms"):
        return HeroSmsProvider(**common_kwargs)
    raise RuntimeError(f"未知接码服务: {provider_key}")


class PhoneCallbackController:
    """把 SMS provider 包装成两阶段回调，注入到 auth_flow.add_phone 流程。

    用法（在 auth_flow._handle_add_phone_verification 里）：
        controller = PhoneCallbackController(...)
        phone = controller.get_phone()         # 阶段1：租号
        flow._add_phone_send(phone)
        ...
        code = controller.get_code()           # 阶段2：等 SMS 验证码
        flow._phone_otp_validate(code)
        controller.report_success()            # 成功
        # 失败时 controller.cancel() / mark_code_failed()
    """

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
        keep_country: bool = False,
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        self.keep_country = bool(keep_country)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False
        self._last_country: Optional[str] = None

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def get_phone(self) -> str:
        """阶段 1：租手机号（已带 +）。"""
        provider = self._provider()
        # 同号复用锁（SmsBower 系列才用，防止两个注册任务并发抢同一个 cache）
        if isinstance(provider, SmsBowerProvider) and not self._verify_lock_acquired:
            _SMS_VERIFY_LOCK.acquire()
            self._verify_lock_acquired = True

        # 收集候选国家列表：用户多选 > 自动选号选出的 best > 单一 country
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed_list = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]

        raw_candidates: list[str] = []

        # 如果要求同一账号保持国家不变，且已经租过号，则沿用上次国家
        if self.keep_country and self._last_country:
            self.log(f"🔒 同一账号保持国家不变: 沿用 {self._last_country} {SMS_COUNTRY_NAMES_CN.get(self._last_country, '')}")
            raw_candidates = [self._last_country]

        if not raw_candidates:
            if self.auto_select_country and isinstance(provider, SmsBowerProvider):
                if allowed_list:
                    self.log(f"🔍 自动选号: 从主人勾选的 {len(allowed_list)} 个国家依次尝试（按价格升序）")
                    try:
                        rows = provider.get_top_countries(service=self.service)
                        # 按价格升序排，只保留在 allowed_list 中的
                        in_allow = [r for r in rows if str(r.get("country") or "") in allowed_list]
                        ordered_allowed = [str(r["country"]) for r in in_allow]
                        # 把 allowed 里没在排名中出现的也加在最后
                        appended = [c for c in allowed_list if c not in ordered_allowed]
                        raw_candidates = ordered_allowed + appended
                        self.log(f"  候选顺序: {','.join(raw_candidates)}")
                    except Exception as e:
                        self.log(f"  排名查询失败({e})，按主人勾选的原始顺序尝试")
                        raw_candidates = list(allowed_list)
                else:
                    # 未多选时，按价格+库存排序
                    self.log("🔍 自动选号（未指定允许国家，按全平台价格+库存挑最优）...")
                    try:
                        rows = provider.get_top_countries(service=self.service)
                        min_stock = _safe_int(self.config.get("sms_auto_min_stock"), 20)
                        max_price = _safe_float(self.config.get("sms_auto_max_price"), 0)
                        strict_whitelist = _safe_bool(self.config.get("sms_strict_whitelist"), False)

                        def _qualifies(row: dict, stock_threshold: int) -> bool:
                            cid = str(row.get("country") or "")
                            if strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                                return False
                            if max_price > 0 and (row.get("price") or 0) > max_price:
                                return False
                            return (row.get("count") or 0) >= stock_threshold

                        # 先按正常库存阈值
                        raw_candidates = [str(r["country"]) for r in rows if _qualifies(r, min_stock)]
                        if not raw_candidates:
                            raw_candidates = [str(r["country"]) for r in rows if _qualifies(r, 1)]
                        if raw_candidates:
                            labels = []
                            for cid in raw_candidates[:5]:
                                name = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
                                wl = "✅白名单" if cid in OPENAI_SMS_COUNTRIES else "⚠️非白名单"
                                labels.append(f"{cid} {name}[{wl}]")
                            self.log(f"✅ 自动选择国家候选: {' > '.join(labels)}{' ...' if len(raw_candidates) > 5 else ''}")
                        else:
                            self.log("⚠️ 未找到满足条件的国家，使用默认 country")
                            raw_candidates = [self.country] if self.country else []
                    except Exception as e:
                        self.log(f"⚠️ 国家智能选择失败({e})，使用默认 country")
                        raw_candidates = [self.country] if self.country else []
            else:
                # 没启用自动选号 → 强制用默认国家
                raw_candidates = [self.country] if self.country else []

        country_candidates = list(raw_candidates)
        if not country_candidates:
            self.log(f"⚠️ 没有候选国家，fallback 默认国家 {SMS_DEFAULT_COUNTRY}")
            country_candidates = [SMS_DEFAULT_COUNTRY]

        country_label_log = ",".join(
            f"{c}({SMS_COUNTRY_NAMES_CN.get(c, '?')})" for c in country_candidates[:5]
        )
        self.log(f"📱 准备租号: provider={self.provider_key} service={self.service} 候选={country_label_log}{' ...' if len(country_candidates) > 5 else ''}")
        try:
            self.activation = provider.get_number(
                service=self.service,
                country=country_candidates[0],
                country_candidates=country_candidates,
            )
        except Exception as exc:
            self._release_lock()
            raise

        reused = bool((self.activation.metadata or {}).get("reused"))
        used_country = self.activation.country or country_candidates[0]
        if self.keep_country:
            self._last_country = used_country
        used_country_label = f"{used_country} {SMS_COUNTRY_NAMES_CN.get(used_country, '')}"
        self.log(f"✅ 已租到号码{'(复用)' if reused else ''}: {self.activation.phone_number} "
                 f"国家={used_country_label} (activation_id={self.activation.activation_id})")
        return self.activation.phone_number

    def get_code(self, timeout: int = 180, *,
                 resend_interval: Optional[int] = None,
                 resend_max: Optional[int] = None) -> str:
        """阶段 2：等待 SMS 验证码。"""
        if not self.activation:
            raise RuntimeError("PhoneCallbackController: 未先 get_phone")
        provider = self._provider()
        self.log(f"⏳ 等待 SMS 验证码... (activation_id={self.activation.activation_id} timeout={timeout}s)")
        code = provider.get_code(
            self.activation.activation_id, timeout=timeout,
            resend_interval=resend_interval, resend_max=resend_max,
        )
        if code:
            self.log(f"✅ 收到 SMS 验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"⚠️ 未收到 SMS 验证码: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as e:
                logger.warning("report_success 失败: %s", e)
            self.completed = True
            self.log(f"🎉 已标记号码成功完成: activation_id={self.activation.activation_id}")
        self._release_lock()

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        try:
            self._provider().set_resend_callback(callback)
        except Exception:
            pass

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号、解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                ok = self.provider.cancel(self.activation.activation_id)
                status = "✅已退款" if ok else "❌退款失败"
                self.log(f"🗑️ 已释放未使用号码: activation_id={self.activation.activation_id} ({status})")
            except Exception as e:
                self.log(f"🗑️ 释放号码异常: activation_id={self.activation.activation_id}, {e}")
        self._release_lock()

    def _release_lock(self) -> None:
        if self._verify_lock_acquired:
            try:
                _SMS_VERIFY_LOCK.release()
            except RuntimeError:
                pass
            self._verify_lock_acquired = False


# ---------------------------------------------------------------------------
# 简单 CLI 测试
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("用法: python sms_provider.py <provider_key> <api_key> [country]")
        sys.exit(1)
    pk = sys.argv[1]
    key = sys.argv[2]
    cc = sys.argv[3] if len(sys.argv) > 3 else ""
    p = create_sms_provider(pk, {"sms_api_key": key, "sms_country": cc})
    print(f"余额: {p.get_balance()}")
