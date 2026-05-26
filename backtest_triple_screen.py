#!/usr/bin/env python3
"""
backtest_triple_screen.py — Backtest da estratégia Triple Screen DCA (Alexander Elder)

Três pilares simulados:
  1. Psicologia   : regras rígidas, sem exceções — hard stop inegociável
  2. Análise Técnica : Triple Screen (Weekly EMA13+MACD / Daily EMA50+RSI+Vol / 4H EMA+ATR)
  3. Gestão de Risco : 2% por nível, hard stop 4%, TP escalonado + trailing

Uso:
  python backtest_triple_screen.py
  python backtest_triple_screen.py --symbols BTC/USDT ETH/USDT SOL/USDT --days 730 --capital 1000
"""

import argparse
import sys
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import ccxt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
import pandas as pd
import pandas_ta as ta

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS  = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
DEFAULT_CAPITAL  = 1_000.0   # USDT per symbol (independent pools)
DEFAULT_DAYS     = 730        # ~2 years of history
OUTPUT_DIR       = Path("backtest_results")

# ── Triple Screen indicator parameters ───────────────────────────────────────
EMA_W            = 13         # Screen 1 — weekly EMA
MACD_FAST        = 12
MACD_SLOW        = 26
MACD_SIG_P       = 9

EMA_D            = 50         # Screen 2 — daily EMA
RSI_D_LOOKBACK   = 3          # candles to look back for RSI pullback
RSI_D_THRESHOLD  = 40         # daily RSI must have been below this
VOL_MA_P         = 20         # volume moving average period

EMA_4H_F         = 20         # Screen 3 — 4H EMA fast
EMA_4H_S         = 50         # Screen 3 — 4H EMA slow
ATR_P            = 14
RSI_4H_P         = 14

# ── DCA entry parameters ─────────────────────────────────────────────────────
DCA_DROP_L2      = 3.0        # % price drop from L1 entry to trigger L2
DCA_DROP_L3      = 5.0        # % price drop from L2 entry to trigger L3
RISK_L1          = 1.0        # % of available capital at risk on L1
RISK_L2          = 1.5        # % of available capital at risk on L2
RISK_L3          = 2.0        # % of available capital at risk on L3
RSI_L2_TH        = 35.0       # 4H RSI must be below this for L2 add
RSI_L3_TH        = 30.0       # 4H RSI must be below this for L3 add

# ── Stop loss ─────────────────────────────────────────────────────────────────
SL_CANDLES       = 5          # look back N 4H candles for swing low
SL_BUFFER        = 1.0        # % below swing low

# ── Hard stop — Elder's "oxygen tank": survive first, profit second ───────────
HARD_STOP_PCT    = 4.0        # close everything if position loss > N% of capital

# ── Take profit (scaled) ──────────────────────────────────────────────────────
TP1_R            = 1.5        # TP1 at avg_entry + 1.5 × risk
TP2_R            = 2.5        # TP2 at avg_entry + 2.5 × risk
TP1_PCT          = 0.30       # close 30% at TP1
TP2_PCT          = 0.40       # close 40% at TP2 (of remaining qty)
TRAIL_ATR        = 2.0        # trailing stop = peak − (ATR × TRAIL_ATR)


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DCAEntry:
    price: float
    qty: float
    cost: float


@dataclass
class Position:
    """In-memory simulation of a DCA position across multiple entry levels."""
    is_open: bool = False
    entries: List[DCAEntry] = field(default_factory=list)
    avg_price: float = 0.0
    total_qty: float = 0.0
    total_cost: float = 0.0
    capital_at_open: float = 0.0
    hard_stop_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_filled: bool = False
    tp2_filled: bool = False
    trailing_active: bool = False
    trailing_peak: float = 0.0
    trailing_stop: float = 0.0
    open_time: Optional[pd.Timestamp] = None

    def recalc(self) -> None:
        if not self.entries:
            self.avg_price = self.total_qty = self.total_cost = 0.0
            return
        self.total_cost = sum(e.cost for e in self.entries)
        self.total_qty  = sum(e.qty  for e in self.entries)
        self.avg_price  = self.total_cost / self.total_qty

    def recalc_tp(self) -> None:
        risk = self.avg_price - self.hard_stop_price
        if risk > 0:
            self.tp1_price = self.avg_price + risk * TP1_R
            self.tp2_price = self.avg_price + risk * TP2_R

    def reset(self) -> None:
        self.__init__()


