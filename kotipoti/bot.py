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
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

import db
import telegram as tg

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_PATH = os.environ.get("BOT_LOG_PATH", "/data/bot.log")
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
except OSError:
    LOG_PATH = "/tmp/kotipoti_bot.log"
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a"),
    ]
)
log = logging.getLogger("kotipoti")

# ── Config ────────────────────────────────────────────────────────────────────
DRY_RUN   = os.environ.get("DRY_RUN", "true").lower() != "false"
API_KEY   = os.environ.get("BYBIT_API_KEY", "")
API_SECRET = os.environ.get("BYBIT_API_SECRET", "")
BOT_VARIANT = os.environ.get("BOT_VARIANT", "codex_v2_active")
DRY_RUN_WALLET = float(os.environ.get("DRY_RUN_WALLET", "5000"))

PAIRS = db.DEFAULT_PAIRS
ALT_PAIRS = {"ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"}
BTC_PAIR  = "BTC/USDT:USDT"
TIMEFRAME = "5m"
CANDLES_NEEDED = int(os.environ.get("CANDLES_NEEDED", "200"))
CANDLES_15M = int(os.environ.get("CANDLES_15M", "150"))
CANDLES_1H = int(os.environ.get("CANDLES_1H", "150"))
CANDLES_4H = int(os.environ.get("CANDLES_4H", "120"))
MAX_API_ERRORS_PER_HOUR = int(os.environ.get("MAX_API_ERRORS_PER_HOUR", "5"))

# ── Runtime state (in-memory) ─────────────────────────────────────────────────
_consecutive_losses: int = 0
_consec_loss_halt_until: Optional[datetime] = None
_daily_loss_halted: bool = False
_daily_loss_reset_day: Optional[int] = None
_pair_last_loss: Dict[str, datetime] = {}   # pair -> time of last losing exit
_api_error_times: List[datetime] = []
_bot_paused: bool = False
_bot_stopped: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_safety_state():
    global _consecutive_losses, _consec_loss_halt_until
    global _daily_loss_halted, _daily_loss_reset_day, _pair_last_loss
    try:
        raw = db.get_bot_state("safety_state", "{}")
        state = json.loads(raw) if raw else {}
        _consecutive_losses = int(state.get("consecutive_losses", 0))
        halt = state.get("consec_loss_halt_until")
        _consec_loss_halt_until = datetime.fromisoformat(halt) if halt else None
        _daily_loss_halted = bool(state.get("daily_loss_halted", False))
        _daily_loss_reset_day = state.get("daily_loss_reset_day")
        if _daily_loss_reset_day is not None:
            _daily_loss_reset_day = int(_daily_loss_reset_day)
        _pair_last_loss = {
            pair: datetime.fromisoformat(ts)
            for pair, ts in state.get("pair_last_loss", {}).items()
        }
    except Exception as e:
        log.warning(f"[Safety] Could not load persisted state: {e}")


def _save_safety_state():
    state = {
        "consecutive_losses": _consecutive_losses,
        "consec_loss_halt_until": _consec_loss_halt_until.isoformat()
        if _consec_loss_halt_until else None,
        "daily_loss_halted": _daily_loss_halted,
        "daily_loss_reset_day": _daily_loss_reset_day,
        "pair_last_loss": {
            pair: ts.isoformat() for pair, ts in _pair_last_loss.items()
        },
    }
    db.set_bot_state("safety_state", json.dumps(state))


def _prune_api_errors(now: datetime):
    global _api_error_times
    cutoff = now - timedelta(hours=1)
    _api_error_times = [t for t in _api_error_times if t > cutoff]


def _record_api_error():
    _api_error_times.append(_utc_now())


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
        _record_api_error()
        return None


# ── Funding rate fetch ────────────────────────────────────────────────────────

def fetch_funding_rate(exchange: ccxt.bybit, pair: str) -> Optional[float]:
    """
    Fetch the current funding rate for a perpetual futures pair.
    Returns the rate as a float (e.g. 0.0001 = 0.01%) or None on failure.
    Positive = longs pay shorts (crowded long).
    Negative = shorts pay longs (crowded short).
    """
    try:
        info = exchange.fetch_funding_rate(pair)
        rate = info.get("fundingRate") or info.get("funding_rate")
        return float(rate) if rate is not None else None
    except Exception as e:
        log.debug(f"fetch_funding_rate {pair}: {e}")
        return None


# ── Indicator computation ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, p: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Add all technical indicators to a 5m OHLCV dataframe."""
    p = p or db.get_all_params()

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


