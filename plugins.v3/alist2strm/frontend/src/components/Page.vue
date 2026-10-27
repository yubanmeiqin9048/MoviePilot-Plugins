<script setup lang="ts">
import { DEFAULT_FILE_AGE_POLICY } from '@/fileAgePolicy'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import { getErrorMessage, readConfiguration, readHistory, readScanStatus, startMappingScan, startScan, stopScan } from '@/api'
import { configurationEditorState } from '@/configState'
import type {
  ConfigModel,
  DirectoryMapping,
  HistoryItem,
  HistoryPage,
  OpenListInstance,
  PluginApi,
  ScanCounters,
  ScanStatus,
  ScanMode,
  ScanTarget,
} from '@/types'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId?: string
  show_switch?: boolean
}>(), {
  pluginId: '',
  show_switch: true,
})

const emit = defineEmits<{
  close: []
  switch: []
  layout: [layout: { maxWidth: string }]
}>()

const pluginId = computed(() => props.pluginId.trim() || 'Alist2Strm')
const activeTab = ref('run')
const config = ref<ConfigModel>(emptyConfig())
const status = ref<ScanStatus>(emptyStatus())
const loading = ref(true)
const triggering = ref(false)
const stopping = ref(false)
const message = ref('')
const messageTone = ref<'success' | 'warning' | 'error'>('success')
const historyPage = ref<HistoryPage>(emptyHistoryPage())
const historyPageNumber = ref(1)
const historyPageSize = ref(10)
const historyLoading = ref(false)
const historyError = ref('')
const historyExpanded = ref<string[]>([])
let pollTimer: ReturnType<typeof window.setInterval> | null = null
let pageMounted = false
let historyRequestSequence = 0

const hasUnsavedChanges = computed(() => configurationEditorState.dirty)
const statusLabel = computed(() => stateLabel(status.value.state))
const statusColor = computed(() => stateColor(status.value.state))
const historyPageCount = computed(() => Math.max(1, Math.ceil(historyPage.value.total / historyPageSize.value)))
const counterRows = computed(() => counterRowsFor(status.value.counters))
const mappingTotal = computed(() => config.value.instances.reduce((total, instance) => total + instance.mappings.length, 0))

onMounted(async () => {
  pageMounted = true
  emit('layout', { maxWidth: '72rem' })
  await loadPage()
  if (!pageMounted) return
  pollTimer = window.setInterval(() => {
    void refreshStatus()
  }, 2000)
})

onUnmounted(() => {
  pageMounted = false
  historyRequestSequence += 1
  stopPolling()
})

watch(activeTab, (tab) => {
  if (tab === 'history') void loadHistory()
})

watch(historyPageNumber, () => {
  if (activeTab.value === 'history') void loadHistory()
})

watch(historyPageSize, () => {
  historyPageNumber.value = 1
  if (activeTab.value === 'history') void loadHistory()
})

function emptyCounters(): ScanCounters {
  return { generated: 0, downloaded: 0, updated: 0, skipped: 0, deleted: 0, failed: 0 }
}

function emptyStatus(): ScanStatus {
  return {
    state: 'idle',
    status: 'idle',
    running: false,
    current_mapping: null,
    total_mappings: 0,
    counters: emptyCounters(),
    mappings: [],
    started_at: null,
    finished_at: null,
  }
}

function emptyHistoryPage(): HistoryPage {
  return { total: 0, page: 1, page_size: 10, items: [] }
}

function counterRowsFor(counters: ScanCounters): Array<{ key: keyof ScanCounters; label: string; icon: string; value: number }> {
  return [
    { key: 'generated', label: '生成', icon: 'mdi-file-plus-outline', value: counters.generated },
    { key: 'downloaded', label: '下载', icon: 'mdi-download-outline', value: counters.downloaded },
    { key: 'updated', label: '更新', icon: 'mdi-file-sync-outline', value: counters.updated },
    { key: 'skipped', label: '跳过', icon: 'mdi-skip-next-outline', value: counters.skipped },
    { key: 'deleted', label: '删除', icon: 'mdi-delete-outline', value: counters.deleted },
    { key: 'failed', label: '失败', icon: 'mdi-alert-circle-outline', value: counters.failed },
  ]
}

