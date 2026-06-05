# backend/main.py
import os
import sys
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional
import uvicorn
import logging
from shapely.geometry import MultiPoint, mapping
from zoneinfo import ZoneInfo

from models import (
    AnalyzeRequest,
    AnalyzeDbRequest,
    ParserStartRequest,
    PedestrianIsochroneRequest,
    TransitIsochroneRequest,
)
from isochrone.pedestrian import compute_pedestrian_isochrones
from isochrone.transit import compute_transit_isochrones
from isochrone.graph_geojson import pedestrian_graph_geojson_in_bbox
from isochrone.pt_analysis import (
    compute_route_telemetry_stats,
    compute_segment_telemetry_stats,
)
from pt_network_store import default_pt_pickle_path
from utils import parse_time, build_analysis_geometry_checker
from services import (
    calculate_statistics,
    region_growing_clusters,
    aggregate_plot_data,
    snap_features_to_road_graph,
    default_roads_graph_path,
    compute_flow_directions,
)
from parser_control import parser_manager
from graph_store import default_geojson_path, default_pickle_path, graph_summary, load_pedestrian_graph
from data_paths import default_buildings_path, default_contours_path
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
if load_dotenv is not None:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=False)


def _congestion_index_from_avg(avg_speed: float) -> int:
    if avg_speed > 0:
        return min(10, max(1, round(11 - (avg_speed / 40) * 10)))
    return 10


def _build_zones_for_direction(
    slow_candidates: List[List[float]],
    slow_indices: List[int],
    speeds: List[float],
    dirs: List[Optional[int]],
    dir_id: int,
    eps_m: float,
    min_pts: int,
    return_all: bool,
) -> List[Dict[str, Any]]:
    filtered_coords: List[List[float]] = []
    filtered_speeds: List[float] = []
    for k, idx in enumerate(slow_indices):
        if dirs[idx] == dir_id:
            filtered_coords.append(slow_candidates[k])
            filtered_speeds.append(speeds[idx])
    if not filtered_coords:
        return []
    clusters = region_growing_clusters(filtered_coords, eps_m, min_pts)
    iterable = clusters if return_all else clusters[:1]
    zones: List[Dict[str, Any]] = []
    for cluster_idx, cluster in enumerate(iterable):
        cluster_coords = [filtered_coords[i] for i in cluster]
        mp = MultiPoint(cluster_coords)
        if mp.is_empty:
            continue
        hull = mp.convex_hull
        hull_gj = mapping(hull)
        avg_sp = sum(filtered_speeds[i] for i in cluster) / len(cluster)
        hull_gj["properties"] = {
            "cluster_size": len(cluster),
            "cluster_index": cluster_idx,
            "avg_speed": avg_sp,
            "direction_id": dir_id,
        }
        zones.append(hull_gj)
    return zones


def _direction_payload(
    dir_id: int,
    ref_bearing: float,
    speeds: List[float],
    dirs: List[Optional[int]],
    raw_times: List[str],
    req: AnalyzeRequest,
    slow_candidates: List[List[float]],
    slow_indices: List[int],
) -> Dict[str, Any]:
    indices = [i for i, d in enumerate(dirs) if d == dir_id]
    if not indices:
        label_angle = (ref_bearing + (180 if dir_id == 1 else 0)) % 360
        return {
            "id": dir_id,
            "label": f"~{label_angle:.0f}°",
            "avg_speed": None,
            "congestion_index": 1,
            "count": 0,
            "statistics": {},
            "plot": {"times": [], "speeds": [], "raw_times": [], "raw_speeds": []},
            "congestion_zones": [],
        }
    sp = [speeds[i] for i in indices]
    tms = [raw_times[i] for i in indices]
    avg = sum(sp) / len(sp)
    ci = _congestion_index_from_avg(avg)
    agg_times, agg_speeds = aggregate_plot_data(tms, sp, interval_minutes=15)
    stats = calculate_statistics(sp)
    zones = _build_zones_for_direction(
        slow_candidates,
        slow_indices,
        speeds,
        dirs,
        dir_id,
        req.eps_m,
        req.min_pts,
        req.return_all_clusters,
    )
    label_angle = (ref_bearing + (180 if dir_id == 1 else 0)) % 360
    return {
        "id": dir_id,
        "label": f"~{label_angle:.0f}°",
        "avg_speed": avg,
        "congestion_index": ci,
        "count": len(indices),
        "statistics": stats,
        "plot": {
            "times": agg_times,
            "speeds": agg_speeds,
            "raw_times": tms,
            "raw_speeds": sp,
        },
        "congestion_zones": zones,
    }


