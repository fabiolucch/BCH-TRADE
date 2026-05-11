"""
execution.py — Lógica de execução de ordens, dimensionamento de posição,
               trailing stop, monitoramento de saída e registro de histórico.

Fluxo ao detectar sinal de entrada:
  1. Busca saldo USDT disponível
  2. Calcula Stop Loss e Take Profit
  3. Dimensiona quantidade com base no risco máximo (RISK_PCT% do saldo)
  4. Envia ordem de compra a mercado
  5. Tenta posicionar ordens de saída (TP limit + SL algo na OKX)
  6. Se a API não suportar, ativa modo de monitoramento por software
  7. Persiste estado em JSON por par (state_BCH_USDT.json, etc.)

Trailing Stop:
  - Ativa quando lucro >= TRAILING_ACTIVATION_PCT
  - Trail SL = máxima_histórica × (1 − TRAILING_STOP_PCT / 100)
  - Ao ativar, cancela ordens abertas e migra para modo monitor
  - Atualiza o trail SL a cada nova máxima
"""

import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import ccxt

from config import (
    RISK_PCT,
    RR_RATIO,
    SL_BUFFER_PCT,
    TRAILING_ACTIVATION_PCT,
    TRAILING_STOP_PCT,
    state_file_for,
)
import telegram_notifier as tg
import trade_history as history

logger = logging.getLogger("bot")


# ═════════════════════════════════════════════════════════════
# GERENCIAMENTO DE ESTADO (JSON por par)
# ═════════════════════════════════════════════════════════════

_EMPTY_STATE: Dict[str, Any] = {
    "is_open"       : False,
    "entry_price"   : 0.0,
    "quantity"      : 0.0,
    "sl_price"      : 0.0,
    "tp_price"      : 0.0,
    "sl_order_id"   : None,
    "tp_order_id"   : None,
    "exit_mode"     : "monitor",
    "entry_time"    : None,
    "highest_price" : 0.0,
    "trail_active"  : False,
    "trail_sl"      : None,
}


def load_state(symbol: str) -> Dict[str, Any]:
    """Carrega o estado da posição do arquivo JSON do par; retorna vazio se não existir."""
    path = state_file_for(symbol)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"[{symbol}] Falha ao ler estado ({exc}). Usando estado vazio.")
    return dict(_EMPTY_STATE)


def save_state(state: Dict[str, Any], symbol: str) -> None:
    """Persiste o estado da posição no arquivo JSON do par."""
    path = state_file_for(symbol)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error(f"[{symbol}] Falha ao salvar estado: {exc}")


def reset_state(symbol: str) -> None:
    """Reseta para estado sem posição aberta."""
    save_state(dict(_EMPTY_STATE), symbol)
    logger.debug(f"[{symbol}] Estado resetado.")


# ═════════════════════════════════════════════════════════════
# CÁLCULOS DE RISCO
# ═════════════════════════════════════════════════════════════

def calculate_sl_tp(entry_price: float, lowest_low: float) -> Tuple[float, float]:
    """
    Calcula Stop Loss e Take Profit com base nas regras da estratégia.

    SL  : 1% abaixo da mínima dos últimos N candles de 4H
    TP  : entrada + (risco_por_unidade × RR_RATIO)
    """
    sl_price      = lowest_low * (1.0 - SL_BUFFER_PCT / 100.0)
    risk_per_unit = entry_price - sl_price

    if risk_per_unit <= 0:
        raise ValueError(
            f"SL ({sl_price:.6f}) ≥ Entry ({entry_price:.6f}). "
            f"Verifique a mínima dos candles e SL_BUFFER_PCT."
        )

    tp_price = entry_price + (risk_per_unit * RR_RATIO)
    logger.debug(
        f"SL/TP → Entry={entry_price:.4f} | SL={sl_price:.4f} | "
        f"Risco/unit={risk_per_unit:.4f} | TP={tp_price:.4f} (R/R 1:{RR_RATIO})"
    )
    return sl_price, tp_price


