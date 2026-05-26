"""backtest.py — Simulação histórica das estratégias DCA com dados da OKX.

Uso:
  python backtest.py                                          # todos os presets, BTC+ETH+SOL, 180 dias
  python backtest.py --pairs BTC/USDT,ETH/USDT,SOL/USDT     # pares específicos
  python backtest.py --strategy triple_screen                 # uma estratégia
  python backtest.py --strategy hibrido_otimizado,triple_screen  # comparação direta
  python backtest.py --days 365                               # período em dias
  python backtest.py --no-chart                               # só tabela, sem imagens
  python backtest.py --output resultados/                     # pasta para salvar gráficos
"""

import argparse
import sys
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ── Imports externos ──────────────────────────────────────────────────────────

try:
    import ccxt
except ImportError:
    print("❌ ccxt não instalado. Execute: pip install ccxt")
    sys.exit(1)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MPL = True
except ImportError:
    HAS_MPL = False
    print("⚠️  matplotlib não encontrado — gráficos desativados. pip install matplotlib")

# ── Imports do bot (opcional) ─────────────────────────────────────────────────

try:
    from strategies import PRESETS
    from config import FEE_RATE
except ImportError:
    print("⚠️  Executando fora do diretório do bot — usando configurações embutidas.")
    FEE_RATE = 0.001
    PRESETS  = {}

# ── Presets embutidos (fallback se strategies.py não disponível) ───────────────

