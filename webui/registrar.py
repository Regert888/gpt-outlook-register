"""Internal implementation details.

Internal implementation details.
Internal implementation details.
"""
from __future__ import annotations

import logging
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
from auth_flow import AuthFlow  # noqa: E402
from mail_providers import (  # noqa: E402
    MailProviderError,
    create_mail_provider,
    get_provider_class,
)
from sms_provider import PhoneCallbackController  # noqa: E402

from . import db  # noqa: E402

# Internal implementation note.
_run_queues: dict[str, queue.Queue] = {}
_lock = threading.Lock()

# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
# Internal implementation note.
#
# Internal implementation note.
# Internal implementation note.
_current_run = threading.local()

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


class QueueLogHandler(logging.Handler):
    """Internal implementation details.

    Internal implementation details.
    """

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
            # Internal implementation note.
            # Internal implementation note.
            rid = getattr(_current_run, "run_id", None)
            if rid is not None and rid != self.run_id:
                return
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
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
    """Internal implementation details."""
    import json as _json
    q = _run_queues.get(run_id)
    if q is None:
        return
    body = payload if isinstance(payload, dict) else {"message": str(payload)}
    body["kind"] = kind
    q.put("__EVENT__:" + _json.dumps(body, ensure_ascii=False))


# Internal implementation note.
_NETWORK_ERROR_PATTERNS = [
    "tls", "ssl", "sslerror", "connection", "connect error", "timeout", "timed out",
    "proxy", "socks", "dns", "name resolution", "name or service",
    "cloudflare", "just a moment", "403 forbidden",
    "csrf token \u83b7\u53d6\u5931\u8d25", "csrf token \u5931\u8d25",
    "/sentinel/req", "sentinel /req", "sentinel quickjs",
    "check_proxy \u5931\u8d25", "\u7f51\u7edc\u9884\u68c0\u67e5",
    "curl: (35)", "curl: (28)", "curl: (6)", "curl: (7)",
    "remote disconnected", "connection reset", "connection aborted",
    "max retries exceeded",
    "invalid_state",
]


