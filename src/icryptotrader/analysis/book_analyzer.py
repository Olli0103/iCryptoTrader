"""Order book pattern analyzer — detects systematic placement patterns.

Analyses L2 order book snapshots over time to detect:

1. **Symmetric liquidity walls**: Equal-sized orders on both sides at regular
   intervals — signature of market-making bots.
2. **Spoofing patterns**: Large orders that appear and disappear within seconds,
   intended to manipulate price perception.
3. **Layering**: Multiple orders stacked at incremental price levels on one side,
   creating an illusion of deep support/resistance.
4. **Quote stuffing**: Rapid add/cancel cycles that consume rate limits.

Works with the existing ``OrderBook`` (L2) from ``ws.book_manager``.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal  # noqa: TC003 — used at runtime in dataclass fields

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BookSnapshot:
    """A point-in-time snapshot of the top-N order book levels."""

    timestamp: float
    bids: list[tuple[Decimal, Decimal]]  # [(price, qty), ...] descending
    asks: list[tuple[Decimal, Decimal]]  # [(price, qty), ...] ascending
    mid_price: Decimal
    spread_bps: Decimal


@dataclass
class SymmetrySignal:
    """Evidence of symmetric market-maker quoting."""

    detected: bool = False
    avg_symmetry_score: float = 0.0  # 0=asymmetric, 1=perfectly symmetric
    num_symmetric_snapshots: int = 0
    confidence: float = 0.0


@dataclass
class SpoofingSignal:
    """Evidence of spoofing / phantom liquidity."""

    detected: bool = False
    vanished_orders: int = 0  # large orders that appeared and disappeared
    avg_lifespan_sec: float = 0.0
    confidence: float = 0.0


@dataclass
class LayeringSignal:
    """Evidence of layering — stacked one-sided orders."""

    detected: bool = False
    bid_layers: int = 0
    ask_layers: int = 0
    max_imbalance_ratio: float = 0.0  # bid_depth / ask_depth ratio
    confidence: float = 0.0


@dataclass
class QuoteStuffingSignal:
    """Evidence of rapid quote updates (stuffing / flickering)."""

    detected: bool = False
    changes_per_sec: float = 0.0
    confidence: float = 0.0


@dataclass
class BookAnalysisReport:
    """Complete order book analysis report."""

    snapshots_analyzed: int
    window_sec: float
    avg_spread_bps: float
    avg_bid_depth: float  # total bid qty in top levels
    avg_ask_depth: float  # total ask qty in top levels

    symmetry: SymmetrySignal = field(default_factory=SymmetrySignal)
    spoofing: SpoofingSignal = field(default_factory=SpoofingSignal)
    layering: LayeringSignal = field(default_factory=LayeringSignal)
    quote_stuffing: QuoteStuffingSignal = field(default_factory=QuoteStuffingSignal)

    def summary(self) -> str:
        """Human-readable summary of order book analysis."""
        lines = [
            "=== Order Book Pattern Analysis ===",
            f"Snapshots: {self.snapshots_analyzed} | "
            f"Window: {self.window_sec:.0f}s",
            f"Avg spread: {self.avg_spread_bps:.1f} bps",
            f"Avg depth: {self.avg_bid_depth:.4f} (bids) / "
            f"{self.avg_ask_depth:.4f} (asks)",
            "",
        ]

        s = self.symmetry
        status = "DETECTED" if s.detected else "not detected"
        lines.append(
            f"[Symmetric MM Quoting] {status} "
            f"(confidence: {s.confidence:.0%})"
        )
        if s.detected:
            lines.append(
                f"  Symmetry score: {s.avg_symmetry_score:.2f} | "
                f"Symmetric snapshots: {s.num_symmetric_snapshots}"
            )

        sp = self.spoofing
        status = "DETECTED" if sp.detected else "not detected"
        lines.append(
            f"[Spoofing / Phantom Liquidity] {status} "
            f"(confidence: {sp.confidence:.0%})"
        )
        if sp.detected:
            lines.append(
                f"  Vanished orders: {sp.vanished_orders} | "
                f"Avg lifespan: {sp.avg_lifespan_sec:.1f}s"
            )

        la = self.layering
        status = "DETECTED" if la.detected else "not detected"
        lines.append(
            f"[Layering] {status} "
            f"(confidence: {la.confidence:.0%})"
        )
        if la.detected:
            lines.append(
                f"  Bid layers: {la.bid_layers} | Ask layers: {la.ask_layers} | "
                f"Max imbalance: {la.max_imbalance_ratio:.1f}x"
            )

        qs = self.quote_stuffing
        status = "DETECTED" if qs.detected else "not detected"
        lines.append(
            f"[Quote Stuffing] {status} "
            f"(confidence: {qs.confidence:.0%})"
        )
        if qs.detected:
            lines.append(
                f"  Changes/sec: {qs.changes_per_sec:.1f}"
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class BookAnalyzer:
    """Analyses order book snapshots over time to detect manipulation patterns.

    Usage::

        analyzer = BookAnalyzer(window_sec=300)
        # Feed snapshots from the OrderBook on every update:
        analyzer.record_snapshot(bids, asks, mid_price, spread_bps)
        # Run analysis:
        report = analyzer.analyze()
        print(report.summary())
    """

    def __init__(
        self,
        window_sec: float = 300.0,
        max_snapshots: int = 10_000,
        clock: object | None = None,
    ) -> None:
        self._window_sec = window_sec
        self._snapshots: deque[BookSnapshot] = deque(maxlen=max_snapshots)
        self._clock = clock

        # Track large orders across snapshots for spoofing detection
        # Key: (side, price), Value: (first_seen_ts, last_seen_ts, qty)
        self._tracked_orders: dict[tuple[str, Decimal], tuple[float, float, Decimal]] = {}
        self._vanished_large: deque[tuple[float, Decimal]] = deque(maxlen=1000)

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()  # type: ignore[operator,no-any-return]
        return time.monotonic()

    def record_snapshot(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        mid_price: Decimal,
        spread_bps: Decimal,
    ) -> None:
        """Record an order book snapshot.

        Args:
            bids: [(price, qty), ...] sorted descending by price.
            asks: [(price, qty), ...] sorted ascending by price.
            mid_price: Current mid price.
            spread_bps: Current spread in basis points.
        """
        now = self._now()
        self._snapshots.append(BookSnapshot(
            timestamp=now,
            bids=list(bids),
            asks=list(asks),
            mid_price=mid_price,
            spread_bps=spread_bps,
        ))
        self._track_large_orders(bids, asks, now)

    def _track_large_orders(
        self,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        now: float,
    ) -> None:
        """Track appearance/disappearance of large orders for spoof detection."""
        # Determine "large" threshold: 3x median qty across all levels
        all_qtys = [q for _, q in bids] + [q for _, q in asks]
        if not all_qtys:
            return

        sorted_qtys = sorted(all_qtys)
        median_qty = sorted_qtys[len(sorted_qtys) // 2]
        large_threshold = median_qty * 3

        current_keys: set[tuple[str, Decimal]] = set()

        for price, qty in bids:
            if qty >= large_threshold:
                key = ("bid", price)
                current_keys.add(key)
                if key not in self._tracked_orders:
                    self._tracked_orders[key] = (now, now, qty)
                else:
                    first, _, _ = self._tracked_orders[key]
                    self._tracked_orders[key] = (first, now, qty)

        for price, qty in asks:
            if qty >= large_threshold:
                key = ("ask", price)
                current_keys.add(key)
                if key not in self._tracked_orders:
                    self._tracked_orders[key] = (now, now, qty)
                else:
                    first, _, _ = self._tracked_orders[key]
                    self._tracked_orders[key] = (first, now, qty)

        # Check for vanished large orders (existed last snapshot, gone now)
        vanished_keys = set(self._tracked_orders.keys()) - current_keys
        for key in vanished_keys:
            first_seen, last_seen, qty = self._tracked_orders.pop(key)
            lifespan = last_seen - first_seen
            # Spoof candidate: existed briefly (< 10s) and never got filled
            if lifespan < 10.0:
                self._vanished_large.append((lifespan, qty))

    @property
    def snapshot_count(self) -> int:
        return len(self._snapshots)

    def _window_snapshots(self) -> list[BookSnapshot]:
        """Return snapshots within the analysis window."""
        now = self._now()
        cutoff = now - self._window_sec
        while self._snapshots and self._snapshots[0].timestamp < cutoff:
            self._snapshots.popleft()
        return list(self._snapshots)

    def analyze(self) -> BookAnalysisReport:
        """Run full order book pattern analysis."""
        snapshots = self._window_snapshots()

        if len(snapshots) < 3:
            return BookAnalysisReport(
                snapshots_analyzed=len(snapshots),
                window_sec=self._window_sec,
                avg_spread_bps=0.0,
                avg_bid_depth=0.0,
                avg_ask_depth=0.0,
            )

        # Compute averages
        spreads = [float(s.spread_bps) for s in snapshots]
        bid_depths = [sum(float(q) for _, q in s.bids) for s in snapshots]
        ask_depths = [sum(float(q) for _, q in s.asks) for s in snapshots]

        avg_spread = statistics.mean(spreads) if spreads else 0.0
        avg_bid = statistics.mean(bid_depths) if bid_depths else 0.0
        avg_ask = statistics.mean(ask_depths) if ask_depths else 0.0

        report = BookAnalysisReport(
            snapshots_analyzed=len(snapshots),
            window_sec=self._window_sec,
            avg_spread_bps=avg_spread,
            avg_bid_depth=avg_bid,
            avg_ask_depth=avg_ask,
        )

        report.symmetry = self._detect_symmetry(snapshots)
        report.spoofing = self._detect_spoofing()
        report.layering = self._detect_layering(snapshots)
        report.quote_stuffing = self._detect_quote_stuffing(snapshots)

        return report

    # -------------------------------------------------------------------
    # Symmetric MM quoting
    # -------------------------------------------------------------------

    def _detect_symmetry(self, snapshots: list[BookSnapshot]) -> SymmetrySignal:
        """Detect symmetric market-maker quoting patterns.

        Market makers typically place equal-sized orders at symmetric
        distances from the mid-price on both sides.
        """
        if len(snapshots) < 3:
            return SymmetrySignal()

        symmetry_scores: list[float] = []

        for snap in snapshots:
            if not snap.bids or not snap.asks:
                continue

            # Compare bid/ask quantities at each level
            n = min(len(snap.bids), len(snap.asks))
            if n == 0:
                continue

            level_scores: list[float] = []
            for i in range(n):
                bid_qty = float(snap.bids[i][1])
                ask_qty = float(snap.asks[i][1])
                total = bid_qty + ask_qty
                if total > 0:
                    # Symmetry = 1 - |diff| / sum
                    score = 1.0 - abs(bid_qty - ask_qty) / total
                    level_scores.append(score)

            if level_scores:
                symmetry_scores.append(
                    sum(level_scores) / len(level_scores)
                )

        if not symmetry_scores:
            return SymmetrySignal()

        avg_score = statistics.mean(symmetry_scores)
        num_symmetric = sum(1 for s in symmetry_scores if s > 0.7)

        confidence = 0.0
        if avg_score > 0.6:
            confidence = min(1.0, (avg_score - 0.5) * 2)

        return SymmetrySignal(
            detected=confidence >= 0.3,
            avg_symmetry_score=avg_score,
            num_symmetric_snapshots=num_symmetric,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Spoofing detection
    # -------------------------------------------------------------------

    def _detect_spoofing(self) -> SpoofingSignal:
        """Detect spoofing: large orders that appear and vanish quickly."""
        if not self._vanished_large:
            return SpoofingSignal()

        lifespans = [ls for ls, _ in self._vanished_large]
        count = len(self._vanished_large)

        avg_lifespan = statistics.mean(lifespans) if lifespans else 0.0

        # More vanished large orders with short lifespans = higher confidence
        confidence = 0.0
        if count >= 2:
            confidence = min(1.0, count * 0.15)
            if avg_lifespan < 3.0:
                confidence = min(1.0, confidence * 1.5)

        return SpoofingSignal(
            detected=confidence >= 0.2,
            vanished_orders=count,
            avg_lifespan_sec=avg_lifespan,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Layering detection
    # -------------------------------------------------------------------

    def _detect_layering(self, snapshots: list[BookSnapshot]) -> LayeringSignal:
        """Detect layering: heavily stacked orders on one side.

        Layering creates an illusion of deep support/resistance by placing
        many orders at incrementally decreasing prices on one side, while
        intending to trade on the other side.
        """
        if len(snapshots) < 3:
            return LayeringSignal()

        max_imbalance = 0.0
        bid_layer_counts: list[int] = []
        ask_layer_counts: list[int] = []

        for snap in snapshots:
            bid_total = sum(float(q) for _, q in snap.bids) if snap.bids else 0.0
            ask_total = sum(float(q) for _, q in snap.asks) if snap.asks else 0.0

            if ask_total > 0:
                ratio = bid_total / ask_total
                max_imbalance = max(max_imbalance, ratio, 1 / ratio)

            bid_layer_counts.append(len(snap.bids))
            ask_layer_counts.append(len(snap.asks))

        avg_bid_layers = statistics.mean(bid_layer_counts) if bid_layer_counts else 0
        avg_ask_layers = statistics.mean(ask_layer_counts) if ask_layer_counts else 0

        # Layering: persistent high imbalance (>3x) with many levels
        confidence = 0.0
        if max_imbalance > 2.0:
            confidence = min(1.0, (max_imbalance - 1.5) * 0.3)

        return LayeringSignal(
            detected=confidence >= 0.2,
            bid_layers=round(avg_bid_layers),
            ask_layers=round(avg_ask_layers),
            max_imbalance_ratio=max_imbalance,
            confidence=confidence,
        )

    # -------------------------------------------------------------------
    # Quote stuffing detection
    # -------------------------------------------------------------------

    def _detect_quote_stuffing(
        self, snapshots: list[BookSnapshot],
    ) -> QuoteStuffingSignal:
        """Detect quote stuffing: unusually rapid book changes.

        Quote stuffing involves rapidly adding and cancelling orders to
        slow down other participants' systems.
        """
        if len(snapshots) < 10:
            return QuoteStuffingSignal()

        # Count how many snapshots show different top-of-book levels
        changes = 0
        for i in range(1, len(snapshots)):
            prev_bid = snapshots[i - 1].bids[0] if snapshots[i - 1].bids else None
            curr_bid = snapshots[i].bids[0] if snapshots[i].bids else None
            prev_ask = snapshots[i - 1].asks[0] if snapshots[i - 1].asks else None
            curr_ask = snapshots[i].asks[0] if snapshots[i].asks else None

            if prev_bid != curr_bid or prev_ask != curr_ask:
                changes += 1

        time_span = snapshots[-1].timestamp - snapshots[0].timestamp
        changes_per_sec = changes / time_span if time_span > 0 else 0.0

        # High change rate = potential stuffing
        # Normal: 1-5 changes/sec. Stuffing: 10+/sec
        confidence = 0.0
        if changes_per_sec > 5:
            confidence = min(1.0, (changes_per_sec - 5) / 15)

        return QuoteStuffingSignal(
            detected=confidence >= 0.2,
            changes_per_sec=changes_per_sec,
            confidence=confidence,
        )
