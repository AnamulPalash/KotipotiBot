"""
db.py — SQLite schema and helper functions for KotipotiBot v2
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

DB_PATH = os.environ.get("DB_PATH", "/data/kotipoti.db")

DEFAULT_PAIRS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
    "XRP/USDT:USDT",
]
ALLOWED_PAIRS = set(DEFAULT_PAIRS)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            pair            TEXT NOT NULL,
            side            TEXT NOT NULL,          -- 'long' or 'short'
            entry_price     REAL NOT NULL,
            exit_price      REAL,
            amount          REAL NOT NULL,          -- base currency amount
            stake_usdt      REAL NOT NULL,
            leverage        INTEGER NOT NULL DEFAULT 5,
            entry_time      TEXT NOT NULL,
            exit_time       TEXT,
            exit_reason     TEXT,                   -- 'stoploss','trailing','roi','signal','manual'
            profit_usdt     REAL,
            profit_pct      REAL,
            gross_profit_usdt REAL,
            estimated_fee_usdt REAL,
            estimated_slippage_usdt REAL,
            entry_tag       TEXT,
            exit_tag        TEXT,
            session         TEXT,                   -- 'Asia','London','US'
            btc_regime      TEXT,                   -- 'bull','bear','range','unknown'
            atr_at_entry    REAL,
            rsi_at_entry    REAL,                   -- RSI value at entry
            vwap_dev_at_entry REAL,                 -- VWAP deviation % at entry
            vol_ratio_at_entry REAL,                -- volume ratio at entry
            bb_pct_at_entry REAL,                   -- how far outside BB at entry (%)
            pair_regime     TEXT,                   -- pair's own 1h regime at entry
            peak_price      REAL,                   -- highest seen while open
            trough_price    REAL,                   -- lowest seen while open
            exchange_order_id TEXT,
            stop_order_id   TEXT,
            bot_variant     TEXT,
            is_open         INTEGER NOT NULL DEFAULT 1,
            dry_run         INTEGER NOT NULL DEFAULT 1
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

        CREATE TABLE IF NOT EXISTS decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            candle_ts   TEXT,
            pair        TEXT NOT NULL,
            price       REAL,
            rsi         REAL,
            atr_pct     REAL,
            vwap_dev    REAL,
            volume_ratio REAL,
            bb_position TEXT,
            ema_trend   TEXT,
            btc_1h_regime TEXT,
            pair_15m_regime TEXT,
            pair_1h_regime TEXT,
            pair_4h_regime TEXT,
            signal_type TEXT,
            fired       INTEGER NOT NULL DEFAULT 0,
            skip_reason TEXT,
            params_snapshot TEXT,
            context_json TEXT,
            future_return_3 REAL,
            future_return_6 REAL,
            future_return_12 REAL,
            future_return_24 REAL,
            max_favorable_excursion REAL,
            max_adverse_excursion REAL,
            bot_variant TEXT
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

        CREATE TABLE IF NOT EXISTS commands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT NOT NULL,
            command     TEXT NOT NULL,          -- 'stop','pause','resume','force_close'
            payload     TEXT,                   -- JSON: e.g. {"trade_id": 5}
            status      TEXT NOT NULL DEFAULT 'pending',  -- 'pending','done','error'
            result      TEXT                    -- outcome message
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            key         TEXT PRIMARY KEY,
            value       TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_pair    ON trades(pair);
        CREATE INDEX IF NOT EXISTS idx_trades_open    ON trades(is_open);
        CREATE INDEX IF NOT EXISTS idx_signals_ts     ON signals(ts);
        CREATE INDEX IF NOT EXISTS idx_signals_pair   ON signals(pair);
        CREATE INDEX IF NOT EXISTS idx_decisions_pair_ts ON decisions(pair, candle_ts);
        """)

    # ── Migrate existing DB: add new columns if they don't exist ─────────────
    new_columns = [
        ("rsi_at_entry",       "REAL"),
        ("vwap_dev_at_entry",  "REAL"),
        ("vol_ratio_at_entry", "REAL"),
        ("bb_pct_at_entry",    "REAL"),
        ("pair_regime",        "TEXT"),
        ("peak_price",         "REAL"),
        ("trough_price",       "REAL"),
        ("exchange_order_id",  "TEXT"),
        ("stop_order_id",      "TEXT"),
        ("gross_profit_usdt",  "REAL"),
        ("estimated_fee_usdt", "REAL"),
        ("estimated_slippage_usdt", "REAL"),
        ("bot_variant",        "TEXT"),
    ]
    with _conn() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col, coltype in new_columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {coltype}")

    decision_columns = [
        ("bot_variant", "TEXT"),
    ]
    with _conn() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(decisions)").fetchall()}
        for col, coltype in decision_columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE decisions ADD COLUMN {col} {coltype}")

    # Seed default params if not present
    defaults = {
        "signal_profile":     os.environ.get("SIGNAL_PROFILE", "active_dry_run"),
        "confirmation_mode":  "soft",
        "rsi_short_entry":    "58",
        "rsi_long_entry":     "42",
        "vwap_rsi_short":     "55",
        "vwap_rsi_long":      "45",
        "trend_rsi_long":     "52",
        "trend_rsi_short":    "48",
        "trend_rsi_max_long": "68",
        "trend_rsi_min_short": "32",
        "active_min_volume_ratio": "0.05",
        "rsi_exit_short":     "45",
        "rsi_exit_long":      "55",
        "bb_period":          "20",
        "bb_std":             "2.0",
        "ema_fast":           "8",
        "ema_slow":           "21",
        "volume_multiplier":  "0.8",
        "atr_period":         "14",
        "atr_min_pct":        "0.1",   # block if ATR% < this (too quiet)
        "atr_max_pct":        "6.0",   # block if ATR% > this (too wild)
        "vwap_dev_min":       "0.15",  # min % deviation for VWAP signal
        "stoploss_pct":       "2.5",
        "trailing_pct":       "1.0",
        "trailing_offset":    "1.5",
        "leverage":           "5",
        "max_open_trades":    "3",
        "stake_usdt":         os.environ.get("STAKE_USDT", "500"),
        "daily_loss_limit":   "10.0",  # % of wallet
        "max_consec_losses":  "4",
        "pair_cooldown_min":  "15",
        "blocked_sessions":   "[]",    # JSON list e.g. '["Asia"]'
        "hermes_auto_apply":   "false", # require review before Hermes changes params
        "bot_variant":         os.environ.get("BOT_VARIANT", "codex_v2_active"),
        "taker_fee_bps":       os.environ.get("TAKER_FEE_BPS", "5.5"),
        "slippage_bps":        os.environ.get("SLIPPAGE_BPS", "2.0"),
        "wallet_start":        os.environ.get("DRY_RUN_WALLET", "5000"),
    }
    with _conn() as conn:
        now = _now()
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO params(key, value, updated_at) VALUES (?,?,?)",
                (k, v, now)
            )

        dry_run = os.environ.get("DRY_RUN", "true").lower() != "false"
        apply_active = os.environ.get("APPLY_ACTIVE_DRY_RUN_PROFILE", "true").lower() != "false"
        profile_done = conn.execute(
            "SELECT value FROM bot_state WHERE key='migration_active_dry_run_profile_v2'"
        ).fetchone()
        if dry_run and apply_active and profile_done is None:
            active_updates = {
                "signal_profile": "active_dry_run",
                "confirmation_mode": "soft",
                "rsi_short_entry": "58",
                "rsi_long_entry": "42",
                "vwap_rsi_short": "55",
                "vwap_rsi_long": "45",
                "trend_rsi_long": "52",
                "trend_rsi_short": "48",
                "trend_rsi_max_long": "68",
                "trend_rsi_min_short": "32",
                "active_min_volume_ratio": "0.05",
                "volume_multiplier": "0.8",
                "atr_min_pct": "0.1",
                "atr_max_pct": "6.0",
                "vwap_dev_min": "0.15",
                "stake_usdt": os.environ.get("STAKE_USDT", "500"),
                "wallet_start": os.environ.get("DRY_RUN_WALLET", "5000"),
            }
            for k, v in active_updates.items():
                conn.execute(
                    "INSERT OR REPLACE INTO params(key, value, updated_at, updated_by) VALUES (?,?,?,?)",
                    (k, v, now, "migration")
                )
            conn.execute(
                "INSERT OR REPLACE INTO bot_state(key, value) VALUES (?,?)",
                ("migration_active_dry_run_profile_v2", now)
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


PARAM_SCHEMA = {
    "signal_profile":     {"type": "choice", "allowed": {"balanced", "active_dry_run"}},
    "confirmation_mode":  {"type": "choice", "allowed": {"hard", "soft"}},
    "rsi_short_entry":    {"type": float, "min": 50, "max": 90},
    "rsi_long_entry":     {"type": float, "min": 10, "max": 50},
    "vwap_rsi_short":     {"type": float, "min": 45, "max": 90},
    "vwap_rsi_long":      {"type": float, "min": 10, "max": 55},
    "trend_rsi_long":     {"type": float, "min": 45, "max": 80},
    "trend_rsi_short":    {"type": float, "min": 20, "max": 55},
    "trend_rsi_max_long": {"type": float, "min": 50, "max": 90},
    "trend_rsi_min_short": {"type": float, "min": 10, "max": 50},
    "active_min_volume_ratio": {"type": float, "min": 0.0, "max": 5.0},
    "rsi_exit_short":     {"type": float, "min": 10, "max": 80},
    "rsi_exit_long":      {"type": float, "min": 20, "max": 90},
    "bb_period":          {"type": int,   "min": 5,  "max": 100},
    "bb_std":             {"type": float, "min": 0.5, "max": 5.0},
    "ema_fast":           {"type": int,   "min": 2,  "max": 50},
    "ema_slow":           {"type": int,   "min": 3,  "max": 200},
    "volume_multiplier":  {"type": float, "min": 0.1, "max": 5.0},
    "atr_period":         {"type": int,   "min": 2,  "max": 100},
    "atr_min_pct":        {"type": float, "min": 0.0, "max": 10.0},
    "atr_max_pct":        {"type": float, "min": 0.1, "max": 20.0},
    "vwap_dev_min":       {"type": float, "min": 0.0, "max": 10.0},
    "stoploss_pct":       {"type": float, "min": 0.1, "max": 50.0},
    "trailing_pct":       {"type": float, "min": 0.1, "max": 50.0},
    "trailing_offset":    {"type": float, "min": 0.1, "max": 50.0},
    "leverage":           {"type": int,   "min": 1,  "max": 10},
    "max_open_trades":    {"type": int,   "min": 1,  "max": len(DEFAULT_PAIRS)},
    "stake_usdt":         {"type": float, "min": 1.0, "max": 100000.0},
    "wallet_start":       {"type": float, "min": 1.0, "max": 100000000.0},
    "daily_loss_limit":   {"type": float, "min": 0.1, "max": 100.0},
    "max_consec_losses":  {"type": int,   "min": 1,  "max": 50},
    "pair_cooldown_min":  {"type": int,   "min": 0,  "max": 1440},
    "blocked_sessions":   {"type": "json_list", "allowed": {"Asia", "London", "US"}},
    "hermes_auto_apply":  {"type": "bool"},
    "bot_variant":        {"type": "text", "max_len": 64},
    "taker_fee_bps":      {"type": float, "min": 0.0, "max": 100.0},
    "slippage_bps":       {"type": float, "min": 0.0, "max": 100.0},
}


def validate_param_update(key: str, value) -> str:
    spec = PARAM_SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"Unknown parameter: {key}")

    typ = spec["type"]
    if typ == "json_list":
        parsed = json.loads(value) if isinstance(value, str) else value
        if not isinstance(parsed, list):
            raise ValueError(f"{key} must be a JSON list")
        allowed = spec.get("allowed")
        if allowed:
            invalid = [x for x in parsed if x not in allowed]
            if invalid:
                raise ValueError(f"{key} contains unsupported value(s): {invalid}")
        return json.dumps(parsed)
    if typ == "bool":
        if isinstance(value, bool):
            return "true" if value else "false"
        sval = str(value).strip().lower()
        if sval not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
        return sval
    if typ == "text":
        sval = str(value).strip()
        if not sval:
            raise ValueError(f"{key} cannot be empty")
        if len(sval) > spec.get("max_len", 255):
            raise ValueError(f"{key} is too long")
        return sval
    if typ == "choice":
        sval = str(value).strip()
        allowed = spec.get("allowed", set())
        if sval not in allowed:
            raise ValueError(f"{key} must be one of {sorted(allowed)}")
        return sval

    try:
        parsed = typ(value)
    except Exception:
        raise ValueError(f"{key} must be a {typ.__name__}")
    if parsed < spec["min"] or parsed > spec["max"]:
        raise ValueError(f"{key} must be between {spec['min']} and {spec['max']}")
    return str(parsed)


