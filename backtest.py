"""
backtest.py — Backtesting da estratégia em dados históricos reais.

Simula a estratégia barra a barra, respeitando a ordem cronológica dos dados
(sem lookahead bias). Inclui trailing stop, métricas e exportação CSV.

Uso:
    python backtest.py
    python backtest.py --symbols BCH/USDT --limit 1000
    python backtest.py --symbols BCH/USDT LTC/USDT --limit 500
"""

import argparse
import math
import sys
from typing import Dict, List, Optional

import pandas as pd
import pandas_ta as ta

from config import (
    SYMBOLS,
    TIMEFRAME_TREND,
    TIMEFRAME_ENTRY,
    RISK_PCT,
    RR_RATIO,
    SL_CANDLES,
    SL_BUFFER_PCT,
    TRAILING_ACTIVATION_PCT,
    TRAILING_STOP_PCT,
    RSI_OVERSOLD,
    RSI_LOOKBACK,
    create_exchange,
)


# ═════════════════════════════════════════════════════════════
# DADOS
# ═════════════════════════════════════════════════════════════

def fetch_ohlcv(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    print(f"  Baixando {limit} candles de {symbol} ({timeframe})...")
    raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    return df.astype(float)


# ═════════════════════════════════════════════════════════════
# INDICADORES (sem lookahead: usa dados até o índice i)
# ═════════════════════════════════════════════════════════════

def get_indicators(
    trend_df: pd.DataFrame,
    entry_df: pd.DataFrame,
    i: int,
) -> Optional[Dict]:
    """
    Calcula indicadores usando apenas dados até a barra i do entry_df.
    Retorna None se não houver dados suficientes para o cálculo.
    """
    e = entry_df.iloc[: i + 1].copy()
    if len(e) < 60:
        return None

    e.ta.ema(length=20, append=True)
    e.ta.ema(length=50, append=True)
    e.ta.rsi(length=14, append=True)

    last     = e.iloc[-1]
    close_4h = float(last["close"])
    ema20_4h = float(last.get("EMA_20", float("nan")))
    ema50_4h = float(last.get("EMA_50", float("nan")))

    rsi_series = e["RSI_14"].dropna()
    if len(rsi_series) < RSI_LOOKBACK + 1:
        return None
    rsi_current  = float(rsi_series.iloc[-1])
    rsi_previous = float(rsi_series.iloc[-2])

    if any(math.isnan(v) for v in [ema20_4h, ema50_4h]):
        return None

    # Alinha com o trend timeframe
    current_time = e.index[-1]
    t_slice = trend_df[trend_df.index <= current_time].copy()
    if len(t_slice) < 55:
        return None

    t_slice.ta.ema(length=50, append=True)
    last_t   = t_slice.iloc[-1]
    close_1d = float(last_t["close"])
    ema50_1d = float(last_t.get("EMA_50", float("nan")))

    if math.isnan(ema50_1d):
        return None

    trend_ok      = close_1d > ema50_1d
    pullback_high = ema20_4h * 1.02
    pullback_ok   = ema50_4h <= close_4h <= pullback_high

    rsi_window       = rsi_series.iloc[-RSI_LOOKBACK:]
    rsi_was_oversold = bool((rsi_window < RSI_OVERSOLD).any())
    rsi_recovering   = rsi_current > rsi_previous
    rsi_ok           = rsi_was_oversold and rsi_recovering

    lowest_low = float(e["low"].iloc[-SL_CANDLES:].min())

    return {
        "signal"      : trend_ok and pullback_ok and rsi_ok,
        "trend_ok"    : trend_ok,
        "pullback_ok" : pullback_ok,
        "rsi_ok"      : rsi_ok,
        "close_4h"    : close_4h,
        "ema20_4h"    : ema20_4h,
        "ema50_4h"    : ema50_4h,
        "close_1d"    : close_1d,
        "ema50_1d"    : ema50_1d,
        "rsi"         : rsi_current,
        "lowest_low"  : lowest_low,
    }


# ═════════════════════════════════════════════════════════════
# SIMULAÇÃO
# ═════════════════════════════════════════════════════════════

def simulate_symbol(exchange, symbol: str, limit: int) -> List[Dict]:
    print(f"\n{'═' * 58}")
    print(f"  Backtesting: {symbol}")
    print(f"{'═' * 58}")

    trend_df = fetch_ohlcv(exchange, symbol, TIMEFRAME_TREND, limit * 3)
    entry_df = fetch_ohlcv(exchange, symbol, TIMEFRAME_ENTRY, limit)

    trades   = []
    position = None
    warmup   = 60

    for i in range(warmup, len(entry_df)):
        bar      = entry_df.iloc[i]
        bar_time = entry_df.index[i]
        high     = float(bar["high"])
        low      = float(bar["low"])
        close    = float(bar["close"])

        # ── Sem posição: verifica sinal ───────────────────────────────────
        if position is None:
            ind = get_indicators(trend_df, entry_df, i)
            if ind is None or not ind["signal"]:
                continue

            entry_price = close
            sl_price    = ind["lowest_low"] * (1.0 - SL_BUFFER_PCT / 100.0)
            risk_unit   = entry_price - sl_price

            if risk_unit <= 0:
                continue

            tp_price = entry_price + risk_unit * RR_RATIO
            position = {
                "entry_time"    : bar_time,
                "entry_price"   : entry_price,
                "sl_price"      : sl_price,
                "tp_price"      : tp_price,
                "highest_price" : entry_price,
                "trail_active"  : False,
                "trail_sl"      : None,
            }
            print(
                f"  → ENTRADA  {bar_time.strftime('%Y-%m-%d %H:%M')} | "
                f"Entry={entry_price:.4f} | SL={sl_price:.4f} | TP={tp_price:.4f}"
            )

        # ── Com posição: monitora saída ────────────────────────────────────
        else:
            entry = position["entry_price"]

            # Atualiza máxima histórica
            if high > position["highest_price"]:
                position["highest_price"] = high

            highest    = position["highest_price"]
            profit_pct = ((highest - entry) / entry) * 100.0

            # Ativa trailing
            if not position["trail_active"] and profit_pct >= TRAILING_ACTIVATION_PCT:
                position["trail_active"] = True
                position["trail_sl"]     = highest * (1.0 - TRAILING_STOP_PCT / 100.0)

            # Atualiza trail SL
            if position["trail_active"]:
                new_trail = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
                if new_trail > position["trail_sl"]:
                    position["trail_sl"] = new_trail

            # Verifica exits (prioridade: SL > Trailing > TP)
            exit_price  = None
            exit_reason = None

            if low <= position["sl_price"]:
                exit_price  = position["sl_price"]
                exit_reason = "Stop Loss"
            elif position["trail_active"] and low <= position["trail_sl"]:
                exit_price  = position["trail_sl"]
                exit_reason = "Trailing Stop"
            elif high >= position["tp_price"]:
                exit_price  = position["tp_price"]
                exit_reason = "Take Profit"

            if exit_price is not None:
                pnl_pct    = ((exit_price - entry) / entry) * 100.0
                risk_unit  = entry - position["sl_price"]
                r_multiple = ((exit_price - entry) / risk_unit) if risk_unit > 0 else 0.0

                trade = {
                    "symbol"      : symbol,
                    "entry_time"  : position["entry_time"].strftime("%Y-%m-%d %H:%M"),
                    "exit_time"   : bar_time.strftime("%Y-%m-%d %H:%M"),
                    "entry_price" : entry,
                    "exit_price"  : exit_price,
                    "sl_price"    : position["sl_price"],
                    "tp_price"    : position["tp_price"],
                    "pnl_pct"     : round(pnl_pct, 2),
                    "r_multiple"  : round(r_multiple, 2),
                    "exit_reason" : exit_reason,
                    "win"         : pnl_pct > 0,
                }
                trades.append(trade)

                icon = "✅" if pnl_pct > 0 else "❌"
                print(
                    f"  {icon} SAÍDA   {bar_time.strftime('%Y-%m-%d %H:%M')} | "
                    f"{exit_reason:<14} | Exit={exit_price:.4f} | "
                    f"PnL={pnl_pct:+.2f}% | {r_multiple:.2f}R"
                )
                position = None

    # Posição ainda aberta no fim dos dados
    if position is not None:
        last     = entry_df.iloc[-1]
        ep       = float(last["close"])
        entry    = position["entry_price"]
        pnl_pct  = ((ep - entry) / entry) * 100.0
        risk_unit = entry - position["sl_price"]
        r_multiple = ((ep - entry) / risk_unit) if risk_unit > 0 else 0.0
        trades.append({
            "symbol"      : symbol,
            "entry_time"  : position["entry_time"].strftime("%Y-%m-%d %H:%M"),
            "exit_time"   : entry_df.index[-1].strftime("%Y-%m-%d %H:%M"),
            "entry_price" : entry,
            "exit_price"  : ep,
            "sl_price"    : position["sl_price"],
            "tp_price"    : position["tp_price"],
            "pnl_pct"     : round(pnl_pct, 2),
            "r_multiple"  : round(r_multiple, 2),
            "exit_reason" : "Em aberto (fim dos dados)",
            "win"         : pnl_pct > 0,
        })
        print(f"  ⏳ Posição ainda aberta no fim do período | PnL={pnl_pct:+.2f}%")

    return trades


# ═════════════════════════════════════════════════════════════
# RELATÓRIO
# ═════════════════════════════════════════════════════════════

def print_report(all_trades: List[Dict]) -> None:
    if not all_trades:
        print("\n  Nenhuma operação encontrada no período.")
        print("  Dica: aumente --limit ou relaxe os parâmetros no .env\n")
        return

    print(f"\n{'═' * 58}")
    print("  RELATÓRIO DE BACKTESTING")
    print(f"{'═' * 58}")

    symbols = list(dict.fromkeys(t["symbol"] for t in all_trades))

    for symbol in symbols:
        trades  = [t for t in all_trades if t["symbol"] == symbol]
        wins    = [t for t in trades if t["win"]]
        losses  = [t for t in trades if not t["win"]]

        if not trades:
            continue

        win_rate      = len(wins) / len(trades) * 100
        total_pnl     = sum(t["pnl_pct"] for t in trades)
        avg_win       = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
        avg_loss      = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
        avg_r         = sum(t["r_multiple"] for t in trades) / len(trades)
        gross_profit  = sum(t["pnl_pct"] for t in wins) if wins else 0
        gross_loss    = abs(sum(t["pnl_pct"] for t in losses)) if losses else 1
        profit_factor = gross_profit / gross_loss if gross_loss else float("inf")

        # Max drawdown
        equity = 100.0
        peak   = equity
        max_dd = 0.0
        for t in trades:
            equity *= (1 + t["pnl_pct"] / 100)
            peak    = max(peak, equity)
            dd      = (peak - equity) / peak * 100
            max_dd  = max(max_dd, dd)

        final_equity = 100.0
        for t in trades:
            final_equity *= (1 + t["pnl_pct"] / 100)

        print(f"\n  ── {symbol} " + "─" * (48 - len(symbol)))
        print(f"  Operações     : {len(trades)}  ({len(wins)} ganhos / {len(losses)} perdas)")
        print(f"  Win Rate      : {win_rate:.1f}%")
        print(f"  PnL Total     : {total_pnl:+.2f}%")
        print(f"  Capital final : {final_equity:.2f}  (base 100)")
        print(f"  Média Ganho   : {avg_win:+.2f}%")
        print(f"  Média Perda   : {avg_loss:+.2f}%")
        print(f"  R Médio       : {avg_r:.2f}R")
        print(f"  Profit Factor : {profit_factor:.2f}")
        print(f"  Max Drawdown  : {max_dd:.2f}%")

        print(f"\n  {'Entrada':<17} {'Saída':<17} {'Entry':>8} {'Exit':>8} {'PnL%':>7} {'R':>5}  Motivo")
        print(f"  {'─' * 72}")
        for t in trades:
            icon = "✅" if t["win"] else "❌"
            print(
                f"  {icon} {t['entry_time']:<15} {t['exit_time']:<15} "
                f"{t['entry_price']:>8.4f} {t['exit_price']:>8.4f} "
                f"{t['pnl_pct']:>+7.2f}% {t['r_multiple']:>5.2f}R  {t['exit_reason']}"
            )

    # Totais gerais se múltiplos pares
    if len(symbols) > 1:
        total    = len(all_trades)
        total_w  = sum(1 for t in all_trades if t["win"])
        total_pnl = sum(t["pnl_pct"] for t in all_trades)
        print(f"\n{'─' * 58}")
        print(f"  TOTAL GERAL  {total} operações | Win Rate: {total_w/total*100:.1f}% | PnL: {total_pnl:+.2f}%")

    print(f"\n{'═' * 58}\n")


def save_csv(all_trades: List[Dict], filename: str = "backtest_result.csv") -> None:
    if not all_trades:
        return
    pd.DataFrame(all_trades).to_csv(filename, index=False, encoding="utf-8")
    print(f"  Resultados salvos em: {filename}\n")


# ═════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest da estratégia de Swing Trade")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS,  help="Pares a testar")
    parser.add_argument("--limit",   type=int,  default=500,      help="Candles históricos por par")
    args = parser.parse_args()

    print(f"\n{'═' * 58}")
    print("  BACKTEST — Swing Trade Bot")
    print(f"  Pares      : {' | '.join(args.symbols)}")
    print(f"  Timeframes : {TIMEFRAME_TREND.upper()} (tendência) / {TIMEFRAME_ENTRY.upper()} (entrada)")
    print(f"  Candles    : {args.limit} por par")
    print(f"  R/R ratio  : 1:{RR_RATIO}  |  Risco: {RISK_PCT}%/op")
    print(f"  Trailing   : ativa em +{TRAILING_ACTIVATION_PCT}%  |  distância {TRAILING_STOP_PCT}%")
    print(f"{'═' * 58}")

    try:
        exchange = create_exchange()
        exchange.load_markets()
    except Exception as exc:
        print(f"\n  ERRO ao conectar à exchange: {exc}")
        sys.exit(1)

    all_trades: List[Dict] = []
    for symbol in args.symbols:
        try:
            trades = simulate_symbol(exchange, symbol, args.limit)
            all_trades.extend(trades)
        except Exception as exc:
            print(f"\n  ERRO em {symbol}: {exc}")

    print_report(all_trades)
    save_csv(all_trades)


if __name__ == "__main__":
    main()
