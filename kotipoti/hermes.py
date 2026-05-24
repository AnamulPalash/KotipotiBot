"""
hermes.py — Performance analyser and auto-tuner for KotipotiBot v2
===================================================================
Runs as a background thread inside the bot container.
Every N hours it reads the trade history and signal log,
finds patterns, and writes tuning suggestions back to the DB.
The bot picks up parameter changes on the next candle.

Analysis performed:
  - Win rate by session (Asia / London / US)
  - Win rate by pair
  - Win rate by entry tag (bb_short, bb_long, vwap_short, vwap_long)
  - Win rate by BTC regime
  - Signal accuracy (fired signals that became profitable vs not)
  - Average holding time winners vs losers
  - Best/worst ATR range for entries

Auto-tuning bounds (Hermes never goes outside these):
  - rsi_short_entry:   60 – 75
  - rsi_long_entry:    25 – 40
  - volume_multiplier: 1.0 – 2.0
  - atr_min_pct:       0.2 – 0.8
  - atr_max_pct:       2.0 – 5.0
"""

import threading
import time
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

import db

log = logging.getLogger("kotipoti.hermes")

HERMES_INTERVAL_H = 6     # run analysis every 6 hours
MIN_TRADES_FOR_TUNING = 10 # don't tune until we have enough data

# Safe tuning bounds — Hermes never exceeds these
PARAM_BOUNDS = {
    "rsi_short_entry":   (60.0, 75.0),
    "rsi_long_entry":    (25.0, 40.0),
    "volume_multiplier": (1.0,  2.0),
    "atr_min_pct":       (0.2,  0.8),
    "atr_max_pct":       (2.0,  5.0),
    "vwap_dev_min":      (0.3,  2.0),
}

STEP = {
    "rsi_short_entry":   1.0,
    "rsi_long_entry":    1.0,
    "volume_multiplier": 0.1,
    "atr_min_pct":       0.05,
    "atr_max_pct":       0.25,
    "vwap_dev_min":      0.1,
}


def _win_rate(trades: List[Dict]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("profit_usdt") or 0) > 0)
    return wins / len(trades) * 100


def _avg_profit(trades: List[Dict]) -> float:
    if not trades:
        return 0.0
    return sum((t.get("profit_usdt") or 0) for t in trades) / len(trades)


def analyse() -> Dict:
    """Run full performance analysis. Returns findings dict."""
    trades = db.get_all_closed_trades()
    findings = {
        "ts":           datetime.now(timezone.utc).isoformat(),
        "total_trades": len(trades),
        "overall_win_rate": _win_rate(trades),
        "total_profit_usdt": sum((t.get("profit_usdt") or 0) for t in trades),
    }

    if not trades:
        return findings

    # ── By session ────────────────────────────────────────────────────────────
    by_session: Dict[str, List] = defaultdict(list)
    for t in trades:
        s = t.get("session") or "unknown"
        by_session[s].append(t)

    findings["by_session"] = {
        s: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for s, ts in by_session.items()
    }

    # ── By pair ───────────────────────────────────────────────────────────────
    by_pair: Dict[str, List] = defaultdict(list)
    for t in trades:
        by_pair[t["pair"]].append(t)

    findings["by_pair"] = {
        pair: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for pair, ts in by_pair.items()
    }

    # ── By entry tag ──────────────────────────────────────────────────────────
    by_tag: Dict[str, List] = defaultdict(list)
    for t in trades:
        tag = t.get("entry_tag") or "unknown"
        by_tag[tag].append(t)

    findings["by_entry_tag"] = {
        tag: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for tag, ts in by_tag.items()
    }

    # ── By BTC regime ─────────────────────────────────────────────────────────
    by_regime: Dict[str, List] = defaultdict(list)
    for t in trades:
        r = t.get("btc_regime") or "unknown"
        by_regime[r].append(t)

    findings["by_btc_regime"] = {
        r: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for r, ts in by_regime.items()
    }

    # ── Holding time: winners vs losers ───────────────────────────────────────
    hold_times_win  = []
    hold_times_lose = []
    for t in trades:
        try:
            entry = datetime.fromisoformat(t["entry_time"])
            exit_ = datetime.fromisoformat(t["exit_time"])
            mins  = (exit_ - entry).total_seconds() / 60
            if (t.get("profit_usdt") or 0) > 0:
                hold_times_win.append(mins)
            else:
                hold_times_lose.append(mins)
        except Exception:
            pass

    findings["avg_hold_min_winners"] = round(
        sum(hold_times_win) / len(hold_times_win), 1) if hold_times_win else None
    findings["avg_hold_min_losers"]  = round(
        sum(hold_times_lose) / len(hold_times_lose), 1) if hold_times_lose else None

    # ── ATR analysis: best entry range ───────────────────────────────────────
    atr_buckets: Dict[str, List] = defaultdict(list)
    for t in trades:
        atr = t.get("atr_at_entry")
        if atr is None:
            continue
        if atr < 0.5:
            bucket = "very_low"
        elif atr < 1.0:
            bucket = "low"
        elif atr < 2.0:
            bucket = "normal"
        elif atr < 3.0:
            bucket = "high"
        else:
            bucket = "very_high"
        atr_buckets[bucket].append(t)

    findings["by_atr_bucket"] = {
        b: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for b, ts in atr_buckets.items()
    }

    # ── Worst sessions (candidates for blocking) ──────────────────────────────
    bad_sessions = [
        s for s, stats in findings["by_session"].items()
        if stats["count"] >= 5 and stats["win_rate"] < 35
    ]
    findings["bad_sessions"] = bad_sessions

    # ── Worst pairs ───────────────────────────────────────────────────────────
    bad_pairs = [
        p for p, stats in findings["by_pair"].items()
        if stats["count"] >= 5 and stats["win_rate"] < 30
    ]
    findings["bad_pairs"] = bad_pairs

    return findings


