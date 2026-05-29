"""
Strategy logic layer — all decision-making for the trading strategy.

Pure functions only (no I/O, no side effects). Every decision the strategy
makes lives here so it can be tested independently of the bot.

Rules derived from 44-day backtest on 1,159 signals:
  Entry:  BTC 3d > 0% AND BTC 7d > 0% AND skip_score <= 1
  TP:     At each TP hit, score 0-3 from (fast, volatile, small-cap)
          Score <= 1 → exit. Score >= 2 → hold, trail SL up.
  Cap:    Exit at TP100 regardless of score. No greed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Speed thresholds (hours) for the continuation score ──────────────────────
_FAST_THRESHOLD: dict[int, float] = {
    5:   12.0,
    10:  24.0,
    20:  24.0,
    30:  36.0,
    50:  48.0,
    75:  60.0,
    100: 72.0,
}

# ── SL ladder: after THIS tp hits with score >= 2, move SL to VALUE ──────────
# Key = tp level just hit.  Value = new SL % from entry (price level).
_SL_LADDER: dict[int, float] = {
    5:   0.0,    # breakeven
    10:  5.0,
    20:  10.0,
    30:  20.0,
    50:  30.0,
    75:  50.0,
}

# ── TP targets in order ───────────────────────────────────────────────────────
TP_LEVELS = [5, 10, 20, 30, 50, 75, 100]


# ─────────────────────────────────────────────────────────────────────────────
# Entry filter
# ─────────────────────────────────────────────────────────────────────────────

def compute_skip_score(alert: dict) -> tuple[int, list[str]]:
    """
    Count how many of the 6 'bad' entry conditions are true.
    Returns (score, list_of_reasons).
    Score >= 2 → skip the signal.
    """
    add = alert.get("additional_data") or {}
    score = 0
    reasons: list[str] = []

    mcap = add.get("market_cap_usd")
    if mcap is not None and mcap > 200_000_000:
        score += 1
        reasons.append(f"mcap ${mcap/1e6:.0f}M > $200M")

    vol_recent = add.get("volatility_recent_10_pct")
    if vol_recent is not None and vol_recent < 1.5:
        score += 1
        reasons.append(f"vol_recent {vol_recent:.2f}% < 1.5%")

    chg_24h = alert.get("price_change_24h")
    if chg_24h is not None and chg_24h < 5.0:
        score += 1
        reasons.append(f"24h chg {chg_24h:.1f}% < 5%")

    oi_chg = add.get("oi_change_pct")
    if oi_chg is not None and oi_chg < 3.0:
        score += 1
        reasons.append(f"OI chg {oi_chg:.1f}% < 3%")

    funding = add.get("funding_rate")
    if funding is not None and funding < 0.0:
        score += 1
        reasons.append(f"funding {funding:.4f}% < 0")

    compressed = add.get("is_compressed")
    if compressed:
        score += 1
        reasons.append("volatility compressed")

    return score, reasons


def passes_entry_filter(
    alert: dict,
    btc_3d: Optional[float],
    btc_7d: Optional[float],
    skip_score_max: int = 1,
) -> tuple[bool, str]:
    """
    Check all entry conditions. Returns (passes, reason_string).
    reason_string explains WHY it passed or failed — used in Telegram alerts.
    """
    reasons: list[str] = []

    # ── BTC macro filter ──────────────────────────────────────────────
    btc_ok = True
    if btc_3d is None:
        reasons.append("⚠️ BTC 3d: unavailable")
        btc_ok = False
    elif btc_3d <= 0:
        reasons.append(f"❌ BTC 3d: {btc_3d:+.2f}% ≤ 0%")
        btc_ok = False
    else:
        reasons.append(f"✅ BTC 3d: {btc_3d:+.2f}%")

    if btc_7d is None:
        reasons.append("⚠️ BTC 7d: unavailable")
        btc_ok = False
    elif btc_7d <= 0:
        reasons.append(f"❌ BTC 7d: {btc_7d:+.2f}% ≤ 0%")
        btc_ok = False
    else:
        reasons.append(f"✅ BTC 7d: {btc_7d:+.2f}%")

    # ── Skip score ────────────────────────────────────────────────────
    score, bad_reasons = compute_skip_score(alert)
    if score > skip_score_max:
        reasons.append(f"❌ Skip score: {score}/6 > max {skip_score_max}")
        for r in bad_reasons:
            reasons.append(f"   • {r}")
        return False, "\n".join(reasons)
    else:
        reasons.append(f"✅ Skip score: {score}/6")
        for r in bad_reasons:
            reasons.append(f"   • {r}")

    if not btc_ok:
        return False, "\n".join(reasons)

    return True, "\n".join(reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Continuation score (used at every TP hit)
# ─────────────────────────────────────────────────────────────────────────────

def compute_continuation_score(
    snapshot: dict,
    tp_level: int,
) -> tuple[int, list[str]]:
    """
    Score 0-3 from the TP snapshot. Higher = more likely to continue.
    Returns (score, list_of_parts_for_display).
    """
    score = 0
    parts: list[str] = []

    fast_h = _FAST_THRESHOLD.get(tp_level, 24.0)
    hours = snapshot.get("hit_hours_after_entry")
    if hours is not None:
        if hours < fast_h:
            score += 1
            parts.append(f"✅ Fast: {hours:.1f}h < {fast_h:.0f}h")
        else:
            parts.append(f"❌ Slow: {hours:.1f}h ≥ {fast_h:.0f}h")
    else:
        parts.append("⚠️ Speed: unknown")

    vol = snapshot.get("volatility_recent_10_pct")
    if vol is not None:
        if vol > 2.5:
            score += 1
            parts.append(f"✅ Volatile: {vol:.2f}% > 2.5%")
        else:
            parts.append(f"❌ Quiet: {vol:.2f}% ≤ 2.5%")
    else:
        parts.append("⚠️ Volatility: unknown")

    mcap = snapshot.get("market_cap_usd")
    if mcap is not None:
        if mcap < 100_000_000:
            score += 1
            parts.append(f"✅ Small cap: ${mcap/1e6:.0f}M < $100M")
        else:
            parts.append(f"❌ Large cap: ${mcap/1e6:.0f}M ≥ $100M")
    else:
        parts.append("⚠️ Market cap: unknown")

    return score, parts


def decide_tp_action(
    score: int,
    tp_level: int,
) -> tuple[str, Optional[float]]:
    """
    Given a continuation score and which TP just hit, decide what to do.
    Returns (action_str, new_sl_pct_or_None).

    action_str: 'EXIT' or 'HOLD'
    new_sl_pct: the % from entry to place the new stop-loss (None if exit)
    """
    # TP100 is always an exit regardless of score
    if tp_level >= 100:
        return "EXIT", None

    if score <= 1:
        return "EXIT", None

    # score >= 2 → hold and trail SL
    new_sl = _SL_LADDER.get(tp_level)
    return "HOLD", new_sl


# ─────────────────────────────────────────────────────────────────────────────
# Position state machine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StrategyPosition:
    """
    Tracks strategy-layer state for one open position.
    NOT the same as the Binance order — this is the strategy decision layer.
    """
    symbol: str
    entry_price: float
    entry_ts: float
    margin_usdt: float           # USDT margin committed
    leverage: int                # e.g. 5
    current_sl_pct: float = -20.0   # current stop-loss as % from entry (initially -20%)
    highest_tp_hit: int = 0          # highest TP level hit so far (0 = none)
    is_closed: bool = False
    close_reason: str = ""
    close_price_pct: float = 0.0     # final realized % from entry
    tp_history: list[dict] = field(default_factory=list)  # log of each TP action

    @property
    def sl_price(self) -> float:
        """Current stop-loss as an absolute price."""
        return self.entry_price * (1 + self.current_sl_pct / 100)

    @property
    def position_notional(self) -> float:
        return self.margin_usdt * self.leverage

    def margin_pnl_pct(self, price_pct: float) -> float:
        """P&L as % of margin for a given price % from entry."""
        return price_pct * self.leverage

    def margin_pnl_usdt(self, price_pct: float, fees_pct: float = 0.8) -> float:
        """P&L in USDT after fees. fees_pct is total fee drag as % of margin."""
        raw = self.margin_usdt * self.margin_pnl_pct(price_pct) / 100
        fee = self.margin_usdt * fees_pct / 100
        return raw - fee

    def log_tp_action(
        self,
        tp_level: int,
        score: int,
        action: str,
        new_sl: Optional[float],
    ) -> None:
        self.tp_history.append({
            "tp_level": tp_level,
            "score": score,
            "action": action,
            "new_sl_pct": new_sl,
        })
        if action == "HOLD" and new_sl is not None:
            self.current_sl_pct = new_sl
        if tp_level > self.highest_tp_hit:
            self.highest_tp_hit = tp_level

    def check_sl_hit(self, current_price: float) -> bool:
        """Return True if the current price has hit or crossed the SL."""
        return current_price <= self.sl_price

    def close(self, reason: str, price_pct: float) -> None:
        self.is_closed = True
        self.close_reason = reason
        self.close_price_pct = price_pct


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def sl_level_name(sl_pct: float) -> str:
    """Human-readable name for the current SL level."""
    if sl_pct <= -19.9:
        return "initial -20% (entry loss)"
    if abs(sl_pct) < 0.1:
        return "breakeven (0%)"
    return f"+{sl_pct:.0f}% (locked profit)"


def tp_icon(tp_level: int) -> str:
    if tp_level >= 75:
        return "💎🚀"
    if tp_level >= 50:
        return "🚀🚀"
    if tp_level >= 30:
        return "🚀"
    if tp_level >= 10:
        return "🎯"
    return "✅"
