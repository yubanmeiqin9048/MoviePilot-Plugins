<script setup lang="ts">
import { onMounted } from 'vue'

import type { PluginApi } from '@/types'

defineProps<{
  api: PluginApi
  pluginId?: string
}>()

const emit = defineEmits<{
  layout: [layout: { maxWidth: string }]
}>()

onMounted(() => emit('layout', { maxWidth: '60rem' }))
</script>

<template>
  <VCard flat>
    <VCardTitle>Alist2Strm</VCardTitle>
    <VCardText>
      <VAlert type="info" variant="tonal">
        运行与历史页面将在扫描功能交付后启用。本页不会在打开或保存配置时启动扫描。
      </VAlert>
    </VCardText>
  </VCard>
</template>
