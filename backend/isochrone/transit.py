"""
Изохроны с учётом ОТ (полная версия):
  пешком до ОП + ожидание + рейс (скорость/интервал по маршруту из анализа)
  + пересадки + пешком от ОП; геометрия — буфер по графу + коридоры между достижимыми ОП.
"""
from __future__ import annotations

import heapq
from typing import Any, Dict, List, Optional, Tuple

from shapely.geometry import LineString, MultiLineString, mapping, shape
from shapely.ops import unary_union

from graph_store import load_pedestrian_graph
from isochrone.pedestrian import (
    DEFAULT_WALK_SPEED_KMH,
    _dijkstra_times,
    _geom_m_to_geojson,
    _resolve_adj,
    _transformer_to_m,
    _zone_polygon_from_reachable,
    _ZONE_COLORS,
    compute_origin_walk_times,
    nearest_graph_node,
)

PT_CORRIDOR_BUFFER_M = 24.0
from isochrone.pt_analysis import segment_speed_key
from pt_network_store import load_pt_network
from utils import haversine_m

INF = 1.0e15
DEFAULT_HEADWAY_MIN = 12.0

def _route_speed_kmh(
    route_num: str,
    route_speeds: Optional[Dict[str, float]],
    fallback_kmh: Optional[float],
) -> Optional[float]:
    if route_speeds:
        v = route_speeds.get(str(route_num))
        if v is not None and float(v) > 0:
            return float(v)
    return fallback_kmh


def _route_headway_min(
    route_num: str,
    edge_headway: float,
    route_headways: Optional[Dict[str, float]],
) -> float:
    if route_headways:
        h = route_headways.get(str(route_num))
        if h is not None and float(h) > 0:
            return float(h)
    return float(edge_headway or DEFAULT_HEADWAY_MIN)


def _segment_speed_kmh(
    edge: Dict[str, Any],
    segment_speeds: Optional[Dict[str, float]],
) -> Optional[float]:
    if not segment_speeds:
        return None
    key = segment_speed_key(
        str(edge["route_num"]),
        int(edge["from_stop"]),
        int(edge["to_stop"]),
    )
    v = segment_speeds.get(key)
    if v is not None and float(v) > 0:
        return float(v)
    return None


def _ride_travel_time_s(
    edge: Dict[str, Any],
    route_speeds: Optional[Dict[str, float]],
    segment_speeds: Optional[Dict[str, float]],
    fallback_kmh: Optional[float],
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
) -> float:
    """Время рейса между соседними ОП: скорость сегмента, иначе маршрута, иначе пешая сеть."""
    t_walk = float(edge["travel_time_s"])
    route = str(edge["route_num"])
    pt_speed = _segment_speed_kmh(edge, segment_speeds)
    if pt_speed is None:
        pt_speed = _route_speed_kmh(route, route_speeds, fallback_kmh)
    if pt_speed is None:
        return t_walk
    walk_speed_kmh = max(float(walk_speed_kmh), 0.5)
    pt_speed = max(float(pt_speed), 1.0)
    length_m = edge.get("length_m")
    if length_m is not None and float(length_m) > 0:
        return float(length_m) / (pt_speed / 3.6)
    return t_walk * (walk_speed_kmh / pt_speed)


def _boarding_wait_s(
    route_num: str,
    last_route: Optional[str],
    edge: Dict[str, Any],
    route_headways: Optional[Dict[str, float]],
) -> float:
    """
    Ожидание на ОП: H/2 при первой посадке, пересадке на другой маршрут
    или после пешего перехода между ОП. Без ожидания — только продолжение
    рейса на том же маршруте (уже в салоне).
    """
    if last_route is not None and last_route == route_num:
        return 0.0
    headway = _route_headway_min(route_num, float(edge.get("headway_min", DEFAULT_HEADWAY_MIN)), route_headways)
    return headway * 60.0 / 2.0


