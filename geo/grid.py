"""
GRTS grid generation and point -> cell lookup.

Grid
    Flat-topped hexagons are laid out in the UTM zone of the boundary centroid. ``cell_size_m`` is the
    side of the equal-area square, so a full hexagon covers ``cell_size_m²`` (a "1 km" grid has 1 km²
    cells) and its side is ``cell_size_m * sqrt(2 / (3 * sqrt(3)))``. Columns are ``1.5 * side`` apart
    and rows ``sqrt(3) * side`` apart; odd columns are shifted half a row north ("odd-q" offset layout).
    Column/row indices are absolute multiples of that spacing (so regenerating the same boundary yields
    the same cells). Each hexagon is clipped to the boundary; clipped pieces smaller than 10% of a full cell
    ("slivers") are dropped. If clipping splits a cell into several parts, the largest part is kept
    (the contract models a cell as a single Polygon).

GRTS ordering (after Stevens & Olsen 2004)
    1. Hexagon columns are offset, so cells are addressed by the position of their centres: the extent
       of the kept cell centres is scaled onto a ``2^L x 2^L`` index space (``L`` one level finer than
       the kept cells' column/row span), covered by a quadtree of ``L`` levels. At level ``k``
       (1 = coarsest) a cell falls in quadrant ``q_k = bit_k(col) + 2 * bit_k(row)`` of its parent node,
       so the top-level split falls at the middle of the area.
    2. Randomise: every quadtree node gets its own random permutation of its four quadrants (seeded
       from ``seed``, the level and the node's position, so the result is deterministic and independent
       of iteration order).
    3. Reverse-hierarchical interleave: each node orders its cells by taking one cell from each
       non-empty quadrant in turn (in the node's permuted order), recursively. Any prefix 1..n of the
       order is therefore spread across the quadrants at every scale — a ranger visiting cells in
       ``grts_order`` covers the area evenly even if they stop early. Unlike sorting by reversed
       base-4 addresses, this stays balanced when clipping to an irregular boundary leaves quadtree
       positions empty. Cells sharing a finest-level position are ordered by column, then row.
    4. Cells kept after clipping are ranked 1..N in that order → ``grts_order``; labels are
       ``GRTS-001`` … in that order (zero padding widens beyond 999 cells).

    ``grts_reverse_hierarchical_order`` (the classic address-reversal value for a complete grid) is kept
    for reference and tests.

Sectors
    Each cell is assigned to the nearest APU base (planar distance in UTM between cell centroid and
    base); with no bases, every cell goes to a single sector (``base_index = None``).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

from shapely.geometry import Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from .core import WGS84, GeoError, geojson_from_shape, reproject, to_local_metric

SLIVER_FRACTION = 0.10
MAX_CELLS = 20_000
HEX_SIDE_PER_CELL_SIZE = math.sqrt(2 / (3 * math.sqrt(3)))


def hexagon(cx: float, cy: float, side: float) -> Polygon:
    """Flat-topped regular hexagon centred on (cx, cy), counter-clockwise from the east vertex."""
    return Polygon([(cx + side * math.cos(math.radians(60 * i)), cy + side * math.sin(math.radians(60 * i))) for i in range(6)])


@dataclass
class GridCell:
    col: int
    row: int
    geometry: dict  # GeoJSON Polygon (WGS84)
    centroid: dict  # GeoJSON Point
    grts_order: int = 0
    label: str = ""
    base_index: int | None = None
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)


def _quadtree_levels(cols: int, rows: int) -> int:
    return max(1, math.ceil(math.log2(max(cols, rows, 2))))


def _node_permutation(seed: str, level: int, pcol: int, prow: int) -> list[int]:
    # str seeds are hashed with SHA-512 by random.seed -> stable across processes/PYTHONHASHSEED.
    rng = random.Random(f"{seed}:{level}:{pcol}:{prow}")
    perm = [0, 1, 2, 3]
    rng.shuffle(perm)
    return perm


def grts_reverse_hierarchical_order(indices: Sequence[tuple[int, int]], cols: int, rows: int, seed: str) -> list[int]:
    """
    Return the reverse-hierarchical sort value for each (col, row) in ``indices`` (see module doc).
    Values are unique for distinct cells.
    """
    levels = _quadtree_levels(cols, rows)
    values = []
    for col, row in indices:
        value = 0
        for k in range(1, levels + 1):
            shift = levels - k
            q = ((col >> shift) & 1) + 2 * ((row >> shift) & 1)
            parent_col, parent_row = col >> (shift + 1), row >> (shift + 1)
            digit = _node_permutation(seed, k, parent_col, parent_row)[q]
            value += digit * (4 ** (k - 1))
        values.append(value)
    return values


def grts_balanced_order(
    indices: Sequence[tuple[int, int]], levels: int, seed: str, tiebreak: Sequence[tuple] | None = None,
) -> list[int]:
    """
    Positions into ``indices`` in GRTS order (see module doc, step 3). ``indices`` are (col, row) in
    ``[0, 2**levels)``; ``tiebreak`` orders items that share a finest-level position.
    """
    keys = tiebreak if tiebreak is not None else list(indices)

    def order(items: list[int], level: int, pcol: int, prow: int) -> list[int]:
        if level > levels or len(items) <= 1:
            return sorted(items, key=lambda i: keys[i])
        shift = levels - level
        buckets: list[list[int]] = [[], [], [], []]
        for i in items:
            col, row = indices[i]
            buckets[((col >> shift) & 1) + 2 * ((row >> shift) & 1)].append(i)
        children = []
        for q in _node_permutation(seed, level, pcol, prow):
            if buckets[q]:
                children.append(order(buckets[q], level + 1, pcol * 2 + (q & 1), prow * 2 + (q >> 1)))
        out: list[int] = []
        for j in range(max(len(ch) for ch in children)):
            out.extend(ch[j] for ch in children if j < len(ch))
        return out

    return order(list(range(len(indices))), 1, 0, 0)


def build_grid(
    boundary: BaseGeometry,
    cell_size_m: int,
    base_points: Sequence[tuple[float, float]] = (),
    seed: str = "patroliq",
) -> list[GridCell]:
    """Generate clipped, GRTS-ordered cells for a WGS84 (Multi)Polygon boundary."""
    if cell_size_m < 50:
        raise GeoError("cell_size_m must be at least 50.", code="validation_error")
    metric, crs = to_local_metric(boundary)
    minx, miny, maxx, maxy = metric.bounds
    side = cell_size_m * HEX_SIDE_PER_CELL_SIZE
    dx, dy = 1.5 * side, math.sqrt(3) * side
    # One extra column/row each way so hexagons overlapping the bounds edge are included.
    c_from, c_to = math.floor(minx / dx) - 1, math.ceil(maxx / dx) + 1
    r_from, r_to = math.floor(miny / dy) - 1, math.ceil(maxy / dy) + 1
    if (c_to - c_from + 1) * (r_to - r_from + 1) > MAX_CELLS * 4:
        raise GeoError("Grid too large for this cell size; choose a larger cell_size_m.", code="grid_too_large")

    prepared = prep(metric)
    full_area = float(cell_size_m * cell_size_m)
    kept: list[tuple[int, int, Polygon]] = []
    for c in range(c_from, c_to + 1):
        for r in range(r_from, r_to + 1):
            cell = hexagon(c * dx, (r + 0.5 * (c % 2)) * dy, side)
            if not prepared.intersects(cell):
                continue
            if prepared.contains(cell):
                piece = cell
            else:
                clipped = metric.intersection(cell)
                parts = [g for g in getattr(clipped, "geoms", [clipped]) if isinstance(g, Polygon) and g.area > 0]
                if not parts:
                    continue
                piece = max(parts, key=lambda g: g.area)
                if piece.area < SLIVER_FRACTION * full_area:
                    continue
            kept.append((c, r, piece))
    if not kept:
        raise GeoError("No grid cells fit inside the boundary at this cell size.", code="grid_empty")
    if len(kept) > MAX_CELLS:
        raise GeoError("Grid too large for this cell size; choose a larger cell_size_m.", code="grid_too_large")

    span_c = max(c for c, _, _ in kept) - min(c for c, _, _ in kept) + 1
    span_r = max(r for _, r, _ in kept) - min(r for _, r, _ in kept) + 1
    levels = _quadtree_levels(span_c, span_r) + 1
    res = 2 ** levels
    centres = [(c * dx, (r + 0.5 * (c % 2)) * dy) for c, r, _ in kept]
    xmin, xmax = min(x for x, _ in centres), max(x for x, _ in centres)
    ymin, ymax = min(y for _, y in centres), max(y for _, y in centres)

    def scaled(v: float, lo: float, hi: float) -> int:
        return 0 if hi <= lo else min(res - 1, int((v - lo) / (hi - lo) * res))

    idx = [(scaled(x, xmin, xmax), scaled(y, ymin, ymax)) for x, y in centres]
    order = grts_balanced_order(idx, levels, seed, tiebreak=[(c, r) for c, r, _ in kept])
    width = max(3, len(str(len(kept))))

    bases_metric = [reproject(Point(lon, lat), WGS84, crs) for lon, lat in base_points]

    cells: list[GridCell] = []
    for rank, idx in enumerate(order, start=1):
        c, r, piece = kept[idx]
        centroid_m = piece.centroid
        base_index = None
        if bases_metric:
            base_index = min(range(len(bases_metric)), key=lambda b: bases_metric[b].distance(centroid_m))
        poly_wgs = reproject(piece, crs, WGS84)
        cen_wgs = reproject(centroid_m, crs, WGS84)
        cells.append(
            GridCell(
                col=c,
                row=r,
                geometry=geojson_from_shape(poly_wgs),
                centroid=geojson_from_shape(cen_wgs),
                grts_order=rank,
                label=f"GRTS-{rank:0{width}d}",
                base_index=base_index,
                bbox=poly_wgs.bounds,
            )
        )
    return cells


class CellIndex:
    """
    In-memory point -> cell lookup for one area: bbox pre-filter, then exact ``covers`` test.
    Build once per request/batch: ``CellIndex([(cell_id, geojson_polygon), ...])``.
    """

    def __init__(self, cells):
        from .core import shape_from_geojson

        self._items = []
        for cell_id, geometry in cells:
            geom = shape_from_geojson(geometry)
            self._items.append((cell_id, geom.bounds, prep(geom)))

    def __len__(self):
        return len(self._items)

    def find(self, lon: float, lat: float):
        pt = Point(lon, lat)
        for cell_id, (minx, miny, maxx, maxy), prepared in self._items:
            if minx <= lon <= maxx and miny <= lat <= maxy and prepared.covers(pt):
                return cell_id
        return None
