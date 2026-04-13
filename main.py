"""
Prometheus — Main Orchestrator
Запускает всё. Ничего лишнего.
"""
from __future__ import annotations
import json
import os
import signal as _signal
import time
from datetime import datetime, date, timezone
from anthropic import Anthropic

import logger as _logger
from config import cfg
from telegram import Telegram
from data import fetch_markets, Market, best_polymarket_url
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
from liquidity import is_market_liquid
from strategy_control import StrategyControl

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

        # Proxy support for geo-restricted regions
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        proxies   = {"http": proxy_url, "https": proxy_url} if proxy_url else None

        # signature_type: 0=EOA, 1=EOA via proxy, 2=magic link
        # Default 0 works for standard MetaMask EOA wallets
        sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
        # Only pass funder if it's set - for EOA mode (sig_type=0) funder is not needed
        clob_kwargs = dict(
            key=cfg.poly_key,
            chain_id=137,
            signature_type=sig_type,
        )
        if cfg.poly_funder and cfg.poly_funder.startswith("0x"):
            clob_kwargs["funder"] = cfg.poly_funder
        client = ClobClient("https://clob.polymarket.com", **clob_kwargs)
        # Inject proxy into the underlying session if available
        if proxies and hasattr(client, "session"):
            client.session.proxies.update(proxies)
        elif proxies:
            import requests as _req
            _session = _req.Session()
            _session.proxies.update(proxies)
            if hasattr(client, "_session"):
                client._session = _session

        # Деривируем L2 API credentials из приватного ключа
        # Работает для signature_type=0 (EOA) и signature_type=2 (Safe proxy)
        import time as _time
        raw_creds = client.create_or_derive_api_creds()
        if isinstance(raw_creds, dict):
            from py_clob_client.clob_types import ApiCreds
            raw_creds = ApiCreds(
                api_key        = raw_creds["api_key"],
                api_secret     = raw_creds["api_secret"],
                api_passphrase = raw_creds["api_passphrase"],
            )
        client.set_api_creds(raw_creds)
        log.info(f"L2 creds OK: {raw_creds.api_key[:8]}... sig_type={sig_type}")
        _time.sleep(2)  # Пауза после деривирования — ключи активируются ~2 сек

        # Check CLOB balance - detailed logging for debugging
        try:
            clob_balance = client.get_balance()
            log.info(f"CLOB balance raw: {clob_balance}")
        except Exception as be:
            log.warning(f"Balance check error: {be}")

        # Check balance-allowance endpoint
        try:
            import requests as _req
            ba_resp = _req.get(
                "https://clob.polymarket.com/balance-allowance",
                params={"asset_type": "USDC"},
                headers=client.get_headers("GET", "/balance-allowance"),
                timeout=5,
            )
            log.info(f"Balance-allowance: {ba_resp.text[:200]}")
        except Exception as be2:
            log.warning(f"Balance-allowance error: {be2}")

        token = market.token_id_yes if direction == "YES" else market.token_id_no
        if not token:
            log.error("Нет token_id для исполнения")
            return False

        order  = MarketOrderArgs(token_id=token, amount=size_usd,
                                 side=BUY, order_type=OrderType.FAK)
        signed = client.create_market_order(order)
        result = client.post_order(signed, OrderType.FAK)
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
        self.ai       = Anthropic(api_key=cfg.anthropic_key) if cfg.ai_mode != "off" and cfg.anthropic_key else None

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
        self.resolver  = PositionResolver(self.risk, self.tg)
        self.learning  = LearningEngine(cfg.logs_dir)

        # State
        self._shutdown:         bool            = False
        self._last_intel:       datetime | None = None
        self._daily_signals:    int             = 0
        self._daily_sm_alerts:  int             = 0
        self._last_report_date: date | None     = None
        self._last_limit_warn:  float           = 0.0
        self._signal_history:   list            = []   # история сигналов для дашборда
        self._last_breaking:    float           = 0.0  # время последнего breaking news цикла
        self._whale_alerts:     list            = []   # крупные необычные сделки
        self._market_signals:   dict            = {}   # market_id → list[Signal] для learning
        self._reviewer = PositionReviewer(
            risk_manager = self.risk,
            ai           = self.ai,
            intel        = self.intel,
            telegram     = self.tg,
        )
        self._domain_prior = DomainPrior(cfg.logs_dir)
        self._pending_open_notifies: dict[str, dict] = {}
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
                self._reconcile_with_polymarket()
                self._resolve_positions()
                self._review_positions()
                self._process_pending_orders()
                self._reconcile_with_polymarket()
                self._run_intel()
                self._check_risk_alerts()
                self._trade_cycle()

                # Breaking alerts disabled: too noisy, not actionable.
                # We keep the normal cycle only.

                self._sleep(cfg.scan_interval)

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
        market_slug: str | None = None,
        event_slug: str | None = None,
        market_url: str | None = None,
        event_url: str | None = None,
        condition_id: str | None = None,
        notify_kwargs: dict,
    ) -> None:
        if cfg.dry_run:
            self.risk.open(
                market_id, question, direction, entry_price, size_usd,
                tags, signal_type, token_id=token_id, slug=slug,
                market_slug=market_slug, event_slug=event_slug,
                market_url=market_url, event_url=event_url, condition_id=condition_id,
            )
            self.tg.position_opened(**notify_kwargs)
            return

        if not token_id or not cfg.poly_funder:
            self._alert_and_pause(
                f"Не могу подтвердить открытие на Polymarket: token_id/funder отсутствуют | {question[:80]}"
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
            market_slug=market_slug,
            event_slug=event_slug,
            market_url=market_url,
            event_url=event_url,
            condition_id=condition_id,
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

        real_positions = fetch_polymarket_positions(cfg.poly_funder)
        now_ts = time.time()

        for action in pending:
            if not action.due(now_ts):
                continue
            real_pos = real_positions.get(action.token_id or "")
            if real_pos and float(real_pos.get("size", 0) or 0) > 0.01:
                self.risk.open(
                    action.market_id, action.question, action.direction,
                    action.entry_price, action.size_usd, action.tags,
                    action.signal_type, token_id=action.token_id, slug=action.slug,
                    market_slug=action.market_slug, event_slug=action.event_slug,
                    market_url=action.market_url, event_url=action.event_url,
                    condition_id=action.condition_id,
                )
                notify_kwargs = self._pending_open_notifies.pop(action.market_id, None)
                if notify_kwargs:
                    self.tg.position_opened(**notify_kwargs)
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


    def _reconcile_with_polymarket(self) -> None:
        if cfg.dry_run or not cfg.poly_funder:
            return
        now = time.time()
        last_reconcile = getattr(self, "_last_reconcile", 0.0)
        reconcile_interval = float(getattr(self, "_reconcile_interval_sec", getattr(cfg, "reconcile_interval_sec", 300)))
        if (now - last_reconcile) < reconcile_interval:
            return
        self._last_reconcile = now
        try:
            real_positions = fetch_polymarket_positions(cfg.poly_funder)
            result = self.risk.reconcile_open_positions(real_positions)
            for item in result.get("externally_closed", []):
                log.warning(f"🔄 External/manual close detected on Polymarket: {item.get('question','')[:80]}")
                try:
                    self.tg.send(
                        f"⚠️ *External close detected*\n\n"
                        f"{item.get('question','position')}\n"
                        f"Source of truth: *Polymarket*\n"
                        f"Reason: `{item.get('reason','external_manual_close')}`"
                    )
                except Exception:
                    pass
            if result.get("discovered"):
                log.warning(f"🔎 Found {len(result['discovered'])} Polymarket positions missing in local state")
            if result.get("externally_closed") or result.get("discovered"):
                self._push_to_api()
        except Exception as e:
            log.warning(f"Reconciliation error: {e}")

    def _resolve_positions(self) -> None:
        closed = self.resolver.run()
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
                self.tg.weights_updated(new_weights)
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
                    st = self._domain_prior._stats.get(d)
                    if st:
                        self.tg.domain_shift(
                            domain   = d,
                            old_mult = old_m,
                            new_mult = new_m,
                            win_rate = st.win_rate,
                            total    = st.total,
                        )
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
                        self.tg.arbitrage(arb[0].title, arb[0].content)
                except Exception as e:
                    log.debug(f"Arbitrage error: {e}")

                # Cross-market correlation check
                try:
                    corr_pairs = self.signals.find_correlated_markets(markets)
                    if corr_pairs:
                        for m1, m2, spread in corr_pairs[:2]:
                            log.info(
                                f"📊 Correlation gap: {m1.question[:40]} ({m1.yes_price:.0%}) "
                                f"vs {m2.question[:40]} ({m2.yes_price:.0%}) "
                                f"spread={spread:.0%}"
                            )
                            msg = (
                                "📊 *Correlation Gap*\n\n"
                                f"`{m1.question[:60]}`  *{m1.yes_price:.0%}*\n"
                                f"`{m2.question[:60]}`  *{m2.yes_price:.0%}*\n\n"
                                f"Spread: *{spread:.0%}* — возможный арбитраж"
                            )
                            self.tg.send(msg, silent=True)
                except Exception as e:
                    log.debug(f"Correlation: {e}")

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
        self.signals.reset_cycle_budget()
        for reenabled in self._strategy_control.refresh_states():
            self._notify_strategy_change(reenabled)

        # 1. Smart Money
        sm_signals = self.sm.scan()
        self._daily_sm_alerts += len(sm_signals)

        # Собираем whale alerts — все крупные сделки для дашборда
        from data import fetch_large_trades
        large_trades = fetch_large_trades(min_size=3000, hours_back=6)
        for t in large_trades[:30]:
            alert = {
                "question":  t.question or t.market_id,
                "direction": "YES" if t.side == "BUY" else "NO",
                "price":     round(t.price, 3),
                "size":      round(t.size, 0),
                "wallet":    (t.maker or "")[:8] + "…" if t.maker else "unknown",
                "time":      t.timestamp.strftime("%H:%M") if t.timestamp else "",
                "type":      "whale",
            }
            # Проверяем тип кошелька
            if t.maker and t.maker in self.sm.known_wallets:
                profile = self.sm.known_wallets[t.maker]
                if profile.trader_class.value != "noise":
                    alert["type"] = profile.trader_class.value
            # Добавляем если ещё нет такой же (дедупликация по market_id + side + size)
            key = f"{t.market_id}_{t.side}_{round(t.size)}"
            existing_keys = {
                f"{a.get('question','')}_{a.get('direction','')}_{round(a.get('size',0))}"
                for a in self._whale_alerts
            }
            if key not in existing_keys:
                self._whale_alerts.append(alert)
        self._whale_alerts = self._whale_alerts[-50:]  # последние 50

        for sig in sm_signals[:3]:
            if not self.risk.snapshot()["can_trade"]:
                break
            decision = self.risk.check(sig.market_id, sig.our_size,
                                        list(sig.wallet.specializations))
            if not decision.allowed:
                log.info(f"SM risk block: {decision.reason}")
                continue

            # Smart money execution — используем token_id из сигнала
            class _Mkt:
                question     = sig.question
                token_id_yes = sig.token_id if sig.direction == "YES" else None
                token_id_no  = sig.token_id if sig.direction == "NO"  else None

            if _execute(_Mkt(), sig.direction, decision.size_usd, sig.entry_price):
                sm_trades += 1
                self._register_open_after_execution(
                    market_id=sig.market_id,
                    question=sig.question,
                    direction=sig.direction,
                    entry_price=sig.entry_price,
                    size_usd=decision.size_usd,
                    tags=list(sig.wallet.specializations),
                    signal_type="smart_money",
                    token_id=sig.token_id or None,
                    slug=sig.slug or None,
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
                        "url": (f"https://polymarket.com/event/{sig.slug}" if sig.slug else ""),
                        "domain_mult": 1.0,
                        "timing": "",
                    },
                )
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

        # 2. Двухэтапный анализ рынков
        # Этап 1: быстрый скрининг 50 рынков без AI
        all_markets = fetch_markets(
            limit          = 50,   # смотрим широко
            min_volume_24h = cfg.min_volume,
        )
        self._daily_signals += len(all_markets)
        log.info(f"Рынков загружено: {len(all_markets)} (из 50)")

        # Этап 2: анализ кандидатов. AI подключается редко внутри SignalEngine.
        top_n = 20 if cfg.dry_run else max(cfg.max_markets, 20)
        candidates = screen(all_markets, top_n=top_n, fetch_kalshi=False)
        log.info(f"После скрининга: {len(candidates)} кандидатов для decision engine")

        if cfg.enable_near_resolution:
            near_res = self._find_near_resolution(all_markets)
            if near_res:
                log.info(f"Near-resolution markets: {len(near_res)}")

        for cand in candidates:
            market = cand.market
            if not self.risk.snapshot()["can_trade"]:
                break

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
            self._signal_history = self._signal_history[-50:]

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

            # Консервативно: low confidence почти всегда пропускаем
            if result.confidence == "low":
                if result.strategy_type == "cross_market_gap" and result.edge >= max(0.12, domain_min_edge):
                    log.info(f"  Cross-market override: edge={result.edge:.1%} conf=low")
                else:
                    continue

            if result.strategy_type == "ai_assisted" and result.edge < max(cfg.ai_unconfirmed_edge, domain_min_edge + 0.02):
                log.info(f"  AI-assisted edge too small ({result.edge:.1%}) — skip")
                continue
            if result.strategy_type == "ai_assisted":
                log.info("  AI-assisted entries disabled — AI is filter only")
                continue

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

            # Portfolio Kelly с domain multiplier
            size = portfolio_kelly_size(
                ai_probability   = result.ai_probability,
                market_price     = market.yes_price,
                direction        = result.direction,
                bankroll         = cfg.bankroll,
                kelly_fraction   = cfg.kelly_frac,
                open_positions   = self.risk.open_positions,
                max_position_usd = cfg.max_pos_usd,
                domain_multiplier = mctx.total_size_multiplier,
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

            if self._strategy_control.all_primary_strategies_weak():
                size = min(max(1.0, size), self._strategy_control.all_weak_min_usd)
                log.info(f"  All primary strategies weak: minimal exploratory size ${size:.2f}")

            size = min(cfg.max_pos_usd, max(1.0, size))

            decision = self.risk.check(market.id, size, list(market.tags))
            if not decision.allowed:
                log.info(f"AI risk block: {decision.reason}")
                continue

            price = market.yes_price if result.direction == "YES" else market.no_price

            # Проверяем ликвидность order book
            liquid, liq_reason = is_market_liquid(market, result.direction)
            if not liquid:
                log.info(f"  Liquidity block: {liq_reason}")
                continue

            if _execute(market, result.direction, decision.size_usd, price):
                token = (market.token_id_yes if result.direction == "YES"
                         else market.token_id_no)
                self._market_signals[market.id] = result.signals
                ai_trades += 1
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
                    market_slug=getattr(market, "market_slug", None),
                    event_slug=getattr(market, "event_slug", None),
                    market_url=getattr(market, "market_url", None),
                    event_url=getattr(market, "event_url", None),
                    condition_id=getattr(market, "condition_id", None) or market.id,
                    notify_kwargs={
                        "question": market.question,
                        "direction": result.direction,
                        "price": price,
                        "size": decision.size_usd,
                        "edge": result.edge,
                        "confidence": result.confidence,
                        "reasoning": result.reasoning,
                        "dry_run": cfg.dry_run,
                        "signals": result.signals,
                        "signal_type": result.strategy_type,
                        "url": market.polymarket_url,
                        "domain_mult": mctx.domain_multiplier,
                        "timing": mctx.timing_reason,
                    },
                )
                # Microstructure уведомление если сильный сигнал
                if (mctx.microstructure and
                    mctx.microstructure.confidence > 0.50 and
                    mctx.microstructure.direction != "NEUTRAL"):
                    action = ("confirmed" if mctx.microstructure.direction == result.direction
                              else "contra")
                    self.tg.microstructure_signal(
                        question   = market.question,
                        direction  = mctx.microstructure.direction,
                        confidence = mctx.microstructure.confidence,
                        reasoning  = mctx.microstructure.reasoning,
                        action     = action,
                    )
                # Помечаем сигнал как исполненный
                for s in reversed(self._signal_history):
                    if s.get("question") == market.question:
                        s["traded"] = True
                        s["size"]   = decision.size_usd
                        break

            time.sleep(1.5)

        snap = self.risk.snapshot()
        self.tg.cycle_summary(ai_trades, sm_trades, snap["daily_pnl"])
        self._push_to_api()

    def _find_near_resolution(self, markets: list) -> list:
        """
        Near-Resolution Strategy: рынки близкие к резолюции.
        Цена 85-97%, закрытие < 5 дней → высокий win rate, быстрый оборот.
        Маленькая позиция (25% от обычного размера) — low risk.
        """
        results = []
        for market in markets:
            # Цена должна быть в зоне "почти решён"
            if not (0.85 <= market.yes_price <= 0.97):
                # Проверяем и NO сторону
                if not (0.03 <= market.yes_price <= 0.15):
                    continue

            # Должно быть < 5 дней до закрытия
            if not market.end_date:
                continue
            try:
                end = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
                days_left = (end - datetime.now(timezone.utc)).total_seconds() / 86400
                if not (0.5 < days_left < 5):
                    continue
            except Exception:
                continue

            # Минимальный объём
            if market.volume_24h < cfg.min_volume * 2:
                continue

            # Не открываем если уже есть позиция
            if any(p.market_id == market.id for p in self.risk.open_positions):
                continue

            direction = "YES" if market.yes_price >= 0.85 else "NO"
            # For NO: the NO token price = 1 - yes_price
            price = market.yes_price if direction == "YES" else (1.0 - market.yes_price)

            # Маленький размер — 25% от обычного Kelly
            # 93% confident the near-resolution market resolves as priced
            size = self.risk.kelly_size(
                ai_probability = 0.93,
                market_price   = market.yes_price,
                direction      = direction,
                bankroll       = cfg.bankroll,
            ) * 0.25   # 25% of normal Kelly for safety

            if size < 0.50:
                continue

            decision = self.risk.check(market.id, size, list(market.tags))
            if not decision.allowed:
                continue

            log.info(f"  📍 Near-res: {market.question[:50]} "
                     f"| {direction} @ {price:.3f} | {days_left:.1f}d left")

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
                    market_slug=getattr(market, "market_slug", None),
                    event_slug=getattr(market, "event_slug", None),
                    market_url=getattr(market, "market_url", None),
                    event_url=getattr(market, "event_url", None),
                    condition_id=getattr(market, "condition_id", None) or market.id,
                    notify_kwargs={
                        "question": market.question,
                        "direction": direction,
                        "price": price,
                        "size": decision.size_usd,
                        "edge": 1.0 - price,
                        "confidence": "high",
                        "reasoning": f"Near-resolution: {days_left:.1f}d left @ {price:.1%}",
                        "dry_run": cfg.dry_run,
                        "signal_type": "near_resolution",
                        "url": market.polymarket_url,
                    },
                )
                results.append(market)

        return results


    def _fetch_live_cash_usd(self) -> float | None:
        """Fetch live available cash from Polymarket/CLOB when possible."""
        if cfg.dry_run or not cfg.poly_key:
            return None
        try:
            from py_clob_client.client import ClobClient
            sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))
            kwargs = {"key": cfg.poly_key, "chain_id": 137, "signature_type": sig_type}
            if cfg.poly_funder and str(cfg.poly_funder).startswith("0x"):
                kwargs["funder"] = cfg.poly_funder
            client = ClobClient("https://clob.polymarket.com", **kwargs)
            raw_creds = client.create_or_derive_api_creds()
            if isinstance(raw_creds, dict):
                from py_clob_client.clob_types import ApiCreds
                raw_creds = ApiCreds(
                    api_key=raw_creds["api_key"],
                    api_secret=raw_creds["api_secret"],
                    api_passphrase=raw_creds["api_passphrase"],
                )
            client.set_api_creds(raw_creds)
            bal = client.get_balance()
            candidates = []
            if isinstance(bal, dict):
                for key in ("balance", "available", "available_balance", "free", "usdc", "total", "amount"):
                    value = bal.get(key)
                    if value is not None:
                        candidates.append(value)
            elif isinstance(bal, (int, float, str)):
                candidates.append(bal)
            for raw in candidates:
                try:
                    return float(raw)
                except Exception:
                    continue
        except Exception as e:
            log.warning(f"Live cash fetch error: {e}")
        return None

    def _push_to_api(self) -> None:
        """Отправить актуальные данные в API сервис."""
        import requests as _r

        api_url = (os.environ.get("API_PUSH_URL", "") or "").strip().rstrip("/")
        if api_url.endswith("/internal/push"):
            api_url = api_url[: -len("/internal/push")]
        if not api_url:
            log.warning("API_PUSH_URL пустой — push в dashboard пропущен")
            return
        try:
            snap = self.risk.snapshot()

            # Перед каждым push сверяем open-позиции с Polymarket.
            real_positions: dict[str, dict] = {}
            if not cfg.dry_run and cfg.poly_funder:
                try:
                    real_positions = fetch_polymarket_positions(cfg.poly_funder)
                    rec = self.risk.reconcile_open_positions(real_positions)
                    if rec.get("externally_closed"):
                        log.info(f"Reconcile before push: {len(rec['externally_closed'])} external/manual close(s)")
                except Exception as e:
                    log.warning(f"Push reconcile error: {e}")

            open_pos = self.risk.open_positions
            all_closed = self.risk.closed_positions
            closed_today = [
                p for p in all_closed
                if (p.closed_at or "")[:10] == datetime.now(timezone.utc).date().isoformat()
            ]

            def calc_upnl(p, current_price: float) -> float:
                """Unrealised P&L по текущей цене."""
                if current_price <= 0 or p.entry_price <= 0:
                    return 0.0
                entry = p.entry_price if p.direction == "YES" else 1 - p.entry_price
                cur = current_price if p.direction == "YES" else 1 - current_price
                return round((cur - entry) / max(entry, 0.01) * p.size_usd, 2)

            def polymarket_url(p) -> str:
                return best_polymarket_url(
                    question=p.question or "",
                    market_url=getattr(p, "market_url", None),
                    event_url=getattr(p, "event_url", None),
                    market_slug=getattr(p, "market_slug", None),
                    event_slug=getattr(p, "event_slug", None),
                    slug=getattr(p, "slug", None),
                )

            def current_price_for(p) -> float:
                if not p.token_id:
                    return 0.0
                real = real_positions.get(p.token_id)
                if real:
                    return float(real.get("cur_price", 0) or 0.0)
                return 0.0

            def fmt(p, fetch_price: bool = False):
                current_price = current_price_for(p) if fetch_price else 0.0
                upnl = calc_upnl(p, current_price) if fetch_price else (p.pnl or 0)
                return {
                    "id":            p.market_id,
                    "question":      p.question,
                    "direction":     p.direction,
                    "price":         p.entry_price,
                    "current_price": current_price,
                    "size":          p.size_usd,
                    "pnl":           upnl,
                    "age":           "now",
                    "tags":          p.tags,
                    "status":        p.status,
                    "type":          p.signal_type,
                    "url":           polymarket_url(p),
                    "slug":          getattr(p, "slug", "") or "",
                    "market_slug":   getattr(p, "market_slug", "") or "",
                    "event_slug":    getattr(p, "event_slug", "") or "",
                    "market_url":    getattr(p, "market_url", "") or "",
                    "event_url":     getattr(p, "event_url", "") or "",
                    "condition_id":  getattr(p, "condition_id", "") or "",
                    "token_id":      p.token_id or "",
                    "last_verified_at": getattr(p, "last_verified_at", "") or "",
                    "reconcile_state": getattr(p, "reconcile_state", "") or "",
                    "closed_reason": getattr(p, "closed_reason", "") or "",
                    "polymarket_size": float(real_positions.get(p.token_id, {}).get("size", 0) or 0) if fetch_price and p.token_id else 0.0,
                }

            open_formatted = [fmt(p, fetch_price=True) for p in open_pos]
            closed_formatted = [fmt(p, fetch_price=False) for p in closed_today]
            all_closed_fmt = [fmt(p, fetch_price=False) for p in all_closed[-100:]]

            total_upnl = sum(p["pnl"] for p in open_formatted)

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

            live_cash = self._fetch_live_cash_usd()
            fallback_equity = float(cfg.bankroll) + float(snap["total_pnl"]) + float(total_upnl)
            fallback_cash = max(0.0, fallback_equity - float(snap["open_exposure"]))
            cash_usd = live_cash if live_cash is not None else fallback_cash
            equity_usd = float(cash_usd) + float(snap["open_exposure"])
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
                    "bankroll":          cfg.bankroll,
                    "cash_usd":          round(float(cash_usd), 2),
                    "equity_usd":        round(float(equity_usd), 2),
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
                    "whale_alerts":  self._whale_alerts[-20:],
                },
                "learning": {
                    "signal_stats": learning_stats,
                    "domain_stats": self._domain_prior.stats_summary(),
                    "type_stats":   self.learning.stats_by_type(),
                    "strategy_stats": self._strategy_control.summary(),
                },
            }

            resp = _r.post(
                f"{api_url}/internal/push",
                json=payload,
                headers={"x-bot-key": cfg.dashboard_key},
                timeout=8,
            )
            resp.raise_for_status()
            log.info(f"📤 Данные отправлены в API | Unrealised P&L: ${total_upnl:+.2f} | Cash ${float(cash_usd):.2f} | Equity ${float(equity_usd):.2f}")
        except Exception as e:
            log.warning(f"Push to API failed: {e}")

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
    _logger.setup(logs_dir=cfg.logs_dir)
    Prometheus().run()
