<script setup>
import { ref, inject, computed, watch } from 'vue'
import axios from 'axios'
import { ChevronLeft, ChevronRight, Upload, Play, Trash2, Download, Activity, Clock } from 'lucide-vue-next'

const props = defineProps(['isOpen'])
const emit = defineEmits(['toggle', 'analysis-complete'])

const API_URL = "http://127.0.0.1:8000/analyze"
const isLoading = inject('isLoading')
const analysisAreaGeometry = inject('analysisAreaGeometry')

const params = ref({
  start: '',
  end: '',
  include_zero: true,
  speed_thresh: 8.0,
  eps_m: 50,
  min_pts: 4,
  routes: [],
  map_matching: false,
  snap_tolerance_m: 50,
  bidirectional_analysis: false,
})

watch(
  () => analysisAreaGeometry?.value,
  (g) => {
    if (!g) params.value.bidirectional_analysis = false
  }
)

const rawGeoJson = ref(null)
const fileName = ref('')
const notifications = ref([])
const availableRoutes = ref([])

/** Границы времени по файлу (после загрузки) */
const dataTimeBounds = ref(null)
/** День для слайсера часов (YYYY-MM-DD) */
const sliceDate = ref('')
const hourFrom = ref(7)
const hourTo = ref(10)

const pad2 = (n) => String(n).padStart(2, '0')

