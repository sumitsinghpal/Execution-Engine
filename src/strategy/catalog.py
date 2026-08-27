"""
The strategy catalog: a fixed set of well-known, publicly documented
technical trading strategies, each attached to the stop-loss/take-profit
convention most commonly associated with it in trading literature.

None of this is proprietary or invented — every rule here is traceable to
a well-known source (cited in each definition's `famous_for`), and every
stop/target rule is the one commonly taught for that strategy, not a
number picked to look good. Where a strategy is genuinely trend-following
with no fixed target (Turtle Trading), that's stated rather than papered
over with a fabricated-sounding ratio.

Categorized by holding period per the three buckets the dashboard shows
separately: INTRADAY (closed same day), MULTI_DAY (days to a few weeks),
OTHER (weeks to months+, positional).

Every `evaluate()` here only ever produces a BUY signal — no shorting.
That's a deliberate simplification, not an oversight: this system has no
margin/short-selling safety controls (borrow, margin calls, unlimited
downside) built anywhere else in Execution-Engine, so adding short signals
here would be building the risky half of a feature this system isn't
otherwise equipped to support safely.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from src.strategy import indicators as ind
from src.strategy.bars import Bar, closes, highs, lows, volumes


class StrategyCategory(str, Enum):
    INTRADAY = "INTRADAY"
    MULTI_DAY = "MULTI_DAY"
    OTHER = "OTHER"


@dataclass
class SignalDetail:
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    rationale: str


@dataclass
class StrategyDefinition:
    id: str
    name: str
    category: StrategyCategory
    description: str
    famous_for: str
    risk_reward_label: str
    stop_rule: str
    target_rule: str
    bar_interval: str  # "5min" or "daily" — see BrokerAdapter.get_price_history
    lookback_days: int
    evaluate: Callable[[list[Bar]], Optional[SignalDetail]]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "famous_for": self.famous_for,
            "risk_reward_label": self.risk_reward_label,
            "stop_rule": self.stop_rule,
            "target_rule": self.target_rule,
            "bar_interval": self.bar_interval,
        }


def _crossed_up(prev_a: Optional[float], prev_b: Optional[float], a: Optional[float], b: Optional[float]) -> bool:
    """Whether series a crossed above series b on the most recent bar."""
    if None in (prev_a, prev_b, a, b):
        return False
    return prev_a <= prev_b and a > b


# ---------------------------------------------------------------- INTRADAY

def _opening_range_breakout(bars: list[Bar]) -> Optional[SignalDetail]:
    if len(bars) < 8:
        return None
    opening = bars[:6]  # first 30 minutes on 5-minute bars
    or_high = max(b.high for b in opening)
    or_low = min(b.low for b in opening)
    if or_high <= or_low:
        return None

    prev_close, last = bars[-2].close, bars[-1]
    if not (prev_close <= or_high and last.close > or_high):
        return None  # only fire on the bar that actually crosses, not every bar after

    risk = or_high - or_low
    return SignalDetail(
        entry_price=last.close,
        stop_loss_price=or_low,
        take_profit_price=last.close + 2 * risk,
        rationale=f"Broke above the opening 30-min range high (${or_high:.2f}); range height ${risk:.2f}.",
    )


def _vwap_reversion(bars: list[Bar]) -> Optional[SignalDetail]:
    if len(bars) < 10:
        return None
    vwap_series = ind.vwap(highs(bars), lows(bars), closes(bars), volumes(bars))
    prev_vwap, last_vwap = vwap_series[-2], vwap_series[-1]
    prev_close, last = bars[-2].close, bars[-1]
    if not _crossed_up(prev_close, prev_vwap, last.close, last_vwap):
        return None

    session_low = min(b.low for b in bars)
    risk = last.close - session_low
    if risk <= 0:
        return None
    return SignalDetail(
        entry_price=last.close,
        stop_loss_price=session_low,
        take_profit_price=last.close + 1.5 * risk,
        rationale=f"Reclaimed session VWAP (${last_vwap:.2f}) after trading below it.",
    )


# --------------------------------------------------------------- MULTI-DAY

def _golden_cross(bars: list[Bar]) -> Optional[SignalDetail]:
    c = closes(bars)
    if len(c) < 202:
        return None
    sma50, sma200 = ind.sma(c, 50), ind.sma(c, 200)
    if not _crossed_up(sma50[-2], sma200[-2], sma50[-1], sma200[-1]):
        return None

    atr14 = ind.atr(highs(bars), lows(bars), c, 14)[-1]
    if not atr14:
        return None
    entry = bars[-1].close
    stop = entry - 2 * atr14
    risk = entry - stop
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=entry + 3 * risk,
        rationale="50-day SMA crossed above the 200-day SMA (Golden Cross).",
    )


def _macd_crossover(bars: list[Bar]) -> Optional[SignalDetail]:
    c = closes(bars)
    if len(c) < 35:
        return None
    macd_line, signal_line, _ = ind.macd(c)
    if not _crossed_up(macd_line[-2], signal_line[-2], macd_line[-1], signal_line[-1]):
        return None

    entry = bars[-1].close
    swing_low = min(b.low for b in bars[-10:])
    stop = min(swing_low, entry * 0.985)
    risk = entry - stop
    if risk <= 0:
        return None
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=entry + 2 * risk,
        rationale="MACD line crossed above its signal line.",
    )


def _bollinger_breakout(bars: list[Bar]) -> Optional[SignalDetail]:
    c = closes(bars)
    if len(c) < 22:
        return None
    upper, mid, _ = ind.bollinger_bands(c, 20, 2.0)
    if not _crossed_up(c[-2], upper[-2], c[-1], upper[-1]):
        return None

    entry = bars[-1].close
    stop = mid[-1]
    if stop is None or stop >= entry:
        return None
    risk = entry - stop
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=entry + 2 * risk,
        rationale="Closed above the upper Bollinger Band (20, 2σ) — volatility breakout.",
    )


def _rsi2_mean_reversion(bars: list[Bar]) -> Optional[SignalDetail]:
    """Larry Connors' 2-period RSI pullback-in-an-uptrend strategy."""
    c = closes(bars)
    if len(c) < 202:
        return None
    rsi2 = ind.rsi(c, 2)
    sma200 = ind.sma(c, 200)
    if rsi2[-1] is None or sma200[-1] is None:
        return None
    if not (rsi2[-1] < 10 and c[-1] > sma200[-1]):
        return None

    entry = bars[-1].close
    stop = min(b.low for b in bars[-5:])
    if stop >= entry:
        return None
    risk = entry - stop
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=entry + 1.5 * risk,
        rationale=f"2-period RSI at {rsi2[-1]:.1f} (oversold, <10) while price stays above its 200-day SMA.",
    )