def candle_quality(df: pd.DataFrame, side: str, lookback: int = 5) -> Dict:
    """
    Analyse the last `lookback` candles for multi-candle signal quality.
    Returns a dict with quality flags and a human-readable reason if any fail.

    Checks:
      - rsi_fresh:      RSI is rising *into* this signal (not already exhausted).
                        For shorts: RSI now > RSI 2 candles ago (momentum building).
                        For longs:  RSI now < RSI 2 candles ago.
      - rsi_not_stale:  RSI hasn't been in the extreme zone for too many candles.
                        Stale = already overbought/oversold for 4+ consecutive candles.
      - body_strong:    Candle body is convincing.
                        Short: close is in the top 70% of the candle's high-low range.
                        Long:  close is in the bottom 30% (i.e. closed near the low).
                        Wait — for a LONG we want a bullish close, meaning close near
                        the HIGH of the candle. So: long close in top 30% of range.
      - vol_expanding:  Volume on the signal candle >= volume on the previous candle.
                        Confirms energy behind the move.
      - rsi_divergence: (bonus, not a hard filter — just a confidence booster)
                        Short: price made a higher high vs N candles ago but RSI did not
                               → bearish divergence, stronger short signal.
                        Long:  price made a lower low vs N candles ago but RSI did not
                               → bullish divergence, stronger long signal.

    Returns dict:
      {
        "rsi_fresh": bool,
        "rsi_not_stale": bool,
        "body_strong": bool,
        "vol_expanding": bool,
        "divergence": bool,       # bonus flag, not a hard gate
        "quality_ok": bool,       # True if all hard gates pass
        "skip_reason": str|None,  # first failing reason, or None
        "confidence_boost": float # 0.0 or 0.1 if divergence present
      }
    """
    if len(df) < lookback + 2:
        return {"quality_ok": True, "skip_reason": None,
                "rsi_fresh": True, "rsi_not_stale": True,
                "body_strong": True, "vol_expanding": True,
                "divergence": False, "confidence_boost": 0.0}

    cur   = df.iloc[-1]
    prev1 = df.iloc[-2]
    prev2 = df.iloc[-3]
    window = df.iloc[-(lookback + 1):-1]  # last `lookback` candles before current

    rsi_now   = float(cur.get("rsi",  50) or 50)
    rsi_prev2 = float(prev2.get("rsi", 50) or 50)

    # ── RSI freshness ─────────────────────────────────────────────────────────
    if side == "short":
        rsi_fresh = rsi_now > rsi_prev2          # RSI rising into overbought
    else:
        rsi_fresh = rsi_now < rsi_prev2          # RSI falling into oversold

    # ── RSI staleness — already been extreme for too long ────────────────────
    if side == "short":
        stale_threshold = 58.0
        stale_count = sum(1 for _, r in window.iterrows()
                          if (r.get("rsi") or 0) > stale_threshold)
    else:
        stale_threshold = 42.0
        stale_count = sum(1 for _, r in window.iterrows()
                          if (r.get("rsi") or 0) < stale_threshold)
    rsi_not_stale = stale_count < 4   # tolerate up to 3 candles, 4+ = exhausted

    # ── Candle body strength ──────────────────────────────────────────────────
    candle_range = float(cur["high"]) - float(cur["low"])
    if candle_range > 0:
        # Position of close within the high-low range (0 = at low, 1 = at high)
        close_position = (float(cur["close"]) - float(cur["low"])) / candle_range
        if side == "short":
            body_strong = close_position >= 0.60   # closed in upper 40% of range
        else:
            body_strong = close_position <= 0.40   # closed in lower 40% of range
    else:
        body_strong = True   # doji/flat candle — don't penalise

    # ── Volume expansion ──────────────────────────────────────────────────────
    vol_cur  = float(cur.get("volume",  0) or 0)
    vol_prev = float(prev1.get("volume", 0) or 0)
    vol_expanding = vol_cur >= vol_prev * 0.85   # allow 15% slack — not strict equality

    # ── Consecutive same-direction candles (extended move filter) ────────────
    # If the last 3 candles all closed in the direction of our trade,
    # the move is already extended — we're entering late, not fresh.
    # Short: 3 consecutive bearish closes (close < open) = already been selling
    # Long:  3 consecutive bullish closes (close > open) = already been buying
    try:
        last3 = df.iloc[-4:-1]   # 3 candles before the signal candle
        if side == "short":
            # 3 consecutive green candles into our short = extended upside, good for short entry
            # BUT if last 3 are all red (selling already done) = move is exhausted
            consec_against = all(float(r["close"]) < float(r["open"]) for _, r in last3.iterrows())
        else:
            # 3 consecutive red candles into our long = move already extended down, good for long
            # BUT if last 3 are all green (buying already done) = extended, skip
            consec_against = all(float(r["close"]) > float(r["open"]) for _, r in last3.iterrows())
        not_extended = not consec_against
    except Exception:
        not_extended = True

    # ── RSI divergence (bonus) ────────────────────────────────────────────────
    divergence = False
    try:
        lookback_row = df.iloc[-(lookback + 1)]
        if side == "short":
            price_hh = float(cur["high"]) > float(lookback_row["high"])
            rsi_hh   = rsi_now > float(lookback_row.get("rsi") or rsi_now)
            divergence = price_hh and not rsi_hh   # price new high, RSI didn't confirm
        else:
            price_ll = float(cur["low"]) < float(lookback_row["low"])
            rsi_ll   = rsi_now < float(lookback_row.get("rsi") or rsi_now)
            divergence = price_ll and not rsi_ll   # price new low, RSI didn't confirm
    except Exception:
        divergence = False

    # ── Aggregate ─────────────────────────────────────────────────────────────
    skip_reason = None
    if not rsi_fresh:
        skip_reason = "rsi_not_fresh"
    elif not rsi_not_stale:
        skip_reason = "rsi_stale"
    elif not body_strong:
        skip_reason = "weak_candle_body"
    elif not vol_expanding:
        skip_reason = "volume_not_expanding"
    elif not not_extended:
        skip_reason = "move_already_extended"

    quality_ok = skip_reason is None

    return {
        "rsi_fresh":        rsi_fresh,
        "rsi_not_stale":    rsi_not_stale,
        "body_strong":      body_strong,
        "vol_expanding":    vol_expanding,
        "not_extended":     not_extended,
        "divergence":       divergence,
        "quality_ok":       quality_ok,
        "skip_reason":      skip_reason,
        "confidence_boost": 0.1 if divergence else 0.0,
    }


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


def _decision_snapshot(df: pd.DataFrame) -> Dict:
    row = df.iloc[-1]
    close = float(row["close"])
    upper = float(row.get("bb_upper", 0) or 0)
    lower = float(row.get("bb_lower", 0) or 0)
    return {
        "candle_ts": str(row.get("date", "")),
        "price": close,
        "rsi": float(row.get("rsi", 0) or 0),
        "atr_pct": float(row.get("atr_pct", 0) or 0),
        "vwap_dev": float(row.get("vwap_dev", 0) or 0),
        "volume_ratio": float(row.get("volume_ratio", 0) or 0),
        "bb_position": "above" if upper and close > upper else "below" if lower and close < lower else "inside",
        "ema_trend": "bear" if float(row.get("ema_fast", 0) or 0) < float(row.get("ema_slow", 0) or 0) else "bull",
    }


def log_decision_snapshot(pair: str, df: pd.DataFrame, p: Dict[str, str],
                          btc_regime_label: str, pair_15m_regime_label: str,
                          pair_1h_regime_label: str, pair_4h_regime_label: str,
                          signal_type: str, fired: bool = False,
                          skip_reason: str = None, context: dict = None):
    snap = _decision_snapshot(df)
    db.log_decision(
        pair=pair,
        candle_ts=snap["candle_ts"],
        price=snap["price"],
        rsi=snap["rsi"],
        atr_pct=snap["atr_pct"],
        vwap_dev=snap["vwap_dev"],
        volume_ratio=snap["volume_ratio"],
        bb_position=snap["bb_position"],
        ema_trend=snap["ema_trend"],
        btc_1h_regime=btc_regime_label,
        pair_15m_regime=pair_15m_regime_label,
        pair_1h_regime=pair_1h_regime_label,
        pair_4h_regime=pair_4h_regime_label,
        signal_type=signal_type,
        fired=fired,
        skip_reason=skip_reason,
        params_snapshot=p,
        context=context,
    )


