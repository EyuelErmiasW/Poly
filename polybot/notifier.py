"""Discord webhook notifications for trade alerts and portfolio updates."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends trade alerts and portfolio updates to a Discord webhook."""

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self._hourly_stats = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "last_report": time.time(),
        }
        self._session_stats = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "start_time": datetime.now(timezone.utc),
        }

    def _send(self, content: str) -> None:
        """Send a message to the Discord webhook."""
        if not self.webhook_url:
            return
        try:
            resp = requests.post(
                self.webhook_url,
                json={"content": content},
                timeout=10,
            )
            if resp.status_code not in (200, 204):
                log.warning("Discord webhook returned %d: %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("Failed to send Discord notification: %s", e)

    def _send_embed(self, title: str, description: str, color: int) -> None:
        """Send a rich embed to Discord."""
        if not self.webhook_url:
            return
        try:
            resp = requests.post(
                self.webhook_url,
                json={
                    "embeds": [{
                        "title": title,
                        "description": description,
                        "color": color,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }]
                },
                timeout=10,
            )
            if resp.status_code not in (200, 204):
                log.warning("Discord webhook returned %d", resp.status_code)
        except Exception as e:
            log.warning("Failed to send Discord embed: %s", e)

    def notify_startup(self, strategy: str, max_trade: float, max_exposure: float) -> None:
        """Send bot startup notification."""
        self._send_embed(
            "Bot Started",
            (
                f"**Strategy:** {strategy}\n"
                f"**Max Trade:** ${max_trade:.2f}\n"
                f"**Max Exposure:** ${max_exposure:.2f}\n"
                f"**Scan Interval:** 15s"
            ),
            0x2196F3,  # blue
        )

    def notify_order_placed(
        self,
        side: str,
        outcome: str,
        price: float,
        size: float,
        market: str,
        reason: str,
    ) -> None:
        """Send notification when an order is placed."""
        cost = size * price
        self._send_embed(
            f"Order Placed: {side} {outcome}",
            (
                f"**Market:** {market[:100]}\n"
                f"**Side:** {side} {outcome}\n"
                f"**Price:** ${price:.4f}\n"
                f"**Shares:** {size:.2f}\n"
                f"**Cost:** ${cost:.2f}\n"
                f"**Signal:** {reason[:150]}"
            ),
            0xFF9800,  # orange
        )
        self._hourly_stats["trades"] += 1
        self._session_stats["trades"] += 1

    def notify_win(self, outcome: str, market: str, pnl: float) -> None:
        """Send win notification."""
        self._hourly_stats["wins"] += 1
        self._hourly_stats["pnl"] += pnl
        self._session_stats["wins"] += 1
        self._session_stats["pnl"] += pnl
        self._send_embed(
            f"WIN: {outcome}",
            (
                f"**Market:** {market[:100]}\n"
                f"**P&L:** ${pnl:+.2f}\n"
                f"**Session Total:** ${self._session_stats['pnl']:+.2f}"
            ),
            0x4CAF50,  # green
        )

    def notify_loss(self, outcome: str, market: str, pnl: float) -> None:
        """Send loss notification."""
        self._hourly_stats["losses"] += 1
        self._hourly_stats["pnl"] += pnl
        self._session_stats["losses"] += 1
        self._session_stats["pnl"] += pnl
        self._send_embed(
            f"LOSS: {outcome}",
            (
                f"**Market:** {market[:100]}\n"
                f"**P&L:** ${pnl:+.2f}\n"
                f"**Session Total:** ${self._session_stats['pnl']:+.2f}"
            ),
            0xF44336,  # red
        )

    def send_hourly_report(self) -> None:
        """Send hourly portfolio summary."""
        stats = self._hourly_stats
        session = self._session_stats

        uptime = datetime.now(timezone.utc) - session["start_time"]
        hours = uptime.total_seconds() / 3600

        win_rate = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
        session_wr = session["wins"] / session["trades"] * 100 if session["trades"] else 0

        self._send_embed(
            "Hourly Report",
            (
                f"**Last Hour:**\n"
                f"Trades: {stats['trades']} | "
                f"Wins: {stats['wins']} | "
                f"Losses: {stats['losses']} | "
                f"Win Rate: {win_rate:.0f}%\n"
                f"Hour P&L: ${stats['pnl']:+.2f}\n"
                f"\n"
                f"**Session Total ({hours:.1f}h):**\n"
                f"Trades: {session['trades']} | "
                f"Wins: {session['wins']} | "
                f"Losses: {session['losses']} | "
                f"Win Rate: {session_wr:.0f}%\n"
                f"Session P&L: ${session['pnl']:+.2f}"
            ),
            0x9C27B0,  # purple
        )

        # Reset hourly counters
        self._hourly_stats = {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
            "last_report": time.time(),
        }

    def maybe_send_hourly(self) -> None:
        """Check if it's time for an hourly report and send if so."""
        elapsed = time.time() - self._hourly_stats["last_report"]
        if elapsed >= 3600:  # 1 hour
            self.send_hourly_report()

    def notify_shutdown(self) -> None:
        """Send bot shutdown notification."""
        session = self._session_stats
        session_wr = session["wins"] / session["trades"] * 100 if session["trades"] else 0
        self._send_embed(
            "Bot Stopped",
            (
                f"**Session Summary:**\n"
                f"Trades: {session['trades']} | "
                f"Win Rate: {session_wr:.0f}%\n"
                f"Total P&L: ${session['pnl']:+.2f}"
            ),
            0x607D8B,  # grey
        )
