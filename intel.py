"""
Prometheus — Intel Pipeline
Единый сборщик данных из всех источников.
Все DataPoints нормализованы. Дедупликация через SQLite.
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("prometheus.intel")


# ── HTTP Session ───────────────────────────────────────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; Prometheus/3.0)"
    retry = Retry(total=2, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

_S = _make_session()


# ── Domain detection ───────────────────────────────────────────────────────────

_DOMAIN_KW: dict[str, list[str]] = {
    "geopolitics": ["iran","israel","russia","ukraine","china","war","attack",
                    "military","nato","strike","missile","conflict","troops"],
    "macro":       ["fed","federal reserve","rate","inflation","cpi","gdp",
                    "unemployment","fomc","economy","recession","treasury","bonds"],
    "crypto":      ["bitcoin","btc","ethereum","eth","crypto","blockchain",
                    "solana","defi","nft","binance","coinbase"],
    "politics":    ["election","president","congress","senate","vote","trump",
                    "biden","democrat","republican","bill","law","ballot"],
    "sports":      ["nba","nfl","nhl","mlb","super bowl","championship",
                    "playoffs","game","match","season","tournament"],
}

def detect_domain(text: str) -> str:
    t = text.lower()
    scores = {d: sum(1 for kw in kws if kw in t) for d, kws in _DOMAIN_KW.items()}
    best   = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"

def relevance_score(text: str, domain: str) -> float:
    t    = text.lower()
    kws  = _DOMAIN_KW.get(domain, [])
    hits = sum(1 for kw in kws if kw in t)
    return min(1.0, hits * 0.12 + 0.2)

def extract_entities(text: str) -> list[str]:
    caps  = re.findall(r'\b[A-Z][a-z]{2,}\b', text)[:5]
    pcts  = re.findall(r'\d+\.?\d*%', text)[:2]
    dates = re.findall(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}\b', text)[:2]
    return list(dict.fromkeys(caps + pcts + dates))


# ── Data Model ─────────────────────────────────────────────────────────────────

@dataclass
class DataPoint:
    source:    str
    domain:    str
    title:     str
    content:   str
    url:       str
    timestamp: datetime
    sentiment: float = 0.0   # -1.0 … +1.0
    relevance: float = 0.0   # 0.0 … 1.0
    entities:  list  = field(default_factory=list)


# ── Storage ────────────────────────────────────────────────────────────────────

class IntelDB:
    """SQLite store. Дедупликация по URL."""

    _CREATE = """
        CREATE TABLE IF NOT EXISTS datapoints (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            source    TEXT NOT NULL,
            domain    TEXT NOT NULL,
            title     TEXT NOT NULL,
            content   TEXT,
            url       TEXT UNIQUE NOT NULL,
            ts        TEXT NOT NULL,
            sentiment REAL DEFAULT 0,
            relevance REAL DEFAULT 0,
            entities  TEXT DEFAULT '[]',
            created   TEXT DEFAULT (datetime('now','utc'))
        );
        CREATE INDEX IF NOT EXISTS idx_domain_ts ON datapoints(domain, ts);
        CREATE INDEX IF NOT EXISTS idx_relevance  ON datapoints(relevance DESC);
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False,
                                    isolation_level=None)   # autocommit
        self.conn.executescript(self._CREATE)

    def save(self, dp: DataPoint) -> bool:
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO datapoints"
                "(source,domain,title,content,url,ts,sentiment,relevance,entities)"
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (dp.source, dp.domain, dp.title[:500], dp.content[:2000],
                 dp.url[:500], dp.timestamp.isoformat(),
                 dp.sentiment, dp.relevance, json.dumps(dp.entities)),
            )
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except Exception as e:
            log.debug(f"DB save error: {e}")
            return False

    def recent(self, domain: str = None, hours: int = 12,
               limit: int = 50) -> list[DataPoint]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        if domain:
            rows = self.conn.execute(
                "SELECT source,domain,title,content,url,ts,sentiment,relevance,entities"
                " FROM datapoints WHERE domain=? AND ts>?"
                " ORDER BY relevance DESC, ts DESC LIMIT ?",
                (domain, since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT source,domain,title,content,url,ts,sentiment,relevance,entities"
                " FROM datapoints WHERE ts>?"
                " ORDER BY relevance DESC, ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT domain, COUNT(*) FROM datapoints"
            " WHERE ts > datetime('now','-24 hours','utc') GROUP BY domain"
        ).fetchall()
        return dict(rows)

    def _row(self, r: tuple) -> DataPoint:
        return DataPoint(
            source=r[0], domain=r[1], title=r[2], content=r[3], url=r[4],
            timestamp=datetime.fromisoformat(r[5]),
            sentiment=r[6], relevance=r[7],
            entities=json.loads(r[8] or "[]"),
        )


# ── RSS helper ─────────────────────────────────────────────────────────────────

def _parse_ts(raw: str) -> datetime:
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()

def _fetch_rss(url: str, domain: str, max_items: int = 10) -> list[DataPoint]:
    results = []
    try:
        r = _S.get(url, timeout=8)
        if r.status_code != 200:
            return []
        root  = ET.fromstring(r.content)
        items = root.findall(".//item")
        for item in items[:max_items]:
            title = _strip_html(item.findtext("title") or "")
            link  = (item.findtext("link") or url).strip()
            desc  = _strip_html(item.findtext("description") or "")[:400]
            ts    = _parse_ts(item.findtext("pubDate") or "")
            if not title:
                continue
            text = title + " " + desc
            results.append(DataPoint(
                source=f"rss_{domain}", domain=domain,
                title=title, content=desc, url=link, timestamp=ts,
                relevance=relevance_score(text, domain),
                entities=extract_entities(text),
            ))
    except ET.ParseError as e:
        log.debug(f"RSS parse error {url}: {e}")
    except Exception as e:
        log.debug(f"RSS error {url}: {e}")
    return results


# ── Source definitions ─────────────────────────────────────────────────────────

_RSS_SOURCES: dict[str, list[str]] = {
    "geopolitics": [
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
    "macro": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
    ],
    "politics": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "https://feeds.reuters.com/Reuters/PoliticsNews",
        "https://rss.politico.com/politics-news.xml",
    ],
    "crypto": [
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
    ],
    "sports": [
        "https://www.espn.com/espn/rss/news",
    ],
}

