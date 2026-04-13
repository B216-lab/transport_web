"""
Привязка GPS-точек к линейному слою дорог через PyQGIS:
native:reprojectlayer (WGS84 → метры) + native:snapgeometries + обратно в WGS84.

Запуск бэкенда: интерпретатор Python из OSGeo4W (где есть qgis), либо задайте PYTHONPATH
к каталогам QGIS; переменная QGIS_PREFIX_PATH — путь к apps/qgis (например C:/OSGeo4W/apps/qgis).

Файл дорог: GeoJSON / Shapefile с линиями (LineString, MultiLineString).
"""
from __future__ import annotations

import atexit
import copy
import logging
import os
import threading
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_qgis_app: Any = None
_qgis_ready = False


def shutdown_qgis() -> None:
    global _qgis_app, _qgis_ready
    with _lock:
        if _qgis_app is not None:
            try:
                _qgis_app.exitQgis()
            except Exception as e:
                logger.debug("exitQgis: %s", e)
            _qgis_app = None
            _qgis_ready = False


atexit.register(shutdown_qgis)


def _ensure_qgis() -> bool:
    global _qgis_app, _qgis_ready
    with _lock:
        if _qgis_ready:
            return True
        try:
            from qgis.core import QgsApplication
            from qgis.analysis import QgsNativeAlgorithms
        except ImportError as e:
            logger.warning("PyQGIS не найден (%s). Используйте Python из OSGeo4W / QGIS.", e)
            return False

        prefix = (os.environ.get("QGIS_PREFIX_PATH") or "").strip()
        if not prefix:
            for candidate in (
                r"C:\OSGeo4W\apps\qgis",
                r"C:\Program Files\QGIS 3.34.3\apps\qgis",
                r"C:\Program Files\QGIS 3.36.0\apps\qgis",
            ):
                if os.path.isdir(candidate):
                    prefix = candidate
                    break
        if not prefix:
            logger.warning("Не задан QGIS_PREFIX_PATH и не найден стандартный каталог QGIS.")
            return False

        prefix = os.path.normpath(prefix)
        QgsApplication.setPrefixPath(prefix, True)
        _qgis_app = QgsApplication([], False)
        _qgis_app.initQgis()
        _qgis_app.processingRegistry().addProvider(QgsNativeAlgorithms())

        try:
            import processing  # noqa: F401
        except ImportError:
            logger.warning(
                "Модуль processing не импортируется. Добавьте plugins в PYTHONPATH, "
                "например: .../apps/qgis/python/plugins"
            )
            _qgis_app.exitQgis()
            _qgis_app = None
            return False

        _qgis_ready = True
        logger.info("QGIS инициализирован, prefix=%s", prefix)
        return True


def _resolve_roads_path(roads_file_path: str) -> str:
    p = os.path.expanduser(roads_file_path.strip())
    if os.path.isfile(p):
        return os.path.abspath(p)
    here = os.path.dirname(os.path.abspath(__file__))
    alt = os.path.abspath(os.path.join(here, "..", roads_file_path))
    if os.path.isfile(alt):
        return alt
    return os.path.abspath(p)


def snap_features_pyqgis(
    features: List[Dict[str, Any]],
    roads_file_path: str,
    tolerance_m: float = 50.0,
) -> List[Dict[str, Any]]:
    """
    Для каждой точки — ближайшая точка на линиях слоя дорог в пределах tolerance_m (метры).
    """
    if not features:
        return features

    roads_file_path = _resolve_roads_path(roads_file_path)
    if not os.path.isfile(roads_file_path):
        logger.error("Файл слоя дорог не найден: %s", roads_file_path)
        return features

    if not _ensure_qgis():
        return features

    import processing
    from qgis.core import (
        QgsFeature,
        QgsField,
        QgsGeometry,
        QgsPointXY,
        QgsVectorLayer,
        QgsCoordinateReferenceSystem,
    )
    from qgis.PyQt.QtCore import QVariant

    out_features = copy.deepcopy(features)
    target_crs = QgsCoordinateReferenceSystem("EPSG:3857")
    wgs = QgsCoordinateReferenceSystem("EPSG:4326")

    roads_wgs = QgsVectorLayer(roads_file_path, "roads_src", "ogr")
    if not roads_wgs.isValid():
        logger.error("Не удалось открыть слой дорог: %s", roads_file_path)
        return features

    try:
        roads_metric = processing.run(
            "native:reprojectlayer",
            {
                "INPUT": roads_wgs,
                "TARGET_CRS": target_crs,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    except Exception as e:
        logger.error("reprojectlayer (дороги): %s", e)
        return features

    layer_def = "Point?crs=EPSG:4326&index=yes"
    points_layer = QgsVectorLayer(layer_def, "bus_points", "memory")
    pr = points_layer.dataProvider()
    pr.addAttributes([QgsField("original_idx", QVariant.Int)])
    points_layer.updateFields()

    qgis_feats: List[QgsFeature] = []
    for i, feat in enumerate(features):
        geom = feat.get("geometry") or {}
        coords = geom.get("coordinates")
        if not coords or len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            continue
        qf = QgsFeature(points_layer.fields())
        qf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        qf.setAttributes([i])
        qgis_feats.append(qf)

    if not qgis_feats:
        return features

    if not pr.addFeatures(qgis_feats):
        logger.error("Не удалось добавить точки во временный слой.")
        return features
    points_layer.updateExtents()

    try:
        pts_metric = processing.run(
            "native:reprojectlayer",
            {
                "INPUT": points_layer,
                "TARGET_CRS": target_crs,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    except Exception as e:
        logger.error("reprojectlayer (точки): %s", e)
        return features

    # BEHAVIOR: 1 — prefer closest point to segment (см. документацию QGIS Processing)
    try:
        snapped_metric = processing.run(
            "native:snapgeometries",
            {
                "INPUT": pts_metric,
                "REFERENCE_LAYER": roads_metric,
                "TOLERANCE": float(tolerance_m),
                "BEHAVIOR": 1,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    except Exception as e:
        logger.error("native:snapgeometries: %s", e)
        return features

    try:
        snapped_wgs = processing.run(
            "native:reprojectlayer",
            {
                "INPUT": snapped_metric,
                "TARGET_CRS": wgs,
                "OUTPUT": "memory:",
            },
        )["OUTPUT"]
    except Exception as e:
        logger.error("reprojectlayer (обратно WGS84): %s", e)
        return features

    idx_field = snapped_wgs.fields().indexFromName("original_idx")
    for qf in snapped_wgs.getFeatures():
        idx = qf.attribute(idx_field) if idx_field >= 0 else None
        if idx is None or idx < 0 or idx >= len(out_features):
            continue
        g = qf.geometry()
        if g is None or g.isEmpty():
            continue
        pt = g.asPoint()
        out_features[idx]["geometry"]["coordinates"] = [pt.x(), pt.y()]
        props = out_features[idx].setdefault("properties", {})
        props["snapped"] = True
        props["snap_engine"] = "qgis"

    return out_features


def qgis_available() -> bool:
    """Проверка без полной инициализации (только импорт модулей)."""
    try:
        from qgis.core import QgsApplication  # noqa: F401
        return True
    except ImportError:
        return False
