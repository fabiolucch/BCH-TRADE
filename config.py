"""config.py — Todas as configurações carregadas via variáveis de ambiente."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Exchange ──────────────────────────────────────────────────────────────────
EXCHANGE_ID    = os.getenv("EXCHANGE_ID", "okx")
API_KEY        = os.getenv("API_KEY", "")
API_SECRET     = os.getenv("API_SECRET", "")
API_PASSPHRASE = os.getenv("API_PASSPHRASE", "")
TESTNET        = os.getenv("TESTNET", "False").strip().lower() in ("true", "1", "yes")

# ── Pares monitorados ─────────────────────────────────────────────────────────
# Ex: PAIRS=BTC/USDT,ETH/USDT,SOL/USDT
PAIRS: list[str] = [
    p.strip() for p in os.getenv("PAIRS", "BTC/USDT").split(",") if p.strip()
]

# ── Parâmetros DCA ────────────────────────────────────────────────────────────
DCA_DROP_PCT    = float(os.getenv("DCA_DROP_PCT", "3.0"))     # % de queda para nova entrada
ORDER_SIZE_USDT = float(os.getenv("ORDER_SIZE_USDT", "20.0")) # valor em quote por aporte
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "1.5"))  # % acima do preço médio para vender
MAX_DCA_ORDERS    = int(os.getenv("MAX_DCA_ORDERS", "10"))       # máximo de aportes por par
MARTINGALE_LEVELS = int(os.getenv("MARTINGALE_LEVELS", "0"))   # 0=off; 1-3 níveis de dobra linear
REENTRY_DROP_PCT  = float(os.getenv("REENTRY_DROP_PCT", "1.5")) # % abaixo do preço de saída para re-entrada (0=desativado)

# ── Stop Loss absoluto (Elder) ────────────────────────────────────────────────
STOP_LOSS_ENABLED = os.getenv("STOP_LOSS_ENABLED", "False").strip().lower() in ("true", "1", "yes")
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "15.0"))  # % de queda máxima aceita

# ── Filtro de Tendência EMA (Elder) ──────────────────────────────────────────
TREND_FILTER_ENABLED = os.getenv("TREND_FILTER_ENABLED", "False").strip().lower() in ("true", "1", "yes")
TREND_EMA_PERIOD     = int(os.getenv("TREND_EMA_PERIOD", "21"))  # períodos diários

# ── Circuit Breaker (Elder) ───────────────────────────────────────────────────
CIRCUIT_BREAKER_ENABLED = os.getenv("CIRCUIT_BREAKER_ENABLED", "False").strip().lower() in ("true", "1", "yes")
CIRCUIT_BREAKER_PCT     = float(os.getenv("CIRCUIT_BREAKER_PCT", "10.0"))  # % drawdown total para pausar

# ── Filtro RSI ────────────────────────────────────────────────────────────────
RSI_ENABLED   = os.getenv("RSI_ENABLED", "False").strip().lower() in ("true", "1", "yes")
RSI_THRESHOLD = float(os.getenv("RSI_THRESHOLD", "45.0"))  # só entra se RSI < threshold
RSI_PERIOD    = int(os.getenv("RSI_PERIOD", "14"))          # período padrão do RSI

# ── Trailing Stop (padrão inicial; ajustável via Telegram) ───────────────────
TRAILING_STOP_ENABLED = os.getenv("TRAILING_STOP_ENABLED", "False").strip().lower() in ("true", "1", "yes")
TRAILING_STOP_PCT     = float(os.getenv("TRAILING_STOP_PCT", "1.0"))  # % abaixo do pico para vender

# ── Taxa da corretora ─────────────────────────────────────────────────────────
# OKX spot taker: 0.1% (0.001). Usado para PnL preciso quando a exchange
# não retorna o campo fee na resposta da ordem.
FEE_RATE = float(os.getenv("FEE_RATE", "0.001"))

# ── Operacional ───────────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))        # segundos entre ciclos
STATE_FILE     = Path(os.getenv("STATE_FILE", "state.json"))
LOG_FILE       = Path(os.getenv("LOG_FILE", "dca_bot.log"))

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "00:00")    # HH:MM UTC
# Garante formato válido mesmo se .env tiver valor vazio ou comentário inline
if ":" not in DAILY_REPORT_TIME:
    DAILY_REPORT_TIME = "00:00"
