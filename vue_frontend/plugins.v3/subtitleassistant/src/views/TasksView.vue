<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

import { deleteTask, getErrorMessage, getTask, listTasks } from '@/api/client'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import CopyValue from '@/components/CopyValue.vue'
import DetailDrawer from '@/components/DetailDrawer.vue'
import DetailRow from '@/components/DetailRow.vue'
import EmptyState from '@/components/EmptyState.vue'
import StateChip from '@/components/StateChip.vue'
import { useDebouncedValue } from '@/composables/useDebouncedValue'
import type { PluginApi, TaskDetail, TaskListItem, TaskStatus } from '@/types'
import {
  displayValue,
  elapsedDuration,
  formatDate,
  formatDuration,
  friendlyKey,
  isTerminalTask,
  mediaLabel,
  mediaTypeLabels,
  packageLabels,
  sourceLabels,
  taskStates,
  taskTriggerLabels,
} from '@/types/presentation'

const props = defineProps<{
  api: PluginApi
  pluginId: string
  active: boolean
}>()

const emit = defineEmits<{ action: [] }>()
const { mdAndUp } = useDisplay()

const statusOptions: Array<{ title: string; value: TaskStatus | '' }> = [
  { title: '全部状态', value: '' },
  { title: '等待中', value: 'queued' },
  { title: '处理中', value: 'processing' },
  { title: '成功', value: 'success' },
  { title: '跳过', value: 'skipped' },
  { title: '失败', value: 'failed' },
  { title: '已中断', value: 'interrupted' },
]
const pageSizeOptions = [25, 50, 100]

const items = ref<TaskListItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref<25 | 50 | 100>(25)
const searchInput = ref('')
const search = useDebouncedValue(searchInput)
const status = ref<TaskStatus | ''>('')
const loading = ref(false)
const refreshing = ref(false)
const loaded = ref(false)
const error = ref('')
const staleError = ref('')
const selectedId = ref('')
const detail = ref<TaskDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const openDetailPanels = ref<number[]>([0, 1])
const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTarget = ref<TaskListItem | null>(null)
const pageVisible = ref(typeof document === 'undefined' || !document.hidden)
const tableFrame = ref<HTMLElement | null>(null)
let listRequest = 0
let detailRequest = 0
let detailRefreshInFlight = false
let pollTimer: ReturnType<typeof setInterval> | undefined

const isDesktop = computed(() => mdAndUp.value)
const hasFilters = computed(() => Boolean(search.value || status.value))
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const canLoadMore = computed(() => !isDesktop.value && items.value.length < total.value)
const selectedListItem = computed(() => items.value.find(item => item.id === selectedId.value) || detail.value || deleteTarget.value)

watch(
  () => props.active,
  active => {
    if (!active) {
      stopPolling()
      return
    }
    void loadPage({ silent: loaded.value, requestedPage: isDesktop.value ? page.value : 1 })
    syncPolling()
  },
  { immediate: true },
)

watch([search, status], () => {
  scrollTableToTop()
  page.value = 1
  items.value = []
  if (props.active) void loadPage({ requestedPage: 1 })
})

watch(pageSize, () => {
  if (!isDesktop.value) return
  scrollTableToTop()
  page.value = 1
  if (props.active) void loadPage({ requestedPage: 1 })
})

watch(page, next => {
  if (!isDesktop.value || !props.active) return
  scrollTableToTop()
  void loadPage({ requestedPage: next })
})

watch(isDesktop, () => {
  page.value = 1
  items.value = []
  if (props.active) void loadPage({ requestedPage: 1 })
})

function scrollTableToTop(): void {
  const wrapper = tableFrame.value?.querySelector<HTMLElement>('.v-table__wrapper')
  if (wrapper) wrapper.scrollTop = 0
}

onMounted(() => {
  pageVisible.value = !document.hidden
  document.addEventListener('visibilitychange', handleVisibilityChange)
  syncPolling()
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', handleVisibilityChange)
  stopPolling()
})

function stopPolling(): void {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = undefined
}

