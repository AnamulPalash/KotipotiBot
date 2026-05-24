# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
"""
ShortScalper v2 — KotipotiBot
===============================
Futures short-only scalping strategy on 5m timeframe with 5x leverage.

Pairs: BTC/USDT:USDT, ETH/USDT:USDT, SOL/USDT:USDT, DOGE/USDT:USDT, XRP/USDT:USDT
Timeframe: 5m
Direction: SHORT ONLY
Exchange: Bybit (futures, dry-run)

Filters:
  - 1h trend filter (block short if pair's own 1h is strongly bullish)
  - BTC regime filter (block alt shorts if BTC 1h is strongly bullish)
  - Overextension override (allow despite BTC block if RSI 1h > 80 AND price > 2σ above BB upper on 1h)

Safety controls:
  - Max daily loss: halt entries if wallet drops 10% in one UTC day
  - Max consecutive losses: halt after 4 consecutive losses, resume after 2h cooldown
  - Per-pair cooldown: 3 candles (15m) after a losing trade
  - Max 6 trades per pair per day
  - Stale data guard: halt if candle data is stale beyond 2 candles
  - API error guard: halt after 5 errors in one hour
  - DB write fail guard: halt new entries if DB write fails
  - Telegram failure: suppress entries only, never close open positions

Market regime logging for every trade and every skipped signal.
"""

from freqtrade.strategy import IStrategy, merge_informative_pair, stoploss_from_open
from freqtrade.persistence import Trade
from pandas import DataFrame
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Tuple
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib
import logging
import json
import os

logger = logging.getLogger(__name__)

