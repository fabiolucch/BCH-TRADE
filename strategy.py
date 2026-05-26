"""
strategy.py — Triple Screen DCA Strategy based on Alexander Elder's methodology.

Three pillars:
  1. Psychology  : Strict rules — no hope, no exceptions; discipline enforced by code
  2. TA          : Triple Screen filters crowd behavior at 3 timeframes
  3. Risk Mgmt   : 2% rule per level, 4% hard stop, scaled profit-taking
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import (
    DCA_DROP_L2_PCT,
    DCA_DROP_L3_PCT,
    DCA_RISK_L1_PCT,
    DCA_RISK_L2_PCT,
    DCA_RISK_L3_PCT,
    HARD_STOP_PCT,
    RSI_L2_THRESHOLD,
    RSI_L3_THRESHOLD,
    TP1_CLOSE_PCT,
    TP1_RISK_RATIO,
    TP2_CLOSE_PCT,
    TP2_RISK_RATIO,
    TRAIL_ATR_MULT,
)

logger = logging.getLogger("bot")


# ═════════════════════════════════════════════════════════════
# DATA CLASSES — position state
# ═════════════════════════════════════════════════════════════

@dataclass
class DCALevel:
    """Represents a single DCA entry level within a position."""
    level: int
    entry_price: float
    quantity: float
    cost: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "level"       : self.level,
            "entry_price" : self.entry_price,
            "quantity"    : self.quantity,
            "cost"        : self.cost,
            "timestamp"   : self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "DCALevel":
        return cls(
            level       = int(d["level"]),
            entry_price = float(d["entry_price"]),
            quantity    = float(d["quantity"]),
            cost        = float(d["cost"]),
            timestamp   = float(d.get("timestamp", time.time())),
        )


@dataclass
class DCAPosition:
    """
    Full state of an open (or closed) DCA position.

    Persisted to JSON between bot cycles so no state is lost on restart.
    """
    symbol: str
    is_open: bool = False
    levels: List[DCALevel] = field(default_factory=list)
    avg_price: float = 0.0
    total_qty: float = 0.0
    total_cost: float = 0.0
    initial_balance: float = 0.0    # account balance when L1 was opened (hard stop reference)
    hard_stop_price: float = 0.0    # non-negotiable exit price — Elder's "oxygen tank"
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    tp1_filled: bool = False
    tp2_filled: bool = False
    trailing_active: bool = False
    trailing_peak: float = 0.0
    trailing_stop: float = 0.0
    exit_mode: str = "monitor"
    tp1_order_id: Optional[str] = None
    tp2_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "symbol"          : self.symbol,
            "is_open"         : self.is_open,
            "levels"          : [lv.to_dict() for lv in self.levels],
            "avg_price"       : self.avg_price,
            "total_qty"       : self.total_qty,
            "total_cost"      : self.total_cost,
            "initial_balance" : self.initial_balance,
            "hard_stop_price" : self.hard_stop_price,
            "tp1_price"       : self.tp1_price,
            "tp2_price"       : self.tp2_price,
            "tp1_filled"      : self.tp1_filled,
            "tp2_filled"      : self.tp2_filled,
            "trailing_active" : self.trailing_active,
            "trailing_peak"   : self.trailing_peak,
            "trailing_stop"   : self.trailing_stop,
            "exit_mode"       : self.exit_mode,
            "tp1_order_id"    : self.tp1_order_id,
            "tp2_order_id"    : self.tp2_order_id,
            "sl_order_id"     : self.sl_order_id,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "DCAPosition":
        pos = cls(symbol=d["symbol"])
        pos.is_open         = bool(d.get("is_open", False))
        pos.levels          = [DCALevel.from_dict(lv) for lv in d.get("levels", [])]
        pos.avg_price       = float(d.get("avg_price", 0.0))
        pos.total_qty       = float(d.get("total_qty", 0.0))
        pos.total_cost      = float(d.get("total_cost", 0.0))
        pos.initial_balance = float(d.get("initial_balance", 0.0))
        pos.hard_stop_price = float(d.get("hard_stop_price", 0.0))
        pos.tp1_price       = float(d.get("tp1_price", 0.0))
        pos.tp2_price       = float(d.get("tp2_price", 0.0))
        pos.tp1_filled      = bool(d.get("tp1_filled", False))
        pos.tp2_filled      = bool(d.get("tp2_filled", False))
        pos.trailing_active = bool(d.get("trailing_active", False))
        pos.trailing_peak   = float(d.get("trailing_peak", 0.0))
        pos.trailing_stop   = float(d.get("trailing_stop", 0.0))
        pos.exit_mode       = str(d.get("exit_mode", "monitor"))
        pos.tp1_order_id    = d.get("tp1_order_id")
        pos.tp2_order_id    = d.get("tp2_order_id")
        pos.sl_order_id     = d.get("sl_order_id")
        return pos

    def recalculate_avg(self) -> None:
        """
        Recalculates avg_price, total_qty, total_cost from the list of DCA levels.
        Called every time a new level is added so position metrics stay accurate.
        """
        if not self.levels:
            self.avg_price  = 0.0
            self.total_qty  = 0.0
            self.total_cost = 0.0
            return

        total_cost = sum(lv.cost for lv in self.levels)
        total_qty  = sum(lv.quantity for lv in self.levels)

        self.total_cost = total_cost
        self.total_qty  = total_qty
        # Weighted average: cost is already price × qty
        self.avg_price  = total_cost / total_qty if total_qty > 0 else 0.0


# ═════════════════════════════════════════════════════════════
# TAKE PROFIT CALCULATION
# ═════════════════════════════════════════════════════════════

def calculate_tp_prices(avg_price: float, hard_stop_price: float) -> tuple:
    """
    Calculates TP1 and TP2 based on the risk distance from avg_price to hard_stop.

    Using risk as the unit ensures reward is always proportional to actual danger.
    Elder: "Define your risk first, then look for reward."

    risk = avg_price - hard_stop_price
    tp1  = avg_price + (risk * TP1_RISK_RATIO)   # default 1.5R
    tp2  = avg_price + (risk * TP2_RISK_RATIO)   # default 2.5R
    """
    if avg_price <= 0 or hard_stop_price <= 0:
        return 0.0, 0.0

    risk = avg_price - hard_stop_price
    if risk <= 0:
        # Stop is above avg price — invalid state, log and return zeros
        logger.warning(
            f"[STRATEGY] calc_tp: hard_stop ({hard_stop_price:.4f}) >= avg_price "
            f"({avg_price:.4f}). TPs set to 0."
        )
        return 0.0, 0.0

    tp1 = avg_price + (risk * TP1_RISK_RATIO)
    tp2 = avg_price + (risk * TP2_RISK_RATIO)

    logger.debug(
        f"[STRATEGY] TP recalc → avg={avg_price:.4f} | stop={hard_stop_price:.4f} | "
        f"risk={risk:.4f} | TP1={tp1:.4f} ({TP1_RISK_RATIO}R) | "
        f"TP2={tp2:.4f} ({TP2_RISK_RATIO}R)"
    )
    return tp1, tp2


# ═════════════════════════════════════════════════════════════
# DCA ENTRY LOGIC
# ═════════════════════════════════════════════════════════════

def check_dca_entry(
    position: DCAPosition,
    indicators: Dict[str, Any],
    account_balance: float,
) -> Optional[Dict]:
    """
    Determines if we should open or add to a DCA position.

    Returns a dict with:
      - 'action'   : 'open_l1' | 'add_l2' | 'add_l3' | None
      - 'risk_pct' : float — % of account to risk on this level
      - 'reason'   : str  — human-readable trade journal entry

    L1 — New position: all three Triple Screen filters must confirm.
    L2 — Add to position:
         • Price has dropped DCA_DROP_L2_PCT% from L1 entry (value improves)
         • RSI < RSI_L2_THRESHOLD (momentum oversold enough to warrant adding)
         • weekly_bullish=True — CRITICAL: refuse to add in a downtrend.
           This single rule would have prevented the -10.11 ETH loss in Jan-Feb 2025.
    L3 — Add to position (same logic as L2 but stricter RSI gate).
    """
    current_price = indicators.get("close_4h", 0.0)

    if current_price <= 0:
        return None

    # ── L1: No position open — require full Triple Screen confirmation ────────
    if not position.is_open:
        if not indicators.get("signal", False):
            return None

        return {
            "action"   : "open_l1",
            "risk_pct" : DCA_RISK_L1_PCT,
            "reason"   : (
                f"L1 entry: Triple Screen confirmed. "
                f"W_EMA_bullish={indicators.get('ema_bullish')} | "
                f"MACD_ok={indicators.get('macd_ok')} | "
                f"1D_trend={indicators.get('trend_ok_1d')} | "
                f"RSI_pullback={indicators.get('rsi_pullback')} | "
                f"Vol_ok={indicators.get('vol_ok')} | "
                f"4H_pullback={indicators.get('pullback_ok')} | "
                f"RSI_4H={indicators.get('rsi_4h_current', 0):.2f} | "
                f"price={current_price:.4f}"
            ),
        }

    # Position is open — check if we can add more levels
    n_levels = len(position.levels)

    # ── L2: First add — price pulled back further from L1 ────────────────────
    if n_levels == 1:
        l1_entry   = position.levels[0].entry_price
        l2_trigger = l1_entry * (1.0 - DCA_DROP_L2_PCT / 100.0)
        rsi_4h     = indicators.get("rsi_4h_current", 100.0)

        # Elder: NEVER add to a loser unless the weekly trend is still bullish.
        # If weekly flipped down, the "pullback" is actually a new downtrend.
        if not indicators.get("weekly_bullish", False):
            logger.info(
                f"[DCA] L2 blocked — weekly_bullish=False. "
                f"Elder: do not add in a downtrend. price={current_price:.4f}"
            )
            return None

        if current_price > l2_trigger:
            return None  # not cheap enough yet

        if rsi_4h >= RSI_L2_THRESHOLD:
            logger.info(
                f"[DCA] L2 blocked — RSI_4H={rsi_4h:.2f} >= {RSI_L2_THRESHOLD}. "
                f"Waiting for deeper oversold condition."
            )
            return None

        return {
            "action"   : "add_l2",
            "risk_pct" : DCA_RISK_L2_PCT,
            "reason"   : (
                f"L2 add: price {current_price:.4f} <= L1_entry×(1-{DCA_DROP_L2_PCT}%) "
                f"={l2_trigger:.4f} | RSI_4H={rsi_4h:.2f} < {RSI_L2_THRESHOLD} | "
                f"weekly_bullish=True"
            ),
        }

    # ── L3: Second add — even deeper pullback from L2 ─────────────────────────
    if n_levels == 2:
        l2_entry   = position.levels[1].entry_price
        l3_trigger = l2_entry * (1.0 - DCA_DROP_L3_PCT / 100.0)
        rsi_4h     = indicators.get("rsi_4h_current", 100.0)

        # Same mandatory weekly filter — no adding into a confirmed downtrend
        if not indicators.get("weekly_bullish", False):
            logger.info(
                f"[DCA] L3 blocked — weekly_bullish=False. "
                f"Elder: do not add in a downtrend. price={current_price:.4f}"
            )
            return None

        if current_price > l3_trigger:
            return None  # not cheap enough yet

        if rsi_4h >= RSI_L3_THRESHOLD:
            logger.info(
                f"[DCA] L3 blocked — RSI_4H={rsi_4h:.2f} >= {RSI_L3_THRESHOLD}. "
                f"Waiting for extreme oversold condition."
            )
            return None

        return {
            "action"   : "add_l3",
            "risk_pct" : DCA_RISK_L3_PCT,
            "reason"   : (
                f"L3 add: price {current_price:.4f} <= L2_entry×(1-{DCA_DROP_L3_PCT}%) "
                f"={l3_trigger:.4f} | RSI_4H={rsi_4h:.2f} < {RSI_L3_THRESHOLD} | "
                f"weekly_bullish=True"
            ),
        }

    # max levels reached
    return None


# ═════════════════════════════════════════════════════════════
# EXIT LOGIC
# ═════════════════════════════════════════════════════════════

def check_exits(
    position: DCAPosition,
    current_price: float,
    atr: float,
    account_balance: float,
) -> Optional[Dict]:
    """
    Checks all exit conditions in priority order. Returns the first triggered exit.

    Exit priority (highest to lowest):
    1. HARD STOP  — position drawdown exceeds HARD_STOP_PCT of account.
                    This is Elder's "oxygen tank" rule: protect capital above all else.
                    It must fire BEFORE any profit-taking logic is considered.
    2. STOP LOSS  — price falls to or below hard_stop_price (same level as hard stop).
    3. TP1        — close TP1_CLOSE_PCT (30%) of position at TP1 price.
    4. TP2        — close TP2_CLOSE_PCT (40%) of position at TP2 price (after TP1).
    5. TRAILING   — once TP1 is filled, track peak and close remaining on trailing stop.

    Returns: {'type': str, 'qty_to_close': float, 'reason': str} or None.
    """
    if not position.is_open or position.total_qty <= 0:
        return None

    avg  = position.avg_price
    stop = position.hard_stop_price

    # ── 1. HARD STOP — survival first, profit second ─────────────────────────
    # Measures actual account drawdown from this position, not just price distance.
    # If total unrealized loss exceeds HARD_STOP_PCT of the account, close immediately.
    unrealized_pnl = (current_price - avg) * position.total_qty
    hard_stop_loss_threshold = account_balance * (HARD_STOP_PCT / 100.0)

    if unrealized_pnl <= -hard_stop_loss_threshold:
        reason = (
            f"HARD STOP triggered: unrealized_loss={unrealized_pnl:.2f} USDT "
            f">= {HARD_STOP_PCT}% of account ({hard_stop_loss_threshold:.2f} USDT). "
            f"Elder: capital preservation is non-negotiable."
        )
        logger.warning(f"[EXIT] {reason}")
        return {
            "type"         : "hard_stop",
            "qty_to_close" : position.total_qty,
            "reason"       : reason,
        }

    # ── 2. STOP LOSS — price hit the technical stop level ────────────────────
    if current_price <= stop:
        reason = (
            f"STOP LOSS: price={current_price:.4f} <= hard_stop={stop:.4f} | "
            f"avg={avg:.4f} | loss={(current_price - avg) * position.total_qty:.2f} USDT"
        )
        logger.warning(f"[EXIT] {reason}")
        return {
            "type"         : "stop_loss",
            "qty_to_close" : position.total_qty,
            "reason"       : reason,
        }

    # ── 3. TP1 — partial profit at 1.5R ──────────────────────────────────────
    if not position.tp1_filled and position.tp1_price > 0:
        if current_price >= position.tp1_price:
            qty_to_close = round(position.total_qty * TP1_CLOSE_PCT, 8)
            reason = (
                f"TP1: price={current_price:.4f} >= tp1={position.tp1_price:.4f} | "
                f"closing {TP1_CLOSE_PCT*100:.0f}% = {qty_to_close:.6f} units"
            )
            logger.info(f"[EXIT] {reason}")
            return {
                "type"         : "tp1",
                "qty_to_close" : qty_to_close,
                "reason"       : reason,
            }

    # ── 4. TP2 — more profit at 2.5R (only after TP1 is done) ────────────────
    if position.tp1_filled and not position.tp2_filled and position.tp2_price > 0:
        if current_price >= position.tp2_price:
            # TP2 closes 40% of ORIGINAL total_qty — but since TP1 already closed 30%,
            # we calculate 40% relative to the post-TP1 remaining quantity.
            # Simpler and more reliable: close TP2_CLOSE_PCT of current total_qty.
            qty_to_close = round(position.total_qty * TP2_CLOSE_PCT, 8)
            reason = (
                f"TP2: price={current_price:.4f} >= tp2={position.tp2_price:.4f} | "
                f"closing {TP2_CLOSE_PCT*100:.0f}% = {qty_to_close:.6f} units"
            )
            logger.info(f"[EXIT] {reason}")
            return {
                "type"         : "tp2",
                "qty_to_close" : qty_to_close,
                "reason"       : reason,
            }

    # ── 5. TRAILING STOP — protects gains after TP1 is filled ────────────────
    # Once TP1 fires, we lock in profits and ride the trend with a dynamic stop.
    # The trailing stop moves up with the price but never moves down.
    if position.tp1_filled and atr > 0:
        # Activate trailing once price exceeds TP1 (already implied by tp1_filled)
        if not position.trailing_active:
            # First time here after TP1 — initialize peak and activate
            position.trailing_active = True
            position.trailing_peak   = current_price
            position.trailing_stop   = current_price - (atr * TRAIL_ATR_MULT)
            logger.info(
                f"[EXIT] Trailing stop activated: peak={current_price:.4f} | "
                f"trail_stop={position.trailing_stop:.4f} (ATR={atr:.4f} × {TRAIL_ATR_MULT})"
            )
        else:
            # Update peak if price made a new high
            if current_price > position.trailing_peak:
                position.trailing_peak  = current_price
                position.trailing_stop  = current_price - (atr * TRAIL_ATR_MULT)
                logger.debug(
                    f"[EXIT] Trailing peak updated: peak={position.trailing_peak:.4f} | "
                    f"trail_stop={position.trailing_stop:.4f}"
                )

            # Check if price fell through the trailing stop
            if current_price <= position.trailing_stop:
                reason = (
                    f"TRAILING STOP: price={current_price:.4f} <= "
                    f"trail_stop={position.trailing_stop:.4f} | "
                    f"peak was {position.trailing_peak:.4f}"
                )
                logger.info(f"[EXIT] {reason}")
                return {
                    "type"         : "trailing",
                    "qty_to_close" : position.total_qty,
                    "reason"       : reason,
                }

    return None
