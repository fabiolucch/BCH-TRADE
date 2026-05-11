"""
config.py — Carrega variáveis de ambiente e instancia a exchange via ccxt.

Todas as configurações da estratégia e da conexão são centralizadas aqui.
Para alterar parâmetros, edite o arquivo .env (nunca hardcode credenciais).
"""

import logging
import os
from dotenv import load_dotenv
import ccxt

# Carrega .env antes de qualquer leitura de os.getenv
load_dotenv()

# ─────────────────────────────────────────────────────────────
# Credenciais e conexão
# ─────────────────────────────────────────────────────────────
EXCHANGE_ID    = os.getenv("EXCHANGE_ID", "okx")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")

# True  → modo demo/testnet (OKX Paper Trading)
# False → dinheiro real
TESTNET = os.getenv("TESTNET", "True").strip().lower() in ("true", "1", "yes")

# ─────────────────────────────────────────────────────────────
# Pares operados (lista separada por vírgula no .env)
# ─────────────────────────────────────────────────────────────
_raw_symbols = os.getenv("SYMBOLS", "BCH/USDT,LTC/USDT,DOGE/USDT")
SYMBOLS = [s.strip() for s in _raw_symbols.split(",") if s.strip()]
SYMBOL  = SYMBOLS[0]  # mantido para compatibilidade com módulos legados

TIMEFRAME_TREND = os.getenv("TIMEFRAME_TREND", "1d")  # Tendência principal
TIMEFRAME_ENTRY = os.getenv("TIMEFRAME_ENTRY", "4h")  # Gatilho de entrada

# ─────────────────────────────────────────────────────────────
# Gerenciamento de risco
# ─────────────────────────────────────────────────────────────
RISK_PCT      = float(os.getenv("RISK_PCT", "1.5"))
RR_RATIO      = float(os.getenv("RR_RATIO", "2.0"))
SL_CANDLES    = int(os.getenv("SL_CANDLES", "5"))
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "1.0"))

# ─────────────────────────────────────────────────────────────
# Trailing Stop
# ─────────────────────────────────────────────────────────────
# TRAILING_ACTIVATION_PCT : lucro mínimo (%) para ativar o trailing
# TRAILING_STOP_PCT       : distância (%) abaixo da máxima histórica
TRAILING_ACTIVATION_PCT = float(os.getenv("TRAILING_ACTIVATION_PCT", "1.0"))
TRAILING_STOP_PCT       = float(os.getenv("TRAILING_STOP_PCT", "2.0"))

# ─────────────────────────────────────────────────────────────
# Parâmetros do RSI
# ─────────────────────────────────────────────────────────────
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30.0"))
RSI_LOOKBACK = int(os.getenv("RSI_LOOKBACK", "3"))

# ─────────────────────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ─────────────────────────────────────────────────────────────
# Operacional
# ─────────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "900"))
OHLCV_LIMIT    = int(os.getenv("OHLCV_LIMIT", "200"))
LOG_FILE       = os.getenv("LOG_FILE", "bot_trade.log")
HISTORY_FILE   = os.getenv("HISTORY_FILE", "trade_history.csv")


def state_file_for(symbol: str) -> str:
    """Retorna o caminho do arquivo de estado para o par informado."""
    return f"state_{symbol.replace('/', '_')}.json"


def create_exchange() -> ccxt.Exchange:
    """
    Cria e retorna a instância configurada da exchange.

    - Usa ccxt para suportar OKX (e opcionalmente Binance).
    - Ativa sandbox/demo automaticamente quando TESTNET=True.
    - A OKX exige o campo 'password' (passphrase da API key).
    """
    if EXCHANGE_ID not in ccxt.exchanges:
        raise ValueError(
            f"Exchange '{EXCHANGE_ID}' não encontrada no ccxt. "
            f"Verifique o valor de EXCHANGE_ID no .env."
        )

    exchange_class = getattr(ccxt, EXCHANGE_ID)

    config = {
        "apiKey"         : API_KEY,
        "secret"         : API_SECRET,
        "password"       : API_PASSPHRASE,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    }

    exchange = exchange_class(config)

    if TESTNET:
        exchange.set_sandbox_mode(True)
        logging.getLogger("bot").info(
            "Modo TESTNET ativado — operações no ambiente de simulação (OKX Demo Trading)."
        )
    else:
        logging.getLogger("bot").warning(
            "Modo PRODUÇÃO ativado — operações com dinheiro real!"
        )

    return exchange
