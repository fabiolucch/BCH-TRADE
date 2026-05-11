"""
main.py — Ponto de entrada do BCH/USDT Swing Trade Bot.

Loop infinito que executa a cada CHECK_INTERVAL segundos (padrão: 15 min).

Fluxo de cada ciclo:
  1. Carrega o estado da posição (arquivo JSON)
  2. Se houver posição aberta → monitora SL/TP
  3. Se não houver posição    → analisa indicadores e verifica sinal de entrada
  4. Registra tudo em log (terminal + arquivo)
  5. Aguarda o próximo ciclo

Interrompa com Ctrl+C para parar o bot com segurança.
"""

import logging
import sys
import time

from config import (
    CHECK_INTERVAL,
    LOG_FILE,
    SYMBOL,
    TESTNET,
    create_exchange,
)
from indicators import calculate_indicators
from execution import (
    check_open_position,
    get_usdc_balance,
    load_state,
    open_position,
)


# ═════════════════════════════════════════════════════════════
# CONFIGURAÇÃO DE LOGGING
# ═════════════════════════════════════════════════════════════

def setup_logging(log_file: str) -> logging.Logger:
    """
    Configura dois handlers de log:
    - Terminal (stdout): nível INFO — mensagens operacionais
    - Arquivo (log_file): nível DEBUG — log completo para análise posterior
    """
    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler de terminal
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    # Handler de arquivo (append mode — não sobrescreve entre reinicializações)
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

    # ── Banner de inicialização ───────────────────────────────────────────────
    logger.info("═" * 62)
    logger.info("  BCH/USDT Swing Trade Bot  —  Iniciando")
    logger.info(f"  Par       : {SYMBOL}")
    logger.info(f"  Modo      : {'TESTNET / Paper Trading' if TESTNET else '⚠  PRODUÇÃO (dinheiro real)'}")
    logger.info(f"  Intervalo : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} min por ciclo)")
    logger.info(f"  Log       : {LOG_FILE}")
    logger.info("═" * 62)

    # ── Conexão com a exchange ────────────────────────────────────────────────
    try:
        exchange = create_exchange()
        exchange.load_markets()
        logger.info(f"Exchange '{exchange.id}' conectada. Mercados carregados.")
    except Exception as exc:
        logger.critical(f"Falha crítica ao conectar à exchange: {exc}")
        sys.exit(1)

    iteration = 0

    while True:
        iteration += 1
        logger.info("─" * 62)
        logger.info(f"[CICLO #{iteration:04d}]  {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

        try:
            # ── Carrega estado da posição ─────────────────────────────────────
            state = load_state()

            # ── RAMO A: Posição aberta → monitorar SL/TP ──────────────────────
            if state.get("is_open"):
                logger.info(
                    f"[STATUS] Posição ABERTA | "
                    f"Entry={state['entry_price']:.4f} | "
                    f"Qty={state['quantity']:.6f} BCH | "
                    f"SL={state['sl_price']:.4f} | "
                    f"TP={state['tp_price']:.4f} | "
                    f"Modo={state.get('exit_mode', 'monitor').upper()}"
                )

                still_open = check_open_position(exchange, state)

                if not still_open:
                    logger.info("[STATUS] ✓ Posição encerrada neste ciclo.")
                else:
                    logger.info("[STATUS] Posição mantida. Aguardando próximo ciclo.")

            # ── RAMO B: Sem posição → verificar sinal de entrada ───────────────
            else:
                logger.info("[STATUS] Sem posição aberta. Analisando mercado...")

                # Exibe saldo atual antes da análise
                try:
                    get_usdc_balance(exchange)
                except Exception:
                    pass  # erro já logado em get_usdc_balance

                # Calcula indicadores e avalia condições
                indicators = calculate_indicators(exchange)

                if indicators["signal"]:
                    logger.info(
                        "[SINAL] ★★★ SINAL DE COMPRA DETECTADO! ★★★  "
                        "Tendência=OK | Pullback=OK | RSI=OK"
                    )
                    success = open_position(exchange, indicators)

                    if success:
                        logger.info("[SINAL] Posição aberta com sucesso!")
                    else:
                        logger.warning("[SINAL] Sinal detectado, mas falha ao abrir posição.")

                else:
                    # Log detalhado dos motivos para não entrar
                    reasons = []

                    if not indicators["trend_ok"]:
                        reasons.append(
                            f"Tendência: Close_1D={indicators['close_1d']:.4f} "
                            f"abaixo da EMA50_1D={indicators['ema50_1d']:.4f}"
                        )

                    if not indicators["pullback_ok"]:
                        reasons.append(
                            f"Pullback: Close_4H={indicators['close_4h']:.4f} fora da zona "
                            f"[EMA50={indicators['ema50_4h']:.4f} ~ EMA20={indicators['ema20_4h']:.4f}]"
                        )

                    if not indicators["rsi_ok"]:
                        oversold_str  = "Sim" if indicators["rsi_was_oversold"]  else "Não"
                        recover_str   = "Sim" if indicators["rsi_recovering"]     else "Não"
                        reasons.append(
                            f"RSI: valor={indicators['rsi_current']:.2f} | "
                            f"Sobrevenda recente={oversold_str} | "
                            f"Recuperando={recover_str}"
                        )

                    logger.info("[SEM SINAL] Motivos:")
                    for i, r in enumerate(reasons, 1):
                        logger.info(f"  {i}. {r}")

        except KeyboardInterrupt:
            logger.info("\n[BOT] Interrompido pelo usuário (Ctrl+C). Encerrando com segurança...")
            break

        except (Exception,) as exc:
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
