"""注册 worker：调 auth_flow.run_register，并把日志/状态实时推到队列。

每个注册任务跑在独立线程；通过 `RunLogger` 把 `logging` 记录 + tail 状态推
到队列，前端用 SSE 实时收日志。
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]  # gpt-outlook-register/
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from mail_outlook import OutlookMailProvider  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402
from proxy_utils import mask_proxy_url  # noqa: E402
from sms_provider import PhoneCallbackController, create_sms_provider  # noqa: E402

from . import db  # noqa: E402

# mail_source 取值里需要"虚拟占位 account、不走本地号池"的模式
_NON_POOL_SOURCES = ("cf_temp", "oep")

# run_id -> queue of log strings; sentinel = None 表示流结束
_run_queues: dict[str, queue.Queue] = {}
_lock = threading.Lock()

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class QueueLogHandler(logging.Handler):
    """把 logging 记录扔进 run queue + 写 log 文件。"""

    def __init__(self, run_id: str, log_file: Path):
        super().__init__()
        self.run_id = run_id
        self._fh = open(log_file, "a", encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self._fh.write(msg + "\n")
            self._fh.flush()
            q = _run_queues.get(self.run_id)
            if q is not None:
                q.put(msg)
        except Exception:
            pass

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
        super().close()


def _emit_status(run_id: str, kind: str, payload: dict | str = ""):
    """前端约定：以 `__EVENT__:` 开头的行被解析成 JSON 状态事件。"""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# 网络/环境层错误特征：命中任一就把号放回 available（号本身没问题，是环境炸了）
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token 获取失败", "csrf token 失败",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy 失败", "网络预检查",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "余额不足", "sms 余额", "接码平台余额",
]


def classify_error(err: str) -> str:
    """分类错误：'network'（环境/代理问题，号无辜）/ 'account'（号本身有问题）/ 'unknown'。"""
    s = (err or "").lower()
    # 先匹配 account 特征（更具体），避免子串误命中（如 "outlook OTP timeout" 含 "timeout"）
    if any(p in s for p in (
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "已有账号", "账号被", "refresh_token 失效",
        "account_deactivated", "deleted or deactivated",
    )):
        return "account"
    if any(p in s for p in _NETWORK_ERROR_PATTERNS):
        return "network"
    return "unknown"


def _do_register(
    run_id: str,
    account: dict,
    options: dict,
    log_file: Path,
):
    """实际注册任务。

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
    """
    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # 第一次需要的话提到 INFO 级别
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    # OEP/CF 模式下 email 是 claim 后才确定的，用一个可变引用让 SMS/错误状态事件也能带上真实邮箱。
    shared_email = {"email": email}
    saved_env = {}
    # 提前读取，避免在 try 块前异常时 except 引用未定义
    mail_source = db.get_setting("mail_source", "outlook")
    mail = None  # 在 try 里赋值；except 里可能引用

    try:
        # 注入环境变量（不污染全局，跑完恢复）
        env_overrides = {}
        # outlook 接码邮箱常被 OpenAI 走 passwordless_signup 流程（新号收码而非设密码），
        # auth_flow 会误判为"已有账号"分支 → 不设 WEBUI_ALLOW_LOGIN 会 fast-fail。
        # 单号 WebUI 场景下 fast-fail 没意义（批量跑才需要"跳过被识别的号"），故强制 ON。
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
        env_overrides["OTP_TIMEOUT"] = str(int(options.get("otp_timeout") or 180))
        # 用户不要 refresh_token → 直接跳过 Codex OAuth（每次都失败浪费 ~10s + 一堆告警）
        if not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # PROXY 走 cfg.proxy，无需 env
        for k, v in env_overrides.items():
            saved_env[k] = os.environ.get(k)
            os.environ[k] = v

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None

        # ─ 代理使用统计 + 上限检查 ─
        if cfg.proxy:
            usage = db.get_proxy_usage(cfg.proxy)
            if usage["used_count"] >= usage["max_uses"]:
                raise RuntimeError(
                    f"代理 {mask_proxy_url(cfg.proxy)} 已达到使用上限 ({usage['used_count']}/{usage['max_uses']})"
                )
            db.record_proxy_usage(cfg.proxy)
            logging.getLogger("registrar").info(
                f"[register] 代理 {mask_proxy_url(cfg.proxy)} 已使用 {usage['used_count'] + 1}/{usage['max_uses']} 次"
            )

        # ─ 邮箱来源路由：outlook 池 vs CF Worker catch-all vs OEP 平台 ─
        if mail_source == "cf_temp":
            sys_path_root = str(ROOT)
            if sys_path_root not in sys.path:
                sys.path.insert(0, sys_path_root)
            from mail_cf import CFTempEmailProvider

            api_url = db.get_setting("cf_api_url", "")
            domain  = db.get_setting("cf_domain", "")
            token   = db.get_cf_admin_token()
            if not api_url or not domain or not token:
                raise RuntimeError(
                    "CF Temp Email 未配置完整（缺 api_url / domain / admin_token），"
                    "请去「邮箱配置」Tab 填写"
                )
            mail = CFTempEmailProvider(
                api_url=api_url, admin_token=token, domain=domain,
            )
            logging.getLogger("registrar").info(
                f"[register] 邮箱来源: cf_temp / domain={domain}"
            )
        elif mail_source == "oep":
            sys_path_root = str(ROOT)
            if sys_path_root not in sys.path:
                sys.path.insert(0, sys_path_root)
            from mail_oep import OutlookEmailPlusProvider

            oep_url = db.get_setting("oep_api_url", "")
            oep_key = db.get_oep_api_key()
            if not oep_url or not oep_key:
                raise RuntimeError(
                    "OutlookEmailPlus 未配置完整（缺 api_url / api_key），"
                    "请去「邮箱配置」Tab 填写"
                )
            mail = OutlookEmailPlusProvider(
                api_url=oep_url,
                api_key=oep_key,
                caller_id=db.get_setting("oep_caller_id", "gpt-outlook-register"),
                project_key=db.get_setting("oep_project_key", ""),
            )
            logging.getLogger("registrar").info(
                f"[register] 邮箱来源: oep / {oep_url}"
            )
        else:
            mail = OutlookMailProvider(
                email=account["email"],
                password=account.get("password", ""),
                client_id=account["client_id"],
                refresh_token=account["refresh_token"],
            )

        # ─ OEP 模式下指定邮箱：让 provider 跳过 claim-random，直接用此邮箱 ─
        specified_email = (options.get("specified_email") or "").strip() or None
        if specified_email and mail_source == "oep":
            ok, msg = mail.check_accessible(specified_email)  # type: ignore[attr-defined]
            if ok:
                mail.set_fixed_email(specified_email)  # type: ignore[attr-defined]
            else:
                if options.get("strict_email", True):
                    raise RuntimeError(
                        f"指定邮箱 {specified_email} 在 OEP 平台不可用 "
                        f"({msg})；停止注册流程"
                    )
                logging.getLogger("registrar").warning(
                    f"[register] 指定邮箱 {specified_email} 不可用 ({msg})，"
                    f"回退到 claim-random"
                )

        if mail is not None:
            _orig_create_mailbox = mail.create_mailbox

            def _wrapped_create_mailbox():
                em = _orig_create_mailbox()
                real_email = em or getattr(mail, "email", "") or email
                shared_email["email"] = real_email
                # 把 runs 表里的占位邮箱换成真实邮箱，避免 UI/日志一直显示 placeholder
                if real_email and "@" in real_email and "@pool.local" not in real_email:
                    try:
                        db.update_run_email(run_id, real_email)
                    except Exception:
                        pass
                    _emit_status(
                        run_id,
                        "phase",
                        {"phase": "email_claimed", "email": real_email},
                    )
                return em

            mail.create_mailbox = _wrapped_create_mailbox

        flow = AuthFlow(cfg, sms_callback=_build_sms_callback(run_id, shared_email))
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] 开始: {email}")

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RuntimeError as e:
            # 部分凭证也算成功（OTP 验证通过 + create_account 成功 → flow.result 有 token）
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # 用户勾选的凭证全拿到 → 算正常完成（不视为 partial）
            wanted_ok = (
                (not need_access or d.get("access_token"))
                and (not need_session or d.get("session_token"))
                and (not need_refresh or d.get("refresh_token"))
            )
            has_any = bool(
                d.get("access_token") or d.get("refresh_token") or d.get("session_token")
            )
            if wanted_ok and has_any:
                logging.getLogger("registrar").warning(
                    f"[register] 流程末段异常但用户勾选的凭证已齐: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] 部分凭证 (缺用户勾选的某项): {e}"
                )
            else:
                raise

        # ─ 用户选项过滤：未勾选的字段从结果里抹掉，DB 只存用户想要的
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # 同样是浏览器注入用
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # 落库
        db.save_registered(d)
        # CF 模式下 email 是虚拟占位（cf_placeholder_XXX@cf.local），不操作号池；
        # OEP 模式由 provider 自己回传 claim-complete，也不走本地号池
        if mail_source not in _NON_POOL_SOURCES:
            db.mark_done(email)
        if mail_source == "oep" and hasattr(mail, "complete_success"):
            mail.complete_success("注册成功")

        # ─ 可选：导出到 CPA / SUB2API 面板（仅勾选启用时才执行） ─
        _try_export_to_panels(run_id, d)

        result_summary = {
            "email": d.get("email"),
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] 完成 email={d.get('email')} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err)
        logging.getLogger("registrar").error(f"[register] 失败 (category={category}): {err}")
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # CF / OEP 模式不操作本地号池（OEP 由 provider 自己回传 release/complete）
        if mail_source == "oep" and mail is not None and hasattr(mail, "release"):
            # 网络错误 → release 回 available；账号问题 → provider 已在
            # mark_outlook_dead/mark_outlook_retired 里回传 claim-complete
            # (provider_blocked→frozen / credential_invalid→retired)；其余默认 release
            if category == "account" and getattr(mail, "outlook_exhausted", False):
                pass  # 已 claim-complete，号池状态已更新，无需再 release
            else:
                mail.release(f"[{category}] {err}"[:200])
        elif mail_source not in _NON_POOL_SOURCES:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} 判定为网络/环境错误，号已 release 回 available"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(
            run_id,
            "error",
            {
                "message": err,
                "category": category,
                "email": shared_email.get("email", email),
            },
        )

    finally:
        # 还原 env
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 关闭 handler
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)  # sentinel: 流结束


def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """注册完成后可选地把凭证导出到 CPA / SUB2API 面板。

    - 任一目标的"启用"开关关闭时,该目标跳过(不发请求);两者都未启用时整段 no-op。
    - 任何异常都不抛,只 emit 日志/状态(不影响注册主流程)。
    """
    export_email = cred.get("email") or ""
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(f"[export] 读取配置失败: {e}")
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # 用户没勾选任何目标 → 完全不执行

    from . import exporter  # 懒 import,避免未启用时强依赖

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level, "email": export_email})
        except Exception:
            pass

    try:
        results = exporter.run_exports(
            cred,
            cpa_cfg=cfg.get("cpa") if cpa_enabled else None,
            sub2api_cfg=cfg.get("sub2api") if sub2api_enabled else None,
            log_fn=_log,
        )
    except Exception as e:
        _log(f"导出整体异常: {e}", "error")
        return

    # 汇总成一个事件给前端
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary, "email": export_email})
    except Exception:
        pass


def _build_sms_callback(run_id: str, email_ref: Optional[dict] = None) -> Optional[PhoneCallbackController]:
    """根据 webui 配置创建 SMS 接码 controller。

    未启用接码或未配置 API key 时返回 None，flow 会回退到环境变量路径。
    log_fn 把租号/等码的状态推到 SSE 流，前端可见。
    email_ref 是可变引用，OEP/CF 模式下 claim 后真实邮箱会同步进去。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    api_key = (cfg.get("sms_api_key") or "").strip()
    if not api_key:
        logging.getLogger("registrar").warning("[sms] 已启用接码但未配置 sms_api_key，跳过")
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # 既写日志、又通过 _emit_status 推 phase 事件给前端
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(
                run_id,
                "phase",
                {
                    "phase": "sms",
                    "message": msg,
                    "email": (email_ref or {}).get("email", ""),
                },
            )
        except Exception:
            pass

    try:
        _log(f"📡 当前接码平台: {cfg['sms_provider']}")
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
            keep_country=bool(cfg.get("sms_keep_country")),
        )
    except Exception as e:
        smslog.warning(f"[sms] 创建接码 controller 失败: {e}")
        return None


