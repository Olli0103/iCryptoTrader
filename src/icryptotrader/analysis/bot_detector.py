"""Bot pattern detector — classifies trading activity from public trade stream.

Analyses the Kraken public trade feed to identify and classify common
algorithmic trading patterns:

1. **Grid bots**: Trades at regular price intervals (arithmetic or geometric).
2. **TWAP / scheduled bots**: Trades at regular time intervals with consistent sizes.
3. **Iceberg orders**: Repeated fills at the same price with consistent small sizes.
4. **Market makers**: Rapid alternating buy/sell at tight spread around mid-price.
5. **Momentum / trend bots**: Burst of same-direction trades following price moves.

Detection is purely statistical — it analyses public trade data to estimate
the *proportion* of bot-driven activity, not to identify specific actors.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PublicTrade:
    """A single public trade from the Kraken trade channel."""

    timestamp: float  # monotonic time of receipt
    price: Decimal
    qty: Decimal
    side: str  # "buy" or "sell"


@dataclass
class GridBotSignal:
    """Evidence of grid-bot activity in the trade stream."""

    detected: bool = False
    num_grid_clusters: int = 0
    avg_spacing_bps: float = 0.0
    geometric: bool = False
    confidence: float = 0.0  # 0-1


@dataclass
class TWAPBotSignal:
    """Evidence of TWAP/scheduled execution in the trade stream."""

    detected: bool = False
    dominant_interval_sec: float = 0.0
    size_cv: float = 0.0  # coefficient of variation of trade sizes
    confidence: float = 0.0


@dataclass
class IcebergSignal:
    """Evidence of iceberg-order activity."""

    detected: bool = False
    num_iceberg_levels: int = 0
    avg_clip_size: float = 0.0
    confidence: float = 0.0


@dataclass
class MarketMakerSignal:
    """Evidence of active market-making bots."""

    detected: bool = False
    alternation_ratio: float = 0.0  # fraction of buy-sell-buy-sell alternations
    avg_spread_bps: float = 0.0
    confidence: float = 0.0


@dataclass
class MomentumBotSignal:
    """Evidence of momentum / trend-following bots."""

    detected: bool = False
    max_consecutive_same_side: int = 0
    burst_count: int = 0  # number of detected momentum bursts
    confidence: float = 0.0


@dataclass
class BotAnalysisReport:
    """Complete bot analysis report for a given observation window."""

    window_sec: float
    trade_count: int
    start_price: Decimal
    end_price: Decimal
    price_range_pct: float

    grid_bot: GridBotSignal = field(default_factory=GridBotSignal)
    twap_bot: TWAPBotSignal = field(default_factory=TWAPBotSignal)
    iceberg: IcebergSignal = field(default_factory=IcebergSignal)
    market_maker: MarketMakerSignal = field(default_factory=MarketMakerSignal)
    momentum_bot: MomentumBotSignal = field(default_factory=MomentumBotSignal)

    estimated_bot_pct: float = 0.0  # rough estimate of bot-driven trade %

    def summary(self) -> str:
        """Human-readable summary of the analysis."""
        lines = [
            "=== Kraken Bot Activity Analysis ===",
            f"Window: {self.window_sec:.0f}s | Trades: {self.trade_count}",
            f"Price: {self.start_price} -> {self.end_price} "
            f"({self.price_range_pct:+.2f}%)",
            f"Estimated bot-driven activity: ~{self.estimated_bot_pct:.0f}%",
            "",
        ]

        # Grid bots
        g = self.grid_bot
        status = "DETECTED" if g.detected else "not detected"
        lines.append(f"[Grid Bots] {status} (confidence: {g.confidence:.0%})")
        if g.detected:
            spacing_type = "geometric" if g.geometric else "arithmetic"
            lines.append(
                f"  Clusters: {g.num_grid_clusters} | "
                f"Avg spacing: {g.avg_spacing_bps:.1f} bps ({spacing_type})"
            )

        # TWAP bots
        t = self.twap_bot
        status = "DETECTED" if t.detected else "not detected"
        lines.append(f"[TWAP/Scheduled Bots] {status} (confidence: {t.confidence:.0%})")
        if t.detected:
            lines.append(
                f"  Interval: ~{t.dominant_interval_sec:.1f}s | "
                f"Size CV: {t.size_cv:.2f}"
            )

        # Iceberg
        ic = self.iceberg
        status = "DETECTED" if ic.detected else "not detected"
        lines.append(f"[Iceberg Orders] {status} (confidence: {ic.confidence:.0%})")
        if ic.detected:
            lines.append(
                f"  Levels: {ic.num_iceberg_levels} | "
                f"Avg clip: {ic.avg_clip_size:.4f}"
            )

        # Market makers
        mm = self.market_maker
        status = "DETECTED" if mm.detected else "not detected"
        lines.append(f"[Market Makers] {status} (confidence: {mm.confidence:.0%})")
        if mm.detected:
            lines.append(
                f"  Alternation: {mm.alternation_ratio:.0%} | "
                f"Avg spread: {mm.avg_spread_bps:.1f} bps"
            )

        # Momentum bots
        mo = self.momentum_bot
        status = "DETECTED" if mo.detected else "not detected"
        lines.append(f"[Momentum Bots] {status} (confidence: {mo.confidence:.0%})")
        if mo.detected:
            lines.append(
                f"  Max consecutive: {mo.max_consecutive_same_side} | "
                f"Bursts: {mo.burst_count}"
            )

        lines.append("")
        lines.append("=== Strategy Insights ===")
        if g.detected and g.avg_spacing_bps > 0:
            lines.append(
                f"  * Grid bots active with ~{g.avg_spacing_bps:.0f} bps spacing "
                f"— consider wider spacing to avoid competition"
            )
        if mm.detected:
            lines.append(
                f"  * Market makers quoting ~{mm.avg_spread_bps:.0f} bps spread "
                f"— tight spreads indicate high competition"
            )
        if ic.detected:
            lines.append(
                f"  * Iceberg orders detected — large hidden liquidity at "
                f"{ic.num_iceberg_levels} price levels"
            )
        if mo.detected:
            lines.append(
                "  * Momentum bots active — expect trend acceleration "
                "on breakouts"
            )
        if not any([g.detected, mm.detected, ic.detected, mo.detected, t.detected]):
            lines.append("  * Low bot activity — market is mostly human-driven")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class BotDetector:
    """Analyses public trade stream to detect bot activity patterns.

    Collects trades via ``record_trade()`` and runs full analysis via
    ``analyze()``.  Designed to work with the existing WS public trade feed.

    Usage::

        detector = BotDetector(window_sec=300)
        # Feed trades from WS:
        detector.record_trade(side="buy", qty=Decimal("0.01"), price=Decimal("85000"))
        # ... more trades ...
        report = detector.analyze()
        print(report.summary())
    """

    def __init__(
        self,
        window_sec: float = 300.0,
        max_trades: int = 50_000,
        clock: object | None = None,
    ) -> None:
        self._window_sec = window_sec
        self._trades: deque[PublicTrade] = deque(maxlen=max_trades)
        self._clock = clock

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator,no-any-return]
        return time.monotonic()

    def record_trade(
        self,
        side: str,
        qty: Decimal,
        price: Decimal,
    ) -> None:
        """Record a public trade from the Kraken trade channel."""
        self._trades.append(PublicTrade(
            timestamp=self._now(),
            price=price,
            qty=qty,
            side=side.lower(),
        ))

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    def _window_trades(self) -> list[PublicTrade]:
        """Return trades within the analysis window, pruning old ones."""
        now = self._now()
        cutoff = now - self._window_sec
        # Prune
        while self._trades and self._trades[0].timestamp < cutoff:
            self._trades.popleft()
        return list(self._trades)

    def analyze(self) -> BotAnalysisReport:
        """Run full bot activity analysis on the current trade window.

        Returns a BotAnalysisReport with detection results for each bot type.
        """
        trades = self._window_trades()

        if len(trades) < 5:
            return BotAnalysisReport(
                window_sec=self._window_sec,
                trade_count=len(trades),
                start_price=trades[0].price if trades else Decimal("0"),
                end_price=trades[-1].price if trades else Decimal("0"),
                price_range_pct=0.0,
            )

        start_price = trades[0].price
        end_price = trades[-1].price
        mid = (start_price + end_price) / 2
        price_range_pct = float((end_price - start_price) / mid * 100) if mid else 0.0

        report = BotAnalysisReport(
            window_sec=self._window_sec,
            trade_count=len(trades),
            start_price=start_price,
            end_price=end_price,
            price_range_pct=price_range_pct,
        )

        report.grid_bot = self._detect_grid_bots(trades)
        report.twap_bot = self._detect_twap_bots(trades)
        report.iceberg = self._detect_iceberg(trades)
        report.market_maker = self._detect_market_makers(trades)
        report.momentum_bot = self._detect_momentum_bots(trades)

        # Rough estimate of bot percentage
        confidences = [
            report.grid_bot.confidence,
            report.twap_bot.confidence,
            report.market_maker.confidence,
            report.iceberg.confidence,
            report.momentum_bot.confidence,
        ]
        # Weighted average — market makers and grid bots typically dominate volume
        weights = [0.30, 0.15, 0.10, 0.25, 0.20]
        weighted = sum(c * w for c, w in zip(confidences, weights, strict=True))
        # Scale to estimated percentage (70-80% of crypto volume is bots per research)
        report.estimated_bot_pct = min(95.0, weighted * 100)

        return report

    # -------------------------------------------------------------------
    # Grid bot detection
    # -------------------------------------------------------------------

    def _detect_grid_bots(self, trades: list[PublicTrade]) -> GridBotSignal:
        """Detect grid-bot patterns: trades at regular price intervals.

        Grid bots place orders at fixed price steps (arithmetic) or fixed
        percentage steps (geometric). We look for clusters of trades at
        price levels that form a regular grid pattern.
        """
        if len(trades) < 10:
            return GridBotSignal()

        # Collect unique price levels rounded to reduce noise
        prices = sorted(set(t.price for t in trades))
        if len(prices) < 4:
            return GridBotSignal()

        # Compute price deltas between consecutive levels
        deltas_bps: list[float] = []
        for i in range(1, len(prices)):
            mid = (prices[i] + prices[i - 1]) / 2
            if mid > 0:
                delta = float((prices[i] - prices[i - 1]) / mid * 10000)
                if delta > 0.5:  # Ignore sub-bps noise
                    deltas_bps.append(delta)

        if len(deltas_bps) < 3:
            return GridBotSignal()

        # Look for a dominant spacing (mode of rounded deltas)
        rounded = [round(d, 0) for d in deltas_bps]
        counter = Counter(rounded)
        most_common_spacing, most_common_count = counter.most_common(1)[0]

        # Check if the dominant spacing appears frequently enough
        regularity = most_common_count / len(rounded)

        # Also check for geometric spacing (consistent ratio)
        ratios: list[float] = []
        for i in range(1, len(prices)):
            if prices[i - 1] > 0:
                ratios.append(float(prices[i] / prices[i - 1]))

        ratio_cv = 0.0
        if ratios:
            mean_ratio = statistics.mean(ratios)
            if mean_ratio > 0 and len(ratios) > 2:
                ratio_cv = statistics.stdev(ratios) / mean_ratio

        geometric = ratio_cv < 0.05  # Very consistent ratio = geometric grid

        # Confidence based on regularity and count
        confidence = 0.0
        if regularity > 0.3 and most_common_count >= 3:
            confidence = min(1.0, regularity * 1.2)

        detected = confidence >= 0.3

        return GridBotSignal(
            detected=detected,
            num_grid_clusters=most_common_count,
            avg_spacing_bps=most_common_spacing if detected else 0.0,
            geometric=geometric,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # TWAP / scheduled bot detection
    # -------------------------------------------------------------------

    def _detect_twap_bots(self, trades: list[PublicTrade]) -> TWAPBotSignal:
        """Detect TWAP/scheduled bots: regular time intervals, consistent sizes.

        TWAP bots execute at fixed intervals (e.g., every 10s, 30s, 60s)
        with similar trade sizes to minimize market impact.
        """
        if len(trades) < 10:
            return TWAPBotSignal()

        # Compute inter-trade intervals
        intervals: list[float] = []
        for i in range(1, len(trades)):
            dt = trades[i].timestamp - trades[i - 1].timestamp
            if dt > 0.1:  # Skip sub-100ms bursts
                intervals.append(dt)

        if len(intervals) < 5:
            return TWAPBotSignal()

        # Look for dominant interval via histogram binning
        # Round to nearest second for clustering
        rounded_intervals = [round(dt) for dt in intervals if dt < 120]
        if len(rounded_intervals) < 5:
            return TWAPBotSignal()

        counter = Counter(rounded_intervals)
        dominant_interval, dominant_count = counter.most_common(1)[0]

        interval_regularity = dominant_count / len(rounded_intervals)

        # Check trade size consistency
        sizes = [float(t.qty) for t in trades]
        mean_size = statistics.mean(sizes)
        size_cv = statistics.stdev(sizes) / mean_size if mean_size > 0 and len(sizes) > 2 else 999.0

        # TWAP bots have low size CV (< 0.3) and regular intervals
        confidence = 0.0
        if interval_regularity > 0.2 and size_cv < 0.5:
            confidence = min(1.0, interval_regularity * (1 - size_cv) * 2)

        detected = confidence >= 0.3

        return TWAPBotSignal(
            detected=detected,
            dominant_interval_sec=float(dominant_interval) if detected else 0.0,
            size_cv=size_cv,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Iceberg order detection
    # -------------------------------------------------------------------

    def _detect_iceberg(self, trades: list[PublicTrade]) -> IcebergSignal:
        """Detect iceberg orders: repeated fills at same price, consistent size.

        Iceberg orders show as many small fills at the same price level,
        each with a similar clip size, as the hidden reserve refills.
        """
        if len(trades) < 10:
            return IcebergSignal()

        # Group trades by price level
        price_groups: dict[Decimal, list[Decimal]] = {}
        for t in trades:
            if t.price not in price_groups:
                price_groups[t.price] = []
            price_groups[t.price].append(t.qty)

        iceberg_levels = 0
        clip_sizes: list[float] = []

        for _price, quantities in price_groups.items():
            if len(quantities) < 4:
                continue

            # Check size consistency within this price level
            float_qtys = [float(q) for q in quantities]
            mean_q = statistics.mean(float_qtys)
            if mean_q <= 0:
                continue

            cv = statistics.stdev(float_qtys) / mean_q if len(float_qtys) > 2 else 999.0

            # Iceberg: many fills, consistent clip size (low CV)
            if cv < 0.3 and len(quantities) >= 4:
                iceberg_levels += 1
                clip_sizes.append(mean_q)

        confidence = 0.0
        if iceberg_levels > 0:
            # More iceberg levels = higher confidence
            confidence = min(1.0, iceberg_levels * 0.25)

        detected = confidence >= 0.25

        return IcebergSignal(
            detected=detected,
            num_iceberg_levels=iceberg_levels,
            avg_clip_size=statistics.mean(clip_sizes) if clip_sizes else 0.0,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Market maker detection
    # -------------------------------------------------------------------

    def _detect_market_makers(self, trades: list[PublicTrade]) -> MarketMakerSignal:
        """Detect market makers: rapid buy-sell alternation at tight spreads.

        Market makers continuously quote both sides. In the trade stream,
        this shows as frequent alternation between buy and sell fills near
        the mid-price with a tight spread between consecutive opposing trades.
        """
        if len(trades) < 10:
            return MarketMakerSignal()

        # Count buy-sell alternations
        alternations = 0
        spreads_bps: list[float] = []

        for i in range(1, len(trades)):
            if trades[i].side != trades[i - 1].side:
                alternations += 1
                # Spread between opposing trades
                mid = (trades[i].price + trades[i - 1].price) / 2
                if mid > 0:
                    spread = abs(float(
                        (trades[i].price - trades[i - 1].price) / mid * 10000
                    ))
                    spreads_bps.append(spread)

        total_transitions = len(trades) - 1
        alternation_ratio = alternations / total_transitions if total_transitions > 0 else 0.0

        avg_spread = statistics.mean(spreads_bps) if spreads_bps else 999.0

        # Market makers: high alternation ratio (>0.4) and tight spreads (<20 bps)
        confidence = 0.0
        if alternation_ratio > 0.35 and avg_spread < 30:
            spread_score = max(0, 1 - avg_spread / 30)
            confidence = min(1.0, alternation_ratio * spread_score * 2)

        detected = confidence >= 0.2

        return MarketMakerSignal(
            detected=detected,
            alternation_ratio=alternation_ratio,
            avg_spread_bps=avg_spread if detected else 0.0,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Momentum / trend bot detection
    # -------------------------------------------------------------------

    def _detect_momentum_bots(self, trades: list[PublicTrade]) -> MomentumBotSignal:
        """Detect momentum bots: bursts of same-direction trades.

        Momentum bots trigger on price breakouts and execute rapid sequences
        of same-direction trades. We look for unusually long consecutive runs
        of buy or sell trades that exceed random expectation.
        """
        if len(trades) < 10:
            return MomentumBotSignal()

        # Find consecutive runs
        max_run = 1
        current_run = 1
        burst_threshold = 5  # Consecutive same-side trades to count as burst
        burst_count = 0

        for i in range(1, len(trades)):
            if trades[i].side == trades[i - 1].side:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                if current_run >= burst_threshold:
                    burst_count += 1
                current_run = 1

        # Don't forget the last run
        if current_run >= burst_threshold:
            burst_count += 1

        # Under random 50/50, expected max run in N trades ≈ log2(N)
        expected_max_run = math.log2(len(trades)) if len(trades) > 1 else 1
        run_excess = max_run / expected_max_run if expected_max_run > 0 else 0

        confidence = 0.0
        if run_excess > 1.5 and burst_count >= 1:
            confidence = min(1.0, (run_excess - 1) * 0.5 + burst_count * 0.1)

        detected = confidence >= 0.2

        return MomentumBotSignal(
            detected=detected,
            max_consecutive_same_side=max_run,
            burst_count=burst_count,
            confidence=confidence,
        )
