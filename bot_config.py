"""bot_config.py — Configuração em tempo de execução com persistência JSON.

Permite que o usuário altere parâmetros via Telegram sem reiniciar o bot.
Os valores do .env servem apenas como padrão inicial.
"""
import json
import logging
from copy import deepcopy
from pathlib import Path

from config import (
    CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_PCT,
    DCA_DROP_PCT,
    MARTINGALE_LEVELS,
    MAX_DCA_ORDERS,
    ORDER_SIZE_USDT,
    PAIRS,
    REENTRY_DROP_PCT,
    RSI_ENABLED,
    RSI_PERIOD,
    RSI_THRESHOLD,
    STOP_LOSS_ENABLED,
    STOP_LOSS_PCT,
    TAKE_PROFIT_PCT,
    TRAILING_STOP_ENABLED,
    TRAILING_STOP_PCT,
    TREND_EMA_PERIOD,
    TREND_FILTER_ENABLED,
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
    "rsi_enabled"            : RSI_ENABLED,
    "rsi_threshold"          : RSI_THRESHOLD,
    "rsi_period"             : RSI_PERIOD,
    "stop_loss_enabled"      : STOP_LOSS_ENABLED,
    "stop_loss_pct"          : STOP_LOSS_PCT,
    "trend_filter_enabled"   : TREND_FILTER_ENABLED,
    "trend_ema_period"       : TREND_EMA_PERIOD,
    "circuit_breaker_enabled": CIRCUIT_BREAKER_ENABLED,
    "circuit_breaker_pct"    : CIRCUIT_BREAKER_PCT,
    "bot_running"            : False,          # inicia parado; usuario ativa pelo Telegram
    "active_pairs"           : list(PAIRS),    # subconjunto de todos os pares ativos
    "extra_pairs"            : list(PAIRS),    # todos os pares gerenciados — inicializado com .env
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
    "rsi_threshold"       : {"label": "RSI máximo para entrada",          "type": float, "min": 10.0, "max": 90.0},
    "rsi_period"          : {"label": "Período do RSI (candles)",         "type": int,   "min": 5,    "max": 50},
    "stop_loss_pct"       : {"label": "Stop Loss (%)",                    "type": float, "min": 1.0,  "max": 50.0},
    "trend_ema_period"    : {"label": "Período EMA tendência (dias)",     "type": int,   "min": 5,    "max": 200},
    "circuit_breaker_pct" : {"label": "Circuit Breaker — drawdown máx (%)", "type": float, "min": 1.0, "max": 50.0},
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
                # extra_pairs contém TODOS os pares gerenciados.
                # Garante que novos pares adicionados ao .env sejam incluídos automaticamente.
                extra = merged.get("extra_pairs", [])
                for p in PAIRS:
                    if p not in extra:
                        extra.append(p)
                merged["extra_pairs"] = extra or list(PAIRS)
                all_pairs = merged["extra_pairs"]
                merged["active_pairs"] = [
                    p for p in merged.get("active_pairs", all_pairs) if p in all_pairs
                ] or list(all_pairs)
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
        updates = {
            "strategy"              : key,
            "dca_drop_pct"          : p["dca_drop_pct"],
            "order_size_usdt"       : p["order_size_usdt"],
            "take_profit_pct"       : p["take_profit_pct"],
            "max_dca_orders"        : p["max_dca_orders"],
            "trailing_stop_enabled" : p["trailing_stop_enabled"],
            "trailing_stop_pct"     : p["trailing_stop_pct"],
            "martingale_levels"     : p["martingale_levels"],
        }
        # Campos Elder: aplicados apenas quando o preset os define explicitamente
        for field in (
            "stop_loss_enabled", "stop_loss_pct",
            "trend_filter_enabled", "trend_ema_period",
            "circuit_breaker_enabled", "circuit_breaker_pct",
            "rsi_enabled", "rsi_threshold", "rsi_period",
        ):
            if field in p:
                updates[field] = p[field]
        self._cfg.update(updates)
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
        """Retorna todos os pares gerenciados (extra_pairs unificado)."""
        return list(self._cfg.get("extra_pairs", PAIRS))

    def set_active_pairs(self, pairs: list[str]) -> None:
        all_pairs = self.get_all_pairs()
        self._cfg["active_pairs"] = [p for p in pairs if p in all_pairs]
        self._save()

    def toggle_rsi(self) -> bool:
        self._cfg["rsi_enabled"] = not self._cfg["rsi_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["rsi_enabled"]

    def toggle_stop_loss(self) -> bool:
        self._cfg["stop_loss_enabled"] = not self._cfg["stop_loss_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["stop_loss_enabled"]

    def toggle_trend_filter(self) -> bool:
        self._cfg["trend_filter_enabled"] = not self._cfg["trend_filter_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["trend_filter_enabled"]

    def toggle_circuit_breaker(self) -> bool:
        self._cfg["circuit_breaker_enabled"] = not self._cfg["circuit_breaker_enabled"]
        self._cfg["strategy"] = "personalizado"
        self._save()
        return self._cfg["circuit_breaker_enabled"]

    def add_extra_pair(self, symbol: str) -> tuple[bool, str]:
        symbol = symbol.upper().strip()
        if "/" not in symbol or len(symbol.split("/")) != 2:
            return False, "Formato inválido. Use: `SOL/USDT`"
        extra = self._cfg.get("extra_pairs", list(PAIRS))
        if symbol in extra:
            return False, f"`{symbol}` já está na lista de pares."
        extra.append(symbol)
        self._cfg["extra_pairs"] = extra
        active = self._cfg.get("active_pairs", list(extra))
        if symbol not in active:
            active.append(symbol)
        self._cfg["active_pairs"] = active
        self._save()
        return True, f"✅ Par `{symbol}` adicionado e ativado."

    def remove_extra_pair(self, symbol: str) -> tuple[bool, str]:
        extra = [p for p in self._cfg.get("extra_pairs", []) if p != symbol]
        if not extra:
            return False, "⚠️ Não é possível remover o único par do sistema."
        self._cfg["extra_pairs"] = extra
        active = [p for p in self._cfg.get("active_pairs", []) if p != symbol]
        self._cfg["active_pairs"] = active or [extra[0]]
        self._save()
        return True, f"Par `{symbol}` removido."
