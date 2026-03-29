"""
Prometheus — Main Orchestrator
Запускает всё. Ничего лишнего.
"""
from __future__ import annotations
import json
import signal as _signal
import time
from datetime import datetime, date, timezone
from anthropic import Anthropic

import logger as _logger
from config import cfg
from telegram import Telegram
from data import fetch_markets, Market
from risk import RiskManager
from signals import SignalEngine, EnsembleResult
from smart_money import SmartMoneyMonitor
from resolver import PositionResolver
from learning import LearningEngine
from intel import IntelPipeline
from intel_ext import ExtendedIntelPipeline

import logging
log = logging.getLogger("prometheus.main")


# ── Execution ──────────────────────────────────────────────────────────────────

def _execute(market: Market, direction: str,
             size_usd: float, price: float) -> bool:
    """Исполнить ордер. В DRY RUN — только логирование."""
    if cfg.dry_run:
        log.info(f"[DRY RUN] {direction} @ {price:.3f} ${size_usd:.2f} | "
                 f"{market.question[:55]}")
        return True

    if not cfg.poly_key:
        log.error("POLYMARKET_PRIVATE_KEY не задан")
        return False

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        client = ClobClient(
            "https://clob.polymarket.com",
            key=cfg.poly_key,
            chain_id=137,
            signature_type=1,
            funder=cfg.poly_funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())

        token = market.token_id_yes if direction == "YES" else market.token_id_no
        if not token:
            log.error("Нет token_id для исполнения")
            return False

        order  = MarketOrderArgs(token_id=token, amount=size_usd,
                                 side=BUY, order_type=OrderType.FOK)
        signed = client.create_market_order(order)
        result = client.post_order(signed, OrderType.FOK)
        log.info(f"Ордер исполнен: {result}")
        return True

    except ImportError:
        log.error("py-clob-client не установлен")
        return False
    except Exception as e:
        log.error(f"Execution error: {e}", exc_info=True)
        return False


# ── Bot ────────────────────────────────────────────────────────────────────────