def check_sms_balance() -> Optional[float]:
    """检查接码平台余额是否达到配置的「最低余额」。

    - 未启用接码 / 未配置最低余额：返回 None（不检查）
    - 查询成功：返回当前余额；低于下限时 raise RuntimeError
    - 查询本身失败：日志警告后返回 None，不阻塞注册（避免供应商抖动导致全停）

    供 auto-loop worker 每轮 claim 新号前调用，也可在单次注册启动前调用。
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    try:
        min_balance = float(cfg.get("sms_min_balance") or 0)
    except (TypeError, ValueError):
        min_balance = 0.0
    if min_balance <= 0:
        return None
    smslog = logging.getLogger("registrar")
    try:
        balance = create_sms_provider(cfg["sms_provider"], cfg).get_balance()
        smslog.info(f"[sms] 余额查询: {cfg['sms_provider']} 当前 {balance} (下限 {min_balance})")
        if balance < min_balance:
            raise RuntimeError(
                f"[sms] 接码平台余额不足: {cfg['sms_provider']} 当前余额 {balance} "
                f"低于配置下限 {min_balance}，停止执行注册"
            )
        return balance
    except RuntimeError:
        raise
    except Exception as e:
        smslog.warning(f"[sms] 余额查询失败({e})，跳过余额下限检查")
        return None


def start_registration(account: dict, options: dict) -> str:
    """启动一次注册任务，返回 run_id。"""
    # 启动前先查接码平台余额：不足直接失败，不创建 run、不消耗号码
    check_sms_balance()
    run_id = uuid.uuid4().hex[:12]
    log_file = LOG_DIR / f"{run_id}.log"
    db.create_run(run_id, account["email"], str(log_file))

    q: queue.Queue = queue.Queue()
    with _lock:
        _run_queues[run_id] = q

    th = threading.Thread(
        target=_do_register,
        args=(run_id, account, options, log_file),
        daemon=True,
        name=f"register-{run_id}",
    )
    th.start()
    return run_id


def get_run_queue(run_id: str) -> Optional[queue.Queue]:
    return _run_queues.get(run_id)


def remove_run_queue(run_id: str) -> None:
    with _lock:
        _run_queues.pop(run_id, None)


