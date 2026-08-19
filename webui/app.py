"""FastAPI application: routes and SSE log streaming.

Start with:
    python -m webui.app
or:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, export_formats, registrar  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from .exporter import _decode_jwt_payload, _get_auth  # noqa: E402
from eligibility import (  # noqa: E402
    parse_plus_eligibility,
    plus_probe_error,
    redact_sensitive_text,
)
from gcash_probe import gcash_probe_error, probe_gcash  # noqa: E402
from mail_providers import (  # noqa: E402
    ImportValidationError,
    MailProviderError,
    create_mail_provider,
    get_provider_class,
    list_pooled_providers,
    list_providers,
)

# Release accounts left in `in_use` by a crashed or forcibly stopped process.
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(
            f"[startup] Released {_released} stale account(s) from in_use"
        )
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] Failed to release stale accounts: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# sms-activate-compatible country IDs. The response keeps the legacy `name_cn`
# key because the current frontend consumes it, but all displayed values are English.
_SMS_COUNTRY_NAMES_EN = dict(enumerate((
    "Russia", "Ukraine", "Kazakhstan", "China", "Philippines", "Myanmar",
    "Indonesia", "Malaysia", "Kenya", "Tanzania", "Vietnam", "Kyrgyzstan",
    "United States (virtual)", "Israel", "Hong Kong", "Poland", "United Kingdom",
    "Madagascar", "Republic of the Congo", "Nigeria", "Macau", "Egypt", "India",
    "Ireland", "Cambodia", "Laos", "Haiti", "Ivory Coast", "Gambia", "Serbia",
    "Yemen", "South Africa", "Romania", "Colombia", "Estonia", "Azerbaijan",
    "Canada", "Morocco", "Ghana", "Argentina", "Uzbekistan", "Cameroon", "Chad",
    "Germany", "Lithuania", "Croatia", "Sweden", "Iraq", "Netherlands", "Latvia",
    "Austria", "Belarus", "Thailand", "Saudi Arabia", "Mexico", "Taiwan", "Spain",
    "Iran", "Algeria", "Slovenia", "Bangladesh", "Senegal", "Turkey",
    "Czech Republic", "Sri Lanka", "Peru", "Pakistan", "New Zealand", "Guinea",
    "Mali", "Venezuela", "Ethiopia", "Mongolia", "Brazil", "Afghanistan", "Uganda",
    "Angola", "Cyprus", "France", "Papua New Guinea", "Mozambique", "Nepal",
    "Belgium", "Bulgaria", "Hungary", "Moldova", "Italy", "Paraguay", "Honduras",
    "Tunisia", "Nicaragua", "East Timor", "Bolivia", "Costa Rica", "Guatemala",
    "United Arab Emirates", "Zimbabwe", "Puerto Rico", "Sudan", "Togo", "Kuwait",
    "El Salvador", "Libya", "Jamaica", "Trinidad and Tobago", "Ecuador", "Eswatini",
    "Oman", "Bosnia and Herzegovina", "Dominican Republic", "Syria", "Qatar",
    "Panama", "Cuba", "Mauritania", "Sierra Leone", "Jordan", "Portugal", "Barbados",
    "Burundi", "Benin", "Brunei", "Bahamas", "Botswana", "Belize",
    "Central African Republic", "Dominica", "Grenada", "Georgia", "Greece",
    "Guinea-Bissau", "Guyana", "Iceland", "Comoros", "Liberia", "Lesotho", "Malawi",
    "Namibia", "Niger", "Rwanda", "Slovakia", "Suriname", "Tajikistan", "Monaco",
    "Bahrain", "Reunion", "Zambia", "Armenia", "Somalia", "DR Congo", "Chile",
    "Burkina Faso", "Lebanon", "Gabon", "Albania", "Uruguay", "Mauritius", "Bhutan",
    "Maldives", "Guadeloupe", "Turkmenistan", "French Guiana", "Finland",
    "Saint Lucia", "Luxembourg", "Saint Vincent and the Grenadines",
    "Equatorial Guinea", "Djibouti", "Antigua and Barbuda", "Cayman Islands",
    "Montenegro", "Denmark", "Switzerland", "Norway", "Australia", "Eritrea",
    "South Sudan", "Sao Tome and Principe", "Aruba", "Montserrat", "Anguilla",
    "North Macedonia", "Seychelles", "New Caledonia", "Cape Verde",
    "United States (physical)", "Palestine", "United States", "China", "South Korea",
    "Ivory Coast", "Japan",
)))


def _sms_country_name(country_id: str) -> str:
    try:
        return _SMS_COUNTRY_NAMES_EN[int(country_id)]
    except (KeyError, TypeError, ValueError):
        return f"Country {country_id}" if country_id else "Unknown"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# -------------------------------- Pydantic models --------------------------------


class ImportReq(BaseModel):
    text: str = Field(..., description="One account per line; the format depends on kind")
    kind: str = Field(
        "",
        description="Email source (outlook / ...). If blank, infer it from the field "
                    "count. Outlook and Gmail both use four fields, so clients should "
                    "provide this value explicitly.",
    )


class RegisterReq(BaseModel):
    email: Optional[str] = Field(
        None, description="Leave blank to claim the next available account automatically"
    )
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True
    # The frontend enables post-registration TOTP enrollment by default.
    # Keep the API default False for old cached clients and direct callers:
    # omitting the field must not silently authorize an irreversible enrollment.
    # The frontend form stores (`want2fa` / `autoWant2fa`) own the visible default.
    want_2fa: bool = False


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    """Import an account batch atomically.

    If any line is invalid, reject the entire batch with HTTP 422 and include
    the line number and reason for every validation error:

        {"ok": false, "message": "...", "errors": [{"line": 3, "error": "..."}]}
    """
    try:
        result = db.import_accounts(req.text, kind=req.kind)
    except ImportValidationError as e:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "message": str(e), "errors": e.errors},
        )
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0, kind: str = ""):
    items = db.list_accounts(status=status, limit=limit, offset=offset, kind=kind)
    total = db.count_accounts(status=status, kind=kind)
    return {
        "ok": True,
        "items": items,
        "total": total,
        "by_kind": db.stats_by_kind(),
    }


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="Delete the listed email addresses")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """Delete pooled accounts by status or email list; status takes precedence."""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "Either status or emails is required")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """Reset one account from done or failed to available."""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"Email address {email} does not exist")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """Reset multiple accounts from done or failed to available."""
    if not req.emails:
        raise HTTPException(400, "emails must not be empty")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ------------------------------- Proxy connectivity -------------------------------


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="Proxies to test")
    timeout: int = Field(8, description="Timeout per proxy in seconds")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="Target URL (returns the exit IP by default)")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """Test proxies concurrently using the registration HTTP client.

    This applies the same SOCKS5 normalization and environment isolation as a
    real registration request. A bare `ip:port` is treated as an HTTP proxy;
    use an explicit `socks5://` scheme for SOCKS5.
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Failed to load http_client: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies must not be empty")

    def _test_one(proxy: str):
        t0 = _t.perf_counter()
        try:
            sess = create_http_session(proxy=proxy)
            resp = sess.get(test_url, timeout=timeout)
            latency = int((_t.perf_counter() - t0) * 1000)
            if resp.status_code != 200:
                return {"ok": False, "latency_ms": latency, "error": f"HTTP {resp.status_code}"}
            ip = ""
            try:
                ip = resp.json().get("ip", "")
            except Exception:
                ip = (resp.text or "").strip()[:64]
            return {"ok": True, "latency_ms": latency, "ip": ip}
        except Exception as e:  # noqa: BLE001
            latency = int((_t.perf_counter() - t0) * 1000)
            return {"ok": False, "latency_ms": latency, "error": str(e)[:140]}

    results = {}
    with ThreadPoolExecutor(max_workers=min(20, len(proxies))) as ex:
        for proxy, res in zip(proxies, ex.map(_test_one, proxies)):
            results[proxy] = res
    return {"ok": True, "results": results}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """Start a registration task and return the run ID used by the SSE stream."""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # The provider's `pooled` capability determines whether an account is claimed,
    # avoiding hard-coded special cases for non-pooled email providers.
    if not provider_cls.pooled:
        # Non-pooled providers create an address; a placeholder carries later setup.
        import time as _t
        account = {
            "email": f"{mail_source}_placeholder_{int(_t.time())}@placeholder.local",
            "password": "",
            "client_id": "",
            "refresh_token": "",
            "relay_url": "",
            "kind": mail_source,
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(
                400,
                f"Email address {req.email} is unavailable "
                "(missing, already in use, or completed)",
            )
        if (account.get("kind") or "outlook") != mail_source:
            # A requested pooled account must match the active source; otherwise
            # credentials for one provider could initialize another.
            db.release_unused(account["email"])
            raise HTTPException(
                400,
                f"{req.email} is a {account.get('kind')} account, but the current "
                f"email source is {mail_source}. Switch the source first.",
            )
    else:
        account = db.claim_next(kind=mail_source)
        if not account:
            raise HTTPException(
                400,
                f"No available {provider_cls.display_name} accounts are in the pool. "
                "Import accounts first.",
            )

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "want_2fa": req.want_2fa,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """Stream task logs and events over SSE."""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # Read from the blocking queue without blocking the event loop.
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # Sentinel: task finished.
                    yield "event: end\ndata: {}\n\n"
                    break
                if msg.startswith("__EVENT__:"):
                    yield f"event: status\ndata: {msg[len('__EVENT__:'):]}\n\n"
                else:
                    yield f"event: log\ndata: {json.dumps({'line': msg}, ensure_ascii=False)}\n\n"
        finally:
            registrar.remove_run_queue(run_id)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering.
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # Heartbeat: let the SSE loop check for disconnection.


@app.get("/api/runs")
def api_runs(limit: int = 50):
    return {"ok": True, "items": db.list_runs(limit=limit)}


@app.get("/api/registered")
def api_registered(limit: int = 20, offset: int = 0, filter: str = "all"):
    items = db.list_registered(limit=limit, offset=offset, filter_rt=filter)
    total = db.count_registered(filter_rt=filter)
    return {"ok": True, "items": items, "total": total}


@app.get("/api/registered/{email}")
def api_registered_one(email: str):
    row = db.get_registered(email)
    if not row:
        raise HTTPException(404, "not found")
    return {"ok": True, "data": row}


@app.delete("/api/registered/{email}")
def api_delete_registered(email: str):
    ok = db.delete_registered(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteRegisteredReq(BaseModel):
    emails: Optional[list[str]] = Field(
        None,
        description="Delete the listed email addresses; omit them and set all=true to delete all",
    )
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "Either emails or all=true is required")


# ------------------------------- Batch text export -------------------------------
# Route order is safe: `formats` has four segments and cannot be consumed by the
# three-segment GET route; `export` is POST while the email routes are GET/DELETE.
# Add new formats only in webui/export_formats.py.


@app.get("/api/registered/export/formats")
def api_export_formats():
    """List the formats available to the registered-account export UI."""
    return {"ok": True, "formats": export_formats.list_formats()}


class ExportRegisteredReq(BaseModel):
    format: str = Field(
        ..., description="Format ID from GET /api/registered/export/formats"
    )
    emails: Optional[list[str]] = Field(None, description="Email addresses to export")
    all: bool = Field(False, description="Export every page and ignore emails")


@app.post("/api/registered/export")
def api_export_registered(req: ExportRegisteredReq):
    fmt = export_formats.get_format(req.format)
    if fmt is None:
        raise HTTPException(400, f"Unknown export format: {req.format}")

    if req.all:
        rows = db.list_registered_full(limit=100000)
    elif req.emails:
        rows = db.list_registered_by_emails(req.emails)
    else:
        raise HTTPException(400, "Either emails or all=true is required")

    # Preserve one output row/file per selection, even when fields are empty.
    # Manual export neither refreshes nor requires refresh_token, unlike auto-push.
    base = {
        "ok": True,
        "count": len(rows),
        "filename": fmt.filename,
        "label": fmt.label,
        "mode": fmt.mode,
        "mime": fmt.mime,
        # Return the exact exported emails so "download and delete" removes only
        # this batch. With all=true, the frontend only holds the current page; a
        # broad status/all deletion could remove accounts that have never been run.
        "emails": [(r.get("email") or "") for r in rows],
    }

    if fmt.mode == "download":
        # Binary downloads use base64 so the frontend can save without a preview.
        blob = export_formats.render_bytes(rows, fmt)
        return {**base, "b64": base64.b64encode(blob).decode("ascii"), "size": len(blob)}

    return {**base, "text": export_formats.render_text(rows, fmt)}


# ---------------------------- Email source configuration ----------------------------


@app.get("/api/mail/providers")
def api_mail_providers(pooled_only: bool = False):
    """List registered email providers, capabilities, and configuration fields.

    Set pooled_only=true to return only providers that support account-pool imports.
    """
    return {
        "ok": True,
        "providers": list_pooled_providers() if pooled_only else list_providers(),
        "current": db.get_setting("mail_source", "outlook"),
    }


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    """Accept provider-defined email configuration fields.

    Fields other than mail_source are declared by each provider and passed
    through to the persistence layer.
    """

    model_config = {"extra": "allow"}

    mail_source: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    try:
        db.save_mail_config(req.model_dump(exclude_none=True))
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """Run the current email provider's connectivity self-test."""
    mail_source = db.get_setting("mail_source", "outlook")
    try:
        provider_cls = get_provider_class(mail_source)
    except MailProviderError as e:
        raise HTTPException(400, str(e))

    # A pooled provider's connectivity depends on a specific account. Validate its
    # format during import and exercise it through an actual registration instead.
    if provider_cls.pooled:
        raise HTTPException(
            400,
            f"{provider_cls.display_name} uses an account pool and does not need "
            "a separate connectivity test. Its format is validated during import.",
        )

    try:
        provider = create_mail_provider(mail_source, db.get_mail_settings())
    except MailProviderError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"Failed to initialize {provider_cls.display_name}: {e}")

    try:
        result = provider.self_test()
    except Exception as e:
        raise HTTPException(500, f"Connection failed: {e}")
    if not result.get("ok"):
        raise HTTPException(500, result.get("message") or "Connection failed")
    return {"ok": True, "message": result.get("message", "Connection successful")}


