"""Structured, side-effect-free account promotion eligibility parsing.

The module intentionally contains no network code. Callers supply the decoded
``accounts/check`` response and receive a small, persistence-safe result.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any


PLUS_TRIAL_CAMPAIGN_ID = "plus-1-month-free"


def redact_sensitive_text(value: Any, *, limit: int = 300) -> str:
    """Redact common authorization, cookie, and proxy credential forms."""
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    text = re.sub(
        r"(?i)((?:https?|socks5h?)://)[^/@\s:]+:[^/@\s]+@",
        r"\1[REDACTED]@",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(\bcookie\s*:\s*)[^\r\n]+", r"\1[REDACTED]", text)
    return text[: max(0, int(limit))]


def _timestamp(value: float | None) -> float:
    return float(time.time() if value is None else value)


def _base_result(*, checked_at: float | None = None) -> dict[str, Any]:
    return {
        "operation": "plus_trial_eligibility",
        "classification": "unknown",
        "eligible": None,
        "decision": "response_unknown",
        "conclusive": False,
        "retryable": True,
        "status": "error",
        "label": "Check failed",
        "checked_at": _timestamp(checked_at),
    }


def plus_probe_error(
    decision: str,
    *,
    retryable: bool,
    status: str = "error",
    label: str = "Check failed",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Return a stable failure contract without carrying upstream error text."""
    result = _base_result(checked_at=checked_at)
    result.update({
        "decision": str(decision or "probe_failed"),
        "retryable": bool(retryable),
        "status": str(status or "error"),
        "label": str(label or "Check failed"),
    })
    return result


def _select_account(accounts: Mapping[str, Any], account_id: str) -> Mapping[str, Any] | None:
    if account_id:
        return accounts.get(account_id) if isinstance(accounts.get(account_id), Mapping) else None
    if isinstance(accounts.get("default"), Mapping):
        return accounts["default"]
    for key, value in accounts.items():
        if key != "default" and isinstance(value, Mapping):
            return value
    return None


def _campaign_details(item: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    campaigns = item.get("eligible_promo_campaigns")
    if not isinstance(campaigns, Mapping):
        return "", {}
    plus = campaigns.get("plus")
    if not isinstance(plus, Mapping):
        return "", {}
    metadata = plus.get("metadata")
    return str(plus.get("id") or "").strip(), metadata if isinstance(metadata, Mapping) else {}


def parse_plus_eligibility(
    payload: Any,
    *,
    account_id: str = "",
    checked_at: float | None = None,
) -> dict[str, Any]:
    """Classify exact ``plus-1-month-free`` eligibility from accounts/check."""
    result = _base_result(checked_at=checked_at)
    if not isinstance(payload, Mapping):
        result["decision"] = "response_not_object"
        return result
    accounts = payload.get("accounts")
    if not isinstance(accounts, Mapping) or not accounts:
        result["decision"] = "accounts_missing"
        return result
    item = _select_account(accounts, str(account_id or "").strip())
    if item is None:
        result["decision"] = "account_entry_missing"
        return result

    account = item.get("account") if isinstance(item.get("account"), Mapping) else {}
    entitlement = item.get("entitlement") if isinstance(item.get("entitlement"), Mapping) else {}
    campaign_id, metadata = _campaign_details(item)
    discount = metadata.get("discount") if isinstance(metadata.get("discount"), Mapping) else {}
    duration = metadata.get("duration") if isinstance(metadata.get("duration"), Mapping) else {}

    plan_type = str(account.get("plan_type") or "").strip()
    subscription_plan = str(entitlement.get("subscription_plan") or "").strip()
    active = bool(entitlement.get("has_active_subscription"))
    deactivated = bool(account.get("is_deactivated"))
    free_plan = plan_type.lower() == "free" or subscription_plan.lower() == "chatgptfreeplan"

    result.update({
        "current_plan_type": plan_type,
        "subscription_plan": subscription_plan,
        "has_active_subscription": active,
        "campaign_id": campaign_id,
        "campaign_title": str(metadata.get("title") or "").strip(),
        "discount_percentage": discount.get("percentage"),
        "duration_periods": duration.get("num_periods"),
        "duration_unit": str(duration.get("period") or "").strip(),
    })

    if deactivated:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "account_deactivated",
            "conclusive": True,
            "retryable": False,
            "status": "banned",
            "label": "Account deactivated",
        })
    elif not plan_type and not subscription_plan:
        result.update({
            "decision": "plan_evidence_missing",
            "status": "unknown",
            "label": "Plan status unknown",
        })
    elif active or (plan_type and plan_type.lower() != "free" and "plus" in plan_type.lower()):
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "active_subscription",
            "conclusive": True,
            "retryable": False,
            "status": "plus_active",
            "label": "Plus active",
        })
    elif free_plan and campaign_id == PLUS_TRIAL_CAMPAIGN_ID:
        result.update({
            "classification": "eligible",
            "eligible": True,
            "decision": "plus_1_month_free_available",
            "conclusive": True,
            "retryable": False,
            "status": "plus_eligible",
            "label": "Plus trial eligible",
        })
    else:
        result.update({
            "classification": "ineligible",
            "eligible": False,
            "decision": "campaign_not_available",
            "conclusive": True,
            "retryable": False,
            "status": "free",
            "label": "Free - no eligible promotion",
        })
    return result


__all__ = [
    "PLUS_TRIAL_CAMPAIGN_ID",
    "parse_plus_eligibility",
    "plus_probe_error",
    "redact_sensitive_text",
]
