"""dca.py — Motor de lógica DCA: avalia condições e executa ordens."""
import asyncio
import logging
from typing import Awaitable, Callable

from config import (
    DCA_DROP_PCT,
    MAX_DCA_ORDERS,
    ORDER_SIZE_USDT,
    PAIRS,
    TAKE_PROFIT_PCT,
)
from exchange import ExchangeClient
from state import StateManager

logger = logging.getLogger("bot.dca")

Notifier = Callable[[str], Awaitable[None]]


class DCAEngine:
    def __init__(
        self,
        exchange: ExchangeClient,
        state: StateManager,
        notify: Notifier,
    ) -> None:
        self.exchange = exchange
        self.state    = state
        self.notify   = notify
        # Lock por par: evita condição de corrida entre o loop DCA e comandos Telegram
        self._locks: dict[str, asyncio.Lock] = {pair: asyncio.Lock() for pair in PAIRS}

    # ── Loop principal ────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """Processa todos os pares em sequência em cada ciclo."""
        for pair in PAIRS:
            try:
                await self._check_pair(pair)
            except Exception as exc:
                msg = f"❌ Erro inesperado em {pair}: {exc}"
                logger.error(msg, exc_info=True)
                await self.notify(msg)

    # ── Lógica por par ────────────────────────────────────────────────────────

    async def _check_pair(self, pair: str) -> None:
        async with self._locks[pair]:
            price = await self.exchange.get_price(pair)
            pos   = self.state.get_position(pair)

            logger.debug(
                f"[{pair}] preço={price:.6f} | ativo={pos['is_active']} | "
                f"aportes={pos['order_count']} | avg={pos['avg_price']:.6f}"
            )

            if pos["is_active"]:
                await self._evaluate_open_position(pair, price, pos)
            else:
                # Primeira entrada: abre posição imediatamente
                await self._buy(pair, price, order_num=1)

    async def _evaluate_open_position(self, pair: str, price: float, pos: dict) -> None:
        # Verifica Take Profit primeiro
        tp_price = pos["avg_price"] * (1 + TAKE_PROFIT_PCT / 100)
        if price >= tp_price:
            await self._sell(pair, pos, price, reason="Take Profit")
            return

        # Verifica nova entrada DCA (somente se não atingiu o limite de aportes)
        if pos["order_count"] >= MAX_DCA_ORDERS:
            logger.debug(f"[{pair}] Limite de {MAX_DCA_ORDERS} aportes atingido. Aguardando TP.")
            return

        drop_pct = (pos["last_buy_price"] - price) / pos["last_buy_price"] * 100
        if drop_pct >= DCA_DROP_PCT:
            await self._buy(pair, price, order_num=pos["order_count"] + 1)

    # ── Execução de ordens ────────────────────────────────────────────────────

    async def _buy(self, pair: str, price: float, order_num: int) -> None:
        quote = pair.split("/")[1]
        balance = await self.exchange.get_balance(quote)

        if balance < ORDER_SIZE_USDT:
            msg = (
                f"⚠️ Saldo insuficiente para DCA #{order_num} em *{pair}*\n"
                f"├ Disponível: `{balance:.2f} {quote}`\n"
                f"└ Necessário: `{ORDER_SIZE_USDT:.2f} {quote}`"
            )
            logger.warning(msg.replace("*", "").replace("`", ""))
            await self.notify(msg)
            return

        qty = self.exchange.amount_to_precision(pair, ORDER_SIZE_USDT / price)

        min_cost = self.exchange.get_min_order_cost(pair)
        if min_cost and (qty * price) < min_cost:
            logger.warning(
                f"[{pair}] Custo estimado {qty * price:.4f} abaixo do mínimo {min_cost:.4f}. "
                f"Aumente ORDER_SIZE_USDT."
            )
            return

        order = await self.exchange.market_buy(pair, qty)
        if order is None:
            await self.notify(f"❌ Falha ao executar compra DCA #{order_num} em *{pair}*")
            return

        filled   = float(order.get("filled") or qty)
        avg_px   = float(order.get("average") or order.get("price") or price)
        cost     = float(order.get("cost") or filled * avg_px)

        pos      = self.state.record_buy(pair, avg_px, filled, cost)
        tp_price = pos["avg_price"] * (1 + TAKE_PROFIT_PCT / 100)

        msg = (
            f"✅ *DCA #{order_num} — {pair}*\n"
            f"├ Preço execução: `{avg_px:.4f}`\n"
            f"├ Quantidade: `{filled:.6f}`\n"
            f"├ Custo: `{cost:.2f} {quote}`\n"
            f"├ Preço médio: `{pos['avg_price']:.4f}`\n"
            f"├ Total investido: `{pos['total_cost']:.2f} {quote}`\n"
            f"└ Alvo TP: `{tp_price:.4f}` (+{TAKE_PROFIT_PCT}%)"
        )
        logger.info(msg.replace("*", "").replace("`", ""))
        await self.notify(msg)

    async def _sell(self, pair: str, pos: dict, price: float, reason: str) -> None:
        quote = pair.split("/")[1]
        order = await self.exchange.market_sell(pair, pos["total_qty"])

        if order is None:
            await self.notify(f"❌ Falha ao executar venda ({reason}) em *{pair}*")
            return

        exit_px = float(order.get("average") or order.get("price") or price)
        trade   = self.state.close_position(pair, exit_px)

        emoji = "🟢" if trade["pnl_usdt"] >= 0 else "🔴"
        msg = (
            f"{emoji} *{reason} — {pair}*\n"
            f"├ Preço saída: `{exit_px:.4f}`\n"
            f"├ Preço médio entrada: `{trade['entry_avg']:.4f}`\n"
            f"├ Aportes realizados: `{trade['order_count']}`\n"
            f"├ Quantidade: `{trade['qty']:.6f}`\n"
            f"├ Investido: `{trade['cost']:.2f} {quote}`\n"
            f"├ Receita: `{trade['revenue']:.2f} {quote}`\n"
            f"└ PnL: `{trade['pnl_usdt']:+.2f} {quote} ({trade['pnl_pct']:+.2f}%)`"
        )
        logger.info(msg.replace("*", "").replace("`", ""))
        await self.notify(msg)

    # ── Comando manual (Telegram) ─────────────────────────────────────────────

    async def force_close(self, pair: str) -> str:
        """Fecha posição a mercado via comando Telegram. Retorna mensagem de resultado."""
        async with self._locks[pair]:
            pos = self.state.get_position(pair)
            if not pos["is_active"]:
                return f"ℹ️ *{pair}* não tem posição aberta."

            price = await self.exchange.get_price(pair)
            await self._sell(pair, pos, price, reason="Fechamento Manual")
            return f"✅ Posição *{pair}* encerrada a mercado."
