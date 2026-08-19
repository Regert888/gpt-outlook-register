"""Side-effect-limited GCash availability probe for ChatGPT checkout.

The public operation can create and update a checkout, calculate taxes, resolve
that checkout, and inspect Stripe Elements capability metadata. It deliberately
has no confirm, start, or payment execution operation.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, Callable


CHATGPT_PAYMENTS_BASE = "https://chatgpt.com/backend-api/payments"
STRIPE_ELEMENTS_URL = "https://api.stripe.com/v1/elements/sessions"
PROMOTION_ID = "plus-1-month-free"
_EXPECTED_CURRENCY = "PHP"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class _ProbeFailure(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def _checked_at(value: float | None) -> float:
    return float(time.time() if value is None else value)


def gcash_probe_error(
    decision: str,
    *,
    retryable: bool,
    status: str = "unknown",
    label: str = "GCash status unknown",
    checked_at: float | None = None,
) -> dict[str, Any]:
    return {
        "operation": "gcash_payment_eligibility",
        "classification": "unknown",
        "eligible": None,
        "decision": str(decision or "probe_failed"),
        "conclusive": False,
        "retryable": bool(retryable),
        "status": str(status or "unknown"),
        "label": str(label or "GCash status unknown"),
        "checked_at": _checked_at(checked_at),
    }


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            if isinstance(child, (Mapping, list, tuple)):
                yield from _walk(child)


def _minor_amount(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip().replace(",", "")):
        return int(value.strip().replace(",", ""))
    return None


def _evidence(payloads: Iterable[Any]) -> dict[str, Any]:
    method_present = False
    explicit_method_collection = False
    custom_ids: list[str] = []
    amounts: set[int] = set()
    currencies: set[str] = set()
    method_keys = {
        "payment_method_types",
        "ordered_payment_method_types",
        "custom_payment_methods",
        "custom_payment_method_data",
    }

    for payload in payloads:
        for item in _walk(payload):
            if not isinstance(item, Mapping):
                continue
            for key, value in item.items():
                key_text = str(key).lower()
                if key_text in method_keys and isinstance(value, (list, tuple)):
                    named_custom_collection = (
                        key_text in {"custom_payment_methods", "custom_payment_method_data"}
                        and not value
                    )
                    if key_text == "custom_payment_methods" and not value:
                        named_custom_collection = True
                    for method in value:
                        if isinstance(method, str):
                            token = method.strip()
                            normalized = token.lower().replace("-", "_")
                            if normalized == "gcash":
                                method_present = True
                                named_custom_collection = True
                            elif token.startswith("cpmt_"):
                                custom_ids.append(token)
                            elif key_text in {"custom_payment_methods", "custom_payment_method_data"}:
                                named_custom_collection = True
                        elif isinstance(method, Mapping):
                            label = " ".join(
                                str(method.get(name) or "")
                                for name in ("display_name", "name", "type", "payment_method_type")
                            ).lower()
                            identifier = str(
                                method.get("custom_payment_method_type_id")
                                or method.get("payment_method_type_id")
                                or method.get("id")
                                or ""
                            ).strip()
                            if identifier.startswith("cpmt_"):
                                custom_ids.append(identifier)
                            if label.strip():
                                named_custom_collection = True
                            if "gcash" in label:
                                method_present = True
                    if named_custom_collection:
                        explicit_method_collection = True
                if key_text in {"currency", "currency_code"} and isinstance(value, str):
                    token = value.strip().upper()
                    if re.fullmatch(r"[A-Z]{3}", token):
                        currencies.add(token)

            total_summary = item.get("total_summary")
            if isinstance(total_summary, Mapping):
                amount = _minor_amount(total_summary.get("due"))
                if amount is not None:
                    amounts.add(amount)
            invoice = item.get("invoice")
            if isinstance(invoice, Mapping):
                amount = _minor_amount(invoice.get("amount_due"))
                if amount is not None:
                    amounts.add(amount)
            amount = _minor_amount(item.get("amount_due"))
            if amount is not None:
                amounts.add(amount)

    return {
        "method_available": True if method_present else False if explicit_method_collection else None,
        "custom_method_ids": list(dict.fromkeys(custom_ids)),
        "amounts": amounts,
        "currencies": currencies,
    }


def classify_gcash_evidence(
    payloads: Iterable[Any],
    *,
    require_zero: bool = True,
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Classify only explicit GCash, amount, and currency evidence."""
    evidence = _evidence(payloads)
    method = evidence["method_available"]
    amounts = evidence["amounts"]
    currencies = evidence["currencies"]
    amount = next(iter(amounts)) if len(amounts) == 1 else None
    currency = next(iter(currencies)) if len(currencies) == 1 else ""
    result = {
        "operation": "gcash_payment_eligibility",
        "classification": "unknown",
        "eligible": None,
        "decision": "gcash_evidence_missing",
        "conclusive": False,
        "retryable": True,
        "status": "unknown",
        "label": "GCash status unknown",
        "checked_at": _checked_at(checked_at),
        "method_available": method,
        "custom_method_id_discovered": bool(evidence["custom_method_ids"]),
        "amount_minor": amount,
        "currency": currency,
    }

    if len(amounts) > 1:
        result["decision"] = "conflicting_amount_evidence"
    elif len(currencies) > 1:
        result["decision"] = "conflicting_currency_evidence"
    elif method is False:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "gcash_unavailable",
            "conclusive": True,
            "retryable": False,
            "status": "ineligible",
            "label": "GCash unavailable",
        })
    elif method is None:
        result["decision"] = "gcash_evidence_missing"
    elif not currency:
        result["decision"] = "currency_unknown"
    elif currency != _EXPECTED_CURRENCY:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "currency_mismatch",
            "conclusive": True,
            "retryable": False,
            "status": "ineligible",
            "label": "GCash ineligible",
        })
    elif require_zero and amount is None:
        result["decision"] = "amount_unknown"
    elif require_zero and amount != 0:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "nonzero_offer",
            "conclusive": True,
            "retryable": False,
            "status": "ineligible",
            "label": "GCash ineligible - nonzero offer",
        })
    else:
        result.update({
            "classification": "eligible",
            "eligible": True,
            "decision": "gcash_zero_due_available" if require_zero else "gcash_available",
            "conclusive": True,
            "retryable": False,
            "status": "eligible",
            "label": "GCash eligible",
        })
    return result