# ------------------------------------------------------------------- OTHER

def _turtle_donchian_breakout(bars: list[Bar]) -> Optional[SignalDetail]:
    """Turtle Trading System 1: entry on a 20-day Donchian breakout, 2N (2x ATR) stop."""
    c, h, l = closes(bars), highs(bars), lows(bars)
    if len(c) < 21:
        return None
    donchian_upper, _ = ind.donchian_channel(h, l, 20)
    if donchian_upper[-1] is None:
        return None
    if not (c[-1] > donchian_upper[-1]):
        return None

    atr20 = ind.atr(h, l, c, 20)[-1]
    if not atr20:
        return None
    entry = c[-1]
    n = atr20  # the Turtles' "N" is literally a 20-day ATR
    stop = entry - 2 * n
    risk = entry - stop
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=stop,
        take_profit_price=entry + 4 * risk,
        rationale=(
            f"New 20-day high breakout (Turtle System 1) at ${donchian_upper[-1]:.2f}; "
            f"stop is the Turtles' literal '2N' rule (2× 20-day ATR). Trend-followers historically "
            f"trail this stop rather than take a fixed profit — the target shown is a representative "
            f"floor, not a hard exit rule."
        ),
    )


def _fifty_two_week_high_breakout(bars: list[Bar]) -> Optional[SignalDetail]:
    """William O'Neil (CANSLIM): buy new highs, cut losses at 7-8%, look to sell into 20-25% strength."""
    c, h = closes(bars), highs(bars)
    if len(c) < 253:
        return None
    prior_high = max(h[-253:-1])
    if not (c[-1] > prior_high):
        return None

    entry = c[-1]
    return SignalDetail(
        entry_price=entry,
        stop_loss_price=entry * 0.92,  # O'Neil's well-known 7-8% stop-loss rule
        take_profit_price=entry * 1.20,  # O'Neil's 20-25% profit-taking guideline
        rationale=f"New 52-week high (prior high ${prior_high:.2f}) — O'Neil-style breakout entry.",
    )


