"""
config.py — Carrega variáveis de ambiente e instancia a exchange via ccxt.
"""
import logging
import os
from dotenv import load_dotenv
import ccxt

load_dotenv()

EXCHANGE_ID    = os.getenv("EXCHANGE_ID", "okx")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")

TESTNET = os.getenv("TESTNET", "True").strip().lower() in ("true", "1", "yes")

_raw_symbols = os.getenv("SYMBOLS", "LTC/USDT,ADA/USDT,XRP/USDT,SOL/USDT")
SYMBOLS = [s.strip() for s in _raw_symbols.split(",") if s.strip()]

TIMEFRAME_TREND = os.getenv("TIMEFRAME_TREND", "4h")
TIMEFRAME_ENTRY = os.getenv("TIMEFRAME_ENTRY", "1h")

EMA_FAST        = int(os.getenv("EMA_FAST", "9"))
EMA_MID         = int(os.getenv("EMA_MID", "21"))
EMA_SLOW        = int(os.getenv("EMA_SLOW", "55"))
RSI_MIN         = float(os.getenv("RSI_MIN", "40"))
RSI_MAX         = float(os.getenv("RSI_MAX", "65"))
SIGNAL_LOOKBACK = int(os.getenv("SIGNAL_LOOKBACK", "3"))

RISK_PCT         = float(os.getenv("RISK_PCT", "1.5"))
RR_RATIO         = float(os.getenv("RR_RATIO", "2.0"))
SL_CANDLES       = int(os.getenv("SL_CANDLES", "2"))
SL_BUFFER_PCT    = float(os.getenv("SL_BUFFER_PCT", "0.5"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "25.0"))
MIN_BALANCE_USDT = float(os.getenv("MIN_BALANCE_USDT", "50.0"))
FEE_RESERVE_PCT  = float(os.getenv("FEE_RESERVE_PCT", "0.3"))

TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", "2.0"))
TRAILING_STOP_PCT       = float(os.getenv("TRAILING_STOP_PCT", "3.5"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "900"))
OHLCV_LIMIT    = int(os.getenv("OHLCV_LIMIT", "300"))
LOG_FILE       = os.getenv("LOG_FILE", "bot_trade.log")
HISTORY_FILE   = os.getenv("HISTORY_FILE", "trade_history.csv")

def state_file_for(symbol: str) -> str:
    return f"state_{symbol.replace('/', '_')}.json"

def create_exchange() -> ccxt.Exchange:
    if EXCHANGE_ID not in ccxt.exchanges:
        raise ValueError(f"Exchange '{EXCHANGE_ID}' não encontrada no ccxt.")

    exchange_class = getattr(ccxt, EXCHANGE_ID)
    config = {
        "apiKey"         : API_KEY,
        "secret"         : API_SECRET,
        "password"       : API_PASSPHRASE,
        "enableRateLimit": True,
        "options"        : {"defaultType": "spot"},
    }
    exchange = exchange_class(config)

    if TESTNET:
        exchange.set_sandbox_mode(True)
        logging.getLogger("bot").info("Modo TESTNET ativado — simulação.")
    else:
        logging.getLogger("bot").warning("⚠ Modo PRODUÇÃO ativado — operações com dinheiro real na OKX!")

    return exchange