def set_validated_param(key: str, value, updated_by: str = "admin"):
    set_param(key, validate_param_update(key, value), updated_by=updated_by)


# ── Trade helpers ─────────────────────────────────────────────────────────────

def open_trade(pair, side, entry_price, amount, stake_usdt, leverage,
               entry_tag, session, btc_regime, atr_at_entry, dry_run=True,
               rsi_at_entry=None, vwap_dev_at_entry=None,
               vol_ratio_at_entry=None, bb_pct_at_entry=None,
               pair_regime=None, exchange_order_id=None, stop_order_id=None,
               bot_variant=None) -> int:
    if bot_variant is None:
        bot_variant = get_param("bot_variant", os.environ.get("BOT_VARIANT", "codex_v2_active"))
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (pair, side, entry_price, amount, stake_usdt, leverage,
               entry_time, entry_tag, session, btc_regime, atr_at_entry,
               rsi_at_entry, vwap_dev_at_entry, vol_ratio_at_entry,
               bb_pct_at_entry, pair_regime, peak_price, trough_price,
               exchange_order_id, stop_order_id, bot_variant, is_open, dry_run)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
        """, (pair, side, entry_price, amount, stake_usdt, leverage,
              _now(), entry_tag, session, btc_regime, atr_at_entry,
              rsi_at_entry, vwap_dev_at_entry, vol_ratio_at_entry,
              bb_pct_at_entry, pair_regime, entry_price, entry_price,
              exchange_order_id, stop_order_id, bot_variant, int(dry_run)))
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
        gross_profit_usdt = stake_usdt * profit_pct / 100
        p = get_all_params()
        taker_fee_bps = float(p.get("taker_fee_bps", "5.5"))
        slippage_bps = float(p.get("slippage_bps", "2.0"))
        entry_notional = abs(amount * entry_price)
        exit_notional = abs(amount * exit_price)
        round_trip_notional = entry_notional + exit_notional
        estimated_fee_usdt = round_trip_notional * taker_fee_bps / 10000
        estimated_slippage_usdt = round_trip_notional * slippage_bps / 10000
        profit_usdt = gross_profit_usdt - estimated_fee_usdt - estimated_slippage_usdt

        conn.execute("""
            UPDATE trades SET
              exit_price=?, exit_time=?, exit_reason=?, exit_tag=?,
              profit_usdt=?, profit_pct=?, gross_profit_usdt=?,
              estimated_fee_usdt=?, estimated_slippage_usdt=?, is_open=0
            WHERE id=?
        """, (exit_price, _now(), exit_reason, exit_tag,
              round(profit_usdt, 4), round(profit_pct, 4),
              round(gross_profit_usdt, 4), round(estimated_fee_usdt, 4),
              round(estimated_slippage_usdt, 4), trade_id))

def get_open_trades() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE is_open=1 ORDER BY entry_time"
        ).fetchall()
    return [dict(r) for r in rows]


def update_trade_extremes(trade_id: int, peak_price: float, trough_price: float):
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET peak_price=?, trough_price=? WHERE id=? AND is_open=1",
            (peak_price, trough_price, trade_id)
        )

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


def get_closed_trades_for_day(day: str) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades
            WHERE is_open=0 AND substr(exit_time, 1, 10)=?
            ORDER BY exit_time DESC
        """, (day,)).fetchall()
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


