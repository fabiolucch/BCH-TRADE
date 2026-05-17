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
MAX_DCA_ORDERS  = int(os.getenv("MAX_DCA_ORDERS", "10"))       # máximo de aportes por par

# ── Operacional ───────────────────────────────────────────────────────────────
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))        # segundos entre ciclos
STATE_FILE     = Path(os.getenv("STATE_FILE", "state.json"))
LOG_FILE       = Path(os.getenv("LOG_FILE", "dca_bot.log"))

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "00:00")    # HH:MM UTC
