import logging
import sys
import time

from config import CHECK_INTERVAL, LOG_FILE, QUOTE_CURRENCY, SYMBOLS, TESTNET, create_exchange
from indicators import calculate_indicators
from execution import check_open_position, get_quote_balance, load_state, open_position
from notifier import send_telegram, telegram_enabled, telegram_status, test_telegram


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
    logger = setup_logging(LOG_FILE)
    logger.info("═" * 62)
    logger.info("  OKX Swing Trade Bot  —  Iniciando")
    logger.info(f"  Pares     : {', '.join(SYMBOLS)}")
    logger.info(f"  Modo      : {'TESTNET / Paper Trading' if TESTNET else '⚠  PRODUÇÃO'}")
    logger.info(f"  Telegram  : {'ATIVO' if telegram_enabled() else 'DESATIVADO'} ({telegram_status()})")
    logger.info("═" * 62)

    try:
        exchange = create_exchange()
        exchange.load_markets()
        server_time = exchange.fetch_time()
        logger.info(f"Exchange '{exchange.id}' conectada. Mercados carregados. server_time={server_time}")
        balance = get_quote_balance(exchange)
        logger.info(f"Conexão autenticada com API privada OKX. Saldo {QUOTE_CURRENCY}={balance:.2f}")
        if telegram_enabled():
            started_msg = (
                f"🤖 Bot iniciado em {'TESTNET' if TESTNET else 'PRODUÇÃO'} | "
                f"Pares: {', '.join(SYMBOLS)}"
            )
            send_telegram(started_msg)
            send_telegram(
                f"✅ API OKX conectada com sucesso | "
                f"Saldo {QUOTE_CURRENCY}: {balance:.2f}"
            )
            if test_telegram():
                logger.info("Teste de Telegram enviado com sucesso.")
            else:
                logger.warning("Falha no teste de Telegram. Verifique token/chat_id e firewall de saída.")
        else:
            logger.warning("Telegram desativado por configuração: %s", telegram_status())
    except Exception as exc:
        logger.critical(f"Falha crítica ao conectar à exchange: {exc}")
        send_telegram(f"❌ Falha na conexão com API OKX: {exc}")
        sys.exit(1)

    iteration = 0
    while True:
        iteration += 1
        logger.info(f"[CICLO #{iteration:04d}] {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
        try:
            for symbol in SYMBOLS:
                logger.info(f"[{symbol}] Iniciando análise...")
                state = load_state(symbol)
                if state.get("is_open"):
                    still_open = check_open_position(exchange, symbol, state)
                    if not still_open:
                        send_telegram(f"✅ [{symbol}] posição encerrada.")
                    continue

                indicators = calculate_indicators(exchange, symbol)
                if indicators["signal"]:
                    success = open_position(exchange, symbol, indicators)
                    if success:
                        send_telegram(f"🟢 [{symbol}] posição aberta.")
                else:
                    logger.info(f"[{symbol}] [SEM SINAL]")

        except KeyboardInterrupt:
            logger.info("[BOT] Interrompido pelo usuário.")
            break
        except Exception as exc:
            logger.error(f"[CICLO #{iteration:04d}] Erro inesperado: {exc}", exc_info=True)
            send_telegram(f"⚠️ Erro no ciclo {iteration}: {exc}")
            time.sleep(60)
            continue

        logger.info(f"[CICLO #{iteration:04d}] Concluído. Próxima verificação em {CHECK_INTERVAL // 60} min.")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_bot()
