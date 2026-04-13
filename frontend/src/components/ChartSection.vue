<script setup>
import { onMounted, onUnmounted, ref, watch, computed, nextTick } from 'vue'
import Chart from 'chart.js/auto'
import { TrendingUp, MapPin, ShieldAlert, Zap, Activity, LayoutDashboard, Timer } from 'lucide-vue-next'
import { rankIntervalsByAvgSpeed } from '../utils/intervalBuckets.js'

const props = defineProps({
  data: Object,
})

const chartCanvas = ref(null)
const chartHost = ref(null)
let chart = null
let resizeObserver = null
const dashboardTab = ref('overview')
/** all — сводка по полигону; 0/1 — отдельное направление (bidirectional) */
const flowDirTab = ref('all')

watch(
  () => props.data,
  () => {
    flowDirTab.value = 'all'
  }
)

const activeStatsSource = computed(() => {
  const d = props.data
  if (!d) return null
  if (flowDirTab.value === 'all' || !d.bidirectional) return d
  const dir = d.bidirectional.directions?.find((x) => x.id === flowDirTab.value)
  return dir || d
})

const statsItems = computed(() => {
  const d = activeStatsSource.value
  if (!d) return []
  const s = d.statistics || {}
  const ci = d.congestion_index || 1
  let ciColor = 'text-green-600'
  let ciBg = 'bg-green-50'
  if (ci >= 4 && ci <= 6) {
    ciColor = 'text-yellow-600'
    ciBg = 'bg-yellow-50'
  } else if (ci > 6) {
    ciColor = 'text-red-600'
    ciBg = 'bg-red-50'
  }
  const countVal =
    flowDirTab.value !== 'all' && props.data?.bidirectional ? d.count ?? 0 : d.count || 0
  const empty = (d.count ?? 0) === 0
  return [
    {
      label: 'Ср. скорость',
      value: empty ? '—' : (d.avg_speed != null ? d.avg_speed : 0).toFixed(1) + ' км/ч',
      icon: TrendingUp,
      color: 'text-blue-600',
      bg: 'bg-blue-50',
    },
    { label: 'Индекс затора', value: empty ? '—' : `${ci} / 10`, icon: Activity, color: ciColor, bg: ciBg },
    { label: 'Зон заторов', value: d.congestion_zones?.length || 0, icon: ShieldAlert, color: 'text-red-600', bg: 'bg-red-50' },
    { label: 'Всего точек', value: countVal, icon: MapPin, color: 'text-gray-600', bg: 'bg-gray-100' },
    {
      label: 'Медиана',
      value: empty ? '—' : (s.median || 0).toFixed(1) + ' км/ч',
      icon: Zap,
      color: 'text-purple-600',
      bg: 'bg-purple-50',
    },
  ]
})

const intervals1h = computed(() =>
  rankIntervalsByAvgSpeed(activeStatsSource.value?.plot?.raw_times, activeStatsSource.value?.plot?.raw_speeds, 1, 8)
)

const formatIvLabel = (r) => {
  const o = { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }
  const a = new Date(r.startMs).toLocaleString('ru-RU', o)
  const b = new Date(r.endMs).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
  return `${a} — ${b}`
}

const pickPlotSeries = () => {
  const plot = activeStatsSource.value?.plot
  if (!plot) return { times: [], speeds: [] }
  let times = Array.from(plot.times || [])
  let speeds = Array.from(plot.speeds || [])
  if (!times.length && plot.raw_times?.length) {
    times = Array.from(plot.raw_times)
    speeds = Array.from(plot.raw_speeds || [])
  }
  return { times, speeds }
}

