"""
bot.py — KotipotiBot v2 main trading loop
==========================================
Pure Python + ccxt. No Freqtrade.
Runs every 5 minutes, fetches candles, computes indicators,
evaluates signals, places orders, manages open positions.

Signals:
  - BB breakout (original): price outside 2σ + RSI extreme + EMA cross + volume
  - VWAP reversion (new):   price deviates >N% from VWAP + volume spike
  - ATR filter (new):       blocks entries during low/high volatility

Environment variables:
  BYBIT_API_KEY, BYBIT_API_SECRET  — exchange credentials
  DRY_RUN                          — "true" (default) or "false"
  DB_PATH                          — path to SQLite file (default: /data/kotipoti.db)
  LOG_LEVEL                        — DEBUG / INFO (default: INFO)
"""

import ccxt
import pandas as pd
import numpy as np
import time
import logging
import os
import json
import sys
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import db
import telegram as tg

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/data/bot.log", mode="a"),
    ]
)
log = logging.getLogger("kotipoti")

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN   = os.environ.get("DRY_RUN", "true").lower() != "false"
API_KEY   = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

PAIRS = [
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
    "XRP/USDT:USDT",
]
ALT_PAIRS = {"ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"}
BTC_PAIR  = "BTC/USDT:USDT"
TIMEFRAME = "5m"
CANDLES_NEEDED = 100  # enough for EMA21, BB20, ATR14, VWAP

# ── Runtime state (in-memory) ─────────────────────────────────────────────────
_consecutive_losses: int = 0
_consec_loss_halt_until: Optional[datetime] = None
_daily_loss_halted: bool = False
_daily_loss_reset_day: Optional[int] = None
_pair_last_loss: Dict[str, datetime] = {}   # pair -> time of last losing exit
_api_error_times: List[datetime] = []
_bot_paused: bool = False
_bot_stopped: bool = False


# ── Exchange setup ────────────────────────────────────────────────────────────

def make_exchange() -> ccxt.bybit:
    exchange = ccxt.bybit({
        "apiKey":    API_KEY,
        "secret":    API_SECRET,
        "options": {
            "defaultType":   "swap",
            "defaultSettle": "USDT",
        },
        "enableRateLimit": True,
    })
    return exchange


# ── Candle fetching ───────────────────────────────────────────────────────────

def fetch_ohlcv(exchange: ccxt.bybit, pair: str, timeframe: str,
                limit: int = CANDLES_NEEDED) -> Optional[pd.DataFrame]:
    try:
        raw = exchange.fetch_ohlcv(pair, timeframe, limit=limit)
        if not raw or len(raw) < 20:
            return None
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.drop(columns=["timestamp"])
        return df
    except Exception as e:
        log.warning(f"fetch_ohlcv {pair} {timeframe}: {e}")
        return None


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to a 5m OHLCV dataframe."""
    p = db.get_all_params()

    bb_period = int(p.get("bb_period", "20"))
    bb_std    = float(p.get("bb_std", "2.0"))
    ema_fast  = int(p.get("ema_fast", "8"))
    ema_slow  = int(p.get("ema_slow", "21"))
    atr_period = int(p.get("atr_period", "14"))

    # EMA
    df["ema_fast"] = df["close"].ewm(span=ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow, adjust=False).mean()

    # Bollinger Bands
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rolling_mean = tp.rolling(bb_period).mean()
    rolling_std  = tp.rolling(bb_period).std()
    df["bb_mid"]   = rolling_mean
    df["bb_upper"] = rolling_mean + bb_std * rolling_std
    df["bb_lower"] = rolling_mean - bb_std * rolling_std
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

    # RSI
    delta  = df["close"].diff()
    gain   = delta.clip(lower=0).rolling(14).mean()
    loss   = (-delta.clip(upper=0)).rolling(14).mean()
    rs     = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ATR
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["atr"] = tr.rolling(atr_period).mean()
    df["atr_pct"] = df["atr"] / df["close"] * 100

    # Volume ratio
    df["volume_mean"] = df["volume"].rolling(20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_mean"].replace(0, np.nan)

    # VWAP (rolling intraday — reset not available in rolling mode, use 20-candle window)
    tp_vol = tp * df["volume"]
    df["vwap"] = tp_vol.rolling(20).sum() / df["volume"].rolling(20).sum()
    df["vwap_dev"] = (df["close"] - df["vwap"]) / df["vwap"] * 100

    return df


def compute_1h_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add 1h trend indicators: EMA21, RSI, higher-highs, lower-lows."""
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["higher_highs"] = (df["high"] > df["high"].shift(1)) & \
                         (df["high"].shift(1) > df["high"].shift(2))
    df["lower_lows"]   = (df["low"] < df["low"].shift(1)) & \
                         (df["low"].shift(1) < df["low"].shift(2))
    return df


