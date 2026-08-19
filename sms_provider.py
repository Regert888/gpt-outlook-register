"""SMS verification provider abstraction and SmsBower implementation.

The design keeps only the protocol-level registration workflow:
    1) rent number    → provider.get_number(service=..., country=...)
    2) wait sms code  → provider.get_code(activation_id, timeout=...)
    3) report outcome → provider.report_success / cancel / mark_code_failed

OpenAI may route many countries through WhatsApp; Thailand (country_id=52) is
the confirmed pure-SMS route. Other countries may not receive SMS. SmsBower can
select automatically by price and inventory.
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
# Abstract base classes.
# ---------------------------------------------------------------------------


@dataclass
class SmsActivation:
    """Handle for one rented phone number."""
    activation_id: str
    phone_number: str          # E.164 with a leading plus sign.
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """Abstract SMS verification provider."""

    auto_report_success_on_code = True  # True reports on receipt; False waits for validation.

    @abstractmethod
    def get_number(self, *, service: str, country: str = "",
                    country_candidates: Optional[list[str]] = None) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """Return the balance in the provider's currency."""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """Report successful validation for settlement or reuse."""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """Request a resend when a received code fails validation."""
        return None

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """Stop reuse when add-phone/send rejects the number."""
        return None

    def mark_send_succeeded(self, activation_id: str) -> None:
        """Record that add-phone/send successfully triggered delivery."""
        return None

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        """Register a callback that retriggers OTP during long waits."""
        return None


# ---------------------------------------------------------------------------
# Country ID to English name for sms-activate-compatible providers.
# ---------------------------------------------------------------------------

