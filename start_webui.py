#!/usr/bin/env python3
"""One-command WebUI launcher: install dependencies, then run Uvicorn.

Usage::
    python start_webui.py             # default: 127.0.0.1:8765
    python start_webui.py --port 9000 # custom port
    python start_webui.py --host 0.0.0.0 --port 8765  # listen on the LAN
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path

# Force UTF-8 output for compatibility with Windows consoles using GBK.
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
    ap.add_argument("--host", default="127.0.0.1", help="Listen address (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765, help="Listen port (default: 8765)")
    ap.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    ap.add_argument("--reload", action="store_true", help="Development mode with automatic reloads")
    args = ap.parse_args()

    # Install optional launcher dependencies when missing.
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("[!] Required packages are missing; installing FastAPI and Uvicorn...")
        import subprocess
        subprocess.check_call([
            sys.executable, "-m", "pip", "install",
            "fastapi", "uvicorn[standard]", "pydantic>=2",
        ])
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401

    sys.path.insert(0, str(ROOT))
    import uvicorn

    url = f"http://{args.host if args.host != '0.0.0.0' else '127.0.0.1'}:{args.port}/"
    print("\n🔔 GPT Auto Register WebUI is starting...")
    print(f"   Open: {url}\n")

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    uvicorn.run(
        "webui.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