def _stop_dijkstra(
    net: Dict[str, Any],
    origin_stop_times: Dict[int, float],
    max_time_s: float,
    max_transfers: int = 1,
    route_speeds: Optional[Dict[str, float]] = None,
    segment_speeds: Optional[Dict[str, float]] = None,
    fallback_speed_kmh: Optional[float] = None,
    route_headways: Optional[Dict[str, float]] = None,
    walk_speed_kmh: float = DEFAULT_WALK_SPEED_KMH,
) -> Dict[int, float]:
    ride_by_from: Dict[int, List[Dict[str, Any]]] = {}
    for e in net.get("ride_edges") or []:
        ride_by_from.setdefault(int(e["from_stop"]), []).append(e)

    transfer_by_from: Dict[int, List[Dict[str, Any]]] = {}
    for e in net.get("transfer_edges") or []:
        transfer_by_from.setdefault(int(e["from_stop"]), []).append(e)

    best: Dict[Tuple[int, int, Optional[str]], float] = {}
    heap: List[Tuple[float, int, int, Optional[str]]] = []

    for sid, t0 in origin_stop_times.items():
        if t0 > max_time_s:
            continue
        key = (sid, 0, None)
        best[key] = t0
        heapq.heappush(heap, (t0, sid, 0, None))

    while heap:
        t, stop, transfers, last_route = heapq.heappop(heap)
        key = (stop, transfers, last_route)
        if t > best.get(key, INF) + 1e-6:
            continue
        if t > max_time_s:
            continue

        for e in ride_by_from.get(stop, []):
            route = str(e["route_num"])
            wait_s = _boarding_wait_s(route, last_route, e, route_headways)
            nt = t + wait_s + _ride_travel_time_s(
                e, route_speeds, segment_speeds, fallback_speed_kmh, walk_speed_kmh
            )
            if nt > max_time_s:
                continue
            ntransfers = transfers
            if last_route is not None and last_route != route:
                ntransfers = transfers + 1
            if ntransfers > max_transfers:
                continue
            nkey = (int(e["to_stop"]), ntransfers, route)
            if nt < best.get(nkey, INF):
                best[nkey] = nt
                heapq.heappush(heap, (nt, int(e["to_stop"]), ntransfers, route))

        if transfers < max_transfers:
            for e in transfer_by_from.get(stop, []):
                nt = t + float(e["walk_time_s"])
                if nt > max_time_s:
                    continue
                nkey = (int(e["to_stop"]), transfers, None)
                if nt < best.get(nkey, INF):
                    best[nkey] = nt
                    heapq.heappush(heap, (nt, int(e["to_stop"]), transfers, None))

    stop_arrival: Dict[int, float] = {}
    for (sid, _tr, _route), arr_t in best.items():
        if arr_t < stop_arrival.get(sid, INF):
            stop_arrival[sid] = arr_t
    return stop_arrival


def _combined_node_times(
    ped_times: Dict[int, float],
    stop_arrivals: Dict[int, float],
    stop_walk_cache: Dict[int, Dict[int, float]],
    stop_to_node: Dict[int, int],
    max_time_s: float,
) -> Dict[int, float]:
    combined: Dict[int, float] = dict(ped_times)
    for sid, arr_t in stop_arrivals.items():
        if arr_t >= INF:
            continue
        nid = stop_to_node.get(sid)
        if nid is None:
            continue
        if arr_t < combined.get(nid, INF):
            combined[nid] = arr_t
        remaining = max_time_s - arr_t
        if remaining <= 0:
            continue
        for n, wt in stop_walk_cache.get(sid, {}).items():
            total = arr_t + wt
            if total <= max_time_s and total < combined.get(n, INF):
                combined[n] = total
    return combined


