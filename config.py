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


# ─────────────────────────────────────────────────────────────
# Triple Screen — timeframes
# ─────────────────────────────────────────────────────────────
TIMEFRAME_WEEKLY  = os.getenv("TIMEFRAME_WEEKLY", "1w")

# ─────────────────────────────────────────────────────────────
# DCA levels — number and drop thresholds
# ─────────────────────────────────────────────────────────────
DCA_MAX_LEVELS   = int(os.getenv("DCA_MAX_LEVELS", "3"))
DCA_DROP_L2_PCT  = float(os.getenv("DCA_DROP_L2_PCT", "3.0"))   # % drop from L1 for L2 entry
DCA_DROP_L3_PCT  = float(os.getenv("DCA_DROP_L3_PCT", "5.0"))   # % drop from L2 for L3 entry
DCA_RISK_L1_PCT  = float(os.getenv("DCA_RISK_L1_PCT", "1.0"))   # % account risk on L1
DCA_RISK_L2_PCT  = float(os.getenv("DCA_RISK_L2_PCT", "1.5"))   # % account risk on L2
DCA_RISK_L3_PCT  = float(os.getenv("DCA_RISK_L3_PCT", "2.0"))   # % account risk on L3

# ─────────────────────────────────────────────────────────────
# Hard stop — Elder's "oxygen tank": survival before profit
# ─────────────────────────────────────────────────────────────
HARD_STOP_PCT    = float(os.getenv("HARD_STOP_PCT", "4.0"))     # max total loss as % of account

# ─────────────────────────────────────────────────────────────
# Take Profit scaling
# ─────────────────────────────────────────────────────────────
TP1_RISK_RATIO   = float(os.getenv("TP1_RISK_RATIO", "1.5"))    # TP1 at avg_entry + 1.5× risk
TP2_RISK_RATIO   = float(os.getenv("TP2_RISK_RATIO", "2.5"))    # TP2 at avg_entry + 2.5× risk
TP1_CLOSE_PCT    = float(os.getenv("TP1_CLOSE_PCT", "0.30"))    # close 30% at TP1
TP2_CLOSE_PCT    = float(os.getenv("TP2_CLOSE_PCT", "0.40"))    # close 40% at TP2
                                                                  # trailing stop for remaining 30%

# ─────────────────────────────────────────────────────────────
# Trailing stop
# ─────────────────────────────────────────────────────────────
TRAIL_ATR_MULT   = float(os.getenv("TRAIL_ATR_MULT", "2.0"))    # trailing = peak - (ATR × mult)

# ─────────────────────────────────────────────────────────────
# MACD parameters — used on weekly screen for trend direction
# ─────────────────────────────────────────────────────────────
MACD_FAST          = int(os.getenv("MACD_FAST", "12"))
MACD_SLOW          = int(os.getenv("MACD_SLOW", "26"))
MACD_SIGNAL_PERIOD = int(os.getenv("MACD_SIGNAL_PERIOD", "9"))

# ─────────────────────────────────────────────────────────────
# Volume filter — daily screen confirms crowd participation
# ─────────────────────────────────────────────────────────────
VOL_MA_PERIOD    = int(os.getenv("VOL_MA_PERIOD", "20"))

# ─────────────────────────────────────────────────────────────
# RSI thresholds for DCA levels
# ─────────────────────────────────────────────────────────────
RSI_L2_THRESHOLD = float(os.getenv("RSI_L2_THRESHOLD", "35.0"))  # RSI must be < 35 for L2
RSI_L3_THRESHOLD = float(os.getenv("RSI_L3_THRESHOLD", "30.0"))  # RSI must be < 30 for L3

# ─────────────────────────────────────────────────────────────
# EMA Weekly
# ─────────────────────────────────────────────────────────────
EMA_WEEKLY_PERIOD = int(os.getenv("EMA_WEEKLY_PERIOD", "13"))


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
