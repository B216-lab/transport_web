"""
Статистика телеметрии по маршрутам и сегментам (между соседними ОП) для расчёта ОТ-доступности.
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from data_paths import resolve_data_file
from graph_store import project_root
from services import calculate_statistics
from utils import haversine_m, parse_time

DEFAULT_HEADWAY_MIN = 12.0
SEGMENT_ASSIGN_MAX_M = 380.0


def segment_speed_key(route_num: str, from_stop: int, to_stop: int) -> str:
    return f"{route_num}|{from_stop}|{to_stop}"


def parse_segment_speed_key(key: str) -> Optional[Tuple[str, int, int]]:
    parts = str(key).split("|")
    if len(parts) != 3:
        return None
    try:
        return parts[0], int(parts[1]), int(parts[2])
    except ValueError:
        return None


def load_route_stops() -> Dict[str, List[int]]:
    path = os.path.join(project_root(), "data", "route_stops.json")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {str(k): [int(x) for x in v] for k, v in raw.items()}


def load_stop_coords() -> Dict[int, Tuple[float, float]]:
    path = resolve_data_file(["proj_stop.geojson", os.path.join("data", "proj_stop.geojson")])
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out: Dict[int, Tuple[float, float]] = {}
    for feat in data.get("features") or []:
        sid = (feat.get("properties") or {}).get("id")
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if sid is None or len(coords) < 2:
            continue
        out[int(sid)] = (float(coords[0]), float(coords[1]))
    return out


def _dist_point_to_segment_m(
    lon: float,
    lat: float,
    lon_a: float,
    lat_a: float,
    lon_b: float,
    lat_b: float,
) -> float:
    """Расстояние от точки до отрезка AB (метры, локальная проекция)."""
    ax, ay = lon_a, lat_a
    bx, by = lon_b, lat_b
    px, py = lon, lat
    abx, aby = bx - ax, by - ay
    apx, apy = px - ax, py - ay
    ab2 = abx * abx + aby * aby
    if ab2 < 1e-18:
        return haversine_m(lon, lat, lon_a, lat_a)
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    qx, qy = ax + t * abx, ay + t * aby
    mid_lat = (lat_a + lat_b) / 2.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(mid_lat))
    m_per_deg_lat = 110540.0
    dx = (px - qx) * m_per_deg_lon
    dy = (py - qy) * m_per_deg_lat
    return math.hypot(dx, dy)


def _assign_segment(
    lon: float,
    lat: float,
    route_num: str,
    route_stops: Dict[str, List[int]],
    stop_coords: Dict[int, Tuple[float, float]],
) -> Optional[Tuple[int, int]]:
    seq = route_stops.get(str(route_num))
    if not seq or len(seq) < 2:
        return None
    best: Optional[Tuple[int, int]] = None
    best_d = SEGMENT_ASSIGN_MAX_M
    for i in range(len(seq) - 1):
        a, b = int(seq[i]), int(seq[i + 1])
        ca, cb = stop_coords.get(a), stop_coords.get(b)
        if not ca or not cb:
            continue
        d = _dist_point_to_segment_m(lon, lat, ca[0], ca[1], cb[0], cb[1])
        if d < best_d:
            best_d, best = d, (a, b)
    return best


def compute_segment_telemetry_stats(
    filtered_points: List[Dict[str, Any]],
    route_stops: Optional[Dict[str, List[int]]] = None,
    stop_coords: Optional[Dict[int, Tuple[float, float]]] = None,
    min_samples: int = 3,
) -> Dict[str, Dict[str, Any]]:
    """
    Скорости по сегментам между соседними ОП на маршруте (как на схеме задания).
    Ключ: route|from_stop|to_stop.
    """
    route_stops = route_stops or load_route_stops()
    stop_coords = stop_coords or load_stop_coords()
    if not route_stops or not stop_coords:
        return {}

    speeds: Dict[str, List[float]] = defaultdict(list)
    meta: Dict[str, Tuple[str, int, int]] = {}

    for feat in filtered_points:
        props = feat.get("properties") or {}
        route = props.get("route_num")
        if route is None or str(route).strip() == "":
            continue
        r = str(route).strip()
        try:
            speed = float(props.get("speed", 0))
        except (TypeError, ValueError):
            continue
        if speed <= 0:
            continue
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        seg = _assign_segment(lon, lat, r, route_stops, stop_coords)
        if not seg:
            continue
        a, b = seg
        key = segment_speed_key(r, a, b)
        speeds[key].append(speed)
        meta[key] = (r, a, b)

    out: Dict[str, Dict[str, Any]] = {}
    for key, vals in speeds.items():
        if len(vals) < min_samples:
            continue
        stats = calculate_statistics(vals)
        r, a, b = meta[key]
        out[key] = {
            "route_num": r,
            "from_stop": a,
            "to_stop": b,
            "avg_speed": round(float(stats.get("mean", 0)), 2),
            "median_speed": round(float(stats.get("median", 0)), 2),
            "count": len(vals),
        }
    return out


def _estimate_headway_min(times_by_vehicle: Dict[str, List[datetime]]) -> float:
    """Медиана интервалов между началами рейсов на маршруте."""
    trip_starts: List[datetime] = []
    for vtimes in times_by_vehicle.values():
        vtimes = sorted(vtimes)
        if not vtimes:
            continue
        trip_starts.append(vtimes[0])
        prev = vtimes[0]
        for t in vtimes[1:]:
            if (t - prev).total_seconds() / 60.0 > 45.0:
                trip_starts.append(t)
            prev = t

    if len(trip_starts) < 2:
        return DEFAULT_HEADWAY_MIN

    trip_starts.sort()
    gaps: List[float] = []
    for i in range(1, len(trip_starts)):
        g = (trip_starts[i] - trip_starts[i - 1]).total_seconds() / 60.0
        if 4.0 <= g <= 40.0:
            gaps.append(g)

    if not gaps:
        return DEFAULT_HEADWAY_MIN
    gaps.sort()
    mid = len(gaps) // 2
    return float(gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0)


def _route_avg_from_segments(
    route_num: str,
    segment_stats: Dict[str, Dict[str, Any]],
) -> Optional[float]:
    total_n = 0
    weighted = 0.0
    for row in segment_stats.values():
        if str(row.get("route_num")) != str(route_num):
            continue
        n = int(row.get("count") or 0)
        v = row.get("avg_speed")
        if n <= 0 or v is None or float(v) <= 0:
            continue
        weighted += float(v) * n
        total_n += n
    if total_n <= 0:
        return None
    return round(weighted / total_n, 2)


def compute_route_telemetry_stats(
    filtered_points: List[Dict[str, Any]],
    min_samples: int = 5,
    segment_stats: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    По точкам анализа: avg/median скорость и headway по маршруту.
    Если есть segment_stats — дополняет avg_speed_from_segments (среднее по сегментам).
    """
    speeds_by_route: Dict[str, List[float]] = defaultdict(list)
    times_by_route_vehicle: Dict[str, Dict[str, List[datetime]]] = defaultdict(lambda: defaultdict(list))

    for feat in filtered_points:
        props = feat.get("properties") or {}
        route = props.get("route_num")
        if route is None or str(route).strip() == "":
            continue
        r = str(route).strip()
        try:
            speed = float(props.get("speed", 0))
        except (TypeError, ValueError):
            continue
        if speed <= 0:
            continue
        speeds_by_route[r].append(speed)
        dt = parse_time(str(props.get("time", "")))
        if dt:
            vid = str(props.get("vehicle_id") or props.get("gos_num") or "unknown")
            times_by_route_vehicle[r][vid].append(dt)

    if segment_stats is None:
        segment_stats = compute_segment_telemetry_stats(filtered_points)

    out: Dict[str, Dict[str, Any]] = {}
    for r, speeds in speeds_by_route.items():
        if len(speeds) < min_samples:
            continue
        stats = calculate_statistics(speeds)
        row: Dict[str, Any] = {
            "avg_speed": round(float(stats.get("mean", 0)), 2),
            "median_speed": round(float(stats.get("median", 0)), 2),
            "count": len(speeds),
            "headway_min": round(_estimate_headway_min(times_by_route_vehicle.get(r, {})), 1),
        }
        seg_avg = _route_avg_from_segments(r, segment_stats)
        if seg_avg is not None:
            row["avg_speed_from_segments"] = seg_avg
        out[r] = row
    return out