# ----------------------------- SMS verification settings -----------------------------


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # Pass '***' to keep the current value.
    sms_country: Optional[str] = None              # ID or country code ('52' / 'th').
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_fixed_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # Comma-separated IDs allowed for auto-selection.
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # Blank uses the provider default; >0 overrides it.
    sms_per_phone_timeout: Optional[str] = None    # Wait per phone number in seconds (default 80).


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """Test SMS provider connectivity by querying the balance."""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "sms_api_key is not configured")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": cfg["sms_provider"],
            "balance": balance,
            "message": f"Connection successful. Balance: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"Connection failed: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """Return countries ranked by price and inventory for the current SMS provider."""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "sms_api_key is not configured")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = _sms_country_name(cid)
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"Query failed: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """Return live country inventory, falling back to the static provider list."""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # Try the provider API for countries with live inventory.
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": _sms_country_name(cid),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # Fall back to the static dictionary.
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {
            "id": cid,
            "name_cn": _sms_country_name(cid),
            "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
        }
        for cid, _name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ------------------------------- Automatic export -------------------------------


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # Pass '***' to keep the current value.
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # Pass '***' to keep the current value.
    sub2api_group_ids: Optional[str] = None  # Comma-separated, e.g. "2" or "1,2,3".
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa or sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """Test CPA or SUB2API connectivity."""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"Unknown target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Test failed: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="Registered account email address to export")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="Export destinations: cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """Export one registered account to selected external panels.

    This explicit request runs even when automatic export is disabled, provided
    the selected panel has its URL and credentials configured.
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"Registered account not found: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # Explicit manual request: force this destination on.
        try:
            out["cpa"] = exporter.export_to_cpa(cred, cpa_cfg)
        except Exception as e:
            out["cpa"] = {"ok": False, "error": str(e)}
    if "sub2api" in targets:
        sub2api_cfg = dict(cfg["sub2api"])
        sub2api_cfg["enabled"] = True
        try:
            out["sub2api"] = exporter.export_to_sub2api(cred, sub2api_cfg)
        except Exception as e:
            out["sub2api"] = {"ok": False, "error": str(e)}

    return {"ok": True, **out}


