"""
two_factor.py — 注册成功后为账号程序化绑定 TOTP 2FA（库函数版）
================================================================
移植自桌面实测脚本 `bind_2fa.py`（2026-08-04 跑通，mfa_enabled:true）。
去掉命令行 / 号池文件 / print，改成可被 registrar 调用的库函数。

两条路，registrar 先快后慢：

  ① bind_totp_2fa_inline(flow, at)  ★快路径★
     直接拿【注册那个 flow】的 session + access_token 打 enroll/activate。
     实测 2026-08-08 <测试号>@<自建域>：A/B/C/D 四个请求全 200，
     mfa_enabled=true，**6.2 秒**跑完。

  ② bind_totp_2fa(cfg, email, password, ...)  兜底
     新起 AuthFlow 重走一遍 login 正式链再 enroll。约 40 秒 + 一次 PoW +
     一封验证码邮件。快路径失败、或者要给【库里已有的老号】补绑时走这条。

★ 桌面《2FA绑定实现方案.txt》【二】说"注册会话直接 enroll 会 401
  recent_auth_required、必须重走正式链"—— 这条【实测不成立】，见上面探针结果。
  原文是推测不是实测。慢路径保留作兜底，但不再是唯一路。

★ secret 只在 enroll 响应里下发【一次】！服务端不存明文、任何接口都取不回。
  丢了 = 该号 2FA 永久锁死（只能走账号申诉）。本函数返回后 registrar 立刻落库。

★ 绑定即生效：之后该号所有登录都要 6 位动态码（密码/邮件验证后进 mfa-challenge）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import sys
import time
import urllib.parse
from pathlib import Path

# 项目根（gpt-outlook-register/）加入 sys.path，便于 import config / auth_flow。
# registrar 载入时已插过一次，这里再兜底一次，保证 two_factor 可被单独 import。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from auth_flow import AuthFlow  # noqa: E402

logger = logging.getLogger("two_factor")


# ── RFC 6238 手写实现（无 pyotp 也能算码，双保险）─────────────────
def hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str) -> str:
    """当前 30 秒窗口的 6 位码。"""
    return hotp(secret_b32, int(time.time()) // 30)


def verify_totp(secret_b32: str, code: str) -> bool:
    """前后各 1 窗口容错的本地校验（激活前自检用）。"""
    c = int(time.time()) // 30
    return code in {hotp(secret_b32, c + d) for d in (-1, 0, 1)}


# ── 真正干活的三步：查已绑 → enroll → activate ────────────────────
def _enroll_and_activate(flow: AuthFlow, at: str) -> dict | None:
    """拿 flow.session + access_token 完成 enroll/activate，成功返回 dict。

    快慢两条路唯一的区别只在【怎么弄到这个 at】，弄到之后的动作完全一样，
    所以抽出来共用，免得两份代码各改各的漂移掉。
    """
    # 幂等保护：已绑 totp 则不重复 enroll（secret 取不回，只能日志提示）
    logger.info("[2fa] 检查是否已绑定...")
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa_info")
    hh["Authorization"] = f"Bearer {at}"
    r2 = flow.session.get(
        "https://chatgpt.com/backend-api/accounts/mfa_info", headers=hh, timeout=30
    )
    if r2.status_code == 200:
        info = r2.json() or {}
        if info.get("mfa_enabled") and (info.get("factors", {}) or {}).get("totp"):
            logger.info("[2fa] 该号已绑 totp，跳过（secret 无法从服务端取回）")
            return None

    logger.info("[2fa] enroll TOTP（★secret 只在本次响应出现）...")
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa/enroll")
    hh["Authorization"] = f"Bearer {at}"
    hh["Content-Type"] = "application/json"
    r3 = flow.session.post(
        "https://chatgpt.com/backend-api/accounts/mfa/enroll",
        headers=hh, json={"factor_type": "totp"}, timeout=30,
    )
    if r3.status_code != 200:
        logger.warning(
            "[2fa] enroll %s: %s", r3.status_code, (r3.text or "")[:200]
        )
        return None
    en = r3.json() or {}
    secret = en.get("secret", "")
    session_id = en.get("session_id", "")
    factor_id = (en.get("factor", {}) or {}).get("id", "")
    if not secret or not session_id:
        logger.warning("[2fa] enroll 响应缺 secret/session_id，跳过: %s", json.dumps(en)[:200])
        return None

    logger.info("[2fa] 算码并激活...")
    code = totp_now(secret)
    hh = flow._common_headers(
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"
    )
    hh["Authorization"] = f"Bearer {at}"
    hh["Content-Type"] = "application/json"
    r4 = flow.session.post(
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment",
        headers=hh,
        json={"code": code, "factor_type": "totp", "session_id": session_id},
        timeout=30,
    )
    if r4.status_code != 200:
        logger.warning(
            "[2fa] activate_enrollment %s: %s（429 可等 60s 换码重试）",
            r4.status_code, (r4.text or "")[:200]
        )
        return None

    # 验证（失败不影响返回：enroll+activate 都 200 即视为成功，secret 已到手）
    time.sleep(2)
    hh = flow._common_headers("https://chatgpt.com/backend-api/accounts/mfa_info")
    hh["Authorization"] = f"Bearer {at}"
    r5 = flow.session.get(
        "https://chatgpt.com/backend-api/accounts/mfa_info", headers=hh, timeout=30
    )
    if r5.status_code == 200 and (r5.json() or {}).get("mfa_enabled"):
        logger.info("[2fa] ✅ 绑定成功，mfa_enabled=true")
    else:
        logger.warning(
            "[2fa] enroll/activate 已 200，但 mfa_info 复核异常: %s %s",
            r5.status_code, (r5.text or "")[:120]
        )

    return {"secret": secret, "factor_id": factor_id, "session_id": session_id}


# ── 快路径：复用注册会话，不重新登录 ──────────────────────────────
def bind_totp_2fa_inline(flow: AuthFlow, access_token: str = "") -> dict | None:
    """注册刚跑完，直接用同一个 flow 绑 2FA。成功返回 dict，失败返回 None。

    【为什么这条能走通】桌面文档说注册会话直接 enroll 会 401 recent_auth_required，
    必须重走 login 正式链 —— 实测**不成立**。2026-08-08 探针 <测试号>@<自建域>：
        GET  mfa_info -> 200 (mfa_enabled:false)
        POST enroll   -> 200 (secret 32 位)
        POST activate -> 200 {"success":true}
        GET  mfa_info -> 200 mfa_enabled=true
    合理性：注册链本身几十秒前刚做完 OTP 验证 + create_account，服务端眼里
    这就是"最近认证过"，login_challenge 那套要求它已经满足了。

    【省了多少】重走登录链要 ~40s，含一次 PoW（约 18s，最贵的一步）和一封
    验证码邮件；这条 6.2s、零 PoW、零邮件。

    失败返回 None 让调用方回落到慢路径，不做异常抛出。
    """
    try:
        at = access_token or getattr(getattr(flow, "result", None), "access_token", "")
        if not at:
            logger.warning("[2fa] 快路径：注册会话没有 access_token，回落慢路径")
            return None
        return _enroll_and_activate(flow, at)
    except Exception as e:  # noqa: BLE001 — 绝不能拖垮已注册成功的号
        logger.warning("[2fa] 快路径异常（回落慢路径）: %s", e)
        return None


# ── 慢路径（兜底）：新起 flow 重走 login 正式链 ────────────────────
def bind_totp_2fa(
    cfg: Config,
    email: str,
    password: str,
    mail_provider=None,
    env_overrides: dict | None = None,
) -> dict | None:
    """
    注册成功后为账号绑定 TOTP 2FA。

    成功 → 返回 {"secret", "factor_id", "session_id"}；
    任何一步失败 / 前提不满足 / 已绑定 → 返回 None（仅记日志，绝不抛异常，
    以免拖垮已经注册成功的号 —— 调用方 registrar 再套一层 try/except 兜底）。

    ⚠️ 用【独立的 AuthFlow 实例】重走一遍 login 正式链（而非复用注册那个已完成
       signup 的 flow）：enroll 要求 session 有 login_challenge，注册走的是 signup
       链且已登录 chatgpt.com，直接 enroll 会 401 recent_auth_required。
       独立实例 = 独立 device_id + 随机 UA，也符合防风控的批量绑定建议。
    """
    if not email or not password:
        logger.warning("[2fa] 缺邮箱或密码，跳过绑定（passwordless 号无法程序化绑定）")
        return None

    try:
        flow = AuthFlow(cfg, env_overrides=dict(env_overrides or {}))

        # OTP 水印：本条绑定链一开始就打，往后到的信都算「本轮的」。
        # 【为什么不卡在 authorize/continue 前一刻】实测 kj5bvjma7o：绑定挑战的码
        # 在 05:50:54 就投出来了，而 authorize/continue 是 05:51:37 才发的 ——
        # 服务端早在 oauth_init 阶段就发码了，比那个位置的水印还早 43 秒。
        # 那次侥幸没炸只因为后面又补投了两封；万一只发早的那封，水印会把唯一
        # 带对码的信判成旧信 → 又是干等超时。
        # 【为什么敢放宽】实测同一次 challenge 内多封信【码完全相同】
        # （1310/1311/1312 全是 124854），resend 只是把同一个码再投一遍，
        # 所以抓到哪一封都对，放宽窗口不会抓错码。
        # 【为什么不会串上注册那个码】注册和绑定是两个 challenge、码不同
        # （869765 vs 124854），而注册最后一封码信到本函数被调用之间，隔着
        # 建账号 / 重定向链 / 换 session 一大串，实测有 60 秒富余，够拉开。
        chain_started_at = time.time()

        logger.info("[2fa] 1/11 检查代理 / 预热...")
        flow.check_proxy()
        # 没拿到 oai-did 就直接 409，不如早退：2FA 是注册后置步骤，
        # 失败只告警不废号（见调用方 registrar），所以这里 raise 是安全的。
        if not flow.warmup():
            raise RuntimeError(
                "warmup 失败：未拿到 oai-did cookie，绑定链路必然 409 invalid_state"
            )

        logger.info("[2fa] 2/11 获取 csrf_token...")
        csrf = flow.get_csrf_token()

        logger.info("[2fa] 3/11 获取 OAuth 授权地址...")
        auth_url = flow.get_auth_url(csrf, email=email)

        logger.info("[2fa] 4/11 OAuth 初始化（拿 device_id）...")
        device_id = flow.auth_oauth_init(auth_url)

        logger.info("[2fa] 5/11 获取 sentinel token（PoW）...")
        flow.get_sentinel_token(device_id)

        logger.info("[2fa] 6/11 authorize/continue 提交邮箱（★正式链，建 login_challenge）...")
        step = flow.authorize_continue(
            email, flow._last_sentinel_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="bind_2fa",
        )
        page_type = flow._extract_page_type(step)
        continue_url = flow._normalize_continue_url(
            flow._extract_continue_url_from_step(step)
        )
        logger.info("[2fa] page.type = %r", page_type)

        # ── 7/11 密码链（服务端给密码页时才走）──
        # 判据和 auth_flow.run_protocol_login 保持一致：page.type 与 continue_url 任一命中。
        if page_type == "login_password" or "/log-in/password" in continue_url:
            logger.info("[2fa] 7/11 打开密码页 + 密码验证...")
            flow.session.get(
                f"https://auth.openai.com/log-in/password?email={urllib.parse.quote(email)}",
                headers=flow._common_headers("https://auth.openai.com/log-in/password"),
                timeout=30,
            )
            step = flow.login_password_verify(password)
            page_type = flow._extract_page_type(step)
            continue_url = flow._normalize_continue_url(
                flow._extract_continue_url_from_step(step)
            )
            logger.info("[2fa] 密码验证后 page.type = %r", page_type)

        # ── 7.5/11 邮件 OTP 链 ──
        # 【实跑定性 2026-08-08，日志 c189580cb8f6】刚注册完几十秒的新号，即使已经
        # 设过密码，authorize_continue 也直接返回 email_otp_verification —— 服务端对
        # 低信任新号强制走邮箱验证。所以绑 2FA 途中【确实要接一封邮件】。
        # 这段逻辑不是我新发明的，照抄 auth_flow.py:826-849（run_protocol_login 里
        # 早已实测跑通的同款分支），改个 mode 字符串而已。
        need_otp = (page_type == "email_otp_verification") or (
            "/email-verification" in (continue_url or "")
        )
        if need_otp:
            if mail_provider is None:
                logger.warning("[2fa] 该号需邮件 OTP，但未提供 mail_provider，跳过绑定")
                return None
            try:
                otp_timeout = max(10, int(flow._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 60
            logger.info("[2fa] 7.5/11 需要邮件 OTP（timeout=%ss）...", otp_timeout)

            # ── 先瞄一眼：码很可能【已经在信箱里了】────────────────────
            # 实测 <测试号>@<自建域>：本轮 challenge 一共收到 3 封信，
            # 码全都一样（808510）——
            #   14:17:19  get_auth_url 带 login_hint 触发，服务端抢跑发的
            #   14:17:39  authorize/continue 提交邮箱触发
            #   14:17:39  ← 我们自己调 resend 触发的第三封
            # 也就是说走到这一步时，前两封早就到了，第三封纯属多余。每个号白
            # 白多两封验证码信，看着就像在刷码，对风控没好处。
            #
            # 【为什么这里敢省，注册链那边不敢】区别在服务端状态动没动：
            #   绑定链 —— resend 明确是「复用同一个 challenge state 再投一遍」，
            #             不改状态，所以已投递的那封本来就有效，省掉无损。
            #   注册链 —— auth_flow.py:2846 那段是实测结论：POST user/register
            #             成功后服务端会把流程切到 email_otp_send 页，signup
            #             阶段发的码【当场失效】，拿它 verify 直接 409
            #             invalid_state。那次 send_otp 是在重置状态，不是单纯
            #             为了送信，删了就炸。所以注册链一行不动。
            #
            # 探不到就安静回退到原来的发码路径，只多花 4 秒。
            otp_code = None
            try:
                peek = getattr(mail_provider, "peek_otp", None)
                if callable(peek):
                    otp_code = peek(email, issued_after=chain_started_at, wait=4)
            except Exception as e:
                logger.debug("[2fa] 预读 OTP 异常（回退到发码）: %s", e)

            if not otp_code:
                logger.info("[2fa] 信箱里还没有码，主动发一封...")
                # 走 existing 分支：这号已经存在了，必须 resend 复用同一个 challenge state。
                # 调 send_otp 新建 challenge 会让服务端已投递的那封码当场失效 → verify 报
                # wrong_email_otp_code（kickoff_otp_delivery 里写得很清楚的坑）。
                if not flow.kickoff_otp_delivery("existing_bind_2fa"):
                    flow.send_otp(referer="https://auth.openai.com/email-verification")
                otp_code = mail_provider.wait_for_otp(
                    email, timeout=otp_timeout, issued_after=chain_started_at,
                )
            otp_resp = flow.verify_otp(otp_code)
            page_type = flow._extract_page_type(otp_resp)
            continue_url = flow._normalize_continue_url(
                flow._extract_continue_url_from_step(otp_resp)
            )
            logger.info("[2fa] OTP 验证通过，page.type = %r", page_type)

        cu = continue_url
        if not cu:
            logger.warning("[2fa] 登录链没拿到 continue_url（page.type=%r），跳过", page_type)
            return None

        # 已绑 2FA 的号验证完直接进 mfa-challenge（此时不该重复绑定）
        if "/mfa-challenge/" in cu:
            logger.info("[2fa] 该号已启用 2FA（进入 mfa-challenge），跳过重复绑定")
            return None

        logger.info("[2fa] 8/11 消费 callback，建立会话...")
        if not flow._consume_callback_for_session(cu):
            logger.warning("[2fa] 消费 callback 失败，跳过")
            return None

        logger.info("[2fa] 9/11 获取 access_token...")
        _st, at = flow.get_auth_session()
        if not at:
            logger.warning("[2fa] 未拿到 access_token，跳过")
            return None

        # 9.5 ~ 11/11：和快路径完全同一套动作，共用 _enroll_and_activate
        return _enroll_and_activate(flow, at)

    except Exception as e:  # noqa: BLE001 — 绑定任何异常都不能拖垮已注册成功的号
        logger.warning("[2fa] 绑定过程异常（账号仍有效，仅未绑 2FA）: %s", e)
        return None