function syncPolling(): void {
  stopPolling()
  if (!props.active || !pageVisible.value) return
  pollTimer = window.setInterval(() => {
    void loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
  }, 3000)
}

function handleVisibilityChange(): void {
  pageVisible.value = !document.hidden
  syncPolling()
  if (props.active && pageVisible.value) {
    void loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
  }
}

async function loadPage(options: { silent?: boolean; append?: boolean; requestedPage?: number } = {}): Promise<void> {
  const requestId = ++listRequest
  const requestedPage = options.requestedPage ?? page.value
  if (!options.silent) {
    if (!loaded.value || !items.value.length) loading.value = true
    else refreshing.value = true
    error.value = ''
  }
  try {
    const response = await listTasks(props.api, props.pluginId, {
      page: requestedPage,
      pageSize: isDesktop.value ? pageSize.value : 25,
      search: search.value,
      status: status.value,
    })
    if (requestId !== listRequest) return

    if (options.append) {
      const known = new Set(items.value.map(item => item.id))
      items.value = [...items.value, ...response.items.filter(item => !known.has(item.id))]
      page.value = requestedPage
    } else if (options.silent && !isDesktop.value && page.value > 1) {
      const freshIds = new Set(response.items.map(item => item.id))
      items.value = [...response.items, ...items.value.filter(item => !freshIds.has(item.id))].slice(0, total.value || undefined)
    } else {
      items.value = response.items
      if (!isDesktop.value) page.value = requestedPage
    }
    total.value = response.total
    loaded.value = true
    error.value = ''
    staleError.value = ''
    if (options.silent && selectedId.value) void refreshOpenDetail()
  } catch (requestError) {
    if (requestId !== listRequest) return
    const message = getErrorMessage(requestError, '任务列表加载失败')
    if (loaded.value && items.value.length) staleError.value = message
    else error.value = message
  } finally {
    if (requestId === listRequest) {
      loading.value = false
      refreshing.value = false
    }
  }
}

async function loadMore(): Promise<void> {
  if (!canLoadMore.value || refreshing.value) return
  refreshing.value = true
  await loadPage({ append: true, requestedPage: page.value + 1 })
  refreshing.value = false
}

async function openDetail(item: TaskListItem): Promise<void> {
  selectedId.value = item.id
  detail.value = null
  detailError.value = ''
  openDetailPanels.value = [0, 1]
  detailLoading.value = true
  const requestId = ++detailRequest
  try {
    const response = await getTask(props.api, props.pluginId, item.id)
    if (requestId === detailRequest && selectedId.value === item.id) detail.value = response
  } catch (requestError) {
    if (requestId === detailRequest) detailError.value = getErrorMessage(requestError, '任务详情加载失败')
  } finally {
    if (requestId === detailRequest) detailLoading.value = false
  }
}

async function refreshOpenDetail(): Promise<void> {
  const taskId = selectedId.value
  if (!taskId || detailLoading.value || detailRefreshInFlight) return
  detailRefreshInFlight = true
  const requestId = ++detailRequest
  try {
    const response = await getTask(props.api, props.pluginId, taskId)
    if (requestId === detailRequest && selectedId.value === taskId) detail.value = response
  } catch (requestError) {
    if (requestId === detailRequest) {
      staleError.value = `任务详情刷新失败：${getErrorMessage(requestError, '无法读取最新任务详情')}`
    }
  } finally {
    detailRefreshInFlight = false
  }
}

function closeDetail(): void {
  selectedId.value = ''
  detail.value = null
  detailError.value = ''
  detailRequest += 1
}

function updateDetailOpen(open: boolean): void {
  if (!open) closeDetail()
}

function requestDelete(item: TaskListItem): void {
  if (!isTerminalTask(item.status)) return
  deleteTarget.value = item
  deleteOpen.value = true
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  try {
    await deleteTask(props.api, props.pluginId, target.id)
    deleteOpen.value = false
    deleteTarget.value = null
    if (selectedId.value === target.id) closeDetail()
    if (isDesktop.value && items.value.length === 1 && page.value > 1) page.value -= 1
    else await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
    emit('action')
  } catch (requestError) {
    staleError.value = getErrorMessage(requestError, '任务记录删除失败')
    deleteOpen.value = false
  } finally {
    deleting.value = false
  }
}

