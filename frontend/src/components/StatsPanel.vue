<script setup>
import { computed } from 'vue'
import { TrendingUp, MapPin, AlertCircle, Zap, ShieldAlert } from 'lucide-vue-next'

const props = defineProps(['stats'])

const items = computed(() => {
  if (!props.stats) return []
  const s = props.stats.statistics || {}
  return [
    { 
      label: 'Ср. скорость', 
      value: (props.stats.avg_speed || 0).toFixed(1) + ' км/ч', 
      icon: TrendingUp,
      color: 'text-blue-600',
      bg: 'bg-blue-50'
    },
    { 
      label: 'Всего точек', 
      value: props.stats.count || 0, 
      icon: MapPin,
      color: 'text-green-600',
      bg: 'bg-green-50'
    },
    { 
      label: 'Зон заторов', 
      value: props.stats.congestion_zones?.length || 0, 
      icon: ShieldAlert,
      color: 'text-red-600',
      bg: 'bg-red-50'
    },
    { 
      label: 'Медиана', 
      value: (s.median || 0).toFixed(1) + ' км/ч', 
      icon: Zap,
      color: 'text-purple-600',
      bg: 'bg-purple-50'
    },
  ]
})
</script>

<template>
  <div class="flex flex-col gap-3">
    <div 
      v-for="item in items" 
      :key="item.label"
      class="bg-white/90 backdrop-blur-md border border-white p-4 rounded-2xl shadow-xl flex items-center gap-4 w-56 animate-in fade-in slide-in-from-right-8"
    >
      <div :class="['p-3 rounded-xl', item.bg]">
        <component :is="item.icon" :class="['w-6 h-6', item.color]" />
      </div>
      <div>
        <div class="text-[10px] font-bold text-gray-400 uppercase tracking-wider">{{ item.label }}</div>
        <div class="text-xl font-bold text-gray-800">{{ item.value }}</div>
      </div>
    </div>
  </div>
</template>
