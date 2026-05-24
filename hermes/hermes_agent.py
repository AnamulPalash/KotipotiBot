"""
Hermes Agent v2 — KotipotiBot
================================
Read-only analyst for Freqtrade dry-run data.

Responsibilities:
  - Daily Telegram digest at 23:55 UTC
  - Weekly Telegram report every Sunday at 22:00 UTC
  - Read from SQLite DB (primary) → exported JSON/CSV (fallback) → logs (errors only)
  - Structured recommendation format
  - NO trading control, NO API keys, NO config writes, NO Freqtrade restarts

Security:
  - Read-only mounts on user_data and logs
  - Writes only to /app/reports/ (hermes/reports/ on host)
  - Never prints secrets in reports or logs
"""

import os
import json
import sqlite3
import schedule
import time
import requests
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

# ---- Paths ----
DB_PATH = Path("/freqtrade/user_data/tradesv3.dryrun.sqlite")
EXPORTS_DIR = Path("/freqtrade/user_data/exports")
LOG_FILE = Path("/freqtrade/user_data/logs/freqtrade.log")
REGIME_LOG = Path("/freqtrade/user_data/logs/regime_log.jsonl")
REPORTS_DIR = Path("/app/reports")

# ---- Config from env ----
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

PAIRS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"]
PAIR_SHORT = {p: p.split("/")[0] for p in PAIRS}  # BTC/USDT:USDT -> BTC

REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ===========================================================================
# Telegram
# ===========================================================================

