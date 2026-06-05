"""
Сборка пешего графа из GeoJSON (экспорт из QGIS) → data/pedestrian_graph.pkl

Запуск из папки backend:
  python -m scripts.build_pedestrian_graph
  python -m scripts.build_pedestrian_graph --input ../data/pedestrian_graph.geojson --speed-kmh 4.5
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import pyproj
from shapely.geometry import LineString, shape

# backend/ в PYTHONPATH при запуске как модуль
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from graph_store import default_geojson_path, default_pickle_path, project_root  # noqa: E402
from data_paths import default_contours_path  # noqa: E402

_transformer_to_m = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

NODE_MERGE_M = 12.0

# Для уже отфильтрованного слоя из QGIS фильтр можно отключить: --no-filter
_PEDESTRIAN_HIGHWAY = frozenset(
    {
        "footway",
        "path",
        "pedestrian",
        "steps",
        "living_street",
        "residential",
        "unclassified",
        "tertiary",
        "tertiary_link",
        "secondary",
        "secondary_link",
        "service",
        "track",
        "bridleway",
        "corridor",
        "crossing",
    }
)
_EXCLUDED_HIGHWAY = frozenset(
    {
        "motorway",
        "motorway_link",
        "trunk",
        "trunk_link",
        "construction",
        "proposed",
        "raceway",
        "bus_guideway",
        "escape",
    }
)
_DENIED_SERVICE = frozenset(
    {"driveway", "parking_aisle", "alley", "parking", "drive-through", "drive_through"}
)


def _norm(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def is_pedestrian_edge(props: Dict[str, Any]) -> bool:
    hw = _norm(props.get("highway"))
    if not hw:
        return True  # слой из QGIS уже без атрибута — считаем проходимым
    if hw in _EXCLUDED_HIGHWAY:
        return False
    if hw in _PEDESTRIAN_HIGHWAY:
        if hw == "service":
            sv = _norm(props.get("service"))
            if sv in _DENIED_SERVICE:
                return False
        foot = _norm(props.get("foot"))
        if foot in ("no", "private"):
            return False
        return True
    return False


def _cell_key(x: float, y: float, cell: float) -> Tuple[int, int]:
    return (int(math.floor(x / cell)), int(math.floor(y / cell)))


def _merge_nodes(points_m: List[Tuple[float, float]]) -> Dict[int, int]:
    """Сопоставление индекса точки → id узла после слияния близких."""
    n = len(points_m)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    cell = NODE_MERGE_M
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i, (x, y) in enumerate(points_m):
        grid[_cell_key(x, y, cell)].append(i)

    for i, (x, y) in enumerate(points_m):
        cx, cy = _cell_key(x, y, cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), []):
                    if j <= i:
                        continue
                    x2, y2 = points_m[j]
                    if math.hypot(x - x2, y - y2) <= NODE_MERGE_M:
                        union(i, j)

    root_to_id: Dict[int, int] = {}
    point_to_node: Dict[int, int] = {}
    next_id = 0
    for i in range(n):
        r = find(i)
        if r not in root_to_id:
            root_to_id[r] = next_id
            next_id += 1
        point_to_node[i] = root_to_id[r]
    return point_to_node


def _line_length_m(coords: List[Tuple[float, float]]) -> float:
    if len(coords) < 2:
        return 0.0
    xs, ys = zip(*[_transformer_to_m.transform(lon, lat) for lon, lat in coords])
    line = LineString(list(zip(xs, ys)))
    return float(line.length)


def build_graph(
    geojson_path: str,
    speed_kmh: float = 4.5,
    apply_filter: bool = True,
) -> Dict[str, Any]:
    with open(geojson_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    features = data.get("features") or []
    speed_mps = max(speed_kmh / 3.6, 0.2)

    raw_edges: List[Tuple[List[Tuple[float, float]], Dict[str, Any]]] = []
    skipped = 0

    for feat in features:
        geom = feat.get("geometry")
        if not geom:
            skipped += 1
            continue
        props = feat.get("properties") or {}
        if apply_filter and not is_pedestrian_edge(props):
            skipped += 1
            continue
        try:
            g = shape(geom)
        except Exception:
            skipped += 1
            continue
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
            coords = [(float(c[0]), float(c[1])) for c in line.coords]
            raw_edges.append((coords, props))

    # Концы рёбер → точки в метрах
    point_list: List[Tuple[float, float]] = []
    edge_endpoints: List[Tuple[int, int, float, Dict[str, Any]]] = []

    for coords, props in raw_edges:
        length_m = _line_length_m(coords)
        if length_m < 0.5:
            continue
        i0 = len(point_list)
        lon0, lat0 = coords[0]
        x0, y0 = _transformer_to_m.transform(lon0, lat0)
        point_list.append((x0, y0))

        i1 = len(point_list)
        lon1, lat1 = coords[-1]
        x1, y1 = _transformer_to_m.transform(lon1, lat1)
        point_list.append((x1, y1))

        edge_endpoints.append((i0, i1, length_m, props))

    point_to_node = _merge_nodes(point_list)
    node_count = max(point_to_node.values()) + 1 if point_to_node else 0

    # Координаты узлов (среднее по слитым точкам)
    node_acc: List[List[float]] = [[0.0, 0.0, 0] for _ in range(node_count)]
    for i, (x, y) in enumerate(point_list):
        nid = point_to_node[i]
        node_acc[nid][0] += x
        node_acc[nid][1] += y
        node_acc[nid][2] += 1

    node_coords_lonlat: Dict[int, Tuple[float, float]] = {}
    inv = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    for nid, (sx, sy, cnt) in enumerate(node_acc):
        if cnt == 0:
            continue
        lon, lat = inv.transform(sx / cnt, sy / cnt)
        node_coords_lonlat[nid] = (float(lon), float(lat))

    adj: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    edges_out: List[Dict[str, Any]] = []
    edge_id = 0
    seen: Set[Tuple[int, int]] = set()

    for i0, i1, length_m, props in edge_endpoints:
        u = point_to_node[i0]
        v = point_to_node[i1]
        if u == v:
            continue
        walk_time_s = length_m / speed_mps
        for a, b in ((u, v), (v, u)):
            key = (a, b)
            if key in seen:
                continue
            seen.add(key)
            adj[a].append((b, walk_time_s))
            edges_out.append(
                {
                    "edge_id": edge_id,
                    "from": a,
                    "to": b,
                    "length_m": round(length_m, 2),
                    "walk_time_s": round(walk_time_s, 2),
                    "highway": props.get("highway"),
                }
            )
            edge_id += 1

    meta = {
        "source_geojson": os.path.abspath(geojson_path),
        "speed_kmh": speed_kmh,
        "node_merge_m": NODE_MERGE_M,
        "node_count": len(node_coords_lonlat),
        "edge_count": len(edges_out),
        "features_in": len(features),
        "features_skipped": skipped,
        "raw_line_edges": len(raw_edges),
    }

    return {
        "meta": meta,
        "node_coords": node_coords_lonlat,
        "edges": edges_out,
        "adj": {k: list(v) for k, v in adj.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Сборка пешего графа GeoJSON → pickle")
    parser.add_argument(
        "--input",
        "-i",
        default=default_geojson_path(),
        help="Путь к pedestrian_graph.geojson (экспорт из QGIS)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=default_pickle_path(),
        help="Куда сохранить pickle",
    )
    parser.add_argument(
        "--speed-kmh",
        type=float,
        default=4.5,
        help="Базовая пешеходная скорость на ровном (этап 1, без рельефа)",
    )
    parser.add_argument(
        "--no-filter",
        action="store_true",
        help="Не фильтровать по highway (если в QGIS уже отобрана пешеходная сеть)",
    )
    parser.add_argument(
        "--elevation",
        action="store_true",
        help="Учесть изолинии (contours.geojson), если файл найден",
    )
    parser.add_argument(
        "--contours",
        default=None,
        help="Путь к GeoJSON изолиний (иначе авто-поиск)",
    )
    parser.add_argument("--slope-k", type=float, default=6.0, help="Коэффициент уклона")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Файл не найден: {args.input}")
        print()
        print("Сначала экспортируйте линейный слой из QGIS:")
        print("  data/pedestrian_graph.geojson  (WGS84, LineString/MultiLineString)")
        print(f"Корень проекта: {project_root()}")
        sys.exit(1)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)

    graph = build_graph(
        args.input,
        speed_kmh=args.speed_kmh,
        apply_filter=not args.no_filter,
    )

    contours = args.contours or default_contours_path()
    if args.elevation and contours:
        from elevation import apply_elevation_to_graph

        apply_elevation_to_graph(graph, contours_path=contours, slope_k=args.slope_k)
        print(f"  Рельеф: {contours}")
    elif args.elevation:
        print("  Рельеф: contours.geojson не найден — пропуск")

    with open(args.output, "wb") as f:
        pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)

    m = graph["meta"]
    print("Готово:", args.output)
    print(f"  Узлов: {m['node_count']}")
    print(f"  Рёбер (направленных): {m['edge_count']}")
    print(f"  Исходных линий: {m['raw_line_edges']}, пропущено объектов: {m['features_skipped']}")
    print(f"  Скорость: {m['speed_kmh']} км/ч")


if __name__ == "__main__":
    main()