function clearFilters(): void {
  searchInput.value = ''
  status.value = ''
}

function resultText(item: TaskListItem): string {
  if (item.status === 'success') {
    return [
      item.result_source ? sourceLabels[item.result_source] : '',
      item.result_package_scope ? packageLabels[item.result_package_scope] : '',
      item.result_format || '',
    ].filter(Boolean).join(' · ') || '字幕已落盘'
  }
  if (item.status === 'queued') return '等待处理'
  if (item.status === 'processing') return '正在处理'
  return item.reason_message || item.reason_code || '未记录原因'
}

function taskTime(item: TaskListItem): string {
  const start = item.started_at || item.created_at
  const duration = item.status === 'processing' ? elapsedDuration(item.started_at) : formatDuration(item.duration_ms)
  return `${formatDate(start)} · ${duration}`
}

function detailEntries(value: Record<string, unknown>): Array<[string, unknown]> {
  return Object.entries(value || {})
}
</script>

<template>
  <section class="view-shell" aria-labelledby="tasks-view-title">
    <div class="view-controls">
      <header class="view-header">
        <div>
          <h2 id="tasks-view-title">任务</h2>
          <p>自动处理记录按运行状态与完成时间排列。</p>
        </div>
        <VTooltip text="刷新任务">
          <template #activator="{ props: tooltipProps }">
            <VBtn
              v-bind="tooltipProps"
              icon="mdi-refresh"
              size="small"
              variant="text"
              :loading="refreshing"
              aria-label="刷新任务"
              @click="loadPage({ silent: loaded, requestedPage: isDesktop ? page : 1 })"
            />
          </template>
        </VTooltip>
      </header>

      <div class="filter-bar">
        <VTextField
          v-model="searchInput"
          label="搜索任务"
          placeholder="媒体、目标文件或原因"
          prepend-inner-icon="mdi-magnify"
          clearable
          hide-details
          :density="isDesktop ? 'compact' : 'comfortable'"
        />
        <VSelect v-model="status" label="状态" :items="statusOptions" hide-details :density="isDesktop ? 'compact' : 'comfortable'" />
      </div>
    </div>

    <VAlert v-if="staleError" type="warning" variant="tonal" density="compact" class="mb-3">
      <div class="inline-alert">
        <span>刷新失败，当前数据可能已过期：{{ staleError }}</span>
        <VBtn size="small" variant="text" prepend-icon="mdi-refresh" @click="loadPage({ silent: true })">重试</VBtn>
      </div>
    </VAlert>

    <div v-if="loading" class="table-frame" aria-label="正在加载任务">
      <VSkeletonLoader :type="isDesktop ? 'table-heading, table-row-divider@7' : 'list-item-two-line@6'" />
    </div>

    <VAlert v-else-if="error" type="error" variant="tonal" class="mt-3" title="任务加载失败">
      <div>{{ error }}</div>
      <VBtn class="mt-2" size="small" variant="text" prepend-icon="mdi-refresh" @click="loadPage()">重试</VBtn>
    </VAlert>

    <EmptyState
      v-else-if="!items.length && !hasFilters && !selectedId"
      icon="mdi-subtitles-outline"
      title="还没有字幕任务"
      message="MoviePilot 完成新的媒体整理后，符合条件的目标会在这里生成任务。"
    />

    <EmptyState
      v-else-if="!items.length && !selectedId"
      icon="mdi-filter-off-outline"
      title="没有符合条件的任务"
      message="调整搜索内容或状态筛选后再试。"
    >
      <template #actions>
        <VBtn variant="tonal" prepend-icon="mdi-filter-remove-outline" @click="clearFilters">清除条件</VBtn>
      </template>
    </EmptyState>

    <div v-else class="master-detail">
      <div class="master-pane">
        <div v-if="isDesktop" ref="tableFrame" class="table-frame">
          <VTable hover fixed-header height="100%" class="data-table">
            <thead>
              <tr>
                <th>媒体</th>
                <th>目标文件</th>
                <th>状态</th>
                <th>触发</th>
                <th>结果</th>
                <th>时间</th>
                <th class="actions-column">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="item in items"
                :key="item.id"
                class="selectable-row"
                :class="{ 'selectable-row--active': selectedId === item.id }"
                tabindex="0"
                role="button"
                :data-subtitleassistant-detail-trigger="`task:${item.id}`"
                :aria-label="`查看任务 ${mediaLabel(item.media_title, item.year, item.season, item.episode)}`"
                @click="openDetail(item)"
                @keydown.enter.prevent="openDetail(item)"
                @keydown.space.prevent="openDetail(item)"
              >
                <td>
                  <div class="primary-cell">
                    <VIcon :icon="item.media_type === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" size="18" />
                    <div>
                      <strong>{{ item.media_title }}</strong>
                      <span>{{ [mediaTypeLabels[item.media_type], item.year, item.season != null ? `S${String(item.season).padStart(2, '0')}` : '', item.episode != null ? `E${String(item.episode).padStart(2, '0')}` : ''].filter(Boolean).join(' · ') }}</span>
                    </div>
                  </div>
                </td>
                <td><span class="file-name" :title="item.target_file_name">{{ item.target_file_name }}</span></td>
                <td>
                  <StateChip :state="taskStates[item.status]" />
                </td>
                <td><span class="cell-note">{{ taskTriggerLabels[item.trigger] }}</span></td>
                <td><span class="result-text">{{ resultText(item) }}</span></td>
                <td><span class="time-text">{{ taskTime(item) }}</span></td>
                <td class="actions-column" @click.stop @keydown.stop>
                  <VTooltip text="查看详情">
                    <template #activator="{ props: tooltipProps }">
                      <VBtn v-bind="tooltipProps" icon="mdi-chevron-right" size="small" variant="text" aria-label="查看任务详情" @click="openDetail(item)" />
                    </template>
                  </VTooltip>
                  <VTooltip v-if="isTerminalTask(item.status)" text="删除任务记录">
                    <template #activator="{ props: tooltipProps }">
                      <VBtn v-bind="tooltipProps" icon="mdi-delete-outline" size="small" variant="text" color="error" aria-label="删除任务记录" @click="requestDelete(item)" />
                    </template>
                  </VTooltip>
                </td>
              </tr>
            </tbody>
          </VTable>
        </div>

        <VList v-else class="mobile-list" lines="three" role="list" aria-label="任务列表">
          <VListItem
            v-for="item in items"
            :key="item.id"
            class="mobile-list__item"
            role="listitem"
          >
            <template #prepend>
              <VIcon :icon="item.media_type === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" />
            </template>
            <button
              type="button"
              class="mobile-list__detail"
              :data-subtitleassistant-detail-trigger="`task:${item.id}`"
              :aria-label="`查看任务 ${mediaLabel(item.media_title, item.year, item.season, item.episode)}`"
              @click="openDetail(item)"
            >
              <VListItemTitle>{{ mediaLabel(item.media_title, item.year, item.season, item.episode) }}</VListItemTitle>
              <VListItemSubtitle class="mobile-subtitle">{{ item.target_file_name }}</VListItemSubtitle>
              <VListItemSubtitle class="mobile-meta">
                <StateChip :state="taskStates[item.status]" size="x-small" />
                <span>{{ formatDate(item.started_at || item.created_at) }}</span>
              </VListItemSubtitle>
            </button>
            <template #append>
              <VMenu v-if="isTerminalTask(item.status)">
                <template #activator="{ props: menuProps }">
                  <VBtn v-bind="menuProps" icon="mdi-dots-vertical" size="small" variant="text" aria-label="任务操作" @click.stop />
                </template>
                <VList density="compact" role="menu" aria-label="任务操作">
                  <VListItem role="menuitem" title="查看详情" prepend-icon="mdi-text-box-search-outline" @click="openDetail(item)" />
                  <VListItem role="menuitem" title="删除任务记录" prepend-icon="mdi-delete-outline" base-color="error" @click="requestDelete(item)" />
                </VList>
              </VMenu>
              <VIcon v-else icon="mdi-chevron-right" aria-hidden="true" />
            </template>
          </VListItem>
        </VList>

        <div v-if="isDesktop" class="pagination-bar">
          <span>共 {{ total }} 条</span>
          <VPagination v-model="page" :length="totalPages" :total-visible="5" density="comfortable" class="table-pagination" />
          <VSelect v-model="pageSize" :items="pageSizeOptions" label="每页" density="compact" class="page-size" />
        </div>
        <div v-else-if="canLoadMore" class="load-more">
          <VBtn variant="tonal" prepend-icon="mdi-chevron-down" :loading="refreshing" @click="loadMore">加载更多</VBtn>
        </div>
      </div>

      <DetailDrawer
        :model-value="Boolean(selectedId)"
        title="任务详情"
        :subtitle="detail?.target_file_name || selectedListItem?.target_file_name"
        close-label="关闭任务详情"
        :return-focus-key="selectedId ? `task:${selectedId}` : null"
        @update:model-value="updateDetailOpen"
      >
        <template #actions>
          <VTooltip v-if="selectedListItem && isTerminalTask(selectedListItem.status)" text="删除任务记录">
            <template #activator="{ props: tooltipProps }">
              <VBtn v-bind="tooltipProps" icon="mdi-delete-outline" color="error" variant="text" aria-label="删除任务记录" @click="requestDelete(selectedListItem)" />
            </template>
          </VTooltip>
        </template>

        <VSkeletonLoader v-if="detailLoading" class="detail-state" type="heading, paragraph, list-item-three-line@5" />
        <VAlert v-else-if="detailError" class="detail-state" type="error" variant="tonal">
          <div>{{ detailError }}</div>
          <VBtn v-if="selectedListItem" class="mt-2" size="small" variant="text" prepend-icon="mdi-refresh" @click="openDetail(selectedListItem)">重试</VBtn>
        </VAlert>
        <VExpansionPanels v-else-if="detail" v-model="openDetailPanels" multiple variant="accordion" class="detail-sections">
          <VExpansionPanel>
            <VExpansionPanelTitle>概览</VExpansionPanelTitle>
            <VExpansionPanelText>
              <dl>
                <DetailRow label="任务 ID"><CopyValue :value="detail.id" label="任务 ID" /></DetailRow>
                <DetailRow label="媒体">{{ mediaLabel(detail.media_title, detail.year, detail.season, detail.episode) }}</DetailRow>
                <DetailRow label="状态"><StateChip :state="taskStates[detail.status]" /></DetailRow>
                <DetailRow label="触发方式">{{ taskTriggerLabels[detail.trigger] }}</DetailRow>
                <DetailRow label="终态原因">{{ detail.reason_message || detail.reason_code || '无' }}</DetailRow>
                <DetailRow label="创建时间">{{ formatDate(detail.created_at) }}</DetailRow>
                <DetailRow label="开始时间">{{ formatDate(detail.started_at) }}</DetailRow>
                <DetailRow label="完成时间">{{ formatDate(detail.finished_at) }}</DetailRow>
                <DetailRow label="耗时">{{ detail.status === 'processing' ? elapsedDuration(detail.started_at) : formatDuration(detail.duration_ms) }}</DetailRow>
              </dl>
            </VExpansionPanelText>
          </VExpansionPanel>

          <VExpansionPanel>
            <VExpansionPanelTitle>目标</VExpansionPanelTitle>
            <VExpansionPanelText>
              <dl>
                <DetailRow label="整理历史 ID"><CopyValue :value="detail.target_history_id == null ? null : String(detail.target_history_id)" label="整理历史 ID" /></DetailRow>
                <DetailRow label="历史目标路径"><CopyValue :value="detail.history_target_path" label="历史目标路径" /></DetailRow>
                <DetailRow label="实际字幕目标"><CopyValue :value="detail.target_path" label="实际字幕目标路径" /></DetailRow>
                <DetailRow label="命中路径映射">
                  {{ detail.matched_path_mapping
                    ? `${detail.matched_path_mapping.source_prefix} → ${detail.matched_path_mapping.target_prefix}`
                    : '未命中' }}
                </DetailRow>
                <DetailRow label="目标视频存在">{{ detail.target_file_exists == null ? '未记录' : (detail.target_file_exists ? '是' : '否') }}</DetailRow>
                <DetailRow label="目标存储">{{ detail.target_storage || '未记录' }}</DetailRow>
                <DetailRow label="媒体类型">{{ mediaTypeLabels[detail.media_type] }}</DetailRow>
                <DetailRow label="TMDB ID">{{ detail.tmdb_id ?? '未记录' }}</DetailRow>
                <DetailRow label="IMDb ID">{{ detail.imdb_id || '未记录' }}</DetailRow>
              </dl>
            </VExpansionPanelText>
          </VExpansionPanel>

          <VExpansionPanel>
            <VExpansionPanelTitle>结果</VExpansionPanelTitle>
            <VExpansionPanelText>
              <dl>
                <DetailRow label="最终字幕"><CopyValue :value="detail.final_subtitle_path" label="字幕路径" /></DetailRow>
                <DetailRow label="结果来源">{{ detail.result_source ? sourceLabels[detail.result_source] : '无' }}</DetailRow>
                <DetailRow label="结果格式">{{ detail.result_format || '未记录' }}</DetailRow>
                <DetailRow label="结果范围">{{ detail.result_package_scope ? packageLabels[detail.result_package_scope] : '未记录' }}</DetailRow>
                <DetailRow v-for="(count, key) in detail.record_counts" :key="key" :label="friendlyKey(key)">{{ count }}</DetailRow>
              </dl>
              <VAlert v-if="detail.trigger === 'manual_candidate'" type="info" variant="tonal" density="compact" class="mt-3">
                这是人工字幕搜索选定候选后的下载任务；库存查询与自动准入筛选不会在此任务中重复执行。
              </VAlert>
              <dl v-if="detail.trigger === 'manual_candidate'" class="manual-summary mt-3">
                <DetailRow v-if="detail.manual_source" label="人工来源">{{ sourceLabels[detail.manual_source] }}</DetailRow>
                <DetailRow v-if="detail.actual_search_query" label="实际搜索词">{{ detail.actual_search_query }}</DetailRow>
                <DetailRow v-if="detail.manual_candidate_key" label="候选键"><CopyValue :value="detail.manual_candidate_key" label="候选键" /></DetailRow>
                <DetailRow v-for="[key, value] in detailEntries(detail.manual_candidate_summary)" :key="key" :label="friendlyKey(key)">{{ displayValue(value) }}</DetailRow>
              </dl>
            </VExpansionPanelText>
          </VExpansionPanel>
        </VExpansionPanels>
      </DetailDrawer>
    </div>

    <ConfirmDialog
      v-model="deleteOpen"
      title="删除任务记录"
      message="只会删除这条终态任务历史，不会删除匹配记录、插件数据文件或媒体目录中的字幕。此操作无法撤销。"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </section>