def _pt_corridor_geom_m(
    net: Dict[str, Any],
    stop_by_id: Dict[int, Dict[str, Any]],
    stop_arrivals: Dict[int, float],
    limit_s: float,
    max_segments: int = 4000,
) -> Optional[Any]:
    """Буфер сегментов между ОП, обе конечные которых укладываются в бюджет времени."""
    lines_m: List[LineString] = []
    for e in net.get("ride_edges") or []:
        fs, ts = int(e["from_stop"]), int(e["to_stop"])
        if stop_arrivals.get(fs, INF) > limit_s + 1e-6 or stop_arrivals.get(ts, INF) > limit_s + 1e-6:
            continue
        sa, sb = stop_by_id.get(fs), stop_by_id.get(ts)
        if not sa or not sb:
            continue
        x1, y1 = _transformer_to_m.transform(float(sa["lon"]), float(sa["lat"]))
        x2, y2 = _transformer_to_m.transform(float(sb["lon"]), float(sb["lat"]))
        lines_m.append(LineString([(x1, y1), (x2, y2)]))
        if len(lines_m) >= max_segments:
            break
    if not lines_m:
        return None
    if len(lines_m) == 1:
        return lines_m[0].buffer(PT_CORRIDOR_BUFFER_M, cap_style=1, join_style=1)
    return MultiLineString(lines_m).buffer(PT_CORRIDOR_BUFFER_M, cap_style=1, join_style=1)


def _transit_zone_geometry(
    node_coords: Dict[int, Tuple[float, float]],
    adj: Dict[int, List[Tuple[int, float]]],
    combined_node_times: Dict[int, float],
    net: Dict[str, Any],
    stop_by_id: Dict[int, Dict[str, Any]],
    stop_arrivals: Dict[int, float],
    limit_s: float,
) -> Optional[Dict[str, Any]]:
    """Пешая сеть (достижимые узлы) + коридоры по рейсам между достижимыми ОП."""
    reachable = [n for n, t in combined_node_times.items() if t <= limit_s + 1e-6]
    parts = []

    walk_geo = _zone_polygon_from_reachable(node_coords, adj, reachable)
    if walk_geo:
        parts.append(shape(walk_geo))

    corridor_m = _pt_corridor_geom_m(net, stop_by_id, stop_arrivals, limit_s)
    if corridor_m is not None and not corridor_m.is_empty:
        corridor_geo = _geom_m_to_geojson(corridor_m)
        if corridor_geo:
            parts.append(shape(corridor_geo))

    if not parts:
        return None

    united = unary_union(parts)
    if united.is_empty:
        return None
    if united.geom_type == "GeometryCollection":
        sub = [
            g
            for g in united.geoms
            if not g.is_empty and g.geom_type in ("Polygon", "MultiPolygon")
        ]
        if not sub:
            return None
        united = unary_union(sub)
    return mapping(united)


