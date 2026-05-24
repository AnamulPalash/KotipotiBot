"""
telegram.py — Telegram alert module for KotipotiBot v2
======================================================
Sends messages to a Telegram chat via the Bot API.
All sends are fire-and-forget (background thread) — never blocks the bot loop.

Environment variables required:
  TELEGRAM_TOKEN   — your bot token from @BotFather
  TELEGRAM_CHAT_ID — chat/group ID to send messages to

If either variable is missing, all calls are silently skipped.
"""

import os
import logging
import threading
import urllib.request
import urllib.parse
import json
from datetime import datetime, timezone

log = logging.getLogger("kotipoti.telegram")

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

_enabled = bool(TOKEN and CHAT_ID)


# ── Core send ─────────────────────────────────────────────────────────────────

def _send(text: str) -> None:
    """HTTP POST to Telegram — called in a daemon thread."""
    if not _enabled:
        return
    url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id":    CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                log.warning(f"Telegram non-200: {resp.status}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def send(text: str) -> None:
    """Non-blocking send — spawns a daemon thread."""
    if not _enabled:
        return
    t = threading.Thread(target=_send, args=(text,), daemon=True)
    t.start()


# ── Formatted alert helpers ───────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M UTC")


def trade_opened(pair: str, side: str, entry_price: float,
                 stake_usdt: float, leverage: int,
                 entry_tag: str, dry_run: bool = True) -> None:
    mode  = "🧪 DRY" if dry_run else "🔴 LIVE"
    emoji = "📈" if side == "long" else "📉"
    send(
        f"{emoji} <b>Trade Opened</b> {mode}\n"
        f"Pair: <code>{pair}</code>\n"
        f"Side: <b>{side.upper()}</b> @ <code>{entry_price:.4f}</code>\n"
        f"Stake: <code>{stake_usdt:.0f} USDT</code>  Lev: <code>{leverage}×</code>\n"
        f"Signal: <code>{entry_tag}</code>  |  {_now()}"
    )


def trade_closed(pair: str, side: str, entry_price: float,
                 exit_price: float, profit_usdt: float,
                 profit_pct: float, exit_reason: str,
                 dry_run: bool = True) -> None:
    mode  = "🧪 DRY" if dry_run else "🔴 LIVE"
    sign  = "✅" if profit_usdt >= 0 else "❌"
    p_str = f"+{profit_usdt:.2f}" if profit_usdt >= 0 else f"{profit_usdt:.2f}"
    pct   = f"+{profit_pct:.2f}%" if profit_pct >= 0 else f"{profit_pct:.2f}%"
    send(
        f"{sign} <b>Trade Closed</b> {mode}\n"
        f"Pair: <code>{pair}</code>  ({side.upper()})\n"
        f"Entry: <code>{entry_price:.4f}</code> → Exit: <code>{exit_price:.4f}</code>\n"
        f"P&amp;L: <b>{p_str} USDT</b> ({pct})\n"
        f"Reason: <code>{exit_reason}</code>  |  {_now()}"
    )


def stoploss_hit(pair: str, side: str, entry_price: float,
                 exit_price: float, loss_usdt: float,
                 dry_run: bool = True) -> None:
    mode = "🧪 DRY" if dry_run else "🔴 LIVE"
    send(
        f"🛑 <b>Stoploss Hit</b> {mode}\n"
        f"Pair: <code>{pair}</code>  ({side.upper()})\n"
        f"Entry: <code>{entry_price:.4f}</code> → SL: <code>{exit_price:.4f}</code>\n"
        f"Loss: <b>{loss_usdt:.2f} USDT</b>  |  {_now()}"
    )


def daily_loss_halt(loss_pct: float, limit_pct: float) -> None:
    send(
        f"⚠️ <b>Daily Loss Limit Reached</b>\n"
        f"Loss today: <b>{loss_pct:.1f}%</b>  (limit: {limit_pct:.1f}%)\n"
        f"Bot is <b>halted until tomorrow UTC</b>.  |  {_now()}"
    )


def consecutive_loss_halt(count: int, halt_minutes: int) -> None:
    send(
        f"⚠️ <b>Consecutive Loss Circuit Breaker</b>\n"
        f"<b>{count}</b> losses in a row — pausing for <b>{halt_minutes} min</b>\n"
        f"Bot will resume automatically.  |  {_now()}"
    )


def hermes_tuned(changes: dict) -> None:
    if not changes:
        return
    lines = "\n".join(f"  • {k}: <code>{v}</code>" for k, v in changes.items())
    send(
        f"🧠 <b>Hermes Auto-Tuning</b>\n"
        f"Updated {len(changes)} parameter(s):\n{lines}\n"
        f"|  {_now()}"
    )


def bot_status(event: str, detail: str = "") -> None:
    """Generic bot lifecycle alert: started, stopped, paused, resumed."""
    icons = {"started": "🚀", "stopped": "🛑", "paused": "⏸", "resumed": "▶️",
             "error": "💥"}
    icon = icons.get(event, "ℹ️")
    msg = f"{icon} <b>Bot {event.capitalize()}</b>"
    if detail:
        msg += f"\n{detail}"
    msg += f"  |  {_now()}"
    send(msg)


def signal_skipped(pair: str, direction: str, reason: str) -> None:
    """Optional: log skipped high-interest signals (e.g. ATR too high)."""
    send(
        f"⏭ <b>Signal Skipped</b>\n"
        f"Pair: <code>{pair}</code>  {direction.upper()}\n"
        f"Reason: <code>{reason}</code>  |  {_now()}"
    )
