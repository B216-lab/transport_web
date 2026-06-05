"""
Пешие изохроны по предобработанному графу (pickle).
"""
from __future__ import annotations

import heapq
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import pyproj
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point, mapping
from shapely.ops import transform as shp_transform
from shapely.strtree import STRtree

from graph_store import load_pedestrian_graph
from utils import haversine_m

logger = logging.getLogger(__name__)

_transformer_to_m = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_transformer_to_wgs = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

DEFAULT_WALK_SPEED_KMH = 4.5
ZONE_NODE_BUFFER_M = 40.0
ZONE_EDGE_BUFFER_M = 32.0

_NODE_TREE_CACHE: Dict[str, Tuple[float, STRtree, List[int]]] = {}

# Цвета зон для фронта (от меньшего интервала к большему): зелёный → оранжевый → красный
_ZONE_COLORS = ("#22c55e", "#f97316", "#ef4444", "#b91c1c", "#7f1d1d", "#14532d")


def _graph_cache_key(graph: Dict[str, Any]) -> str:
    meta = graph.get("meta") or {}
    return str(meta.get("source_geojson", "")) + str(meta.get("edge_count", ""))


def _node_spatial_index(graph: Dict[str, Any]) -> Tuple[STRtree, List[int]]:
    key = _graph_cache_key(graph)
    pkl_mtime = graph.get("meta", {}).get("_pkl_mtime")
    cache_key = f"{key}:{pkl_mtime}"

    cached = _NODE_TREE_CACHE.get(cache_key)
    if cached:
        return cached[1], cached[2]

    node_coords: Dict[int, Tuple[float, float]] = graph["node_coords"]
    node_ids = list(node_coords.keys())
    points_m = []
    for nid in node_ids:
        lon, lat = node_coords[nid]
        x, y = _transformer_to_m.transform(lon, lat)
        points_m.append(Point(x, y))

    tree = STRtree(points_m)
    _NODE_TREE_CACHE[cache_key] = (0.0, tree, node_ids)
    return tree, node_ids


def nearest_graph_node(
    graph: Dict[str, Any],
    lon: float,
    lat: float,
    max_snap_m: float = 80.0,
    adj: Optional[Dict[int, List[Tuple[int, float]]]] = None,
) -> Tuple[Optional[int], float]:
    """Привязка к узлу: среди ближайших выбираем связный (не «тупик» с гигантским ребром)."""
    tree, node_ids = _node_spatial_index(graph)
    x, y = _transformer_to_m.transform(lon, lat)
    pt = Point(x, y)
    cand_idx = tree.query(pt.buffer(max_snap_m))
    if len(cand_idx) == 0:
        idx = tree.nearest(pt)
        cand_idx = [idx] if idx is not None else []

    best_nid: Optional[int] = None
    best_dist = float("inf")
    best_score = -1.0

    for idx in cand_idx:
        nid = node_ids[int(idx)]
        lon_n, lat_n = graph["node_coords"][nid]
        dist_m = haversine_m(lon, lat, lon_n, lat_n)
        if dist_m > max_snap_m:
            continue
        score = 0.0
        if adj:
            edges = adj.get(int(nid), [])
            if edges:
                min_w = min(w for _, w in edges)
                if min_w <= 1800.0:
                    score = float(len(edges)) * 1000.0 - min_w
        else:
            score = -dist_m
        if score > best_score or (score == best_score and dist_m < best_dist):
            best_score = score
            best_nid = nid
            best_dist = dist_m

    if best_nid is None:
        return None, float("inf")
    return best_nid, best_dist


def _direct_walk_times(
    origin_lon: float,
    origin_lat: float,
    node_coords: Dict[int, Tuple[float, float]],
    max_time_s: float,
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
    snap_penalty_s: float = 0.0,
    detour_factor: float = 1.25,
) -> Dict[int, float]:
    """
    Оценка пешего времени по прямой (с коэфф. обхода).
    Нужна, когда граф разорван — иначе изохрона «схлопывается» в точку.
    """
    mps = max(float(walk_speed_kmh) / 3.6, 0.25)
    max_d = max_time_s * mps / detour_factor
    max_deg = max_d / 85000.0
    out: Dict[int, float] = {}
    for nid, (nlon, nlat) in node_coords.items():
        if abs(nlon - origin_lon) > max_deg or abs(nlat - origin_lat) > max_deg * 1.2:
            continue
        d = haversine_m(origin_lon, origin_lat, nlon, nlat)
        if d > max_d:
            continue
        t = snap_penalty_s + (d * detour_factor) / mps
        if t <= max_time_s + 1e-6:
            out[nid] = t
    return out


