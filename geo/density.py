"""
Kernel density estimation for point patterns (spec v1.5 §C) — the maths only, no Django.

The estimator is the classic planar kernel density surface used by ArcGIS "Kernel Density" and by
QGIS "Heatmap"::

    density(s) = Σ_i w_i · K(‖s − x_i‖ ; h)

with the points ``x_i`` and the evaluation locations ``s`` in a **local metric plane** (metres) and
the kernel ``K`` normalised so that ``∫ K dA = 1``. Consequently the surface integrates to the total
weight ``Σ w_i`` — which is what :func:`quartic_kernel` / :func:`gaussian_kernel` are tested against.

Kernels
-------
``quartic`` (default; the biweight kernel ArcGIS uses, Silverman 1986 eq. 4.5)::

    K(d) = 3 / (π h²) · (1 − (d/h)²)²        for d < h, else 0

``gaussian`` (truncated at 3 h so the sum stays local; the truncation loses ≈ 1.1 % of the mass)::

    K(d) = 1 / (2π h²) · exp(−d² / (2 h²))    for d < 3 h, else 0

Bandwidth
---------
:func:`silverman_bandwidth` implements the **spatial Silverman rule of thumb** that ArcGIS uses as
its default Kernel Density search radius (Silverman 1986, "Density Estimation for Statistics and
Data Analysis", eq. 3.31, adapted to two dimensions by ESRI)::

    h = 0.9 · min( SD , sqrt(1 / ln 2) · D_m ) · n^(−0.2)

where

* ``SD`` is the weighted **standard distance** — the root-mean-square distance of the points from
  their weighted mean centre, ``sqrt( Σ w_i (x_i − x̄)² / Σw + Σ w_i (y_i − ȳ)² / Σw )``;
* ``D_m`` is the weighted **median distance** from the weighted mean centre;
* ``n`` is the total weight (the "population field" sum in ESRI's wording).

The ``sqrt(1/ln 2) · D_m`` term makes the rule robust to a few far outliers, and the ``n^(−0.2)``
term shrinks the bandwidth as the sample grows. The result is clamped to
``[MIN_BANDWIDTH_M, MAX_BANDWIDTH_M]`` and falls back to :data:`DEFAULT_BANDWIDTH_M` for fewer than
two points, where neither SD nor D_m is defined.

Projection
----------
:class:`LocalPlane` is a local equidistant-cylindrical (plate carrée) projection centred on the area:
``x = R·cos(lat0)·Δlon`` and ``y = R·Δlat`` in radians. Over a conservation area (tens of km) its
scale error against UTM is well under 0.1 %, and unlike UTM it maps a *regular lon/lat grid onto a
regular metric grid* — which is exactly what the raster the dashboard draws needs.
"""
from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6_371_008.8

MIN_BANDWIDTH_M = 150.0
MAX_BANDWIDTH_M = 5000.0
DEFAULT_BANDWIDTH_M = 1000.0

KERNELS = ("quartic", "gaussian")
#: How many bandwidths away a kernel still contributes.
KERNEL_REACH = {"quartic": 1.0, "gaussian": 3.0}


class LocalPlane:
    """Local equidistant-cylindrical metre plane centred on ``(lon0, lat0)``."""

    __slots__ = ("lon0", "lat0", "m_per_deg_lon", "m_per_deg_lat")

    def __init__(self, lon0: float, lat0: float):
        self.lon0, self.lat0 = float(lon0), float(lat0)
        rad = math.pi / 180.0
        self.m_per_deg_lat = EARTH_RADIUS_M * rad
        self.m_per_deg_lon = EARTH_RADIUS_M * rad * max(math.cos(self.lat0 * rad), 1e-6)

    def to_xy(self, lon, lat):
        lon, lat = np.asarray(lon, dtype=float), np.asarray(lat, dtype=float)
        return (lon - self.lon0) * self.m_per_deg_lon, (lat - self.lat0) * self.m_per_deg_lat

    def to_lonlat(self, x, y):
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        return x / self.m_per_deg_lon + self.lon0, y / self.m_per_deg_lat + self.lat0

    def deg_per_m(self) -> tuple[float, float]:
        """(degrees of longitude, degrees of latitude) per metre at the plane's centre."""
        return 1.0 / self.m_per_deg_lon, 1.0 / self.m_per_deg_lat


