"""iCloud 隐藏邮箱 + HTML 中转取件 provider。

主人手上的形态：
    邮箱      wariest-grimier.33@icloud.com    （iCloud「隐藏我的邮件」生成的别名）
    中转链接  https://mail.ai1998.xyz/messages/<token>/<email>

中转站是第三方部署的 HTML 页面，**没有 JSON 接口**（试过 ?format=json，
还是吐 HTML），所以取码只能从 HTML 里抠。

⚠️ 中转站不止一家，取件方式各写各的（实测已遇到 3 家）。主人买号是一批一批买的，
   不同批次可能来自不同中转商，所以这里**几种形态都要认**，自动判别，不能写死一种。

   先分两大类：**JSON 接口式**（形态 C）和 **HTML 页面式**（版式 A/B）。
   判据是链接里有没有 `#email=...&key=...`，见 `_parse_pickup_credentials`。
   下面三种，靠特征标签 / 链接形状自动判别：

   版式 A（mail.ai1998.xyz）—— 纯文本正文，本地时区时间
       <article class="mail-card">
         <span class="subject">New sign-in to your OpenAI account</span>
         <span class="date">2026-08-04 09:19:36</span>
         <div class="meta">发件人：noreply_at_tm_openai_com_...@icloud.com</div>
         <pre class="body">正文……</pre>
       </article>

   版式 B（icloud-api.top）—— 正文是**整封原始 HTML**，RFC2822 UTC 时间
       <div class="card">
         <div class="fr">ChatGPT &lt;otp_at_tm1_openai_com_...@icloud.com&gt;</div>
         <div class="su">ChatGPT の一時的な認証コード</div>
         <div class="dt">Tue, 04 Aug 2026 06:29:52 +0000</div>
         <div class="bd"><html>…整封邮件…</html></div>
       </div>

   A / B 默认都只给最新一封，要全部得带参数：A 用 ?all=1，B 用 ?n=10。
   实测两家都容忍对方的参数（多余的会被忽略），所以两个一起带，
   不用先探测是哪家。

   形态 C（flysms.xyz）—— **纯 JSON 接口，没有 HTML 可抠**（2026-08-07 新增）
       链接长这样，注意参数在 `#` 后面：
           https://flysms.xyz/icloud/pickup#email=xxx%40icloud.com&key=tok_xxx

       `#` 后面的内容**浏览器不会发给服务器**，所以直接 GET 这个地址只拿得到
       一个 339 字节的 React 空壳，一封信都没有 —— A / B / 兜底三个解析器
       全都扫不到东西（主人 2026-08-07 遇到的就是这个）。真正取信的是页面里
       JS 自己发的那个请求，协议是从它的 bundle 里挖出来的：

           GET  <base>/api/pickup/messages?limit=20
           Accept:          application/json
           Authorization:   Bearer <key>
           X-Mailbox-Email: <email>

       返回 {"email","scope","revision","messages":[…],"nextCursor"}，
       每封信有 uid / from / to / date / subject / preview / hasAttachments。

       ✅ 这条路比抠 HTML 稳得多：
          · `uid` 是**真正的邮件 ID**。A / B 只能拿「时间+主题」凑指纹，
            而 OpenAI 连发几封验证码时这两项几乎一模一样，最容易撞车。
          · `date` 是带 Z 的 ISO8601 **绝对时间**，防旧码的时间窗从此可靠
            （版式 A 那个「本机时区墙上时间」的坑在这里不存在）。
          · `from` 是**真实发件人** noreply@tm.openai.com，
            不是中转站改写过的 noreply_at_tm_openai_com_xxx@icloud.com。

       ⚠️ **uid 顺序和时间顺序对不上**（实测 uid …116 比 …118 还新）。
          所以 uid 只能当指纹，排序和时间窗一律认 `date`。
       ⚠️ 正文只有 `preview`（截断的纯文本），没有取全文的接口。OpenAI 的
          验证码都在正文开头，实测日/英两种模板都取得到；哪天模板把码挪到
          很靠后，这里要另想办法。

能力：pooled=True     一批号导进号池，一个一个 claim（每个号自带取件链接）
      ephemeral=False 地址固定不变 ⚠️

⚠️ ephemeral=False 的实际表现（实测 2026-08-04）：
   同一个地址第二次跑时，OpenAI 认出它是老号，流程走
   `检测到已有账号 → passwordless_login`。好消息是它**不要密码**，
   发邮件验证码就能过，所以能拿到 token；但这意味着这类号是
   「登录已有账号」而不是「注册新账号」。
   （base.py docstring 第 30 行原本担心会走 login_password 要密码
     导致 401 —— 实测没有，走的是 passwordless_login。）

   ⚠️ 因此 registrar 里 classify_error 把「已有账号」判为 account 类
   失败是不适用于本 provider 的 —— 见下面 accepts_existing_account。
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    extract_otp,
    register,
    validate_email,
)

logger = logging.getLogger(__name__)

# 只认这些发件人（中转站会把发件人改写成 noreply_at_tm_openai_com_xxx@icloud.com，
# 所以匹配的是下划线形态，不是正常域名）
_FROM_HINTS = (
    "openai", "tm_openai", "chatgpt", "auth0", "tm.openai", "chatgpt.com",
)

# ──────────────────────── HTML 解析 ────────────────────────
#
# 不引 bs4（项目没这个依赖，不想为一个 provider 加）。中转站页面是
# 模板直出的，结构稳定，正则够用；解析失败会记日志而不是静默返回空。

# ── 版式 A：mail.ai1998.xyz ──
_A_CARD = re.compile(r'<article class="mail-card">(.*?)</article>', re.S)
_A_SUBJECT = re.compile(r'<span class="subject">(.*?)</span>', re.S)
_A_DATE = re.compile(r'<span class="date">(.*?)</span>', re.S)
_A_META = re.compile(r'<div class="meta">(.*?)</div>', re.S)
_A_BODY = re.compile(r'<pre class="body">(.*?)</pre>', re.S)

# ── 版式 B：icloud-api.top ──
#
# ⚠️ 正文 <div class="bd"> 里塞的是整封原始 HTML（含嵌套 div），
#    非贪婪匹配到第一个 </div> 会把正文切断在半路。所以 bd 不用正则切，
#    改成从 '<div class="bd">' 一直吃到 card 块末尾（正文永远是最后一个字段）。
_B_CARD = re.compile(r'<div class="card">(.*?)</div></div>\s*(?=<div class="card">|</body>)', re.S)
_B_FROM = re.compile(r'<div class="fr">(.*?)</div>', re.S)
_B_SUBJECT = re.compile(r'<div class="su">(.*?)</div>', re.S)
_B_DATE = re.compile(r'<div class="dt">(.*?)</div>', re.S)
_B_BODY_START = '<div class="bd">'

_RE_TAG = re.compile(r"<[^>]+>")
# 版式 B 正文是整封 HTML，<style>/<script>/条件注释里全是数字（字号、色值、
# 行高），不清掉会被 extract_otp 当成验证码。
_RE_STYLE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
_RE_SCRIPT = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_HEAD = re.compile(r"<head[^>]*>.*?</head>", re.S | re.I)


# 版式 B 的发件人写成 `ChatGPT <otp_at_tm1_openai_com_xxx@icloud.com>`，
# 尖括号是**没转义的裸字符**，去标签时会被当成 HTML 标签整段吃掉，
# 只剩下 "ChatGPT" —— 而发件人正是 _looks_like_openai 的判据。先摘出来。
_RE_ANGLE_ADDR = re.compile(r"<([^<>\s]*@[^<>\s]*)>")


def _strip_tags(s: str) -> str:
    """去标签 + 反转义 HTML 实体（&amp; &#39; 之类）。

    尖括号里的邮箱地址先脱掉括号保下来，再去标签。
    """
    s = _RE_ANGLE_ADDR.sub(r" \1 ", s or "")
    return _html.unescape(_RE_TAG.sub(" ", s)).strip()


def _html_to_text(s: str) -> str:
    """把整封 HTML 正文压成纯文本，保留换行结构。

    版式 B 的正文是原封不动的邮件 HTML。先砍掉 head/style/script/条件注释
    —— 里面 `font-size: 24px` `#F3F3F3` `line-height: 28px` 这类数字一大堆，
    留着的话 extract_otp 会从 CSS 里挖出一个假验证码。
    """
    s = _RE_HEAD.sub(" ", s or "")
    s = _RE_STYLE.sub(" ", s)
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)          # <!--[if mso]> 把验证码夹在中间
    s = re.sub(r"<(br|/p|/div|/tr|/td|/h[1-6])[^>]*>", "\n", s, flags=re.I)
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)
    # 逐行去空白并丢掉空行：extract_otp 靠行结构防误判，一行一个字段最干净
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _parse_date(s: str) -> Optional[float]:
    """把中转页的时间字符串解析成 epoch 秒，两种版式都认。

    版式 A '2026-08-04 09:19:36' —— 没写时区，实测是**本机时区**的墙上时间
        （页面 09:19:36 对应邮件正文里的 EDT 9:19 PM，差 12h，
          说明它就是按服务器本地时区渲染的），按 naive 本地时间解析。

    版式 B 'Tue, 04 Aug 2026 06:29:52 +0000' —— 标准 RFC2822，**带时区**，
        用 email.utils 解析，直接得到正确的绝对时间。有的还带
        ' (UTC)' 后缀，parsedate_to_datetime 处理不了，先削掉。

    解析不出来返回 None —— 调用方会退化成"不做时间过滤"，
    宁可多读一封也不要因为解析失败漏掉验证码。
    """
    s = (s or "").strip()
    if not s:
        return None

    # 版式 B：有星期几或月份缩写 → 走 RFC2822
    if "," in s or re.search(r"\b[A-Z][a-z]{2}\b", s):
        cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*$", "", s)     # 去掉 ' (UTC)'
        try:
            dt = parsedate_to_datetime(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            pass

    # 版式 A：naive 本地时间
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# ──────────────────── 形态 C：JSON 接口 ────────────────────


def _parse_iso8601(s: str) -> Optional[float]:
    """'2026-08-07T01:38:25.000Z' -> epoch 秒。

    带 Z 的绝对时间，不用猜时区 —— 这是 JSON 接口相对 HTML 版式最大的好处。
    解析不出来退回通用的 _parse_date，别为一个格式微调就把时间窗整个丢掉。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return _parse_date(s)