def send_telegram(message: str, parse_mode: str = "Markdown") -> bool:
    """Send message to Telegram. Returns True on success. Never logs the token."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Hermes] WARNING: Telegram credentials not set.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # Split messages > 4000 chars (Telegram limit)
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    success = True
    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": parse_mode,
            }, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            print(f"[Hermes] Telegram send failed: {type(e).__name__}")
            success = False
    return success


# ===========================================================================
# Data access — SQLite primary, JSON/CSV fallback
# ===========================================================================

def _get_db_conn() -> Optional[sqlite3.Connection]:
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            return conn
        except Exception as e:
            print(f"[Hermes] Cannot open DB: {e}")
    return None


def fetch_trades_from_db(date_from: datetime, date_to: datetime) -> List[Dict]:
    """Fetch closed trades from SQLite for a date range."""
    conn = _get_db_conn()
    if conn is None:
        return []
    try:
        from_ts = date_from.timestamp()
        to_ts = date_to.timestamp()
        cur = conn.execute("""
            SELECT
                id, pair, is_short, open_date, close_date,
                open_rate, close_rate, amount, stake_amount,
                profit_abs, profit_ratio,
                exit_reason, fee_open, fee_close,
                open_trade_value, close_profit_abs
            FROM trades
            WHERE is_open = 0
              AND close_date_hum IS NOT NULL
              AND open_date > ? AND close_date < ?
            ORDER BY close_date ASC
        """, (from_ts, to_ts))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[Hermes] DB query failed: {e}")
        if conn:
            conn.close()
        return []


def fetch_open_trades_from_db() -> List[Dict]:
    conn = _get_db_conn()
    if conn is None:
        return []
    try:
        cur = conn.execute("""
            SELECT id, pair, is_short, open_date, open_rate, stake_amount, amount
            FROM trades WHERE is_open = 1
        """)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"[Hermes] DB open trades query failed: {e}")
        return []


def fetch_wallet_balance_from_db() -> Dict[str, float]:
    """Get simulated wallet start/end from dry-run DB."""
    conn = _get_db_conn()
    if conn is None:
        return {"start": 2000.0, "current": 2000.0}
    try:
        cur = conn.execute("""
            SELECT balance FROM wallet_history
            ORDER BY datetime ASC LIMIT 1
        """)
        row = cur.fetchone()
        start = float(row[0]) if row else 2000.0

        cur = conn.execute("""
            SELECT balance FROM wallet_history
            ORDER BY datetime DESC LIMIT 1
        """)
        row = cur.fetchone()
        current = float(row[0]) if row else start

        conn.close()
        return {"start": start, "current": current}
    except Exception as e:
        print(f"[Hermes] Wallet query failed: {e}")
        return {"start": 2000.0, "current": 2000.0}


def fetch_trades_fallback(date_from: datetime, date_to: datetime) -> List[Dict]:
    """Fallback: load from exported JSON files in exports/."""
    trades = []
    if not EXPORTS_DIR.exists():
        return trades
    for f in sorted(EXPORTS_DIR.glob("trades_closed_*.json")):
        try:
            data = json.loads(f.read_text())
            for t in data:
                close_str = t.get("close_date", "")
                try:
                    close_dt = datetime.fromisoformat(close_str)
                    if date_from <= close_dt <= date_to:
                        trades.append(t)
                except Exception:
                    pass
        except Exception as e:
            print(f"[Hermes] Failed to read {f}: {e}")
    return trades


def get_trades(date_from: datetime, date_to: datetime) -> List[Dict]:
    """Get trades: SQLite primary, JSON fallback."""
    trades = fetch_trades_from_db(date_from, date_to)
    if not trades:
        print("[Hermes] DB empty or unavailable — trying export fallback")
        trades = fetch_trades_fallback(date_from, date_to)
    return trades


# ===========================================================================
# Log parsing — errors and operational events only
# ===========================================================================

def parse_log_errors(date_from: datetime, date_to: datetime) -> Dict:
    """Read freqtrade.log for errors, warnings, restarts. Not for P&L."""
    result = {
        "error_count": 0,
        "warning_count": 0,
        "stale_data_warnings": 0,
        "api_errors": 0,
        "restarts": 0,
        "errors": [],
    }
    if not LOG_FILE.exists():
        return result
    try:
        with open(LOG_FILE, "r") as f:
            for line in f:
                # Filter to date range
                try:
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if not (date_from <= ts <= date_to):
                        continue
                except Exception:
                    continue

                lower = line.lower()
                if " - error" in lower or "fatal" in lower:
                    result["error_count"] += 1
                    result["errors"].append(line.strip()[:200])
                if " - warning" in lower:
                    result["warning_count"] += 1
                if "stale" in lower or "old candle" in lower:
                    result["stale_data_warnings"] += 1
                if "api" in lower and ("error" in lower or "failed" in lower):
                    result["api_errors"] += 1
                if "starting worker" in lower:
                    result["restarts"] += 1
        # Keep last 5 errors only
        result["errors"] = result["errors"][-5:]
    except Exception as e:
        print(f"[Hermes] Log parse failed: {e}")
    return result


# ===========================================================================
# Stats computation
# ===========================================================================

def compute_stats(trades: List[Dict]) -> Dict:
    """Compute all required metrics from trade list."""
    if not trades:
        return {
            "total": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "net_pnl": 0, "gross_profit": 0, "gross_loss": 0,
            "profit_factor": 0, "avg_hold_minutes": 0,
            "max_losing_streak": 0, "best_trade": None, "worst_trade": None,
            "best_pair": None, "worst_pair": None,
            "exit_reasons": {}, "pair_stats": {},
            "fee_estimate": 0,
        }

    wins = [t for t in trades if (t.get("profit_ratio") or 0) >= 0]
    losses = [t for t in trades if (t.get("profit_ratio") or 0) < 0]

    gross_profit = sum(t.get("profit_abs") or 0 for t in wins)
    gross_loss = abs(sum(t.get("profit_abs") or 0 for t in losses))
    net_pnl = gross_profit - gross_loss
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0

    # Hold times
    hold_times = []
    for t in trades:
        try:
            o = datetime.fromisoformat(str(t.get("open_date", "")))
            c = datetime.fromisoformat(str(t.get("close_date", "")))
            hold_times.append((c - o).total_seconds() / 60)
        except Exception:
            pass
    avg_hold = round(sum(hold_times) / len(hold_times), 1) if hold_times else 0

    # Max losing streak
    streak = max_streak = 0
    for t in sorted(trades, key=lambda x: x.get("close_date", "")):
        if (t.get("profit_ratio") or 0) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Best / worst trade
    sorted_by_profit = sorted(trades, key=lambda x: x.get("profit_ratio") or 0)
    worst_trade = sorted_by_profit[0] if sorted_by_profit else None
    best_trade = sorted_by_profit[-1] if sorted_by_profit else None

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        reason = t.get("exit_reason") or "unknown"
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Per-pair stats
    pair_stats = {}
    for t in trades:
        pair = t.get("pair", "unknown")
        short_name = pair.split("/")[0]
        if short_name not in pair_stats:
            pair_stats[short_name] = {"trades": 0, "wins": 0, "pnl": 0.0}
        pair_stats[short_name]["trades"] += 1
        if (t.get("profit_ratio") or 0) >= 0:
            pair_stats[short_name]["wins"] += 1
        pair_stats[short_name]["pnl"] += t.get("profit_abs") or 0

    best_pair = max(pair_stats, key=lambda p: pair_stats[p]["pnl"]) if pair_stats else None
    worst_pair = min(pair_stats, key=lambda p: pair_stats[p]["pnl"]) if pair_stats else None

    # Fee estimate: ~0.055% per side on Bybit futures
    fee_estimate = sum((t.get("stake_amount") or 0) * 0.00055 * 2 for t in trades)

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "net_pnl": round(net_pnl, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": profit_factor,
        "avg_hold_minutes": avg_hold,
        "max_losing_streak": max_streak,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "best_pair": best_pair,
        "worst_pair": worst_pair,
        "exit_reasons": exit_reasons,
        "pair_stats": pair_stats,
        "fee_estimate": round(fee_estimate, 4),
    }


def _fmt_trade(t: Optional[Dict]) -> str:
    if not t:
        return "n/a"
    pair = t.get("pair", "?").split("/")[0]
    pnl = t.get("profit_abs") or 0
    pct = (t.get("profit_ratio") or 0) * 100
    reason = t.get("exit_reason") or "?"
    return f"{pair} {pnl:+.2f} USDT ({pct:+.2f}%) [{reason}]"


# ===========================================================================
# Daily report
# ===========================================================================

def build_daily_report(date: datetime) -> str:
    """Build the full daily Telegram report for yesterday UTC."""
    yesterday = date - timedelta(days=1)
    day_from = yesterday.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_to = yesterday.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    trades = get_trades(day_from, day_to)
    stats = compute_stats(trades)
    wallet = fetch_wallet_balance_from_db()
    log_info = parse_log_errors(day_from, day_to)
    open_trades = fetch_open_trades_from_db()

    pnl_emoji = "🟢" if stats["net_pnl"] >= 0 else "🔴"
    date_str = yesterday.strftime("%Y-%m-%d")

    # Exit reason breakdown
    exit_lines = "\n".join(
        f"  • {reason}: `{count}`"
        for reason, count in sorted(stats["exit_reasons"].items(), key=lambda x: -x[1])
    ) or "  • No exits"

    # Pair breakdown
    pair_lines = "\n".join(
        f"  • {p}: `{d['trades']}` trades | `{d['wins']}/{d['trades']}` W | `{d['pnl']:+.2f}` USDT"
        for p, d in stats["pair_stats"].items()
    ) or "  • No trades"

    # Suggested follow-up (simple heuristic)
    suggestions = []
    if stats["max_losing_streak"] >= 3:
        suggestions.append("Review losing streak — check regime log for shared conditions")
    if stats["profit_factor"] != float("inf") and stats["profit_factor"] < 1.0:
        suggestions.append("Profit factor < 1.0 — exits may be premature or stops too tight")
    if log_info["stale_data_warnings"] > 0:
        suggestions.append(f"{log_info['stale_data_warnings']} stale data warnings — check Pi connectivity")
    if not suggestions:
        suggestions.append("No critical issues detected — monitor next session")

    report = f"""🤖 *Kotipoti Trading Bot — Daily Report*
