import json
import requests
import urllib.parse
from datetime import datetime
from collections import defaultdict

# Публичный сервер OSRM (Ограничение: max 100 координат на запрос)
OSRM_MATCH_URL = "http://router.project-osrm.org/match/v1/driving/{coords}"

def parse_time(time_str):
    try:
        return datetime.fromisoformat(str(time_str).replace('Z', ''))
    except:
        return datetime.min

def snap_geojson_to_roads(input_file, output_file, max_chunk_size=90):
    print(f"Читаем файл {input_file}...")
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if data.get("type") != "FeatureCollection":
        print("Ошибка: ожидается FeatureCollection")
        return

    features = data["features"]
    
    # Группируем точки по транспортным средствам (по госномеру)
    # Чтобы правильно привязать точки к дороге, нужно передавать их в хронологическом порядке
    vehicles = defaultdict(list)
    for feat in features:
        props = feat.get("properties", {})
        # Ищем идентификатор транспортного средства (госномер)
        veh_id = props.get("gos_num") or props.get("plate") or props.get("bnum") or "unknown"
        vehicles[veh_id].append(feat)

    print(f"Найдено {len(vehicles)} транспортных средств.")

    snapped_features = []
    
    for veh_id, veh_features in vehicles.items():
        # Сортируем точки по времени
        veh_features.sort(key=lambda x: parse_time(x["properties"].get("time", "")))
        
        # Разбиваем на чанки (OSRM поддерживает максимум 100 точек)
        for i in range(0, len(veh_features), max_chunk_size):
            chunk = veh_features[i:i + max_chunk_size]
            if len(chunk) < 2:
                snapped_features.extend(chunk)
                continue
                
            coords_str = ";".join([f"{f['geometry']['coordinates'][0]},{f['geometry']['coordinates'][1]}" for f in chunk])
            timestamps = ";".join([str(int(parse_time(f["properties"].get("time", "")).timestamp())) for f in chunk])
            radiuses = ";".join(["50" for _ in chunk]) # Радиус поиска дороги (50 метров)
            
            # Формируем запрос
            url = f"{OSRM_MATCH_URL.format(coords=coords_str)}?overview=false&radiuses={radiuses}&timestamps={timestamps}&gaps=ignore"
            
            try:
                response = requests.get(url)
                res_json = response.json()
                
                if res_json.get("code") == "Ok":
                    matchings = res_json.get("matchings", [])
                    tracepoints = res_json.get("tracepoints", [])
                    
                    # Обновляем координаты точек
                    for idx, trace in enumerate(tracepoints):
                        if trace is not None and "location" in trace:
                            # OSRM возвращает [lon, lat]
                            new_coords = trace["location"]
                            chunk[idx]["geometry"]["coordinates"] = new_coords
                            # Можно добавить флаг, что точка привязана
                            chunk[idx]["properties"]["snapped"] = True
                else:
                    print(f"Предупреждение OSRM для ТС {veh_id}: {res_json.get('message', 'Неизвестная ошибка')}")
            except Exception as e:
                print(f"Ошибка HTTP запроса: {e}")
            
            snapped_features.extend(chunk)

    data["features"] = snapped_features
    print(f"Сохраняем обработанные данные в {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
        
    print("Готово!")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Использование: python snap_to_road.py <input.geojson> <output.geojson>")
    else:
        snap_geojson_to_roads(sys.argv[1], sys.argv[2])
