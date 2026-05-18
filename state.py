"""state.py — Persistência do estado das posições e histórico de trades em JSON."""
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from config import STATE_FILE

logger = logging.getLogger("bot.state")

_EMPTY_POSITION: dict[str, Any] = {
    "is_active"           : False,
    "orders"              : [],      # lista de {price, qty, cost, ts}
    "avg_price"           : 0.0,
    "total_qty"           : 0.0,
    "total_cost"          : 0.0,     # total investido em quote currency
    "last_buy_price"      : 0.0,
    "order_count"         : 0,
    "first_buy_at"        : None,
    # Trailing Stop
    "trailing_active"     : False,
    "peak_price"          : 0.0,     # maior preço visto desde ativação do trailing
    "trailing_stop_price" : 0.0,     # preço de venda = peak * (1 - trailing_pct/100)
}

_EMPTY_STATE: dict[str, Any] = {
    "positions"    : {},
    "trade_history": [],
}


class StateManager:
    def __init__(self) -> None:
        self._data = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # Garante campos novos em posições existentes (migração)
                for pair, pos in data.get("positions", {}).items():
                    for k, v in _EMPTY_POSITION.items():
                        pos.setdefault(k, v)
                logger.info(f"Estado carregado de '{STATE_FILE}'.")
                return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Não foi possível ler '{STATE_FILE}' ({exc}). Iniciando do zero.")
        return deepcopy(_EMPTY_STATE)

    def _save(self) -> None:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False, default=str)
        except OSError as exc:
            logger.error(f"Falha ao salvar estado: {exc}")

    # ── Posições ──────────────────────────────────────────────────────────────

    def get_position(self, pair: str) -> dict:
        return deepcopy(self._data["positions"].get(pair, _EMPTY_POSITION))

    def _set_position(self, pair: str, pos: dict) -> None:
        self._data["positions"][pair] = pos
        self._save()

    def record_buy(self, pair: str, price: float, qty: float, cost: float) -> dict:
        """Registra uma compra DCA e recalcula o preço médio. Retorna posição atualizada."""
        pos = self.get_position(pair)
        now = datetime.now(timezone.utc).isoformat()

        pos["orders"].append({"price": price, "qty": qty, "cost": cost, "ts": now})
        pos["total_qty"]     += qty
        pos["total_cost"]    += cost
        pos["avg_price"]      = pos["total_cost"] / pos["total_qty"]
        pos["last_buy_price"] = price
        pos["order_count"]   += 1
        pos["is_active"]      = True

        if not pos["first_buy_at"]:
            pos["first_buy_at"] = now

        self._set_position(pair, pos)
        return pos

    def update_trailing(
        self,
        pair: str,
        peak_price: float,
        trailing_stop_price: float,
        active: bool = True,
    ) -> None:
        """Atualiza os campos de trailing stop de uma posição aberta."""
        pos = self.get_position(pair)
        pos["trailing_active"]     = active
        pos["peak_price"]          = peak_price
        pos["trailing_stop_price"] = trailing_stop_price
        self._set_position(pair, pos)

    def close_position(self, pair: str, exit_price: float) -> dict:
        """Encerra posição, calcula PnL e registra no histórico. Retorna o trade."""
        pos = self.get_position(pair)
        now = datetime.now(timezone.utc).isoformat()

        revenue  = exit_price * pos["total_qty"]
        pnl_usdt = revenue - pos["total_cost"]
        pnl_pct  = (pnl_usdt / pos["total_cost"] * 100) if pos["total_cost"] > 0 else 0.0

        trade = {
            "pair"        : pair,
            "entry_avg"   : pos["avg_price"],
            "exit_price"  : exit_price,
            "qty"         : pos["total_qty"],
            "cost"        : pos["total_cost"],
            "revenue"     : revenue,
            "pnl_usdt"    : pnl_usdt,
            "pnl_pct"     : pnl_pct,
            "order_count" : pos["order_count"],
            "opened_at"   : pos["first_buy_at"],
            "closed_at"   : now,
        }
        self._data["trade_history"].append(trade)
        self._data["positions"][pair] = deepcopy(_EMPTY_POSITION)
        self._save()
        return trade

    # ── Histórico ─────────────────────────────────────────────────────────────

    def get_trade_history(self) -> list[dict]:
        return self._data["trade_history"]
