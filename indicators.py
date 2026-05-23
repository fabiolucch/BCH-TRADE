"""
indicators.py — Estratégia Triple EMA Cross + RSI Filter.
"""
import logging
from typing import Dict, Any
import ccxt
import pandas as pd
import pandas_ta as ta
from config import (
    TIMEFRAME_TREND, TIMEFRAME_ENTRY, OHLCV_LIMIT, EMA_FAST, EMA_MID, EMA_SLOW,
    RSI_MIN, RSI_MAX, SIGNAL_LOOKBACK, SL_CANDLES,
)

logger = logging.getLogger("bot")

def fetch_ohlcv(exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        return df
    except Exception as exc:
        logger.error(f"Erro ao buscar OHLCV {symbol} {timeframe}: {exc}")
        raise

def calculate_indicators(exchange: ccxt.Exchange, symbol: str) -> Dict[str, Any]:
    df_trend = fetch_ohlcv(exchange, symbol, TIMEFRAME_TREND, OHLCV_LIMIT)
    df_entry = fetch_ohlcv(exchange, symbol, TIMEFRAME_ENTRY, OHLCV_LIMIT)

    if df_trend.empty or df_entry.empty:
        return {"signal": False, "error": "Dados insuficientes"}

    df_trend.ta.ema(length=EMA_MID, append=True)
    df_trend.ta.ema(length=EMA_SLOW, append=True)

    close_trend = float(df_trend["close"].iloc[-1])
    ema_mid_t   = float(df_trend[f"EMA_{EMA_MID}"].iloc[-1])
    ema_slow_t  = float(df_trend[f"EMA_{EMA_SLOW}"].iloc[-1])
    trend_ok    = ema_mid_t > ema_slow_t

    df_entry.ta.ema(length=EMA_FAST, append=True)
    df_entry.ta.ema(length=EMA_MID, append=True)
    df_entry.ta.rsi(length=14, append=True)

    ema_fast_series = df_entry[f"EMA_{EMA_FAST}"].dropna()
    ema_mid_series  = df_entry[f"EMA_{EMA_MID}"].dropna()
    rsi_series      = df_entry["RSI_14"].dropna()

    close_entry = float(df_entry["close"].iloc[-1])
    ema_fast_e  = float(ema_fast_series.iloc[-1])
    ema_mid_e   = float(ema_mid_series.iloc[-1])
    rsi_current = float(rsi_series.iloc[-1])

    lookback = min(SIGNAL_LOOKBACK, len(ema_fast_series) - 2)
    cross_ok = False
    for k in range(lookback):
        was_below = ema_fast_series.iloc[-(k + 2)] < ema_mid_series.iloc[-(k + 2)]
        is_above  = ema_fast_series.iloc[-(k + 1)] > ema_mid_series.iloc[-(k + 1)]
        if was_below and is_above:
            cross_ok = True
            break

    rsi_ok = RSI_MIN <= rsi_current <= RSI_MAX
    lowest_low = float(df_entry["low"].iloc[-SL_CANDLES:].min())

    signal = trend_ok and cross_ok and rsi_ok

    logger.info(
        f"[{symbol}] [INDICADORES] "
        f"{TIMEFRAME_TREND.upper()}: Tendência={'✓' if trend_ok else '✗'} | "
        f"{TIMEFRAME_ENTRY.upper()}: RSI={rsi_current:.2f} | Cross={'✓' if cross_ok else '✗'}"
    )

    return {
        "close_trend": close_trend, "ema_mid_t": ema_mid_t, "ema_slow_t": ema_slow_t, "trend_ok": trend_ok,
        "close_entry": close_entry, "ema_fast_e": ema_fast_e, "ema_mid_e": ema_mid_e, "rsi_current": rsi_current,
        "cross_ok": cross_ok, "rsi_ok": rsi_ok, "lowest_low_5c": lowest_low, "signal": signal,
    }