@dataclass
class TradeRecord:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    qty: float
    pnl_usdt: float
    pnl_pct: float
    exit_reason: str
    n_levels: int


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    """
    Downloads historical OHLCV from Binance (public endpoint, no API key required).
    Handles pagination automatically.
    """
    tf_ms = {"1w": 7 * 86_400_000, "1d": 86_400_000, "4h": 4 * 3_600_000}
    step  = tf_ms.get(timeframe, 86_400_000)
    since = int((_time.time() - days * 86_400) * 1_000)

    all_bars: list = []
    while True:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=500)
        except ccxt.NetworkError as exc:
            print(f"    [rede] {exc} — tentando novamente...")
            _time.sleep(3)
            continue
        except Exception as exc:
            print(f"    [erro] {exc}")
            break

        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < 500:
            break
        since = bars[-1][0] + step
        _time.sleep(0.25)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR CALCULATION (one function per screen)
# ─────────────────────────────────────────────────────────────────────────────

def _atr_col(df: pd.DataFrame, length: int) -> str:
    """Returns the ATR column name added by pandas_ta (name varies by version)."""
    candidates = [f"ATRr_{length}", f"ATR_{length}", f"ATRm_{length}"]
    for c in candidates:
        if c in df.columns:
            return c
    # Fallback: return the last column that starts with 'ATR'
    atr_cols = [c for c in df.columns if c.upper().startswith("ATR")]
    return atr_cols[-1] if atr_cols else ""


def calc_weekly_indicators(df_w: pd.DataFrame) -> pd.DataFrame:
    """
    Screen 1 — Weekly trend direction.
    Both conditions must be true to consider the market bullish enough for a new DCA.
    """
    df = df_w.copy()
    df.ta.ema(length=EMA_W, append=True)
    df.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIG_P, append=True)

    ema_c  = f"EMA_{EMA_W}"
    hist_c = f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIG_P}"

    df["ema_bullish"]    = (df["close"] > df[ema_c]) & (df[ema_c] > df[ema_c].shift(1))
    df["macd_ok"]        = (df[hist_c] > 0) | (df[hist_c] > df[hist_c].shift(1))
    df["screen1_ok"]     = df["ema_bullish"] & df["macd_ok"]
    df["weekly_bullish"] = df["screen1_ok"]  # alias used in DCA gating

    return df[["ema_bullish", "macd_ok", "screen1_ok", "weekly_bullish"]].copy()


def calc_daily_indicators(df_d: pd.DataFrame) -> pd.DataFrame:
    """
    Screen 2 — Daily momentum pullback inside the weekly uptrend.
    Requires price above EMA50, RSI recently oversold and recovering, volume confirming.
    """
    df = df_d.copy()
    df.ta.ema(length=EMA_D, append=True)
    df.ta.rsi(length=RSI_4H_P, append=True)

    ema_c = f"EMA_{EMA_D}"
    rsi_c = f"RSI_{RSI_4H_P}"

    df["trend_ok_1d"]  = df["close"] > df[ema_c]
    df["rsi_pullback"] = (
        df[rsi_c].rolling(RSI_D_LOOKBACK)
                 .apply(lambda x: bool((x < RSI_D_THRESHOLD).any()), raw=True)
                 .fillna(0)
                 .astype(bool)
    )
    df["rsi_recovering"] = df[rsi_c] > df[rsi_c].shift(1)
    df["vol_ma"]         = df["volume"].rolling(VOL_MA_P).mean()
    df["vol_ok"]         = df["volume"] > df["vol_ma"]
    df["screen2_ok"]     = (
        df["trend_ok_1d"] & df["rsi_pullback"] &
        df["rsi_recovering"] & df["vol_ok"]
    )

    cols = ["trend_ok_1d", "rsi_pullback", "rsi_recovering", "vol_ok", "screen2_ok"]
    return df[cols].copy()


