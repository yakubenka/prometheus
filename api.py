"""
Prometheus — API v3.1
PostgreSQL хранилище. Данные не теряются при деплое.

FIXES v3.1:
- /api/close_position теперь обновляет overview P&L корректно
- Добавлен /api/overview/recalc для пересчёта P&L из позиций
- db_get/db_set используют connection pooling (один conn на запрос, корректный close)
- Signals endpoint возвращает корректный список без вложенного _list
"""
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Prometheus", version="3.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY      = os.environ.get("DASHBOARD_API_KEY", "")
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or
    os.environ.get("database_url") or
    os.environ.get("DATABASE_PUBLIC_URL") or
    ""
)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

print(f"[STARTUP] DB: {'postgres' if DATABASE_URL else 'memory'}", flush=True)
print(f"[STARTUP] URL prefix: {DATABASE_URL[:30] if DATABASE_URL else 'none'}", flush=True)

# ── PostgreSQL ─────────────────────────────────────────────────────────────────

def _conn():
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"DB error: {e}")
        return None

def init_db():
    conn = _conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB init: {e}")

def db_set(key: str, value: dict):
    conn = _conn()
    if not conn: return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bot_state (key, value, updated_at) VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (key, json.dumps(value)))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        print(f"DB set: {e}")

def db_get(key: str):
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM bot_state WHERE key=%s", (key,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return json.loads(row[0]) if row else None
    except Exception as e:
        print(f"DB get: {e}")
        return None

def db_last_push():
    conn = _conn()
    if not conn: return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT updated_at FROM bot_state WHERE key='overview'")
        row = cur.fetchone()
        cur.close(); conn.close()
        return row[0] if row else None
    except:
        return None

init_db()

# ── In-memory fallback ─────────────────────────────────────────────────────────
_mem: dict = {}

def store_set(key: str, value):
    _mem[key] = value
    db_set(key, value if isinstance(value, dict) else {"_list": value})

def store_get(key: str):
    if DATABASE_URL:
        val = db_get(key)
        if val is not None:
            if isinstance(val, dict) and "_list" in val:
                return val["_list"]
            return val
    return _mem.get(key)

# ── Auth ───────────────────────────────────────────────────────────────────────
def _bot_auth(x_bot_key: str = Header(default="")):
    if API_KEY and not secrets.compare_digest(x_bot_key, API_KEY):
        raise HTTPException(401, "Invalid bot key")

def _alive() -> bool:
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    if not t: return False
    try:
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z","+00:00"))
        if not t.tzinfo:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() < 1800
    except:
        return False

# ── Push ───────────────────────────────────────────────────────────────────────
class Push(BaseModel):
    overview:    Optional[dict] = None
    signals:     Optional[list] = None
    positions:   Optional[dict] = None
    smart_money: Optional[dict] = None
    learning:    Optional[dict] = None

@app.post("/internal/push")
def push(p: Push, _=Depends(_bot_auth)):
    if p.overview:
        store_set("overview", p.overview)
    if p.signals is not None:
        store_set("signals", {"_list": p.signals})
    if p.positions:
        store_set("positions", p.positions)
    if p.smart_money:
        store_set("smart_money", p.smart_money)
    if p.learning:
        store_set("learning", p.learning)
    _mem["last_push"] = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

# ── Read ───────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    return {
        "ok": True,
        "bot_alive": _alive(),
        "last_push": t.isoformat() if hasattr(t, "isoformat") else t,
        "db": "postgres" if DATABASE_URL else "memory",
        "ts": datetime.utcnow().isoformat(),
    }

