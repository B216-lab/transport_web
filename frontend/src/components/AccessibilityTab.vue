<script setup>
import { ref, inject, computed, onMounted, onUnmounted } from 'vue'
import axios from 'axios'
import { MapPin, Play, Trash2, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { API_BASE } from '../apiBase.js'

const props = defineProps(['isOpen'])
const emit = defineEmits(['toggle'])

const API_PED = `${API_BASE}/isochrone/pedestrian`
const API_TRANSIT = `${API_BASE}/isochrone/transit`
const API_GRAPH = `${API_BASE}/graph/pedestrian/status`
const API_DATA = `${API_BASE}/isochrone/data-status`

const isLoading = inject('isLoading')
const analysisResult = inject('analysisResult', null)
const isochroneOrigin = inject('isochroneOrigin')
const isochroneResult = inject('isochroneResult')
const isochronePickMode = inject('isochronePickMode')
const showPedestrianGraph = inject('showPedestrianGraph', null)

const showPedGraph = computed({
  get: () => !!showPedestrianGraph?.value,
  set: (v) => {
    if (showPedestrianGraph) showPedestrianGraph.value = !!v
  },
})

const graphReady = ref(false)
const graphMeta = ref(null)
const dataStatus = ref(null)
const notifications = ref([])

const intervalStep = ref(5)
const intervalCount = ref(3)
const maxSnapM = ref(80)
const useElevation = ref(true)
const includeBuildingStats = ref(true)
const travelMode = ref('pedestrian') // pedestrian | transit
const maxTransfers = ref(1)
/** walk — как в сети (пешая); avg | median — из последнего анализа */
const ptSpeedMetric = ref('avg')

const analysisAvgSpeed = computed(() => {
  const v = analysisResult?.value?.avg_speed
  return v != null && Number(v) > 0 ? Number(v) : null
})

const analysisMedianSpeed = computed(() => {
  const v = analysisResult?.value?.statistics?.median
  return v != null && Number(v) > 0 ? Number(v) : null
})

const analysisRouteStats = computed(() => analysisResult?.value?.route_stats ?? null)
const analysisSegmentStats = computed(() => analysisResult?.value?.segment_stats ?? null)

const buildRouteMapsFromAnalysis = () => {
  const stats = analysisRouteStats.value
  if (!stats || typeof stats !== 'object') return { speeds: null, headways: null, segments: null }
  const speeds = {}
  const headways = {}
  const metric = ptSpeedMetric.value
  const speedKey = metric === 'median' ? 'median_speed' : 'avg_speed'
  for (const [route, row] of Object.entries(stats)) {
    let sp = row?.[speedKey]
    if (metric === 'segments' && row?.avg_speed_from_segments != null) {
      sp = row.avg_speed_from_segments
    }
    if (sp != null && Number(sp) > 0) speeds[route] = Number(sp)
    const hw = row?.headway_min
    if (hw != null && Number(hw) > 0) headways[route] = Number(hw)
  }

  const segments = {}
  const segStats = analysisSegmentStats.value
  if (segStats && typeof segStats === 'object') {
    const segKey = metric === 'median' ? 'median_speed' : 'avg_speed'
    for (const [key, row] of Object.entries(segStats)) {
      const sp = row?.[segKey]
      if (sp != null && Number(sp) > 0) segments[key] = Number(sp)
    }
  }

  return {
    speeds: Object.keys(speeds).length ? speeds : null,
    headways: Object.keys(headways).length ? headways : null,
    segments: Object.keys(segments).length ? segments : null,
  }
}

const analysisRoutesCount = computed(() => {
  const s = analysisRouteStats.value
  return s ? Object.keys(s).length : 0
})

const analysisSegmentsCount = computed(() => {
  const s = analysisSegmentStats.value
  return s ? Object.keys(s).length : 0
})

const analysisPointsCount = computed(() => analysisResult?.value?.count ?? 0)

const intervalsPreview = computed(() => {
  const out = []
  for (let i = 0; i < intervalCount.value; i++) {
    out.push(intervalStep.value * (i + 1))
  }
  return out
})

const hasOrigin = computed(() => Array.isArray(isochroneOrigin?.value) && isochroneOrigin.value.length === 2)

const addNotification = (message, type = 'info') => {
  const id = Date.now()
  notifications.value.push({ id, message, type })
  setTimeout(() => {
    notifications.value = notifications.value.filter((n) => n.id !== id)
  }, 6000)
}

const loadGraphStatus = async () => {
  try {
    const [graphRes, dataRes] = await Promise.all([axios.get(API_GRAPH), axios.get(API_DATA)])
    const data = graphRes.data
    graphReady.value = !!data.ready
    graphMeta.value = data.ready ? data : null
    dataStatus.value = dataRes.data
    if (!data.ready) {
      addNotification(data.hint || 'Сначала соберите пеший граф (этап 1)', 'error')
    }
  } catch (e) {
    graphReady.value = false
    addNotification(e.response?.data?.detail || e.message, 'error')
  }
}

const hasPopulationStats = computed(
  () => isochroneResult?.value?.zones?.some((z) => z.population != null) ?? false,
)

onMounted(() => {
  loadGraphStatus()
  if (isochronePickMode) isochronePickMode.value = true
})

onUnmounted(() => {
  if (isochronePickMode) isochronePickMode.value = false
  if (showPedestrianGraph) showPedestrianGraph.value = false
})

const togglePick = () => {
  if (isochronePickMode) isochronePickMode.value = !isochronePickMode.value
}

const clearAll = () => {
  if (isochroneOrigin) isochroneOrigin.value = null
  if (isochroneResult) isochroneResult.value = null
}

const runIsochrone = async () => {
  if (!graphReady.value) {
    addNotification('Граф не готов. Выполните build_pedestrian_graph.', 'error')
    return
  }
  if (!hasOrigin.value) {
    addNotification('Укажите точку на карте (клик при включённом выборе).', 'error')
    return
  }

  isLoading.value = true
  try {
    const url = travelMode.value === 'transit' ? API_TRANSIT : API_PED
    const body = {
      origin: isochroneOrigin.value,
      interval_step_min: intervalStep.value,
      interval_count: intervalCount.value,
      max_snap_m: maxSnapM.value,
      use_elevation: useElevation.value,
      include_building_stats: includeBuildingStats.value,
    }
    if (travelMode.value === 'transit') {
      body.max_transfers = maxTransfers.value
      if (ptSpeedMetric.value !== 'walk') {
        if (!analysisRouteStats.value && !analysisAvgSpeed.value) {
          addNotification(
            'Сначала выполните анализ на вкладке «Анализ» — нужны скорости по маршрутам.',
            'error',
          )
          isLoading.value = false
          return
        }
        const { speeds, headways, segments } = buildRouteMapsFromAnalysis()
        if (speeds || segments || headways) {
          if (headways) body.route_headways = headways
          if (segments) body.segment_speeds = segments
          if (speeds) body.route_speeds = speeds
        } else {
          const fallback = ptSpeedMetric.value === 'median' ? analysisMedianSpeed.value : analysisAvgSpeed.value
          if (fallback == null || fallback <= 0) {
            addNotification('Нет скорости из анализа для расчёта ОТ.', 'error')
            isLoading.value = false
            return
          }
          body.pt_speed_kmh = fallback
        }
      }
    }
    const { data } = await axios.post(url, body)
    isochroneResult.value = data
    const pop = data.zones?.reduce((s, z) => s + (z.population || 0), 0)
    const elevNote = data.elevation?.elevation_applied ? 'рельеф вкл.' : 'без рельефа'
    let speedNote = ''
    if (travelMode.value === 'transit' && data.segments_with_speed) {
      speedNote = `, ОТ: ${data.segments_with_speed} сегм. + ${data.routes_with_speed || 0} маршр.`
    } else if (travelMode.value === 'transit' && data.routes_with_speed) {
      speedNote = `, ОТ: ${data.routes_with_speed} маршр. из анализа`
    } else if (travelMode.value === 'transit' && data.pt_speed_kmh) {
      speedNote = `, ОТ ${data.pt_speed_kmh.toFixed(1)} км/ч`
    } else if (travelMode.value === 'transit' && data.pt_speed_source === 'walk_network') {
      speedNote = ', ОТ: пешая скорость сети'
    }
    addNotification(
      `Зон: ${data.zones?.length || 0}, ${elevNote}, привязка ${data.snap_distance_m} м` +
        speedNote +
        (pop > 0 ? `, население до ${Math.round(pop).toLocaleString('ru-RU')}` : ''),
      'success',
    )
    if (data.elevation?.elevation_warning) addNotification(data.elevation.elevation_warning, 'info')
    if (data.buildings?.warning) addNotification(data.buildings.warning, 'info')
  } catch (e) {
    const msg = e.response?.data?.detail || e.message
    addNotification(typeof msg === 'string' ? msg : JSON.stringify(msg), 'error')
  } finally {
    isLoading.value = false
  }
}
</script>

<template>
  <aside
    :class="[
      'bg-white border-r border-gray-200 flex flex-col transition-all duration-300 z-20 shadow-sm h-full',
      isOpen ? 'w-80' : 'w-0 overflow-hidden',
    ]"
  >
    <div class="p-4 border-b border-gray-100 flex items-center justify-between">
      <h2 class="font-bold text-gray-800">Доступность</h2>
      <button @click="emit('toggle')" class="p-1 hover:bg-gray-100 rounded-lg text-gray-500">
        <ChevronLeft v-if="isOpen" class="w-5 h-5" />
        <ChevronRight v-else class="w-5 h-5" />
      </button>
    </div>

    <div class="p-4 space-y-4 overflow-y-auto flex-1 text-sm">
      <div
        :class="[
          'rounded-lg px-3 py-2 text-xs',
          graphReady ? 'bg-green-50 text-green-800 border border-green-100' : 'bg-amber-50 text-amber-900 border border-amber-100',
        ]"
      >
        <template v-if="graphReady">
          <div>
            Граф: {{ graphMeta?.node_count?.toLocaleString() }} узлов,
            {{ graphMeta?.edge_count?.toLocaleString() }} рёбер
          </div>
          <div class="mt-1 text-[10px] opacity-90">
            Рельеф в графе:
            <span :class="graphMeta?.has_elevation ? 'font-semibold' : ''">
              {{ graphMeta?.has_elevation ? 'да' : 'нет (enrich_pedestrian_graph)' }}
            </span>
            · Изолинии: {{ dataStatus?.contours_available ? 'файл есть' : 'нет' }}
            · Здания: {{ dataStatus?.buildings_available ? 'файл есть' : 'нет' }}
            · ОТ: {{ dataStatus?.pt_network_ready ? (dataStatus.pt_route_count + ' маршр.') : 'нет' }}
          </div>
        </template>
        <template v-else>Граф не собран — см. этап 1</template>
      </div>

      <div>
        <label class="text-xs font-semibold text-gray-500 uppercase">Режим</label>
        <select
          v-model="travelMode"
          class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-2 text-sm"
        >
          <option value="pedestrian">Пешком</option>
          <option value="transit">Общественный транспорт</option>
        </select>
        <p v-if="travelMode === 'transit' && !dataStatus?.pt_network_ready" class="text-[10px] text-amber-700 mt-1">
          Сеть ОТ не собрана: build_pt_network в backend
        </p>
      </div>

      <div v-if="travelMode === 'transit'" class="space-y-3">
        <div>
          <label class="text-xs font-semibold text-gray-500 uppercase">Скорость ОТ</label>
          <select
            v-model="ptSpeedMetric"
            class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-2 text-sm"
          >
            <option value="avg">Средняя по маршруту</option>
            <option value="median">Медиана по маршруту</option>
            <option value="segments">Средняя по сегментам ОП (как на схеме)</option>
            <option value="walk">Как в сети (пешая, 4,5 км/ч)</option>
          </select>
          <p v-if="ptSpeedMetric !== 'walk' && analysisRoutesCount > 0" class="text-[10px] text-gray-500 mt-1">
            Из анализа: {{ analysisRoutesCount }} маршр., {{ analysisSegmentsCount }} сегм.
            <span v-if="ptSpeedMetric !== 'walk'"> · интервалы H</span>
            <span v-if="analysisAvgSpeed != null"> · общая средняя {{ analysisAvgSpeed.toFixed(1) }} км/ч</span>
          </p>
          <p
            v-else-if="ptSpeedMetric !== 'walk' && analysisAvgSpeed != null"
            class="text-[10px] text-gray-500 mt-1"
          >
            Анализ: средняя {{ analysisAvgSpeed.toFixed(1) }} км/ч
            <span v-if="analysisMedianSpeed != null"> · медиана {{ analysisMedianSpeed.toFixed(1) }} км/ч</span>
            <span v-if="analysisPointsCount"> · {{ analysisPointsCount.toLocaleString('ru-RU') }} точек</span>
          </p>
          <p
            v-else-if="ptSpeedMetric !== 'walk'"
            class="text-[10px] text-amber-700 mt-1"
          >
            Нет данных анализа — сначала запустите анализ на вкладке «Анализ» с нужным периодом.
          </p>
        </div>
        <div>
          <label class="text-xs text-gray-500">Макс. пересадок</label>
          <input
            v-model.number="maxTransfers"
            type="number"
            min="0"
            max="3"
            class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-1.5"
          />
        </div>
      </div>

      <div class="space-y-2 text-xs">
        <label class="flex items-center gap-2 cursor-pointer">
          <input v-model="showPedGraph" type="checkbox" class="rounded border-gray-300" />
          <span>Показать пеший граф на карте</span>
        </label>
        <p v-if="showPedGraph" class="text-[10px] text-gray-500 pl-5">
          Серые линии — улицы/тротуары сети. Ставьте точку <b>на линию</b>, не во двор.
        </p>
        <label class="flex items-center gap-2 cursor-pointer">
          <input v-model="useElevation" type="checkbox" class="rounded border-gray-300" />
          <span>Учитывать рельеф (перепад высот)</span>
        </label>
        <label class="flex items-center gap-2 cursor-pointer">
          <input v-model="includeBuildingStats" type="checkbox" class="rounded border-gray-300" />
          <span>Статистика по зданиям / населению</span>
        </label>
      </div>

      <div class="space-y-2">
        <label class="text-xs font-semibold text-gray-500 uppercase">Точка объекта</label>
        <button
          type="button"
          @click="togglePick"
          :class="[
            'w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg border font-medium transition-colors',
            isochronePickMode?.value
              ? 'border-primary-500 bg-primary-50 text-primary-700'
              : 'border-gray-200 text-gray-600 hover:bg-gray-50',
          ]"
        >
          <MapPin class="w-4 h-4" />
          {{ isochronePickMode?.value ? 'Кликните на карте…' : 'Выбрать на карте' }}
        </button>
        <p v-if="hasOrigin" class="text-xs text-gray-600 font-mono">
          {{ isochroneOrigin[0].toFixed(5) }}, {{ isochroneOrigin[1].toFixed(5) }}
        </p>
        <p v-else class="text-xs text-gray-400">Точка не выбрана</p>
      </div>

      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-xs text-gray-500">Шаг, мин</label>
          <input
            v-model.number="intervalStep"
            type="number"
            min="1"
            max="60"
            class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-1.5"
          />
        </div>
        <div>
          <label class="text-xs text-gray-500">Число зон</label>
          <input
            v-model.number="intervalCount"
            type="number"
            min="1"
            max="8"
            class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-1.5"
          />
        </div>
      </div>
      <p class="text-xs text-gray-500">
        Интервалы: <span class="font-semibold">{{ intervalsPreview.join(', ') }}</span> мин
      </p>

      <div>
        <label class="text-xs text-gray-500">Привязка к графу, м</label>
        <input
          v-model.number="maxSnapM"
          type="number"
          min="20"
          max="300"
          class="w-full mt-1 border border-gray-200 rounded-lg px-2 py-1.5"
        />
      </div>

      <button
        type="button"
        @click="runIsochrone"
        :disabled="isLoading || !graphReady"
        class="w-full flex items-center justify-center gap-2 bg-primary-600 text-white py-2.5 rounded-lg font-semibold hover:bg-primary-700 disabled:opacity-50"
      >
        <Play class="w-4 h-4" />
        Построить зоны
      </button>

      <button
        type="button"
        @click="clearAll"
        class="w-full flex items-center justify-center gap-2 border border-gray-200 text-gray-600 py-2 rounded-lg hover:bg-gray-50"
      >
        <Trash2 class="w-4 h-4" />
        Очистить
      </button>

      <div v-if="isochroneResult?.zones?.length" class="border border-gray-100 rounded-lg p-3 space-y-2">
        <h3 class="text-xs font-bold text-gray-500 uppercase">Результат</h3>
        <table class="w-full text-xs text-left">
          <thead>
            <tr class="text-gray-400 border-b">
              <th class="py-1 font-medium">Зона</th>
              <th class="py-1 font-medium text-right">Узлы</th>
              <th v-if="hasPopulationStats" class="py-1 font-medium text-right">Насел.</th>
              <th v-if="hasPopulationStats" class="py-1 font-medium text-right">Зданий</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="z in isochroneResult.zones" :key="z.interval_min" class="border-b border-gray-50">
              <td class="py-1.5">≤ {{ z.interval_min }} мин</td>
              <td class="py-1.5 text-right text-gray-500">{{ z.reachable_nodes }}</td>
              <td v-if="hasPopulationStats" class="py-1.5 text-right font-medium">
                {{ (z.population ?? 0).toLocaleString('ru-RU') }}
              </td>
              <td v-if="hasPopulationStats" class="py-1.5 text-right text-gray-500">
                {{ z.buildings_count ?? 0 }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="hasPopulationStats" class="text-[10px] text-gray-400">
          Население — накопительно (≤ N мин).
          <span v-if="isochroneResult.buildings?.population_from_levels">
            Оценка по этажам OSM (25 чел./этаж).
          </span>
        </p>
        <p class="text-[10px] text-gray-400 pt-1 border-t">
          Привязка: {{ isochroneResult.snap_distance_m }} м
          <span v-if="isochroneResult.elevation?.elevation_applied"> · рельеф</span>
          <span v-if="isochroneResult.pt_speed_kmh"> · ОТ {{ isochroneResult.pt_speed_kmh.toFixed(1) }} км/ч</span>
          <span v-else-if="isochroneResult.routes_with_speed">
            · ОТ: {{ isochroneResult.routes_with_speed }} маршр.
          </span>
          <span v-if="isochroneResult.zone_geometry_method === 'network_buffer_pt_corridors'"> · по сети + коридоры ОТ</span>
          <span v-else-if="isochroneResult.zone_geometry_method === 'network_buffer'"> · по уличной сети</span>
        </p>
      </div>
    </div>

    <div class="p-2 space-y-1">
      <div
        v-for="n in notifications"
        :key="n.id"
        :class="[
          'text-xs px-3 py-2 rounded-lg',
          n.type === 'error' ? 'bg-red-50 text-red-700' : n.type === 'success' ? 'bg-green-50 text-green-700' : 'bg-gray-50 text-gray-700',
        ]"
      >
        {{ n.message }}
      </div>
    </div>
  </aside>
</template>
