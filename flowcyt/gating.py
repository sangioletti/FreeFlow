"""
gating.py - Gate definitions and population statistics.

Supports polygon and rectangle gates.  Each gate stores its geometry,
the parent data shape, and can test which events fall inside.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

import numpy as np
from matplotlib.path import Path as MplPath


# ------------------------------------------------------------------ #
#  Gate classes
# ------------------------------------------------------------------ #

@dataclass
class Gate:
    """Base for all gate types."""
    name: str
    x_channel: str
    y_channel: str
    color: str = "#ff0000"
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    parent_gate_uid: str | None = None  # Parent gate for hierarchical gating

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """Return boolean mask of events inside this gate."""
        raise NotImplementedError


@dataclass
class PolygonGate(Gate):
    """Gate defined by an arbitrary polygon (list of (x, y) vertices)."""
    vertices: list[tuple[float, float]] = field(default_factory=list)

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if len(self.vertices) < 3:
            return np.zeros(len(x), dtype=bool)
        path = MplPath(self.vertices)
        points = np.column_stack([x, y])
        return path.contains_points(points)


@dataclass
class RectangleGate(Gate):
    """Gate defined by axis-aligned rectangle."""
    x_min: float = 0.0
    x_max: float = 0.0
    y_min: float = 0.0
    y_max: float = 0.0

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return (
            (x >= self.x_min)
            & (x <= self.x_max)
            & (y >= self.y_min)
            & (y <= self.y_max)
        )

    @property
    def vertices(self) -> list[tuple[float, float]]:
        return [
            (self.x_min, self.y_min),
            (self.x_max, self.y_min),
            (self.x_max, self.y_max),
            (self.x_min, self.y_max),
        ]


@dataclass
class QuadrantGate(Gate):
    """Gate defined by one quadrant of a crosshair split.

    The crosshair is placed at (mid_x, mid_y).  *quadrant* selects
    which of the four regions counts as "inside":
      Q1 = upper-right  (x >= mid_x, y >= mid_y)
      Q2 = upper-left   (x <  mid_x, y >= mid_y)
      Q3 = lower-left   (x <  mid_x, y <  mid_y)
      Q4 = lower-right  (x >= mid_x, y <  mid_y)
    """
    mid_x: float = 0.0
    mid_y: float = 0.0
    quadrant: str = "Q1"

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if self.quadrant == "Q1":
            return (x >= self.mid_x) & (y >= self.mid_y)
        elif self.quadrant == "Q2":
            return (x < self.mid_x) & (y >= self.mid_y)
        elif self.quadrant == "Q3":
            return (x < self.mid_x) & (y < self.mid_y)
        elif self.quadrant == "Q4":
            return (x >= self.mid_x) & (y < self.mid_y)
        return np.zeros(len(x), dtype=bool)

    def quadrant_masks(self, x: np.ndarray, y: np.ndarray) -> dict[str, np.ndarray]:
        """Return boolean masks for all four quadrants of this gate.

        Independent of which quadrant is the "selected" one — useful for
        rendering counts/percentages on every quadrant of the crosshair.
        """
        return {
            "Q1": (x >= self.mid_x) & (y >= self.mid_y),
            "Q2": (x <  self.mid_x) & (y >= self.mid_y),
            "Q3": (x <  self.mid_x) & (y <  self.mid_y),
            "Q4": (x >= self.mid_x) & (y <  self.mid_y),
        }

    @property
    def vertices(self) -> list[tuple[float, float]]:
        """Quadrant gates don't have polygon vertices — return empty.

        Drawing is handled specially via crosshair lines.
        """
        return []


@dataclass
class ThresholdGate(Gate):
    """Gate defined by a threshold on a single channel (1D gating).

    *side* selects which side of the threshold counts as "inside":
      "left"  → x < threshold
      "right" → x >= threshold
    The y_channel is stored for bookkeeping but not used for containment.
    """
    threshold: float = 0.0
    channel: str = ""     # the channel being thresholded (== x_channel)
    side: str = "right"   # "left" or "right"

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if self.side == "left":
            return x < self.threshold
        else:
            return x >= self.threshold

    @property
    def vertices(self) -> list[tuple[float, float]]:
        """Threshold gates don't have polygon vertices."""
        return []