SMS_COUNTRY_NAMES: dict[str, str] = {
    "0": "Russia", "1": "Ukraine", "2": "Kazakhstan", "3": "China", "4": "Philippines",
    "5": "Myanmar", "6": "Indonesia", "7": "Malaysia", "8": "Kenya", "9": "Tanzania",
    "10": "Vietnam", "11": "Kyrgyzstan", "12": "United States (Virtual)", "13": "Israel", "14": "Hong Kong",
    "15": "Poland", "16": "United Kingdom", "17": "Madagascar", "18": "Republic of the Congo", "19": "Nigeria",
    "20": "Macau", "21": "Egypt", "22": "India", "23": "Ireland", "24": "Cambodia",
    "25": "Laos", "26": "Haiti", "27": "Ivory Coast", "28": "Gambia", "29": "Serbia",
    "30": "Yemen", "31": "South Africa", "32": "Romania", "33": "Colombia", "34": "Estonia",
    "35": "Azerbaijan", "36": "Canada", "37": "Morocco", "38": "Ghana", "39": "Argentina",
    "40": "Uzbekistan", "41": "Cameroon", "42": "Chad", "43": "Germany", "44": "Lithuania",
    "45": "Croatia", "46": "Sweden", "47": "Iraq", "48": "Netherlands", "49": "Latvia",
    "50": "Austria", "51": "Belarus", "52": "Thailand", "53": "Saudi Arabia", "54": "Mexico",
    "55": "Taiwan", "56": "Spain", "57": "Iran", "58": "Algeria", "59": "Slovenia",
    "60": "Bangladesh", "61": "Senegal", "62": "Turkey", "63": "Czech Republic", "64": "Sri Lanka",
    "65": "Peru", "66": "Pakistan", "67": "New Zealand", "68": "Guinea", "69": "Mali",
    "70": "Venezuela", "71": "Ethiopia", "72": "Mongolia", "73": "Brazil", "74": "Afghanistan",
    "75": "Uganda", "76": "Angola", "77": "Cyprus", "78": "France", "79": "Papua New Guinea",
    "80": "Mozambique", "81": "Nepal", "82": "Belgium", "83": "Bulgaria", "84": "Hungary",
    "85": "Moldova", "86": "Italy", "87": "Paraguay", "88": "Honduras", "89": "Tunisia",
    "90": "Nicaragua", "91": "Timor-Leste", "92": "Bolivia", "93": "Costa Rica", "94": "Guatemala",
    "95": "United Arab Emirates", "96": "Zimbabwe", "97": "Puerto Rico", "98": "Sudan", "99": "Togo",
    "100": "Kuwait", "101": "El Salvador", "102": "Libya", "103": "Jamaica", "104": "Trinidad and Tobago",
    "105": "Ecuador", "106": "Eswatini", "107": "Oman", "108": "Bosnia and Herzegovina", "109": "Dominican Republic",
    "110": "Syria", "111": "Qatar", "112": "Panama", "113": "Cuba", "114": "Mauritania",
    "115": "Sierra Leone", "116": "Jordan", "117": "Portugal", "118": "Barbados", "119": "Burundi",
    "120": "Benin", "121": "Brunei", "122": "Bahamas", "123": "Botswana", "124": "Belize",
    "125": "Central African Republic", "126": "Dominica", "127": "Grenada", "128": "Georgia", "129": "Greece",
    "130": "Guinea-Bissau", "131": "Guyana", "132": "Iceland", "133": "Comoros", "134": "Liberia",
    "135": "Lesotho", "136": "Malawi", "137": "Namibia", "138": "Niger", "139": "Rwanda",
    "140": "Slovakia", "141": "Suriname", "142": "Tajikistan", "143": "Monaco", "144": "Bahrain",
    "145": "Reunion", "146": "Zambia", "147": "Armenia", "148": "Somalia", "149": "Democratic Republic of the Congo",
    "150": "Chile", "151": "Burkina Faso", "152": "Lebanon", "153": "Gabon", "154": "Albania",
    "155": "Uruguay", "156": "Mauritius", "157": "Bhutan", "158": "Maldives", "159": "Guadeloupe",
    "160": "Turkmenistan", "161": "French Guiana", "162": "Finland", "163": "Saint Lucia", "164": "Luxembourg",
    "165": "Saint Vincent and the Grenadines", "166": "Equatorial Guinea", "167": "Djibouti", "168": "Antigua and Barbuda", "169": "Cayman Islands",
    "170": "Montenegro", "171": "Denmark", "172": "Switzerland", "173": "Norway", "174": "Australia",
    "175": "Eritrea", "176": "South Sudan", "177": "Sao Tome and Principe", "178": "Aruba", "179": "Montserrat",
    "180": "Anguilla", "181": "North Macedonia", "182": "Seychelles", "183": "New Caledonia", "184": "Cape Verde",
    "185": "United States (Physical)", "186": "Palestine", "187": "United States", "188": "China", "189": "South Korea",
    "190": "Ivory Coast", "191": "Japan",
}

# Backward-compatible export retained for existing WebUI imports.
SMS_COUNTRY_NAMES_CN = SMS_COUNTRY_NAMES


def country_label(country_id) -> str:
    """Return a display label such as '52 Thailand'."""
    cid = str(country_id or "").strip()
    name = SMS_COUNTRY_NAMES.get(cid, "")
    return f"{cid} {name}".strip()


# ---------------------------------------------------------------------------
# Shared SmsBower/SMSBower API protocol.
# ---------------------------------------------------------------------------

SMS_DEFAULT_SERVICE = "dr"
SMS_DEFAULT_COUNTRY = "52"  # Thailand is the stable OpenAI SMS route.
SMS_PHONE_LIFETIME = 20 * 60  # Rental window in seconds.
_SMS_CACHE_LOCK = threading.Lock()
_SMS_VERIFY_LOCK = threading.RLock()
_SMS_CACHE: Optional[dict] = None  # Cross-thread number-reuse cache.

