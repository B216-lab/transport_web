<script setup>
import { onMounted, onUnmounted, ref, watch, inject } from 'vue'
import L from 'leaflet'
import 'leaflet-draw'
import 'leaflet-draw/dist/leaflet.draw.css'
import 'leaflet.heat'
import { Layers } from 'lucide-vue-next'

const props = defineProps(['data', 'height'])
const mapContainer = ref(null)
const heatmapMode = ref(false)
const analysisAreaGeometry = inject('analysisAreaGeometry')

let map = null
let routeLayer = null
let congestionLayer = null
let heatLayer = null
let drawnItems = null

onMounted(() => {
  map = L.map(mapContainer.value).setView([52.3, 104.3], 11)
  
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map)

  drawnItems = new L.FeatureGroup()
  map.addLayer(drawnItems)

  const drawControl = new L.Control.Draw({
    position: 'topleft',
    draw: {
      polygon: {
        allowIntersection: false,
        showArea: false,
        shapeOptions: {
          color: '#2563eb',
          weight: 2,
          fillColor: '#3b82f6',
          fillOpacity: 0.15,
        },
      },
      rectangle: false,
      polyline: false,
      circle: false,
      marker: false,
      circlemarker: false,
    },
    edit: {
      featureGroup: drawnItems,
      remove: true,
    },
  })
  map.addControl(drawControl)

  const syncGeometryFromLayer = (layer) => {
    const gj = layer.toGeoJSON()
    if (gj?.geometry && analysisAreaGeometry) {
      analysisAreaGeometry.value = gj.geometry
    }
  }

  map.on(L.Draw.Event.CREATED, (e) => {
    drawnItems.clearLayers()
    drawnItems.addLayer(e.layer)
    syncGeometryFromLayer(e.layer)
  })

  map.on(L.Draw.Event.EDITED, (e) => {
    e.layers.eachLayer((layer) => syncGeometryFromLayer(layer))
  })

  map.on(L.Draw.Event.DELETED, () => {
    if (analysisAreaGeometry) analysisAreaGeometry.value = null
  })

  // Wait for parent layout to stabilize
  setTimeout(() => {
    map.invalidateSize()
  }, 800)
})

onUnmounted(() => {
  if (map) map.remove()
})

const getSpeedColor = (s) => {
  if (s < 10) return "#ef4444" // Red
  if (s < 20) return "#f97316" // Orange
  if (s < 40) return "#eab308" // Yellow
  return "#22c55e" // Green
}

/** dir — градусы курса от севера по часовой стрелке (как в навигации) */
const parseDirDegrees = (dir) => {
  const n = Number(dir)
  return Number.isFinite(n) ? ((n % 360) + 360) % 360 : null
}

/** Каплевидный маркер: остриё указывает направление движения */
const createBusDirectionIcon = (fillColor, dir) => {
  const deg = parseDirDegrees(dir)
  const rotation = deg != null ? deg : 0
  // viewBox 32×40: остриё сверху (север), центр вращения ~ (16, 20)
  const html = `<svg width="22" height="28" viewBox="0 0 32 40" style="display:block" aria-hidden="true">
    <g transform="rotate(${rotation} 16 20)">
      <path
        fill="${fillColor}"
        stroke="#ffffff"
        stroke-width="1.1"
        stroke-linejoin="round"
        d="M16 3 C22 9 27 17 27 25 C27 33 22 37 16 37 C10 37 5 33 5 25 C5 17 10 9 16 3 Z"
      />
    </g>
  </svg>`
  return L.divIcon({
    className: 'bus-dir-marker',
    html,
    iconSize: [22, 28],
    iconAnchor: [11, 14],
    popupAnchor: [0, -15],
  })
}

watch(() => props.data, (newData) => {
  if (!newData) {
    if (routeLayer) map.removeLayer(routeLayer)
    if (congestionLayer) map.removeLayer(congestionLayer)
    if (heatLayer) map.removeLayer(heatLayer)
    return
  }

  drawMap()
}, { deep: true })

watch(heatmapMode, () => {
  drawMap()
})