def log_decision(pair: str, candle_ts: str = None, price=None, rsi=None,
                 atr_pct=None, vwap_dev=None, volume_ratio=None,
                 bb_position=None, ema_trend=None, btc_1h_regime=None,
                 pair_15m_regime=None, pair_1h_regime=None, pair_4h_regime=None,
                 signal_type=None, fired: bool = False, skip_reason=None,
                 params_snapshot: dict = None, context: dict = None,
                 bot_variant=None):
    if bot_variant is None:
        bot_variant = get_param("bot_variant", os.environ.get("BOT_VARIANT", "codex_v2_active"))
    with _conn() as conn:
        conn.execute("""
            INSERT INTO decisions
              (ts, candle_ts, pair, price, rsi, atr_pct, vwap_dev,
               volume_ratio, bb_position, ema_trend, btc_1h_regime,
               pair_15m_regime, pair_1h_regime, pair_4h_regime,
               signal_type, fired, skip_reason, params_snapshot, context_json,
               bot_variant)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _now(), candle_ts, pair, price, rsi, atr_pct, vwap_dev,
            volume_ratio, bb_position, ema_trend, btc_1h_regime,
            pair_15m_regime, pair_1h_regime, pair_4h_regime,
            signal_type, int(fired), skip_reason,
            json.dumps(params_snapshot) if params_snapshot else None,
            json.dumps(context) if context else None,
            bot_variant,
        ))


# ── Hermes log ────────────────────────────────────────────────────────────────

def log_hermes(analysis: dict, suggestions: dict, applied: bool = False):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO hermes_log(ts, analysis, suggestions, applied) VALUES (?,?,?,?)",
            (_now(), json.dumps(analysis), json.dumps(suggestions), int(applied))
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


# ── Command queue (admin → bot) ───────────────────────────────────────────────

def send_command(command: str, payload: dict = None) -> int:
    """Queue a command for the bot to pick up. Returns command id."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO commands(ts, command, payload, status) VALUES (?,?,?,?)",
            (_now(), command, json.dumps(payload) if payload else None, "pending")
        )
        return cur.lastrowid

