"""bot_config.py — Configuração em tempo de execução com persistência JSON.

Permite que o usuário altere parâmetros via Telegram sem reiniciar o bot.
Os valores do .env servem apenas como padrão inicial.
"""
import json
import logging
from copy import deepcopy
from pathlib import Path

from config import (
    DCA_DROP_PCT,
    MARTINGALE_LEVELS,
    MAX_DCA_ORDERS,
    ORDER_SIZE_USDT,
    PAIRS,
    REENTRY_DROP_PCT,
    RSI_ENABLED,
    RSI_PERIOD,
    RSI_THRESHOLD,
    TAKE_PROFIT_PCT,
    TRAILING_STOP_ENABLED,
    TRAILING_STOP_PCT,
)

logger = logging.getLogger("bot.config_mgr")

CONFIG_FILE = Path("bot_config.json")

_DEFAULTS: dict = {
    "strategy"              : "personalizado",
    "dca_drop_pct"          : DCA_DROP_PCT,
    "order_size_usdt"       : ORDER_SIZE_USDT,
    "take_profit_pct"       : TAKE_PROFIT_PCT,
    "max_dca_orders"        : MAX_DCA_ORDERS,
    "trailing_stop_enabled" : TRAILING_STOP_ENABLED,
    "trailing_stop_pct"     : TRAILING_STOP_PCT,
    "martingale_levels"     : MARTINGALE_LEVELS,
    "reentry_drop_pct"      : REENTRY_DROP_PCT,
    "rsi_enabled"           : RSI_ENABLED,
    "rsi_threshold"         : RSI_THRESHOLD,
    "rsi_period"            : RSI_PERIOD,
    "bot_running"           : False,          # inicia parado; usuario ativa pelo Telegram
    "active_pairs"          : list(PAIRS),    # subconjunto de todos os pares ativos
    "extra_pairs"           : [],             # pares adicionados via Telegram (fora do .env)
}

# Metadados dos campos configuráveis pelo usuário
CONFIG_FIELDS: dict[str, dict] = {
    "dca_drop_pct"      : {"label": "Queda para DCA (%)",             "type": float, "min": 0.1, "max": 50.0},
    "order_size_usdt"   : {"label": "Valor inicial do aporte (USDT)", "type": float, "min": 1.0, "max": 100_000.0},
    "take_profit_pct"   : {"label": "Take Profit (%)",                "type": float, "min": 0.1, "max": 100.0},
    "max_dca_orders"    : {"label": "Máximo de aportes",              "type": int,   "min": 1,   "max": 100},
    "trailing_stop_pct" : {"label": "Trailing Stop (%)",              "type": float, "min": 0.1, "max": 20.0},
    "martingale_levels" : {"label": "Níveis de Martingale (0-3)",     "type": int,   "min": 0,   "max": 3},
    "reentry_drop_pct"  : {"label": "Queda p/ re-entrada (%)",        "type": float, "min": 0.0, "max": 20.0},
    "rsi_threshold"     : {"label": "RSI máximo para entrada",        "type": float, "min": 10.0, "max": 90.0},
    "rsi_period"        : {"label": "Período do RSI (candles)",       "type": int,   "min": 5,    "max": 50},
}