def calculate_position_size(
    balance_usdt: float,
    entry_price: float,
    sl_price: float,
) -> float:
    """
    Calcula a quantidade a comprar com base no risco máximo por operação.

    risco_max = balance × (RISK_PCT / 100)
    quantidade = risco_max / (entry − sl)
    """
    max_risk      = balance_usdt * (RISK_PCT / 100.0)
    risk_per_unit = entry_price - sl_price

    if risk_per_unit <= 0:
        raise ValueError("Risco por unidade inválido: entry ≤ SL.")

    quantity = max_risk / risk_per_unit
    logger.info(
        f"[DIMENSIONAMENTO] Saldo={balance_usdt:.2f} USDT | "
        f"Risco máx={max_risk:.2f} USDT ({RISK_PCT}%) | "
        f"Entry={entry_price:.4f} | SL={sl_price:.4f} | → Qty={quantity:.6f}"
    )
    return quantity


# ═════════════════════════════════════════════════════════════
# CONSULTAS À EXCHANGE
# ═════════════════════════════════════════════════════════════

def get_usdc_balance(exchange: ccxt.Exchange) -> float:
    """Retorna o saldo livre de USDT na conta spot."""
    try:
        balance = exchange.fetch_balance()
        usdt    = float(balance.get("USDT", {}).get("free", 0.0))
        logger.info(f"[SALDO] USDT disponível: {usdt:.2f}")
        return usdt
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"Erro ao buscar saldo USDT: {exc}")
        raise


def get_current_price(exchange: ccxt.Exchange, symbol: str) -> float:
    """Retorna o último preço negociado do par."""
    try:
        ticker = exchange.fetch_ticker(symbol)
        return float(ticker["last"])
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"[{symbol}] Erro ao buscar preço: {exc}")
        raise


def _apply_market_precision(
    exchange: ccxt.Exchange,
    quantity: float,
    symbol: str,
) -> float:
    """Aplica a precisão mínima de quantidade exigida pela exchange para o par."""
    try:
        market    = exchange.market(symbol)
        precision = market.get("precision", {}).get("amount", None)
        min_qty   = market.get("limits", {}).get("amount", {}).get("min", 0.0)

        if precision is not None:
            if isinstance(precision, int):
                factor   = 10 ** precision
                quantity = math.floor(quantity * factor) / factor
            elif isinstance(precision, float) and precision > 0:
                factor   = 1.0 / precision
                quantity = math.floor(quantity * factor) / factor

        if min_qty and quantity < min_qty:
            raise ValueError(
                f"Quantidade calculada ({quantity:.8f}) menor que o mínimo "
                f"permitido ({min_qty}) para {symbol}."
            )
        return quantity
    except Exception as exc:
        logger.warning(f"[{symbol}] Precisão não aplicada: {exc}. Usando valor bruto.")
        return quantity


# ═════════════════════════════════════════════════════════════
# ENVIO DE ORDENS
# ═════════════════════════════════════════════════════════════

def place_market_buy(
    exchange: ccxt.Exchange,
    quantity: float,
    symbol: str,
) -> Optional[Dict]:
    """Envia ordem de compra a mercado e retorna o objeto da ordem."""
    try:
        logger.info(f"[{symbol}] [ORDEM] Enviando COMPRA a mercado: {quantity:.6f}")
        order = exchange.create_market_buy_order(symbol, quantity)
        logger.info(
            f"[{symbol}] [ORDEM] Compra → ID={order.get('id')} | "
            f"Status={order.get('status')} | Qty={order.get('amount')}"
        )
        return order
    except ccxt.InsufficientFunds as exc:
        logger.error(f"[{symbol}] Saldo insuficiente: {exc}")
        return None
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"[{symbol}] Erro da exchange ao comprar: {exc}")
        return None
    except Exception as exc:
        logger.error(f"[{symbol}] Erro inesperado ao comprar: {exc}")
        return None


