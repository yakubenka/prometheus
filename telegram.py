"""
Prometheus — Telegram Notifier v4.0
Только самое важное. Красиво. Информативно.

Отправляем:
- Открытие позиции (с сигналами, категорией, ликвидностью)
- Закрытие позиции (P&L, движение цены, время удержания)
- Position review: только если реальное действие (TAKE_PROFIT / CUT_LOSS)
- Smart money / insider detected
- Ежедневный и еженедельный отчёты
- Критические ошибки и лимиты

НЕ отправляем:
- Breaking news (триггерят доп. цикл, но без TG-спама)
- Обзор ДЕРЖИМ
- Microstructure signals
- Обновления весов
- Domain shift
- Cycle summary
"""
from __future__ import annotations
import logging
import requests

log = logging.getLogger("prometheus.telegram")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"

_STRATEGY_LABELS = {
    "ai":               "AI Ensemble",
    "ai_assisted":      "AI Assisted",
    "ensemble":         "AI Ensemble",
    "cross_market_gap": "Cross-Market",
    "market_data":      "Market Data",
    "smart_money":      "Smart Money",
    "near_resolution":  "Near Resolution",
}
_CATEGORY_LABELS = {
    "electoral":    "🗳 Electoral",
    "economic":     "📈 Economic",
    "geopolitical": "🌍 Geopolitical",
    "crypto":       "₿ Crypto",
}