def classify_error(err: str, mail_source: str = "") -> str:
    """Internal implementation details.

    Internal implementation details.
    Internal implementation details.
    Internal implementation details.
    """
    s = (err or "").lower()

    account_patterns = [
        "wrong_email_otp_code", "invalid_grant", "imap xoauth2",
        "outlook imap account unusable", "user is authenticated but not connected",
        "outlook refresh failed", "authentication failed", "authenticate failed",
        "outlook otp timeout", "registration_disallowed",
        "\u5df2\u6709\u8d26\u53f7", "\u8d26\u53f7\u88ab", "refresh_token \u5931\u6548",
    ]
    if mail_source:
        try:
            exempt = get_provider_class(mail_source).accepts_existing_account
        except MailProviderError:
            exempt = False  # Unknown source uses the strict default.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        if exempt and "\u5df2\u6709\u8d26\u53f7" in account_patterns:
            account_patterns.remove("\u5df2\u6709\u8d26\u53f7")

    # Internal implementation note.
    if any(p in s for p in account_patterns):
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
    """Internal implementation details.

    options:
        want_access_token: bool
        want_session_token: bool
        want_refresh_token: bool
        proxy: Optional[str]
        otp_timeout: int
        allow_existing_login: bool
    """
    # Internal implementation note.
    # Internal implementation note.
    _current_run.run_id = run_id

    handler = QueueLogHandler(run_id, log_file)
    handler.setLevel(logging.INFO)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # Internal implementation note.
    if root_logger.level > logging.INFO or root_logger.level == 0:
        root_logger.setLevel(logging.INFO)

    email = account["email"]
    # Internal implementation note.
    mail_source = db.get_setting("mail_source", "outlook")
    # Internal implementation note.
    # Internal implementation note.
    # Internal implementation note.
    try:
        is_pooled = get_provider_class(mail_source).pooled
    except MailProviderError:
        is_pooled = True

    try:
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        env_overrides = {}
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        env_overrides["WEBUI_ALLOW_LOGIN"] = "1"
        env_overrides["OTP_TIMEOUT"] = str(int(options.get("otp_timeout") or 180))
        # Internal implementation note.
        if not options.get("want_refresh_token", True):
            env_overrides["SKIP_OAUTH_TOKEN_EXCHANGE"] = "1"
            env_overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            env_overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"
        # Internal implementation note.

        cfg = Config()
        cfg.proxy = (options.get("proxy") or "").strip() or None

        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        mail = create_mail_provider(mail_source, db.get_mail_settings(), account)
        logging.getLogger("registrar").info(
            f"[register] Email source: {mail_source} ({mail.display_name})"
        )

        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        _tfa_box: dict = {}

        def _bind_2fa_hook(_flow, at: str) -> None:
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            from .two_factor import bind_totp_2fa_inline
            info = bind_totp_2fa_inline(_flow, at)
            if info and info.get("secret"):
                _tfa_box.update(info)
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                # Internal implementation note.
                try:
                    real_email = getattr(getattr(_flow, "result", None), "email", "") or email
                    db.save_totp_early(real_email, info["secret"], info.get("factor_id", ""))
                    logging.getLogger("registrar").info(
                        f"[register] Saved the 2FA secret early for email={real_email}"
                    )
                except Exception as e:
                    logging.getLogger("registrar").warning(
                        f"[register] Failed to save the 2FA secret early "
                        f"(still retained in memory): {e}"
                    )

        def _account_callback_for_flow(email: str) -> dict:
            """Internal implementation details.

            Internal implementation details.
            Internal implementation details.
            """
            try:
                data = db.get_registered(email)
                if data:
                    return {
                        "password": data.get("password", ""),
                        "totp_secret": data.get("totp_secret", ""),
                    }
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] account_callback error: {e}"
                )
            return {}

        flow = AuthFlow(
            cfg,
            sms_callback=_build_sms_callback(run_id),
            env_overrides=env_overrides,
            on_password=_save_password_early,
            on_session_ready=_bind_2fa_hook if options.get("want_2fa") else None,
            account_callback=_account_callback_for_flow,
        )
        _emit_status(run_id, "phase", {"phase": "starting", "email": email})
        logging.getLogger("registrar").info(f"[register] Starting: {email}")

        partial = False
        d: dict
        try:
            result = flow.run_register(mail)
            d = result.to_dict()
        except RuntimeError as e:
            # Internal implementation note.
            d = flow.result.to_dict()
            need_access = options.get("want_access_token", True)
            need_session = options.get("want_session_token", True)
            need_refresh = options.get("want_refresh_token", True)
            # Internal implementation note.
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
                    f"[register] A late-stage error occurred, but every requested "
                    f"credential is available: {e}"
                )
            elif has_any:
                partial = True
                logging.getLogger("registrar").warning(
                    f"[register] Partial credentials (one or more requested values "
                    f"are missing): {e}"
                )
            else:
                raise

        # Internal implementation note.
        full = d
        d = {
            "email": full.get("email", ""),
            "password": full.get("password", ""),
        }
        if options.get("want_access_token", True):
            d["access_token"] = full.get("access_token", "")
        if options.get("want_session_token", True):
            d["session_token"] = full.get("session_token", "")
            d["cookie_header"] = full.get("cookie_header", "")  # Browser injection.
        if options.get("want_refresh_token", True):
            d["refresh_token"] = full.get("refresh_token", "")
            d["id_token"] = full.get("id_token", "")

        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        if not (d.get("password") or "").strip():
            try:
                _saved = db.get_registered(d.get("email") or "")
                _pw = ((_saved or {}).get("password") or "").strip()
                if _pw:
                    d["password"] = _pw
                    logging.getLogger("registrar").info(
                        "[register] No password was set in this run; using the password "
                        "saved by an earlier register_password step"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] Failed to reload the saved password: {e}"
                )

        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        if options.get("want_2fa"):
            _emit_status(run_id, "phase", {"phase": "binding_2fa", "email": d.get("email")})
            try:
                from .two_factor import bind_totp_2fa, bind_totp_2fa_inline
                # Internal implementation note.
                tinfo = dict(_tfa_box) if _tfa_box.get("secret") else None
                if not tinfo:
                    tinfo = bind_totp_2fa_inline(flow, full.get("access_token", ""))
                if not (tinfo and tinfo.get("secret")):
                    # Internal implementation note.
                    if (d.get("password") or "").strip():
                        logging.getLogger("registrar").info(
                            "[register] Fast 2FA path did not complete; retrying through "
                            "the full login flow..."
                        )
                        tinfo = bind_totp_2fa(
                            cfg, d.get("email", ""), d.get("password", ""),
                            mail_provider=mail, env_overrides=env_overrides,
                        )
                    else:
                        logging.getLogger("registrar").warning(
                            "[register] Fast 2FA path did not complete and no password "
                            "is available; skipping the full login fallback"
                        )
                if tinfo and tinfo.get("secret"):
                    d["totp_secret"] = tinfo["secret"]
                    d["totp_factor_id"] = tinfo.get("factor_id", "")
                    logging.getLogger("registrar").info(
                        f"[register] 2FA enrollment successful for email={d.get('email')}"
                    )
                    _emit_status(run_id, "phase", {"phase": "2fa_bound", "email": d.get("email")})
                else:
                    logging.getLogger("registrar").warning(
                        "[register] 2FA enrollment did not complete; the account remains valid"
                    )
            except Exception as e:
                logging.getLogger("registrar").warning(
                    f"[register] 2FA enrollment error; the account remains valid: {e}"
                )
        # Internal implementation note.
        db.save_registered(d)
        # Internal implementation note.
        # Internal implementation note.
        if is_pooled:
            db.mark_done(email)

        # Internal implementation note.
        _try_export_to_panels(run_id, d)

        result_summary = {
            "email": d.get("email"),
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            # Internal implementation note.
            "password": d.get("password") or "",
            "access_token_len": len(d.get("access_token") or ""),
            "session_token_len": len(d.get("session_token") or ""),
            "refresh_token_len": len(d.get("refresh_token") or ""),
            # Internal implementation note.
            # Internal implementation note.
            "totp_secret": d.get("totp_secret") or "",
            "partial": partial,
        }
        _emit_status(run_id, "done", result_summary)
        logging.getLogger("registrar").info(
            f"[register] Completed email={d.get('email')} "
            f"pw={d.get('password') or '(none)'} "
            f"at={result_summary['access_token_len']} "
            f"st={result_summary['session_token_len']} "
            f"rt={result_summary['refresh_token_len']}"
        )
        db.finish_run(run_id, "done")

    except Exception as e:
        err = str(e)
        category = classify_error(err, mail_source)
        logging.getLogger("registrar").error(
            f"[register] Failed (category={category}): {err}"
        )
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        try:
            _pw = (flow.result.password or "").strip()
            if _pw:
                logging.getLogger("registrar").error(
                    f"[register] A password was generated; save it now: "
                    f"{flow.result.email or email} / {_pw}"
                )
        except Exception:
            pass  # The error occurred before AuthFlow produced a password.
        if category != "account":
            logging.getLogger("registrar").error(traceback.format_exc())
        # Internal implementation note.
        if is_pooled:
            if category == "network":
                db.release_unused(email)
                logging.getLogger("registrar").warning(
                    f"[register] {email} failed because of a network/environment error; "
                    "the account was released back to available"
                )
            else:
                db.mark_failed(email, f"[{category}] {err}")
        db.finish_run(run_id, "failed", err, category=category)
        _emit_status(run_id, "error", {"message": err, "category": category})

    finally:
        # Internal implementation note.
        # Internal implementation note.
        try:
            root_logger.removeHandler(handler)
            handler.close()
        except Exception:
            pass
        q = _run_queues.get(run_id)
        if q is not None:
            q.put(None)  # End-of-stream sentinel.
        # Internal implementation note.
        # Internal implementation note.
        # Internal implementation note.
        _current_run.run_id = None