def diagnose_no_signal(pair: str, df5m: pd.DataFrame, p: Dict[str, str],
                       btc_regime_label: str, pair_regime_label: str,
                       pair_15m_regime_label: str,
                       pair_4h_regime_label: str) -> Tuple[str, Dict]:
    """Explain which gates kept the latest candle out of a trade."""
    row = df5m.iloc[-1]
    close = float(row.get("close", 0) or 0)
    rsi = float(row.get("rsi", 0) or 0)
    bb_upper = float(row.get("bb_upper", 0) or 0)
    bb_lower = float(row.get("bb_lower", 0) or 0)
    atr_pct = float(row.get("atr_pct", 0) or 0)
    vwap_dev = float(row.get("vwap_dev", 0) or 0)
    vol_ratio = float(row.get("volume_ratio", 0) or 0)

    rsi_short = float(p.get("rsi_short_entry", "64"))
    rsi_long = float(p.get("rsi_long_entry", "36"))
    vwap_rsi_short = float(p.get("vwap_rsi_short", "55"))
    vwap_rsi_long = float(p.get("vwap_rsi_long", "45"))
    vol_mult = float(p.get("volume_multiplier", "0.8"))
    atr_min = float(p.get("atr_min_pct", "0.1"))
    atr_max = float(p.get("atr_max_pct", "6.0"))
    vwap_min = float(p.get("vwap_dev_min", "0.25"))

    reasons = []
    candidates = []
    if atr_pct < atr_min:
        reasons.append("atr_too_low")
    elif atr_pct > atr_max:
        reasons.append("atr_too_high")

    if vol_ratio < vol_mult:
        reasons.append("volume_low")

    if bb_upper and close > bb_upper:
        candidates.append("bb_short")
        if rsi <= rsi_short:
            reasons.append("bb_short_rsi_not_high_enough")
    elif bb_lower and close < bb_lower:
        candidates.append("bb_long")
        if rsi >= rsi_long:
            reasons.append("bb_long_rsi_not_low_enough")
    else:
        reasons.append("inside_bollinger_bands")

    if vwap_dev > vwap_min:
        candidates.append("vwap_short")
        if rsi <= vwap_rsi_short:
            reasons.append("vwap_short_rsi_not_high_enough")
    elif vwap_dev < -vwap_min:
        candidates.append("vwap_long")
        if rsi >= vwap_rsi_long:
            reasons.append("vwap_long_rsi_not_low_enough")
    else:
        reasons.append("vwap_deviation_too_small")

    if pair in ALT_PAIRS and btc_regime_label in {"bull", "bear"}:
        reasons.append(f"btc_{btc_regime_label}_context")
    if pair_regime_label in {"bull", "bear"}:
        reasons.append(f"pair_1h_{pair_regime_label}_context")
    if pair_15m_regime_label == pair_4h_regime_label and pair_15m_regime_label in {"bull", "bear"}:
        reasons.append(f"multi_tf_{pair_15m_regime_label}_context")

    unique_reasons = list(dict.fromkeys(reasons)) or ["thresholds_not_met"]
    context = {
        "profile": p.get("signal_profile", "active_dry_run"),
        "confirmation_mode": p.get("confirmation_mode", "soft"),
        "candidate_setups": candidates,
        "thresholds": {
            "rsi_short_entry": rsi_short,
            "rsi_long_entry": rsi_long,
            "vwap_rsi_short": vwap_rsi_short,
            "vwap_rsi_long": vwap_rsi_long,
            "volume_multiplier": vol_mult,
            "atr_min_pct": atr_min,
            "atr_max_pct": atr_max,
            "vwap_dev_min": vwap_min,
        },
    }
    return ",".join(unique_reasons[:5]), context


# ── Signal evaluation ─────────────────────────────────────────────────────────

