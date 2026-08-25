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
  type HistoryRow,
  type PluginApi,
  type RecordListItem,
  type TargetItem,
} from '@/types'
import { historyId, historyLabel, mediaLabel, shortPath } from '@/types/presentation'

interface BatchRow {
  record: RecordListItem
  target: HistoryRow | TargetItem | null
  preview: BatchRetargetPreviewItem | null
  completed: boolean
  error: string
  executionError: string
}

type SortKey = 'subtitle' | 'source' | 'target' | 'destination'

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

const SORT_COLUMNS: Array<{ key: SortKey; label: string }> = [
  { key: 'subtitle', label: '字幕文件' },
  { key: 'source', label: '来源媒体' },
  { key: 'target', label: '改配到' },
  { key: 'destination', label: '改配后字幕' },
]

/** 中文按拼音、数字按数值，否则「第 10 集」会排在「第 2 集」前面。 */
const collator = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' })

const { smAndDown } = useDisplay()
const titleId = `subtitleassistant-batch-retarget-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const rows = ref<BatchRow[]>([])
const showCompleted = ref(false)
const previewing = ref(false)
const saving = ref(false)
const generalError = ref('')
const sortKey = ref<SortKey | null>(null)
const sortDirection = ref<'asc' | 'desc'>('asc')
const openPickerId = ref('')
/**
 * 「待处理优先」只在打开批次和提交之后各取一次快照。
 * 若每次改目标都重排，刚修好的行会立刻跳出待处理档，用户会丢失当前位置。
 */
const triageOrder = ref<string[]>([])
let previewRequest = 0

const pendingRows = computed(() => rows.value.filter(row => !row.completed))
const completedRows = computed(() => rows.value.filter(row => row.completed))
const completedCount = computed(() => completedRows.value.length)
const executable = computed(() => Boolean(
  !inputError.value
  && pendingRows.value.length
  && pendingRows.value.every(row => row.target && historyId(row.target) != null && row.preview?.executable)
  && !previewing.value
  && !saving.value,
))
const inputError = computed(() => {
  if (!props.records.length) return '请至少选择一条匹配记录。'
  if (props.records.length > MAX_RECORD_BATCH_SIZE) return `一次最多改配 ${MAX_RECORD_BATCH_SIZE} 条记录，请缩小选择范围。`
  return ''
})
const blockedCount = computed(() => pendingRows.value.filter(row => !isReady(row)).length)
const visibleRows = computed(() => showCompleted.value ? sortedRows.value : sortedRows.value.filter(row => !row.completed))

/** 多条记录写入同一目录时把公共前缀提到表头写一次，行内只留差异部分。 */
const commonDirectory = computed(() => {
  const directories = rows.value
    .map(row => row.preview?.preview?.final_subtitle_path)
    .filter((path): path is string => Boolean(path))
    .map(path => path.slice(0, Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'))))
  if (directories.length < 2) return ''
  let prefix = directories[0]
  for (const directory of directories.slice(1)) {
    while (prefix && !directory.startsWith(prefix)) {
      prefix = prefix.slice(0, Math.max(prefix.lastIndexOf('/'), prefix.lastIndexOf('\\')))
    }
    if (!prefix) return ''
  }
  return prefix
})

/**
 * 未选择排序时按「待处理优先」排：缺目标或预检不通过的在最前，已完成的在最后。
 * 每档内部保持批次原始顺序，也就是用户在匹配记录列表里的选择顺序。
 */
const sortedRows = computed(() => {
  if (!sortKey.value) {
    const snapshot = triageOrder.value
    return rows.value
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const leftRank = snapshot.indexOf(left.row.record.id)
        const rightRank = snapshot.indexOf(right.row.record.id)
        // 快照里没有的行（尚未预检完）保持批次原始顺序，排在已定位行之后。
        if (leftRank < 0 && rightRank < 0) return left.index - right.index
        if (leftRank < 0) return 1
        if (rightRank < 0) return -1
        return leftRank - rightRank
      })
      .map(entry => entry.row)
  }
  const factor = sortDirection.value === 'asc' ? 1 : -1
  return [...rows.value].sort((left, right) => {
    const leftText = sortText(left)
    const rightText = sortText(right)
    // 空值恒排末尾且不随方向翻转，避免降序把待办项藏到列表底部。
    if (!leftText && rightText) return 1
    if (leftText && !rightText) return -1
    return factor * collator.compare(leftText, rightText)
  })
})

function isReady(row: BatchRow): boolean {
  return Boolean(row.target && historyId(row.target) != null && row.preview?.executable && !rowError(row))
}

/** 重排「待处理优先」快照：缺目标或预检不通过的在前，已完成的在后，档内保持原顺序。 */
function captureTriageOrder(): void {
  triageOrder.value = rows.value
    .map((row, index) => ({ id: row.record.id, rank: triageRank(row), index }))
    .sort((left, right) => left.rank - right.rank || left.index - right.index)
    .map(entry => entry.id)
}

function triageRank(row: BatchRow): number {
  if (row.completed) return 3
  if (!row.target || historyId(row.target) == null) return 0
  if (!isReady(row)) return 1
  return 2
}

function sortText(row: BatchRow): string {
  if (sortKey.value === 'subtitle') return row.record.subtitle_file_name || ''
  if (sortKey.value === 'source') return sourceLabel(row)
  if (sortKey.value === 'target') return row.target ? historyLabel(row.target) : ''
  return row.preview?.preview?.final_subtitle_path || ''
}

function sourceLabel(row: BatchRow): string {
  return mediaLabel(row.record.media_title, row.record.year, row.record.season, row.record.episode)
}

function destinationText(row: BatchRow): string {
  const path = row.preview?.preview?.final_subtitle_path
  if (!path) return ''
  if (commonDirectory.value && path.startsWith(commonDirectory.value)) {
    return path.slice(commonDirectory.value.length + 1) || path
  }
  return shortPath(path)
}

function rowError(row: BatchRow): string {
  if (row.executionError && row.error && row.executionError !== row.error) {
    return `${row.executionError}；重新预览：${row.error}`
  }
  return row.executionError || row.error
}

function rowState(row: BatchRow): { label: string; tone: 'success' | 'warning' | 'error' | 'muted' } {
  if (row.completed) return { label: '已完成', tone: 'success' }
  if (rowError(row)) return { label: rowError(row), tone: 'error' }
  if (!row.target || historyId(row.target) == null) return { label: '待选目标', tone: 'warning' }
  if (!row.preview) return { label: '预检中', tone: 'muted' }
  if (!row.preview.executable) return { label: '无法执行', tone: 'error' }
  return { label: '可以执行', tone: 'success' }
}

function toggleSort(key: SortKey): void {
  if (sortKey.value !== key) {
    sortKey.value = key
    sortDirection.value = 'asc'
    return
  }
  if (sortDirection.value === 'asc') {
    sortDirection.value = 'desc'
    return
  }
  sortKey.value = null
}

function ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
  if (sortKey.value !== key) return 'none'
  return sortDirection.value === 'asc' ? 'ascending' : 'descending'
}

watch(() => props.modelValue, open => {
  if (!open) {
    previewRequest += 1
    openPickerId.value = ''
    return
  }
  captureFocus()
  rows.value = props.records.map(record => ({ record, target: null, preview: null, completed: false, error: '', executionError: '' }))
  showCompleted.value = false
  sortKey.value = null
  sortDirection.value = 'asc'
  openPickerId.value = ''
  captureTriageOrder()
  generalError.value = inputError.value
  if (inputError.value) return
  void previewAll({ resortTriage: true })
})

async function previewAll(options: { preserveGeneralError?: boolean; resortTriage?: boolean } = {}): Promise<void> {
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
      active.map(row => ({ record_id: row.record.id, target_history_id: historyId(row.target) })),
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
    if (options.resortTriage) captureTriageOrder()
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

function updateTarget(recordId: string, target: HistoryRow | null): void {
  openPickerId.value = ''
  rows.value = rows.value.map(row => row.record.id === recordId
    ? { ...row, target, preview: null, error: '', executionError: '' }
    : row)
  void previewAll()
}

function removeRow(row: BatchRow): void {
  if (saving.value || row.completed) return
  previewRequest += 1
  openPickerId.value = ''
  rows.value = rows.value.filter(item => item.record.id !== row.record.id)
  triageOrder.value = triageOrder.value.filter(id => id !== row.record.id)
  emit('remove', row.record.id)
  if (!rows.value.length) {
    emit('update:modelValue', false)
    return
  }
  void previewAll()
}

async function submit(): Promise<void> {
  if (!executable.value) return
  const targetHistoryIds = pendingRows.value.map(row => historyId(row.target))
  if (targetHistoryIds.some(historyIdValue => historyIdValue == null)) return
  saving.value = true
  generalError.value = ''
  try {
    const result = await retargetBatchRecords(
      props.api,
      props.pluginId,
      pendingRows.value.map((row, index) => ({
        record_id: row.record.id,
        target_history_id: targetHistoryIds[index]!,
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
    await previewAll({ resortTriage: true })
  } catch (requestError) {
    const submitError = getErrorMessage(requestError, '批量改配提交失败，请重新预览')
    generalError.value = submitError
    await previewAll({ preserveGeneralError: true, resortTriage: true })
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
          <small>
            共 {{ rows.length || records.length }} 条<template v-if="blockedCount">；<b>{{ blockedCount }} 条待处理</b></template>
            <template v-if="completedCount">；已完成 {{ completedCount }} 条</template>
          </small>
        </div>
        <VBtn icon="mdi-close" variant="text" aria-label="关闭批量改配目标" :disabled="saving" @click="close" />
      </VCardTitle>

      <VCardText class="dialog-content">
        <VAlert v-if="generalError" type="error" variant="tonal" density="compact" class="dialog-alert">{{ generalError }}</VAlert>

        <template v-if="rows.length">
          <div class="toolbar">
            <p v-if="commonDirectory" class="toolbar__prefix">
              共同目录 <code :title="commonDirectory">{{ commonDirectory }}</code>
              <span>表中只显示相对该目录的差异</span>
            </p>
            <VSpacer />
            <VBtn
              v-if="completedCount"
              size="small"
              variant="text"
              :prepend-icon="showCompleted ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
              @click="showCompleted = !showCompleted"
            >
              {{ showCompleted ? '隐藏已完成' : `显示已完成 ${completedCount} 条` }}
            </VBtn>
            <!-- 窄屏没有可点表头，排序改由这组控件承担。 -->
            <div class="toolbar__sort">
              <VSelect
                :model-value="sortKey"
                :items="[{ title: '待处理优先（默认）', value: null }, ...SORT_COLUMNS.map(column => ({ title: column.label, value: column.key }))]"
                label="排序"
                density="compact"
                variant="outlined"
                hide-details
                @update:model-value="sortKey = $event"
              />
              <VBtn
                :prepend-icon="sortDirection === 'asc' ? 'mdi-arrow-up' : 'mdi-arrow-down'"
                size="small"
                variant="tonal"
                :disabled="!sortKey"
                @click="sortDirection = sortDirection === 'asc' ? 'desc' : 'asc'"
              >
                {{ sortDirection === 'asc' ? '升序' : '降序' }}
              </VBtn>
            </div>
          </div>

          <div class="table-wrap">
            <table class="batch-table">
              <thead>
                <tr>
                  <th v-for="column in SORT_COLUMNS" :key="column.key" scope="col" :aria-sort="ariaSort(column.key)">
                    <button
                      type="button"
                      class="sort-button"
                      :class="{ 'sort-button--active': sortKey === column.key }"
                      @click="toggleSort(column.key)"
                    >
                      {{ column.label }}
                      <VIcon
                        :icon="sortKey === column.key ? (sortDirection === 'asc' ? 'mdi-arrow-up' : 'mdi-arrow-down') : 'mdi-arrow-up-down'"
                        size="14"
                        :class="{ 'sort-button__idle': sortKey !== column.key }"
                      />
                    </button>
                  </th>
                  <th scope="col" class="cell-actions"><span class="sr-only">操作</span></th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in visibleRows"
                  :key="row.record.id"
                  :class="{
                    'row--blocked': !row.completed && !isReady(row),
                    'row--missing': !row.completed && (!row.target || historyId(row.target) == null),
                    'row--done': row.completed,
                  }"
                >
                  <td class="cell-name">
                    <strong>{{ row.record.subtitle_file_name }}</strong>
                    <em :class="`tone-${rowState(row).tone}`">{{ rowState(row).label }}</em>
                  </td>
                  <td class="cell-source" data-label="来源媒体">
                    {{ sourceLabel(row) }}
                    <span class="cell-source__path" :title="row.preview?.current_subtitle_path || row.record.path">
                      {{ shortPath(row.preview?.current_subtitle_path || row.record.path) }}
                    </span>
                  </td>
                  <td class="cell-target" data-label="改配到">
                    <VMenu
                      v-if="!row.completed"
                      :model-value="openPickerId === row.record.id"
                      :close-on-content-click="false"
                      location="bottom start"
                      min-width="min(32rem, 92vw)"
                      @update:model-value="open => openPickerId = open ? row.record.id : ''"
                    >
                      <template #activator="{ props: activator }">
                        <button
                          v-bind="activator"
                          type="button"
                          class="target-button"
                          :class="{ 'target-button--empty': !row.target || historyId(row.target) == null }"
                          :disabled="saving"
                        >
                          <span>{{ row.target && historyId(row.target) != null ? historyLabel(row.target) : '选择整理历史…' }}</span>
                          <VIcon icon="mdi-menu-down" size="18" />
                        </button>
                      </template>
                      <VCard class="target-panel">
                        <TargetSelector
                          :model-value="row.target"
                          :api="api"
                          :plugin-id="pluginId"
                          :show-heading="false"
                          :disabled="saving"
                          searchable
                          compact
                          fill-height
                          @update:model-value="updateTarget(row.record.id, $event)"
                        />
                      </VCard>
                    </VMenu>
                    <span v-else class="target-button target-button--static">{{ row.target ? historyLabel(row.target) : '—' }}</span>
                  </td>
                  <td class="cell-destination" data-label="改配后字幕">
                    <code v-if="destinationText(row)" :title="row.preview?.preview?.final_subtitle_path">{{ destinationText(row) }}</code>
                    <span v-else class="muted">选择目标后显示预计路径</span>
                  </td>
                  <td class="cell-actions">
                    <VBtn
                      v-if="!row.completed"
                      icon="mdi-close"
                      size="small"
                      variant="text"
                      color="error"
                      aria-label="移出本次批量改配"
                      :disabled="saving"
                      @click="removeRow(row)"
                    />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </template>
        <VAlert v-else type="info" variant="tonal">没有待处理的记录；本次批量改配不会发送空请求。</VAlert>
      </VCardText>

      <VCardActions class="dialog-actions">
        <span class="action-summary">
          {{ executable ? `${pendingRows.length} 条待执行` : (blockedCount ? `${blockedCount} 条待处理，无法提交` : (completedCount ? '仅剩已完成结果' : '请先处理所有预检问题')) }}
        </span>
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
.sr-only { position: absolute; overflow: hidden; width: 1px; height: 1px; clip-path: inset(50%); }
.batch-card { display: flex; max-block-size: min(90dvh, 56rem); flex-direction: column; overflow: hidden; }
.dialog-title { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); white-space: normal; }
.dialog-title > div { min-width: 0; flex: 1 1 auto; }
.dialog-title > :deep(.v-btn) { flex: 0 0 auto; }
.dialog-title span, .dialog-title small { display: block; }
.dialog-title span { font-size: 1rem; font-weight: 650; }
.dialog-title small { margin-top: 0.25rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; }
.dialog-title small b { color: rgb(var(--v-theme-warning)); font-weight: 650; }
.dialog-content { display: flex; min-block-size: 0; flex: 1 1 auto; flex-direction: column; gap: 0.625rem; overflow: hidden !important; padding: 1rem 1.25rem; }
.dialog-alert { flex: 0 0 auto; }
.toolbar { display: flex; flex-wrap: wrap; flex: 0 0 auto; align-items: center; gap: 0.5rem; }
.toolbar__prefix { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem; margin: 0; min-width: 0; font-size: 0.75rem; }
.toolbar__prefix code { max-width: 32rem; overflow: hidden; padding: 0.1rem 0.3rem; border-radius: 0.25rem; background: rgba(var(--v-theme-on-surface), 0.06); font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }
.toolbar__prefix span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
/* 桌面靠表头排序；这组控件只在窄屏出现。 */
.toolbar__sort { display: none; }

.table-wrap { min-block-size: 0; flex: 1 1 auto; overflow: auto; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; overscroll-behavior: contain; scrollbar-width: thin; scrollbar-color: rgba(var(--v-theme-on-surface), 0.25) transparent; }
.batch-table { width: 100%; border-collapse: collapse; font-size: 0.8125rem; }
.batch-table thead th { position: sticky; top: 0; z-index: 1; padding: 0; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); text-align: start; background: rgb(var(--v-theme-surface)); font-size: 0.75rem; font-weight: 650; }
.batch-table tbody td { padding: 0.4rem 0.6rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); vertical-align: middle; }
.batch-table tbody tr:last-child td { border-bottom: 0; }
.sort-button { display: flex; width: 100%; align-items: center; gap: 0.25rem; padding: 0.45rem 0.6rem; border: 0; color: inherit; text-align: start; background: transparent; cursor: pointer; font: inherit; }
.sort-button:hover { color: rgb(var(--v-theme-on-surface)); background: rgba(var(--v-theme-on-surface), 0.04); }
.sort-button:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.sort-button--active { color: rgb(var(--v-theme-primary)); }
/* 未激活的箭头留在原位并压暗，避免 hover 时列宽跳动。 */
.sort-button__idle { opacity: 0.28; }

.cell-name { max-width: 15rem; }
.cell-name strong { display: block; overflow-wrap: anywhere; font-size: 0.8125rem; font-weight: 600; }
.cell-name em { display: block; font-size: 0.6875rem; font-style: normal; }
.tone-success { color: rgb(var(--v-theme-success)); }
.tone-warning { color: rgb(var(--v-theme-warning)); }
.tone-error { color: rgb(var(--v-theme-error)); }
.tone-muted { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
.cell-source { max-width: 13rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.cell-source__path { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cell-destination code { font-size: 0.75rem; overflow-wrap: anywhere; }
.cell-actions { width: 2.75rem; }
.muted { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
.row--blocked { background: rgba(var(--v-theme-error), 0.06); }
.row--missing { background: rgba(var(--v-theme-warning), 0.07); }
.row--done { opacity: 0.6; }

.target-button { display: inline-flex; max-width: 100%; min-width: 9rem; align-items: center; gap: 0.25rem; padding: 0.25rem 0.4rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.3rem; color: inherit; text-align: start; background: rgba(var(--v-theme-on-surface), 0.03); cursor: pointer; font-size: 0.8125rem; }
.target-button:hover:not(:disabled) { border-color: rgba(var(--v-theme-primary), 0.6); background: rgba(var(--v-theme-primary), 0.06); }
.target-button:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 1px; }
.target-button:disabled { cursor: default; opacity: 0.6; }
.target-button > span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.target-button--empty { border-color: rgba(var(--v-theme-warning), 0.7); color: rgb(var(--v-theme-warning)); background: rgba(var(--v-theme-warning), 0.07); }
.target-button--static { border-style: dashed; cursor: default; }
.target-panel { display: flex; block-size: min(24rem, 70dvh); flex-direction: column; padding: 0.625rem; }

.dialog-actions { flex: 0 0 auto; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.action-summary { min-width: 0; overflow: hidden; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; text-overflow: ellipsis; white-space: nowrap; }

/*
 * 断点由内容决定：表格五列在约 680px 以下开始横向溢出，所以 700px 就换成卡片。
 * 窄屏下不再嵌套滚动容器 —— 标题与操作栏保持固定，整个表体作为唯一滚动区，
 * 这样横屏（视口高仅 375px）也不会退化成只剩几十像素的内层滚动框。
 */
@media (max-width: 43.75rem) {
  .batch-card { max-block-size: 100dvh; }
  .dialog-title, .dialog-actions { padding-inline: 1rem; }
  .dialog-content { overflow-y: auto !important; padding: 0.875rem 1rem; }
  .action-summary { display: none; }

  .toolbar { position: sticky; top: -0.875rem; z-index: 2; padding-block: 0.5rem; background: rgb(var(--v-theme-surface)); }
  .toolbar__prefix { flex: 1 1 100%; }
  .toolbar__sort { display: flex; flex: 1 1 100%; align-items: center; gap: 0.4rem; }
  .toolbar__sort > :deep(.v-input) { flex: 1 1 auto; }

  .table-wrap { overflow: visible; border: 0; }
  .batch-table, .batch-table tbody, .batch-table tr { display: block; width: 100%; }
  .batch-table td { display: block; }
  .batch-table thead { display: none; }
  .batch-table tbody tr { position: relative; padding: 0.5rem 3rem 0.6rem 0.6rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
  .batch-table tbody tr + tr { margin-block-start: 0.5rem; }
  .batch-table tbody td { padding: 0.1rem 0; border: 0; }
  .cell-name, .cell-source, .cell-destination { max-width: none; }
  .cell-name strong { font-size: 0.875rem; }
  .cell-source__path { white-space: normal; overflow-wrap: anywhere; }
  /* 卡片里没有表头，字段名来自 data-label，文案只有标记这一处来源。 */
  .cell-source::before, .cell-destination::before {
    display: block;
    color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
    content: attr(data-label);
    font-size: 0.6875rem;
  }
  /* 目标是主控件，占满整行；截断它等于把关键信息藏起来。 */
  .cell-target { padding-block: 0.35rem !important; }
  .target-button { width: 100%; }
  .target-button > span { white-space: normal; }
  /* 移出按钮脱离文档流，宽度不能被上面的块级规则接管。 */
  .batch-table tbody td.cell-actions { position: absolute; top: 0.35rem; right: 0.35rem; width: auto; padding: 0; }
}

/* 触屏（含带触屏的桌面）放大命中区，与视口宽度无关。 */
@media (pointer: coarse) {
  .target-button { min-block-size: 2.75rem; padding-inline: 0.6rem; }
  .sort-button { min-block-size: 2.75rem; }
  .cell-actions :deep(.v-btn) { width: 2.75rem; height: 2.75rem; }
}

/* 刘海与 home 指示条：窄屏是全屏弹窗，底部操作栏必须避让。 */
@supports (padding: max(0px)) {
  .dialog-actions { padding-block-end: max(0.75rem, env(safe-area-inset-bottom)); }
}

/* 横屏手机竖直空间稀缺：压扁固定块，把高度让给唯一的滚动区。 */
@media (max-height: 30rem) and (orientation: landscape) {
  .dialog-title { padding-block: 0.35rem; }
  .dialog-title small { display: none; }
  .dialog-actions { min-height: 0; padding-block: 0.4rem; }
  .toolbar__prefix { display: none; }
  /* 横屏有横向空间而没有纵向空间：卡片内部改成两列，把五行压到三行。 */
  .batch-table tbody tr { display: grid; column-gap: 0.75rem; grid-template-columns: minmax(0, 1fr) 15rem; }
  .cell-name { grid-column: 1 / -1; }
  .cell-source, .cell-destination { grid-column: 1; }
  .cell-target { grid-column: 2; grid-row: 2 / span 2; align-self: start; }
  .cell-source::before, .cell-destination::before { display: inline; margin-inline-end: 0.3rem; }
}
</style>
