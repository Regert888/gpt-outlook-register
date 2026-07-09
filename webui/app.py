"""FastAPI 主程序：路由 + SSE 流式日志。

启动:
    python -m webui.app
或者:
    python start_webui.py
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from . import db, registrar  # noqa: E402
from .auto_loop import CONTROLLER as AUTO_LOOP  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402
from config import Config  # noqa: E402
from proxy_utils import mask_proxy_url, parse_proxy_pool  # noqa: E402

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


def _log_startup_config():
    """启动时打印关键数据库配置，方便排查。"""
    try:
        mail_source = db.get_setting("mail_source", "outlook")
        proxy_cfg = db.get_proxy_config()
        sms_cfg = db.get_sms_internal_config()
        export_cfg = db.get_export_internal_config()

        pool_lines = [
            line.strip()
            for line in (proxy_cfg.get("proxy_pool") or "").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        logger.info(
            "[startup] 配置: mail_source=%s | proxy_max_uses=%s | proxy_pool=%d条 | proxy=%s",
            mail_source,
            proxy_cfg.get("proxy_max_uses", "?"),
            len(pool_lines),
            mask_proxy_url(proxy_cfg.get("proxy", "") or "(无)"),
        )
        logger.info(
            "[startup] 配置: sms_enabled=%s | sms_provider=%s | sms_country=%s",
            sms_cfg.get("sms_enabled"),
            sms_cfg.get("sms_provider"),
            sms_cfg.get("sms_country"),
        )
        logger.info(
            "[startup] 配置: cpa_enabled=%s | sub2api_enabled=%s",
            export_cfg.get("cpa", {}).get("enabled"),
            export_cfg.get("sub2api", {}).get("enabled"),
        )
    except Exception as _e:
        logger.warning("[startup] 打印配置失败: %s", _e)


_log_startup_config()

app = FastAPI(title="GPT Outlook Register WebUI", docs_url=None, redoc_url=None)


# ──────────────────────── Pydantic 模型 ────────────────────────


class ImportReq(BaseModel):
    text: str = Field(..., description="多行 4 段格式 (email----password----client_id----refresh_token)")


class RegisterReq(BaseModel):
    email: Optional[str] = Field(None, description="留空 = 自动 claim 下一个 available")
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""
    proxy_pool: str = ""
    random_proxy_from_pool: bool = Field(
        False,
        description="True=若填写了代理池，单个注册从代理池随机选一条代理",
    )
    otp_timeout: int = 180
    allow_existing_login: bool = True
    strict_email: bool = Field(
        True,
        description="True=指定邮箱不可用时直接报错；False=自动 fallback 到下一个 available",
    )


# ──────────────────────── API ────────────────────────


@app.get("/api/health")
def health():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/import")
def api_import(req: ImportReq):
    rows = db.parse_lines(req.text)
    for r in rows:
        r["email"] = r["email"].lower()
    result = db.import_accounts(rows)
    return {"ok": True, **result, "stats": db.stats()}


@app.get("/api/accounts")
def api_accounts(status: str = "", limit: int = 500):
    return {"ok": True, "items": db.list_accounts(status=status, limit=limit)}


@app.delete("/api/accounts/{email}")
def api_delete_account(email: str):
    ok = db.delete_account(email.lower())
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
        n = db.delete_accounts_by_emails([e.lower() for e in req.emails])
        return {"ok": True, "deleted": n, "by": "emails", "stats": db.stats()}
    raise HTTPException(400, "需要 status 或 emails")


@app.post("/api/accounts/reset_failed")
def api_reset_failed():
    n = db.reset_failed_to_available()
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/reset/{email}")
def api_reset_account(email: str):
    """重置单个号：done / failed → available。"""
    ok = db.reset_to_available(email.lower())
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
    n = db.bulk_reset_to_available([e.lower() for e in req.emails])
    return {"ok": True, "reset": n, "stats": db.stats()}


@app.post("/api/accounts/release_stale")
def api_release_stale(stale_seconds: int = 1800):
    n = db.release_stale_in_use(stale_seconds=stale_seconds)
    return {"ok": True, "released": n, "stats": db.stats()}


@app.get("/api/stats")
def api_stats():
    return {"ok": True, "stats": db.stats()}


@app.post("/api/register")
def api_register(req: RegisterReq):
    """启动注册任务，返回 run_id。前端拿 run_id 去 /api/runs/{run_id}/stream 订阅 SSE。"""
    mail_source = db.get_setting("mail_source", "outlook")
    is_pool_less = mail_source in ("cf_temp", "oep")
    proxy = req.proxy
    if req.random_proxy_from_pool:
        proxy_pool = parse_proxy_pool(req.proxy_pool)
        if proxy_pool:
            proxy = random.choice(proxy_pool)
            logger.info("[run] 单个注册从代理池随机选择代理: %s", mask_proxy_url(proxy))

    if is_pool_less:
        # CF / OEP 模式：不需要 outlook 号池；只有 OEP 支持指定邮箱
        oep_specified = req.email if mail_source == "oep" else None
        if oep_specified:
            account = {
                "email": oep_specified.strip(),
                "password": "",
                "client_id": "",
                "refresh_token": "",
            }
        else:
            import time as _t
            account = {
                "email": f"placeholder_{int(_t.time())}@pool.local",
                "password": "",
                "client_id": "",
                "refresh_token": "",
            }
    elif req.email:
        account = db.claim_account(req.email.lower())
        if not account:
            if req.strict_email:
                raise HTTPException(
                    400, f"邮箱 {req.email} 不可用 (不存在 / 已 in_use / 已完成)"
                )
            account = db.claim_next()
            if not account:
                raise HTTPException(400, "号池里没有 available 账号；请先批量导入")
    else:
        account = db.claim_next()
        if not account:
            raise HTTPException(400, "号池里没有 available 账号；请先批量导入")

    options = {
        "want_access_token": req.want_access_token,
        "want_session_token": req.want_session_token,
        "want_refresh_token": req.want_refresh_token,
        "proxy": proxy,
        "otp_timeout": int(req.otp_timeout),
        "allow_existing_login": req.allow_existing_login,
        "strict_email": req.strict_email,
        # OEP 模式下指定了邮箱时透传给 registrar，让 OEP provider 跳过 claim-random；
        # CF / outlook 模式不支持指定邮箱（CF 始终随机生成临时邮箱；outlook 走本地号池 claim）
        "specified_email": oep_specified,
    }
    try:
        run_id = registrar.start_registration(account, options)
    except RuntimeError as e:
        msg = str(e)
        if "余额不足" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise
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
def api_registered(limit: int = 500):
    return {"ok": True, "items": db.list_registered(limit=limit)}


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
    mail_source: Optional[str] = None       # outlook / cf_temp / oep
    cf_api_url: Optional[str] = None
    cf_admin_token: Optional[str] = None
    cf_domain: Optional[str] = None
    oep_api_url: Optional[str] = None
    oep_api_key: Optional[str] = None
    oep_project_key: Optional[str] = None
    oep_caller_id: Optional[str] = None


@app.post("/api/settings/mail")
def api_save_mail_config(req: SaveMailConfigReq):
    db.save_mail_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_mail_config()}


@app.post("/api/settings/mail/test")
def api_test_mail():
    """测试邮箱来源连通性：CF Temp Email 或 OutlookEmailPlus 平台。"""
    mail_source = db.get_setting("mail_source", "outlook")
    if mail_source == "cf_temp":
        api_url = db.get_setting("cf_api_url", "")
        domain = db.get_setting("cf_domain", "")
        token = db.get_cf_admin_token()
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
    elif mail_source == "oep":
        api_url = db.get_setting("oep_api_url", "")
        api_key = db.get_oep_api_key()
        if not api_url:
            raise HTTPException(400, "未配置 oep_api_url")
        if not api_key:
            raise HTTPException(400, "未配置 oep_api_key")

        import sys as _sys
        ROOT_DIR = Path(__file__).resolve().parents[1]
        if str(ROOT_DIR) not in _sys.path:
            _sys.path.insert(0, str(ROOT_DIR))
        from mail_oep import OutlookEmailPlusProvider
        try:
            provider = OutlookEmailPlusProvider(
                api_url=api_url, api_key=api_key,
                caller_id=db.get_setting("oep_caller_id", "gpt-outlook-register"),
                project_key=db.get_setting("oep_project_key", ""),
            )
            # 健康检查 + 池状态
            import urllib.request as _u, json as _j
            req = _u.Request(api_url.rstrip("/") + "/api/external/health")
            req.add_header("X-API-Key", api_key)
            health = _j.loads(_u.urlopen(req, timeout=15).read())
            stats_req = _u.Request(api_url.rstrip("/") + "/api/external/pool/stats")
            stats_req.add_header("X-API-Key", api_key)
            stats = _j.loads(_u.urlopen(stats_req, timeout=15).read())
            avail = (stats.get("data") or {}).get("pool_counts", {}).get("available", "?")
            return {
                "ok": True,
                "message": f"连接成功 (v{health.get('data',{}).get('version','?')})，池中可用邮箱: {avail}",
            }
        except Exception as e:
            raise HTTPException(500, f"连接失败: {e}")
    else:
        raise HTTPException(400, f"当前 mail_source={mail_source}，不需要测试")


# ──────────────────────── SMS 接码配置 ────────────────────────


@app.get("/api/settings/sms")
def api_get_sms_config():
    return {"ok": True, "config": db.get_sms_config()}


class SaveSmsConfigReq(BaseModel):
    sms_enabled: Optional[str] = None              # "0" / "1"
    sms_provider: Optional[str] = None             # smsbower / herosms
    smsbower_api_key: Optional[str] = None         # 传 '***' 表示不修改
    herosms_api_key: Optional[str] = None          # 传 '***' 表示不修改
    sms_country: Optional[str] = None              # ID 或国家代码（'52' / 'th'）
    sms_service: Optional[str] = None              # OpenAI = 'dr'
    sms_max_price: Optional[str] = None
    sms_reuse_phone: Optional[str] = None
    sms_phone_success_max: Optional[str] = None
    sms_auto_country: Optional[str] = None
    sms_keep_country: Optional[str] = None
    sms_strict_whitelist: Optional[str] = None
    sms_allowed_countries: Optional[str] = None    # 逗号分隔的 ID 列表，自动选号时只从这里挑
    sms_auto_min_stock: Optional[str] = None
    sms_auto_max_price: Optional[str] = None
    sms_max_phone_attempts: Optional[str] = None   # 空 = 用 provider 默认；>0 = 自定义
    sms_resend_interval: Optional[str] = None      # OpenAI resend 间隔秒数（默认 20）
    sms_resend_max: Optional[str] = None           # OpenAI resend 最多次数（默认 3）
    sms_min_balance: Optional[str] = None          # 短信供应商最低余额；低于则停止注册


@app.post("/api/settings/sms")
def api_save_sms_config(req: SaveSmsConfigReq):
    db.save_sms_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_sms_config()}


class TestSmsReq(BaseModel):
    provider: Optional[str] = Field(None, description="smsbower / herosms；不传 = 用当前选中的 provider")


@app.post("/api/settings/sms/test")
def api_test_sms(req: TestSmsReq):
    """测试指定 SMS provider 连通性：查询余额。"""
    target = (req.provider or db.get_setting("sms_provider", "smsbower")).strip().lower()
    if target not in ("smsbower", "herosms"):
        raise HTTPException(400, f"未知 provider: {target}")

    api_key = db.get_setting(f"{target}_api_key", "").strip()
    if not api_key:
        raise HTTPException(400, f"未配置 {target}_api_key")

    cfg = {
        "sms_provider": target,
        "sms_api_key": api_key,
        "sms_country": db.get_setting("sms_country", "52"),
        "sms_service": db.get_setting("sms_service", "dr"),
        "sms_max_price": db.get_setting("sms_max_price", ""),
        "sms_proxy": db.get_setting("proxy", ""),
    }

    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import create_sms_provider
    try:
        provider = create_sms_provider(target, cfg)
        balance = provider.get_balance()
        return {
            "ok": True,
            "provider": target,
            "balance": balance,
            "message": f"连接成功，余额: {balance}",
        }
    except Exception as e:
        raise HTTPException(500, f"连接失败: {e}")


@app.get("/api/settings/sms/countries")
def api_sms_top_countries():
    """查询 SmsBower / HeroSMS 的国家排名（价格 + 库存）。"""
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_api_key"):
        raise HTTPException(400, "未配置 sms_api_key")
    if cfg["sms_provider"] not in ("smsbower", "herosms"):
        return {"ok": True, "countries": [], "message": "当前 provider 不支持国家排名查询"}

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
def api_sms_all_countries():
    """返回所有已知国家 ID + 中文名（用于下拉框 / 多选）。"""
    import sys as _sys
    ROOT_DIR = Path(__file__).resolve().parents[1]
    if str(ROOT_DIR) not in _sys.path:
        _sys.path.insert(0, str(ROOT_DIR))
    from sms_provider import SMS_COUNTRY_NAMES_CN, OPENAI_SMS_COUNTRIES
    # 按 ID 数值升序
    items = sorted(SMS_COUNTRY_NAMES_CN.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 9999)
    countries = [
        {
            "id": cid,
            "name_cn": name,
            "openai_sms_safe": cid in OPENAI_SMS_COUNTRIES,
        }
        for cid, name in items
    ]
    return {"ok": True, "countries": countries, "openai_sms_safe": list(OPENAI_SMS_COUNTRIES)}


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


# ──────────────────────── 代理配置 ────────────────────────


class ProxyConfigReq(BaseModel):
    proxy: Optional[str] = None
    proxy_pool: Optional[str] = None
    auto_rotate_proxy: Optional[bool] = None
    rotate_proxy_every: Optional[int] = None
    proxy_max_uses: Optional[int] = None


@app.get("/api/settings/proxy")
def api_get_proxy_config():
    return {"ok": True, "config": db.get_proxy_config()}


@app.post("/api/settings/proxy")
def api_save_proxy_config(req: ProxyConfigReq):
    db.save_proxy_config(req.model_dump(exclude_none=True))
    return {"ok": True, "config": db.get_proxy_config()}


# ──────────────────────── 代理池连通性测试 ────────────────────────


def _test_single_proxy(proxy: str) -> dict:
    """测试单个代理：复用 AuthFlow.check_proxy()（cloudflare trace + chatgpt csrf）。"""
    try:
        cfg = Config(proxy=proxy or None)
        flow = AuthFlow(cfg)
        ok = flow.check_proxy()
        return {
            "proxy": proxy,
            "ok": ok,
            "error": "" if ok else "代理可联网，但 chatgpt.com 返回 403（IP 被 Cloudflare 拦截或 TLS 指纹被识别）",
        }
    except Exception as e:
        return {"proxy": proxy, "ok": False, "error": str(e)}


class ProxyPoolTestReq(BaseModel):
    proxy_pool: str = ""    # 多行代理池
    proxy: str = ""         # 单代理（无代理池时）


@app.post("/api/settings/proxy/test")
def api_test_proxy(req: ProxyPoolTestReq):
    """批量测试代理池里每个代理的可用性。"""
    proxies = [
        line.strip()
        for line in (req.proxy_pool or "").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not proxies and req.proxy:
        proxies = [req.proxy.strip()]
    if not proxies:
        raise HTTPException(400, "未提供代理，请填写「代理」或「代理池」")

    results: list[dict] = []
    max_workers = min(10, max(1, len(proxies)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_proxy = {ex.submit(_test_single_proxy, p): p for p in proxies}
        for future in concurrent.futures.as_completed(future_to_proxy):
            results.append(future.result())

    # 按原始顺序返回
    order = {p: i for i, p in enumerate(proxies)}
    results.sort(key=lambda r: order.get(r["proxy"], 0))
    available = sum(1 for r in results if r["ok"])
    return {
        "ok": True,
        "available": available,
        "total": len(results),
        "results": results,
    }


# ──────────────────────── auto-loop ────────────────────────


class AutoLoopStartReq(BaseModel):
    """跟 RegisterReq 复用同样的字段，auto-loop 内部传给每个 run。"""
    want_access_token: bool = True
    want_session_token: bool = True
    want_refresh_token: bool = True
    proxy: str = ""              # 单代理（concurrency=1 + 无代理池时用）
    proxy_pool: str = ""         # 多代理池（每行一个）；优先于 proxy
    concurrency: int = 1         # 并发 worker 数（1-20）
    otp_timeout: int = 180
    allow_existing_login: bool = True
    cool_down_seconds: float = 3.0  # 每个 worker 跑完后冷却（防风控）
    auto_rotate_proxy: bool = True   # 是否按账号批量轮换代理
    rotate_proxy_every: int = 5      # 每 N 个账号轮换一次代理


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
    return FileResponse(STATIC_DIR / "index.html", headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
    })


# 静态文件禁止浏览器缓存，确保改 app.js/index.html 后硬刷新不需要
app.mount(
    "/static",
    StaticFiles(directory=str(STATIC_DIR), html=False),
    name="static",
)


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8765"))
    print(f"\n🔔 团子喵 WebUI: http://127.0.0.1:{_port}/\n")
    uvicorn.run("webui.app:app", host="127.0.0.1", port=_port, reload=False)
