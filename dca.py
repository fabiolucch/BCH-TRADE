"""dca.py — Motor de lógica DCA com Trailing Stop e gestão de capital."""
import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable

from config import FEE_RATE, PAIRS
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
        # defaultdict cria Lock automaticamente para novos pares adicionados via Telegram
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Rastreia se já foi emitido alerta de saldo baixo por moeda de quote
        self._low_balance_alerts: dict[str, bool] = {}

    # ── Loop principal ────────────────────────────────────────────────────────

    async def tick(self) -> None:
        """Processa todos os pares em sequência em cada ciclo."""
        all_pairs = self.bot_config.get_all_pairs()
        await self._check_capital(all_pairs)
        for pair in all_pairs:
            try:
                await self._check_pair(pair)
            except Exception as exc:
                msg = f"❌ Erro inesperado em {pair}: {exc}"
                logger.error(msg, exc_info=True)
                await self.notify(msg)

    # ── Gestão de capital ─────────────────────────────────────────────────────

    async def _check_capital(self, pairs: list[str] | None = None) -> None:
        """Verifica saldo de cada quote currency e emite alertas no Telegram."""
        cfg    = self.bot_config.get()
        quotes = set(pair.split("/")[1] for pair in (pairs or self.bot_config.get_all_pairs()))
        for quote in quotes:
            try:
                balance = await self.exchange.get_balance(quote)
            except Exception:
                continue
            was_low = self._low_balance_alerts.get(quote, False)
            is_low  = balance < cfg["order_size_usdt"]
            if is_low and not was_low:
                self._low_balance_alerts[quote] = True
                msg = (
                    f"⚠️ *Saldo insuficiente — {quote}*\n"
                    f"├ Disponível: `{balance:.2f} {quote}`\n"
                    f"├ Necessário por aporte: `{cfg['order_size_usdt']:.2f} {quote}`\n"
                    f"└ Novas entradas DCA em {quote} estão bloqueadas."
                )
                logger.warning(msg.replace("*", "").replace("`", ""))
                await self.notify(msg)
            elif not is_low and was_low:
                self._low_balance_alerts[quote] = False
                msg = (
                    f"✅ *Saldo recuperado — {quote}*\n"
                    f"├ Disponível: `{balance:.2f} {quote}`\n"
                    f"└ DCA retomado para pares {quote}."
                )
                logger.info(msg.replace("*", "").replace("`", ""))
                await self.notify(msg)

    # ── Avaliação por par ─────────────────────────────────────────────────────

    async def _check_pair(self, pair: str) -> None:
        async with self._locks[pair]:
            pos = self.state.get_position(pair)
            cfg = self.bot_config.get()

            # Quando parado e sem posição aberta, não há nada a fazer neste par
            if not pos["is_active"] and not cfg.get("bot_running", False):
                return

            price = await self.exchange.get_price(pair)

            logger.debug(
                f"[{pair}] preço={price:.6f} | ativo={pos['is_active']} | "
                f"aportes={pos['order_count']} | avg={pos['avg_price']:.6f} | "
                f"trailing={pos['trailing_active']}"
            )

            if pos["is_active"]:
                await self._evaluate_open_position(pair, price, pos)
            else:
                # Só abre posição nova se par estiver na lista ativa
                all_pairs = self.bot_config.get_all_pairs()
                active    = [p for p in cfg.get("active_pairs", all_pairs) if p in all_pairs]
                if pair in active:
                    # Verificação de re-entrada: aguarda queda abaixo do preço de saída
                    reentry_drop = cfg.get("reentry_drop_pct", 0.0)
                    last_exit    = pos.get("last_exit_price", 0.0)
                    if last_exit > 0 and reentry_drop > 0:
                        threshold = last_exit * (1 - reentry_drop / 100)
                        if price > threshold:
                            logger.debug(
                                f"[{pair}] Re-entrada bloqueada: preço {price:.4f} > "
                                f"limiar {threshold:.4f} (saída {last_exit:.4f} -{reentry_drop}%)"
                            )
                            return
                    # Filtro RSI: só entra se RSI < threshold
                    if cfg.get("rsi_enabled", False):
                        rsi = await self._get_rsi(pair, cfg)
                        if rsi is not None and rsi >= cfg.get("rsi_threshold", 45.0):
                            logger.debug(
                                f"[{pair}] Entrada bloqueada por RSI: "
                                f"{rsi:.1f} ≥ {cfg['rsi_threshold']:.0f}"
                            )
                            return
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
        if drop_pct >= cfg["dca_drop_pct"] and cfg.get("bot_running", False):
            await self._buy(pair, price, order_num=pos["order_count"] + 1)

    # ── Execução de ordens ────────────────────────────────────────────────────

    async def _buy(self, pair: str, price: float, order_num: int) -> None:
        cfg       = self.bot_config.get()
        quote     = pair.split("/")[1]
        size_usdt = _order_size(cfg, order_num)
        balance   = await self.exchange.get_balance(quote)

        if balance < size_usdt:
            logger.warning(
                f"[{pair}] Saldo insuficiente: {balance:.2f} {quote} < "
                f"{size_usdt:.2f} {quote}"
            )
            # _check_capital() já enviou alerta por quote currency — não duplicar
            if not self._low_balance_alerts.get(quote, False):
                await self.notify(
                    f"⚠️ Saldo insuficiente para DCA #{order_num} em *{pair}*\n"
                    f"├ Disponível: `{balance:.2f} {quote}`\n"
                    f"└ Necessário: `{size_usdt:.2f} {quote}`"
                )
            return

        qty = self.exchange.amount_to_precision(pair, size_usdt / price)

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

        filled   = float(order.get("filled") or qty)
        avg_px   = float(order.get("average") or order.get("price") or price)
        cost     = float(order.get("cost") or filled * avg_px)
        fee_usdt = _extract_fee(order, quote, avg_px, fallback=cost * FEE_RATE)

        pos      = self.state.record_buy(pair, avg_px, filled, cost, fee_usdt=fee_usdt)
        tp_price = pos["avg_price"] * (1 + cfg["take_profit_pct"] / 100)

        levels = int(cfg.get("martingale_levels", 0))
        mart_line = (
            f"├ Martingale: `×{min(order_num, max(levels, 1))}` "
            f"(`{size_usdt:.0f} {quote}`)\n"
            if levels > 0 else ""
        )
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
            f"{mart_line}"
            f"├ Taxa: `{fee_usdt:.4f} {quote}`\n"
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

        exit_px      = float(order.get("average") or order.get("price") or price)
        gross_rev    = exit_px * pos["total_qty"]
        sell_fee     = _extract_fee(order, quote, exit_px, fallback=gross_rev * FEE_RATE)
        trade        = self.state.close_position(pair, exit_px, sell_fee_usdt=sell_fee)

        emoji = "🟢" if trade["pnl_net"] >= 0 else "🔴"
        msg = (
            f"{emoji} *{reason} — {pair}*\n"
            f"├ Preço saída: `{exit_px:.4f}`\n"
            f"├ Preço médio entrada: `{trade['entry_avg']:.4f}`\n"
            f"├ Aportes realizados: `{trade['order_count']}`\n"
            f"├ Quantidade: `{trade['qty']:.6f}`\n"
            f"├ Investido: `{trade['cost']:.2f} {quote}`\n"
            f"├ Receita bruta: `{trade['gross_revenue']:.2f} {quote}`\n"
            f"├ Taxas totais: `{trade['fees_usdt']:.4f} {quote}`\n"
            f"├ PnL bruto: `{trade['pnl_gross']:+.2f} {quote}`\n"
            f"└ PnL líquido: `{trade['pnl_usdt']:+.2f} {quote} ({trade['pnl_pct']:+.2f}%)`"
        )
        logger.info(msg.replace("*", "").replace("`", ""))
        await self.notify(msg)

    # ── RSI ───────────────────────────────────────────────────────────────────

    async def _get_rsi(self, pair: str, cfg: dict) -> float | None:
        """Calcula RSI atual do par. Retorna None se falhar (não bloqueia entrada)."""
        try:
            period = int(cfg.get("rsi_period", 14))
            ohlcv  = await self.exchange.get_ohlcv(pair, timeframe="1h", limit=period + 20)
            closes = [float(c[4]) for c in ohlcv]
            return self.exchange.calculate_rsi(closes, period=period)
        except Exception as exc:
            logger.warning(f"[{pair}] Falha ao calcular RSI: {exc}")
            return None

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _order_size(cfg: dict, order_num: int) -> float:
    """Retorna o valor em quote do aporte N com martingale linear.

    Fórmula: initial * min(order_num, max(levels, 1))

    Exemplos:
      initial=5,  levels=3: [5, 10, 15, 15, 15, ...]   (×1, ×2, ×3, ×3, ...)
      initial=10, levels=2: [10, 20, 20, 20, ...]       (×1, ×2, ×2, ...)
      initial=20, levels=0: [20, 20, 20, 20, ...]       (sem martingale)
    """
    levels = int(cfg.get("martingale_levels", 0))
    return cfg["order_size_usdt"] * min(order_num, max(levels, 1))


