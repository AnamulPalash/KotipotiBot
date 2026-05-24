"""
db.py — SQLite schema and helper functions for KotipotiBot v2
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

DB_PATH = os.environ.get("DB_PATH", "/data/kotipoti.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pair        TEXT NOT NULL,
            side        TEXT NOT NULL,          -- 'long' or 'short'
            entry_price REAL NOT NULL,
            exit_price  REAL,
            amount      REAL NOT NULL,          -- base currency amount
            stake_usdt  REAL NOT NULL,
            leverage    INTEGER NOT NULL DEFAULT 5,
            entry_time  TEXT NOT NULL,
            exit_time   TEXT,
            exit_reason TEXT,                   -- 'stoploss','trailing','roi','signal','manual'
            profit_usdt REAL,
            profit_pct  REAL,
            entry_tag   TEXT,
            exit_tag    TEXT,
            session     TEXT,                   -- 'Asia','London','US'
            btc_regime  TEXT,                   -- 'bull','bear','range','unknown'
            atr_at_entry REAL,
            is_open     INTEGER NOT NULL DEFAULT 1,
            dry_run     INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS signals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            pair        TEXT NOT NULL,
            direction   TEXT NOT NULL,          -- 'long' or 'short'
            fired       INTEGER NOT NULL,       -- 1=entered trade, 0=skipped
            skip_reason TEXT,
            price       REAL,
            rsi         REAL,
            bb_pct      REAL,                   -- how far outside BB (%)
            ema_gap     REAL,                   -- ema_fast - ema_slow / price
            atr_pct     REAL,                   -- ATR as % of price
            vwap_dev    REAL,                   -- % deviation from VWAP
            volume_ratio REAL,                  -- volume / volume_mean
            btc_regime  TEXT,
            session     TEXT,
            context_json TEXT                   -- full indicator snapshot as JSON
        );

        CREATE TABLE IF NOT EXISTS params (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            updated_by  TEXT NOT NULL DEFAULT 'system'  -- 'system' or 'hermes'
        );

        CREATE TABLE IF NOT EXISTS hermes_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            analysis    TEXT NOT NULL,          -- JSON: findings
            suggestions TEXT NOT NULL,          -- JSON: param changes suggested
            applied     INTEGER NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_trades_pair    ON trades(pair);
        CREATE INDEX IF NOT EXISTS idx_trades_open    ON trades(is_open);
        CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
        CREATE INDEX IF NOT EXISTS idx_signals_pair   ON signals(pair);
        """)

    # Seed default params if not present
    defaults = {
        "rsi_short_entry":    "68",
        "rsi_long_entry":     "32",
        "rsi_exit_short":     "45",
        "rsi_exit_long":      "55",
        "bb_period":          "20",
        "bb_std":             "2.0",
        "ema_fast":           "8",
        "ema_slow":           "21",
        "volume_multiplier":  "1.2",
        "atr_period":         "14",
        "atr_min_pct":        "0.3",   # block if ATR% < this (too quiet)
        "atr_max_pct":        "3.0",   # block if ATR% > this (too wild)
        "vwap_dev_min":       "0.5",   # min % deviation for VWAP signal
        "stoploss_pct":       "2.5",
        "trailing_pct":       "1.0",
        "trailing_offset":    "1.5",
        "leverage":           "5",
        "max_open_trades":    "3",
        "stake_usdt":         "200",
        "daily_loss_limit":   "10.0",  # % of wallet
        "max_consec_losses":  "4",
        "pair_cooldown_min":  "15",
        "blocked_sessions":   "[]",    # JSON list e.g. '["Asia"]'
    }
    with _conn() as conn:
        now = _now()
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO params(key, value, updated_at) VALUES (?,?,?)",
                (k, v, now)
            )


# ── Param helpers ─────────────────────────────────────────────────────────────

def get_param(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM params WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"]

def get_param_float(key: str, default: float = 0.0) -> float:
    return float(get_param(key, str(default)))

def get_param_int(key: str, default: int = 0) -> int:
    return int(get_param(key, str(default)))

def set_param(key: str, value, updated_by: str = "system"):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO params(key, value, updated_at, updated_by) VALUES (?,?,?,?)",
            (key, str(value), _now(), updated_by)
        )

