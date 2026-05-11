"""
trade_history.py — Registra o histórico completo de operações em CSV.

Cada operação fechada gera uma linha em trade_history.csv com
métricas calculadas (PnL, R múltiplo, motivo de saída, etc.).
"""

import csv
import os
from datetime import datetime, timezone
from typing import Dict, Optional

HISTORY_FILE = "trade_history.csv"

_HEADERS = [
    "symbol", "entry_time", "exit_time",
    "entry_price", "exit_price", "quantity",
    "pnl_usdt", "pnl_pct", "r_multiple",
    "exit_reason", "sl_price", "tp_price",
]


def _ensure_headers() -> None:
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(_HEADERS)


def record_trade(
    symbol: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
    sl_price: float,
    tp_price: float,
    exit_reason: str,
    entry_time: Optional[str] = None,
) -> Dict[str, float]:
    """
    Persiste uma operação encerrada e retorna as métricas calculadas.

    Retorna: {"pnl_usdt": float, "pnl_pct": float, "r_multiple": float}
    """
    _ensure_headers()

    now        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    pnl_usdt   = (exit_price - entry_price) * quantity
    pnl_pct    = ((exit_price - entry_price) / entry_price) * 100.0
    risk_unit  = entry_price - sl_price
    r_multiple = ((exit_price - entry_price) / risk_unit) if risk_unit > 0 else 0.0

    row = {
        "symbol"      : symbol,
        "entry_time"  : entry_time or now,
        "exit_time"   : now,
        "entry_price" : f"{entry_price:.4f}",
        "exit_price"  : f"{exit_price:.4f}",
        "quantity"    : f"{quantity:.6f}",
        "pnl_usdt"    : f"{pnl_usdt:+.2f}",
        "pnl_pct"     : f"{pnl_pct:+.2f}",
        "r_multiple"  : f"{r_multiple:.2f}",
        "exit_reason" : exit_reason,
        "sl_price"    : f"{sl_price:.4f}",
        "tp_price"    : f"{tp_price:.4f}",
    }

    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=_HEADERS).writerow(row)

    return {"pnl_usdt": pnl_usdt, "pnl_pct": pnl_pct, "r_multiple": r_multiple}