# --- kernels -------------------------------------------------------------------------------------

def quartic_kernel(d2: np.ndarray, h: float) -> np.ndarray:
    """``3/(π h²)·(1 − (d/h)²)²`` for ``d < h``; ``d2`` is the *squared* distance in m²."""
    u = np.clip(1.0 - d2 / (h * h), 0.0, None)
    return (3.0 / (math.pi * h * h)) * u * u


def gaussian_kernel(d2: np.ndarray, h: float) -> np.ndarray:
    """``1/(2π h²)·exp(−d²/2h²)`` truncated at ``3h``; ``d2`` is the squared distance in m²."""
    out = (1.0 / (2.0 * math.pi * h * h)) * np.exp(-d2 / (2.0 * h * h))
    return np.where(d2 <= (3.0 * h) ** 2, out, 0.0)


def kernel_function(name: str):
    return gaussian_kernel if name == "gaussian" else quartic_kernel


# --- bandwidth -----------------------------------------------------------------------------------

def weighted_mean_centre(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    total = float(w.sum())
    if total <= 0:
        return float(x.mean()), float(y.mean())
    return float((x * w).sum() / total), float((y * w).sum() / total)


def weighted_standard_distance(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    """RMS distance of the points from their weighted mean centre (ESRI "standard distance")."""
    cx, cy = weighted_mean_centre(x, y, w)
    total = float(w.sum()) or float(len(x))
    var = float((w * ((x - cx) ** 2 + (y - cy) ** 2)).sum()) / total
    return math.sqrt(max(var, 0.0))


def weighted_median(values: np.ndarray, w: np.ndarray) -> float:
    """Lower weighted median: the smallest value whose cumulative weight reaches half the total."""
    if values.size == 0:
        return 0.0
    order = np.argsort(values)
    v, cw = values[order], np.cumsum(w[order])
    total = float(cw[-1])
    if total <= 0:
        return float(np.median(values))
    return float(v[int(np.searchsorted(cw, total / 2.0, side="left"))])


def silverman_bandwidth(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, str]:
    """
    ArcGIS's default Kernel Density search radius (see the module docstring for the formula).

    Returns ``(bandwidth_m, rule)`` where ``rule`` is ``"silverman"`` or ``"default"`` when there
    were too few points for the statistics to be defined.
    """
    n_points = int(x.size)
    if n_points < 2:
        return DEFAULT_BANDWIDTH_M, "default"
    total_weight = float(w.sum())
    if total_weight <= 0:
        w = np.ones_like(x)
        total_weight = float(w.sum())
    cx, cy = weighted_mean_centre(x, y, w)
    distances = np.hypot(x - cx, y - cy)
    sd = weighted_standard_distance(x, y, w)
    dm = weighted_median(distances, w)
    spread = min(sd, math.sqrt(1.0 / math.log(2.0)) * dm) if dm > 0 else sd
    if spread <= 0:
        return DEFAULT_BANDWIDTH_M, "default"
    h = 0.9 * spread * total_weight ** -0.2
    return float(min(max(h, MIN_BANDWIDTH_M), MAX_BANDWIDTH_M)), "silverman"


# --- evaluation ----------------------------------------------------------------------------------

def density_at(px: np.ndarray, py: np.ndarray, w: np.ndarray, qx: np.ndarray, qy: np.ndarray,
               h: float, kernel: str = "quartic") -> np.ndarray:
    """
    Density (weight per m²) at arbitrary query points ``(qx, qy)``.

    Evaluated point by point so memory stays ``O(len(q))`` rather than ``O(len(p)·len(q))``.
    """
    k = kernel_function(kernel)
    qx, qy = np.asarray(qx, dtype=float), np.asarray(qy, dtype=float)
    out = np.zeros(qx.shape, dtype=float)
    if px.size == 0:
        return out
    reach2 = (KERNEL_REACH.get(kernel, 1.0) * h) ** 2
    for x0, y0, weight in zip(px, py, w):
        d2 = (qx - x0) ** 2 + (qy - y0) ** 2
        near = d2 <= reach2
        if near.any():
            out[near] += weight * k(d2[near], h)
    return out


def density_grid(px: np.ndarray, py: np.ndarray, w: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray,
                 h: float, kernel: str = "quartic") -> np.ndarray:
    """
    KDE surface on the regular metric grid ``x_axis × y_axis`` (cell *centres*, ascending).

    Returns an array of shape ``(len(y_axis), len(x_axis))`` in weight per m², row 0 = southernmost.
    Each point only touches the sub-window of cells within its kernel's reach, so the cost is
    ``O(points · (reach/cell_size)²)`` instead of ``O(points · cells)``.
    """
    grid = np.zeros((y_axis.size, x_axis.size), dtype=float)
    if px.size == 0 or x_axis.size == 0 or y_axis.size == 0:
        return grid
    k = kernel_function(kernel)
    reach = KERNEL_REACH.get(kernel, 1.0) * h
    reach2 = reach * reach
    for x0, y0, weight in zip(px, py, w):
        i0, i1 = np.searchsorted(x_axis, [x0 - reach, x0 + reach])
        j0, j1 = np.searchsorted(y_axis, [y0 - reach, y0 + reach])
        i0, j0 = max(int(i0) - 1, 0), max(int(j0) - 1, 0)
        i1, j1 = min(int(i1) + 1, x_axis.size), min(int(j1) + 1, y_axis.size)
        if i0 >= i1 or j0 >= j1:
            continue
        dx = x_axis[i0:i1] - x0
        dy = y_axis[j0:j1] - y0
        d2 = dy[:, None] ** 2 + dx[None, :] ** 2
        contribution = np.where(d2 <= reach2, k(d2, h), 0.0)
        grid[j0:j1, i0:i1] += weight * contribution
    return grid


def per_km2(values):
    """Convert a density expressed per m² to per km²."""
    return np.asarray(values, dtype=float) * 1_000_000.0


def scale_to_uint8(values: np.ndarray, max_value: float) -> np.ndarray:
    """
    Quantise a density surface to the wire format: ``0`` = nothing, ``1..255`` = linear to ``max``.

    Any strictly positive density becomes at least 1, so a faint but real hotspot never disappears
    into the "no data" value.
    """
    out = np.zeros(values.shape, dtype=np.uint8)
    if max_value <= 0:
        return out
    scaled = np.rint(np.clip(values / max_value, 0.0, 1.0) * 255.0)
    positive = values > 0
    out[positive] = np.maximum(scaled[positive], 1).astype(np.uint8)
    return out


def integrate(grid: np.ndarray, cell_size_m: float) -> float:
    """Total weight represented by a per-m² surface: ``Σ density · cell area``."""
    return float(grid.sum()) * cell_size_m * cell_size_m


def peak_indices(grid: np.ndarray, count: int, min_separation_cells: float) -> list[tuple[int, int]]:
    """
    Up to ``count`` grid maxima, greedily chosen so that no two are closer than
    ``min_separation_cells`` — a cheap non-maximum suppression so hotspots are distinct places
    rather than neighbouring pixels of the same peak.
    """
    if grid.size == 0 or count <= 0:
        return []
    flat = grid.ravel()
    order = np.argsort(flat)[::-1]
    picks: list[tuple[int, int]] = []
    sep2 = max(min_separation_cells, 1.0) ** 2
    for idx in order:
        if flat[idx] <= 0:
            break
        j, i = divmod(int(idx), grid.shape[1])
        if all((j - pj) ** 2 + (i - pi) ** 2 >= sep2 for pj, pi in picks):
            picks.append((j, i))
            if len(picks) >= count:
                break
    return picks
