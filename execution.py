"""
execution.py — Lógica de execução de ordens, dimensionamento de posição e
               monitoramento de saída (Stop Loss / Take Profit).

Fluxo ao detectar sinal de entrada:
  1. Busca saldo USDT disponível
  2. Calcula Stop Loss e Take Profit
  3. Dimensiona a quantidade de BCH com base no risco máximo (1.5% do saldo)
  4. Envia ordem de compra a mercado
  5. Tenta posicionar ordens de saída via API da OKX (TP limit + SL algo)
  6. Se a API não suportar, ativa modo de monitoramento por software
  7. Persiste o estado da posição em JSON para sobreviver a reinicializações

Monitoramento (quando modo='monitor'):
  - A cada ciclo verifica o preço atual
  - Fecha a mercado se atingir SL ou TP matematicamente
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import ccxt

from config import (
    RISK_PCT,
    RR_RATIO,
    SL_BUFFER_PCT,
    STATE_FILE,
    SYMBOL,
)

logger = logging.getLogger("bot")


# ═════════════════════════════════════════════════════════════
# GERENCIAMENTO DE ESTADO (persistência em JSON)
# ═════════════════════════════════════════════════════════════

_EMPTY_STATE: Dict[str, Any] = {
    "is_open"     : False,
    "entry_price" : 0.0,
    "quantity"    : 0.0,
    "sl_price"    : 0.0,
    "tp_price"    : 0.0,
    "sl_order_id" : None,
    "tp_order_id" : None,
    "exit_mode"   : "monitor",  # "orders" | "monitor"
}


def load_state() -> Dict[str, Any]:
    """Carrega o estado da posição do arquivo JSON; retorna estado vazio se não existir."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"Falha ao ler arquivo de estado ({exc}). Usando estado vazio.")
    return dict(_EMPTY_STATE)


