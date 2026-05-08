"""
Prometheus — Main Orchestrator
Запускает всё. Ничего лишнего.
"""
from __future__ import annotations
import json
import os
import signal as _signal
import time
from collections import deque
from datetime import datetime, date, timezone
from pathlib import Path
from anthropic import Anthropic

import logger as _logger
from config import cfg
from telegram import Telegram
from data import fetch_markets, Market
from risk import RiskManager, PendingAction
from signals import SignalEngine, EnsembleResult, get_domain_min_edge, get_confidence_kelly_mult
from smart_money import SmartMoneyMonitor
from resolver import PositionResolver, fetch_polymarket_positions
from learning import LearningEngine
from intel import IntelPipeline
from intel_ext import ExtendedIntelPipeline
from screener import screen
from position_review import PositionReviewer
from domain_intel import DomainPrior, build_market_context, portfolio_kelly_size
from liquidity import is_market_liquid, check_liquidity, liquidity_size_mult
from strategy_control import StrategyControl
from api import store_get
from polymarket_client import PolymarketClient

import logging
log = logging.getLogger("prometheus.main")


# ── Dead-letter queue ──────────────────────────────────────────────────────────

def _dead_letter(
    market_id: str,
    question:  str,
    direction: str,
    size_usd:  float,
    price:     float,
    error:     str = "",
) -> None:
    """
    Фиксирует упавший ордер в dead_letter.jsonl для аудита и ручного восстановления.
    Вызывается когда _execute() вернул False в live-режиме.
    Файл доступен через Railway Volumes / docker mount — оператор видит все потери.
    """
    entry = {
        "ts":        datetime.now(timezone.utc).isoformat(),
        "market_id": market_id,
        "question":  question[:120],
        "direction": direction,
        "size_usd":  size_usd,
        "price":     price,
        "error":     error[:200],
    }
    try:
        path = Path(cfg.logs_dir) / "dead_letter.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.error(f"Dead letter write failed: {e}")
    log.warning(
        f"💀 Dead letter: {direction} ${size_usd:.2f} @ {price:.3f} "
        f"| {question[:60]} | err={error[:60]}"
    )


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
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType, BalanceAllowanceParams, AssetType
        from py_clob_client_v2.order_builder.constants import BUY

        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        clob_kwargs = dict(
            key=cfg.poly_key,
            chain_id=137,
            signature_type=sig_type,
        )
        if cfg.poly_funder and cfg.poly_funder.startswith("0x"):
            clob_kwargs["funder"] = cfg.poly_funder
        client = ClobClient("https://clob.polymarket.com", **clob_kwargs)

        import time as _time
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        log.info(f"L2 creds OK: {creds.api_key[:8]}... sig_type={sig_type}")
        _time.sleep(2)

        try:
            ba = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            log.info(f"pUSD balance-allowance: {ba}")
        except Exception as be:
            log.warning(f"Balance-allowance check error: {be}")

        token = market.token_id_yes if direction == "YES" else market.token_id_no
        if not token:
            log.error("Нет token_id для исполнения")
            return False

        order  = MarketOrderArgs(token_id=token, amount=size_usd,
                                 side=BUY, order_type=OrderType.FAK)
        result = client.create_and_post_market_order(order, order_type=OrderType.FAK)
        log.info(f"Ордер исполнен: {result}")
        return True

    except ImportError:
        log.error("py_clob_client_v2 не установлен")
        return False
    except Exception as e:
        log.error(f"Execution error: {e}", exc_info=True)
        return False


# ── Bot ────────────────────────────────────────────────────────────────────────