_BUILTIN_PRESETS = {
    "conservador": {
        "name": "Conservador", "emoji": "🛡️",
        "dca_drop_pct": 5.0, "order_size_usdt": 20.0, "take_profit_pct": 2.5,
        "max_dca_orders": 5, "trailing_stop_enabled": False, "trailing_stop_pct": 1.0,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "moderado": {
        "name": "Moderado", "emoji": "⚖️",
        "dca_drop_pct": 3.0, "order_size_usdt": 30.0, "take_profit_pct": 1.5,
        "max_dca_orders": 8, "trailing_stop_enabled": True, "trailing_stop_pct": 0.8,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "agressivo": {
        "name": "Agressivo", "emoji": "🔥",
        "dca_drop_pct": 2.0, "order_size_usdt": 25.0, "take_profit_pct": 1.0,
        "max_dca_orders": 15, "trailing_stop_enabled": True, "trailing_stop_pct": 0.5,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "hodl": {
        "name": "HODL DCA", "emoji": "📈",
        "dca_drop_pct": 8.0, "order_size_usdt": 50.0, "take_profit_pct": 5.0,
        "max_dca_orders": 5, "trailing_stop_enabled": True, "trailing_stop_pct": 2.0,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "scalper": {
        "name": "Scalper DCA", "emoji": "⚡",
        "dca_drop_pct": 1.5, "order_size_usdt": 15.0, "take_profit_pct": 0.8,
        "max_dca_orders": 20, "trailing_stop_enabled": False, "trailing_stop_pct": 0.3,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "martingale": {
        "name": "DCA Martingale", "emoji": "🎲",
        "dca_drop_pct": 2.0, "order_size_usdt": 20.0, "take_profit_pct": 2.5,
        "max_dca_orders": 8, "trailing_stop_enabled": True, "trailing_stop_pct": 0.4,
        "martingale_levels": 3, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "hibrido_otimizado": {
        # Estratégia anterior — sem filtro de tendência e sem stop total.
        # Mantida para comparação: responsável pelo PnL -3.31 BTC e -10.11 ETH
        # no crash de Jan-Fev 2025 (DCA numa queda forte sem limite de perda).
        "name": "Híbrido Otimizado", "emoji": "🔄",
        "dca_drop_pct": 2.5, "order_size_usdt": 25.0, "take_profit_pct": 1.5,
        "max_dca_orders": 12, "trailing_stop_enabled": True, "trailing_stop_pct": 0.6,
        "martingale_levels": 0, "rsi_enabled": False, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": False, "stop_loss_pct": 15.0, "trend_filter_enabled": False,
        "trend_ema_period": 21, "circuit_breaker_enabled": False, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
    "triple_screen": {
        # Alexander Elder — Triple Screen DCA
        # Pilar 1 (Psicologia)   : regras rígidas — hard stop inegociável a 4%
        # Pilar 2 (Análise Técn.): filtro de tendência (EMA21) + RSI < 40 na entrada
        # Pilar 3 (Gestão Risco) : máx 3 aportes, stop a 4%, trailing após TP
        # Diferença-chave vs Híbrido: não entra DCA em queda de tendência
        "name": "Triple Screen", "emoji": "📊",
        "dca_drop_pct": 3.0, "order_size_usdt": 20.0, "take_profit_pct": 1.5,
        "max_dca_orders": 3, "trailing_stop_enabled": True, "trailing_stop_pct": 1.0,
        "martingale_levels": 0, "rsi_enabled": True, "rsi_threshold": 40.0, "rsi_period": 14,
        "stop_loss_enabled": True, "stop_loss_pct": 4.0, "trend_filter_enabled": True,
        "trend_ema_period": 21, "circuit_breaker_enabled": True, "circuit_breaker_pct": 6.0,
        "reentry_drop_pct": 1.5,
    },
    "elder_prudente": {
        "name": "Elder Prudente", "emoji": "🎓",
        "dca_drop_pct": 5.0, "order_size_usdt": 20.0, "take_profit_pct": 3.0,
        "max_dca_orders": 5, "trailing_stop_enabled": True, "trailing_stop_pct": 1.5,
        "martingale_levels": 0, "rsi_enabled": True, "rsi_threshold": 50.0, "rsi_period": 14,
        "stop_loss_enabled": True, "stop_loss_pct": 12.0, "trend_filter_enabled": True,
        "trend_ema_period": 21, "circuit_breaker_enabled": True, "circuit_breaker_pct": 8.0,
        "reentry_drop_pct": 1.5,
    },
    "elder_balanceado": {
        "name": "Elder Balanceado", "emoji": "⚖️🎓",
        "dca_drop_pct": 3.0, "order_size_usdt": 25.0, "take_profit_pct": 2.0,
        "max_dca_orders": 8, "trailing_stop_enabled": True, "trailing_stop_pct": 1.0,
        "martingale_levels": 0, "rsi_enabled": True, "rsi_threshold": 45.0, "rsi_period": 14,
        "stop_loss_enabled": True, "stop_loss_pct": 8.0, "trend_filter_enabled": True,
        "trend_ema_period": 21, "circuit_breaker_enabled": True, "circuit_breaker_pct": 6.0,
        "reentry_drop_pct": 1.5,
    },
    "elder_momentum": {
        "name": "Elder Momentum", "emoji": "🚀🎓",
        "dca_drop_pct": 2.0, "order_size_usdt": 20.0, "take_profit_pct": 1.5,
        "max_dca_orders": 10, "trailing_stop_enabled": True, "trailing_stop_pct": 0.7,
        "martingale_levels": 1, "rsi_enabled": True, "rsi_threshold": 55.0, "rsi_period": 14,
        "stop_loss_enabled": True, "stop_loss_pct": 6.0, "trend_filter_enabled": True,
        "trend_ema_period": 14, "circuit_breaker_enabled": True, "circuit_breaker_pct": 10.0,
        "reentry_drop_pct": 1.5,
    },
}

if not PRESETS:
    PRESETS = _BUILTIN_PRESETS
else:
    # Garante que todos os presets têm os campos Elder (backward compat)
    for k, p in PRESETS.items():
        for field_key, default in [
            ("rsi_enabled", False), ("rsi_threshold", 45.0), ("rsi_period", 14),
            ("stop_loss_enabled", False), ("stop_loss_pct", 15.0),
            ("trend_filter_enabled", False), ("trend_ema_period", 21),
            ("circuit_breaker_enabled", False), ("circuit_breaker_pct", 10.0),
            ("reentry_drop_pct", 1.5),
        ]:
            if field_key not in p:
                p[field_key] = default


# ══════════════════════════════════════════════════════════════════════════════
# BUSCA DE DADOS
# ══════════════════════════════════════════════════════════════════════════════

def _build_exchange() -> ccxt.Exchange:
    """Exchange pública (sem credenciais) para busca de dados históricos."""
    return ccxt.okx({
        "enableRateLimit": True,
        "options"        : {"defaultType": "spot"},
    })


def fetch_history(ex: ccxt.Exchange, symbol: str, timeframe: str, days: int) -> list:
    """Busca candles OHLCV históricos com paginação automática."""
    tf_ms = {
        "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
    }.get(timeframe, 3_600_000)

    now_ms   = int(time.time() * 1000)
    since_ms = now_ms - days * 86_400_000
    candles  = []
    since    = since_ms

    print(f"  ↳ Buscando {timeframe} para {symbol} ({days} dias)…", end=" ", flush=True)

    while since < now_ms:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=300)
        if not batch:
            break
        candles.extend(batch)
        since = batch[-1][0] + tf_ms
        if len(batch) < 300:
            break
        time.sleep(0.25)

    # Deduplica e filtra
    seen, unique = set(), []
    for c in candles:
        if c[0] not in seen and c[0] >= since_ms:
            seen.add(c[0])
            unique.append(c)
    unique.sort(key=lambda x: x[0])
    print(f"{len(unique)} candles")
    return unique


# ══════════════════════════════════════════════════════════════════════════════
# INDICADORES
# ══════════════════════════════════════════════════════════════════════════════

def calc_rsi_series(closes: list, period: int = 14) -> list:
    """Série completa de RSI (Wilder). Índice i = RSI calculado com closes[0..i]."""
    result = [50.0] * period
    if len(closes) <= period:
        return [50.0] * len(closes)

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]

    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    result.append(100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 2))

    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        result.append(100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 2))

    return result


def calc_ema_series(closes: list, period: int) -> list:
    """Série de EMA. Primeiros (period-1) valores são None."""
    if len(closes) < period:
        return [None] * len(closes)

    k      = 2.0 / (period + 1)
    ema    = sum(closes[:period]) / period
    result = [None] * (period - 1) + [ema]

    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
        result.append(ema)

    return result


def build_daily_ema_lookup(daily_candles: list, period: int) -> dict:
    """Mapa timestamp_inicio_do_dia_ms → EMA naquele dia (calculada com candles anteriores)."""
    closes = [float(c[4]) for c in daily_candles]
    emas   = calc_ema_series(closes, period)
    return {daily_candles[i][0]: emas[i] for i in range(len(daily_candles))}


def get_daily_ema_at(ts_ms: int, daily_ema_lookup: dict):
    """Retorna a EMA diária mais recente disponível antes de ts_ms."""
    best = None
    for day_ts, ema in daily_ema_lookup.items():
        if day_ts <= ts_ms and ema is not None:
            if best is None or day_ts > best[0]:
                best = (day_ts, ema)
    return best[1] if best else None


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR DE SIMULAÇÃO
# ══════════════════════════════════════════════════════════════════════════════

class _Pos:
    """Estado de uma posição durante a simulação."""
    __slots__ = (
        "is_active", "avg_price", "total_qty", "total_invested", "total_buy_fees",
        "order_count", "last_buy_price", "trailing_active", "peak_price",
        "trailing_stop_price", "last_exit_price",
    )

    def __init__(self):
        self.is_active           = False
        self.avg_price           = 0.0
        self.total_qty           = 0.0
        self.total_invested      = 0.0
        self.total_buy_fees      = 0.0
        self.order_count         = 0
        self.last_buy_price      = 0.0
        self.trailing_active     = False
        self.peak_price          = 0.0
        self.trailing_stop_price = 0.0
        self.last_exit_price     = 0.0

    def reset(self, exit_price: float):
        self.is_active           = False
        self.trailing_active     = False
        self.last_exit_price     = exit_price
        self.avg_price           = 0.0
        self.total_qty           = 0.0
        self.total_invested      = 0.0
        self.total_buy_fees      = 0.0
        self.order_count         = 0
        self.last_buy_price      = 0.0
        self.peak_price          = 0.0
        self.trailing_stop_price = 0.0


def _order_size(cfg: dict, order_num: int) -> float:
    levels = int(cfg.get("martingale_levels", 0))
    return cfg["order_size_usdt"] * min(order_num, max(levels, 1))


def simulate(
    strategy_key: str,
    cfg: dict,
    hourly_candles: list,
    daily_ema_lookup: dict,
    rsi_series: list,
) -> dict:
    """
    Simula estratégia DCA sobre candles históricos.
    Retorna dict com trades, equity curve e métricas.
    """
    pos            = _Pos()
    trades         = []
    equity_curve   = []
    entries        = []
    exits          = []
    cumulative_pnl = 0.0
    max_drawdown   = 0.0

    reentry_drop = cfg.get("reentry_drop_pct", 0.0)
    sl_enabled   = cfg.get("stop_loss_enabled", False)
    sl_pct       = cfg.get("stop_loss_pct", 15.0)
    tf_enabled   = cfg.get("trend_filter_enabled", False)
    rsi_enabled  = cfg.get("rsi_enabled", False)
    rsi_thresh   = cfg.get("rsi_threshold", 45.0)
    cb_enabled   = cfg.get("circuit_breaker_enabled", False)
    cb_pct       = cfg.get("circuit_breaker_pct", 10.0)

    for i, candle in enumerate(hourly_candles):
        ts_ms      = candle[0]
        ts         = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        price_high = float(candle[2])
        price_low  = float(candle[3])
        price      = float(candle[4])

        # ── Posição aberta ────────────────────────────────────────────────────
        if pos.is_active:

            # Stop Loss — verifica contra a mínima do candle
            if sl_enabled:
                sl_price = pos.avg_price * (1 - sl_pct / 100)
                if price_low <= sl_price:
                    exit_price     = min(sl_price, price)
                    cumulative_pnl = _close(pos, exit_price, "Stop Loss", ts,
                                           trades, exits, equity_curve, cumulative_pnl)
                    continue

            # Trailing Stop ativo
            if pos.trailing_active:
                if price_high > pos.peak_price:
                    pos.peak_price          = price_high
                    pos.trailing_stop_price = price_high * (1 - cfg["trailing_stop_pct"] / 100)
                if price_low <= pos.trailing_stop_price:
                    exit_price     = min(pos.trailing_stop_price, price)
                    cumulative_pnl = _close(pos, exit_price, "Trailing Stop", ts,
                                           trades, exits, equity_curve, cumulative_pnl)
                    continue

            else:
                # Take Profit
                tp_price = pos.avg_price * (1 + cfg["take_profit_pct"] / 100)
                if price >= tp_price:
                    if cfg.get("trailing_stop_enabled"):
                        pos.trailing_active      = True
                        pos.peak_price           = price
                        pos.trailing_stop_price  = price * (1 - cfg["trailing_stop_pct"] / 100)
                    else:
                        cumulative_pnl = _close(pos, price, "Take Profit", ts,
                                               trades, exits, equity_curve, cumulative_pnl)
                        continue

                # DCA add
                elif pos.order_count < cfg["max_dca_orders"]:
                    drop = (pos.last_buy_price - price) / pos.last_buy_price * 100
                    if drop >= cfg["dca_drop_pct"]:
                        size = _order_size(cfg, pos.order_count + 1)
                        _open(pos, price, size, pos.order_count + 1, ts, entries)

            # Drawdown
            if pos.is_active and pos.total_invested > 0:
                unreal = (price - pos.avg_price) * pos.total_qty
                dd     = -unreal / pos.total_invested * 100
                if dd > max_drawdown:
                    max_drawdown = dd

        # ── Sem posição — verifica entrada ────────────────────────────────────
        else:
            # Re-entrada: aguarda queda abaixo do preço de saída
            if pos.last_exit_price > 0 and reentry_drop > 0:
                if price > pos.last_exit_price * (1 - reentry_drop / 100):
                    continue

            # Filtro RSI
            if rsi_enabled and rsi_series is not None:
                rsi = rsi_series[i] if i < len(rsi_series) else 50.0
                if rsi >= rsi_thresh:
                    continue

            # Filtro EMA tendência — não entra se preço < EMA (downtrend)
            if tf_enabled and daily_ema_lookup is not None:
                ema = get_daily_ema_at(ts_ms, daily_ema_lookup)
                if ema is not None and price < ema:
                    continue

            # Abre posição
            size = _order_size(cfg, 1)
            _open(pos, price, size, 1, ts, entries)

    # Fecha posição aberta ao fim do período (mark-to-market)
    if pos.is_active:
        last_price = float(hourly_candles[-1][4])
        last_ts    = datetime.fromtimestamp(hourly_candles[-1][0] / 1000, tz=timezone.utc)
        cumulative_pnl = _close(pos, last_price, "Fim do período", last_ts,
                                trades, exits, equity_curve, cumulative_pnl)

    num_trades  = len(trades)
    wins        = sum(1 for t in trades if t["pnl_net"] > 0)
    total_fees  = sum(t["fees"] for t in trades)
    avg_pnl     = cumulative_pnl / num_trades if num_trades else 0.0
    win_rate    = wins / num_trades * 100 if num_trades else 0.0
    total_invested_peak = max((t["cost"] for t in trades), default=0.0)

    return {
        "strategy"    : strategy_key,
        "trades"      : trades,
        "equity_curve": equity_curve,
        "entries"     : entries,
        "exits"       : exits,
        "total_pnl"   : cumulative_pnl,
        "num_trades"  : num_trades,
        "win_rate"    : win_rate,
        "avg_pnl"     : avg_pnl,
        "max_drawdown": max_drawdown,
        "total_fees"  : total_fees,
        "max_invested": total_invested_peak,
    }


def _open(pos: _Pos, price: float, size_usdt: float, order_num: int,
          ts: datetime, entries: list) -> None:
    qty     = size_usdt / price
    buy_fee = size_usdt * FEE_RATE

    pos.total_qty      += qty
    pos.total_invested += size_usdt
    pos.total_buy_fees += buy_fee
    pos.order_count     = order_num
    pos.last_buy_price  = price
    pos.avg_price       = (pos.total_invested + pos.total_buy_fees) / pos.total_qty
    pos.is_active       = True
    entries.append((ts, price, order_num))


def _close(
    pos: _Pos, price: float, reason: str, ts: datetime,
    trades: list, exits: list, equity_curve: list, cumulative_pnl: float,
) -> float:
    gross_rev  = price * pos.total_qty
    sell_fee   = gross_rev * FEE_RATE
    pnl_gross  = gross_rev - pos.total_invested
    pnl_net    = pnl_gross - pos.total_buy_fees - sell_fee
    pnl_pct    = pnl_net / pos.total_invested * 100 if pos.total_invested > 0 else 0.0
    total_fees = pos.total_buy_fees + sell_fee
    new_cum    = cumulative_pnl + pnl_net

    trades.append({
        "ts"       : ts,
        "reason"   : reason,
        "entry_avg": pos.avg_price,
        "exit_px"  : price,
        "qty"      : pos.total_qty,
        "cost"     : pos.total_invested,
        "pnl_gross": pnl_gross,
        "pnl_net"  : pnl_net,
        "pnl_pct"  : pnl_pct,
        "fees"     : total_fees,
        "orders"   : pos.order_count,
    })
    exits.append((ts, price, reason, pnl_net))
    equity_curve.append((ts, new_cum))
    pos.reset(price)
    return new_cum


# ══════════════════════════════════════════════════════════════════════════════
# SAÍDA TEXTUAL
# ══════════════════════════════════════════════════════════════════════════════

_SEP = "─" * 110


def print_results(all_results: dict) -> None:
    """Imprime tabela comparativa de todas as estratégias × pares."""
    print(f"\n{'═' * 110}")
    print("  RESULTADO DO BACKTEST — COMPARAÇÃO DE ESTRATÉGIAS")
    print(f"{'═' * 110}\n")

    header = (
        f"{'Estratégia':<22} {'Par':<12} {'Trades':>7} {'Win%':>6} "
        f"{'PnL USDT':>10} {'PnL%':>7} {'Avg/trade':>10} {'Max DD':>8} {'Taxas':>8}"
    )
    print(header)
    print(_SEP)

    for pair, results in all_results.items():
        results_sorted = sorted(results, key=lambda r: r["total_pnl"], reverse=True)
        for r in results_sorted:
            p        = PRESETS.get(r["strategy"], {})
            name     = p.get("name", r["strategy"])
            emoji    = p.get("emoji", "")
            pnl_sign = "+" if r["total_pnl"] >= 0 else ""
            avg_sign = "+" if r["avg_pnl"] >= 0 else ""
            invested = r.get("max_invested", r.get("total_fees", 0) * 500)
            pnl_pct  = r["total_pnl"] / invested * 100 if invested > 0 else 0.0
            print(
                f"{emoji} {name:<20} {pair:<12} {r['num_trades']:>7} "
                f"{r['win_rate']:>5.1f}% "
                f"{pnl_sign}{r['total_pnl']:>9.2f} "
                f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:>6.1f}% "
                f"{avg_sign}{r['avg_pnl']:>9.2f} "
                f"{r['max_drawdown']:>7.1f}% "
                f"{r['total_fees']:>8.2f}"
            )
        print(_SEP)

    # Detalhe dos trades de cada estratégia
    for pair, results in all_results.items():
        for r in results:
            if not r["trades"]:
                continue
            p     = PRESETS.get(r["strategy"], {})
            name  = p.get("name", r["strategy"])
            emoji = p.get("emoji", "")
            print(f"\n{'─'*60}")
            print(f" {emoji} {name}  ·  {pair}")
            print(f"{'─'*60}")
            print(f"  {'Data':<20} {'Razão':<16} {'Avg Entrada':>12} {'Saída':>10} {'Aportes':>8} {'PnL':>10}")
            for t in r["trades"]:
                sign = "+" if t["pnl_net"] >= 0 else ""
                print(
                    f"  {t['ts'].strftime('%d/%m/%Y %H:%M'):<20} "
                    f"{t['reason']:<16} "
                    f"{t['entry_avg']:>12.4f} "
                    f"{t['exit_px']:>10.4f} "
                    f"{t['orders']:>8} "
                    f"{sign}{t['pnl_net']:>9.2f}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# GRÁFICOS
# ══════════════════════════════════════════════════════════════════════════════

_COLORS = [
    "#00d4aa", "#ff6b6b", "#ffd166", "#06d6a0", "#118ab2",
    "#ef476f", "#ffb703", "#8338ec", "#fb8500", "#3a86ff",
]


def plot_pair(pair: str, results: list, hourly_candles: list, output_dir: Path) -> None:
    """Gera gráfico de preço + equity curve para um par."""
    if not HAS_MPL:
        return

    dates  = [datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc) for c in hourly_candles]
    closes = [float(c[4]) for c in hourly_candles]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 10),
        gridspec_kw={"height_ratios": [2, 1]},
        facecolor="#1a1a2e",
    )
    fig.suptitle(
        f"Backtest DCA — {pair}",
        fontsize=14, fontweight="bold", color="white", y=0.98,
    )

    # ── Gráfico de preço ──────────────────────────────────────────────────────
    ax1.set_facecolor("#16213e")
    ax1.plot(dates, closes, color="#4a6fa5", linewidth=1.2, alpha=0.8, zorder=1)
    ax1.set_ylabel("Preço", color="#aaaaaa", fontsize=9)
    ax1.tick_params(colors="#aaaaaa", labelsize=8)
    for spine in ax1.spines.values():
        spine.set_color("#333355")

    for i, r in enumerate(results):
        col = _COLORS[i % len(_COLORS)]
        p   = PRESETS.get(r["strategy"], {})
        lbl = p.get("name", r["strategy"])

        if r["entries"]:
            et = [e[0] for e in r["entries"]]
            ep = [e[1] for e in r["entries"]]
            ax1.scatter(et, ep, marker="^", color=col, s=35, zorder=5,
                        label=f"↑ {lbl}", alpha=0.85)
        if r["exits"]:
            xt = [e[0] for e in r["exits"]]
            xp = [e[1] for e in r["exits"]]
            xc = ["#00d4aa" if e[3] >= 0 else "#ff6b6b" for e in r["exits"]]
            ax1.scatter(xt, xp, marker="v", color=xc, s=35, zorder=5, alpha=0.85)

    ax1.legend(fontsize=7, facecolor="#1a1a2e", labelcolor="white",
               edgecolor="#333355", loc="upper left")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    plt.setp(ax1.get_xticklabels(), visible=False)

    # ── Curvas de equity ─────────────────────────────────────────────────────
    ax2.set_facecolor("#16213e")
    ax2.axhline(y=0, color="#ffffff30", linestyle="--", linewidth=1)

    for i, r in enumerate(results):
        if not r["equity_curve"]:
            continue
        col   = _COLORS[i % len(_COLORS)]
        p     = PRESETS.get(r["strategy"], {})
        lbl   = p.get("name", r["strategy"])
        eq_t  = [e[0] for e in r["equity_curve"]]
        eq_v  = [e[1] for e in r["equity_curve"]]
        final = eq_v[-1]
        sign  = "+" if final >= 0 else ""
        ax2.plot(eq_t, eq_v, color=col, linewidth=1.8, label=f"{lbl} ({sign}{final:.2f})")
        ax2.scatter(eq_t[-1:], eq_v[-1:], color=col, s=50, zorder=5)

    ax2.set_ylabel("PnL Acumulado (USDT)", color="#aaaaaa", fontsize=9)
    ax2.tick_params(colors="#aaaaaa", labelsize=8)
    for spine in ax2.spines.values():
        spine.set_color("#333355")
    ax2.legend(fontsize=7, facecolor="#1a1a2e", labelcolor="white",
               edgecolor="#333355", loc="upper left")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    plt.xticks(rotation=20, ha="right", color="#aaaaaa", fontsize=8)

    plt.tight_layout()

    safe_pair = pair.replace("/", "_")
    out_path  = output_dir / f"backtest_{safe_pair}.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n📊 Gráfico salvo: {out_path}")