📅 `{date_str}`

💰 *Wallet*
  • Start: `{wallet['start']:.2f} USDT`
  • End: `{wallet['current']:.2f} USDT`
  • Net P&L: {pnl_emoji} `{stats['net_pnl']:+.4f} USDT`
  • Est. fees: `{stats['fee_estimate']:.4f} USDT`

📊 *Trade Summary*
  • Total trades: `{stats['total']}`
  • Wins / Losses: `{stats['wins']} / {stats['losses']}`
  • Win rate: `{stats['win_rate']}%`
  • Profit factor: `{stats['profit_factor']}`
  • Avg hold time: `{stats['avg_hold_minutes']} min`
  • Max losing streak: `{stats['max_losing_streak']}`
  • Open trades now: `{len(open_trades)}`

🏆 *Best / Worst*
  • Best pair: `{stats['best_pair'] or 'n/a'}`
  • Worst pair: `{stats['worst_pair'] or 'n/a'}`
  • Best trade: `{_fmt_trade(stats['best_trade'])}`
  • Worst trade: `{_fmt_trade(stats['worst_trade'])}`

📤 *Exit Reasons*
{exit_lines}

📈 *Per-Pair Breakdown*
{pair_lines}

⚠️ *Operational*
  • Errors: `{log_info['error_count']}`
  • Warnings: `{log_info['warning_count']}`
  • Stale data warnings: `{log_info['stale_data_warnings']}`
  • API errors: `{log_info['api_errors']}`
  • Restarts: `{log_info['restarts']}`

💡 *Suggested Follow-up*
{chr(10).join('  • ' + s for s in suggestions)}

