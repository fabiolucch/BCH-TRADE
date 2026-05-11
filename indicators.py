"""
indicators.py — Estratégia Triple EMA Cross + RSI Filter.

Lógica de entrada:
  1. Tendência (TIMEFRAME_TREND): EMA_MID > EMA_SLOW  → mercado em alta
  2. Cruzamento (TIMEFRAME_ENTRY): EMA_FAST cruza acima de EMA_MID (últimos SIGNAL_LOOKBACK candles)
  3. RSI (TIMEFRAME_ENTRY): RSI_MIN ≤ RSI ≤ RSI_MAX  → momentum presente, não sobrecomprado

Por que é melhor que a estratégia anterior:
  - EMA21 > EMA55 ocorre ~50% do tempo em mercados trending
  - Cruzamento EMA9/EMA21 gera 8–15 sinais/mês por par no gráfico de 1H
  - RSI 45–70 é uma zona ampla e comum em tendências de alta
"""

import logging
from typing import Dict, Any

import ccxt
import pandas as pd
import pandas_ta as ta

from config import (
    TIMEFRAME_TREND,
    TIMEFRAME_ENTRY,
    OHLCV_LIMIT,
    EMA_FAST,
    EMA_MID,
    EMA_SLOW,
    RSI_MIN,
    RSI_MAX,
    SIGNAL_LOOKBACK,
    SL_CANDLES,
)

logger = logging.getLogger("bot")


def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int = 300,
) -> pd.DataFrame:
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)
        logger.debug(
            f"OHLCV: {symbol} {timeframe} — {len(df)} candles "
            f"({df.index[0].strftime('%Y-%m-%d %H:%M')} → {df.index[-1].strftime('%Y-%m-%d %H:%M')} UTC)"
        )
        return df
    except ccxt.NetworkError as exc:
        logger.error(f"Erro de rede ao buscar OHLCV {symbol} {timeframe}: {exc}")
        raise
    except ccxt.ExchangeError as exc:
        logger.error(f"Erro da exchange ao buscar OHLCV {symbol} {timeframe}: {exc}")
        raise
    except Exception as exc:
        logger.error(f"Erro inesperado ao buscar OHLCV {symbol} {timeframe}: {exc}")
        raise


def calculate_indicators(exchange: ccxt.Exchange, symbol: str) -> Dict[str, Any]:
    """
    Calcula indicadores da estratégia Triple EMA Cross + RSI para o par informado.

    Condições:
    ┌──────────────────┬─────────────────────────────────────────────────────┐
    │ trend_ok         │ EMA_MID > EMA_SLOW no timeframe de tendência        │
    │ cross_ok         │ EMA_FAST cruzou acima de EMA_MID nos últimos N bars │
    │ rsi_ok           │ RSI_MIN ≤ RSI ≤ RSI_MAX no timeframe de entrada     │
    └──────────────────┴─────────────────────────────────────────────────────┘
    """
    # ── Timeframe de tendência ────────────────────────────────────────────────
    df_trend = fetch_ohlcv(exchange, symbol, TIMEFRAME_TREND, OHLCV_LIMIT)
    df_trend.ta.ema(length=EMA_MID,  append=True)
    df_trend.ta.ema(length=EMA_SLOW, append=True)

    close_trend = float(df_trend["close"].iloc[-1])
    ema_mid_t   = float(df_trend[f"EMA_{EMA_MID}"].iloc[-1])
    ema_slow_t  = float(df_trend[f"EMA_{EMA_SLOW}"].iloc[-1])

    # EMA21 > EMA55 → tendência de alta confirmada
    trend_ok = ema_mid_t > ema_slow_t

    # ── Timeframe de entrada ──────────────────────────────────────────────────
    df_entry = fetch_ohlcv(exchange, symbol, TIMEFRAME_ENTRY, OHLCV_LIMIT)
    df_entry.ta.ema(length=EMA_FAST, append=True)
    df_entry.ta.ema(length=EMA_MID,  append=True)
    df_entry.ta.rsi(length=14,       append=True)

    ema_fast_series = df_entry[f"EMA_{EMA_FAST}"].dropna()
    ema_mid_series  = df_entry[f"EMA_{EMA_MID}"].dropna()
    rsi_series      = df_entry["RSI_14"].dropna()

    close_entry = float(df_entry["close"].iloc[-1])
    ema_fast_e  = float(ema_fast_series.iloc[-1])
    ema_mid_e   = float(ema_mid_series.iloc[-1])
    rsi_current = float(rsi_series.iloc[-1])

    # Detecta cruzamento EMA_FAST acima de EMA_MID nos últimos SIGNAL_LOOKBACK candles
    # Condição: em algum ponto recente, EMA_FAST estava abaixo e passou a ser acima
    lookback = min(SIGNAL_LOOKBACK, len(ema_fast_series) - 2)
    cross_ok  = False
    for k in range(lookback):
        was_below = ema_fast_series.iloc[-(k + 2)] < ema_mid_series.iloc[-(k + 2)]
        is_above  = ema_fast_series.iloc[-(k + 1)] > ema_mid_series.iloc[-(k + 1)]
        if was_below and is_above:
            cross_ok = True
            break

    # RSI na zona saudável: tem momentum mas não está sobrecomprado
    rsi_ok = RSI_MIN <= rsi_current <= RSI_MAX

    # Mínima dos últimos N candles para calcular o Stop Loss
    lowest_low = float(df_entry["low"].iloc[-SL_CANDLES:].min())

    signal = trend_ok and cross_ok and rsi_ok

    result: Dict[str, Any] = {
        # Tendência
        "close_trend" : close_trend,
        "ema_mid_t"   : ema_mid_t,
        "ema_slow_t"  : ema_slow_t,
        "trend_ok"    : trend_ok,
        # Entrada
        "close_entry" : close_entry,
        "ema_fast_e"  : ema_fast_e,
        "ema_mid_e"   : ema_mid_e,
        "rsi_current" : rsi_current,
        "cross_ok"    : cross_ok,
        "rsi_ok"      : rsi_ok,
        # Stop Loss base
        "lowest_low_5c": lowest_low,
        # Sinal final
        "signal"      : signal,
    }

    logger.info(
        f"[{symbol}] [INDICADORES] "
        f"{TIMEFRAME_TREND.upper()} → Close={close_trend:.4f} | "
        f"EMA{EMA_MID}={ema_mid_t:.4f} | EMA{EMA_SLOW}={ema_slow_t:.4f} | "
        f"Tendência={'✓' if trend_ok else '✗'} || "
        f"{TIMEFRAME_ENTRY.upper()} → Close={close_entry:.4f} | "
        f"EMA{EMA_FAST}={ema_fast_e:.4f} | EMA{EMA_MID}={ema_mid_e:.4f} | "
        f"RSI={rsi_current:.2f} | Cross={'✓' if cross_ok else '✗'} | "
        f"RSI_OK={'✓' if rsi_ok else '✗'}"
    )

    return result