class BotConfig:
    """Gerenciador de configuração dinâmica — leitura é thread-safe (asyncio)."""

    def __init__(self) -> None:
        self._cfg: dict = self._load()

    def _load(self) -> dict:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                merged = deepcopy(_DEFAULTS)
                merged.update(saved)
                # extra_pairs = pares adicionados via Telegram fora do .env
                extra = [p for p in merged.get("extra_pairs", []) if p not in PAIRS]
                merged["extra_pairs"] = extra
                all_pairs = list(PAIRS) + extra
                # active_pairs deve ser subconjunto de todos os pares conhecidos
                merged["active_pairs"] = [
                    p for p in merged.get("active_pairs", all_pairs) if p in all_pairs
                ] or all_pairs
                logger.info(
                    f"Configuração carregada: estratégia='{merged['strategy']}' | "
                    f"bot={'rodando' if merged['bot_running'] else 'parado'}"
                )
                return merged
            except Exception as exc:
                logger.warning(f"Erro ao ler {CONFIG_FILE} ({exc}). Usando padrões do .env.")
        return deepcopy(_DEFAULTS)

    def _save(self) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error(f"Falha ao salvar {CONFIG_FILE}: {exc}")

    def get(self) -> dict:
        return deepcopy(self._cfg)

    def apply_preset(self, key: str) -> dict:
        from strategies import PRESETS
        p = PRESETS[key]
        self._cfg.update({
            "strategy"              : key,
            "dca_drop_pct"          : p["dca_drop_pct"],
            "order_size_usdt"       : p["order_size_usdt"],
            "take_profit_pct"       : p["take_profit_pct"],
            "max_dca_orders"        : p["max_dca_orders"],
            "trailing_stop_enabled" : p["trailing_stop_enabled"],
            "trailing_stop_pct"     : p["trailing_stop_pct"],
            "martingale_levels"     : p["martingale_levels"],
        })
        self._save()
        logger.info(f"Estratégia aplicada: {key}")
        return self.get()

    def set_field(self, field: str, raw_value: str) -> tuple[bool, str]:
        """Valida e persiste um campo. Retorna (sucesso, mensagem)."""
        meta = CONFIG_FIELDS.get(field)
        if not meta:
            return False, f"Campo '{field}' desconhecido."
        try:
            value = meta["type"](raw_value.replace(",", "."))
        except ValueError:
            return False, "Valor inválido. Digite um número (ex: `3.5`)."

        if not (meta["min"] <= value <= meta["max"]):
            return False, (
                f"Valor fora do intervalo permitido: "
                f"`{meta['min']}` a `{meta['max']}`."
            )
        self._cfg[field]      = value
        self._cfg["strategy"] = "personalizado"
        self._save()
        return True, f"✅ *{meta['label']}* atualizado para `{value}`."

    def toggle_trailing(self) -> bool:
        self._cfg["trailing_stop_enabled"] = not self._cfg["trailing_stop_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["trailing_stop_enabled"]

    def set_running(self, value: bool) -> None:
        self._cfg["bot_running"] = value
        self._save()

    def get_all_pairs(self) -> list[str]:
        """Retorna PAIRS do .env + pares extras adicionados via Telegram (ordem estável)."""
        extra = [p for p in self._cfg.get("extra_pairs", []) if p not in PAIRS]
        return list(PAIRS) + extra

    def set_active_pairs(self, pairs: list[str]) -> None:
        all_pairs = self.get_all_pairs()
        self._cfg["active_pairs"] = [p for p in pairs if p in all_pairs]
        self._save()

    def toggle_rsi(self) -> bool:
        self._cfg["rsi_enabled"] = not self._cfg["rsi_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["rsi_enabled"]

    def add_extra_pair(self, symbol: str) -> tuple[bool, str]:
        symbol = symbol.upper().strip()
        if "/" not in symbol or len(symbol.split("/")) != 2:
            return False, "Formato inválido. Use: `SOL/USDT`"
        if symbol in PAIRS:
            return False, f"`{symbol}` já está nos pares do `.env`."
        extra = self._cfg.get("extra_pairs", [])
        if symbol in extra:
            return False, f"`{symbol}` já foi adicionado."
        extra.append(symbol)
        self._cfg["extra_pairs"] = extra
        active = self._cfg.get("active_pairs", list(PAIRS))
        if symbol not in active:
            active.append(symbol)
        self._cfg["active_pairs"] = active
        self._save()
        return True, f"✅ Par `{symbol}` adicionado e ativado."

    def remove_extra_pair(self, symbol: str) -> None:
        self._cfg["extra_pairs"] = [p for p in self._cfg.get("extra_pairs", []) if p != symbol]
        self._cfg["active_pairs"] = [p for p in self._cfg.get("active_pairs", []) if p != symbol]
        self._save()
