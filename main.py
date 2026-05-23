"""
main.py — Ponto de entrada do Swing Trade Bot (Com Graceful Shutdown).
"""
import logging
import sys
import time
import signal
from datetime import datetime, timezone

from config import CHECK_INTERVAL, LOG_FILE, SYMBOLS, TESTNET, create_exchange
from indicators import calculate_indicators
from execution import check_open_position, get_usdt_balance, load_state, open_position
import telegram_notifier as tg
import trade_history as history

shutdown_flag = False

def handle_shutdown(signum, frame):
    global shutdown_flag
    logger = logging.getLogger("bot")
    logger.warning("\n[SISTEMA] Desligamento seguro ativado! Fechando o ciclo e salvando...")
    shutdown_flag = True

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(fmt="%(asctime)s [%(levelname)-8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    
    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger

def run_bot() -> None:
    global shutdown_flag
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logger = setup_logging(LOG_FILE)
    logger.info("═" * 62)
    logger.info(f"  Modo: {'TESTNET' if TESTNET else '⚠ PRODUÇÃO (Dinheiro Real)'}")
    logger.info("═" * 62)

    try:
        exchange = create_exchange()
        exchange.load_markets()
    except Exception as exc:
        logger.critical(f"Falha de Exchange: {exc}")
        sys.exit(1)

    try: initial_balance = get_usdt_balance(exchange)
    except Exception: initial_balance = 0.0

    tg.notify_bot_start(SYMBOLS, initial_balance)

    iteration = 0
    last_summary_date = None

    while not shutdown_flag:
        iteration += 1
        current_utc_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        logger.info(f"[CICLO #{iteration:04d}] {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

        if last_summary_date != current_utc_date and iteration > 1:
            try:
                bal = get_usdt_balance(exchange)
                pnl = history.get_current_month_pnl()
                opes = sum(1 for sym in SYMBOLS if load_state(sym).get("is_open"))
                tg.notify_daily_summary(bal, pnl, opes)
            except Exception: pass
            last_summary_date = current_utc_date

        for symbol in SYMBOLS:
            if shutdown_flag: break
            try:
                state = load_state(symbol)
                if state.get("is_open"):
                    check_open_position(exchange, state, symbol)
                else:
                    inds = calculate_indicators(exchange, symbol)
                    if inds.get("signal"):
                        tg.notify_signal(symbol)
                        open_position(exchange, inds, symbol)
            except Exception as exc:
                logger.error(f"Erro [{symbol}]: {exc}")

        if not shutdown_flag:
            for _ in range(CHECK_INTERVAL):
                if shutdown_flag: break
                time.sleep(1)

    logger.info("🤖 Desligamento seguro concluído.")
    tg.send("🤖 <b>Bot desligado com segurança.</b>")
    sys.exit(0)

if __name__ == "__main__":
    run_bot()