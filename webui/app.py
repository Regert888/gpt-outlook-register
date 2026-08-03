"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, registrar  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402

# 启动时自动释放卡死的 in_use 号（上次进程崩溃 / 强退留下的）
try:
    _released = db.release_stale_in_use(stale_seconds=1800)
    if _released > 0:
        logging.getLogger("webui").info(f"[startup] 释放 {_released} 个卡死的 in_use 号")
except Exception as _e:
    logging.getLogger("webui").warning(f"[startup] release_stale 失败: {_e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="多行 4 段格式 (email----password----client_id----refresh_token)")


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    mail_source: Optional[str] = Field(None, description="outlook / cf_temp / icloud_hme；留空使用当前配置")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    otp_timeout: int = 10
    allow_existing_login: bool = True


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    result = db.import_accounts(req.text)
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 50, offset: int = 0):
    items = db.list_accounts(status=status, limit=limit, offset=offset)
    total = db.count_accounts(status=status)
    return {"ok": True, "items": items, "total": total}


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True}


class BulkDeleteReq(BaseModel):
    status: Optional[str] = Field(None, description="available/in_use/done/failed/all")
    emails: Optional[list[str]] = Field(None, description="按 email 列表删")


@app.post("/api/accounts/bulk_delete")
def api_bulk_delete(req: BulkDeleteReq):
    """按状态或 email 列表批量删除号池。两个参数二选一（status 优先）。"""
    if req.status:
        n = db.delete_accounts_by_status(req.status)
        return {"ok": True, "deleted": n, "by": "status", "stats": db.stats()}
    if req.emails:
        n = db.delete_accounts_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email)
    if not ok:
        raise HTTPException(404, f"邮箱 {email} 不存在")
    return {"ok": True, "email": email}


class BulkResetReq(BaseModel):
    emails: list[str]


@app.post("/api/accounts/bulk_reset")
def api_bulk_reset(req: BulkResetReq):
    """批量重置：done / failed → available。"""
    if not req.emails:
        raise HTTPException(400, "emails 不能为空")
    n = db.bulk_reset_to_available(req.emails)
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


# ──────────────────────── 代理连通性测试 ────────────────────────


class ProxyTestReq(BaseModel):
    proxies: list[str] = Field(..., description="要测试的代理列表")
    timeout: int = Field(8, description="每个代理超时秒数")
    test_url: str = Field("https://api.ipify.org?format=json",
                          description="测试目标 URL（默认返回出口 IP）")


@app.post("/api/proxy/test")
def api_proxy_test(req: ProxyTestReq):
    """并发测试代理连通性。复用真实注册流程的 create_http_session（含 socks5->socks5h
    标准化、trust_env=False），保证「测试正常」== 「跑号能用」。返回 ok / 延迟 / 出口 IP。

    协议说明：不写协议的 `ip:port` 被 curl 按 HTTP 代理处理；SOCKS5 需显式写 socks5://。
    """
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    try:
        from http_client import create_http_session
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"加载 http_client 失败: {e}")

    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    timeout = max(1, min(int(req.timeout or 8), 60))
    test_url = (req.test_url or "https://api.ipify.org?format=json").strip()

    proxies = [p.strip() for p in (req.proxies or []) if p and p.strip()]
    if not proxies:
        raise HTTPException(400, "proxies 不能为空")

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
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = (req.mail_source or db.get_setting("mail_source", "outlook")).strip().lower()
    if mail_source not in ("outlook", "cf_temp", "icloud_hme"):
        raise HTTPException(400, f"不支持的 mail_source={mail_source}")
    is_virtual_mail_source = mail_source in ("cf_temp", "icloud_hme")

    if is_virtual_mail_source:
        # CF / iCloud HME 模式：不需要 outlook 号池；
        # iCloud 从邮箱列表点“使用”时保留目标邮箱，由 provider claim 对应账号。
        import time as _t
        prefix = "cf" if mail_source == "cf_temp" else "icloud"
        virtual_email = (req.email or "").strip().lower() if mail_source == "icloud_hme" else ""
        if not virtual_email:
            virtual_email = f"{prefix}_placeholder_{int(_t.time())}@{prefix}.local"
        account = {
            "email": virtual_email,
            "password": "",
            "client_id": "",
            "refresh_token": "",
        }
    elif req.email:
        account = db.claim_account(req.email)
        if not account:
            raise HTTPException(400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)")
    else:
        account = db.claim_next()
        if not account:
            raise HTTPException(400, "号池里没有 available 账号；请先批量导入")

    options = {
        "mail_source": mail_source,
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": req.proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
    }
    run_id = registrar.start_registration(account, options)
    logger.info(f"[run] {run_id} -> {account['email']} (mail_source={mail_source})")
    return {"ok": True, "run_id": run_id, "email": account["email"]}


