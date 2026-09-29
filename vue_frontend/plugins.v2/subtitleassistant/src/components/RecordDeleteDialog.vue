<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'

import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import type { RecordDeleteMode, RecordListItem } from '@/types'
import StateChip from '@/components/StateChip.vue'
import { locationLabels, recordStates } from '@/types/presentation'

const props = withDefaults(defineProps<{
  modelValue: boolean
  record: RecordListItem | null
  loading?: boolean
  error?: string
}>(), {
  loading: false,
  error: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: [mode: RecordDeleteMode]
}>()

const titleId = `subtitleassistant-record-delete-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const mode = ref<RecordDeleteMode | null>(null)
const isMatched = computed(() => props.record?.status === 'matched')
const effectiveMode = computed<RecordDeleteMode | null>(() => props.record ? mode.value : null)
const currentFilePath = computed(() => props.record?.current_file_path || props.record?.path || '')
const confirmLabel = computed(() => {
  if (!effectiveMode.value) return '请选择删除范围'
  return effectiveMode.value === 'record_only'
    ? '删除记录，保留当前字幕文件'
    : '删除记录及当前字幕文件'
})

watch(() => props.modelValue, open => {
  if (!open) return
  captureFocus()
  mode.value = null
})

function close(): void {
  if (!props.loading) emit('update:modelValue', false)
}

function confirm(): void {
  if (effectiveMode.value) emit('confirm', effectiveMode.value)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    max-width="36rem"
    :persistent="loading"
    retain-focus
    :aria-labelledby="titleId"
    @update:model-value="emit('update:modelValue', $event)"
    @after-leave="restoreFocus"
  >
    <VCard v-if="record">
      <VCardTitle class="dialog-title">
        <VIcon icon="mdi-alert-outline" color="error" size="22" />
        <span :id="titleId">删除匹配记录</span>
      </VCardTitle>
      <VCardText class="dialog-content">
        <p class="impact-copy">
          将删除 <strong>{{ record.subtitle_file_name }}</strong> 的匹配记录。当前字幕文件位于{{ locationLabels[record.location] }}。
        </p>
        <dl class="record-facts">
          <div><dt>状态</dt><dd><StateChip :state="recordStates[record.status]" size="small" /></dd></div>
          <div><dt>当前字幕文件</dt><dd :title="currentFilePath">{{ currentFilePath }}</dd></div>
          <div><dt>版本</dt><dd>{{ record.updated_at }}</dd></div>
        </dl>

        <VRadioGroup v-if="record" v-model="mode" aria-label="选择删除范围" class="delete-options">
          <VRadio v-if="isMatched" value="record_only" label="删除记录，保留当前字幕文件" :disabled="loading" />
          <VRadio value="record_and_file" label="删除记录及当前字幕文件" :disabled="loading" />
        </VRadioGroup>
        <VAlert v-if="!isMatched" type="warning" variant="tonal" density="compact" class="file-only-notice">
          {{ record.status === 'staged' ? '暂存' : '未匹配' }}记录的字幕文件位于插件数据目录。为避免留下工作台未追踪的文件，只能选择“删除记录及当前字幕文件”。
        </VAlert>
        <p v-if="effectiveMode === 'record_only'" class="mode-warning">字幕文件会保留在原路径，但工作台删除记录后将不再追踪它。</p>
        <p v-else-if="effectiveMode === 'record_and_file'" class="mode-warning">文件不存在时按幂等成功处理；文件存在但删除失败不会降级为仅删除记录。</p>
        <VAlert v-if="error" type="error" variant="tonal" density="compact" class="mt-3">{{ error }}</VAlert>
      </VCardText>
      <VCardActions>
        <VSpacer />
        <VBtn variant="text" :disabled="loading" @click="close">取消</VBtn>
        <VBtn color="error" variant="flat" prepend-icon="mdi-delete-outline" :loading="loading" :disabled="!effectiveMode" @click="confirm">
          {{ confirmLabel }}
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.dialog-title { display: flex; align-items: center; gap: 0.625rem; font-size: 1rem; }
.dialog-content { padding-block-end: 0.5rem; }
.impact-copy { margin: 0 0 1rem; line-height: 1.6; }
.record-facts { display: grid; gap: 0.625rem; margin: 0 0 1rem; }
.record-facts div { display: grid; grid-template-columns: 7rem minmax(0, 1fr); gap: 0.75rem; align-items: start; }
.record-facts dt { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.record-facts dd { min-width: 0; margin: 0; overflow-wrap: anywhere; font-size: 0.8125rem; }
.delete-options { margin: 0; }
.mode-warning { margin: 0.75rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.file-only-notice { margin-block: 0.75rem; }
</style>
