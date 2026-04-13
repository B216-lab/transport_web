"""
Привязка точек к линейному GeoJSON (граф дорог): Shapely + STRtree + последовательный Viterbi.

По каждому ТС: для каждой эпохи — несколько кандидатных рёбер вокруг GPS, далее динамическое
программирование с учётом:
- связности рёбер (общие концы в пределах NODE_MERGE_M);
- согласованности длины шага GPS и расстояния между проекциями на дорогу;
- азимута (как раньше) в эмиссии.

Это сильнее стабилизирует траекторию, чем независимый выбор «ближайшей линии» на каждой точке.

Рёбра графа фильтруются под автобус: исключаются пешеходные/велотипы, track, living_street,
типичные дворовые service (подъезд, парковка), грунт по surface/tracktype. Для оставшихся дорог
в эмиссии Viterbi добавляется штраф за низкий класс highway (магистрали предпочтительнее).
"""
from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pyproj
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import nearest_points, transform
from shapely.strtree import STRtree

from utils import haversine_m, parse_time

logger = logging.getLogger(__name__)

_transformer_to_m = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
_transformer_to_deg = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

# mtime, tree, lines_m, neighbors, edge_props (параллельно lines_m)
_roads_cache: Dict[str, Tuple[float, STRtree, List[LineString], Dict[int, Set[int]], List[Dict[str, Any]]]] = {}

ANGLE_WEIGHT_M = 22.0
MIN_LEG_M = 2.0

MAX_CANDIDATES = 14
NODE_MERGE_M = 14.0
TRANS_GAP_M = 92.0
TRANS_ADJ_M = 5.0
TRANS_SPEED_SCALE = 0.28
INF = 1.0e15

# Разрыв анонимного трека (все без gos_num/plate попали в "unknown"): новое ТС / новый рейс
TRACK_BREAK_GAP_M = 2800.0
TRACK_BREAK_TIME_S = 20 * 60

# Штраф в метрах (эмиссия): чем больше — тем реже алгоритм выберет этот класс при равном расстоянии
_HIGHWAY_PRIORITY_EXTRA_M: Dict[str, float] = {
    "motorway": 0.0,
    "motorway_link": 0.5,
    "trunk": 0.5,
    "trunk_link": 1.0,
    "primary": 1.5,
    "primary_link": 2.0,
    "secondary": 3.5,
    "secondary_link": 4.5,
    "tertiary": 7.0,
    "tertiary_link": 8.0,
    "unclassified": 11.0,
    "busway": 2.5,
    "bus_guideway": 2.0,
    "residential": 16.0,
    "service": 28.0,
}
_DEFAULT_PRIORITY_EXTRA_M = 14.0

# Типы highway, к которым автобус не привязываем
_EXCLUDED_HIGHWAY = frozenset(
    {
        "path",
        "footway",
        "cycleway",
        "pedestrian",
        "steps",
        "bridleway",
        "corridor",
        "platform",
        "raceway",
        "track",
        "living_street",
        "construction",
        "proposed",
        "escape",
        "services",
        "road",
        "elevator",
    }
)

# service=* при highway=service — разрешены только явно «транспортные»
_ALLOWED_SERVICE_SUBTYPE = frozenset({"bus", "busway"})

# Явно дворовые / вспомогательные
_DENIED_SERVICE_SUBTYPE = frozenset(
    {
        "driveway",
        "parking_aisle",
        "alley",
        "drive-through",
        "drive_through",
        "emergency_access",
        "parking",
        "driveway2",
        "slipway",
        "yard",
        "drive-through2",
    }
)

_BAD_SURFACE = frozenset(
    {
        "unpaved",
        "gravel",
        "fine_gravel",
        "pebblestone",
        "ground",
        "earth",
        "dirt",
        "grass",
        "sand",
        "mud",
        "wood",
        "clay",
        "rock",
        "artificial_turf",
    }
)