class UpdateCredReq(BaseModel):
    email: str = Field(..., description="Registered account email address to update")
    # None keeps a field unchanged; an empty string explicitly clears it.
    password: Optional[str] = Field(None, description="New password; omit to keep the current value")
    totp_secret: Optional[str] = Field(
        None, description="New TOTP secret; omit to keep the current value"
    )


@app.post("/api/registered/update_credentials")
def api_update_credentials(req: UpdateCredReq):
    """Update a registered account's locally stored password or TOTP secret.

    This only changes the local database; it does not change OpenAI credentials.
    TOTP secrets are validated as base32 before storage.
    """
    email = (req.email or "").strip().lower()
    if not email:
        raise HTTPException(400, "email must not be empty")
    if req.password is None and req.totp_secret is None:
        raise HTTPException(400, "No fields were provided to update")
    try:
        ok = db.update_registered_manual(
            email, password=req.password, totp_secret=req.totp_secret
        )
    except ValueError as e:
        # Return the specific validation error so the user can correct the field.
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"Registered account not found: {email}")

    changed = [n for n, v in (("password", req.password), ("TOTP secret", req.totp_secret))
               if v is not None]
    logger.info(
        f"[registered] Manually updated credentials for email={email}; "
        f"fields={'+'.join(changed)}"
    )
    return {"ok": True, "email": email, "changed": changed}


