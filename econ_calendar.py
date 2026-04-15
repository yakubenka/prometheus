"""
Prometheus — Economic Calendar
Major US economic releases that impact prediction markets.

Используется SignalEngine чтобы:
  1. Блокировать торговлю за 2h до выхода важных данных (неопределённость высокая)
  2. Информировать base_rate сигнал о контексте рынка
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta, date

# ── FOMC meeting end dates (rate decision ~2 PM ET) ───────────────────────────
# Источник: опубликованный календарь ФРС
_FOMC_DATES: list[tuple[int, int, int]] = [
    # 2025
    (2025,  1, 29), (2025,  3, 19), (2025,  5,  7),
    (2025,  6, 18), (2025,  7, 30), (2025,  9, 17),
    (2025, 10, 29), (2025, 12, 10),
    # 2026
    (2026,  1, 28), (2026,  3, 18), (2026,  4, 29),
    (2026,  6, 17), (2026,  7, 29), (2026,  9, 16),
    (2026, 10, 28), (2026, 12,  9),
]

# Ключевые слова которые связывают вопрос рынка с конкретным релизом
_RELEASE_KEYWORDS: dict[str, list[str]] = {
    "NFP":  ["jobs report", "nfp", "payroll", "employment", "unemployment",
             "labor market", "job growth", "non-farm"],
    "CPI":  ["cpi", "inflation", "consumer price", "price index", "core cpi",
             "pce", "deflation"],
    "FOMC": ["fed", "fomc", "federal reserve", "rate cut", "rate hike",
             "interest rate", "basis points", "fed funds", "jerome powell"],
    "GDP":  ["gdp", "gross domestic", "economic growth", "recession", "contraction",
             "expansion"],
}


def _et_offset_hours(utc: datetime) -> int:
    """Приблизительный UTC→ET сдвиг: -4 летом (март–ноябрь), -5 зимой."""
    return -4 if 3 <= utc.month <= 11 else -5


def _first_friday(year: int, month: int) -> date:
    """Первая пятница месяца."""
    d = date(year, month, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def _second_wednesday(year: int, month: int) -> date:
    """Вторая среда месяца (приблизительная дата выхода CPI)."""
    d = date(year, month, 1)
    first_wed = d + timedelta(days=(2 - d.weekday()) % 7)
    return first_wed + timedelta(weeks=1)


def _last_wednesday_of_month(year: int, month: int) -> date:
    """Последняя среда месяца (приблизительная дата advance GDP)."""
    # Идём с конца месяца назад
    if month == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last_day.weekday() - 2) % 7  # 2 = Wednesday
    return last_day - timedelta(days=days_back)


def get_upcoming_releases(hours_forward: float = 3.0) -> list[dict]:
    """
    Возвращает крупные экономические релизы США в ближайшие N часов.
    Все времена в US Eastern Time (±DST).
    """
    now_utc = datetime.now(timezone.utc)
    et_h    = _et_offset_hours(now_utc)
    now_et  = now_utc + timedelta(hours=et_h)
    end_et  = now_et + timedelta(hours=hours_forward)

    releases: list[dict] = []

    def _add(name: str, dt_et: datetime, domains: list[str]) -> None:
        if now_et <= dt_et <= end_et:
            releases.append({"name": name, "dt_et": dt_et, "domains": domains})

    # Проверяем текущий и следующий месяц (чтобы не пропустить граничные случаи)
    for delta_m in (0, 1):
        y = now_utc.year + (now_utc.month + delta_m - 1) // 12
        m = (now_utc.month + delta_m - 1) % 12 + 1

        # NFP: первая пятница, 8:30 AM ET
        nfp_d = _first_friday(y, m)
        _add("NFP",  datetime(y, m, nfp_d.day, 8, 30),  ["macro", "economic"])

        # CPI: ~вторая среда, 8:30 AM ET
        cpi_d = _second_wednesday(y, m)
        _add("CPI",  datetime(y, m, cpi_d.day, 8, 30),  ["macro", "economic"])

        # GDP advance: последняя неделя января, апреля, июля, октября, 8:30 AM ET
        if m in (1, 4, 7, 10):
            gdp_d = _last_wednesday_of_month(y, m)
            _add("GDP", datetime(y, m, gdp_d.day, 8, 30), ["macro", "economic"])

    # FOMC: решение в 2:00 PM ET
    for fy, fm, fd in _FOMC_DATES:
        _add("FOMC", datetime(fy, fm, fd, 14, 0), ["macro", "economic", "crypto"])

    return releases


def imminent_release_for(question: str, tags: tuple, hours: float = 2.0) -> str | None:
    """
    Возвращает название ближайшего экономического релиза если он касается
    данного рынка (по тегам домена или ключевым словам в вопросе).
    Возвращает None если актуальных релизов нет.
    """
    q        = (question or "").lower()
    tag_set  = {str(t).lower() for t in (tags or [])}
    macro_domains = {"macro", "economic", "crypto", "politics"}

    for event in get_upcoming_releases(hours_forward=hours):
        # Совпадение по доменным тегам
        if any(d in tag_set for d in event["domains"]):
            return event["name"]
        # Совпадение по ключевым словам в вопросе
        for kw in _RELEASE_KEYWORDS.get(event["name"], []):
            if kw in q:
                return event["name"]

    return None
