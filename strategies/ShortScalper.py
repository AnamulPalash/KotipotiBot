# pragma pylint: disable=missing-docstring, invalid-name, pointless-string-statement
# flake8: noqa: F401
"""
ShortScalper Strategy for KotipotiBot
======================================
Futures short-only scalping on 5m timeframe with 5x leverage.
Uses EMA crossover + RSI + Bollinger Bands for entry/exit signals.

Exchange: Bybit (futures)
Timeframe: 5m
Leverage: 5x
Mode: Dry run (paper trading)
"""

from freqtrade.strategy import IStrategy, merge_informative_pair
from pandas import DataFrame
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


class ShortScalper(IStrategy):
    """
    Short-only scalping strategy.
    Enters shorts when price is overextended above BBands upper band
    with confirming RSI overbought signal and EMA trend alignment.
    """

    # ---- Strategy metadata ----
    INTERFACE_VERSION = 3
    strategy_name = "ShortScalper"
    can_short = True

    # ---- Timeframe ----
    timeframe = "5m"
    inf_timeframe = "15m"  # informative timeframe for trend filter

    # ---- ROI table (take profit) ----
    # Scalping: exit fast at small profit
    minimal_roi = {
        "0":  0.015,   # 1.5% anytime
        "10": 0.010,   # 1.0% after 10 min
        "30": 0.005,   # 0.5% after 30 min
        "60": 0.0,     # break-even after 1 hr
    }

    # ---- Stop loss ----
    stoploss = -0.025  # 2.5% stop loss

    # ---- Trailing stop ----
    trailing_stop = True
    trailing_stop_positive = 0.010       # activate at 1% profit
    trailing_stop_positive_offset = 0.015  # offset from peak
    trailing_only_offset_is_reached = True

    # ---- Startup candles ----
    startup_candle_count = 50

    # ---- Order types ----
    order_types = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    # ---- Plot config (for freqtrade webui) ----
    plot_config = {
        "main_plot": {
            "ema_fast": {"color": "blue"},
            "ema_slow": {"color": "orange"},
            "bb_upperband": {"color": "red", "fill_to": "bb_lowerband", "opacity": 0.1},
            "bb_lowerband": {"color": "green"},
        },
        "subplots": {
            "RSI": {
                "rsi": {"color": "purple"},
            },
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # ---- EMA ----
        dataframe["ema_fast"] = ta.EMA(dataframe, timeperiod=8)
        dataframe["ema_slow"] = ta.EMA(dataframe, timeperiod=21)

        # ---- RSI ----
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        # ---- Bollinger Bands ----
        bollinger = qtpylib.bollinger_bands(
            qtpylib.typical_price(dataframe), window=20, stds=2
        )
        dataframe["bb_lowerband"] = bollinger["lower"]
        dataframe["bb_middleband"] = bollinger["mid"]
        dataframe["bb_upperband"] = bollinger["upper"]
        dataframe["bb_width"] = (
            dataframe["bb_upperband"] - dataframe["bb_lowerband"]
        ) / dataframe["bb_middleband"]

        # ---- Volume SMA (filter low-volume candles) ----
        dataframe["volume_mean"] = dataframe["volume"].rolling(20).mean()

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Short entry conditions:
        - Price closes above upper Bollinger Band (overextended)
        - RSI > 70 (overbought)
        - EMA fast < EMA slow (bearish trend)
        - Volume above average (genuine move)
        """
        dataframe.loc[
            (
                (dataframe["close"] > dataframe["bb_upperband"])
                & (dataframe["rsi"] > 68)
                & (dataframe["ema_fast"] < dataframe["ema_slow"])
                & (dataframe["volume"] > dataframe["volume_mean"] * 1.2)
                & (dataframe["volume"] > 0)
            ),
            "enter_short",
        ] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """
        Short exit conditions:
        - Price closes below BB middle band (mean reversion complete)
        - RSI drops below 45
        """
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["bb_middleband"])
                & (dataframe["rsi"] < 45)
                & (dataframe["volume"] > 0)
            ),
            "exit_short",
        ] = 1

        return dataframe

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag, side: str) -> float:
        """Fixed 5x leverage for all shorts."""
        return 5.0
