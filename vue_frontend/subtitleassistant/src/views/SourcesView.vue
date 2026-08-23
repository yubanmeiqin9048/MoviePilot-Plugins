<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { getErrorMessage, listSourceStatus, refreshSourceStatus } from '@/api/client'
import DetailRow from '@/components/DetailRow.vue'
import EmptyState from '@/components/EmptyState.vue'
import StateChip from '@/components/StateChip.vue'
import type { PluginApi, SourceStatusItem, SubtitleSource } from '@/types'
import {
  displayValue,
  formatDate,
  formatDuration,
  friendlyKey,
  sourceHealthStates,
  sourceLabels,
} from '@/types/presentation'

const props = defineProps<{
  api: PluginApi
  pluginId: string
  active: boolean
}>()

const emit = defineEmits<{ action: [] }>()
const sourceOrder: SubtitleSource[] = ['moviepilot', 'opensubtitles', 'assrt']
const items = ref<SourceStatusItem[]>([])
const loading = ref(false)
const refreshing = ref(false)
const loaded = ref(false)
const error = ref('')
const staleError = ref('')
const openPanels = ref<number[]>([])
let requestId = 0

const orderedItems = computed(() => [...items.value].sort(
  (left, right) => sourceOrder.indexOf(left.source) - sourceOrder.indexOf(right.source),
))

watch(
  () => props.active,
  active => {
    if (active) void loadStatus(loaded.value)
  },
  { immediate: true },
)

async function loadStatus(silent = false): Promise<void> {
  const currentRequest = ++requestId
  if (!silent) loading.value = true
  error.value = ''
  try {
    const response = await listSourceStatus(props.api, props.pluginId)
    if (currentRequest !== requestId) return
    items.value = response
    loaded.value = true
    error.value = ''
    staleError.value = ''
  } catch (requestError) {
    if (currentRequest !== requestId) return
    const message = getErrorMessage(requestError, '字幕源状态加载失败')
    if (loaded.value && items.value.length) staleError.value = message
    else error.value = message
  } finally {
    if (currentRequest === requestId) loading.value = false
  }
}

async function refreshAll(): Promise<void> {
  if (refreshing.value) return
  refreshing.value = true
  staleError.value = ''
  try {
    await refreshSourceStatus(props.api, props.pluginId)
    await loadStatus(true)
    emit('action')
  } catch (requestError) {
    staleError.value = getErrorMessage(requestError, '字幕源状态刷新失败')
  } finally {
    refreshing.value = false
  }
}

function detailEntries(item: SourceStatusItem): Array<[string, unknown]> {
  return Object.entries(item.details || {})
}

function detailValue(key: string, value: unknown): string {
  if ((key.endsWith('_at') || key.endsWith('_until')) && typeof value === 'string') return formatDate(value)
  if (key.endsWith('_duration_ms') && typeof value === 'number') return formatDuration(value)
  return displayValue(value)
}
</script>

