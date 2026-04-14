"""
Prometheus v3 — Risk Manager
Защита капитала. Никакой сделки без прохождения всех проверок.
Audit log каждого решения. PostgreSQL persistence.
"""

from __future__ import annotations
import json
import logging
import hashlib
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("prometheus.risk")

REASON_OK              = ""
REASON_DAILY_LOSS      = "daily_loss_limit_reached"
REASON_MAX_POSITIONS   = "max_open_positions"
REASON_DUPLICATE       = "duplicate_market"
REASON_CORRELATED      = "correlated_exposure_limit"
REASON_SIZE_TOO_SMALL  = "size_below_minimum"
REASON_SIZE_TOO_LARGE  = "size_above_maximum"
REASON_BOT_PAUSED      = "bot_paused"


@dataclass
class Position:
    market_id:    str
    question:     str
    direction:    str
    entry_price:  float
    size_usd:     float
    opened_at:    str          # ISO
    tags:         list[str]
    status:       str          = "open"
    closed_at:    Optional[str]   = None
    exit_price:   Optional[float] = None
    pnl:          Optional[float] = None
    signal_type:  str          = "ai"
    token_id:     Optional[str]   = None   # для realtime P&L
    slug:         Optional[str]   = None   # для прямой ссылки на Polymarket


@dataclass
class RiskDecision:
    allowed:    bool
    reason:     str
    size_usd:   float          # после всех корректировок
    audit_hash: str = ""       # для аудит-лога

    @classmethod
    def allow(cls, size: float) -> "RiskDecision":
        return cls(True, REASON_OK, size)

    @classmethod
    def deny(cls, reason: str) -> "RiskDecision":
        return cls(False, reason, 0.0)


