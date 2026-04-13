# backend/services.py
from typing import List, Dict, Tuple, Optional, Any
from math import sqrt
from datetime import datetime, timedelta
from collections import defaultdict
import logging
import os
from utils import (
    haversine_m,
    parse_time,
    bearing_deg,
    circular_mean_bearing_deg,
    direction_bin_from_ref,
)

logger = logging.getLogger(__name__)

def default_roads_graph_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(here, ".."))
    return os.path.join(project_root, "highway_graph.geojson")


def snap_features_to_road_graph(
    features: List[Dict[str, Any]],
    tolerance_m: float = 50.0,
) -> List[Dict[str, Any]]:
    if not features:
        return features

    roads_path = default_roads_graph_path()
    if not os.path.isfile(roads_path):
        logger.warning("Файл дорожного графа не найден: %s", roads_path)
        return features

    from road_graph_snap import snap_features_to_roads_geojson
    return snap_features_to_roads_geojson(features, roads_path, tolerance_m=tolerance_m)


def track_key_from_props(props: Dict[str, Any]) -> str:
    """Ключ трека для группировки точек одного ТС / маршрута."""
    vid = (
        props.get("vehicle_id")
        or props.get("vehicle")
        or props.get("board")
        or props.get("gsm")
        or props.get("gos_num")
        or props.get("plate")
        or props.get("bnum")
        or ""
    )
    r = props.get("route_num") or ""
    return f"{vid}|{r}"


def compute_flow_directions(
    features: List[Dict[str, Any]],
    min_segment_m: float = 8.0,
) -> Tuple[List[Optional[int]], List[Optional[float]], float]:
    """
    По цепочкам точек (по треку) считает азимут движения и делит на два направления
    относительно среднего азимута сегментов в выборке.

    Возвращает:
      dirs[i] — 0 или 1 относительно опорного направления, либо None если не удалось определить;
      point_bearings[i] — азимут в точке (для отображения стрелки);
      ref_bearing — опорный азимут (круговое среднее сегментов).
    """
    n = len(features)
    point_bearings: List[Optional[float]] = [None] * n
    segment_bearings: List[float] = []

    groups: Dict[str, List[int]] = defaultdict(list)
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        groups[track_key_from_props(props)].append(i)

    for _key, indices in groups.items():
        indices_sorted = sorted(
            indices,
            key=lambda i: parse_time(features[i]["properties"].get("time")) or datetime.min,
        )
        for a in range(len(indices_sorted) - 1):
            i, j = indices_sorted[a], indices_sorted[a + 1]
            coords_i = features[i].get("geometry", {}).get("coordinates") or []
            coords_j = features[j].get("geometry", {}).get("coordinates") or []
            if len(coords_i) < 2 or len(coords_j) < 2:
                continue
            lon1, lat1 = float(coords_i[0]), float(coords_i[1])
            lon2, lat2 = float(coords_j[0]), float(coords_j[1])
            d = haversine_m(lon1, lat1, lon2, lat2)
            if d < min_segment_m:
                continue
            b = bearing_deg(lon1, lat1, lon2, lat2)
            segment_bearings.append(b)
            point_bearings[j] = b
            if point_bearings[i] is None:
                point_bearings[i] = b

    if not segment_bearings:
        return [None] * n, point_bearings, 0.0

    ref = circular_mean_bearing_deg(segment_bearings)
    dirs: List[Optional[int]] = []
    for i in range(n):
        pb = point_bearings[i]
        if pb is None:
            dirs.append(None)
        else:
            dirs.append(direction_bin_from_ref(pb, ref))

    return dirs, point_bearings, ref


