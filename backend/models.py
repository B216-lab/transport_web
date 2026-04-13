# backend/models.py
from pydantic import BaseModel, Field, validator
from typing import Any, Dict, List, Optional

class AnalyzeRequest(BaseModel):
    geojson: Dict[str, Any] = Field(..., description="GeoJSON data with features")
    start: Optional[str] = Field(None, description="Start datetime filter")
    end: Optional[str] = Field(None, description="End datetime filter")
    include_zero: bool = Field(True, description="Include zero speed points")
    speed_thresh: float = Field(8.0, ge=0, description="Speed threshold for congestion detection (km/h)")
    eps_m: float = Field(50.0, gt=0, description="Clustering radius in meters")
    min_pts: int = Field(4, ge=1, description="Minimum points per cluster")
    return_all_clusters: bool = Field(True, description="Return all congestion clusters, not just the largest")
    routes: Optional[List[str]] = Field(None, description="List of route numbers to filter by")
    map_matching: bool = Field(False, description="Включить привязку точек к графу дорожной сети")
    snap_tolerance_m: float = Field(
        50.0,
        gt=0,
        le=500,
        description="Допуск привязки к дороге, метры",
    )
    analysis_geometry: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional GeoJSON geometry (Polygon or MultiPolygon, WGS84). Only points inside are analyzed.",
    )
    bidirectional_analysis: bool = Field(
        False,
        description="Раздельная статистика по двум направлениям движения внутри полигона (нужен analysis_geometry).",
    )

    @validator("geojson")
    def validate_geojson(cls, v):
        if not isinstance(v, dict) or v.get("type") != "FeatureCollection":
            raise ValueError("GeoJSON must be a FeatureCollection")
        return v

    @validator("analysis_geometry")
    def validate_analysis_geometry(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict) or v.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("analysis_geometry must be a GeoJSON Polygon or MultiPolygon object")
        return v