def _parse_pickup_credentials(url: str) -> Optional[dict]:
    """认不认得出这是「JSON 接口式」链接？认得就返回调接口要的三件套。

    判据：`#` 后面同时有 email 和 key（实测就放这儿）。顺带也看 query，
    是给以后别家留的后路。**认不出来返回 None**，调用方继续走 HTML 版式，
    所以这个改动对 A / B 两家零影响。

    接口地址从页面路径推出来，对应 bundle 里那句
    `${'/icloud/'.replace(/\\/$/,'')}/api/pickup/messages`：
        https://flysms.xyz/icloud/pickup -> https://flysms.xyz/icloud/api/pickup/messages
    不写死域名，换个中转商只要形状一样就还能用。
    """
    parts = urllib.parse.urlsplit(url or "")
    for blob in (parts.fragment, parts.query):
        q = dict(urllib.parse.parse_qsl(blob or ""))
        email = (q.get("email") or "").strip()
        token = (q.get("key") or q.get("token") or "").strip()
        if email and token:
            path = parts.path.rstrip("/")
            if path.endswith("/pickup"):
                path = path[: -len("/pickup")]
            api = urllib.parse.urlunsplit(
                (parts.scheme, parts.netloc, f"{path}/api/pickup/messages", "", "")
            )
            return {"api_url": api, "email": email.lower(), "token": token}
    return None