const formatLocal = (d) =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`

const clampToBounds = (a, b) => {
  if (!dataTimeBounds.value) return { start: formatLocal(a), end: formatLocal(b) }
  const { min, max } = dataTimeBounds.value
  const s = new Date(Math.max(a.getTime(), min.getTime()))
  const e = new Date(Math.min(b.getTime(), max.getTime()))
  if (s.getTime() >= e.getTime()) return null
  return { start: formatLocal(s), end: formatLocal(e) }
}

const sliceDateBounds = computed(() => {
  if (!dataTimeBounds.value) return { min: '', max: '' }
  const { min, max } = dataTimeBounds.value
  const f = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
  return { min: f(min), max: f(max) }
})

const addNotification = (message, type = 'info') => {
  const id = Date.now()
  notifications.value.push({ id, message, type })
  setTimeout(() => {
    notifications.value = notifications.value.filter(n => n.id !== id)
  }, 5000)
}

const handleFileUpload = async (event) => {
  const file = event.target.files[0]
  if (!file) return
  fileName.value = file.name

  try {
    const text = await file.text()
    const parsed = JSON.parse(text)
    
    if (parsed.type !== "FeatureCollection") {
      throw new Error("Ожидается GeoJSON FeatureCollection")
    }
    
    rawGeoJson.value = parsed
    
    const routeSet = new Set()
    parsed.features.forEach(f => {
      if (f.properties?.route_num) {
        routeSet.add(String(f.properties.route_num))
      }
    })
    availableRoutes.value = Array.from(routeSet).sort((a, b) => a.localeCompare(b, undefined, {numeric: true}))
    
    addNotification(`Файл загружен: ${parsed.features.length} объектов`, 'success')

    // Auto-detect time range if possible
    detectTimeRange(parsed)
  } catch (e) {
    addNotification(`Ошибка: ${e.message}`, 'error')
    rawGeoJson.value = null
    dataTimeBounds.value = null
  }
}

const detectTimeRange = (geojson) => {
  const times = []
  
  geojson.features.forEach(f => {
    const raw = f.properties?.time
    if (!raw) return
    
    // Support DD.MM.YYYY HH:mm:ss format seen in output.geojson
    const ddmmyyyyMatch = String(raw).match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{1,2})/)
    if (ddmmyyyyMatch) {
      const [, day, month, year, hour, minute] = ddmmyyyyMatch
      // Create date and add 8 hours
      const d = new Date(year, month - 1, day, parseInt(hour) + 8, minute)
      if (!isNaN(d.getTime())) times.push(d)
    } else {
      const d = new Date(raw)
      if (!isNaN(d.getTime())) {
        d.setHours(d.getHours() + 8) // Apply offset to ISO format as well
        times.push(d)
      }
    }
  })

  if (times.length > 0) {
    const min = new Date(Math.min(...times))
    const max = new Date(Math.max(...times))
    dataTimeBounds.value = { min, max }
    sliceDate.value = `${min.getFullYear()}-${pad2(min.getMonth() + 1)}-${pad2(min.getDate())}`
    hourFrom.value = 7
    hourTo.value = 10

    params.value.start = formatLocal(min)
    params.value.end = formatLocal(max)
    addNotification(`Период определен: ${min.toLocaleDateString()} - ${max.toLocaleDateString()}`, "info")
  } else {
    dataTimeBounds.value = null
  }
}

const applyPreset = (id) => {
  if (!dataTimeBounds.value) {
    addNotification('Сначала загрузите файл с метками времени', 'warning')
    return
  }
  const { min, max } = dataTimeBounds.value
  const D = new Date(min.getFullYear(), min.getMonth(), min.getDate(), 0, 0, 0, 0)

  const slot = (h0, h1) => {
    const startSlot = new Date(D.getFullYear(), D.getMonth(), D.getDate(), h0, 0, 0, 0)
    const endSlot = new Date(D.getFullYear(), D.getMonth(), D.getDate(), h1, 59, 59, 999)
    return clampToBounds(startSlot, endSlot)
  }

  let r = null
  if (id === 'full') {
    r = { start: formatLocal(min), end: formatLocal(max) }
  } else if (id === 'morning') r = slot(6, 10)
  else if (id === 'day') r = slot(10, 16)
  else if (id === 'evening') r = slot(16, 22)
  else if (id === 'peak_am') r = slot(7, 9)
  else if (id === 'peak_pm') r = slot(17, 19)
  else if (id === 'last2h') {
    const e = max
    const s = new Date(e.getTime() - 2 * 3600000)
    r = clampToBounds(s, e)
  } else if (id === 'last4h') {
    const e = max
    const s = new Date(e.getTime() - 4 * 3600000)
    r = clampToBounds(s, e)
  }

  if (!r) {
    addNotification('Пресет не пересекается с данными файла', 'warning')
    return
  }
  params.value.start = r.start
  params.value.end = r.end
  addNotification('Период обновлён по пресету', 'info')
}

const applyHourSlice = () => {
  const d = sliceDate.value
  if (!d) return
  const h0 = Math.min(hourFrom.value, hourTo.value)
  const h1 = Math.max(hourFrom.value, hourTo.value)
  const startSlot = new Date(`${d}T${pad2(h0)}:00`)
  const endSlot = new Date(`${d}T${pad2(h1)}:59`)
  const r = clampToBounds(startSlot, endSlot)
  if (!r) {
    addNotification('Выбранные часы не пересекаются с данными файла', 'warning')
    return
  }
  params.value.start = r.start
  params.value.end = r.end
  addNotification('Период обновлён по часам', 'info')
}

const buildPayload = () => ({
  geojson: rawGeoJson.value,
  include_zero: params.value.include_zero,
  speed_thresh: params.value.speed_thresh,
  eps_m: params.value.eps_m,
  min_pts: params.value.min_pts,
  routes: params.value.routes.length > 0 ? params.value.routes : null,
  map_matching: params.value.map_matching,
  snap_tolerance_m: params.value.snap_tolerance_m,
  analysis_geometry: analysisAreaGeometry?.value || null,
  bidirectional_analysis: params.value.bidirectional_analysis,
})

const runAnalysis = async () => {
  if (!rawGeoJson.value) {
    addNotification("Сначала загрузите GeoJSON", "warning")
    return
  }

  if (!params.value.start || !params.value.end) {
    addNotification('Задайте начало и конец периода', 'warning')
    return
  }

  isLoading.value = true
  try {
    const res = await axios.post(API_URL, {
      ...buildPayload(),
      start: params.value.start.replace('T', ' '),
      end: params.value.end.replace('T', ' '),
    })
    emit('analysis-complete', res.data)
    addNotification('Анализ завершён', 'success')
  } catch (e) {
    const msg = e.response?.data?.detail || e.message
    addNotification(`Ошибка анализа: ${msg}`, "error")
  } finally {
    isLoading.value = false
  }
}

const clearData = () => {
  rawGeoJson.value = null
  fileName.value = ''
  availableRoutes.value = []
  params.value.routes = []
  dataTimeBounds.value = null
  if (analysisAreaGeometry) analysisAreaGeometry.value = null
  emit('analysis-complete', null)
  addNotification("Данные очищены", "info")
}

const clearAnalysisAreaOnly = () => {
  if (analysisAreaGeometry) analysisAreaGeometry.value = null
  addNotification("Область анализа снята", "info")
}

const exportPdf = () => {
  window.print();
}
</script>

<template>
  <aside 
    :class="[
      'bg-white border-r border-gray-200 transition-all duration-300 flex flex-col z-50',
      isOpen ? 'w-80' : 'w-16'
    ]"
  >
    <!-- Header -->
    <div class="p-4 border-b border-gray-100 flex items-center justify-between overflow-hidden whitespace-nowrap">
      <h1 v-if="isOpen" class="font-bold text-lg text-primary-600 flex items-center gap-2">
        <Activity class="w-6 h-6" />
        Анализ транспорта
      </h1>
      <button 
        @click="emit('toggle')"
        class="p-2 hover:bg-gray-100 rounded-lg text-gray-500 transition-colors"
      >
        <ChevronLeft v-if="isOpen" />
        <ChevronRight v-else />
      </button>
    </div>

    <!-- Content -->
    <div v-show="isOpen" class="flex-1 overflow-y-auto custom-scrollbar p-4 space-y-6">
      
      <!-- Notifications inside sidebar -->
      <TransitionGroup name="list" tag="div" class="space-y-2">
        <div 
          v-for="note in notifications" 
          :key="note.id"
          :class="[
            'p-3 rounded-lg text-sm border flex items-start gap-2 animate-in slide-in-from-top-4',
            note.type === 'success' ? 'bg-green-50 border-green-200 text-green-800' :
            note.type === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
            note.type === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' : 'bg-blue-50 border-blue-200 text-blue-800'
          ]"
        >
          {{ note.message }}
        </div>
      </TransitionGroup>

      <!-- Section: Load Data -->
      <div class="space-y-3">
        <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Загрузка данных</h2>
        <label class="group block border-2 border-dashed border-gray-200 hover:border-primary-400 rounded-xl p-6 transition-all cursor-pointer bg-gray-50 hover:bg-white text-center">
          <Upload class="w-8 h-8 mx-auto mb-3 text-gray-300 group-hover:text-primary-500 transition-colors" />
          <span class="block text-sm font-medium text-gray-600 group-hover:text-primary-600">
            {{ fileName || 'Выберите GeoJSON файл' }}
          </span>
          <input type="file" @change="handleFileUpload" class="hidden" accept=".geojson,.json" />
        </label>
      </div>

      <!-- Section: Map AOI -->
      <div class="space-y-2 pt-2 border-t border-gray-100">
        <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Область на карте</h2>
        <p class="text-[11px] text-gray-500 leading-snug">
          На карте слева: инструмент <strong>полигон</strong>. После выделения анализ учитывает только точки внутри области.
        </p>
        <div
          v-if="analysisAreaGeometry"
          class="flex items-center justify-between gap-2 p-2 rounded-lg bg-primary-50 border border-primary-100"
        >
          <span class="text-xs text-primary-800 font-medium">Область задана</span>
          <button
            type="button"
            @click="clearAnalysisAreaOnly"
            class="text-[11px] font-semibold text-primary-700 hover:text-primary-900 underline shrink-0"
          >
            Снять
          </button>
        </div>
        <p v-else class="text-[11px] text-gray-400 italic">Без выделения — по всему файлу</p>
        <div
          class="flex items-start justify-between gap-2 p-2 rounded-lg border border-gray-100 bg-gray-50/80"
          :class="!analysisAreaGeometry ? 'opacity-60' : ''"
        >
          <div class="min-w-0">
            <span class="text-xs font-medium text-gray-800">Две стороны движения</span>
            <p class="text-[10px] text-gray-500 leading-snug mt-0.5">
              Отдельная ср. скорость, индекс затора и зоны по каждому направлению в полигоне (по цепочкам GPS).
            </p>
          </div>
          <input
            type="checkbox"
            v-model="params.bidirectional_analysis"
            :disabled="!analysisAreaGeometry"
            class="w-4 h-4 text-primary-600 rounded shrink-0 mt-0.5"
            title="Сначала нарисуйте полигон на карте"
          />
        </div>
      </div>

      <!-- Section: Analysis Parameters -->
      <div class="space-y-4 pt-4 border-t border-gray-100">
        <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wider">Параметры анализа</h2>
        
        <div class="grid grid-cols-2 gap-3">
          <div class="space-y-1">
            <label class="text-xs text-gray-500">Начало</label>
            <input 
              type="datetime-local" 
              v-model="params.start" 
              class="w-full text-xs p-2 border rounded-md focus:ring-1 focus:ring-primary-500 outline-none"
            />
          </div>
          <div class="space-y-1">
            <label class="text-xs text-gray-500">Конец</label>
            <input 
              type="datetime-local" 
              v-model="params.end" 
              class="w-full text-xs p-2 border rounded-md focus:ring-1 focus:ring-primary-500 outline-none"
            />
          </div>
        </div>

        <div v-if="dataTimeBounds" class="space-y-2 pt-2 border-t border-gray-100">
          <h2 class="text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-1.5">
            <Clock class="w-3.5 h-3.5" /> Быстрый выбор
          </h2>
          <div class="flex flex-wrap gap-1">
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('full')">Весь период</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('morning')">6–10</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('day')">10–16</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('evening')">16–22</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('peak_am')">Пик утро</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('peak_pm')">Пик вечер</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('last2h')">−2 ч</button>
            <button type="button" class="px-2 py-1 text-[10px] font-medium rounded-md bg-gray-100 hover:bg-primary-100 text-gray-700 border border-gray-200" @click="applyPreset('last4h')">−4 ч</button>
          </div>
          <div class="space-y-2 rounded-lg border border-gray-100 bg-gray-50/80 p-2">
            <div class="text-[10px] font-semibold text-gray-600">Слайдер по часам (1-й день в данных)</div>
            <input
              type="date"
              v-model="sliceDate"
              :min="sliceDateBounds.min"
              :max="sliceDateBounds.max"
              class="w-full text-xs p-1.5 border rounded-md bg-white"
            />
            <div class="flex items-center gap-2">
              <span class="text-[10px] text-gray-500 w-6 shrink-0">С</span>
              <input v-model.number="hourFrom" type="range" min="0" max="23" class="flex-1 h-1.5 accent-primary-600" />
              <span class="text-[11px] font-mono w-6 text-right">{{ hourFrom }}</span>
            </div>
            <div class="flex items-center gap-2">
              <span class="text-[10px] text-gray-500 w-6 shrink-0">По</span>
              <input v-model.number="hourTo" type="range" min="0" max="23" class="flex-1 h-1.5 accent-primary-600" />
              <span class="text-[11px] font-mono w-6 text-right">{{ hourTo }}</span>
            </div>
            <button type="button" class="w-full text-[11px] font-semibold py-1.5 rounded-lg bg-primary-600 text-white hover:bg-primary-700" @click="applyHourSlice">
              Применить часы к периоду
            </button>
          </div>
          <p class="text-[10px] text-gray-400 leading-snug">Пресеты 6–10, 10–16… — по <strong>первому календарному дню</strong> из файла. «−2 ч» — от конца данных.</p>
        </div>

        <div v-if="availableRoutes.length > 0" class="space-y-1">
          <label class="text-xs text-gray-500">Маршруты (не выбрано = все)</label>
          <div class="h-24 overflow-y-auto border border-gray-200 rounded-md p-2 bg-gray-50 custom-scrollbar grid grid-cols-2 gap-1">
            <label v-for="r in availableRoutes" :key="r" class="flex items-center gap-2 p-1 bg-white hover:bg-primary-50 border border-gray-100 rounded cursor-pointer transition-colors">
              <input type="checkbox" :value="r" v-model="params.routes" class="w-3 h-3 text-primary-600 rounded" />
              <span class="text-xs font-semibold text-gray-700">{{ r }}</span>
            </label>
          </div>
        </div>

        <div class="space-y-3">
          <div class="flex items-center justify-between p-2 bg-gray-50 rounded-lg border border-gray-100">
            <span class="text-sm text-gray-700">Включать 0 км/ч</span>
            <input 
              type="checkbox" 
              v-model="params.include_zero"
              class="w-4 h-4 text-primary-600 rounded"
            />
          </div>

          <div class="space-y-2 p-2 bg-primary-50 rounded-lg border border-primary-100">
            <div class="flex items-center justify-between gap-2">
              <div class="flex flex-col min-w-0">
                <span class="text-sm text-primary-800 font-medium">Привязка к дорогам</span>
                <span class="text-[10px] text-primary-600 leading-tight">Привязка к графу дорожной сети проекта</span>
              </div>
              <input
                type="checkbox"
                v-model="params.map_matching"
                class="w-4 h-4 text-primary-600 rounded shrink-0"
              />
            </div>
            <div v-if="params.map_matching" class="space-y-2 pt-1 border-t border-primary-100/80">
              <div class="flex items-center gap-2">
                <label class="text-[10px] text-primary-700 shrink-0">Допуск, м</label>
                <input
                  v-model.number="params.snap_tolerance_m"
                  type="number"
                  min="5"
                  max="500"
                  step="5"
                  class="flex-1 text-xs p-1.5 border border-primary-200 rounded-md bg-white"
                />
              </div>
              <p class="text-[9px] text-primary-600 leading-snug">
                Граф дорог загружается автоматически из файла <code class="text-primary-800">highway_graph.geojson</code> в корне проекта.
              </p>
            </div>
          </div>

          <div class="space-y-1">
            <div class="flex justify-between">
              <label class="text-xs text-gray-500">Порог затора (км/ч)</label>
              <span class="text-xs font-medium">{{ params.speed_thresh }}</span>
            </div>
            <input type="range" min="1" max="50" step="0.5" v-model="params.speed_thresh" class="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-primary-600" />
          </div>

          <div class="space-y-1">
            <div class="flex justify-between">
              <label class="text-xs text-gray-500">Радиус кластера (м)</label>
              <span class="text-xs font-medium">{{ params.eps_m }}</span>
            </div>
            <input type="range" min="10" max="200" step="10" v-model="params.eps_m" class="w-full h-1.5 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-primary-600" />
          </div>
        </div>
      </div>

      <!-- Actions -->
      <div class="pt-4 space-y-2">
        <button 
          @click="runAnalysis"
          :disabled="isLoading"
          class="w-full bg-primary-600 hover:bg-primary-700 disabled:bg-gray-400 text-white font-semibold py-3 px-4 rounded-xl shadow-lg shadow-primary-200 transition-all flex items-center justify-center gap-2"
        >
          <Play v-if="!isLoading" class="w-4 h-4 fill-current" />
          <div v-else class="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin"></div>
          Анализировать
        </button>
        
        <div class="grid grid-cols-2 gap-2">
          <button @click="clearData" class="flex items-center justify-center gap-1 py-2 text-xs font-medium text-gray-600 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors">
            <Trash2 class="w-3.5 h-3.5" /> Очистить
          </button>
          <button @click="exportPdf" class="flex items-center justify-center gap-1 py-2 text-xs font-medium text-gray-600 hover:text-primary-600 hover:bg-primary-50 rounded-lg transition-colors">
            <Download class="w-3.5 h-3.5" /> Экспорт
          </button>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.list-enter-active, .list-leave-active { transition: all 0.3s ease; }
.list-enter-from { opacity: 0; transform: translateY(-10px); }
.list-leave-to { opacity: 0; transform: translateX(30px); }
</style>