_TWITTER_ACCOUNTS: dict[str, list[str]] = {
    "geopolitics": ["Reuters", "BBCBreaking", "OSINTdefender"],
    "macro":       ["federalreserve", "NickTimiraos", "ReutersBiz"],
    "crypto":      ["whale_alert", "WuBlockchain", "CoinDesk"],
    "politics":    ["NBCNews", "politico", "axios"],
}

_NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.net",
]

_FRED_SERIES: dict[str, tuple[str, str]] = {
    "FEDFUNDS": ("macro", "Fed Funds Rate"),
    "CPIAUCSL": ("macro", "CPI"),
    "UNRATE":   ("macro", "Unemployment Rate"),
    "GDP":      ("macro", "US GDP"),
    "T10Y2Y":   ("macro", "10Y-2Y Spread"),
}

_TG_CHANNELS: dict[str, list[str]] = {
    "geopolitics": [
        "https://rsshub.app/telegram/channel/intelslava",
        "https://rsshub.app/telegram/channel/wartranslated",
    ],
    "crypto": [
        "https://rsshub.app/telegram/channel/wublockchain",
    ],
}


# ── Individual fetchers ────────────────────────────────────────────────────────

def _fetch_twitter(username: str, domain: str) -> list[DataPoint]:
    for instance in _NITTER_INSTANCES:
        try:
            url = f"{instance}/{username}/rss"
            r   = _S.get(url, timeout=8)
            if r.status_code != 200:
                continue
            root  = ET.fromstring(r.content)
            items = root.findall(".//item")
            results = []
            cutoff  = datetime.now(timezone.utc) - timedelta(hours=6)
            for item in items[:15]:
                title = _strip_html(item.findtext("title") or "")
                link  = item.findtext("link") or f"https://twitter.com/{username}"
                ts    = _parse_ts(item.findtext("pubDate") or "")
                if ts < cutoff or len(title) < 20:
                    continue
                clean = re.sub(r"https?://\S+", "", title).strip()
                results.append(DataPoint(
                    source="twitter", domain=domain,
                    title=f"@{username}: {clean[:120]}",
                    content=clean, url=link, timestamp=ts,
                    relevance=min(1.0, relevance_score(clean, domain) * 1.2),
                    entities=extract_entities(clean),
                ))
            return results
        except Exception as e:
            log.debug(f"Nitter {instance}/{username}: {e}")
    return []


