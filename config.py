"""
Prometheus — Config
Единственное место где читается конфиг.
Всё из env. Нет хардкода. Валидация при старте.
"""
from __future__ import annotations
import os
import logging

log = logging.getLogger("prometheus.config")


def _s(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _f(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        log.warning(f"Invalid float for {key}, using {default}")
        return default

def _i(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        log.warning(f"Invalid int for {key}, using {default}")
        return default

def _b(key: str, default: bool = False) -> bool:
    val = os.environ.get(key, str(default)).lower()
    return val in ("1", "true", "yes", "on")


class Config:
    """Иммутабельный конфиг. Читается один раз при старте."""

    # ── API Keys ──────────────────────────────────────────────────────────────
    anthropic_key:  str = _s("ANTHROPIC_API_KEY")
    poly_key:       str = _s("POLYMARKET_PRIVATE_KEY")
    poly_funder:    str = _s("POLYMARKET_FUNDER")
    tg_token:       str = _s("TELEGRAM_BOT_TOKEN")
    tg_chat:        str = _s("TELEGRAM_CHAT_ID")
    fred_key:       str = _s("FRED_API_KEY")
    odds_key:       str = _s("ODDS_API_KEY")
    dashboard_key:  str = _s("DASHBOARD_API_KEY")

    # ── Mode ──────────────────────────────────────────────────────────────────
    dry_run:        bool  = _b("DRY_RUN", True)
    twitter_on:     bool  = _b("TWITTER_ON", True)
    trends_on:      bool  = _b("GOOGLE_TRENDS_ON", True)

    # ── Risk ──────────────────────────────────────────────────────────────────
    max_pos_usd:    float = _f("MAX_POSITION_USD",    20.0)
    max_daily_loss: float = _f("MAX_DAILY_LOSS_USD",  50.0)
    max_open:       int   = _i("MAX_OPEN_POSITIONS",  5)
    kelly_frac:     float = _f("KELLY_FRACTION",      0.20)

    # ── Signals ───────────────────────────────────────────────────────────────
    min_edge:       float = _f("MIN_EDGE",            0.05)
    min_volume:     float = _f("MIN_VOLUME_24H",      10_000)

    # ── Timing ────────────────────────────────────────────────────────────────
    scan_interval:  int   = _i("SCAN_INTERVAL_SEC",   300)
    intel_interval: int   = _i("INTEL_INTERVAL_SEC",  900)
    max_markets:    int   = _i("MAX_MARKETS_PER_RUN", 30)
    report_hour:    int   = _i("DAILY_REPORT_HOUR",   9)
    bankroll:       float = _f("BANKROLL",            100.0)

    # ── Paths ─────────────────────────────────────────────────────────────────
    logs_dir:       str   = _s("LOGS_DIR", "/app/logs")

    def validate(self) -> None:
        """Проверить обязательные поля. Падаем сразу с понятной ошибкой."""
        if not self.anthropic_key:
            raise ValueError("ANTHROPIC_API_KEY не задан в .env")
        if not self.dry_run and not self.poly_key:
            raise ValueError("DRY_RUN=false но POLYMARKET_PRIVATE_KEY не задан")
        # POLYMARKET_FUNDER is optional - only needed for proxy wallet mode
        # For EOA trading (signature_type=0), funder is not required
        if self.max_pos_usd <= 0:
            raise ValueError("MAX_POSITION_USD должен быть > 0")
        if self.kelly_frac <= 0 or self.kelly_frac > 1:
            raise ValueError("KELLY_FRACTION должен быть от 0 до 1")

    def summary(self) -> str:
        lines = [
            f"Mode:          {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}",
            f"Max position:  ${self.max_pos_usd}",
            f"Max daily loss:${self.max_daily_loss}",
            f"Kelly fraction:{self.kelly_frac}",
            f"Min edge:      {self.min_edge:.0%}",
            f"Scan interval: {self.scan_interval}s",
            f"Intel interval:{self.intel_interval}s",
            f"FRED key:      {'✓' if self.fred_key else '✗'}",
            f"Odds key:      {'✓' if self.odds_key else '✗'}",
            f"Twitter:       {'on' if self.twitter_on else 'off'}",
            f"Telegram:      {'✓' if self.tg_token else '✗'}",
        ]
        return "\n".join(lines)


# Singleton — импортируется везде
cfg = Config()
