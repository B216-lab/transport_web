"""
Выборка телеметрии из PostgreSQL для построения порядка остановок на маршрутах.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

from zoneinfo import ZoneInfo


def get_db_dsn() -> str:
    if psycopg is None:
        raise RuntimeError(
            "Установите psycopg: pip install psycopg[binary]"
        )
    dsn = os.getenv("IRKBUS_DB_DSN")
    if not dsn:
        raise RuntimeError("IRKBUS_DB_DSN не задан в backend/.env")
    return dsn


def get_telemetry_time_bounds(dsn: Optional[str] = None) -> tuple[Optional[datetime], Optional[datetime]]:
    dsn = dsn or get_db_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MIN(event_time), MAX(event_time)
                FROM transport.telemetry_snapshot
                WHERE event_time IS NOT NULL;
                """
            )
            row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def list_route_numbers(dsn: Optional[str] = None) -> List[str]:
    dsn = dsn or get_db_dsn()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT route_num
                FROM transport.telemetry_snapshot
                WHERE route_num IS NOT NULL AND route_num <> ''
                ORDER BY route_num;
                """
            )
            return [str(r[0]) for r in cur.fetchall()]


def _since_from_days(days: Optional[int], dsn: str) -> Optional[datetime]:
    if days is None or days <= 0:
        return None
    _min_t, max_t = get_telemetry_time_bounds(dsn)
    if max_t is None:
        return None
    if getattr(max_t, "tzinfo", None) is None:
        max_t = max_t.replace(tzinfo=timezone.utc)
    return max_t - timedelta(days=days)


def fetch_route_features(
    route_num: str,
    max_points: int = 80000,
    days: Optional[int] = 14,
    dsn: Optional[str] = None,
    since: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Точки одного маршрута как GeoJSON features (для infer route_stops)."""
    dsn = dsn or get_db_dsn()
    where = ["route_num = %s", "geom IS NOT NULL", "event_time IS NOT NULL"]
    params: List[Any] = [route_num]

    if since is None and days is not None and days > 0:
        since = _since_from_days(days, dsn)
    if since is not None:
        where.append("event_time >= %s")
        params.append(since)

    params.append(max_points)
    sql = f"""
        SELECT
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat,
            vehicle_id,
            gos_num,
            event_time
        FROM transport.telemetry_snapshot
        WHERE {' AND '.join(where)}
        ORDER BY vehicle_id, event_time ASC
        LIMIT %s
    """

    features: List[Dict[str, Any]] = []
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for lon, lat, vehicle_id, gos_num, event_time in cur.fetchall():
                if lon is None or lat is None:
                    continue
                if hasattr(event_time, "isoformat"):
                    time_text = event_time.astimezone(timezone.utc).replace(tzinfo=None).isoformat(
                        sep=" "
                    )
                else:
                    time_text = str(event_time)
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lon), float(lat)],
                        },
                        "properties": {
                            "vehicle_id": vehicle_id,
                            "gos_num": gos_num,
                            "time": time_text,
                            "route_num": route_num,
                        },
                    }
                )
    return features


def fetch_all_route_features_batched(
    route_nums: Optional[List[str]] = None,
    max_points_per_route: int = 80000,
    days: Optional[int] = 14,
    dsn: Optional[str] = None,
) -> List[Dict[str, Any]]:
    dsn = dsn or get_db_dsn()
    routes = route_nums or list_route_numbers(dsn)
    out: List[Dict[str, Any]] = []
    for r in routes:
        out.extend(
            fetch_route_features(
                r, max_points=max_points_per_route, days=days, dsn=dsn
            )
        )
    return out
