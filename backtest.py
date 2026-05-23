"""
backtest.py — Backtesting da estratégia Triple EMA Cross + RSI Filter.

Simula a estratégia barra a barra sem lookahead bias.

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
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_MIN,
    RSI_MAX,
    SIGNAL_LOOKBACK,
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
# INDICADORES (sem lookahead)
# ═════════════════════════════════════════════════════════════

def get_indicators(
    trend_df: pd.DataFrame,
    entry_df: pd.DataFrame,
    i: int,
) -> Optional[Dict]:
    """Calcula indicadores da estratégia Triple EMA usando dados até a barra i."""
    e = entry_df.iloc[: i + 1].copy()
    if len(e) < EMA_SLOW + 10:
        return None

    e.ta.ema(length=EMA_FAST, append=True)
    e.ta.ema(length=EMA_MID,  append=True)
    e.ta.rsi(length=14,       append=True)

    ema_fast_s = e[f"EMA_{EMA_FAST}"].dropna()
    ema_mid_s  = e[f"EMA_{EMA_MID}"].dropna()
    rsi_s      = e["RSI_14"].dropna()

    if len(ema_fast_s) < SIGNAL_LOOKBACK + 2 or len(rsi_s) < 2:
        return None

    close_entry = float(e["close"].iloc[-1])
    ema_fast_e  = float(ema_fast_s.iloc[-1])
    ema_mid_e   = float(ema_mid_s.iloc[-1])
    rsi_current = float(rsi_s.iloc[-1])

    if any(math.isnan(v) for v in [ema_fast_e, ema_mid_e, rsi_current]):
        return None

    # Alinha com trend timeframe
    current_time = e.index[-1]
    t = trend_df[trend_df.index <= current_time].copy()
    if len(t) < EMA_SLOW + 10:
        return None

    t.ta.ema(length=EMA_MID,  append=True)
    t.ta.ema(length=EMA_SLOW, append=True)

    ema_mid_t  = float(t[f"EMA_{EMA_MID}"].dropna().iloc[-1])
    ema_slow_t = float(t[f"EMA_{EMA_SLOW}"].dropna().iloc[-1])

    if any(math.isnan(v) for v in [ema_mid_t, ema_slow_t]):
        return None

    trend_ok = ema_mid_t > ema_slow_t

    # Detecta cruzamento EMA_FAST acima de EMA_MID
    lookback = min(SIGNAL_LOOKBACK, len(ema_fast_s) - 2)
    cross_ok  = False
    for k in range(lookback):
        was_below = ema_fast_s.iloc[-(k + 2)] < ema_mid_s.iloc[-(k + 2)]
        is_above  = ema_fast_s.iloc[-(k + 1)] > ema_mid_s.iloc[-(k + 1)]
        if was_below and is_above:
            cross_ok = True
            break

    rsi_ok     = RSI_MIN <= rsi_current <= RSI_MAX
    lowest_low = float(e["low"].iloc[-SL_CANDLES:].min())

    return {
        "signal"      : trend_ok and cross_ok and rsi_ok,
        "trend_ok"    : trend_ok,
        "cross_ok"    : cross_ok,
        "rsi_ok"      : rsi_ok,
        "close_entry" : close_entry,
        "ema_fast_e"  : ema_fast_e,
        "ema_mid_e"   : ema_mid_e,
        "ema_mid_t"   : ema_mid_t,
        "ema_slow_t"  : ema_slow_t,
        "rsi_current" : rsi_current,
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
    warmup   = EMA_SLOW + 20

    for i in range(warmup, len(entry_df)):
        bar      = entry_df.iloc[i]
        bar_time = entry_df.index[i]
        high     = float(bar["high"])
        low      = float(bar["low"])
        close    = float(bar["close"])

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
                f"Entry={entry_price:.4f} | SL={sl_price:.4f} | TP={tp_price:.4f} | "
                f"RSI={ind['rsi_current']:.1f}"
            )

        else:
            entry = position["entry_price"]

            if high > position["highest_price"]:
                position["highest_price"] = high

            highest    = position["highest_price"]
            profit_pct = ((highest - entry) / entry) * 100.0

            if not position["trail_active"] and profit_pct >= TRAILING_ACTIVATION_PCT:
                position["trail_active"] = True
                position["trail_sl"]     = highest * (1.0 - TRAILING_STOP_PCT / 100.0)

            if position["trail_active"]:
                new_trail = highest * (1.0 - TRAILING_STOP_PCT / 100.0)
                if new_trail > position["trail_sl"]:
                    position["trail_sl"] = new_trail

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

    if position is not None:
        ep        = float(entry_df.iloc[-1]["close"])
        entry     = position["entry_price"]
        pnl_pct   = ((ep - entry) / entry) * 100.0
        risk_unit = entry - position["sl_price"]
        trades.append({
            "symbol"      : symbol,
            "entry_time"  : position["entry_time"].strftime("%Y-%m-%d %H:%M"),
            "exit_time"   : entry_df.index[-1].strftime("%Y-%m-%d %H:%M"),
            "entry_price" : entry,
            "exit_price"  : ep,
            "sl_price"    : position["sl_price"],
            "tp_price"    : position["tp_price"],
            "pnl_pct"     : round(pnl_pct, 2),
            "r_multiple"  : round(((ep - entry) / risk_unit) if risk_unit > 0 else 0.0, 2),
            "exit_reason" : "Em aberto (fim dos dados)",
            "win"         : pnl_pct > 0,
        })
        print(f"  ⏳ Posição ainda aberta | PnL={pnl_pct:+.2f}%")

    return trades


# ═════════════════════════════════════════════════════════════
# RELATÓRIO
# ═════════════════════════════════════════════════════════════

def print_report(all_trades: List[Dict]) -> None:
    if not all_trades:
        print("\n  Nenhuma operação encontrada.")
        print("  Dica: aumente --limit ou ajuste SIGNAL_LOOKBACK no .env\n")
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

        equity = 100.0
        peak   = equity
        max_dd = 0.0
        for t in trades:
            equity *= (1 + t["pnl_pct"] / 100)
            peak    = max(peak, equity)
            dd      = (peak - equity) / peak * 100
            max_dd  = max(max_dd, dd)

        print(f"\n  ── {symbol} " + "─" * (48 - len(symbol)))
        print(f"  Operações     : {len(trades)}  ({len(wins)}W / {len(losses)}L)")
        print(f"  Win Rate      : {win_rate:.1f}%")
        print(f"  PnL Total     : {total_pnl:+.2f}%")
        print(f"  Capital final : {equity:.2f}  (base 100)")
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

    if len(symbols) > 1:
        total     = len(all_trades)
        total_w   = sum(1 for t in all_trades if t["win"])
        total_pnl = sum(t["pnl_pct"] for t in all_trades)
        print(f"\n{'─' * 58}")
        print(f"  TOTAL: {total} ops | Win Rate: {total_w/total*100:.1f}% | PnL: {total_pnl:+.2f}%")

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
    parser = argparse.ArgumentParser(description="Backtest Triple EMA + RSI")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS)
    parser.add_argument("--limit",   type=int,  default=500)
    args = parser.parse_args()

    print(f"\n{'═' * 58}")
    print("  BACKTEST — Triple EMA Cross + RSI Filter")
    print(f"  Pares      : {' | '.join(args.symbols)}")
    print(f"  Timeframes : {TIMEFRAME_TREND.upper()} (tendência) / {TIMEFRAME_ENTRY.upper()} (entrada)")
    print(f"  EMAs       : {EMA_FAST} / {EMA_MID} / {EMA_SLOW}")
    print(f"  RSI zona   : {RSI_MIN} – {RSI_MAX}")
    print(f"  R/R        : 1:{RR_RATIO}  |  Risco: {RISK_PCT}%/op")
    print(f"  Trailing   : ativa em +{TRAILING_ACTIVATION_PCT}% | distância {TRAILING_STOP_PCT}%")
    print(f"  Candles    : {args.limit} por par")
    print(f"{'═' * 58}")

    try:
        exchange = create_exchange()
        exchange.load_markets()
    except Exception as exc:
        print(f"\n  ERRO ao conectar: {exc}")
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
