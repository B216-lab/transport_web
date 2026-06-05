"""Загрузка сети ОТ (остановки, рейсы, пересадки пешком)."""
from __future__ import annotations

import os
import pickle
from typing import Any, Dict, Optional, Tuple

from graph_store import project_root

_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def default_pt_pickle_path() -> str:
    return os.path.join(project_root(), "data", "pt_network.pkl")


def default_stops_geojson_path() -> str:
    root = project_root()
    for name in ("proj_stop.geojson", os.path.join("data", "proj_stop.geojson")):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            return p
    return os.path.join(root, "proj_stop.geojson")


def load_pt_network(path: Optional[str] = None) -> Dict[str, Any]:
    pkl = path or default_pt_pickle_path()
    if not os.path.isfile(pkl):
        raise FileNotFoundError(
            f"Сеть ОТ не найдена: {pkl}. "
            "Выполните: cd backend && python -m scripts.build_pt_network"
        )
    mtime = os.path.getmtime(pkl)
    cached = _CACHE.get(pkl)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(pkl, "rb") as f:
        net = pickle.load(f)
    _CACHE[pkl] = (mtime, net)
    return net
