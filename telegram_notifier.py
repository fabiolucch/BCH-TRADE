"""
telegram_notifier.py — Envia notificações formatadas para o Telegram.
"""
import logging
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("bot")

def send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning(f"Falha ao enviar notificação Telegram: {exc}")

def notify_bot_start(symbols: list, balance: float) -> None:
    syms = " | ".join(symbols)
    send(
        f"🤖 <b>Bot iniciado com sucesso!</b>\n"
        f"💰 Saldo Atual: <b>{balance:.2f} USDT</b>\n"
        f"🎯 Pares: <code>{syms}</code>"
    )

def notify_daily_summary(balance: float, monthly_pnl: float, open_count: int) -> None:
    icon = "📈" if monthly_pnl >= 0 else "📉"
    send(
        f"📊 <b>Resumo Gerencial Diário</b>\n\n"
        f"💰 Saldo Disponível: <b>{balance:.2f} USDT</b>\n"
        f"{icon} Lucro do Mês: <b>{monthly_pnl:+.2f} USDT</b>\n"
        f"⚙️ Posições Abertas: <b>{open_count}</b>"
    )

def notify_signal(symbol: str) -> None:
    send(f"📡 <b>Sinal detectado!</b>\nPar: <code>{symbol}</code>\nExecutando...")

def notify_position_opened(symbol: str, entry: float, sl: float, tp: float, qty: float) -> None:
    risk_pct = ((entry - sl) / entry) * 100
    reward_pct = ((tp - entry) / entry) * 100
    send(
        f"🟢 <b>Posição Aberta</b>\n"
        f"Par: <code>{symbol}</code>\n"
        f"Entry: <b>{entry:.4f}</b>\n"
        f"SL: {sl:.4f} (-{risk_pct:.1f}%)\n"
        f"TP: {tp:.4f} (+{reward_pct:.1f}%)\n"
        f"Qty: {qty:.6f}"
    )

def notify_trailing_activated(symbol: str, highest: float, trail_sl: float) -> None:
    send(
        f"🟡 <b>Trailing Stop ATIVADO</b>\n"
        f"Par: <code>{symbol}</code>\n"
        f"Máxima: {highest:.4f}\n"
        f"Trail SL: {trail_sl:.4f}"
    )

def notify_trailing_updated(symbol: str, trail_sl: float) -> None:
    send(f"🟡 Trail SL atualizado | <code>{symbol}</code> → <b>{trail_sl:.4f}</b>")

def notify_position_closed(symbol: str, reason: str, entry: float, exit_price: float, pnl_usdt: float, pnl_pct: float, r_multiple: float) -> None:
    icon = "✅" if pnl_usdt >= 0 else "❌"
    send(
        f"{icon} <b>Fechado — {reason}</b>\n"
        f"Par: <code>{symbol}</code>\n"
        f"Entry: {entry:.4f}\n"
        f"Exit: {exit_price:.4f}\n"
        f"PnL: <b>{pnl_usdt:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
        f"R múlt: {r_multiple:.2f}R"
    )