@app.get("/api/runs/{run_id}/stream")
async def api_stream(run_id: str, request: Request):
    """SSE 实时推送日志 + 事件。"""
    q = registrar.get_run_queue(run_id)
    if q is None:
        raise HTTPException(404, "run_id not found or finished")

    async def event_gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 从队列取消息（用 run_in_executor 避免阻塞 event loop）
                msg = await loop.run_in_executor(None, _safe_get, q)
                if msg is None:
                    # sentinel: 任务结束
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
            "X-Accel-Buffering": "no",  # 避免 nginx 缓冲
            "Connection": "keep-alive",
        },
    )


def _safe_get(q):
    try:
        return q.get(timeout=60)
    except Exception:
        return ""  # 心跳：返空串让 SSE 检查 disconnect


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
    emails: Optional[list[str]] = Field(None, description="按 email 列表删；留空 + all=true 则删全部")
    all: bool = False


@app.post("/api/registered/bulk_delete")
def api_bulk_delete_registered(req: BulkDeleteRegisteredReq):
    if req.all:
        n = db.delete_all_registered()
        return {"ok": True, "deleted": n, "by": "all"}
    if req.emails:
        n = db.delete_registered_by_emails(req.emails)
        return {"ok": True, "deleted": n, "by": "emails"}
    raise HTTPException(400, "需要 emails 或 all=true")


# ──────────────────────── 邮箱来源配置 ────────────────────────


@app.get("/api/settings/mail")
def api_get_mail_config():
    return {"ok": True, "config": db.get_mail_config()}


class SaveMailConfigReq(BaseModel):
    mail_source: Optional[str] = None       # outlook / cf_temp / icloud_hme
    cf_api_url: Optional[str] = None
    cf_admin_token: Optional[str] = None
    cf_domain: Optional[str] = None
    icloud_region: Optional[str] = None
    icloud_cookie: Optional[str] = None
    icloud_maildomain_host: Optional[str] = None
    icloud_label_prefix: Optional[str] = None
    icloud_note: Optional[str] = None
    icloud_auto_generate: Optional[str | bool] = None
    icloud_imap_host: Optional[str] = None
    icloud_imap_port: Optional[str] = None
    icloud_imap_username: Optional[str] = None
    icloud_imap_password: Optional[str] = None
    icloud_imap_folder: Optional[str] = None
    icloud_imap_use_ssl: Optional[str | bool] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    db.save_mail_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail(req: Optional[SaveMailConfigReq] = None):
    """测试当前邮箱来源配置；请求体存在时按表单临时值测试，不落库。"""
    incoming = req.model_dump(exclude_none=True) if req is not None else {}
    mail_source = str(incoming.get("mail_source") or db.get_setting("mail_source", "outlook")).strip().lower()
    if mail_source == "icloud_hme":
        import sys as _sys
        ROOT_DIR = Path(__file__).resolve().parents[1]
        if str(ROOT_DIR) not in _sys.path:
            _sys.path.insert(0, str(ROOT_DIR))
        from mail_icloud import validate_icloud_config
        try:
            cfg = db.get_icloud_config()
            for key in (
                "icloud_region", "icloud_maildomain_host", "icloud_label_prefix",
                "icloud_note", "icloud_auto_generate", "icloud_imap_host",
                "icloud_imap_port", "icloud_imap_username", "icloud_imap_folder",
                "icloud_imap_use_ssl",
            ):
                if key in incoming:
                    cfg[key] = incoming[key]
            for key in ("icloud_cookie", "icloud_imap_password"):
                val = incoming.get(key)
                if val and val != "***":
                    cfg[key] = val
            if not cfg.get("icloud_cookie"):
                raise HTTPException(400, "未配置 icloud_cookie")
            result = validate_icloud_config(cfg)
            account = result.get("account") or {}
            msg = (
                f"iCloud 连接成功: {account.get('apple_id', 'Unknown')} / "
                f"maildomain={account.get('maildomain_host') or 'Default'} / "
                f"{result.get('imap_message')}"
            )
            return {"ok": True, "message": msg, **result}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, f"连接失败: {e}")

    if mail_source != "cf_temp":
        raise HTTPException(400, f"当前 mail_source={mail_source}，不需要测试")

    api_url = str(incoming.get("cf_api_url") if "cf_api_url" in incoming else db.get_setting("cf_api_url", "")).strip()
    domain = str(incoming.get("cf_domain") if "cf_domain" in incoming else db.get_setting("cf_domain", "")).strip()
    incoming_token = incoming.get("cf_admin_token")
    token = str(incoming_token).strip() if incoming_token and incoming_token != "***" else db.get_cf_admin_token()
    if not api_url:
        raise HTTPException(400, "未配置 cf_api_url")
    if not domain:
        raise HTTPException(400, "未配置 cf_domain")
    if not token:
        raise HTTPException(400, "未配置 cf_admin_token")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from mail_cf import CFTempEmailProvider
    try:
        provider = CFTempEmailProvider(api_url=api_url, admin_token=token, domain=domain)
        test_email = provider.create_mailbox()
        return {"ok": True, "message": f"连接成功，测试邮箱: {test_email}"}
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


