<script setup>
import { ref, provide } from 'vue'
import Sidebar from './components/Sidebar.vue'
import MainMap from './components/MainMap.vue'
import ChartSection from './components/ChartSection.vue'
import StatsPanel from './components/StatsPanel.vue'

const isSidebarOpen = ref(true)
const analysisResult = ref(null)
const isLoading = ref(false)
/** GeoJSON Polygon | MultiPolygon | null — область анализа на карте (WGS84) */
const analysisAreaGeometry = ref(null)

// Provide shared state/actions
provide('analysisResult', analysisResult)
provide('isLoading', isLoading)
provide('analysisAreaGeometry', analysisAreaGeometry)

const toggleSidebar = () => {
  isSidebarOpen.value = !isSidebarOpen.value
}

const handleAnalysisComplete = (data) => {
  analysisResult.value = data
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
  <div class="flex h-screen bg-gray-100 overflow-hidden font-sans">
    <!-- Sidebar -->
    <Sidebar 
      :isOpen="isSidebarOpen" 
      @toggle="toggleSidebar"
      @analysis-complete="handleAnalysisComplete"
    />

    <!-- Main Content Area -->
    <main 
      class="flex-1 flex flex-col relative transition-all duration-300 ease-in-out overflow-hidden h-screen"
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
