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
    render_points_limit: int = Field(
        5000,
        ge=1000,
        le=200000,
        description="Maximum number of points returned in filtered_geojson for map rendering (stats still use all points).",
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


class ParserStartRequest(BaseModel):
    use_db: bool = Field(False, description="Enable writing parser output into PostgreSQL/PostGIS")
    cookie: Optional[str] = Field(
        None,
        description="Optional full Cookie header with PHPSESSID and related values",
    )


class AnalyzeDbRequest(BaseModel):
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
    max_points: int = Field(
        100000,
        ge=1000,
        le=500000,
        description="Max points to fetch from DB for one analysis request",
    )
    render_points_limit: int = Field(
        5000,
        ge=1000,
        le=200000,
        description="Maximum number of points returned in filtered_geojson for map rendering (stats still use all points).",
    )

    @validator("analysis_geometry")
    def validate_analysis_geometry(cls, v):
        if v is None:
            return v
        if not isinstance(v, dict) or v.get("type") not in ("Polygon", "MultiPolygon"):
            raise ValueError("analysis_geometry must be a GeoJSON Polygon or MultiPolygon object")
        return v


class PedestrianIsochroneRequest(BaseModel):
    """Запрос пеших изохрон. origin — [longitude, latitude] WGS84."""

    origin: List[float] = Field(..., min_length=2, max_length=2, description="[lon, lat]")
    interval_step_min: float = Field(5.0, gt=0, le=120, description="Шаг интервала, минуты")
    interval_count: int = Field(3, ge=1, le=10, description="Число зон (5, 10, 15 при шаге 5)")
    max_snap_m: float = Field(80.0, gt=0, le=500, description="Допуск привязки точки к графу, м")
    intervals_min: Optional[List[float]] = Field(
        None,
        description="Явный список порогов в минутах; если задан — interval_step/count игнорируются",
    )
    use_elevation: bool = Field(
        True,
        description="Учитывать рельеф (adj_elevation в графе). Если нет — плоский граф.",
    )
    include_building_stats: bool = Field(
        True,
        description="Считать население и число зданий в зонах (buildings.geojson).",
    )

    @validator("origin")
    def validate_origin(cls, v):
        lon, lat = float(v[0]), float(v[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("origin must be valid WGS84 coordinates [lon, lat]")
        return [lon, lat]


class TransitIsochroneRequest(PedestrianIsochroneRequest):
    """Изохроны с ОТ (ожидание + пересадки)."""

    max_transfers: int = Field(1, ge=0, le=3, description="Макс. число пересадок")
    max_walk_to_stop_m: float = Field(
        600.0,
        gt=50,
        le=2000,
        description="Макс. расстояние пешком до ОП для посадки, м",
    )
    pt_speed_kmh: Optional[float] = Field(
        None,
        gt=0,
        le=120,
        description="Скорость ОТ (км/ч) по умолчанию, если нет данных по маршруту",
    )
    route_speeds: Optional[Dict[str, float]] = Field(
        None,
        description="Скорость по маршрутам route_num -> km/h из анализа",
    )
    route_headways: Optional[Dict[str, float]] = Field(
        None,
        description="Интервал движения по маршрутам route_num -> мин из анализа",
    )
    segment_speeds: Optional[Dict[str, float]] = Field(
        None,
        description="Скорость по сегментам route|from_stop|to_stop -> km/h из анализа",
    )