def _place_okx_algo_sl(
    exchange: ccxt.Exchange,
    quantity: float,
    sl_price: float,
    symbol: str,
) -> Optional[Dict]:
    """Cria ordem de Stop Loss condicional (algo order) na OKX."""
    params = {
        "ordType"        : "conditional",
        "slTriggerPx"    : str(sl_price),
        "slOrdPx"        : "-1",
        "slTriggerPxType": "last",
    }
    return exchange.create_order(symbol, "market", "sell", quantity, params=params)


def place_exit_orders(
    exchange: ccxt.Exchange,
    quantity: float,
    tp_price: float,
    sl_price: float,
    symbol: str,
) -> Dict[str, Any]:
    """
    Posiciona ordens de saída após abertura da posição.

    Tenta: TP limit + SL algo (OKX). Fallback: modo monitor por software.
    """
    result: Dict[str, Any] = {"exit_mode": "monitor", "tp_order_id": None, "sl_order_id": None}

    tp_order_id = None
    try:
        tp_order    = exchange.create_limit_sell_order(symbol, quantity, tp_price)
        tp_order_id = tp_order.get("id")
        logger.info(f"[{symbol}] [SAÍDA] TP limit → ID={tp_order_id} | Preço={tp_price:.4f}")
    except Exception as exc:
        logger.warning(f"[{symbol}] Falha ao criar ordem TP: {exc}")

    sl_order_id = None
    if tp_order_id:
        try:
            if exchange.id == "okx":
                sl_order    = _place_okx_algo_sl(exchange, quantity, sl_price, symbol)
                sl_order_id = sl_order.get("id")
                logger.info(f"[{symbol}] [SAÍDA] SL algo → ID={sl_order_id} | Trigger={sl_price:.4f}")
            else:
                sl_order    = exchange.create_stop_market_order(symbol, "sell", quantity, sl_price)
                sl_order_id = sl_order.get("id")
                logger.info(f"[{symbol}] [SAÍDA] SL stop market → ID={sl_order_id}")
        except Exception as exc:
            logger.warning(f"[{symbol}] Falha ao criar ordem SL: {exc}")
            try:
                exchange.cancel_order(tp_order_id, symbol)
                logger.info(f"[{symbol}] Ordem TP cancelada (rollback — SL falhou).")
            except Exception:
                pass
            tp_order_id = None

    if tp_order_id and sl_order_id:
        result["exit_mode"]   = "orders"
        result["tp_order_id"] = tp_order_id
        result["sl_order_id"] = sl_order_id
        logger.info(f"[{symbol}] Modo saída: ORDENS (TP limit + SL algo)")
    else:
        logger.info(f"[{symbol}] Modo saída: MONITOR por software")

    return result


def close_position_market(
    exchange: ccxt.Exchange,
    quantity: float,
    reason: str,
    symbol: str,
) -> bool:
    """Fecha a posição com ordem de venda a mercado."""
    try:
        logger.info(f"[{symbol}] [SAÍDA] Fechando a mercado. Motivo: {reason}")
        order = exchange.create_market_sell_order(symbol, quantity)
        logger.info(
            f"[{symbol}] [SAÍDA] Venda → ID={order.get('id')} | Status={order.get('status')}"
        )
        return True
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"[{symbol}] Erro ao fechar posição: {exc}")
        return False
    except Exception as exc:
        logger.error(f"[{symbol}] Erro inesperado ao fechar: {exc}")
        return False


def _cancel_exit_orders(exchange: ccxt.Exchange, state: Dict, symbol: str) -> None:
    """Cancela ordens de TP e SL abertas."""
    for key in ("tp_order_id", "sl_order_id"):
        order_id = state.get(key)
        if order_id:
            try:
                exchange.cancel_order(order_id, symbol)
                logger.info(f"[{symbol}] Ordem {key} cancelada: {order_id}")
            except Exception as exc:
                logger.warning(f"[{symbol}] Não cancelou {key} ({order_id}): {exc}")


# ═════════════════════════════════════════════════════════════
# TRAILING STOP
# ═════════════════════════════════════════════════════════════

