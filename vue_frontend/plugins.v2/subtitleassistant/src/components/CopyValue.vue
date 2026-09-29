<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  value?: string | null
  display?: string
  label?: string
}>()

const feedback = ref('复制')

async function copyValue(): Promise<void> {
  if (!props.value) return
  try {
    await navigator.clipboard.writeText(props.value)
    feedback.value = '已复制'
  } catch {
    feedback.value = '复制失败'
  }
  window.setTimeout(() => {
    feedback.value = '复制'
  }, 1800)
}
</script>

<template>
  <div class="copy-value">
    <span class="copy-value__text" :title="value || undefined">{{ display || value || '未记录' }}</span>
    <VTooltip v-if="value" :text="`${feedback}${label ? ` ${label}` : ''}`">
      <template #activator="{ props: tooltipProps }">
        <VBtn
          v-bind="tooltipProps"
          icon="mdi-content-copy"
          size="x-small"
          variant="text"
          :aria-label="`复制${label || '内容'}`"
          @click="copyValue"
        />
      </template>
    </VTooltip>
  </div>
</template>

<style scoped>
.copy-value {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 0.25rem;
}

.copy-value__text {
  min-width: 0;
  overflow-wrap: anywhere;
  color: rgb(var(--v-theme-on-surface));
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 0.8125rem;
  line-height: 1.5;
}
</style>
