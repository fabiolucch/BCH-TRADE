"""dca.py — Motor de lógica DCA com Trailing Stop."""
import asyncio
import logging
from typing import Awaitable, Callable

from config import PAIRS
from exchange import ExchangeClient
from state import StateManager

logger = logging.getLogger("bot.dca")

Notifier = Callable[[str], Awaitable[None]]


class DCAEngine:
    def __init__(
        self,
        exchange: ExchangeClient,
        state: StateManager,
        bot_config,          # BotConfig — injetado para config dinâmica
        notify: Notifier,
    ) -> None:
        self.exchange   = exchange
        self.state      = state
        self.bot_config = bot_config
        self.notify     = notify
        # Lock por par: evita condição de corrida entre loop DCA e comandos Telegram
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

    # ── Avaliação por par ─────────────────────────────────────────────────────

    async def _check_pair(self, pair: str) -> None:
        async with self._locks[pair]:
            price = await self.exchange.get_price(pair)
            pos   = self.state.get_position(pair)

            logger.debug(
                f"[{pair}] preço={price:.6f} | ativo={pos['is_active']} | "
                f"aportes={pos['order_count']} | avg={pos['avg_price']:.6f} | "
                f"trailing={pos['trailing_active']}"
            )

            if pos["is_active"]:
                await self._evaluate_open_position(pair, price, pos)
            else:
                await self._buy(pair, price, order_num=1)

    async def _evaluate_open_position(self, pair: str, price: float, pos: dict) -> None:
        cfg = self.bot_config.get()

        # ── Trailing Stop ativo ───────────────────────────────────────────────
        if pos["trailing_active"]:
            # Atualiza pico e preço de stop se o preço subiu
            if price > pos["peak_price"]:
                new_stop = price * (1 - cfg["trailing_stop_pct"] / 100)
                self.state.update_trailing(pair, price, new_stop)
                logger.debug(
                    f"[{pair}] Trailing: novo pico={price:.4f} | stop={new_stop:.4f}"
                )

            # Relê posição atualizada para verificar stop atual
            pos = self.state.get_position(pair)
            if price <= pos["trailing_stop_price"]:
                await self._sell(pair, pos, price, reason="Trailing Stop")
            return

        # ── Take Profit ───────────────────────────────────────────────────────
        tp_price = pos["avg_price"] * (1 + cfg["take_profit_pct"] / 100)
        if price >= tp_price:
            if cfg["trailing_stop_enabled"]:
                # Ativa trailing em vez de vender imediatamente
                stop_px = price * (1 - cfg["trailing_stop_pct"] / 100)
                self.state.update_trailing(pair, price, stop_px)
                quote = pair.split("/")[1]
                msg = (
                    f"🎯 *Trailing Stop ativado — {pair}*\n"
                    f"├ TP atingido em: `{price:.4f}`\n"
                    f"├ Avg entrada: `{pos['avg_price']:.4f}`\n"
                    f"├ Stop inicial: `{stop_px:.4f}` (-{cfg['trailing_stop_pct']}%)\n"
                    f"└ Bot acompanha o preço até cair {cfg['trailing_stop_pct']}% do pico."
                )
                logger.info(msg.replace("*", "").replace("`", ""))
                await self.notify(msg)
            else:
                await self._sell(pair, pos, price, reason="Take Profit")
            return

        # ── Nova entrada DCA ──────────────────────────────────────────────────
        if pos["order_count"] >= cfg["max_dca_orders"]:
            logger.debug(f"[{pair}] Limite de {cfg['max_dca_orders']} aportes atingido. Aguardando TP.")
            return

        drop_pct = (pos["last_buy_price"] - price) / pos["last_buy_price"] * 100
        if drop_pct >= cfg["dca_drop_pct"]:
            await self._buy(pair, price, order_num=pos["order_count"] + 1)

    # ── Execução de ordens ────────────────────────────────────────────────────

    async def _buy(self, pair: str, price: float, order_num: int) -> None:
        cfg   = self.bot_config.get()
        quote = pair.split("/")[1]
        balance = await self.exchange.get_balance(quote)

        if balance < cfg["order_size_usdt"]:
            msg = (
                f"⚠️ Saldo insuficiente para DCA #{order_num} em *{pair}*\n"
                f"├ Disponível: `{balance:.2f} {quote}`\n"
                f"└ Necessário: `{cfg['order_size_usdt']:.2f} {quote}`"
            )
            logger.warning(msg.replace("*", "").replace("`", ""))
            await self.notify(msg)
            return

        qty = self.exchange.amount_to_precision(pair, cfg["order_size_usdt"] / price)

        min_cost = self.exchange.get_min_order_cost(pair)
        if min_cost and (qty * price) < min_cost:
            logger.warning(
                f"[{pair}] Custo {qty * price:.4f} abaixo do mínimo {min_cost:.4f}. "
                "Aumente ORDER_SIZE_USDT."
            )
            return

        order = await self.exchange.market_buy(pair, qty)
        if order is None:
            await self.notify(f"❌ Falha ao executar compra DCA #{order_num} em *{pair}*")
            return

        filled  = float(order.get("filled") or qty)
        avg_px  = float(order.get("average") or order.get("price") or price)
        cost    = float(order.get("cost") or filled * avg_px)

        pos     = self.state.record_buy(pair, avg_px, filled, cost)
        tp_price = pos["avg_price"] * (1 + cfg["take_profit_pct"] / 100)
        trail_info = (
            f"├ Trailing: `{'ON' if cfg['trailing_stop_enabled'] else 'OFF'}`"
            + (f" (-{cfg['trailing_stop_pct']}%)" if cfg["trailing_stop_enabled"] else "")
            + "\n"
        )

        msg = (
            f"✅ *DCA #{order_num} — {pair}*\n"
            f"├ Preço execução: `{avg_px:.4f}`\n"
            f"├ Quantidade: `{filled:.6f}`\n"
            f"├ Custo: `{cost:.2f} {quote}`\n"
            f"├ Preço médio: `{pos['avg_price']:.4f}`\n"
            f"├ Total investido: `{pos['total_cost']:.2f} {quote}`\n"
            f"{trail_info}"
            f"└ Alvo TP: `{tp_price:.4f}` (+{cfg['take_profit_pct']}%)"
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

    # ── Comando manual ────────────────────────────────────────────────────────

    async def force_close(self, pair: str) -> str:
        """Fecha posição a mercado via comando Telegram."""
        async with self._locks[pair]:
            pos = self.state.get_position(pair)
            if not pos["is_active"]:
                return f"ℹ️ *{pair}* não tem posição aberta."
            price = await self.exchange.get_price(pair)
            await self._sell(pair, pos, price, reason="Fechamento Manual")
            return f"✅ Posição *{pair}* encerrada a mercado."
