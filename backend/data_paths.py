"""Поиск опциональных GIS-файлов в корне проекта и в data/."""
from __future__ import annotations

import os
from typing import List, Optional

from graph_store import project_root


def resolve_data_file(candidates: List[str]) -> Optional[str]:
    root = project_root()
    for name in candidates:
        for base in (root, os.path.join(root, "data")):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return path
    return None


def default_contours_path() -> Optional[str]:
    return resolve_data_file(
        [
            "contours.geojson",
            "elevation_contours.geojson",
            "isolines.geojson",
            "izolinii.geojson",
            "изолинии.geojson",
        ]
    )


def default_buildings_path() -> Optional[str]:
    return resolve_data_file(
        [
            "buildings.geojson",
            "buildings_pop.geojson",
            "zdaniya.geojson",
            "здания.geojson",
        ]
    )
