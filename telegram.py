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
        icon = "📝" if dry_run else "💰"
        tag  = "PAPER" if dry_run else "🔴 LIVE"

        lines = [
            f"{icon} *{tag} — Новая позиция*",
            f"",
            f"*{question[:80]}*",
            f"",
            f"▸ Направление: *{direction}* @ `{price:.3f}`",
            f"▸ Размер: *${size:.2f}* | Edge: *{edge:.1%}*",
            f"▸ Уверенность: *{confidence}*",
        ]

        # Reasoning от сигналов
        if signals:
            lines.append(f"")
            lines.append(f"🧠 *Почему открыли:*")
            for s in signals:
                if s.reasoning and s.reasoning not in ("error", "insufficient history", "no Kalshi match"):
                    icon_s = "↑" if s.direction == "YES" else "↓" if s.direction == "NO" else "→"
                    lines.append(f"`{s.name}` {icon_s} {s.reasoning[:100]}")
        elif reasoning:
            lines.append(f"")
            lines.append(f"🧠 *Reasoning:*")
            # Разбиваем reasoning на части если он содержит · разделители
            parts = reasoning.split(" · ")
            for part in parts[:3]:
                if part.strip():
                    lines.append(f"• {part.strip()[:120]}")

        self.send("\n".join(lines))

    def smart_money(self, question: str, direction: str,
                    price: float, size: float, reasoning: str) -> None:
        self.send(
            f"🔎 *Smart Money*\n"
            f"{question[:65]}\n\n"
            f"{direction} @ `{price:.3f}` | `${size:.2f}`\n"
            f"_{reasoning[:120]}_"
        )

    def closed(self, question: str, direction: str,
               outcome: str, pnl: float) -> None:
        icon = "✅" if pnl >= 0 else "❌"
        result = "WIN" if pnl >= 0 else "LOSS"
        self.send(
            f"{icon} *{result} — Позиция закрыта*\n\n"
            f"*{question[:80]}*\n\n"
            f"▸ Ставка: *{direction}* → Исход: *{outcome}*\n"
            f"▸ P&L: *${pnl:+.2f}*"
        )

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"🎯 *Арбитраж*\n{title}\n_{content}_")

    def breaking(self, items: list[str]) -> None:
        lines = ["🚨 *Breaking Intel*"] + [f"• {i[:80]}" for i in items[:3]]
        self.send("\n".join(lines))

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
