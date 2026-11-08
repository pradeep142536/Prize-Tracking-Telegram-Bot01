"""Logging configuration with secret redaction.

Two loggers:
  * root          normal application log (console + logs/bot.log)
  * "pricecheck"  one line per price-check attempt (console + logs/price_checks.log)

Every handler uses RedactingFormatter, so BOT_TOKEN, proxy passwords and anything that looks
like a Telegram token are masked even inside exception tracebacks.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from logging.handlers import RotatingFileHandler
from typing import Iterable

from .config import Settings

CHECK_LOGGER = "pricecheck"

# No \b at the start: Telegram API URLs contain "bot<token>", where a word boundary would not match.
_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_PROXY_CREDS_RE = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _TOKEN_RE.sub("[REDACTED_TOKEN]", text)
    return _PROXY_CREDS_RE.sub(r"\1***@", text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, datefmt: str | None = None, secrets: Iterable[str] = ()) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)  # includes any traceback text


def _secrets(settings: Settings) -> list[str]:
    values = [settings.bot_token]
    for proxy in settings.proxy_urls:
        values.append(proxy)
    return values


def setup_logging(settings: Settings) -> None:
    secrets = _secrets(settings)
    os.makedirs(settings.log_dir, exist_ok=True)

    # A Windows console may not be able to print every character (e.g. the rupee sign).
    # Replace what it can't show instead of crashing the logger.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    def file_handler(name: str) -> RotatingFileHandler:
        return RotatingFileHandler(
            os.path.join(settings.log_dir, name), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )

    app_fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", secrets=secrets)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(app_fmt)
    app_file = file_handler("bot.log")
    app_file.setFormatter(app_fmt)
    root.addHandler(console)
    root.addHandler(app_file)

    checks = logging.getLogger(CHECK_LOGGER)
    for handler in list(checks.handlers):
        checks.removeHandler(handler)
    checks.setLevel(logging.INFO)
    checks.propagate = False  # don't print every check twice
    line_console = logging.StreamHandler()
    line_console.setFormatter(RedactingFormatter("%(message)s", secrets=secrets))
    line_file = file_handler("price_checks.log")
    line_file.setFormatter(
        RedactingFormatter("%(asctime)s | %(levelname)s | %(message)s", "%Y-%m-%d %H:%M:%S", secrets=secrets)
    )
    checks.addHandler(line_console)
    checks.addHandler(line_file)