</template>

<style scoped>
.view-shell { min-width: 0; }
.view-controls {
  position: sticky;
  z-index: 3;
  inset-block-start: var(--layout-navbar-block-size, var(--v-layout-top, 0px));
  margin-block-end: 1rem;
  padding-block: 0.25rem 0.75rem;
  background: rgb(var(--v-theme-background));
  border-block-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  box-shadow: 0 0.25rem 0.75rem rgba(0, 0, 0, 0.06);
}
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-block-end: 0.75rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 1rem; font-weight: 650; letter-spacing: 0; }
.view-header p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.filter-bar { display: grid; grid-template-columns: minmax(15rem, 1fr) minmax(10rem, 14rem); gap: 0.75rem; }
.inline-alert { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.master-detail, .master-pane { min-width: 0; }
.table-frame { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.72); }
.table-frame :deep(.v-table__wrapper) { overscroll-behavior: contain; scrollbar-gutter: stable; }
.selectable-row { cursor: pointer; transition: background-color 180ms ease; }
.selectable-row:hover, .selectable-row--active { background: rgba(var(--v-theme-primary), 0.08); }
.selectable-row:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; scroll-margin-block-start: var(--v-table-header-height, 3rem); }
.primary-cell { display: flex; min-width: 12rem; align-items: flex-start; gap: 0.5rem; }
.primary-cell strong, .primary-cell span { display: block; }
.primary-cell strong { max-width: 16rem; overflow: hidden; color: rgb(var(--v-theme-on-surface)); font-size: 0.875rem; text-overflow: ellipsis; white-space: nowrap; }
.primary-cell span, .cell-note, .time-text { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.file-name { display: block; max-width: 14rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.result-text { display: -webkit-box; max-width: 18rem; overflow: hidden; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-size: 0.8125rem; }
.time-text { display: block; min-width: 9rem; line-height: 1.45; }
.actions-column { width: 6.5rem; text-align: end !important; white-space: nowrap; }
.pagination-bar { display: grid; grid-template-columns: auto 1fr 7rem; align-items: center; gap: 1rem; padding: 0.75rem 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.table-pagination { justify-self: center; }
.page-size { min-width: 7rem; }
.detail-state { margin: 1rem; }
.detail-sections { border-radius: 0; }
.muted { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; line-height: 1.5; }
.error-text { display: block; margin-top: 0.25rem; color: rgb(var(--v-theme-error)); font-size: 0.8125rem; }
.mobile-list { padding: 0; border-block: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: transparent; }
.mobile-list__item { min-height: 6rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.mobile-list__detail { display: grid; width: 100%; min-width: 0; padding: 0; border: 0; color: inherit; text-align: start; background: transparent; cursor: pointer; font: inherit; }
.mobile-list__detail:focus-visible { border-radius: 0.125rem; outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
.mobile-subtitle { overflow-wrap: anywhere; }
.mobile-meta { display: flex !important; align-items: center; gap: 0.5rem; margin-top: 0.375rem; }
.load-more { display: flex; justify-content: center; padding: 1rem; }

@media (min-width: 960px) {
  .view-shell {
    display: flex;
    block-size: 100%;
    min-block-size: 0;
    flex-direction: column;
    overflow: hidden;
  }
  .view-controls {
    position: static;
    z-index: auto;
    flex: 0 0 auto;
    margin-block-end: 0.5rem;
    padding-block: 0 0.5rem;
    box-shadow: none;
  }
  .view-header { min-block-size: 2.5rem; margin-block-end: 0.5rem; }
  .master-detail {
    flex: 1 1 auto;
    min-block-size: 0;
    overflow: hidden;
  }
  .master-pane {
    display: grid;
    block-size: 100%;
    min-block-size: 0;
    grid-template-rows: minmax(0, 1fr) auto;
  }
  .table-frame { min-block-size: 0; }
  .data-table { block-size: 100%; }
  .view-shell > .table-frame {
    flex: 1 1 auto;
    min-block-size: 0;
  }
  .pagination-bar { min-block-size: 3rem; padding-block: 0.375rem 0; }
}

@media (max-width: 959px) {
  .filter-bar { grid-template-columns: 1fr; }
}

@media (max-width: 37.5rem) {
  .view-controls { margin-block-end: 0.75rem; padding-block-end: 0.5rem; }
  .view-header { margin-block-end: 0.5rem; }
  .view-header p { display: none; }
  .filter-bar { grid-template-columns: minmax(0, 1fr) minmax(7.5rem, 9rem); gap: 0.5rem; }
}

@media (max-width: 26rem) {
  .filter-bar { grid-template-columns: minmax(0, 1fr); }
}

@media (prefers-reduced-motion: reduce) {
  .selectable-row { transition: none; }
}
</style>