# ------------------------------ Plus trial check ------------------------------

EligibilityEmail = Annotated[str, Field(max_length=320)]


class CheckPlusReq(BaseModel):
    emails: list[EligibilityEmail] = Field(
        ..., min_length=1, max_length=50, description="Registered accounts to check"
    )
    proxy: str = Field(
        "", max_length=2048, description="Proxy used for the check; blank means direct"
    )


class CheckGCashReq(BaseModel):
    emails: list[EligibilityEmail] = Field(
        ..., min_length=1, max_length=50, description="Registered accounts to probe"
    )
    proxy: str = Field(
        "", max_length=2048, description="Proxy used for every probe stage"
    )


_GCASH_CONFIRMATION_HEADER = "x-gcash-probe-confirmation"
_GCASH_CONFIRMATION_VALUE = "checkout-side-effects-acknowledged"


def _require_local_confirmed_gcash_request(request: Request) -> None:
    client_host = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    try:
        is_loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        is_loopback = client_host.lower() == "localhost"
    configured_token = os.getenv("GPT_AUTO_REGISTER_ADMIN_TOKEN", "").strip()
    supplied_token = request.headers.get("x-gpt-admin-token", "")
    authenticated_proxy = bool(configured_token) and secrets.compare_digest(
        supplied_token, configured_token
    )
    if not is_loopback and not authenticated_proxy:
        raise HTTPException(
            403,
            "GCash probing requires a loopback client or a valid reverse-proxy admin token",
        )
    if request.headers.get(_GCASH_CONFIRMATION_HEADER) != _GCASH_CONFIRMATION_VALUE:
        raise HTTPException(
            403,
            "Explicit acknowledgement of checkout side effects is required",
        )


