"""Settings loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    db_path: str = "data/tracker.db"

    # plans
    free_link_limit: int = 2
    premium_link_limit: int = 25
    free_interval: int = 1800
    premium_interval: int = 300
    owner_interval: int = 15
    premium_stars: int = 150
    premium_days: int = 30

    # Current public plans. The legacy PREMIUM_* settings stay available so
    # existing paid records keep their original entitlement after upgrade.
    pro_link_limit: int = 10
    pro_interval: int = 1200
    pro_stars: int = 50
    ultra_link_limit: int = 25
    ultra_interval: int = 600
    ultra_stars: int = 120
    subscription_days: int = 30
    trial_days: int = 7
    founding_enabled: bool = True
    founding_buyer_limit: int = 100
    founding_pro_stars: int = 25

    # fetching
    max_concurrency: int = 5
    host_min_gap: float = 2.0
    request_timeout: int = 15
    proxy_urls: tuple[str, ...] = ()

    # logging / monitoring
    log_dir: str = "logs"
    check_log_hours: int = 24  # how long per-check rows are kept in the database
    summary_seconds: int = 300  # how often the monitor logs its summary

    # affiliate
    amazon_tag: str = ""
    flipkart_affid: str = ""
    affiliate_template: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and fill it in.")
        owners = frozenset(int(x) for x in _split(os.getenv("OWNER_IDS", "")))
        if not owners:
            raise RuntimeError("OWNER_IDS is missing. Put your Telegram user id in .env.")
        return cls(
            bot_token=token,
            owner_ids=owners,
            db_path=os.getenv("DB_PATH", "data/tracker.db"),
            free_link_limit=_env_int("FREE_LINK_LIMIT", 2),
            premium_link_limit=_env_int("PREMIUM_LINK_LIMIT", 25),
            free_interval=_env_int("FREE_INTERVAL", 1800),
            premium_interval=_env_int("PREMIUM_INTERVAL", 300),
            owner_interval=_env_int("OWNER_INTERVAL", 15),
            premium_stars=_env_int("PREMIUM_STARS", 150),
            premium_days=_env_int("PREMIUM_DAYS", 30),
            pro_link_limit=_env_int("PRO_LINK_LIMIT", 10),
            pro_interval=_env_int("PRO_INTERVAL", 1200),
            pro_stars=_env_int("PRO_STARS", 50),
            ultra_link_limit=_env_int("ULTRA_LINK_LIMIT", 25),
            ultra_interval=_env_int("ULTRA_INTERVAL", 600),
            ultra_stars=_env_int("ULTRA_STARS", 120),
            subscription_days=_env_int("SUBSCRIPTION_DAYS", 30),
            trial_days=_env_int("TRIAL_DAYS", 7),
            founding_enabled=os.getenv("FOUNDING_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            founding_buyer_limit=_env_int("FOUNDING_BUYER_LIMIT", 100),
            founding_pro_stars=_env_int("FOUNDING_PRO_STARS", 25),
            max_concurrency=_env_int("MAX_CONCURRENCY", 5),
            host_min_gap=_env_float("HOST_MIN_GAP", 2.0),
            request_timeout=_env_int("REQUEST_TIMEOUT", 15),
            proxy_urls=tuple(_split(os.getenv("PROXY_URLS", ""))),
            log_dir=os.getenv("LOG_DIR", "logs").strip() or "logs",
            check_log_hours=_env_int("CHECK_LOG_HOURS", 24),
            summary_seconds=_env_int("SUMMARY_SECONDS", 300),
            amazon_tag=os.getenv("AMAZON_TAG", "").strip(),
            flipkart_affid=os.getenv("FLIPKART_AFFID", "").strip(),
            affiliate_template=os.getenv("AFFILIATE_TEMPLATE", "").strip(),
        )
