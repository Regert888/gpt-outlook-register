#!/usr/bin/env python3
"""WebUI 一键启动脚本：装依赖 → 跑 uvicorn。

用法：
    python start_webui.py             # 默认 127.0.0.1:8765，不自动打开浏览器
    python start_webui.py --browser   # 启动后自动打开浏览器
    python start_webui.py --no-browser # 显式不打开浏览器（同默认）
    python start_webui.py --port 9000 # 自定义端口
    python start_webui.py --host 0.0.0.0 --port 8765  # 内网监听
    python start_webui.py --reload  # 开发模式，auto-reload
    python start_webui.py --host 0.0.0.0 --ssl-keyfile key.pem --ssl-certfile cert.pem  # HTTPS
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

# Windows 控制台 GBK 编码兼容：强制 UTF-8 输出
if sys.platform.startswith("win"):
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    ap.add_argument(
        "--port", type=int, default=8765,
        help="监听端口 (默认 8765)",
    )
    ap.add_argument("--no-browser", action="store_true", default=True,
                    help="不自动打开浏览器（默认已启用）")
    ap.add_argument("--browser", action="store_true", default=False,
                    help="启动后自动打开浏览器（覆盖 --no-browser）")
    ap.add_argument(
        "--reload", action="store_true", default=False,
        help="开发模式:代码改动自动重启 (默认关闭)",
    )
    ap.add_argument(
        "--no-reload", action="store_true",
        help="禁用 auto reload (默认即关闭)",
    )
    ap.add_argument("--ssl-keyfile", default=None, help="SSL 私钥文件路径")
    ap.add_argument("--ssl-certfile", default=None, help="SSL 证书文件路径")
    args = ap.parse_args()

    reload = args.reload and not args.no_reload
    port = args.port

    # 确保依赖装了
    try:
        import curl_cffi  # noqa: F401
        import requests  # noqa: F401
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("[!] 缺少依赖，正在安装 curl-cffi / requests / fastapi / uvicorn ...")
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "curl-cffi>=0.7.0", "requests>=2.31.0",
            "fastapi>=0.110.0", "uvicorn[standard]>=0.27.0", "pydantic>=2.5.0",
        ])
        import curl_cffi  # noqa: F401
        import requests  # noqa: F401
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

    sys.path.insert(0, str(ROOT))
    import uvicorn

    # Sentinel QuickJS 路径依赖 Node，启动前检查，避免后续 OTP 静默丢失
    node_bin = (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"
    if not shutil.which(node_bin):
        print(
            f"\n[!] 未检测到 Node.js 可执行文件: {node_bin}\n"
            "    Sentinel QuickJS 方案需要 Node 环境，否则 OpenAI 会静默拒发 OTP。\n"
            "    请安装 Node.js 后重试，或通过环境变量 OPENAI_SENTINEL_NODE_PATH 指定路径。\n"
        )
        sys.exit(1)
    try:
        node_ver = subprocess.check_output([node_bin, "--version"], text=True, timeout=10).strip()
        print(f"\n✅ Node.js 已安装: {node_ver}")
    except Exception as e:
        print(f"\n[!] 检测到 Node 可执行文件但无法获取版本: {e}")
        sys.exit(1)

    ssl_keyfile = getattr(args, "ssl_keyfile", None)
    ssl_certfile = getattr(args, "ssl_certfile", None)
    scheme = "https" if ssl_keyfile and ssl_certfile else "http"
    url = f"{scheme}://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    mode = "auto-reload" if reload else "no-reload"
    print(f"\n🔔 团子喵 WebUI 启动中... ({mode})")
    print(f"   访问: {url}\n")
    if args.browser or not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        "webui.app:app",
        host=args.host,
        port=port,
        reload=reload,
        reload_dirs=[str(ROOT)] if reload else None,
        log_level="info",
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        timeout_graceful_shutdown=0,
    )


if __name__ == "__main__":
    main()