class Prometheus:

    def __init__(self) -> None:
        cfg.validate()

        self.tg       = Telegram(cfg.tg_token, cfg.tg_chat)
        self.ai       = Anthropic(api_key=cfg.anthropic_key) if cfg.ai_mode != "off" and cfg.anthropic_key else None

        self.risk     = RiskManager(
            max_position_usd   = cfg.max_pos_usd,
            max_daily_loss_usd = cfg.max_daily_loss,
            max_open_positions = cfg.max_open,
            max_correlated_usd = cfg.max_correlated,
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
        self.resolver  = PositionResolver(self.risk, self.tg)
        self.learning  = LearningEngine(cfg.logs_dir)

        # Восстанавливаем накопленные веса из предыдущей сессии
        _saved_weights = self.learning.load_last_weights()
        if _saved_weights:
            self.signals.update_weights(_saved_weights)
        else:
            log.info("🧠 Нет сохранённых весов — используем дефолты")

        # State
        self._shutdown:         bool            = False
        self._last_intel:       datetime | None = None
        self._daily_signals:    int             = 0
        self._daily_sm_alerts:  int             = 0
        self._last_report_date: date | None     = None
        self._last_limit_warn:  float           = 0.0
        self._signal_history:   deque           = deque(maxlen=50)  # история сигналов для дашборда
        self._last_breaking:    float           = 0.0  # время последнего breaking news цикла
        self._whale_alerts:     deque           = deque(maxlen=50)  # крупные необычные сделки
        self._whale_alert_keys: set[str]        = set()             # для O(1) дедупликации
        self._market_signals:   dict            = {}   # market_id → list[Signal] для learning
        self._reviewer = PositionReviewer(
            risk_manager = self.risk,
            ai           = self.ai,
            intel        = self.intel,
            telegram     = self.tg,
        )
        self._domain_prior = DomainPrior(cfg.logs_dir)
        self._pending_open_notifies: dict[str, dict] = {}
        self._poly_client  = PolymarketClient(cfg.poly_funder)
        self._real_bankroll: float = cfg.bankroll   # обновляется в _sync_balance()
        self._clob_usdc_balance:    float = 0.0     # реальный USDC в CLOB (с auth)
        self._clob_balance_synced_at: float = 0.0   # timestamp последней синхронизации
        self._fast_cycles_remaining: int  = 0       # адаптивный интервал: короткие циклы после сделки
        self._execute_cooldown: dict[str, float] = {}  # market_id → retry_after timestamp
        self._strategy_control = StrategyControl(
            cfg.logs_dir,
            min_trades=cfg.strategy_min_trades,
            weak_win_rate=cfg.strategy_weak_win_rate,
            reenable_days=cfg.strategy_reenable_days,
            weakened_mult=cfg.strategy_weakened_size_mult,
            all_weak_min_usd=cfg.all_strategies_weak_min_usd,
        )

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
                self._process_pending_orders()
                self._resolve_positions()
                self._review_positions()
                self._process_pending_orders()
                self._run_intel()
                self._check_risk_alerts()
                self._trade_cycle()

                # Breaking news триггер — дополнительный цикл без TG-спама
                breaking = self.intel.breaking_news(hours=0.1)
                if breaking and (time.time() - self._last_breaking) > 180:
                    log.info(f"🚨 Breaking: {breaking[0].title[:60]} — extra cycle")
                    self._resolve_positions()
                    self._trade_cycle()
                    self._last_breaking = time.time()

                # Адаптивный интервал: после сделки — 3 быстрых цикла (30 сек)
                # В тихое время — обычный интервал из конфига
                if self._fast_cycles_remaining > 0:
                    interval = max(30, cfg.scan_interval // 3)
                    self._fast_cycles_remaining -= 1
                    log.info(f"⚡ Fast cycle, осталось {self._fast_cycles_remaining}")
                else:
                    interval = cfg.scan_interval
                self._sleep(interval)

            except Exception as e:
                log.error(f"Критическая ошибка: {e}", exc_info=True)
                self.tg.error(str(e))
                self._sleep(60)

        self.tg.stopped()
        log.info("Prometheus остановлен")

    # ── Steps ──────────────────────────────────────────────────────────────────


    def _alert_and_pause(self, message: str) -> None:
        if not self.risk.is_paused:
            self.risk.pause()
        log.error(message)
        self.tg.error(message)


    def _notify_strategy_change(self, change: dict) -> None:
        if not change:
            return
        stats = change.get("stats", {})
        msg = (
            "🧭 *Strategy update*\n\n"
            f"*{change.get('strategy','unknown')}*\n"
            f"`{change.get('from','?')}` → *{change.get('to','?')}*\n"
            f"Причина: `{change.get('reason','')}`\n"
            f"Trades: *{stats.get('trades', 0)}* | WR: *{stats.get('win_rate', 0):.0%}*\n"
            f"PnL: *${stats.get('pnl', 0):+.2f}* | ROI: *{stats.get('roi', 0):+.1%}*"
        )
        self.tg.send(msg)

    def _register_open_after_execution(
        self,
        *,
        market_id: str,
        question: str,
        direction: str,
        entry_price: float,
        size_usd: float,
        tags: list,
        signal_type: str,
        token_id: str | None,
        slug: str | None,
        url: str = "",
        notify_kwargs: dict,
    ) -> None:
        if cfg.dry_run:
            self.risk.open(
                market_id, question, direction, entry_price, size_usd,
                tags, signal_type, token_id=token_id, slug=slug, url=url,
            )
            self.tg.position_opened(**notify_kwargs)
            return

        if not token_id or not cfg.poly_funder:
            self._alert_and_pause(
                f"Не могу подтвердить открытие: token_id/funder отсутствуют | {question[:80]}"
            )
            return

        self.risk.register_pending_open(
            market_id=market_id,
            question=question,
            direction=direction,
            entry_price=entry_price,
            size_usd=size_usd,
            tags=tags,
            signal_type=signal_type,
            token_id=token_id,
            slug=slug,
            url=url,
            max_attempts=cfg.max_order_retries,
            interval_sec=cfg.open_verify_interval_sec,
        )
        self._pending_open_notifies[market_id] = notify_kwargs

    def _process_pending_opens(self) -> None:
        if cfg.dry_run or not cfg.poly_funder:
            return
        pending = self.risk.pending_open_actions
        if not pending:
            return

        # Используем PolymarketClient (кэшированный, thread-safe) вместо прямого вызова
        real_positions = self._poly_client.positions_dict()
        now_ts = time.time()

        log.info(f"Pending opens: {len(pending)}, on-chain positions: {len(real_positions)}")
        for action in pending:
            if not action.due(now_ts):
                continue
            real_pos = real_positions.get(action.token_id or "")
            try:
                real_size = float(real_pos.get("size", 0) or 0) if real_pos else 0.0
            except (TypeError, ValueError):
                real_size = 0.0
            log.info(
                f"  Verify {action.question[:50]} | token={str(action.token_id or '')[:12]} "
                f"| on-chain size={real_size:.4f} | attempt {action.attempts+1}/{action.max_attempts}"
            )
            if real_size > 0.01:
                self.risk.open(
                    action.market_id, action.question, action.direction,
                    action.entry_price, action.size_usd, action.tags,
                    action.signal_type, token_id=action.token_id, slug=action.slug,
                    url=getattr(action, 'url', ''),
                )
                notify_kwargs = self._pending_open_notifies.pop(action.market_id, None)
                if notify_kwargs:
                    ok = self.tg.position_opened(**notify_kwargs)
                    log.info(f"  📤 Telegram position_opened sent: {ok}")
                else:
                    # notify_kwargs lost on restart — rebuild from action and send anyway
                    log.info(f"  📤 Telegram notify_kwargs missing (restart) — sending from action data")
                    self.tg.position_opened(
                        question    = action.question,
                        direction   = action.direction,
                        price       = action.entry_price,
                        size        = action.size_usd,
                        edge        = 0.0,
                        confidence  = "medium",
                        reasoning   = "",
                        dry_run     = False,
                        signal_type = action.signal_type,
                        url         = getattr(action, 'url', ''),
                    )
                self.risk.remove_pending_action(action.market_id, "open_verify")
                log.info(f"✅ Подтверждено открытие на Polymarket: {action.question[:60]}")
                continue

            self.risk.mark_action_attempt(
                action,
                error="position_not_visible_on_polymarket",
                due_in=cfg.open_verify_interval_sec,
            )
            if action.attempts >= action.max_attempts:
                self.risk.remove_pending_action(action.market_id, "open_verify")
                self._pending_open_notifies.pop(action.market_id, None)
                # Before pausing, check if market resolved — fast-resolving markets
                # (sports games, breaking events) disappear from "open positions"
                # the moment they resolve, which is NOT a failure
                try:
                    from resolver import get_market_info, check_market_outcome, _yes_price_from_info
                    info = get_market_info(action.market_id)
                    outcome = check_market_outcome(action.market_id,
                                                   current_price=_yes_price_from_info(info),
                                                   market_data=info)
                    if outcome is not None:
                        log.info(f"Open verify: market resolved as {outcome} before confirmation — not an error: {action.question[:60]}")
                        return
                except Exception:
                    pass
                self._alert_and_pause(
                    f"Не удалось подтвердить открытие сделки после {action.max_attempts} попыток: {action.question[:80]}"
                )

    def _process_pending_orders(self) -> None:
        self._process_pending_opens()
        try:
            pending_closed = self.resolver.retry_pending_closures()
            if pending_closed:
                self._push_to_api()
        except Exception as e:
            log.warning(f"Pending close retry error: {e}")
        try:
            self.resolver.retry_pending_redeems()
        except Exception as e:
            log.warning(f"Pending redeem retry error: {e}")

    def _resolve_positions(self) -> None:
        closed = self.resolver.run()
        if closed:
            self._poly_client.invalidate()  # positions changed, refresh cache
        for c in closed:
            # Получаем сохранённые сигналы по market_id (если есть)
            saved_signals = self._market_signals.get(c["market_id"], [])
            # Ищем question_type из истории сигналов
            q_type = "general"
            for sh in self._signal_history:
                if sh.get("question") == c.get("question"):
                    q_type = sh.get("question_type", "general")
                    break
            new_weights = self.learning.record(
                market_id      = c["market_id"],
                signals        = saved_signals,
                direction      = c["direction"],
                market_price   = c["entry_price"],
                actual_outcome = c["outcome"],
                pnl            = c["pnl"],
                question_type  = q_type,
            )
            if new_weights:
                self.signals.update_weights(new_weights)
                log.info(f"Weights updated: {new_weights}")
            # Обновляем байесовский prior по доменам
            _tags = c.get("tags", [])
            _prev_mults = {
                d: self._domain_prior.get_size_multiplier([d])
                for d in _tags
            }
            self._domain_prior.record(
                tags = _tags,
                won  = c["won"],
                pnl  = c["pnl"],
                size = c.get("size_usd", 0),
            )
            # Уведомление если множитель значительно изменился
            for d in _tags:
                new_m = self._domain_prior.get_size_multiplier([d])
                old_m = _prev_mults.get(d, 1.0)
                # Уведомляем при переходе через пороги 1.2x / 0.7x
                crossed = (
                    (old_m < 1.2 and new_m >= 1.2) or
                    (old_m > 1.2 and new_m < 1.2) or
                    (old_m > 0.7 and new_m <= 0.7) or
                    (old_m < 0.7 and new_m > 0.7)
                )
                if crossed and abs(new_m - old_m) > 0.05:
                    log.info(f"Domain shift: {d} {old_m:.2f}x → {new_m:.2f}x")
            change = self._strategy_control.record_close(
                market_id=c["market_id"],
                strategy_type=c.get("signal_type", "market_data"),
                pnl=c["pnl"],
                won=c["won"],
                size_usd=c.get("size_usd", 0.0),
            )
            if change:
                self._notify_strategy_change(change)
            for reenabled in self._strategy_control.refresh_states():
                self._notify_strategy_change(reenabled)
            # Чистим после использования
            self._market_signals.pop(c["market_id"], None)

    def _review_positions(self) -> None:
        """Динамическая переоценка открытых позиций."""
        if not self._reviewer.should_run():
            return
        try:
            actions = self._reviewer.run()
            if actions:
                self._push_to_api()  # обновляем дашборд сразу
        except Exception as e:
            log.warning(f"Position review error: {e}")

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
                        log.info(f"Arbitrage: {arb[0].title}")
                except Exception as e:
                    log.debug(f"Arbitrage error: {e}")

                # Cross-market correlation check (log only, no Telegram)
                try:
                    corr_pairs = self.signals.find_correlated_markets(markets)
                    if corr_pairs:
                        for m1, m2, spread in corr_pairs[:2]:
                            log.info(
                                f"📊 Correlation gap: {m1.question[:40]} ({m1.yes_price:.0%}) "
                                f"vs {m2.question[:40]} ({m2.yes_price:.0%}) "
                                f"spread={spread:.0%}"
                            )
                except Exception as e:
                    log.debug(f"Correlation: {e}")

            except Exception as e:
                log.warning(f"Intel error: {e}")

    def _fetch_clob_balance(self) -> float:
        """
        Получить реальный USDC-баланс из Polymarket CLOB через авторизованный API.
        Возвращает 0.0 если не настроен приватный ключ или произошла ошибка.
        Вызывать не чаще раза в 5 минут — создание кредов занимает ~2 сек.
        """
        if not cfg.poly_key or not cfg.poly_funder:
            return 0.0
        try:
            from py_clob_client_v2.client import ClobClient
            from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
            sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
            kwargs: dict = {"key": cfg.poly_key, "chain_id": 137, "signature_type": sig_type}
            if cfg.poly_funder.startswith("0x"):
                kwargs["funder"] = cfg.poly_funder
            client = ClobClient("https://clob.polymarket.com", **kwargs)
            creds = client.create_or_derive_api_key()
            client.set_api_creds(creds)
            ba = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            # pUSD has 6 decimal places — divide by 1e6 to get dollars
            result = float(ba.get("balance", 0)) / 1e6 if isinstance(ba, dict) else 0.0
            if result > 0:
                log.info(f"💵 CLOB pUSD balance: ${result:.2f}")
            return result
        except ImportError:
            return 0.0
        except Exception as e:
            log.debug(f"_fetch_clob_balance error: {e}")
            return 0.0

    def _sync_balance(self) -> None:
        """
        Синхронизировать реальный баланс с Polymarket.

        Приоритет источников (от лучшего к худшему):
          1. Реальный USDC из CLOB (требует POLYMARKET_PRIVATE_KEY) — раз в 5 мин.
             _real_bankroll = clob_usdc + текущая стоимость позиций
          2. Оценка по Data API (публичный, не требует ключа):
             _real_bankroll = cfg.bankroll + realized_pnl + unrealised_pnl
          3. Только внутренний учёт (нет poly_funder):
             _real_bankroll = cfg.bankroll + realized_pnl
        """
        risk_snap    = self.risk.snapshot()
        realized_pnl = risk_snap.get("total_pnl", 0.0)
        now          = time.time()

        # ── Приоритет 1: реальный USDC из CLOB (TTL = 5 мин) ─────────────────
        if cfg.poly_key and cfg.poly_funder and (now - self._clob_balance_synced_at > 300):
            usdc = self._fetch_clob_balance()
            if usdc > 0:
                self._clob_usdc_balance   = usdc
                self._clob_balance_synced_at = now

        if self._clob_usdc_balance > 0:
            snap = self._poly_client.portfolio()
            # Total assets = USDC cash in CLOB + current market value of positions
            new_bankroll = max(1.0, self._clob_usdc_balance + snap.positions_value)
            if abs(new_bankroll - self._real_bankroll) > 0.5:
                log.info(
                    f"💰 Bankroll (CLOB): ${self._real_bankroll:.2f} → ${new_bankroll:.2f} "
                    f"(USDC ${self._clob_usdc_balance:.2f} + positions ${snap.positions_value:.2f})"
                )
            self._real_bankroll = new_bankroll
            return

        # ── Приоритет 2: оценка по Data API (публичный, требует poly_funder) ───
        try:
            upnl_est = 0.0
            if cfg.poly_funder:
                snap     = self._poly_client.portfolio()
                upnl_est = snap.unrealised_pnl
                internal_count = len(self.risk.open_positions)
                poly_count     = snap.position_count
                if internal_count != poly_count and poly_count > 0:
                    log.warning(
                        f"⚠️ Рассинхронизация позиций: внутри={internal_count}, "
                        f"Polymarket={poly_count}"
                    )
            else:
                # Без poly_funder — считаем uPnL из CLOB midpoint по каждой позиции
                # (публичный API, авторизация не нужна)
                for pos in self.risk.open_positions:
                    if not pos.token_id:
                        continue
                    cur = self._poly_client.token_price(pos.token_id)
                    if cur and cur > 0 and pos.entry_price > 0:
                        upnl_est += (cur - pos.entry_price) / max(pos.entry_price, 0.01) * pos.size_usd

            old_bankroll = self._real_bankroll
            self._real_bankroll = max(1.0, cfg.bankroll + realized_pnl + upnl_est)
            if abs(self._real_bankroll - old_bankroll) > 0.50:
                log.info(
                    f"💰 Bankroll: ${old_bankroll:.2f} → ${self._real_bankroll:.2f} "
                    f"(realized ${realized_pnl:+.2f}, uPnL ${upnl_est:+.2f})"
                )
        except Exception as e:
            log.debug(f"_sync_balance error: {e}")

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
        self._sync_balance()   # актуализируем bankroll перед Kelly sizing
        snap = self.risk.snapshot()

        if not snap["can_trade"]:
            log.warning(f"Торговля заблокирована | P&L: ${snap['daily_pnl']:.2f}")
            self._push_to_api()  # heartbeat must fire even when paused
            return

        ai_trades = sm_trades = 0
        self.signals.reset_cycle_budget()
        for reenabled in self._strategy_control.refresh_states():
            self._notify_strategy_change(reenabled)

        # 1. Smart Money — read Athena external signals directly from store (no HTTP round-trip)
        athena_data: dict | None = store_get("_athena_external") or None

        _open_athena = [
            {"market_id": p.market_id, "source": "athena", "question": p.question}
            for p in self.risk.open_positions
            if p.signal_type == "athena"
        ]
        sm_signals = self.sm.scan(athena_data=athena_data, open_positions=_open_athena)

        # Handle Athena exit signals before buy logic
        _exit_sigs = [s for s in sm_signals if s.signal_type == "athena_exit"]
        sm_signals  = [s for s in sm_signals if s.signal_type != "athena_exit"]
        for esig in _exit_sigs:
            pos = next(
                (p for p in self.risk.open_positions
                 if p.market_id == esig.market_id and p.signal_type == "athena"),
                None,
            )
            if pos:
                self.risk.register_pending_close(
                    pos=pos, reason="athena_exit_copy",
                    max_attempts=cfg.max_order_retries,
                    interval_sec=cfg.close_retry_interval_sec,
                )
                self.risk._audit("ATHENA_EXIT", pos.market_id, pos.size_usd, "athena_exit_copy")
                log.info(f"🏛 Athena EXIT copy registered | {pos.question[:60]}")
                self.tg.athena_exit_signal(
                    question = pos.question,
                    wallet   = getattr(esig.wallet, "address", ""),
                    tier     = esig.reasoning.split("tier-")[1][0] if "tier-" in esig.reasoning else "",
                )

        self._daily_sm_alerts += len(sm_signals)

        # Собираем whale alerts — все крупные сделки для дашборда
        from data import fetch_large_trades
        large_trades = fetch_large_trades(min_size=3000, hours_back=6)
        for t in large_trades[:30]:
            key = f"{t.market_id}_{t.side}_{round(t.size)}"
            if key in self._whale_alert_keys:
                continue
            alert = {
                "question":  t.question or t.market_id,
                "direction": "YES" if t.side == "BUY" else "NO",
                "price":     round(t.price, 3),
                "size":      round(t.size, 0),
                "wallet":    (t.maker or "")[:8] + "…" if t.maker else "unknown",
                "time":      t.timestamp.strftime("%H:%M") if t.timestamp else "",
                "type":      "whale",
                "_key":      key,  # сохраняем для синхронизации ключей
            }
            # Проверяем тип кошелька
            if t.maker and t.maker in self.sm.known_wallets:
                profile = self.sm.known_wallets[t.maker]
                if profile.trader_class.value != "noise":
                    alert["type"] = profile.trader_class.value
            self._whale_alerts.append(alert)
            self._whale_alert_keys.add(key)
        # Синхронизируем ключи с живым содержимым deque — удаляем вытесненные записи
        self._whale_alert_keys = {a["_key"] for a in self._whale_alerts if "_key" in a}

        for sig in sm_signals[:3]:
            if not self.risk.snapshot()["can_trade"]:
                break
            decision = self.risk.check(sig.market_id, sig.our_size,
                                        list(sig.wallet.specializations),
                                        question=sig.question)
            if not decision.allowed:
                log.info(f"SM risk block: {decision.reason}")
                continue

            # Smart money execution — используем token_id из сигнала
            class _Mkt:
                question     = sig.question
                token_id_yes = sig.token_id if sig.direction == "YES" else None
                token_id_no  = sig.token_id if sig.direction == "NO"  else None

            _sm_url = (f"https://polymarket.com/event/{sig.slug}" if sig.slug else "")
            _is_athena = getattr(sig, "signal_type", "") == "athena"
            if _execute(_Mkt(), sig.direction, decision.size_usd, sig.entry_price):
                sm_trades += 1
                if _is_athena:
                    self.sm.mark_athena_executed(sig.market_id)
                self._register_open_after_execution(
                    market_id=sig.market_id,
                    question=sig.question,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    size_usd=decision.size_usd,
                    tags=list(sig.wallet.specializations),
                    signal_type="athena" if _is_athena else "smart_money",
                    token_id=sig.token_id or None,
                    slug=sig.slug or None,
                    url=_sm_url,
                    notify_kwargs={
                        "question": sig.question,
                        "direction": sig.direction,
                        "price": sig.entry_price,
                        "size": decision.size_usd,
                        "edge": 0.0,
                        "confidence": "high",
                        "reasoning": sig.reasoning,
                        "dry_run": cfg.dry_run,
                        "signals": None,
                        "signal_type": "smart_money",
                        "url": _sm_url,
                        "domain_mult": 1.0,
                        "timing": "",
                    },
                )
                if _is_athena:
                    _tier = "S" if sig.wallet.win_rate >= 0.70 else ("A" if sig.wallet.win_rate >= 0.60 else "B")
                    self.tg.athena_opened(
                        question   = sig.question,
                        direction  = sig.direction,
                        price      = sig.entry_price,
                        their_size = sig.their_size,
                        our_size   = decision.size_usd,
                        tier       = _tier,
                        wallet     = sig.wallet.address,
                        reasoning  = sig.reasoning,
                        dry_run    = cfg.dry_run,
                        url        = (f"https://polymarket.com/event/{sig.slug}" if sig.slug else ""),
                    )
                else:
                    self.tg.insider_detected(
                        question     = sig.question,
                        direction    = sig.direction,
                        price        = sig.entry_price,
                        their_size   = sig.their_size,
                        our_size     = decision.size_usd,
                        trader_class = sig.wallet.trader_class.value,
                        win_rate     = sig.wallet.win_rate,
                        roi          = sig.wallet.roi,
                        reasoning    = sig.reasoning,
                        url          = (f"https://polymarket.com/event/{sig.slug}" if sig.slug else ""),
                    )
            elif not cfg.dry_run:
                _dead_letter(
                    market_id = sig.market_id,
                    question  = sig.question,
                    direction = sig.direction,
                    size_usd  = decision.size_usd,
                    price     = sig.entry_price,
                    error     = "execute_failed_smart_money",
                )
                now_ts = time.time()
                if sig.market_id not in self._execute_cooldown or now_ts >= self._execute_cooldown[sig.market_id]:
                    self.tg.error(f"Ордер упал (smart money): {sig.question[:60]}")
                self._execute_cooldown[sig.market_id] = now_ts + 1800  # 30 min cooldown

        # 2. Двухэтапный анализ рынков
        # Этап 1: быстрый скрининг 100 рынков без AI
        all_markets = fetch_markets(
            limit          = 100,  # смотрим широко
            min_volume_24h = cfg.min_volume,
        )
        self._daily_signals += len(all_markets)
        log.info(f"Рынков загружено: {len(all_markets)} (из 100)")

        # Этап 2: анализ кандидатов. AI подключается редко внутри SignalEngine.
        top_n = max(cfg.max_markets, 30)
        candidates = screen(all_markets, top_n=top_n, fetch_kalshi=False)
        log.info(f"После скрининга: {len(candidates)} кандидатов для decision engine")

        near_res = self._find_near_resolution(all_markets) if cfg.enable_near_resolution else []
        if near_res:
            log.info(f"Near-resolution markets: {len(near_res)}")

        now_ts = time.time()
        # Expire stale cooldown entries
        self._execute_cooldown = {k: v for k, v in self._execute_cooldown.items() if v > now_ts}

        for cand in candidates:
            market = cand.market
            if not self.risk.snapshot()["can_trade"]:
                break

            # Skip markets that recently failed execution (30 min cooldown)
            if market.id in self._execute_cooldown:
                log.debug(f"Execute cooldown active, skipping {market.question[:55]}")
                continue

            result: EnsembleResult = self.signals.analyze(market, pre_score=cand.pre_score)
            log.info(
                f"  {market.question[:55]}\n"
                f"  pre={cand.pre_score:.2f} → {result.direction} "
                f"edge={result.edge:.2%} prob={result.ai_probability:.2%} "
                f"conf={result.confidence} quality={result.trade_quality} "
                f"strategy={result.strategy_type} ai={'yes' if result.ai_used else 'no'} | {cand.reason}"
            )

            # Записываем сигнал в историю
            self._signal_history.append({
                "time":          datetime.now(timezone.utc).strftime("%H:%M"),
                "type":          "ensemble",
                "question":      market.question,
                "direction":     result.direction,
                "price":         round(market.yes_price, 3),
                "edge":          round(result.edge, 4),
                "confidence":    result.confidence,
                "prob":          round(result.ai_probability, 3),
                "detail":        f"{result.reasoning[:120]} | Pre-score: {cand.pre_score:.2f} ({cand.reason})",
                "url":           market.polymarket_url,
                "traded":        False,
                "pre_score":     cand.pre_score,
                "question_type": result.question_type,
            })
            # deque(maxlen=50) автоматически отбрасывает старые записи

            if result.direction == "NEUTRAL":
                continue

            if result.trade_quality < cfg.min_trade_quality:
                log.info(f"  Quality {result.trade_quality} < min {cfg.min_trade_quality} — skip")
                continue

            # Adaptive min_edge по домену (крипто=10%, политика=4%)
            domain_min_edge = get_domain_min_edge(list(market.tags), cfg.min_edge)
            if result.edge < domain_min_edge:
                log.info(f"  Edge {result.edge:.1%} < domain_min {domain_min_edge:.1%} — skip")
                continue

            # Low confidence: cross-market с высоким edge или market_data с уменьшенным размером
            _ai_size_mult = 1.0  # default; may be overridden below
            if result.confidence == "low":
                if result.strategy_type == "cross_market_gap" and result.edge >= max(0.12, domain_min_edge):
                    log.info(f"  Cross-market override: edge={result.edge:.1%} conf=low")
                elif result.edge >= domain_min_edge and result.trade_quality >= max(cfg.min_trade_quality - 10, 40):
                    _ai_size_mult = 0.60
                    log.info(f"  Low-conf exploratory: edge={result.edge:.1%} quality={result.trade_quality}, size×0.60")
                else:
                    continue

            if result.strategy_type == "ai_assisted":
                if result.edge < max(cfg.ai_unconfirmed_edge, domain_min_edge + 0.02):
                    log.info(f"  AI-assisted edge too small ({result.edge:.1%}) — skip")
                    continue
                _min_quality = max(cfg.min_trade_quality + 15, 65)
                if result.trade_quality < _min_quality:
                    log.info(f"  AI-assisted quality {result.trade_quality} < {_min_quality} — skip")
                    continue
                _ai_size_mult = 0.50
                log.info(
                    f"  AI-assisted entry: edge={result.edge:.1%}, "
                    f"quality={result.trade_quality} — size ×0.50"
                )

            strategy_mult = self._strategy_control.size_multiplier(result.strategy_type)
            if strategy_mult <= 0:
                log.info(f"  Strategy {result.strategy_type} disabled by strategy control — skip")
                continue

            # Строим контекст рынка (domain prior, timing, microstructure)
            token_id_for_ctx = (market.token_id_yes if result.direction == "YES"
                                else market.token_id_no)
            mctx = build_market_context(
                tags          = list(market.tags),
                token_id      = token_id_for_ctx or "",
                current_price = market.yes_price,
                domain_prior  = self._domain_prior,
                intel_db      = self.intel.db,
            )
            log.info(f"  Context: {mctx.summary()}")

            # Калибровка вероятности: сжимаем claimed edge к market price
            # пропорционально доверию к track record (shrinkage 0.50–0.95).
            # Компенсирует систематическое завышение ensemble-оценок.
            _shrinkage = self.learning.probability_shrinkage()
            _cal_prob = market.yes_price + (result.ai_probability - market.yes_price) * _shrinkage
            _cal_prob = max(0.01, min(0.99, _cal_prob))
            if _shrinkage < 0.90:
                log.info(
                    f"  Prob shrinkage ×{_shrinkage:.2f}: "
                    f"{result.ai_probability:.3f} → {_cal_prob:.3f} "
                    f"(n={len(self.learning._records)} records)"
                )

            # Ликвидность: получаем снэпшот один раз — используем и для блокировки
            # и для непрерывного Kelly-множителя (тонкий рынок → меньший размер)
            _token_for_liq = (market.token_id_yes if result.direction == "YES"
                              else market.token_id_no)
            _liq_snap = check_liquidity(_token_for_liq) if _token_for_liq else None
            if _liq_snap is not None and not _liq_snap.is_liquid:
                log.info(f"  Liquidity block: {_liq_snap.reason}")
                continue
            _liq_mult = liquidity_size_mult(_liq_snap) if _liq_snap else 1.0
            if _liq_mult < 0.99:
                log.info(f"  Liquidity mult ×{_liq_mult:.2f} "
                         f"(spread={_liq_snap.spread:.3f}, "
                         f"depth=${min(_liq_snap.bid_depth_100, _liq_snap.ask_depth_100):.0f})")

            # Portfolio Kelly с domain + liquidity multiplier
            size = portfolio_kelly_size(
                ai_probability    = _cal_prob,
                market_price      = market.yes_price,
                direction         = result.direction,
                bankroll          = self._real_bankroll,
                kelly_fraction    = cfg.kelly_frac,
                open_positions    = self.risk.open_positions,
                max_position_usd  = cfg.max_pos_usd,
                domain_multiplier = mctx.total_size_multiplier * _liq_mult,
            )

            # Плавный sizing по confidence (high=100%, medium=70%, low=40%)
            conf_mult = get_confidence_kelly_mult(result.confidence)
            if conf_mult < 1.0:
                size = size * conf_mult
                log.info(f"  Confidence mult {conf_mult:.0%} ({result.confidence}): ${size:.2f}")

            # Microstructure confirmation: если сигнал против нас — уменьшаем
            if (mctx.microstructure and
                mctx.microstructure.direction != "NEUTRAL" and
                mctx.microstructure.direction != result.direction and
                mctx.microstructure.confidence > 0.40):
                size = size * 0.6
                log.info(f"  Micro contra-signal ({mctx.microstructure.reasoning}): size reduced")

            if strategy_mult < 1.0:
                size = max(1.0, size * strategy_mult)
                log.info(f"  Strategy control mult {strategy_mult:.0%}: ${size:.2f}")

            if _ai_size_mult < 1.0:
                size = size * _ai_size_mult
                log.info(f"  AI-assisted size ×{_ai_size_mult:.2f}: ${size:.2f}")

            if self._strategy_control.all_primary_strategies_weak():
                size = min(max(1.0, size), self._strategy_control.all_weak_min_usd)
                log.info(f"  All primary strategies weak: minimal exploratory size ${size:.2f}")

            # Time-to-close decay: короткий горизонт = выше execution/resolution риск
            if market.end_date:
                try:
                    _end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                    _days_left = (_end - datetime.now(timezone.utc)).total_seconds() / 86400
                    if _days_left < 1:
                        _time_mult = 0.30
                    elif _days_left < 3:
                        _time_mult = 0.55
                    elif _days_left < 7:
                        _time_mult = 0.75
                    else:
                        _time_mult = 1.0
                    if _time_mult < 1.0:
                        size = size * _time_mult
                        log.info(f"  Time decay ×{_time_mult:.2f} ({_days_left:.1f}d left): ${size:.2f}")
                except Exception:
                    _time_mult = 1.0  # safe default if end_date malformed

            size = min(cfg.max_pos_usd, max(1.0, size))

            decision = self.risk.check(market.id, size, list(market.tags),
                                       question=market.question)
            if not decision.allowed:
                log.info(f"AI risk block: {decision.reason}")
                continue

            price = market.yes_price if result.direction == "YES" else market.no_price

            if _execute(market, result.direction, decision.size_usd, price):
                token = (market.token_id_yes if result.direction == "YES"
                         else market.token_id_no)
                self._market_signals[market.id] = result.signals
                ai_trades += 1
                self._poly_client.invalidate()  # сброс кэша после сделки
                self._register_open_after_execution(
                    market_id=market.id,
                    question=market.question,
                    direction=result.direction,
                    entry_price=price,
                    size_usd=decision.size_usd,
                    tags=list(market.tags),
                    signal_type=result.strategy_type,
                    token_id=token,
                    slug=market.slug,
                    url=market.polymarket_url,
                    notify_kwargs={
                        "question":      market.question,
                        "direction":     result.direction,
                        "price":         price,
                        "size":          decision.size_usd,
                        "edge":          result.edge,
                        "confidence":    result.confidence,
                        "reasoning":     result.reasoning,
                        "dry_run":       cfg.dry_run,
                        "signals":       result.signals,
                        "signal_type":   result.strategy_type,
                        "url":           market.polymarket_url,
                        "domain_mult":   mctx.domain_multiplier,
                        "timing":        mctx.timing_reason,
                        "question_type": result.question_type,
                        "liq_mult":      _liq_mult,
                        "liq_snap":      _liq_snap,
                    },
                )
                # Помечаем сигнал как исполненный
                for s in reversed(self._signal_history):
                    if s.get("question") == market.question:
                        s["traded"] = True
                        s["size"]   = decision.size_usd
                        break
            elif not cfg.dry_run:
                _dead_letter(
                    market_id = market.id,
                    question  = market.question,
                    direction = result.direction,
                    size_usd  = decision.size_usd,
                    price     = price,
                    error     = "execute_failed",
                )
                now_ts = time.time()
                if market.id not in self._execute_cooldown or now_ts >= self._execute_cooldown[market.id]:
                    self.tg.error(f"Ордер упал: {market.question[:60]}")
                self._execute_cooldown[market.id] = now_ts + 1800  # 30 min cooldown

            time.sleep(1.5)

        snap = self.risk.snapshot()
        # cycle_summary disabled — too noisy per-cycle
        self._push_to_api()

        # После сделок — ускоряем следующие 3 цикла для мониторинга позиций
        if ai_trades + sm_trades > 0:
            self._fast_cycles_remaining = max(self._fast_cycles_remaining, 3)
            log.info(f"⚡ {ai_trades + sm_trades} сделок → активируем быстрые циклы (×3)")

    def _find_near_resolution(self, markets: list) -> list:
        """
        Resolution Speed Arbitrage: рынки близкие к резолюции.

        Логика:
        - Цена 80–97% YES (или 3–20% → торгуем NO) — рынок "почти решён"
        - Закрытие через 0.5–7 дней — достаточно времени для входа
        - Kelly × 30% — консервативный размер (высокая вероятность, но не 100%)
        - Лимит: не более 3 near-resolution позиций одновременно

        Зачем работает: маркет-мейкеры ставят цены inefficiently у resolution,
        создавая небольшие но стабильные арбитражные возможности.
        """
        # Лимит: не перегружаем портфель near-res позициями
        MAX_NEAR_RES = 3
        current_near_res = sum(
            1 for p in self.risk.open_positions
            if getattr(p, "signal_type", "") == "near_resolution"
        )
        if current_near_res >= MAX_NEAR_RES:
            return []

        results = []
        for market in markets:
            if len(results) + current_near_res >= MAX_NEAR_RES:
                break

            # Цена в зоне "почти решён" (расширили с 85% до 80%)
            yes_near = 0.80 <= market.yes_price <= 0.97
            no_near  = 0.03 <= market.yes_price <= 0.20
            if not yes_near and not no_near:
                continue

            if not market.end_date:
                continue
            try:
                end       = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                days_left = (end - datetime.now(timezone.utc)).total_seconds() / 86400
                if not (0.5 < days_left < 7):          # расширили с 5 до 7 дней
                    continue
            except Exception:
                continue

            if market.volume_24h < cfg.min_volume * 2:
                continue

            if any(p.market_id == market.id for p in self.risk.open_positions):
                continue

            direction = "YES" if yes_near else "NO"
            price     = market.yes_price if direction == "YES" else (1.0 - market.yes_price)

            # Implied probability с поправкой на days_left:
            # чем дальше до резолюции — тем меньше уверенность
            # 1d: 0.93 | 3d: 0.90 | 7d: 0.86
            implied_prob = round(max(0.82, 0.95 - days_left * 0.013), 3)

            size = self.risk.kelly_size(
                ai_probability = implied_prob,
                market_price   = market.yes_price,
                direction      = direction,
                bankroll       = self._real_bankroll,
            ) * 0.30   # 30% of normal Kelly (was 25%)

            if size < 0.50:
                continue

            decision = self.risk.check(market.id, size, list(market.tags),
                                       question=market.question)
            if not decision.allowed:
                continue

            log.info(f"  📍 Near-res: {market.question[:50]} "
                     f"| {direction} @ {price:.3f} p={implied_prob:.0%} | {days_left:.1f}d left")

            if _execute(market, direction, decision.size_usd, price):
                token = (market.token_id_yes if direction == "YES"
                         else market.token_id_no)
                self._register_open_after_execution(
                    market_id=market.id,
                    question=market.question,
                    direction=direction,
                    entry_price=price,
                    size_usd=decision.size_usd,
                    tags=list(market.tags),
                    signal_type="near_resolution",
                    token_id=token,
                    slug=market.slug,
                    url=market.polymarket_url,
                    notify_kwargs={
                        "question": market.question,
                        "direction": direction,
                        "price": price,
                        "size": decision.size_usd,
                        "edge": 1.0 - price,
                        "confidence": "high",
                        "reasoning": f"Near-resolution: {days_left:.1f}d left @ {price:.1%} p={implied_prob:.0%}",
                        "dry_run": cfg.dry_run,
                        "signal_type": "near_resolution",
                        "url": market.polymarket_url,
                    },
                )
                results.append(market)

        return results

    def _push_to_api(self) -> None:
        """Отправить актуальные данные в API сервис."""
        api_url = cfg.api_push_url
        if not api_url:
            return
        try:
            import requests as _r
            snap     = self.risk.snapshot()
            open_pos = self.risk.open_positions
            # Все закрытые — для полной истории
            all_closed = self.risk.closed_positions
            closed_today = [p for p in all_closed
                           if (p.closed_at or "")[:10] == datetime.now(timezone.utc).date().isoformat()]

            # Portfolio snapshot из Polymarket (кэшированный, TTL 30с — без доп. HTTP запросов)
            poly_snap   = self._poly_client.portfolio()
            poly_by_tok = {pos.token_id: pos for pos in poly_snap.positions}

            def get_current_price(token_id: str) -> float:
                """Текущая цена — из Polymarket кэша или запрос через клиент."""
                if not token_id:
                    return 0.0
                poly_pos = poly_by_tok.get(token_id)
                if poly_pos:
                    return poly_pos.cur_price
                price = self._poly_client.token_price(token_id)
                return price if price else 0.0

            def calc_upnl(p, current_price: float) -> float:
                """Unrealised P&L — приоритет данных Polymarket Data API."""
                poly_pos = poly_by_tok.get(p.token_id or "")
                if poly_pos:
                    return round(poly_pos.unrealised_pnl, 2)
                if current_price <= 0 or p.entry_price <= 0:
                    return 0.0
                return round((current_price - p.entry_price) / max(p.entry_price, 0.01) * p.size_usd, 2)

            def polymarket_url(p) -> str:
                """Ссылка на рынок: приоритет сохранённый URL → поиск по вопросу."""
                import urllib.parse
                stored = getattr(p, 'url', '') or ''
                if stored:
                    return stored
                # Fallback: поиск по полному тексту вопроса (никогда не 404)
                q = urllib.parse.quote((p.question or '')[:120])
                return f"https://polymarket.com/?s={q}"

            def fmt(p, fetch_price: bool = False):
                poly_pos      = poly_by_tok.get(p.token_id or "") if p.token_id else None
                current_price = get_current_price(p.token_id) if fetch_price and p.token_id else 0.0
                upnl = calc_upnl(p, current_price) if fetch_price else (p.pnl or 0)
                return {
                    "id":            p.market_id,
                    "question":      p.question,
                    "direction":     p.direction,
                    "price":         p.entry_price,
                    "current_price": current_price,
                    "size":          p.size_usd,
                    "pnl":           upnl,
                    "pnl_pct":       round(poly_pos.pnl_pct, 4) if poly_pos else 0.0,
                    "poly_verified": poly_pos is not None,
                    "opened_at":     getattr(p, "opened_at", None) or "",
                    "closed_at":     getattr(p, "closed_at", None) or "",
                    "tags":          p.tags,
                    "status":        p.status,
                    "type":          p.signal_type,
                    "url":           polymarket_url(p),
                    "slug":          getattr(p, "slug", "") or "",
                    "token_id":      p.token_id or "",
                    "source":        "athena" if p.signal_type == "athena" else "prometheus",
                }

            # Форматируем позиции (цены из Polymarket через кэш, без доп. запросов)
            open_formatted   = [fmt(p, fetch_price=True)  for p in open_pos]
            closed_formatted = [fmt(p, fetch_price=False) for p in closed_today]
            all_closed_fmt   = [fmt(p, fetch_price=False) for p in all_closed[-100:]]

            # Суммарный unrealised P&L — из Polymarket Data API (точнее CLOB midpoint)
            total_upnl = poly_snap.unrealised_pnl if poly_snap.positions else sum(p["pnl"] for p in open_formatted)

            # Smart money leaderboard
            sm_traders = []
            for addr, profile in list(self.sm.known_wallets.items())[:10]:
                if profile.trader_class.value == "noise":
                    continue
                sm_traders.append({
                    "addr":    addr[:6] + "…" + addr[-4:] if len(addr) > 10 else addr,
                    "class":   profile.trader_class.value.capitalize(),
                    "wr":      round(profile.win_rate, 3),
                    "roi":     round(profile.roi, 3),
                    "trades":  profile.resolved_trades,
                    "vol":     round(profile.total_volume, 0),
                    "score":   round(profile.insider_score if profile.trader_class.value == "insider" else profile.sharp_score, 3),
                    "domains": profile.specializations,
                })

            # Считаем сколько крупных сделок просканировано всего
            total_scanned = getattr(self.sm, '_total_scanned', 0)

            # Learning stats
            learning_stats = self.learning.stats()

            # Prefer real CLOB balance (USDC cash + positions) over formula estimate.
            # _sync_balance() may have already set _real_bankroll to the CLOB value;
            # only fall back to formula if we have no live CLOB data.
            _realized   = snap["total_pnl"]
            if self._clob_usdc_balance > 0:
                _bankroll_ui = max(1.0, self._clob_usdc_balance + poly_snap.positions_value)
            else:
                _bankroll_ui = max(1.0, cfg.bankroll + _realized + total_upnl)
            self._real_bankroll = _bankroll_ui

            payload = {
                "overview": {
                    "bot_running":       True,
                    "dry_run":           cfg.dry_run,
                    "pnl_today":         snap["daily_pnl"],
                    "pnl_total":         snap["total_pnl"],
                    "unrealised_pnl":    round(total_upnl, 2),
                    "win_rate":          snap["win_rate"],
                    "total_trades":      snap["total_trades"],
                    "open_positions":    snap["open_positions"],
                    "open_exposure":     snap["open_exposure"],
                    "bankroll":          round(_bankroll_ui, 2),
                    "clob_usdc":         round(self._clob_usdc_balance, 2),
                    "portfolio_value":   round(poly_snap.positions_value, 2),
                    "total_invested":    round(poly_snap.total_invested, 2),
                    "poly_sync_at":      datetime.now(timezone.utc).strftime("%H:%M UTC"),
                    "daily_loss_used":   snap["daily_loss_pct"],
                    "signals_today":     self._daily_signals,
                    "smart_money_today": self._daily_sm_alerts,
                },
                "positions": {
                    "open":         open_formatted,
                    "closed_today": closed_formatted,
                    "history":      all_closed_fmt,
                },
                "signals": list(reversed(self._signal_history)),
                "smart_money": {
                    "traders":       sm_traders,
                    "total_tracked": len(self.sm.known_wallets),
                    "total_noise":   sum(1 for p in self.sm.known_wallets.values() if p.trader_class.value == "noise"),
                    "status":        "active" if self._daily_sm_alerts > 0 else "scanning",
                    "scanned_today": self._daily_signals,
                    "whale_alerts":  list(self._whale_alerts)[-20:],
                },
                "learning": {
                    "signal_stats": learning_stats,
                    "domain_stats": self._domain_prior.stats_summary(),
                    "type_stats":   self.learning.stats_by_type(),
                    "strategy_stats": self._strategy_control.summary(),
                },
            }

            r = _r.post(
                f"{api_url}/internal/push",
                json=payload,
                headers={"x-bot-key": cfg.dashboard_key},
                timeout=8,
            )
            r.raise_for_status()
            log.info(
                f"📤 Данные отправлены в API | "
                f"Bankroll ${self._real_bankroll:.2f} | "
                f"uPnL ${total_upnl:+.2f} | "
                f"Portfolio ${poly_snap.positions_value:.2f}"
            )
        except Exception as e:
            log.warning(f"⚠️ Push to API failed ({api_url}): {type(e).__name__}: {e}")

    def _maybe_daily_report(self) -> None:
        now   = datetime.now(timezone.utc)
        today = date.today()
        if now.hour == cfg.report_hour and self._last_report_date != today:
            self._last_report_date = today
            self.tg.daily_report(
                snap         = self.risk.snapshot(),
                by_tag       = self.risk.performance_by_tag(),
                signals      = self._daily_signals,
                sm_alerts    = self._daily_sm_alerts,
                domain_stats = self._domain_prior.stats_summary(),
            )
            self._daily_signals   = 0
            self._daily_sm_alerts = 0
            # Ежедневная чистка старых данных
            self.learning.cleanup_old(days=90)
            try:
                self.intel.db.cleanup(days=7)
            except Exception:
                pass
            # Еженедельный отчёт по воскресеньям
            if now.weekday() == 6:
                self.tg.weekly_report(
                    snap         = self.risk.snapshot(),
                    domain_stats = self._domain_prior.stats_summary(),
                )

    def _sleep(self, seconds: int) -> None:
        """Прерываемый sleep."""
        log.info(f"Следующий цикл через {seconds}с")
        for _ in range(seconds):
            if self._shutdown:
                break
            time.sleep(1)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Prometheus trading bot")
    parser.add_argument(
        "--backtest",
        type=int,
        nargs="?",
        const=100,
        metavar="N",
        help="Запустить backtest на N закрытых рынках (по умолчанию 100)",
    )
    args = parser.parse_args()

    _logger.setup(logs_dir=cfg.logs_dir)

    if args.backtest is not None:
        from backtest import Backtester
        log.info(f"Запуск backtest на {args.backtest} рынках...")
        bt     = Backtester(Anthropic(api_key=cfg.anthropic_key))
        result = bt.run(n_markets=args.backtest)
        print(result.summary())
    else:
        Prometheus().run()
