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
    anthropic_key:  str = _s("ANTHROPIC_API_KEY")
    poly_key:       str = _s("POLYMARKET_PRIVATE_KEY")
    poly_funder:    str = _s("POLYMARKET_FUNDER")
    tg_token:       str = _s("TELEGRAM_BOT_TOKEN")
    tg_chat:        str = _s("TELEGRAM_CHAT_ID")
    fred_key:       str = _s("FRED_API_KEY")
    odds_key:       str = _s("ODDS_API_KEY")
    dashboard_key:  str = _s("DASHBOARD_API_KEY")
    manual_close_key: str = _s("MANUAL_CLOSE_KEY")
    allowed_origins: str = _s("ALLOWED_ORIGINS", "*")

    api_push_url:   str   = _s("API_PUSH_URL")
    poly_sig_type:  str   = _s("POLYMARKET_SIGNATURE_TYPE", "0")
    clear_on_start: bool  = _b("CLEAR_POSITIONS_ON_START", False)

    dry_run:        bool  = _b("DRY_RUN", True)
    twitter_on:     bool  = _b("TWITTER_ON", True)
    trends_on:      bool  = _b("GOOGLE_TRENDS_ON", True)

    max_pos_usd:    float = _f("MAX_POSITION_USD", 10.0)
    max_daily_loss: float = _f("MAX_DAILY_LOSS_USD", 50.0)
    max_open:       int   = _i("MAX_OPEN_POSITIONS", 25)
    max_correlated: float = _f("MAX_CORRELATED_USD", 80.0)
    kelly_frac:     float = _f("KELLY_FRACTION", 0.20)

    min_edge:       float = _f("MIN_EDGE", 0.05)
    min_volume:     float = _f("MIN_VOLUME_24H", 10_000)
    low_liquidity_size_usd: float = _f("LOW_LIQUIDITY_SIZE_USD", 3.0)

    scan_interval:  int   = _i("SCAN_INTERVAL_SEC", 300)
    intel_interval: int   = _i("INTEL_INTERVAL_SEC", 900)
    max_markets:    int   = _i("MAX_MARKETS_PER_RUN", 60)
    report_hour:    int   = _i("DAILY_REPORT_HOUR", 9)
    bankroll:       float = _f("BANKROLL", 100.0)

    open_verify_interval_sec: int = _i("OPEN_VERIFY_INTERVAL_SEC", 60)
    close_retry_interval_sec: int = _i("CLOSE_RETRY_INTERVAL_SEC", 1800)
    max_order_retries: int = _i("MAX_ORDER_RETRIES", 10)
    ai_unconfirmed_edge: float = _f("AI_UNCONFIRMED_EDGE", 0.10)

    ai_mode:        str   = _s("AI_MODE", "minimal").lower()  # off|minimal|full
    ai_request_budget_per_cycle: int = _i("AI_REQUEST_BUDGET_PER_CYCLE", 20)
    ai_min_quality_for_call: int = _i("AI_MIN_QUALITY_FOR_CALL", 30)
    ai_high_quality_skip: int = _i("AI_HIGH_QUALITY_SKIP", 72)
    min_trade_quality: int = _i("MIN_TRADE_QUALITY", 42)
    enable_near_resolution: bool = _b("ENABLE_NEAR_RESOLUTION", False)

    strategy_min_trades: int = _i("STRATEGY_MIN_TRADES", 10)
    strategy_weak_win_rate: float = _f("STRATEGY_WEAK_WIN_RATE", 0.40)
    strategy_reenable_days: int = _i("STRATEGY_REENABLE_DAYS", 3)
    strategy_weakened_size_mult: float = _f("STRATEGY_WEAKENED_SIZE_MULT", 0.50)
    all_strategies_weak_min_usd: float = _f("ALL_STRATEGIES_WEAK_MIN_USD", 1.0)

    logs_dir:       str = _s("LOGS_DIR", "/app/logs")
    review_model:   str = _s("REVIEW_MODEL", "claude-sonnet-4-20250514")
    review_min_gain_usd:  float = _f("REVIEW_MIN_GAIN_USD", 3.0)
    review_min_loss_usd:  float = _f("REVIEW_MIN_LOSS_USD", 2.0)
    enrich_request_interval: float = _f("ENRICH_REQUEST_INTERVAL_SEC", 0.05)

    # Stop-loss, trailing stop, take-profit, time-based exit
    stop_loss_pct:            float = _f("STOP_LOSS_PCT", 0.40)
    trailing_stop_pct:        float = _f("TRAILING_STOP_PCT", 0.18)
    trailing_stop_min_gain:   float = _f("TRAILING_STOP_MIN_GAIN", 0.12)
    take_profit_price:        float = _f("TAKE_PROFIT_PRICE", 0.88)
    time_exit_hours:          float = _f("TIME_EXIT_HOURS", 2.0)
    time_exit_min_loss_pct:   float = _f("TIME_EXIT_MIN_LOSS_PCT", 0.20)

    def validate(self) -> None:
        if self.ai_mode not in {"off", "minimal", "full"}:
            raise ValueError("AI_MODE должен быть off|minimal|full")
        if self.ai_mode != "off" and not self.anthropic_key:
            raise ValueError("ANTHROPIC_API_KEY не задан в .env")
        if not self.dry_run and not self.poly_key:
            raise ValueError("DRY_RUN=false но POLYMARKET_PRIVATE_KEY не задан")
        if self.max_pos_usd <= 0:
            raise ValueError("MAX_POSITION_USD должен быть > 0")
        if self.kelly_frac <= 0 or self.kelly_frac > 1:
            raise ValueError("KELLY_FRACTION должен быть от 0 до 1")
        if self.max_order_retries < 1:
            raise ValueError("MAX_ORDER_RETRIES должен быть >= 1")
        if self.open_verify_interval_sec < 1 or self.close_retry_interval_sec < 60:
            raise ValueError("Интервалы повторов заданы некорректно")
        if self.ai_request_budget_per_cycle < 0:
            raise ValueError("AI_REQUEST_BUDGET_PER_CYCLE должен быть >= 0")
        if self.strategy_min_trades < 1:
            raise ValueError("STRATEGY_MIN_TRADES должен быть >= 1")
        if not self.dry_run and not self.poly_funder:
            log.warning("LIVE режим без POLYMARKET_FUNDER: подтверждение позиции на Polymarket будет ограничено")

    def summary(self) -> str:
        lines = [
            f"Mode:          {'DRY RUN' if self.dry_run else '🔴 LIVE TRADING'}",
            f"Max position:  ${self.max_pos_usd}",
            f"Max daily loss:${self.max_daily_loss}",
            f"Max open:      {self.max_open}",
            f"Kelly fraction:{self.kelly_frac}",
            f"Min edge:      {self.min_edge:.0%}",
            f"Trade quality: {self.min_trade_quality}",
            f"AI mode:       {self.ai_mode} (budget {self.ai_request_budget_per_cycle}/cycle)",
            f"Strategy ctl:  n={self.strategy_min_trades}, weaken x{self.strategy_weakened_size_mult:.2f}, reenable {self.strategy_reenable_days}d",
            f"Scan interval: {self.scan_interval}s",
            f"Open verify:   {self.open_verify_interval_sec}s",
            f"Close retry:   {self.close_retry_interval_sec}s x {self.max_order_retries}",
            f"Logs dir:      {self.logs_dir}",
        ]
        return "\n".join(lines)


cfg = Config()