def compute_transit_isochrones(
    origin_lon: float,
    origin_lat: float,
    intervals_min: List[float],
    max_snap_m: float = 80.0,
    use_elevation: bool = True,
    include_building_stats: bool = True,
    max_transfers: int = 1,
    max_walk_to_stop_m: float = 600.0,
    pt_speed_kmh: Optional[float] = None,
    route_speeds: Optional[Dict[str, float]] = None,
    segment_speeds: Optional[Dict[str, float]] = None,
    route_headways: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    intervals_sorted = sorted(set(float(t) for t in intervals_min if float(t) > 0))
    if not intervals_sorted:
        raise ValueError("intervals_min must contain positive values")

    ped = load_pedestrian_graph()
    net = load_pt_network()
    walk_speed_kmh = float((ped.get("meta") or {}).get("speed_kmh", DEFAULT_WALK_SPEED_KMH))
    max_time_s = intervals_sorted[-1] * 60.0
    adj, elev_info = _resolve_adj(ped, use_elevation)
    start_node, snap_m = nearest_graph_node(
        ped, origin_lon, origin_lat, max_snap_m=max_snap_m, adj=adj
    )
    if start_node is None:
        raise ValueError(f"Не удалось привязать точку к графу (>{max_snap_m} м)")

    ped_times, snap_penalty = compute_origin_walk_times(
        origin_lon,
        origin_lat,
        ped,
        adj,
        start_node,
        max_time_s,
        snap_m,
        walk_speed_kmh,
    )

    stop_by_id = {int(s["id"]): s for s in net.get("stops") or []}
    origin_stop_times: Dict[int, float] = {}
    walk_mps = max(walk_speed_kmh / 3.6, 0.25)
    for sid, s in stop_by_id.items():
        d = haversine_m(origin_lon, origin_lat, s["lon"], s["lat"])
        if d > max_walk_to_stop_m:
            continue
        nid = int(s["node_id"])
        graph_t = ped_times.get(nid, INF)
        direct_t = snap_penalty + (d * 1.25) / walk_mps
        t_board = min(graph_t, direct_t)
        if t_board >= INF or t_board > max_time_s:
            continue
        origin_stop_times[sid] = t_board

    stop_arrivals = _stop_dijkstra(
        net,
        origin_stop_times,
        max_time_s,
        max_transfers=max_transfers,
        route_speeds=route_speeds,
        segment_speeds=segment_speeds,
        fallback_speed_kmh=pt_speed_kmh,
        route_headways=route_headways,
        walk_speed_kmh=walk_speed_kmh,
    )

    node_coords: Dict[int, Tuple[float, float]] = ped["node_coords"]
    stop_to_node = {sid: int(s["node_id"]) for sid, s in stop_by_id.items()}

    stop_walk_cache: Dict[int, Dict[int, float]] = {}
    for sid in stop_arrivals:
        nid = stop_to_node.get(sid)
        if nid is None:
            continue
        stop_walk_cache[sid] = _dijkstra_times(adj, nid, max_time_s)

    combined_node_times = _combined_node_times(
        ped_times, stop_arrivals, stop_walk_cache, stop_to_node, max_time_s
    )

    zone_method = "network_buffer_pt_corridors"
    routes_with_speed = len(route_speeds or {})
    segments_with_speed = len(segment_speeds or {})
    routes_with_headway = len(route_headways or {})

    features: List[Dict[str, Any]] = []
    zones_meta: List[Dict[str, Any]] = []
    zone_features: List[Dict[str, Any]] = []

    for i, t_min in enumerate(intervals_sorted):
        limit_s = t_min * 60.0
        reachable = [n for n, t in combined_node_times.items() if t <= limit_s + 1e-6]
        geom = _transit_zone_geometry(
            node_coords,
            adj,
            combined_node_times,
            net,
            stop_by_id,
            stop_arrivals,
            limit_s,
        )
        if not geom:
            continue
        color = _ZONE_COLORS[i % len(_ZONE_COLORS)]
        stops_in_zone = sum(1 for t in stop_arrivals.values() if t <= limit_s + 1e-6)
        props = {
            "interval_min": t_min,
            "reachable_nodes": len(reachable),
            "stops_reached": stops_in_zone,
            "mode": "transit",
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
                "stops_reached": stops_in_zone,
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
        except Exception as ex:
            building_info = {"included": False, "warning": str(ex)}

    snap_lon, snap_lat = node_coords[start_node]
    speed_source = "walk_network"
    if segment_speeds:
        speed_source = "analysis_per_segment"
    elif route_speeds:
        speed_source = "analysis_per_route"
    elif pt_speed_kmh is not None:
        speed_source = "analysis_global"

    return {
        "mode": "transit",
        "origin": [origin_lon, origin_lat],
        "snapped_node": [snap_lon, snap_lat],
        "snap_distance_m": round(snap_m, 1),
        "snap_time_s": round(snap_penalty, 1),
        "zone_geometry_method": zone_method,
        "max_walk_to_stop_m": max_walk_to_stop_m,
        "intervals_min": intervals_sorted,
        "max_transfers": max_transfers,
        "stops_reachable": len(stop_arrivals),
        "zones": zones_meta,
        "elevation": elev_info,
        "buildings": building_info,
        "geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [origin_lon, origin_lat]},
                    "properties": {"role": "origin"},
                },
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [snap_lon, snap_lat]},
                    "properties": {"role": "snapped_node", "snap_distance_m": round(snap_m, 1)},
                },
            ]
            + features,
        },
        "pt_meta": net.get("meta"),
        "pt_speed_kmh": pt_speed_kmh,
        "pt_speed_source": speed_source,
        "routes_with_speed": routes_with_speed,
        "segments_with_speed": segments_with_speed,
        "routes_with_headway": routes_with_headway,
        "walk_speed_kmh_ref": walk_speed_kmh,
    }
