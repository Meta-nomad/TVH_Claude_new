"""
Анализатор v3: исправлены все три проблемы:

ПРОБЛЕМА 1 — Неверная котировка (ONDO → USD вместо USDT):
  Если get_launch_date не вернул дату (API не ответил),
  старый код делал fallback на QUOTES_OLD (USD первый).
  Исправление: если дата неизвестна → смотрим РЕАЛЬНЫЕ данные.
  Если на новой монете USDT пара существует и имеет историю
  раньше USD — берём USDT. Жёсткий fallback → QUOTES_NEW.

ПРОБЛЕМА 2 — Неверная дата:
  CryptoCompare для Kraken мог возвращать агрегированную дату
  (с других бирж), а не реальный старт на Kraken.
  Исправление: для каждой биржи берём дату ТОЛЬКО из её собственного
  прямого API если CryptoCompare и прямой API расходятся >30 дней —
  берём максимум (позднюю), то есть реальный старт на этой бирже.

ПРОБЛЕМА 3 — Разрывы не фильтруются:
  Gap score был только tiebreaker для бирж с одинаковой датой.
  Исправление: биржи с gap_ratio > GAP_REJECT_THRESHOLD (0.20 = 20%)
  полностью исключаются из выборки ЕСЛИ есть хоть одна биржа лучше.
"""

import asyncio
import logging
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone, date, timedelta
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
    earliest_date_binance, earliest_date_kraken,
)
from utils import cache

log = logging.getLogger(__name__)

_USDT_BIRTH_DATE   = date.fromisoformat(USDT_BIRTH)
SAME_DATE_DAYS     = 3     # окно для gap-сортировки
GAP_REJECT         = 0.20  # биржи с gap_ratio > этого отбрасываем (если есть лучшие)
GAP_SCORE_ALL      = True  # скорить gap для ВСЕХ кандидатов, не только тай-группы


@dataclass
class ExchangeResult:
    cc_name:    str
    tv_prefix:  str
    base:       str
    quote:      str
    start_date: datetime
    symbol:     str
    url:        str
    gap_ratio:  float = 1.0
    gap_scored: bool  = False


@dataclass
class AnalysisResult:
    ticker:       str
    best:         ExchangeResult
    alternatives: list[ExchangeResult] = field(default_factory=list)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _cc_to_tv(cc_name: str) -> str:
    return CC_TO_TV.get(cc_name, cc_name.upper().replace(" ", "").replace("-", ""))


def _sym(tv: str, base: str, quote: str) -> str:
    return f"{tv}:{base}{quote}"


def _url(symbol: str) -> str:
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol, safe='')}"


def _make(cc_name: str, base: str, quote: str, dt: datetime) -> ExchangeResult:
    tv  = _cc_to_tv(cc_name)
    sym = _sym(tv, base, quote)
    return ExchangeResult(cc_name=cc_name, tv_prefix=tv, base=base, quote=quote,
                          start_date=dt, symbol=sym, url=_url(sym))


def _sort_key(r: ExchangeResult, q_priority: list[str]) -> tuple:
    day   = r.start_date.replace(hour=0, minute=0, second=0, microsecond=0)
    gap   = r.gap_ratio if r.gap_scored else 0.5
    q_idx = q_priority.index(r.quote) if r.quote in q_priority else 99
    return (day, gap, q_idx)


# ── Исправление 1: выбор котировки ───────────────────────────────────────────

async def _resolve_quote_priority(
    session: aiohttp.ClientSession,
    ticker: str,
    launch_date: Optional[str],
) -> list[str]:
    """
    Определяем приоритет котировок.
    Если дата запуска неизвестна — проверяем реально доступные пары:
      - если USDT торгуется, а USD нет → QUOTES_NEW
      - если оба торгуются → смотрим какая пара раньше появилась
      - если только USD → QUOTES_OLD
    """
    if launch_date:
        try:
            d = date.fromisoformat(launch_date[:10])
            priority = QUOTES_OLD if d < _USDT_BIRTH_DATE else QUOTES_NEW
            log.info("%s launch=%s → %s", ticker, launch_date, priority[0])
            return priority
        except (ValueError, TypeError):
            pass

    # Дата неизвестна — проверяем реальные пары параллельно
    log.info("%s: launch_date unknown, probing actual pairs", ticker)

    async def probe_pair(quote: str) -> tuple[str, Optional[datetime]]:
        # Пробуем Binance как самый надёжный публичный API
        dt = await earliest_date_binance(session, ticker, quote)
        if dt:
            return quote, dt
        # Пробуем CryptoCompare без привязки к бирже (агрегат)
        from services.sources import earliest_date_cryptocompare
        dt = await earliest_date_cryptocompare(session, ticker, quote, "CCCAGG")
        return quote, dt

    usdt_res, usd_res = await asyncio.gather(
        probe_pair("USDT"),
        probe_pair("USD"),
    )

    usdt_dt = usdt_res[1]
    usd_dt  = usd_res[1]

    log.info("%s pair probe: USDT=%s  USD=%s", ticker,
             usdt_dt.strftime("%Y-%m-%d") if usdt_dt else "None",
             usd_dt.strftime("%Y-%m-%d") if usd_dt else "None")

    if usdt_dt and not usd_dt:
        return QUOTES_NEW   # только USDT есть

    if usd_dt and not usdt_dt:
        return QUOTES_OLD   # только USD есть

    if usdt_dt and usd_dt:
        # Есть обе — выбираем по дате
        if usd_dt < usdt_dt - timedelta(days=180):
            # USD значительно старше (монета предшествует USDT по факту)
            return QUOTES_OLD
        return QUOTES_NEW   # USDT не хуже — приоритет современной котировке

    # Ничего не нашли через быстрый пробинг — дефолт новая монета
    return QUOTES_NEW


