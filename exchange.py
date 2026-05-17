"""exchange.py — Wrapper assíncrono sobre ccxt com retry e rate limit."""
import asyncio
import logging
import time
from typing import Optional

import ccxt

from config import API_KEY, API_SECRET, API_PASSPHRASE, EXCHANGE_ID, TESTNET

logger = logging.getLogger("bot.exchange")

_MAX_RETRIES = 4
_BACKOFF_BASE = 2  # segundos


def _build_exchange() -> ccxt.Exchange:
    if EXCHANGE_ID not in ccxt.exchanges:
        raise ValueError(f"Exchange '{EXCHANGE_ID}' não suportada. Verifique EXCHANGE_ID no .env.")

    cls = getattr(ccxt, EXCHANGE_ID)
    extra_headers = {"x-simulated-trading": "1"} if TESTNET else {}

    return cls({
        "apiKey"         : API_KEY,
        "secret"         : API_SECRET,
        "password"       : API_PASSPHRASE,
        "enableRateLimit": True,
        "headers"        : extra_headers,
        "options"        : {"defaultType": "spot"},
    })


class ExchangeClient:
    """Wrapper assíncrono em torno do ccxt — todas as chamadas de rede usam run_in_executor."""

    def __init__(self) -> None:
        self._ex = _build_exchange()
        self._ex.load_markets()
        mode = "DEMO" if TESTNET else "PRODUÇÃO"
        logger.info(
            f"Exchange '{self._ex.id}' conectada [{mode}]. "
            f"{len(self._ex.markets)} mercados carregados."
        )

    # ── Chamada com retry ─────────────────────────────────────────────────────

    def _call(self, fn, *args, **kwargs):
        """Executa uma função ccxt com retry exponencial em falhas transitórias."""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return fn(*args, **kwargs)
            except ccxt.RateLimitExceeded:
                wait = _BACKOFF_BASE ** attempt
                logger.warning(f"Rate limit atingido. Aguardando {wait}s (tentativa {attempt})…")
                time.sleep(wait)
            except ccxt.NetworkError as exc:
                wait = _BACKOFF_BASE ** attempt
                logger.warning(f"Erro de rede: {exc}. Aguardando {wait}s (tentativa {attempt})…")
                time.sleep(wait)
            except ccxt.ExchangeError:
                raise  # erros da exchange não têm retry
        raise ccxt.NetworkError(f"Falha após {_MAX_RETRIES} tentativas.")

    async def _run(self, fn, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._call(fn, *args, **kwargs))

    # ── Consultas ─────────────────────────────────────────────────────────────

    async def get_price(self, symbol: str) -> float:
        ticker = await self._run(self._ex.fetch_ticker, symbol)
        return float(ticker["last"])

    async def get_balance(self, currency: str) -> float:
        balance = await self._run(self._ex.fetch_balance)
        return float(balance.get(currency, {}).get("free", 0.0))

    def amount_to_precision(self, symbol: str, qty: float) -> float:
        """Arredonda qty para a precisão mínima aceita pelo par (cálculo local, sem I/O)."""
        try:
            return float(self._ex.amount_to_precision(symbol, qty))
        except Exception:
            return qty

    def get_min_order_cost(self, symbol: str) -> float:
        """Retorna o custo mínimo de ordem em quote currency para o par."""
        try:
            market = self._ex.market(symbol)
            return float(market.get("limits", {}).get("cost", {}).get("min") or 0.0)
        except Exception:
            return 0.0

    # ── Ordens ───────────────────────────────────────────────────────────────

    async def market_buy(self, symbol: str, qty: float) -> Optional[dict]:
        """Compra qty unidades da moeda base a mercado. Retorna a ordem ou None."""
        try:
            order = await self._run(self._ex.create_market_buy_order, symbol, qty)
            return order
        except ccxt.InsufficientFunds as exc:
            logger.error(f"[{symbol}] Saldo insuficiente: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[{symbol}] Falha na compra: {exc}")
            return None

    async def market_sell(self, symbol: str, qty: float) -> Optional[dict]:
        """Vende qty unidades da moeda base a mercado. Retorna a ordem ou None."""
        try:
            order = await self._run(self._ex.create_market_sell_order, symbol, qty)
            return order
        except Exception as exc:
            logger.error(f"[{symbol}] Falha na venda: {exc}")
            return None
