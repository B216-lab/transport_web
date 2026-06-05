"""
Высоты по изолиниям (GeoJSON LineString) для расчёта пешего времени с уклоном.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Tuple

import pyproj
from shapely.geometry import LineString, Point, shape
from shapely.ops import nearest_points
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)

_transformer_to_m = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

_ELEVATION_PROP_KEYS = (
    "elevation",
    "elev",
    "ELEV",
    "height",
    "HEIGHT",
    "contour",
    "altitude",
    "alt",
    "level",
    "LEVEL",
    "z",
    "отметка",
    "высота",
    "elev_m",
)

_MODEL_CACHE: Dict[str, "ContourElevationModel"] = {}


def _parse_elevation(props: Dict[str, Any]) -> Optional[float]:
    for key in _ELEVATION_PROP_KEYS:
        if key in props and props[key] is not None and str(props[key]).strip() != "":
            try:
                return float(str(props[key]).replace(",", "."))
            except (TypeError, ValueError):
                continue
    for k, v in props.items():
        kl = str(k).lower()
        if kl in ("elevation", "elev", "height", "contour", "altitude", "level", "z"):
            try:
                return float(str(v).replace(",", "."))
            except (TypeError, ValueError):
                pass
    return None


def walk_time_seconds(
    length_m: float,
    elev_delta_m: float,
    speed_kmh: float = 4.5,
    slope_k: float = 6.0,
) -> float:
    """
    t = L / v0 * (1 + k * |Δh| / L)  — эквивалент v = v0 / (1 + k*|Δh|/L).
    """
    if length_m < 0.5:
        return 0.0
    v0 = max(speed_kmh / 3.6, 0.25)
    return length_m * (1.0 + slope_k * abs(elev_delta_m) / length_m) / v0


class ContourElevationModel:
    """Интерполяция высоты по ближайшим сегментам изолиний."""

    def __init__(self, geojson_path: str, max_search_m: float = 250.0) -> None:
        self.path = os.path.abspath(geojson_path)
        self.max_search_m = max_search_m
        self._segments_m: List[LineString] = []
        self._elevations: List[float] = []
        self._tree: Optional[STRtree] = None
        self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        features = data.get("features") or []
        skipped = 0
        for feat in features:
            elev = _parse_elevation(feat.get("properties") or {})
            if elev is None:
                skipped += 1
                continue
            geom = feat.get("geometry")
            if not geom:
                skipped += 1
                continue
            try:
                g = shape(geom)
            except Exception:
                skipped += 1
                continue
            lines = []
            if g.geom_type == "LineString":
                lines = [g]
            elif g.geom_type == "MultiLineString":
                lines = list(g.geoms)
            else:
                skipped += 1
                continue
            for line in lines:
                if line.is_empty or len(line.coords) < 2:
                    continue
                coords_m = [_transformer_to_m.transform(float(c[0]), float(c[1])) for c in line.coords]
                self._segments_m.append(LineString(coords_m))
                self._elevations.append(elev)

        if not self._segments_m:
            raise ValueError(
                f"В {self.path} не найдено изолиний с высотой. "
                f"Нужен атрибут elevation/elev/height и LineString."
            )
        self._tree = STRtree(self._segments_m)
        logger.info(
            "Модель высот: %s сегментов, пропущено объектов: %s",
            len(self._segments_m),
            skipped,
        )

    def elevation_at(self, lon: float, lat: float) -> Optional[float]:
        if not self._tree:
            return None
        x, y = _transformer_to_m.transform(lon, lat)
        pt = Point(x, y)
        idx = self._tree.nearest(pt)
        if idx is None:
            return None
        seg = self._segments_m[int(idx)]
        snap, _ = nearest_points(seg, pt)
        if pt.distance(snap) > self.max_search_m:
            return None
        return float(self._elevations[int(idx)])


def get_contour_model(path: Optional[str] = None) -> ContourElevationModel:
    from data_paths import default_contours_path

    p = path or default_contours_path()
    if not p:
        raise FileNotFoundError(
            "Файл изолиний не найден. Положите contours.geojson в корень проекта или data/."
        )
    mtime = os.path.getmtime(p)
    key = f"{p}:{mtime}"
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = ContourElevationModel(p)
    return _MODEL_CACHE[key]


def apply_elevation_to_graph(
    graph: Dict[str, Any],
    contours_path: Optional[str] = None,
    speed_kmh: Optional[float] = None,
    slope_k: float = 6.0,
) -> Dict[str, Any]:
    """Добавляет node_elevation_m и adj_elevation в граф (in-place + return)."""
    model = get_contour_model(contours_path)
    meta = graph.setdefault("meta", {})
    speed = float(speed_kmh if speed_kmh is not None else meta.get("speed_kmh", 4.5))

    node_coords: Dict[int, Tuple[float, float]] = graph["node_coords"]
    node_elev: Dict[int, float] = {}
    missing = 0
    for nid, (lon, lat) in node_coords.items():
        e = model.elevation_at(lon, lat)
        if e is None:
            missing += 1
            e = 0.0
        node_elev[int(nid)] = float(e)

    adj_elev: Dict[int, List[Tuple[int, float]]] = {}
    for edge in graph.get("edges") or []:
        u, v = int(edge["from"]), int(edge["to"])
        length_m = float(edge["length_m"])
        dh = abs(node_elev.get(v, 0.0) - node_elev.get(u, 0.0))
        t = walk_time_seconds(length_m, dh, speed_kmh=speed, slope_k=slope_k)
        adj_elev.setdefault(u, []).append((v, t))

    graph["node_elevation_m"] = node_elev
    graph["adj_elevation"] = {k: list(v) for k, v in adj_elev.items()}
    meta["has_elevation"] = True
    meta["elevation_source"] = model.path
    meta["slope_k"] = slope_k
    meta["elevation_missing_nodes"] = missing
    return graph
