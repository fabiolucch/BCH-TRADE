"""
execution.py — Execução, dimensionamento e trailing stop (Atualizado p/ Produção).
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
    RISK_PCT, RR_RATIO, SL_BUFFER_PCT, MAX_POSITION_PCT, MIN_BALANCE_USDT,
    FEE_RESERVE_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT, state_file_for,
)
import telegram_notifier as tg
import trade_history as history

logger = logging.getLogger("bot")

_EMPTY_STATE = {
    "is_open": False, "entry_price": 0.0, "quantity": 0.0, "sl_price": 0.0, "tp_price": 0.0,
    "sl_order_id": None, "tp_order_id": None, "exit_mode": "monitor", "entry_time": None,
    "highest_price": 0.0, "trail_active": False, "trail_sl": None,
}

def load_state(symbol: str) -> Dict[str, Any]:
    path = state_file_for(symbol)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return dict(_EMPTY_STATE)

def save_state(state: Dict[str, Any], symbol: str) -> None:
    try:
        with open(state_file_for(symbol), "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.error(f"[{symbol}] Falha ao salvar estado: {exc}")

def reset_state(symbol: str) -> None:
    save_state(dict(_EMPTY_STATE), symbol)

def calculate_sl_tp(entry_price: float, lowest_low: float) -> Tuple[float, float]:
    sl_price = lowest_low * (1.0 - SL_BUFFER_PCT / 100.0)
    risk_per_unit = entry_price - sl_price
    if risk_per_unit <= 0:
        raise ValueError("SL >= Entry.")
    tp_price = entry_price + (risk_per_unit * RR_RATIO)
    return sl_price, tp_price

def calculate_position_size(effective_balance: float, entry_price: float, sl_price: float) -> float:
    max_risk = effective_balance * (RISK_PCT / 100.0)
    risk_per_unit = entry_price - sl_price
    quantity = max_risk / risk_per_unit
    
    max_position_value = effective_balance * (MAX_POSITION_PCT / 100.0)
    position_value = quantity * entry_price
    if position_value > max_position_value:
        quantity = max_position_value / entry_price
    return quantity

def get_usdt_balance(exchange: ccxt.Exchange) -> float:
    try:
        balance = exchange.fetch_balance()
        # Tratamento seguro contra bug do CCXT retornando None para USDT vazio
        usdt_data = balance.get("USDT")
        usdt = float(usdt_data.get("free", 0.0)) if isinstance(usdt_data, dict) else 0.0
        return usdt
    except Exception as exc:
        logger.error(f"Erro ao buscar saldo USDT: {exc}")
        raise

def get_current_price(exchange: ccxt.Exchange, symbol: str) -> float:
    return float(exchange.fetch_ticker(symbol)["last"])

def _apply_market_precision(exchange: ccxt.Exchange, quantity: float, symbol: str) -> float:
    """Usa a formatação nativa do CCXT para não errar a quantidade mínima da OKX."""
    try:
        formatted_qty = exchange.amount_to_precision(symbol, quantity)
        return float(formatted_qty)
    except Exception as exc:
        logger.warning(f"[{symbol}] Precisão não aplicada: {exc}. Usando bruto.")
        return quantity

def place_market_buy(exchange: ccxt.Exchange, quantity: float, symbol: str) -> Optional[Dict]:
    try:
        return exchange.create_market_buy_order(symbol, quantity)
    except Exception as exc:
        logger.error(f"[{symbol}] Erro ao comprar: {exc}")
        return None

def _place_okx_algo_sl(exchange: ccxt.Exchange, quantity: float, sl_price: float, symbol: str) -> Optional[Dict]:
    params = {"ordType": "conditional", "slTriggerPx": str(sl_price), "slOrdPx": "-1", "slTriggerPxType": "last"}
    return exchange.create_order(symbol, "market", "sell", quantity, params=params)

def place_exit_orders(exchange: ccxt.Exchange, quantity: float, tp_price: float, sl_price: float, symbol: str) -> Dict:
    result = {"exit_mode": "monitor", "tp_order_id": None, "sl_order_id": None}
    
    try:
        tp_order = exchange.create_limit_sell_order(symbol, quantity, tp_price)
        result["tp_order_id"] = tp_order.get("id")
    except Exception:
        pass

    if result["tp_order_id"]:
        try:
            sl_order = _place_okx_algo_sl(exchange, quantity, sl_price, symbol)
            result["sl_order_id"] = sl_order.get("id")
        except Exception:
            try: exchange.cancel_order(result["tp_order_id"], symbol)
            except Exception: pass
            result["tp_order_id"] = None

    if result["tp_order_id"] and result["sl_order_id"]:
        result["exit_mode"] = "orders"
    return result

def close_position_market(exchange: ccxt.Exchange, quantity: float, reason: str, symbol: str) -> bool:
    try:
        exchange.create_market_sell_order(symbol, quantity)
        return True
    except Exception as exc:
        logger.error(f"[{symbol}] Erro ao fechar posição: {exc}")
        return False

def _cancel_exit_orders(exchange: ccxt.Exchange, state: Dict, symbol: str) -> None:
    for key in ("tp_order_id", "sl_order_id"):
        if state.get(key):
            try: exchange.cancel_order(state[key], symbol)
            except Exception: pass

def _update_trailing_stop(exchange: ccxt.Exchange, state: Dict, price: float, symbol: str) -> bool:
    entry = state["entry_price"]
    if price > state.get("highest_price", entry):
        state["highest_price"] = price
        save_state(state, symbol)

    highest = state["highest_price"]
    profit_pct = ((highest - entry) / entry) * 100.0

    if not state.get("trail_active") and profit_pct >= TRAILING_ACTIVATION_PCT:
        trail_sl = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
        state["trail_active"] = True
        state["trail_sl"] = trail_sl
        if state.get("exit_mode") == "orders":
            _cancel_exit_orders(exchange, state, symbol)
            state["exit_mode"] = "monitor"
        tg.notify_trailing_activated(symbol, highest, trail_sl)
        save_state(state, symbol)

    if state.get("trail_active"):
        new_trail_sl = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
        if new_trail_sl > state.get("trail_sl", 0.0):
            state["trail_sl"] = new_trail_sl
            save_state(state, symbol)
            tg.notify_trailing_updated(symbol, new_trail_sl)

        if price <= state["trail_sl"]:
            return True
    return False

def check_open_position(exchange: ccxt.Exchange, state: Dict, symbol: str) -> bool:
    if not state.get("is_open"): return False

    entry, tp_price, sl_price, quantity = state["entry_price"], state["tp_price"], state["sl_price"], state["quantity"]
    price = get_current_price(exchange, symbol)
    pnl_pct = ((price - entry) / entry) * 100.0

    if state.get("exit_mode") == "monitor":
        if _update_trailing_stop(exchange, state, price, symbol):
            if close_position_market(exchange, quantity, "Trailing Stop", symbol):
                metrics = history.record_trade(symbol, entry, price, quantity, sl_price, tp_price, "Trailing Stop")
                tg.notify_position_closed(symbol, "Trailing Stop", entry, price, metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"])
                reset_state(symbol)
                return False

        if price >= tp_price:
            if close_position_market(exchange, quantity, "Take Profit", symbol):
                metrics = history.record_trade(symbol, entry, price, quantity, sl_price, tp_price, "Take Profit")
                tg.notify_position_closed(symbol, "Take Profit", entry, price, metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"])
                reset_state(symbol)
                return False

        elif price <= sl_price:
            if close_position_market(exchange, quantity, "Stop Loss", symbol):
                metrics = history.record_trade(symbol, entry, price, quantity, sl_price, tp_price, "Stop Loss")
                tg.notify_position_closed(symbol, "Stop Loss", entry, price, metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"])
                reset_state(symbol)
                return False
    else:
        _update_trailing_stop(exchange, state, price, symbol)
        if state.get("exit_mode") == "monitor": return True

        for order_key, other_key in (("tp_order_id", "sl_order_id"), ("sl_order_id", "tp_order_id")):
            order_id = state.get(order_key)
            if not order_id: continue

            try:
                order = exchange.fetch_order(order_id, symbol)
                if order.get("status") == "closed":
                    label = "Take Profit" if order_key == "tp_order_id" else "Stop Loss"
                    exit_price = float(order.get("average") or order.get("price") or price)
                    
                    if state.get(other_key):
                        try: exchange.cancel_order(state.get(other_key), symbol)
                        except Exception: pass
                    
                    metrics = history.record_trade(symbol, entry, exit_price, quantity, sl_price, tp_price, label)
                    tg.notify_position_closed(symbol, label, entry, exit_price, metrics["pnl_usdt"], metrics["pnl_pct"], metrics["r_multiple"])
                    reset_state(symbol)
                    return False

                elif order.get("status") == "canceled":
                    _cancel_exit_orders(exchange, state, symbol)
                    state["exit_mode"] = "monitor"
                    save_state(state, symbol)
                    break
            except Exception: pass
    return True

def open_position(exchange: ccxt.Exchange, indicators: Dict, symbol: str) -> bool:
    try:
        balance = get_usdt_balance(exchange)
        if balance < MIN_BALANCE_USDT:
            return False

        effective_balance = balance * (1.0 - FEE_RESERVE_PCT / 100.0)
        entry_est, lowest_low = indicators["close_entry"], indicators["lowest_low_5c"]
        sl_price, tp_price = calculate_sl_tp(entry_est, lowest_low)
        
        quantity = calculate_position_size(effective_balance, entry_est, sl_price)
        quantity = _apply_market_precision(exchange, quantity, symbol)

        buy_order = place_market_buy(exchange, quantity, symbol)
        if not buy_order: return False

        time.sleep(2)
        entry_real, qty_real = entry_est, quantity
        
        try:
            filled = exchange.fetch_order(buy_order["id"], symbol)
            if filled.get("average"): entry_real = float(filled["average"])
            if filled.get("filled"): qty_real = float(filled["filled"])
            if entry_real != entry_est:
                sl_price, tp_price = calculate_sl_tp(entry_real, lowest_low)
        except Exception: pass

        exit_info = place_exit_orders(exchange, qty_real, tp_price, sl_price, symbol)
        
        state = {
            "is_open": True, "entry_price": entry_real, "quantity": qty_real,
            "sl_price": sl_price, "tp_price": tp_price,
            "tp_order_id": exit_info.get("tp_order_id"), "sl_order_id": exit_info.get("sl_order_id"),
            "exit_mode": exit_info.get("exit_mode", "monitor"), "entry_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "highest_price": entry_real, "trail_active": False, "trail_sl": None,
        }
        save_state(state, symbol)
        tg.notify_position_opened(symbol, entry_real, sl_price, tp_price, qty_real)
        return True
    except Exception as exc:
        logger.error(f"[{symbol}] Erro inesperado ao abrir: {exc}")
        return False