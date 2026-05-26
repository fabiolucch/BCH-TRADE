"""
indicators.py — Busca dados OHLCV da exchange e calcula indicadores técnicos.

Contém dois conjuntos de indicadores:
  1. calculate_indicators()      — estratégia original BCH/USDC (mantida para compatibilidade)
  2. calculate_triple_screen()   — Triple Screen de Alexander Elder para DCA multi-ativo

Triple Screen (Elder):
  Screen 1 — Weekly  : filtra direção da tendência (EMA13 + MACD)
  Screen 2 — Daily   : confirma pullback de oscilador em tendência de alta
  Screen 3 — 4H      : define zona de entrada precisa + ATR para sizing
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
    TIMEFRAME_WEEKLY,
    OHLCV_LIMIT,
    RSI_OVERSOLD,
    RSI_LOOKBACK,
    SL_CANDLES,
    MACD_FAST,
    MACD_SLOW,
    MACD_SIGNAL_PERIOD,
    VOL_MA_PERIOD,
    EMA_WEEKLY_PERIOD,
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
# Indicadores originais (compatibilidade)
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
        f"Tendência={'OK' if trend_ok else 'X'} || "
        f"4H → Close={close_4h:.4f} | EMA20={ema20_4h:.4f} | "
        f"EMA50={ema50_4h:.4f} | RSI={rsi_current:.2f} | "
        f"Pullback={'OK' if pullback_ok else 'X'} | "
        f"RSI_Sinal={'OK' if rsi_ok else 'X'}"
    )

    return result


# ─────────────────────────────────────────────────────────────
# Triple Screen (Elder) — nova estratégia DCA
# ─────────────────────────────────────────────────────────────

def calculate_triple_screen(exchange: ccxt.Exchange, symbol: str = None) -> Dict[str, Any]:
    """
    Elder's Triple Screen aplicado à estratégia DCA.

    Screen 1 (Weekly): Direção da tendência — operar SOMENTE a favor da tendência.
      - EMA 13 semanal: close_w > ema13_w E ema13_w subindo (ema13[-1] > ema13[-2])
      - Histograma MACD(12,26,9): positivo OU virando positivo (hist[-1] > hist[-2])
      - screen1_ok = ema_bullish AND macd_ok

    Screen 2 (Daily): Pullback de oscilador — entrar na fraqueza dentro da tendência.
      - Preço > EMA 50 diária (uptrend no diário)
      - RSI(14) esteve abaixo de 40 nos últimos 3 candles (pullback ocorreu)
      - RSI se recuperando (rsi[-1] > rsi[-2])
      - Volume no último candle > MA(20) de volume (participação da multidão)
      - screen2_ok = trend_ok AND rsi_pullback AND rsi_recovering AND vol_ok

    Screen 3 (4H): Entrada precisa — preço em zona de suporte.
      - Preço entre EMA50_4H e EMA20_4H × 1.02 (zona de pullback)
      - RSI(14) < 50 (não sobrecomprado na entrada)
      - ATR(14) calculado para dimensionamento da posição
      - screen3_ok = pullback_ok AND rsi_not_overbought

    Sinal = screen1_ok AND screen2_ok AND screen3_ok

    Também retorna:
      - weekly_bullish: bool (obrigatório para DCA levels 2 e 3)
      - atr_4h: float (para cálculo do trailing stop)
      - rsi_4h_current: float (para requisitos de DCA level)
      - lowest_low_5c: float (para Stop Loss inicial)
    """
    # Usa o symbol passado ou cai para o SYMBOL global (BCH/USDC por padrão)
    sym = symbol or SYMBOL

    # ── Screen 1: Weekly — Elder usa o maior timeframe para filtrar a direção ──

    # Precisamos de candles suficientes para MACD(12,26,9): min ~52 candles
    df_w = fetch_ohlcv(exchange, sym, TIMEFRAME_WEEKLY, max(OHLCV_LIMIT, 100))

    df_w.ta.ema(length=EMA_WEEKLY_PERIOD, append=True)
    ema_col_w = f"EMA_{EMA_WEEKLY_PERIOD}"

    # MACD usando pandas_ta — retorna colunas MACD_fast_slow_signal, MACDh_, MACDs_
    macd_df = df_w.ta.macd(fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL_PERIOD, append=False)
    # pandas_ta naming: MACDh_{fast}_{slow}_{signal}
    hist_col = f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL_PERIOD}"

    close_w   = float(df_w["close"].iloc[-1])
    ema13_w   = float(df_w[ema_col_w].iloc[-1])
    ema13_w_1 = float(df_w[ema_col_w].iloc[-2])  # candle anterior para checar slope

    # EMA bullish: preço acima da EMA13 semanal E EMA13 em alta
    ema_bullish = (close_w > ema13_w) and (ema13_w > ema13_w_1)

    # MACD histograma: positivo OU virando positivo (Elder: "turning" basta para alerta)
    hist_series = macd_df[hist_col].dropna()
    hist_last   = float(hist_series.iloc[-1])
    hist_prev   = float(hist_series.iloc[-2])
    # Virando positivo captura o momento de menor pressão vendedora — Elder's "hook"
    macd_ok     = (hist_last > 0) or (hist_last > hist_prev)

    screen1_ok      = ema_bullish and macd_ok
    weekly_bullish  = screen1_ok   # alias explícito usado no DCA check

    # ── Screen 2: Daily — pullback do oscilador dentro da tendência ──

    df_1d = fetch_ohlcv(exchange, sym, TIMEFRAME_TREND, OHLCV_LIMIT)

    df_1d.ta.ema(length=50, append=True)
    df_1d.ta.rsi(length=14, append=True)

    close_1d  = float(df_1d["close"].iloc[-1])
    ema50_1d  = float(df_1d["EMA_50"].iloc[-1])

    rsi_1d_series = df_1d["RSI_14"].dropna()
    rsi_1d_last   = float(rsi_1d_series.iloc[-1])
    rsi_1d_prev   = float(rsi_1d_series.iloc[-2])

    # Volume MA para confirmar que o candle tem participação real
    vol_ma_1d  = float(df_1d["volume"].iloc[-VOL_MA_PERIOD:].mean())
    vol_last_1d = float(df_1d["volume"].iloc[-1])

    # Tendência diária: preço acima da EMA50
    trend_ok_1d = close_1d > ema50_1d

    # Pullback: RSI esteve abaixo de 40 nos últimos 3 candles (fraqueza temporária)
    # Elder: no uptrend, osciladores em sobrevenda = oportunidade de compra
    rsi_pullback  = bool((rsi_1d_series.iloc[-3:] < 40.0).any())
    rsi_recovering = rsi_1d_last > rsi_1d_prev
    vol_ok        = vol_last_1d > vol_ma_1d

    screen2_ok = trend_ok_1d and rsi_pullback and rsi_recovering and vol_ok

    # ── Screen 3: 4H — zona precisa de entrada ──

    df_4h = fetch_ohlcv(exchange, sym, TIMEFRAME_ENTRY, OHLCV_LIMIT)

    df_4h.ta.ema(length=20, append=True)
    df_4h.ta.ema(length=50, append=True)
    df_4h.ta.rsi(length=14, append=True)
    df_4h.ta.atr(length=14, append=True)

    close_4h  = float(df_4h["close"].iloc[-1])
    ema20_4h  = float(df_4h["EMA_20"].iloc[-1])
    ema50_4h  = float(df_4h["EMA_50"].iloc[-1])

    rsi_4h_series  = df_4h["RSI_14"].dropna()
    rsi_4h_current = float(rsi_4h_series.iloc[-1])

    atr_4h_series = df_4h["ATRr_14"].dropna()
    atr_4h        = float(atr_4h_series.iloc[-1])

    # Zona de pullback: entre EMA50 e EMA20 (tolerância de 2% acima da EMA20)
    pullback_high   = ema20_4h * 1.02
    pullback_ok     = ema50_4h <= close_4h <= pullback_high

    # Entrada apenas se RSI não estiver sobrecomprado (< 50 em pullback)
    rsi_not_overbought = rsi_4h_current < 50.0

    screen3_ok = pullback_ok and rsi_not_overbought

    # Mínima dos últimos N candles de 4H — base do Stop Loss inicial
    lowest_low = float(df_4h["low"].iloc[-SL_CANDLES:].min())

    # ── Sinal final: todas as três telas devem confirmar ──
    signal = screen1_ok and screen2_ok and screen3_ok

    result: Dict[str, Any] = {
        # ── Screen 1 (Weekly)
        "close_w"           : close_w,
        "ema13_w"           : ema13_w,
        "ema_bullish"       : ema_bullish,
        "macd_hist_w"       : hist_last,
        "macd_ok"           : macd_ok,
        "screen1_ok"        : screen1_ok,
        "weekly_bullish"    : weekly_bullish,   # alias para DCA check
        # ── Screen 2 (Daily)
        "close_1d"          : close_1d,
        "ema50_1d"          : ema50_1d,
        "rsi_1d_current"    : rsi_1d_last,
        "trend_ok_1d"       : trend_ok_1d,
        "rsi_pullback"      : rsi_pullback,
        "rsi_recovering_1d" : rsi_recovering,
        "vol_ok"            : vol_ok,
        "screen2_ok"        : screen2_ok,
        # ── Screen 3 (4H)
        "close_4h"          : close_4h,
        "ema20_4h"          : ema20_4h,
        "ema50_4h"          : ema50_4h,
        "rsi_4h_current"    : rsi_4h_current,
        "atr_4h"            : atr_4h,
        "pullback_ok"       : pullback_ok,
        "rsi_not_overbought": rsi_not_overbought,
        "screen3_ok"        : screen3_ok,
        # ── Shared values used by strategy.py
        "lowest_low_5c"     : lowest_low,
        # ── Final signal
        "signal"            : signal,
    }

    logger.info(
        "[TRIPLE SCREEN] "
        f"W: Close={close_w:.4f} | EMA13={ema13_w:.4f} | MACD_hist={hist_last:.4f} | "
        f"Screen1={'OK' if screen1_ok else 'X'} || "
        f"1D: Close={close_1d:.4f} | EMA50={ema50_1d:.4f} | RSI={rsi_1d_last:.2f} | "
        f"Vol={'OK' if vol_ok else 'X'} | Screen2={'OK' if screen2_ok else 'X'} || "
        f"4H: Close={close_4h:.4f} | EMA50={ema50_4h:.4f} | EMA20={ema20_4h:.4f} | "
        f"RSI={rsi_4h_current:.2f} | ATR={atr_4h:.4f} | "
        f"Screen3={'OK' if screen3_ok else 'X'} || "
        f"SINAL={'COMPRA' if signal else 'AGUARDAR'}"
    )

    return result