def get_all_params() -> Dict[str, str]:
    with _conn() as conn:
        rows = conn.execute("SELECT key, value FROM params").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Trade helpers ─────────────────────────────────────────────────────────────

def open_trade(pair, side, entry_price, amount, stake_usdt, leverage,
               entry_tag, session, btc_regime, atr_at_entry, dry_run=True) -> int:
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (pair, side, entry_price, amount, stake_usdt, leverage,
               entry_time, entry_tag, session, btc_regime, atr_at_entry, is_open, dry_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?)
        """, (pair, side, entry_price, amount, stake_usdt, leverage,
              _now(), entry_tag, session, btc_regime, atr_at_entry, int(dry_run)))
        return cur.lastrowid

def close_trade(trade_id: int, exit_price: float, exit_reason: str, exit_tag: str = ""):
    with _conn() as conn:
        row = conn.execute(
            "SELECT entry_price, amount, stake_usdt, leverage, side FROM trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        if not row:
            return
        entry_price = row["entry_price"]
        amount      = row["amount"]
        stake_usdt  = row["stake_usdt"]
        leverage    = row["leverage"]
        side        = row["side"]

        if side == "long":
            profit_pct  = (exit_price - entry_price) / entry_price * 100 * leverage
        else:
            profit_pct  = (entry_price - exit_price) / entry_price * 100 * leverage
        profit_usdt = stake_usdt * profit_pct / 100

        conn.execute("""
            UPDATE trades SET
              exit_price=?, exit_time=?, exit_reason=?, exit_tag=?,
              profit_usdt=?, profit_pct=?, is_open=0
            WHERE id=?
        """, (exit_price, _now(), exit_reason, exit_tag,
              round(profit_usdt, 4), round(profit_pct, 4), trade_id))

def get_open_trades() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE is_open=1 ORDER BY entry_time"
        ).fetchall()
    return [dict(r) for r in rows]

def get_recent_trades(limit: int = 50) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE is_open=0 ORDER BY exit_time DESC LIMIT ?",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_all_closed_trades() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE is_open=0 ORDER BY exit_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Signal helpers ────────────────────────────────────────────────────────────

def log_signal(pair, direction, fired, skip_reason=None, price=None,
               rsi=None, bb_pct=None, ema_gap=None, atr_pct=None,
               vwap_dev=None, volume_ratio=None, btc_regime=None,
               session=None, context: dict = None):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO signals
              (ts, pair, direction, fired, skip_reason, price, rsi, bb_pct,
               ema_gap, atr_pct, vwap_dev, volume_ratio, btc_regime, session, context_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (_now(), pair, direction, int(fired), skip_reason, price,
              rsi, bb_pct, ema_gap, atr_pct, vwap_dev, volume_ratio,
              btc_regime, session,
              json.dumps(context) if context else None))

def get_recent_signals(limit: int = 100) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Hermes log ────────────────────────────────────────────────────────────────

def log_hermes(analysis: dict, suggestions: dict):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO hermes_log(ts, analysis, suggestions, applied) VALUES (?,?,?,0)",
            (_now(), json.dumps(analysis), json.dumps(suggestions))
        )

def get_hermes_logs(limit: int = 10) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM hermes_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Profit summary ────────────────────────────────────────────────────────────

def get_profit_summary() -> Dict:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT profit_usdt, profit_pct, side FROM trades WHERE is_open=0"
        ).fetchall()
    if not rows:
        return {"trade_count": 0, "total_profit_usdt": 0, "win_rate": 0,
                "avg_profit_pct": 0, "best_trade": 0, "worst_trade": 0}
    profits = [r["profit_usdt"] for r in rows]
    pcts    = [r["profit_pct"]  for r in rows]
    wins    = [p for p in profits if p > 0]
    return {
        "trade_count":       len(profits),
        "total_profit_usdt": round(sum(profits), 4),
        "win_rate":          round(len(wins) / len(profits) * 100, 1),
        "avg_profit_pct":    round(sum(pcts) / len(pcts), 3),
        "best_trade":        round(max(profits), 4),
        "worst_trade":       round(min(profits), 4),
    }


# ── Util ──────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
