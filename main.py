"""
main.py — Ponto de entrada do Swing Trade Bot (múltiplos pares).

Loop infinito que executa a cada CHECK_INTERVAL segundos (padrão: 15 min).

Fluxo de cada ciclo:
  1. Exibe saldo USDT disponível
  2. Para cada par monitorado:
     a. Se houver posição aberta → monitora Trailing SL / SL / TP
     b. Se não houver posição    → analisa indicadores e verifica sinal de entrada
  3. Registra tudo em log (terminal + arquivo)
  4. Aguarda o próximo ciclo

Interrompa com Ctrl+C para parar o bot com segurança.
"""

import logging
import sys
import time

from config import (
    CHECK_INTERVAL,
    LOG_FILE,
    SYMBOLS,
    TESTNET,
    TIMEFRAME_TREND,
    TIMEFRAME_ENTRY,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_MIN,
    RSI_MAX,
    create_exchange,
)
from indicators import calculate_indicators
from execution import (
    check_open_position,
    get_usdc_balance,
    load_state,
    open_position,
)
import telegram_notifier as tg


# ═════════════════════════════════════════════════════════════
# CONFIGURAÇÃO DE LOGGING
# ═════════════════════════════════════════════════════════════

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


# ═════════════════════════════════════════════════════════════
# LOOP PRINCIPAL
# ═════════════════════════════════════════════════════════════

def run_bot() -> None:
    logger = setup_logging(LOG_FILE)

    logger.info("═" * 62)
    logger.info("  Swing Trade Bot  —  Iniciando")
    logger.info(f"  Pares     : {' | '.join(SYMBOLS)}")
    logger.info(f"  Modo      : {'TESTNET / Paper Trading' if TESTNET else '⚠  PRODUÇÃO (dinheiro real)'}")
    logger.info(f"  Intervalo : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} min por ciclo)")
    logger.info(f"  Log       : {LOG_FILE}")
    logger.info("═" * 62)

    try:
        exchange = create_exchange()
        exchange.load_markets()
        logger.info(f"Exchange '{exchange.id}' conectada. Mercados carregados.")
    except Exception as exc:
        logger.critical(f"Falha crítica ao conectar à exchange: {exc}")
        sys.exit(1)

    tg.notify_bot_start(SYMBOLS)

    iteration = 0

    while True:
        iteration += 1
        logger.info("─" * 62)
        logger.info(f"[CICLO #{iteration:04d}]  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

        try:
            # Saldo exibido uma vez por ciclo
            try:
                get_usdc_balance(exchange)
            except Exception:
                pass

            # ── Processa cada par ─────────────────────────────────────────────
            for symbol in SYMBOLS:
                logger.info(f"── [{symbol}] " + "─" * (50 - len(symbol)))

                try:
                    state = load_state(symbol)

                    # ── RAMO A: Posição aberta → monitorar ────────────────────
                    if state.get("is_open"):
                        trail_str = "ATIVO" if state.get("trail_active") else "inativo"
                        logger.info(
                            f"[{symbol}] [STATUS] Posição ABERTA | "
                            f"Entry={state['entry_price']:.4f} | "
                            f"Qty={state['quantity']:.6f} | "
                            f"SL={state['sl_price']:.4f} | "
                            f"TP={state['tp_price']:.4f} | "
                            f"Trail={trail_str} | "
                            f"Modo={state.get('exit_mode', 'monitor').upper()}"
                        )
                        still_open = check_open_position(exchange, state, symbol)
                        if not still_open:
                            logger.info(f"[{symbol}] [STATUS] ✓ Posição encerrada neste ciclo.")
                        else:
                            logger.info(f"[{symbol}] [STATUS] Posição mantida. Aguardando próximo ciclo.")

                    # ── RAMO B: Sem posição → verificar sinal ──────────────────
                    else:
                        logger.info(f"[{symbol}] [STATUS] Sem posição aberta. Analisando mercado...")
                        indicators = calculate_indicators(exchange, symbol)

                        if indicators["signal"]:
                            logger.info(f"[{symbol}] [SINAL] ★★★ SINAL DE COMPRA DETECTADO! ★★★")
                            tg.notify_signal(symbol)
                            success = open_position(exchange, indicators, symbol)
                            if success:
                                logger.info(f"[{symbol}] [SINAL] Posição aberta com sucesso!")
                            else:
                                logger.warning(f"[{symbol}] [SINAL] Falha ao abrir posição.")
                        else:
                            reasons = []
                            if not indicators["trend_ok"]:
                                reasons.append(
                                    f"Tendência ({TIMEFRAME_TREND.upper()}): "
                                    f"EMA{EMA_MID}={indicators['ema_mid_t']:.4f} < "
                                    f"EMA{EMA_SLOW}={indicators['ema_slow_t']:.4f}"
                                )
                            if not indicators["cross_ok"]:
                                reasons.append(
                                    f"Cruzamento ({TIMEFRAME_ENTRY.upper()}): "
                                    f"EMA{EMA_FAST}={indicators['ema_fast_e']:.4f} não cruzou "
                                    f"EMA{EMA_MID}={indicators['ema_mid_e']:.4f} recentemente"
                                )
                            if not indicators["rsi_ok"]:
                                reasons.append(
                                    f"RSI ({TIMEFRAME_ENTRY.upper()})={indicators['rsi_current']:.2f} "
                                    f"fora da zona [{RSI_MIN}–{RSI_MAX}]"
                                )
                            logger.info(f"[{symbol}] [SEM SINAL] Motivos:")
                            for i, r in enumerate(reasons, 1):
                                logger.info(f"  {i}. {r}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.error(
                        f"[{symbol}] Erro inesperado no ciclo: {exc}",
                        exc_info=True,
                    )

        except KeyboardInterrupt:
            logger.info("\n[BOT] Interrompido pelo usuário (Ctrl+C). Encerrando com segurança...")
            break

        except Exception as exc:
            logger.error(
                f"[CICLO #{iteration:04d}] Erro inesperado: {exc}",
                exc_info=True,
            )
            logger.info("Aguardando 60 segundos antes de tentar novamente...")
            time.sleep(60)
            continue

        logger.info(
            f"[CICLO #{iteration:04d}] Concluído. "
            f"Próxima verificação em {CHECK_INTERVAL // 60} minutos."
        )
        time.sleep(CHECK_INTERVAL)


# ═════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_bot()
