"""
Paper / Live position manager.

In PAPER MODE (paper_mode: true in config):
  - Simulates position opens, SL moves, and closes
  - Tracks a virtual balance starting at starting_balance
  - Sends Telegram alerts for every action
  - Persists state to data/paper_positions.json

In LIVE MODE (paper_mode: false):
  - Places real Binance Futures orders (market entry, stop-market SL)
  - All other logic identical to paper mode
  - Only enable after 30+ days of paper validation

PAPER_MODE is the master safety switch.
Never touches real orders unless explicitly disabled in config.
"""

from __future__ import annotations

import json
import logging
import time
import threading
from pathlib import Path
from typing import Optional

from strategy import StrategyPosition, sl_level_name, tp_icon

logger = logging.getLogger(__name__)


class PaperTrader:
    """
    Manages all open strategy positions.
    Thread-safe. Persists state across restarts.
    """

    def __init__(
        self,
        config: dict,
        notifier,              # TelegramNotifier
        binance=None,          # BinanceClient (only needed for live mode)
    ) -> None:
        sc = config.get("strategy", {})

        self.enabled: bool       = sc.get("enabled", True)
        self.paper_mode: bool    = sc.get("paper_mode", True)
        self.balance: float      = sc.get("starting_balance", 50.0)
        self.leverage: int       = sc.get("leverage", 5)
        self.margin_pct: float   = sc.get("margin_pct_per_trade", 0.03)
        self.max_open: int       = sc.get("max_open_trades", 40)
        self.initial_sl: float   = sc.get("initial_sl_pct", -20.0)
        self.fee_rate: float     = sc.get("fee_rate", 0.0004)     # 0.04% taker each side
        self.slip_rate: float    = sc.get("slippage_rate", 0.001) # 0.1% slippage each side

        data_dir = Path(config.get("tracker", {}).get("data_dir", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state_file = data_dir / "paper_positions.json"
        self._stats_file = data_dir / "paper_stats.json"

        self._notifier = notifier
        self._binance = binance
        self._lock = threading.Lock()

        # symbol → StrategyPosition
        self._positions: dict[str, StrategyPosition] = {}

        # Cumulative stats
        self._stats: dict = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakevens": 0,
            "liquidations": 0,
            "total_pnl_usdt": 0.0,
            "peak_balance": self.balance,
        }

        self._load_state()

        mode = "PAPER" if self.paper_mode else "🔴 LIVE"
        logger.info(
            "PaperTrader initialised [%s mode] — balance=$%.2f, leverage=%dx, "
            "margin=%.0f%%, max_open=%d",
            mode, self.balance, self.leverage, self.margin_pct * 100, self.max_open,
        )
        if not self.paper_mode:
            logger.warning(
                "⚠️  LIVE MODE ACTIVE — real orders will be placed on Binance!"
            )

    # ── persistence ───────────────────────────────────────────────────────────

    def _state_to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "stats": self._stats,
            "positions": {
                sym: {
                    "symbol":         p.symbol,
                    "entry_price":    p.entry_price,
                    "entry_ts":       p.entry_ts,
                    "margin_usdt":    p.margin_usdt,
                    "leverage":       p.leverage,
                    "current_sl_pct": p.current_sl_pct,
                    "highest_tp_hit": p.highest_tp_hit,
                    "tp_history":     p.tp_history,
                }
                for sym, p in self._positions.items()
                if not p.is_closed
            },
        }

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.balance = data.get("balance", self.balance)
            self._stats.update(data.get("stats", {}))
            for sym, pd in data.get("positions", {}).items():
                p = StrategyPosition(
                    symbol=pd["symbol"],
                    entry_price=pd["entry_price"],
                    entry_ts=pd["entry_ts"],
                    margin_usdt=pd["margin_usdt"],
                    leverage=pd["leverage"],
                    current_sl_pct=pd.get("current_sl_pct", self.initial_sl),
                    highest_tp_hit=pd.get("highest_tp_hit", 0),
                    tp_history=pd.get("tp_history", []),
                )
                self._positions[sym] = p
            logger.info(
                "Loaded paper state: balance=$%.2f, %d open positions",
                self.balance, len(self._positions),
            )
        except Exception as exc:
            logger.error("Failed to load paper state: %s", exc)

    def _save_state(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state_to_dict(), fh, indent=2)
            tmp.replace(self._state_file)
        except Exception as exc:
            logger.error("Failed to save paper state: %s", exc)

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        return len([p for p in self._positions.values() if not p.is_closed])

    @property
    def equity(self) -> float:
        """Free balance + locked margin in open positions."""
        locked = sum(p.margin_usdt for p in self._positions.values() if not p.is_closed)
        return self.balance + locked

    # ── entry ─────────────────────────────────────────────────────────────────

    def open_position(self, alert: dict, btc_3d: float, btc_7d: float) -> bool:
        """
        Open a new position for the given signal.
        Returns True if position was opened.
        """
        if not self.enabled:
            return False

        with self._lock:
            symbol = alert["symbol"]

            if symbol in self._positions and not self._positions[symbol].is_closed:
                logger.info("Already tracking %s — skipping duplicate entry", symbol)
                return False

            if self.open_count >= self.max_open:
                logger.info("Max open trades (%d) reached — skipping %s", self.max_open, symbol)
                self._notifier.send_strategy_skipped_max_open(symbol, self.open_count, self.equity)
                return False

            # Margin = 3% of current equity
            eq = self.equity
            margin = round(eq * self.margin_pct, 4)
            if margin > self.balance:
                logger.info("Insufficient free balance ($%.2f) for margin $%.2f", self.balance, margin)
                return False
            if margin < 0.50:
                logger.info("Margin too small ($%.4f) — skipping %s", margin, symbol)
                return False

            entry_price = float(alert.get("entry_price") or alert.get("price", 0))
            if entry_price <= 0:
                logger.warning("Cannot open position for %s — entry price is 0", symbol)
                return False

            # Deduct margin from free balance
            self.balance -= margin

            pos = StrategyPosition(
                symbol=symbol,
                entry_price=entry_price,
                entry_ts=alert.get("alert_time_ts", time.time()),
                margin_usdt=margin,
                leverage=self.leverage,
                current_sl_pct=self.initial_sl,
            )
            self._positions[symbol] = pos
            self._stats["total_trades"] += 1

            if not self.paper_mode:
                self._place_live_entry(symbol, entry_price, margin)

            self._save_state()
            logger.info(
                "📈 [%s] Position OPENED: %s  margin=$%.2f  entry=$%.6f  SL=-20%%  [%d/%d open]",
                "PAPER" if self.paper_mode else "LIVE",
                symbol, margin, entry_price, self.open_count, self.max_open,
            )

            self._notifier.send_strategy_entry(alert, pos, btc_3d, btc_7d, self.equity, self.open_count, self.max_open)
            return True

    # ── TP hit handler ────────────────────────────────────────────────────────

    def on_tp_hit(
        self,
        symbol: str,
        tp_level: int,
        score: int,
        score_parts: list[str],
        action: str,
        new_sl: Optional[float],
        snapshot: dict,
        current_price: float,
    ) -> None:
        """
        Called by tracker when a TP level is hit for a tracked signal.
        Applies the strategy decision (exit or hold+trail).
        """
        if not self.enabled:
            return

        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None or pos.is_closed:
                return

            pos.log_tp_action(tp_level, score, action, new_sl)
            price_pct = tp_level  # price moved exactly to this TP

            if action == "EXIT":
                pnl = pos.margin_pnl_usdt(price_pct, fees_pct=(self.fee_rate + self.slip_rate) * self.leverage * 2 * 100)
                pnl = max(pnl, -pos.margin_usdt)   # can't lose more than margin
                pos.close(f"exit_tp{tp_level}", price_pct)
                self._finalise_close(pos, pnl, price_pct)

                if not self.paper_mode:
                    self._place_live_exit(symbol, current_price)

                self._notifier.send_strategy_tp_exit(
                    pos=pos, tp_level=tp_level, score=score, score_parts=score_parts,
                    pnl=pnl, balance=self.equity,
                )
            else:  # HOLD
                if not self.paper_mode and new_sl is not None:
                    sl_price = pos.entry_price * (1 + new_sl / 100)
                    self._update_live_sl(symbol, sl_price)

                self._save_state()
                self._notifier.send_strategy_tp_hold(
                    pos=pos, tp_level=tp_level, score=score, score_parts=score_parts,
                    new_sl=new_sl, balance=self.equity,
                )

    # ── SL monitoring ─────────────────────────────────────────────────────────

    def check_sl_hits(self, prices: dict[str, float]) -> None:
        """
        Called regularly (each price update cycle) to check if any trailed SL was hit.
        prices: {symbol: current_mark_price}

        KEY RULE: BTC dumping does NOT close open positions.
        Only the coin's OWN price vs its trailed SL matters.
        Small caps often run independently of BTC. Let each position
        live or die on its own merits.
        """
        if not self.enabled:
            return

        to_close: list[tuple[str, float]] = []

        with self._lock:
            for sym, pos in self._positions.items():
                if pos.is_closed:
                    continue
                price = prices.get(sym)
                if price is None:
                    continue
                if pos.check_sl_hit(price):
                    to_close.append((sym, price))

        for sym, price in to_close:
            self._close_at_sl(sym, price)

    def _close_at_sl(self, symbol: str, current_price: float) -> None:
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None or pos.is_closed:
                return

            sl_pct = pos.current_sl_pct
            price_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100
            # Use SL level, not current price, as the booked price
            booked_pct = sl_pct

            pnl = pos.margin_pnl_usdt(booked_pct, fees_pct=(self.fee_rate + self.slip_rate) * self.leverage * 2 * 100)
            pnl = max(pnl, -pos.margin_usdt)

            close_reason = "sl_initial" if sl_pct <= -19.9 else f"sl_trailed_{sl_pct:.0f}pct"
            pos.close(close_reason, booked_pct)
            self._finalise_close(pos, pnl, booked_pct)

            if not self.paper_mode:
                self._place_live_exit(symbol, current_price)

            logger.info(
                "🛑 [%s] SL hit %s: sl_pct=%.1f%%  price=$%.6f  pnl=%+.4f",
                "PAPER" if self.paper_mode else "LIVE",
                symbol, sl_pct, current_price, pnl,
            )
            self._notifier.send_strategy_sl_hit(pos=pos, sl_pct=sl_pct, pnl=pnl, balance=self.equity)
            self._save_state()

    def _finalise_close(self, pos: StrategyPosition, pnl: float, price_pct: float) -> None:
        """Update balance and stats after a position closes."""
        self.balance += pos.margin_usdt + pnl  # return margin + P&L
        self._stats["total_pnl_usdt"] += pnl

        if pnl > 0.01:
            self._stats["wins"] += 1
        elif pnl < -0.01:
            if price_pct <= -19.0:
                self._stats["liquidations"] += 1
            else:
                self._stats["losses"] += 1
        else:
            self._stats["breakevens"] += 1

        if self.equity > self._stats["peak_balance"]:
            self._stats["peak_balance"] = self.equity

        self._save_state()

    # ── manual / forced close ─────────────────────────────────────────────────

    def force_close_expired(self, symbol: str, exit_price: float) -> None:
        """Close a position that has been archived by the tracker (7-day expiry)."""
        with self._lock:
            pos = self._positions.get(symbol)
            if pos is None or pos.is_closed:
                return

            price_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100
            pnl = pos.margin_pnl_usdt(price_pct, fees_pct=(self.fee_rate + self.slip_rate) * self.leverage * 2 * 100)
            pnl = max(pnl, -pos.margin_usdt)
            pos.close("7day_timeout", price_pct)
            self._finalise_close(pos, pnl, price_pct)

            logger.info("⏰ [%s] Timeout close: %s  price_pct=%.2f%%  pnl=%+.4f",
                        "PAPER" if self.paper_mode else "LIVE", symbol, price_pct, pnl)
            self._notifier.send_strategy_timeout_close(pos=pos, pnl=pnl, price_pct=price_pct, balance=self.equity)
            self._save_state()

    # ── live order stubs (only called when paper_mode = False) ────────────────

    def _place_live_entry(self, symbol: str, entry_price: float, margin: float) -> None:
        """Place a real long market entry order on Binance Futures."""
        if self._binance is None:
            logger.error("Live mode: binance client not set — cannot place entry for %s", symbol)
            return
        try:
            notional = margin * self.leverage
            # qty = notional / entry_price rounded to symbol precision (simplified)
            qty = round(notional / entry_price, 3)
            logger.info("LIVE ORDER: MARKET BUY %s qty=%.4f", symbol, qty)
            # self._binance.place_order(symbol=symbol, side='BUY', type='MARKET', quantity=qty)
            # ↑ Uncomment and implement when going live. Requires signed API endpoint.
            logger.warning("Live order execution not yet implemented — set paper_mode=true")
        except Exception as exc:
            logger.error("Live entry failed for %s: %s", symbol, exc)

    def _update_live_sl(self, symbol: str, sl_price: float) -> None:
        """Cancel old SL order and place a new stop-market at sl_price."""
        if self._binance is None:
            return
        try:
            logger.info("LIVE SL UPDATE: %s → $%.6f", symbol, sl_price)
            # self._binance.cancel_open_orders(symbol)
            # self._binance.place_order(symbol=symbol, side='SELL', type='STOP_MARKET', stopPrice=sl_price, closePosition=True)
            logger.warning("Live SL update not yet implemented — set paper_mode=true")
        except Exception as exc:
            logger.error("Live SL update failed for %s: %s", symbol, exc)

    def _place_live_exit(self, symbol: str, price: float) -> None:
        """Close the full position at market."""
        if self._binance is None:
            return
        try:
            logger.info("LIVE EXIT: %s at ~$%.6f", symbol, price)
            # self._binance.cancel_open_orders(symbol)
            # self._binance.place_order(symbol=symbol, side='SELL', type='MARKET', reduceOnly=True, quantity=qty)
            logger.warning("Live exit not yet implemented — set paper_mode=true")
        except Exception as exc:
            logger.error("Live exit failed for %s: %s", symbol, exc)

    # ── public getters ────────────────────────────────────────────────────────

    def get_positions_during_dump(self, prices: dict[str, float]) -> str:
        """
        Called when BTC is dumping. Returns a status of ALL open positions
        showing each coin's OWN behavior — because coins move independently.

        This replaces the "no signals today" message with a more useful
        "here is what your open positions are doing right now" report.
        """
        open_pos = self.get_open_positions()
        if not open_pos:
            return ""

        lines = ["<b>📊 OPEN POSITIONS (managed independently of BTC)</b>", "━" * 28, ""]

        for pos in open_pos:
            price = prices.get(pos.symbol)
            if price is None:
                coin_pct = None
                status = "⚠️ price unavailable"
            else:
                coin_pct = ((price - pos.entry_price) / pos.entry_price) * 100
                sl_pct = pos.current_sl_pct
                distance_to_sl = coin_pct - sl_pct   # how far above SL we are

                if coin_pct > 0:
                    status = f"🟢 +{coin_pct:.1f}%  (SL at {sl_pct:+.0f}%,  {distance_to_sl:.1f}% room)"
                elif coin_pct > sl_pct:
                    status = f"🟡 {coin_pct:+.1f}%  (above SL {sl_pct:+.0f}%)"
                else:
                    status = f"🔴 {coin_pct:+.1f}%  ← SL TRIGGERED"

            best_tp = f"+{pos.highest_tp_hit}%" if pos.highest_tp_hit else "none yet"
            lines.append(f"<b>{pos.symbol}</b>  {status}")
            lines.append(f"   Best TP: {best_tp}  |  Margin: ${pos.margin_usdt:.2f}")
            lines.append("")

        lines.append("<i>BTC dumping only blocks NEW entries. Each position is held/closed on its own price action.</i>")
        return "\n".join(lines)

    def get_position(self, symbol: str) -> Optional[StrategyPosition]:
        return self._positions.get(symbol)

    def get_open_positions(self) -> list[StrategyPosition]:
        return [p for p in self._positions.values() if not p.is_closed]

    def get_stats_summary(self) -> str:
        """Returns a formatted stats string for Telegram status commands."""
        s = self._stats
        eq = self.equity
        total = s["total_trades"]
        wins = s["wins"]
        losses = s["losses"]
        be = s["breakevens"]
        liqs = s["liquidations"]
        wr = wins / total * 100 if total > 0 else 0
        peak = s["peak_balance"]
        dd = (eq - peak) / peak * 100 if peak > 0 else 0
        mode = "📋 PAPER" if self.paper_mode else "🔴 LIVE"

        lines = [
            f"<b>{mode} TRADING — ACCOUNT STATUS</b>",
            f"{'━' * 28}",
            f"💰 Balance:    ${self.balance:.2f} (free)",
            f"📊 Equity:     ${eq:.2f} (incl. open margin)",
            f"📈 Total P&L:  {'+' if s['total_pnl_usdt'] >= 0 else ''}${s['total_pnl_usdt']:.2f}",
            f"🏔 Peak:       ${peak:.2f}",
            f"📉 DD from pk: {dd:+.1f}%",
            f"",
            f"<b>TRADE STATS ({total} trades)</b>",
            f"✅ Wins:       {wins} ({wr:.1f}%)",
            f"🛑 Losses:     {losses}",
            f"⚖️  Breakevens: {be}",
            f"💥 Liquidated: {liqs}",
            f"",
            f"<b>OPEN POSITIONS: {self.open_count}/{self.max_open}</b>",
        ]
        for p in self.get_open_positions():
            sl_label = sl_level_name(p.current_sl_pct)
            lines.append(f"  • {p.symbol}  SL→{sl_label}  TP-best: +{p.highest_tp_hit}%")

        return "\n".join(lines)