def _downsample_indices(total: int, limit: int) -> List[int]:
    if total <= limit:
        return list(range(total))
    if limit <= 1:
        return [0]
    step = (total - 1) / (limit - 1)
    return [min(total - 1, round(i * step)) for i in range(limit)]


app = FastAPI(
    title="Transport Analysis API",
    description="API для анализа транспортных данных и выявления зон заторов",
    version="2.1.0",
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    GZipMiddleware,
    minimum_size=1024,
    compresslevel=5,
)

@app.get("/")
async def root():
    return {
        "name": "Transport Analysis API",
        "version": "2.1.0",
        "status": "online"
    }

@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/parser/status")
async def parser_status():
    return parser_manager.status()


@app.post("/parser/start")
async def parser_start(req: ParserStartRequest):
    status = parser_manager.start(use_db=req.use_db, cookie=req.cookie)
    return status


@app.post("/parser/stop")
async def parser_stop():
    status = parser_manager.stop()
    return status


@app.get("/parser/logs")
async def parser_logs(lines: int = 200):
    try:
        return parser_manager.read_logs(lines=lines)
    except Exception as exc:
        logger.error("Error reading parser logs: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


def _build_geojson_from_db(req: AnalyzeDbRequest) -> Dict[str, Any]:
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed in backend interpreter: "
            f"{sys.executable}. Install with: \"{sys.executable}\" -m pip install psycopg[binary]"
        )

    dsn = os.getenv("IRKBUS_DB_DSN")
    if not dsn:
        raise RuntimeError("IRKBUS_DB_DSN is not configured.")

    start_dt = parse_time(req.start) if req.start else None
    end_dt = parse_time(req.end) if req.end else None
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(status_code=400, detail="Start time must be before end time")

    # Client time for DB mode is local Irkutsk time; convert to UTC for DB query.
    irkutsk_tz = ZoneInfo("Asia/Irkutsk")
    db_start = _to_utc_for_db(start_dt, irkutsk_tz) if start_dt else None
    db_end = _to_utc_for_db(end_dt, irkutsk_tz) if end_dt else None

    where = ["event_time IS NOT NULL", "geom IS NOT NULL"]
    params: List[Any] = []
    if db_start:
        where.append("event_time >= %s")
        params.append(db_start)
    if db_end:
        where.append("event_time <= %s")
        params.append(db_end)
    if req.routes:
        where.append("route_num = ANY(%s)")
        params.append(req.routes)

    sql = f"""
        SELECT
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat,
            vehicle_id,
            rid,
            route_num,
            route_type,
            speed_kmh,
            dir_deg,
            low_floor,
            wifi,
            gos_num,
            event_time
        FROM transport.telemetry_snapshot
        WHERE {' AND '.join(where)}
        ORDER BY event_time ASC
        LIMIT %s
    """
    params.append(req.max_points)

    features: List[Dict[str, Any]] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    for row in rows:
        lon, lat = float(row[0]), float(row[1])
        event_time = row[11]
        # Emit naive UTC text so existing /analyze (+8h) behaves like file mode.
        if hasattr(event_time, "astimezone"):
            naive_utc = event_time.astimezone(timezone.utc).replace(tzinfo=None)
            time_text = naive_utc.strftime("%Y-%m-%d %H:%M:%S")
        else:
            time_text = str(event_time)

        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "id": row[2],
                    "route_id": row[3],
                    "route_num": row[4],
                    "rtype": row[5],
                    "speed": row[6],
                    "dir": row[7],
                    "low_floor": "1" if row[8] else "0" if row[8] is not None else None,
                    "wifi": "1" if row[9] else "0" if row[9] is not None else None,
                    "gos_num": row[10],
                    "time": time_text,
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        gj = req.geojson
        features = gj.get("features", [])
        
        if not features:
            raise HTTPException(status_code=400, detail="No features found in GeoJSON")
        
        start_dt = parse_time(req.start) if req.start else None
        end_dt = parse_time(req.end) if req.end else None
        
        if start_dt and end_dt and start_dt > end_dt:
            raise HTTPException(status_code=400, detail="Start time must be before end time")

        if req.bidirectional_analysis and not req.analysis_geometry:
            raise HTTPException(
                status_code=400,
                detail="Двунаправленный анализ доступен только при выбранной области на карте (полигон).",
            )

        fmt_errors = 0
        filtered_points = []
        speeds = []
        dropped = 0
        slow_candidates = []
        slow_indices = []

        analysis_contains = build_analysis_geometry_checker(req.analysis_geometry)
        route_filter = set(req.routes) if req.routes else None

        # Process features
        for feat in features:
            geom = feat.get("geometry")
            props = feat.get("properties", {})
            
            if not geom or geom.get("type") != "Point":
                continue
            
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            
            try:
                lon, lat = float(coords[0]), float(coords[1])
            except (TypeError, ValueError):
                continue

            if not analysis_contains(lon, lat):
                continue

            # Parse time and apply +8 hours offset
            dt = parse_time(str(props.get("time")))
            if dt is None:
                fmt_errors += 1
                continue
            
            # Apply time offset (+8 hours)
            dt = dt + timedelta(hours=8)
            
            # Apply filters
            if (start_dt and dt < start_dt) or (end_dt and dt > end_dt):
                continue
            
            # Apply route filter
            if route_filter and str(props.get("route_num")) not in route_filter:
                continue

            try:
                speed = float(props.get("speed", 0))
                if speed < 0: speed = 0
            except:
                continue

            if (not req.include_zero) and speed <= 1.0:
                dropped += 1
                continue

            # Accept point
            idx = len(filtered_points)
            filtered_points.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {**props, "speed": speed, "time": dt.isoformat()}
            })
            speeds.append(speed)

            if speed <= req.speed_thresh:
                slow_candidates.append([lon, lat])
                slow_indices.append(idx)

        if req.map_matching and filtered_points:
            filtered_points = snap_features_to_road_graph(
                filtered_points,
                tolerance_m=req.snap_tolerance_m,
            )
            # Rebuild slow candidates with potentially new coordinates
            slow_candidates = []
            for i in slow_indices:
                feat = filtered_points[i]
                coords = feat.get("geometry", {}).get("coordinates")
                if coords:
                    slow_candidates.append(coords)

        dirs: Optional[List[Optional[int]]] = None
        point_bearings: Optional[List[Optional[float]]] = None
        ref_bearing = 0.0
        if req.bidirectional_analysis and filtered_points:
            dirs, point_bearings, ref_bearing = compute_flow_directions(
                filtered_points,
                min_segment_m=8.0,
            )
            for i, feat in enumerate(filtered_points):
                props = feat.setdefault("properties", {})
                if point_bearings is not None and point_bearings[i] is not None:
                    props["flow_bearing"] = round(point_bearings[i], 2)
                if dirs is not None and dirs[i] is not None:
                    props["flow_dir"] = dirs[i]

        # Calculate Congestion Index (1-10)
        # Assuming 40 km/h is free flow speed.
        avg_speed = sum(speeds) / len(speeds) if speeds else 0
        congestion_index = 1
        if avg_speed > 0:
            congestion_index = min(10, max(1, round(11 - (avg_speed / 40) * 10)))
        elif speeds:
            congestion_index = 10 # 0 avg speed means max congestion

        segment_stats = compute_segment_telemetry_stats(filtered_points)
        result = {
            "avg_speed": avg_speed if speeds else None,
            "congestion_index": congestion_index,
            "dropped": dropped,
            "count": len(filtered_points),
            "filtered_geojson": {"type": "FeatureCollection", "features": []},
            "congestion_zones": [],
            "plot": {"times": [], "speeds": []},
            "statistics": calculate_statistics(speeds) if speeds else {},
            "segment_stats": segment_stats,
            "route_stats": compute_route_telemetry_stats(
                filtered_points, segment_stats=segment_stats
            ),
            "analysis_period": {"start": req.start, "end": req.end},
            "warnings": [],
        }

        if fmt_errors:
            result["warnings"].append(f"Could not parse {fmt_errors} time values")

        if req.map_matching:
            graph_path = default_roads_graph_path()
            if not os.path.isfile(graph_path):
                result["warnings"].append(
                    f"Привязка к графу: файл highway_graph.geojson не найден в корне проекта ({graph_path})."
                )

        if not filtered_points:
            result["warnings"].append("No points matching criteria")
            return result

        # Plot data
        raw_times = [f["properties"]["time"] for f in filtered_points]
        agg_times, agg_speeds = aggregate_plot_data(raw_times, speeds, interval_minutes=15)
        result["plot"] = {
            "times": agg_times, 
            "speeds": agg_speeds,
            "raw_times": raw_times,
            "raw_speeds": speeds
        }

        render_indices = _downsample_indices(len(filtered_points), req.render_points_limit)
        result["filtered_geojson"]["features"] = [filtered_points[i] for i in render_indices]
        if len(filtered_points) > req.render_points_limit:
            result["warnings"].append(
                "Карта отображает выборку точек для ускорения интерфейса "
                f"({req.render_points_limit} из {len(filtered_points)}). "
                "Статистика и зоны заторов рассчитаны по всем точкам."
            )

        # Clustering
        if slow_candidates:
            clusters = region_growing_clusters(slow_candidates, req.eps_m, req.min_pts)
            for cluster_idx, cluster in enumerate(clusters if req.return_all_clusters else clusters[:1]):
                cluster_coords = [slow_candidates[i] for i in cluster]
                mp = MultiPoint(cluster_coords)
                if not mp.is_empty:
                    hull = mp.convex_hull
                    hull_gj = mapping(hull)
                    hull_gj["properties"] = {
                        "cluster_size": len(cluster),
                        "cluster_index": cluster_idx,
                        "avg_speed": sum(speeds[slow_indices[i]] for i in cluster) / len(cluster)
                    }
                    result["congestion_zones"].append(hull_gj)
            
            if result["congestion_zones"]:
                result["congestion"] = result["congestion_zones"][0]

        if req.bidirectional_analysis and filtered_points and dirs is not None:
            ucount = sum(1 for d in dirs if d is None)
            result["bidirectional"] = {
                "reference_bearing_deg": ref_bearing,
                "unclassified_count": ucount,
                "directions": [
                    _direction_payload(0, ref_bearing, speeds, dirs, raw_times, req, slow_candidates, slow_indices),
                    _direction_payload(1, ref_bearing, speeds, dirs, raw_times, req, slow_candidates, slow_indices),
                ],
            }
            if ucount == len(dirs):
                result["warnings"].append(
                    "Двунаправленный анализ: не удалось классифицировать точки по направлению "
                    "(нужны цепочки точек с перемещением между соседними по времени > 8 м).",
                )

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in analyze: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/db")
async def analyze_db(req: AnalyzeDbRequest):
    try:
        geojson = _build_geojson_from_db(req)
        bridge_req = AnalyzeRequest(
            geojson=geojson,
            # SQL query already applies time filtering in local (Irkutsk) semantics.
            # Avoid re-filtering in /analyze where +8h offset is applied for file mode.
            start=None,
            end=None,
            include_zero=req.include_zero,
            speed_thresh=req.speed_thresh,
            eps_m=req.eps_m,
            min_pts=req.min_pts,
            return_all_clusters=req.return_all_clusters,
            routes=req.routes,
            map_matching=req.map_matching,
            snap_tolerance_m=req.snap_tolerance_m,
            analysis_geometry=req.analysis_geometry,
            bidirectional_analysis=req.bidirectional_analysis,
            render_points_limit=req.render_points_limit,
        )
        result = await analyze(bridge_req)
        if len(geojson.get("features", [])) >= req.max_points:
            result.setdefault("warnings", []).append(
                f"Достигнут лимит выборки из БД: {req.max_points} точек. Уточните период/фильтры."
            )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in analyze_db: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _intervals_from_request(req: PedestrianIsochroneRequest) -> List[float]:
    if req.intervals_min:
        intervals = sorted({float(x) for x in req.intervals_min if float(x) > 0})
    else:
        intervals = [req.interval_step_min * (i + 1) for i in range(req.interval_count)]
    if not intervals:
        raise HTTPException(status_code=400, detail="Не заданы положительные интервалы времени")
    return intervals


