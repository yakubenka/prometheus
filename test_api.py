"""
Тесты для api.py
Покрывает: rate limiter, store_set/store_get (in-memory),
           _mem eviction, _alive(), API endpoints через TestClient.
DATABASE_URL не задан → всё работает через _mem.
"""
import sys
import types
import os
import time
from unittest.mock import MagicMock, patch

# Убеждаемся что DATABASE_URL пустой (in-memory режим)
os.environ.pop("DATABASE_URL", None)
os.environ.pop("database_url", None)
os.environ.pop("DATABASE_PUBLIC_URL", None)
os.environ.setdefault("DASHBOARD_API_KEY", "test-key")
os.environ.setdefault("MANUAL_CLOSE_KEY", "manual-key")

if "psycopg2" not in sys.modules:
    sys.modules["psycopg2"] = MagicMock()
if "requests" not in sys.modules:
    _mock_requests = MagicMock()
    sys.modules["requests"] = _mock_requests
    sys.modules["requests.adapters"] = MagicMock()

import pytest
from fastapi.testclient import TestClient

from api import (
    app,
    _rate_limited,
    _client_ip,
    _alive,
    store_set,
    store_get,
    _mem,
    _MEM_MAX_KEYS,
    _rl_store,
)


BOT_KEY    = "test-key"
CLOSE_KEY  = "manual-key"
client     = TestClient(app)


# ── _rate_limited ──────────────────────────────────────────────────────────────

class TestRateLimited:
    def setup_method(self):
        _rl_store.clear()

    def test_first_request_allowed(self):
        assert _rate_limited("test-ip", limit=5, window=60) is False

    def test_within_limit_allowed(self):
        for _ in range(4):
            _rate_limited("ip-a", limit=5, window=60)
        assert _rate_limited("ip-a", limit=5, window=60) is False

    def test_at_limit_blocked(self):
        for _ in range(5):
            _rate_limited("ip-b", limit=5, window=60)
        assert _rate_limited("ip-b", limit=5, window=60) is True

    def test_different_keys_independent(self):
        for _ in range(5):
            _rate_limited("ip-x", limit=5, window=60)
        # ip-y не тронут → должен быть разрешён
        assert _rate_limited("ip-y", limit=5, window=60) is False

    def test_window_expiry_allows_again(self):
        # Используем очень маленькое окно (1 секунда)
        for _ in range(3):
            _rate_limited("ip-exp", limit=3, window=1)
        assert _rate_limited("ip-exp", limit=3, window=1) is True

        # Ждём истечения окна — патчим time.time
        future = time.time() + 2
        with patch("api.time.time", return_value=future):
            _rate_limited("ip-exp", limit=3, window=1)  # пересчитывает окно
            assert _rate_limited("ip-exp", limit=3, window=1) is False


# ── _client_ip ────────────────────────────────────────────────────────────────

class TestClientIp:
    def test_uses_x_forwarded_for_header(self):
        req = MagicMock()
        req.headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        req.client   = None
        assert _client_ip(req) == "1.2.3.4"

    def test_falls_back_to_client_host(self):
        req = MagicMock()
        req.headers = {}
        req.client.host = "9.9.9.9"
        assert _client_ip(req) == "9.9.9.9"

    def test_returns_unknown_when_no_info(self):
        req = MagicMock()
        req.headers = {}
        req.client   = None
        assert _client_ip(req) == "unknown"


# ── store_set / store_get ─────────────────────────────────────────────────────

class TestStore:
    def setup_method(self):
        _mem.clear()

    def test_set_and_get_dict(self):
        store_set("overview", {"pnl": 10.0})
        assert store_get("overview") == {"pnl": 10.0}

    def test_set_and_get_list(self):
        store_set("signals", [{"edge": 0.1}])
        result = store_get("signals")
        assert result == [{"edge": 0.1}]

    def test_overwrite_existing_key(self):
        store_set("overview", {"pnl": 5.0})
        store_set("overview", {"pnl": 9.9})
        assert store_get("overview")["pnl"] == pytest.approx(9.9)

    def test_missing_key_returns_none(self):
        assert store_get("nonexistent") is None

    def test_mem_eviction_at_max_keys(self):
        _mem.clear()
        for i in range(_MEM_MAX_KEYS):
            store_set(f"key-{i}", {"v": i})
        # Все ключи должны быть в пределах лимита
        assert len(_mem) == _MEM_MAX_KEYS

        # Добавляем ещё один → старый должен быть вытеснен
        store_set("key-extra", {"v": "new"})
        assert len(_mem) == _MEM_MAX_KEYS
        assert "key-extra" in _mem

    def test_mem_not_grows_beyond_max(self):
        _mem.clear()
        for i in range(_MEM_MAX_KEYS + 50):
            store_set(f"dyn-{i}", {"v": i})
        assert len(_mem) <= _MEM_MAX_KEYS


# ── _alive ────────────────────────────────────────────────────────────────────

