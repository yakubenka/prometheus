"""
Prometheus — Telegram Notifier v3.6
Чистый минималистичный дизайн уведомлений.
"""
from __future__ import annotations
import logging
import requests

log = logging.getLogger("prometheus.telegram")

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "Prometheus/3.0"


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
        except Exception as e:
            log.warning(f"Telegram error: {e}")
        return False

    # ── Системные ─────────────────────────────────────────────────────────────

    def started(self, dry_run: bool) -> None:
        mode = "Paper Trading" if dry_run else "🔴 LIVE"
        self.send(f"🤖 *Prometheus* запущен — {mode}")

    def stopped(self) -> None:
        self.send("⛔ Prometheus остановлен")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Ошибка*\n`{msg[:200]}`")

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
        dir_icon  = "🟢" if direction == "YES" else "🔴"
        conf_icon = {"high": "🔥", "medium": "✅", "low": "⚡"}.get(confidence, "")
        src       = "AI" if signal_type == "ai" else "Smart Money"
        mode      = "PAPER" if dry_run else "LIVE"

        token_price  = price if direction == "YES" else 1.0 - price
        expected_win = round(size * ((1.0 / max(token_price, 0.01)) - 1), 2)

        lines = [
            f"{dir_icon} *{mode} · {src}*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"Направление:  *{direction}*  @  `{price:.3f}`",
            f"Размер:       *${size:.2f}*  →  потенциал *+${expected_win:.2f}*",
            f"Edge:         *{edge:.1%}*  {conf_icon} {confidence}",
        ]

        # Главный сигнал одной строкой
        if signals:
            active = [s for s in signals
                      if s.direction != "NEUTRAL"
                      and s.reasoning not in ("error", "insufficient history",
                                              "no Kalshi match", "no PredictIt match")]
            if active:
                top = active[0]
                lines += ["", f"💡 _{top.reasoning[:120]}_"]

        if url:
            lines += ["", f"[Открыть на Polymarket]({url})"]

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
        won      = pnl > 0
        icon     = "✅" if won else ("🛑" if outcome == "STOP_LOSS" else "❌")
        result   = "ПОБЕДА" if won else ("СТОП-ЛОСС" if outcome == "STOP_LOSS" else "ПОТЕРЯ")
        pnl_str  = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
        src      = "AI" if signal_type == "ai" else "Smart Money"

        lines = [
            f"{icon} *{src} — {result}*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"*{pnl_str}*  |  {direction}  @  `{entry_price:.3f}` → `{exit_price:.3f}`",
        ]

        if url:
            lines += ["", f"[Polymarket]({url})"]

        self.send("\n".join(lines))

    # ── Smart Money ───────────────────────────────────────────────────────────

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        dir_icon = "🟢" if direction == "YES" else "🔴"
        self.send(
            f"🔎 *Smart Money*\n\n"
            f"*{question[:80]}*\n\n"
            f"{dir_icon} {direction}  @  `{price:.3f}`  →  *${size:.2f}*\n\n"
            f"_{reasoning[:150]}_"
        )

    # ── Алерты ────────────────────────────────────────────────────────────────

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"🎯 *Арбитраж*\n\n{title}\n_{content}_")

    def breaking(self, items: list[str]) -> None:
        news = "\n".join(f"• {i[:80]}" for i in items[:3])
        self.send(f"🚨 *Breaking*\n\n{news}")

    def limit_warning(self, pct: float) -> None:
        self.send(f"⚠️ Лимит потерь использован на *{pct:.0%}*")

    def weights_updated(self, weights: dict) -> None:
        lines = ["🧠 *Веса сигналов*", ""]
        for name, w in sorted(weights.items(), key=lambda x: -x[1]):
            bar = "▓" * int(w * 15) + "░" * (15 - int(w * 15))
            lines.append(f"`{name:<12}` {bar} {w:.0%}")
        self.send("\n".join(lines), silent=True)

    def daily_report(self, snap: dict, by_tag: dict,
                     signals: int, sm_alerts: int) -> None:
        from datetime import date
        pnl_today = snap["daily_pnl"]
        pnl_total = snap["total_pnl"]
        icon = "📈" if pnl_today >= 0 else "📉"

        lines = [
            f"📊 *Отчёт* — {date.today()}",
            f"",
            f"{icon} Сегодня:  *{'+' if pnl_today>=0 else ''}${pnl_today:.2f}*",
            f"   Всего:   *{'+' if pnl_total>=0 else ''}${pnl_total:.2f}*",
            f"",
            f"Win rate:  *{snap['win_rate']:.0%}*  ({snap['total_trades']} сделок)",
            f"Открыто:  {snap['open_positions']}  |  Сигналов: {signals}",
        ]

        if by_tag:
            top = sorted(
                [(t, s) for t, s in by_tag.items() if s["trades"] >= 2],
                key=lambda x: x[1]["pnl"], reverse=True
            )[:4]
            if top:
                lines += ["", "*Домены:*"]
                for tag, s in top:
                    i = "✅" if s["pnl"] >= 0 else "❌"
                    lines.append(f"{i} {tag}: WR {s['win_rate']:.0%}  ${s['pnl']:+.1f}")

        self.send("\n".join(lines))

    def cycle_summary(self, ai: int, sm: int, pnl: float) -> None:
        if ai == 0 and sm == 0:
            return
        icon = "📈" if pnl >= 0 else "📉"
        self.send(
            f"🔄 Цикл: AI={ai} SM={sm}  {icon} ${pnl:+.2f}",
            silent=True,
        )

    def trade(self, question: str, direction: str, price: float,
              size: float, edge: float, confidence: str,
              reasoning: str, dry_run: bool,
              signals: list = None) -> None:
        self.position_opened(
            question=question, direction=direction, price=price,
            size=size, edge=edge, confidence=confidence,
            reasoning=reasoning, dry_run=dry_run,
            signals=signals, signal_type="ai",
        )
