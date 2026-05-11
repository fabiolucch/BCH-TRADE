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
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")  # Obrigatório na OKX

# True  → modo demo/testnet (OKX Paper Trading)
# False → dinheiro real
TESTNET = os.getenv("TESTNET", "True").strip().lower() in ("true", "1", "yes")

# ─────────────────────────────────────────────────────────────
# Par e timeframes
# ─────────────────────────────────────────────────────────────
SYMBOL           = os.getenv("SYMBOL", "BCH/USDC")
TIMEFRAME_TREND  = os.getenv("TIMEFRAME_TREND", "1d")  # Tendência principal
TIMEFRAME_ENTRY  = os.getenv("TIMEFRAME_ENTRY", "4h")  # Gatilho de entrada

# ─────────────────────────────────────────────────────────────
# Gerenciamento de risco
# ─────────────────────────────────────────────────────────────
RISK_PCT      = float(os.getenv("RISK_PCT", "1.5"))    # % do saldo por operação
RR_RATIO      = float(os.getenv("RR_RATIO", "2.0"))    # Risco:Retorno
SL_CANDLES    = int(os.getenv("SL_CANDLES", "5"))      # Candles para mínima do SL
SL_BUFFER_PCT = float(os.getenv("SL_BUFFER_PCT", "1.0"))  # % abaixo da mínima

# ─────────────────────────────────────────────────────────────
# Parâmetros do RSI
# ─────────────────────────────────────────────────────────────
RSI_OVERSOLD = float(os.getenv("RSI_OVERSOLD", "30.0"))
RSI_LOOKBACK = int(os.getenv("RSI_LOOKBACK", "3"))

# ─────────────────────────────────────────────────────────────
# Operacional
# ─────────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "900"))    # segundos
OHLCV_LIMIT    = int(os.getenv("OHLCV_LIMIT", "200"))       # candles históricos
STATE_FILE     = os.getenv("STATE_FILE", "position_state.json")
LOG_FILE       = os.getenv("LOG_FILE", "bot_trade.log")


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
        "password"       : API_PASSPHRASE,  # 'password' é o campo ccxt para passphrase OKX
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",          # Garante operações no mercado spot
        },
    }

    exchange = exchange_class(config)

    if TESTNET:
        # OKX Demo Trading: set_sandbox_mode ajusta as URLs automaticamente
        exchange.set_sandbox_mode(True)
        logging.getLogger("bot").info(
            "Modo TESTNET ativado — operações no ambiente de simulação (OKX Demo Trading)."
        )
    else:
        logging.getLogger("bot").warning(
            "Modo PRODUÇÃO ativado — operações com dinheiro real!"
        )

    return exchange