# ── Regime classification ─────────────────────────────────────────────────────

def btc_regime(btc_1h: Optional[pd.DataFrame]) -> str:
    if btc_1h is None or len(btc_1h) < 3:
        return "unknown"
    row = btc_1h.iloc[-1]
    try:
        above_ema = row["close"] > row["ema21"]
        rsi_bull  = row["rsi"] > 55
        rsi_bear  = row["rsi"] < 45
        hh        = bool(row.get("higher_highs", False))
        ll        = bool(row.get("lower_lows",   False))
        if above_ema and rsi_bull and hh:
            return "bull"
        elif not above_ema and rsi_bear and ll:
            return "bear"
        else:
            return "range"
    except Exception:
        return "unknown"


def pair_1h_regime(df_1h: Optional[pd.DataFrame]) -> str:
    if df_1h is None or len(df_1h) < 3:
        return "unknown"
    row = df_1h.iloc[-1]
    try:
        above_ema = row["close"] > row["ema21"]
        rsi_bull  = row["rsi"] > 55
        rsi_bear  = row["rsi"] < 45
        if above_ema and rsi_bull:
            return "bull"
        elif not above_ema and rsi_bear:
            return "bear"
        else:
            return "range"
    except Exception:
        return "unknown"


def session_label() -> str:
    h = datetime.now(timezone.utc).hour
    if 0 <= h < 8:
        return "Asia"
    elif 8 <= h < 13:
        return "London"
    else:
        return "US"


# ── Signal evaluation ─────────────────────────────────────────────────────────