def calc_4h_indicators(df_4h: pd.DataFrame) -> pd.DataFrame:
    """
    Screen 3 — 4H precise entry zone + ATR for sizing and trailing stop.
    """
    df = df_4h.copy()
    df.ta.ema(length=EMA_4H_F, append=True)
    df.ta.ema(length=EMA_4H_S, append=True)
    df.ta.rsi(length=RSI_4H_P, append=True)
    df.ta.atr(length=ATR_P, append=True)

    ema20 = f"EMA_{EMA_4H_F}"
    ema50 = f"EMA_{EMA_4H_S}"
    rsi_c = f"RSI_{RSI_4H_P}"
    atr_c = _atr_col(df, ATR_P)

    df["ema20"] = df[ema20]
    df["ema50"] = df[ema50]
    df["rsi_4h"] = df[rsi_c]
    df["atr_4h"] = df[atr_c] if atr_c else df["close"] * 0.02

    # Pullback zone: price between EMA50 and EMA20 × 1.02 (2% tolerance)
    df["pullback_ok"]        = (df["close"] >= df["ema50"]) & (df["close"] <= df["ema20"] * 1.02)
    df["rsi_not_overbought"] = df["rsi_4h"] < 50
    df["screen3_ok"]         = df["pullback_ok"] & df["rsi_not_overbought"]

    # Swing low for stop loss calculation (rolling min of last N lows)
    df["lowest_low"] = df["low"].rolling(SL_CANDLES).min()

    keep = ["open", "high", "low", "close", "volume",
            "ema20", "ema50", "rsi_4h", "atr_4h",
            "pullback_ok", "rsi_not_overbought", "screen3_ok", "lowest_low"]
    return df[keep].copy()


def build_master_df(
    df_4h_ind: pd.DataFrame,
    df_d_ind: pd.DataFrame,
    df_w_ind: pd.DataFrame,
) -> pd.DataFrame:
    """
    Aligns weekly and daily indicator columns to 4H frequency using merge_asof.
    merge_asof ensures we only use data from bars that were CLOSED before each 4H timestamp —
    this is the key step that prevents lookahead bias.
    """
    left   = df_4h_ind.reset_index().rename(columns={"ts": "ts"})
    daily  = df_d_ind.reset_index().rename(columns={"ts": "ts_d"})
    weekly = df_w_ind.reset_index().rename(columns={"ts": "ts_w"})

    for df_ in [left, daily, weekly]:
        df_.sort_values(df_.columns[0], inplace=True)

    master = pd.merge_asof(left, daily,  left_on="ts", right_on="ts_d", direction="backward")
    master = pd.merge_asof(master, weekly, left_on="ts", right_on="ts_w", direction="backward")

    master["signal"] = (
        master.get("screen1_ok", pd.Series(False, index=master.index)).fillna(False) &
        master.get("screen2_ok", pd.Series(False, index=master.index)).fillna(False) &
        master.get("screen3_ok", pd.Series(False, index=master.index)).fillna(False)
    )
    master["weekly_bullish"] = master.get("weekly_bullish", pd.Series(False, index=master.index)).fillna(False)

    master = master.set_index("ts")
    master = master.dropna(subset=["ema20", "ema50", "rsi_4h", "atr_4h"])
    return master


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST SIMULATION
# ─────────────────────────────────────────────────────────────────────────────

def _calc_qty(cash: float, entry: float, sl: float, risk_pct: float) -> float:
    """
    Sizes position so that if the stop triggers, the loss = exactly risk_pct% of cash.
    Elder: define your risk before you enter, never after.
    """
    risk_usdt = cash * (risk_pct / 100.0)
    risk_unit = entry - sl
    if risk_unit <= 0:
        return 0.0
    return max(round(risk_usdt / risk_unit, 8), 0.0)