const initChart = () => {
  if (!chartCanvas.value) return
  if (chart) chart.destroy()

  const ctx = chartCanvas.value.getContext('2d')
  const { height } = chartHost.value?.getBoundingClientRect() || { height: 0 }
  const h = Math.max(height || chartCanvas.value.parentElement?.clientHeight || 240, 120)
  const gradient = ctx.createLinearGradient(0, 0, 0, h)
  gradient.addColorStop(0, 'rgba(37, 99, 235, 0.2)')
  gradient.addColorStop(1, 'rgba(37, 99, 235, 0)')

  const { times: tA, speeds: sA } = pickPlotSeries()
  const dataA = tA
    .map((t, i) => {
      const ms = new Date(t).getTime()
      return { x: ms, y: Number(sA[i]) }
    })
    .filter((p) => !Number.isNaN(p.x) && Number.isFinite(p.y))
  dataA.sort((a, b) => a.x - b.x)

  chart = new Chart(ctx, {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Скорость (км/ч)',
          data: dataA,
          borderColor: '#2563eb',
          borderWidth: 2,
          fill: true,
          backgroundColor: gradient,
          tension: 0.4,
          pointRadius: 0,
          pointHoverRadius: 6,
          parsing: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: {
        intersect: false,
        mode: 'index',
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: 'rgba(255, 255, 255, 0.95)',
          titleColor: '#1f2937',
          titleFont: { size: 12, weight: 'bold' },
          bodyColor: '#3b82f6',
          bodyFont: { size: 14, weight: '600' },
          borderColor: '#e5e7eb',
          borderWidth: 1,
          padding: 12,
          displayColors: false,
          callbacks: {
            title: (items) => {
              const v = items[0]?.parsed?.x
              const date = new Date(typeof v === 'number' ? v : v)
              return Number.isNaN(date.getTime())
                ? ''
                : date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
            },
            label: (item) => {
              return `Ср. скорость: ${item.parsed.y.toFixed(1)} км/ч`
            },
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          grid: { display: false },
          ticks: {
            color: '#9ca3af',
            font: { size: 10 },
            maxTicksLimit: 8,
            callback: (v) => {
              const d = new Date(v)
              return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
            },
          },
        },
        y: {
          beginAtZero: true,
          grid: { color: '#f3f4f6' },
          ticks: { color: '#9ca3af', font: { size: 10 } },
        },
      },
    },
  })
  requestAnimationFrame(() => {
    chart?.resize()
  })
}

const scheduleInitChart = () => {
  nextTick(() => initChart())
}

const plotWatchKey = () => {
  const p = activeStatsSource.value?.plot
  if (!p) return ''
  const t = p.times
  const s = p.speeds
  const rt = p.raw_times
  const rs = p.raw_speeds
  return [
    flowDirTab.value,
    t?.length ?? 0,
    s?.length ?? 0,
    t?.[0],
    t?.[(t?.length ?? 1) - 1],
    rt?.length ?? 0,
    rs?.length ?? 0,
  ].join('|')
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => {
    chart?.resize()
  })
  nextTick(() => {
    if (chartHost.value) resizeObserver.observe(chartHost.value)
    scheduleInitChart()
  })
})

watch(plotWatchKey, scheduleInitChart)

watch(flowDirTab, () => {
  nextTick(() => {
    requestAnimationFrame(() => chart?.resize())
  })
})

watch(dashboardTab, (tab) => {
  if (tab !== 'overview') return
  nextTick(() => {
    requestAnimationFrame(() => chart?.resize())
  })
})

onUnmounted(() => {
  if (resizeObserver) {
    resizeObserver.disconnect()
    resizeObserver = null
  }
  if (chart) {
    chart.destroy()
    chart = null
  }
})
</script>