function stateColor(state: string): string {
  if (state === 'failed' || state === 'partial_failure') return 'error'
  if (state === 'success') return 'success'
  if (state === 'stopped' || state === 'stopping' || state === 'interrupted') return 'warning'
  if (state === 'running') return 'primary'
  return 'info'
}

function stateLabel(state: string): string {
  if (state === 'running') return '进行中'
  if (state === 'stopping') return '正在停止'
  if (state === 'stopped') return '已停止'
  if (state === 'interrupted') return '已中断'
  if (state === 'success') return '已完成'
  if (state === 'partial_failure') return '部分失败'
  if (state === 'failed') return '失败'
  if (state === 'pending' || state === 'queued') return '等待中'
  return '空闲'
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return '未记录'
  const timestamp = new Date(value)
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString()
}

function hasHistoryError(item: HistoryItem): boolean {
  return Boolean(item.error || item.state === 'failed' || item.state === 'partial_failure' || item.state === 'interrupted' || item.mappings.some((mapping) => mapping.error))
}

function historyKey(item: HistoryItem): string {
  return item.id || item.identity
}

async function loadHistory(): Promise<void> {
  const requestSequence = ++historyRequestSequence
  const requestPage = historyPageNumber.value
  const requestPageSize = historyPageSize.value
  historyLoading.value = true
  historyError.value = ''
  try {
    const response = await readHistory(props.api, pluginId.value, requestPage, requestPageSize)
    if (requestSequence !== historyRequestSequence) return
    historyPage.value = response
    historyPageNumber.value = response.page
    historyExpanded.value = historyExpanded.value.filter((id) => response.items.some((item) => historyKey(item) === id))
  } catch (error) {
    if (requestSequence !== historyRequestSequence) return
    historyError.value = `读取运行历史失败：${getErrorMessage(error, '请稍后重试')}`
  } finally {
    if (requestSequence === historyRequestSequence) historyLoading.value = false
  }
}