class ICloudGenerateReq(BaseModel):
    count: int = Field(1, ge=1, le=50, description="生成数量")
    label_prefix: Optional[str] = None
    note: Optional[str] = None


def _icloud_client_from_config():
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from mail_icloud import ICloudHMEClient
    cfg = db.get_icloud_config()
    if not cfg.get("icloud_cookie"):
        raise HTTPException(400, "未配置 icloud_cookie")
    client = ICloudHMEClient(
        cookie=cfg.get("icloud_cookie", ""),
        region=cfg.get("icloud_region", "global"),
        maildomain_host=cfg.get("icloud_maildomain_host", ""),
    )
    if not (cfg.get("icloud_maildomain_host") or "").strip():
        account = client.validate_account()
        if account.get("error"):
            err = account.get("error")
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HTTPException(500, f"iCloud Cookie 校验失败: {msg}")
        detected = account.get("detectedMaildomainHost") or client.maildomain_host
        if detected and detected != client.maildomain_host:
            client = ICloudHMEClient(
                cookie=cfg.get("icloud_cookie", ""),
                region=cfg.get("icloud_region", "global"),
                maildomain_host=detected,
            )
    return client, cfg


@app.post("/api/icloud/generate")
def api_icloud_generate(req: ICloudGenerateReq):
    client, cfg = _icloud_client_from_config()
    label_prefix = (req.label_prefix or cfg.get("icloud_label_prefix") or "gpt-register").strip()
    note = (req.note or cfg.get("icloud_note") or "Generated by gpt-outlook-register").strip()
    rows = []
    errors = []
    for i in range(req.count):
        label = f"{label_prefix}-{int(time.time())}-{i + 1}" if req.count > 1 else f"{label_prefix}-{int(time.time())}"
        res = client.generate_reserved(label=label, note=note)
        if not res.get("success"):
            errors.append(res.get("error") or res.get("reason") or res)
            continue
        result = res.get("result") or {}
        email = (result.get("hme") or "").lower()
        if not email:
            errors.append({"message": f"生成响应缺 hme: {res}"})
            continue
        row = {
            "email": email,
            "label": label,
            "note": note,
            "is_active": True,
            "state": "available",
            "source": "generated",
        }
        db.upsert_icloud_address(row)
        rows.append(row)
    return {
        "ok": bool(rows) and not errors,
        "generated": len(rows),
        "items": rows,
        "errors": errors,
        "stats": db.icloud_stats(),
    }


@app.post("/api/icloud/sync")
def api_icloud_sync():
    client, _cfg = _icloud_client_from_config()
    res = client.list_email()
    if not res.get("success"):
        err = res.get("error") or res.get("reason") or res
        raise HTTPException(500, f"同步失败: {err}")
    rows = (res.get("result") or {}).get("hmeEmails") or []
    result = db.sync_icloud_addresses(rows)
    return {"ok": True, **result, "stats": db.icloud_stats()}


@app.get("/api/icloud/addresses")
def api_icloud_addresses(status: str = "", state: str = "", limit: int = 50, offset: int = 0):
    filt = status or state
    items = db.list_icloud_addresses(state=filt, limit=limit, offset=offset)
    total = db.count_icloud_addresses(state=filt)
    return {"ok": True, "items": items, "total": total, "stats": db.icloud_stats()}


@app.post("/api/icloud/addresses/reset/{email}")
def api_icloud_reset_address(email: str):
    ok = db.reset_icloud_address(email)
    if not ok:
        raise HTTPException(404, f"iCloud 地址 {email} 不存在或不可重置")
    return {"ok": True, "email": email, "stats": db.icloud_stats()}