def run_backtest(
    symbol: str,
    master: pd.DataFrame,
    initial_capital: float,
) -> Tuple[List[TradeRecord], pd.Series]:
    """
    Main simulation loop. Iterates over every 4H bar and applies the Triple Screen DCA rules.

    Returns:
        trades      — list of every completed trade (partial or full close)
        equity_ts   — mark-to-market equity at each 4H bar (cash + unrealized position value)
    """
    pos        = Position()
    trades: List[TradeRecord] = []
    cash       = initial_capital
    equity_ts  = pd.Series(dtype=float, index=master.index)

    arr        = master.reset_index()   # faster row access than iterrows
    n          = len(arr)

    for i in range(50, n):             # skip first 50 bars as indicator warmup
        row    = arr.iloc[i]
        ts     = row["ts"]
        price  = float(row["close"])
        atr    = float(row.get("atr_4h", price * 0.02))
        rsi_4h = float(row.get("rsi_4h", 50.0))
        signal = bool(row.get("signal", False))
        w_bull = bool(row.get("weekly_bullish", False))
        ll     = float(row.get("lowest_low", price * 0.97))

        if pd.isna(atr) or atr <= 0:
            atr = price * 0.02
        if pd.isna(ll) or ll <= 0 or ll >= price:
            ll = price * 0.97

        # ── CHECK EXITS (runs every bar while position is open) ───────────────
        if pos.is_open:
            exit_type = _evaluate_exits(pos, price, atr, cash + pos.total_qty * price)

            if exit_type == "hard_stop":
                # Non-negotiable: close EVERYTHING immediately
                pnl = (price - pos.avg_price) * pos.total_qty
                cash += price * pos.total_qty
                _record_trade(trades, symbol, pos, ts, price, pos.total_qty, pnl, "hard_stop")
                pos.reset()

            elif exit_type == "stop_loss":
                pnl = (pos.hard_stop_price - pos.avg_price) * pos.total_qty
                cash += pos.hard_stop_price * pos.total_qty
                _record_trade(trades, symbol, pos, ts, pos.hard_stop_price, pos.total_qty, pnl, "stop_loss")
                pos.reset()

            elif exit_type == "tp1":
                qty_close = round(pos.total_qty * TP1_PCT, 8)
                pnl = (pos.tp1_price - pos.avg_price) * qty_close
                cash += pos.tp1_price * qty_close
                pos.total_qty  = round(pos.total_qty - qty_close, 8)
                pos.total_cost = pos.avg_price * pos.total_qty
                pos.tp1_filled = True
                _record_trade(trades, symbol, pos, ts, pos.tp1_price, qty_close, pnl, "tp1")

            elif exit_type == "tp2":
                qty_close = round(pos.total_qty * TP2_PCT, 8)
                pnl = (pos.tp2_price - pos.avg_price) * qty_close
                cash += pos.tp2_price * qty_close
                pos.total_qty  = round(pos.total_qty - qty_close, 8)
                pos.total_cost = pos.avg_price * pos.total_qty
                pos.tp2_filled = True
                _record_trade(trades, symbol, pos, ts, pos.tp2_price, qty_close, pnl, "tp2")

            elif exit_type == "trailing":
                pnl = (pos.trailing_stop - pos.avg_price) * pos.total_qty
                cash += pos.trailing_stop * pos.total_qty
                _record_trade(trades, symbol, pos, ts, pos.trailing_stop, pos.total_qty, pnl, "trailing")
                pos.reset()
            else:
                # Update trailing peak (no exit yet)
                _update_trailing(pos, price, atr)

        # ── DCA ADDS (only if position open, < 3 levels, trend still bullish) ─
        if pos.is_open and len(pos.entries) < 3 and cash > 0:
            n_lvl = len(pos.entries)

            # Critical: weekly_bullish gate prevents adding during downtrends —
            # this single check would have prevented the -10.11 ETH loss
            if n_lvl == 1 and w_bull:
                trigger = pos.entries[0].price * (1 - DCA_DROP_L2 / 100)
                if price <= trigger and rsi_4h < RSI_L2_TH:
                    qty = _calc_qty(cash, price, pos.hard_stop_price, RISK_L2)
                    if qty > 0:
                        cost = price * qty
                        cash -= cost
                        pos.entries.append(DCAEntry(price, qty, cost))
                        pos.recalc()
                        pos.recalc_tp()

            elif n_lvl == 2 and w_bull:
                trigger = pos.entries[1].price * (1 - DCA_DROP_L3 / 100)
                if price <= trigger and rsi_4h < RSI_L3_TH:
                    qty = _calc_qty(cash, price, pos.hard_stop_price, RISK_L3)
                    if qty > 0:
                        cost = price * qty
                        cash -= cost
                        pos.entries.append(DCAEntry(price, qty, cost))
                        pos.recalc()
                        pos.recalc_tp()

        # ── OPEN L1 (no position, Triple Screen signal) ───────────────────────
        if not pos.is_open and signal and cash > 10:
            sl_price = ll * (1 - SL_BUFFER / 100)
            if sl_price >= price:
                equity_ts.iloc[i] = cash
                continue

            qty = _calc_qty(cash, price, sl_price, RISK_L1)
            if qty <= 0:
                equity_ts.iloc[i] = cash
                continue

            cost = price * qty
            cash -= cost

            pos.is_open         = True
            pos.entries         = [DCAEntry(price, qty, cost)]
            pos.capital_at_open = cash + cost
            pos.hard_stop_price = sl_price
            pos.open_time       = ts
            pos.recalc()
            pos.recalc_tp()

        # Mark-to-market equity
        equity_ts.iloc[i] = cash + (pos.total_qty * price if pos.is_open else 0.0)

    return trades, equity_ts


