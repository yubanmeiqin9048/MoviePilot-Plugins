<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useDisplay } from 'vuetify'

import { getErrorMessage, previewBatchRetargetRecords, retargetBatchRecords } from '@/api/client'
import TargetSelector from '@/components/TargetSelector.vue'
import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import {
  MAX_RECORD_BATCH_SIZE,
  type BatchRetargetPreviewItem,
  type BatchRetargetResponse,
  type PluginApi,
  type RecordListItem,
  type TargetItem,
} from '@/types'
import { mediaLabel, shortPath } from '@/types/presentation'

interface BatchRow {
  record: RecordListItem
  target: TargetItem | null
  preview: BatchRetargetPreviewItem | null
  completed: boolean
  error: string
  executionError: string
}

const props = defineProps<{
  modelValue: boolean
  api: PluginApi
  pluginId: string
  records: RecordListItem[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  complete: [result: BatchRetargetResponse]
  remove: [recordId: string]
}>()

const { smAndDown } = useDisplay()
const titleId = `subtitleassistant-batch-retarget-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const rows = ref<BatchRow[]>([])
const activeId = ref('')
const completedExpanded = ref(false)
const previewing = ref(false)
const saving = ref(false)
const generalError = ref('')
let previewRequest = 0

const pendingRows = computed(() => rows.value.filter(row => !row.completed))
const completedRows = computed(() => rows.value.filter(row => row.completed))
const completedCount = computed(() => completedRows.value.length)
const activeRow = computed(() => rows.value.find(row => row.record.id === activeId.value) || rows.value[0] || null)
const activeIndex = computed(() => {
  const index = rows.value.findIndex(row => row.record.id === activeRow.value?.record.id)
  return index < 0 ? 0 : index
})
const executable = computed(() => Boolean(
  !inputError.value
  && pendingRows.value.length
  && pendingRows.value.every(row => row.target && row.preview?.executable)
  && !previewing.value
  && !saving.value,
))
const canMovePrevious = computed(() => activeIndex.value > 0)
const canMoveNext = computed(() => activeIndex.value < rows.value.length - 1)
const inputError = computed(() => {
  if (!props.records.length) return '请至少选择一条匹配记录。'
  if (props.records.length > MAX_RECORD_BATCH_SIZE) return `一次最多改配 ${MAX_RECORD_BATCH_SIZE} 条记录，请缩小选择范围。`
  return ''
})

function rowError(row: BatchRow): string {
  if (row.executionError && row.error && row.executionError !== row.error) {
    return `${row.executionError}；重新预览：${row.error}`
  }
  return row.executionError || row.error
}

watch(() => props.modelValue, open => {
  if (!open) {
    previewRequest += 1
    return
  }
  captureFocus()
  rows.value = props.records.map(record => ({ record, target: null, preview: null, completed: false, error: '', executionError: '' }))
  activeId.value = rows.value[0]?.record.id || ''
  completedExpanded.value = false
  generalError.value = inputError.value
  if (inputError.value) return
  void previewAll()
})

async function previewAll(options: { preserveGeneralError?: boolean } = {}): Promise<void> {
  if (inputError.value) return
  const active = pendingRows.value
  if (!active.length) {
    previewing.value = false
    return
  }
  const requestId = ++previewRequest
  previewing.value = true
  if (!options.preserveGeneralError) generalError.value = ''
  try {
    const response = await previewBatchRetargetRecords(
      props.api,
      props.pluginId,
      active.map(row => ({ record_id: row.record.id, target_history_id: row.target?.history_id ?? null })),
    )
    if (requestId !== previewRequest) return
    const byId = new Map(response.items.map(item => [item.record_id, item]))
    rows.value = rows.value.map(row => {
      if (row.completed) return row
      const preview = byId.get(row.record.id) || null
      return {
        ...row,
        target: row.target || preview?.target || null,
        preview,
        error: preview?.message || '',
      }
    })
  } catch (requestError) {
    if (requestId === previewRequest) {
      const previewError = getErrorMessage(requestError, '批量改配预览失败')
      generalError.value = options.preserveGeneralError && generalError.value
        ? `${generalError.value}；${previewError}`
        : previewError
    }
  } finally {
    if (requestId === previewRequest) previewing.value = false
  }
}

function setActive(recordId: string): void {
  const row = rows.value.find(item => item.record.id === recordId)
  if (!row) return
  if (row.completed) completedExpanded.value = true
  activeId.value = recordId
}

function moveActive(delta: -1 | 1): void {
  const nextIndex = activeIndex.value + delta
  const next = rows.value[nextIndex]
  if (next) activeId.value = next.record.id
}

function updateTarget(recordId: string, target: TargetItem | null): void {
  rows.value = rows.value.map(row => row.record.id === recordId
    ? { ...row, target, preview: null, error: '', executionError: '' }
    : row)
  void previewAll()
}

function removeRow(row: BatchRow): void {
  if (saving.value || row.completed) return
  previewRequest += 1
  const wasActive = row.record.id === activeRow.value?.record.id
  rows.value = rows.value.filter(item => item.record.id !== row.record.id)
  emit('remove', row.record.id)
  if (!rows.value.length) {
    emit('update:modelValue', false)
    return
  }
  if (wasActive || !rows.value.some(item => item.record.id === activeId.value)) {
    const next = rows.value.find(item => !item.completed) || rows.value[0]
    activeId.value = next.record.id
  }
  void previewAll()
}

async function submit(): Promise<void> {
  if (!executable.value) return
  saving.value = true
  generalError.value = ''
  try {
    const result = await retargetBatchRecords(
      props.api,
      props.pluginId,
      pendingRows.value.map(row => ({
        record_id: row.record.id,
        target_history_id: row.target!.history_id,
      })),
    )
    const byId = new Map(result.items.map(item => [item.record_id, item]))
    rows.value = rows.value.map(row => {
      const item = byId.get(row.record.id)
      if (!item) return row
      return {
        ...row,
        completed: item.success,
        executionError: item.success ? '' : (item.message || '改配失败，请重试'),
        preview: item.success
          ? row.preview
          : { ...row.preview!, executable: false, error_code: item.error_code, message: item.message },
      }
    })
    emit('complete', result)
    if (result.failure_count === 0) {
      emit('update:modelValue', false)
      return
    }
    const firstFailure = rows.value.find(row => !row.completed)
    if (firstFailure) activeId.value = firstFailure.record.id
    await previewAll()
  } catch (requestError) {
    const submitError = getErrorMessage(requestError, '批量改配提交失败，请重新预览')
    generalError.value = submitError
    await previewAll({ preserveGeneralError: true })
  } finally {
    saving.value = false
  }
}

function close(): void {
  if (!saving.value) emit('update:modelValue', false)
}

function handleDialogUpdate(open: boolean): void {
  if (saving.value) return
  emit('update:modelValue', open)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="smAndDown"
    :persistent="saving"
    max-width="1120"
    scrollable
    retain-focus
    :aria-labelledby="titleId"
    @update:model-value="handleDialogUpdate"
    @after-leave="restoreFocus"
  >
    <VCard class="batch-card">
      <VCardTitle class="dialog-title">
        <div>
          <span :id="titleId">批量改配目标</span>
          <small>本次批次 {{ rows.length || records.length }} 条记录；每次只编辑当前记录，提交仍一次处理整批。</small>
        </div>
        <VBtn icon="mdi-close" variant="text" aria-label="关闭批量改配目标" :disabled="saving" @click="close" />
      </VCardTitle>

      <VCardText class="dialog-content">
        <VAlert v-if="generalError" type="error" variant="tonal" density="compact" class="dialog-alert">{{ generalError }}</VAlert>
        <VAlert v-if="completedCount" type="info" variant="tonal" density="compact" class="dialog-alert">
          已完成 {{ completedCount }} 条；成功项已折叠，提交按钮只会重试剩余失败项。
        </VAlert>

        <div v-if="rows.length" class="editor-shell">
          <aside class="row-list" aria-label="批量改配记录清单">
            <div class="row-list-heading">记录清单 <span>{{ rows.length }} 条</span></div>
            <button v-if="completedCount" type="button" class="completed-summary" :aria-expanded="completedExpanded" @click="completedExpanded = !completedExpanded">
              <VIcon :icon="completedExpanded ? 'mdi-chevron-down' : 'mdi-chevron-right'" size="18" />
              <span>已完成 {{ completedCount }} 条</span>
            </button>
            <template v-for="row in rows" :key="row.record.id">
              <div
                v-if="!row.completed || completedExpanded || row.record.id === activeRow?.record.id"
                class="row-nav"
                :class="{ 'row-nav--active': row.record.id === activeRow?.record.id, 'row-nav--complete': row.completed }"
              >
                <button
                  type="button"
                  class="row-nav__select"
                  :aria-current="row.record.id === activeRow?.record.id ? 'true' : undefined"
                  @click="setActive(row.record.id)"
                >
                  <VIcon :icon="row.completed ? 'mdi-check-circle' : (rowError(row) ? 'mdi-alert-circle-outline' : 'mdi-file-document-outline')" :color="row.completed ? 'success' : (rowError(row) ? 'error' : undefined)" size="18" />
                  <span class="row-nav__text">
                    <strong>{{ row.record.subtitle_file_name }}</strong>
                    <small>{{ mediaLabel(row.record.media_title, row.record.year, row.record.season, row.record.episode) }}</small>
                    <small v-if="row.completed" class="row-nav__status">已完成</small>
                    <small v-else-if="rowError(row)" class="row-nav__status row-nav__status--error">需处理</small>
                  </span>
                </button>
                <VBtn
                  v-if="!row.completed"
                  icon="mdi-close"
                  size="x-small"
                  variant="text"
                  color="error"
                  aria-label="移出本次批量改配"
                  :disabled="saving"
                  @click="removeRow(row)"
                />
              </div>
            </template>
          </aside>

          <section v-if="activeRow" class="editor-pane" aria-label="当前记录改配编辑器">
            <header class="editor-heading">
              <div class="editor-heading__identity">
                <strong>{{ activeRow.record.subtitle_file_name }}</strong>
                <span>{{ mediaLabel(activeRow.record.media_title, activeRow.record.year, activeRow.record.season, activeRow.record.episode) }} · {{ activeRow.record.format }}</span>
              </div>
              <div class="editor-heading__actions">
                <VBtn icon="mdi-chevron-left" size="small" variant="text" :disabled="!canMovePrevious" aria-label="上一条记录" @click="moveActive(-1)" />
                <span class="editor-counter">第 {{ activeIndex + 1 }} / {{ rows.length }} 条</span>
                <VBtn icon="mdi-chevron-right" size="small" variant="text" :disabled="!canMoveNext" aria-label="下一条记录" @click="moveActive(1)" />
                <VMenu location="bottom end">
                  <template #activator="{ props: menuProps }">
                    <VBtn v-bind="menuProps" icon="mdi-format-list-bulleted" size="small" variant="text" aria-label="打开记录清单" />
                  </template>
                  <VList density="compact" max-height="min(60dvh, 24rem)" role="menu" aria-label="批量改配记录清单">
                    <VListItem
                      v-for="row in rows"
                      :key="row.record.id"
                      :active="row.record.id === activeRow.record.id"
                      :title="row.record.subtitle_file_name"
                      :subtitle="row.completed ? '已完成' : (rowError(row) ? '需处理' : '待处理')"
                      role="menuitemradio"
                      :aria-checked="row.record.id === activeRow.record.id"
                      tabindex="0"
                      @click="setActive(row.record.id)"
                      @keydown.enter.prevent="setActive(row.record.id)"
                      @keydown.space.prevent="setActive(row.record.id)"
                    >
                      <template #prepend><VIcon :icon="row.completed ? 'mdi-check-circle' : (rowError(row) ? 'mdi-alert-circle-outline' : 'mdi-file-document-outline')" :color="row.completed ? 'success' : (rowError(row) ? 'error' : undefined)" size="18" /></template>
                    </VListItem>
                  </VList>
                </VMenu>
                <VBtn v-if="!activeRow.completed" icon="mdi-close-circle-outline" size="small" variant="text" color="error" :disabled="saving" aria-label="移出当前记录" @click="removeRow(activeRow)" />
              </div>
            </header>

            <div class="preview-panel">
              <dl class="path-preview">
                <div>
                  <dt>当前字幕</dt>
                  <dd :title="activeRow.preview?.current_subtitle_path || activeRow.record.path">{{ shortPath(activeRow.preview?.current_subtitle_path || activeRow.record.path) }}</dd>
                </div>
                <div>
                  <dt>当前目标</dt>
                  <dd v-if="activeRow.record.target_path" :title="activeRow.record.target_path">{{ shortPath(activeRow.record.target_path) }}</dd>
                  <dd v-else class="muted">未关联整理目标</dd>
                </div>
                <div>
                  <dt>新目标</dt>
                  <dd v-if="activeRow.preview?.preview" :title="activeRow.preview.preview.target_path">{{ shortPath(activeRow.preview.preview.target_path) }}</dd>
                  <dd v-else class="muted">选择目标后显示映射路径</dd>
                </div>
                <div>
                  <dt>最终字幕</dt>
                  <dd v-if="activeRow.preview?.preview" :title="activeRow.preview.preview.final_subtitle_path">{{ shortPath(activeRow.preview.preview.final_subtitle_path) }}</dd>
                  <dd v-else class="muted">选择目标后显示预计字幕路径</dd>
                </div>
              </dl>
              <VAlert v-if="rowError(activeRow)" type="warning" variant="tonal" density="compact" class="mt-3">{{ rowError(activeRow) }}</VAlert>
              <VAlert v-else-if="activeRow.preview?.executable" type="success" variant="tonal" density="compact" class="mt-3" icon="mdi-folder-check-outline">可以执行</VAlert>
              <VAlert v-else-if="activeRow.completed" type="success" variant="tonal" density="compact" class="mt-3" icon="mdi-check-circle-outline">已完成；此项仅供查看。</VAlert>
            </div>

            <TargetSelector
              :model-value="activeRow.target"
              :api="api"
              :plugin-id="pluginId"
              label="整理历史目标"
              hint="系统仅按精确媒体身份和季集自动建议；可以手动修改。"
              :disabled="saving || activeRow.completed"
              compact
              @update:model-value="updateTarget(activeRow.record.id, $event)"
            />
          </section>
        </div>
        <VAlert v-else type="info" variant="tonal">没有待处理的记录；本次批量改配不会发送空请求。</VAlert>
      </VCardText>

      <VCardActions class="dialog-actions">
        <span class="action-summary">{{ executable ? `${pendingRows.length} 条待执行` : (completedCount ? '仅剩已完成结果' : '请先处理所有预检问题') }}</span>
        <VSpacer />
        <VBtn variant="text" :disabled="saving" @click="close">关闭</VBtn>
        <VBtn color="primary" :loading="saving" :disabled="!executable" prepend-icon="mdi-swap-horizontal-bold" @click="submit">
          {{ completedCount ? '仅重试失败项' : '确认批量改配' }}
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.batch-card { display: flex; max-block-size: min(90dvh, 56rem); flex-direction: column; overflow: hidden; }
.dialog-title { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); white-space: normal; }
.dialog-title > div { min-width: 0; flex: 1 1 auto; }
.dialog-title > :deep(.v-btn) { flex: 0 0 auto; }
.dialog-title span, .dialog-title small { display: block; }
.dialog-title span { font-size: 1rem; font-weight: 650; }
.dialog-title small { margin-top: 0.25rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; }
.dialog-content { display: flex; min-block-size: 0; flex: 1 1 auto; flex-direction: column; overflow: hidden !important; padding: 1rem 1.25rem; }
.dialog-alert { flex: 0 0 auto; margin-block-end: 0.75rem; }
.editor-shell { display: grid; min-block-size: 0; flex: 1 1 auto; grid-template-columns: minmax(15rem, 0.34fr) minmax(0, 0.66fr); gap: 1rem; }
.row-list, .editor-pane { min-block-size: 0; overflow: auto; overscroll-behavior: contain; }
.row-list { border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.5); }
.row-list-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; padding: 0.75rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); font-size: 0.8125rem; font-weight: 650; }
.row-list-heading span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; }
.completed-summary { display: flex; width: 100%; align-items: center; gap: 0.35rem; padding: 0.6rem 0.75rem; border: 0; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); text-align: start; background: rgba(var(--v-theme-primary), 0.04); cursor: pointer; font-size: 0.75rem; }
.completed-summary:hover, .completed-summary:focus-visible { background: rgba(var(--v-theme-primary), 0.08); }
.completed-summary:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.row-nav { display: flex; width: 100%; min-block-size: 4.5rem; align-items: flex-start; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); color: inherit; background: transparent; }
.row-nav:last-child { border-bottom: 0; }
.row-nav:hover, .row-nav--active { background: rgba(var(--v-theme-primary), 0.08); }
.row-nav--complete { opacity: 0.68; }
.row-nav__select { display: flex; min-width: 0; min-block-size: 4.5rem; flex: 1 1 auto; align-items: flex-start; gap: 0.5rem; padding: 0.7rem 0.35rem 0.7rem 0.6rem; border: 0; color: inherit; text-align: start; background: transparent; cursor: pointer; }
.row-nav__select:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.row-nav > :deep(.v-btn) { flex: 0 0 auto; margin: 0.45rem 0.35rem 0 0; }
.row-nav__text { display: grid; min-width: 0; flex: 1 1 auto; gap: 0.15rem; }
.row-nav__text strong, .row-nav__text small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-nav__text strong { font-size: 0.8rem; }
.row-nav__text small { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.7rem; }
.row-nav__status { color: rgb(var(--v-theme-success)) !important; }
.row-nav__status--error { color: rgb(var(--v-theme-error)) !important; }
.editor-pane { padding-inline-end: 0.25rem; }
.editor-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 0.75rem; margin-block-end: 0.75rem; padding-block-end: 0.75rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.editor-heading__identity { display: grid; min-width: 0; gap: 0.15rem; }
.editor-heading__identity strong, .editor-heading__identity span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.editor-heading__identity strong { font-size: 0.9rem; }
.editor-heading__identity span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.editor-heading__actions { display: flex; flex: 0 0 auto; align-items: center; gap: 0.1rem; }
.editor-counter { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.72rem; white-space: nowrap; }
.preview-panel { margin-block-end: 1rem; padding: 0.875rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.path-preview { display: grid; gap: 0.625rem; margin: 0; }
.path-preview div { min-width: 0; }
.path-preview div + div { padding-top: 0.625rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.path-preview dt { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 600; }
.path-preview dd { margin: 0.25rem 0 0; overflow-wrap: anywhere; font-size: 0.8125rem; }
.muted, .action-summary { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
.dialog-actions { flex: 0 0 auto; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.action-summary { font-size: 0.75rem; }
@media (max-width: 59.99rem) { .editor-shell { grid-template-columns: 1fr; } .row-list { display: none; } }
@media (max-width: 37.5rem) {
  .batch-card { max-block-size: 100dvh; }
  .dialog-title, .dialog-actions { padding-inline: 1rem; }
  .dialog-content { padding: 0.875rem 1rem; }
  .editor-heading { align-items: flex-start; }
  .editor-heading__actions { flex-wrap: wrap; justify-content: flex-end; }
  .action-summary { display: none; }
}
</style>
