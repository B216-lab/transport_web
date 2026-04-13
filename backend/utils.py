# backend/utils.py
import logging
from math import radians, cos, sin, asin, sqrt, atan2, degrees
from datetime import datetime
from dateutil import parser
from typing import Any, Dict, Optional

from shapely.geometry import Point, shape

logger = logging.getLogger(__name__)

def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """
    Начальный азимут от точки 1 к точке 2, градусы [0, 360), по часовой от севера.
    """
    φ1, φ2 = radians(lat1), radians(lat2)
    dλ = radians(lon2 - lon1)
    x = sin(dλ) * cos(φ2)
    y = cos(φ1) * sin(φ2) - sin(φ1) * cos(φ2) * cos(dλ)
    θ = atan2(x, y)
    return (degrees(θ) + 360) % 360


def circular_mean_bearing_deg(bearings: list) -> float:
    """Круговое среднее набора азимутов, градусы [0, 360)."""
    if not bearings:
        return 0.0
    xr = sum(cos(radians(b)) for b in bearings)
    yr = sum(sin(radians(b)) for b in bearings)
    return degrees(atan2(yr, xr)) % 360


def direction_bin_from_ref(bearing: float, ref: float) -> int:
    """
    Деление пополам относительно ref: 0 — тот же полукруг, что и ref; 1 — противоположный.
    """
    d = radians(bearing - ref)
    return 0 if cos(d) >= 0 else 1


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

def point_in_analysis_geometry(lon: float, lat: float, geom: Optional[Dict[str, Any]]) -> bool:
    """
    True if (lon, lat) lies inside or on the boundary of the given GeoJSON geometry.
    Supports Polygon and MultiPolygon in WGS84 (same as incoming points).
    """
    if not geom:
        return True
    gtype = geom.get("type")
    if gtype not in ("Polygon", "MultiPolygon"):
        return False
    try:
        g = shape(geom)
        if not g.is_valid:
            g = g.buffer(0)
        return bool(g.covers(Point(lon, lat)))
    except Exception:
        return False


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