def _parse_pickup_json(payload: dict) -> list[dict]:
    """把接口返回的 messages 映射成和 HTML 版式**完全一样**的字典结构。

    统一形状是故意的：wait_for_otp 里那套时间窗 / 发件人校验 / _seen 去重
    一行都不用改，两条取件路径直接共用。多出来的 uid 只有 _fp 会用。
    """
    out: list[dict] = []
    for m in (payload or {}).get("messages") or []:
        if not isinstance(m, dict):
            continue
        date_str = str(m.get("date") or "")
        out.append({
            "subject": str(m.get("subject") or ""),
            "sender": str(m.get("from") or ""),
            "body": str(m.get("preview") or ""),
            "date_str": date_str,
            "ts": _parse_iso8601(date_str),
            "layout": "json",
            "uid": m.get("uid"),
        })
    return out


def _parse_layout_a(text: str) -> list[dict]:
    """<article class="mail-card"> + span.subject/span.date/pre.body"""
    def grab(rx, block, *, raw=False):
        m = rx.search(block)
        if not m:
            return ""
        # 正文要保留换行（extract_otp 依赖行结构做防误判），只反转义不去标签
        return _html.unescape(m.group(1)) if raw else _strip_tags(m.group(1))

    out: list[dict] = []
    for block in _A_CARD.findall(text):
        date_str = grab(_A_DATE, block)
        out.append({
            "subject": grab(_A_SUBJECT, block),
            "sender": grab(_A_META, block),
            "body": grab(_A_BODY, block, raw=True),
            "date_str": date_str,
            "ts": _parse_date(date_str),
            "layout": "A",
        })
    return out