@dataclass
class EllipseGate(Gate):
    """Gate defined by an ellipse (center + semi-axes + rotation angle)."""
    center_x: float = 0.0
    center_y: float = 0.0
    semi_x: float = 0.0  # Semi-axis in x direction
    semi_y: float = 0.0  # Semi-axis in y direction
    angle: float = 0.0   # Rotation angle in radians (0 for axis-aligned)

    def contains(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if self.semi_x == 0 or self.semi_y == 0:
            return np.zeros(len(x), dtype=bool)

        # Translate to ellipse center
        x_t = x - self.center_x
        y_t = y - self.center_y

        # Rotate by -angle to align ellipse with axes
        cos_a = np.cos(-self.angle)
        sin_a = np.sin(-self.angle)
        x_r = x_t * cos_a - y_t * sin_a
        y_r = x_t * sin_a + y_t * cos_a

        # Check ellipse equation: (x_r/a)^2 + (y_r/b)^2 <= 1
        return ((x_r / self.semi_x) ** 2 + (y_r / self.semi_y) ** 2) <= 1.0

    @property
    def vertices(self) -> list[tuple[float, float]]:
        """Return points on ellipse perimeter for visualization."""
        angles = np.linspace(0, 2 * np.pi, 100)
        cos_a = np.cos(self.angle)
        sin_a = np.sin(self.angle)

        verts = []
        for theta in angles:
            # Ellipse equation in standard position
            x_e = self.semi_x * np.cos(theta)
            y_e = self.semi_y * np.sin(theta)
            # Rotate and translate
            x = self.center_x + x_e * cos_a - y_e * sin_a
            y = self.center_y + x_e * sin_a + y_e * cos_a
            verts.append((x, y))
        return verts


# ------------------------------------------------------------------ #
#  Gate manager — keeps a list of gates, computes stats
# ------------------------------------------------------------------ #

GATE_COLORS = [
    "#e6194b", "#3cb44b", "#4363d8", "#f58231",
    "#911eb4", "#42d4f4", "#f032e6", "#bfef45",
    "#fabed4", "#469990", "#dcbeff", "#9A6324",
]


class GateManager:
    """Holds all gates and computes population statistics."""

    def __init__(self):
        self.gates: list[Gate] = []
        self._color_idx = 0

    def _next_color(self) -> str:
        c = GATE_COLORS[self._color_idx % len(GATE_COLORS)]
        self._color_idx += 1
        return c

    def add_polygon_gate(
        self,
        name: str,
        x_channel: str,
        y_channel: str,
        vertices: list[tuple[float, float]],
        parent_gate_uid: str | None = None,
    ) -> PolygonGate:
        gate = PolygonGate(
            name=name,
            x_channel=x_channel,
            y_channel=y_channel,
            vertices=vertices,
            color=self._next_color(),
            parent_gate_uid=parent_gate_uid,
        )
        self.gates.append(gate)
        return gate

    def add_rectangle_gate(
        self,
        name: str,
        x_channel: str,
        y_channel: str,
        x_min: float,
        x_max: float,
        y_min: float,
        y_max: float,
        parent_gate_uid: str | None = None,
    ) -> RectangleGate:
        gate = RectangleGate(
            name=name,
            x_channel=x_channel,
            y_channel=y_channel,
            x_min=x_min,
            x_max=x_max,
            y_min=y_min,
            y_max=y_max,
            color=self._next_color(),
            parent_gate_uid=parent_gate_uid,
        )
        self.gates.append(gate)
        return gate

    def add_quadrant_gate(
        self,
        name: str,
        x_channel: str,
        y_channel: str,
        mid_x: float,
        mid_y: float,
        quadrant: str,
        parent_gate_uid: str | None = None,
    ) -> QuadrantGate:
        gate = QuadrantGate(
            name=name,
            x_channel=x_channel,
            y_channel=y_channel,
            mid_x=mid_x,
            mid_y=mid_y,
            quadrant=quadrant,
            color=self._next_color(),
            parent_gate_uid=parent_gate_uid,
        )
        self.gates.append(gate)
        return gate

    def add_threshold_gate(
        self,
        name: str,
        x_channel: str,
        y_channel: str,
        threshold: float,
        side: str = "right",
        parent_gate_uid: str | None = None,
    ) -> ThresholdGate:
        gate = ThresholdGate(
            name=name,
            x_channel=x_channel,
            y_channel=y_channel,
            threshold=threshold,
            channel=x_channel,
            side=side,
            color=self._next_color(),
            parent_gate_uid=parent_gate_uid,
        )
        self.gates.append(gate)
        return gate

    def add_ellipse_gate(
        self,
        name: str,
        x_channel: str,
        y_channel: str,
        center_x: float,
        center_y: float,
        semi_x: float,
        semi_y: float,
        angle: float = 0.0,
        parent_gate_uid: str | None = None,
    ) -> EllipseGate:
        gate = EllipseGate(
            name=name,
            x_channel=x_channel,
            y_channel=y_channel,
            center_x=center_x,
            center_y=center_y,
            semi_x=semi_x,
            semi_y=semi_y,
            angle=angle,
            color=self._next_color(),
            parent_gate_uid=parent_gate_uid,
        )
        self.gates.append(gate)
        return gate

    def remove_gate(self, uid: str):
        self.gates = [g for g in self.gates if g.uid != uid]

    def clear(self):
        self.gates.clear()
        self._color_idx = 0

    def _topological_sort(self) -> list[Gate]:
        """Sort gates so parents come before children."""
        visited = set()
        order = []

        def visit(gate):
            if gate.uid in visited:
                return
            visited.add(gate.uid)

            # Visit parent first
            if gate.parent_gate_uid:
                parent = next(
                    (g for g in self.gates if g.uid == gate.parent_gate_uid), None
                )
                if parent:
                    visit(parent)

            order.append(gate)

        for gate in self.gates:
            visit(gate)

        return order

    # ------------------------------------------------------------------ #
    #  Statistics
    # ------------------------------------------------------------------ #
    def compute_stats(
        self, data: np.ndarray, channel_names: list[str]
    ) -> list[dict]:
        """
        Return per-gate statistics with parent-child hierarchy.

        Each dict contains:
          name, uid, color, count, percent, medians (per channel),
          parent_uid (for hierarchical display)
        """
        total = len(data)
        results = []

        # Sort gates in topological order (parents before children)
        sorted_gates = self._topological_sort()

        # Track masks and counts for parent gates
        parent_masks = {}
        parent_counts = {}

        for gate in sorted_gates:
            try:
                xi = channel_names.index(gate.x_channel)
                yi = channel_names.index(gate.y_channel)
            except ValueError:
                continue

            # Get gate mask
            mask = gate.contains(data[:, xi], data[:, yi])

            # Apply parent mask if this is a child gate
            parent_count = total  # Default to total for percentage calculation
            if gate.parent_gate_uid:
                if gate.parent_gate_uid in parent_masks:
                    parent_mask = parent_masks[gate.parent_gate_uid]
                    mask = mask & parent_mask
                    # For child gates, percentage is relative to parent
                    parent_count = parent_counts[gate.parent_gate_uid]
                    logger.debug("Gate '%s' is child of parent_uid=%s",
                                gate.name, gate.parent_gate_uid)
                    logger.debug("  Parent count: %s, Child count: %s",
                                f"{parent_count:,}", f"{mask.sum():,}")
                else:
                    logger.warning("Gate '%s' has parent_uid=%s but parent not found in masks!",
                                   gate.name, gate.parent_gate_uid)
                    logger.warning("  Available parent UIDs: %s", list(parent_masks.keys()))

            # Store mask and count for potential child gates
            parent_masks[gate.uid] = mask
            count = int(mask.sum())
            parent_counts[gate.uid] = count

            # Calculate percentage relative to parent (or total if no parent)
            pct = 100.0 * count / parent_count if parent_count else 0.0

            if gate.parent_gate_uid:
                logger.debug("  Percentage: %s / %s = %.1f%%",
                            f"{count:,}", f"{parent_count:,}", pct)
            else:
                logger.debug("Top-level gate '%s': %s / %s = %.1f%%",
                            gate.name, f"{count:,}", f"{total:,}", pct)

            medians = {}
            for ci, ch in enumerate(channel_names):
                if count > 0:
                    medians[ch] = float(np.median(data[mask, ci]))
                else:
                    medians[ch] = 0.0

            pct_of_total = 100.0 * count / total if total else 0.0
            entry = {
                "name": gate.name,
                "uid": gate.uid,
                "color": gate.color,
                "count": count,
                "total": total,
                "percent": pct,               # % of parent (or total if no parent)
                "percent_of_total": pct_of_total,  # always % of all events
                "parent_count": parent_count,
                "medians": medians,
                "parent_uid": gate.parent_gate_uid,
            }

            # For quadrant gates also report counts and percentages for the
            # *other* three quadrants so the UI can render labels on every
            # quadrant and the stats panel can list all four.
            if isinstance(gate, QuadrantGate):
                qmasks = gate.quadrant_masks(data[:, xi], data[:, yi])
                if gate.parent_gate_uid and gate.parent_gate_uid in parent_masks:
                    pm = parent_masks[gate.parent_gate_uid]
                    qmasks = {q: (m & pm) for q, m in qmasks.items()}
                # Denominator for "% of parent" matches what we use for the
                # selected-quadrant percentage above.
                denom = parent_count if parent_count else total
                breakdown = {}
                for q, qm in qmasks.items():
                    qc = int(qm.sum())
                    breakdown[q] = {
                        "count": qc,
                        "percent": (100.0 * qc / denom) if denom else 0.0,
                        "percent_of_total": (100.0 * qc / total) if total else 0.0,
                    }
                entry["quadrant_breakdown"] = breakdown
                entry["selected_quadrant"] = gate.quadrant

            results.append(entry)
        return results
