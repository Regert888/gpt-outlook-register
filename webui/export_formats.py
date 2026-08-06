"""批量导出格式注册表。

**以后要加导出格式，只改这一个文件**：往 `FORMATS` 里加一条就行。
后端路由、前端下拉框都是照着这张表自动长出来的，一行都不用动。

两种 mode：
  - `text`     一行一条记录，前端弹窗预览 + 复制 + 下载（`render` 逐行）
  - `download` 整份文档，前端拿到直接下载、不弹预览（`render_all` 返回 bytes）

约定（主人定的）：
- **不跳行**。勾了几个号就出几行 / 几个文件，字段为空就留空，
  分隔符照样保留（`邮箱----`），方便主人自己在文本里对齐、补齐。
- **手动导出不管有没有 refresh_token，一律照出**（这是和自动推送的区别所在）。
  自动推送 `exporter.run_exports` 会先用 rt 换 Codex 风格 token，换不到就整个放弃；
  手动导出**不刷新、不拦截**，DB 里是什么就导什么，能不能用主人自己判断。
- 行序 = 「注册结果」表格里的顺序（created_at 倒序），好核对。
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExportFormat:
    id: str                                       # 前端 command 用的唯一标识
    label: str                                    # 下拉菜单里显示的名字
    filename: str                                 # 下载的文件名
    mode: str = "text"                            # "text" | "download"
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None          # mode=text：一行记录 -> 一行文本
    render_all: Optional[Callable[[list], bytes]] = None    # mode=download：整批 -> 文件字节
    note: str = ""                                # 下拉菜单里的灰色小字说明


def _s(row: dict, key: str) -> str:
    """取字段并转成干净字符串（None / 非 str 都兜住）。"""
    v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


# ──────────────────────── CPA / SUB2API ────────────────────────
# 直接复用 exporter.py 里自动推送用的那两个 build 函数，不重写一遍字段拼装 ——
# 将来推送逻辑改了字段，手动导出自动跟着改，不会两边漂移。


def _safe_filename(name: str) -> str:
    """邮箱 -> 安全文件名。Windows 非法字符全换成 _。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    cleaned = cleaned.strip(". ") or "unknown"
    return cleaned[:120]


def _cpa_fallback(row: dict) -> dict:
    """build_cpa_token_json 抛错时的兜底（比如 access_token 是空的）。

    照样出文件、字段结构不变，**不静默丢号**。
    """
    return {
        "type": "codex",
        "email": _s(row, "email"),
        "expired": "",
        "id_token": _s(row, "id_token"),
        "account_id": "",
        "access_token": _s(row, "access_token"),
        "last_refresh": "",
        "refresh_token": _s(row, "refresh_token"),
    }


def _render_cpa_zip(rows: list) -> bytes:
    """CPA：每个号一个 `{email}.json`，打成 zip。

    CPA 的 auth-files 就是按「一号一文件」吃的（见 exporter.export_to_cpa:335），
    所以这里不做成 JSON 数组，直接给能丢进目录的形态。
    """
    from . import exporter

    buf = io.BytesIO()
    used: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for row in rows or []:
            try:
                data = exporter.build_cpa_token_json(row)
            except Exception as e:
                logger.warning(f"[export] CPA 构建失败，用兜底结构: {_s(row, 'email')}: {e}")
                data = _cpa_fallback(row)

            base = _safe_filename(_s(row, "email") or "unknown")
            n = used.get(base, 0)
            used[base] = n + 1
            name = f"{base}.json" if n == 0 else f"{base}_{n}.json"

            z.writestr(name, json.dumps(data, ensure_ascii=False, indent=2))
    return buf.getvalue()


def _sub2api_group_ids() -> list:
    """从「导出配置」里读 group_ids，读不到就用 exporter 的默认值。"""
    from . import db, exporter

    raw = None
    try:
        raw = (db.get_export_config() or {}).get("sub2api_group_ids")
    except Exception as e:
        logger.warning(f"[export] 读取 sub2api_group_ids 失败，用默认值: {e}")
    return exporter._parse_group_ids(raw)


def _sub2api_fallback(row: dict, group_ids: list) -> dict:
    from . import exporter

    email = _s(row, "email")
    return {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": _s(row, "access_token"),
            "refresh_token": _s(row, "refresh_token"),
            "expires_in": exporter.SUB2API_DEFAULT_EXPIRES_IN,
            "expires_at": 0,
            "chatgpt_account_id": "",
            "chatgpt_user_id": "",
            "organization_id": "",
            "client_id": exporter.CODEX_CLIENT_ID,
            "id_token": _s(row, "id_token"),
        },
        "extra": {"email": email},
        "group_ids": list(group_ids),
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": True,
    }


def _render_sub2api_json(rows: list) -> bytes:
    """SUB2API：一整个 JSON 数组，每个元素就是 POST /api/v1/admin/accounts 的 body。"""
    from . import exporter

    group_ids = _sub2api_group_ids()
    out = []
    for row in rows or []:
        try:
            out.append(exporter.build_sub2api_payload(row, group_ids))
        except Exception as e:
            logger.warning(f"[export] SUB2API 构建失败，用兜底结构: {_s(row, 'email')}: {e}")
            out.append(_sub2api_fallback(row, group_ids))
    return json.dumps(out, ensure_ascii=False, indent=2).encode("utf-8")


# ──────────────────────── 注册表 ────────────────────────


FORMATS: list[ExportFormat] = [
    ExportFormat(
        id="at",
        label="access_token",
        filename="AT.txt",
        render=lambda r: _s(r, "access_token"),
    ),
    ExportFormat(
        id="email_pw",
        label="邮箱----密码",
        filename="账号密码.txt",
        render=lambda r: f'{_s(r, "email")}----{_s(r, "password")}',
    ),
    ExportFormat(
        id="cpa",
        label="CPA (zip)",
        filename="cpa_tokens.zip",
        mode="download",
        mime="application/zip",
        render_all=_render_cpa_zip,
    ),
    ExportFormat(
        id="sub2api",
        label="SUB2API (json)",
        filename="sub2api_accounts.json",
        mode="download",
        mime="application/json; charset=utf-8",
        render_all=_render_sub2api_json,
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    """给前端的精简清单（不含 render 函数）。"""
    return [
        {
            "id": f.id,
            "label": f.label,
            "filename": f.filename,
            "mode": f.mode,
            "mime": f.mime,
            "note": f.note,
        }
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[ExportFormat]:
    return _BY_ID.get((fmt_id or "").strip())


def render_text(rows: list, fmt: "ExportFormat | str") -> str:
    """mode=text：一行一条记录。

    单条渲染炸了不整体失败 —— 那一行留空，其余照常导出。
    """
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render:
        raise RuntimeError(f"格式 {f.id} 不是文本格式")

    lines = []
    for r in rows or []:
        try:
            lines.append(f.render(r))
        except Exception:
            lines.append("")
    return "\n".join(lines)


def render_bytes(rows: list, fmt: "ExportFormat | str") -> bytes:
    """mode=download：整份文件字节。"""
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render_all:
        raise RuntimeError(f"格式 {f.id} 不是下载格式")
    return f.render_all(rows or [])


# 兼容旧调用名
def render(rows: list, fmt: "ExportFormat | str") -> str:
    return render_text(rows, fmt)