@app.post("/isochrone/pedestrian")
async def isochrone_pedestrian(req: PedestrianIsochroneRequest):
    try:
        intervals = _intervals_from_request(req)
        return compute_pedestrian_isochrones(
            origin_lon=req.origin[0],
            origin_lat=req.origin[1],
            intervals_min=intervals,
            max_snap_m=req.max_snap_m,
            use_elevation=req.use_elevation,
            include_building_stats=req.include_building_stats,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("isochrone_pedestrian: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/isochrone/transit")
async def isochrone_transit(req: TransitIsochroneRequest):
    try:
        intervals = _intervals_from_request(req)
        return compute_transit_isochrones(
            origin_lon=req.origin[0],
            origin_lat=req.origin[1],
            intervals_min=intervals,
            max_snap_m=req.max_snap_m,
            use_elevation=req.use_elevation,
            include_building_stats=req.include_building_stats,
            max_transfers=req.max_transfers,
            max_walk_to_stop_m=req.max_walk_to_stop_m,
            pt_speed_kmh=req.pt_speed_kmh,
            route_speeds=req.route_speeds,
            segment_speeds=req.segment_speeds,
            route_headways=req.route_headways,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("isochrone_transit: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/isochrone/data-status")
async def isochrone_data_status():
    """Наличие опциональных слоёв этапа 3: изолинии и здания."""
    pkl = default_pickle_path()
    has_elevation_in_graph = False
    if os.path.isfile(pkl):
        try:
            g = load_pedestrian_graph(pkl)
            has_elevation_in_graph = bool((g.get("meta") or {}).get("has_elevation"))
        except Exception:
            pass
    contours = default_contours_path()
    buildings = default_buildings_path()
    pt_pkl = default_pt_pickle_path()
    pt_ready = os.path.isfile(pt_pkl)
    pt_meta = {}
    if pt_ready:
        try:
            from pt_network_store import load_pt_network

            pt_meta = load_pt_network(pt_pkl).get("meta") or {}
        except Exception:
            pt_ready = False
    return {
        "contours_path": contours,
        "contours_available": contours is not None,
        "buildings_path": buildings,
        "buildings_available": buildings is not None,
        "graph_has_elevation": has_elevation_in_graph,
        "enrich_hint": (
            "python -m scripts.enrich_pedestrian_graph"
            if contours and not has_elevation_in_graph
            else None
        ),
        "pt_network_ready": pt_ready,
        "pt_network_path": pt_pkl,
        "pt_stop_count": pt_meta.get("stop_count"),
        "pt_route_count": pt_meta.get("route_count"),
        "pt_build_hint": (
            None
            if pt_ready
            else "python -m scripts.build_pt_network --from-db"
        ),
    }


@app.get("/graph/pedestrian/status")
async def pedestrian_graph_status():
    """Проверка этапа 1: собран ли пеший граф из QGIS → pickle."""
    pkl = default_pickle_path()
    geojson = default_geojson_path()
    if not os.path.isfile(pkl):
        return {
            "ready": False,
            "pickle_path": pkl,
            "geojson_path": geojson,
            "geojson_exists": os.path.isfile(geojson),
            "hint": (
                "Экспортируйте пешую сеть в data/pedestrian_graph.geojson и выполните: "
                "cd backend && python -m scripts.build_pedestrian_graph"
            ),
        }
    try:
        summary = graph_summary(pkl)
        g = load_pedestrian_graph(pkl)
        summary["has_elevation"] = bool((g.get("meta") or {}).get("has_elevation"))
        return {"ready": True, "pickle_path": pkl, **summary}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/graph/pedestrian/geojson")
async def pedestrian_graph_geojson(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    limit: int = 6000,
):
    """
    Рёбра пешего графа в bbox карты (для подсказки, куда ставить точку).
    limit ≤ 15000.
    """
    limit = max(100, min(int(limit), 15000))
    pkl = default_pickle_path()
    if not os.path.isfile(pkl):
        raise HTTPException(
            status_code=503,
            detail="Пеший граф не собран. Выполните build_pedestrian_graph.",
        )
    try:
        g = load_pedestrian_graph(pkl)
        return pedestrian_graph_geojson_in_bbox(
            g, min_lon, min_lat, max_lon, max_lat, limit=limit
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("pedestrian_graph_geojson: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/db/meta")
async def analyze_db_meta():
    try:
        if psycopg is None:
            raise RuntimeError(
                "psycopg is not installed in backend interpreter: "
                f"{sys.executable}. Install with: \"{sys.executable}\" -m pip install psycopg[binary]"
            )

        dsn = os.getenv("IRKBUS_DB_DSN")
        if not dsn:
            raise RuntimeError("IRKBUS_DB_DSN is not configured.")

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        MIN(event_time) AS min_time,
                        MAX(event_time) AS max_time,
                        COUNT(*) AS points_count
                    FROM transport.telemetry_snapshot
                    WHERE event_time IS NOT NULL;
                    """
                )
                row = cur.fetchone()
                min_time = row[0] if row else None
                max_time = row[1] if row else None
                points_count = int(row[2] or 0) if row else 0

                cur.execute(
                    """
                    SELECT DISTINCT route_num
                    FROM transport.telemetry_snapshot
                    WHERE route_num IS NOT NULL AND route_num <> ''
                    ORDER BY route_num;
                    """
                )
                routes = [str(r[0]) for r in cur.fetchall()]

        irkutsk_tz = ZoneInfo("Asia/Irkutsk")
        min_local = _format_local_naive(min_time, irkutsk_tz)
        max_local = _format_local_naive(max_time, irkutsk_tz)

        return {
            # Send local-naive datetimes to avoid timezone shifts in browser auto-fill.
            "min_time": min_local,
            "max_time": max_local,
            "points_count": points_count,
            "routes": routes,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in analyze_db_meta: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _to_utc_for_db(dt: Any, local_tz: ZoneInfo):
    if getattr(dt, "tzinfo", None) is None:
        aware_local = dt.replace(tzinfo=local_tz)
    else:
        aware_local = dt.astimezone(local_tz)
    return aware_local.astimezone(timezone.utc)


def _format_local_naive(dt: Any, local_tz: ZoneInfo) -> Optional[str]:
    if dt is None:
        return None
    local_dt = dt.astimezone(local_tz)
    return local_dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