def _norm_prop(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def _surface_bus_ok(props: Dict[str, Any]) -> bool:
    s = _norm_prop(props.get("surface"))
    if not s:
        return True
    if s in _BAD_SURFACE:
        return False
    for bad in ("unpaved", "gravel", "dirt", "ground", "grass", "sand", "mud", "earth"):
        if bad in s:
            return False
    return True


def _is_bus_routable_edge(props: Dict[str, Any]) -> bool:
    hw = _norm_prop(props.get("highway"))
    if not hw:
        return False
    if hw in _EXCLUDED_HIGHWAY:
        return False
    if os.environ.get("BUS_SNAP_STRICT", "").lower() in ("1", "true", "yes") and hw == "residential":
        return False
    if hw == "service":
        sv = _norm_prop(props.get("service"))
        if sv in _ALLOWED_SERVICE_SUBTYPE:
            return True
        if sv in _DENIED_SERVICE_SUBTYPE or not sv:
            return False
        return False
    if not _surface_bus_ok(props):
        return False
    tt = _norm_prop(props.get("tracktype"))
    if tt in ("grade4", "grade5"):
        return False
    return True


def _priority_penalty_m(props: Dict[str, Any]) -> float:
    hw = _norm_prop(props.get("highway"))
    return float(_HIGHWAY_PRIORITY_EXTRA_M.get(hw, _DEFAULT_PRIORITY_EXTRA_M))


def _vehicle_key(props: Dict[str, Any]) -> str:
    for k in ("gos_num", "plate", "bnum", "vehicle_id", "veh_id", "device_id"):
        v = props.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "unknown"


def _split_sorted_track(features: List[Dict[str, Any]], idxs: List[int]) -> List[List[int]]:
    """Делит индексы, уже отсортированные по времени, если соседние точки не могут быть одним треком."""
    if len(idxs) <= 1:
        return [idxs] if idxs else []
    chunks: List[List[int]] = []
    cur: List[int] = [idxs[0]]
    for prev_i, next_i in zip(idxs, idxs[1:]):
        g0 = features[prev_i].get("geometry") or {}
        g1 = features[next_i].get("geometry") or {}
        c0, c1 = g0.get("coordinates"), g1.get("coordinates")
        split = False
        if (
            isinstance(c0, list)
            and isinstance(c1, list)
            and len(c0) >= 2
            and len(c1) >= 2
            and isinstance(c0[0], (int, float))
            and isinstance(c1[0], (int, float))
        ):
            try:
                d_m = haversine_m(float(c0[0]), float(c0[1]), float(c1[0]), float(c1[1]))
                if d_m > TRACK_BREAK_GAP_M:
                    split = True
            except (TypeError, ValueError):
                pass
        if not split:
            t0 = parse_time(str(features[prev_i].get("properties", {}).get("time", "")))
            t1 = parse_time(str(features[next_i].get("properties", {}).get("time", "")))
            if t0 and t1:
                gap = (t1 - t0).total_seconds()
                if gap > TRACK_BREAK_TIME_S:
                    split = True
        if split:
            chunks.append(cur)
            cur = [next_i]
        else:
            cur.append(next_i)
    chunks.append(cur)
    return chunks


def _nearest_routable_candidate(
    pt_m: Point,
    tree: STRtree,
    lines_m: List[LineString],
    max_m: float,
) -> Optional[Tuple[int, Point, float]]:
    try:
        j = int(tree.nearest(pt_m))
    except Exception:
        return None
    if j < 0 or j >= len(lines_m):
        return None
    line_m = lines_m[j]
    snap_m, _ = nearest_points(line_m, pt_m)
    dist = float(pt_m.distance(snap_m))
    if dist > max_m:
        return None
    return (j, snap_m, dist)


def _resolve_roads_path(roads_file_path: str) -> str:
    p = os.path.expanduser(roads_file_path.strip())
    if os.path.isfile(p):
        return os.path.abspath(p)
    here = os.path.dirname(os.path.abspath(__file__))
    alt = os.path.abspath(os.path.join(here, "..", roads_file_path))
    if os.path.isfile(alt):
        return alt
    return os.path.abspath(p)


def _to_m(geom):
    return transform(lambda x, y: _transformer_to_m.transform(x, y), geom)


def _to_wgs(pt_m: Point) -> Tuple[float, float]:
    lon, lat = _transformer_to_deg.transform(pt_m.x, pt_m.y)
    return float(lon), float(lat)


def _collect_bus_routable_edges(fc: Dict[str, Any]) -> Tuple[List[LineString], List[Dict[str, Any]], int]:
    """
    Только рёбра, по которым допустима привязка автобуса; для каждого — свойства OSM (highway, surface, …).
    """
    lines: List[LineString] = []
    edge_props: List[Dict[str, Any]] = []
    skipped = 0
    for feat in fc.get("features") or []:
        props = feat.get("properties") or {}
        if not _is_bus_routable_edge(props):
            skipped += 1
            continue
        g = feat.get("geometry")
        if not g:
            skipped += 1
            continue
        try:
            geom = shape(g)
        except Exception:
            skipped += 1
            continue
        if geom.geom_type == "LineString":
            if len(geom.coords) >= 2:
                lines.append(geom)
                edge_props.append(props)
        elif geom.geom_type == "MultiLineString":
            for part in geom.geoms:
                if len(part.coords) >= 2:
                    lines.append(part)
                    edge_props.append(props)
    return lines, edge_props, skipped


def _build_neighbors(lines_m: List[LineString], merge_m: float) -> Dict[int, Set[int]]:
    pts: List[Tuple[int, Point]] = []
    for i, ln in enumerate(lines_m):
        arr = np.asarray(ln.coords)
        pts.append((i, Point(float(arr[0, 0]), float(arr[0, 1]))))
        pts.append((i, Point(float(arr[-1, 0]), float(arr[-1, 1]))))
    endpoints = [p[1] for p in pts]
    tree_e = STRtree(endpoints)
    neigh: Dict[int, Set[int]] = defaultdict(set)
    for k, (seg_i, pt) in enumerate(pts):
        try:
            raw = tree_e.query(pt.buffer(merge_m), predicate="intersects")
        except Exception:
            continue
        for idx in np.atleast_1d(np.asarray(raw, dtype=np.int64)).tolist():
            j = int(idx)
            if j == k:
                continue
            seg_j, _ = pts[j]
            if seg_i != seg_j:
                neigh[seg_i].add(seg_j)
                neigh[seg_j].add(seg_i)
    return neigh


def _load_graph(roads_path: str) -> Tuple[STRtree, List[LineString], Dict[int, Set[int]], List[Dict[str, Any]]]:
    key = os.path.abspath(roads_path)
    mtime = os.path.getmtime(key)
    hit = _roads_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2], hit[3], hit[4]

    logger.info("Загрузка дорожного графа: %s", key)
    with open(key, "r", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("type") != "FeatureCollection":
        raise ValueError("Ожидается GeoJSON FeatureCollection дорог")

    wgs_lines, edge_props, skipped = _collect_bus_routable_edges(data)
    if not wgs_lines:
        raise ValueError(
            "После фильтра под автобус не осталось рёбер. Проверьте теги highway/surface в GeoJSON."
        )

    lines_m = [_to_m(ln) for ln in wgs_lines]
    tree = STRtree(lines_m)
    logger.info("Построение смежности рёбер (≤%.0f м между узлами)…", NODE_MERGE_M)
    neighbors = _build_neighbors(lines_m, NODE_MERGE_M)
    _roads_cache[key] = (mtime, tree, lines_m, neighbors, edge_props)
    logger.info(
        "Граф для автобуса: %d рёбер (отброшено по типу/покрытию: %d), ср. степень %.1f",
        len(lines_m),
        skipped,
        _avg_deg(neighbors),
    )
    return tree, lines_m, neighbors, edge_props


def _avg_deg(neighbors: Dict[int, Set[int]]) -> float:
    if not neighbors:
        return 0.0
    s = sum(len(v) for v in neighbors.values())
    return s / max(len(neighbors), 1)


def _indices_from_query(raw) -> List[int]:
    if raw is None:
        return []
    # Shapely ≥2: иногда (input_idx, tree_idx); нужны индексы геометрий в дереве
    if isinstance(raw, tuple) and len(raw) == 2:
        raw = raw[1]
    arr = np.atleast_1d(np.asarray(raw, dtype=np.int64))
    out: List[int] = []
    seen = set()
    for x in arr.tolist():
        i = int(x)
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _bearing_move_m(a: Point, b: Point) -> Optional[float]:
    dx, dy = b.x - a.x, b.y - a.y
    leg = math.hypot(dx, dy)
    if leg < MIN_LEG_M:
        return None
    return math.atan2(dy, dx)


def _line_bearing_at_snap_m(line_m: LineString, snap_m: Point) -> float:
    try:
        sld = line_m.project(snap_m)
    except Exception:
        return 0.0
    length = line_m.length
    if length <= 0:
        return 0.0
    span = min(12.0, max(length * 0.12, 2.0))
    d0 = max(0.0, sld - span / 2)
    d1 = min(length, sld + span / 2)
    if d1 - d0 < 1.0:
        d0, d1 = max(0.0, sld - 0.5), min(length, sld + 0.5)
    p0 = line_m.interpolate(d0)
    p1 = line_m.interpolate(d1)
    return math.atan2(p1.y - p0.y, p1.x - p0.x)


def _travel_vs_road_penalty(travel_b: float, line_b: float) -> float:
    d = abs(travel_b - line_b)
    d = d % (2 * math.pi)
    if d > math.pi:
        d = 2 * math.pi - d
    perp = min(d, math.pi - d)
    return perp / (math.pi / 2)


def _enumerate_candidates(
    pt_m: Point,
    tree: STRtree,
    lines_m: List[LineString],
    tol_search: float,
    tol_keep: float,
    max_k: int,
) -> List[Tuple[int, Point, float]]:
    buf = pt_m.buffer(tol_search)
    try:
        raw = tree.query(buf, predicate="intersects")
    except Exception:
        raw = None
    idxs = _indices_from_query(raw)
    if not idxs:
        try:
            ni = tree.nearest(pt_m)
            idxs = [int(np.asarray(ni).reshape(-1)[0])]
        except Exception:
            idxs = []

    scored: List[Tuple[float, int, Point]] = []
    for idx in idxs:
        if idx < 0 or idx >= len(lines_m):
            continue
        line_m = lines_m[idx]
        snap_m, _ = nearest_points(line_m, pt_m)
        dist = float(pt_m.distance(snap_m))
        if dist > tol_keep:
            continue
        scored.append((dist, idx, snap_m))
    scored.sort(key=lambda x: x[0])
    out = [(i, s, d) for d, i, s in scored[:max_k]]
    if not out and idxs:
        try:
            idx = int(tree.nearest(pt_m))
            line_m = lines_m[idx]
            snap_m, _ = nearest_points(line_m, pt_m)
            d = float(pt_m.distance(snap_m))
            if d <= tol_keep * 1.85:
                out = [(idx, snap_m, d)]
        except Exception:
            pass
    return out


def _emission(
    gps_pts_m: List[Point],
    i: int,
    cand: Tuple[int, Point, float],
    lines_m: List[LineString],
    edge_props: List[Dict[str, Any]],
) -> float:
    seg, snap_m, dist = cand
    pt_m = gps_pts_m[i]
    em = dist + _priority_penalty_m(edge_props[seg])
    prev_m = gps_pts_m[i - 1] if i > 0 else None
    next_m = gps_pts_m[i + 1] if i + 1 < len(gps_pts_m) else None
    travel_b: Optional[float] = None
    if prev_m is not None:
        travel_b = _bearing_move_m(prev_m, pt_m)
    if travel_b is None and next_m is not None:
        travel_b = _bearing_move_m(pt_m, next_m)
    if travel_b is not None:
        lb = _line_bearing_at_snap_m(lines_m[seg], snap_m)
        em += ANGLE_WEIGHT_M * _travel_vs_road_penalty(travel_b, lb)
    return em


def _transition(
    neighbors: Dict[int, Set[int]],
    seg_p: int,
    snap_p: Point,
    seg_c: int,
    snap_c: Point,
    gps_p: Point,
    gps_c: Point,
) -> float:
    d_gps = float(gps_p.distance(gps_c))
    d_snap = float(snap_p.distance(snap_c))
    if d_gps < 0.8:
        speed_pen = 0.0
    else:
        speed_pen = min(abs(d_snap - d_gps), 280.0) * TRANS_SPEED_SCALE
    if seg_p == seg_c:
        conn = 0.0
    elif seg_c in neighbors.get(seg_p, ()):
        conn = TRANS_ADJ_M
    else:
        conn = TRANS_GAP_M
    return speed_pen + conn


def _viterbi_track(
    gps_pts_m: List[Point],
    tree: STRtree,
    lines_m: List[LineString],
    neighbors: Dict[int, Set[int]],
    edge_props: List[Dict[str, Any]],
    base_tol: float,
) -> List[Optional[Point]]:
    n = len(gps_pts_m)
    if n == 0:
        return []
    all_cands: List[List[Tuple[int, Point, float]]] = []
    for relax in (1.0, 1.65):
        tol_search = min(base_tol * 1.45 * relax, 170.0 * relax)
        tol_keep = min(base_tol * 1.22 * relax, 135.0 * relax)
        all_cands = [
            _enumerate_candidates(gps_pts_m[i], tree, lines_m, tol_search, tol_keep, MAX_CANDIDATES)
            for i in range(n)
        ]
        if not any(len(c) == 0 for c in all_cands):
            break

    rescue_m = max(280.0, min(base_tol * 5.0, 450.0))
    for i in range(n):
        if all_cands[i]:
            continue
        forced = _nearest_routable_candidate(gps_pts_m[i], tree, lines_m, rescue_m)
        if forced:
            all_cands[i] = [forced]

    if any(len(c) == 0 for c in all_cands):
        return [None] * n

    if n == 1:
        c0 = all_cands[0]
        best = min(
            range(len(c0)),
            key=lambda kk: _emission(gps_pts_m, 0, c0[kk], lines_m, edge_props),
        )
        snap = c0[best][1]
        return [snap]

    dp_prev = [INF] * len(all_cands[0])
    for k in range(len(all_cands[0])):
        dp_prev[k] = _emission(gps_pts_m, 0, all_cands[0][k], lines_m, edge_props)

    all_bp: List[List[int]] = []

    for i in range(1, n):
        Ki = len(all_cands[i])
        dp_cur = [INF] * Ki
        bp_row = [-1] * Ki
        gps_p, gps_c = gps_pts_m[i - 1], gps_pts_m[i]
        for k in range(Ki):
            seg_c, snap_c, _ = all_cands[i][k]
            em = _emission(gps_pts_m, i, all_cands[i][k], lines_m, edge_props)
            for kp in range(len(all_cands[i - 1])):
                seg_p, snap_p, _ = all_cands[i - 1][kp]
                tr = _transition(neighbors, seg_p, snap_p, seg_c, snap_c, gps_p, gps_c)
                tot = dp_prev[kp] + tr + em
                if tot < dp_cur[k]:
                    dp_cur[k] = tot
                    bp_row[k] = kp
        all_bp.append(bp_row)
        dp_prev = dp_cur

    best_k = min(range(len(all_cands[-1])), key=lambda kk: dp_prev[kk])
    snaps: List[Optional[Point]] = [None] * n
    snaps[-1] = all_cands[-1][best_k][1]
    k = best_k
    for t in range(n - 2, -1, -1):
        k = all_bp[t][k]
        snaps[t] = all_cands[t][k][1]
    return snaps


def snap_features_to_roads_geojson(
    features: List[Dict[str, Any]],
    roads_file_path: str,
    tolerance_m: float = 50.0,
) -> List[Dict[str, Any]]:
    if not features:
        return features

    path = _resolve_roads_path(roads_file_path)
    if not os.path.isfile(path):
        logger.error("Файл графа не найден: %s", path)
        return features

    try:
        tree, lines_m, neighbors, edge_props = _load_graph(path)
    except Exception as e:
        logger.exception("Не удалось загрузить граф дорог: %s", e)
        return features

    tol = max(float(tolerance_m), 1.0)

    by_veh: Dict[str, List[int]] = defaultdict(list)
    for i, feat in enumerate(features):
        props = feat.get("properties") or {}
        vid = _vehicle_key(props)
        by_veh[vid].append(i)

    snapped_n = 0
    tried = 0

    for vid, idxs in by_veh.items():
        idxs.sort(
            key=lambda ii: parse_time(str(features[ii].get("properties", {}).get("time", "")))
            or datetime.min
        )

        subtracks = _split_sorted_track(features, idxs) if vid == "unknown" else [idxs]

        for seg in subtracks:
            track_idx: List[int] = []
            gps_pts_m: List[Point] = []
            for ii in seg:
                geom = features[ii].get("geometry") or {}
                coords = geom.get("coordinates")
                if not coords or len(coords) < 2:
                    continue
                try:
                    lon, lat = float(coords[0]), float(coords[1])
                except (TypeError, ValueError):
                    continue
                track_idx.append(ii)
                gps_pts_m.append(_to_m(Point(lon, lat)))

            if not track_idx:
                continue

            tried += len(track_idx)
            snaps = _viterbi_track(gps_pts_m, tree, lines_m, neighbors, edge_props, tol)

            for ii, snap_m in zip(track_idx, snaps):
                if snap_m is None:
                    continue
                nlon, nlat = _to_wgs(snap_m)
                feat = features[ii]
                feat["geometry"]["coordinates"] = [nlon, nlat]
                props = feat.setdefault("properties", {})
                props["snapped"] = True
                props["snap_engine"] = "graph"
                snapped_n += 1

    logger.info(
        "Привязка к графу (Viterbi по треку): %d из %d точек, базовый допуск %.0f м",
        snapped_n,
        tried,
        tol,
    )
    return features