def save_state(state: Dict[str, Any]) -> None:
    """Persiste o estado da posição no arquivo JSON."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error(f"Falha ao salvar arquivo de estado: {exc}")


def reset_state() -> None:
    """Reseta para estado sem posição aberta e apaga o arquivo de estado."""
    save_state(dict(_EMPTY_STATE))
    logger.debug("Estado da posição resetado.")


# ═════════════════════════════════════════════════════════════
# CÁLCULOS DE RISCO
# ═════════════════════════════════════════════════════════════

def calculate_sl_tp(entry_price: float, lowest_low: float) -> Tuple[float, float]:
    """
    Calcula Stop Loss e Take Profit com base nas regras da estratégia.

    Stop Loss  : 1% abaixo da mínima dos últimos 5 candles de 4H
    Take Profit: entrada + (risco_por_unidade × RR_RATIO)

    Lança ValueError se o SL calculado for maior ou igual ao preço de entrada.
    """
    sl_price      = lowest_low * (1.0 - SL_BUFFER_PCT / 100.0)
    risk_per_unit = entry_price - sl_price

    if risk_per_unit <= 0:
        raise ValueError(
            f"SL ({sl_price:.6f}) ≥ Entry ({entry_price:.6f}). "
            f"Verifique a mínima dos últimos candles e o SL_BUFFER_PCT."
        )

    tp_price = entry_price + (risk_per_unit * RR_RATIO)

    logger.debug(
        f"SL/TP calculados → Entry={entry_price:.4f} | "
        f"Lowest_low={lowest_low:.4f} | SL={sl_price:.4f} | "
        f"Risco/unidade={risk_per_unit:.4f} | TP={tp_price:.4f} (R/R 1:{RR_RATIO})"
    )
    return sl_price, tp_price


def calculate_position_size(
    balance_usdc: float,
    entry_price: float,
    sl_price: float,
) -> float:
    """
    Calcula a quantidade de BCH a comprar com base no risco máximo por operação.

    Fórmula:
        risco_max_usdc = balance × (RISK_PCT / 100)
        quantidade     = risco_max_usdc / (entry - sl)

    Garante que o pior cenário (SL ativado) limite a perda a RISK_PCT% do saldo.
    """
    max_risk_usdc = balance_usdc * (RISK_PCT / 100.0)
    risk_per_unit = entry_price - sl_price

    if risk_per_unit <= 0:
        raise ValueError("Risco por unidade inválido: entry ≤ SL.")

    quantity = max_risk_usdc / risk_per_unit

    logger.info(
        f"[DIMENSIONAMENTO] Saldo={balance_usdc:.2f} USDT | "
        f"Risco máx={max_risk_usdc:.2f} USDT ({RISK_PCT}%) | "
        f"Entry={entry_price:.4f} | SL={sl_price:.4f} | "
        f"Risco/unit={risk_per_unit:.6f} | → Qty={quantity:.6f} BCH"
    )
    return quantity


# ═════════════════════════════════════════════════════════════
# CONSULTAS À EXCHANGE
# ═════════════════════════════════════════════════════════════

def get_usdc_balance(exchange: ccxt.Exchange) -> float:
    """Retorna o saldo livre de USDT na conta spot."""
    try:
        balance = exchange.fetch_balance()
        usdc    = float(balance.get("USDT", {}).get("free", 0.0))
        logger.info(f"[SALDO] USDT disponível: {usdc:.2f}")
        return usdc
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"Erro ao buscar saldo USDT: {exc}")
        raise


def get_current_price(exchange: ccxt.Exchange) -> float:
    """Retorna o último preço negociado do par."""
    try:
        ticker = exchange.fetch_ticker(SYMBOL)
        return float(ticker["last"])
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"Erro ao buscar preço atual: {exc}")
        raise


def _apply_market_precision(
    exchange: ccxt.Exchange,
    quantity: float,
) -> float:
    """
    Aplica a precisão mínima de quantidade exigida pela exchange para o par.
    Arredonda para baixo para evitar rejeição por excesso de casas decimais.
    """
    try:
        market    = exchange.market(SYMBOL)
        precision = market.get("precision", {}).get("amount", None)
        min_qty   = market.get("limits", {}).get("amount", {}).get("min", 0.0)

        if precision is not None:
            # ccxt pode retornar precisão como int (casas decimais) ou float (tick size)
            if isinstance(precision, int):
                import math
                factor   = 10 ** precision
                quantity = math.floor(quantity * factor) / factor
            elif isinstance(precision, float) and precision > 0:
                import math
                factor   = 1.0 / precision
                quantity = math.floor(quantity * factor) / factor

        if min_qty and quantity < min_qty:
            raise ValueError(
                f"Quantidade calculada ({quantity:.8f}) menor que o mínimo "
                f"permitido ({min_qty}) para {SYMBOL}."
            )

        return quantity
    except Exception as exc:
        logger.warning(f"Não foi possível aplicar precisão do mercado: {exc}. Usando valor bruto.")
        return quantity


# ═════════════════════════════════════════════════════════════
# ENVIO DE ORDENS
# ═════════════════════════════════════════════════════════════

def place_market_buy(exchange: ccxt.Exchange, quantity: float) -> Optional[Dict]:
    """Envia ordem de compra a mercado e retorna o objeto da ordem."""
    try:
        logger.info(f"[ORDEM] Enviando COMPRA a mercado: {quantity:.6f} {SYMBOL}")
        order = exchange.create_market_buy_order(SYMBOL, quantity)
        logger.info(
            f"[ORDEM] Compra enviada → ID={order.get('id')} | "
            f"Status={order.get('status')} | "
            f"Qty={order.get('amount')} | Preço≈{order.get('price') or 'mercado'}"
        )
        return order
    except ccxt.InsufficientFunds as exc:
        logger.error(f"Saldo insuficiente para compra: {exc}")
        return None
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"Erro da exchange ao enviar compra: {exc}")
        return None
    except Exception as exc:
        logger.error(f"Erro inesperado ao enviar compra: {exc}")
        return None


def _place_okx_algo_sl(
    exchange: ccxt.Exchange,
    quantity: float,
    sl_price: float,
) -> Optional[Dict]:
    """
    Tenta criar ordem de Stop Loss condicional (algo order) na OKX.
    Retorna o objeto da ordem ou None se falhar.
    """
    params = {
        "ordType"      : "conditional",
        "slTriggerPx"  : str(sl_price),
        "slOrdPx"      : "-1",         # -1 = executar a mercado ao atingir trigger
        "slTriggerPxType": "last",
    }
    return exchange.create_order(SYMBOL, "market", "sell", quantity, params=params)


def place_exit_orders(
    exchange: ccxt.Exchange,
    quantity: float,
    tp_price: float,
    sl_price: float,
) -> Dict[str, Any]:
    """
    Posiciona ordens de saída logo após a abertura da posição.

    Estratégia em duas tentativas:
    1. Ordem de TP como limit sell + SL como algo order condicional (OKX)
    2. Fallback: monitoramento por software (loop no main.py)

    Retorna dicionário com 'exit_mode', 'tp_order_id' e 'sl_order_id'.
    """
    result: Dict[str, Any] = {
        "exit_mode"   : "monitor",
        "tp_order_id" : None,
        "sl_order_id" : None,
    }

    # ── Tentativa 1: TP como limit sell ──────────────────────────────────────
    tp_order_id = None
    try:
        tp_order    = exchange.create_limit_sell_order(SYMBOL, quantity, tp_price)
        tp_order_id = tp_order.get("id")
        logger.info(
            f"[SAÍDA] Ordem TP (limit sell) criada → "
            f"ID={tp_order_id} | Preço={tp_price:.4f}"
        )
    except Exception as exc:
        logger.warning(f"[SAÍDA] Falha ao criar ordem TP limit: {exc}")

    # ── Tentativa 2: SL como algo order (OKX) ────────────────────────────────
    sl_order_id = None
    if tp_order_id:  # só tenta SL se TP foi criado com sucesso
        try:
            if exchange.id == "okx":
                sl_order    = _place_okx_algo_sl(exchange, quantity, sl_price)
                sl_order_id = sl_order.get("id")
                logger.info(
                    f"[SAÍDA] Ordem SL (algo condicional OKX) criada → "
                    f"ID={sl_order_id} | Trigger={sl_price:.4f}"
                )
            else:
                sl_order    = exchange.create_stop_market_order(SYMBOL, "sell", quantity, sl_price)
                sl_order_id = sl_order.get("id")
                logger.info(
                    f"[SAÍDA] Ordem SL (stop market) criada → "
                    f"ID={sl_order_id} | Trigger={sl_price:.4f}"
                )
        except Exception as exc:
            logger.warning(f"[SAÍDA] Falha ao criar ordem SL: {exc}")
            # Cancela o TP para não ficar uma perna órfã
            try:
                exchange.cancel_order(tp_order_id, SYMBOL)
                logger.info("[SAÍDA] Ordem TP cancelada (rollback — SL falhou).")
            except Exception:
                pass
            tp_order_id = None

    if tp_order_id and sl_order_id:
        result["exit_mode"]    = "orders"
        result["tp_order_id"]  = tp_order_id
        result["sl_order_id"]  = sl_order_id
        logger.info("[SAÍDA] Modo de saída: ORDENS (TP limit + SL algo)")
    else:
        logger.info(
            "[SAÍDA] Modo de saída: MONITORAMENTO por software "
            "(loop verifica SL/TP a cada ciclo)"
        )

    return result


def close_position_market(
    exchange: ccxt.Exchange,
    quantity: float,
    reason: str,
) -> bool:
    """Fecha a posição integralmente com ordem de venda a mercado."""
    try:
        logger.info(f"[SAÍDA] Fechando posição a mercado. Motivo: {reason}")
        order = exchange.create_market_sell_order(SYMBOL, quantity)
        logger.info(
            f"[SAÍDA] Venda executada → ID={order.get('id')} | "
            f"Status={order.get('status')}"
        )
        return True
    except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
        logger.error(f"Erro da exchange ao fechar posição: {exc}")
        return False
    except Exception as exc:
        logger.error(f"Erro inesperado ao fechar posição: {exc}")
        return False


def _cancel_exit_orders(exchange: ccxt.Exchange, state: Dict) -> None:
    """Tenta cancelar ordens de TP e SL abertas (chamado antes de fechar a mercado)."""
    for key in ("tp_order_id", "sl_order_id"):
        order_id = state.get(key)
        if order_id:
            try:
                exchange.cancel_order(order_id, SYMBOL)
                logger.info(f"[ORDENS] Ordem {key} cancelada: {order_id}")
            except Exception as exc:
                logger.warning(f"Não foi possível cancelar {key} ({order_id}): {exc}")


# ═════════════════════════════════════════════════════════════
# MONITORAMENTO DE POSIÇÃO ABERTA
# ═════════════════════════════════════════════════════════════

def check_open_position(exchange: ccxt.Exchange, state: Dict) -> bool:
    """
    Verifica se a posição aberta deve ser encerrada (SL ou TP atingido).

    Retorna:
        True  → posição ainda aberta (continua monitorando)
        False → posição foi fechada neste ciclo

    Suporta dois modos definidos em state['exit_mode']:
    - "monitor" : verifica preço atual e fecha a mercado se SL/TP atingido
    - "orders"  : verifica status das ordens de TP/SL na exchange
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
            price   = get_current_price(exchange)
            pnl_pct = ((price - entry) / entry) * 100.0

            logger.info(
                f"[POSIÇÃO] Entry={entry:.4f} | Atual={price:.4f} | "
                f"SL={sl_price:.4f} | TP={tp_price:.4f} | "
                f"PnL não realizado={pnl_pct:+.2f}%"
            )

            if price >= tp_price:
                logger.info(
                    f"[SAÍDA] ★ TAKE PROFIT ★ Preço={price:.4f} ≥ TP={tp_price:.4f} "
                    f"| Ganho≈{pnl_pct:+.2f}%"
                )
                if close_position_market(exchange, quantity, "Take Profit atingido"):
                    reset_state()
                    return False

            elif price <= sl_price:
                logger.warning(
                    f"[SAÍDA] ✗ STOP LOSS ✗ Preço={price:.4f} ≤ SL={sl_price:.4f} "
                    f"| Perda≈{pnl_pct:+.2f}%"
                )
                if close_position_market(exchange, quantity, "Stop Loss atingido"):
                    reset_state()
                    return False

        except Exception as exc:
            logger.error(f"Erro ao monitorar posição (modo software): {exc}")

    # ── Modo com ordens abertas na exchange ───────────────────────────────────
    else:
        try:
            price   = get_current_price(exchange)
            pnl_pct = ((price - entry) / entry) * 100.0

            logger.info(
                f"[POSIÇÃO] Entry={entry:.4f} | Atual={price:.4f} | "
                f"SL={sl_price:.4f} | TP={tp_price:.4f} | "
                f"PnL não realizado={pnl_pct:+.2f}%"
            )

            for order_key, other_key in (
                ("tp_order_id", "sl_order_id"),
                ("sl_order_id", "tp_order_id"),
            ):
                order_id = state.get(order_key)
                if not order_id:
                    continue

                order  = exchange.fetch_order(order_id, SYMBOL)
                status = order.get("status", "")

                if status == "closed":
                    label = "TAKE PROFIT" if order_key == "tp_order_id" else "STOP LOSS"
                    logger.info(
                        f"[SAÍDA] ★ {label} ★ Ordem {order_id} executada. "
                        f"Cancelando ordem complementar."
                    )
                    # Cancela a outra perna
                    other_id = state.get(other_key)
                    if other_id:
                        try:
                            exchange.cancel_order(other_id, SYMBOL)
                        except Exception:
                            pass
                    reset_state()
                    return False

                elif status == "canceled":
                    logger.warning(
                        f"[SAÍDA] Ordem {order_key} ({order_id}) foi cancelada externamente. "
                        f"Migrando para modo de monitoramento por software."
                    )
                    _cancel_exit_orders(exchange, state)
                    state["exit_mode"]    = "monitor"
                    state["tp_order_id"]  = None
                    state["sl_order_id"]  = None
                    save_state(state)
                    break

        except Exception as exc:
            logger.error(f"Erro ao verificar ordens de saída: {exc}")

    return True  # posição ainda aberta