@app.delete("/api/icloud/addresses/{email}")
def api_icloud_delete_address(email: str):
    ok = db.delete_icloud_address(email)
    if not ok:
        raise HTTPException(404, f"iCloud 地址 {email} 不存在")
    return {"ok": True, "email": email, "stats": db.icloud_stats()}


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    sms_api_key: Optional[str] = None              # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_per_phone_timeout: Optional[str] = None    # 单号等待秒数（默认 80）


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


@app.post("/api/settings/sms/test")
def api_test_sms():
    """测试 SMS provider 连通性：查询余额。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

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
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询当前接码平台的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider, OPENAI_SMS_COUNTRIES, SMS_COUNTRY_NAMES_CN
    try:
        provider = create_sms_provider(cfg["sms_provider"], cfg)
        rows = provider.get_top_countries(service=cfg.get("sms_service") or "dr")
        for r in rows:
            cid = str(r.get("country"))
            r["openai_sms_safe"] = cid in OPENAI_SMS_COUNTRIES
            r["name_cn"] = SMS_COUNTRY_NAMES_CN.get(cid, "未知")
        return {"ok": True, "countries": rows[:30], "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/settings/sms/all_countries")
def api_sms_all_countries(provider: str = ""):
    """返回当前平台实际有库存的国家（动态查询）；查询失败则 fallback 到静态字典。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES, create_sms_provider

    cfg = db.get_sms_internal_config()
    if provider:
        cfg["sms_provider"] = provider

    # 尝试从平台 API 动态获取有库存的国家
    if cfg.get("sms_api_key"):
        try:
            p = create_sms_provider(cfg["sms_provider"], cfg)
            rows = p.get_top_countries(service=cfg.get("sms_service") or "dr")
            countries = []
            for r in rows:
                cid = str(r.get("country") or "")
                countries.append({
                    "id": cid,
                    "name_cn": SMS_COUNTRY_NAMES_CN.get(cid, f"国家{cid}"),
                    "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
                    "price": r.get("price"),
                    "count": r.get("count"),
                })
            if countries:
                return {"ok": True, "countries": countries,
                        "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "live"}
        except Exception:
            pass

    # fallback: 静态字典
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {"id": cid, "name_cn": name, "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES}
        for cid, name in items
    ]
    return {"ok": True, "countries": countries,
            "openai_sms_safe": list(OPENAI_SMS_COUNTRIES), "source": "static"}


# ──────────────────────── 自动导出 (CPA / SUB2API) ────────────────────────


class SaveExportConfigReq(BaseModel):
    # CPA
    cpa_enabled: Optional[str] = None       # "0" / "1"
    cpa_url: Optional[str] = None
    cpa_mgmt_key: Optional[str] = None      # 传 '***' 表示不修改
    cpa_timeout: Optional[str] = None
    # SUB2API
    sub2api_enabled: Optional[str] = None
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None   # '***' 不修改
    sub2api_group_ids: Optional[str] = None  # 逗号分隔，例 "2" 或 "1,2,3"
    sub2api_timeout: Optional[str] = None


@app.get("/api/settings/export")
def api_get_export_config():
    return {"ok": True, "config": db.get_export_config()}


@app.post("/api/settings/export")
def api_save_export_config(req: SaveExportConfigReq):
    db.save_export_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_export_config()}


class TestExportReq(BaseModel):
    target: str = Field(..., description="cpa 或 sub2api")


