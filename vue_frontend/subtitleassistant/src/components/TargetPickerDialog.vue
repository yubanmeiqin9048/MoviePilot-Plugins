<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import { useDisplay } from 'vuetify'

import TargetSelector from '@/components/TargetSelector.vue'
import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import type { HistoryRow, PluginApi } from '@/types'
import { historyId, historyLabel } from '@/types/presentation'

const props = defineProps<{
  modelValue: boolean
  api: PluginApi
  pluginId: string
  current: HistoryRow | null
  /** 已有搜索结果或自定义关键词时，确认选择会清空它们。 */
  clearsSearchState?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  select: [target: HistoryRow]
}>()

const { smAndDown } = useDisplay()
const titleId = `subtitleassistant-target-picker-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const draft = ref<HistoryRow | null>(null)

watch(() => props.modelValue, open => {
  if (!open) return
  captureFocus()
  draft.value = props.current
})

function confirm(): void {
  const selected = draft.value
  if (!selected || historyId(selected) == null) return
  emit('select', selected)
  emit('update:modelValue', false)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="smAndDown"
    max-width="60rem"
    scrollable
    retain-focus
    :aria-labelledby="titleId"
    @update:model-value="emit('update:modelValue', $event)"
    @after-leave="restoreFocus"
  >
    <VCard class="picker-card">
      <VCardTitle class="picker-title">
        <div>
          <span :id="titleId">选择 MoviePilot 整理历史</span>
          <small>按 MoviePilot 原始顺序浏览当前整理历史页；选中后才验证是否可用于字幕操作。</small>
        </div>
        <VBtn
          icon="mdi-close"
          variant="text"
          aria-label="关闭整理历史选择"
          @click="emit('update:modelValue', false)"
        />
      </VCardTitle>

      <VCardText class="picker-content">
        <VAlert
          v-if="clearsSearchState"
          type="warning"
          variant="tonal"
          density="compact"
          class="picker-alert"
        >
          确认选择后会清空当前搜索结果和自定义关键词。
        </VAlert>
        <TargetSelector
          v-model="draft"
          :api="api"
          :plugin-id="pluginId"
          :show-heading="false"
          searchable
          compact
          fill-height
        />
      </VCardText>

      <VCardActions class="picker-actions">
        <span class="picker-choice" role="status" aria-live="polite">
          {{ draft ? `已选：${historyLabel(draft)}` : '尚未选择整理历史' }}
        </span>
        <VSpacer />
        <VBtn type="button" variant="text" @click="emit('update:modelValue', false)">取消</VBtn>
        <VBtn
          type="button"
          color="primary"
          variant="flat"
          prepend-icon="mdi-check"
          :disabled="!draft"
          @click="confirm"
        >
          使用该目标
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.picker-card { display: flex; max-block-size: min(90dvh, 52rem); flex-direction: column; overflow: hidden; }
.picker-title { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); white-space: normal; }
.picker-title > div { min-width: 0; flex: 1 1 auto; }
.picker-title > :deep(.v-btn) { flex: 0 0 auto; }
.picker-title span, .picker-title small { display: block; }
.picker-title span { font-size: 1rem; font-weight: 650; }
.picker-title small { margin-top: 0.25rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; line-height: 1.5; }
.picker-content { display: flex; min-block-size: 0; flex: 1 1 auto; flex-direction: column; overflow: hidden !important; padding: 1rem 1.25rem; }
.picker-alert { flex: 0 0 auto; margin-bottom: 0.75rem; }
.picker-actions { flex: 0 0 auto; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.picker-choice { min-width: 0; overflow: hidden; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 37.5rem) {
  .picker-card { max-block-size: 100dvh; }
  .picker-title, .picker-actions { padding-inline: 1rem; }
  .picker-content { padding: 0.875rem 1rem; }
  .picker-choice { display: none; }
}
</style>
