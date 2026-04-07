# backend/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from dateutil import parser
from math import radians, cos, sin, asin, sqrt
from shapely.geometry import MultiPoint, mapping, Point
from collections import defaultdict
import uvicorn
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Transport Analysis API",
    description="API для анализа транспортных данных и выявления зон заторов",
    version="2.0.0"
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Helper Functions ----------
def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Calculate distance between two points using Haversine formula.
    Returns distance in meters.
    """
    lon1, lat1, lon2, lat2 = map(radians, [lon1, lat1, lon2, lat2])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    R = 6371000  # Earth radius in meters
    return R * c

def parse_time(s: str) -> Optional[datetime]:
    """
    Parse datetime string with flexible format support.
    Prefers day-first format.
    """
    if not s:
        return None
    try:
        return parser.parse(str(s), dayfirst=True)
    except Exception as e:
        logger.warning(f"Failed to parse time '{s}': {e}")
        return None

def calculate_statistics(speeds: List[float]) -> Dict[str, float]:
    """Calculate comprehensive speed statistics."""
    if not speeds:
        return {}
    
    sorted_speeds = sorted(speeds)
    n = len(sorted_speeds)
    
    return {
        "min": min(speeds),
        "max": max(speeds),
        "mean": sum(speeds) / n,
        "median": sorted_speeds[n // 2] if n % 2 == 1 else (sorted_speeds[n // 2 - 1] + sorted_speeds[n // 2]) / 2,
        "std": sqrt(sum((x - sum(speeds) / n) ** 2 for x in speeds) / n) if n > 1 else 0,
        "q25": sorted_speeds[n // 4] if n >= 4 else sorted_speeds[0],
        "q75": sorted_speeds[3 * n // 4] if n >= 4 else sorted_speeds[-1],
    }

# ---------- API Models ----------
class AnalyzeRequest(BaseModel):
    geojson: Dict[str, Any] = Field(..., description="GeoJSON data with features")
    start: Optional[str] = Field(None, description="Start datetime filter")
    end: Optional[str] = Field(None, description="End datetime filter")
    include_zero: bool = Field(True, description="Include zero speed points")
    speed_thresh: float = Field(8.0, ge=0, description="Speed threshold for congestion detection (km/h)")
    eps_m: float = Field(50.0, gt=0, description="Clustering radius in meters")
    min_pts: int = Field(4, ge=1, description="Minimum points per cluster")
    return_all_clusters: bool = Field(True, description="Return all congestion clusters, not just the largest")

    @validator("geojson")
    def validate_geojson(cls, v):
        if not isinstance(v, dict) or v.get("type") != "FeatureCollection":
            raise ValueError("GeoJSON must be a FeatureCollection")
        return v

# ---------- Clustering Algorithm (Optimized) ----------
def region_growing_clusters(
    points: List[List[float]], 
    eps_m: float, 
    min_pts: int
) -> List[List[int]]:
    """
    Optimized region-growing clustering algorithm (similar to DBSCAN).
    Uses spatial indexing optimization for better performance.
    
    Args:
        points: List of [lon, lat] coordinates
        eps_m: Maximum distance between points in a cluster (meters)
        min_pts: Minimum number of points required to form a cluster
    
    Returns:
        List of clusters, each cluster is a list of point indices
    """
    n = len(points)
    if n == 0:
        return []
    
    # For small datasets, use simple approach
    if n < 100:
        return _simple_clustering(points, eps_m, min_pts)
    
    # For larger datasets, use spatial grid optimization
    return _spatial_grid_clustering(points, eps_m, min_pts)

def _simple_clustering(
    points: List[List[float]], 
    eps_m: float, 
    min_pts: int
) -> List[List[int]]:
    """Simple clustering for small datasets."""
    n = len(points)
    visited = [False] * n
    clusters = []

    for i in range(n):
        if visited[i]:
            continue
        
        stack = [i]
        cluster = []
        
        while stack:
            cur = stack.pop()
            if visited[cur]:
                continue
            
            visited[cur] = True
            cluster.append(cur)
            lon1, lat1 = points[cur]
            
            for j in range(n):
                if visited[j]:
                    continue
                lon2, lat2 = points[j]
                d = haversine_m(lon1, lat1, lon2, lat2)
                if d <= eps_m:
                    stack.append(j)
        
        if len(cluster) >= min_pts:
            clusters.append(cluster)
    
    clusters.sort(key=len, reverse=True)
    return clusters

def _spatial_grid_clustering(
    points: List[List[float]], 
    eps_m: float, 
    min_pts: int
) -> List[List[int]]:
    """Spatial grid-based clustering for large datasets."""
    n = len(points)
    if n == 0:
        return []
    
    # Create spatial grid
    # Estimate grid cell size based on eps_m (rough approximation)
    # 1 degree latitude ≈ 111 km, so eps_m / 111000 gives approximate degrees
    cell_size = max(eps_m / 111000 * 2, 0.001)  # At least 0.001 degrees
    
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    
    # Assign points to grid cells
    for i, (lon, lat) in enumerate(points):
        cell_x = int(lon / cell_size)
        cell_y = int(lat / cell_size)
        grid[(cell_x, cell_y)].append(i)
    
    visited = [False] * n
    clusters = []
    
    for i in range(n):
        if visited[i]:
            continue
        
        stack = [i]
        cluster = []
        
        while stack:
            cur = stack.pop()
            if visited[cur]:
                continue
            
            visited[cur] = True
            cluster.append(cur)
            lon1, lat1 = points[cur]
            
            # Check only nearby grid cells
            cell_x = int(lon1 / cell_size)
            cell_y = int(lat1 / cell_size)
            
            # Check current cell and 8 neighboring cells
            for dx in [-1, 0, 1]:
                for dy in [-1, 0, 1]:
                    cell_key = (cell_x + dx, cell_y + dy)
                    if cell_key not in grid:
                        continue
                    
                    for j in grid[cell_key]:
                        if visited[j]:
                            continue
                        lon2, lat2 = points[j]
                        d = haversine_m(lon1, lat1, lon2, lat2)
                        if d <= eps_m:
                            stack.append(j)
        
        if len(cluster) >= min_pts:
            clusters.append(cluster)
    
    clusters.sort(key=len, reverse=True)
    return clusters

# ---------- Data Aggregation ----------
def aggregate_plot_data(
    times: List[str],
    speeds: List[float],
    interval_minutes: int = 15
) -> Tuple[List[str], List[float]]:
    """
    Aggregate plot data into time intervals.
    
    Args:
        times: List of ISO format datetime strings
        speeds: List of speeds corresponding to times
        interval_minutes: Aggregation interval in minutes
    
    Returns:
        Tuple of (aggregated_times, aggregated_speeds)
    """
    if not times or not speeds or len(times) != len(speeds):
        return times, speeds
    
    # Parse times
    parsed_times = []
    valid_indices = []
    for i, t in enumerate(times):
        try:
            dt = parse_time(t) if isinstance(t, str) else t
            if dt is not None:
                parsed_times.append(dt)
                valid_indices.append(i)
        except:
            continue
    
    if not parsed_times or len(parsed_times) != len(valid_indices):
        return times, speeds
    
    # Group by time intervals
    interval_delta = timedelta(minutes=interval_minutes)
    min_time = min(parsed_times)
    
    # Create buckets
    buckets: Dict[int, List[float]] = defaultdict(list)
    
    for idx, dt in enumerate(parsed_times):
        original_idx = valid_indices[idx]
        if original_idx >= len(speeds):
            continue
        # Calculate bucket index
        time_diff = dt - min_time
        bucket_idx = int(time_diff.total_seconds() / interval_delta.total_seconds())
        buckets[bucket_idx].append(speeds[original_idx])
    
    # Aggregate: use average speed for each interval
    aggregated_times = []
    aggregated_speeds = []
    
    for bucket_idx in sorted(buckets.keys()):
        bucket_speeds = buckets[bucket_idx]
        if bucket_speeds:
            # Calculate interval start time
            interval_start = min_time + timedelta(seconds=bucket_idx * interval_delta.total_seconds())
            aggregated_times.append(interval_start.isoformat())
            aggregated_speeds.append(sum(bucket_speeds) / len(bucket_speeds))
    
    return aggregated_times, aggregated_speeds

# ---------- API Endpoints ----------
@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "name": "Transport Analysis API",
        "version": "2.0.0",
        "endpoints": {
            "/analyze": "POST - Analyze transport data",
            "/health": "GET - Health check"
        }
    }

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}

@app.post("/analyze")
async def analyze(req: AnalyzeRequest):
    """
    Analyze transport data from GeoJSON.
    
    Processes GeoJSON features, filters by time range and speed,
    identifies congestion zones using clustering, and returns
    comprehensive analysis results.
    """
    try:
        gj = req.geojson
        
        # Validate GeoJSON structure
        if not isinstance(gj, dict):
            raise HTTPException(status_code=400, detail="GeoJSON must be a dictionary/object")
        
        if gj.get("type") != "FeatureCollection":
            raise HTTPException(
                status_code=400, 
                detail=f"Expected FeatureCollection, got: {gj.get('type', 'unknown')}"
            )
        
        features = gj.get("features", [])
        
        if not features:
            raise HTTPException(
                status_code=400, 
                detail="No features found in GeoJSON. The file may be empty or all features were filtered out."
            )
        
        if not isinstance(features, list):
            raise HTTPException(
                status_code=400,
                detail="Features must be an array"
            )
        
        start_dt = parse_time(req.start) if req.start else None
        end_dt = parse_time(req.end) if req.end else None
        
        # Validate time range
        if start_dt and end_dt and start_dt > end_dt:
            raise HTTPException(status_code=400, detail="Start time must be before end time")

        fmt_errors = 0
        filtered_points = []
        speeds = []
        dropped = 0
        slow_candidates = []   # Coordinates for clustering
        slow_indices = []      # Mapping to filtered_points indices

        logger.info(f"Processing {len(features)} features")

        # Process features
        for feat in features:
            geom = feat.get("geometry")
            props = feat.get("properties", {})
            
            # Validate geometry
            if not geom or geom.get("type") != "Point":
                continue
            
            coords = geom.get("coordinates")
            if not coords or len(coords) < 2:
                continue
            
            lon, lat = coords[0], coords[1]
            
            # Validate coordinates
            if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
                logger.warning(f"Invalid coordinates: lon={lon}, lat={lat}")
                continue

            # Parse time
            raw_time = props.get("time")
            if raw_time is None:
                continue
            
            dt = parse_time(str(raw_time))
            if dt is None:
                fmt_errors += 1
                continue
            
            # Apply time filter
            if start_dt and dt < start_dt:
                continue
            if end_dt and dt > end_dt:
                continue

            # Parse speed
            raw_speed = props.get("speed")
            try:
                speed = float(raw_speed)
                if speed < 0:
                    logger.warning(f"Negative speed detected: {speed}")
                    speed = 0
            except (ValueError, TypeError):
                continue

            # Apply zero speed filter
            if (not req.include_zero) and speed <= 1.0:
                dropped += 1
                continue

            # Accept point
            p = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    **props,
                    "speed": speed,
                    "time": dt.isoformat()
                }
            }
            
            idx = len(filtered_points)
            filtered_points.append(p)
            speeds.append(speed)

            # Collect slow points for clustering
            if speed <= req.speed_thresh:
                slow_candidates.append([lon, lat])
                slow_indices.append(idx)

        # Build result
        result = {
            "avg_speed": None,
            "dropped": dropped,
            "count": len(filtered_points),
            "filtered_geojson": {
                "type": "FeatureCollection",
                "features": filtered_points
            },
            "congestion": None,
            "congestion_zones": [],
            "plot": {
                "times": [],
                "speeds": []
            },
            "statistics": {},
            "warnings": []
        }

        # Add warnings
        if fmt_errors:
            result["warnings"].append(f"Не удалось распарсить {fmt_errors} значений времени")
        
        if len(filtered_points) == 0:
            result["warnings"].append("Нет точек, соответствующих критериям фильтрации")
            return result

        # Calculate statistics
        if speeds:
            result["avg_speed"] = sum(speeds) / len(speeds)
            result["statistics"] = calculate_statistics(speeds)

        # Build plot data with 15-minute aggregation
        raw_times = [f["properties"]["time"] for f in filtered_points]
        raw_speeds = [f["properties"]["speed"] for f in filtered_points]
        
        # Aggregate to 15-minute intervals for better performance and readability
        aggregated_times, aggregated_speeds = aggregate_plot_data(raw_times, raw_speeds, interval_minutes=15)
        
        result["plot"]["times"] = aggregated_times
        result["plot"]["speeds"] = aggregated_speeds
        result["plot"]["raw_times"] = raw_times  # Keep raw data for detailed view if needed
        result["plot"]["raw_speeds"] = raw_speeds

        # Clustering for congestion zones
        if slow_candidates:
            logger.info(f"Clustering {len(slow_candidates)} slow points")
            clusters = region_growing_clusters(
                slow_candidates, 
                req.eps_m, 
                req.min_pts
            )
            
            if clusters:
                logger.info(f"Found {len(clusters)} congestion clusters")
                
                # Process all clusters or just the largest
                clusters_to_process = clusters if req.return_all_clusters else [clusters[0]]
                
                for cluster_idx, cluster in enumerate(clusters_to_process):
                    # Create convex hull for cluster
                    cluster_coords = [slow_candidates[i] for i in cluster]
                    mp = MultiPoint(cluster_coords)
                    
                    if mp.is_empty:
                        continue
                    
                    hull = mp.convex_hull
                    
                    # Convert to GeoJSON
                    hull_geojson = mapping(hull)
                    hull_geojson["properties"] = {
                        "cluster_size": len(cluster),
                        "cluster_index": cluster_idx,
                        "avg_speed": sum(speeds[slow_indices[i]] for i in cluster) / len(cluster) if cluster else 0
                    }
                    
                    result["congestion_zones"].append(hull_geojson)
                
                # Backward compatibility: set largest cluster as congestion
                if result["congestion_zones"]:
                    result["congestion"] = result["congestion_zones"][0]

        logger.info(f"Analysis complete: {len(filtered_points)} points, {len(result['congestion_zones'])} congestion zones")
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected error in analyze: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