def build_analysis_pt_maps(
    route_stats: Dict[str, Dict[str, Any]],
    segment_stats: Dict[str, Dict[str, Any]],
    speed_metric: str = "avg",
) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float]]:
    """
    Карты для API изохрон: route_speeds, route_headways, segment_speeds.
    speed_metric: avg | median | segments (приоритет avg_speed_from_segments).
    """
    route_speeds: Dict[str, float] = {}
    route_headways: Dict[str, float] = {}
    segment_speeds: Dict[str, float] = {}

    use_segments_route = speed_metric == "segments"
    speed_key = "median_speed" if speed_metric == "median" else "avg_speed"

    for route, row in route_stats.items():
        if use_segments_route:
            sp = row.get("avg_speed_from_segments") or row.get(speed_key)
        else:
            sp = row.get(speed_key)
        if sp is not None and float(sp) > 0:
            route_speeds[str(route)] = float(sp)
        hw = row.get("headway_min")
        if hw is not None and float(hw) > 0:
            route_headways[str(route)] = float(hw)

    seg_key = "median_speed" if speed_metric == "median" else "avg_speed"
    for key, row in segment_stats.items():
        sp = row.get(seg_key)
        if sp is not None and float(sp) > 0:
            segment_speeds[key] = float(sp)

    return route_speeds, route_headways, segment_speeds