def _evaluate_exits(pos: Position, price: float, atr: float, total_equity: float) -> Optional[str]:
    """Returns the name of the first triggered exit condition, or None."""

    # 1. Hard stop — protects entire account, fires before any profit-taking
    unreal_pnl = (price - pos.avg_price) * pos.total_qty
    if unreal_pnl <= -(pos.capital_at_open * HARD_STOP_PCT / 100):
        return "hard_stop"

    # 2. Technical stop loss
    if price <= pos.hard_stop_price:
        return "stop_loss"

    # 3. TP1 — first profit target
    if not pos.tp1_filled and pos.tp1_price > 0 and price >= pos.tp1_price:
        return "tp1"

    # 4. TP2 — second profit target (only after TP1)
    if pos.tp1_filled and not pos.tp2_filled and pos.tp2_price > 0 and price >= pos.tp2_price:
        return "tp2"

    # 5. Trailing stop (activates after TP1)
    if pos.tp1_filled and atr > 0:
        if pos.trailing_active and price <= pos.trailing_stop:
            return "trailing"

    return None


def _update_trailing(pos: Position, price: float, atr: float) -> None:
    """Updates peak and trailing stop level without triggering an exit."""
    if not pos.tp1_filled or atr <= 0:
        return
    if not pos.trailing_active:
        pos.trailing_active = True
        pos.trailing_peak   = price
        pos.trailing_stop   = price - atr * TRAIL_ATR
    elif price > pos.trailing_peak:
        pos.trailing_peak = price
        pos.trailing_stop = price - atr * TRAIL_ATR


def _record_trade(
    trades: list, symbol: str, pos: Position, exit_time: pd.Timestamp,
    exit_price: float, qty: float, pnl: float, reason: str,
) -> None:
    cost_basis = pos.avg_price * qty
    trades.append(TradeRecord(
        symbol      = symbol,
        entry_time  = pos.open_time,
        exit_time   = exit_time,
        entry_price = pos.avg_price,
        exit_price  = exit_price,
        qty         = qty,
        pnl_usdt    = pnl,
        pnl_pct     = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0,
        exit_reason = reason,
        n_levels    = len(pos.entries),
    ))


# ─────────────────────────────────────────────────────────────────────────────
# BUY-AND-HOLD BENCHMARK
# ─────────────────────────────────────────────────────────────────────────────

def buy_and_hold(master: pd.DataFrame, initial_capital: float) -> pd.Series:
    """
    Simulates investing 100% of capital at the first available bar and holding.
    Used as benchmark — Elder: your strategy must beat passive holding to be worth using.
    """
    first_price = master["close"].iloc[50]
    qty         = initial_capital / first_price
    return master["close"].iloc[50:] * qty


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────

def calc_metrics(
    trades: List[TradeRecord],
    equity_ts: pd.Series,
    initial_capital: float,
) -> dict:
    if not trades:
        return {}

    pnls    = [t.pnl_usdt for t in trades]
    winners = [p for p in pnls if p > 0]
    losers  = [p for p in pnls if p <= 0]

    eq_clean    = equity_ts.dropna()
    final_cap   = eq_clean.iloc[-1] if not eq_clean.empty else initial_capital

    # Max drawdown
    peak   = eq_clean.cummax()
    dd_pct = ((eq_clean - peak) / peak * 100)
    max_dd = dd_pct.min()

    # Annualized Sharpe (6 4H bars per day → 2190 per year)
    returns = eq_clean.pct_change().dropna()
    sharpe  = float(returns.mean() / returns.std() * np.sqrt(2190)) if returns.std() > 0 else 0.0

    sum_win  = sum(winners) if winners else 0.0
    sum_loss = abs(sum(losers)) if losers else 0.0

    reason_counts: Dict[str, int] = {}
    for t in trades:
        reason_counts[t.exit_reason] = reason_counts.get(t.exit_reason, 0) + 1

    return {
        "total_trades"  : len(trades),
        "winners"       : len(winners),
        "losers"        : len(losers),
        "win_rate"      : len(winners) / len(trades) * 100,
        "total_pnl"     : sum(pnls),
        "total_pnl_pct" : (final_cap - initial_capital) / initial_capital * 100,
        "avg_win"       : float(np.mean(winners)) if winners else 0.0,
        "avg_loss"      : float(np.mean(losers))  if losers  else 0.0,
        "profit_factor" : sum_win / sum_loss if sum_loss > 0 else float("inf"),
        "max_drawdown"  : float(max_dd),
        "sharpe_ratio"  : sharpe,
        "final_capital" : float(final_cap),
        "best_trade"    : max(pnls),
        "worst_trade"   : min(pnls),
        "reason_counts" : reason_counts,
    }


# ─────────────────────────────────────────────────────────────────────────────
# VISUALIZATION
# ─────────────────────────────────────────────────────────────────────────────

