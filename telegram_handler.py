"""telegram_handler.py — Comandos interativos e relatórios agendados via Telegram."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    DAILY_REPORT_TIME,
    PAIRS,
    TAKE_PROFIT_PCT,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
)

if TYPE_CHECKING:
    from dca import DCAEngine
    from state import StateManager

logger = logging.getLogger("bot.telegram")


class TelegramHandler:
    def __init__(self, state: "StateManager") -> None:
        self.state  = state
        self.engine: "DCAEngine" = None  # injetado via set_engine após construção
        self.app    = Application.builder().token(TELEGRAM_TOKEN).build()
        self._register_handlers()

    def set_engine(self, engine: "DCAEngine") -> None:
        self.engine = engine

    # ── Registro de comandos ──────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        cmds = [
            ("start",      self._cmd_start),
            ("help",       self._cmd_help),
            ("status",     self._cmd_status),
            ("pnl",        self._cmd_pnl),
            ("close",      self._cmd_close),
            ("panic_sell", self._cmd_close),  # alias
        ]
        for name, handler in cmds:
            self.app.add_handler(CommandHandler(name, self._only_owner(handler)))

    def _only_owner(self, fn):
        """Decorator que rejeita mensagens de chats não autorizados."""
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
                await update.message.reply_text("⛔ Acesso não autorizado.")
                return
            await fn(update, ctx)
        return wrapper

    # ── Envio de notificações ─────────────────────────────────────────────────

    async def send(self, text: str) -> None:
        """Envia mensagem proativa ao chat configurado (notificações do bot)."""
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error(f"Falha ao enviar mensagem Telegram: {exc}")

    # ── Comandos ──────────────────────────────────────────────────────────────

    async def _cmd_start(self, update: Update, _ctx) -> None:
        await update.message.reply_text(
            "🤖 *DCA Bot ativo!*\nUse /help para ver os comandos disponíveis.",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_help(self, update: Update, _ctx) -> None:
        await update.message.reply_text(
            "*Comandos disponíveis:*\n\n"
            "/status — Posições abertas e PnL não realizado\n"
            "/pnl — Relatório de PnL do dia e do mês\n"
            "/close `PAR` — Fecha posição a mercado (ex: `/close BTC/USDT`)\n"
            "/panic\\_sell `PAR` — Alias de /close",
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_status(self, update: Update, _ctx) -> None:
        lines = ["*📊 Status das Posições*\n"]
        for pair in PAIRS:
            pos = self.state.get_position(pair)
            if not pos["is_active"]:
                lines.append(f"• {pair}: _sem posição aberta_")
                continue
            try:
                price    = await self.engine.exchange.get_price(pair)
                pnl_u    = (price - pos["avg_price"]) * pos["total_qty"]
                pnl_pct  = (price / pos["avg_price"] - 1) * 100
                tp_price = pos["avg_price"] * (1 + TAKE_PROFIT_PCT / 100)
                emoji    = "🟢" if pnl_pct >= 0 else "🔴"
                quote    = pair.split("/")[1]
                lines.append(
                    f"{emoji} *{pair}*\n"
                    f"  Aportes: {pos['order_count']} | Qty: `{pos['total_qty']:.6f}`\n"
                    f"  Avg: `{pos['avg_price']:.4f}` | Atual: `{price:.4f}`\n"
                    f"  PnL: `{pnl_u:+.2f} {quote} ({pnl_pct:+.2f}%)`\n"
                    f"  Alvo TP: `{tp_price:.4f}`"
                )
            except Exception as exc:
                lines.append(f"• {pair}: ⚠️ erro ao buscar preço ({exc})")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

    async def _cmd_pnl(self, update: Update, _ctx) -> None:
        await update.message.reply_text(
            self._build_pnl_report("Relatório de PnL"),
            parse_mode=ParseMode.MARKDOWN,
        )

    async def _cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text("Uso: `/close BTC/USDT`", parse_mode=ParseMode.MARKDOWN)
            return
        pair = ctx.args[0].upper().replace("-", "/")
        if pair not in PAIRS:
            await update.message.reply_text(
                f"Par `{pair}` não está na lista monitorada.\n"
                f"Pares ativos: {', '.join(PAIRS)}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await update.message.reply_text(f"⏳ Fechando posição {pair}…")
        result = await self.engine.force_close(pair)
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)

    # ── Relatórios ────────────────────────────────────────────────────────────

    def _build_pnl_report(self, title: str) -> str:
        now     = datetime.now(timezone.utc)
        history = self.state.get_trade_history()

        today_trades = [t for t in history if t["closed_at"][:10] == now.strftime("%Y-%m-%d")]
        month_trades = [t for t in history if t["closed_at"][:7]  == now.strftime("%Y-%m")]

        def summarize(trades: list, label: str) -> str:
            if not trades:
                return f"*{label}:* Nenhuma operação finalizada."
            total = sum(t["pnl_usdt"] for t in trades)
            emoji = "🟢" if total >= 0 else "🔴"
            lines = [f"*{label}:* {emoji} PnL total: `{total:+.2f}` ({len(trades)} ops)"]
            for t in trades:
                e = "🟢" if t["pnl_usdt"] >= 0 else "🔴"
                lines.append(
                    f"  {e} {t['pair']}: `{t['pnl_usdt']:+.2f}` ({t['pnl_pct']:+.2f}%)"
                )
            return "\n".join(lines)

        open_lines = []
        for pair in PAIRS:
            pos = self.state.get_position(pair)
            if pos["is_active"]:
                quote = pair.split("/")[1]
                open_lines.append(
                    f"  • {pair}: {pos['order_count']} aportes | "
                    f"`{pos['total_cost']:.2f} {quote}` investidos | "
                    f"avg `{pos['avg_price']:.4f}`"
                )

        parts = [
            f"*📈 {title}*",
            f"_{now.strftime('%d/%m/%Y %H:%M')} UTC_\n",
            summarize(today_trades, "Hoje"),
            "",
            summarize(month_trades, "Este mês"),
            "",
            "*Posições abertas:*",
        ]
        parts += open_lines if open_lines else ["  Nenhuma posição aberta."]
        return "\n".join(parts)

    # ── Schedulers ────────────────────────────────────────────────────────────

    async def schedule_daily_report(self) -> None:
        """Envia relatório diário no horário DAILY_REPORT_TIME (UTC)."""
        h, m = map(int, DAILY_REPORT_TIME.split(":"))
        while True:
            now    = datetime.now(timezone.utc)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self.send(self._build_pnl_report("Relatório Diário"))

    async def schedule_monthly_report(self) -> None:
        """Envia relatório mensal no 1º dia do mês à meia-noite UTC."""
        while True:
            now = datetime.now(timezone.utc)
            if now.month == 12:
                target = now.replace(year=now.year + 1, month=1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
            else:
                target = now.replace(month=now.month + 1, day=1,
                                     hour=0, minute=0, second=0, microsecond=0)
            await asyncio.sleep((target - now).total_seconds())
            await self.send(self._build_pnl_report("Relatório Mensal"))

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot iniciado (polling ativo).")

    async def stop(self) -> None:
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        logger.info("Telegram bot encerrado.")