def suggest_tuning(findings: Dict) -> Dict:
    """
    Given analysis findings, suggest parameter adjustments.
    Only suggests changes within PARAM_BOUNDS.
    Returns dict of {param_key: new_value}.
    """
    suggestions = {}

    if findings["total_trades"] < MIN_TRADES_FOR_TUNING:
        return suggestions

    overall_wr = findings.get("overall_win_rate", 50)

    # ── RSI threshold tuning ──────────────────────────────────────────────────
    # If win rate is low and bb_short tag is underperforming → tighten RSI
    bb_short_stats = findings.get("by_entry_tag", {}).get("bb_short", {})
    if bb_short_stats.get("count", 0) >= 5:
        wr = bb_short_stats["win_rate"]
        cur_rsi_short = db.get_param_float("rsi_short_entry", 68)
        lo, hi = PARAM_BOUNDS["rsi_short_entry"]
        step   = STEP["rsi_short_entry"]
        if wr < 40 and cur_rsi_short < hi:
            new_val = min(cur_rsi_short + step, hi)
            suggestions["rsi_short_entry"] = new_val
            log.info(f"[Hermes] bb_short win rate {wr:.1f}% → tighten rsi_short_entry: "
                     f"{cur_rsi_short} → {new_val}")
        elif wr > 65 and cur_rsi_short > lo:
            new_val = max(cur_rsi_short - step, lo)
            suggestions["rsi_short_entry"] = new_val
            log.info(f"[Hermes] bb_short win rate {wr:.1f}% → relax rsi_short_entry: "
                     f"{cur_rsi_short} → {new_val}")

    bb_long_stats = findings.get("by_entry_tag", {}).get("bb_long", {})
    if bb_long_stats.get("count", 0) >= 5:
        wr = bb_long_stats["win_rate"]
        cur_rsi_long = db.get_param_float("rsi_long_entry", 32)
        lo, hi = PARAM_BOUNDS["rsi_long_entry"]
        step   = STEP["rsi_long_entry"]
        if wr < 40 and cur_rsi_long > lo:
            new_val = max(cur_rsi_long - step, lo)
            suggestions["rsi_long_entry"] = new_val
            log.info(f"[Hermes] bb_long win rate {wr:.1f}% → tighten rsi_long_entry: "
                     f"{cur_rsi_long} → {new_val}")
        elif wr > 65 and cur_rsi_long < hi:
            new_val = min(cur_rsi_long + step, hi)
            suggestions["rsi_long_entry"] = new_val
            log.info(f"[Hermes] bb_long win rate {wr:.1f}% → relax rsi_long_entry: "
                     f"{cur_rsi_long} → {new_val}")

    # ── Volume multiplier tuning ──────────────────────────────────────────────
    # If too many losing trades → raise volume bar
    cur_vol = db.get_param_float("volume_multiplier", 1.2)
    lo, hi  = PARAM_BOUNDS["volume_multiplier"]
    step    = STEP["volume_multiplier"]
    if overall_wr < 40 and cur_vol < hi:
        new_val = min(cur_vol + step, hi)
        suggestions["volume_multiplier"] = new_val
        log.info(f"[Hermes] Overall WR {overall_wr:.1f}% → raise volume_multiplier: "
                 f"{cur_vol} → {new_val}")

    # ── ATR tuning ────────────────────────────────────────────────────────────
    atr_buckets = findings.get("by_atr_bucket", {})
    # If "very_low" ATR trades are losing → raise atr_min
    very_low = atr_buckets.get("very_low", {})
    if very_low.get("count", 0) >= 3 and very_low.get("win_rate", 50) < 35:
        cur = db.get_param_float("atr_min_pct", 0.3)
        lo, hi = PARAM_BOUNDS["atr_min_pct"]
        if cur < hi:
            new_val = min(cur + STEP["atr_min_pct"], hi)
            suggestions["atr_min_pct"] = new_val
            log.info(f"[Hermes] very_low ATR win rate {very_low['win_rate']:.1f}% "
                     f"→ raise atr_min_pct: {cur} → {new_val}")

    # If "very_high" ATR trades are losing → lower atr_max
    very_high = atr_buckets.get("very_high", {})
    if very_high.get("count", 0) >= 3 and very_high.get("win_rate", 50) < 35:
        cur = db.get_param_float("atr_max_pct", 3.0)
        lo, hi = PARAM_BOUNDS["atr_max_pct"]
        if cur > lo:
            new_val = max(cur - STEP["atr_max_pct"], lo)
            suggestions["atr_max_pct"] = new_val
            log.info(f"[Hermes] very_high ATR win rate {very_high['win_rate']:.1f}% "
                     f"→ lower atr_max_pct: {cur} → {new_val}")

    # ── Session blocking ──────────────────────────────────────────────────────
    bad_sessions = findings.get("bad_sessions", [])
    if bad_sessions:
        current_blocked = json.loads(db.get_param("blocked_sessions", "[]"))
        new_blocked = list(set(current_blocked + bad_sessions))
        if new_blocked != current_blocked:
            suggestions["blocked_sessions"] = json.dumps(new_blocked)
            log.info(f"[Hermes] Suggesting block sessions: {bad_sessions}")

    return suggestions