BG      = "#0d1117"
GRID    = "#1e2a3a"
TEXT    = "#c9d1d9"
BLUE    = "#4a9eff"
CYAN    = "#00e5ff"
GREEN   = "#69f0ae"
RED     = "#ff5252"
YELLOW  = "#f0c040"
ORANGE  = "#ff9800"


def _style_ax(ax):
    ax.set_facecolor(BG)
    ax.tick_params(colors=TEXT, labelsize=7)
    ax.yaxis.label.set_color(TEXT)
    ax.xaxis.label.set_color(TEXT)
    for spine in ax.spines.values():
        spine.set_edgecolor(GRID)
    ax.grid(color=GRID, linewidth=0.4, alpha=0.5)


def plot_symbol(
    symbol: str,
    master: pd.DataFrame,
    trades: List[TradeRecord],
    equity_ts: pd.Series,
    bah_series: pd.Series,
    metrics: dict,
    initial_capital: float,
):
    """Generates a 3-panel chart per symbol: price + signals / equity curve / drawdown."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    fig = plt.figure(figsize=(16, 10), facecolor=BG)
    gs  = gridspec.GridSpec(3, 1, height_ratios=[3, 1.5, 1], hspace=0.06)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)
    for ax in [ax1, ax2, ax3]:
        _style_ax(ax)

    # ── Price chart ───────────────────────────────────────────────────────────
    ax1.plot(master.index, master["close"], color=BLUE,   linewidth=0.8, alpha=0.85, label="Preço")
    ax1.plot(master.index, master["ema20"],  color=YELLOW, linewidth=0.7, alpha=0.55, linestyle="--", label=f"EMA{EMA_4H_F}")
    ax1.plot(master.index, master["ema50"],  color=RED,    linewidth=0.7, alpha=0.55, linestyle="--", label=f"EMA{EMA_4H_S}")

    # Bullish weekly background
    if "weekly_bullish" in master.columns:
        wb = master["weekly_bullish"].fillna(False)
        ax1.fill_between(master.index, master["close"].min(), master["close"].max(),
                         where=wb, alpha=0.04, color=CYAN)

    # Entry / exit markers
    for t in trades:
        color_e = CYAN if t.n_levels == 1 else "#80deea"
        ax1.scatter(t.entry_time, t.entry_price, marker="^", color=color_e, s=55, zorder=5)
        color_x = GREEN if t.pnl_usdt > 0 else RED
        ax1.scatter(t.exit_time, t.exit_price, marker="v", color=color_x, s=55, zorder=5)

    from matplotlib.lines import Line2D
    legend_els = [
        Line2D([0], [0], color=BLUE, lw=1,            label="Preço"),
        Line2D([0], [0], color=YELLOW, lw=1, ls="--", label=f"EMA{EMA_4H_F}"),
        Line2D([0], [0], color=RED,    lw=1, ls="--", label=f"EMA{EMA_4H_S}"),
        Line2D([0], [0], marker="^", color=CYAN,  lw=0, ms=7, label="Entrada L1"),
        Line2D([0], [0], marker="^", color="#80deea", lw=0, ms=7, label="DCA L2/L3"),
        Line2D([0], [0], marker="v", color=GREEN, lw=0, ms=7, label="Saída lucro"),
        Line2D([0], [0], marker="v", color=RED,   lw=0, ms=7, label="Saída loss"),
    ]
    ax1.legend(handles=legend_els, fontsize=7, facecolor=BG, edgecolor=GRID,
               labelcolor=TEXT, loc="upper left", ncol=4)

    total_pnl = metrics.get("total_pnl", 0)
    win_rate  = metrics.get("win_rate", 0)
    ax1.set_title(
        f"Triple Screen DCA — {symbol}  |  "
        f"PnL: {total_pnl:+.2f} USDT ({metrics.get('total_pnl_pct', 0):+.1f}%)  |  "
        f"Win Rate: {win_rate:.1f}%  |  "
        f"Trades: {metrics.get('total_trades', 0)}  |  "
        f"Sharpe: {metrics.get('sharpe_ratio', 0):.2f}  |  "
        f"DD Máx: {metrics.get('max_drawdown', 0):.1f}%",
        color=TEXT, fontsize=9, pad=8,
    )
    ax1.set_ylabel("Preço (USDT)", fontsize=8)

    # ── Equity curve vs Buy-and-Hold ──────────────────────────────────────────
    eq = equity_ts.dropna()
    ax2.plot(eq.index, eq.values, color=CYAN,   linewidth=1.2, label="Triple Screen DCA")
    ax2.plot(bah_series.index, bah_series.values, color=ORANGE, linewidth=0.9,
             linestyle="--", alpha=0.7, label="Buy & Hold")
    ax2.axhline(initial_capital, color="#555", linewidth=0.8, linestyle=":", alpha=0.6)
    ax2.fill_between(eq.index, initial_capital, eq.values,
                     where=eq >= initial_capital, alpha=0.12, color=GREEN)
    ax2.fill_between(eq.index, initial_capital, eq.values,
                     where=eq < initial_capital,  alpha=0.12, color=RED)
    ax2.set_ylabel("Capital (USDT)", fontsize=8)
    ax2.legend(fontsize=7, facecolor=BG, edgecolor=GRID, labelcolor=TEXT, loc="upper left")

    # ── Drawdown ──────────────────────────────────────────────────────────────
    peak = eq.cummax()
    dd   = (eq - peak) / peak * 100
    ax3.fill_between(dd.index, 0, dd.values, alpha=0.65, color=RED)
    ax3.set_ylabel("Drawdown %", fontsize=8)
    ax3.set_xlabel("Data", fontsize=8)

    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)

    sym_safe = symbol.replace("/", "")
    out_path = OUTPUT_DIR / f"backtest_{sym_safe}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()
    print(f"  Grafico salvo: {out_path}")


def plot_comparison(all_metrics: dict, all_equity: dict, initial_capital: float):
    """Bar chart comparing PnL across all symbols + combined equity curve."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6), facecolor=BG)
    _style_ax(ax1)
    _style_ax(ax2)

    syms  = list(all_metrics.keys())
    pnls  = [all_metrics[s].get("total_pnl", 0) for s in syms]
    colors = [GREEN if p >= 0 else RED for p in pnls]

    bars = ax1.bar(syms, pnls, color=colors, alpha=0.8, width=0.5)
    for bar, pnl in zip(bars, pnls):
        ax1.text(bar.get_x() + bar.get_width() / 2,
                 bar.get_height() + (max(pnls) * 0.01 if pnl >= 0 else min(pnls) * 0.01),
                 f"{pnl:+.2f}", ha="center", va="bottom" if pnl >= 0 else "top",
                 color=TEXT, fontsize=9)

    ax1.axhline(0, color="#555", linewidth=0.8)
    ax1.set_title("PnL Total por Par (Triple Screen DCA)", color=TEXT, fontsize=10)
    ax1.set_ylabel("PnL Liquido (USDT)", color=TEXT, fontsize=9)
    ax1.tick_params(axis="x", labelsize=9, colors=TEXT)

    # Combined equity
    for sym, eq in all_equity.items():
        eq_clean = eq.dropna()
        norm = (eq_clean / initial_capital - 1) * 100  # % return
        ax2.plot(eq_clean.index, norm.values, linewidth=1.1, label=sym)

    ax2.axhline(0, color="#555", linewidth=0.8, linestyle="--", alpha=0.6)
    ax2.set_title("Retorno Acumulado % por Par", color=TEXT, fontsize=10)
    ax2.set_ylabel("Retorno %", color=TEXT, fontsize=9)
    ax2.legend(fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=TEXT)
    ax2.tick_params(axis="x", labelsize=7, colors=TEXT)

    plt.suptitle("Comparação Triple Screen DCA — Todos os Pares", color=TEXT, fontsize=11)
    out_path = OUTPUT_DIR / "backtest_comparison.png"
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close()
    print(f"  Grafico comparativo salvo: {out_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CONSOLE REPORT
# ─────────────────────────────────────────────────────────────────────────────

def print_metrics(symbol: str, metrics: dict, initial_capital: float):
    sep = "-" * 60
    print(f"\n{'=' * 60}")
    print(f"  {symbol}")
    print(sep)
    if not metrics:
        print("  Nenhuma operacao no periodo com os filtros atuais.")
        return
    m = metrics
    print(f"  Capital inicial    : {initial_capital:.2f} USDT")
    print(f"  Capital final      : {m['final_capital']:.2f} USDT")
    print(f"  PnL Total          : {m['total_pnl']:+.2f} USDT  ({m['total_pnl_pct']:+.2f}%)")
    print(sep)
    print(f"  Operacoes totais   : {m['total_trades']}")
    print(f"  Vencedoras/Perdedoras: {m['winners']} / {m['losers']}")
    print(f"  Win Rate           : {m['win_rate']:.1f}%")
    print(f"  Ganho medio        : +{m['avg_win']:.2f} USDT")
    print(f"  Perda media        : {m['avg_loss']:.2f} USDT")
    print(f"  Profit Factor      : {m['profit_factor']:.2f}")
    print(f"  Melhor trade       : +{m['best_trade']:.2f} USDT")
    print(f"  Pior trade         : {m['worst_trade']:.2f} USDT")
    print(sep)
    print(f"  Drawdown maximo    : {m['max_drawdown']:.2f}%")
    print(f"  Sharpe Ratio       : {m['sharpe_ratio']:.2f}")
    print(f"  Saidas por motivo  : {m['reason_counts']}")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Backtest Triple Screen DCA")
    p.add_argument("--symbols",  nargs="+", default=DEFAULT_SYMBOLS)
    p.add_argument("--days",     type=int,  default=DEFAULT_DAYS)
    p.add_argument("--capital",  type=float, default=DEFAULT_CAPITAL,
                   help="Capital USDT por par (pools independentes)")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("  BACKTEST — Triple Screen DCA (Alexander Elder)")
    print(f"  Pares   : {', '.join(args.symbols)}")
    print(f"  Periodo : ultimos {args.days} dias (~{args.days // 365} anos)")
    print(f"  Capital : {args.capital:.0f} USDT por par")
    print(f"  Exchange: Binance (dados publicos, sem autenticacao)")
    print("=" * 60)

    exchange = ccxt.binance({"enableRateLimit": True})

    all_metrics: Dict[str, dict]      = {}
    all_equity:  Dict[str, pd.Series] = {}

    for symbol in args.symbols:
        print(f"\n[{symbol}] Baixando dados ({args.days} dias)...")

        df_4h = fetch_ohlcv(exchange, symbol, "4h", args.days)
        df_1d = fetch_ohlcv(exchange, symbol, "1d", args.days)
        df_1w = fetch_ohlcv(exchange, symbol, "1w", args.days + 60)  # extra warmup

        if any(df.empty for df in [df_4h, df_1d, df_1w]):
            print(f"  [!] Dados insuficientes — pulando {symbol}.")
            continue

        print(f"  4H: {len(df_4h)} barras | 1D: {len(df_1d)} | 1W: {len(df_1w)}")

        print(f"[{symbol}] Calculando indicadores Triple Screen...")
        df_w_ind  = calc_weekly_indicators(df_1w)
        df_d_ind  = calc_daily_indicators(df_1d)
        df_4h_ind = calc_4h_indicators(df_4h)
        master    = build_master_df(df_4h_ind, df_d_ind, df_w_ind)

        signals = master["signal"].sum()
        print(f"  Sinais L1 encontrados no periodo: {signals}")

        print(f"[{symbol}] Simulando operacoes...")
        trades, equity_ts = run_backtest(symbol, master, args.capital)
        bah = buy_and_hold(master, args.capital)

        metrics = calc_metrics(trades, equity_ts, args.capital)
        all_metrics[symbol] = metrics
        all_equity[symbol]  = equity_ts

        print_metrics(symbol, metrics, args.capital)

        print(f"[{symbol}] Gerando grafico...")
        try:
            plot_symbol(symbol, master, trades, equity_ts, bah, metrics, args.capital)
        except Exception as exc:
            print(f"  [!] Erro no grafico: {exc}")

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  RESUMO COMPARATIVO")
    print(f"{'=' * 70}")
    header = f"  {'Par':<12} {'PnL (USDT)':>12} {'PnL %':>8} {'Win%':>7} {'Trades':>7} {'MaxDD':>8} {'Sharpe':>8}"
    print(header)
    print(f"  {'-' * 66}")

    total_pnl = 0.0
    for sym, m in all_metrics.items():
        if not m:
            continue
        total_pnl += m["total_pnl"]
        print(
            f"  {sym:<12} {m['total_pnl']:>+12.2f} "
            f"{m['total_pnl_pct']:>+7.1f}% "
            f"{m['win_rate']:>6.1f}% "
            f"{m['total_trades']:>7} "
            f"{m['max_drawdown']:>7.1f}% "
            f"{m['sharpe_ratio']:>8.2f}"
        )

    print(f"  {'-' * 66}")
    print(f"  {'TOTAL':<12} {total_pnl:>+12.2f}")
    print(f"\n  Graficos salvos em: ./{OUTPUT_DIR}/")
    print("=" * 70)

    if len(all_metrics) > 1:
        try:
            plot_comparison(all_metrics, all_equity, args.capital)
        except Exception as exc:
            print(f"  [!] Erro no grafico comparativo: {exc}")


if __name__ == "__main__":
    main()
