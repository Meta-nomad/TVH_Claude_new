"""
Анализатор: для тикера находит биржу с максимальной историей.

Алгоритм:
  1. Проверяем что монета существует (через любой доступный API)
  2. Получаем дату запуска → выбираем приоритет котировок
  3. Для каждой котировки собираем список бирж
  4. Для каждой (биржа, котировка) запрашиваем дату начала истории
  5. Биржи с одинаковой датой (±3 дня) сортируем по gap_ratio часового графика
  6. Возвращаем лучшую биржу + альтернативы
"""

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

import aiohttp

from config import (
    CACHE_TTL_SECONDS, USDT_BIRTH,
    QUOTES_OLD, QUOTES_NEW,
    CC_TO_TV, ALL_CC_EXCHANGES,
)
from services.sources import (
    coin_exists, get_launch_date, get_all_exchanges,
    get_earliest_date, get_hourly_gap_score,
)
from utils import cache

log = logging.getLogger(__name__)

_USDT_BIRTH_DATE = date.fromisoformat(USDT_BIRTH)
SAME_DATE_DAYS   = 3   # биржи в пределах N дней считаются «одинаковыми»


@dataclass
class ExchangeResult:
    cc_name:    str       # CryptoCompare market name (e.g. "Kraken")
    tv_prefix:  str       # TradingView prefix     (e.g. "KRAKEN")
    base:       str
    quote:      str
    start_date: datetime
    symbol:     str       # e.g. "KRAKEN:BTCUSD"
    url:        str       # TradingView chart URL
    gap_ratio:  float  = 1.0
    gap_scored: bool   = False


@dataclass
class AnalysisResult:
    ticker:       str
    best:         ExchangeResult
    alternatives: list[ExchangeResult] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cc_to_tv(cc_name: str) -> str:
    return CC_TO_TV.get(cc_name, cc_name.upper().replace(" ", "").replace("-", ""))


def _build_symbol(tv_prefix: str, base: str, quote: str) -> str:
    return f"{tv_prefix}:{base}{quote}"


def _build_url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol, safe='')}"


def _quote_priority(launch_date: Optional[str]) -> list[str]:
    """Возвращает список котировок в нужном порядке."""
    if not launch_date:
        return QUOTES_OLD  # неизвестная дата → считаем старой монетой
    try:
        d = date.fromisoformat(launch_date[:10])
        return QUOTES_OLD if d < _USDT_BIRTH_DATE else QUOTES_NEW
    except (ValueError, TypeError):
        return QUOTES_OLD


def _make_result(cc_name: str, base: str, quote: str, dt: datetime) -> ExchangeResult:
    tv = _cc_to_tv(cc_name)
    sym = _build_symbol(tv, base, quote)
    return ExchangeResult(
        cc_name=cc_name, tv_prefix=tv,
        base=base, quote=quote,
        start_date=dt,
        symbol=sym, url=_build_url(sym),
    )


def _sort_key(r: ExchangeResult, q_priority: list[str]) -> tuple:
    day   = r.start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    gap   = r.gap_ratio if r.gap_scored else 0.5
    q_idx = q_priority.index(r.quote) if r.quote in q_priority else 99
    return (day, gap, q_idx)


# ── Main ───────────────────────────────────────────────────────────────────────

async def analyze(ticker: str) -> Optional[AnalysisResult]:
    ticker = ticker.strip().upper()

    cached = cache.get(f"v2:{ticker}")
    if cached is not None:
        log.info("Cache hit: %s", ticker)
        return cached

    async with aiohttp.ClientSession() as session:
        return await _analyze(session, ticker)


async def _analyze(session: aiohttp.ClientSession, ticker: str) -> Optional[AnalysisResult]:

    # ── 1. Монета существует? ────────────────────────────────────────────────
    if not await coin_exists(session, ticker):
        log.info("Coin not found: %s", ticker)
        return None

    # ── 2. Дата запуска → приоритет котировок ───────────────────────────────
    launch_date = await get_launch_date(session, ticker)
    q_priority  = _quote_priority(launch_date)
    log.info("%s launch=%s quotes=%s", ticker, launch_date, q_priority)

    # ── 3. Биржи для каждой котировки ───────────────────────────────────────
    exchange_map = await get_all_exchanges(session, ticker, q_priority)

    # Если API ничего не нашли — берём полный список известных бирж
    if not exchange_map:
        log.warning("%s: no exchanges from API, using ALL_CC_EXCHANGES fallback", ticker)
        exchange_map = {q: ALL_CC_EXCHANGES for q in q_priority}

    # ── 4. Зондируем дату начала истории ────────────────────────────────────
    sem = asyncio.Semaphore(8)

    async def probe(cc_ex: str, base: str, quote: str) -> Optional[ExchangeResult]:
        async with sem:
            dt = await get_earliest_date(session, cc_ex, base, quote)
            if dt:
                return _make_result(cc_ex, base, quote, dt)
            return None

    tasks = []
    for quote, exchanges in exchange_map.items():
        for ex in exchanges:
            tasks.append(probe(ex, ticker, quote))

    results_raw = await asyncio.gather(*tasks)
    results = [r for r in results_raw if r is not None]

    if not results:
        log.warning("%s: no historical data found", ticker)
        return None

    # ── 5. Дедупликация: оставляем лучший результат на (биржа, котировка) ──
    best_per_key: dict[tuple[str, str], ExchangeResult] = {}
    for r in results:
        k = (r.tv_prefix, r.quote)
        if k not in best_per_key or r.start_date < best_per_key[k].start_date:
            best_per_key[k] = r
    unique = list(best_per_key.values())

    # ── 6. Gap scoring для тай-группы ───────────────────────────────────────
    earliest_dt = min(r.start_date for r in unique)

    def is_in_tie_group(r: ExchangeResult) -> bool:
        return abs((r.start_date - earliest_dt).days) <= SAME_DATE_DAYS

    tie_group = [r for r in unique if is_in_tie_group(r)]
    rest      = [r for r in unique if not is_in_tie_group(r)]

    if len(tie_group) > 1:
        async def score(r: ExchangeResult) -> ExchangeResult:
            async with sem:
                r.gap_ratio  = await get_hourly_gap_score(session, r.cc_name, r.base, r.quote)
                r.gap_scored = True
                return r
        tie_group = list(await asyncio.gather(*[score(r) for r in tie_group]))
        log.info(
            "%s tie-group gap scores: %s",
            ticker,
            [(r.symbol, f"{r.gap_ratio:.3f}") for r in tie_group],
        )
    elif tie_group:
        r = tie_group[0]
        r.gap_ratio  = await get_hourly_gap_score(session, r.cc_name, r.base, r.quote)
        r.gap_scored = True

    # ── 7. Финальная сортировка ──────────────────────────────────────────────
    tie_group.sort(key=lambda r: _sort_key(r, q_priority))
    rest.sort(key=lambda r: _sort_key(r, q_priority))
    ranked = tie_group + rest

    best = ranked[0]
    alternatives = [
        r for r in ranked[1:]
        if not (r.tv_prefix == best.tv_prefix and r.quote == best.quote)
    ][:4]

    result = AnalysisResult(ticker=ticker, best=best, alternatives=alternatives)
    cache.set(f"v2:{ticker}", result, CACHE_TTL_SECONDS)
    return result