def evaluate_signals(pair: str, df5m: pd.DataFrame,
                     btc_regime_label: str,
                     pair_regime_label: str) -> List[Dict]:
    """
    Returns list of signal dicts with keys:
      direction, entry_tag, price, confidence (0-1)
    Empty list if no signal.
    """
    p      = db.get_all_params()
    row    = df5m.iloc[-1]
    signals = []
    session = session_label()

    # Check blocked sessions
    blocked = json.loads(p.get("blocked_sessions", "[]"))
    if session in blocked:
        return []

    rsi           = row.get("rsi")
    close         = row["close"]
    bb_upper      = row.get("bb_upper")
    bb_lower      = row.get("bb_lower")
    bb_mid        = row.get("bb_mid")
    ema_f         = row.get("ema_fast")
    ema_s         = row.get("ema_slow")
    vol_ratio     = row.get("volume_ratio", 0)
    atr_pct       = row.get("atr_pct", 1.0)
    vwap_dev      = row.get("vwap_dev", 0)

    rsi_short     = float(p.get("rsi_short_entry", "68"))
    rsi_long      = float(p.get("rsi_long_entry",  "32"))
    vol_mult      = float(p.get("volume_multiplier", "1.2"))
    atr_min       = float(p.get("atr_min_pct", "0.3"))
    atr_max       = float(p.get("atr_max_pct", "3.0"))
    vwap_min      = float(p.get("vwap_dev_min", "0.5"))

    if rsi is None or bb_upper is None:
        return []

    # ── ATR gate (applies to all signals) ────────────────────────────────────
    if atr_pct < atr_min:
        return []   # market too quiet — scalps won't cover fees
    if atr_pct > atr_max:
        return []   # market too explosive — stoploss will get slipped

    # ── Volume gate ───────────────────────────────────────────────────────────
    vol_ok = vol_ratio >= vol_mult

    # ── BB breakout SHORT ─────────────────────────────────────────────────────
    bb_short_ok  = close > bb_upper
    rsi_short_ok = rsi > rsi_short
    ema_short_ok = ema_f < ema_s   # bearish EMA cross

    if bb_short_ok and rsi_short_ok and ema_short_ok and vol_ok:
        # 1h trend filter: block short if pair is strongly bullish (unless overextended)
        overextended = rsi > 80
        trend_ok = not (pair_regime_label == "bull" and not overextended)
        # BTC filter (alts only)
        btc_ok = True
        if pair in ALT_PAIRS:
            btc_ok = not (btc_regime_label == "bull" and not overextended)

        if trend_ok and btc_ok:
            bb_pct = (close - bb_upper) / bb_upper * 100
            ema_gap = (ema_f - ema_s) / close * 100
            signals.append({
                "direction": "short",
                "entry_tag": "bb_short",
                "price":     close,
                "rsi":       rsi,
                "bb_pct":    round(bb_pct, 3),
                "ema_gap":   round(ema_gap, 4),
                "atr_pct":   round(atr_pct, 3),
                "vwap_dev":  round(vwap_dev, 3),
                "vol_ratio": round(vol_ratio, 2),
                "session":   session,
                "skip_reason": None,
            })
        else:
            reason = "trend_filter" if not trend_ok else "btc_filter"
            db.log_signal(pair, "short", fired=False, skip_reason=reason,
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)

    # ── BB breakout LONG ──────────────────────────────────────────────────────
    bb_long_ok  = close < bb_lower
    rsi_long_ok = rsi < rsi_long
    ema_long_ok = ema_f > ema_s   # bullish EMA cross

    if bb_long_ok and rsi_long_ok and ema_long_ok and vol_ok:
        overextended = rsi < 20
        trend_ok = not (pair_regime_label == "bear" and not overextended)
        btc_ok = True
        if pair in ALT_PAIRS:
            btc_ok = not (btc_regime_label == "bear" and not overextended)

        if trend_ok and btc_ok:
            bb_pct = (bb_lower - close) / bb_lower * 100
            ema_gap = (ema_f - ema_s) / close * 100
            signals.append({
                "direction": "long",
                "entry_tag": "bb_long",
                "price":     close,
                "rsi":       rsi,
                "bb_pct":    round(bb_pct, 3),
                "ema_gap":   round(ema_gap, 4),
                "atr_pct":   round(atr_pct, 3),
                "vwap_dev":  round(vwap_dev, 3),
                "vol_ratio": round(vol_ratio, 2),
                "session":   session,
                "skip_reason": None,
            })
        else:
            reason = "trend_filter" if not trend_ok else "btc_filter"
            db.log_signal(pair, "long", fired=False, skip_reason=reason,
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)

    # ── VWAP reversion SHORT ──────────────────────────────────────────────────
    # Price significantly above VWAP + volume spike → expect reversion
    if vwap_dev > vwap_min and vol_ratio >= vol_mult * 1.3 and rsi > 60:
        trend_ok = pair_regime_label != "bull"
        btc_ok   = btc_regime_label != "bull" if pair in ALT_PAIRS else True
        if trend_ok and btc_ok:
            signals.append({
                "direction": "short",
                "entry_tag": "vwap_short",
                "price":     close,
                "rsi":       rsi,
                "bb_pct":    round((close - bb_upper) / bb_upper * 100 if bb_upper else 0, 3),
                "ema_gap":   round((ema_f - ema_s) / close * 100 if ema_f and ema_s else 0, 4),
                "atr_pct":   round(atr_pct, 3),
                "vwap_dev":  round(vwap_dev, 3),
                "vol_ratio": round(vol_ratio, 2),
                "session":   session,
                "skip_reason": None,
            })

    # ── VWAP reversion LONG ───────────────────────────────────────────────────
    if vwap_dev < -vwap_min and vol_ratio >= vol_mult * 1.3 and rsi < 40:
        trend_ok = pair_regime_label != "bear"
        btc_ok   = btc_regime_label != "bear" if pair in ALT_PAIRS else True
        if trend_ok and btc_ok:
            signals.append({
                "direction": "long",
                "entry_tag": "vwap_long",
                "price":     close,
                "rsi":       rsi,
                "bb_pct":    round((bb_lower - close) / bb_lower * 100 if bb_lower else 0, 3),
                "ema_gap":   round((ema_f - ema_s) / close * 100 if ema_f and ema_s else 0, 4),
                "atr_pct":   round(atr_pct, 3),
                "vwap_dev":  round(vwap_dev, 3),
                "vol_ratio": round(vol_ratio, 2),
                "session":   session,
                "skip_reason": None,
            })

    return signals