const drawMap = () => {
  if (!props.data?.filtered_geojson) return

  // Clear existing
  if (routeLayer) map.removeLayer(routeLayer)
  if (congestionLayer) map.removeLayer(congestionLayer)
  if (heatLayer) map.removeLayer(heatLayer)

  // 1. Add Points/Route or Heatmap
  if (heatmapMode.value) {
    const heatPoints = props.data.filtered_geojson.features.map(f => {
      const p = f.properties
      const c = f.geometry.coordinates
      const speed = p.speed || 0
      // Calculate delay intensity (0 to 1) for speeds < 30
      const intensity = Math.max(0, 30 - speed) / 30
      // Emphasize slower speeds exponentially
      return intensity > 0 ? [c[1], c[0], Math.pow(intensity, 1.5)] : null
    }).filter(p => p !== null)

    heatLayer = L.heatLayer(heatPoints, {
      radius: 25,
      blur: 20,
      maxZoom: 16,
      gradient: { 0.2: 'lime', 0.5: 'yellow', 0.8: 'orange', 1.0: 'red' }
    }).addTo(map)

    const bounds = L.latLngBounds(heatPoints.map(p => [p[0], p[1]]))
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [50, 50] })
    }
  } else {
    routeLayer = L.geoJSON(props.data.filtered_geojson, {
      pointToLayer: (feature, latlng) => {
        const color = getSpeedColor(feature.properties.speed)
        const dir = feature.properties?.dir ?? feature.properties?.flow_bearing
        if (parseDirDegrees(dir) != null) {
          return L.marker(latlng, {
            icon: createBusDirectionIcon(color, dir),
            interactive: true,
          })
        }
        return L.circleMarker(latlng, {
          radius: 4,
          fillColor: color,
          color: "#fff",
          weight: 1,
          opacity: 1,
          fillOpacity: 0.8
        })
      },
      onEachFeature: (feature, layer) => {
        const p = feature.properties
        
        // Map vehicle type
        const typeMap = {
          'А': 'Автобус',
          'Т': 'Трамвай',
          'Тр': 'Троллейбус',
          'М': 'Маршрутное такси'
        }
        const vehicleType = typeMap[p.rtype] || p.rtype || 'Транспорт'
        
        // Find potential license plate / gos number
        const gosNumber = p.gos_num || p.plate || p.bnum || p.veh_num || p.number || null

        layer.bindPopup(`
          <div class="p-2 space-y-2 min-w-[180px]">
            <div class="flex flex-col border-b pb-1 mb-1">
              <span class="text-[10px] text-gray-400 uppercase font-bold tracking-wider">${vehicleType}</span>
              <span class="text-lg font-bold text-primary-600">Маршрут ${p.route_num || '—'}</span>
            </div>
            
            <div class="space-y-1">
              <div class="flex justify-between text-sm">
                <span class="text-gray-500">Гос. номер:</span>
                <span class="font-mono font-bold text-gray-800">${gosNumber || p.id || '—'}</span>
              </div>
              
              <div class="flex justify-between text-sm">
                <span class="text-gray-500">Скорость:</span>
                <span class="font-bold ${p.speed < 10 ? 'text-red-500' : 'text-green-600'}">${p.speed.toFixed(1)} км/ч</span>
              </div>
              ${parseDirDegrees(p.dir) != null ? `
              <div class="flex justify-between text-sm">
                <span class="text-gray-500">Курс:</span>
                <span class="font-mono font-semibold text-gray-800">${parseDirDegrees(p.dir).toFixed(0)}°</span>
              </div>` : ''}
            </div>

            <div class="grid grid-cols-2 gap-2 pt-1">
              <div class="flex items-center gap-1 text-[10px] bg-gray-50 p-1 rounded">
                <span>WiFi:</span>
                <span>${p.wifi === '1' ? '✅' : '❌'}</span>
              </div>
              <div class="flex items-center gap-1 text-[10px] bg-gray-50 p-1 rounded">
                <span>Низкий пол:</span>
                <span>${p.low_floor === '1' ? '♿' : '❌'}</span>
              </div>
            </div>

            <div class="text-[10px] text-gray-400 mt-2 border-t pt-1 flex justify-between">
              <span>${new Date(p.time).toLocaleDateString('ru-RU')}</span>
              <span>${new Date(p.time).toLocaleTimeString('ru-RU', {hour: '2-digit', minute:'2-digit'})}</span>
            </div>
          </div>
        `)
      }
    }).addTo(map)

    // Fit bounds
    const bounds = routeLayer.getBounds()
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [50, 50] })
    }
  }

  // 2. Add Congestion Zones (always visible)
  if (props.data.congestion_zones && props.data.congestion_zones.length > 0) {
    congestionLayer = L.featureGroup()
    props.data.congestion_zones.forEach(zone => {
      L.geoJSON(zone, {
        style: {
          color: "#ef4444",
          weight: 2,
          fillColor: "#ef4444",
          fillOpacity: 0.3
        }
      }).addTo(congestionLayer)
    })
    congestionLayer.addTo(map)
  }
}