function emptyConfig(): ConfigModel {
  return {
    instances: [],
    schedule_enabled: false,
    active_recheck_days: 1,
    file_age_policy: { ...DEFAULT_FILE_AGE_POLICY },
    history_retention_days: 90,
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function closePage(): void {
  pageMounted = false
  stopPolling()
  emit('close')
}

function openSettings(): void {
  pageMounted = false
  stopPolling()
  emit('switch')
}

async function loadPage(): Promise<void> {
  loading.value = true
  try {
    const response = await readConfiguration(props.api, pluginId.value)
    if (response?.data) config.value = response.data
    await refreshStatus()
  } catch (error) {
    messageTone.value = 'error'
    message.value = `读取已保存配置失败：${getErrorMessage(error, '请稍后重试')}`
  } finally {
    loading.value = false
  }
}

async function refreshStatus(): Promise<void> {
  try {
    status.value = await readScanStatus(props.api, pluginId.value)
  } catch (error) {
    messageTone.value = 'error'
    message.value = `读取扫描状态失败：${getErrorMessage(error, '请稍后重试')}`
  }
}

async function runAll(scanMode: ScanMode = 'normal'): Promise<void> {
  triggering.value = true
  try {
    const response = await startScan(props.api, pluginId.value, scanMode)
    showResult(response.accepted ? 'success' : 'warning', response.message)
  } catch (error) {
    showResult('error', `启动扫描失败：${getErrorMessage(error, '请稍后重试')}`)
  } finally {
    triggering.value = false
    await refreshStatus()
  }
}

async function runMapping(
  instance: OpenListInstance,
  mapping: DirectoryMapping,
  index: number,
  scanMode: ScanMode = 'normal',
): Promise<void> {
  const target: ScanTarget = {
    instance_id: instance.id,
    mapping_index: index + 1,
    source_dir: mapping.source_dir,
  }
  triggering.value = true
  try {
    const response = await startMappingScan(props.api, pluginId.value, target, scanMode)
    showResult(response.accepted ? 'success' : 'warning', response.message)
  } catch (error) {
    showResult('error', `启动扫描失败：${getErrorMessage(error, '请稍后重试')}`)
  } finally {
    triggering.value = false
    await refreshStatus()
  }
}

async function stopScanRun(): Promise<void> {
  stopping.value = true
  try {
    const response = await stopScan(props.api, pluginId.value)
    showResult('warning', response.message)
  } catch (error) {
    showResult('error', `停止扫描失败：${getErrorMessage(error, '请稍后重试')}`)
  } finally {
    stopping.value = false
    await refreshStatus()
  }
}

function showResult(tone: 'success' | 'warning' | 'error', text: string): void {
  messageTone.value = tone
  message.value = text
}

</script>

<template>
  <div class="a2s-page">
    <!-- 顶栏 -->
    <header class="a2s-head">
      <div class="a2s-head-text">
        <div class="a2s-head-title">Alist2Strm 运行</div>
        <div class="a2s-head-sub">{{ config.instances.length }} 个实例 · {{ mappingTotal }} 条映射</div>
      </div>
      <VBtn
        v-if="show_switch"
        size="small"
        variant="text"
        prepend-icon="mdi-cog-outline"
        @click="openSettings"
      >
        设置
      </VBtn>
      <VBtn icon="mdi-close" size="small" variant="text" aria-label="关闭" @click="closePage" />
    </header>
    <VTabs v-model="activeTab" class="a2s-tabs" density="compact" color="primary">
      <VTab value="run" prepend-icon="mdi-play-circle-outline">运行</VTab>
      <VTab value="history" prepend-icon="mdi-history">历史</VTab>
    </VTabs>
    <VDivider />

    <VWindow v-model="activeTab" class="a2s-window">
      <VWindowItem value="run">
        <div class="a2s-main">
          <VAlert v-if="hasUnsavedChanges" type="warning" variant="tonal" density="compact">
            配置弹窗有未保存修改；本次运行只使用已保存配置，当前修改将在保存后下一次运行生效。
          </VAlert>
          <VAlert v-if="message" :type="messageTone" variant="tonal" density="compact" closable @click:close="message = ''">
            {{ message }}
          </VAlert>

          <!-- 本次运行概览 -->
          <section class="a2s-panel">
            <div class="a2s-panel-head">
              <div class="a2s-panel-text">
                <div class="a2s-panel-title">
                  本次运行
                  <span class="a2s-state" :class="`text-${statusColor}`">
                    <span class="a2s-state-dot" :class="{ 'a2s-state-dot--pulse': status.running }" />
                    {{ statusLabel }}
                  </span>
                </div>
                <div class="a2s-panel-sub">
                  <template v-if="status.current_mapping">当前映射：<span class="a2s-mono">{{ status.current_mapping }}</span></template>
                  <template v-else-if="status.started_at">开始于 {{ formatTimestamp(status.started_at) }}</template>
                  <template v-else>尚无运行记录</template>
                </div>
              </div>
              <div class="a2s-panel-actions">
                <VBtn
                  color="error"
                  variant="tonal"
                  prepend-icon="mdi-stop"
                  :loading="stopping"
                  :disabled="loading || stopping || triggering || !status.running"
                  @click="stopScanRun"
                >
                  停止扫描
                </VBtn>
                <VBtn
                  color="primary"
                  variant="flat"
                  prepend-icon="mdi-play"
                  :loading="triggering"
                  :disabled="loading || triggering || status.running || !config.instances.length"
                  @click="runAll()"
                >
                  运行全部
                </VBtn>
                <VBtn
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-folder-refresh-outline"
                  :disabled="loading || triggering || status.running || !config.instances.length"
                  @click="runAll('force_complete')"
                >
                  全部强制完整扫描
                </VBtn>
              </div>
            </div>
            <VProgressLinear v-if="status.running" indeterminate color="primary" height="2" />
            <div class="a2s-stats">
              <div
                v-for="row in counterRows"
                :key="row.key"
                class="a2s-stat"
                :class="{ 'a2s-stat--error': row.key === 'failed' && row.value > 0 }"
              >
                <VIcon size="18" :icon="row.icon" class="a2s-stat-icon" />
                <div class="a2s-stat-value">{{ row.value }}</div>
                <div class="a2s-stat-label">{{ row.label }}</div>
              </div>
            </div>
          </section>

          <!-- 映射状态 -->
          <section v-if="status.mappings.length" class="a2s-section">
            <div class="a2s-section-head">
              <div class="a2s-section-title">映射状态</div>
              <p v-if="status.counters.failed" class="a2s-section-desc">失败请按运行时间、实例和映射查阅宿主日志。</p>
            </div>
            <div class="a2s-table-wrap">
              <VTable density="compact" class="a2s-table">
                <thead>
                  <tr>
                    <th>映射</th>
                    <th>状态</th>
                    <th v-for="row in counterRows" :key="`mapping-${row.key}`" class="text-center">{{ row.label }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="mapping in status.mappings" :key="`${mapping.instance_id}-${mapping.mapping_index}`">
                    <td class="a2s-path-cell">
                      <div class="text-medium-emphasis text-caption">{{ mapping.instance_name }}</div>
                      <div class="a2s-mono">{{ mapping.source_dir }}</div>
                    </td>
                    <td>
                      <VChip size="x-small" :color="stateColor(mapping.state)" variant="tonal" label>{{ stateLabel(mapping.state) }}</VChip>
                      <div v-if="mapping.error" class="text-error text-caption mt-1">{{ mapping.error }}</div>
                    </td>
                    <td
                      v-for="row in counterRowsFor(mapping.counters)"
                      :key="`${mapping.instance_id}-${mapping.mapping_index}-${row.key}`"
                      class="text-center"
                      :class="{ 'text-error font-weight-bold': row.key === 'failed' && row.value > 0, 'text-disabled': !row.value }"
                    >
                      {{ row.value }}
                    </td>
                  </tr>
                </tbody>
              </VTable>
            </div>
          </section>

          <!-- 映射列表 -->
          <section class="a2s-section">
            <div class="a2s-section-head">
              <div class="a2s-section-title">目录映射</div>
              <p v-if="config.instances.length" class="a2s-section-desc">
                强制完整扫描仅绕过时间剪枝，检查所选范围的所有允许目录并尝试补回缺失产物；白名单、排除、已有文件跳过及播放地址更新规则仍生效。
              </p>
            </div>

            <div v-if="!loading && !config.instances.length" class="a2s-empty">
              <VIcon size="36" icon="mdi-folder-cog-outline" color="medium-emphasis" />
              <div class="a2s-empty-text">尚未配置实例与映射，请先保存配置。</div>
            </div>

            <div v-for="instance in config.instances" :key="instance.id || instance.name" class="a2s-instance">
              <div class="a2s-instance-head">
                <VIcon size="18" icon="mdi-server-network-outline" class="a2s-instance-icon" />
                <span class="a2s-instance-name">{{ instance.name }}</span>
                <span class="a2s-count">{{ instance.mappings.length }}</span>
              </div>
              <div v-if="!instance.mappings.length" class="a2s-mapping a2s-mapping--empty">该实例尚未配置映射。</div>
              <div v-for="(mapping, index) in instance.mappings" :key="`${instance.id}-${index}`" class="a2s-mapping">
                <span class="a2s-mapping-index">{{ index + 1 }}</span>
                <div class="a2s-mapping-paths">
                  <div class="a2s-mono a2s-mapping-source">{{ mapping.source_dir }}</div>
                  <div class="a2s-mono a2s-mapping-output">
                    <VIcon size="14" icon="mdi-arrow-right-bottom" />
                    {{ mapping.output_dir }}
                  </div>
                </div>
                <div class="a2s-mapping-actions">
                  <VBtn
                    size="small"
                    variant="text"
                    prepend-icon="mdi-folder-refresh-outline"
                    :loading="triggering"
                    :disabled="loading || triggering || status.running"
                    @click="runMapping(instance, mapping, index, 'force_complete')"
                  >
                    强制完整扫描
                  </VBtn>
                  <VBtn
                    size="small"
                    color="primary"
                    variant="tonal"
                    prepend-icon="mdi-play-outline"
                    :loading="triggering"
                    :disabled="loading || triggering || status.running"
                    @click="runMapping(instance, mapping, index)"
                  >
                    运行此映射
                  </VBtn>
                </div>
              </div>
            </div>
          </section>
        </div>
      </VWindowItem>

      <VWindowItem value="history">
        <div class="a2s-main">
          <div class="a2s-history-toolbar">
            <span class="a2s-section-title">共 {{ historyPage.total }} 次运行</span>
            <VSpacer />
            <VSelect
              v-model="historyPageSize"
              :items="[10, 20, 50]"
              label="每页"
              density="compact"
              variant="outlined"
              hide-details
              class="a2s-page-size"
            />
            <VBtn icon="mdi-refresh" size="small" variant="text" :loading="historyLoading" aria-label="刷新历史" @click="loadHistory">
              <VIcon icon="mdi-refresh" />
              <VTooltip activator="parent">刷新历史</VTooltip>
            </VBtn>
          </div>

          <VProgressLinear v-if="historyLoading" indeterminate color="primary" height="2" />
          <div v-if="historyLoading" class="a2s-empty">
            <div class="a2s-empty-text">正在读取运行历史。</div>
          </div>
          <VAlert v-else-if="historyError" type="error" variant="tonal" density="compact">
            {{ historyError }}
          </VAlert>
          <div v-else-if="!historyPage.items.length" class="a2s-empty">
            <VIcon size="36" icon="mdi-history" color="medium-emphasis" />
            <div class="a2s-empty-text">暂无扫描历史。</div>
          </div>

          <VExpansionPanels
            v-else
            v-model="historyExpanded"
            multiple
            variant="accordion"
            class="a2s-history"
          >
            <VExpansionPanel v-for="item in historyPage.items" :key="historyKey(item)" :value="historyKey(item)" elevation="0">
              <VExpansionPanelTitle>
                <div class="a2s-history-title">
                  <VChip size="small" :color="stateColor(item.state)" variant="tonal" label>{{ stateLabel(item.state) }}</VChip>
                  <span class="a2s-history-time">{{ formatTimestamp(item.started_at) }}</span>
                  <span class="a2s-history-summary">
                    {{ item.mappings.length }} 个映射 · 生成 {{ item.counters.generated }} · 更新 {{ item.counters.updated }}
                    <span v-if="item.counters.failed" class="text-error"> · 失败 {{ item.counters.failed }}</span>
                  </span>
                </div>
              </VExpansionPanelTitle>
              <VExpansionPanelText>
                <div class="a2s-history-meta">
                  <span><VIcon size="14" icon="mdi-clock-start" /> 开始：{{ formatTimestamp(item.started_at) }}</span>
                  <span><VIcon size="14" icon="mdi-clock-end" /> 结束：{{ formatTimestamp(item.finished_at) }}</span>
                </div>
                <div class="a2s-stats a2s-stats--compact">
                  <div
                    v-for="row in counterRowsFor(item.counters)"
                    :key="`history-${item.id}-${row.key}`"
                    class="a2s-stat"
                    :class="{ 'a2s-stat--error': row.key === 'failed' && row.value > 0 }"
                  >
                    <div class="a2s-stat-value">{{ row.value }}</div>
                    <div class="a2s-stat-label">{{ row.label }}</div>
                  </div>
                </div>
                <VAlert v-if="hasHistoryError(item)" type="warning" variant="tonal" density="compact" class="mt-3">
                  失败请按运行时间、实例和映射查阅宿主日志。
                </VAlert>
                <div v-if="item.mappings.length" class="a2s-table-wrap mt-3">
                  <VTable density="compact" class="a2s-table">
                    <thead>
                      <tr>
                        <th>实例 / 映射</th>
                        <th>源目录 → 输出目录</th>
                        <th>时间</th>
                        <th>状态</th>
                        <th v-for="row in counterRowsFor(item.mappings[0].counters)" :key="`history-map-${item.id}-${row.key}`" class="text-center">
                          {{ row.label }}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="mapping in item.mappings" :key="mapping.identity">
                        <td class="text-no-wrap">{{ mapping.instance_name }} / {{ mapping.mapping_index }}</td>
                        <td class="a2s-path-cell">
                          <div class="a2s-mono">{{ mapping.source_dir }}</div>
                          <div class="a2s-mono text-medium-emphasis">→ {{ mapping.output_dir }}</div>
                        </td>
                        <td class="text-caption text-no-wrap">
                          <div>{{ formatTimestamp(mapping.started_at) }}</div>
                          <div class="text-medium-emphasis">{{ formatTimestamp(mapping.finished_at) }}</div>
                        </td>
                        <td>
                          <VChip size="x-small" :color="stateColor(mapping.state)" variant="tonal" label>{{ stateLabel(mapping.state) }}</VChip>
                          <div v-if="mapping.error" class="text-error text-caption mt-1">{{ mapping.error }}</div>
                        </td>
                        <td
                          v-for="row in counterRowsFor(mapping.counters)"
                          :key="`${mapping.identity}-${row.key}`"
                          class="text-center"
                          :class="{ 'text-error font-weight-bold': row.key === 'failed' && row.value > 0, 'text-disabled': !row.value }"
                        >
                          {{ row.value }}
                        </td>
                      </tr>
                    </tbody>
                  </VTable>
                </div>
              </VExpansionPanelText>
            </VExpansionPanel>
          </VExpansionPanels>

          <VPagination
            v-if="historyPage.total && !historyLoading && !historyError"
            v-model="historyPageNumber"
            :length="historyPageCount"
            :total-visible="7"
            density="comfortable"
            size="small"
          />
        </div>
      </VWindowItem>
    </VWindow>
  </div>
</template>

<style scoped>
.a2s-page {
  container-type: inline-size;
  width: 100%;
  display: flex;
  flex-direction: column;
  background: rgb(var(--v-theme-surface));
  color: rgb(var(--v-theme-on-surface));
}

/* ── 顶栏 ── */
.a2s-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 0.75rem 0.5rem 1.25rem;
}
.a2s-head-text { flex: 1; min-width: 0; }
.a2s-head-title { font-size: 1.0625rem; font-weight: 600; line-height: 1.4; }
.a2s-head-sub { font-size: 0.75rem; color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-tabs { padding: 0 0.75rem; }
.a2s-head .v-btn + .v-btn { margin-inline-start: -0.5rem; }

.a2s-main {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
  padding: 1.25rem 1.5rem 1.75rem;
}
.a2s-mono { font-family: ui-monospace, 'SF Mono', Consolas, monospace; font-size: 0.8125rem; overflow-wrap: anywhere; }

/* ── 运行概览 ── */
.a2s-panel {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 12px;
  overflow: hidden;
}
.a2s-panel-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem 1rem;
  padding: 1rem 1.25rem;
  background: rgba(var(--v-theme-on-surface), 0.02);
}
.a2s-panel-text { flex: 1 1 14rem; min-width: 0; }
.a2s-panel-title { display: flex; align-items: center; gap: 0.625rem; font-size: 0.9375rem; font-weight: 600; }
.a2s-state {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.0625rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 500;
  background: rgba(var(--v-theme-on-surface), 0.06);
}
.a2s-state-dot { width: 0.4375rem; height: 0.4375rem; border-radius: 50%; background: currentColor; }
.a2s-state-dot--pulse { animation: a2s-pulse 1.4s ease-in-out infinite; }
@keyframes a2s-pulse { 50% { opacity: 0.3; } }
.a2s-panel-sub { margin-top: 0.125rem; font-size: 0.8125rem; color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-panel-actions { display: flex; gap: 0.5rem; margin-inline-start: auto; }

.a2s-stats {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}
.a2s-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.125rem;
  padding: 0.875rem 0.5rem;
}
.a2s-stat + .a2s-stat { border-inline-start: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.a2s-stat-icon { color: rgba(var(--v-theme-on-surface), 0.45); margin-bottom: 0.125rem; }
.a2s-stat-value { font-size: 1.375rem; font-weight: 600; line-height: 1.2; font-variant-numeric: tabular-nums; }
.a2s-stat-label { font-size: 0.75rem; color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-stat--error .a2s-stat-value,
.a2s-stat--error .a2s-stat-icon { color: rgb(var(--v-theme-error)); }
.a2s-stats--compact {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
}
.a2s-stats--compact .a2s-stat { padding: 0.5rem; }
.a2s-stats--compact .a2s-stat-value { font-size: 1.0625rem; }

/* ── 分区 ── */
.a2s-section { display: flex; flex-direction: column; gap: 0.75rem; }
.a2s-section-head { display: flex; flex-direction: column; gap: 0.125rem; }
.a2s-section-title { font-size: 0.9375rem; font-weight: 600; line-height: 1.5; }
.a2s-section-desc { margin: 0; font-size: 0.8125rem; line-height: 1.5; color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-count {
  padding: 0 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  background: rgba(var(--v-theme-on-surface), 0.08);
}

/* ── 映射列表 ── */
.a2s-instance { display: flex; flex-direction: column; gap: 0.5rem; }
.a2s-instance + .a2s-instance { margin-top: 0.5rem; }
.a2s-instance-head { display: flex; align-items: center; gap: 0.5rem; font-size: 0.875rem; font-weight: 600; }
.a2s-instance-icon { color: rgb(var(--v-theme-primary)); }
.a2s-instance-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.a2s-mapping {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.625rem 0.75rem;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
  transition: background-color 0.15s;
}
.a2s-mapping:hover { background: rgba(var(--v-theme-on-surface), 0.03); }
.a2s-mapping--empty { font-size: 0.8125rem; color: rgba(var(--v-theme-on-surface), 0.6); border-style: dashed; }
.a2s-mapping-index {
  display: inline-grid;
  place-items: center;
  width: 1.5rem;
  height: 1.5rem;
  flex-shrink: 0;
  border-radius: 6px;
  font-size: 0.75rem;
  font-weight: 600;
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
}
.a2s-mapping-paths { flex: 1; min-width: 0; }
.a2s-mapping-output { color: rgba(var(--v-theme-on-surface), 0.6); font-size: 0.75rem; }
.a2s-mapping-actions { display: flex; flex-wrap: wrap; gap: 0.25rem; flex-shrink: 0; }

/* ── 表格 ── */
.a2s-table-wrap {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
  overflow: hidden;
}
.a2s-table :deep(th) { font-size: 0.75rem !important; white-space: nowrap; color: rgba(var(--v-theme-on-surface), 0.6) !important; }
.a2s-table :deep(td) { font-size: 0.8125rem; padding-block: 0.375rem !important; }
.a2s-path-cell { min-width: 12rem; max-width: 24rem; }

/* ── 历史 ── */
.a2s-history-toolbar { display: flex; align-items: center; gap: 0.5rem; }
.a2s-page-size { max-width: 7rem; }
.a2s-history {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
  overflow: hidden;
}
.a2s-history :deep(.v-expansion-panel-title) { min-height: 3rem; padding: 0.625rem 1rem; }
.a2s-history :deep(.v-expansion-panel-text__wrapper) { padding: 0.25rem 1rem 1rem; }
.a2s-history-title { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 0.375rem 0.75rem; }
.a2s-history-time { font-weight: 600; font-size: 0.875rem; font-variant-numeric: tabular-nums; }
.a2s-history-summary { font-size: 0.75rem; color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-history-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem 1.25rem;
  margin-bottom: 0.75rem;
  font-size: 0.8125rem;
  color: rgba(var(--v-theme-on-surface), 0.7);
}

/* ── 空状态 ── */
.a2s-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2rem 1rem;
  border: 1px dashed rgba(var(--v-border-color), 0.4);
  border-radius: 10px;
  text-align: center;
}
.a2s-empty-text { font-size: 0.8125rem; color: rgba(var(--v-theme-on-surface), 0.6); }

/* ── 窄宽度 ── */
@container (max-width: 680px) {
  .a2s-main { padding: 1rem; gap: 1.25rem; }
  .a2s-stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .a2s-stat:nth-child(4) { border-inline-start: 0; }
  .a2s-stat:nth-child(n + 4) { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
  .a2s-panel-actions { width: 100%; }
  .a2s-panel-actions .v-btn { flex: 1; }
  .a2s-mapping { flex-wrap: wrap; }
  .a2s-mapping-paths { flex-basis: calc(100% - 2.25rem); }
  .a2s-mapping-actions { width: 100%; justify-content: flex-end; }
}
</style>