def apply_suggestions(suggestions: Dict):
    """Write parameter suggestions to DB."""
    for key, value in suggestions.items():
        db.set_param(key, value, updated_by="hermes")
        log.info(f"[Hermes] Applied: {key} = {value}")


def run_once():
    """Run one full Hermes analysis cycle."""
    log.info("[Hermes] Starting analysis cycle...")
    try:
        findings    = analyse()
        suggestions = suggest_tuning(findings)

        db.log_hermes(findings, suggestions)

        log.info(f"[Hermes] Trades analysed: {findings['total_trades']} | "
                 f"Win rate: {findings['overall_win_rate']:.1f}% | "
                 f"Total P&L: {findings['total_profit_usdt']:+.2f} USDT")

        if findings.get("by_session"):
            for s, stats in findings["by_session"].items():
                log.info(f"[Hermes]   {s}: {stats['count']} trades | "
                         f"WR={stats['win_rate']:.1f}% | "
                         f"avg={stats['avg_profit']:+.2f} USDT")

        if suggestions:
            log.info(f"[Hermes] Applying {len(suggestions)} parameter update(s)...")
            apply_suggestions(suggestions)
        else:
            log.info("[Hermes] No parameter changes needed.")

    except Exception as e:
        log.error(f"[Hermes] Analysis failed: {e}", exc_info=True)


def start_background_thread():
    """Start Hermes as a daemon thread that runs every HERMES_INTERVAL_H hours."""
    def _loop():
        # Wait a bit before first run to let the bot accumulate some trades
        time.sleep(300)
        while True:
            run_once()
            time.sleep(HERMES_INTERVAL_H * 3600)

    t = threading.Thread(target=_loop, daemon=True, name="hermes")
    t.start()
    log.info(f"[Hermes] Background thread started (interval: {HERMES_INTERVAL_H}h)")
    return t


if __name__ == "__main__":
    # Can also be run standalone for a manual analysis
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    db.init_db()
    run_once()