class Telegram:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token   = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._base   = f"https://api.telegram.org/bot{token}"
        if not self.enabled:
            log.warning("Telegram отключён — TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы")

    def send(self, text: str, silent: bool = False) -> bool:
        if not self.enabled:
            return False
        import time as _time
        for attempt in range(3):
            try:
                r = _SESSION.post(
                    f"{self._base}/sendMessage",
                    json={
                        "chat_id":              self.chat_id,
                        "text":                 text[:4096],
                        "parse_mode":           "Markdown",
                        "disable_notification": silent,
                        "disable_web_page_preview": True,
                    },
                    timeout=8,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    retry_after = 5
                    try:
                        retry_after = r.json().get("parameters", {}).get("retry_after", 5)
                    except Exception:
                        pass
                    log.warning(f"Telegram flood limit — ждём {retry_after}с")
                    _time.sleep(retry_after)
                    continue
                log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
                return False
            except Exception as e:
                log.warning(f"Telegram error (попытка {attempt+1}/3): {e}")
                if attempt < 2:
                    _time.sleep(3)
        return False

    # ── Система ───────────────────────────────────────────────────────────────

    def started(self, dry_run: bool) -> None:
        mode = "Paper Trading" if dry_run else "🔴 LIVE"
        self.send(f"🤖 *Prometheus* запущен — {mode}")

    def stopped(self) -> None:
        self.send("⛔ Prometheus остановлен")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Ошибка*\n`{msg[:300]}`")

    def limit_warning(self, pct: float) -> None:
        self.send(f"⚠️ *Лимит потерь* использован на *{pct:.0%}*")

    # ── Открытие позиции ──────────────────────────────────────────────────────

    def position_opened(
        self,
        question:      str,
        direction:     str,
        price:         float,
        size:          float,
        edge:          float,
        confidence:    str,
        reasoning:     str,
        dry_run:       bool,
        signals:       list  = None,
        signal_type:   str   = "ai",
        url:           str   = "",
        domain_mult:   float = 1.0,
        timing:        str   = "",
        question_type: str   = "",
        liq_mult:      float = 1.0,
        liq_snap:      object = None,
    ) -> None:
        dir_icon  = "🟢" if direction == "YES" else "🔴"
        mode      = "PAPER" if dry_run else "LIVE"
        strategy  = _STRATEGY_LABELS.get(signal_type, signal_type.replace("_", " ").title())
        category  = _CATEGORY_LABELS.get(question_type, "")
        cat_str   = f"  ·  {category}" if category else ""

        # price is already the correct token price (NO token = ~0.73, not 1-0.73)
        potential    = round(size * ((1.0 / max(price, 0.01)) - 1), 2)
        conf_icon    = {"high": "🔥", "medium": "✅", "low": "⚡"}.get(confidence, "")

        lines = [
            f"{dir_icon} *{mode} · {strategy}{cat_str}*",
            f"",
            f"*{question[:90]}*",
            f"",
            f"Направление: *{direction}*  @  `{price:.3f}`",
            f"Размер: *${size:.2f}*   Потенциал: *+${potential:.2f}*",
            f"Edge: *{edge:.1%}*  {conf_icon} {confidence}",
        ]

        # Активные сигналы — топ-4, только направленные
        if signals:
            no_data = {"no Kalshi match", "no PredictIt match", "error",
                       "insufficient history", "no anomaly", "no intel",
                       "no domain tag", "no external match"}
            active = [
                s for s in signals
                if s.direction != "NEUTRAL"
                and s.reasoning not in no_data
                and not s.reasoning.startswith("error")
            ]
            if active:
                lines.append("")
                lines.append("*Сигналы:*")
                for s in active[:4]:
                    d_icon = "▲" if s.direction == "YES" else "▽"
                    w_str  = f"{s.weight:.0%}" if hasattr(s, "weight") and s.weight else ""
                    note   = s.reasoning[:70] if s.reasoning else ""
                    lines.append(f"  {d_icon} `{s.name:<12}` {w_str}  _{note}_")

        # Ликвидность если не идеальная
        if liq_snap is not None and liq_mult < 0.99:
            spread_pct = round(liq_snap.spread * 100, 1)
            depth      = min(liq_snap.bid_depth_100, liq_snap.ask_depth_100)
            lines.append("")
            lines.append(f"💧 Ликвидность: *{liq_mult:.2f}×*  "
                         f"(спред {spread_pct}%, глубина ${depth:.0f})")

        # Domain multiplier только при заметном отклонении
        if domain_mult > 1.15:
            lines.append(f"📊 Домен: *{domain_mult:.1f}×* (домен в плюсе)")
        elif domain_mult < 0.80:
            lines.append(f"📊 Домен: *{domain_mult:.1f}×* (домен слабый)")

        if url:
            lines += ["", f"[Открыть на Polymarket ↗]({url})"]

        self.send("\n".join(lines))

    # ── Закрытие позиции ──────────────────────────────────────────────────────

    def position_closed(
        self,
        question:    str,
        direction:   str,
        entry_price: float,
        exit_price:  float,
        size:        float,
        pnl:         float,
        outcome:     str,
        signal_type: str   = "ai",
        opened_at:   str   = None,
        url:         str   = "",
    ) -> None:
        won      = pnl > 0
        icon     = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
        label    = "ПОБЕДА" if won else ("СТОП-ЛОСС" if outcome == "STOP_LOSS" else "ПОТЕРЯ")
        pnl_str  = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
        strategy = _STRATEGY_LABELS.get(signal_type, signal_type.replace("_", " ").title())

        # Движение цены — считаем по токену позиции (NO инвертирован)
        entry_tok = entry_price if direction == "YES" else (1.0 - entry_price)
        exit_tok  = exit_price  if direction == "YES" else (1.0 - exit_price)
        move = exit_tok - entry_tok
        move_str = f"{'+' if move >= 0 else ''}{move:.3f}  ({'+' if move >= 0 else ''}{move/max(entry_tok,0.001)*100:.1f}%)"

        # Время удержания
        held_str = ""
        if opened_at:
            try:
                from datetime import datetime, timezone
                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                held_sec  = (datetime.now(timezone.utc) - opened_dt).total_seconds()
                held_h    = held_sec / 3600
                if held_h < 24:
                    held_str = f"{held_h:.0f}ч"
                else:
                    held_str = f"{held_h/24:.1f}д"
            except Exception:
                pass

        lines = [
            f"{icon} *{label}  {pnl_str}*",
            f"",
            f"*{question[:90]}*",
            f"",
        ]
        row = f"{direction}  `{entry_tok:.3f}` → `{exit_tok:.3f}`  Δ{move_str}"
        lines.append(row)

        meta = f"Размер: ${size:.2f}  ·  {strategy}"
        if held_str:
            meta += f"  ·  {held_str}"
        lines.append(meta)

        if url:
            lines += ["", f"[Polymarket ↗]({url})"]

        self.send("\n".join(lines))

    # ── Position Review — только реальные действия ────────────────────────────

    def position_review_hold(self, *args, **kwargs) -> None:
        """Тихо — не отправляем 'держим', это не нужная информация."""
        pass

    def position_review_action(
        self,
        question:    str,
        direction:   str,
        action:      str,
        entry_price: float,
        exit_price:  float,
        pnl:         float,
        reasoning:   str,
        url:         str = "",
    ) -> None:
        won   = pnl >= 0
        icon  = "💰" if action == "TAKE_PROFIT" else "✂️"
        label = "ФИКСАЦИЯ ПРИБЫЛИ" if action == "TAKE_PROFIT" else "СТОП ПО ТЕЗИСУ"
        pnl_str = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"

        lines = [
            f"{icon} *{label}  {pnl_str}*",
            f"",
            f"*{question[:90]}*",
            f"",
            f"{direction}  `{entry_price:.3f}` → `{exit_price:.3f}`",
            f"",
            f"_{reasoning[:140]}_",
        ]
        if url:
            lines += ["", f"[Polymarket ↗]({url})"]
        self.send("\n".join(lines))

    # ── Smart Money ───────────────────────────────────────────────────────────

    def insider_detected(
        self,
        question:     str,
        direction:    str,
        price:        float,
        their_size:   float,
        our_size:     float,
        trader_class: str,
        win_rate:     float,
        roi:          float,
        reasoning:    str,
        url:          str = "",
    ) -> None:
        dir_icon = "🟢" if direction == "YES" else "🔴"
        cls_icon = {"insider": "🔴", "sharp": "🟡", "contrarian": "🔵"}.get(
            trader_class.lower(), "🐋")
        their_str = f"${their_size/1000:.0f}k" if their_size >= 1000 else f"${their_size:.0f}"

        lines = [
            f"{cls_icon} *{trader_class.upper()} DETECTED*",
            f"",
            f"*{question[:90]}*",
            f"",
            f"{dir_icon} *{direction}*  @  `{price:.3f}`",
            f"Их ставка: *{their_str}*  ·  Наша: *${our_size:.2f}*",
            f"WR *{win_rate:.0%}*  ROI *{roi:+.0%}*",
            f"",
            f"_{reasoning[:140]}_",
        ]
        if url:
            lines += ["", f"[Polymarket ↗]({url})"]
        self.send("\n".join(lines))

    # ── Отчёты ────────────────────────────────────────────────────────────────

    def daily_report(self, snap: dict, by_tag: dict,
                     signals: int, sm_alerts: int,
                     domain_stats: dict = None) -> None:
        from datetime import date
        pnl_today = snap["daily_pnl"]
        pnl_total = snap["total_pnl"]
        icon      = "📈" if pnl_today >= 0 else "📉"

        pnl_t_str = f"{'+' if pnl_today>=0 else ''}${pnl_today:.2f}"
        pnl_all_str = f"{'+' if pnl_total>=0 else ''}${pnl_total:.2f}"

        lines = [
            f"📊 *Отчёт — {date.today()}*",
            f"",
            f"{icon} Сегодня: *{pnl_t_str}*   Всего: *{pnl_all_str}*",
            f"Win rate: *{snap['win_rate']:.0%}*  ({snap['total_trades']} сделок)",
            f"Открыто: *{snap['open_positions']}*  ·  Сигналов: {signals}",
        ]

        # По доменам
        stats = domain_stats or {}
        active = [(d, s) for d, s in stats.items()
                  if s.get("wins", 0) + s.get("losses", 0) >= 3]
        active.sort(key=lambda x: x[1].get("pnl", 0), reverse=True)
        if not active and by_tag:
            active = [(t, {"win_rate": s["win_rate"], "pnl": s["pnl"], "multiplier": 1.0})
                      for t, s in by_tag.items() if s["trades"] >= 2]
            active.sort(key=lambda x: x[1]["pnl"], reverse=True)

        if active:
            lines += ["", "*Домены:*"]
            for domain, s in active[:5]:
                mult    = s.get("multiplier", 1.0)
                m_str   = f" {mult:.1f}×" if abs(mult - 1.0) > 0.1 else ""
                pnl_d   = s.get("pnl", 0)
                wr      = s.get("win_rate", 0)
                d_icon  = "✅" if pnl_d >= 0 else "❌"
                lines.append(f"  {d_icon} `{domain:<12}` WR {wr:.0%}  {'+' if pnl_d>=0 else ''}${pnl_d:.1f}{m_str}")

        self.send("\n".join(lines))

    def weekly_report(self, snap: dict, domain_stats: dict = None) -> None:
        pnl_total = snap["total_pnl"]
        icon      = "📈" if pnl_total >= 0 else "📉"
        pnl_str   = f"{'+' if pnl_total>=0 else ''}${pnl_total:.2f}"

        lines = [
            f"📆 *Недельный итог*",
            f"",
            f"{icon} P&L: *{pnl_str}*",
            f"Win rate: *{snap['win_rate']:.0%}*  ({snap['total_trades']} сделок)",
            f"Открыто: *{snap['open_positions']}*",
        ]

        stats = domain_stats or {}
        hot  = [(d, s) for d, s in stats.items() if s.get("multiplier", 1.0) > 1.2]
        cold = [(d, s) for d, s in stats.items() if s.get("multiplier", 1.0) < 0.7]
        if hot:
            lines += ["", "🔥 *Горячие домены:*"]
            for d, s in sorted(hot, key=lambda x: -x[1].get("multiplier", 1))[:3]:
                lines.append(f"  {d}: *{s.get('multiplier',1):.1f}×*  WR {s.get('win_rate',0):.0%}")
        if cold:
            lines += ["", "❄️ *Слабые домены:*"]
            for d, s in sorted(cold, key=lambda x: x[1].get("multiplier", 1))[:3]:
                lines.append(f"  {d}: *{s.get('multiplier',1):.1f}×*  WR {s.get('win_rate',0):.0%}")

        self.send("\n".join(lines))

    # ── Заглушки (убраны, не спамим) ─────────────────────────────────────────

    def breaking(self, items: list[str]) -> None:
        pass

    def cycle_summary(self, ai: int, sm: int, pnl: float) -> None:
        pass

    def weights_updated(self, weights: dict) -> None:
        pass

    def domain_shift(self, **kwargs) -> None:
        pass

    def microstructure_signal(self, **kwargs) -> None:
        pass

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"🎯 *Арбитраж*\n\n{title}\n_{content}_", silent=True)

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        """Обратная совместимость."""
        self.insider_detected(
            question=question, direction=direction, price=price,
            their_size=size * 10, our_size=size, trader_class="sharp",
            win_rate=0.0, roi=0.0, reasoning=reasoning,
        )

    def trade(self, question: str, direction: str, price: float,
              size: float, edge: float, confidence: str,
              reasoning: str, dry_run: bool,
              signals: list = None) -> None:
        """Обратная совместимость."""
        self.position_opened(
            question=question, direction=direction, price=price,
            size=size, edge=edge, confidence=confidence,
            reasoning=reasoning, dry_run=dry_run,
            signals=signals, signal_type="ai",
        )