class TestAlive:
    def setup_method(self):
        _mem.clear()

    def test_false_when_no_last_push(self):
        assert _alive() is False

    def test_true_when_recent_push(self):
        from datetime import datetime, timezone
        _mem["last_push"] = datetime.now(timezone.utc).isoformat()
        assert _alive() is True

    def test_false_when_push_is_old(self):
        from datetime import datetime, timezone, timedelta
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        _mem["last_push"] = old
        assert _alive() is False


# ── API endpoints ─────────────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_returns_ok(self):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert "bot_alive" in data
        assert "db" in data

    def test_health_db_is_memory(self):
        r = client.get("/api/health")
        assert r.json()["db"] == "memory"


class TestOverviewEndpoint:
    def setup_method(self):
        _mem.clear()

    def test_returns_default_when_empty(self):
        r = client.get("/api/overview")
        assert r.status_code == 200
        data = r.json()
        assert "pnl_today" in data
        assert "bot_running" in data

    def test_returns_stored_data(self):
        store_set("overview", {"pnl_today": 42.0, "win_rate": 0.75})
        r = client.get("/api/overview")
        assert r.status_code == 200
        assert r.json()["pnl_today"] == pytest.approx(42.0)


class TestSignalsEndpoint:
    def setup_method(self):
        _mem.clear()

    def test_returns_empty_list_when_no_signals(self):
        r = client.get("/api/signals")
        assert r.status_code == 200
        assert r.json()["signals"] == []

    def test_returns_stored_signals(self):
        store_set("signals", [{"market_id": "m1", "edge": 0.12}])
        r = client.get("/api/signals")
        assert r.status_code == 200
        assert len(r.json()["signals"]) == 1

    def test_limit_parameter_respected(self):
        store_set("signals", [{"id": i} for i in range(20)])
        r = client.get("/api/signals?limit=5")
        assert len(r.json()["signals"]) == 5


class TestPositionsEndpoint:
    def setup_method(self):
        _mem.clear()

    def test_returns_default_structure(self):
        r = client.get("/api/positions")
        assert r.status_code == 200
        data = r.json()
        assert "open" in data
        assert "closed_today" in data

    def test_returns_stored_positions(self):
        store_set("positions", {"open": [{"id": "m1"}], "closed_today": []})
        r = client.get("/api/positions")
        assert r.status_code == 200
        assert len(r.json()["open"]) == 1


class TestClosePositionEndpoint:
    def setup_method(self):
        _mem.clear()

    def test_requires_auth(self):
        r = client.post("/api/close_position", json={"market_id": "m1"})
        assert r.status_code in (401, 422, 503)

    def test_rejects_missing_market_id(self):
        r = client.post(
            "/api/close_position",
            json={},
            headers={"x-manual-close-key": CLOSE_KEY},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_rejects_invalid_market_id_format(self):
        r = client.post(
            "/api/close_position",
            json={"market_id": "../../etc/passwd"},
            headers={"x-manual-close-key": CLOSE_KEY},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "invalid" in r.json()["error"]

    def test_position_not_found(self):
        store_set("positions", {"open": [], "closed_today": [], "history": []})
        r = client.post(
            "/api/close_position",
            json={"market_id": "mkt-nonexistent"},
            headers={"x-manual-close-key": CLOSE_KEY},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_close_position_success(self):
        store_set("positions", {
            "open": [{
                "market_id": "mkt-001",
                "direction": "YES",
                "price": 0.40,
                "current_price": 0.60,
                "size": 10.0,
            }],
            "closed_today": [],
            "history": [],
        })
        r = client.post(
            "/api/close_position",
            json={"market_id": "mkt-001"},
            headers={"x-manual-close-key": CLOSE_KEY},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["pnl"] == pytest.approx(5.0, rel=1e-3)

    def test_close_position_removes_from_open(self):
        store_set("positions", {
            "open": [{"market_id": "mkt-001", "direction": "YES",
                      "price": 0.50, "current_price": 0.50, "size": 10.0}],
            "closed_today": [], "history": [],
        })
        client.post(
            "/api/close_position",
            json={"market_id": "mkt-001"},
            headers={"x-manual-close-key": CLOSE_KEY},
        )
        positions = store_get("positions")
        assert all(p.get("market_id") != "mkt-001" for p in positions["open"])


class TestInternalPushEndpoint:
    def setup_method(self):
        _mem.clear()

    def test_requires_bot_auth(self):
        r = client.post("/internal/push", json={})
        assert r.status_code in (401, 422, 503)

    def test_push_overview(self):
        r = client.post(
            "/internal/push",
            json={"overview": {"pnl_today": 7.5}},
            headers={"x-bot-key": BOT_KEY},
        )
        assert r.status_code == 200
        assert store_get("overview")["pnl_today"] == pytest.approx(7.5)

    def test_push_signals(self):
        r = client.post(
            "/internal/push",
            json={"signals": [{"edge": 0.10}]},
            headers={"x-bot-key": BOT_KEY},
        )
        assert r.status_code == 200
        assert len(store_get("signals")) == 1