def evaluate_signals(pair: str, df5m: pd.DataFrame,
                     btc_regime_label: str,
                     pair_regime_label: str,
                     pair_15m_regime_label: str = "unknown",
                     pair_4h_regime_label: str = "unknown",
                     p: Optional[Dict[str, str]] = None) -> List[Dict]:
    """
    Returns list of signal dicts with keys:
      direction, entry_tag, price, confidence (0-1)
    Empty list if no signal.
    """
    p      = p or db.get_all_params()
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

    rsi_short      = float(p.get("rsi_short_entry", "64"))
    rsi_long       = float(p.get("rsi_long_entry",  "36"))
    vol_mult       = float(p.get("volume_multiplier", "0.8"))
    atr_min        = float(p.get("atr_min_pct", "0.1"))
    atr_max        = float(p.get("atr_max_pct", "6.0"))
    vwap_min       = float(p.get("vwap_dev_min", "0.25"))
    vwap_rsi_short = float(p.get("vwap_rsi_short", "55"))
    vwap_rsi_long  = float(p.get("vwap_rsi_long", "45"))
    confirmation_mode = p.get("confirmation_mode", "soft")
    soft_confirm = confirmation_mode == "soft"

    if rsi is None or bb_upper is None:
        return []

    # ── ATR gate (applies to all signals) ────────────────────────────────────
    if atr_pct < atr_min:
        log.debug(f"[Signal] {pair} ATR {atr_pct:.2f}% < min {atr_min} — too quiet")
        return []
    if atr_pct > atr_max:
        log.debug(f"[Signal] {pair} ATR {atr_pct:.2f}% > max {atr_max} — too wild")
        return []

    # ── Volume gate ───────────────────────────────────────────────────────────
    vol_ok = vol_ratio >= vol_mult

    # ── Multi-candle quality (computed once, reused per signal) ──────────────
    cq_short = candle_quality(df5m, "short")
    cq_long  = candle_quality(df5m, "long")

    # Log current indicator snapshot every candle (INFO so it's always visible)
    log.info(f"[Indicators] {pair} | close={close:.4f} | rsi={rsi:.1f} | "
             f"atr={atr_pct:.2f}% | vwap_dev={vwap_dev:+.2f}% | "
             f"vol={vol_ratio:.2f}x | bb_pos={'above' if close>bb_upper else 'below' if close<bb_lower else 'inside'} | "
             f"ema={'bear' if ema_f<ema_s else 'bull'}")

    # ── BB breakout SHORT ─────────────────────────────────────────────────────
    # EMA cross removed — BB+RSI is already directional enough for scalping
    bb_short_ok  = close > bb_upper
    rsi_short_ok = rsi > rsi_short

    if bb_short_ok and rsi_short_ok and vol_ok:
        if not cq_short["quality_ok"]:
            log.info(f"[Signal] ⛔ bb_short {pair} quality filter: {cq_short['skip_reason']} "
                     f"(rsi_fresh={cq_short['rsi_fresh']} stale={not cq_short['rsi_not_stale']} "
                     f"body={cq_short['body_strong']} vol_exp={cq_short['vol_expanding']})")
            db.log_signal(pair, "short", fired=False, skip_reason=cq_short["skip_reason"],
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
        else:
            overextended = rsi > 80
            trend_ok = not (pair_regime_label == "bull" and not overextended)
            raw_confirm_ok = not (
                pair_15m_regime_label == "bull" and pair_4h_regime_label == "bull"
                and not overextended
            )
            confirm_ok = raw_confirm_ok or soft_confirm
            btc_ok = True
            if pair in ALT_PAIRS:
                btc_ok = not (btc_regime_label == "bull" and not overextended)

            if trend_ok and confirm_ok and btc_ok:
                bb_pct = (close - bb_upper) / bb_upper * 100
                ema_gap = (ema_f - ema_s) / close * 100
                div_tag = " 📉divergence" if cq_short["divergence"] else ""
                log.info(f"[Signal] ✅ bb_short {pair} rsi={rsi:.1f} bb_pct={bb_pct:.2f}% "
                         f"vol={vol_ratio:.2f}x{div_tag}")
                signals.append({
                    "direction": "short", "entry_tag": "bb_short",
                    "price": close, "rsi": rsi,
                    "bb_pct": round(bb_pct, 3), "ema_gap": round(ema_gap, 4),
                    "atr_pct": round(atr_pct, 3), "vwap_dev": round(vwap_dev, 3),
                    "vol_ratio": round(vol_ratio, 2), "session": session, "skip_reason": None,
                    "confirmation_softened": not raw_confirm_ok and soft_confirm,
                    "divergence": cq_short["divergence"],
                })
            else:
                reason = "trend_filter" if not trend_ok else "confirmation_filter" if not confirm_ok else "btc_filter"
                log.info(f"[Signal] ⛔ bb_short {pair} blocked: {reason}")
                db.log_signal(pair, "short", fired=False, skip_reason=reason,
                              price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
    elif bb_short_ok:
        miss = []
        if not rsi_short_ok: miss.append(f"rsi={rsi:.1f}<{rsi_short}")
        if not vol_ok:        miss.append(f"vol={vol_ratio:.2f}x<{vol_mult}x")
        log.info(f"[Signal] 〰 bb_short {pair} almost — missing: {', '.join(miss)}")

    # ── BB breakout LONG ──────────────────────────────────────────────────────
    bb_long_ok  = close < bb_lower
    rsi_long_ok = rsi < rsi_long

    if bb_long_ok and rsi_long_ok and vol_ok:
        if not cq_long["quality_ok"]:
            log.info(f"[Signal] ⛔ bb_long {pair} quality filter: {cq_long['skip_reason']} "
                     f"(rsi_fresh={cq_long['rsi_fresh']} stale={not cq_long['rsi_not_stale']} "
                     f"body={cq_long['body_strong']} vol_exp={cq_long['vol_expanding']})")
            db.log_signal(pair, "long", fired=False, skip_reason=cq_long["skip_reason"],
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
        else:
            overextended = rsi < 20
            trend_ok = not (pair_regime_label == "bear" and not overextended)
            raw_confirm_ok = not (
                pair_15m_regime_label == "bear" and pair_4h_regime_label == "bear"
                and not overextended
            )
            confirm_ok = raw_confirm_ok or soft_confirm
            btc_ok = True
            if pair in ALT_PAIRS:
                btc_ok = not (btc_regime_label == "bear" and not overextended)

            if trend_ok and confirm_ok and btc_ok:
                bb_pct = (bb_lower - close) / bb_lower * 100
                ema_gap = (ema_f - ema_s) / close * 100
                div_tag = " 📈divergence" if cq_long["divergence"] else ""
                log.info(f"[Signal] ✅ bb_long {pair} rsi={rsi:.1f} bb_pct={bb_pct:.2f}% "
                         f"vol={vol_ratio:.2f}x{div_tag}")
                signals.append({
                    "direction": "long", "entry_tag": "bb_long",
                    "price": close, "rsi": rsi,
                    "bb_pct": round(bb_pct, 3), "ema_gap": round(ema_gap, 4),
                    "atr_pct": round(atr_pct, 3), "vwap_dev": round(vwap_dev, 3),
                    "vol_ratio": round(vol_ratio, 2), "session": session, "skip_reason": None,
                    "confirmation_softened": not raw_confirm_ok and soft_confirm,
                    "divergence": cq_long["divergence"],
                })
            else:
                reason = "trend_filter" if not trend_ok else "confirmation_filter" if not confirm_ok else "btc_filter"
                log.info(f"[Signal] ⛔ bb_long {pair} blocked: {reason}")
                db.log_signal(pair, "long", fired=False, skip_reason=reason,
                              price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
    elif bb_long_ok:
        miss = []
        if not rsi_long_ok: miss.append(f"rsi={rsi:.1f}>{rsi_long}")
        if not vol_ok:       miss.append(f"vol={vol_ratio:.2f}x<{vol_mult}x")
        log.info(f"[Signal] 〰 bb_long {pair} almost — missing: {', '.join(miss)}")

    # ── VWAP reversion SHORT ──────────────────────────────────────────────────
    # vol_mult (not 1.3x) — VWAP signal doesn't need as extreme a volume spike
    if vwap_dev > vwap_min and vol_ratio >= vol_mult and rsi > vwap_rsi_short:
        if not cq_short["quality_ok"]:
            log.info(f"[Signal] ⛔ vwap_short {pair} quality filter: {cq_short['skip_reason']}")
            db.log_signal(pair, "short", fired=False, skip_reason=cq_short["skip_reason"],
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
        else:
            raw_trend_ok = pair_regime_label != "bull" and pair_15m_regime_label != "bull"
            trend_ok = raw_trend_ok or soft_confirm
            btc_ok   = btc_regime_label != "bull" if pair in ALT_PAIRS else True
            if trend_ok and btc_ok:
                div_tag = " 📉divergence" if cq_short["divergence"] else ""
                log.info(f"[Signal] ✅ vwap_short {pair} vwap_dev={vwap_dev:+.2f}% rsi={rsi:.1f}{div_tag}")
                signals.append({
                    "direction": "short", "entry_tag": "vwap_short",
                    "price": close, "rsi": rsi,
                    "bb_pct": round((close - bb_upper) / bb_upper * 100 if bb_upper else 0, 3),
                    "ema_gap": round((ema_f - ema_s) / close * 100 if ema_f and ema_s else 0, 4),
                    "atr_pct": round(atr_pct, 3), "vwap_dev": round(vwap_dev, 3),
                    "vol_ratio": round(vol_ratio, 2), "session": session, "skip_reason": None,
                    "confirmation_softened": not raw_trend_ok and soft_confirm,
                    "divergence": cq_short["divergence"],
                })

    # ── VWAP reversion LONG ───────────────────────────────────────────────────
    if vwap_dev < -vwap_min and vol_ratio >= vol_mult and rsi < vwap_rsi_long:
        if not cq_long["quality_ok"]:
            log.info(f"[Signal] ⛔ vwap_long {pair} quality filter: {cq_long['skip_reason']}")
            db.log_signal(pair, "long", fired=False, skip_reason=cq_long["skip_reason"],
                          price=close, rsi=rsi, btc_regime=btc_regime_label, session=session)
        else:
            raw_trend_ok = pair_regime_label != "bear" and pair_15m_regime_label != "bear"
            trend_ok = raw_trend_ok or soft_confirm
            btc_ok   = btc_regime_label != "bear" if pair in ALT_PAIRS else True
            if trend_ok and btc_ok:
                div_tag = " 📈divergence" if cq_long["divergence"] else ""
                log.info(f"[Signal] ✅ vwap_long {pair} vwap_dev={vwap_dev:+.2f}% rsi={rsi:.1f}{div_tag}")
                signals.append({
                    "direction": "long", "entry_tag": "vwap_long",
                    "price": close, "rsi": rsi,
                    "bb_pct": round((bb_lower - close) / bb_lower * 100 if bb_lower else 0, 3),
                    "ema_gap": round((ema_f - ema_s) / close * 100 if ema_f and ema_s else 0, 4),
                    "atr_pct": round(atr_pct, 3), "vwap_dev": round(vwap_dev, 3),
                    "vol_ratio": round(vol_ratio, 2), "session": session, "skip_reason": None,
                    "confirmation_softened": not raw_trend_ok and soft_confirm,
                    "divergence": cq_long["divergence"],
                })

    return signals


# ── Exit evaluation ───────────────────────────────────────────────────────────

def check_exits(open_trades: List[Dict], df5m: Dict[str, pd.DataFrame],
                current_prices: Dict[str, float]) -> List[Tuple[int, float, str]]:
    """
    Returns list of (trade_id, exit_price, reason) for trades to close.
    """
    p = db.get_all_params()
    sl_pct       = float(p.get("stoploss_pct",    "1.2")) / 100
    trail_pct    = float(p.get("trailing_pct",    "1.0")) / 100
    trail_offset = float(p.get("trailing_offset", "1.5")) / 100
    rsi_exit_short = float(p.get("rsi_exit_short", "45"))
    rsi_exit_long  = float(p.get("rsi_exit_long",  "55"))
    max_hold_hours = float(p.get("max_hold_hours", "6.0"))

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

        peak = max(float(trade.get("peak_price") or entry_price), current)
        trough = min(float(trade.get("trough_price") or entry_price), current)
        if peak != trade.get("peak_price") or trough != trade.get("trough_price"):
            db.update_trade_extremes(trade_id, peak, trough)

        # Hard stoploss
        if profit_pct <= -sl_pct:
            exits.append((trade_id, current, "stoploss"))
            continue

        # Trailing stop: track max favorable excursion since entry.
        if side == "long":
            best_profit_pct = (peak - entry_price) / entry_price
            trail_price = peak * (1 - trail_pct)
            if best_profit_pct >= trail_offset and current <= trail_price:
                exits.append((trade_id, current, "trailing_stop"))
                continue
        else:
            best_profit_pct = (entry_price - trough) / entry_price
            trail_price = trough * (1 + trail_pct)
            if best_profit_pct >= trail_offset and current >= trail_price:
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

        # Max hold time — cut the trade regardless of P&L to avoid trades
        # drifting open for days. Prefer a small loss over an uncapped one.
        if mins >= max_hold_hours * 60:
            exits.append((trade_id, current, "max_hold"))
            continue

    return exits


# ── Safety gates ─────────────────────────────────────────────────────────────

def safety_ok(pair: str, current_time: datetime) -> Tuple[bool, str]:
    global _consecutive_losses, _consec_loss_halt_until
    global _daily_loss_halted, _daily_loss_reset_day, _pair_last_loss

    p = db.get_all_params()
    max_consec   = int(p.get("max_consec_losses", "4"))
    cooldown_min = int(p.get("pair_cooldown_min", "15"))
    _prune_api_errors(current_time.astimezone(timezone.utc) if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc))

    utc = current_time.replace(tzinfo=timezone.utc) if current_time.tzinfo is None \
          else current_time.astimezone(timezone.utc)

    # Daily loss reset at UTC midnight
    today = utc.date().toordinal()
    if _daily_loss_reset_day != today:
        _daily_loss_reset_day = today
        _daily_loss_halted = False
        _save_safety_state()

    if _daily_loss_halted:
        return False, "daily_loss_halt"

    if len(_api_error_times) >= MAX_API_ERRORS_PER_HOUR:
        return False, f"api_error_limit_{MAX_API_ERRORS_PER_HOUR}_per_hour"

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
            _save_safety_state()

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
        _save_safety_state()
    else:
        _consecutive_losses = 0
        _save_safety_state()


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
            _save_safety_state()


# ── Order placement ───────────────────────────────────────────────────────────

def _position_amount(exchange: ccxt.bybit, pair: str, stake_usdt: float,
                     leverage: int, current_price: float) -> float:
    raw_amount = stake_usdt * leverage / current_price
    if DRY_RUN:
        return round(raw_amount, 6)
    try:
        exchange.load_markets()
        market = exchange.market(pair)
        min_amount = (market.get("limits", {}).get("amount", {}) or {}).get("min")
        min_cost = (market.get("limits", {}).get("cost", {}) or {}).get("min")
        amount = float(exchange.amount_to_precision(pair, raw_amount))
    except Exception:
        # Fall back to a conservative decimal trim if ccxt metadata is unavailable.
        amount = float(Decimal(str(raw_amount)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN))
        min_amount = None
        min_cost = None
    if min_amount and amount < float(min_amount):
        raise ValueError(f"amount {amount} below exchange minimum {min_amount}")
    if min_cost and amount * current_price < float(min_cost):
        raise ValueError(f"notional {amount * current_price:.4f} below exchange minimum {min_cost}")
    return amount


def _stop_price(side: str, entry_price: float, stoploss_pct: float) -> float:
    if side == "long":
        return entry_price * (1 - stoploss_pct / 100)
    return entry_price * (1 + stoploss_pct / 100)


def _atr_stop_price(side: str, entry_price: float, atr: float,
                    atr_multiplier: float = 1.5) -> float:
    """
    Compute a dynamic ATR-based stop price.
    Longs: stop = entry - (atr * multiplier)
    Shorts: stop = entry + (atr * multiplier)
    Falls back to a fixed 1.5% stop if ATR is zero/None.
    """
    if not atr or atr <= 0:
        # fallback to 1.5% fixed
        return _stop_price(side, entry_price, 1.5)
    offset = atr * atr_multiplier
    if side == "long":
        return entry_price - offset
    return entry_price + offset


def _risk_based_stake(entry_price: float, stop_price: float,
                      risk_usdt: float, leverage: int,
                      max_stake: float) -> float:
    """
    Calculate stake so that hitting the stop costs exactly risk_usdt.
    stake = risk_usdt / (|entry - stop| / entry)
    Capped at max_stake to avoid oversizing.
    """
    if entry_price <= 0 or stop_price <= 0:
        return max_stake
    pct_risk = abs(entry_price - stop_price) / entry_price
    if pct_risk <= 0:
        return max_stake
    # Without leverage: stake * pct_risk = risk_usdt
    # With leverage: effective loss = stake * pct_risk * leverage
    stake = risk_usdt / (pct_risk * leverage)
    return min(stake, max_stake)


def place_exchange_stop(exchange: ccxt.bybit, pair: str, side: str,
                        amount: float, entry_price: float) -> Optional[str]:
    """Place a reduce-only conditional stop order in live mode."""
    if DRY_RUN:
        return f"dry_stop_{int(time.time())}"
    p = db.get_all_params()
    stoploss_pct = float(p.get("stoploss_pct", "2.5"))
    stop_price = _stop_price(side, entry_price, stoploss_pct)
    try:
        stop_price = float(exchange.price_to_precision(pair, stop_price))
    except Exception:
        stop_price = round(stop_price, 4)

    close_side = "sell" if side == "long" else "buy"
    try:
        order = exchange.create_order(
            symbol=pair,
            type="market",
            side=close_side,
            amount=amount,
            price=None,
            params={
                "triggerPrice": stop_price,
                "reduceOnly": True,
                "positionIdx": 1 if side == "long" else 2,
                "triggerDirection": 2 if side == "long" else 1,
            },
        )
        stop_id = order.get("id")
        log.info(f"[Order] STOP {side.upper()} {pair} trigger={stop_price:.4f} | id={stop_id}")
        return stop_id
    except Exception as e:
        log.error(f"[Order] Failed to place protective stop for {side} {pair}: {e}")
        _record_api_error()
        return None


def place_order(exchange: ccxt.bybit, pair: str, side: str,
                stake_usdt: float, leverage: int,
                current_price: float) -> Optional[Dict]:
    """Place a market order. Returns order dict or None on failure."""
    try:
        amount = _position_amount(exchange, pair, stake_usdt, leverage, current_price)
    except Exception as e:
        log.error(f"[Order] Invalid order size for {side} {pair}: {e}")
        return None
    if DRY_RUN:
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
        _record_api_error()
        return None


def close_order(exchange: ccxt.bybit, pair: str, side: str, amount: float,
                current_price: float, reason: str, stop_order_id: str = "") -> bool:
    """Close an open position with a market order."""
    if DRY_RUN:
        log.info(f"[DryRun] CLOSE {side.upper()} {pair} @ {current_price:.4f} ({reason})")
        return True
    try:
        if stop_order_id:
            try:
                exchange.cancel_order(stop_order_id, pair)
                log.info(f"[Order] Cancelled protective stop {stop_order_id} for {pair}")
            except Exception as e:
                log.warning(f"[Order] Could not cancel protective stop {stop_order_id}: {e}")
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
        return True
    except Exception as e:
        log.error(f"[Order] Failed to close {side} on {pair}: {e}")
        _record_api_error()
        return False


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
                close_ok = close_order(exchange, trade["pair"], trade["side"],
                                       trade["amount"], price, "admin_force_close",
                                       trade.get("stop_order_id") or "")
                if not close_ok:
                    db.ack_command(cmd_id, "error",
                                   f"Exchange close failed for trade {trade_id}; DB left open")
                    continue
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
    _load_safety_state()
    exchange = make_exchange()
    active_pairs = db.get_active_pairs()
    tg.bot_status("started", f"DRY_RUN={DRY_RUN} | Pairs: {', '.join(active_pairs)}")

    wallet_start = float(db.get_param("wallet_start", "0") or "0")
    if wallet_start == 0:
        wallet_start = DRY_RUN_WALLET
        db.set_param("wallet_start", str(wallet_start))

    last_candle_time: Dict[str, str] = {}  # pair -> last processed candle timestamp

    log.info(f"Pairs: {active_pairs}")
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
            p = db.get_all_params()
            active_pairs = db.get_active_pairs()
            fetch_pairs = active_pairs if BTC_PAIR in active_pairs else [BTC_PAIR] + active_pairs

            # ── 1. Fetch BTC 1h for regime ────────────────────────────────────
            btc_1h_df = fetch_ohlcv(exchange, BTC_PAIR, "1h", limit=CANDLES_1H)
            if btc_1h_df is not None:
                btc_1h_df = compute_1h_indicators(btc_1h_df)
            btc_regime_label = btc_regime(btc_1h_df)

            # ── 2. Fetch and cache candles + funding rates ────────────────────
            df5m: Dict[str, pd.DataFrame] = {}
            df15m: Dict[str, pd.DataFrame] = {}
            df1h: Dict[str, pd.DataFrame] = {}
            df4h: Dict[str, pd.DataFrame] = {}
            current_prices: Dict[str, float] = {}
            funding_rates: Dict[str, Optional[float]] = {}
            new_candle_pairs = set()

            for pair in fetch_pairs:
                df = fetch_ohlcv(exchange, pair, TIMEFRAME, limit=CANDLES_NEEDED)
                if df is None or len(df) < 30:
                    log.warning(f"Insufficient candles for {pair}, skipping")
                    continue
                df = compute_indicators(df, p)
                df5m[pair] = df
                current_prices[pair] = float(df.iloc[-1]["close"])

                df_15 = fetch_ohlcv(exchange, pair, "15m", limit=CANDLES_15M)
                if df_15 is not None and len(df_15) >= 30:
                    df15m[pair] = compute_1h_indicators(df_15)

                df_1h = btc_1h_df if pair == BTC_PAIR else fetch_ohlcv(exchange, pair, "1h", limit=CANDLES_1H)
                if df_1h is not None and len(df_1h) >= 30:
                    df1h[pair] = compute_1h_indicators(df_1h) if pair != BTC_PAIR else df_1h

                df_4h = fetch_ohlcv(exchange, pair, "4h", limit=CANDLES_4H)
                if df_4h is not None and len(df_4h) >= 30:
                    df4h[pair] = compute_1h_indicators(df_4h)

                # Fetch funding rate (only for non-BTC pairs to save API calls)
                if pair != BTC_PAIR:
                    funding_rates[pair] = fetch_funding_rate(exchange, pair)

                # Check if this is a new candle
                last_ts = str(df.iloc[-1]["date"])
                is_new  = last_candle_time.get(pair) != last_ts
                last_candle_time[pair] = last_ts
                if is_new:
                    new_candle_pairs.add(pair)
                    log.debug(f"New candle {pair} @ {last_ts} | close={current_prices[pair]:.4f}")

            # ── 3. Manage open positions ──────────────────────────────────────
            open_trades = db.get_open_trades()
            exits = check_exits(open_trades, df5m, current_prices)

            for trade_id, exit_price, reason in exits:
                trade = next((t for t in open_trades if t["id"] == trade_id), None)
                if not trade:
                    continue
                close_ok = close_order(exchange, trade["pair"], trade["side"],
                                       trade["amount"], exit_price, reason,
                                       trade.get("stop_order_id") or "")
                if not close_ok:
                    log.error(f"[Order] Trade #{trade_id} remains open because exchange close failed")
                    continue
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
            max_open  = int(p.get("max_open_trades", "3"))
            stake     = float(p.get("stake_usdt", "200"))
            leverage  = int(p.get("leverage", "5"))

            if len(open_trades) < max_open:
                for pair in active_pairs:
                    if pair not in df5m:
                        continue
                    if pair not in new_candle_pairs:
                        continue

                    df = df5m[pair]
                    p_regime = pair_1h_regime(df1h.get(pair))
                    p15_regime = pair_1h_regime(df15m.get(pair))
                    p4_regime = pair_1h_regime(df4h.get(pair))

                    # Skip if already have an open trade on this pair
                    if pair in open_pairs:
                        log_decision_snapshot(
                            pair, df, p, btc_regime_label, p15_regime, p_regime,
                            p4_regime, "open_position", False, "open_position"
                        )
                        continue

                    # Safety gate
                    ok, reason = safety_ok(pair, now)
                    if not ok:
                        log.debug(f"[Safety] {pair} blocked: {reason}")
                        log_decision_snapshot(
                            pair, df, p, btc_regime_label, p15_regime, p_regime,
                            p4_regime, "safety_block", False, reason
                        )
                        continue

                    # Check if this is a new candle for this pair
                    last_ts = str(df.iloc[-1]["date"])
                    # (already checked above — if we're here it was new)

                    sigs = evaluate_signals(pair, df, btc_regime_label, p_regime,
                                            p15_regime, p4_regime, p)
                    if not sigs:
                        skip_reason, no_signal_context = diagnose_no_signal(
                            pair, df, p, btc_regime_label, p_regime,
                            p15_regime, p4_regime
                        )
                        log_decision_snapshot(
                            pair, df, p, btc_regime_label, p15_regime, p_regime,
                            p4_regime, "no_signal", False, skip_reason,
                            no_signal_context
                        )
                    for sig in sigs:
                        if len(open_trades) >= max_open:
                            break

                        direction  = sig["direction"]
                        entry_tag  = sig["entry_tag"]
                        price      = sig["price"]
                        session    = sig["session"]

                        # ── Funding rate filter ───────────────────────────────
                        funding_rate = funding_rates.get(pair)
                        funding_threshold = float(p.get("funding_rate_threshold", "0.0005"))
                        if funding_rate is not None:
                            if direction == "long" and funding_rate > funding_threshold:
                                log.info(f"[Signal] ⛔ {entry_tag} {pair} LONG skipped: "
                                         f"funding={funding_rate:.4%} > {funding_threshold:.4%} "
                                         f"(crowded long)")
                                db.log_signal(pair, direction, fired=False,
                                              skip_reason="funding_rate_crowded_long",
                                              price=price, rsi=sig["rsi"],
                                              btc_regime=btc_regime_label, session=session)
                                continue
                            if direction == "short" and funding_rate < -funding_threshold:
                                log.info(f"[Signal] ⛔ {entry_tag} {pair} SHORT skipped: "
                                         f"funding={funding_rate:.4%} < -{funding_threshold:.4%} "
                                         f"(crowded short)")
                                db.log_signal(pair, direction, fired=False,
                                              skip_reason="funding_rate_crowded_short",
                                              price=price, rsi=sig["rsi"],
                                              btc_regime=btc_regime_label, session=session)
                                continue

                        # ── ATR-based dynamic stop price ──────────────────────
                        atr_val = float(df.iloc[-1].get("atr") or 0)
                        atr_multiplier = float(p.get("atr_stop_multiplier", "1.5"))
                        stop_px = _atr_stop_price(direction, price, atr_val, atr_multiplier)
                        stop_pct = abs(price - stop_px) / price * 100

                        # ── Risk-based position sizing ────────────────────────
                        risk_usdt = float(p.get("risk_per_trade_usdt", "50"))
                        actual_stake = _risk_based_stake(price, stop_px, risk_usdt,
                                                         leverage, stake)

                        log.info(f"🔔 SIGNAL {direction.upper()} {pair} "
                                 f"| tag={entry_tag} | price={price:.4f} "
                                 f"| rsi={sig['rsi']:.1f} | atr={sig['atr_pct']:.2f}% "
                                 f"| stop={stop_px:.4f} ({stop_pct:.2f}%) "
                                 f"| stake={actual_stake:.1f} USDT (risk={risk_usdt} USDT) "
                                 f"| funding={funding_rate:.4%}" if funding_rate is not None
                                 else f"🔔 SIGNAL {direction.upper()} {pair} "
                                 f"| tag={entry_tag} | price={price:.4f} "
                                 f"| rsi={sig['rsi']:.1f} | atr={sig['atr_pct']:.2f}% "
                                 f"| stop={stop_px:.4f} ({stop_pct:.2f}%) "
                                 f"| stake={actual_stake:.1f} USDT (risk={risk_usdt} USDT) "
                                 f"| regime=BTC:{btc_regime_label} pair:{p_regime}")

                        order = place_order(exchange, pair, direction,
                                            actual_stake, leverage, price)
                        if order:
                            stop_id = place_exchange_stop(
                                exchange, pair, direction,
                                float(order.get("amount", 0) or 0), price
                            )
                            if not stop_id:
                                close_order(exchange, pair, direction,
                                            float(order.get("amount", 0) or 0),
                                            price, "protective_stop_failed")
                                log.error(f"[Order] Entry on {pair} immediately closed because protective stop failed")
                                log_decision_snapshot(
                                    pair, df, p, btc_regime_label, p15_regime,
                                    p_regime, p4_regime, entry_tag, False,
                                    "protective_stop_failed", {"signal": sig}
                                )
                                continue
                            trade_id = db.open_trade(
                                pair=pair,
                                side=direction,
                                entry_price=price,
                                amount=order.get("amount", 0),
                                stake_usdt=actual_stake,
                                leverage=leverage,
                                entry_tag=entry_tag,
                                session=session,
                                btc_regime=btc_regime_label,
                                atr_at_entry=sig["atr_pct"],
                                dry_run=DRY_RUN,
                                rsi_at_entry=sig["rsi"],
                                vwap_dev_at_entry=sig["vwap_dev"],
                                vol_ratio_at_entry=sig["vol_ratio"],
                                bb_pct_at_entry=sig["bb_pct"],
                                pair_regime=p_regime,
                                exchange_order_id=order.get("id"),
                                stop_order_id=stop_id,
                                bot_variant=BOT_VARIANT,
                            )
                            tg.trade_opened(pair, direction, price, actual_stake,
                                            leverage, entry_tag, DRY_RUN)
                            db.log_signal(
                                pair=pair, direction=direction, fired=True,
                                price=price, rsi=sig["rsi"],
                                bb_pct=sig["bb_pct"], ema_gap=sig["ema_gap"],
                                atr_pct=sig["atr_pct"], vwap_dev=sig["vwap_dev"],
                                volume_ratio=sig["vol_ratio"],
                                btc_regime=btc_regime_label, session=session,
                                context={
                                    "pair_15m_regime": p15_regime,
                                    "pair_1h_regime": p_regime,
                                    "pair_4h_regime": p4_regime,
                                    "params": p,
                                },
                            )
                            log_decision_snapshot(
                                pair, df, p, btc_regime_label, p15_regime,
                                p_regime, p4_regime, entry_tag, True, None,
                                {"signal": sig, "trade_id": trade_id}
                            )
                            open_trades = db.get_open_trades()
                            open_pairs  = {t["pair"] for t in open_trades}
                        else:
                            log_decision_snapshot(
                                pair, df, p, btc_regime_label, p15_regime,
                                p_regime, p4_regime, entry_tag, False,
                                "order_failed", {"signal": sig}
                            )

            # ── 5. Check daily loss limit ─────────────────────────────────────
            total_profit = sum(
                t.get("profit_usdt", 0) or 0
                for t in db.get_closed_trades_for_day(now.date().isoformat())
            )
            wallet_now = wallet_start + total_profit
            check_daily_loss(wallet_start, wallet_now)

            # ── 6. Store indicator snapshot for dashboard ─────────────────────
            snapshot = {}
            for pair, df in df5m.items():
                row = df.iloc[-1]
                snapshot[pair] = {
                    "close":    round(float(row["close"]), 4),
                    "rsi":      round(float(row.get("rsi", 0) or 0), 1),
                    "atr_pct":  round(float(row.get("atr_pct", 0) or 0), 3),
                    "vwap_dev": round(float(row.get("vwap_dev", 0) or 0), 2),
                    "vol_ratio":round(float(row.get("volume_ratio", 0) or 0), 2),
                    "bb_pos":   "above" if float(row["close"]) > float(row.get("bb_upper", 0) or 0)
                                else "below" if float(row["close"]) < float(row.get("bb_lower", 999999) or 999999)
                                else "inside",
                    "ema_trend":"bear" if float(row.get("ema_fast", 0) or 0) < float(row.get("ema_slow", 0) or 0) else "bull",
                }
            db.set_bot_state("indicators", json.dumps(snapshot))
            db.set_bot_state("btc_regime", btc_regime_label)

            # ── 7. Status log every loop ──────────────────────────────────────
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