# ── Исправление 2: верификация даты ──────────────────────────────────────────

async def _verified_earliest(
    session: aiohttp.ClientSession,
    cc_name: str, base: str, quote: str,
) -> Optional[datetime]:
    """
    Получает дату начала истории и верифицирует её через прямой биржевой API.

    CryptoCompare иногда возвращает агрегированную дату с других бирж.
    Если прямой API биржи даёт более позднюю дату — используем её
    (это и есть реальный старт торгов на этой бирже).
    """
    from services.sources import earliest_date_cryptocompare

    cc_dt = await earliest_date_cryptocompare(session, base, quote, cc_name)

    # Прямой API для конкретных бирж
    direct_dt: Optional[datetime] = None
    if cc_name == "Binance":
        direct_dt = await earliest_date_binance(session, base, quote)
    elif cc_name == "Kraken":
        direct_dt = await earliest_date_kraken(session, base, quote)

    if cc_dt is None and direct_dt is None:
        return None

    if cc_dt is None:
        return direct_dt

    if direct_dt is None:
        return cc_dt

    # Если расхождение > 30 дней → CryptoCompare даёт неверную (чужую) дату
    diff = abs((cc_dt - direct_dt).days)
    if diff > 30:
        # Берём более позднюю (реальный старт на этой бирже)
        real_dt = max(cc_dt, direct_dt)
        log.info(
            "%s/%s@%s: CC=%s direct=%s diff=%dd → using %s",
            base, quote, cc_name,
            cc_dt.strftime("%Y-%m-%d"), direct_dt.strftime("%Y-%m-%d"),
            diff, real_dt.strftime("%Y-%m-%d"),
        )
        return real_dt

    # Расхождение небольшое → берём более раннюю (CC обычно точнее для истории)
    return min(cc_dt, direct_dt)


# ── Main ───────────────────────────────────────────────────────────────────────

async def analyze(ticker: str) -> Optional[AnalysisResult]:
    ticker = ticker.strip().upper()
    cached = cache.get(f"v3:{ticker}")
    if cached is not None:
        log.info("Cache hit: %s", ticker)
        return cached

    async with aiohttp.ClientSession() as session:
        result = await _analyze(session, ticker)
        if result:
            cache.set(f"v3:{ticker}", result, CACHE_TTL_SECONDS)
        return result


async def _analyze(session: aiohttp.ClientSession, ticker: str) -> Optional[AnalysisResult]:

    # ── 1. Монета существует? ────────────────────────────────────────────────
    if not await coin_exists(session, ticker):
        log.info("Coin not found: %s", ticker)
        return None

    # ── 2. Приоритет котировок (исправление 1) ───────────────────────────────
    launch_date = await get_launch_date(session, ticker)
    q_priority  = await _resolve_quote_priority(session, ticker, launch_date)

    # ── 3. Список бирж для каждой котировки ─────────────────────────────────
    exchange_map = await get_all_exchanges(session, ticker, q_priority)

    if not exchange_map:
        log.warning("%s: no exchanges from API, using ALL_CC_EXCHANGES fallback", ticker)
        exchange_map = {q: ALL_CC_EXCHANGES for q in q_priority}

    # ── 4. Зондируем дату начала истории (с верификацией, исправление 2) ────
    sem = asyncio.Semaphore(8)

    async def probe(cc_ex: str, base: str, quote: str) -> Optional[ExchangeResult]:
        async with sem:
            dt = await _verified_earliest(session, cc_ex, base, quote)
            if dt:
                return _make(cc_ex, base, quote, dt)
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

    # ── 5. Дедупликация ──────────────────────────────────────────────────────
    best_per_key: dict[tuple[str, str], ExchangeResult] = {}
    for r in results:
        k = (r.tv_prefix, r.quote)
        if k not in best_per_key or r.start_date < best_per_key[k].start_date:
            best_per_key[k] = r
    unique = list(best_per_key.values())

    # ── 6. Gap scoring ДЛЯ ВСЕХ кандидатов (исправление 3) ──────────────────
    async def score(r: ExchangeResult) -> ExchangeResult:
        async with sem:
            r.gap_ratio  = await get_hourly_gap_score(session, r.cc_name, r.base, r.quote)
            r.gap_scored = True
            return r

    unique = list(await asyncio.gather(*[score(r) for r in unique]))

    log.info(
        "%s all gap scores: %s",
        ticker,
        [(r.symbol, f"{r.gap_ratio:.3f}") for r in sorted(unique, key=lambda x: x.gap_ratio)],
    )

    # ── 7. Фильтрация по gap_ratio (исправление 3) ───────────────────────────
    # Отбрасываем биржи с gap_ratio > GAP_REJECT, НО только если есть лучшие
    good = [r for r in unique if r.gap_ratio <= GAP_REJECT]
    if good:
        # Есть биржи без серьёзных разрывов — плохие отбрасываем
        filtered = good
        bad_count = len(unique) - len(good)
        if bad_count:
            log.info("%s: dropped %d exchange(s) with gap_ratio > %.0f%%",
                     ticker, bad_count, GAP_REJECT * 100)
    else:
        # Все биржи имеют разрывы — оставляем всё, выбираем наименее плохую
        filtered = unique
        log.info("%s: all exchanges have high gap_ratio, keeping all", ticker)

    # ── 8. Финальная сортировка: дата → gap → котировка ─────────────────────
    filtered.sort(key=lambda r: _sort_key(r, q_priority))

    best = filtered[0]
    alternatives = [
        r for r in filtered[1:]
        if not (r.tv_prefix == best.tv_prefix and r.quote == best.quote)
    ][:4]

    return AnalysisResult(ticker=ticker, best=best, alternatives=alternatives)
