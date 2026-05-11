"""
indicators.py — Busca dados OHLCV da exchange e calcula os indicadores técnicos
                necessários para a estratégia de Swing Trade BCH/USDC.

Indicadores calculados:
  - Gráfico 1D : EMA 50  (filtro de tendência)
  - Gráfico 4H : EMA 20, EMA 50, RSI 14  (gatilho de entrada)

Usa apenas pandas/numpy — sem dependência de pandas_ta.
"""

import logging
from typing import Dict, Any

import ccxt
import pandas as pd
import numpy as np

from config import (
    SYMBOL,
    TIMEFRAME_TREND,
    TIMEFRAME_ENTRY,
    OHLCV_LIMIT,
    RSI_OVERSOLD,
    RSI_LOOKBACK,
    SL_CANDLES,
)

logger = logging.getLogger("bot")


# ─────────────────────────────────────────────────────────────
# Funções de indicadores (implementação pura pandas/numpy)
# ─────────────────────────────────────────────────────────────

def _ema(series: pd.Series, length: int) -> pd.Series:
    """EMA usando pandas ewm com adjust=False (equivalente ao TA padrão)."""
    return series.ewm(span=length, adjust=False).mean()


def _rsi(series: pd.Series, length: int = 14) -> pd.Series:
    """
    RSI de Wilder (Smoothed Moving Average dos ganhos/perdas).
    Equivalente ao RSI padrão usado em TradingView e pandas_ta.
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Primeira média: SMA simples
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


# ─────────────────────────────────────────────────────────────
# Busca de dados
# ─────────────────────────────────────────────────────────────

def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    limit: int = 200,
) -> pd.DataFrame:
    """
    Baixa candles OHLCV da exchange e retorna um DataFrame limpo.

    Colunas: open, high, low, close, volume (índice = timestamp UTC).
    """
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(
            raw,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df.set_index("timestamp", inplace=True)
        df = df.astype(float)

        logger.debug(
            f"OHLCV carregado: {symbol} {timeframe} — {len(df)} candles "
            f"({df.index[0].strftime('%Y-%m-%d %H:%M')} → "
            f"{df.index[-1].strftime('%Y-%m-%d %H:%M')} UTC)"
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


# ─────────────────────────────────────────────────────────────
# Cálculo de indicadores e avaliação das condições
# ─────────────────────────────────────────────────────────────

def calculate_indicators(exchange: ccxt.Exchange) -> Dict[str, Any]:
    """
    Calcula todos os indicadores e avalia as condições de entrada.

    Retorna um dicionário com:
      - Valores brutos dos indicadores (close, EMA, RSI…)
      - Flags booleanas por condição (trend_ok, pullback_ok, rsi_ok)
      - Flag `signal`: True apenas se TODAS as condições forem verdadeiras
      - `lowest_low_5c`: mínima dos últimos N candles de 4H (base do Stop Loss)

    Condições avaliadas:
    ┌─────────────────┬──────────────────────────────────────────────────────┐
    │ trend_ok        │ Close_1D > EMA50_1D                                   │
    │ pullback_ok     │ EMA50_4H ≤ Close_4H ≤ EMA20_4H × 1.02 (tolerância 2%)│
    │ rsi_ok          │ RSI esteve em sobrevenda recentemente E está subindo  │
    └─────────────────┴──────────────────────────────────────────────────────┘
    """
    # ── Gráfico 1D: Tendência principal ──────────────────────────────────────
    df_1d = fetch_ohlcv(exchange, SYMBOL, TIMEFRAME_TREND, OHLCV_LIMIT)

    df_1d["EMA_50"] = _ema(df_1d["close"], 50)

    close_1d = float(df_1d["close"].iloc[-1])
    ema50_1d = float(df_1d["EMA_50"].iloc[-1])

    trend_ok = close_1d > ema50_1d

    # ── Gráfico 4H: Gatilho de entrada ───────────────────────────────────────
    df_4h = fetch_ohlcv(exchange, SYMBOL, TIMEFRAME_ENTRY, OHLCV_LIMIT)

    df_4h["EMA_20"] = _ema(df_4h["close"], 20)
    df_4h["EMA_50"] = _ema(df_4h["close"], 50)
    df_4h["RSI_14"] = _rsi(df_4h["close"], 14)

    close_4h  = float(df_4h["close"].iloc[-1])
    ema20_4h  = float(df_4h["EMA_20"].iloc[-1])
    ema50_4h  = float(df_4h["EMA_50"].iloc[-1])

    rsi_series   = df_4h["RSI_14"].dropna()
    rsi_current  = float(rsi_series.iloc[-1])
    rsi_previous = float(rsi_series.iloc[-2])

    # Condição 2: pullback na zona de suporte entre EMA50 e EMA20
    pullback_high = ema20_4h * 1.02
    pullback_ok   = ema50_4h <= close_4h <= pullback_high

    # Condição 3: RSI esteve em sobrevenda nos últimos N candles E está subindo
    rsi_window        = rsi_series.iloc[-RSI_LOOKBACK:]
    rsi_was_oversold  = bool((rsi_window < RSI_OVERSOLD).any())
    rsi_recovering    = rsi_current > rsi_previous
    rsi_ok            = rsi_was_oversold and rsi_recovering

    lowest_low = float(df_4h["low"].iloc[-SL_CANDLES:].min())

    signal = trend_ok and pullback_ok and rsi_ok

    result: Dict[str, Any] = {
        "close_1d"         : close_1d,
        "ema50_1d"         : ema50_1d,
        "trend_ok"         : trend_ok,
        "close_4h"         : close_4h,
        "ema20_4h"         : ema20_4h,
        "ema50_4h"         : ema50_4h,
        "rsi_current"      : rsi_current,
        "rsi_previous"     : rsi_previous,
        "rsi_was_oversold" : rsi_was_oversold,
        "rsi_recovering"   : rsi_recovering,
        "pullback_ok"      : pullback_ok,
        "rsi_ok"           : rsi_ok,
        "lowest_low_5c"    : lowest_low,
        "signal"           : signal,
    }

    logger.info(
        "[INDICADORES] "
        f"1D → Close={close_1d:.4f} | EMA50={ema50_1d:.4f} | "
        f"Tendência={'✓' if trend_ok else '✗'} || "
        f"4H → Close={close_4h:.4f} | EMA20={ema20_4h:.4f} | "
        f"EMA50={ema50_4h:.4f} | RSI={rsi_current:.2f} | "
        f"Pullback={'✓' if pullback_ok else '✗'} | "
        f"RSI_Sinal={'✓' if rsi_ok else '✗'}"
    )

    return result