def _try_export_to_panels(run_id: str, cred: dict) -> None:
    """Internal implementation details.

    Internal implementation details.
    Internal implementation details.
    """
    try:
        cfg = db.get_export_internal_config()
    except Exception as e:
        logging.getLogger("registrar").warning(
            f"[export] Failed to load configuration: {e}"
        )
        return

    cpa_enabled = bool(cfg.get("cpa", {}).get("enabled"))
    sub2api_enabled = bool(cfg.get("sub2api", {}).get("enabled"))
    if not (cpa_enabled or sub2api_enabled):
        return  # No destination is enabled.

    from . import exporter  # Lazy import avoids an unused dependency path.

    explog = logging.getLogger("registrar")

    def _log(msg: str, level: str = "info") -> None:
        if level == "error":
            explog.error(f"[export] {msg}")
        elif level == "warn":
            explog.warning(f"[export] {msg}")
        else:
            explog.info(f"[export] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "export", "message": msg, "level": level})
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
        _log(f"Export failed: {e}", "error")
        return

    # Internal implementation note.
    summary = {}
    if results.get("cpa") is not None:
        summary["cpa"] = {"ok": bool(results["cpa"].get("ok")),
                          "message": results["cpa"].get("message") or results["cpa"].get("error") or ""}
    if results.get("sub2api") is not None:
        summary["sub2api"] = {"ok": bool(results["sub2api"].get("ok")),
                              "message": results["sub2api"].get("message") or results["sub2api"].get("error") or ""}
    try:
        _emit_status(run_id, "phase", {"phase": "export_done", "summary": summary})
    except Exception:
        pass


def _save_password_early(email: str, password: str) -> None:
    """Internal implementation details.

    Internal implementation details.
    Internal implementation details.
    Internal implementation details.

    Internal implementation details.
    Internal implementation details.
    """
    log = logging.getLogger("registrar")
    try:
        db.save_password_early(email, password)
        log.info(f"[register] Password saved for {email} (credentials pending)")
    except Exception as e:
        # Internal implementation note.
        log.warning(f"[register] Failed to save the password; only the log copy remains: {e}")


def _build_sms_callback(run_id: str) -> Optional[PhoneCallbackController]:
    """Internal implementation details.

    Internal implementation details.
    Internal implementation details.
    """
    cfg = db.get_sms_internal_config()
    if not cfg.get("sms_enabled"):
        return None
    api_key = (cfg.get("sms_api_key") or "").strip()
    if not api_key:
        logging.getLogger("registrar").warning(
            "[sms] SMS verification is enabled but sms_api_key is not configured; skipping"
        )
        return None

    smslog = logging.getLogger("registrar")

    def _log(msg: str) -> None:
        # Internal implementation note.
        smslog.info(f"[sms] {msg}")
        try:
            _emit_status(run_id, "phase", {"phase": "sms", "message": msg})
        except Exception:
            pass

    try:
        return PhoneCallbackController(
            provider_key=cfg["sms_provider"],
            config=cfg,
            service=cfg.get("sms_service") or "openai",
            country=cfg.get("sms_country") or "52",
            log_fn=_log,
            auto_select_country=bool(cfg.get("sms_auto_country")),
        )
    except Exception as e:
        smslog.warning(f"[sms] Failed to create the SMS controller: {e}")
        return None


def start_registration(account: dict, options: dict) -> str:
    """Internal implementation details."""
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
