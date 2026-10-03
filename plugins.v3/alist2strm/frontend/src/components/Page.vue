<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { getErrorMessage, readConfiguration, readScanStatus, startMappingScan, startScan, stopScan } from '@/api'
import { configurationEditorState } from '@/configState'
import type {
  ConfigModel,
  DirectoryMapping,
  OpenListInstance,
  PluginApi,
  ScanCounters,
  ScanMappingStatus,
  ScanStatus,
  ScanTarget,
} from '@/types'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId?: string
}>(), {
  pluginId: '',
})

const emit = defineEmits<{
  close: []
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
let pollTimer: ReturnType<typeof window.setInterval> | null = null
let pageMounted = false

const hasUnsavedChanges = computed(() => configurationEditorState.dirty)
const statusLabel = computed(() => {
  if (status.value.state === 'running') return '进行中'
  if (status.value.state === 'stopping') return '正在停止'
  if (status.value.state === 'stopped') return '已停止'
  if (status.value.state === 'success') return '已完成'
  if (status.value.state === 'partial_failure') return '部分失败'
  if (status.value.state === 'failed') return '失败'
  return '空闲'
})
const statusColor = computed(() => {
  if (status.value.state === 'failed' || status.value.state === 'partial_failure') return 'error'
  if (status.value.state === 'success') return 'success'
  if (status.value.state === 'stopped' || status.value.state === 'stopping') return 'warning'
  return 'info'
})
const counterRows = computed(() => [
  { key: 'generated', label: '生成', value: status.value.counters.generated },
  { key: 'downloaded', label: '下载', value: status.value.counters.downloaded },
  { key: 'updated', label: '更新', value: status.value.counters.updated },
  { key: 'skipped', label: '跳过', value: status.value.counters.skipped },
  { key: 'deleted', label: '删除', value: status.value.counters.deleted },
  { key: 'failed', label: '失败', value: status.value.counters.failed },
])

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
  stopPolling()
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

function emptyConfig(): ConfigModel {
  return {
    instances: [],
    schedule_enabled: false,
    schedule_cron: '0 3 * * *',
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

async function runAll(): Promise<void> {
  triggering.value = true
  try {
    const response = await startScan(props.api, pluginId.value)
    showResult(response.accepted ? 'success' : 'warning', response.message)
  } catch (error) {
    showResult('error', `启动扫描失败：${getErrorMessage(error, '请稍后重试')}`)
  } finally {
    triggering.value = false
    await refreshStatus()
  }
}

async function runMapping(instance: OpenListInstance, mapping: DirectoryMapping, index: number): Promise<void> {
  const target: ScanTarget = {
    instance_id: instance.id,
    mapping_index: index + 1,
    source_dir: mapping.source_dir,
  }
  triggering.value = true
  try {
    const response = await startMappingScan(props.api, pluginId.value, target)
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

function mappingStatus(mapping: ScanMappingStatus): string {
  if (mapping.state === 'running') return '进行中'
  if (mapping.state === 'stopping') return '正在停止'
  if (mapping.state === 'stopped') return '已停止'
  if (mapping.state === 'partial_failure') return '部分失败'
  if (mapping.state === 'failed') return '失败'
  if (mapping.state === 'success') return '已完成'
  return '等待中'
}
</script>

<template>
  <VCard class="alist2strm-page" flat>
    <VCardTitle class="d-flex align-center justify-space-between">
      <span>Alist2Strm 运行</span>
      <VBtn icon="mdi-close" variant="text" aria-label="关闭" @click="closePage" />
    </VCardTitle>
    <VTabs v-model="activeTab" class="px-4" density="comfortable">
      <VTab value="run">运行</VTab>
    </VTabs>
    <VWindow v-model="activeTab">
      <VWindowItem value="run">
        <VCardText>
          <VAlert v-if="hasUnsavedChanges" type="warning" variant="tonal" class="mb-4">
            配置弹窗有未保存修改；本次运行只使用已保存配置，当前修改将在保存后下一次运行生效。
          </VAlert>
          <VAlert v-if="message" :type="messageTone" variant="tonal" closable class="mb-4" @click:close="message = ''">
            {{ message }}
          </VAlert>

          <div class="d-flex flex-wrap align-center ga-3 mb-4">
            <VBtn
              color="primary"
              prepend-icon="mdi-play"
              :loading="triggering"
              :disabled="loading || triggering || status.running || !config.instances.length"
              @click="runAll"
            >
              运行全部
            </VBtn>
            <VBtn
              color="warning"
              prepend-icon="mdi-stop"
              :loading="stopping"
              :disabled="loading || stopping || triggering || !status.running"
              @click="stopScanRun"
            >
              停止扫描
            </VBtn>
            <VChip :color="statusColor" variant="tonal">状态：{{ statusLabel }}</VChip>
            <span v-if="status.current_mapping" class="text-body-2">当前映射：{{ status.current_mapping }}</span>
          </div>

          <VTable density="compact" class="mb-6">
            <thead>
              <tr>
                <th v-for="row in counterRows" :key="row.key" class="text-center">{{ row.label }}</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td v-for="row in counterRows" :key="row.key" class="text-center">{{ row.value }}</td>
              </tr>
            </tbody>
          </VTable>

          <VAlert v-if="!loading && !config.instances.length" type="info" variant="tonal">
            尚未配置实例与映射，请先保存配置。
          </VAlert>
          <div v-for="instance in config.instances" :key="instance.id || instance.name" class="mapping-group mb-5">
            <div class="text-subtitle-1 mb-2">{{ instance.name }}</div>
            <div v-if="!instance.mappings.length" class="text-body-2 text-medium-emphasis">该实例尚未配置映射。</div>
            <div v-for="(mapping, index) in instance.mappings" :key="`${instance.id}-${index}`" class="mapping-row">
              <div class="mapping-path">
                <div>{{ mapping.source_dir }}</div>
                <div class="text-caption text-medium-emphasis">→ {{ mapping.output_dir }}</div>
              </div>
              <VBtn
                size="small"
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

          <div v-if="status.mappings.length" class="mt-6">
            <div class="text-subtitle-1 mb-2">本次映射状态</div>
            <div v-if="status.counters.failed" class="text-caption text-medium-emphasis mb-2">
              失败请按运行时间、实例和映射查阅宿主日志。
            </div>
            <VTable density="compact">
              <thead>
                <tr>
                  <th>映射</th>
                  <th>状态</th>
                  <th v-for="row in counterRows" :key="`mapping-${row.key}`" class="text-center">{{ row.label }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="mapping in status.mappings" :key="`${mapping.instance_id}-${mapping.mapping_index}`">
                  <td>{{ mapping.instance_name }}：{{ mapping.source_dir }}</td>
                  <td>
                    {{ mappingStatus(mapping) }}
                    <div v-if="mapping.error" class="text-error text-caption">{{ mapping.error }}</div>
                  </td>
                  <td class="text-center">{{ mapping.counters.generated }}</td>
                  <td class="text-center">{{ mapping.counters.downloaded }}</td>
                  <td class="text-center">{{ mapping.counters.updated }}</td>
                  <td class="text-center">{{ mapping.counters.skipped }}</td>
                  <td class="text-center">{{ mapping.counters.deleted }}</td>
                  <td class="text-center">{{ mapping.counters.failed }}</td>
                </tr>
              </tbody>
            </VTable>
          </div>
        </VCardText>
      </VWindowItem>
    </VWindow>
  </VCard>
</template>

<style scoped>
.alist2strm-page { width: 100%; color: rgb(var(--v-theme-on-surface)); }
.mapping-group { border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); padding-bottom: 1rem; }
.mapping-row { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: .5rem 0; }
.mapping-path { min-width: 0; overflow-wrap: anywhere; }
@media (max-width: 600px) {
  .mapping-row { align-items: flex-start; flex-direction: column; }
}
</style>
