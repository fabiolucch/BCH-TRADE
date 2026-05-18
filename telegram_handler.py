"""telegram_handler.py — Menu interativo, comandos e relatórios agendados."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot_config import CONFIG_FIELDS, BotConfig
from config import FEE_RATE, PAIRS, TELEGRAM_CHAT_ID, TELEGRAM_TOKEN, DAILY_REPORT_TIME
from strategies import PRESETS

if TYPE_CHECKING:
    from dca import DCAEngine
    from state import StateManager

logger = logging.getLogger("bot.telegram")


# ── Helpers de teclado ────────────────────────────────────────────────────────

def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)

def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(list(rows))


class TelegramHandler:
    def __init__(self, state: "StateManager", bot_config: BotConfig) -> None:
        self.state      = state
        self.bot_config = bot_config
        self.engine: "DCAEngine" = None
        self.app        = Application.builder().token(TELEGRAM_TOKEN).build()

        # Armazena qual campo o usuário está digitando: chat_id → (field, msg_id)
        self._awaiting: dict[int, tuple[str, int]] = {}

        self._register_handlers()

    def set_engine(self, engine: "DCAEngine") -> None:
        self.engine = engine

    # ── Registro ─────────────────────────────────────────────────────────────

    def _register_handlers(self) -> None:
        guard = self._only_owner

        self.app.add_handler(CommandHandler("start",  guard(self._cmd_menu)))
        self.app.add_handler(CommandHandler("menu",   guard(self._cmd_menu)))
        self.app.add_handler(CommandHandler("status", guard(self._cmd_status)))
        self.app.add_handler(CommandHandler("pnl",    guard(self._cmd_pnl)))
        self.app.add_handler(CommandHandler("close",  guard(self._cmd_close_text)))
        self.app.add_handler(CommandHandler("panic_sell", guard(self._cmd_close_text)))

        self.app.add_handler(CallbackQueryHandler(guard(self._on_callback)))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, guard(self._on_text_input))
        )

    def _only_owner(self, fn):
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            chat_id = (
                update.effective_chat.id
                if update.effective_chat
                else update.callback_query.message.chat_id
            )
            if str(chat_id) != str(TELEGRAM_CHAT_ID):
                if update.message:
                    await update.message.reply_text("⛔ Acesso não autorizado.")
                return
            await fn(update, ctx)
        return wrapper

    # ── Notificações proativas ────────────────────────────────────────────────

    async def send(self, text: str) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as exc:
            logger.error(f"Falha ao enviar mensagem Telegram: {exc}")

    # ── Menus (texto + teclado) ───────────────────────────────────────────────

    def _build_main_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        cfg            = self.bot_config.get()
        strategy_label = PRESETS.get(cfg["strategy"], {}).get("name", "Personalizado")
        open_pos       = [p for p in PAIRS if self.state.get_position(p)["is_active"]]
        active_pairs   = [p for p in cfg.get("active_pairs", PAIRS) if p in PAIRS] or list(PAIRS)
        running        = cfg.get("bot_running", False)

        text = (
            "🤖 *DCA Trading Bot*\n\n"
            f"{'🟢' if running else '🔴'} Status: *{'Rodando' if running else 'Parado'}*\n"
            f"📌 Estratégia: *{strategy_label}*\n"
            f"📊 Posições abertas: *{len(open_pos)}* de *{len(PAIRS)}*\n"
            f"🔁 Pares ativos: `{', '.join(active_pairs)}`"
        )
        toggle_label = "⏹ Parar Bot" if running else "▶️ Iniciar Bot"
        kb = _kb(
            [_btn("📊 Status",              "nav:status"),
             _btn("💰 PnL",                "nav:pnl")],
            [_btn("⚙️ Configurações",      "nav:config")],
            [_btn("📍 Pares Ativos",        "nav:pairs")],
            [_btn(toggle_label,             "toggle:running"),
             _btn("🔴 Fechar Posição",     "nav:close_menu")],
        )
        return text, kb

    def _build_pairs_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        cfg    = self.bot_config.get()
        active = set(cfg.get("active_pairs", PAIRS))
        text = (
            "📍 *Pares Ativos*\n\n"
            "Toque para ativar/desativar um par.\n"
            "_Pares desativados não abrem novas posições._\n"
            "_Posições já abertas continuam sendo gerenciadas._"
        )
        rows = [
            [_btn(f"{'✅' if p in active else '❌'} {p}", f"pair_toggle:{p.replace('/', '_')}")]
            for p in PAIRS
        ]
        rows.append([_btn("◀️ Voltar", "nav:main")])
        return text, _kb(*rows)

    def _build_config_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        cfg = self.bot_config.get()
        trail_icon = "✅" if cfg["trailing_stop_enabled"] else "❌"
        strategy_label = PRESETS.get(cfg["strategy"], {}).get("name", "Personalizado")
        levels = int(cfg.get("martingale_levels", 0))
        mart_line = (
            f"🎲 Martingale: `{levels} {'nível' if levels == 1 else 'níveis'}`\n"
            if levels > 0 else
            "🎲 Martingale: `OFF`\n"
        )
        reentry = cfg.get("reentry_drop_pct", 0.0)
        reentry_line = (
            f"🔁 Re-entrada: `{reentry}%` abaixo da saída\n"
            if reentry > 0 else
            "🔁 Re-entrada: `imediata (OFF)`\n"
        )
        text = (
            "⚙️ *Configurações*\n\n"
            f"📌 Estratégia atual: *{strategy_label}*\n"
            f"📉 Queda p/ DCA: `{cfg['dca_drop_pct']}%`\n"
            f"💵 Aporte inicial: `{cfg['order_size_usdt']} USDT`\n"
            f"🎯 Take Profit: `{cfg['take_profit_pct']}%`\n"
            f"🔢 Máx. aportes: `{cfg['max_dca_orders']}`\n"
            f"{mart_line}"
            f"{trail_icon} Trailing Stop: `{'ON' if cfg['trailing_stop_enabled'] else 'OFF'}`"
            + (f" — `{cfg['trailing_stop_pct']}%`\n" if cfg["trailing_stop_enabled"] else "\n")
            + reentry_line.rstrip("\n")
        )
        kb = _kb(
            [_btn("📋 Estratégias Prontas",  "nav:strategies"),
             _btn("🔧 Personalizar",         "nav:custom")],
            [_btn(f"{trail_icon} Trailing Stop", "toggle:trailing")],
            [_btn("◀️ Voltar",               "nav:main")],
        )
        return text, kb

    def _build_strategies_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        text = "📋 *Estratégias Prontas*\n\nEscolha uma estratégia para aplicar:"
        rows = []
        for key, p in PRESETS.items():
            rows.append([_btn(f"{p['emoji']} {p['name']}", f"preset:{key}")])
        rows.append([_btn("◀️ Voltar", "nav:config")])
        return text, _kb(*rows)

    def _build_strategy_detail(self, key: str) -> tuple[str, InlineKeyboardMarkup]:
        p      = PRESETS[key]
        cfg    = self.bot_config.get()
        trail  = "✅ Sim" if p["trailing_stop_enabled"] else "❌ Não"
        levels = int(p.get("martingale_levels", 0))
        if levels > 0:
            sizes = [p["order_size_usdt"] * min(n, levels) for n in range(1, levels + 2)]
            sizes_str = " → ".join(f"{s:.0f}" for s in sizes[:-1]) + f" → {sizes[-1]:.0f}…"
            mart_line = f"🎲 Martingale: `{levels} níveis` ({sizes_str} USDT)\n"
        else:
            mart_line = "🎲 Martingale: `OFF`\n"
        text = (
            f"{p['emoji']} *{p['name']}*\n\n"
            f"_{p['description']}_\n\n"
            f"📉 Queda p/ DCA: `{p['dca_drop_pct']}%`\n"
            f"💵 Aporte inicial: `{p['order_size_usdt']} USDT`\n"
            f"🎯 Take Profit: `{p['take_profit_pct']}%`\n"
            f"🔢 Máx. aportes: `{p['max_dca_orders']}`\n"
            f"{mart_line}"
            f"📈 Trailing Stop: {trail}"
            + (f" (`{p['trailing_stop_pct']}%`)" if p["trailing_stop_enabled"] else "")
        )
        active = cfg["strategy"] == key
        kb = _kb(
            [_btn("✅ Aplicar esta estratégia" if not active else "✔️ Já aplicada", f"apply:{key}")],
            [_btn("◀️ Voltar", "nav:strategies")],
        )
        return text, kb

    def _build_custom_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        cfg    = self.bot_config.get()
        levels = int(cfg.get("martingale_levels", 0))
        if levels > 0:
            sizes = " → ".join(
                str(int(cfg["order_size_usdt"] * min(n, levels)))
                for n in range(1, levels + 2)
            ) + "…"
            mart_val = f"`{levels} níveis` ({sizes} USDT)"
        else:
            mart_val = "`OFF`"
        reentry = cfg.get("reentry_drop_pct", 0.0)
        reentry_val = f"`{reentry}%` abaixo da saída" if reentry > 0 else "`OFF` (imediata)"
        text = (
            "🔧 *Configuração Personalizada*\n\n"
            "Toque em um parâmetro para alterar:\n\n"
            f"  📉 Queda p/ DCA: `{cfg['dca_drop_pct']}%`\n"
            f"  💵 Aporte inicial: `{cfg['order_size_usdt']} USDT`\n"
            f"  🎯 Take Profit: `{cfg['take_profit_pct']}%`\n"
            f"  🔢 Máx. aportes: `{cfg['max_dca_orders']}`\n"
            f"  🎲 Martingale: {mart_val}\n"
            f"  📈 Trailing Stop: `{cfg['trailing_stop_pct']}%`\n"
            f"  🔁 Re-entrada: {reentry_val}"
        )
        kb = _kb(
            [_btn("📉 Queda DCA",      "set:dca_drop_pct"),
             _btn("💵 Aporte inicial", "set:order_size_usdt")],
            [_btn("🎯 Take Profit",    "set:take_profit_pct"),
             _btn("🔢 Máx. aportes",  "set:max_dca_orders")],
            [_btn("🎲 Martingale",     "set:martingale_levels"),
             _btn("📈 Trailing %",     "set:trailing_stop_pct")],
            [_btn("🔁 Re-entrada %",   "set:reentry_drop_pct")],
            [_btn("◀️ Voltar",         "nav:config")],
        )
        return text, kb

    def _build_close_menu(self) -> tuple[str, InlineKeyboardMarkup]:
        active = [p for p in PAIRS if self.state.get_position(p)["is_active"]]
        if not active:
            text = "ℹ️ Nenhuma posição aberta no momento."
            kb   = _kb([_btn("◀️ Voltar", "nav:main")])
        else:
            text = "🔴 *Fechar Posição a Mercado*\n\nQual par deseja encerrar?"
            rows = [[_btn(f"🔴 {p}", f"close:{p.replace('/', '_')}")] for p in active]
            rows.append([_btn("◀️ Voltar", "nav:main")])
            kb   = _kb(*rows)
        return text, kb

    # ── Dispatcher de callbacks ───────────────────────────────────────────────

    async def _on_callback(self, update: Update, _ctx) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data

        action, _, param = data.partition(":")

        nav_map = {
            "main"       : self._build_main_menu,
            "config"     : self._build_config_menu,
            "strategies" : self._build_strategies_menu,
            "custom"     : self._build_custom_menu,
            "close_menu" : self._build_close_menu,
            "pairs"      : self._build_pairs_menu,
        }

        if action == "nav":
            if param == "status":
                await self._send_status(query=query)
                return
            if param == "pnl":
                text = self._build_pnl_report("PnL Atual")
                await query.edit_message_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_kb([_btn("◀️ Voltar", "nav:main")]),
                )
                return
            builder = nav_map.get(param)
            if builder:
                text, kb = builder()
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        elif action == "preset":
            text, kb = self._build_strategy_detail(param)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        elif action == "apply":
            self.bot_config.apply_preset(param)
            p    = PRESETS[param]
            text = f"✅ Estratégia *{p['emoji']} {p['name']}* aplicada com sucesso!"
            kb   = _kb([_btn("◀️ Configurações", "nav:config")])
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        elif action == "toggle":
            if param == "running":
                cfg     = self.bot_config.get()
                new_val = not cfg.get("bot_running", False)
                self.bot_config.set_running(new_val)
                if new_val:
                    active = [p for p in cfg.get("active_pairs", PAIRS) if p in PAIRS] or list(PAIRS)
                    strat  = PRESETS.get(cfg["strategy"], {}).get("name", "Personalizado")
                    await self.send(
                        f"▶️ *Bot iniciado*\n"
                        f"├ Estratégia: `{strat}`\n"
                        f"└ Pares: `{', '.join(active)}`"
                    )
                else:
                    await self.send(
                        "⏹ *Bot parado.*\n"
                        "_Posições abertas continuam sendo monitoradas para TP e trailing._"
                    )
                text, kb = self._build_main_menu()
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

            elif param == "trailing":
                enabled = self.bot_config.toggle_trailing()
                state_txt = "✅ ativado" if enabled else "❌ desativado"
                await query.answer(f"Trailing Stop {state_txt}!", show_alert=True)
                text, kb = self._build_config_menu()
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

        elif action == "pair_toggle":
            pair = param.replace("_", "/")
            if pair not in PAIRS:
                return
            cfg    = self.bot_config.get()
            active = set(p for p in cfg.get("active_pairs", PAIRS) if p in PAIRS)
            if pair in active:
                if len(active) <= 1:
                    await query.answer("⚠️ Mantenha pelo menos um par ativo!", show_alert=True)
                    return
                active.discard(pair)
            else:
                active.add(pair)
            self.bot_config.set_active_pairs(list(active))
            text, kb = self._build_pairs_menu()
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return

        elif action == "set":
            meta = CONFIG_FIELDS.get(param, {})
            label = meta.get("label", param)
            min_v = meta.get("min", 0)
            max_v = meta.get("max", 999)
            prompt = (
                f"✏️ *{label}*\n\n"
                f"Intervalo permitido: `{min_v}` a `{max_v}`\n"
                f"Digite o novo valor:"
            )
            msg = await query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN)
            self._awaiting[query.message.chat_id] = (param, msg.message_id)

        elif action == "close":
            pair = param.replace("_", "/")
            await query.edit_message_text(
                f"⏳ Fechando *{pair}* a mercado…", parse_mode=ParseMode.MARKDOWN
            )
            result = await self.engine.force_close(pair)
            await query.edit_message_text(
                result,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=_kb([_btn("◀️ Menu", "nav:main")]),
            )

    # ── Input de texto (configuração personalizada) ───────────────────────────

    async def _on_text_input(self, update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        pending = self._awaiting.pop(chat_id, None)
        if not pending:
            return

        field, msg_id = pending
        ok, feedback  = self.bot_config.set_field(field, update.message.text.strip())

        await update.message.delete()

        text, kb = (
            (feedback + "\n\n" + self._build_custom_menu()[0], self._build_custom_menu()[1])
            if ok
            else (f"⚠️ {feedback}\n\nTente novamente:", _kb([_btn("◀️ Cancelar", "nav:custom")]))
        )

        try:
            await self.app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb,
            )
        except Exception:
            await update.message.reply_text(feedback, parse_mode=ParseMode.MARKDOWN)

    # ── Comandos de texto ─────────────────────────────────────────────────────

    async def _cmd_menu(self, update: Update, _ctx) -> None:
        text, kb = self._build_main_menu()
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    async def _cmd_status(self, update: Update, _ctx) -> None:
        await self._send_status(message=update.message)

    async def _cmd_pnl(self, update: Update, _ctx) -> None:
        await update.message.reply_text(
            self._build_pnl_report("Relatório de PnL"), parse_mode=ParseMode.MARKDOWN
        )

    async def _cmd_close_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await update.message.reply_text("Uso: `/close BTC/USDT`", parse_mode=ParseMode.MARKDOWN)
            return
        pair = ctx.args[0].upper().replace("-", "/")
        if pair not in PAIRS:
            await update.message.reply_text(f"Par `{pair}` não monitorado.", parse_mode=ParseMode.MARKDOWN)
            return
        await update.message.reply_text(f"⏳ Fechando *{pair}*…", parse_mode=ParseMode.MARKDOWN)
        result = await self.engine.force_close(pair)
        await update.message.reply_text(result, parse_mode=ParseMode.MARKDOWN)

    # ── Status ────────────────────────────────────────────────────────────────

    async def _send_status(self, message=None, query=None) -> None:
        lines = ["*📊 Status das Posições*\n"]
        cfg   = self.bot_config.get()

        reentry_drop = cfg.get("reentry_drop_pct", 0.0)

        for pair in PAIRS:
            pos = self.state.get_position(pair)
            if not pos["is_active"]:
                last_exit = pos.get("last_exit_price", 0.0)
                if last_exit > 0 and reentry_drop > 0:
                    threshold = last_exit * (1 - reentry_drop / 100)
                    lines.append(
                        f"• {pair}: _aguardando re-entrada_\n"
                        f"  Saída: `{last_exit:.4f}` | Limiar: `{threshold:.4f}` (-{reentry_drop}%)"
                    )
                else:
                    lines.append(f"• {pair}: _sem posição_")
                continue
            try:
                price         = await self.engine.exchange.get_price(pair)
                pnl_gross     = (price - pos["avg_price"]) * pos["total_qty"]
                pnl_pct       = (price / pos["avg_price"] - 1) * 100
                est_sell_fee  = price * pos["total_qty"] * FEE_RATE
                pnl_net_est   = pnl_gross - pos["total_fees_usdt"] - est_sell_fee
                tp_price      = pos["avg_price"] * (1 + cfg["take_profit_pct"] / 100)
                emoji         = "🟢" if pnl_net_est >= 0 else "🔴"
                quote         = pair.split("/")[1]

                trail_line = ""
                if pos["trailing_active"]:
                    trail_line = f"\n  🎯 Trail ativo: stop=`{pos['trailing_stop_price']:.4f}` | pico=`{pos['peak_price']:.4f}`"

                lines.append(
                    f"{emoji} *{pair}*\n"
                    f"  Aportes: {pos['order_count']} | Qty: `{pos['total_qty']:.6f}`\n"
                    f"  Avg: `{pos['avg_price']:.4f}` | Atual: `{price:.4f}`\n"
                    f"  PnL bruto: `{pnl_gross:+.2f} {quote}` ({pnl_pct:+.2f}%)\n"
                    f"  PnL líq. est.: `{pnl_net_est:+.2f} {quote}`\n"
                    f"  Alvo TP: `{tp_price:.4f}`"
                    f"{trail_line}"
                )
            except Exception as exc:
                lines.append(f"• {pair}: ⚠️ {exc}")

        text = "\n".join(lines)
        kb   = _kb([_btn("🔄 Atualizar", "nav:status"), _btn("◀️ Menu", "nav:main")])

        if query:
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        elif message:
            await message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

    # ── Relatório PnL ─────────────────────────────────────────────────────────

    def _build_pnl_report(self, title: str) -> str:
        now     = datetime.now(timezone.utc)
        history = self.state.get_trade_history()

        today_t = [t for t in history if t["closed_at"][:10] == now.strftime("%Y-%m-%d")]
        month_t = [t for t in history if t["closed_at"][:7]  == now.strftime("%Y-%m")]

        def summarize(trades: list, label: str) -> str:
            if not trades:
                return f"*{label}:* Nenhuma operação finalizada."
            total_net  = sum(t["pnl_usdt"] for t in trades)
            total_fees = sum(t.get("fees_usdt", 0.0) for t in trades)
            emoji = "🟢" if total_net >= 0 else "🔴"
            lines = [
                f"*{label}:* {emoji} `{total_net:+.2f}` USDT líq. "
                f"| Taxas: `{total_fees:.4f}` USDT ({len(trades)} ops)"
            ]
            for t in trades:
                e = "🟢" if t["pnl_usdt"] >= 0 else "🔴"
                fee_str = f" | taxa `{t.get('fees_usdt', 0):.4f}`" if t.get("fees_usdt") else ""
                lines.append(
                    f"  {e} {t['pair']}: líq. `{t['pnl_usdt']:+.2f}` ({t['pnl_pct']:+.2f}%){fee_str}"
                )
            return "\n".join(lines)

        cfg  = self.bot_config.get()
        open_lines = []
        for pair in PAIRS:
            pos = self.state.get_position(pair)
            if pos["is_active"]:
                quote = pair.split("/")[1]
                open_lines.append(
                    f"  • {pair}: {pos['order_count']} aportes | "
                    f"`{pos['total_cost']:.2f} {quote}` | avg `{pos['avg_price']:.4f}`"
                )

        parts = [
            f"*📈 {title}*",
            f"_{now.strftime('%d/%m/%Y %H:%M')} UTC_\n",
            summarize(today_t, "Hoje"),
            "",
            summarize(month_t, "Este mês"),
            "",
            "*Posições abertas:*",
        ]
        parts += open_lines or ["  Nenhuma posição aberta."]
        return "\n".join(parts)

    # ── Schedulers ────────────────────────────────────────────────────────────

    async def schedule_daily_report(self) -> None:
        try:
            h, m = map(int, DAILY_REPORT_TIME.split(":"))
        except (ValueError, AttributeError):
            logger.warning(f"DAILY_REPORT_TIME inválido ('{DAILY_REPORT_TIME}'). Usando 00:00.")
            h, m = 0, 0
        while True:
            now    = datetime.now(timezone.utc)
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            await self.send(self._build_pnl_report("Relatório Diário"))

    async def schedule_monthly_report(self) -> None:
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
        try:
            await self.app.updater.stop()
        except Exception:
            pass
        try:
            await self.app.stop()
        except Exception:
            pass
        try:
            await self.app.shutdown()
        except Exception:
            pass
        logger.info("Telegram bot encerrado.")
