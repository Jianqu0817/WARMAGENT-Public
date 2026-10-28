"""Single project entry; runtime mode must be chosen explicitly."""
import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def parser():
    root = argparse.ArgumentParser(prog="python -m warmagent")
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check", "bind", "chat", "web", "wechat", "dual", "versions"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path("config/bridge.local.toml"))
        if name in {"prepare", "check", "bind", "wechat", "dual", "versions"}:
            command.add_argument("--wechat-config", type=Path)
        if name in {'prepare', 'check', 'dual'}:
            command.add_argument('--onebot-config', type=Path)
        if name in {"chat", "web", "wechat", "dual"}:
            mode = command.add_mutually_exclusive_group(required=True)
            mode.add_argument("--offline", dest="mode", action="store_const", const="offline")
            mode.add_argument("--live", dest="mode", action="store_const", const="live")
        if name in {'chat', 'web'}:
            command.add_argument("--scope")
            command.add_argument("--ui", action="store_true", help="同时提供8766管理API和8767/8768观察页面")
        if name == 'web':
            command.add_argument('--port', type=int, default=8769, help='本地聊天网页端口，默认8769')
            command.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
        if name in {"wechat", "dual"}:
            command.add_argument("--observe-only", action="store_true", help="接收观察，不提交回复")
            command.add_argument("--ui", action="store_true", help="同时提供8767监视与8768上下文窗口")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    from . import launch
    logger = logging.getLogger("warmagent")
    previous_level = logger.level
    handler = None
    if args.command in {"wechat", "dual", "web"}:
        path = args.config.resolve().parent.parent / "var/logs/warmagent.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.info("WeChat service starting: mode=%s", args.mode)
    try:
        if args.command in {"prepare", "check", "versions"}:
            return getattr(launch, args.command)(args)
        asyncio.run(getattr(launch, args.command)(args))
    except KeyboardInterrupt:
        if handler:
            logger.info("Service interrupted by keyboard")
    except (OSError, ValueError, RuntimeError) as error:
        if handler:
            logger.exception("Service stopped with error")
        raise SystemExit(str(error)) from None
    except Exception:
        if handler:
            logger.exception("Service stopped with unexpected error")
        raise
    finally:
        if handler:
            logger.info("WeChat service stopped")
            logger.removeHandler(handler)
            handler.close()
            logger.setLevel(previous_level)


if __name__ == "__main__":
    main()
