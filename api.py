"""
Prometheus — API v3
PostgreSQL хранилище. Данные не теряются при деплое.
"""
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Prometheus", version="3.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

API_KEY      = os.environ.get("DASHBOARD_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
            if "_list" in val: return val["_list"]
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
    if p.overview:    store_set("overview", p.overview)
    if p.signals is not None: store_set("signals", {"_list": p.signals})
    if p.positions:   store_set("positions", p.positions)
    if p.smart_money: store_set("smart_money", p.smart_money)
    if p.learning:    store_set("learning", p.learning)
    _mem["last_push"] = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

# ── Read ───────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    t = db_last_push() if DATABASE_URL else _mem.get("last_push")
    return {"ok": True, "bot_alive": _alive(),
            "last_push": t.isoformat() if hasattr(t,"isoformat") else t,
            "db": "postgres" if DATABASE_URL else "memory",
            "ts": datetime.utcnow().isoformat()}

@app.get("/api/overview")
def overview():
    d = store_get("overview")
    if d:
        d = dict(d); d["bot_running"] = _alive(); return d
    return {"bot_running":_alive(),"dry_run":True,"pnl_today":0,"pnl_total":0,
            "win_rate":0,"total_trades":0,"open_positions":0,"open_exposure":0,
            "bankroll":float(os.environ.get("BANKROLL","100")),"daily_loss_used":0,
            "signals_today":0,"smart_money_today":0}

@app.get("/api/signals")
def signals(limit: int = 30):
    d = store_get("signals")
    if isinstance(d, list): return {"signals": d[:limit]}
    if isinstance(d, dict) and "_list" in d: return {"signals": d["_list"][:limit]}
    return {"signals": []}

@app.get("/api/positions")
def positions():
    return store_get("positions") or {"open":[],"closed_today":[]}

@app.get("/api/smart_money")
def smart_money():
    return store_get("smart_money") or {"traders":[]}

@app.get("/api/learning")
def learning():
    return store_get("learning") or {"signal_stats":{}}

@app.get("/api/audit")
def audit():
    return {"entries":[]}
