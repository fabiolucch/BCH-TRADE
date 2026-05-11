"""
indicators.py — Busca dados OHLCV da exchange e calcula os indicadores técnicos
                necessários para a estratégia de Swing Trade BCH/USDC.

Indicadores calculados:
  - Gráfico 1D : EMA 50  (filtro de tendência)
  - Gráfico 4H : EMA 20, EMA 50, RSI 14  (gatilho de entrada)

A função principal `calculate_indicators()` retorna um dicionário com todos os
valores e as flags booleanas de cada condição, facilitando o log e a decisão
no loop principal.
"""

import logging
from typing import Dict, Any

import ccxt
import pandas as pd
import pandas_ta as ta

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

    Lança ccxt.NetworkError ou ccxt.ExchangeError em caso de falha,
    permitindo que o chamador decida como tratar.
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

    # pandas_ta: calcula EMA e adiciona coluna 'EMA_50' ao DataFrame
    df_1d.ta.ema(length=50, append=True)

    close_1d = float(df_1d["close"].iloc[-1])
    ema50_1d = float(df_1d["EMA_50"].iloc[-1])

    # Condição 1: preço de fechamento diário acima da EMA 50 diária
    trend_ok = close_1d > ema50_1d

    # ── Gráfico 4H: Gatilho de entrada ───────────────────────────────────────
    df_4h = fetch_ohlcv(exchange, SYMBOL, TIMEFRAME_ENTRY, OHLCV_LIMIT)

    df_4h.ta.ema(length=20, append=True)
    df_4h.ta.ema(length=50, append=True)
    df_4h.ta.rsi(length=14, append=True)

    close_4h  = float(df_4h["close"].iloc[-1])
    ema20_4h  = float(df_4h["EMA_20"].iloc[-1])
    ema50_4h  = float(df_4h["EMA_50"].iloc[-1])

    # Remove NaN gerados pelo período de aquecimento dos indicadores
    rsi_series   = df_4h["RSI_14"].dropna()
    rsi_current  = float(rsi_series.iloc[-1])
    rsi_previous = float(rsi_series.iloc[-2])

    # Condição 2: pullback na zona de suporte entre EMA50 e EMA20
    # Tolerância de 2% acima da EMA20 para capturar toques levemente acima
    pullback_high = ema20_4h * 1.02
    pullback_ok   = ema50_4h <= close_4h <= pullback_high

    # Condição 3: RSI esteve em sobrevenda (< 30) nos últimos N candles
    #             E está atualmente apontando para cima (reversão)
    rsi_window        = rsi_series.iloc[-RSI_LOOKBACK:]
    rsi_was_oversold  = bool((rsi_window < RSI_OVERSOLD).any())
    rsi_recovering    = rsi_current > rsi_previous
    rsi_ok            = rsi_was_oversold and rsi_recovering

    # Mínima dos últimos N candles de 4H (usada para calcular o Stop Loss)
    lowest_low = float(df_4h["low"].iloc[-SL_CANDLES:].min())

    # ── Sinal final: todas as condições verdadeiras ───────────────────────────
    signal = trend_ok and pullback_ok and rsi_ok

    result: Dict[str, Any] = {
        # Valores 1D
        "close_1d"         : close_1d,
        "ema50_1d"         : ema50_1d,
        "trend_ok"         : trend_ok,
        # Valores 4H
        "close_4h"         : close_4h,
        "ema20_4h"         : ema20_4h,
        "ema50_4h"         : ema50_4h,
        "rsi_current"      : rsi_current,
        "rsi_previous"     : rsi_previous,
        "rsi_was_oversold" : rsi_was_oversold,
        "rsi_recovering"   : rsi_recovering,
        # Flags de condição
        "pullback_ok"      : pullback_ok,
        "rsi_ok"           : rsi_ok,
        # Stop Loss base
        "lowest_low_5c"    : lowest_low,
        # Sinal de entrada
        "signal"           : signal,
    }

    # Log resumido sempre visível no terminal
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
