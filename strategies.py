"""strategies.py — Estratégias DCA pré-configuradas por especialistas."""
from typing import TypedDict


class StrategyParams(TypedDict):
    name: str
    emoji: str
    description: str
    dca_drop_pct: float
    order_size_usdt: float
    take_profit_pct: float
    max_dca_orders: int
    trailing_stop_enabled: bool
    trailing_stop_pct: float
    martingale_levels: int


PRESETS: dict[str, StrategyParams] = {
    "conservador": {
        "name"                 : "Conservador",
        "emoji"                : "🛡️",
        "description"          : (
            "Compra apenas em quedas expressivas (5%). TP em 2.5% sem trailing.\n"
            "Menor exposição, operações espaçadas. Ideal para quem quer dormir tranquilo."
        ),
        "dca_drop_pct"         : 5.0,
        "order_size_usdt"      : 20.0,
        "take_profit_pct"      : 2.5,
        "max_dca_orders"       : 5,
        "trailing_stop_enabled": False,
        "trailing_stop_pct"    : 1.0,
        "martingale_levels"    : 0,
    },
    "moderado": {
        "name"                 : "Moderado",
        "emoji"                : "⚖️",
        "description"          : (
            "Entradas a cada 3%, trailing stop de 0.8% protege os lucros.\n"
            "Equilíbrio entre frequência e segurança. Recomendado para a maioria."
        ),
        "dca_drop_pct"         : 3.0,
        "order_size_usdt"      : 30.0,
        "take_profit_pct"      : 1.5,
        "max_dca_orders"       : 8,
        "trailing_stop_enabled": True,
        "trailing_stop_pct"    : 0.8,
        "martingale_levels"    : 0,
    },
    "agressivo": {
        "name"                 : "Agressivo",
        "emoji"                : "🔥",
        "description"          : (
            "Muitas entradas em quedas de 2%, trailing apertado de 0.5%.\n"
            "Captura máximo de lucro em tendências. Alto risco — exige saldo robusto."
        ),
        "dca_drop_pct"         : 2.0,
        "order_size_usdt"      : 25.0,
        "take_profit_pct"      : 1.0,
        "max_dca_orders"       : 15,
        "trailing_stop_enabled": True,
        "trailing_stop_pct"    : 0.5,
        "martingale_levels"    : 0,
    },
    "hodl": {
        "name"                 : "HODL DCA",
        "emoji"                : "📈",
        "description"          : (
            "Aportes espaçados em quedas de 8%. TP generoso de 5% com trailing de 2%.\n"
            "Estratégia de acumulação de longo prazo. Poucas operações, alto impacto."
        ),
        "dca_drop_pct"         : 8.0,
        "order_size_usdt"      : 50.0,
        "take_profit_pct"      : 5.0,
        "max_dca_orders"       : 5,
        "trailing_stop_enabled": True,
        "trailing_stop_pct"    : 2.0,
        "martingale_levels"    : 0,
    },
    "scalper": {
        "name"                 : "Scalper DCA",
        "emoji"                : "⚡",
        "description"          : (
            "Margens pequenas (0.8%) com entradas a cada 1.5%. Alta frequência.\n"
            "Para mercados lateralizados e voláteis. Sem trailing para saída rápida."
        ),
        "dca_drop_pct"         : 1.5,
        "order_size_usdt"      : 15.0,
        "take_profit_pct"      : 0.8,
        "max_dca_orders"       : 20,
        "trailing_stop_enabled": False,
        "trailing_stop_pct"    : 0.3,
        "martingale_levels"    : 0,
    },
    "martingale": {
        "name"                 : "DCA Martingale",
        "emoji"                : "🎲",
        "description"          : (
            "Queda de 2%, TP 2.5% com trailing 0.4%. Aportes crescem linearmente:\n"
            "20 → 40 → 60 → 60 USDT. Recupera posição mais rápido após quedas."
        ),
        "dca_drop_pct"         : 2.0,
        "order_size_usdt"      : 20.0,
        "take_profit_pct"      : 2.5,
        "max_dca_orders"       : 8,
        "trailing_stop_enabled": True,
        "trailing_stop_pct"    : 0.4,
        "martingale_levels"    : 3,
    },
}
