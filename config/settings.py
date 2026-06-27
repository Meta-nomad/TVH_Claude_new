import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CRYPTOCOMPARE_API_KEY: str = os.getenv("CRYPTOCOMPARE_API_KEY", "")

CACHE_TTL_SECONDS: int = 86400  # 24 часа

# USDT появился 6 октября 2014.
# Монеты старше этой даты могут иметь историю в USD/BTC задолго до USDT.
USDT_BIRTH = "2014-10-06"

# Котировки в порядке приоритета для СТАРЫХ монет (старше USDT)
QUOTES_OLD = ["USD", "BTC", "ETH", "USDT", "USDC"]

# Котировки в порядке приоритета для НОВЫХ монет (моложе USDT)
QUOTES_NEW = ["USDT", "USD", "BUSD", "BTC", "ETH", "USDC"]

# ── CryptoCompare market name → TradingView prefix ─────────────────────────
CC_TO_TV: dict[str, str] = {
    "Bitstamp":   "BITSTAMP",
    "Kraken":     "KRAKEN",
    "Coinbase":   "COINBASE",
    "Bitfinex":   "BITFINEX",
    "Poloniex":   "POLONIEX",
    "Bittrex":    "BITTREX",
    "Binance":    "BINANCE",
    "Bybit":      "BYBIT",
    "OKEx":       "OKX",
    "Kucoin":     "KUCOIN",
    "GateIO":     "GATEIO",
    "Huobi":      "HTX",
    "MEXC":       "MEXC",
    "Gemini":     "GEMINI",
    "BitMEX":     "BITMEX",
    "HitBTC":     "HITBTC",
    "Yobit":      "YOBIT",
    "Exmo":       "EXMO",
    "Cexio":      "CEXIO",
    "Bithumb":    "BITHUMB",
    "Upbit":      "UPBIT",
    "Coinone":    "COINONE",
    "Korbit":     "KORBIT",
    "BTC-e":      "BTCE",
    "BTC38":      "BTC38",
    "Bitbay":     "BITBAY",
    "Bitso":      "BITSO",
    "Mercado":    "MERCADOBITCOIN",
    "Indodax":    "INDODAX",
    "Luno":       "LUNO",
    "Phemex":     "PHEMEX",
    "Deribit":    "DERIBIT",
    "WOO":        "WOONETWORK",
    "Crypto.com": "CRYPTOCOM",
    "LBank":      "LBANK",
    "Bitget":     "BITGET",
    "AscendEX":   "ASCENDEX",
    "Hotbit":     "HOTBIT",
    "Coincheck":  "COINCHECK",
    "Zaif":       "ZAIF",
    "bitFlyer":   "BITFLYER",
    "Bitvavo":    "BITVAVO",
    "Liquid":     "LIQUID",
    "Bitbank":    "BITBANK",
    "BigONE":     "BIGONE",
    "DigiFinex":  "DIGIFINEX",
    "CoinEx":     "COINEX",
    "Tidex":      "TIDEX",
    "Livecoin":   "LIVECOIN",
    "C2CX":       "C2CX",
    "BTCC":       "BTCC",
    "OKCoin":     "OKCOIN",
    "Huobi Pro":  "HTX",
    "Binance US": "BINANCEUS",
}

# Полный список бирж CryptoCompare для перебора если top/exchanges пуст
ALL_CC_EXCHANGES: list[str] = list(CC_TO_TV.keys())
