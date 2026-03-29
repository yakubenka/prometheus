"""
Prometheus — Extended Intel Sources
Дополнительные источники данных: prediction markets, betting odds,
institutional signals, Wikipedia, Google Trends, Economic Calendar.
"""
from __future__ import annotations
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional
import requests

from intel import (DataPoint, IntelDB, detect_domain,
                   relevance_score, extract_entities, _S, _strip_html, _parse_ts)

log = logging.getLogger("prometheus.intel_ext")


# ── Prediction Markets ─────────────────────────────────────────────────────────

def fetch_metaculus(limit: int = 20) -> list[DataPoint]:
    """Metaculus — лучшая калибровка среди всех prediction markets."""
    try:
        r = _S.get(
            "https://www.metaculus.com/api2/questions/",
            params={"status":"open","order_by":"-activity","limit":limit,"type":"forecast"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        results = []
        for q in r.json().get("results", []):
            title    = q.get("title", "")
            cp       = q.get("community_prediction") or {}
            prob_raw = cp.get("full", {}).get("q2") if isinstance(cp, dict) else cp
            if prob_raw is None:
                continue
            prob    = float(prob_raw)
            url     = f"https://www.metaculus.com/questions/{q.get('id','')}/"
            content = (f"Metaculus community: {prob:.1%}. "
                       f"Forecasters: {q.get('number_of_forecasters',0)}.")
            results.append(DataPoint(
                source="metaculus", domain=detect_domain(title),
                title=f"[Metaculus {prob:.0%}] {title[:100]}",
                content=content, url=url,
                timestamp=datetime.now(timezone.utc),
                sentiment=(prob - 0.5) * 2, relevance=0.80,
                entities=extract_entities(title),
            ))
        return results
    except Exception as e:
        log.debug(f"Metaculus: {e}")
        return []


def fetch_manifold(limit: int = 20) -> list[DataPoint]:
    """Manifold Markets — crypto-native prediction market."""
    try:
        r = _S.get(
            "https://api.manifold.markets/v0/markets",
            params={"limit": limit, "sort": "liquidity"},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        results = []
        for m in r.json():
            if m.get("outcomeType") != "BINARY":
                continue
            prob  = float(m.get("probability", 0.5))
            title = m.get("question", "")
            vol   = float(m.get("volume", 0))
            if vol < 100:
                continue
            results.append(DataPoint(
                source="manifold", domain=detect_domain(title),
                title=f"[Manifold {prob:.0%}] {title[:100]}",
                content=f"Manifold: {prob:.1%}. Volume: M${vol:.0f}. Traders: {m.get('uniqueBettorCount',0)}",
                url=m.get("url","https://manifold.markets"),
                timestamp=datetime.now(timezone.utc),
                sentiment=(prob - 0.5) * 2,
                relevance=min(0.85, vol / 5000),
                entities=extract_entities(title),
            ))
        return results
    except Exception as e:
        log.debug(f"Manifold: {e}")
        return []


# ── Institutional Signals ──────────────────────────────────────────────────────

_UNUSUAL_WHALES_FEEDS = [
    ("https://unusualwhales.com/rss/options",  "options",  0.70),
    ("https://unusualwhales.com/rss/congress", "congress", 0.90),
]

def fetch_unusual_whales() -> list[DataPoint]:
    """Options flow и сделки конгрессменов."""
    results = []
    for url, feed_type, base_relevance in _UNUSUAL_WHALES_FEEDS:
        try:
            r = _S.get(url, timeout=8)
            if r.status_code != 200:
                continue
            for item in ET.fromstring(r.content).findall(".//item")[:10]:
                title = _strip_html(item.findtext("title") or "")
                link  = item.findtext("link") or url
                desc  = _strip_html(item.findtext("description") or "")[:400]
                ts    = _parse_ts(item.findtext("pubDate") or "")
                if not title:
                    continue
                results.append(DataPoint(
                    source=f"unusual_whales_{feed_type}",
                    domain=detect_domain(title + " " + desc),
                    title=f"[{feed_type.upper()}] {title[:100]}",
                    content=desc, url=link, timestamp=ts,
                    sentiment=0.2 if "buy" in title.lower() else -0.2,
                    relevance=base_relevance,
                    entities=extract_entities(title + " " + desc),
                ))
        except Exception as e:
            log.debug(f"Unusual Whales {feed_type}: {e}")
    return results


def fetch_congress() -> list[DataPoint]:
    """Congress.gov — законопроекты и голосования США."""
    feeds = [
        "https://www.congress.gov/rss/most-viewed-bills.xml",
        "https://www.congress.gov/rss/senate-floor-today.xml",
        "https://www.congress.gov/rss/house-floor-today.xml",
    ]
    results = []
    for url in feeds:
        try:
            r = _S.get(url, timeout=8)
            if r.status_code != 200:
                continue
            for item in ET.fromstring(r.content).findall(".//item")[:8]:
                title = _strip_html(item.findtext("title") or "")
                link  = item.findtext("link") or url
                desc  = _strip_html(item.findtext("description") or "")[:300]
                if not title:
                    continue
                results.append(DataPoint(
                    source="congress", domain="politics",
                    title=f"[Congress] {title[:100]}",
                    content=desc or title, url=link,
                    timestamp=datetime.now(timezone.utc),
                    relevance=0.75, entities=extract_entities(title),
                ))
        except Exception as e:
            log.debug(f"Congress {url}: {e}")
    return results


# ── Real-time Signals ──────────────────────────────────────────────────────────

_WIKI_PAGES = [
    "Federal Reserve", "Israeli–Palestinian conflict",
    "Russo-Ukrainian War", "Bitcoin", "Donald Trump",
    "Federal funds rate", "Iran",
]

def fetch_wikipedia_changes() -> list[DataPoint]:
    """Страницы с высокой активностью правок = что-то происходит."""
    results = []
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=2)
    for page in _WIKI_PAGES[:6]:
        try:
            r = _S.get(
                "https://en.wikipedia.org/w/api.php",
                params={"action":"query","prop":"revisions","titles":page,
                        "rvprop":"timestamp|user|comment","rvlimit":5,"format":"json"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            for page_data in r.json().get("query",{}).get("pages",{}).values():
                revs   = page_data.get("revisions", [])
                recent = [rev for rev in revs
                          if _parse_ts(rev.get("timestamp","")) > cutoff]
                if len(recent) < 2:
                    continue
                title   = page_data.get("title", page)
                comment = revs[0].get("comment", "")
                results.append(DataPoint(
                    source="wikipedia", domain=detect_domain(title),
                    title=f"⚡ Wikipedia surge: {title} ({len(recent)} edits/2h)",
                    content=f"Latest: '{comment[:100]}' by {revs[0].get('user','')}",
                    url=f"https://en.wikipedia.org/wiki/{title.replace(' ','_')}",
                    timestamp=datetime.now(timezone.utc),
                    relevance=min(1.0, 0.5 + len(recent) * 0.12),
                    entities=extract_entities(title + " " + comment),
                ))
            time.sleep(0.2)
        except Exception as e:
            log.debug(f"Wikipedia {page}: {e}")
    return results


def fetch_economic_calendar() -> list[DataPoint]:
    """Предстоящие экономические события (CPI, FOMC, GDP)."""
    try:
        r = _S.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            timeout=8,
        )
        if r.status_code != 200:
            return []
        results = []
        for event in (r.json() if isinstance(r.json(), list) else []):
            impact  = event.get("impact", "")
            country = event.get("country", "")
            if impact not in ("High", "Medium"):
                continue
            if country not in ("USD", "EUR", "GBP"):
                continue
            title   = event.get("title", "")
            raw_dt  = event.get("date", "")
            try:
                ts = datetime.fromisoformat(raw_dt.replace("Z","+00:00"))
            except Exception:
                continue
            hours_until = (ts - datetime.now(timezone.utc)).total_seconds() / 3600
            if hours_until < 0 or hours_until > 168:
                continue
            forecast = event.get("forecast", "n/a")
            previous = event.get("previous", "n/a")
            results.append(DataPoint(
                source="economic_calendar", domain="macro",
                title=f"📅 [{impact}] {country}: {title} in {hours_until:.0f}h",
                content=f"Forecast: {forecast}, Previous: {previous}",
                url="https://www.forexfactory.com/calendar",
                timestamp=datetime.now(timezone.utc),
                relevance=0.92 if impact == "High" else 0.72,
                entities=extract_entities(f"{country} {title}"),
            ))
        return results
    except Exception as e:
        log.debug(f"Economic calendar: {e}")
        return []


def fetch_betting_odds(api_key: str) -> list[DataPoint]:
    """Betting odds от Pinnacle, Betfair через The Odds API."""
    if not api_key:
        return []
    sports = ["americanfootball_nfl", "basketball_nba", "baseball_mlb"]
    results = []
    for sport in sports:
        try:
            r = _S.get(
                f"https://api.the-odds-api.com/v4/sports/{sport}/odds/",
                params={"apiKey":api_key,"regions":"us,uk",
                        "markets":"h2h","oddsFormat":"decimal"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            for game in r.json()[:4]:
                home = game.get("home_team","")
                away = game.get("away_team","")
                bms  = game.get("bookmakers",[])
                if not bms:
                    continue
                home_odds, away_odds = [], []
                for bm in bms:
                    for mkt in bm.get("markets",[]):
                        if mkt.get("key") == "h2h":
                            for o in mkt.get("outcomes",[]):
                                if o["name"] == home:
                                    home_odds.append(float(o["price"]))
                                elif o["name"] == away:
                                    away_odds.append(float(o["price"]))
                if not home_odds or not away_odds:
                    continue
                avg_h = sum(home_odds) / len(home_odds)
                avg_a = sum(away_odds) / len(away_odds)
                hp    = (1/avg_h) / (1/avg_h + 1/avg_a)
                results.append(DataPoint(
                    source="betting_odds", domain="sports",
                    title=f"[Odds] {away} @ {home}",
                    content=(f"{home} win: {hp:.1%} (avg {avg_h:.2f}) | "
                             f"{away} win: {1-hp:.1%} (avg {avg_a:.2f}) | "
                             f"Books: {len(home_odds)}"),
                    url="https://the-odds-api.com",
                    timestamp=datetime.now(timezone.utc),
                    relevance=0.80,
                    entities=[home, away, sport.split("_")[1].upper()],
                ))
            time.sleep(0.3)
        except Exception as e:
            log.debug(f"Odds API {sport}: {e}")
    return results


def fetch_google_trends() -> list[DataPoint]:
    """Google Trends всплески как опережающий индикатор."""
    topics = {
        "macro":       ["federal reserve rate cut","inflation","recession 2026"],
        "geopolitics": ["iran attack","ukraine war news"],
        "crypto":      ["bitcoin price","ethereum"],
        "politics":    ["trump","congress vote"],
    }
    results = []
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))
        for domain, keywords in list(topics.items())[:2]:
            try:
                pt.build_payload(keywords[:3], timeframe="now 1-d", geo="US")
                data = pt.interest_over_time()
                if data.empty:
                    continue
                for kw in keywords[:3]:
                    if kw not in data.columns:
                        continue
                    vals    = data[kw].tolist()
                    current = vals[-1] if vals else 0
                    avg     = sum(vals) / len(vals) if vals else 1
                    spike   = current / avg if avg > 0 else 1
                    if spike < 1.5:
                        continue
                    results.append(DataPoint(
                        source="google_trends", domain=domain,
                        title=f"📈 Trends spike: '{kw}' ({spike:.1f}x)",
                        content=(f"Search interest for '{kw}' is {spike:.1f}x "
                                 f"above daily average. Index: {current}."),
                        url=f"https://trends.google.com/trends/explore?q={kw.replace(' ','+')}",
                        timestamp=datetime.now(timezone.utc),
                        relevance=min(1.0, spike / 5),
                        entities=extract_entities(kw),
                    ))
                time.sleep(1.0)
            except Exception as e:
                log.debug(f"Trends {domain}: {e}")
    except ImportError:
        log.debug("pytrends not installed")
    return results


# ── Arbitrage Detector ─────────────────────────────────────────────────────────

def detect_cross_platform_arbitrage(
    pm_markets:    list[dict],
    meta_points:   list[DataPoint],
    manifold_points: list[DataPoint],
    min_spread:    float = 0.05,
) -> list[DataPoint]:
    """Найти расхождения между Polymarket и Metaculus/Manifold."""
    results = []

    def _extract_prob(title: str) -> Optional[float]:
        m = re.search(r"\[(?:Metaculus|Manifold)\s+(\d+)%\]", title)
        return float(m.group(1)) / 100 if m else None

    for pm in pm_markets:
        pm_title = (pm.get("question") or "").lower()
        pm_price = float(pm.get("yes_price", 0.5))
        pm_words = set(pm_title.split())

        for dp in (meta_points + manifold_points):
            other_words = set(dp.title.lower().split())
            overlap     = len(pm_words & other_words) / max(len(pm_words), 1)
            if overlap < 0.28:
                continue

            other_prob = _extract_prob(dp.title)
            if other_prob is None:
                continue

            spread = abs(pm_price - other_prob)
            if spread < min_spread:
                continue

            direction = "YES" if other_prob > pm_price else "NO"
            platform  = "Metaculus" if "metaculus" in dp.source else "Manifold"

            results.append(DataPoint(
                source="arbitrage", domain=detect_domain(pm_title),
                title=(f"🎯 ARBI {spread:.0%}: PM={pm_price:.0%} vs "
                       f"{platform}={other_prob:.0%}"),
                content=(f"Polymarket: {pm_price:.1%} | {platform}: {other_prob:.1%} | "
                         f"Spread: {spread:.1%} | Trade: {direction} on PM"),
                url="https://polymarket.com",
                timestamp=datetime.now(timezone.utc),
                sentiment=(other_prob - pm_price) * 2,
                relevance=min(1.0, 0.6 + spread * 4),
                entities=extract_entities(pm_title),
            ))

    results.sort(key=lambda x: x.relevance, reverse=True)
    return results[:8]


# ── Extended Pipeline ──────────────────────────────────────────────────────────

class ExtendedIntelPipeline:
    """Все дополнительные источники. Работает поверх основного IntelPipeline."""

    def __init__(self, db: IntelDB, odds_api_key: str = "",
                 trends_on: bool = True) -> None:
        self.db       = db
        self.odds_key = odds_api_key
        self.trends   = trends_on

    def run(self) -> dict[str, int]:
        log.info("📡 Extended Intel — запуск...")
        counts: dict[str, int] = {}

        sources = [
            ("metaculus",  lambda: fetch_metaculus(20)),
            ("manifold",   lambda: fetch_manifold(20)),
            ("unusual_wh", fetch_unusual_whales),
            ("congress",   fetch_congress),
            ("wikipedia",  fetch_wikipedia_changes),
            ("econ_cal",   fetch_economic_calendar),
        ]

        if self.odds_key:
            sources.append(("odds", lambda: fetch_betting_odds(self.odds_key)))

        if self.trends:
            sources.append(("trends", fetch_google_trends))

        for name, fn in sources:
            try:
                pts   = fn()
                saved = sum(1 for dp in pts if self.db.save(dp))
                counts[name] = saved
                log.info(f"  {name}: {saved} новых")
            except Exception as e:
                log.warning(f"  {name} failed: {e}")
                counts[name] = 0

        total = sum(counts.values())
        log.info(f"📡 Extended: {total} новых DataPoints")
        return counts

    def find_arbitrage(self, pm_markets: list[dict]) -> list[DataPoint]:
        meta     = fetch_metaculus(30)
        manifold = fetch_manifold(30)
        arb      = detect_cross_platform_arbitrage(pm_markets, meta, manifold)
        for dp in arb:
            self.db.save(dp)
        return arb
