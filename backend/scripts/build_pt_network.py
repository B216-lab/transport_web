"""
Сборка сети ОТ: остановки + маршруты + время в пути + пешие пересадки между ОП.

Источники:
  - proj_stop.geojson — остановки
  - data/route_stops.json — порядок ОП по маршрутам (можно сгенерировать из треков)
  - data/route_meta.json — интервалы движения (headway_min)

Запуск:
  python -m scripts.build_pt_network
  python -m scripts.build_pt_network --from-db --days 14
  python -m scripts.build_pt_network --infer-routes --tracks ../output.geojson
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import heapq

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from graph_store import load_pedestrian_graph, project_root  # noqa: E402
from isochrone.pedestrian import nearest_graph_node, _dijkstra_times  # noqa: E402
from pt_network_store import default_pt_pickle_path, default_stops_geojson_path  # noqa: E402
from utils import haversine_m, parse_time  # noqa: E402

TRANSFER_MAX_M = 500.0
DEFAULT_HEADWAY_MIN = 12.0
STOP_SNAP_M = 100.0


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _ped_adj(graph: Dict[str, Any], use_elevation: bool) -> Dict[int, List[Tuple[int, float]]]:
    raw = graph.get("adj_elevation") if use_elevation and graph.get("adj_elevation") else graph.get("adj")
    adj: Dict[int, List[Tuple[int, float]]] = {}
    for k, edges in (raw or {}).items():
        adj[int(k)] = [(int(v), float(w)) for v, w in edges]
    return adj


def _walk_time_between_nodes(
    adj: Dict[int, List[Tuple[int, float]]],
    node_from: int,
    node_to: int,
    max_s: float = 3600.0,
) -> float:
    if node_from == node_to:
        return 0.0
    times = _dijkstra_times(adj, node_from, max_s)
    return float(times.get(node_to, float("inf")))


def infer_route_stops_from_features(
    features: List[Dict[str, Any]],
    stops: List[Dict[str, Any]],
    snap_m: float = 120.0,
) -> Dict[str, List[int]]:
    """По GPS-трекам (список GeoJSON features): порядок ОП на каждом маршруте."""
    stop_coords = [(s["id"], s["lon"], s["lat"]) for s in stops]

    def nearest_stop(lon: float, lat: float) -> Optional[int]:
        best, best_d = None, snap_m
        for sid, slon, slat in stop_coords:
            d = haversine_m(lon, lat, slon, slat)
            if d < best_d:
                best_d, best = d, sid
        return best

    route_sequences: Dict[str, List[List[int]]] = defaultdict(list)

    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for feat in features:
        p = feat.get("properties") or {}
        r = p.get("route_num")
        if not r:
            continue
        groups[str(r)].append(feat)

    for route_num, feats in groups.items():
        by_vehicle: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for f in feats:
            p = f.get("properties") or {}
            key = str(p.get("vehicle_id") or p.get("gos_num") or p.get("id") or "unknown")
            by_vehicle[key].append(f)

        for _vid, points in by_vehicle.items():
            points.sort(
                key=lambda f: parse_time(str((f.get("properties") or {}).get("time", "")))
                or parse_time(str((f.get("properties") or {}).get("event_time", "")))
                or __import__("datetime").datetime.min,
            )
            seq: List[int] = []
            for f in points:
                c = f.get("geometry", {}).get("coordinates") or []
                while isinstance(c, list) and c and isinstance(c[0], list):
                    c = c[0]
                if len(c) < 2:
                    continue
                sid = nearest_stop(float(c[0]), float(c[1]))
                if sid is None:
                    continue
                if not seq or seq[-1] != sid:
                    seq.append(sid)
            if len(seq) >= 2:
                route_sequences[route_num].append(seq)

    result: Dict[str, List[int]] = {}
    for route_num, seqs in route_sequences.items():
        if not seqs:
            continue
        # Самая длинная цепочка как эталон
        best = max(seqs, key=len)
        result[route_num] = best
    return result


def infer_route_stops_from_tracks(
    tracks_path: str,
    stops: List[Dict[str, Any]],
    snap_m: float = 120.0,
) -> Dict[str, List[int]]:
    data = _load_json(tracks_path)
    return infer_route_stops_from_features(data.get("features") or [], stops, snap_m=snap_m)


def infer_route_stops_from_db(
    stops: List[Dict[str, Any]],
    days: Optional[int] = 14,
    max_points_per_route: int = 80000,
    route_nums: Optional[List[str]] = None,
    snap_m: float = 120.0,
) -> Dict[str, List[int]]:
    from pt_tracks_db import _since_from_days, fetch_route_features, get_db_dsn, list_route_numbers

    dsn = get_db_dsn()
    routes = route_nums or list_route_numbers(dsn)
    result: Dict[str, List[int]] = {}
    print(f"Маршрутов в БД: {len(routes)}")
    since = _since_from_days(days, dsn) if days and days > 0 else None
    for i, route_num in enumerate(routes, 1):
        feats = fetch_route_features(
            route_num,
            max_points=max_points_per_route,
            days=None,
            dsn=dsn,
            since=since,
        )
        if not feats:
            continue
        partial = infer_route_stops_from_features(feats, stops, snap_m=snap_m)
        if route_num in partial:
            result[route_num] = partial[route_num]
        if i % 10 == 0 or i == len(routes):
            print(f"  обработано {i}/{len(routes)}, построено цепочек: {len(result)}")
    return result


def build_pt_network(
    stops_path: str,
    route_stops: Dict[str, List[int]],
    route_meta: Dict[str, Dict[str, Any]],
    use_elevation: bool = True,
    transfer_max_m: float = TRANSFER_MAX_M,
) -> Dict[str, Any]:
    graph = load_pedestrian_graph()
    adj = _ped_adj(graph, use_elevation)

    raw = _load_json(stops_path)
    stops_out: List[Dict[str, Any]] = []
    for feat in raw.get("features") or []:
        sid = feat.get("properties", {}).get("id")
        geom = feat.get("geometry") or {}
        if sid is None or geom.get("type") != "Point":
            continue
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        node_id, snap_d = nearest_graph_node(graph, lon, lat, max_snap_m=STOP_SNAP_M)
        if node_id is None:
            continue
        stops_out.append(
            {
                "id": int(sid),
                "name": feat.get("properties", {}).get("name"),
                "lon": lon,
                "lat": lat,
                "node_id": int(node_id),
                "snap_m": round(snap_d, 1),
            }
        )

    stop_by_id = {s["id"]: s for s in stops_out}
    ride_edges: List[Dict[str, Any]] = []
    walk_mps = max(float((graph.get("meta") or {}).get("speed_kmh", 4.5)) / 3.6, 0.25)

    for route_num, stop_ids in route_stops.items():
        meta = route_meta.get(str(route_num), {})
        headway = float(meta.get("headway_min", DEFAULT_HEADWAY_MIN))
        ids = [int(x) for x in stop_ids if int(x) in stop_by_id]
        for i in range(len(ids) - 1):
            a, b = ids[i], ids[i + 1]
            na, nb = stop_by_id[a]["node_id"], stop_by_id[b]["node_id"]
            t_ab = _walk_time_between_nodes(adj, na, nb)
            t_ba = _walk_time_between_nodes(adj, nb, na)
            # оценка длины сегмента по времени на графе (лучше, чем прямая между ОП)
            len_ab = t_ab * walk_mps if t_ab < float("inf") else float("inf")
            if t_ab < float("inf"):
                ride_edges.append(
                    {
                        "route_num": str(route_num),
                        "from_stop": a,
                        "to_stop": b,
                        "travel_time_s": round(t_ab, 1),
                        "length_m": round(len_ab, 1),
                        "headway_min": headway,
                    }
                )
            if t_ba < float("inf"):
                ride_edges.append(
                    {
                        "route_num": str(route_num),
                        "from_stop": b,
                        "to_stop": a,
                        "travel_time_s": round(t_ba, 1),
                        "length_m": round(len_ab, 1),
                        "headway_min": headway,
                    }
                )

    # Пешие пересадки между близкими ОП (оценка по расстоянию — быстро на ~1000+ ОП)
    walk_mps = 4.5 / 3.6
    detour_factor = 1.25
    transfer_edges: List[Dict[str, Any]] = []
    cell_deg = 0.0045
    grid: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for s in stops_out:
        grid[(int(s["lon"] / cell_deg), int(s["lat"] / cell_deg))].append(s)

    seen_pairs: Set[Tuple[int, int]] = set()
    for s in stops_out:
        cx, cy = int(s["lon"] / cell_deg), int(s["lat"] / cell_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for o in grid.get((cx + dx, cy + dy), []):
                    if o["id"] == s["id"]:
                        continue
                    pair = (min(s["id"], o["id"]), max(s["id"], o["id"]))
                    if pair in seen_pairs:
                        continue
                    d = haversine_m(s["lon"], s["lat"], o["lon"], o["lat"])
                    if d > transfer_max_m:
                        continue
                    seen_pairs.add(pair)
                    t = (d * detour_factor) / walk_mps
                    transfer_edges.append(
                        {"from_stop": s["id"], "to_stop": o["id"], "walk_time_s": round(t, 1)}
                    )
                    transfer_edges.append(
                        {"from_stop": o["id"], "to_stop": s["id"], "walk_time_s": round(t, 1)}
                    )

    return {
        "meta": {
            "stops_source": os.path.abspath(stops_path),
            "stop_count": len(stops_out),
            "route_count": len(route_stops),
            "ride_edge_count": len(ride_edges),
            "transfer_edge_count": len(transfer_edges),
            "transfer_max_m": transfer_max_m,
            "use_elevation": use_elevation,
        },
        "stops": stops_out,
        "route_stops": route_stops,
        "route_meta": route_meta,
        "ride_edges": ride_edges,
        "transfer_edges": transfer_edges,
    }


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(_BACKEND, ".env"), override=False)
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Сборка сети ОТ")
    parser.add_argument("--stops", default=default_stops_geojson_path())
    parser.add_argument("--output", "-o", default=default_pt_pickle_path())
    parser.add_argument("--tracks", help="GeoJSON треков (FeatureCollection) для авто-порядка ОП")
    parser.add_argument("--infer-routes", action="store_true", help="Вывести route_stops из --tracks")
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="Взять треки всех маршрутов из PostgreSQL (IRKBUS_DB_DSN)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=14,
        help="Глубина выборки из БД, дней (0 = без ограничения по времени)",
    )
    parser.add_argument(
        "--max-points-per-route",
        type=int,
        default=80000,
        help="Лимит точек на маршрут при выборке из БД",
    )
    parser.add_argument("--no-elevation", action="store_true")
    args = parser.parse_args()

    root = project_root()
    data_dir = os.path.join(root, "data")
    os.makedirs(data_dir, exist_ok=True)

    route_stops_path = os.path.join(data_dir, "route_stops.json")
    route_meta_path = os.path.join(data_dir, "route_meta.json")

    stops_data = _load_json(args.stops)
    stops_list = []
    for feat in stops_data.get("features") or []:
        sid = feat.get("properties", {}).get("id")
        geom = feat.get("geometry") or {}
        if sid is None or geom.get("type") != "Point":
            continue
        c = geom.get("coordinates") or []
        if len(c) >= 2:
            stops_list.append({"id": int(sid), "lon": float(c[0]), "lat": float(c[1])})

    if args.from_db:
        route_stops = infer_route_stops_from_db(
            stops_list,
            days=args.days if args.days > 0 else None,
            max_points_per_route=args.max_points_per_route,
        )
        with open(route_stops_path, "w", encoding="utf-8") as f:
            json.dump(route_stops, f, ensure_ascii=False, indent=2)
        print(f"Сохранён {route_stops_path} ({len(route_stops)} маршрутов из БД)")
    elif args.infer_routes and args.tracks:
        route_stops = infer_route_stops_from_tracks(args.tracks, stops_list)
        with open(route_stops_path, "w", encoding="utf-8") as f:
            json.dump(route_stops, f, ensure_ascii=False, indent=2)
        print(f"Сохранён {route_stops_path} ({len(route_stops)} маршрутов)")
    elif os.path.isfile(route_stops_path):
        route_stops = _load_json(route_stops_path)
    else:
        print("Нет data/route_stops.json — создайте вручную или укажите --infer-routes --tracks")
        route_stops = {}

    if os.path.isfile(route_meta_path):
        route_meta = _load_json(route_meta_path)
    else:
        route_meta = {}
    for r in route_stops:
        route_meta.setdefault(str(r), {"headway_min": DEFAULT_HEADWAY_MIN})
    with open(route_meta_path, "w", encoding="utf-8") as f:
        json.dump(route_meta, f, ensure_ascii=False, indent=2)

    if not route_stops:
        sys.exit(1)

    net = build_pt_network(
        args.stops,
        route_stops,
        route_meta,
        use_elevation=not args.no_elevation,
    )
    with open(args.output, "wb") as f:
        pickle.dump(net, f, protocol=pickle.HIGHEST_PROTOCOL)

    m = net["meta"]
    print("Готово:", args.output)
    print(f"  Остановок: {m['stop_count']}")
    print(f"  Маршрутов: {m['route_count']}")
    print(f"  Рёбер рейса: {m['ride_edge_count']}")
    print(f"  Рёбер пересадки (пешком): {m['transfer_edge_count']}")


if __name__ == "__main__":
    main()