# Confirmed pure-SMS countries; other countries may route through WhatsApp.
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
    return str(value).strip().lower() not in {"0", "false", "no", "off", "\u5426"}


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
    """Internal implementation details."""

    DEFAULT_BASE_URL = "https://smsbower.page/stubs/handler_api.php"
    auto_report_success_on_code = False  # Wait for validation to support reuse.

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        fixed_price: float = -1,
        proxy: Optional[str] = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.fixed_price = float(fixed_price or -1)
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
        resp = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
        resp.raise_for_status()
        return resp

    # Internal implementation note.

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"SmsBower getBalance failed: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("SmsBower getPrices returned an unexpected response structure")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """Internal implementation details."""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        # Internal implementation note.
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue
        # Internal implementation note.
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
        """Internal implementation details.

        Internal implementation details.
        Internal implementation details.
        Internal implementation details.
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            logger.warning("SmsBower get_best_country query failed: %s", exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                # Internal implementation note.
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
                # Internal implementation note.
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    logger.warning(
                        "SmsBower selected a country outside the OpenAI SMS allowlist: "
                        "country=%s price=%s (OpenAI may require WhatsApp verification, "
                        "so the SMS code may not arrive)",
                        cid, price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # Internal implementation note.

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
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

    # Internal implementation note.

    def _request_number_single_action(self, action: str, service: str, country: str) -> dict:
        """Internal implementation details.

        Internal implementation details.
        """
        common = {"action": action, "service": service, "country": country}
        # Internal implementation note.
        if self.fixed_price > 0:
            if "hero-sms.com" in self.base_url:
                common["maxPrice"] = self.fixed_price
                common["fixedPrice"] = "true"
            else:
                # Internal implementation note.
                common["minPrice"] = self.fixed_price
                common["maxPrice"] = self.fixed_price
        elif self.max_price > 0:
            common["maxPrice"] = self.max_price
        logger.info("SmsBower %s: service=%s country=%s maxPrice=%s",
                    action, service, country, common.get("maxPrice", "not set"))

        try:
            resp = self._request(common)
            resp_text = resp.text.strip()
            logger.info("SmsBower %s resp: status=%s text=%s", action, resp.status_code, resp_text[:500])

            # Internal implementation note.
            if action == "getNumberV2":
                try:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("activationId"):
                        return data
                except ValueError:
                    pass
                raise RuntimeError(resp_text[:200] or "empty response")

            # Internal implementation note.
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
            # Internal implementation note.
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
        """Internal implementation details.

        Internal implementation details.

        Internal implementation details.
        Internal implementation details.
        """
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        # Internal implementation note.
        if not country_candidates:
            country_candidates = [str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()]

        with _SMS_VERIFY_LOCK:
            with _SMS_CACHE_LOCK:
                # Internal implementation note.
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

                # Internal implementation note.
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
                                failures.append(f"{cid}: {action} returned incomplete data")
                                continue  # Try the next action for this country.
                            # Internal implementation note.
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
                                logger.info("SmsBower rented number %s in country %s (action=%s)", phone, cid, action)
                            return activation
                        except Exception as e:
                            msg = str(e)[:120]
                            failures.append(f"{cid}: {action}={msg}")
                            last_exc = e
                            continue  # Try the next action for this country.

                detail = " | ".join(failures) if failures else "unknown"
                raise RuntimeError(
                    f"SmsBower failed in all {len(country_candidates)} candidate countries: {detail}"
                ) from last_exc

    # Internal implementation note.

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
        """Internal implementation details.
        Internal implementation details.
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
                    logger.debug("SmsBower status %s failed: %s", src, e)

            elapsed = time.time() - start
            # Internal implementation note.
            expected_resend_count = min(openai_resend_max, int(elapsed // openai_resend_interval))
            if expected_resend_count > openai_resend_count and self._resend_callback:
                try:
                    self._resend_callback()
                    openai_resend_count = expected_resend_count
                    logger.info(
                        "SmsBower: requested OpenAI resend (%d/%d, elapsed=%ds)",
                        openai_resend_count, openai_resend_max, int(elapsed),
                    )
                except Exception as e:
                    logger.warning("OpenAI resend callback failed: %s", e)
                # Internal implementation note.
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()
            elif time.time() - last_smsbower_resend >= openai_resend_interval:
                # Internal implementation note.
                self.request_resend_sms(activation_id)
                last_smsbower_resend = time.time()

            time.sleep(poll)
        return None

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        candidate = self.wait_for_code(activation_id, timeout=timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # Internal implementation note.

    def cancel(self, activation_id: str) -> bool:
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            ok = resp.status_code == 204 or "ACCESS_CANCEL" in resp.text
        except Exception:
            ok = False
        if not ok:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
                ok = "ACCESS_CANCEL" in resp.text
            except Exception:
                ok = False
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
        # Internal implementation note.
        cancel_ok = False
        try:
            resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
            cancel_ok = "ACCESS_CANCEL" in resp.text or resp.status_code in (200, 204)
        except Exception:
            pass
        # Internal implementation note.
        short_reason = (reason or "unknown reason")[:80]
        logger.info("SmsBower activation_id=%s cancellation refund %s (reason: %s)",
                    activation_id, "✅" if cancel_ok else "❌", short_reason)
        # Internal implementation note.
        with _SMS_CACHE_LOCK:
            cache = _SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "phone rejected"
                self._save_cache(cache)
                self._clear_cache()

    def set_resend_callback(self, callback: Optional[Callable[[], None]]) -> None:
        self._resend_callback = callback



# ---------------------------------------------------------------------------
# Internal implementation note.
# ---------------------------------------------------------------------------


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """Internal implementation details.

    provider_key: smsbower / herosms
    Internal implementation details.
                sms_reuse_phone / sms_phone_success_max
    """
    pk = (provider_key or "").lower().strip()
    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"API key is not configured for {pk}")
    country = str(config.get("sms_country") or "").strip()
    service = str(config.get("sms_service") or "").strip() or "dr"
    # Internal implementation note.
    # Internal implementation note.
    proxy = (str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None
    max_price = _safe_float(config.get("sms_max_price"), -1)
    fixed_price = _safe_float(config.get("sms_fixed_price"), -1)
    reuse = _safe_bool(config.get("sms_reuse_phone"), False)
    succ_max = max(0, _safe_int(config.get("sms_phone_success_max"), 3))

    if pk in ("smsbower", "sms_bower"):
        return SmsBowerProvider(api_key=api_key,
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    if pk in ("herosms", "hero_sms"):
        return SmsBowerProvider(api_key=api_key,
                                base_url="https://hero-sms.com/stubs/handler_api.php",
                                default_service=service,
                                default_country=country or SMS_DEFAULT_COUNTRY,
                                max_price=max_price,
                                fixed_price=fixed_price,
                                proxy=proxy,
                                reuse_phone_to_max=reuse,
                                phone_success_max=succ_max)
    raise RuntimeError(f"Unknown SMS provider: {provider_key}")


class PhoneCallbackController:
    """Internal implementation details.

    Internal implementation details.
        controller = PhoneCallbackController(...)
        Internal implementation details.
        flow._add_phone_send(phone)
        ...
        Internal implementation details.
        flow._phone_otp_validate(code)
        Internal implementation details.
        # Internal implementation note.
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
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or logger.info
        self.auto_select_country = bool(auto_select_country)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False

    def _provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def get_phone(self) -> str:
        """Internal implementation details."""
        provider = self._provider()
        # Internal implementation note.
        if isinstance(provider, SmsBowerProvider) and not self._verify_lock_acquired:
            _SMS_VERIFY_LOCK.acquire()
            self._verify_lock_acquired = True

        # Internal implementation note.
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed_list = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]

        effective_country = self.country
        country_candidates: list[str] = []

        if self.auto_select_country and isinstance(provider, SmsBowerProvider):
            if allowed_list:
                self.log(f"🔍 Automatic selection: trying {len(allowed_list)} selected countries in ascending price order")
                try:
                    rows = provider.get_top_countries(service=self.service)
                    # Internal implementation note.
                    in_allow = [r for r in rows if str(r.get("country") or "") in allowed_list]
                    ordered_allowed = [str(r["country"]) for r in in_allow]
                    # Internal implementation note.
                    appended = [c for c in allowed_list if c not in ordered_allowed]
                    country_candidates = ordered_allowed + appended
                    self.log(f"  Candidate order: {','.join(country_candidates)}")
                except Exception as e:
                    self.log(f"  Ranking query failed ({e}); using the original selected order")
                    country_candidates = list(allowed_list)
            else:
                # Internal implementation note.
                self.log("🔍 Automatic selection: choosing the best platform-wide price and stock...")
                try:
                    best = provider.get_best_country(
                        service=self.service,
                        min_stock=_safe_int(self.config.get("sms_auto_min_stock"), 20),
                        max_price=_safe_float(self.config.get("sms_auto_max_price"), 0),
                        strict_whitelist=_safe_bool(self.config.get("sms_strict_whitelist"), False),
                    )
                    if best:
                        country_name = SMS_COUNTRY_NAMES.get(best, "Unknown")
                        in_wl = best in OPENAI_SMS_COUNTRIES
                        wl_label = "✅ OpenAI SMS allowlist" if in_wl else "⚠️ outside allowlist"
                        self.log(f"✅ Automatically selected country: {best} {country_name} [{wl_label}]")
                        country_candidates = [best]
                    else:
                        self.log("⚠️ No country met the criteria; using the default country")
                        country_candidates = [self.country] if self.country else []
                except Exception as e:
                    self.log(f"⚠️ Automatic country selection failed ({e}); using the default country")
                    country_candidates = [self.country] if self.country else []
        else:
            # Internal implementation note.
            country_candidates = [self.country] if self.country else []

        if not country_candidates:
            country_candidates = [SMS_DEFAULT_COUNTRY]

        country_label_log = ",".join(
            f"{c}({SMS_COUNTRY_NAMES.get(c, '?')})" for c in country_candidates[:5]
        )
        self.log(f"📱 Preparing to rent a number: provider={self.provider_key} service={self.service} candidates={country_label_log}{' ...' if len(country_candidates) > 5 else ''}")
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
        used_country_label = f"{used_country} {SMS_COUNTRY_NAMES.get(used_country, '')}"
        self.log(f"✅ Rented number{' (reused)' if reused else ''}: {self.activation.phone_number} "
                 f"country={used_country_label} (activation_id={self.activation.activation_id})")
        return self.activation.phone_number

    def get_code(self, timeout: int = 180) -> str:
        """Internal implementation details."""
        if not self.activation:
            raise RuntimeError("PhoneCallbackController: get_phone must be called first")
        provider = self._provider()
        self.log(f"⏳ Waiting for SMS verification code... (activation_id={self.activation.activation_id} timeout={timeout}s)")
        code = provider.get_code(self.activation.activation_id, timeout=timeout)
        if code:
            self.log(f"✅ Received SMS verification code: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"⚠️ SMS verification code was not received: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as e:
                logger.warning("report_success failed: %s", e)
            self.completed = True
            self.log(f"🎉 Marked number as successfully completed: activation_id={self.activation.activation_id}")
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
        """Internal implementation details."""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"🗑️ Released unused number: activation_id={self.activation.activation_id}")
            except Exception:
                pass
        self._release_lock()

    def _release_lock(self) -> None:
        if self._verify_lock_acquired:
            try:
                _SMS_VERIFY_LOCK.release()
            except RuntimeError:
                pass
            self._verify_lock_acquired = False


# ---------------------------------------------------------------------------
# Internal implementation note.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python sms_provider.py <provider_key> <api_key> [country]")
        sys.exit(1)
    pk = sys.argv[1]
    key = sys.argv[2]
    cc = sys.argv[3] if len(sys.argv) > 3 else ""
    p = create_sms_provider(pk, {"sms_api_key": key, "sms_country": cc})
    print(f"Balance: {p.get_balance()}")
