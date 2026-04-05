"""
Prometheus — Telegram Notifier v3.2
Красивые, информативные уведомления для каждого события.

FIXES v3.2:
- trade() теперь отправляет при DRY RUN тоже (раньше молчал при некоторых условиях)
- Добавлен position_opened() — отдельное красивое уведомление при открытии
- Добавлен position_closed() — отдельное уведомление с P&L, R/R, итогом
- cycle_summary() теперь silent=False — виден пользователю
- Все методы добавляют Markdown форматирование для читаемости
- Добавлен debug_check() для диагностики
"""
from __future__ import annotations
import logging
import requests

log = logging.getLogger("prometheus.telegram")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"

# Эмодзи палитра
_DIR  = {"YES": "🟢", "NO": "🔴"}
_CONF = {"high": "🔥 HIGH", "medium": "✅ MED", "low": "⚡ LOW"}
_RES  = {"YES": "✅ YES", "NO": "❌ NO", "STOP_LOSS": "🛑 STOP"}


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
        try:
            r = _SESSION.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":              self.chat_id,
                    "text":                 text[:4096],
                    "parse_mode":           "Markdown",
                    "disable_notification": silent,
                },
                timeout=8,
            )
            if r.status_code == 200:
                return True
            log.warning(f"Telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        except requests.exceptions.Timeout:
            log.warning("Telegram timeout")
        except Exception as e:
            log.warning(f"Telegram error: {e}")
        return False

    def debug_check(self) -> str:
        """Диагностика — проверить что токен и chat_id рабочие."""
        if not self.token:
            return "❌ TELEGRAM_BOT_TOKEN не задан"
        if not self.chat_id:
            return "❌ TELEGRAM_CHAT_ID не задан"
        try:
            r = _SESSION.get(f"{self._base}/getMe", timeout=5)
            if r.status_code != 200:
                return f"❌ Bot token невалидный: {r.status_code}"
            bot_name = r.json().get("result", {}).get("username", "?")
            ok = self.send("🔧 Prometheus — тест соединения ✅")
            return f"✓ Bot @{bot_name} | send={'OK' if ok else 'FAIL'}"
        except Exception as e:
            return f"❌ {e}"

    # ── Системные ─────────────────────────────────────────────────────────────

    def started(self, dry_run: bool) -> None:
        mode = "📝 *PAPER TRADING*" if dry_run else "🔴 *LIVE TRADING*"
        self.send(
            f"🚀 *Prometheus запущен*\n"
            f"{'─'*30}\n"
            f"Режим: {mode}\n"
            f"Банкролл: $100 | Макс. позиция: $20\n"
            f"Сканирую рынки каждые 5 мин..."
        )

    def stopped(self) -> None:
        self.send("⛔ *Prometheus остановлен*")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Критическая ошибка*\n\n```\n{msg[:300]}\n```")

    # ── Открытие позиции ──────────────────────────────────────────────────────

    def position_opened(
        self,
        question:    str,
        direction:   str,
        price:       float,
        size:        float,
        edge:        float,
        confidence:  str,
        reasoning:   str,
        dry_run:     bool,
        signals:     list = None,
        signal_type: str  = "ai",
        url:         str  = "",
    ) -> None:
        """
        Красивое уведомление об открытии позиции.
        Вызывается сразу после _execute() — до следующего цикла.
        """
        mode     = "📋 PAPER" if dry_run else "⚡ LIVE"
        dir_icon = _DIR.get(direction, "•")
        conf_txt = _CONF.get(confidence, confidence.upper())

        # Ожидаемый выигрыш/проигрыш
        token_price = price if direction == "YES" else 1.0 - price
        expected_win  = round(size * ((1.0 / max(token_price, 0.01)) - 1), 2)
        expected_loss = size
        rr = round(expected_win / max(expected_loss, 0.01), 1)

        src = "🧠 AI" if signal_type == "ai" else "🔎 Smart Money"

        lines = [
            f"{mode} {src} — *ПОЗИЦИЯ ОТКРЫТА*",
            f"{'─'*32}",
            f"",
            f"*{question[:80]}*",
            f"",
            f"{dir_icon} *{direction}*  @  `{price:.3f}`  →  *${size:.2f}*",
            f"",
            f"📊 *Параметры:*",
            f"  Edge:        *{edge:.1%}*",
            f"  Уверенность: {conf_txt}",
            f"  Потенциал:   +${expected_win:.2f} / −${expected_loss:.2f}",
            f"  R/R:         *1 : {rr}*",
        ]

        # Топ сигналы
        if signals:
            active = [
                s for s in signals
                if s.direction != "NEUTRAL"
                and s.reasoning not in ("error", "insufficient history",
                                        "no Kalshi match", "no PredictIt match")
            ]
            if active:
                lines += ["", "🔬 *Сигналы:*"]
                for s in active[:4]:
                    arrow = "↑" if s.direction == "YES" else "↓"
                    conf_pct = f"{s.confidence:.0%}"
                    lines.append(f"  `{s.name:<12}` {arrow} ({conf_pct}) {s.reasoning[:70]}")

        # Ключевой тезис
        if reasoning:
            clean = [p.strip() for p in reasoning.split("·") if p.strip() and "[" not in p]
            if clean:
                lines += ["", f"💡 _{clean[0][:160]}_"]

        # Ссылка на рынок
        if url:
            lines += ["", f"🔗 [Открыть на Polymarket]({url})"]
        lines += [f"{'─'*32}"]
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
        signal_type: str = "ai",
        url:         str = "",
    ) -> None:
        """
        Красивое уведомление о закрытии позиции с полным P&L.
        """
        won       = pnl > 0
        icon      = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
        result    = "ПОБЕДА" if won else ("СТОП-ЛОСС" if outcome == "STOP_LOSS" else "ПОТЕРЯ")
        pnl_str   = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
        res_icon  = _RES.get(outcome, outcome)
        dir_icon  = _DIR.get(direction, "•")
        src       = "🧠 AI" if signal_type == "ai" else "🔎 Smart Money"

        # Движение цены
        price_move = exit_price - entry_price
        price_arrow = "↑" if price_move > 0 else "↓"

        lines = [
            f"{icon} {src} — *{result}*",
            f"{'─'*32}",
            f"",
            f"*{question[:80]}*",
            f"",
            f"{dir_icon} Ставка: *{direction}*  |  Исход: {res_icon}",
            f"",
            f"💰 *P&L: {pnl_str}*",
            f"",
            f"📈 *Детали:*",
            f"  Вход:   `{entry_price:.3f}`",
            f"  Выход:  `{exit_price:.3f}`  {price_arrow}",
            f"  Размер: ${size:.2f}",
            f"{'─'*32}",
        ]
        self.send("\n".join(lines))

    # ── Smart Money ───────────────────────────────────────────────────────────

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        dir_icon = _DIR.get(direction, "•")
        self.send(
            f"🔎 *SMART MONEY*\n"
            f"{'─'*30}\n\n"
            f"*{question[:80]}*\n\n"
            f"{dir_icon} *{direction}*  @  `{price:.3f}`  →  *${size:.2f}*\n\n"
            f"📋 _{reasoning[:200]}_"
        )

    # ── Системные алерты ──────────────────────────────────────────────────────

    def arbitrage(self, title: str, content: str) -> None:
        self.send(
            f"🎯 *АРБИТРАЖ*\n"
            f"{'─'*30}\n\n"
            f"{title}\n\n"
            f"_{content}_"
        )

    def breaking(self, items: list[str]) -> None:
        lines = [
            "🚨 *BREAKING NEWS*",
            f"{'─'*30}",
            "_Запускаю внеочередной анализ..._",
            "",
        ]
        for i, item in enumerate(items[:3], 1):
            lines.append(f"{i}. {item[:100]}")
        self.send("\n".join(lines))

    def limit_warning(self, pct: float) -> None:
        self.send(
            f"⚠️ *Лимит потерь*\n\n"
            f"Использовано *{pct:.0%}* дневного лимита.\n"
            f"Торговля продолжается осторожно."
        )

    def weights_updated(self, weights: dict) -> None:
        lines = ["🧠 *Веса сигналов обновлены*", ""]
        for name, w in sorted(weights.items(), key=lambda x: -x[1]):
            bar = "█" * int(w * 20)
            lines.append(f"  `{name:<12}` {bar} {w:.0%}")
        self.send("\n".join(lines), silent=True)

    def daily_report(self, snap: dict, by_tag: dict,
                     signals: int, sm_alerts: int) -> None:
        from datetime import date
        pnl_today = snap["daily_pnl"]
        pnl_total = snap["total_pnl"]
        icon_today = "📈" if pnl_today >= 0 else "📉"
        icon_total = "📈" if pnl_total >= 0 else "📉"

        lines = [
            f"📊 *Дневной отчёт* — {date.today()}",
            f"{'─'*30}",
            f"",
            f"{icon_today} P&L сегодня:  *{'+' if pnl_today>=0 else ''}${pnl_today:.2f}*",
            f"{icon_total} P&L всего:    *{'+' if pnl_total>=0 else ''}${pnl_total:.2f}*",
            f"",
            f"Win rate:   *{snap['win_rate']:.1%}*  ({snap['total_trades']} сделок)",
            f"Открыто:   {snap['open_positions']} позиций  (${snap['open_exposure']:.2f})",
            f"",
            f"Сигналов:  {signals}  |  Smart Money: {sm_alerts}",
        ]

        if by_tag:
            top_tags = sorted(
                [(t, s) for t, s in by_tag.items() if s["trades"] >= 2],
                key=lambda x: x[1]["pnl"], reverse=True
            )[:5]
            if top_tags:
                lines += ["", "*По доменам:*"]
                for tag, s in top_tags:
                    icon = "✅" if s["pnl"] >= 0 else "❌"
                    lines.append(
                        f"  {icon} `{tag:<12}` "
                        f"WR {s['win_rate']:.0%}  "
                        f"P&L ${s['pnl']:+.1f}  "
                        f"({s['trades']})"
                    )

        self.send("\n".join(lines))

    def cycle_summary(self, ai: int, sm: int, pnl: float) -> None:
        """Краткий итог цикла — только если что-то произошло."""
        if ai == 0 and sm == 0:
            return  # Тихий цикл — не спамим
        icon = "📈" if pnl >= 0 else "📉"
        self.send(
            f"🔄 *Цикл завершён*\n"
            f"Новых сделок: AI={ai}  SM={sm}\n"
            f"{icon} P&L сегодня: ${pnl:+.2f}",
            silent=False,
        )

    # ── Устаревший метод для обратной совместимости ───────────────────────────

    def trade(self, question: str, direction: str, price: float,
              size: float, edge: float, confidence: str,
              reasoning: str, dry_run: bool,
              signals: list = None) -> None:
        """Обёртка для обратной совместимости — делегирует в position_opened()."""
        self.position_opened(
            question=question, direction=direction, price=price,
            size=size, edge=edge, confidence=confidence,
            reasoning=reasoning, dry_run=dry_run,
            signals=signals, signal_type="ai",
        )