def _update_trailing_stop(
    exchange: ccxt.Exchange,
    state: Dict,
    price: float,
    symbol: str,
) -> bool:
    """
    Atualiza a lógica de trailing stop para a posição aberta.

    Retorna True se o trailing SL foi atingido (posição deve ser fechada).
    """
    entry = state["entry_price"]

    # Atualiza máxima histórica
    if price > state.get("highest_price", entry):
        state["highest_price"] = price
        save_state(state, symbol)

    highest    = state["highest_price"]
    profit_pct = ((highest - entry) / entry) * 100.0

    # Ativa trailing quando lucro >= TRAILING_ACTIVATION_PCT
    if not state.get("trail_active") and profit_pct >= TRAILING_ACTIVATION_PCT:
        trail_sl = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
        state["trail_active"] = True
        state["trail_sl"]     = trail_sl

        # Cancela ordens abertas e migra para modo monitor
        if state.get("exit_mode") == "orders":
            _cancel_exit_orders(exchange, state, symbol)
            state["exit_mode"]   = "monitor"
            state["sl_order_id"] = None
            state["tp_order_id"] = None

        logger.info(
            f"[{symbol}] [TRAILING] ★ Ativado! "
            f"Máxima={highest:.4f} | Trail SL={trail_sl:.4f} | Lucro={profit_pct:.2f}%"
        )
        tg.notify_trailing_activated(symbol, highest, trail_sl)
        save_state(state, symbol)

    # Atualiza trail SL se preço fez nova máxima
    if state.get("trail_active"):
        new_trail_sl = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
        if new_trail_sl > state.get("trail_sl", 0.0):
            state["trail_sl"] = new_trail_sl
            save_state(state, symbol)
            logger.info(f"[{symbol}] [TRAILING] SL atualizado → {new_trail_sl:.4f}")
            tg.notify_trailing_updated(symbol, new_trail_sl)

        # Verifica se preço atingiu o trailing SL
        if price <= state["trail_sl"]:
            logger.info(
                f"[{symbol}] [TRAILING] ✗ Trail SL atingido! "
                f"Preço={price:.4f} ≤ Trail SL={state['trail_sl']:.4f}"
            )
            return True

    return False


# ═════════════════════════════════════════════════════════════
# MONITORAMENTO DE POSIÇÃO ABERTA
# ═════════════════════════════════════════════════════════════