<template>
  <div class="h-full flex flex-col min-h-0 gap-3 overflow-hidden">
    <div class="flex flex-wrap gap-1 border-b border-gray-100 pb-2 flex-shrink-0">
      <button
        type="button"
        :class="[
          'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors',
          dashboardTab === 'overview'
            ? 'bg-primary-600 text-white shadow-sm'
            : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
        ]"
        @click="dashboardTab = 'overview'"
      >
        <LayoutDashboard class="w-3.5 h-3.5" />
        Ключевые показатели
      </button>
      <button
        type="button"
        :class="[
          'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors',
          dashboardTab === 'intervals'
            ? 'bg-primary-600 text-white shadow-sm'
            : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
        ]"
        @click="dashboardTab = 'intervals'"
      >
        <Timer class="w-3.5 h-3.5" />
        Тяжёлые интервалы
      </button>
    </div>

    <div
      v-if="data?.bidirectional"
      class="flex flex-col gap-1.5 border-b border-gray-100 pb-2 flex-shrink-0"
    >
      <div class="flex flex-wrap items-center gap-1">
        <span class="text-[10px] text-gray-500 font-semibold uppercase shrink-0">Направление</span>
        <button
          type="button"
          :class="[
            'px-2.5 py-1 rounded-lg text-[11px] font-semibold transition-colors',
            flowDirTab === 'all'
              ? 'bg-slate-700 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
          ]"
          @click="flowDirTab = 'all'"
        >
          Все точки
        </button>
        <button
          v-for="dir in data.bidirectional.directions"
          :key="dir.id"
          type="button"
          :class="[
            'px-2.5 py-1 rounded-lg text-[11px] font-semibold transition-colors',
            flowDirTab === dir.id
              ? 'bg-primary-600 text-white'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
          ]"
          @click="flowDirTab = dir.id"
        >
          {{ dir.label }}
        </button>
      </div>
      <p v-if="flowDirTab === 'all'" class="text-[10px] text-gray-500 leading-snug">
        Опорный азимут сегментов: {{ Number(data.bidirectional.reference_bearing_deg).toFixed(1) }}°.
        Без направления (короткие шаги): {{ data.bidirectional.unclassified_count }}.
      </p>
    </div>

    <div v-show="dashboardTab === 'overview'" class="flex-1 min-h-0 flex flex-col md:flex-row gap-6 overflow-hidden">
      <div class="md:w-[min(22rem,100%)] grid grid-cols-2 md:grid-cols-1 gap-2 overflow-y-auto custom-scrollbar pr-1 flex-shrink-0">
        <div
          v-for="item in statsItems"
          :key="item.label"
          class="bg-gray-50 border border-gray-100 p-2.5 rounded-xl flex items-center gap-2.5"
        >
          <div :class="['p-2 rounded-lg', item.bg]">
            <component :is="item.icon" :class="['w-4 h-4', item.color]" />
          </div>
          <div class="min-w-0">
            <div class="text-[9px] text-gray-400 font-bold uppercase tracking-tighter truncate">{{ item.label }}</div>
            <div class="text-sm font-bold text-gray-800">{{ item.value }}</div>
          </div>
        </div>
      </div>

      <div class="flex-1 flex flex-col min-w-0 min-h-[200px]">
        <div class="flex items-center justify-between mb-2 flex-shrink-0">
          <h3 class="text-xs font-bold text-gray-700">Профиль скорости</h3>
        </div>
        <div
          ref="chartHost"
          class="relative flex-1 min-h-0 w-full min-h-[12rem] bg-gray-50/50 rounded-xl border border-gray-100 p-2"
        >
          <canvas ref="chartCanvas" class="absolute inset-0 block h-full w-full max-h-full"></canvas>
        </div>
      </div>
    </div>

    <div
      v-show="dashboardTab === 'intervals'"
      class="flex-1 min-h-0 overflow-y-auto custom-scrollbar space-y-4 pr-1"
    >
      <p class="text-[11px] text-gray-500 leading-snug">
        Часовые окна с <strong>наименьшей</strong> средней скоростью по сырым точкам ответа (после фильтров анализа).
      </p>

      <div class="rounded-xl border border-gray-100 overflow-hidden max-w-3xl">
        <div class="text-[10px] font-bold text-gray-500 uppercase px-3 py-2 bg-gray-50 border-b border-gray-100">
          Окна 1 час (худшие по ср. скорости)
        </div>
        <table v-if="intervals1h.length" class="w-full text-[11px]">
          <thead>
            <tr class="text-gray-500 border-b border-gray-100 text-left">
              <th class="py-1.5 px-2 font-medium">#</th>
              <th class="py-1.5 px-2 font-medium">Интервал</th>
              <th class="py-1.5 px-2 font-medium">Ср. км/ч</th>
              <th class="py-1.5 px-2 font-medium">Точек</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in intervals1h" :key="r.startMs" class="border-b border-gray-50">
              <td class="py-1 px-2 text-gray-400">{{ i + 1 }}</td>
              <td class="py-1 px-2 text-gray-800">{{ formatIvLabel(r) }}</td>
              <td class="py-1 px-2 font-semibold text-gray-900">{{ r.avgSpeed.toFixed(1) }}</td>
              <td class="py-1 px-2 text-gray-600">{{ r.count }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="text-xs text-gray-400 p-3">Недостаточно данных для часовых окон.</p>
      </div>
    </div>
  </div>
</template>
