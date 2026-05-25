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
  - RSI at entry buckets (short and long separately)
  - Volume ratio at entry buckets
  - VWAP deviation at entry buckets

Auto-tuning bounds (Hermes never goes outside these):
  - rsi_short_entry:   60 – 75
  - rsi_long_entry:    25 – 40
  - volume_multiplier: 1.0 – 2.0
  - atr_min_pct:       0.2 – 0.8
  - atr_max_pct:       2.0 – 5.0
  - vwap_dev_min:      0.3 – 2.0
"""

import threading
import time
import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

import db
import telegram as tg

log = logging.getLogger("kotipoti.hermes")

HERMES_INTERVAL_H = 6      # run analysis every 6 hours
MIN_TRADES_FOR_TUNING = 10  # don't tune until we have enough data
MIN_BUCKET_TRADES = 4       # minimum trades in a bucket to trust its stats

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

# Representative mid-point values for each bucket — used to map best bucket → param value
RSI_SHORT_BUCKET_VALUES = {
    "<65":   63.0,
    "65-70": 67.5,
    "70-75": 72.5,
    "75+":   76.0,
}
RSI_LONG_BUCKET_VALUES = {
    ">35":   37.0,
    "30-35": 32.5,
    "25-30": 27.5,
    "<25":   24.0,
}
VOL_BUCKET_VALUES = {
    "1.0-1.5x": 1.2,
    "1.5-2.0x": 1.7,
    "2.0-3.0x": 2.4,
    "3.0x+":    3.2,
}
VWAP_BUCKET_VALUES = {
    "<0.5%":    0.3,
    "0.5-1.0%": 0.75,
    "1.0-1.5%": 1.25,
    "1.5-2.0%": 1.75,
    "2.0%+":    2.3,
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


def _bucket_summary(buckets: Dict[str, List]) -> Dict:
    return {
        b: {
            "count":      len(ts),
            "win_rate":   round(_win_rate(ts), 1),
            "avg_profit": round(_avg_profit(ts), 3),
        }
        for b, ts in buckets.items()
    }


def _best_bucket(bucket_findings: Dict, bucket_values: Dict,
                 min_trades: int = MIN_BUCKET_TRADES) -> Optional[float]:
    """Return the representative value of the bucket with the highest win rate.
    Only considers buckets with at least min_trades trades. Returns None if no
    bucket qualifies."""
    best_wr  = -1.0
    best_val = None
    for b, stats in bucket_findings.items():
        if stats["count"] >= min_trades and stats["win_rate"] > best_wr:
            best_wr  = stats["win_rate"]
            best_val = bucket_values.get(b)
    return best_val


def analyse() -> Dict:
    """Run full performance analysis. Returns findings dict."""
    trades = db.get_all_closed_trades()
    findings = {
        "ts":                datetime.now(timezone.utc).isoformat(),
        "total_trades":      len(trades),
        "overall_win_rate":  _win_rate(trades),
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

    # ── ATR analysis: best entry range ────────────────────────────────────────
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

    findings["by_atr_bucket"] = _bucket_summary(atr_buckets)

    # ── RSI at entry buckets ──────────────────────────────────────────────────
    rsi_short_buckets: Dict[str, List] = defaultdict(list)
    rsi_long_buckets:  Dict[str, List] = defaultdict(list)
    for t in trades:
        rsi  = t.get("rsi_at_entry")
        side = t.get("side", "")
        if rsi is None:
            continue
        if side == "short":
            if rsi < 65:        b = "<65"
            elif rsi < 70:      b = "65-70"
            elif rsi < 75:      b = "70-75"
            else:               b = "75+"
            rsi_short_buckets[b].append(t)
        elif side == "long":
            if rsi > 35:        b = ">35"
            elif rsi > 30:      b = "30-35"
            elif rsi > 25:      b = "25-30"
            else:               b = "<25"
            rsi_long_buckets[b].append(t)

    findings["by_rsi_short_bucket"] = _bucket_summary(rsi_short_buckets)
    findings["by_rsi_long_bucket"]  = _bucket_summary(rsi_long_buckets)

    # ── Volume ratio at entry buckets ─────────────────────────────────────────
    vol_buckets: Dict[str, List] = defaultdict(list)
    for t in trades:
        vr = t.get("vol_ratio_at_entry")
        if vr is None:
            continue
        if vr < 1.5:        b = "1.0-1.5x"
        elif vr < 2.0:      b = "1.5-2.0x"
        elif vr < 3.0:      b = "2.0-3.0x"
        else:               b = "3.0x+"
        vol_buckets[b].append(t)

    findings["by_vol_bucket"] = _bucket_summary(vol_buckets)

    # ── VWAP deviation at entry buckets ───────────────────────────────────────
    vwap_buckets: Dict[str, List] = defaultdict(list)
    for t in trades:
        vd = t.get("vwap_dev_at_entry")
        if vd is None:
            continue
        vd_abs = abs(vd)
        if vd_abs < 0.5:        b = "<0.5%"
        elif vd_abs < 1.0:      b = "0.5-1.0%"
        elif vd_abs < 1.5:      b = "1.0-1.5%"
        elif vd_abs < 2.0:      b = "1.5-2.0%"
        else:                   b = "2.0%+"
        vwap_buckets[b].append(t)

    findings["by_vwap_bucket"] = _bucket_summary(vwap_buckets)

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
    Uses bucket optima for direct threshold setting where data is sufficient,
    falls back to ±step nudge based on tag win rates.
    Only suggests changes within PARAM_BOUNDS.
    Returns dict of {param_key: new_value}.
    """
    suggestions = {}

    if findings["total_trades"] < MIN_TRADES_FOR_TUNING:
        return suggestions

    overall_wr = findings.get("overall_win_rate", 50)

    # ── RSI short entry threshold ─────────────────────────────────────────────
    rsi_short_buckets = findings.get("by_rsi_short_bucket", {})
    if rsi_short_buckets:
        optimal = _best_bucket(rsi_short_buckets, RSI_SHORT_BUCKET_VALUES)
        if optimal is not None:
            cur = db.get_param_float("rsi_short_entry", 68)
            lo, hi = PARAM_BOUNDS["rsi_short_entry"]
            clamped = max(lo, min(hi, optimal))
            if abs(clamped - cur) >= STEP["rsi_short_entry"]:
                suggestions["rsi_short_entry"] = round(clamped, 1)
                log.info(f"[Hermes] RSI short bucket optimal {optimal} → "
                         f"rsi_short_entry: {cur} → {round(clamped, 1)}")
    else:
        # Fallback: nudge based on bb_short tag win rate
        bb_short_stats = findings.get("by_entry_tag", {}).get("bb_short", {})
        if bb_short_stats.get("count", 0) >= 5:
            wr = bb_short_stats["win_rate"]
            cur = db.get_param_float("rsi_short_entry", 68)
            lo, hi = PARAM_BOUNDS["rsi_short_entry"]
            step   = STEP["rsi_short_entry"]
            if wr < 40 and cur < hi:
                new_val = min(cur + step, hi)
                suggestions["rsi_short_entry"] = new_val
                log.info(f"[Hermes] bb_short WR {wr:.1f}% → tighten rsi_short_entry: "
                         f"{cur} → {new_val}")
            elif wr > 65 and cur > lo:
                new_val = max(cur - step, lo)
                suggestions["rsi_short_entry"] = new_val
                log.info(f"[Hermes] bb_short WR {wr:.1f}% → relax rsi_short_entry: "
                         f"{cur} → {new_val}")

    # ── RSI long entry threshold ──────────────────────────────────────────────
    rsi_long_buckets = findings.get("by_rsi_long_bucket", {})
    if rsi_long_buckets:
        optimal = _best_bucket(rsi_long_buckets, RSI_LONG_BUCKET_VALUES)
        if optimal is not None:
            cur = db.get_param_float("rsi_long_entry", 32)
            lo, hi = PARAM_BOUNDS["rsi_long_entry"]
            clamped = max(lo, min(hi, optimal))
            if abs(clamped - cur) >= STEP["rsi_long_entry"]:
                suggestions["rsi_long_entry"] = round(clamped, 1)
                log.info(f"[Hermes] RSI long bucket optimal {optimal} → "
                         f"rsi_long_entry: {cur} → {round(clamped, 1)}")
    else:
        # Fallback: nudge based on bb_long tag win rate
        bb_long_stats = findings.get("by_entry_tag", {}).get("bb_long", {})
        if bb_long_stats.get("count", 0) >= 5:
            wr = bb_long_stats["win_rate"]
            cur = db.get_param_float("rsi_long_entry", 32)
            lo, hi = PARAM_BOUNDS["rsi_long_entry"]
            step   = STEP["rsi_long_entry"]
            if wr < 40 and cur > lo:
                new_val = max(cur - step, lo)
                suggestions["rsi_long_entry"] = new_val
                log.info(f"[Hermes] bb_long WR {wr:.1f}% → tighten rsi_long_entry: "
                         f"{cur} → {new_val}")
            elif wr > 65 and cur < hi:
                new_val = min(cur + step, hi)
                suggestions["rsi_long_entry"] = new_val
                log.info(f"[Hermes] bb_long WR {wr:.1f}% → relax rsi_long_entry: "
                         f"{cur} → {new_val}")

    # ── Volume multiplier ─────────────────────────────────────────────────────
    vol_buckets = findings.get("by_vol_bucket", {})
    if vol_buckets:
        optimal = _best_bucket(vol_buckets, VOL_BUCKET_VALUES)
        if optimal is not None:
            cur = db.get_param_float("volume_multiplier", 1.2)
            lo, hi = PARAM_BOUNDS["volume_multiplier"]
            clamped = max(lo, min(hi, optimal))
            if abs(clamped - cur) >= STEP["volume_multiplier"]:
                suggestions["volume_multiplier"] = round(clamped, 2)
                log.info(f"[Hermes] Vol bucket optimal {optimal} → "
                         f"volume_multiplier: {cur} → {round(clamped, 2)}")
    else:
        # Fallback: raise bar if overall WR is poor
        cur = db.get_param_float("volume_multiplier", 1.2)
        lo, hi = PARAM_BOUNDS["volume_multiplier"]
        if overall_wr < 40 and cur < hi:
            new_val = min(cur + STEP["volume_multiplier"], hi)
            suggestions["volume_multiplier"] = new_val
            log.info(f"[Hermes] Overall WR {overall_wr:.1f}% → raise volume_multiplier: "
                     f"{cur} → {new_val}")

    # ── VWAP dev min ──────────────────────────────────────────────────────────
    vwap_buckets = findings.get("by_vwap_bucket", {})
    if vwap_buckets:
        optimal = _best_bucket(vwap_buckets, VWAP_BUCKET_VALUES)
        if optimal is not None:
            cur = db.get_param_float("vwap_dev_min", 0.5)
            lo, hi = PARAM_BOUNDS["vwap_dev_min"]
            clamped = max(lo, min(hi, optimal))
            if abs(clamped - cur) >= STEP["vwap_dev_min"]:
                suggestions["vwap_dev_min"] = round(clamped, 2)
                log.info(f"[Hermes] VWAP bucket optimal {optimal} → "
                         f"vwap_dev_min: {cur} → {round(clamped, 2)}")

    # ── ATR tuning (nudge-based — no clear representative value per bucket) ───
    atr_buckets = findings.get("by_atr_bucket", {})
    # If "very_low" ATR trades are losing → raise atr_min
    very_low = atr_buckets.get("very_low", {})
    if very_low.get("count", 0) >= 3 and very_low.get("win_rate", 50) < 35:
        cur = db.get_param_float("atr_min_pct", 0.3)
        lo, hi = PARAM_BOUNDS["atr_min_pct"]
        if cur < hi:
            new_val = min(cur + STEP["atr_min_pct"], hi)
            suggestions["atr_min_pct"] = new_val
            log.info(f"[Hermes] very_low ATR WR {very_low['win_rate']:.1f}% "
                     f"→ raise atr_min_pct: {cur} → {new_val}")

    # If "very_high" ATR trades are losing → lower atr_max
    very_high = atr_buckets.get("very_high", {})
    if very_high.get("count", 0) >= 3 and very_high.get("win_rate", 50) < 35:
        cur = db.get_param_float("atr_max_pct", 3.0)
        lo, hi = PARAM_BOUNDS["atr_max_pct"]
        if cur > lo:
            new_val = max(cur - STEP["atr_max_pct"], lo)
            suggestions["atr_max_pct"] = new_val
            log.info(f"[Hermes] very_high ATR WR {very_high['win_rate']:.1f}% "
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
        db.set_validated_param(key, value, updated_by="hermes")
        log.info(f"[Hermes] Applied: {key} = {value}")


def run_once():
    """Run one full Hermes analysis cycle."""
    log.info("[Hermes] Starting analysis cycle...")
    try:
        findings    = analyse()
        suggestions = suggest_tuning(findings)

        log.info(f"[Hermes] Trades analysed: {findings['total_trades']} | "
                 f"Win rate: {findings['overall_win_rate']:.1f}% | "
                 f"Total P&L: {findings['total_profit_usdt']:+.2f} USDT")

        if findings.get("by_session"):
            for s, stats in findings["by_session"].items():
                log.info(f"[Hermes]   session={s}: {stats['count']} trades | "
                         f"WR={stats['win_rate']:.1f}% | "
                         f"avg={stats['avg_profit']:+.2f} USDT")

        # Log best bucket per indicator
        for label, bucket_key, bucket_values in [
            ("RSI short", "by_rsi_short_bucket", RSI_SHORT_BUCKET_VALUES),
            ("RSI long",  "by_rsi_long_bucket",  RSI_LONG_BUCKET_VALUES),
            ("Vol ratio", "by_vol_bucket",        VOL_BUCKET_VALUES),
            ("VWAP dev",  "by_vwap_bucket",       VWAP_BUCKET_VALUES),
        ]:
            bkt = findings.get(bucket_key, {})
            if bkt:
                best = max(
                    ((b, s) for b, s in bkt.items() if s["count"] >= MIN_BUCKET_TRADES),
                    key=lambda x: x[1]["win_rate"],
                    default=None,
                )
                if best:
                    log.info(f"[Hermes]   {label} best bucket: '{best[0]}' "
                             f"WR={best[1]['win_rate']:.1f}% ({best[1]['count']} trades)")

        auto_apply = db.get_param("hermes_auto_apply", "false").lower() == "true"
        db.log_hermes(findings, suggestions, applied=bool(suggestions and auto_apply))

        if suggestions and auto_apply:
            log.info(f"[Hermes] Applying {len(suggestions)} parameter update(s)...")
            apply_suggestions(suggestions)
            tg.hermes_tuned(suggestions)
        elif suggestions:
            log.info(f"[Hermes] {len(suggestions)} suggestion(s) recorded for review; "
                     "auto-apply disabled.")
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