def _parse_layout_b(text: str) -> list[dict]:
    """<div class="card"> + div.fr/div.su/div.dt/div.bd（正文是整封 HTML）"""
    def grab(rx, block):
        m = rx.search(block)
        return _strip_tags(m.group(1)) if m else ""

    out: list[dict] = []
    blocks = _B_CARD.findall(text)
    if not blocks:
        # 只有一封信时页面结构可能收尾不同，退化成"整页当一个 card"
        idx = text.find('<div class="card">')
        if idx != -1:
            blocks = [text[idx:]]

    for block in blocks:
        date_str = grab(_B_DATE, block)
        bi = block.find(_B_BODY_START)
        body_html = block[bi + len(_B_BODY_START):] if bi != -1 else ""
        out.append({
            "subject": grab(_B_SUBJECT, block),
            "sender": grab(_B_FROM, block),
            "body": _html_to_text(body_html),
            "date_str": date_str,
            "ts": _parse_date(date_str),
            "layout": "B",
        })
    return out


# 兜底告警节流：wait_for_otp 每 3 秒轮一次，不节流的话一次注册能刷 40 条
# 一模一样的警告，把 WebUI 日志淹了。60 秒一条足够主人看见，又不会刷屏。
# （裸 float 的并发写是良性的：最坏结果是两个 worker 各打一条。）
_FALLBACK_WARN_INTERVAL = 60.0
_last_fallback_warn = 0.0


def _warn_fallback_once(text_len: int) -> None:
    global _last_fallback_warn
    now = time.time()
    if now - _last_fallback_warn < _FALLBACK_WARN_INTERVAL:
        logger.debug("[icloud_relay] 版式仍不认识，继续整页扫描（告警已节流）")
        return
    _last_fallback_warn = now
    logger.warning(
        "⚠️ [icloud_relay] 中转页版式不认识（既无 mail-card 也无 card），"
        "已降级到【整页扫描】：时间窗和发件人校验失效，可能取到旧码。"
        "请把这个中转链接发给我加版式。页面长度=%d", text_len,
    )


