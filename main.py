"""
main.py — Ponto de entrada do BCH/USDC Swing Trade Bot.

Loop infinito que executa a cada CHECK_INTERVAL segundos (padrão: 15 min).

Fluxo de cada ciclo (run_bot — estratégia original BCH/USDC):
  1. Carrega o estado da posição (arquivo JSON)
  2. Se houver posição aberta → monitora SL/TP
  3. Se não houver posição    → analisa indicadores e verifica sinal de entrada
  4. Registra tudo em log (terminal + arquivo)
  5. Aguarda o próximo ciclo

Fluxo de cada ciclo (run_dca_bot — Elder Triple Screen DCA):
  1. Per-symbol: carrega estado DCA da posição
  2. Se posição aberta: verifica saídas (hard stop, TP1, TP2, trailing)
  3. Se posição aberta com < 3 levels: verifica condições de adicionar ao DCA
  4. Se sem posição: verifica sinal de entrada L1
  5. Registra tudo no diário de operações (trade_journal.csv)

Interrompa com Ctrl+C para parar o bot com segurança.
"""

import csv
import logging
import os
import sys
import time
from typing import List, Optional

from config import (
    CHECK_INTERVAL,
    DCA_MAX_LEVELS,
    LOG_FILE,
    SL_BUFFER_PCT,
    SL_CANDLES,
    SYMBOL,
    TESTNET,
    create_exchange,
)
from indicators import calculate_indicators, calculate_triple_screen
from execution import (
    check_open_position,
    calculate_dca_qty,
    execute_full_close,
    execute_partial_close,
    get_usdc_balance,
    load_dca_state,
    load_state,
    open_position,
    save_dca_state,
    _apply_dca_precision,
)
from strategy import (
    DCALevel,
    DCAPosition,
    calculate_tp_prices,
    check_dca_entry,
    check_exits,
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
    logger.info("  BCH/USDC Swing Trade Bot  —  Iniciando")
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
# TRADE JOURNAL
# ═════════════════════════════════════════════════════════════

JOURNAL_FILE = "trade_journal.csv"
_JOURNAL_HEADERS = [
    "timestamp_utc",
    "symbol",
    "action",
    "level",
    "price",
    "quantity",
    "avg_price",
    "total_qty",
    "hard_stop",
    "tp1",
    "tp2",
    "account_balance",
    "reason",
]


def _log_journal(row: dict) -> None:
    """
    Appends one row to the trade journal CSV.

    Elder's "diary" principle: every trade must be recorded with its REASON.
    Without a written reason, traders repeat the same mistakes and cannot
    identify patterns in their decision-making over time.
    """
    file_exists = os.path.exists(JOURNAL_FILE)
    try:
        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_JOURNAL_HEADERS, extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            # Fill missing fields with empty string for clean CSV
            writer.writerow({h: row.get(h, "") for h in _JOURNAL_HEADERS})
    except OSError as exc:
        logging.getLogger("bot").warning(f"[JOURNAL] Failed to write to {JOURNAL_FILE}: {exc}")


# ═════════════════════════════════════════════════════════════
# DCA BOT LOOP
# ═════════════════════════════════════════════════════════════

def run_dca_bot(symbols: Optional[List[str]] = None) -> None:
    """
    DCA bot loop using Triple Screen strategy (Elder's three pillars).

    Per cycle per symbol:
      1. Load DCA position state from JSON
      2. Fetch Triple Screen indicators
      3. If position open: check all exits (hard stop, TP1, TP2, trailing)
      4. If position open and < DCA_MAX_LEVELS: check DCA add conditions
      5. If no position: check L1 entry signal
      6. Log everything to trade_journal.csv

    The hard stop check runs FIRST, every single cycle, before any other logic.
    This is non-negotiable — Elder's "oxygen tank" must always be checked first.
    """
    logger = setup_logging(LOG_FILE)

    if symbols is None:
        symbols = [SYMBOL]

    logger.info("=" * 62)
    logger.info("  Triple Screen DCA Bot (Elder)  —  Iniciando")
    logger.info(f"  Symbols   : {', '.join(symbols)}")
    logger.info(f"  Modo      : {'TESTNET / Paper Trading' if TESTNET else '  PRODUCAO (dinheiro real)'}")
    logger.info(f"  Intervalo : {CHECK_INTERVAL}s ({CHECK_INTERVAL // 60} min por ciclo)")
    logger.info(f"  Journal   : {JOURNAL_FILE}")
    logger.info(f"  Log       : {LOG_FILE}")
    logger.info("=" * 62)

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
        ts_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        logger.info("-" * 62)
        logger.info(f"[CICLO #{iteration:04d}]  {ts_utc}")

        for symbol in symbols:
            logger.info(f"[{symbol}] --- processando ---")
            try:
                _run_dca_cycle(exchange, symbol, logger)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.error(
                    f"[{symbol}] Erro inesperado no ciclo: {exc}", exc_info=True
                )

        logger.info(
            f"[CICLO #{iteration:04d}] Concluído. "
            f"Próxima verificação em {CHECK_INTERVAL // 60} minutos."
        )

        try:
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("\n[BOT] Interrompido pelo usuário (Ctrl+C). Encerrando...")
            break


def _run_dca_cycle(exchange, symbol: str, logger: logging.Logger) -> None:
    """
    Executes one complete DCA cycle for a single symbol.
    Separated from the main loop to keep the loop clean and enable per-symbol error isolation.
    """
    # ── Load state ────────────────────────────────────────────────────────────
    position = load_dca_state(symbol)

    # ── Fetch account balance (needed for sizing and hard stop check) ─────────
    balance = 0.0
    try:
        # Try USDT first, then USDC (different pairs use different quote currencies)
        raw_balance = exchange.fetch_balance()
        for quote in ("USDT", "USDC", "BUSD"):
            balance = float(raw_balance.get(quote, {}).get("free", 0.0))
            if balance > 0:
                break
        logger.info(f"[{symbol}] Balance: {balance:.2f}")
    except Exception as exc:
        logger.warning(f"[{symbol}] Could not fetch balance: {exc}. Using 0.")

    # ── Fetch indicators (Triple Screen) ─────────────────────────────────────
    try:
        indicators = calculate_triple_screen(exchange, symbol)
    except Exception as exc:
        logger.error(f"[{symbol}] Failed to calculate indicators: {exc}", exc_info=True)
        return

    current_price = indicators.get("close_4h", 0.0)
    atr           = indicators.get("atr_4h", 0.0)

    # ═══ PHASE 1: Exit checks (if position is open) ══════════════════════════
    if position.is_open:
        logger.info(
            f"[{symbol}] Position open | levels={len(position.levels)} | "
            f"avg={position.avg_price:.4f} | qty={position.total_qty:.6f} | "
            f"hard_stop={position.hard_stop_price:.4f} | "
            f"tp1={position.tp1_price:.4f} | tp2={position.tp2_price:.4f}"
        )

        exit_signal = check_exits(position, current_price, atr, balance)

        if exit_signal:
            exit_type    = exit_signal["type"]
            qty_to_close = exit_signal["qty_to_close"]
            reason       = exit_signal["reason"]

            if exit_type in ("hard_stop", "stop_loss", "trailing"):
                # Full close — hard stop is the highest-priority exit; no exceptions
                success = execute_full_close(exchange, position, reason)
                if success:
                    position.is_open = False
                    _log_journal({
                        "timestamp_utc"   : time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                        "symbol"          : symbol,
                        "action"          : f"EXIT_{exit_type.upper()}",
                        "level"           : len(position.levels),
                        "price"           : current_price,
                        "quantity"        : qty_to_close,
                        "avg_price"       : position.avg_price,
                        "total_qty"       : 0,
                        "hard_stop"       : position.hard_stop_price,
                        "tp1"             : position.tp1_price,
                        "tp2"             : position.tp2_price,
                        "account_balance" : balance,
                        "reason"          : reason,
                    })
                    save_dca_state(position)
                    logger.warning(f"[{symbol}] Position CLOSED via {exit_type.upper()}.")
                    return

            elif exit_type == "tp1":
                success = execute_partial_close(exchange, position, qty_to_close, reason)
                if success:
                    position.tp1_filled = True
                    # Recalculate TPs based on new avg after partial fill
                    position.tp1_price, position.tp2_price = calculate_tp_prices(
                        position.avg_price, position.hard_stop_price
                    )
                    _log_journal({
                        "timestamp_utc"   : time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                        "symbol"          : symbol,
                        "action"          : "TP1",
                        "level"           : len(position.levels),
                        "price"           : current_price,
                        "quantity"        : qty_to_close,
                        "avg_price"       : position.avg_price,
                        "total_qty"       : position.total_qty,
                        "hard_stop"       : position.hard_stop_price,
                        "tp1"             : position.tp1_price,
                        "tp2"             : position.tp2_price,
                        "account_balance" : balance,
                        "reason"          : reason,
                    })
                    save_dca_state(position)
                    logger.info(f"[{symbol}] TP1 executed. Remaining qty={position.total_qty:.6f}")

            elif exit_type == "tp2":
                success = execute_partial_close(exchange, position, qty_to_close, reason)
                if success:
                    position.tp2_filled = True
                    _log_journal({
                        "timestamp_utc"   : time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                        "symbol"          : symbol,
                        "action"          : "TP2",
                        "level"           : len(position.levels),
                        "price"           : current_price,
                        "quantity"        : qty_to_close,
                        "avg_price"       : position.avg_price,
                        "total_qty"       : position.total_qty,
                        "hard_stop"       : position.hard_stop_price,
                        "tp1"             : position.tp1_price,
                        "tp2"             : position.tp2_price,
                        "account_balance" : balance,
                        "reason"          : reason,
                    })
                    save_dca_state(position)
                    logger.info(f"[{symbol}] TP2 executed. Remaining qty={position.total_qty:.6f}")

            # Save trailing state updates even without a trade (peak/stop moved)
            save_dca_state(position)

    # ═══ PHASE 2: DCA add check (if position open and below max levels) ═══════
    if position.is_open and len(position.levels) < DCA_MAX_LEVELS:
        dca_signal = check_dca_entry(position, indicators, balance)

        if dca_signal and dca_signal["action"] in ("add_l2", "add_l3"):
            _execute_dca_add(exchange, position, dca_signal, current_price, balance, symbol, logger)

    # ═══ PHASE 3: L1 entry (if no position) ══════════════════════════════════
    elif not position.is_open:
        dca_signal = check_dca_entry(position, indicators, balance)

        if dca_signal and dca_signal["action"] == "open_l1":
            _execute_dca_open(exchange, position, dca_signal, indicators, balance, symbol, logger)
        else:
            logger.info(
                f"[{symbol}] No entry signal. "
                f"screen1={indicators.get('screen1_ok')} | "
                f"screen2={indicators.get('screen2_ok')} | "
                f"screen3={indicators.get('screen3_ok')}"
            )


def _execute_dca_open(
    exchange,
    position: DCAPosition,
    dca_signal: dict,
    indicators: dict,
    balance: float,
    symbol: str,
    logger: logging.Logger,
) -> None:
    """Opens L1 of a new DCA position."""
    current_price = indicators.get("close_4h", 0.0)
    lowest_low    = indicators.get("lowest_low_5c", 0.0)

    if current_price <= 0 or lowest_low <= 0:
        logger.warning(f"[{symbol}] L1 skipped: invalid price/lowest_low.")
        return

    # Hard stop = 1% below lowest low of last N candles (same logic as original bot)
    hard_stop = lowest_low * (1.0 - SL_BUFFER_PCT / 100.0)
    if hard_stop >= current_price:
        logger.warning(
            f"[{symbol}] L1 skipped: hard_stop ({hard_stop:.4f}) >= price ({current_price:.4f})"
        )
        return

    try:
        qty = calculate_dca_qty(balance, current_price, hard_stop, dca_signal["risk_pct"])
        qty = _apply_dca_precision(exchange, symbol, qty)
    except (ValueError, Exception) as exc:
        logger.error(f"[{symbol}] L1 qty calc failed: {exc}")
        return

    try:
        order = exchange.create_market_buy_order(symbol, qty)
    except Exception as exc:
        logger.error(f"[{symbol}] L1 buy order failed: {exc}")
        return

    time.sleep(2)
    entry_real = current_price
    qty_real   = qty
    try:
        filled     = exchange.fetch_order(order["id"], symbol)
        avg        = filled.get("average") or filled.get("price")
        filled_qty = filled.get("filled")
        if avg:
            entry_real = float(avg)
        if filled_qty:
            qty_real = float(filled_qty)
    except Exception as exc:
        logger.warning(f"[{symbol}] Could not fetch fill price: {exc}")

    # Build the DCA position
    level = DCALevel(
        level       = 1,
        entry_price = entry_real,
        quantity    = qty_real,
        cost        = entry_real * qty_real,
    )
    position.symbol          = symbol
    position.is_open         = True
    position.levels          = [level]
    position.initial_balance = balance
    position.hard_stop_price = hard_stop
    position.recalculate_avg()

    tp1, tp2 = calculate_tp_prices(position.avg_price, hard_stop)
    position.tp1_price = tp1
    position.tp2_price = tp2

    save_dca_state(position)

    _log_journal({
        "timestamp_utc"   : time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "symbol"          : symbol,
        "action"          : "OPEN_L1",
        "level"           : 1,
        "price"           : entry_real,
        "quantity"        : qty_real,
        "avg_price"       : position.avg_price,
        "total_qty"       : position.total_qty,
        "hard_stop"       : hard_stop,
        "tp1"             : tp1,
        "tp2"             : tp2,
        "account_balance" : balance,
        "reason"          : dca_signal["reason"],
    })

    logger.info(
        f"[{symbol}] L1 OPENED → entry={entry_real:.4f} | qty={qty_real:.6f} | "
        f"hard_stop={hard_stop:.4f} | TP1={tp1:.4f} | TP2={tp2:.4f}"
    )


def _execute_dca_add(
    exchange,
    position: DCAPosition,
    dca_signal: dict,
    current_price: float,
    balance: float,
    symbol: str,
    logger: logging.Logger,
) -> None:
    """Adds a new DCA level (L2 or L3) to an open position."""
    action     = dca_signal["action"]
    level_num  = int(action[-1])   # "add_l2" → 2, "add_l3" → 3
    hard_stop  = position.hard_stop_price   # hard stop never moves down

    try:
        qty = calculate_dca_qty(balance, current_price, hard_stop, dca_signal["risk_pct"])
        qty = _apply_dca_precision(exchange, symbol, qty)
    except (ValueError, Exception) as exc:
        logger.error(f"[{symbol}] {action} qty calc failed: {exc}")
        return

    try:
        order = exchange.create_market_buy_order(symbol, qty)
    except Exception as exc:
        logger.error(f"[{symbol}] {action} buy order failed: {exc}")
        return

    time.sleep(2)
    entry_real = current_price
    qty_real   = qty
    try:
        filled     = exchange.fetch_order(order["id"], symbol)
        avg        = filled.get("average") or filled.get("price")
        filled_qty = filled.get("filled")
        if avg:
            entry_real = float(avg)
        if filled_qty:
            qty_real = float(filled_qty)
    except Exception as exc:
        logger.warning(f"[{symbol}] Could not fetch fill price for {action}: {exc}")

    level = DCALevel(
        level       = level_num,
        entry_price = entry_real,
        quantity    = qty_real,
        cost        = entry_real * qty_real,
    )
    position.levels.append(level)
    position.recalculate_avg()

    # Recalculate TPs against the new (lower) avg price
    # This typically raises TP1/TP2 in absolute terms relative to new avg,
    # but the risk unit (avg - hard_stop) shrinks as avg falls, so TPs are conservative.
    tp1, tp2 = calculate_tp_prices(position.avg_price, position.hard_stop_price)
    position.tp1_price = tp1
    position.tp2_price = tp2

    save_dca_state(position)

    _log_journal({
        "timestamp_utc"   : time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "symbol"          : symbol,
        "action"          : action.upper(),
        "level"           : level_num,
        "price"           : entry_real,
        "quantity"        : qty_real,
        "avg_price"       : position.avg_price,
        "total_qty"       : position.total_qty,
        "hard_stop"       : position.hard_stop_price,
        "tp1"             : tp1,
        "tp2"             : tp2,
        "account_balance" : balance,
        "reason"          : dca_signal["reason"],
    })

    logger.info(
        f"[{symbol}] {action.upper()} → entry={entry_real:.4f} | qty={qty_real:.6f} | "
        f"new_avg={position.avg_price:.4f} | total_qty={position.total_qty:.6f} | "
        f"TP1={tp1:.4f} | TP2={tp2:.4f}"
    )


# ═════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BCH-TRADE Bot")
    parser.add_argument(
        "--mode",
        choices=["original", "dca"],
        default="original",
        help="'original' = legacy BCH/USDC strategy | 'dca' = Triple Screen DCA (Elder)",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=None,
        help="Symbols for DCA mode (e.g. BTC/USDT ETH/USDT). Defaults to SYMBOL from .env",
    )
    args = parser.parse_args()

    if args.mode == "dca":
        run_dca_bot(symbols=args.symbols)
    else:
        run_bot()