STRATEGIES: dict[str, StrategyDefinition] = {
    d.id: d
    for d in [
        StrategyDefinition(
            id="orb",
            name="Opening Range Breakout",
            category=StrategyCategory.INTRADAY,
            description="Buys a breakout above the first 30 minutes' trading range.",
            famous_for="Classic day-trading setup popularized by Toby Crabel",
            risk_reward_label="1 : 2",
            stop_rule="Opposite side of the opening range",
            target_rule="2× the opening range height",
            bar_interval="5min",
            lookback_days=1,
            evaluate=_opening_range_breakout,
        ),
        StrategyDefinition(
            id="vwap_reversion",
            name="VWAP Reversion",
            category=StrategyCategory.INTRADAY,
            description="Buys a reclaim of the session VWAP after trading below it.",
            famous_for="Staple intraday mean-reversion tactic among prop/institutional day traders",
            risk_reward_label="1 : 1.5",
            stop_rule="Session low so far",
            target_rule="1.5× the entry-to-stop distance",
            bar_interval="5min",
            lookback_days=1,
            evaluate=_vwap_reversion,
        ),
        StrategyDefinition(
            id="golden_cross",
            name="Golden Cross (50/200 SMA)",
            category=StrategyCategory.MULTI_DAY,
            description="Buys when the 50-day moving average crosses above the 200-day.",
            famous_for="One of the most widely cited trend-following signals in technical analysis",
            risk_reward_label="1 : 3",
            stop_rule="2× ATR(14) below entry",
            target_rule="3× the entry-to-stop distance",
            bar_interval="daily",
            lookback_days=260,
            evaluate=_golden_cross,
        ),
        StrategyDefinition(
            id="macd_crossover",
            name="MACD Crossover",
            category=StrategyCategory.MULTI_DAY,
            description="Buys when the MACD line crosses above its signal line.",
            famous_for="Gerald Appel's MACD (12, 26, 9) — one of the most-used swing indicators",
            risk_reward_label="1 : 2",
            stop_rule="Recent 10-day swing low",
            target_rule="2× the entry-to-stop distance",
            bar_interval="daily",
            lookback_days=60,
            evaluate=_macd_crossover,
        ),
        StrategyDefinition(
            id="bollinger_breakout",
            name="Bollinger Band Breakout",
            category=StrategyCategory.MULTI_DAY,
            description="Buys a close above the upper Bollinger Band (20, 2σ).",
            famous_for="John Bollinger's Bollinger Bands",
            risk_reward_label="1 : 2",
            stop_rule="20-day SMA (the band midline)",
            target_rule="2× the entry-to-stop distance",
            bar_interval="daily",
            lookback_days=40,
            evaluate=_bollinger_breakout,
        ),
        StrategyDefinition(
            id="rsi2_connors",
            name="RSI(2) Pullback",
            category=StrategyCategory.MULTI_DAY,
            description="Buys a short-term oversold dip (2-period RSI < 10) while above the 200-day trend.",
            famous_for="Larry Connors' 2-period RSI mean-reversion system",
            risk_reward_label="1 : 1.5",
            stop_rule="Recent 5-day low",
            target_rule="1.5× the entry-to-stop distance",
            bar_interval="daily",
            lookback_days=220,
            evaluate=_rsi2_mean_reversion,
        ),
        StrategyDefinition(
            id="turtle_donchian",
            name="Turtle 20-Day Breakout",
            category=StrategyCategory.OTHER,
            description="Buys a new 20-day high, the original Turtle Trading entry rule.",
            famous_for="Richard Dennis & William Eckhardt's Turtle Traders (1983)",
            risk_reward_label="Trend-following (no fixed target; shown as ~1 : 4)",
            stop_rule="2N stop — 2× the 20-day ATR ('N') below entry",
            target_rule="Traditionally trailed, not fixed; ~4× risk shown as a representative floor",
            bar_interval="daily",
            lookback_days=45,
            evaluate=_turtle_donchian_breakout,
        ),
        StrategyDefinition(
            id="fifty_two_week_high",
            name="52-Week High Breakout",
            category=StrategyCategory.OTHER,
            description="Buys a new 52-week high with a fixed percentage stop and profit target.",
            famous_for="William O'Neil's CANSLIM (\"cut losses at 7-8%\")",
            risk_reward_label="~1 : 2.5",
            stop_rule="8% below entry",
            target_rule="20% above entry",
            bar_interval="daily",
            lookback_days=270,
            evaluate=_fifty_two_week_high_breakout,
        ),
    ]
}


__all__ = ["StrategyCategory", "SignalDetail", "StrategyDefinition", "STRATEGIES"]
