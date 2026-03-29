"""
Prometheus — API v2
In-memory store: Bot пушит данные сюда, дашборд читает.
Решает проблему разных контейнеров на Railway.
"""
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Prometheus", version="3.1")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)

API_KEY = os.environ.get("DASHBOARD_API_KEY", "")

# ── In-memory store ────────────────────────────────────────────────────────────
_store = {
    "overview":    None,
    "signals":     [],
    "positions":   {"open": [], "closed_today": []},
    "smart_money": {"traders": []},
    "learning":    {"signal_stats": {}},
    "last_push":   None,
}

# ── Auth ───────────────────────────────────────────────────────────────────────
def _auth(x_api_key: str = Header(default="")):
    if API_KEY and not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")

def _bot_auth(x_bot_key: str = Header(default="")):
    if API_KEY and not secrets.compare_digest(x_bot_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid bot key")

def _bot_alive() -> bool:
    if not _store["last_push"]:
        return False
    try:
        last = datetime.fromisoformat(_store["last_push"])
        return (datetime.now(timezone.utc) - last).total_seconds() < 1200
    except Exception:
        return False

# ── Push (Bot → API) ───────────────────────────────────────────────────────────
class PushPayload(BaseModel):
    overview:    Optional[dict] = None
    signals:     Optional[list] = None
    positions:   Optional[dict] = None
    smart_money: Optional[dict] = None
    learning:    Optional[dict] = None

@app.post("/internal/push")
def push(payload: PushPayload, _=Depends(_bot_auth)):
    if payload.overview:    _store["overview"]    = payload.overview
    if payload.signals is not None: _store["signals"] = payload.signals
    if payload.positions:   _store["positions"]   = payload.positions
    if payload.smart_money: _store["smart_money"] = payload.smart_money
    if payload.learning:    _store["learning"]    = payload.learning
    _store["last_push"] = datetime.now(timezone.utc).isoformat()
    return {"ok": True}

# ── Read (Dashboard → API) ─────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"ok": True, "bot_alive": _bot_alive(),
            "last_push": _store["last_push"],
            "ts": datetime.utcnow().isoformat()}

@app.get("/api/overview")
def overview(_=Depends(_auth)):
    if _store["overview"]:
        data = dict(_store["overview"])
        data["bot_running"] = _bot_alive()
        return data
    return {
        "bot_running": _bot_alive(),
        "dry_run": os.environ.get("DRY_RUN","true").lower() == "true",
        "pnl_today": 0.0, "pnl_total": 0.0, "win_rate": 0.0,
        "total_trades": 0, "open_positions": 0, "open_exposure": 0.0,
        "bankroll": float(os.environ.get("BANKROLL","100")),
        "daily_loss_used": 0.0, "signals_today": 0, "smart_money_today": 0,
    }

@app.get("/api/signals")
def signals(limit: int = 30, _=Depends(_auth)):
    return {"signals": _store["signals"][:limit]}

@app.get("/api/positions")
def positions(_=Depends(_auth)):
    return _store["positions"]

@app.get("/api/smart_money")
def smart_money(_=Depends(_auth)):
    return _store["smart_money"]

@app.get("/api/learning")
def learning(_=Depends(_auth)):
    return _store["learning"]

@app.get("/api/audit")
def audit(_=Depends(_auth)):
    return {"entries": []}

@app.get("/api/backtest")
def backtest(_=Depends(_auth)):
    raise HTTPException(404, "Backtest not run yet")
