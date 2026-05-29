"""
Telegram Bot API helper.

Sends:
  - Breakout alerts (signal entry)
  - Take-profit target hit alerts
  - Reversal warning alerts
  - Startup summary
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._session = requests.Session()
        self._ok = False

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def validate(self) -> bool:
        try:
            r = self._session.get(self._url("getMe"), timeout=10).json()
            if r.get("ok"):
                logger.info("Telegram bot validated: @%s", r["result"].get("username"))
                self._ok = True
                return True
            logger.error("Telegram validation failed: %s", r)
        except Exception as exc:
            logger.error("Telegram validation error: %s", exc)
        return False

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        for attempt in range(3):
            try:
                r = self._session.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    logger.warning("Telegram 429 — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    def send_document(self, file_path: str, caption: str = "") -> bool:
        """Send a file as a Telegram document."""
        for attempt in range(3):
            try:
                with open(file_path, "rb") as f:
                    r = self._session.post(
                        self._url("sendDocument"),
                        data={"chat_id": self._chat_id, "caption": caption},
                        files={"document": f},
                        timeout=30,
                    ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    time.sleep(wait)
                    continue
                logger.error("Telegram send_document error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram send_document failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    # ── alert types ──────────────────────────────────────────────────

    def send_alert(self, data: dict) -> bool:
        return self.send(self._fmt_alert(data))

    def send_startup(self, summary: str) -> bool:
        return self.send(
            f"🤖 <b>Volume Scanner Started</b>\n\n{summary}\n\nScanner is now running …"
        )

    def send_take_profit(self, data: dict) -> bool:
        return self.send(self._fmt_take_profit(data))

    def send_reversal_warning(self, data: dict) -> bool:
        return self.send(self._fmt_reversal(data))

    # ── strategy alert types ──────────────────────────────────────────

    def send_strategy_entry(
        self, alert: dict, pos, btc_3d, btc_7d,
        balance: float, open_count: int, max_open: int,
    ) -> bool:
        """Alert when strategy opens a paper/live position."""
        sym = alert["symbol"]
        reason = alert.get("strategy_filter_reason", "")
        skip_score = alert.get("strategy_skip_score", 0)
        mode = "📋 PAPER" if True else "🔴 LIVE"  # always shows mode in message

        notional = pos.margin_usdt * pos.leverage
        sl_price = pos.entry_price * (1 + pos.current_sl_pct / 100)

        text = (
            f"🟢 <b>STRATEGY ENTRY — {sym}</b>\n"
            f"{'━' * 28}\n\n"
            f"💵 Entry:      {self._fp(pos.entry_price)}\n"
            f"🛑 SL:         {self._fp(sl_price)}  ({pos.current_sl_pct:.0f}%)\n"
            f"📦 Margin:     ${pos.margin_usdt:.2f} × {pos.leverage}x = ${notional:.2f} notional\n\n"
            f"<b>Entry filter: ✅ PASSED</b>\n"
            f"{reason}\n\n"
            f"📊 Position {open_count}/{max_open}\n"
            f"💰 Balance:    ${balance:.2f}"
        )
        return self.send(text)

    def send_strategy_blocked(self, alert: dict, filter_reason: str) -> bool:
        """Alert for signals seen but NOT traded because filter failed."""
        sym = alert["symbol"]
        text = (
            f"⚪ <b>SIGNAL BLOCKED — {sym}</b>\n"
            f"{'━' * 28}\n\n"
            f"Strategy filter: ❌ DID NOT PASS\n\n"
            f"{filter_reason}\n\n"
            f"<i>Signal is tracked normally. Not traded.</i>"
        )
        return self.send(text)

    def send_strategy_tp_hold(
        self, pos, tp_level: int, score: int, score_parts: list,
        new_sl, balance: float,
    ) -> bool:
        """Alert when TP hits and strategy says HOLD + move SL."""
        from strategy import tp_icon, sl_level_name
        sl_label = sl_level_name(new_sl) if new_sl is not None else "unchanged"
        sl_price = pos.entry_price * (1 + (new_sl or 0) / 100)

        score_text = "\n".join(f"  {p}" for p in score_parts)
        pnl_if_sl = pos.margin_pnl_usdt(new_sl or 0) if new_sl is not None else 0

        text = (
            f"{tp_icon(tp_level)} <b>TP{tp_level}% HIT — HOLDING — {pos.symbol}</b>\n"
            f"{'━' * 28}\n\n"
            f"<b>Continuation score: {score}/3</b>\n"
            f"{score_text}\n\n"
            f"🔒 <b>ACTION: HOLD</b>\n"
            f"   SL moved to: {sl_label}\n"
            f"   SL price:    {self._fp(sl_price)}\n"
            f"   Min locked:  +${pnl_if_sl:.2f} (if SL hits)\n\n"
            f"💰 Balance: ${balance:.2f}"
        )
        return self.send(text)

    def send_strategy_tp_exit(
        self, pos, tp_level: int, score: int, score_parts: list,
        pnl: float, balance: float,
    ) -> bool:
        """Alert when TP hits and strategy says EXIT."""
        from strategy import tp_icon
        score_text = "\n".join(f"  {p}" for p in score_parts)
        margin_ret = (pnl / pos.margin_usdt * 100) if pos.margin_usdt > 0 else 0
        tp_path = " → ".join(
            [f"TP{e['tp_level']}" + ("✅" if e['action'] == 'EXIT' else "🔒")
             for e in pos.tp_history]
        )

        text = (
            f"{tp_icon(tp_level)} <b>CLOSED at TP{tp_level}% — {pos.symbol}</b>\n"
            f"{'━' * 28}\n\n"
            f"<b>Score: {score}/3 → EXIT</b>\n"
            f"{score_text}\n\n"
            f"✅ <b>POSITION CLOSED</b>\n"
            f"   Price exit:  +{tp_level}%\n"
            f"   Margin:      ${pos.margin_usdt:.2f}\n"
            f"   Return:      {margin_ret:+.1f}% on margin\n"
            f"   P&L:         {'+' if pnl >= 0 else ''}${pnl:.2f}\n\n"
            f"📊 Trade path: {tp_path}\n\n"
            f"💰 Balance: ${balance:.2f}"
        )
        return self.send(text)

    def send_strategy_sl_hit(self, pos, sl_pct: float, pnl: float, balance: float) -> bool:
        """Alert when a trailed SL is hit and position closes."""
        from strategy import sl_level_name
        sl_label = sl_level_name(sl_pct)
        margin_ret = (pnl / pos.margin_usdt * 100) if pos.margin_usdt > 0 else 0
        best_tp = pos.highest_tp_hit

        if sl_pct <= -19.9:
            icon = "💥"
            title = f"STOPPED OUT (initial SL)"
        elif sl_pct >= 0:
            icon = "🛡️"
            title = f"SL HIT — profit secured"
        else:
            icon = "🛑"
            title = f"SL HIT at {sl_label}"

        text = (
            f"{icon} <b>{title} — {pos.symbol}</b>\n"
            f"{'━' * 28}\n\n"
            f"SL level:     {sl_label}\n"
            f"Margin:       ${pos.margin_usdt:.2f}\n"
            f"Return:       {margin_ret:+.1f}% on margin\n"
            f"P&L:          {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
            f"Best TP hit:  +{best_tp}%\n\n"
            f"💰 Balance: ${balance:.2f}"
        )
        return self.send(text)

    def send_strategy_timeout_close(
        self, pos, pnl: float, price_pct: float, balance: float,
    ) -> bool:
        """Alert when a position is closed because 7-day window expired."""
        margin_ret = (pnl / pos.margin_usdt * 100) if pos.margin_usdt > 0 else 0

        text = (
            f"⏰ <b>7-DAY TIMEOUT — {pos.symbol}</b>\n"
            f"{'━' * 28}\n\n"
            f"Closed at:    {price_pct:+.2f}% from entry\n"
            f"Margin:       ${pos.margin_usdt:.2f}\n"
            f"Return:       {margin_ret:+.1f}% on margin\n"
            f"P&L:          {'+' if pnl >= 0 else ''}${pnl:.2f}\n"
            f"Best TP hit:  +{pos.highest_tp_hit}%\n\n"
            f"💰 Balance: ${balance:.2f}"
        )
        return self.send(text)

    def send_no_signals_status(
        self, reason: str, btc_3d, btc_7d, btc_detail: dict = None,
        positions_status: str = "",
    ) -> bool:
        """Alert when no signals are being taken due to macro filter."""
        btc_3d_str = f"{btc_3d:+.2f}%" if btc_3d is not None else "unavailable"
        btc_7d_str = f"{btc_7d:+.2f}%" if btc_7d is not None else "unavailable"
        btc_4h = btc_detail.get("btc_chg_4h", 0) if btc_detail else 0
        btc_24h = btc_detail.get("btc_chg_24h", 0) if btc_detail else 0

        text = (
            f"📵 <b>NO NEW ENTRIES — FILTER BLOCKING</b>\n"
            f"{'━' * 28}\n\n"
            f"<b>Reason:</b> {reason}\n\n"
            f"BTC 4h:   {btc_4h:+.2f}%\n"
            f"BTC 24h:  {btc_24h:+.2f}%\n"
            f"BTC 3d:   {btc_3d_str}\n"
            f"BTC 7d:   {btc_7d_str}\n\n"
            f"<i>New entries blocked. Open positions running normally.</i>\n"
            f"<i>Entries resume when BTC 3d AND 7d both &gt; 0%</i>"
        )
        if positions_status:
            text += f"\n\n{positions_status}"

        return self.send(text)

    def send_strategy_skipped_max_open(
        self, symbol: str, open_count: int, balance: float,
    ) -> bool:
        """Alert when a signal is skipped because max_open is reached."""
        text = (
            f"⚠️ <b>SKIPPED (MAX OPEN) — {symbol}</b>\n"
            f"Open positions: {open_count} (at limit)\n"
            f"Signal passed filter but no slot available.\n"
            f"💰 Balance: ${balance:.2f}"
        )
        return self.send(text)

    # ── price formatting ─────────────────────────────────────────────

    @staticmethod
    def _fp(price: float) -> str:
        if price <= 0:
            return "N/A"
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.8f}"

    # ── signal alert format ──────────────────────────────────────────

    @staticmethod
    def _fmt_alert(d: dict) -> str:
        symbol = d["symbol"]
        tf = d.get("timeframe", "1h")
        price = d.get("price", "N/A")
        brk_margin = d.get("breakout_margin_pct", 0)
        price_chg = d.get("price_change_24h", 0)
        v1 = d.get("vol_candle_1_fmt", "?")
        v2 = d.get("vol_candle_2_fmt", "?")
        v3 = d.get("vol_candle_3_fmt", "?")
        bv1 = d.get("vol_candle_1_base_fmt", "?")
        bv2 = d.get("vol_candle_2_base_fmt", "?")
        bv3 = d.get("vol_candle_3_base_fmt", "?")
        rvol = d.get("rvol", 0)
        alert_time = d.get("alert_time", "N/A")
        cooldown = d.get("cooldown_hours", 12)

        chg_icon = "🟢" if price_chg >= 0 else "🔴"
        high_brk = d.get("high_breakout_warning", False)

        btc_trend = d.get("btc_trend", "unknown")
        btc_detail = d.get("btc_trend_detail", {})
        btc_icons = {"ranging": "🟢", "pumping": "🟡", "dumping": "🔴", "unknown": "❓"}
        btc_labels = {"ranging": "RANGING ✓", "pumping": "PUMPING", "dumping": "DUMPING", "unknown": "UNKNOWN"}
        btc_icon = btc_icons.get(btc_trend, "❓")
        btc_label = btc_labels.get(btc_trend, "UNKNOWN")

        header = "⚠️ <b>BREAKOUT SIGNAL — HIGH BREAKOUT</b>" if high_brk else "🚨 <b>BREAKOUT SIGNAL</b>"

        base_coin = symbol.replace("USDT", "").replace("BUSD", "")

        lines = [
            header,
            f"{'━' * 28}",
            "",
            f"📌 <b>{symbol}</b>  |  {tf}",
            f"💵 <b>Price:</b>  ${price}",
            "",
            f"1️⃣ <b>Breakout:</b>  +{brk_margin:.2f}% above 24h high",
            f"2️⃣ <b>Vol USDT:</b>  {v1} → {v2} → {v3}  ({rvol:.1f}x avg)",
            f"    <b>Vol {base_coin}:</b>  {bv1} → {bv2} → {bv3}",
            f"3️⃣ <b>24h Change:</b>  {chg_icon} {price_chg:+.1f}%",
            "",
        ]

        btc_chg_4h = btc_detail.get("btc_chg_4h")
        btc_chg_24h = btc_detail.get("btc_chg_24h")
        if btc_chg_4h is not None:
            lines.append(f"₿ <b>BTC Trend:</b>  {btc_icon} {btc_label}  (4h: {btc_chg_4h:+.2f}%  24h: {btc_chg_24h:+.2f}%)")
            lines.append("")

        if high_brk:
            lines.append(f"⚠️ <b>Warning:</b> Breakout margin {brk_margin:.2f}% > 5% — enter with caution")
            lines.append("")

        q_score = d.get("quality_score", "?")
        s_flags = d.get("soft_flags", 0)
        sf_details = d.get("soft_flag_details", [])
        q_details = d.get("quality_details", [])

        if q_score >= 7:
            grade = "🟢 EXCELLENT"
        elif q_score >= 5:
            grade = "🟢 STRONG"
        elif q_score >= 4:
            grade = "🟡 GOOD"
        elif q_score >= 2:
            grade = "🟠 FAIR"
        else:
            grade = "🔴 WEAK"

        lines.append(f"⭐ <b>Quality:</b>  {q_score}/8  {grade}")
        if s_flags > 0:
            lines.append(f"🚩 <b>Warnings:</b>  {s_flags}/8  ({', '.join(sf_details)})")
        else:
            lines.append(f"🚩 <b>Warnings:</b>  0/8")
        lines.append("")

        lines.extend([
            f"🕐 <b>Time:</b>  {alert_time}",
            f"⏱ <b>Cooldown:</b>  {cooldown}h",
        ])

        # ── STRATEGY DECISION — always shown at bottom of signal ──────
        should_trade = d.get("strategy_should_trade")
        if should_trade is True:
            b3   = d.get("strategy_btc_3d")
            b7   = d.get("strategy_btc_7d")
            skip = d.get("strategy_skip_score", 0)
            b3s  = f"{b3:+.2f}%" if b3 is not None else "N/A"
            b7s  = f"{b7:+.2f}%" if b7 is not None else "N/A"
            lines += [
                "",
                "━" * 28,
                "🟢 <b>STRATEGY: TRADE OPENED</b>",
                f"   BTC 3d: {b3s} ✅   BTC 7d: {b7s} ✅",
                f"   Skip score: {skip}/6 ✅",
                f"   📋 Paper position opened",
            ]
        elif should_trade is False:
            reason = d.get("strategy_filter_reason", "")
            lines += [
                "",
                "━" * 28,
                "⛔ <b>STRATEGY: NOT TRADED</b>",
            ]
            for line in reason.split("\n"):
                if line.strip():
                    lines.append(f"   {line.strip()}")
        elif should_trade is None:
            pass  # strategy disabled — show nothing

        return "\n".join(lines)

    # ── take-profit alert format ─────────────────────────────────────

    def _fmt_take_profit(self, d: dict) -> str:
        target = d["target"]
        if target >= 75:
            icon = "💎🚀🚀"
        elif target >= 50:
            icon = "🚀🚀🚀"
        elif target >= 30:
            icon = "🚀🚀"
        elif target >= 10:
            icon = "🚀"
        elif target >= 5:
            icon = "🎯"
        else:
            icon = "✅"

        cur_pct = d.get("cur_pct", 0)
        high_pct = d.get("high_pct", 0)
        age = d.get("age_str", "")

        return (
            f"{icon} <b>TARGET HIT  +{target}%</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{high_pct:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({cur_pct:+.2f}%)\n"
            f"⏱  Age:      {age}\n\n"
            f"{'🟢 Still above target' if cur_pct >= target else '⚠️ Price pulled back from target'}"
        )

    # ── reversal warning format ──────────────────────────────────────

    def _fmt_reversal(self, d: dict) -> str:
        return (
            f"⚠️ <b>REVERSAL WARNING</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{d['high_pct']:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({d['cur_pct']:+.2f}%)\n"
            f"📉 Drop:     {d['drop_pct']:.2f}% from peak\n"
            f"⏱  Age:      {d.get('age_str', '')}\n\n"
            f"Price has dropped significantly from its peak.\n"
            f"Consider taking remaining profits."
        )
