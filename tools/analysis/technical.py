"""Technical analysis tools for price series."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from tools.base_tool import BaseTool


class MovingAverageSnapshot(BaseModel):
    """Moving average values for a price series."""

    short_period: int
    long_period: int
    short_sma: float | None
    long_sma: float | None
    short_ema: float | None
    long_ema: float | None


class MacdSnapshot(BaseModel):
    """MACD values for a price series."""

    macd: float | None
    signal: float | None
    histogram: float | None


class TechnicalAnalysisSnapshot(BaseModel):
    """Normalized technical analysis output."""

    sample_size: int
    latest_price: float | None
    rsi_period: int
    rsi: float | None
    moving_averages: MovingAverageSnapshot
    macd: MacdSnapshot
    trend: str


class TechnicalAnalysisTool(BaseTool):
    """Compute RSI, MACD, simple moving averages, and exponential moving averages."""

    name = "technical_analysis"
    description = "Compute basic technical analysis over a price series: RSI, MACD, SMA, and EMA."
    parameters = {
        "type": "object",
        "properties": {
            "prices": {
                "type": "array",
                "description": "Price list. Items may be numbers or objects containing a numeric price field.",
            },
            "short_window": {"type": "integer", "minimum": 2, "maximum": 100, "default": 20},
            "long_window": {"type": "integer", "minimum": 3, "maximum": 300, "default": 50},
            "rsi_period": {"type": "integer", "minimum": 2, "maximum": 100, "default": 14},
        },
        "required": ["prices"],
    }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Analyze a price series and return normalized indicators."""

        prices = self._extract_prices(kwargs["prices"])
        if len(prices) < 2:
            return self.failure("At least two prices are required for technical analysis", sample_size=len(prices))

        short_window = int(kwargs.get("short_window", 20))
        long_window = int(kwargs.get("long_window", 50))
        rsi_period = int(kwargs.get("rsi_period", 14))

        short_ema_series = self._ema_series(prices, short_window)
        long_ema_series = self._ema_series(prices, long_window)
        macd_series = self._macd_series(prices)
        signal_series = self._ema_series(macd_series, 9) if macd_series else []

        moving_averages = MovingAverageSnapshot(
            short_period=short_window,
            long_period=long_window,
            short_sma=self._sma(prices, short_window),
            long_sma=self._sma(prices, long_window),
            short_ema=short_ema_series[-1] if short_ema_series else None,
            long_ema=long_ema_series[-1] if long_ema_series else None,
        )
        macd = self._latest_macd(macd_series, signal_series)
        snapshot = TechnicalAnalysisSnapshot(
            sample_size=len(prices),
            latest_price=prices[-1],
            rsi_period=rsi_period,
            rsi=self._rsi(prices, rsi_period),
            moving_averages=moving_averages,
            macd=macd,
            trend=self._trend(moving_averages, macd),
        )
        return self.success(snapshot.model_dump(), source="local")

    def _extract_prices(self, raw_prices: Any) -> list[float]:
        """Extract numeric prices from raw inputs."""

        if not isinstance(raw_prices, list):
            return []

        prices: list[float] = []
        for item in raw_prices:
            value = item.get("price") if isinstance(item, dict) else item
            try:
                prices.append(float(value))
            except (TypeError, ValueError):
                continue
        return prices

    def _sma(self, prices: list[float], period: int) -> float | None:
        """Return the latest simple moving average."""

        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    def _ema_series(self, prices: list[float], period: int) -> list[float]:
        """Return the exponential moving average series."""

        if not prices or period <= 0:
            return []

        multiplier = 2 / (period + 1)
        ema_values = [prices[0]]
        for price in prices[1:]:
            ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
        return ema_values

    def _rsi(self, prices: list[float], period: int) -> float | None:
        """Return the latest relative strength index."""

        if len(prices) <= period:
            return None

        gains: list[float] = []
        losses: list[float] = []
        deltas = [current - previous for previous, current in zip(prices, prices[1:], strict=False)]
        for delta in deltas[-period:]:
            if delta >= 0:
                gains.append(delta)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(delta))

        average_gain = sum(gains) / period
        average_loss = sum(losses) / period
        if average_loss == 0:
            return 100.0
        relative_strength = average_gain / average_loss
        return 100 - (100 / (1 + relative_strength))

    def _macd_series(self, prices: list[float]) -> list[float]:
        """Return MACD line values."""

        if len(prices) < 2:
            return []
        short = self._ema_series(prices, 12)
        long = self._ema_series(prices, 26)
        return [short_value - long_value for short_value, long_value in zip(short, long, strict=False)]

    def _latest_macd(self, macd_series: list[float], signal_series: list[float]) -> MacdSnapshot:
        """Return the latest MACD snapshot."""

        macd = macd_series[-1] if macd_series else None
        signal = signal_series[-1] if signal_series else None
        histogram = macd - signal if macd is not None and signal is not None else None
        return MacdSnapshot(macd=macd, signal=signal, histogram=histogram)

    def _trend(self, moving_averages: MovingAverageSnapshot, macd: MacdSnapshot) -> str:
        """Infer a simple trend label from averages and MACD."""

        bullish_ma = (
            moving_averages.short_ema is not None
            and moving_averages.long_ema is not None
            and moving_averages.short_ema > moving_averages.long_ema
        )
        bearish_ma = (
            moving_averages.short_ema is not None
            and moving_averages.long_ema is not None
            and moving_averages.short_ema < moving_averages.long_ema
        )
        bullish_macd = macd.histogram is not None and macd.histogram > 0
        bearish_macd = macd.histogram is not None and macd.histogram < 0

        if bullish_ma and bullish_macd:
            return "bullish"
        if bearish_ma and bearish_macd:
            return "bearish"
        return "mixed"