class RiskManager:
    """
    Единая точка принятия решений о размере позиции.
    Все решения логируются. Ничего не пропускается без проверки.
    """

    def __init__(
        self,
        max_position_usd:   float = 20.0,
        max_daily_loss_usd: float = 50.0,
        max_open_positions: int   = 5,
        max_correlated_usd: float = 35.0,
        kelly_fraction:     float = 0.20,
        data_dir:           str   = "/app/logs",
    ):
        self.max_position_usd   = max_position_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_open_positions = max_open_positions
        self.max_correlated_usd = max_correlated_usd
        self.kelly_fraction     = kelly_fraction
        self._dir               = Path(data_dir)
        self._paused            = False

        self._positions: list[Position] = []
        self._audit_log: list[dict]     = []
        self._db_init()
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def kelly_size(
        self,
        ai_probability: float,
        market_price:   float,
        direction:      str,
        bankroll:       float = 100.0,
    ) -> float:
        """Чистый Kelly. Всегда дробный (×KELLY_FRAC)."""
        p     = ai_probability if direction == "YES" else 1 - ai_probability
        price = market_price   if direction == "YES" else 1 - market_price
        price = max(price, 0.01)

        payout = (1 / price) - 1
        kelly  = (p * payout - (1 - p)) / payout
        kelly  = max(0.0, kelly)

        raw  = bankroll * kelly * self.kelly_fraction
        return round(min(raw, self.max_position_usd), 2)

    def check(
        self,
        market_id: str,
        size_usd:  float,
        tags:      list[str] = None,
    ) -> RiskDecision:
        """Все проверки перед сделкой. Вернёт deny с причиной если нельзя."""
        tags = tags or []

        if self._paused:
            return self._log_deny(market_id, REASON_BOT_PAUSED, size_usd)

        if self._daily_pnl() <= -self.max_daily_loss_usd:
            return self._log_deny(market_id, REASON_DAILY_LOSS, size_usd)

        if len(self.open_positions) >= self.max_open_positions:
            return self._log_deny(market_id, REASON_MAX_POSITIONS, size_usd)

        if any(p.market_id == market_id for p in self.open_positions):
            return self._log_deny(market_id, REASON_DUPLICATE, size_usd)

        if tags:
            correlated = sum(
                p.size_usd for p in self.open_positions
                if any(t in p.tags for t in tags)
            )
            if correlated + size_usd > self.max_correlated_usd:
                return self._log_deny(market_id, REASON_CORRELATED, size_usd)

        if size_usd < 1.0:
            return self._log_deny(market_id, REASON_SIZE_TOO_SMALL, size_usd)

        capped = min(size_usd, self.max_position_usd)
        self._audit("ALLOW", market_id, capped, REASON_OK)
        return RiskDecision.allow(capped)

    def open(
        self,
        market_id:   str,
        question:    str,
        direction:   str,
        entry_price: float,
        size_usd:    float,
        tags:        list[str] = None,
        signal_type: str       = "ai",
        token_id:    str       = None,
        slug:        str       = None,
    ) -> Position:
        pos = Position(
            market_id   = market_id,
            question    = question,
            direction   = direction,
            entry_price = entry_price,
            size_usd    = size_usd,
            opened_at   = datetime.now(timezone.utc).isoformat(),
            tags        = tags or [],
            signal_type = signal_type,
            token_id    = token_id,
            slug        = slug,
        )
        self._positions.append(pos)
        self._save()
        log.info(
            f"✅ Позиция открыта | {direction} @ {entry_price:.3f} "
            f"${size_usd:.2f} | {question[:50]}"
        )
        return pos

    def close(self, market_id: str, exit_price: float, won: bool) -> Optional[Position]:
        for pos in self._positions:
            if pos.market_id == market_id and pos.status == "open":
                price  = pos.entry_price if pos.direction == "YES" else 1 - pos.entry_price
                payout = (1 / max(price, 0.01)) - 1
                pnl    = pos.size_usd * payout if won else -pos.size_usd

                pos.status     = "closed"
                pos.closed_at  = datetime.now(timezone.utc).isoformat()
                pos.exit_price = exit_price
                pos.pnl        = round(pnl, 2)

                self._save()
                result = "WIN" if won else "LOSS"
                log.info(f"{'✅' if won else '❌'} {result} | P&L ${pnl:+.2f} | {pos.question[:50]}")
                return pos
        return None

    def pause(self):
        self._paused = True
        log.warning("⏸ Бот приостановлен")

    def resume(self):
        self._paused = False
        log.info("▶ Бот возобновлён")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> list[Position]:
        return [p for p in self._positions if p.status == "closed"]

    @property
    def is_paused(self) -> bool:
        return self._paused

    def snapshot(self) -> dict:
        daily  = self._daily_pnl()
        total  = sum(p.pnl or 0 for p in self.closed_positions)
        closed = self.closed_positions
        wins   = [p for p in closed if (p.pnl or 0) > 0]

        return {
            "open_positions":  len(self.open_positions),
            "open_exposure":   round(sum(p.size_usd for p in self.open_positions), 2),
            "daily_pnl":       round(daily, 2),
            "total_pnl":       round(total, 2),
            "total_trades":    len(closed),
            "win_rate":        round(len(wins)/len(closed), 3) if closed else 0,
            "daily_loss_pct":  round(abs(min(0, daily)) / self.max_daily_loss_usd, 3),
            "can_trade":       not self._paused and daily > -self.max_daily_loss_usd,
            "paused":          self._paused,
        }

    def performance_by_tag(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for p in self.closed_positions:
            for tag in p.tags:
                if tag not in result:
                    result[tag] = {"trades":0,"wins":0,"pnl":0.0,"vol":0.0}
                result[tag]["trades"] += 1
                if (p.pnl or 0) > 0:
                    result[tag]["wins"] += 1
                result[tag]["pnl"] += p.pnl or 0
                result[tag]["vol"] += p.size_usd
        for tag in result:
            s = result[tag]
            s["win_rate"] = round(s["wins"]/s["trades"], 3)
            s["roi"]      = round(s["pnl"]/s["vol"], 3) if s["vol"] else 0
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _daily_pnl(self) -> float:
        today = date.today().isoformat()
        return sum(
            p.pnl or 0
            for p in self.closed_positions
            if (p.closed_at or "")[:10] == today
        )

    def _log_deny(self, market_id: str, reason: str, size: float) -> RiskDecision:
        self._audit("DENY", market_id, size, reason)
        log.info(f"🚫 Сделка отклонена: {reason} | ${size:.2f} | {market_id[:16]}")
        return RiskDecision.deny(reason)

    def _audit(self, action: str, market_id: str, size: float, reason: str):
        entry = {
            "ts":        datetime.now(timezone.utc).isoformat(),
            "action":    action,
            "market_id": market_id,
            "size":      size,
            "reason":    reason,
        }
        entry["hash"] = hashlib.sha256(json.dumps(entry).encode()).hexdigest()[:16]
        self._audit_log.append(entry)

        try:
            audit_path = self._dir / "audit.jsonl"
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with audit_path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error(f"Audit log write failed: {e}")

    def _db_conn(self):
        """PostgreSQL соединение если доступно."""
        db_url = os.environ.get("DATABASE_URL","")
        if not db_url:
            return None
        try:
            import psycopg2
            if db_url.startswith("postgres://"):
                db_url = db_url.replace("postgres://","postgresql://",1)
            return psycopg2.connect(db_url)
        except Exception:
            return None

    def _db_init(self):
        """Создать таблицу позиций если не существует."""
        conn = self._db_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    market_id   TEXT PRIMARY KEY,
                    data        TEXT NOT NULL,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            log.debug(f"DB init positions: {e}")

    def _save(self):
        # Сохраняем в файл
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = [asdict(p) for p in self._positions]
            (self._dir / "positions.json").write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.error(f"Positions file save failed: {e}")

        # Сохраняем в PostgreSQL
        conn = self._db_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            for p in self._positions:
                cur.execute("""
                    INSERT INTO positions (market_id, data, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (market_id) DO UPDATE
                    SET data = EXCLUDED.data, updated_at = NOW()
                """, (p.market_id, json.dumps(asdict(p))))
            conn.commit(); cur.close(); conn.close()
        except Exception as e:
            log.debug(f"DB save positions: {e}")

    def _load(self):
        # Сначала пробуем PostgreSQL
        conn = self._db_conn()
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("SELECT data FROM positions ORDER BY updated_at DESC")
                rows = cur.fetchall()
                cur.close(); conn.close()
                if rows:
                    for row in rows:
                        d = json.loads(row[0])
                        self._positions.append(Position(**d))
                    log.info(f"Загружено {len(self._positions)} позиций из PostgreSQL")
                    return
            except Exception as e:
                log.debug(f"DB load positions: {e}")

        # Fallback — файл
        try:
            path = self._dir / "positions.json"
            if not path.exists():
                return
            for d in json.loads(path.read_text()):
                self._positions.append(Position(**d))
            log.info(f"Загружено {len(self._positions)} позиций из файла")
        except Exception as e:
            log.error(f"Positions load failed: {e}")
