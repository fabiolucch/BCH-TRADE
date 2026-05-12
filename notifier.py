import logging
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("bot")


def telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def telegram_status() -> str:
    if not TELEGRAM_BOT_TOKEN and not TELEGRAM_CHAT_ID:
        return "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID ausentes"
    if not TELEGRAM_BOT_TOKEN:
        return "TELEGRAM_BOT_TOKEN ausente"
    if not TELEGRAM_CHAT_ID:
        return "TELEGRAM_CHAT_ID ausente"
    return "ok"


def send_telegram(message: str) -> bool:
    if not telegram_enabled():
        logger.warning("Telegram desabilitado: %s.", telegram_status())
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.ok:
            return True
        logger.warning("Falha Telegram (%s): %s", resp.status_code, resp.text[:250])
        return False
    except Exception as exc:
        logger.warning("Erro ao enviar Telegram: %s", exc)
        return False


def test_telegram() -> bool:
    """Envia uma mensagem curta de teste para validar token/chat_id e conectividade."""
    return send_telegram("✅ Teste Telegram: integração ativa no VPS.")
