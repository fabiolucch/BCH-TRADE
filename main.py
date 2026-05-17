"""main.py — Ponto de entrada: inicializa módulos e roda o loop DCA + Telegram."""
import asyncio
import logging
import signal
import sys

from config import CHECK_INTERVAL, LOG_FILE, PAIRS, TESTNET
from dca import DCAEngine
from exchange import ExchangeClient
from state import StateManager
from telegram_handler import TelegramHandler


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(name)-18s] [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file_h = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(fmt)

    logger.addHandler(console)
    logger.addHandler(file_h)
    return logger


async def dca_loop(engine: DCAEngine) -> None:
    cycle = 0
    while True:
        cycle += 1
        logging.getLogger("bot.dca").debug(f"── Ciclo #{cycle:06d} ──")
        await engine.tick()
        await asyncio.sleep(CHECK_INTERVAL)


async def main() -> None:
    logger = setup_logging()

    logger.info("═" * 60)
    logger.info("  DCA Trading Bot — Iniciando")
    logger.info(f"  Modo     : {'⚠  PRODUÇÃO (dinheiro real)' if not TESTNET else 'DEMO'}")
    logger.info(f"  Pares    : {', '.join(PAIRS)}")
    logger.info(f"  Intervalo: {CHECK_INTERVAL}s")
    logger.info("═" * 60)

    # ── Inicialização dos módulos ─────────────────────────────────────────────
    try:
        exchange = ExchangeClient()
    except Exception as exc:
        logger.critical(f"Falha ao conectar à exchange: {exc}")
        sys.exit(1)

    state    = StateManager()
    telegram = TelegramHandler(state)
    engine   = DCAEngine(exchange, state, notify=telegram.send)
    telegram.set_engine(engine)

    # ── Inicia Telegram antes do loop ─────────────────────────────────────────
    await telegram.start()
    await telegram.send(
        f"🤖 *DCA Bot iniciado*\n"
        f"Modo: `{'DEMO' if TESTNET else 'PRODUÇÃO'}`\n"
        f"Pares: `{', '.join(PAIRS)}`\n"
        f"Intervalo: `{CHECK_INTERVAL}s`"
    )

    # ── Sinal de encerramento gracioso ────────────────────────────────────────
    # get_running_loop() é obrigatório no Python 3.12+ dentro de coroutines.
    # Docker envia SIGTERM para PID 1; o exec form no CMD garante que o Python
    # seja PID 1 e receba o sinal diretamente.
    loop = asyncio.get_running_loop()

    def _on_signal(sig: signal.Signals) -> None:
        logger.info(f"Sinal {sig.name} recebido. Encerrando…")
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: _on_signal(s))

    # ── Execução paralela: DCA + schedulers Telegram ──────────────────────────
    try:
        await asyncio.gather(
            dca_loop(engine),
            telegram.schedule_daily_report(),
            telegram.schedule_monthly_report(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Encerrando bot…")
        try:
            await telegram.send("🛑 DCA Bot encerrado.")
        except Exception:
            pass
        await telegram.stop()
        logger.info("Bot encerrado com segurança.")


if __name__ == "__main__":
    asyncio.run(main())