def _chatgpt_headers(
    access_token: str,
    *,
    account_id: str,
    device_id: str,
    cookie_header: str,
    route: str,
) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": _USER_AGENT,
        "OAI-Device-Id": device_id,
        "x-openai-target-path": route,
        "x-openai-target-route": route,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    if cookie_header:
        headers["Cookie"] = cookie_header
    return headers


def _json_response(response: Any, stage: str) -> Mapping[str, Any]:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not 200 <= status_code < 300:
        retryable = status_code in {408, 425, 429} or status_code >= 500
        raise _ProbeFailure(f"{stage}_http_{status_code}", retryable=retryable)
    try:
        payload = response.json()
    except Exception as exc:
        raise _ProbeFailure(f"{stage}_invalid_json", retryable=True) from exc
    if not isinstance(payload, Mapping):
        raise _ProbeFailure(f"{stage}_invalid_payload", retryable=True)
    return payload


def _post_json(session: Any, url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: int, stage: str):
    try:
        response = session.post(
            url,
            json=dict(payload),
            headers=dict(headers),
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise _ProbeFailure(f"{stage}_transport_error", retryable=True) from exc
    return _json_response(response, stage)


def _get_json(session: Any, url: str, *, headers: Mapping[str, str], timeout: int, stage: str, params=None):
    try:
        response = session.get(
            url,
            headers=dict(headers),
            params=params,
            timeout=timeout,
            allow_redirects=False,
        )
    except Exception as exc:
        raise _ProbeFailure(f"{stage}_transport_error", retryable=True) from exc
    return _json_response(response, stage)


def _checkout_session(payload: Mapping[str, Any]) -> tuple[str, str]:
    session_id = str(
        payload.get("checkout_session_id") or payload.get("session_id") or payload.get("id") or ""
    ).strip()
    if not session_id.startswith(("cs_", "oaics_")):
        raise _ProbeFailure("checkout_session_missing", retryable=True)
    if not re.fullmatch(r"(?:cs|oaics)_[A-Za-z0-9_-]+", session_id):
        raise _ProbeFailure("checkout_session_invalid", retryable=False)
    processor = str(payload.get("processor_entity") or "openai_ie").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", processor):
        raise _ProbeFailure("checkout_processor_invalid", retryable=False)
    return session_id, processor


def _stripe_params(checkout: Mapping[str, Any], custom_ids: list[str]) -> dict[str, str] | None:
    customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
    publishable_key = str(checkout.get("publishable_key") or checkout.get("stripe_publishable_key") or "").strip()
    if not customer_secret or not publishable_key.startswith("pk_") or not custom_ids:
        return None
    params = {
        "customer_session_client_secret": customer_secret,
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": "0",
        "deferred_intent[currency]": "php",
        "currency": "php",
        "key": publishable_key,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(uuid.uuid4()),
        "locale": "en-PH",
        "type": "deferred_intent",
        "deferred_intent[setup_future_usage]": "off_session",
        "_stripe_version": (
            "2025-03-31.basil; checkout_server_update_beta=v1; "
            "checkout_manual_approval_preview=v1"
        ),
    }
    for index, custom_id in enumerate(custom_ids):
        params[f"custom_payment_methods[{index}]"] = custom_id
    return params


def probe_gcash(
    access_token: str,
    *,
    account_id: str = "",
    device_id: str = "",
    cookie_header: str = "",
    proxy: str = "",
    timeout: int = 30,
    require_zero: bool = True,
    session_factory: Callable[..., Any] | None = None,
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Probe GCash checkout capability without confirming or starting payment."""
    token = str(access_token or "").strip()
    if not token:
        return gcash_probe_error(
            "missing_access_token", retryable=False, status="no_at",
            label="No access token", checked_at=checked_at,
        )
    timeout = max(5, min(int(timeout or 30), 60))
    stable_device_id = str(device_id or "").strip() or str(uuid.uuid4())
    if session_factory is None:
        from http_client import create_http_session

        session_factory = create_http_session

    session = None
    stage = "session"
    try:
        session = session_factory(proxy=str(proxy or "").strip() or None, impersonate="chrome110")
        checkout_route = "/backend-api/payments/checkout"
        stage = "checkout"
        checkout = _post_json(
            session,
            f"{CHATGPT_PAYMENTS_BASE}/checkout",
            {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "PH", "currency": "PHP"},
                "checkout_ui_mode": "custom",
                "promo_campaign": {
                    "promo_campaign_id": PROMOTION_ID,
                    "is_coupon_from_query_param": False,
                },
                "check_card_proxy": True,
            },
            _chatgpt_headers(
                token, account_id=account_id, device_id=stable_device_id,
                cookie_header=cookie_header, route=checkout_route,
            ),
            timeout,
            stage,
        )
        checkout_session_id, processor = _checkout_session(checkout)

        stage = "promotion_update"
        update_route = "/backend-api/payments/checkout/update"
        update = _post_json(
            session,
            f"{CHATGPT_PAYMENTS_BASE}/checkout/update",
            {
                "checkout_session_id": checkout_session_id,
                "processor_entity": processor,
                "plan_name": "chatgptplusplan",
                "price_interval": "month",
                "seat_quantity": 1,
                "promo_campaign": {
                    "promo_campaign_id": PROMOTION_ID,
                    "is_coupon_from_query_param": False,
                },
            },
            _chatgpt_headers(
                token, account_id=account_id, device_id=stable_device_id,
                cookie_header=cookie_header, route=update_route,
            ),
            timeout,
            stage,
        )

        stage = "taxes"
        taxes_route = "/backend-api/payments/checkout/taxes"
        try:
            taxes = _post_json(
                session,
                f"{CHATGPT_PAYMENTS_BASE}/checkout/taxes",
                {
                    "checkout_session_id": checkout_session_id,
                    "processor_entity": processor,
                    "checkout_email": "eligibility-probe@example.invalid",
                    "billing_country": "PH",
                    "billing_name": "Eligibility Probe",
                    "currency": "PHP",
                    "tax_id": None,
                    "billing_address": {
                        "country": "PH", "line1": "Ayala Avenue", "line2": "",
                        "city": "Makati", "state": "Metro Manila", "postal_code": "1226",
                    },
                },
                _chatgpt_headers(
                    token, account_id=account_id, device_id=stable_device_id,
                    cookie_header=cookie_header, route=taxes_route,
                ),
                timeout,
                stage,
            )
        except _ProbeFailure:
            taxes = {}

        stage = "resolve"
        resolve_route = f"/backend-api/payments/checkout/{processor}/{checkout_session_id}"
        resolved = _get_json(
            session,
            f"{CHATGPT_PAYMENTS_BASE}/checkout/{processor}/{checkout_session_id}",
            headers=_chatgpt_headers(
                token, account_id=account_id, device_id=stable_device_id,
                cookie_header=cookie_header, route=resolve_route,
            ),
            timeout=timeout,
            stage=stage,
        )

        payloads: list[Mapping[str, Any]] = [checkout, update, taxes, resolved]
        discovered = _evidence(payloads)["custom_method_ids"]
        stripe_params = _stripe_params(checkout, discovered)
        if stripe_params is not None:
            stage = "stripe_capability"
            publishable_key = str(stripe_params["key"])
            try:
                stripe = _get_json(
                    session,
                    STRIPE_ELEMENTS_URL,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {publishable_key}",
                        "Origin": "https://js.stripe.com",
                        "Referer": "https://js.stripe.com/",
                        "User-Agent": _USER_AGENT,
                    },
                    params=stripe_params,
                    timeout=timeout,
                    stage=stage,
                )
                payloads.append(stripe)
            except _ProbeFailure:
                pass
        return classify_gcash_evidence(
            payloads, require_zero=require_zero, checked_at=checked_at,
        )
    except _ProbeFailure as exc:
        status = "token_invalid" if exc.code.endswith(("http_401", "http_403")) else "unknown"
        label = "Access token invalid" if status == "token_invalid" else "GCash status unknown"
        return gcash_probe_error(
            exc.code, retryable=exc.retryable, status=status,
            label=label, checked_at=checked_at,
        )
    except Exception:
        return gcash_probe_error(
            f"{stage}_unexpected_error", retryable=True, checked_at=checked_at,
        )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


__all__ = [
    "classify_gcash_evidence",
    "gcash_probe_error",
    "probe_gcash",
]