watch(() => props.height, () => {
  if (map) {
    map.invalidateSize()
  }
})

// Сброс фигуры на карте, если область очищена из сайдбара
watch(
  () => analysisAreaGeometry?.value,
  (geom) => {
    if (geom == null && drawnItems) {
      drawnItems.clearLayers()
    }
  }
)
</script>

<template>
  <div class="w-full h-full relative z-10">
    <div ref="mapContainer" class="w-full h-full"></div>
    
    <div class="absolute top-4 left-14 z-[1000] max-w-[220px] pointer-events-none">
      <div class="pointer-events-auto bg-white/95 backdrop-blur-sm border border-gray-200 rounded-xl shadow-lg px-3 py-2 text-[11px] text-gray-600 leading-snug">
        <span class="font-semibold text-gray-800">Область анализа:</span>
        панель слева — нарисуйте полигон. Редактирование и удаление — там же.
      </div>
    </div>

    <!-- Controls -->
    <div class="absolute top-4 right-4 z-[1000] flex flex-col gap-2">
      <button 
        @click="heatmapMode = !heatmapMode"
        :class="[
          'p-3 rounded-xl border shadow-xl flex items-center justify-center transition-all bg-white hover:bg-gray-50',
          heatmapMode ? 'border-primary-500 text-primary-600 ring-2 ring-primary-100' : 'border-gray-200 text-gray-400'
        ]"
        title="Тепловая карта задержек"
      >
        <Layers class="w-5 h-5" />
      </button>
    </div>

    <!-- Map Legend -->
    <div v-if="!heatmapMode" class="absolute bottom-6 right-6 z-[1000] bg-white/90 backdrop-blur-md p-3 rounded-xl border border-gray-200 shadow-xl w-36 animate-in fade-in slide-in-from-bottom-4">
      <h4 class="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-2">Легенда скорости</h4>
      <div class="space-y-1.5">
        <div class="flex items-center gap-2">
          <div class="w-3 h-3 rounded-full bg-[#ef4444]"></div>
          <span class="text-xs text-gray-600">0 - 10 км/ч</span>
        </div>
        <div class="flex items-center gap-2">
          <div class="w-3 h-3 rounded-full bg-[#f97316]"></div>
          <span class="text-xs text-gray-600">10 - 20 км/ч</span>
        </div>
        <div class="flex items-center gap-2">
          <div class="w-3 h-3 rounded-full bg-[#eab308]"></div>
          <span class="text-xs text-gray-600">20 - 40 км/ч</span>
        </div>
        <div class="flex items-center gap-2">
          <div class="w-3 h-3 rounded-full bg-[#22c55e]"></div>
          <span class="text-xs text-gray-600">> 40 км/ч</span>
        </div>
        <div class="pt-2 mt-1 border-t border-gray-100 space-y-1">
          <div class="flex items-start gap-2 opacity-80">
            <svg class="w-3 h-3 shrink-0 mt-0.5 text-gray-600" viewBox="0 0 32 40" aria-hidden="true">
              <path fill="currentColor" d="M16 3 C22 9 27 17 27 25 C27 33 22 37 16 37 C10 37 5 33 5 25 C5 17 10 9 16 3 Z" />
            </svg>
            <span class="text-[10px] text-gray-500 leading-tight">ТС: капля по курсу (поле <code class="text-gray-600">dir</code>)</span>
          </div>
          <div class="flex items-center gap-2 opacity-70">
            <div class="w-3 h-3 rounded bg-[#ef4444]/30 border border-[#ef4444]"></div>
            <span class="text-[10px] text-gray-500 font-medium">Зона затора</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style>
.bus-dir-marker {
  background: transparent !important;
  border: none !important;
}
.leaflet-popup-content-wrapper {
  border-radius: 12px;
  box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1);
}
.leaflet-popup-tip {
  display: none;
}
</style>