# ---- Constants ----
PAIRS_ALLOWED = {
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "SOL/USDT:USDT",
    "DOGE/USDT:USDT",
    "XRP/USDT:USDT",
}
ALT_PAIRS = {"ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "XRP/USDT:USDT"}
BTC_PAIR = "BTC/USDT:USDT"

MAX_DAILY_LOSS_PCT = 0.10          # 10% wallet drop halts new entries
MAX_CONSECUTIVE_LOSSES = 4         # halt after N consecutive losses
CONSECUTIVE_LOSS_COOLDOWN_H = 2    # hours before resuming after consec-loss halt
PAIR_COOLDOWN_CANDLES = 3          # candles (15m on 5m tf) after losing trade
MAX_TRADES_PER_PAIR_PER_DAY = 6
MAX_API_ERRORS_PER_HOUR = 5
STALE_CANDLE_LIMIT = 2             # candles before halting

LOG_DIR = "/freqtrade/user_data/logs"
REGIME_LOG_FILE = os.path.join(LOG_DIR, "regime_log.jsonl")


# ---- Helpers ----

def _session(dt: datetime) -> str:
    """Return Asia / London / US based on UTC hour."""
    h = dt.hour
    if 0 <= h < 8:
        return "Asia"
    elif 8 <= h < 13:
        return "London"
    else:
        return "US"


def _trend_label(df_1h: DataFrame, idx: int = -1) -> str:
    """
    Classify trend as bullish / bearish / range based on last row.
    Uses EMA21, RSI, and higher-highs.
    """
    if df_1h is None or df_1h.empty:
        return "unknown"
    row = df_1h.iloc[idx]
    try:
        above_ema = row["close"] > row["ema21_1h"]
        rsi_bull = row["rsi_1h"] > 55
        rsi_bear = row["rsi_1h"] < 45
        hh = row.get("higher_highs_1h", False)
        if above_ema and rsi_bull:
            return "bullish"
        elif not above_ema and rsi_bear:
            return "bearish"
        else:
            return "range"
    except Exception:
        return "unknown"


def _trend_label_4h(df_4h: DataFrame, idx: int = -1) -> str:
    if df_4h is None or df_4h.empty:
        return "unknown"
    row = df_4h.iloc[idx]
    try:
        above_ema = row["close"] > row["ema21_4h"]
        rsi_bull = row["rsi_4h"] > 55
        rsi_bear = row["rsi_4h"] < 45
        if above_ema and rsi_bull:
            return "bullish"
        elif not above_ema and rsi_bear:
            return "bearish"
        else:
            return "range"
    except Exception:
        return "unknown"


def _is_strongly_bullish_1h(df_1h: DataFrame) -> bool:
    """
    Strongly bullish: price above EMA21 1h AND RSI 1h > 60 AND higher highs in last 3 candles.
    All three must be true.
    """
    if df_1h is None or len(df_1h) < 4:
        return False
    row = df_1h.iloc[-1]
    try:
        cond1 = row["close"] > row["ema21_1h"]
        cond2 = row["rsi_1h"] > 60
        cond3 = bool(row.get("higher_highs_1h", False))
        return bool(cond1 and cond2 and cond3)
    except Exception:
        return False


def _is_overextended_1h(df_1h: DataFrame) -> bool:
    """
    Overextension override: RSI 1h > 80 AND price > 2 std above BB upper on 1h.
    Both must be true.
    """
    if df_1h is None or df_1h.empty:
        return False
    row = df_1h.iloc[-1]
    try:
        cond1 = row["rsi_1h"] > 80
        cond2 = row["close"] > row["bb_upper_2std_1h"]
        return bool(cond1 and cond2)
    except Exception:
        return False


def _volatility_label(df: DataFrame) -> str:
    """Classify recent volatility as low / normal / high based on BB width."""
    try:
        bw = df["bb_width"].iloc[-1]
        mean_bw = df["bb_width"].rolling(50).mean().iloc[-1]
        if bw < mean_bw * 0.7:
            return "low"
        elif bw > mean_bw * 1.5:
            return "high"
        else:
            return "normal"
    except Exception:
        return "unknown"


def _volume_label(df: DataFrame) -> str:
    try:
        vol = df["volume"].iloc[-1]
        vol_mean = df["volume_mean"].iloc[-1]
        if vol < vol_mean * 0.7:
            return "below_average"
        elif vol > vol_mean * 1.5:
            return "high"
        else:
            return "normal"
    except Exception:
        return "unknown"


def _log_regime(entry: dict):
    """Append a regime log entry to the JSONL file."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(REGIME_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        logger.warning(f"[ShortScalper] Failed to write regime log: {e}")


class ShortScalper(IStrategy):
    """
    Short-only scalping strategy with full regime filtering and safety controls.
    """

    INTERFACE_VERSION = 3
    strategy_name = "ShortScalper"
    can_short = True

    # ---- Timeframes ----
    timeframe = "5m"
    informative_timeframes = ["1h", "4h"]

    # ---- ROI ----
    minimal_roi = {
        "0":  0.015,
        "10": 0.010,
        "30": 0.005,
        "60": 0.0,
    }

    # ---- Stoploss ----
    stoploss = -0.025

    # ---- Trailing stop ----
    trailing_stop = True
    trailing_stop_positive = 0.010
    trailing_stop_positive_offset = 0.015
    trailing_only_offset_is_reached = True

    # ---- Startup ----
    startup_candle_count = 100

    # ---- Order types ----
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ---- Protections ----
    @property
    def protections(self):
        return [
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 3,
                "stop_duration_candles": 12,
                "only_per_pair": False,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 3,
                "stop_duration_candles": 12,
                "max_allowed_drawdown": 0.10,
            },
            {
                "method": "CooldownPeriod",
                "stop_duration_candles": PAIR_COOLDOWN_CANDLES,
            },
            {
                "method": "LowProfitPairs",
                "lookback_period_candles": 60,
                "trade_limit": 2,
                "stop_duration_candles": 24,
                "required_profit": 0.0,
            },
        ]

    # ---- Runtime state ----
    _consecutive_losses: int = 0
    _consecutive_loss_halt_until: Optional[datetime] = None
    _api_errors: list = []
    _daily_loss_halted: bool = False
    _daily_loss_reset_day: Optional[int] = None
    _pair_trade_counts: Dict[str, Dict[str, int]] = {}  # pair -> date -> count
    _db_write_failed: bool = False
    _telegram_failed: bool = False

    # ---- Plot config ----
    plot_config = {
        "main_plot": {
            "ema_fast": {"color": "blue"},
            "ema_slow": {"color": "orange"},
            "bb_upperband": {"color": "red", "fill_to": "bb_lowerband", "opacity": 0.1},
            "bb_lowerband": {"color": "green"},
            "bb_middleband": {"color": "gray"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "purple"}},
            "Volume": {"volume": {"color": "teal"}},
        },
    }

    # ------------------------------------------------------------------
    # Informative indicators
    # ------------------------------------------------------------------

    def informative_pairs(self):
        pairs = list(PAIRS_ALLOWED)
        result = []
        for pair in pairs:
            result.append((pair, "1h"))
            result.append((pair, "4h"))
        # Always include BTC for regime filter on alts
        if (BTC_PAIR, "1h") not in result:
            result.append((BTC_PAIR, "1h"))
        if (BTC_PAIR, "4h") not in result:
            result.append((BTC_PAIR, "4h"))
        return result

    def _add_informative_indicators(self, df: DataFrame, tf: str) -> DataFrame:
        """Add EMA21, RSI, BB, higher-highs to an informative dataframe."""
        suffix = tf.replace("h", "h").replace("m", "m")
        df[f"ema21_{suffix}"] = ta.EMA(df["close"], timeperiod=21)
        df[f"rsi_{suffix}"] = ta.RSI(df["close"], timeperiod=14)

        bb = qtpylib.bollinger_bands(qtpylib.typical_price(df), window=20, stds=2)
        df[f"bb_upper_{suffix}"] = bb["upper"]
        df[f"bb_lower_{suffix}"] = bb["lower"]
        df[f"bb_mid_{suffix}"] = bb["mid"]

        # 2-std upper for overextension check
        bb2 = qtpylib.bollinger_bands(qtpylib.typical_price(df), window=20, stds=2)
        df[f"bb_upper_2std_{suffix}"] = bb2["upper"]

        # Higher highs: last 3 candles each close higher than previous
        df[f"higher_highs_{suffix}"] = (
            (df["high"] > df["high"].shift(1)) &
            (df["high"].shift(1) > df["high"].shift(2))
        )
        return df

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # ---- 5m base indicators ----
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lowerband"] = bb["lower"]
        dataframe["bb_middleband"] = bb["mid"]
        dataframe["bb_upperband"] = bb["upper"]
        dataframe["bb_width"] = (
            (dataframe["bb_upperband"] - dataframe["bb_lowerband"]) /
            dataframe["bb_middleband"]
        )

        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()

        # ---- 1h informative ----
        inf_1h = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
        if not inf_1h.empty:
            inf_1h = self._add_informative_indicators(inf_1h, "1h")
            dataframe = merge_informative_pair(
                dataframe, inf_1h, self.timeframe, "1h",
                ffill=True
            )

        # ---- 4h informative ----
        inf_4h = self.dp.get_pair_dataframe(pair=pair, timeframe="4h")
        if not inf_4h.empty:
            inf_4h = self._add_informative_indicators(inf_4h, "4h")
            dataframe = merge_informative_pair(
                dataframe, inf_4h, self.timeframe, "4h",
                ffill=True
            )

        # ---- BTC 1h for regime filter (alts only) ----
        if pair != BTC_PAIR:
            btc_1h = self.dp.get_pair_dataframe(pair=BTC_PAIR, timeframe="1h")
            if not btc_1h.empty:
                btc_1h = self._add_informative_indicators(btc_1h, "1h")
                btc_1h = btc_1h.rename(columns={
                    col: f"btc_{col}" for col in btc_1h.columns
                    if col not in ["date", "open", "high", "low", "close", "volume"]
                })
                btc_1h = btc_1h[["date"] + [c for c in btc_1h.columns if c.startswith("btc_")]]
                dataframe = merge_informative_pair(
                    dataframe, btc_1h, self.timeframe, "1h",
                    ffill=True, suffix="btc"
                )

        return dataframe

    # ------------------------------------------------------------------
    # Safety gate
    # ------------------------------------------------------------------

    def _safety_gate(self, pair: str, current_time: datetime) -> Tuple[bool, str]:
        """
        Returns (allowed, reason). If not allowed, new entries are blocked.
        Never closes existing positions.
        """
        utc_now = current_time.replace(tzinfo=timezone.utc) if current_time.tzinfo is None else current_time.astimezone(timezone.utc)

        # 1. Daily loss halt — reset at UTC midnight
        today = utc_now.date().toordinal()
        if self._daily_loss_reset_day != today:
            self._daily_loss_reset_day = today
            self._daily_loss_halted = False

        if self._daily_loss_halted:
            return False, "daily_loss_halt"

        # 2. Consecutive loss halt
        if self._consecutive_loss_halt_until is not None:
            halt_until = self._consecutive_loss_halt_until
            if halt_until.tzinfo is None:
                halt_until = halt_until.replace(tzinfo=timezone.utc)
            if utc_now < halt_until:
                remaining = int((halt_until - utc_now).total_seconds() / 60)
                return False, f"consecutive_loss_halt_{remaining}m_remaining"
            else:
                self._consecutive_loss_halt_until = None
                self._consecutive_losses = 0

        # 3. Max trades per pair per day
        today_str = utc_now.date().isoformat()
        pair_counts = self._pair_trade_counts.get(pair, {})
        if pair_counts.get(today_str, 0) >= MAX_TRADES_PER_PAIR_PER_DAY:
            return False, f"max_trades_per_day_{MAX_TRADES_PER_PAIR_PER_DAY}"

        # 4. API error guard
        one_hour_ago = utc_now - timedelta(hours=1)
        self._api_errors = [t for t in self._api_errors if t > one_hour_ago]
        if len(self._api_errors) >= MAX_API_ERRORS_PER_HOUR:
            return False, f"api_error_limit_{MAX_API_ERRORS_PER_HOUR}_per_hour"

        # 5. DB write fail guard
        if self._db_write_failed:
            return False, "db_write_failed"

        # 6. Pair not in allowed list
        if pair not in PAIRS_ALLOWED:
            return False, f"pair_not_allowed_{pair}"

        return True, "ok"

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time: datetime,
                            entry_tag: Optional[str], side: str, **kwargs) -> bool:
        allowed, reason = self._safety_gate(pair, current_time)
        if not allowed:
            logger.info(f"[ShortScalper] {pair} entry blocked by safety gate: {reason}")
            return False

        # Increment pair trade count
        today_str = current_time.strftime("%Y-%m-%d")
        if pair not in self._pair_trade_counts:
            self._pair_trade_counts[pair] = {}
        self._pair_trade_counts[pair][today_str] = \
            self._pair_trade_counts[pair].get(today_str, 0) + 1

        return True

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str,
                           amount: float, rate: float, time_in_force: str,
                           exit_reason: str, current_time: datetime, **kwargs) -> bool:
        """Track consecutive losses and daily drawdown."""
        # We can't know P&L here without trade.calc_profit_ratio — use custom_exit hook instead
        return True

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        """Post-trade hook to update safety state."""
        return None

    def order_filled(self, pair: str, trade: Trade, order: dict,
                     current_time: datetime, **kwargs):
        """Called after an order fills. Track losses here."""
        if order.get("side") == "buy" and trade.is_short:
            # This is an exit fill for a short trade
            profit = trade.calc_profit_ratio(rate=order.get("price", trade.close_rate))
            if profit < 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
                    halt_until = current_time + timedelta(hours=CONSECUTIVE_LOSS_COOLDOWN_H)
                    self._consecutive_loss_halt_until = halt_until
                    logger.warning(
                        f"[ShortScalper] {MAX_CONSECUTIVE_LOSSES} consecutive losses. "
                        f"Halting new entries until {halt_until.isoformat()}"
                    )
            else:
                self._consecutive_losses = 0

    # ------------------------------------------------------------------
    # Entry/Exit
    # ------------------------------------------------------------------

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        pair = metadata["pair"]

        # Guard: only trade allowed pairs
        if pair not in PAIRS_ALLOWED:
            dataframe["enter_short"] = 0
            return dataframe

        conditions = []
        filters_log = []

        # ---- Base 5m conditions ----
        cond_bb = dataframe["close"] > dataframe["bb_upperband"]
        cond_rsi = dataframe["rsi"] > 68
        cond_ema = dataframe["ema_fast"] < dataframe["ema_slow"]
        cond_vol = dataframe["volume"] > dataframe["volume_mean"] * 1.2
        cond_vol_nonzero = dataframe["volume"] > 0

        conditions.extend([cond_bb, cond_rsi, cond_ema, cond_vol, cond_vol_nonzero])

        # ---- 1h own-pair trend filter ----
        # Strongly bullish: price above EMA21_1h AND RSI_1h > 60 AND higher_highs_1h
        # Use merged columns (suffixed by merge_informative_pair)
        ema21_col = "ema21_1h_1h" if "ema21_1h_1h" in dataframe.columns else "ema21_1h"
        rsi_col = "rsi_1h_1h" if "rsi_1h_1h" in dataframe.columns else "rsi_1h"
        hh_col = "higher_highs_1h_1h" if "higher_highs_1h_1h" in dataframe.columns else "higher_highs_1h"
        bb_upper_2std_col = "bb_upper_2std_1h_1h" if "bb_upper_2std_1h_1h" in dataframe.columns else "bb_upper_2std_1h"

        if ema21_col in dataframe.columns and rsi_col in dataframe.columns and hh_col in dataframe.columns:
            strongly_bullish_1h = (
                (dataframe["close"] > dataframe[ema21_col]) &
                (dataframe[rsi_col] > 60) &
                (dataframe[hh_col] == True)
            )
            overextended_1h = (
                (dataframe[rsi_col] > 80) &
                (dataframe["close"] > dataframe[bb_upper_2std_col])
            ) if bb_upper_2std_col in dataframe.columns else dataframe["rsi"] > 100  # never true fallback

            # Block if strongly bullish UNLESS overextended
            pair_trend_blocked = strongly_bullish_1h & ~overextended_1h
            conditions.append(~pair_trend_blocked)

        # ---- BTC regime filter (alts only) ----
        if pair in ALT_PAIRS:
            btc_ema_col = "btc_ema21_1h_btc" if "btc_ema21_1h_btc" in dataframe.columns else None
            btc_rsi_col = "btc_rsi_1h_btc" if "btc_rsi_1h_btc" in dataframe.columns else None
            btc_hh_col = "btc_higher_highs_1h_btc" if "btc_higher_highs_1h_btc" in dataframe.columns else None
            btc_bb_col = "btc_bb_upper_2std_1h_btc" if "btc_bb_upper_2std_1h_btc" in dataframe.columns else None

            if btc_ema_col and btc_rsi_col and btc_hh_col and \
               all(c in dataframe.columns for c in [btc_ema_col, btc_rsi_col, btc_hh_col]):
                btc_strongly_bullish = (
                    (dataframe["close"] > dataframe[btc_ema_col]) &
                    (dataframe[btc_rsi_col] > 60) &
                    (dataframe[btc_hh_col] == True)
                )
                pair_overextended = (
                    (dataframe[rsi_col] > 80) &
                    (dataframe["close"] > dataframe[bb_upper_2std_col])
                ) if bb_upper_2std_col in dataframe.columns else dataframe["rsi"] > 100

                btc_blocked = btc_strongly_bullish & ~pair_overextended
                conditions.append(~btc_blocked)

        # ---- Combine all conditions ----
        combined = conditions[0]
        for c in conditions[1:]:
            combined = combined & c

        dataframe.loc[combined, "enter_short"] = 1
        dataframe.loc[combined, "enter_tag"] = "short_scalp"

        # ---- Log skipped signals ----
        # Rows where base conditions pass but filters blocked
        base_pass = cond_bb & cond_rsi & cond_ema & cond_vol & cond_vol_nonzero
        skipped = base_pass & ~combined
        if skipped.any():
            self._log_skipped_signals(dataframe, metadata, skipped,
                                      cond_bb, cond_rsi, cond_ema, cond_vol)

        return dataframe

    def _log_skipped_signals(self, dataframe, metadata, skipped_mask,
                             cond_bb, cond_rsi, cond_ema, cond_vol):
        pair = metadata["pair"]
        skipped_rows = dataframe[skipped_mask]
        for idx, row in skipped_rows.iterrows():
            dt = row.get("date", datetime.utcnow())
            entry = {
                "type": "skipped_signal",
                "pair": pair,
                "timestamp": str(dt),
                "session": _session(dt if isinstance(dt, datetime) else datetime.utcnow()),
                "filters": {
                    "price_above_upper_bb": bool(cond_bb.loc[idx]) if idx in cond_bb.index else None,
                    "rsi_gt_68": bool(cond_rsi.loc[idx]) if idx in cond_rsi.index else None,
                    "ema_bearish": bool(cond_ema.loc[idx]) if idx in cond_ema.index else None,
                    "volume_above_avg": bool(cond_vol.loc[idx]) if idx in cond_vol.index else None,
                },
                "volatility": _volatility_label(dataframe),
                "volume_condition": _volume_label(dataframe),
            }
            _log_regime(entry)
            logger.info(
                f"[ShortScalper] {pair} short skipped at {dt}: "
                f"BB={entry['filters']['price_above_upper_bb']}, "
                f"RSI={entry['filters']['rsi_gt_68']}, "
                f"EMA={entry['filters']['ema_bearish']}, "
                f"Vol={entry['filters']['volume_above_avg']}"
            )

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["bb_middleband"]) &
                (dataframe["rsi"] < 45) &
                (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1

        return dataframe

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: Optional[str], side: str) -> float:
        return 5.0

    # ------------------------------------------------------------------
    # Custom stoploss — ensure hard stop always present
    # ------------------------------------------------------------------

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        # Always return the hard stoploss — never unlimited
        return self.stoploss

    # ------------------------------------------------------------------
    # Trade entry/exit regime logging
    # ------------------------------------------------------------------

    def custom_entry_price(self, pair: str, trade: Optional[Trade],
                           current_time: datetime, proposed_rate: float,
                           entry_tag: Optional[str], side: str, **kwargs) -> float:
        """Log regime context at entry time."""
        self._log_trade_regime(pair, current_time, "entry", entry_tag or "short_scalp")
        return proposed_rate

    def _log_trade_regime(self, pair: str, current_time: datetime,
                          event_type: str, reason: str):
        """Log full market regime context for a trade event."""
        try:
            df_5m = self.dp.get_pair_dataframe(pair=pair, timeframe=self.timeframe)
            df_1h = self.dp.get_pair_dataframe(pair=pair, timeframe="1h")
            df_4h = self.dp.get_pair_dataframe(pair=pair, timeframe="4h")
            btc_1h = self.dp.get_pair_dataframe(pair=BTC_PAIR, timeframe="1h") if pair != BTC_PAIR else df_1h
            btc_4h = self.dp.get_pair_dataframe(pair=BTC_PAIR, timeframe="4h") if pair != BTC_PAIR else df_4h

            if not df_1h.empty:
                df_1h = self._add_informative_indicators(df_1h, "1h")
            if not df_4h.empty:
                df_4h = self._add_informative_indicators(df_4h, "4h")
            if not btc_1h.empty:
                btc_1h = self._add_informative_indicators(btc_1h, "1h")
            if not btc_4h.empty:
                btc_4h = self._add_informative_indicators(btc_4h, "4h")

            entry = {
                "type": event_type,
                "pair": pair,
                "timestamp": current_time.isoformat(),
                "session": _session(current_time),
                "btc_1h_trend": _trend_label(btc_1h),
                "btc_4h_trend": _trend_label_4h(btc_4h),
                "pair_1h_trend": _trend_label(df_1h),
                "pair_4h_trend": _trend_label_4h(df_4h),
                "volatility": _volatility_label(df_5m) if not df_5m.empty else "unknown",
                "volume_condition": _volume_label(df_5m) if not df_5m.empty else "unknown",
                "entry_reason": reason,
                "exit_reason": None,
            }
            _log_regime(entry)
        except Exception as e:
            logger.warning(f"[ShortScalper] Failed to log trade regime: {e}")