def check_open_position(exchange: ccxt.Exchange, state: Dict, symbol: str) -> bool:
    """
    Verifica se a posição aberta deve ser encerrada (Trailing SL, SL ou TP).

    Retorna True se ainda aberta, False se foi fechada neste ciclo.
    """
    if not state.get("is_open"):
        return False

    entry    = state["entry_price"]
    tp_price = state["tp_price"]
    sl_price = state["sl_price"]
    quantity = state["quantity"]

    # ── Modo monitoramento por software ──────────────────────────────────────
    if state.get("exit_mode") == "monitor":
        try:
            price   = get_current_price(exchange, symbol)
            pnl_pct = ((price - entry) / entry) * 100.0

            trail_info = ""
            if state.get("trail_active"):
                trail_info = f" | Trail SL={state['trail_sl']:.4f}"

            logger.info(
                f"[{symbol}] [POSIÇÃO] Entry={entry:.4f} | Atual={price:.4f} | "
                f"SL={sl_price:.4f} | TP={tp_price:.4f} | "
                f"PnL={pnl_pct:+.2f}%{trail_info}"
            )

            # Trailing stop tem prioridade
            trail_hit = _update_trailing_stop(exchange, state, price, symbol)
            if trail_hit:
                if close_position_market(exchange, quantity, "Trailing Stop", symbol):
                    metrics = history.record_trade(
                        symbol, entry, price, quantity,
                        sl_price, tp_price, "Trailing Stop", state.get("entry_time"),
                    )
                    tg.notify_position_closed(
                        symbol, "Trailing Stop", entry, price,
                        metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"],
                    )
                    reset_state(symbol)
                    return False

            # Take Profit
            if price >= tp_price:
                logger.info(
                    f"[{symbol}] [SAÍDA] ★ TAKE PROFIT ★ Preço={price:.4f} ≥ TP={tp_price:.4f} "
                    f"| Ganho≈{pnl_pct:+.2f}%"
                )
                if close_position_market(exchange, quantity, "Take Profit", symbol):
                    metrics = history.record_trade(
                        symbol, entry, price, quantity,
                        sl_price, tp_price, "Take Profit", state.get("entry_time"),
                    )
                    tg.notify_position_closed(
                        symbol, "Take Profit", entry, price,
                        metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"],
                    )
                    reset_state(symbol)
                    return False

            # Stop Loss
            elif price <= sl_price:
                logger.warning(
                    f"[{symbol}] [SAÍDA] ✗ STOP LOSS ✗ Preço={price:.4f} ≤ SL={sl_price:.4f} "
                    f"| Perda≈{pnl_pct:+.2f}%"
                )
                if close_position_market(exchange, quantity, "Stop Loss", symbol):
                    metrics = history.record_trade(
                        symbol, entry, price, quantity,
                        sl_price, tp_price, "Stop Loss", state.get("entry_time"),
                    )
                    tg.notify_position_closed(
                        symbol, "Stop Loss", entry, price,
                        metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"],
                    )
                    reset_state(symbol)
                    return False

        except Exception as exc:
            logger.error(f"[{symbol}] Erro ao monitorar (modo software): {exc}")

    # ── Modo com ordens abertas na exchange ───────────────────────────────────
    else:
        try:
            price   = get_current_price(exchange, symbol)
            pnl_pct = ((price - entry) / entry) * 100.0

            logger.info(
                f"[{symbol}] [POSIÇÃO] Entry={entry:.4f} | Atual={price:.4f} | "
                f"SL={sl_price:.4f} | TP={tp_price:.4f} | PnL={pnl_pct:+.2f}%"
            )

            # Verifica trailing (pode migrar para modo monitor)
            _update_trailing_stop(exchange, state, price, symbol)

            # Se trailing mudou o modo, recarrega estado e sai
            if state.get("exit_mode") == "monitor":
                return True

            for order_key, other_key in (
                ("tp_order_id", "sl_order_id"),
                ("sl_order_id", "tp_order_id"),
            ):
                order_id = state.get(order_key)
                if not order_id:
                    continue

                order  = exchange.fetch_order(order_id, symbol)
                status = order.get("status", "")

                if status == "closed":
                    label      = "Take Profit" if order_key == "tp_order_id" else "Stop Loss"
                    exit_price = float(order.get("average") or order.get("price") or price)
                    logger.info(f"[{symbol}] [SAÍDA] ★ {label.upper()} ★ Ordem executada.")

                    other_id = state.get(other_key)
                    if other_id:
                        try:
                            exchange.cancel_order(other_id, symbol)
                        except Exception:
                            pass

                    metrics = history.record_trade(
                        symbol, entry, exit_price, quantity,
                        sl_price, tp_price, label, state.get("entry_time"),
                    )
                    tg.notify_position_closed(
                        symbol, label, entry, exit_price,
                        metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"],
                    )
                    reset_state(symbol)
                    return False

                elif status == "canceled":
                    logger.warning(
                        f"[{symbol}] Ordem {order_key} cancelada externamente. "
                        f"Migrando para modo monitor."
                    )
                    _cancel_exit_orders(exchange, state, symbol)
                    state["exit_mode"]   = "monitor"
                    state["tp_order_id"] = None
                    state["sl_order_id"] = None
                    save_state(state, symbol)
                    break

        except Exception as exc:
            logger.error(f"[{symbol}] Erro ao verificar ordens de saída: {exc}")

    return True


# ═════════════════════════════════════════════════════════════
# FLUXO COMPLETO DE ABERTURA DE POSIÇÃO
# ═════════════════════════════════════════════════════════════

