"""Command line interface: ``yask serve`` (web UI), ``yask mcp`` (stdio)
and ``yask telegram`` (Telegram bot)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

DEFAULT_PORT = 4304  # 0x10D0


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))
    return Path(base) / "yask"


def _data_path(args) -> Path:
    if getattr(args, "data", None):
        return Path(args.data).expanduser()
    env = os.environ.get("YASK_DATA")
    if env:
        return Path(env).expanduser()
    return default_data_dir()


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    db_path = _data_path(args) / "yask.db"
    app = create_app(db_path)
    print(f"yask: serving web UI on http://{args.host}:{args.port} (data: {db_path})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_mcp(args) -> int:
    from . import db
    from .mcp_server import run_mcp
    from .store import Store

    conn = db.connect(_data_path(args) / "yask.db")
    run_mcp(Store(conn))
    return 0


def cmd_telegram(args) -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print(
            "yask: TELEGRAM_BOT_TOKEN is not set (get a bot from @BotFather and export it)",
            file=sys.stderr,
        )
        return 1
    from .telegram_bot import main as bot_main

    return bot_main(token, _data_path(args))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="yask", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the local web UI")
    s.add_argument("--port", type=int, default=int(os.environ.get("YASK_PORT", DEFAULT_PORT)))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--data", help="data directory (default: $YASK_DATA or ~/.local/share/yask)")
    s.set_defaults(func=cmd_serve)

    m = sub.add_parser("mcp", help="run the MCP server on stdio")
    m.add_argument("--data", help="data directory (default: $YASK_DATA or ~/.local/share/yask)")
    m.set_defaults(func=cmd_mcp)

    t = sub.add_parser("telegram", help="run the Telegram bot (long polling)")
    t.add_argument("--data", help="data directory (default: $YASK_DATA or ~/.local/share/yask)")
    t.set_defaults(func=cmd_telegram)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