def get_pending_commands() -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM commands WHERE status='pending' ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]

def ack_command(cmd_id: int, status: str = "done", result: str = ""):
    with _conn() as conn:
        conn.execute(
            "UPDATE commands SET status=?, result=? WHERE id=?",
            (status, result, cmd_id)
        )

def get_recent_commands(limit: int = 20) -> List[Dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Bot state ─────────────────────────────────────────────────────────────────

def set_bot_state(key: str, value: str):
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_state(key, value) VALUES (?,?)",
            (key, value)
        )

def get_bot_state(key: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


# ── Active pairs ──────────────────────────────────────────────────────────────

def get_active_pairs() -> List[str]:
    raw = get_param("active_pairs", "")
    if not raw:
        return list(DEFAULT_PAIRS)
    try:
        return validate_pairs(json.loads(raw))
    except Exception:
        return list(DEFAULT_PAIRS)

def set_active_pairs(pairs: List[str]):
    set_param("active_pairs", json.dumps(validate_pairs(pairs)), updated_by="admin")


def validate_pairs(pairs: List[str]) -> List[str]:
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Provide a non-empty pairs list")
    cleaned = []
    for pair in pairs:
        p = str(pair).strip().upper()
        if p not in ALLOWED_PAIRS:
            raise ValueError(f"Unsupported pair: {p}")
        if p not in cleaned:
            cleaned.append(p)
    return cleaned


# ── Util ──────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