_Hermes v2 | {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_"""

    return report


# ===========================================================================
# Weekly report
# ===========================================================================

def build_weekly_report(date: datetime) -> str:
    """Build Sunday weekly report covering last 7 days."""
    week_to = date.replace(hour=21, minute=59, second=59, tzinfo=timezone.utc)
    week_from = week_to - timedelta(days=7)

    trades = get_trades(week_from, week_to)
    stats = compute_stats(trades)
    log_info = parse_log_errors(week_from, week_to)

    week_str = f"{week_from.strftime('%Y-%m-%d')} → {week_to.strftime('%Y-%m-%d')}"

    # Session analysis from regime log
    session_stats: Dict[str, Dict] = {"Asia": {}, "London": {}, "US": {}}
    regime_stats: Dict[str, Dict] = {}
    skipped_count = 0
    skipped_filter_fails: Dict[str, int] = {}

    if REGIME_LOG.exists():
        try:
            with open(REGIME_LOG, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        ts = datetime.fromisoformat(entry.get("timestamp", ""))
                        if not (week_from <= ts.replace(tzinfo=timezone.utc) <= week_to):
                            continue
                        if entry.get("type") == "skipped_signal":
                            skipped_count += 1
                            for filt, passed in (entry.get("filters") or {}).items():
                                if passed is False:
                                    skipped_filter_fails[filt] = skipped_filter_fails.get(filt, 0) + 1
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Hermes] Regime log read failed: {e}")

    # Pair ranking
    pair_lines = ""
    if stats["pair_stats"]:
        ranked = sorted(stats["pair_stats"].items(), key=lambda x: -x[1]["pnl"])
        pair_lines = "\n".join(
            f"  {i+1}. *{p}*: `{d['trades']}` trades | `{d['wins']}/{d['trades']}` W | `{d['pnl']:+.2f}` USDT"
            for i, (p, d) in enumerate(ranked)
        )
    else:
        pair_lines = "  No trades this week"

    # Exit reason analysis
    exit_lines = "\n".join(
        f"  • {r}: `{c}`"
        for r, c in sorted(stats["exit_reasons"].items(), key=lambda x: -x[1])
    ) or "  • No exits"

    # Skipped signal filter analysis
    skip_lines = "\n".join(
        f"  • {f}: `{c}` times"
        for f, c in sorted(skipped_filter_fails.items(), key=lambda x: -x[1])
    ) or "  • No skipped signal data"

    # Overtrading check
    overtrade_pairs = [
        p for p, d in stats["pair_stats"].items()
        if d["trades"] > 30  # > 30 trades/week on one pair = possible overtrading
    ]

    # Weekly recommendations
    recommendations = _build_weekly_recommendations(stats, log_info, skipped_filter_fails, overtrade_pairs)

    report = f"""📋 *Kotipoti Trading Bot — Weekly Report*
🗓 `{week_str}`

📊 *Overview*
  • Total trades: `{stats['total']}`
  • Wins / Losses: `{stats['wins']} / {stats['losses']}`
  • Win rate: `{stats['win_rate']}%`
  • Net P&L: `{stats['net_pnl']:+.4f} USDT`
  • Profit factor: `{stats['profit_factor']}`
  • Est. fees: `{stats['fee_estimate']:.4f} USDT`
  • Max losing streak: `{stats['max_losing_streak']}`

🏅 *Pair Ranking (by P&L)*
{pair_lines}

📤 *Exit Reason Breakdown*
{exit_lines}

🔍 *Skipped Signal Analysis*
  Signals blocked: `{skipped_count}`
  Most common filter failure:
{skip_lines}

⚠️ *Overtrading Check*
  {'⚠️ Possible overtrading: ' + ', '.join(overtrade_pairs) if overtrade_pairs else '✅ No overtrading detected'}

🔧 *Operational*
  • Errors: `{log_info['error_count']}`
  • Stale data warnings: `{log_info['stale_data_warnings']}`
  • Restarts: `{log_info['restarts']}`

💡 *Hermes Recommendations*
{recommendations}

_Hermes v2 Weekly | {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_"""

    return report


def _build_weekly_recommendations(stats: Dict, log_info: Dict,
                                   filter_fails: Dict, overtrade_pairs: List) -> str:
    """
    Build structured recommendations following required format:
    Recommendation / Evidence / Expected benefit / Risk / Test method / Status
    """
    recs = []

    if stats["profit_factor"] != float("inf") and stats["profit_factor"] < 1.2:
        recs.append(
            "📌 *Tighten exit signal*\n"
            f"  Evidence: Profit factor is `{stats['profit_factor']}` (below 1.2)\n"
            "  Benefit: Fewer losing exits, better P&L\n"
            "  Risk: May hold trades too long, increasing drawdown\n"
            "  Test method: Backtest last 30 days with RSI exit threshold at 40 vs 45\n"
            "  Status: `Paper-test only`"
        )

    if stats["max_losing_streak"] >= 4:
        recs.append(
            "📌 *Review consecutive loss conditions*\n"
            f"  Evidence: Max losing streak was `{stats['max_losing_streak']}`\n"
            "  Benefit: Identify regime conditions causing streak\n"
            "  Risk: Overfitting to one week of data\n"
            "  Test method: Check regime_log.jsonl for shared BTC/pair trend during streak\n"
            "  Status: `Review required`"
        )

    if filter_fails.get("ema_bearish", 0) > filter_fails.get("rsi_gt_68", 0):
        recs.append(
            "📌 *EMA alignment is the most common filter failure*\n"
            f"  Evidence: EMA filter blocked `{filter_fails.get('ema_bearish', 0)}` signals\n"
            "  Benefit: Understanding if EMA 8/21 cross is too strict for current conditions\n"
            "  Risk: Loosening EMA filter may allow counter-trend entries\n"
            "  Test method: Backtest with EMA 5/13 vs 8/21 on SOL and DOGE\n"
            "  Status: `Paper-test only`"
        )

    if log_info["stale_data_warnings"] > 3:
        recs.append(
            "📌 *Check Pi network stability*\n"
            f"  Evidence: `{log_info['stale_data_warnings']}` stale data warnings this week\n"
            "  Benefit: Reduce missed signals and incorrect safety halts\n"
            "  Risk: None — this is infrastructure, not strategy\n"
            "  Test method: Check Pi uptime and Bybit API latency logs\n"
            "  Status: `Review required`"
        )

    if overtrade_pairs:
        recs.append(
            f"📌 *Possible overtrading on {', '.join(overtrade_pairs)}*\n"
            "  Evidence: More than 30 trades/week on one pair\n"
            "  Benefit: Reducing churn may improve net P&L after fees\n"
            "  Risk: May miss genuine signals\n"
            "  Test method: Review per-pair daily trade counts in regime log\n"
            "  Status: `Review required`"
        )

    if not recs:
        recs.append(
            "✅ No critical issues detected this week.\n"
            "  Continue monitoring. Next review: next Sunday."
        )

    return "\n\n".join(recs)


# ===========================================================================
# Report saving
# ===========================================================================

def save_report(report: str, report_type: str):
    """Save report text to reports/ folder."""
    ts = datetime.utcnow().strftime("%Y-%m-%d_%H%M")
    fname = REPORTS_DIR / f"{report_type}_{ts}.txt"
    try:
        fname.write_text(report)
        print(f"[Hermes] Report saved: {fname}")
    except Exception as e:
        print(f"[Hermes] Failed to save report: {e}")


# ===========================================================================
# Scheduled jobs
# ===========================================================================

def run_daily_report():
    print(f"[Hermes] Running daily report at {datetime.utcnow().isoformat()}")
    report = build_daily_report(datetime.utcnow())
    save_report(report, "daily")
    ok = send_telegram(report)
    if not ok:
        print("[Hermes] Daily report Telegram send failed — suppressing new entries not applicable here")


def run_weekly_report():
    print(f"[Hermes] Running weekly report at {datetime.utcnow().isoformat()}")
    report = build_weekly_report(datetime.utcnow())
    save_report(report, "weekly")
    ok = send_telegram(report)
    if not ok:
        print("[Hermes] Weekly report Telegram send failed")


# ===========================================================================
# Main
# ===========================================================================

def main():
    print(f"[Hermes] Agent v2 started at {datetime.utcnow().isoformat()}")
    print(f"[Hermes] DB path: {DB_PATH} — exists: {DB_PATH.exists()}")
    print(f"[Hermes] Reports dir: {REPORTS_DIR}")
    print(f"[Hermes] Pairs monitored: {', '.join(PAIR_SHORT.values())}")

    send_telegram(
        "🤖 *Hermes Agent v2 started*\n"
        "Daily report: `23:55 UTC`\n"
        "Weekly report: `Sunday 22:00 UTC`\n"
        "Mode: read-only analyst\n"
        f"Pairs: `{', '.join(PAIR_SHORT.values())}`"
    )

    # Schedule
    schedule.every().day.at("23:55").do(run_daily_report)
    schedule.every().sunday.at("22:00").do(run_weekly_report)

    # Run immediately if past daily report time (e.g. after a restart)
    now_utc = datetime.utcnow()
    if now_utc.hour == 23 and now_utc.minute >= 55:
        run_daily_report()
    if now_utc.weekday() == 6 and now_utc.hour >= 22:
        run_weekly_report()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