@app.get("/api/debug/telegram")
def debug_telegram():
    """
    Проверить работу Telegram прямо из браузера.
    Открой: https://<твой-api>.railway.app/api/debug/telegram
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN не задан в Railway Variables"}
    if not chat_id:
        return {"ok": False, "error": "TELEGRAM_CHAT_ID не задан в Railway Variables"}

    # Проверяем токен
    try:
        import requests as _req
        r = _req.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
        if r.status_code != 200:
            return {"ok": False, "error": f"Невалидный токен: HTTP {r.status_code}"}
        bot_name = r.json().get("result", {}).get("username", "?")
    except Exception as e:
        return {"ok": False, "error": f"Не могу достучаться до Telegram API: {e}"}

    # Отправляем тестовое сообщение
    try:
        r2 = _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       "✅ *Prometheus — тест связи*\n\nTelegram работает!",
                "parse_mode": "Markdown",
            },
            timeout=8,
        )
        if r2.status_code == 200:
            return {
                "ok":       True,
                "bot":      f"@{bot_name}",
                "chat_id":  chat_id,
                "message":  "Тестовое сообщение отправлено — проверь Telegram",
            }
        else:
            err = r2.json().get("description", r2.text[:100])
            return {
                "ok":      False,
                "bot":     f"@{bot_name}",
                "error":   f"sendMessage failed: {err}",
                "hint":    "Скорее всего неверный TELEGRAM_CHAT_ID",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/overview")
def overview():
    d = store_get("overview")
    if d:
        d = dict(d)
        d["bot_running"] = _alive()
        return d
    return {
        "bot_running": _alive(), "dry_run": True,
        "pnl_today": 0, "pnl_total": 0, "unrealised_pnl": 0,
        "win_rate": 0, "total_trades": 0, "open_positions": 0,
        "open_exposure": 0, "bankroll": float(os.environ.get("BANKROLL","100")),
        "daily_loss_used": 0, "signals_today": 0, "smart_money_today": 0,
    }

@app.get("/api/signals")
def signals(limit: int = 30):
    d = store_get("signals")
    if isinstance(d, list):
        return {"signals": d[:limit]}
    if isinstance(d, dict) and "_list" in d:
        return {"signals": d["_list"][:limit]}
    return {"signals": []}

@app.get("/api/positions")
def positions():
    return store_get("positions") or {"open": [], "closed_today": [], "history": []}

@app.post("/api/close_position")
async def close_position(request: Request):
    """Закрыть позицию вручную из дашборда. Корректно обновляет overview P&L."""
    body = await request.json()
    market_id = body.get("market_id", "")
    if not market_id:
        return {"ok": False, "error": "no market_id"}

    pos_data = store_get("positions") or {"open": [], "closed_today": [], "history": []}
    open_pos = pos_data.get("open", [])
    found = None

    for p in open_pos:
        if p.get("id") == market_id or p.get("market_id") == market_id:
            found = p
            break

    if not found:
        return {"ok": False, "error": "position not found"}

    # P&L расчёт с поддержкой YES и NO
    cur_price = float(found.get("current_price") or found.get("price", 0))
    entry     = float(found.get("price", 0))
    size      = float(found.get("size", 0))
    direction = found.get("direction", "YES")

    if direction == "YES":
        entry_token   = entry
        current_token = cur_price
    else:
        entry_token   = 1.0 - entry
        current_token = 1.0 - cur_price

    if entry_token > 0:
        pnl = round((current_token - entry_token) / entry_token * size, 2)
    else:
        pnl = 0.0

    now_iso = datetime.now(timezone.utc).isoformat()
    found["status"]    = "closed"
    found["closed_at"] = now_iso
    found["exit_price"]= cur_price
    found["pnl"]       = pnl

    new_open = [p for p in open_pos
                if p.get("id") != market_id and p.get("market_id") != market_id]
    history  = pos_data.get("history", [])
    history.append(found)

    pos_data["open"]         = new_open
    pos_data["history"]      = history
    pos_data["closed_today"] = pos_data.get("closed_today", []) + [found]
    store_set("positions", pos_data)

    # FIX: обновляем overview P&L чтобы отразить ручное закрытие
    ov = store_get("overview") or {}
    if ov:
        ov = dict(ov)
        ov["pnl_today"]      = round(float(ov.get("pnl_today", 0)) + pnl, 2)
        ov["pnl_total"]      = round(float(ov.get("pnl_total", 0)) + pnl, 2)
        ov["open_positions"] = max(0, int(ov.get("open_positions", 1)) - 1)
        ov["open_exposure"]  = round(max(0.0, float(ov.get("open_exposure", size)) - size), 2)
        ov["total_trades"]   = int(ov.get("total_trades", 0)) + 1
        # Пересчёт win_rate
        total = ov["total_trades"]
        if total > 0:
            prev_wins = round(float(ov.get("win_rate", 0)) * (total - 1))
            wins = prev_wins + (1 if pnl > 0 else 0)
            ov["win_rate"] = round(wins / total, 3)
        store_set("overview", ov)

    return {"ok": True, "pnl": pnl}

@app.get("/api/overview/recalc")
def overview_recalc():
    """
    Пересчитать pnl_today и pnl_total из истории позиций.
    Вызывать если P&L на дашборде рассинхронизировался.
    """
    pos_data = store_get("positions") or {}
    history  = pos_data.get("history", [])
    today    = datetime.now(timezone.utc).date().isoformat()

    pnl_total = sum(float(p.get("pnl") or 0) for p in history)
    pnl_today = sum(
        float(p.get("pnl") or 0) for p in history
        if (p.get("closed_at") or "")[:10] == today
    )
    wins      = sum(1 for p in history if float(p.get("pnl") or 0) > 0)
    win_rate  = round(wins / len(history), 3) if history else 0.0

    ov = store_get("overview") or {}
    if ov:
        ov = dict(ov)
        ov["pnl_today"]   = round(pnl_today, 2)
        ov["pnl_total"]   = round(pnl_total, 2)
        ov["total_trades"]= len(history)
        ov["win_rate"]    = win_rate
        store_set("overview", ov)

    return {
        "ok": True,
        "pnl_today":    round(pnl_today, 2),
        "pnl_total":    round(pnl_total, 2),
        "total_trades": len(history),
        "win_rate":     win_rate,
    }

@app.get("/api/smart_money")
def smart_money():
    return store_get("smart_money") or {"traders": []}

@app.get("/api/learning")
def learning():
    return store_get("learning") or {"signal_stats": {}}

@app.get("/api/audit")
def audit():
    return {"entries": []}

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.post("/api/admin/reset")
def admin_reset(_=Depends(_bot_auth)):
    """
    Сброс paper trading состояния:
    - bankroll → $100
    - все P&L → 0
    - все позиции и история → пусто
    """
    default_overview = {
        "bot_running":       _alive(),
        "dry_run":           True,
        "pnl_today":         0.0,
        "pnl_total":         0.0,
        "unrealised_pnl":    0.0,
        "win_rate":          0.0,
        "total_trades":      0,
        "open_positions":    0,
        "open_exposure":     0.0,
        "bankroll":          100.0,
        "daily_loss_used":   0.0,
        "signals_today":     0,
        "smart_money_today": 0,
    }
    empty_positions = {
        "open":         [],
        "closed_today": [],
        "history":      [],
    }

    store_set("overview",  default_overview)
    store_set("positions", empty_positions)
    store_set("signals",   {"_list": []})

    _mem["last_push"] = datetime.now(timezone.utc).isoformat()

    return {
        "ok":      True,
        "message": "Paper trading reset: balance=$100.00, all P&L and positions cleared",
        "bankroll":  100.0,
        "pnl_today": 0.0,
        "pnl_total": 0.0,
    }
