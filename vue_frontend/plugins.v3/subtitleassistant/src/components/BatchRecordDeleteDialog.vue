<script setup lang="ts">
import { computed, ref, useId, watch } from 'vue'
import { useDisplay } from 'vuetify'

import { deleteBatchRecords, getErrorMessage } from '@/api/client'
import StateChip from '@/components/StateChip.vue'
import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import {
  MAX_RECORD_BATCH_SIZE,
  type BatchRecordDeleteResponse,
  type BatchRecordDeleteResultItem,
  type FileLocation,
  type PluginApi,
  type RecordDeleteMode,
  type RecordListItem,
  type RecordStatus,
} from '@/types'
import { locationLabels, recordStates } from '@/types/presentation'

interface DeleteRow {
  record: RecordListItem
  result: BatchRecordDeleteResultItem | null
}

const props = defineProps<{
  modelValue: boolean
  api: PluginApi
  pluginId: string
  records: RecordListItem[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  complete: [result: BatchRecordDeleteResponse, refreshRequiredRecordIds: string[]]
  'refresh-required': [message: string]
}>()

const { smAndDown } = useDisplay()
const titleId = `subtitleassistant-batch-delete-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn()
const rows = ref<DeleteRow[]>([])
const mode = ref<RecordDeleteMode | null>(null)
const saving = ref(false)
const generalError = ref('')

const inputError = computed(() => {
  if (!props.records.length) return '请至少选择一条匹配记录。'
  if (props.records.length > MAX_RECORD_BATCH_SIZE) return `一次最多删除 ${MAX_RECORD_BATCH_SIZE} 条记录，请缩小选择范围。`
  return ''
})
const hasResults = computed(() => rows.value.some(row => row.result))
const recordOnlyDisabled = computed(() => rows.value.some(row => row.record.status !== 'matched'))
const submitRows = computed(() => hasResults.value
  ? rows.value.filter(row => row.result?.status === 'failed' && !requiresRefresh(row.result))
  : rows.value)
const canSubmit = computed(() => Boolean(
  !inputError.value
  && mode.value
  && submitRows.value.length
  && !saving.value,
))
const statusSummary = computed(() => summarizeByStatus(rows.value.map(row => row.record)))
const locationSummary = computed(() => summarizeByLocation(rows.value.map(row => row.record)))
const successCount = computed(() => rows.value.filter(row => row.result?.status === 'success').length)
const failureCount = computed(() => rows.value.filter(row => row.result?.status === 'failed').length)
const refreshRequiredCount = computed(() => rows.value.filter(row => row.result && requiresRefresh(row.result)).length)
const retryableFailureCount = computed(() => submitRows.value.length)
const confirmLabel = computed(() => {
  if (!mode.value) return '请选择删除范围'
  const count = submitRows.value.length
  if (!count) return '没有可重试的记录'
  const action = mode.value === 'record_only' ? '删除记录，保留当前字幕文件' : '删除记录及当前字幕文件'
  return hasResults.value ? `重试 ${count} 条：${action}` : `${action}（${count} 条）`
})

watch(() => props.modelValue, open => {
  if (!open) return
  captureFocus()
  rows.value = props.records.map(record => ({ record, result: null }))
  mode.value = null
  saving.value = false
  generalError.value = inputError.value
})

watch(recordOnlyDisabled, disabled => {
  if (disabled && mode.value === 'record_only') mode.value = null
})

function summarizeByStatus(records: RecordListItem[]): Array<{ key: RecordStatus; count: number }> {
  return (['matched', 'staged', 'unmatched'] as const)
    .map(key => ({ key, count: records.filter(record => record.status === key).length }))
    .filter(item => item.count)
}

function summarizeByLocation(records: RecordListItem[]): Array<{ key: FileLocation; count: number }> {
  return (['media_directory', 'plugin_data'] as const)
    .map(key => ({ key, count: records.filter(record => record.location === key).length }))
    .filter(item => item.count)
}

function currentFilePath(record: RecordListItem): string {
  return record.current_file_path || record.path
}

function requiresRefresh(result: BatchRecordDeleteResultItem): boolean {
  if (result.status === 'not_executed' || result.consistency_risk) return true
  const code = (result.error_code || '').toLowerCase()
  return code.includes('shared_current_file')
    || code.includes('shared_record_file')
    || code.includes('record_not_found')
    || code.includes('record_version')
    || code.includes('record_confirmation')
    || code.includes('record_conflict')
}

function resultLabel(result: BatchRecordDeleteResultItem | null): string {
  if (!result) return '待确认'
  if (result.status === 'success') return '已删除'
  if (requiresRefresh(result)) return '需刷新重选'
  return '删除失败，可重试'
}

function resultColor(result: BatchRecordDeleteResultItem | null): 'success' | 'warning' | 'error' | undefined {
  if (!result) return undefined
  if (result.status === 'success') return 'success'
  return requiresRefresh(result) ? 'error' : 'warning'
}

async function submit(): Promise<void> {
  if (!canSubmit.value || !mode.value) return
  const rowsForRequest = submitRows.value
  const submittedIds = new Set(rowsForRequest.map(row => row.record.id))
  saving.value = true
  generalError.value = ''
  try {
    const result = await deleteBatchRecords(props.api, props.pluginId, {
      delete_mode: mode.value,
      items: rowsForRequest.map(({ record }) => ({
        record_id: record.id,
        expected_status: record.status,
        expected_location: record.location,
        expected_path: record.path,
        expected_updated_at: record.updated_at,
      })),
    })
    const byId = new Map(result.items.map(item => [item.record_id, item]))
    rows.value = rows.value.map(row => {
      if (!submittedIds.has(row.record.id)) return row
      return {
        ...row,
        result: byId.get(row.record.id) || {
          record_id: row.record.id,
          status: 'not_executed',
          error_code: 'batch_result_missing',
          message: '服务端未返回该记录的执行结果，请刷新后重新选择。',
          consistency_risk: false,
        },
      }
    })
    const refreshRequiredRecordIds = rows.value
      .filter(row => row.result && requiresRefresh(row.result))
      .map(row => row.record.id)
    emit('complete', result, refreshRequiredRecordIds)
    if (rows.value.every(row => row.result?.status === 'success')) emit('update:modelValue', false)
  } catch (requestError) {
    const message = getErrorMessage(requestError, '批量删除失败，请稍后重试。')
    const statusCode = (requestError as { response?: { status?: number } })?.response?.status
    if (statusCode === 409) {
      emit('refresh-required', message)
      emit('update:modelValue', false)
      return
    }
    generalError.value = message
  } finally {
    saving.value = false
  }
}

function close(): void {
  if (!saving.value) emit('update:modelValue', false)
}

function handleDialogUpdate(open: boolean): void {
  if (!saving.value) emit('update:modelValue', open)
}
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="smAndDown"
    :persistent="saving"
    max-width="900"
    scrollable
    retain-focus
    :aria-labelledby="titleId"
    @update:model-value="handleDialogUpdate"
    @after-leave="restoreFocus"
  >
    <VCard class="batch-delete-card">
      <VCardTitle class="dialog-title">
        <div>
          <span :id="titleId">批量删除匹配记录</span>
          <small>本次共 {{ rows.length || records.length }} 条记录，所有记录使用同一种删除范围。</small>
        </div>
        <VBtn icon="mdi-close" variant="text" aria-label="关闭批量删除确认" :disabled="saving" @click="close" />
      </VCardTitle>

      <VCardText class="dialog-content">
        <VAlert v-if="inputError" type="error" variant="tonal" density="compact" class="dialog-alert">{{ inputError }}</VAlert>
        <VAlert v-else-if="generalError" type="error" variant="tonal" density="compact" class="dialog-alert">{{ generalError }}</VAlert>
        <VAlert v-if="recordOnlyDisabled" type="warning" variant="tonal" density="compact" class="dialog-alert">
          本次选择包含暂存或未匹配记录。为避免遗留插件数据目录中的未追踪字幕文件，只能删除记录及当前字幕文件。
        </VAlert>
        <VAlert v-if="hasResults" type="info" variant="tonal" density="compact" class="dialog-alert" aria-live="polite">
          已删除 {{ successCount }} 条，删除失败 {{ failureCount }} 条；{{ refreshRequiredCount }} 条需要刷新后重新选择。
        </VAlert>

        <section class="impact-summary" aria-label="批量删除影响汇总">
          <div>
            <span>记录总数</span>
            <strong>{{ rows.length }}</strong>
          </div>
          <div v-for="item in statusSummary" :key="item.key">
            <span>{{ recordStates[item.key].label }}</span>
            <strong>{{ item.count }}</strong>
          </div>
          <div v-for="item in locationSummary" :key="item.key">
            <span>{{ locationLabels[item.key] }}</span>
            <strong>{{ item.count }}</strong>
          </div>
        </section>

        <VRadioGroup v-model="mode" aria-label="选择批量删除范围" class="delete-options">
          <VRadio value="record_only" label="删除记录，保留当前字幕文件" :disabled="saving || recordOnlyDisabled || hasResults" />
          <VRadio value="record_and_file" label="删除记录及当前字幕文件" :disabled="saving || hasResults" />
        </VRadioGroup>
        <p v-if="mode === 'record_only'" class="mode-warning">所有当前字幕文件会保留在原路径，但工作台不再追踪这些文件。</p>
        <p v-else-if="mode === 'record_and_file'" class="mode-warning">文件不存在按幂等成功处理；文件删除失败不会降级为仅删除记录。</p>

        <section class="record-list" aria-label="待删除记录明细">
          <article v-for="row in rows" :key="row.record.id" class="record-row">
            <header class="record-row__heading">
              <strong>{{ row.record.subtitle_file_name }}</strong>
              <span v-if="row.result" class="result-label" :class="`result-label--${resultColor(row.result)}`">{{ resultLabel(row.result) }}</span>
            </header>
            <div class="record-row__facts">
              <span><StateChip :state="recordStates[row.record.status]" size="x-small" /></span>
              <span>{{ locationLabels[row.record.location] }}</span>
              <span class="record-path" :title="currentFilePath(row.record)">{{ currentFilePath(row.record) }}</span>
            </div>
            <VAlert v-if="row.result && row.result.status !== 'success'" :type="requiresRefresh(row.result) ? 'error' : 'warning'" variant="tonal" density="compact" class="record-result">
              {{ row.result.message || (requiresRefresh(row.result) ? '此项不能沿用当前确认信息，请刷新后重新选择。' : '删除失败，可保持当前选择后重试。') }}
            </VAlert>
          </article>
        </section>
      </VCardText>

      <VCardActions class="dialog-actions">
        <span class="action-summary" aria-live="polite">
          {{ hasResults ? (retryableFailureCount ? `可重试 ${retryableFailureCount} 条失败记录` : '没有可直接重试的记录') : '请选择统一删除范围' }}
        </span>
        <VSpacer />
        <VBtn variant="text" :disabled="saving" @click="close">取消</VBtn>
        <VBtn color="error" variant="flat" prepend-icon="mdi-delete-outline" :loading="saving" :disabled="!canSubmit" @click="submit">
          {{ confirmLabel }}
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.batch-delete-card { display: flex; max-block-size: min(90dvh, 52rem); flex-direction: column; overflow: hidden; }
.dialog-title { display: flex; flex: 0 0 auto; align-items: flex-start; justify-content: space-between; gap: 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); white-space: normal; }
.dialog-title > div { min-width: 0; flex: 1 1 auto; }
.dialog-title > :deep(.v-btn) { flex: 0 0 auto; }
.dialog-title span, .dialog-title small { display: block; }
.dialog-title span { font-size: 1rem; font-weight: 650; }
.dialog-title small { margin-top: 0.25rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; font-weight: 400; }
.dialog-content { display: flex; min-block-size: 0; flex: 1 1 auto; flex-direction: column; overflow: hidden !important; padding: 1rem 1.25rem; }
.dialog-alert { flex: 0 0 auto; margin-block-end: 0.75rem; }
.impact-summary { display: flex; flex: 0 0 auto; flex-wrap: wrap; gap: 0.5rem; margin-block-end: 0.75rem; }
.impact-summary div { display: grid; min-width: 6rem; gap: 0.15rem; padding: 0.5rem 0.625rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.impact-summary span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.7rem; }
.impact-summary strong { font-size: 0.875rem; }
.delete-options { flex: 0 0 auto; margin: 0; }
.mode-warning { flex: 0 0 auto; margin: 0.5rem 0 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.record-list { min-block-size: 0; flex: 1 1 auto; overflow: auto; overscroll-behavior: contain; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.record-row { min-width: 0; padding: 0.75rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.record-row:last-child { border-bottom: 0; }
.record-row__heading { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 0.75rem; }
.record-row__heading strong { min-width: 0; overflow-wrap: anywhere; font-size: 0.8125rem; line-height: 1.45; }
.record-row__facts { display: grid; min-width: 0; grid-template-columns: auto auto minmax(0, 1fr); align-items: center; gap: 0.5rem; margin-top: 0.5rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.record-path { min-width: 0; overflow-wrap: anywhere; }
.result-label { flex: 0 0 auto; font-size: 0.75rem; white-space: nowrap; }
.result-label--success { color: rgb(var(--v-theme-success)); }
.result-label--warning { color: rgb(var(--v-theme-warning)); }
.result-label--error { color: rgb(var(--v-theme-error)); }
.record-result { margin-top: 0.625rem; font-size: 0.75rem; line-height: 1.45; }
.dialog-actions { flex: 0 0 auto; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.action-summary { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
@media (max-width: 37.5rem) {
  .batch-delete-card { max-block-size: 100dvh; }
  .dialog-title, .dialog-actions { padding-inline: 1rem; }
  .dialog-content { padding: 0.875rem 1rem; }
  .impact-summary { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .impact-summary div { min-width: 0; }
  .record-row__facts { grid-template-columns: auto 1fr; }
  .record-path { grid-column: 1 / -1; }
  .action-summary { display: none; }
}
</style>
