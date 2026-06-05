"""
Статистика по зданиям внутри изохрон.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import Point, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

logger = logging.getLogger(__name__)

_POPULATION_PROP_KEYS = (
    "population",
    "pop",
    "POP",
    "Population",
    "people",
    "residents",
    "население",
    "насел",
    "popul",
    "living",
)

_INDEX_CACHE: Dict[str, "BuildingIndex"] = {}


def _parse_levels(props: Dict[str, Any]) -> Optional[float]:
    raw = props.get("building:levels") or props.get("levels") or props.get("building_levels")
    if raw is None:
        return None
    try:
        # OSM иногда: "2;3" для разных частей
        part = str(raw).split(";")[0].strip()
        return max(0.0, float(part.replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _parse_population(props: Dict[str, Any]) -> Tuple[float, bool]:
    """
    Возвращает (население, estimated).
    estimated=True — оценка по building:levels (OSM без population).
    """
    for key in _POPULATION_PROP_KEYS:
        if key in props and props[key] is not None:
            try:
                return max(0.0, float(str(props[key]).replace(",", "."))), False
            except (TypeError, ValueError):
                continue
    for k, v in props.items():
        if str(k).lower() in ("population", "pop", "people", "население"):
            try:
                return max(0.0, float(str(v).replace(",", "."))), False
            except (TypeError, ValueError):
                pass
    levels = _parse_levels(props)
    if levels is not None and levels > 0:
        # Грубая оценка для OSM: ~25 чел. на этаж (можно уточнить в дипломе)
        return levels * 25.0, True
    return 0.0, False


class BuildingIndex:
    def __init__(self, geojson_path: str) -> None:
        self.path = os.path.abspath(geojson_path)
        self.points: List[Point] = []
        self.populations: List[float] = []
        self.population_estimated: List[bool] = []
        self._tree: Optional[STRtree] = None
        self._load()

    def _load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for feat in data.get("features") or []:
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                g = shape(geom)
            except Exception:
                continue
            if g.geom_type == "Point":
                pt = g
            elif g.geom_type == "MultiPoint" and len(g.geoms):
                pt = g.geoms[0]
            elif g.geom_type == "Polygon":
                pt = g.centroid
            else:
                continue
            pop, est = _parse_population(feat.get("properties") or {})
            self.points.append(pt)
            self.populations.append(pop)
            self.population_estimated.append(est)
        if not self.points:
            raise ValueError(f"В {self.path} нет точек зданий.")
        self._tree = STRtree(self.points)
        est_count = sum(1 for e in self.population_estimated if e)
        logger.info(
            "Индекс зданий: %s точек (%s с оценкой по этажам)",
            len(self.points),
            est_count,
        )

    def stats_for_geometry(self, geom_dict: Dict[str, Any]) -> Dict[str, Any]:
        if not self._tree:
            return {"buildings_count": 0, "population": 0, "population_estimated": False}
        poly = shape(geom_dict)
        if poly.is_empty:
            return {"buildings_count": 0, "population": 0, "population_estimated": False}
        prepared = prep(poly)
        candidates = self._tree.query(poly)
        count = 0
        pop_sum = 0.0
        any_estimated = False
        for idx in candidates:
            i = int(idx)
            pt = self.points[i]
            if not prepared.contains(pt):
                continue
            count += 1
            pop_sum += self.populations[i]
            if self.population_estimated[i]:
                any_estimated = True
        return {
            "buildings_count": count,
            "population": round(pop_sum, 1),
            "population_estimated": any_estimated and pop_sum > 0,
        }


def get_building_index(path: Optional[str] = None) -> BuildingIndex:
    from data_paths import default_buildings_path

    p = path or default_buildings_path()
    if not p:
        raise FileNotFoundError(
            "Файл зданий не найден. Положите buildings.geojson в корень проекта или data/."
        )
    mtime = os.path.getmtime(p)
    key = f"{p}:{mtime}"
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = BuildingIndex(p)
    return _INDEX_CACHE[key]


def enrich_zones_with_building_stats(
    zones: List[Dict[str, Any]],
    zone_features: List[Dict[str, Any]],
    buildings_path: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    index = get_building_index(buildings_path)
    prev_geom = None
    prev_stats = {"buildings_count": 0, "population": 0.0}
    meta = {
        "population_from_levels": any(index.population_estimated),
        "people_per_level_assumption": 25,
    }

    for i, z in enumerate(zones):
        feat = zone_features[i] if i < len(zone_features) else None
        geom = feat.get("geometry") if feat else None
        if not geom:
            continue
        cumulative = index.stats_for_geometry(geom)
        ring = {
            "buildings_count": max(0, cumulative["buildings_count"] - prev_stats["buildings_count"]),
            "population": round(max(0.0, cumulative["population"] - prev_stats["population"]), 1),
        }
        z["buildings_count"] = cumulative["buildings_count"]
        z["population"] = cumulative["population"]
        z["population_estimated"] = cumulative.get("population_estimated", False)
        z["ring_buildings_count"] = ring["buildings_count"]
        z["ring_population"] = ring["population"]
        if feat and feat.get("properties") is not None:
            feat["properties"]["buildings_count"] = cumulative["buildings_count"]
            feat["properties"]["population"] = cumulative["population"]
        prev_stats = cumulative
        prev_geom = geom

    return zones, zone_features, meta