# ═════════════════════════════════════════════════════════════
# FLUXO COMPLETO DE ABERTURA DE POSIÇÃO
# ═════════════════════════════════════════════════════════════

def open_position(exchange: ccxt.Exchange, indicators: Dict) -> bool:
    """
    Executa o fluxo completo de abertura de uma nova posição:

    1. Verifica saldo mínimo
    2. Calcula SL e TP
    3. Dimensiona quantidade com base no risco máximo
    4. Aplica precisão do mercado
    5. Envia ordem de compra a mercado
    6. Obtém preço real de execução (recalcula SL/TP se necessário)
    7. Posiciona ordens de saída (TP + SL)
    8. Persiste estado em JSON

    Retorna True se a posição foi aberta com sucesso, False caso contrário.
    """
    try:
        # ── 1. Saldo disponível ───────────────────────────────────────────────
        balance = get_usdc_balance(exchange)
        if balance < 10.0:
            logger.warning(
                f"[ENTRADA] Saldo insuficiente para operar: {balance:.2f} USDT "
                f"(mínimo recomendado: 10 USDT)."
            )
            return False

        # ── 2. Estimativa inicial de SL/TP ────────────────────────────────────
        entry_est  = indicators["close_4h"]
        lowest_low = indicators["lowest_low_5c"]
        sl_price, tp_price = calculate_sl_tp(entry_est, lowest_low)

        logger.info(
            f"[ENTRADA] Estimativas → Entry≈{entry_est:.4f} | "
            f"SL={sl_price:.4f} | TP={tp_price:.4f} | R/R=1:{RR_RATIO}"
        )

        # ── 3. Dimensionamento ────────────────────────────────────────────────
        quantity = calculate_position_size(balance, entry_est, sl_price)

        # ── 4. Precisão do mercado ────────────────────────────────────────────
        quantity = _apply_market_precision(exchange, quantity)
        logger.info(f"[ENTRADA] Quantidade ajustada (precisão): {quantity:.6f} BCH")

        # ── 5. Ordem de compra a mercado ──────────────────────────────────────
        buy_order = place_market_buy(exchange, quantity)
        if not buy_order:
            return False

        # Breve espera para a ordem ser processada
        time.sleep(2)

        # ── 6. Preço real de execução ─────────────────────────────────────────
        entry_real = entry_est
        qty_real   = quantity
        try:
            filled = exchange.fetch_order(buy_order["id"], SYMBOL)
            avg    = filled.get("average") or filled.get("price")
            filled_qty = filled.get("filled")

            if avg:
                entry_real = float(avg)
            if filled_qty:
                qty_real = float(filled_qty)

            if entry_real != entry_est:
                # Recalcula SL/TP com preço real para maior precisão
                sl_price, tp_price = calculate_sl_tp(entry_real, lowest_low)
                logger.info(
                    f"[ENTRADA] Preço real: {entry_real:.4f} | "
                    f"SL recalculado={sl_price:.4f} | TP recalculado={tp_price:.4f}"
                )
        except Exception as exc:
            logger.warning(
                f"Não foi possível obter preço real de execução: {exc}. "
                f"Usando estimativa."
            )

        # ── 7. Ordens de saída ────────────────────────────────────────────────
        exit_info = place_exit_orders(exchange, qty_real, tp_price, sl_price)

        # ── 8. Persistir estado ───────────────────────────────────────────────
        state = {
            "is_open"     : True,
            "entry_price" : entry_real,
            "quantity"    : qty_real,
            "sl_price"    : sl_price,
            "tp_price"    : tp_price,
            "tp_order_id" : exit_info.get("tp_order_id"),
            "sl_order_id" : exit_info.get("sl_order_id"),
            "exit_mode"   : exit_info.get("exit_mode", "monitor"),
        }
        save_state(state)

        logger.info(
            f"[POSIÇÃO ABERTA] ✓ Entry={entry_real:.4f} | Qty={qty_real:.6f} BCH | "
            f"SL={sl_price:.4f} | TP={tp_price:.4f} | "
            f"Modo saída: {exit_info['exit_mode'].upper()}"
        )
        return True

    except ValueError as exc:
        logger.error(f"[ENTRADA] Parâmetros inválidos: {exc}")
        return False
    except Exception as exc:
        logger.error(f"[ENTRADA] Erro inesperado ao abrir posição: {exc}", exc_info=True)
        return False