# OpenAI uses several phrases for deactivation in 401/403 response bodies.
# Match lowercase substrings here and log unmatched body metadata for extension.
_DEACTIVATED_MARKERS = (
    "account_deactivated",
    "accountdeactivated",
    "deactivated",
    "has been deactivated",
    "disabled",
    "suspended",
    "banned",
    "violat",          # violating / violation of our policies
    "potential abuse",
    "terminated",
)


def _body_text(resp) -> str:
    """Read response text without allowing errors to stop the probe loop."""
    try:
        return (resp.text or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _looks_deactivated(body: str) -> bool:
    return any(m in body.lower() for m in _DEACTIVATED_MARKERS)


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """Check Plus trial eligibility using each account's access token."""
    from http_client import create_http_session

    emails = list(dict.fromkeys(
        str(email or "").strip().lower() for email in req.emails
        if str(email or "").strip()
    ))
    if not emails:
        raise HTTPException(400, "At least one email is required")
    if len(emails) > 50:
        raise HTTPException(400, "A maximum of 50 accounts can be checked at once")

    log = logging.getLogger("webui")
    url = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )

    # Reuse the registration HTTP client. It converts socks5:// to socks5h:// so
    # DNS resolves through the proxy, and uses trust_env=False so an explicitly
    # blank proxy is a true direct connection rather than an environment override.
    proxy = req.proxy.strip()
    if len(proxy) > 2048:
        raise HTTPException(400, "Proxy URL is too long")
    try:
        sess = create_http_session(proxy=proxy or None, impersonate="chrome110")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Failed to create HTTP session: {type(e).__name__}")

    note = ""

    def _check(access_token: str, account_id: str = "", device_id: str = ""):
        """Send one eligibility request.

        Never fall back silently to a direct connection after a proxy error. A
        stale or rejected proxy must not expose the user's real IP. Retry once
        through the same route instead.

        Include browser-equivalent Origin, Referer, account, and device headers.
        Decode account_id from the access-token JWT without another request.
        """
        nonlocal note
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": ua,
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
        }
        if account_id:
            headers["ChatGPT-Account-ID"] = account_id
        if device_id:
            headers["OAI-Device-Id"] = device_id
        try:
            return sess.get(url, headers=headers, timeout=15, allow_redirects=False)
        except Exception as e:  # noqa: BLE001
            if proxy and not note:
                # Preserve curl codes: 97 means SOCKS5 authentication rejection;
                # 7 means unreachable. They distinguish credentials from outages.
                msg = str(e)
                if "(97)" in msg or "rejected by the SOCKS5" in msg:
                    note = "Proxy authentication was rejected (SOCKS5 error 97)"
                elif "(7)" in msg:
                    note = "The proxy is unreachable (curl error 7)"
                else:
                    note = f"The proxy request failed ({type(e).__name__}); direct fallback was not used"
                log.warning("[check_plus] %s: %s", note, redact_sensitive_text(msg, limit=140))
            raise

    results = {}
    for email in emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = plus_probe_error(
                "account_not_found", retryable=False, status="not_found", label="Not found"
            )
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = plus_probe_error(
                "missing_access_token", retryable=False, status="no_at", label="No access token"
            )
            continue
        # Decode account_id from the access-token JWT without another request.
        auth_claims = _get_auth(_decode_jwt_payload(at))
        account_id = str(
            auth_claims.get("chatgpt_account_id") or auth_claims.get("account_id") or ""
        ).strip()
        # Derive a stable UUID when device_id was not persisted so repeat checks
        # resemble a consistent client rather than a new random device each time.
        device_id = (cred.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"dango-check-plus:{email}")
        )
        try:
            resp = _check(at, account_id, device_id)
        except Exception as e:  # noqa: BLE001
            results[email] = plus_probe_error(
                "network_error", retryable=True, status="error", label="Network error"
            )
            log.warning(
                "[check_plus] request failed for %s: %s",
                email,
                redact_sensitive_text(e, limit=140),
            )
            continue
        if resp.status_code in (401, 403):
            # Inspect 401/403 bodies: a deactivated account can revoke an otherwise
            # unexpired token, so status alone cannot distinguish deactivation from
            # ordinary credential expiration or rotation.
            body = _body_text(resp)
            if _looks_deactivated(body):
                results[email] = plus_probe_error(
                    "account_deactivated", retryable=False,
                    status="banned", label="Account deactivated",
                )
                results[email].update({"classification": "ineligible", "eligible": False, "conclusive": True})
                log.info("[check_plus] account deactivated for %s (HTTP %s)", email, resp.status_code)
                continue
            if resp.status_code == 401:
                results[email] = plus_probe_error(
                    "token_invalid", retryable=False,
                    status="token_invalid", label="Access token invalid",
                )
                # Log only metadata; response bodies may contain sensitive data.
                log.info("[check_plus] unauthorized response for %s (body length=%s)", email, len(body))
                continue
            results[email] = plus_probe_error(
                f"http_{resp.status_code}", retryable=False,
                status="error", label=f"HTTP {resp.status_code}",
            )
            log.info("[check_plus] forbidden response for %s (body length=%s)", email, len(body))
            continue
        if resp.status_code != 200:
            results[email] = plus_probe_error(
                f"http_{resp.status_code}", retryable=resp.status_code >= 500,
                status="error", label=f"HTTP {resp.status_code}",
            )
            continue
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            results[email] = plus_probe_error(
                "invalid_json", retryable=True, status="error", label="Invalid response"
            )
            continue
        results[email] = parse_plus_eligibility(data, account_id=account_id)

    try:
        sess.close()
    except Exception:  # noqa: BLE001
        pass

    checked_at = time.time()
    for email, info in results.items():
        # Do not persist not_found/no_at/error: they indicate that no conclusion was
        # reached. Persisting them would hide the account from the unchecked filter.
        #
        # Persist token_invalid so an old plus_eligible result is not left visible
        # after token revocation. It is a meaningful terminal observation here.
        if info["status"] not in ("not_found", "no_at", "error"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results, "note": note}