class Prometheus:

    def __init__(self) -> None:
        cfg.validate()

        self.tg       = Telegram(cfg.tg_token, cfg.tg_chat)
        self.ai       = Anthropic(api_key=cfg.anthropic_key)

        self.risk     = RiskManager(
            max_position_usd   = cfg.max_pos_usd,
            max_daily_loss_usd = cfg.max_daily_loss,
            max_open_positions = cfg.max_open,
            kelly_fraction     = cfg.kelly_frac,
            data_dir           = cfg.logs_dir,
        )
        self.intel    = IntelPipeline(
            fred_api_key = cfg.fred_key,
            twitter_on   = cfg.twitter_on,
            data_dir     = cfg.logs_dir,
        )
        self.intel_ext = ExtendedIntelPipeline(
            db           = self.intel.db,
            odds_api_key = cfg.odds_key,
            trends_on    = cfg.trends_on,
        )
        self.signals   = SignalEngine(self.ai, self.intel)
        self.sm        = SmartMoneyMonitor(cfg.max_pos_usd)
        self.resolver  = PositionResolver(self.risk, self.tg.send)
        self.learning  = LearningEngine(cfg.logs_dir)

        # State
        self._shutdown:         bool            = False
        self._last_intel:       datetime | None = None
        self._daily_signals:    int             = 0
        self._daily_sm_alerts:  int             = 0
        self._last_report_date: date | None     = None
        self._last_limit_warn:  float           = 0.0

        # Graceful shutdown
        _signal.signal(_signal.SIGTERM, self._on_signal)
        _signal.signal(_signal.SIGINT,  self._on_signal)

        log.info(f"Prometheus инициализирован\n{cfg.summary()}")

    def _on_signal(self, signum: int, frame) -> None:
        log.info(f"Сигнал {signum} — завершаем после текущего цикла")
        self._shutdown = True

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.tg.started(cfg.dry_run)

        while not self._shutdown:
            try:
                self._maybe_daily_report()
                self._resolve_positions()
                self._run_intel()
                self._check_risk_alerts()
                self._trade_cycle()
                self._sleep(cfg.scan_interval)

            except Exception as e:
                log.error(f"Критическая ошибка: {e}", exc_info=True)
                self.tg.error(str(e))
                self._sleep(60)

        self.tg.stopped()
        log.info("Prometheus остановлен")

    # ── Steps ──────────────────────────────────────────────────────────────────

    def _resolve_positions(self) -> None:
        closed = self.resolver.run()
        for c in closed:
            new_weights = self.learning.record(
                market_id      = c["market_id"],
                signals        = [],
                direction      = c["direction"],
                market_price   = c["entry_price"],
                actual_outcome = c["outcome"],
                pnl            = c["pnl"],
            )
            if new_weights:
                self.signals.update_weights(new_weights)
                self.tg.weights_updated(new_weights)

    def _run_intel(self) -> None:
        now = datetime.now(timezone.utc)
        if (self._last_intel is None or
                (now - self._last_intel).total_seconds() > cfg.intel_interval):
            try:
                c1 = self.intel.run_full()
                c2 = self.intel_ext.run()
                total = sum(c1.values()) + sum(c2.values())
                log.info(f"📡 Intel: {total} новых DataPoints")
                self._last_intel = now

                # Арбитраж
                try:
                    markets = fetch_markets(limit=30, min_volume_24h=cfg.min_volume)
                    arb = self.intel_ext.find_arbitrage(
                        [{"question": m.question, "yes_price": m.yes_price, "id": m.id}
                         for m in markets]
                    )
                    if arb:
                        self.tg.arbitrage(arb[0].title, arb[0].content)
                except Exception as e:
                    log.debug(f"Arbitrage error: {e}")

                # Breaking news
                breaking = self.intel.breaking_news(hours=1)
                if breaking:
                    self.tg.breaking([dp.title for dp in breaking[:3]])

            except Exception as e:
                log.warning(f"Intel error: {e}")

    def _check_risk_alerts(self) -> None:
        snap = self.risk.snapshot()
        pct  = snap["daily_loss_pct"]
        now  = time.time()
        # Алерт не чаще раза в час
        if 0.75 <= pct < 0.90 and now - self._last_limit_warn > 3600:
            self.tg.limit_warning(pct)
            self._last_limit_warn = now

    def _trade_cycle(self) -> None:
        log.info("─" * 60)
        snap = self.risk.snapshot()

        if not snap["can_trade"]:
            log.warning(f"Торговля заблокирована | P&L: ${snap['daily_pnl']:.2f}")
            return

        ai_trades = sm_trades = 0

        # 1. Smart Money
        sm_signals = self.sm.scan()
        self._daily_sm_alerts += len(sm_signals)

        for sig in sm_signals[:3]:
            if not self.risk.snapshot()["can_trade"]:
                break
            decision = self.risk.check(sig.market_id, sig.our_size,
                                        list(sig.wallet.specializations))
            if not decision.allowed:
                log.info(f"SM risk block: {decision.reason}")
                continue

            self.tg.smart_money(sig.question, sig.direction,
                                sig.entry_price, decision.size_usd, sig.reasoning)

            # Smart money execution использует Market-like объект
            class _Mkt:
                question    = sig.question
                token_id_yes = None
                token_id_no  = None

            if _execute(_Mkt(), sig.direction, decision.size_usd, sig.entry_price):
                self.risk.open(sig.market_id, sig.question, sig.direction,
                               sig.entry_price, decision.size_usd,
                               list(sig.wallet.specializations), "smart_money")
                sm_trades += 1

        # 2. AI Ensemble
        markets = fetch_markets(limit=cfg.max_markets,
                                min_volume_24h=cfg.min_volume)
        self._daily_signals += len(markets)
        log.info(f"Рынков для анализа: {len(markets)}")

        for market in markets:
            if not self.risk.snapshot()["can_trade"]:
                break

            result: EnsembleResult = self.signals.analyze(market)
            log.info(
                f"  {market.question[:55]}\n"
                f"  → {result.direction} edge={result.edge:.2%} "
                f"prob={result.ai_probability:.2%} conf={result.confidence}"
            )

            if result.direction == "NEUTRAL":       continue
            if result.edge < cfg.min_edge:           continue
            if result.confidence == "low":           continue

            size     = self.risk.kelly_size(result.ai_probability,
                                            market.yes_price, result.direction,
                                            cfg.bankroll)
            decision = self.risk.check(market.id, size, list(market.tags))
            if not decision.allowed:
                log.info(f"AI risk block: {decision.reason}")
                continue

            price = market.yes_price if result.direction == "YES" else market.no_price

            self.tg.trade(market.question, result.direction, price,
                          decision.size_usd, result.edge, result.confidence,
                          result.reasoning, cfg.dry_run)

            if _execute(market, result.direction, decision.size_usd, price):
                token = (market.token_id_yes if result.direction == "YES"
                         else market.token_id_no)
                self.risk.open(market.id, market.question, result.direction,
                               price, decision.size_usd, list(market.tags),
                               "ai", token_id=token)
                ai_trades += 1

            time.sleep(1.5)

        snap = self.risk.snapshot()
        self.tg.cycle_summary(ai_trades, sm_trades, snap["daily_pnl"])
        self._push_to_api()

    def _push_to_api(self) -> None:
        """Отправить актуальные данные в API сервис."""
        api_url = os.environ.get("API_PUSH_URL", "")
        if not api_url:
            return
        try:
            import requests as _r
            snap     = self.risk.snapshot()
            open_pos = self.risk.open_positions
            closed   = [p for p in self.risk.closed_positions
                        if (p.closed_at or "")[:10] == datetime.now(timezone.utc).date().isoformat()]

            def fmt(p):
                return {
                    "id": p.market_id, "question": p.question,
                    "direction": p.direction, "price": p.entry_price,
                    "size": p.size_usd, "pnl": p.pnl or 0,
                    "age": "now", "tags": p.tags,
                    "status": p.status, "type": p.signal_type,
                }

            payload = {
                "overview": {
                    "bot_running":       True,
                    "dry_run":           cfg.dry_run,
                    "pnl_today":         snap["daily_pnl"],
                    "pnl_total":         snap["total_pnl"],
                    "win_rate":          snap["win_rate"],
                    "total_trades":      snap["total_trades"],
                    "open_positions":    snap["open_positions"],
                    "open_exposure":     snap["open_exposure"],
                    "bankroll":          cfg.bankroll,
                    "daily_loss_used":   snap["daily_loss_pct"],
                    "signals_today":     self._daily_signals,
                    "smart_money_today": self._daily_sm_alerts,
                },
                "positions": {
                    "open":         [fmt(p) for p in open_pos],
                    "closed_today": [fmt(p) for p in closed],
                },
            }

            _r.post(
                f"{api_url}/internal/push",
                json=payload,
                headers={"x-bot-key": cfg.dashboard_key},
                timeout=5,
            )
            log.info("📤 Данные отправлены в API")
        except Exception as e:
            log.debug(f"Push to API failed: {e}")

    def _maybe_daily_report(self) -> None:
        now   = datetime.now(timezone.utc)
        today = date.today()
        if now.hour == cfg.report_hour and self._last_report_date != today:
            self._last_report_date = today
            self.tg.daily_report(
                snap      = self.risk.snapshot(),
                by_tag    = self.risk.performance_by_tag(),
                signals   = self._daily_signals,
                sm_alerts = self._daily_sm_alerts,
            )
            self._daily_signals   = 0
            self._daily_sm_alerts = 0

    def _sleep(self, seconds: int) -> None:
        """Прерываемый sleep."""
        log.info(f"Следующий цикл через {seconds}с")
        for _ in range(seconds):
            if self._shutdown:
                break
            time.sleep(1)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _logger.setup(logs_dir=cfg.logs_dir)
    Prometheus().run()
