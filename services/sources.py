"""
Все источники данных с автоматическим fallback.

Порядок попыток для каждой операции:
  1. CryptoCompare  (min-api.cryptocompare.com)
  2. Binance        (api.binance.com)          — публичный, без ключа
  3. Kraken         (api.kraken.com)           — публичный, без ключа
  4. CoinGecko      (api.coingecko.com)        — может требовать Pro

Если источник недоступен — логируем и идём к следующему.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from config import CRYPTOCOMPARE_API_KEY

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)
HEADERS = {"User-Agent": "Mozilla/5.0 (TradingViewHistoryBot/2.0)"}


# ════════════════════════════════════════════════════════════
#  НИЗКОУРОВНЕВЫЕ HTTP-ХЕЛПЕРЫ
# ════════════════════════════════════════════════════════════

async def _get_json(session: aiohttp.ClientSession, url: str,
                    params: dict = None, extra_headers: dict = None) -> Optional[any]:
    hdrs = {**HEADERS, **(extra_headers or {})}
    try:
        async with session.get(url, params=params, headers=hdrs, timeout=TIMEOUT) as r:
            if r.status == 429:
                log.warning("Rate limit %s, retrying after 5s", url)
                await asyncio.sleep(5)
                async with session.get(url, params=params, headers=hdrs, timeout=TIMEOUT) as r2:
                    if r2.status != 200:
                        return None
                    return await r2.json()
            if r.status != 200:
                log.debug("HTTP %s for %s", r.status, url)
                return None
            return await r.json()
    except asyncio.TimeoutError:
        log.debug("Timeout: %s", url)
    except Exception as e:
        log.debug("Error %s: %s", url, e)
    return None


def _cc_headers() -> dict:
    h = {}
    if CRYPTOCOMPARE_API_KEY:
        h["authorization"] = f"Apikey {CRYPTOCOMPARE_API_KEY}"
    return h


# ════════════════════════════════════════════════════════════
#  ПРОВЕРКА СУЩЕСТВОВАНИЯ МОНЕТЫ
# ════════════════════════════════════════════════════════════

async def check_coin_cryptocompare(session: aiohttp.ClientSession, ticker: str) -> bool:
    """Монета существует если CryptoCompare возвращает хоть одну цену."""
    data = await _get_json(
        session,
        "https://min-api.cryptocompare.com/data/price",
        {"fsym": ticker.upper(), "tsyms": "USD,USDT,BTC"},
        _cc_headers(),
    )
    if not data or "Response" in data:
        return False
    return bool(data)  # {"USD": 1.23} → True


async def check_coin_binance(session: aiohttp.ClientSession, ticker: str) -> bool:
    """Проверяем наличие тикера как baseAsset на Binance."""
    data = await _get_json(session, "https://api.binance.com/api/v3/exchangeInfo")
    if not data:
        return False
    t = ticker.upper()
    return any(s.get("baseAsset", "").upper() == t for s in data.get("symbols", []))


async def check_coin_kraken(session: aiohttp.ClientSession, ticker: str) -> bool:
    """Проверяем наличие тикера как base на Kraken."""
    data = await _get_json(session, "https://api.kraken.com/0/public/AssetPairs")
    if not data or data.get("error"):
        return False
    t = ticker.upper()
    result = data.get("result", {})
    for info in result.values():
        base = info.get("base", "").upper().lstrip("XZ")
        alt  = info.get("altname", "")
        if base == t or alt.startswith(t):
            return True
    return False


async def check_coin_coingecko(session: aiohttp.ClientSession, ticker: str) -> bool:
    data = await _get_json(
        session,
        "https://api.coingecko.com/api/v3/search",
        {"query": ticker.upper()},
    )
    if not data:
        return False
    t = ticker.upper()
    return any(c.get("symbol", "").upper() == t for c in data.get("coins", []))


async def coin_exists(session: aiohttp.ClientSession, ticker: str) -> bool:
    """
    Проверяет существование монеты через все источники.
    Возвращает True при первом подтверждении.
    """
    checks = [
        check_coin_cryptocompare,
        check_coin_binance,
        check_coin_kraken,
        check_coin_coingecko,
    ]
    for fn in checks:
        try:
            if await fn(session, ticker):
                log.info("Coin %s confirmed via %s", ticker, fn.__name__)
                return True
        except Exception as e:
            log.debug("%s failed: %s", fn.__name__, e)
    return False


# ════════════════════════════════════════════════════════════
#  ДАТА ЗАПУСКА МОНЕТЫ
# ════════════════════════════════════════════════════════════

async def get_launch_date_cryptocompare(session: aiohttp.ClientSession, ticker: str) -> Optional[str]:
    """Дата запуска через CryptoCompare Data API v2."""
    data = await _get_json(
        session,
        "https://data-api.cryptocompare.com/asset/v1/summary",
        {"asset_lookup_priority": "SYMBOL", "asset": ticker.upper()},
    )
    if not data or "Data" not in data:
        return None
    d = data["Data"]
    for field in ("LAUNCH_DATE", "ASSET_LAUNCH_DATE", "CREATED_ON"):
        ts = d.get(field)
        if ts:
            try:
                return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


async def get_launch_date_coingecko(session: aiohttp.ClientSession, ticker: str) -> Optional[str]:
    """Genesis date через CoinGecko."""
    # Шаг 1: найти coin_id
    search = await _get_json(
        session,
        "https://api.coingecko.com/api/v3/search",
        {"query": ticker.upper()},
    )
    if not search:
        return None
    coin_id = None
    for c in search.get("coins", []):
        if c.get("symbol", "").upper() == ticker.upper():
            coin_id = c["id"]
            break
    if not coin_id:
        return None

    # Шаг 2: детали монеты
    details = await _get_json(
        session,
        f"https://api.coingecko.com/api/v3/coins/{coin_id}",
        {"localization": "false", "tickers": "false",
         "market_data": "false", "community_data": "false", "developer_data": "false"},
    )
    if not details:
        return None
    return details.get("genesis_date")  # "2009-01-03" или None


async def get_launch_date(session: aiohttp.ClientSession, ticker: str) -> Optional[str]:
    """Пробует все источники, возвращает самую раннюю найденную дату."""
    dates = []
    for fn in (get_launch_date_cryptocompare, get_launch_date_coingecko):
        try:
            d = await fn(session, ticker)
            if d:
                dates.append(d)
        except Exception as e:
            log.debug("%s failed: %s", fn.__name__, e)
    return min(dates) if dates else None


# ════════════════════════════════════════════════════════════
#  СПИСОК БИРЖ ДЛЯ ПАРЫ
# ════════════════════════════════════════════════════════════

async def exchanges_cryptocompare(
    session: aiohttp.ClientSession, base: str, quote: str
) -> list[str]:
    """Список бирж через CryptoCompare top/exchanges."""
    data = await _get_json(
        session,
        "https://min-api.cryptocompare.com/data/top/exchanges/full",
        {"fsym": base.upper(), "tsym": quote.upper(), "limit": 100},
        _cc_headers(),
    )
    if not data or "Data" not in data:
        return []
    return [e["MARKET"] for e in data["Data"].get("Exchanges", []) if e.get("MARKET")]


async def exchanges_binance(
    session: aiohttp.ClientSession, base: str, quote: str
) -> list[str]:
    """Проверяет наличие пары на Binance."""
    symbol = f"{base.upper()}{quote.upper()}"
    data = await _get_json(
        session,
        "https://api.binance.com/api/v3/ticker/price",
        {"symbol": symbol},
    )
    if data and "price" in data:
        return ["Binance"]
    return []


async def exchanges_kraken(
    session: aiohttp.ClientSession, base: str, quote: str
) -> list[str]:
    """Проверяет наличие пары на Kraken."""
    data = await _get_json(session, "https://api.kraken.com/0/public/AssetPairs")
    if not data or data.get("error"):
        return []

    b = _kraken_asset(base)
    q = _kraken_asset(quote)
    result = data.get("result", {})

    for info in result.values():
        pair_base  = info.get("base", "").upper()
        pair_quote = info.get("quote", "").upper()
        # Kraken добавляет X/Z префиксы
        if (pair_base.lstrip("XZ") == b or pair_base == f"X{b}" or pair_base == b):
            if (pair_quote.lstrip("XZ") == q or pair_quote == f"Z{q}" or pair_quote == q):
                return ["Kraken"]
    return []


def _kraken_asset(s: str) -> str:
    """Нормализует имя актива для Kraken."""
    return {"BTC": "XBT", "DOGE": "XDG", "STR": "XLM"}.get(s.upper(), s.upper())


async def get_all_exchanges(
    session: aiohttp.ClientSession, base: str, quotes: list[str]
) -> dict[str, list[str]]:
    """
    Для каждой котировки собирает список бирж из всех источников.
    Возвращает {quote: [exchange, ...]}
    """
    result: dict[str, list[str]] = {}
    sem = asyncio.Semaphore(6)

    async def fetch_for_quote(quote: str):
        async with sem:
            seen = set()
            exs = []
            for fn in (exchanges_cryptocompare, exchanges_binance, exchanges_kraken):
                try:
                    found = await fn(session, base, quote)
                    for e in found:
                        if e not in seen:
                            seen.add(e)
                            exs.append(e)
                except Exception as e_:
                    log.debug("%s failed for %s/%s: %s", fn.__name__, base, quote, e_)
            return quote, exs

    tasks = [fetch_for_quote(q) for q in quotes]
    for quote, exs in await asyncio.gather(*tasks):
        if exs:
            result[quote] = exs

    return result


# ════════════════════════════════════════════════════════════
#  САМАЯ РАННЯЯ ДАТА ИСТОРИИ
# ════════════════════════════════════════════════════════════

async def earliest_date_cryptocompare(
    session: aiohttp.ClientSession, base: str, quote: str, exchange: str
) -> Optional[datetime]:
    """Самая ранняя дата через CryptoCompare histoday allData=true."""
    data = await _get_json(
        session,
        "https://min-api.cryptocompare.com/data/v2/histoday",
        {"fsym": base.upper(), "tsym": quote.upper(), "e": exchange,
         "limit": 2000, "allData": "true"},
        _cc_headers(),
    )
    if not data or "Data" not in data or "Data" not in data["Data"]:
        return None
    bars = data["Data"]["Data"]
    for bar in bars:
        if bar.get("time", 0) > 0 and (bar.get("volumefrom", 0) > 0 or bar.get("volumeto", 0) > 0):
            return datetime.fromtimestamp(bar["time"], tz=timezone.utc)
    return None


async def earliest_date_binance(
    session: aiohttp.ClientSession, base: str, quote: str
) -> Optional[datetime]:
    """Самая ранняя дата через Binance klines (startTime=0 → самая старая свеча)."""
    symbol = f"{base.upper()}{quote.upper()}"
    data = await _get_json(
        session,
        "https://api.binance.com/api/v3/klines",
        {"symbol": symbol, "interval": "1d", "limit": 1, "startTime": 0},
    )
    if data and len(data) > 0:
        try:
            return datetime.fromtimestamp(data[0][0] / 1000, tz=timezone.utc)
        except Exception:
            pass
    return None


async def earliest_date_kraken(
    session: aiohttp.ClientSession, base: str, quote: str
) -> Optional[datetime]:
    """Самая ранняя дата через Kraken OHLC (since=0)."""
    b = _kraken_asset(base)
    q = _kraken_asset(quote)
    # Пробуем разные форматы пар Kraken
    for pair in [f"X{b}Z{q}", f"{b}{q}", f"X{b}{q}", f"{b}Z{q}",
                 f"{b}USD" if q == "ZUSD" else None]:
        if not pair:
            continue
        data = await _get_json(
            session,
            "https://api.kraken.com/0/public/OHLC",
            {"pair": pair, "interval": 1440},  # 1440 мин = 1 день
        )
        if not data or data.get("error"):
            continue
        result = data.get("result", {})
        for key, rows in result.items():
            if key == "last" or not isinstance(rows, list) or not rows:
                continue
            try:
                return datetime.fromtimestamp(rows[0][0], tz=timezone.utc)
            except Exception:
                pass
    return None


async def get_earliest_date(
    session: aiohttp.ClientSession,
    cc_exchange: str, base: str, quote: str,
) -> Optional[datetime]:
    """
    Пробует получить самую раннюю дату из всех доступных источников.
    CryptoCompare → биржевой публичный API.
    """
    # 1. CryptoCompare (знает почти все биржи)
    dt = await earliest_date_cryptocompare(session, base, quote, cc_exchange)
    if dt:
        return dt

    # 2. Прямой API биржи (если CryptoCompare не ответил)
    if cc_exchange == "Binance":
        dt = await earliest_date_binance(session, base, quote)
        if dt:
            return dt

    if cc_exchange == "Kraken":
        dt = await earliest_date_kraken(session, base, quote)
        if dt:
            return dt

    return None


# ════════════════════════════════════════════════════════════
#  КАЧЕСТВО ЧАСОВОГО ГРАФИКА (GAP SCORE)
# ════════════════════════════════════════════════════════════

def _gap_ratio(bars: list[dict | list], ts_key) -> float:
    """
    Вычисляет долю пропущенных часовых слотов.
    ts_key: callable → unix timestamp в секундах из одного бара.
    """
    times = sorted(ts_key(b) for b in bars if ts_key(b) > 0)
    if len(times) < 2:
        return 1.0
    expected = max(1, (times[-1] - times[0]) // 3600)
    hard_gaps = sum(
        int((times[i] - times[i-1]) / 3600) - 1
        for i in range(1, len(times))
        if (times[i] - times[i-1]) / 3600 > 1.5
    )
    # Нулевые бары считаем мягкими разрывами
    if isinstance(bars[0], dict):
        zero_bars = sum(1 for b in bars if b.get("volumefrom", 0) == 0 and b.get("volumeto", 0) == 0)
    else:
        zero_bars = 0
    total = hard_gaps + zero_bars * 0.5
    return min(total / expected, 1.0)


async def hourly_gap_cryptocompare(
    session: aiohttp.ClientSession, base: str, quote: str, exchange: str
) -> Optional[float]:
    data = await _get_json(
        session,
        "https://min-api.cryptocompare.com/data/v2/histohour",
        {"fsym": base.upper(), "tsym": quote.upper(), "e": exchange, "limit": 2000},
        _cc_headers(),
    )
    if not data or "Data" not in data or "Data" not in data["Data"]:
        return None
    bars = data["Data"]["Data"]
    if not bars:
        return None
    return _gap_ratio(bars, lambda b: b.get("time", 0))


async def hourly_gap_binance(
    session: aiohttp.ClientSession, base: str, quote: str
) -> Optional[float]:
    symbol = f"{base.upper()}{quote.upper()}"
    data = await _get_json(
        session,
        "https://api.binance.com/api/v3/klines",
        {"symbol": symbol, "interval": "1h", "limit": 1000},
    )
    if not data or len(data) < 2:
        return None
    # klines: [open_time, open, high, low, close, volume, ...]
    return _gap_ratio(data, lambda b: b[0] // 1000)


async def get_hourly_gap_score(
    session: aiohttp.ClientSession,
    cc_exchange: str, base: str, quote: str,
) -> float:
    """Возвращает gap_ratio [0.0=идеально .. 1.0=плохо]."""
    score = await hourly_gap_cryptocompare(session, base, quote, cc_exchange)
    if score is not None:
        return score

    if cc_exchange == "Binance":
        score = await hourly_gap_binance(session, base, quote)
        if score is not None:
            return score

    return 0.5  # нейтральный score если данных нет
