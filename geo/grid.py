"""
GRTS grid generation and point -> cell lookup.

Grid
    Square cells of ``cell_size_m`` are laid out in the UTM zone of the boundary centroid, with the
    origin snapped to a multiple of the cell size (so regenerating the same boundary yields the same
    cells). Each square is clipped to the boundary; clipped pieces smaller than 10% of a full cell
    ("slivers") are dropped. If clipping splits a cell into several parts, the largest part is kept
    (the contract models a cell as a single Polygon).

GRTS reverse-hierarchical ordering (Stevens & Olsen 2004)
    1. Cover the column/row index space of the kept cells (offset so the westernmost/southernmost
       kept cell is 0) with a quadtree of ``L = ceil(log2(max(cols, rows)))`` levels. At level ``k`` (1 = coarsest) a cell falls in quadrant
       ``q_k = bit_k(col) + 2 * bit_k(row)`` of its parent node.
    2. Randomise: every quadtree node gets its own random permutation of the four quadrant digits
       (seeded from ``seed``, the level and the node's position, so the result is deterministic and
       independent of iteration order). The cell's hierarchical address is the sequence of permuted
       digits ``d_1 d_2 … d_L`` (base 4).
    3. Reverse: read the address backwards, i.e. ``value = Σ d_k · 4^(k-1)`` (the coarsest digit is
       the least significant). Sorting cells by ``value`` interleaves quadrants at every scale, so any
       prefix 1..n of the order is a spatially balanced sample — a ranger visiting cells in
       ``grts_order`` covers the area evenly even if they stop early.
    4. Cells kept after clipping are ranked 1..N by ``value`` → ``grts_order``; labels are
       ``GRTS-001`` … in that order (zero padding widens beyond 999 cells).

Sectors
    Each cell is assigned to the nearest APU base (planar distance in UTM between cell centroid and
    base); with no bases, every cell goes to a single sector (``base_index = None``).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence

from shapely.geometry import Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from .core import WGS84, GeoError, geojson_from_shape, reproject, to_local_metric

SLIVER_FRACTION = 0.10
MAX_CELLS = 20_000


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
    x0 = math.floor(minx / cell_size_m) * cell_size_m
    y0 = math.floor(miny / cell_size_m) * cell_size_m
    cols = max(1, math.ceil((maxx - x0) / cell_size_m))
    rows = max(1, math.ceil((maxy - y0) / cell_size_m))
    if cols * rows > MAX_CELLS * 4:
        raise GeoError("Grid too large for this cell size; choose a larger cell_size_m.", code="grid_too_large")

    prepared = prep(metric)
    full_area = float(cell_size_m * cell_size_m)
    kept: list[tuple[int, int, Polygon]] = []
    for c in range(cols):
        for r in range(rows):
            square = box(x0 + c * cell_size_m, y0 + r * cell_size_m, x0 + (c + 1) * cell_size_m, y0 + (r + 1) * cell_size_m)
            if not prepared.intersects(square):
                continue
            if prepared.contains(square):
                piece = square
            else:
                clipped = metric.intersection(square)
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

    # Build the quadtree over the extent of the cells actually kept (not the snapped bounding grid),
    # so the top-level split matches the area and early samples are balanced across it.
    cmin, rmin = min(c for c, _, _ in kept), min(r for _, r, _ in kept)
    span_c = max(c for c, _, _ in kept) - cmin + 1
    span_r = max(r for _, r, _ in kept) - rmin + 1
    values = grts_reverse_hierarchical_order([(c - cmin, r - rmin) for c, r, _ in kept], span_c, span_r, seed)
    order = sorted(range(len(kept)), key=lambda i: values[i])
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