def _extract_fee(order: dict, quote: str, ref_price: float, fallback: float) -> float:
    """Extrai taxa em quote currency do retorno da ordem.

    ccxt pode retornar:
      - order["fee"]  → dict  {"cost": N, "currency": "USDT"}   (forma singular)
      - order["fees"] → list  [{"cost": N, "currency": "USDT"}] (forma plural / OKX multi-leg)

    Se a moeda da taxa for a base (ex: BTC), converte para quote usando ref_price.
    Usa `fallback` se a ordem não retornar nenhuma taxa.
    """
    # Tenta forma singular primeiro
    fee_info = order.get("fee")
    if not isinstance(fee_info, dict) or not fee_info.get("cost"):
        # Tenta forma plural: soma todas as taxas em quote equivalente
        fees_list = order.get("fees")
        if isinstance(fees_list, list) and fees_list:
            total = 0.0
            for f in fees_list:
                if not isinstance(f, dict) or not f.get("cost"):
                    continue
                c = abs(float(f["cost"]))
                if f.get("currency", quote) != quote:
                    c *= ref_price
                total += c
            return total if total > 0 else fallback
        return fallback

    cost     = float(fee_info["cost"])
    currency = fee_info.get("currency", quote)
    if currency != quote:
        return abs(cost) * ref_price
    return abs(cost)