def plot_summary_bar(all_results: dict, output_dir: Path) -> None:
    """Gráfico de barras: PnL total por estratégia × par."""
    if not HAS_MPL:
        return

    strategies = list({r["strategy"] for results in all_results.values() for r in results})
    pairs      = list(all_results.keys())
    n_strat    = len(strategies)
    n_pairs    = len(pairs)

    if n_strat == 0:
        return

    fig, ax = plt.subplots(figsize=(max(12, n_strat * 2), 6), facecolor="#1a1a2e")
    ax.set_facecolor("#16213e")

    width = 0.8 / n_pairs
    x     = range(n_strat)

    for j, pair in enumerate(pairs):
        pnl_map = {r["strategy"]: r["total_pnl"] for r in all_results[pair]}
        vals    = [pnl_map.get(s, 0.0) for s in strategies]
        offsets = [i + (j - n_pairs / 2 + 0.5) * width for i in x]
        colors  = ["#00d4aa" if v >= 0 else "#ff6b6b" for v in vals]
        bars    = ax.bar(offsets, vals, width=width * 0.9, color=colors, alpha=0.85, label=pair)
        for bar, val in zip(bars, vals):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                val + (0.3 if val >= 0 else -1.5),
                f"{val:+.1f}",
                ha="center", va="bottom", fontsize=7, color="white",
            )

    names = [PRESETS.get(s, {}).get("name", s) for s in strategies]
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=25, ha="right", color="#aaaaaa", fontsize=8)
    ax.axhline(y=0, color="#ffffff30", linewidth=1)
    ax.set_ylabel("PnL Líquido (USDT)", color="#aaaaaa", fontsize=9)
    ax.set_title("Comparação de Estratégias — PnL Líquido Total", color="white",
                 fontsize=12, fontweight="bold")
    ax.tick_params(colors="#aaaaaa", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#333355")
    ax.legend(facecolor="#1a1a2e", labelcolor="white", edgecolor="#333355", fontsize=9)

    plt.tight_layout()
    out_path = output_dir / "backtest_comparacao.png"
    plt.savefig(out_path, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"📊 Comparação salva: {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Backtester DCA — simula estratégias sobre dados históricos da OKX"
    )
    parser.add_argument(
        "--pairs", default="BTC/USDT,ETH/USDT,SOL/USDT",
        help="Pares separados por vírgula (padrão: BTC/USDT,ETH/USDT,SOL/USDT)",
    )
    parser.add_argument(
        "--strategy", default="all",
        help=f"Estratégia ou 'all'. Opções: {', '.join(PRESETS.keys())}",
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Número de dias de histórico (padrão: 180)",
    )
    parser.add_argument(
        "--no-chart", action="store_true",
        help="Não gera gráficos",
    )
    parser.add_argument(
        "--output", default="backtest_results",
        help="Pasta para salvar gráficos (padrão: backtest_results/)",
    )
    args = parser.parse_args()

    pairs = [p.strip().upper() for p in args.pairs.split(",") if p.strip()]

    if args.strategy == "all":
        strategies = list(PRESETS.keys())
    else:
        # Suporta lista separada por vírgula: --strategy hibrido_otimizado,triple_screen
        requested = [s.strip() for s in args.strategy.split(",")]
        invalid   = [s for s in requested if s not in PRESETS]
        if invalid:
            print(f"❌ Estratégia(s) não encontrada(s): {', '.join(invalid)}")
            print(f"   Disponíveis: {', '.join(PRESETS.keys())}")
            sys.exit(1)
        strategies = requested

    output_dir = Path(args.output)
    if not args.no_chart and HAS_MPL:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'═'*60}")
    print(f"  DCA Backtester — OKX")
    print(f"  Pares      : {', '.join(pairs)}")
    print(f"  Estratégias: {len(strategies)} selecionadas")
    print(f"  Período    : {args.days} dias")
    print(f"{'═'*60}\n")

    ex          = _build_exchange()
    all_results = {}

    for pair in pairs:
        print(f"\n🔍 Par: {pair}")
        all_results[pair] = []

        hourly = fetch_history(ex, pair, "1h", args.days + 2)
        if not hourly:
            print(f"  ⚠️  Sem dados para {pair}. Pulando.")
            continue

        max_ema = max(
            (PRESETS[s].get("trend_ema_period", 21) for s in strategies
             if PRESETS[s].get("trend_filter_enabled")),
            default=21,
        )
        daily = fetch_history(ex, pair, "1d", args.days + max_ema + 10)

        hourly_closes = [float(c[4]) for c in hourly]
        rsi_period    = max(
            (PRESETS[s].get("rsi_period", 14) for s in strategies
             if PRESETS[s].get("rsi_enabled")),
            default=14,
        )
        rsi_series = calc_rsi_series(hourly_closes, rsi_period)

        for strat_key in strategies:
            cfg        = deepcopy(PRESETS[strat_key])
            ema_period = int(cfg.get("trend_ema_period", 21))
            ema_lookup = (
                build_daily_ema_lookup(daily, ema_period)
                if cfg.get("trend_filter_enabled") else None
            )
            rsi_s = rsi_series if cfg.get("rsi_enabled") else None

            name = PRESETS[strat_key].get("name", strat_key)
            print(f"  ⚙️  Simulando: {name}…", end=" ", flush=True)
            result = simulate(strat_key, cfg, hourly, ema_lookup, rsi_s)
            all_results[pair].append(result)
            sign = "+" if result["total_pnl"] >= 0 else ""
            print(
                f"{result['num_trades']} trades | "
                f"PnL {sign}{result['total_pnl']:.2f} USDT | "
                f"Win {result['win_rate']:.0f}%"
            )

        if not args.no_chart and HAS_MPL:
            plot_pair(pair, all_results[pair], hourly, output_dir)

    print_results(all_results)

    if not args.no_chart and HAS_MPL:
        plot_summary_bar(all_results, output_dir)

    print(f"\n✅ Backtest concluído.")
    if not args.no_chart and HAS_MPL:
        print(f"   Gráficos em: {output_dir.resolve()}/")


if __name__ == "__main__":
    main()