def open_position(exchange: ccxt.Exchange, indicators: Dict, symbol: str) -> bool:
    """
    Executa o fluxo completo de abertura de uma nova posição para o par informado.

    1. Verifica saldo mínimo
    2. Calcula SL e TP
    3. Dimensiona quantidade com base no risco
    4. Aplica precisão do mercado
    5. Envia ordem de compra a mercado
    6. Obtém preço real de execução
    7. Posiciona ordens de saída (TP + SL)
    8. Persiste estado e envia notificação Telegram
    """
    try:
        # ── 1. Saldo disponível ───────────────────────────────────────────────
        balance = get_usdc_balance(exchange)
        if balance < 10.0:
            logger.warning(
                f"[{symbol}] Saldo insuficiente: {balance:.2f} USDT (mínimo: 10 USDT)."
            )
            return False

        # ── 2. SL / TP ────────────────────────────────────────────────────────
        entry_est  = indicators["close_4h"]
        lowest_low = indicators["lowest_low_5c"]
        sl_price, tp_price = calculate_sl_tp(entry_est, lowest_low)

        logger.info(
            f"[{symbol}] [ENTRADA] Entry≈{entry_est:.4f} | "
            f"SL={sl_price:.4f} | TP={tp_price:.4f} | R/R=1:{RR_RATIO}"
        )

        # ── 3. Dimensionamento ────────────────────────────────────────────────
        quantity = calculate_position_size(balance, entry_est, sl_price)

        # ── 4. Precisão do mercado ────────────────────────────────────────────
        quantity = _apply_market_precision(exchange, quantity, symbol)
        logger.info(f"[{symbol}] [ENTRADA] Qty ajustada (precisão): {quantity:.6f}")

        # ── 5. Ordem de compra ────────────────────────────────────────────────
        buy_order = place_market_buy(exchange, quantity, symbol)
        if not buy_order:
            return False

        time.sleep(2)

        # ── 6. Preço real de execução ─────────────────────────────────────────
        entry_real = entry_est
        qty_real   = quantity
        try:
            filled     = exchange.fetch_order(buy_order["id"], symbol)
            avg        = filled.get("average") or filled.get("price")
            filled_qty = filled.get("filled")
            if avg:
                entry_real = float(avg)
            if filled_qty:
                qty_real = float(filled_qty)
            if entry_real != entry_est:
                sl_price, tp_price = calculate_sl_tp(entry_real, lowest_low)
                logger.info(
                    f"[{symbol}] Preço real: {entry_real:.4f} | "
                    f"SL={sl_price:.4f} | TP={tp_price:.4f}"
                )
        except Exception as exc:
            logger.warning(f"[{symbol}] Preço real não obtido: {exc}. Usando estimativa.")

        # ── 7. Ordens de saída ────────────────────────────────────────────────
        exit_info  = place_exit_orders(exchange, qty_real, tp_price, sl_price, symbol)
        entry_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # ── 8. Persiste estado ────────────────────────────────────────────────
        state = {
            "is_open"       : True,
            "entry_price"   : entry_real,
            "quantity"      : qty_real,
            "sl_price"      : sl_price,
            "tp_price"      : tp_price,
            "tp_order_id"   : exit_info.get("tp_order_id"),
            "sl_order_id"   : exit_info.get("sl_order_id"),
            "exit_mode"     : exit_info.get("exit_mode", "monitor"),
            "entry_time"    : entry_time,
            "highest_price" : entry_real,
            "trail_active"  : False,
            "trail_sl"      : None,
        }
        save_state(state, symbol)

        tg.notify_position_opened(symbol, entry_real, sl_price, tp_price, qty_real)

        logger.info(
            f"[{symbol}] [POSIÇÃO ABERTA] ✓ Entry={entry_real:.4f} | "
            f"Qty={qty_real:.6f} | SL={sl_price:.4f} | TP={tp_price:.4f} | "
            f"Modo saída: {exit_info['exit_mode'].upper()}"
        )
        return True

    except ValueError as exc:
        logger.error(f"[{symbol}] Parâmetros inválidos: {exc}")
        return False
    except Exception as exc:
        logger.error(f"[{symbol}] Erro inesperado ao abrir posição: {exc}", exc_info=True)
        return False