<template>
  <section class="view-shell" aria-labelledby="sources-view-title">
    <header class="view-header">
      <div>
        <h2 id="sources-view-title">字幕源状态</h2>
        <p>状态来自最近任务观测或本次手动检测，不进行后台健康轮询。</p>
      </div>
      <VBtn
        variant="tonal"
        prepend-icon="mdi-refresh"
        :loading="refreshing"
        :disabled="loading"
        @click="refreshAll"
      >
        刷新状态
      </VBtn>
    </header>

    <VAlert v-if="staleError" type="warning" variant="tonal" density="compact" class="mb-3">
      <div class="inline-alert">
        <span>刷新未完成，当前状态可能已过期：{{ staleError }}</span>
        <VBtn size="small" variant="text" prepend-icon="mdi-refresh" @click="refreshAll">重试</VBtn>
      </div>
    </VAlert>

    <div v-if="loading" class="source-skeleton" aria-label="正在加载字幕源状态">
      <VSkeletonLoader type="list-item-three-line@3" />
    </div>
    <VAlert v-else-if="error" type="error" variant="tonal" title="字幕源状态加载失败">
      <div>{{ error }}</div>
      <VBtn class="mt-2" size="small" variant="text" prepend-icon="mdi-refresh" @click="loadStatus()">重试</VBtn>
    </VAlert>
    <EmptyState v-else-if="!orderedItems.length" icon="mdi-database-off-outline" title="没有字幕源状态" message="插件尚未返回字幕源状态，请刷新后重试。">
      <template #actions><VBtn variant="tonal" prepend-icon="mdi-refresh" @click="refreshAll">刷新状态</VBtn></template>
    </EmptyState>

    <VExpansionPanels v-else v-model="openPanels" multiple variant="accordion" class="source-list">
      <VExpansionPanel v-for="item in orderedItems" :key="item.source" class="source-panel">
        <VExpansionPanelTitle>
          <div class="source-summary">
            <div class="source-identity">
              <VIcon :icon="item.source === 'moviepilot' ? 'mdi-server-network' : item.source === 'opensubtitles' ? 'mdi-closed-caption-outline' : 'mdi-subtitles-outline'" size="20" />
              <div><strong>{{ sourceLabels[item.source] }}</strong><span>{{ item.enabled ? '已启用' : '未启用' }} · {{ item.configured ? '配置完整' : '配置不完整' }}</span></div>
            </div>
            <StateChip :state="sourceHealthStates[item.health]" />
            <div class="source-time"><span>最近成功</span><strong>{{ formatDate(item.last_success_at) }}</strong></div>
            <div class="source-error"><span>最近错误</span><strong>{{ item.last_error_summary || '无' }}</strong></div>
          </div>
        </VExpansionPanelTitle>
        <VExpansionPanelText>
          <div class="source-detail">
            <dl>
              <DetailRow label="启用状态">{{ item.enabled ? '已启用' : '已关闭' }}</DetailRow>
              <DetailRow label="配置状态">{{ item.configured ? '配置完整' : '配置不完整' }}</DetailRow>
              <DetailRow label="健康状态"><StateChip :state="sourceHealthStates[item.health]" /></DetailRow>
              <DetailRow label="最近检测">{{ formatDate(item.last_checked_at) }}</DetailRow>
              <DetailRow label="最近成功">{{ formatDate(item.last_success_at) }}</DetailRow>
              <DetailRow label="最近异常">{{ formatDate(item.last_error_at) }}</DetailRow>
              <DetailRow label="最近耗时">{{ formatDuration(item.last_duration_ms) }}</DetailRow>
              <DetailRow v-if="item.last_error_summary" label="错误摘要"><span class="error-text">{{ item.last_error_summary }}</span></DetailRow>
            </dl>

            <VDivider class="my-3" />
            <div class="source-observations">
              <h3>{{ item.source === 'moviepilot' ? '站点聚合观测' : item.source === 'opensubtitles' ? '会话与额度' : '配额与限流' }}</h3>
              <dl v-if="detailEntries(item).length">
                <DetailRow v-for="[key, value] in detailEntries(item)" :key="key" :label="friendlyKey(key)">{{ detailValue(key, value) }}</DetailRow>
              </dl>
              <p v-else class="muted">尚无来源特有观测</p>
              <p v-if="item.source === 'moviepilot'" class="source-note">未返回候选的站点只表示本次无返回，不推断为异常。</p>
              <p v-if="item.source === 'assrt'" class="source-note">字幕服务由 <a href="https://assrt.net" target="_blank" rel="noopener noreferrer">assrt.net</a> 提供。</p>
            </div>
          </div>
        </VExpansionPanelText>
      </VExpansionPanel>
    </VExpansionPanels>
  </section>
</template>

<style scoped>
.view-shell { min-width: 0; }
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 1rem; font-weight: 650; letter-spacing: 0; }
.view-header p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.inline-alert { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.source-skeleton, .source-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.source-panel + .source-panel { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.source-summary { display: grid; width: 100%; min-width: 0; grid-template-columns: minmax(14rem, 1.2fr) minmax(7rem, auto) minmax(10rem, 0.8fr) minmax(12rem, 1fr); align-items: center; gap: 1rem; padding-right: 0.75rem; }
.source-identity { display: flex; min-width: 0; align-items: center; gap: 0.625rem; }
.source-identity div { min-width: 0; }
.source-identity strong, .source-identity span, .source-time span, .source-time strong, .source-error span, .source-error strong { display: block; }
.source-identity strong { color: rgb(var(--v-theme-on-surface)); font-size: 0.875rem; }
.source-identity span, .source-time span, .source-error span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.source-time strong, .source-error strong { margin-top: 0.125rem; overflow: hidden; font-size: 0.8125rem; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.source-detail { display: grid; grid-template-columns: minmax(15rem, 1fr) minmax(16rem, 1.2fr); gap: 2rem; }
.source-observations h3 { margin: 0 0 0.5rem; font-size: 0.875rem; letter-spacing: 0; }
.source-note, .muted { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; line-height: 1.55; }
.source-note a { color: rgb(var(--v-theme-primary)); }
.source-note a:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
.error-text { color: rgb(var(--v-theme-error)); }
@media (max-width: 959px) { .view-header { align-items: stretch; flex-direction: column; } .view-header :deep(.v-btn) { align-self: flex-start; } .source-summary { grid-template-columns: minmax(0, 1fr) auto; gap: 0.625rem; } .source-time, .source-error { grid-column: 1 / -1; } .source-detail { grid-template-columns: 1fr; gap: 0; } }
@media (max-width: 37.5rem) { .source-summary { grid-template-columns: 1fr; } .source-summary :deep(.v-chip) { justify-self: start; } }
</style>
