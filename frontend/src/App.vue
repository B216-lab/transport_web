<script setup>
import { ref, shallowRef, provide } from 'vue'
import Sidebar from './components/Sidebar.vue'
import MainMap from './components/MainMap.vue'
import ChartSection from './components/ChartSection.vue'
import ParserTab from './components/ParserTab.vue'
import AccessibilityTab from './components/AccessibilityTab.vue'

const isSidebarOpen = ref(true)
const accessibilitySidebarOpen = ref(true)
const analysisResult = shallowRef(null)
const isLoading = ref(false)
const activeTab = ref('analysis')
/** GeoJSON Polygon | MultiPolygon | null — область анализа на карте (WGS84) */
const analysisAreaGeometry = ref(null)
/** [lon, lat] — точка для изохрон */
const isochroneOrigin = ref(null)
const isochroneResult = shallowRef(null)
const isochronePickMode = ref(false)
const showPedestrianGraph = ref(false)
// Provide shared state/actions
provide('analysisResult', analysisResult)
provide('isLoading', isLoading)
provide('analysisAreaGeometry', analysisAreaGeometry)
provide('isochroneOrigin', isochroneOrigin)
provide('isochroneResult', isochroneResult)
provide('isochronePickMode', isochronePickMode)
provide('showPedestrianGraph', showPedestrianGraph)
const toggleSidebar = () => {
  isSidebarOpen.value = !isSidebarOpen.value
}

const handleAnalysisComplete = (data) => {
  analysisResult.value = data
}

const setTab = (tab) => {
  activeTab.value = tab
}

const chartHeight = ref(320)
const isResizing = ref(false)

const startResize = () => {
  isResizing.value = true
  window.addEventListener('mousemove', doResize)
  window.addEventListener('mouseup', stopResize)
  document.body.style.userSelect = 'none'
  document.body.style.cursor = 'row-resize'
}

const doResize = (e) => {
  if (!isResizing.value) return
  const newHeight = window.innerHeight - e.clientY
  // Min 150px, Max 80% of screen
  if (newHeight >= 150 && newHeight <= window.innerHeight * 0.8) {
    chartHeight.value = newHeight
  }
}

const stopResize = () => {
  isResizing.value = false
  window.removeEventListener('mousemove', doResize)
  window.removeEventListener('mouseup', stopResize)
  document.body.style.userSelect = ''
  document.body.style.cursor = ''
}
</script>

<template>
  <div class="flex flex-col h-screen bg-gray-100 overflow-hidden font-sans">
    <header class="h-14 bg-white border-b border-gray-200 flex items-center px-4 gap-2">
      <button
        @click="setTab('analysis')"
        :class="[
          'px-4 py-2 rounded-lg text-sm font-semibold transition-colors',
          activeTab === 'analysis' ? 'bg-primary-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
        ]"
      >
        Анализ
      </button>
      <button
        @click="setTab('accessibility')"
        :class="[
          'px-4 py-2 rounded-lg text-sm font-semibold transition-colors',
          activeTab === 'accessibility' ? 'bg-primary-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
        ]"
      >
        Доступность
      </button>
      <button
        @click="setTab('parser')"
        :class="[
          'px-4 py-2 rounded-lg text-sm font-semibold transition-colors',
          activeTab === 'parser' ? 'bg-primary-600 text-white' : 'bg-gray-100 text-gray-700 hover:bg-gray-200',
        ]"
      >
        Парсер
      </button>
    </header>

    <div v-if="activeTab === 'analysis'" class="flex flex-1 overflow-hidden">
      <!-- Sidebar -->
      <Sidebar 
        :isOpen="isSidebarOpen" 
        @toggle="toggleSidebar"
        @analysis-complete="handleAnalysisComplete"
      />

      <!-- Main Content Area -->
      <main 
        class="flex-1 flex flex-col relative transition-all duration-300 ease-in-out overflow-hidden h-full"
      >
        <!-- Map Area -->
        <div class="grow relative min-h-0 bg-gray-100 flex flex-col">
          <MainMap :data="analysisResult" :height="chartHeight" class="flex-1" />
        </div>

        <!-- Resizer Handle -->
        <div 
          v-if="analysisResult"
          @mousedown="startResize"
          class="h-1.5 w-full bg-gray-200 hover:bg-primary-500 cursor-row-resize transition-colors z-[1001] flex items-center justify-center group"
        >
          <div class="w-12 h-1 bg-gray-400 group-hover:bg-white rounded-full opacity-50 group-hover:opacity-100 transition-all"></div>
        </div>

        <!-- Bottom Chart & Stats Section -->
        <div 
          v-if="analysisResult" 
          :style="{ height: chartHeight + 'px' }"
          class="flex-shrink-0 bg-white border-t border-gray-100 p-4 relative"
        >
          <ChartSection :data="analysisResult" />
        </div>
      </main>
    </div>

    <div v-else-if="activeTab === 'accessibility'" class="flex flex-1 overflow-hidden">
      <AccessibilityTab
        :isOpen="accessibilitySidebarOpen"
        @toggle="accessibilitySidebarOpen = !accessibilitySidebarOpen"
      />
      <main class="flex-1 relative min-h-0 bg-gray-100">
        <MainMap :data="null" class="w-full h-full" />
      </main>
    </div>

    <div v-else class="flex-1 overflow-hidden">
      <ParserTab />
    </div>
  </div>
</template>

<style>
/* Global Leaflet Adjustments */
.leaflet-control-zoom {
  border: none !important;
  box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1) !important;
}

.leaflet-bar a {
  background-color: white !important;
  color: #374151 !important;
  border-bottom: 1px solid #f3f4f6 !important;
}
</style>