def merge_walk_times(
    graph_times: Dict[int, float],
    direct_times: Dict[int, float],
) -> Dict[int, float]:
    merged = dict(graph_times)
    for nid, t in direct_times.items():
        if t < merged.get(nid, math.inf):
            merged[nid] = t
    return merged


def compute_origin_walk_times(
    origin_lon: float,
    origin_lat: float,
    graph: Dict[str, Any],
    adj: Dict[int, List[Tuple[int, float]]],
    start_node: int,
    max_time_s: float,
    snap_m: float,
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
) -> Tuple[Dict[int, float], float]:
    """Минимум времени по графу и по прямой от точки клика."""
    snap_pen = snap_time_seconds(snap_m, walk_speed_kmh)
    graph_times = apply_snap_penalty(
        _dijkstra_times(adj, start_node, max_time_s),
        snap_m,
        walk_speed_kmh,
    )
    direct_times = _direct_walk_times(
        origin_lon,
        origin_lat,
        graph["node_coords"],
        max_time_s,
        walk_speed_kmh,
        snap_penalty_s=snap_pen,
    )
    return merge_walk_times(graph_times, direct_times), snap_pen


def _dijkstra_times(
    adj: Dict[int, List[Tuple[int, float]]],
    start: int,
    max_time_s: float,
) -> Dict[int, float]:
    dist: Dict[int, float] = {start: 0.0}
    heap: List[Tuple[float, int]] = [(0.0, start)]

    while heap:
        d, u = heapq.heappop(heap)
        if d > dist.get(u, math.inf):
            continue
        if d > max_time_s:
            continue
        for v, w in adj.get(u, []):
            nd = d + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                heapq.heappush(heap, (nd, v))
    return dist


def snap_time_seconds(snap_m: float, walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH) -> float:
    """Доп. время от клика до привязанного узла графа."""
    if snap_m <= 0:
        return 0.0
    mps = max(float(walk_speed_kmh) / 3.6, 0.25)
    return float(snap_m) / mps


def apply_snap_penalty(
    times: Dict[int, float],
    snap_m: float,
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
) -> Dict[int, float]:
    penalty = snap_time_seconds(snap_m, walk_speed_kmh)
    if penalty <= 0:
        return times
    return {nid: t + penalty for nid, t in times.items()}


def _geom_m_to_geojson(geom_m: Any) -> Optional[Dict[str, Any]]:
    if geom_m is None or geom_m.is_empty:
        return None
    geom_m = geom_m.simplify(2.5, preserve_topology=True)
    geom_wgs = shp_transform(
        lambda x, y, z=None: _transformer_to_wgs.transform(x, y), geom_m
    )
    return mapping(geom_wgs)


