<script setup lang="ts">
import { ref, useId, watch } from 'vue'
import { useDisplay } from 'vuetify'

import TargetSelector from '@/components/TargetSelector.vue'
import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import type { PluginApi, TargetItem } from '@/types'
import { mediaLabel } from '@/types/presentation'

const props = defineProps<{
  modelValue: boolean
  api: PluginApi
  pluginId: string
  current: TargetItem | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  select: [target: TargetItem]
}>()

const { smAndDown } = useDisplay()
const titleId = `subtitleassistant-target-picker-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const draft = ref<TargetItem | null>(null)

watch(() => props.modelValue, open => {
  if (!open) return
  captureFocus()
  draft.value = props.current
})

function confirm(): void {
  if (!draft.value) return
  emit('select', draft.value)
  emit('update:modelValue', false)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="smAndDown"
    max-width="900"
    scrollable
    retain-focus
    :aria-labelledby="titleId"
    @update:model-value="emit('update:modelValue', $event)"
    @after-leave="restoreFocus"
  >
    <VCard class="picker-card">
      <VCardTitle class="picker-title">
        <div>
          <span :id="titleId">选择整理历史目标</span>
          <small>确定目标后即可按来源关键词搜索字幕。</small>
        </div>
        <VBtn icon="mdi-close" variant="text" aria-label="关闭目标选择" @click="emit('update:modelValue', false)" />
      </VCardTitle>

      <VCardText class="picker-content">
        <TargetSelector
          v-model="draft"
          :api="api"
          :plugin-id="pluginId"
          label="搜索整理历史"
          hint="仅列出 MoviePilot 成功的本地单文件整理历史；目标视频当前可以不存在。"
          compact
        />
      </VCardText>

      <VCardActions class="picker-actions">
        <span class="picker-choice">
          {{ draft ? `已选：${mediaLabel(draft.media_title, draft.year, draft.season, draft.episode)}` : '尚未选择目标' }}
        </span>
        <VSpacer />
        <VBtn variant="text" @click="emit('update:modelValue', false)">取消</VBtn>
        <VBtn color="primary" prepend-icon="mdi-check" :disabled="!draft" @click="confirm">使用该目标</VBtn>
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
.picker-title small { margin-top: 0.25rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; }
.picker-content { flex: 1 1 auto; padding: 1rem 1.25rem; }
.picker-actions { flex: 0 0 auto; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.picker-choice { min-width: 0; overflow: hidden; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }
@media (max-width: 37.5rem) {
  .picker-card { max-block-size: 100dvh; }
  .picker-title, .picker-actions { padding-inline: 1rem; }
  .picker-content { padding: 0.875rem 1rem; }
  .picker-choice { display: none; }
}
</style>