def _fetch_fred(api_key: str) -> list[DataPoint]:
    if not api_key:
        return []
    results = []
    for series_id, (domain, name) in _FRED_SERIES.items():
        try:
            r = _S.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": api_key,
                        "file_type": "json", "sort_order": "desc", "limit": 2},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            obs = r.json().get("observations", [])
            if len(obs) < 2:
                continue
            cur  = obs[0]["value"]
            prev = obs[1]["value"]
            if cur == "." or prev == ".":
                continue
            cur, prev = float(cur), float(prev)
            delta  = cur - prev
            arrow  = "▲" if delta > 0 else "▼" if delta < 0 else "→"
            results.append(DataPoint(
                source="fred", domain=domain,
                title=f"[FRED] {name}: {cur:.2f} {arrow} ({delta:+.2f})",
                content=f"{series_id}: current={cur:.3f}, prev={prev:.3f}, Δ={delta:+.3f}",
                url=f"https://fred.stlouisfed.org/series/{series_id}",
                timestamp=datetime.now(timezone.utc),
                sentiment=min(1.0, max(-1.0, delta / max(abs(prev), 0.01))),
                relevance=0.85,
                entities=[series_id, name.split()[0]],
            ))
            time.sleep(0.2)
        except Exception as e:
            log.debug(f"FRED {series_id}: {e}")
    return results


def _fetch_crypto() -> list[DataPoint]:
    try:
        r = _S.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbols": '["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT"]'},
            timeout=8,
        )
        if r.status_code != 200:
            return []
        results = []
        for t in r.json():
            sym    = t["symbol"].replace("USDT", "")
            price  = float(t["lastPrice"])
            change = float(t["priceChangePercent"])
            vol    = float(t["quoteVolume"])
            icon   = "🟢" if change > 0 else "🔴"
            results.append(DataPoint(
                source="binance", domain="crypto",
                title=f"[Binance] {sym}: ${price:,.2f} {icon} {change:+.1f}%",
                content=f"{sym} price={price:.2f} change={change:+.2f}% vol_24h=${vol/1e6:.0f}M",
                url=f"https://www.binance.com/en/trade/{sym}_USDT",
                timestamp=datetime.now(timezone.utc),
                sentiment=min(1.0, max(-1.0, change / 10)),
                relevance=0.9 if abs(change) > 3 else 0.55,
                entities=[sym, "crypto"],
            ))
        return results
    except Exception as e:
        log.debug(f"Binance: {e}")
        return []