def _zone_polygon_from_reachable(
    node_coords: Dict[int, Tuple[float, float]],
    adj: Dict[int, List[Tuple[int, float]]],
    node_ids: List[int],
    edge_buffer_m: float = ZONE_EDGE_BUFFER_M,
    node_buffer_m: float = ZONE_NODE_BUFFER_M,
    max_vertices: int = 6000,
    max_edges: int = 15000,
) -> Optional[Dict[str, Any]]:
    """
    Полигон по достижимым рёбрам графа (форма вдоль улиц), с запасным буфером узлов.
    """
    if not node_ids:
        return None
    if len(node_ids) > max_vertices:
        step = max(1, len(node_ids) // max_vertices)
        node_ids = node_ids[::step]

    reachable = set(node_ids)
    lines_m: List[LineString] = []
    seen: set = set()
    for u, nbrs in adj.items():
        if u not in reachable:
            continue
        for v, _w in nbrs:
            if v not in reachable:
                continue
            key = (min(u, v), max(u, v))
            if key in seen:
                continue
            seen.add(key)
            if u not in node_coords or v not in node_coords:
                continue
            lon1, lat1 = node_coords[u]
            lon2, lat2 = node_coords[v]
            x1, y1 = _transformer_to_m.transform(lon1, lat1)
            x2, y2 = _transformer_to_m.transform(lon2, lat2)
            lines_m.append(LineString([(x1, y1), (x2, y2)]))
            if len(lines_m) >= max_edges:
                break
        if len(lines_m) >= max_edges:
            break

    try:
        if lines_m:
            if len(lines_m) == 1:
                geom_m = lines_m[0].buffer(edge_buffer_m, cap_style=1, join_style=1)
            else:
                geom_m = MultiLineString(lines_m).buffer(
                    edge_buffer_m, cap_style=1, join_style=1
                )
            out = _geom_m_to_geojson(geom_m)
            if out:
                return out
    except Exception as ex:
        logger.warning("edge-buffer zone failed, fallback to nodes: %s", ex)

    return _zone_polygon_from_nodes(
        node_coords, node_ids, buffer_m=node_buffer_m, max_vertices=max_vertices
    )


def _zone_polygon_from_nodes(
    node_coords: Dict[int, Tuple[float, float]],
    node_ids: List[int],
    buffer_m: float = ZONE_NODE_BUFFER_M,
    max_vertices: int = 6000,
) -> Optional[Dict[str, Any]]:
    """
    Полигон зоны: объединённый буфер достижимых узлов (точнее, чем convex hull).
    """
    if not node_ids:
        return None
    if len(node_ids) > max_vertices:
        step = max(1, len(node_ids) // max_vertices)
        node_ids = node_ids[::step]

    pts_m: List[Tuple[float, float]] = []
    for n in node_ids:
        if n not in node_coords:
            continue
        lon, lat = node_coords[n]
        x, y = _transformer_to_m.transform(lon, lat)
        pts_m.append((x, y))

    if not pts_m:
        return None

    try:
        if len(pts_m) == 1:
            geom_m = Point(pts_m[0]).buffer(buffer_m)
        else:
            geom_m = MultiPoint(pts_m).buffer(buffer_m)
        if geom_m.is_empty:
            return None
        return _geom_m_to_geojson(geom_m)
    except Exception as ex:
        logger.warning("buffer zone failed, fallback to hull: %s", ex)
        return _hull_from_nodes(node_coords, node_ids, max_vertices=max_vertices)


def _hull_from_nodes(
    node_coords: Dict[int, Tuple[float, float]],
    node_ids: List[int],
    max_vertices: int = 8000,
) -> Optional[Dict[str, Any]]:
    """Запасной вариант — выпуклая оболочка (завышает площадь)."""
    if not node_ids:
        return None
    if len(node_ids) > max_vertices:
        step = max(1, len(node_ids) // max_vertices)
        node_ids = node_ids[::step]

    coords = [node_coords[n] for n in node_ids if n in node_coords]
    if len(coords) < 3:
        if len(coords) == 1:
            pt = Point(coords[0])
            geom = pt.buffer(0.00025)
            return mapping(geom)
        if len(coords) == 2:
            mp = MultiPoint(coords)
            geom = mp.buffer(0.0002)
            return mapping(geom)
        return None

    mp = MultiPoint(coords)
    hull = mp.convex_hull
    if hull.is_empty:
        return None
    return mapping(hull)


def _resolve_adj(
    graph: Dict[str, Any],
    use_elevation: bool,
) -> Tuple[Dict[int, List[Tuple[int, float]]], Dict[str, Any]]:
    meta = graph.get("meta") or {}
    info: Dict[str, Any] = {"use_elevation": use_elevation, "elevation_applied": False}

    adj_raw = None
    if use_elevation and graph.get("adj_elevation"):
        adj_raw = graph["adj_elevation"]
        info["elevation_applied"] = True
    else:
        adj_raw = graph.get("adj") or {}
        if use_elevation and meta.get("has_elevation"):
            info["elevation_warning"] = "В графе нет adj_elevation — выполните enrich_pedestrian_graph"
        elif use_elevation:
            info["elevation_warning"] = "Нет данных рельефа — положите contours.geojson и enrich_pedestrian_graph"

    adj: Dict[int, List[Tuple[int, float]]] = {}
    for k, edges in adj_raw.items():
        adj[int(k)] = [(int(v), float(w)) for v, w in edges]
    return adj, info


def compute_pedestrian_isochrones(
    origin_lon: float,
    origin_lat: float,
    intervals_min: List[float],
    max_snap_m: float = 80.0,
    graph: Optional[Dict[str, Any]] = None,
    use_elevation: bool = True,
    include_building_stats: bool = True,
) -> Dict[str, Any]:
    if not intervals_min:
        raise ValueError("intervals_min must not be empty")

    intervals_sorted = sorted(set(float(t) for t in intervals_min if float(t) > 0))
    if not intervals_sorted:
        raise ValueError("intervals_min must contain positive values")

    g = graph or load_pedestrian_graph()
    adj, elevation_info = _resolve_adj(g, use_elevation=use_elevation)
    start_node, snap_m = nearest_graph_node(
        g, origin_lon, origin_lat, max_snap_m=max_snap_m, adj=adj
    )
    if start_node is None:
        raise ValueError(
            f"Не удалось привязать точку к графу в пределах {max_snap_m} м (расстояние до ближайшего узла: {snap_m:.0f} м)"
        )

    max_time_s = intervals_sorted[-1] * 60.0
    walk_speed_kmh = float((g.get("meta") or {}).get("speed_kmh", DEFAULT_WALK_SPEED_KMH))
    times_s, snap_pen = compute_origin_walk_times(
        origin_lon,
        origin_lat,
        g,
        adj,
        start_node,
        max_time_s,
        snap_m,
        walk_speed_kmh,
    )
    node_coords: Dict[int, Tuple[float, float]] = g["node_coords"]
    zone_method = "network_buffer"

    features: List[Dict[str, Any]] = []
    zone_features: List[Dict[str, Any]] = []
    zones_meta: List[Dict[str, Any]] = []

    for i, t_min in enumerate(intervals_sorted):
        limit_s = t_min * 60.0
        reachable = [nid for nid, t in times_s.items() if t <= limit_s + 1e-6]
        geom = _zone_polygon_from_reachable(node_coords, adj, reachable)
        if not geom:
            continue
        color = _ZONE_COLORS[i % len(_ZONE_COLORS)]
        props = {
            "interval_min": t_min,
            "interval_s": limit_s,
            "reachable_nodes": len(reachable),
            "zone_geometry": zone_method,
            "fill_color": color,
            "stroke_color": color,
        }
        feat = {"type": "Feature", "geometry": geom, "properties": props}
        features.append(feat)
        zone_features.append(feat)
        zones_meta.append(
            {
                "interval_min": t_min,
                "reachable_nodes": len(reachable),
                "max_travel_s": round(min((times_s[n] for n in reachable), default=0), 1),
            }
        )

    building_info: Dict[str, Any] = {"included": False}
    if include_building_stats and zone_features:
        try:
            from .stats import enrich_zones_with_building_stats

            zones_meta, zone_features, bmeta = enrich_zones_with_building_stats(
                zones_meta, zone_features
            )
            building_info = {"included": True, **bmeta}
            features = zone_features
        except FileNotFoundError as e:
            building_info = {"included": False, "warning": str(e)}
        except Exception as e:
            logger.warning("building stats failed: %s", e)
            building_info = {"included": False, "warning": str(e)}

    snap_lon, snap_lat = node_coords[start_node]
    origin_feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [origin_lon, origin_lat]},
        "properties": {"role": "origin"},
    }
    snap_feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [snap_lon, snap_lat]},
        "properties": {"role": "snapped_node", "snap_distance_m": round(snap_m, 1)},
    }

    return {
        "origin": [origin_lon, origin_lat],
        "snapped_node": [snap_lon, snap_lat],
        "snap_distance_m": round(snap_m, 1),
        "snap_time_s": round(snap_pen, 1),
        "zone_geometry_method": zone_method,
        "start_node_id": start_node,
        "intervals_min": intervals_sorted,
        "zones": zones_meta,
        "elevation": elevation_info,
        "buildings": building_info,
        "geojson": {
            "type": "FeatureCollection",
            "features": [origin_feature, snap_feature] + features,
        },
        "graph_meta": g.get("meta"),
    }
