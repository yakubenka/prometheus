"""
Prometheus — Telegram Notifier
Простой, надёжный. Не падает если Telegram недоступен.
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

    def send(self, text: str, silent: bool = False) -> bool:
        if not self.enabled:
            return False
        try:
            r = _SESSION.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id":              self.chat_id,
                    "text":                 text[:4096],   # Telegram limit
                    "parse_mode":           "Markdown",
                    "disable_notification": silent,
                },
                timeout=8,
            )
            if r.status_code != 200:
                log.warning(f"Telegram HTTP {r.status_code}: {r.text[:100]}")
                return False
            return True
        except requests.exceptions.Timeout:
            log.warning("Telegram timeout")
        except Exception as e:
            log.warning(f"Telegram error: {e}")
        return False

    # ── Typed helpers ──────────────────────────────────────────────────────────

    def started(self, dry_run: bool) -> None:
        mode = "📝 DRY RUN" if dry_run else "🔴 LIVE TRADING"
        self.send(f"🚀 *Prometheus запущен*\nРежим: {mode}")

    def stopped(self) -> None:
        self.send("⛔ *Prometheus остановлен*")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Ошибка*\n```{msg[:300]}```")

    def trade(self, question: str, direction: str, price: float,
              size: float, edge: float, confidence: str,
              reasoning: str, dry_run: bool,
              signals: list = None) -> None:

        mode = "📋 PAPER" if dry_run else "⚡ LIVE"
        dir_icon = "🟢 LONG" if direction == "YES" else "🔴 SHORT"
        conf_icon = {"high": "🔥", "medium": "✅", "low": "⚡"}.get(confidence, "•")

        # Ожидаемый профит если рынок резолвится в нашу пользу
        if direction == "YES":
            expected_win = round(size * (1/max(price, 0.01) - 1), 2)
        else:
            expected_win = round(size * (1/max(1-price, 0.01) - 1), 2)
        expected_loss = size  # максимальная потеря

        lines = [
            f"{mode} TRADE",
            f"{'─'*32}",
            f"",
            f"*{question}*",
            f"",
            f"{dir_icon}  `{price:.3f}`  →  size *${size:.2f}*",
            f"",
            f"📊 *Анализ:*",
            f"  Edge:         *{edge:.1%}*",
            f"  Уверенность:  {conf_icon} *{confidence.upper()}*",
            f"  Потенциал:    +${expected_win:.2f} / −${expected_loss:.2f}",
            f"  R/R:          *1:{round(expected_win/max(expected_loss,0.01), 1)}*",
        ]

        # Сигналы — только значимые
        if signals:
            active = [s for s in signals
                      if s.direction != "NEUTRAL"
                      and s.reasoning
                      and s.reasoning not in ("error", "insufficient history", "no Kalshi match", "no PredictIt match")]
            if active:
                lines.append(f"")
                lines.append(f"🧠 *Почему вошли:*")
                for s in active[:4]:
                    arrow = "↑" if s.direction == "YES" else "↓"
                    conf_pct = f"{s.confidence:.0%}"
                    lines.append(f"  `{s.name:<12}` {arrow} {s.reasoning[:90]}")

        # Ключевой вывод из reasoning
        if reasoning:
            key_parts = [p.strip() for p in reasoning.split("·") if p.strip() and "[" not in p]
            if key_parts:
                lines.append(f"")
                lines.append(f"💡 *Ключевой тезис:*")
                lines.append(f"  _{key_parts[0][:150]}_")

        lines.append(f"")
        lines.append(f"{'─'*32}")

        self.send("\n".join(lines))

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        dir_icon = "🟢" if direction == "YES" else "🔴"
        self.send(
            f"🔎 *SMART MONEY SIGNAL*\n"
            f"{'─'*28}\n\n"
            f"*{question[:80]}*\n\n"
            f"{dir_icon} *{direction}* @ `{price:.3f}` | *${size:.2f}*\n\n"
            f"🧠 _{reasoning[:150]}_"
        )

    def closed(self, question: str, direction: str,
               outcome: str, pnl: float) -> None:
        won = pnl >= 0
        icon = "✅" if won else "❌"
        result = "WIN" if won else "LOSS"
        pnl_str = f"+${pnl:.2f}" if won else f"−${abs(pnl):.2f}"
        self.send(
            f"{icon} *{result}*\n"
            f"{'─'*28}\n\n"
            f"*{question[:80]}*\n\n"
            f"Ставка: *{direction}* → *{outcome}*\n"
            f"P&L: *{pnl_str}*"
        )

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"🎯 *АРБИТРАЖ*\n{'─'*28}\n\n{title}\n_{content}_")

    def breaking(self, items: list[str]) -> None:
        return

    def limit_warning(self, pct: float) -> None:
        self.send(f"⚠️ Дневной лимит потерь: *{pct:.0%}* использовано")

    def weights_updated(self, weights: dict) -> None:
        lines = ["🧠 *Веса сигналов обновлены*"]
        for name, w in weights.items():
            lines.append(f"  {name}: {w:.0%}")
        self.send("\n".join(lines))

    def daily_report(self, snap: dict, by_tag: dict,
                     signals: int, sm_alerts: int) -> None:
        from datetime import date
        lines = [
            f"📊 *Дневной отчёт* | {date.today()}",
            "",
            f"P&L сегодня:  *${snap['daily_pnl']:+.2f}*",
            f"P&L всего:    *${snap['total_pnl']:+.2f}*",
            f"Win rate:     {snap['win_rate']:.1%}",
            f"Trades:       {snap['total_trades']}",
            f"Open:         {snap['open_positions']}",
            "",
            f"Сигналов: {signals} | SM: {sm_alerts}",
        ]
        if by_tag:
            lines.append("\n*По доменам:*")
            for tag, s in sorted(by_tag.items(),
                                  key=lambda x: x[1]["pnl"], reverse=True):
                if s["trades"] >= 2:
                    lines.append(
                        f"  {tag}: WR {s['win_rate']:.0%} "
                        f"P&L ${s['pnl']:+.1f} ({s['trades']})"
                    )
        self.send("\n".join(lines))

    def cycle_summary(self, ai: int, sm: int, pnl: float) -> None:
        self.send(
            f"🔄 Цикл завершён\n"
            f"AI сделок: {ai} | SM: {sm}\n"
            f"P&L сегодня: ${pnl:+.2f}",
            silent=True,
        )