def _fetch_reddit(domain: str) -> list[DataPoint]:
    subs = {"macro": ["economics","investing"], "crypto": ["CryptoCurrency","Bitcoin"],
            "geopolitics": ["worldnews","geopolitics"], "politics": ["politics"]}
    results = []
    for sub in subs.get(domain, [])[:2]:
        try:
            r = _S.get(
                f"https://www.reddit.com/r/{sub}/hot.json",
                params={"limit": 8},
                headers={"User-Agent": "Prometheus/3.0"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            for post in r.json().get("data", {}).get("children", []):
                d     = post["data"]
                score = d.get("score", 0)
                if score < 150:
                    continue
                title = d.get("title", "")
                results.append(DataPoint(
                    source="reddit", domain=domain,
                    title=f"r/{sub}: {title[:100]}",
                    content=(d.get("selftext") or "")[:300] or title,
                    url=f"https://reddit.com{d.get('permalink','')}",
                    timestamp=datetime.fromtimestamp(
                        d.get("created_utc", 0), tz=timezone.utc),
                    relevance=min(1.0, score / 5000),
                    entities=extract_entities(title),
                ))
        except Exception as e:
            log.debug(f"Reddit r/{sub}: {e}")
    return results


def _fetch_polymarket_movers() -> list[DataPoint]:
    try:
        r = _S.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active":"true","closed":"false","limit":50,
                    "order":"volume24hr","ascending":"false"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        results = []
        for m in r.json():
            try:
                prices = m.get("outcomePrices")
                if isinstance(prices, str):
                    prices = json.loads(prices)
                if not prices:
                    continue
                yes  = float(prices[0])
                prev = float(m.get("lastTradedPrice") or yes)
                move = abs(yes - prev)
                if move < 0.04:
                    continue
                q   = m.get("question") or m.get("title", "")
                vol = float(m.get("volume24hr") or 0)
                results.append(DataPoint(
                    source="polymarket_mover", domain=detect_domain(q),
                    title=f"{'📈' if yes > prev else '📉'} PM: {q[:80]}",
                    content=f"YES {prev:.2%}→{yes:.2%} (Δ{move:+.2%}) vol=${vol:,.0f}",
                    url=f"https://polymarket.com/market/{m.get('id','')}",
                    timestamp=datetime.now(timezone.utc),
                    sentiment=0.3 if yes > prev else -0.3,
                    relevance=min(1.0, move * 4 + vol / 200_000),
                    entities=extract_entities(q),
                ))
            except Exception:
                continue
        return sorted(results, key=lambda x: x.relevance, reverse=True)[:10]
    except Exception as e:
        log.debug(f"Polymarket movers: {e}")
        return []


def _fetch_telegram_channels() -> list[DataPoint]:
    results = []
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=4)
    for domain, feeds in _TG_CHANNELS.items():
        for url in feeds:
            try:
                r = _S.get(url, timeout=8)
                if r.status_code != 200:
                    continue
                root    = ET.fromstring(r.content)
                channel = url.split("/")[-1]
                for item in root.findall(".//item")[:8]:
                    title = _strip_html(item.findtext("title") or "")
                    desc  = _strip_html(item.findtext("description") or "")[:400]
                    link  = item.findtext("link") or url
                    ts    = _parse_ts(item.findtext("pubDate") or "")
                    if ts < cutoff:
                        continue
                    text = (title + " " + desc).strip()
                    if len(text) < 30:
                        continue
                    results.append(DataPoint(
                        source="telegram", domain=domain,
                        title=f"[TG @{channel}] {text[:100]}",
                        content=desc or title, url=link, timestamp=ts,
                        relevance=min(1.0, relevance_score(text, domain) * 1.3),
                        entities=extract_entities(text),
                    ))
            except Exception as e:
                log.debug(f"Telegram {url}: {e}")
    return results


# ── Master Pipeline ────────────────────────────────────────────────────────────

class IntelPipeline:
    """
    Запускает все источники, сохраняет в DB.
    Возвращает контекст для AI анализа любого рынка.
    """

    def __init__(self, fred_api_key: str = "",
                 twitter_on: bool = True,
                 data_dir: str = "/app/logs") -> None:
        self.fred_key   = fred_api_key
        self.twitter_on = twitter_on
        self.db         = IntelDB(f"{data_dir}/intel.db")

    def run_full(self) -> dict[str, int]:
        """Полный сбор. Запускается каждые 15 минут."""
        log.info("📡 Intel pipeline — запуск...")
        counts: dict[str, int] = {}

        # RSS
        for domain, feeds in _RSS_SOURCES.items():
            pts   = []
            for url in feeds:
                pts.extend(_fetch_rss(url, domain))
                time.sleep(0.3)
            saved = sum(1 for dp in pts if self.db.save(dp))
            counts[f"rss_{domain}"] = saved

        # Twitter
        if self.twitter_on:
            for domain, accounts in _TWITTER_ACCOUNTS.items():
                total = 0
                for acc in accounts:
                    pts    = _fetch_twitter(acc, domain)
                    total += sum(1 for dp in pts if self.db.save(dp))
                    time.sleep(1.0)
                counts[f"twitter_{domain}"] = total

        # FRED
        pts   = _fetch_fred(self.fred_key)
        counts["fred"] = sum(1 for dp in pts if self.db.save(dp))

        # Crypto
        pts   = _fetch_crypto()
        counts["crypto"] = sum(1 for dp in pts if self.db.save(dp))

        # Reddit
        for domain in ["macro", "crypto", "geopolitics"]:
            pts   = _fetch_reddit(domain)
            counts[f"reddit_{domain}"] = sum(1 for dp in pts if self.db.save(dp))

        # Polymarket movers
        pts   = _fetch_polymarket_movers()
        counts["pm_movers"] = sum(1 for dp in pts if self.db.save(dp))

        # Telegram
        pts   = _fetch_telegram_channels()
        counts["telegram"] = sum(1 for dp in pts if self.db.save(dp))

        total = sum(counts.values())
        log.info(f"📡 Intel: {total} новых | {self.db.stats()}")
        return counts

    def context_for(self, question: str, hours: int = 12) -> str:
        """Собрать релевантный контекст для AI анализа рынка."""
        domain   = detect_domain(question)
        q_words  = set(question.lower().split())
        points   = self.db.recent(domain=domain, hours=hours, limit=40)

        scored: list[tuple[float, DataPoint]] = []
        for dp in points:
            text    = (dp.title + " " + dp.content).lower()
            overlap = len(q_words & set(text.split())) / max(len(q_words), 1)
            score   = dp.relevance * 0.5 + overlap * 0.3 + 0.2
            if score > 0.2:
                scored.append((score, dp))

        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return "No recent relevant intelligence found."

        lines = [f"Intelligence ({len(scored[:8])} items, last {hours}h):"]
        for score, dp in scored[:8]:
            age = int((datetime.now(timezone.utc) - dp.timestamp).total_seconds() / 3600)
            lines.append(f"[{dp.source} {age}h | {score:.2f}] {dp.title}")
            if dp.content and dp.content != dp.title:
                lines.append(f"  → {dp.content[:120]}")
        return "\n".join(lines)

    def breaking_news(self, hours: int = 1) -> list[DataPoint]:
        """Высокорелевантные свежие новости для алертов."""
        return [p for p in self.db.recent(hours=hours, limit=50)
                if p.relevance > 0.70]
