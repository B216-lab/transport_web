# backend/main.py
import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import timedelta
from typing import Any, Dict, List, Optional
import uvicorn
import logging
from shapely.geometry import MultiPoint, mapping

from models import AnalyzeRequest
from utils import parse_time, point_in_analysis_geometry
from services import (
    calculate_statistics,
    region_growing_clusters,
    aggregate_plot_data,
    snap_features_to_road_graph,
    default_roads_graph_path,
    compute_flow_directions,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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

app = FastAPI(
    title="Transport Analysis API",
    description="API для анализа транспортных данных и выявления зон заторов",
    version="2.1.0"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

            if req.analysis_geometry and not point_in_analysis_geometry(lon, lat, req.analysis_geometry):
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
            if req.routes and str(props.get("route_num")) not in req.routes:
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

        result = {
            "avg_speed": avg_speed if speeds else None,
            "congestion_index": congestion_index,
            "dropped": dropped,
            "count": len(filtered_points),
            "filtered_geojson": {"type": "FeatureCollection", "features": filtered_points},
            "congestion_zones": [],
            "plot": {"times": [], "speeds": []},
            "statistics": calculate_statistics(speeds) if speeds else {},
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

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