def calculate_statistics(speeds: List[float]) -> Dict[str, float]:
    """Calculate comprehensive speed statistics."""
    if not speeds:
        return {}
    
    sorted_speeds = sorted(speeds)
    n = len(sorted_speeds)
    mean_val = sum(speeds) / n
    
    return {
        "min": min(speeds),
        "max": max(speeds),
        "mean": mean_val,
        "median": sorted_speeds[n // 2] if n % 2 == 1 else (sorted_speeds[n // 2 - 1] + sorted_speeds[n // 2]) / 2,
        "std": sqrt(sum((x - mean_val) ** 2 for x in speeds) / n) if n > 1 else 0,
        "q25": sorted_speeds[n // 4] if n >= 4 else sorted_speeds[0],
        "q75": sorted_speeds[3 * n // 4] if n >= 4 else sorted_speeds[-1],
    }

def region_growing_clusters(
    points: List[List[float]], 
    eps_m: float, 
    min_pts: int
) -> List[List[int]]:
    """Optimized region-growing clustering algorithm."""
    n = len(points)
    if n == 0:
        return []
    
    if n < 100:
        return _simple_clustering(points, eps_m, min_pts)
    
    return _spatial_grid_clustering(points, eps_m, min_pts)

def _simple_clustering(points, eps_m, min_pts):
    n = len(points)
    visited = [False] * n
    clusters = []

    for i in range(n):
        if visited[i]: continue
        
        stack = [i]
        cluster = []
        while stack:
            cur = stack.pop()
            if visited[cur]: continue
            
            visited[cur] = True
            cluster.append(cur)
            lon1, lat1 = points[cur]
            
            for j in range(n):
                if visited[j]: continue
                lon2, lat2 = points[j]
                if haversine_m(lon1, lat1, lon2, lat2) <= eps_m:
                    stack.append(j)
        
        if len(cluster) >= min_pts:
            clusters.append(cluster)
    
    clusters.sort(key=len, reverse=True)
    return clusters

def _spatial_grid_clustering(points, eps_m, min_pts):
    n = len(points)
    cell_size = max(eps_m / 111000 * 2, 0.001)
    grid = defaultdict(list)
    
    for i, (lon, lat) in enumerate(points):
        grid[(int(lon / cell_size), int(lat / cell_size))].append(i)
    
    visited = [False] * n
    clusters = []
    
    for i in range(n):
        if visited[i]: continue
        
        stack = [i]
        cluster = []
        while stack:
            cur = stack.pop()
            if visited[cur]: continue
            
            visited[cur] = True
            cluster.append(cur)
            lon1, lat1 = points[cur]
            cx, cy = int(lon1 / cell_size), int(lat1 / cell_size)
            
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    for j in grid.get((cx + dx, cy + dy), []):
                        if not visited[j]:
                            lon2, lat2 = points[j]
                            if haversine_m(lon1, lat1, lon2, lat2) <= eps_m:
                                stack.append(j)
        
        if len(cluster) >= min_pts:
            clusters.append(cluster)
    
    clusters.sort(key=len, reverse=True)
    return clusters

def aggregate_plot_data(
    times: List[str],
    speeds: List[float],
    interval_minutes: int = 15
) -> Tuple[List[str], List[float]]:
    """Aggregate plot data into time intervals."""
    if not times or not speeds or len(times) != len(speeds):
        return times, speeds
    
    parsed_data = []
    for i, t in enumerate(times):
        dt = parse_time(t) if isinstance(t, str) else t
        if dt: parsed_data.append((dt, speeds[i]))
    
    if not parsed_data: return times, speeds
    
    min_time = min(d[0] for d in parsed_data)
    interval_delta = timedelta(minutes=interval_minutes).total_seconds()
    buckets = defaultdict(list)
    
    for dt, speed in parsed_data:
        bucket_idx = int((dt - min_time).total_seconds() / interval_delta)
        buckets[bucket_idx].append(speed)
    
    agg_times, agg_speeds = [], []
    for b_idx in sorted(buckets.keys()):
        interval_start = min_time + timedelta(seconds=b_idx * interval_delta)
        agg_times.append(interval_start.isoformat())
        agg_speeds.append(sum(buckets[b_idx]) / len(buckets[b_idx]))
    
    return agg_times, agg_speeds
