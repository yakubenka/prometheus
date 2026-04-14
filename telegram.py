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
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._base = f"https://api.telegram.org/bot{token}"

    def send(self, text: str, silent: bool = False) -> bool:
        blocked_markers = (
            "Wikipedia surge",
            "🚨 *BREAKING NEWS*",
            "🚨 Breaking",
            "\nBreaking\n",
            "Breaking\n",
            "Breaking",
        )
        if any(marker in text for marker in blocked_markers):
            return False
        if not self.enabled:
            return False
        try:
            r = _SESSION.post(
                f"{self._base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text[:4096],
                    "parse_mode": "Markdown",
                    "disable_notification": silent,
                },
                timeout=8,
            )
            if r.status_code != 200:
                log.warning(f"Telegram HTTP {r.status_code}: {r.text[:160]}")
                return False
            return True
        except requests.exceptions.Timeout:
            log.warning("Telegram timeout")
        except Exception as e:
            log.warning(f"Telegram error: {e}")
        return False

    def started(self, dry_run: bool) -> None:
        mode = "🟡 DRY RUN" if dry_run else "🔴 LIVE"
        self.send(f"🤖 *Prometheus запущен* — {mode}")

    def stopped(self) -> None:
        self.send("⛔️ Prometheus остановлен")

    def error(self, msg: str) -> None:
        self.send(f"🚨 *Ошибка*\n`{msg[:3500]}`")

    def opened(self, q: str, d: str, p: float, size: float, why: str = "", slug: str | None = None) -> None:
        url = f"\n🔗 https://polymarket.com/event/{slug}" if slug else ""
        extra = f"\n_{why[:250]}_" if why else ""
        self.send(f"✅ *Открыта позиция*\n{q}\n*{d}* @ `{p:.3f}` на `${size:.2f}`{extra}{url}")

    def closed(self, q: str, d: str, pnl: float) -> None:
        sign = "+" if pnl >= 0 else ""
        self.send(f"💰 *Позиция закрыта*\n{q}\n*{d}* → `{sign}${pnl:.2f}`")

    def review(self, q: str, upnl: float, captured_pct: float, trigger: str, prob: float, reason: str) -> None:
        sign = "+" if upnl >= 0 else ""
        self.send(
            "🔎 *Обзор — ДЕРЖИ*\n\n"
            f"{q}\n\n"
            f"📉 uP&L: *{sign}${upnl:.2f}* | Захвачено: *{captured_pct:.0f}%*\n"
            f"Триггер: `{trigger}` | Prob: *{prob:.0f}%*\n"
            f"_{reason[:240]}_"
        )

    def breaking(self, items: list[str]) -> None:
        return

    def limit_warning(self, pct: float) -> None:
        self.send(f"⚠️ Дневной лимит потерь: *{pct:.0%}* использовано")

    def weights_updated(self, weights: dict) -> None:
        lines = ["🧠 *Веса сигналов обновлены*"]
        for name, w in weights.items():
            lines.append(f"  {name}: {w:.0%}")
        self.send("\n".join(lines))

    def cycle_summary(self, ai_trades: int, sm_trades: int, daily_pnl: float) -> None:
        sign = "+" if daily_pnl >= 0 else ""
        self.send(
            f"📊 Цикл завершён\nAI trades: *{ai_trades}*\nSmart-money trades: *{sm_trades}*\nDaily PnL: *{sign}${daily_pnl:.2f}*",
            silent=True,
        )

    def arbitrage(self, title: str, content: str) -> None:
        self.send(f"⚖️ *Арбитраж*\n*{title}*\n{content[:500]}")

    def daily_report(self, snap: dict, by_tag: dict, signals: int, sm_alerts: int) -> None:
        lines = [
            "📅 *Ежедневный отчёт*",
            f"Total PnL: *${snap.get('total_pnl', 0):.2f}*",
            f"Daily PnL: *${snap.get('daily_pnl', 0):.2f}*",
            f"Signals: *{signals}*",
            f"Smart-money alerts: *{sm_alerts}*",
        ]
        if by_tag:
            lines.append("")
            lines.append("*By tag:*")
            for tag, val in list(by_tag.items())[:8]:
                lines.append(f"• {tag}: ${val:.2f}")
        self.send("\n".join(lines))