def _parse_fallback(text: str) -> list[dict]:
    """应急兜底：两种版式都认不出来时，把**整页**当一封信扫。

    动机：中转商换模板 → 标签名全变 → 上面两个解析器同时失灵 → 返回空列表
    → 注册全线卡死，要等我加完新版式才能继续跑。这个兜底的唯一目的是
    **在那段空窗期里让主人还能继续注册**。

    ⚠️ 代价必须说清楚（降级不是平替）：
      · 没有 ts  → wait_for_otp 的时间窗 cutoff 失效，页面上躺着的**旧码
                   有可能被当成新码交上去**（主人以前踩过这个坑）。
      · 没有 sender/subject → _looks_like_openai 发件人校验失效，
                   页面上任何 6 位数字都可能被当验证码。
      · 只在页面内容**变化时**才会被消费 —— 见 date_str 用的页面 hash：
        它天然接进现有的 _seen 去重，页面一个字节没动就不会重复触发，
        等于 gptfree 那套 baseline，但不用另写一套状态。

    所以定位是应急通道：**一旦看到这条 warning，就把新中转链接发给我加版式**。
    """
    body = _html_to_text(text)          # 先剥 head/style/script/注释，
    if not body:                        # 否则 CSS 里的 #F3F3F3 / 24px 会变成假码
        return []
    # 指纹用整页内容 hash：页面没变 → 指纹不变 → _seen 命中 → 不会重复消费。
    digest = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]
    return [{
        "subject": "(整页扫描兜底)",
        # sender 塞一个 _FROM_HINTS 里的词，让 _looks_like_openai 放行。
        # 这里是**故意**绕过发件人校验的：版式都认不出来，本来也拿不到发件人。
        "sender": "openai (fallback: 发件人未知)",
        "body": body,
        "date_str": f"fallback:{digest}",
        "ts": None,                     # 显式无时间 → cutoff 检查短路跳过
        "layout": "fallback",
    }]


def parse_relay_html(text: str) -> list[dict]:
    """把中转页 HTML 解析成邮件列表，自动判别版式。

    返回 [{"subject", "sender", "body", "date_str", "ts", "layout"}, ...]，
    顺序保持页面原序（中转站都是新邮件在前）。

    判别靠特征标签而不是域名 —— 同一家换模板、或者主人以后又买到
    第三家的号，只要结构对得上就还能用；两种都对不上就降级走
    _parse_fallback（整页扫描），保证改版当天不至于完全跑不动。
    """
    text = text or ""
    if "mail-card" in text:
        msgs = _parse_layout_a(text)
    elif 'class="card"' in text:
        msgs = _parse_layout_b(text)
    else:
        _warn_fallback_once(len(text))
        return _parse_fallback(text)

    if not msgs:
        logger.warning("[icloud_relay] 中转页识别出版式但解析到 0 封，结构可能有变动")
    return msgs


