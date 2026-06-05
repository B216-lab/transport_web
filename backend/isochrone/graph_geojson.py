"""
GeoJSON-выборка рёбер пешего графа по bbox (для отображения на карте).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def pedestrian_graph_geojson_in_bbox(
    graph: Dict[str, Any],
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    limit: int = 6000,
) -> Dict[str, Any]:
    """LineString по рёбрам, попадающим в видимую область."""
    if min_lon > max_lon:
        min_lon, max_lon = max_lon, min_lon
    if min_lat > max_lat:
        min_lat, max_lat = max_lat, min_lat

    node_coords: Dict[int, Tuple[float, float]] = graph.get("node_coords") or {}
    features: List[Dict[str, Any]] = []
    seen: set = set()

    def in_bbox(lon: float, lat: float) -> bool:
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    def add_edge(u: int, v: int) -> None:
        if len(features) >= limit:
            return
        key = (min(u, v), max(u, v))
        if key in seen:
            return
        if u not in node_coords or v not in node_coords:
            return
        lon1, lat1 = node_coords[u]
        lon2, lat2 = node_coords[v]
        if not (in_bbox(lon1, lat1) or in_bbox(lon2, lat2)):
            return
        seen.add(key)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[lon1, lat1], [lon2, lat2]],
                },
                "properties": {"role": "ped_graph"},
            }
        )

    for e in graph.get("edges") or []:
        add_edge(int(e["from"]), int(e["to"]))
        if len(features) >= limit:
            break

    if len(features) < limit:
        adj = graph.get("adj") or {}
        for u_str, nbrs in adj.items():
            u = int(u_str)
            for v, _w in nbrs:
                add_edge(u, int(v))
                if len(features) >= limit:
                    break
            if len(features) >= limit:
                break

    return {
        "type": "FeatureCollection",
        "features": features,
        "meta": {
            "count": len(features),
            "limit": limit,
            "truncated": len(features) >= limit,
            "bbox": [min_lon, min_lat, max_lon, max_lat],
        },
    }