# ── Exit evaluation ───────────────────────────────────────────────────────────

def check_exits(open_trades: List[Dict], df5m: Dict[str, pd.DataFrame],
                current_prices: Dict[str, float]) -> List[Tuple[int, float, str]]:
    """
    Returns list of (trade_id, exit_price, reason) for trades to close.
    """
    p = db.get_all_params()
    sl_pct       = float(p.get("stoploss_pct",    "2.5")) / 100
    trail_pct    = float(p.get("trailing_pct",    "1.0")) / 100
    trail_offset = float(p.get("trailing_offset", "1.5")) / 100
    rsi_exit_short = float(p.get("rsi_exit_short", "45"))
    rsi_exit_long  = float(p.get("rsi_exit_long",  "55"))

    exits = []
    for trade in open_trades:
        pair        = trade["pair"]
        side        = trade["side"]
        entry_price = trade["entry_price"]
        trade_id    = trade["id"]
        current     = current_prices.get(pair)
        if current is None:
            continue

        # Calculate current profit
        if side == "long":
            profit_pct = (current - entry_price) / entry_price
        else:
            profit_pct = (entry_price - current) / entry_price

        # Hard stoploss
        if profit_pct <= -sl_pct:
            exits.append((trade_id, current, "stoploss"))
            continue

        # Trailing stop: only kicks in once offset is reached
        if profit_pct >= trail_offset:
            trail_stop = profit_pct - trail_pct
            if trail_stop <= 0:
                exits.append((trade_id, current, "trailing_stop"))
                continue

        # Signal-based exit
        df = df5m.get(pair)
        if df is not None and len(df) > 0:
            row = df.iloc[-1]
            rsi    = row.get("rsi")
            bb_mid = row.get("bb_mid")
            close  = row["close"]

            if side == "short" and rsi is not None and bb_mid is not None:
                if rsi < rsi_exit_short and close < bb_mid:
                    exits.append((trade_id, current, "signal"))
                    continue

            if side == "long" and rsi is not None and bb_mid is not None:
                if rsi > rsi_exit_long and close > bb_mid:
                    exits.append((trade_id, current, "signal"))
                    continue

        # ROI exit (minimal_roi equivalent)
        roi_map = {0: 0.015, 10: 0.010, 30: 0.005, 60: 0.0}
        # Minutes since entry
        try:
            entry_dt = datetime.fromisoformat(trade["entry_time"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            mins = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
        except Exception:
            mins = 0
        for threshold_min in sorted(roi_map.keys(), reverse=True):
            if mins >= threshold_min:
                if profit_pct >= roi_map[threshold_min]:
                    exits.append((trade_id, current, "roi"))
                break

    return exits


# ── Safety gates ─────────────────────────────────────────────────────────────

def safety_ok(pair: str, current_time: datetime) -> Tuple[bool, str]:
    global _consecutive_losses, _consec_loss_halt_until
    global _daily_loss_halted, _daily_loss_reset_day, _pair_last_loss

    p = db.get_all_params()
    max_consec   = int(p.get("max_consec_losses", "4"))
    cooldown_min = int(p.get("pair_cooldown_min", "15"))

    utc = current_time.replace(tzinfo=timezone.utc) if current_time.tzinfo is None \
          else current_time.astimezone(timezone.utc)

    # Daily loss reset at UTC midnight
    today = utc.date().toordinal()
    if _daily_loss_reset_day != today:
        _daily_loss_reset_day = today
        _daily_loss_halted = False

    if _daily_loss_halted:
        return False, "daily_loss_halt"

    # Consecutive loss halt
    if _consec_loss_halt_until is not None:
        halt = _consec_loss_halt_until
        if halt.tzinfo is None:
            halt = halt.replace(tzinfo=timezone.utc)
        if utc < halt:
            mins = int((halt - utc).total_seconds() / 60)
            return False, f"consec_loss_halt_{mins}m"
        else:
            _consec_loss_halt_until = None
            _consecutive_losses = 0

    # Per-pair cooldown after a loss
    last_loss = _pair_last_loss.get(pair)
    if last_loss is not None:
        elapsed = (utc - last_loss).total_seconds() / 60
        if elapsed < cooldown_min:
            return False, f"pair_cooldown_{int(cooldown_min - elapsed)}m"

    return True, "ok"


def record_exit_outcome(pair: str, profit_usdt: float, current_time: datetime):
    """Update in-memory safety state after a trade closes."""
    global _consecutive_losses, _consec_loss_halt_until
    global _daily_loss_halted, _pair_last_loss

    p = db.get_all_params()
    max_consec    = int(p.get("max_consec_losses", "4"))
    cooldown_h    = 2

    if profit_usdt < 0:
        _consecutive_losses += 1
        _pair_last_loss[pair] = current_time.replace(tzinfo=timezone.utc) \
            if current_time.tzinfo is None else current_time
        if _consecutive_losses >= max_consec:
            halt_until = current_time + timedelta(hours=cooldown_h)
            _consec_loss_halt_until = halt_until
            log.warning(f"[Safety] {max_consec} consecutive losses — halting entries for {cooldown_h}h")
            tg.consecutive_loss_halt(max_consec, cooldown_h * 60)
    else:
        _consecutive_losses = 0


def check_daily_loss(wallet_start: float, wallet_now: float):
    global _daily_loss_halted
    p = db.get_all_params()
    limit_pct = float(p.get("daily_loss_limit", "10.0"))
    if wallet_start > 0:
        loss_pct = (wallet_start - wallet_now) / wallet_start * 100
        if loss_pct >= limit_pct and not _daily_loss_halted:
            _daily_loss_halted = True
            log.warning(f"[Safety] Daily loss {loss_pct:.1f}% >= {limit_pct}% — halting entries")
            tg.daily_loss_halt(loss_pct, limit_pct)


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(exchange: ccxt.bybit, pair: str, side: str,
                stake_usdt: float, leverage: int,
                current_price: float) -> Optional[Dict]:
    """Place a market order. Returns order dict or None on failure."""
    if DRY_RUN:
        amount = round(stake_usdt * leverage / current_price, 6)
        log.info(f"[DryRun] {side.upper()} {pair} @ {current_price:.4f} "
                 f"| stake={stake_usdt} USDT | amount={amount} | lev={leverage}x")
        return {
            "id":     f"dry_{int(time.time())}",
            "price":  current_price,
            "amount": amount,
            "side":   side,
        }
    try:
        exchange.set_leverage(leverage, pair)
        amount = round(stake_usdt * leverage / current_price, 6)
        order = exchange.create_market_order(
            symbol=pair,
            side="buy" if side == "long" else "sell",
            amount=amount,
            params={"positionIdx": 1 if side == "long" else 2}
        )
        log.info(f"[Order] {side.upper()} {pair} @ {current_price:.4f} "
                 f"| amt={amount} | id={order.get('id')}")
        return order
    except Exception as e:
        log.error(f"[Order] Failed to place {side} on {pair}: {e}")
        _api_error_times.append(datetime.now(timezone.utc))
        return None


def close_order(exchange: ccxt.bybit, pair: str, side: str, amount: float,
                current_price: float, reason: str):
    """Close an open position with a market order."""
    if DRY_RUN:
        log.info(f"[DryRun] CLOSE {side.upper()} {pair} @ {current_price:.4f} ({reason})")
        return
    try:
        close_side = "sell" if side == "long" else "buy"
        exchange.create_market_order(
            symbol=pair,
            side=close_side,
            amount=amount,
            params={
                "positionIdx": 1 if side == "long" else 2,
                "reduceOnly": True,
            }
        )
        log.info(f"[Order] CLOSE {side.upper()} {pair} @ {current_price:.4f} ({reason})")
    except Exception as e:
        log.error(f"[Order] Failed to close {side} on {pair}: {e}")


# ── Admin command processor ───────────────────────────────────────────────────

def _process_commands(exchange: ccxt.bybit):
    """Process any pending admin commands from the DB command queue."""
    global _bot_paused, _bot_stopped

    for cmd in db.get_pending_commands():
        command = cmd["command"]
        cmd_id  = cmd["id"]
        payload = json.loads(cmd["payload"]) if cmd.get("payload") else {}

        try:
            if command == "stop":
                _bot_stopped = True
                db.ack_command(cmd_id, "done", "Bot stopped")
                log.warning("[Admin] STOP command received")

            elif command == "pause":
                _bot_paused = True
                db.ack_command(cmd_id, "done", "Bot paused")
                log.warning("[Admin] PAUSE command received — new entries halted")
                tg.bot_status("paused", "New entries halted via admin panel.")

            elif command == "resume":
                _bot_paused = False
                db.ack_command(cmd_id, "done", "Bot resumed")
                log.info("[Admin] RESUME command received")
                tg.bot_status("resumed", "Signal evaluation active again.")

            elif command == "force_close":
                trade_id = payload.get("trade_id")
                if not trade_id:
                    db.ack_command(cmd_id, "error", "Missing trade_id")
                    continue
                open_trades = db.get_open_trades()
                trade = next((t for t in open_trades if t["id"] == trade_id), None)
                if not trade:
                    db.ack_command(cmd_id, "error", f"Trade {trade_id} not found or not open")
                    continue
                # Fetch current price
                try:
                    ticker = exchange.fetch_ticker(trade["pair"])
                    price  = float(ticker["last"])
                except Exception as e:
                    price = trade["entry_price"]
                    log.warning(f"[Admin] Could not fetch price for force close: {e}")
                close_order(exchange, trade["pair"], trade["side"],
                            trade["amount"], price, "admin_force_close")
                db.close_trade(trade_id, price, "force_close", "admin")
                db.ack_command(cmd_id, "done",
                               f"Force closed trade {trade_id} @ {price:.4f}")
                log.warning(f"[Admin] Force closed trade #{trade_id} {trade['pair']} @ {price}")
                tg.trade_closed(trade["pair"], trade["side"], trade["entry_price"],
                                price, 0, 0, "force_close", DRY_RUN)

            elif command == "update_pairs":
                pairs = payload.get("pairs", [])
                if pairs:
                    db.set_active_pairs(pairs)
                    db.ack_command(cmd_id, "done", f"Pairs updated: {pairs}")
                    log.info(f"[Admin] Active pairs updated: {pairs}")
                else:
                    db.ack_command(cmd_id, "error", "Empty pairs list")

            else:
                db.ack_command(cmd_id, "error", f"Unknown command: {command}")

        except Exception as e:
            log.error(f"[Admin] Error processing command {command}: {e}")
            db.ack_command(cmd_id, "error", str(e))


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info(f"KotipotiBot v2 starting | DRY_RUN={DRY_RUN}")
    log.info("=" * 60)

    db.init_db()
    exchange = make_exchange()
    tg.bot_status("started", f"DRY_RUN={DRY_RUN} | Pairs: {', '.join(PAIRS)}")

    wallet_start = float(db.get_param("wallet_start", "0") or "0")
    if wallet_start == 0:
        wallet_start = float(db.get_param("stake_usdt", "200")) * 10
        db.set_param("wallet_start", str(wallet_start))

    last_candle_time: Dict[str, str] = {}  # pair -> last processed candle timestamp

    log.info(f"Pairs: {PAIRS}")
    log.info(f"Timeframe: {TIMEFRAME} | Candles needed: {CANDLES_NEEDED}")

    while True:
        loop_start = time.time()
        now = datetime.now(timezone.utc)

        # ── 0. Process admin commands ─────────────────────────────────────────
        global _bot_paused, _bot_stopped
        _process_commands(exchange)
        if _bot_stopped:
            log.info("[Bot] Stop command received — exiting loop.")
            tg.bot_status("stopped", "Stop command received via admin panel.")
            break

        # Update bot state heartbeat for dashboard
        db.set_bot_state("status", "paused" if _bot_paused else "running")
        db.set_bot_state("last_heartbeat", now.isoformat())

        if _bot_paused:
            log.info("[Bot] Paused — skipping signal evaluation.")
            time.sleep(30)
            continue

        try:
            # ── 1. Fetch BTC 1h for regime ────────────────────────────────────
            btc_1h_df = fetch_ohlcv(exchange, BTC_PAIR, "1h", limit=50)
            if btc_1h_df is not None:
                btc_1h_df = compute_1h_indicators(btc_1h_df)
            btc_regime_label = btc_regime(btc_1h_df)

            # ── 2. Fetch 5m candles for all pairs ─────────────────────────────
            df5m: Dict[str, pd.DataFrame] = {}
            current_prices: Dict[str, float] = {}

            for pair in PAIRS:
                df = fetch_ohlcv(exchange, pair, TIMEFRAME, limit=CANDLES_NEEDED)
                if df is None or len(df) < 30:
                    log.warning(f"Insufficient candles for {pair}, skipping")
                    continue
                df = compute_indicators(df)
                df5m[pair] = df
                current_prices[pair] = float(df.iloc[-1]["close"])

                # Check if this is a new candle
                last_ts = str(df.iloc[-1]["date"])
                is_new  = last_candle_time.get(pair) != last_ts
                last_candle_time[pair] = last_ts
                if not is_new:
                    continue   # skip signal eval — same candle as last loop

                log.debug(f"New candle {pair} @ {last_ts} | close={current_prices[pair]:.4f}")

            # ── 3. Manage open positions ──────────────────────────────────────
            open_trades = db.get_open_trades()
            exits = check_exits(open_trades, df5m, current_prices)

            for trade_id, exit_price, reason in exits:
                trade = next((t for t in open_trades if t["id"] == trade_id), None)
                if not trade:
                    continue
                close_order(exchange, trade["pair"], trade["side"],
                            trade["amount"], exit_price, reason)
                db.close_trade(trade_id, exit_price, reason)
                trade_side = trade["side"]
                stake      = trade["stake_usdt"]
                lev        = trade["leverage"]
                entry_p    = trade["entry_price"]
                if trade_side == "long":
                    profit_usdt = (exit_price - entry_p) / entry_p * stake * lev
                    profit_pct  = (exit_price - entry_p) / entry_p * 100 * lev
                else:
                    profit_usdt = (entry_p - exit_price) / entry_p * stake * lev
                    profit_pct  = (entry_p - exit_price) / entry_p * 100 * lev
                record_exit_outcome(trade["pair"], profit_usdt, now)
                emoji = "✅" if profit_usdt >= 0 else "❌"
                log.info(f"{emoji} CLOSED {trade_side.upper()} {trade['pair']} "
                         f"@ {exit_price:.4f} | reason={reason} "
                         f"| P&L={profit_usdt:+.2f} USDT")
                if reason == "stoploss":
                    tg.stoploss_hit(trade["pair"], trade_side, entry_p,
                                    exit_price, profit_usdt, DRY_RUN)
                else:
                    tg.trade_closed(trade["pair"], trade_side, entry_p,
                                    exit_price, profit_usdt, profit_pct,
                                    reason, DRY_RUN)

            # Reload open trades after exits
            open_trades = db.get_open_trades()
            open_pairs  = {t["pair"] for t in open_trades}

            # ── 4. Evaluate entry signals ─────────────────────────────────────
            p = db.get_all_params()
            max_open  = int(p.get("max_open_trades", "3"))
            stake     = float(p.get("stake_usdt", "200"))
            leverage  = int(p.get("leverage", "5"))

            if len(open_trades) < max_open:
                for pair in PAIRS:
                    if pair not in df5m:
                        continue

                    # Skip if already have an open trade on this pair
                    if pair in open_pairs:
                        continue

                    # Safety gate
                    ok, reason = safety_ok(pair, now)
                    if not ok:
                        log.debug(f"[Safety] {pair} blocked: {reason}")
                        continue

                    # Get 1h regime for this pair
                    pair_1h_df = fetch_ohlcv(exchange, pair, "1h", limit=50)
                    if pair_1h_df is not None:
                        pair_1h_df = compute_1h_indicators(pair_1h_df)
                    p_regime = pair_1h_regime(pair_1h_df)

                    # Check if this is a new candle for this pair
                    df = df5m[pair]
                    last_ts = str(df.iloc[-1]["date"])
                    # (already checked above — if we're here it was new)

                    sigs = evaluate_signals(pair, df, btc_regime_label, p_regime)
                    for sig in sigs:
                        if len(open_trades) >= max_open:
                            break

                        direction  = sig["direction"]
                        entry_tag  = sig["entry_tag"]
                        price      = sig["price"]
                        session    = sig["session"]

                        log.info(f"🔔 SIGNAL {direction.upper()} {pair} "
                                 f"| tag={entry_tag} | price={price:.4f} "
                                 f"| rsi={sig['rsi']:.1f} | atr={sig['atr_pct']:.2f}% "
                                 f"| regime=BTC:{btc_regime_label} pair:{p_regime}")

                        order = place_order(exchange, pair, direction,
                                            stake, leverage, price)
                        if order:
                            trade_id = db.open_trade(
                                pair=pair,
                                side=direction,
                                entry_price=price,
                                amount=order.get("amount", 0),
                                stake_usdt=stake,
                                leverage=leverage,
                                entry_tag=entry_tag,
                                session=session,
                                btc_regime=btc_regime_label,
                                atr_at_entry=sig["atr_pct"],
                                dry_run=DRY_RUN,
                            )
                            tg.trade_opened(pair, direction, price, stake,
                                            leverage, entry_tag, DRY_RUN)
                            db.log_signal(
                                pair=pair, direction=direction, fired=True,
                                price=price, rsi=sig["rsi"],
                                bb_pct=sig["bb_pct"], ema_gap=sig["ema_gap"],
                                atr_pct=sig["atr_pct"], vwap_dev=sig["vwap_dev"],
                                volume_ratio=sig["vol_ratio"],
                                btc_regime=btc_regime_label, session=session,
                            )
                            open_trades = db.get_open_trades()
                            open_pairs  = {t["pair"] for t in open_trades}

            # ── 5. Check daily loss limit ─────────────────────────────────────
            total_profit = sum(
                t.get("profit_usdt", 0) or 0
                for t in db.get_all_closed_trades()
                if t.get("exit_time", "")[:10] == now.date().isoformat()
            )
            wallet_now = wallet_start + total_profit
            check_daily_loss(wallet_start, wallet_now)

            # ── 6. Status log every loop ──────────────────────────────────────
            open_trades = db.get_open_trades()
            log.info(f"[Loop] BTC={btc_regime_label} | open={len(open_trades)}/{max_open} "
                     f"| session={session_label()} | wallet≈{wallet_now:.0f} USDT")

        except KeyboardInterrupt:
            log.info("Shutting down gracefully.")
            break
        except Exception as e:
            log.error(f"[Loop error] {e}", exc_info=True)

        # ── Sleep until next 5m candle close ─────────────────────────────────
        elapsed = time.time() - loop_start
        sleep_s = max(10, 300 - elapsed)   # 5 min - processing time, min 10s
        log.debug(f"Sleeping {sleep_s:.0f}s until next candle")
        time.sleep(sleep_s)


if __name__ == "__main__":
    run()