@register
class ICloudRelayProvider(MailProvider):
    """iCloud 隐藏邮箱（第三方 HTML 中转取件）。

    使用方式：
        mail = ICloudRelayProvider(
            email="wariest-grimier.33@icloud.com",
            relay_url="https://mail.ai1998.xyz/messages/<token>/<email>",
        )
    """

    kind = "icloud_relay"
    display_name = "iCloud 隐藏邮箱（中转）"
    pooled = True           # 一批号导进号池，每个号自带自己的取件链接
    ephemeral = False       # 固定地址 ⚠️ 见模块 docstring

    # 2 段格式：email----relay_url
    # 每个号的中转链接都不一样（token 和地址都嵌在 URL 里），
    # 所以链接必须跟着号走，不能放全局配置。
    line_segments = 2
    import_hint = "email----中转链接"
    import_placeholder = (
        "wariest-grimier.33@icloud.com----https://mail.example.com/messages/TOKEN/"
        "wariest-grimier.33%40icloud.com"
    )

    # 号池型 provider 不需要全局配置 —— 凭证全在每一行导入数据里。
    # 空列表会让「邮箱配置」页只显示能力说明和"去导入页"的提示。
    config_fields = []

    # 本类邮箱天生就是老号：中转站卖的号大多已经注册过 ChatGPT，
    # OpenAI 走 passwordless_login 照样能拿 token，不该当失败处理。
    # registrar.classify_error 会读这个标志（默认 False，不影响其他 provider）。
    accepts_existing_account = True

    def __init__(self, email: str, relay_url: str, timeout: int = 20):
        email = (email or "").strip().lower()
        relay_url = (relay_url or "").strip()
        if not email:
            raise ValueError("iCloud 邮箱地址不能为空")
        validate_email(email)
        if not relay_url.lower().startswith(("http://", "https://")):
            raise ValueError("中转链接必须是 http(s):// 开头的完整地址")

        self.email = email
        self.relay_url = relay_url
        self.http_timeout = timeout
        self._dead = False
        self.last_persona = None

        # 取件方式自动分流：链接里带 #email=&key= 就是 JSON 接口式（形态 C），
        # 否则按老路子拉 HTML 页面（版式 A/B/兜底）。在这里判一次就够，
        # 省得每轮轮询（3 秒一次）都重新解析一遍 URL。
        self._pickup = _parse_pickup_credentials(relay_url)
        if self._pickup:
            if self._pickup["email"] != email:
                # 号池那一行的邮箱和链接里的对不上 —— 多半是导入时粘串行了。
                # 以链接里的为准（key 是按它签的，用错必然 401），但要吼一声。
                logger.warning(
                    "[icloud_relay] 号池邮箱 %s 与链接里的 %s 不一致，"
                    "按链接里的取件（key 是跟着链接走的）",
                    email, self._pickup["email"],
                )
            logger.debug("[icloud_relay] %s 走 JSON 接口取件", email)

        # 已消费过的邮件指纹（主题+时间），避免同一封被读两遍
        self._seen: set[str] = set()
        # 起始快照只做一次 —— 见 wait_for_otp 里的说明
        self._snapshot_done = False

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        """从号池记录构造 —— 每个号的邮箱和中转链接都在自己那一行里。

        没有全局配置回退：中转链接一号一条（token 嵌在 URL 里），
        放全局只能存一条，跑完就没了。缺 account 直接报错，
        比"悄悄用了上一个号的链接"要好排查得多。
        """
        if not account:
            raise MailProviderError(
                "iCloud 中转邮箱是号池型：请先去「导入邮箱」页导入号，"
                "格式 email----中转链接",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        relay = (account.get("relay_url") or "").strip()
        if not relay:
            raise MailProviderError(
                f"号池里的 {email} 没有中转链接 —— 可能是用旧格式导入的，"
                f"请按 email----中转链接 重新导入",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, relay_url=relay)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    # ──────────────────────── HTTP ────────────────────────

    def _fetch(self) -> str:
        """拉中转页 HTML。

        两家中转的"看全部"参数不一样（A 用 all=1，B 用 n=N），默认都只给
        最新一封。实测两家都会忽略自己不认识的那个参数，所以两个一起带，
        省掉一次探测请求。
        """
        parts = urllib.parse.urlsplit(self.relay_url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q["all"] = "1"
        q.setdefault("n", "20")
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(q), parts.fragment)
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # 401/403/404 → token 失效或链接写错，是配置问题，号本身没救
            if e.code in (401, 403, 404, 410):
                raise MailProviderError(
                    f"中转链接无效（HTTP {e.code}）—— token 可能过期或链接填错了",
                    fatal=True, kind=self.kind,
                ) from e
            raise

    # ──────────────────────── 公共 API ────────────────────────

    def create_mailbox(self) -> str:
        """地址是配置里填死的，直接返回，不造新地址。"""
        return self.email

    def _fetch_json(self, limit: int = 20) -> list[dict]:
        """形态 C：直接调 JSON 接口取信。

        认证是 `Authorization: Bearer` + `X-Mailbox-Email` 两件套，少一个都是 401。
        """
        p = self._pickup
        url = f"{p['api_url']}?{urllib.parse.urlencode({'limit': limit})}"
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {p['token']}",
            "X-Mailbox-Email": p["email"],
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Cache-Control": "no-cache",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.http_timeout) as r:
                payload = json.loads(r.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 410):
                raise MailProviderError(
                    f"取件接口拒绝（HTTP {e.code}）—— key 可能过期，或邮箱和链接对不上",
                    fatal=True, kind=self.kind,
                ) from e
            if e.code == 429:
                # 限流是暂时的，不该当致命错误。轮询本来就 3 秒一轮，
                # 这里退回空列表继续等，比直接把整个号判死友好。
                logger.warning(
                    "[icloud_relay] 取件接口限流 429（Retry-After=%s），本轮跳过",
                    (e.headers.get("Retry-After") if e.headers else None) or "?",
                )
                return []
            raise

        if not isinstance(payload, dict) or payload.get("messages") is None:
            logger.warning("[icloud_relay] 接口返回里没有 messages 字段，结构可能有变动")
            return []
        return _parse_pickup_json(payload)

    def _load(self) -> list[dict]:
        """拉一次并解析（异常原样往上抛）。两种取件方式在这里分流。"""
        if self._pickup:
            return self._fetch_json()
        return parse_relay_html(self._fetch())

    def _messages(self) -> list[dict]:
        """给轮询用的包装：异常吞掉返回空列表（不该因一次网络抖动就崩）。"""
        try:
            return self._load()
        except MailProviderError:
            raise                      # 致命错误要往上抛，不能被当成"暂时没邮件"
        except Exception as e:
            logger.warning(f"[icloud_relay] 拉取邮件异常（吞掉重试）: {e}")
            return []

    @staticmethod
    def _fp(m: dict) -> str:
        """邮件指纹，防同一封被读两遍。

        JSON 接口给了真的 uid，直接用。HTML 版式没有 message id，只能拿
        时间+主题 凑 —— 而 OpenAI 连发几封验证码时这两项几乎一模一样
        （实测 3 封里 2 封同主题、时间差 20 秒），撞车了就会漏读新码。
        又一个该优先走接口的理由。
        """
        uid = m.get("uid")
        if uid not in (None, ""):
            return f"uid:{uid}"
        return f"{m.get('date_str','')}|{m.get('subject','')[:80]}"

    def _looks_like_openai(self, m: dict) -> bool:
        blob = f"{m.get('sender','')} {m.get('subject','')}".lower()
        return any(h in blob for h in _FROM_HINTS)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询中转页等 OTP。

        issued_after 是防串号时间窗：中转页会一直留着历史邮件（主人这个
        邮箱里已经躺着一封旧的 OpenAI 登录通知），不按时间过滤会直接
        读到旧码。页面时间精度只到秒，所以留 90 秒宽容度 —— 宁可放宽
        也不要因为几秒钟的时钟偏差把刚到的码判成旧的。
        """
        timeout = max(int(timeout), 60)
        deadline = time.time() + timeout
        cutoff = (issued_after - 90) if issued_after else None
        logger.info(
            f"[icloud_relay] 等待 OTP -> {email_addr} "
            f"(timeout={timeout}s, cutoff={cutoff})"
        )

        # 起始快照：把开跑前页面上就有的邮件标记为已见。
        #
        # ⚠️ 只在**第一次**调用时做。auth_flow 的 resend 重试链路会拿同一个
        #    provider 实例反复调本方法（超时 → resend → 再等），如果每次进来
        #    都重做快照，第 1 轮等待期间刚到的那封新验证码会在第 2 轮开头被
        #    标记成"已见"，然后被永远跳过 —— 表现为"明明收到了却一直超时"。
        #    时间窗 cutoff 才是防旧码的正解，_seen 只负责防同一封重复处理。
        if not self._snapshot_done:
            try:
                for m in self._messages():
                    self._seen.add(self._fp(m))
                self._snapshot_done = True
                logger.debug(f"[icloud_relay] 初始已有 {len(self._seen)} 封，跳过")
            except MailProviderError:
                raise
            except Exception as e:
                logger.warning(f"[icloud_relay] 初始快照异常: {e}")

        while time.time() < deadline:
            for m in self._messages():
                fp = self._fp(m)
                if fp in self._seen:
                    continue
                self._seen.add(fp)

                if cutoff and m.get("ts") and m["ts"] < cutoff:
                    logger.debug(
                        f"[icloud_relay] 跳过旧邮件 {m.get('date_str')} "
                        f"({m.get('subject','')[:40]})"
                    )
                    continue
                if not self._looks_like_openai(m):
                    logger.debug(
                        f"[icloud_relay] 跳过非 OpenAI 邮件: "
                        f"{m.get('sender','')[:60]}"
                    )
                    continue

                otp = extract_otp(f"{m.get('subject','')}\r\n\r\n{m.get('body','')}")
                if otp:
                    if m.get("layout") == "fallback":
                        # 降级取到的码没经过时间窗和发件人校验，可能是页面上
                        # 躺着的旧码。用 warning 顶到 WebUI 日志上，注册要是
                        # 报"验证码错误"，主人看到这条就知道是这个原因。
                        logger.warning(
                            f"⚠️ [icloud_relay] OTP={otp} 来自【整页扫描兜底】"
                            f"，未经时间窗/发件人校验，如果验证失败请重试一次"
                        )
                    else:
                        logger.info(
                            f"[icloud_relay] ✅ OTP={otp} "
                            f"({m.get('date_str')} {m.get('subject','')[:40]})"
                        )
                    return otp
                logger.debug(
                    f"[icloud_relay] 该邮件无 OTP: {m.get('subject','')[:50]}"
                )
            time.sleep(3)

        raise TimeoutError(
            f"iCloud 中转 OTP 超时 {timeout}s（{email_addr}）—— "
            f"确认中转站能收到这个邮箱的信"
        )

    # ──────────────────────── 导入格式 ────────────────────────

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """email----relay_url

        pooled=False 时用不到，但先写好：主人以后有一批中转链接，
        把 pooled 改成 True 就能直接走号池导入。
        """
        parts = [p.strip() for p in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（email----中转链接），实际 {len(parts)} 段"
            )
        email, relay = parts
        validate_email(email)
        if not relay.lower().startswith(("http://", "https://")):
            raise ValueError("第 2 段必须是 http(s):// 开头的中转链接")
        return {
            "email": email.lower(),
            "kind": cls.kind,
            "relay_url": relay,
        }

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """WebUI「测试连通性」：拉一次，报告用的哪种取件方式、看到几封信。

        把方式也报出来，是为了让主人一眼看出这个链接被判成了哪一类 ——
        万一新链接没被认成 JSON 接口式，这里会直接显示「HTML 中转页」，
        比等注册跑挂了再翻日志快得多。
        """
        way = "JSON 接口" if self._pickup else "HTML 中转页"
        try:
            msgs = self._load()
        except MailProviderError as e:
            return {"ok": False, "message": f"[{way}] {e}"}
        except Exception as e:
            return {"ok": False, "message": f"[{way}] 拉取失败: {e}"}

        if not msgs:
            return {
                "ok": True,
                "message": (
                    f"[{way}] 链接可访问，但当前收件箱是空的（{self.email}）。"
                    "建议先发一封测试邮件确认能收到。"
                ),
            }
        newest = msgs[0]
        return {
            "ok": True,
            "message": (
                f"[{way}] 连接成功，{self.email} 当前有 {len(msgs)} 封邮件，"
                f"最新一封：{newest.get('subject','(无主题)')[:40]}"
                f"（{newest.get('date_str','时间未知')}）"
            ),
        }