@app.post("/api/settings/export/test")
def api_test_export(req: TestExportReq):
    """测试 CPA / SUB2API 连通性。"""
    from . import exporter
    cfg = db.get_export_internal_config()
    target = (req.target or "").strip().lower()
    try:
        if target == "cpa":
            return exporter.test_cpa(cfg["cpa"])
        if target == "sub2api":
            return exporter.test_sub2api(cfg["sub2api"])
        raise HTTPException(400, f"未知 target: {target}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"测试失败: {e}")


class ManualExportReq(BaseModel):
    email: str = Field(..., description="要导出的已注册账号邮箱")
    targets: list[str] = Field(default_factory=lambda: ["cpa", "sub2api"],
                                description="选择导出目标：cpa / sub2api")


@app.post("/api/registered/export_to_panel")
def api_manual_export_to_panel(req: ManualExportReq):
    """对一个已注册账号手动触发到面板的导出。

    targets 里选 cpa / sub2api 之一或全部。即使总开关未启用，本接口也会执行
    （只要 URL/密钥 等基础配置已填）。
    """
    from . import exporter
    cred = db.get_registered(req.email)
    if not cred:
        raise HTTPException(404, f"未找到已注册账号: {req.email}")

    cfg = db.get_export_internal_config()
    out = {"email": req.email, "cpa": None, "sub2api": None}
    targets = {t.strip().lower() for t in (req.targets or []) if t}

    if "cpa" in targets:
        cpa_cfg = dict(cfg["cpa"])
        cpa_cfg["enabled"] = True  # 手动触发：强制启用
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


# ──────────────────────── Plus 试用检查 ────────────────────────


class CheckPlusReq(BaseModel):
    emails: list[str] = Field(..., description="要检查的邮箱列表")
    proxy: str = Field("", description="查询代理，留空直连")


@app.post("/api/registered/check_plus")
def api_check_plus(req: CheckPlusReq):
    """用 access_token 查询账号的 Plus 试用状态。"""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        raise HTTPException(500, "curl_cffi 未安装")

    results = {}
    for email in req.emails:
        cred = db.get_registered(email)
        if not cred:
            results[email] = {"status": "not_found", "label": "未找到"}
            continue
        at = (cred.get("access_token") or "").strip()
        if not at:
            results[email] = {"status": "no_at", "label": "无AT"}
            continue
        try:
            proxies = None
            proxy = req.proxy.strip()
            if proxy:
                proxies = {"https": proxy, "http": proxy}
            resp = cffi_requests.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers={
                    "Authorization": f"Bearer {at}",
                    "Accept": "application/json",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/145.0.0.0 Safari/537.36"
                    ),
                },
                proxies=proxies,
                impersonate="chrome110",
                timeout=15,
            )
            if resp.status_code == 401:
                results[email] = {"status": "banned", "label": "封号"}
                continue
            if resp.status_code != 200:
                # HTTP 非 200/401 不记录，让前端继续显示"未检测"
                continue
            data = resp.json()
            accts = data.get("accounts", {})
            if not accts:
                # 无账户数据不记录，让前端继续显示"未检测"
                continue
            info = next(iter(accts.values()))
            acct = info.get("account", {})
            ent = info.get("entitlement", {})
            promo = info.get("eligible_promo_campaigns", {})
            is_deactivated = acct.get("is_deactivated", False)
            if is_deactivated:
                results[email] = {"status": "banned", "label": "封号"}
                continue
            plan = acct.get("plan_type", "free")
            has_sub = ent.get("has_active_subscription", False)
            has_plus_promo = "plus" in promo and promo["plus"].get("id") == "plus-1-month-free"
            if plan == "plus" or has_sub:
                results[email] = {"status": "plus_active", "label": "Plus生效中"}
            elif has_plus_promo:
                results[email] = {"status": "plus_eligible", "label": "可领Plus试用"}
            else:
                results[email] = {"status": "free", "label": "Free"}
        except Exception as e:
            # 所有异常（包括 curl 网络错误）都不记录，让前端继续显示"未检测"
            pass

    import time as _time
    checked_at = _time.time()
    for email, info in results.items():
        if info["status"] not in ("not_found", "no_at"):
            db.update_plus_check(email, {**info, "checked_at": checked_at})

    return {"ok": True, "results": results}


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 10
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    target_count: int = 0        # 目标成功数（0=不限量，达标自动停止）


@app.post("/api/auto/start")
def api_auto_start(req: AutoLoopStartReq):
    res = AUTO_LOOP.start(req.model_dump())
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "启动失败"))
    return res


@app.post("/api/auto/pause")
def api_auto_pause():
    res = AUTO_LOOP.pause()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "暂停失败"))
    return res


@app.post("/api/auto/resume")
def api_auto_resume():
    res = AUTO_LOOP.resume()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "恢复失败"))
    return res


@app.post("/api/auto/stop")
def api_auto_stop():
    res = AUTO_LOOP.stop()
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "停止失败"))
    return res


@app.get("/api/auto/status")
def api_auto_status():
    return {"ok": True, **AUTO_LOOP.status()}


@app.get("/api/auto/stream")
async def api_auto_stream(request: Request):
    """SSE 推送 auto-loop 状态变化 + run_started / run_finished 事件。"""
    q = AUTO_LOOP.subscribe()

    async def gen():
        loop = asyncio.get_event_loop()
        try:
            while True:
                if await request.is_disconnected():
                    break
                # 阻塞拿消息，但每 30s 心跳
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


# ──────────────────────── 静态资源 ────────────────────────


@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8765, reload=False)
