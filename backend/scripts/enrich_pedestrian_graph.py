"""
Добавить в существующий pickle учёт рельефа (изолинии) без пересборки GeoJSON.

  cd backend
  python -m scripts.enrich_pedestrian_graph
  python -m scripts.enrich_pedestrian_graph --contours ../contours.geojson --slope-k 6
"""
from __future__ import annotations

import argparse
import os
import pickle
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from data_paths import default_contours_path  # noqa: E402
from elevation import apply_elevation_to_graph  # noqa: E402
from graph_store import default_pickle_path  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Обогащение графа высотами из изолиний")
    parser.add_argument("--pickle", "-p", default=default_pickle_path())
    parser.add_argument("--contours", "-c", default=None)
    parser.add_argument("--slope-k", type=float, default=6.0)
    args = parser.parse_args()

    if not os.path.isfile(args.pickle):
        print(f"Pickle не найден: {args.pickle}")
        sys.exit(1)

    contours = args.contours or default_contours_path()
    if not contours:
        print("Файл изолиний не найден (contours.geojson в корне или data/).")
        sys.exit(1)

    with open(args.pickle, "rb") as f:
        graph = pickle.load(f)

    apply_elevation_to_graph(graph, contours_path=contours, slope_k=args.slope_k)

    with open(args.pickle, "wb") as f:
        pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)

    m = graph["meta"]
    print("Готово:", args.pickle)
    print(f"  has_elevation: {m.get('has_elevation')}")
    print(f"  источник: {m.get('elevation_source')}")
    print(f"  узлов без высоты: {m.get('elevation_missing_nodes')}")


if __name__ == "__main__":
    main()