@app.post("/api/registered/check_gcash")
def api_check_gcash(req: CheckGCashReq, request: Request):
    """Explicitly probe GCash eligibility without confirming or starting payment."""
    _require_local_confirmed_gcash_request(request)
    emails = list(dict.fromkeys(
        str(email or "").strip().lower() for email in req.emails
        if str(email or "").strip()
    ))
    if not emails:
        raise HTTPException(400, "At least one email is required")
    if len(emails) > 50:
        raise HTTPException(400, "A maximum of 50 accounts can be checked at once")
    proxy = str(req.proxy or "").strip()
    if len(proxy) > 2048:
        raise HTTPException(400, "Proxy URL is too long")

    results: dict[str, dict] = {}
    summary = {"eligible": 0, "ineligible": 0, "unknown": 0}
    for email in emails:
        credential = db.get_registered(email)
        if not credential:
            result = gcash_probe_error(
                "account_not_found", retryable=False,
                status="not_found", label="Not found",
            )
            results[email] = result
            summary["unknown"] += 1
            continue
        access_token = str(credential.get("access_token") or "").strip()
        if not access_token:
            result = gcash_probe_error(
                "missing_access_token", retryable=False,
                status="no_at", label="No access token",
            )
            results[email] = result
            summary["unknown"] += 1
            continue

        claims = _get_auth(_decode_jwt_payload(access_token))
        account_id = str(
            claims.get("chatgpt_account_id") or claims.get("account_id") or ""
        ).strip()
        device_id = str(credential.get("device_id") or "").strip() or str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"gpt-auto-register-gcash:{email}")
        )
        try:
            result = probe_gcash(
                access_token=access_token,
                account_id=account_id,
                device_id=device_id,
                cookie_header=str(credential.get("cookie_header") or ""),
                proxy=proxy,
            )
        except Exception:
            logger.warning("[gcash_check] unexpected probe failure for %s", email)
            result = gcash_probe_error(
                "probe_unexpected_error", retryable=True,
                status="unknown", label="GCash status unknown",
            )
        results[email] = result
        db.update_eligibility_check(email, "gcash_check", result)
        classification = str(result.get("classification") or "unknown")
        summary[classification if classification in summary else "unknown"] += 1

    return {"ok": True, "results": results, "summary": summary}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """Options forwarded to every registration task started by auto-loop."""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # Single proxy for one worker without a pool.
    proxy_pool: str = ""         # One proxy per line; takes precedence over proxy.
    concurrency: int = 1         # Worker count (1-20).
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # Delay after each worker run to reduce risk.
    target_count: int = 0        # Successful target; 0 means unlimited.
    # The batch UI enables 2FA by default, but the API default remains False for
    # old cached clients and direct callers. AutoLoop.vue owns the visible default.
    want_2fa: bool = False


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Failed to start"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Failed to pause"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Failed to resume"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "Failed to stop"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """Stream auto-loop state, run_started, and run_finished events over SSE."""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # Block for messages while emitting a heartbeat every 30 seconds.
                try:
                    msg = await loop.run_in_executor(None, lambda: q.get(timeout=30))
                except Exception:
                    yield ": heartbeat\n\n"
                    continue
                if msg is None:
                    break
                kind = msg.get("kind", "state")
                data = msg.get("data", {})
                yield f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        finally:
            AUTO_LOOP.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# -------------------------------- Static assets --------------------------------


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)
