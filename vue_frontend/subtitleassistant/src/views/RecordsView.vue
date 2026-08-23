<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

import { deleteRecord, getErrorMessage, getRecord, listRecords } from '@/api/client'
import BatchRecordDeleteDialog from '@/components/BatchRecordDeleteDialog.vue'
import BatchRetargetDialog from '@/components/BatchRetargetDialog.vue'
import CopyValue from '@/components/CopyValue.vue'
import DetailDrawer from '@/components/DetailDrawer.vue'
import DetailRow from '@/components/DetailRow.vue'
import EmptyState from '@/components/EmptyState.vue'
import RecordDeleteDialog from '@/components/RecordDeleteDialog.vue'
import StateChip from '@/components/StateChip.vue'
import { useDebouncedValue } from '@/composables/useDebouncedValue'
import {
  MAX_RECORD_BATCH_SIZE,
  type BatchRecordDeleteResponse,
  type BatchRetargetResponse,
  type PluginApi,
  type RecordDeleteMode,
  type RecordDetail,
  type RecordListItem,
  type RecordStatus,
} from '@/types'
import {
  formatBytes,
  formatDate,
  attributionEvidenceLabels,
  displayValue,
  fileAttributionLabels,
  friendlyKey,
  locationLabels,
  mediaLabel,
  mediaTypeLabels,
  packageLabels,
  recordStates,
  shortPath,
  sourceLabels,
  translationLabels,
  unmatchedReasonLabels,
} from '@/types/presentation'

const props = defineProps<{
  api: PluginApi
  pluginId: string
  active: boolean
}>()

const emit = defineEmits<{ action: [] }>()
const { mdAndUp } = useDisplay()
const statusOptions: Array<{ title: string; value: RecordStatus | '' }> = [
  { title: '全部状态', value: '' },
  { title: '已匹配', value: 'matched' },
  { title: '暂存', value: 'staged' },
  { title: '未匹配', value: 'unmatched' },
]
const pageSizeOptions = [25, 50, 100]

const items = ref<RecordListItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref<25 | 50 | 100>(25)
const status = ref<RecordStatus | ''>('')
const searchInput = ref('')
const search = useDebouncedValue(searchInput)
const loading = ref(false)
const refreshing = ref(false)
const loaded = ref(false)
const error = ref('')
const staleError = ref('')
const selectedId = ref('')
const detail = ref<RecordDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const openDetailPanels = ref<number[]>([0, 1])
const deleteOpen = ref(false)
const deleting = ref(false)
const deleteTarget = ref<RecordListItem | null>(null)
const deleteError = ref('')
const batchDeleteOpen = ref(false)
const batchDeleteSessionRecords = ref<RecordListItem[]>([])
const batchRetargetOpen = ref(false)
const retargetSessionRecords = ref<RecordListItem[]>([])
const retargetSessionUsesSelection = ref(false)
const selectedRecords = ref<Map<string, RecordListItem>>(new Map())
const batchOperationNotice = ref('')
const tableFrame = ref<HTMLElement | null>(null)
let listRequest = 0
let detailRequest = 0

const isDesktop = computed(() => mdAndUp.value)
const hasFilters = computed(() => Boolean(search.value || status.value))
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const canLoadMore = computed(() => !isDesktop.value && items.value.length < total.value)
const selectedListItem = computed(() => items.value.find(item => item.id === selectedId.value) || detail.value || deleteTarget.value)
const selectedRecordItems = computed(() => Array.from(selectedRecords.value.values()))
const selectedCount = computed(() => selectedRecords.value.size)
const selectionLimitError = computed(() => selectedCount.value > MAX_RECORD_BATCH_SIZE
  ? `一次最多处理 ${MAX_RECORD_BATCH_SIZE} 条记录。当前已选择 ${selectedCount.value} 条，请取消部分选择后再执行批量操作。`
  : '')
const selectionCountLabel = computed(() => isDesktop.value
  ? `已选择 ${selectedCount.value} 条`
  : `已选 ${selectedCount.value}`)
const currentPageSelected = computed(() => items.value.filter(item => selectedRecords.value.has(item.id)).length)
const allCurrentPageSelected = computed(() => Boolean(items.value.length) && currentPageSelected.value === items.value.length)
const someCurrentPageSelected = computed(() => currentPageSelected.value > 0 && !allCurrentPageSelected.value)

watch(
  () => props.active,
  active => {
    if (active) void loadPage({ silent: loaded.value, requestedPage: isDesktop.value ? page.value : 1 })
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

async function loadPage(options: { silent?: boolean; append?: boolean; requestedPage?: number } = {}): Promise<void> {
  const requestId = ++listRequest
  const requestedPage = options.requestedPage ?? page.value
  if (!options.silent) {
    if (!loaded.value || !items.value.length) loading.value = true
    else refreshing.value = true
    error.value = ''
  }
  try {
    const response = await listRecords(props.api, props.pluginId, {
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
    } else {
      items.value = response.items
      if (!isDesktop.value) page.value = requestedPage
    }
    const refreshedSelection = new Map(selectedRecords.value)
    for (const item of response.items) {
      if (refreshedSelection.has(item.id)) refreshedSelection.set(item.id, item)
    }
    selectedRecords.value = refreshedSelection
    total.value = response.total
    loaded.value = true
    error.value = ''
    staleError.value = ''
    if (options.silent && selectedId.value && !batchRetargetOpen.value && !batchDeleteOpen.value) void refreshOpenDetail()
  } catch (requestError) {
    if (requestId !== listRequest) return
    const message = getErrorMessage(requestError, '匹配记录加载失败')
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

async function openDetail(item: RecordListItem): Promise<void> {
  selectedId.value = item.id
  detail.value = null
  detailError.value = ''
  openDetailPanels.value = [0, 1]
  detailLoading.value = true
  const requestId = ++detailRequest
  try {
    const response = await getRecord(props.api, props.pluginId, item.id)
    if (requestId === detailRequest && selectedId.value === item.id) detail.value = response
  } catch (requestError) {
    if (requestId === detailRequest) detailError.value = getErrorMessage(requestError, '匹配记录详情加载失败')
  } finally {
    if (requestId === detailRequest) detailLoading.value = false
  }
}

async function refreshOpenDetail(): Promise<void> {
  const recordId = selectedId.value
  if (!recordId || detailLoading.value) return
  const requestId = ++detailRequest
  try {
    const response = await getRecord(props.api, props.pluginId, recordId)
    if (requestId === detailRequest && selectedId.value === recordId) detail.value = response
  } catch (requestError) {
    if (requestId === detailRequest) {
      staleError.value = `匹配记录详情刷新失败：${getErrorMessage(requestError, '无法读取最新详情')}`
    }
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

function requestDelete(item: RecordListItem): void {
  deleteTarget.value = item
  deleteError.value = ''
  deleteOpen.value = true
}

async function confirmDelete(mode: RecordDeleteMode): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  deleting.value = true
  deleteError.value = ''
  try {
    await deleteRecord(props.api, props.pluginId, target.id, {
      delete_mode: mode,
      expected_status: target.status,
      expected_location: target.location,
      expected_path: target.path,
      expected_updated_at: target.updated_at,
    })
    deleteOpen.value = false
    deleteTarget.value = null
    const nextSelection = new Map(selectedRecords.value)
    nextSelection.delete(target.id)
    selectedRecords.value = nextSelection
    if (selectedId.value === target.id) closeDetail()
    if (isDesktop.value && items.value.length === 1 && page.value > 1) page.value -= 1
    else await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
    emit('action')
  } catch (requestError) {
    const statusCode = (requestError as { response?: { status?: number } })?.response?.status
    if (statusCode === 409) {
      deleteOpen.value = false
      deleteTarget.value = null
      await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
      staleError.value = '记录在确认后已发生变化，请刷新后重新确认删除。'
    } else {
      deleteError.value = getErrorMessage(requestError, '匹配记录删除失败；原记录和文件未改变。')
    }
  } finally {
    deleting.value = false
  }
}

function clearFilters(): void {
  searchInput.value = ''
  status.value = ''
}

function detailEntries(value: Record<string, unknown> | null | undefined): Array<[string, unknown]> {
  return Object.entries(value || {})
}

function setRecordSelected(item: RecordListItem, selected: boolean): void {
  const next = new Map(selectedRecords.value)
  if (selected) next.set(item.id, item)
  else next.delete(item.id)
  selectedRecords.value = next
}

function setCurrentPageSelected(selected: boolean): void {
  const next = new Map(selectedRecords.value)
  for (const item of items.value) {
    if (selected) next.set(item.id, item)
    else next.delete(item.id)
  }
  selectedRecords.value = next
}

function clearRecordSelection(): void {
  selectedRecords.value = new Map()
}

function removeFromBatch(recordId: string): void {
  const next = new Map(selectedRecords.value)
  next.delete(recordId)
  selectedRecords.value = next
}

function openBatchRetarget(): void {
  if (!selectedCount.value || selectionLimitError.value) return
  retargetSessionRecords.value = selectedRecordItems.value
  retargetSessionUsesSelection.value = true
  batchRetargetOpen.value = true
}

function openBatchDelete(): void {
  if (!selectedCount.value || selectionLimitError.value) return
  batchDeleteSessionRecords.value = selectedRecordItems.value
  batchDeleteOpen.value = true
}

async function handleBatchComplete(result: BatchRetargetResponse): Promise<void> {
  const completedIds = new Set(result.items.filter(item => item.success).map(item => item.record_id))
  if (completedIds.size) {
    if (retargetSessionUsesSelection.value) {
      const next = new Map(selectedRecords.value)
      for (const id of completedIds) next.delete(id)
      selectedRecords.value = next
    }
    await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
    if (selectedId.value && completedIds.has(selectedId.value)) await refreshOpenDetail()
    emit('action')
  }
}

function removeFromRetargetSession(recordId: string): void {
  retargetSessionRecords.value = retargetSessionRecords.value.filter(record => record.id !== recordId)
  if (retargetSessionUsesSelection.value) removeFromBatch(recordId)
}

function requestRetarget(item: RecordListItem): void {
  retargetSessionRecords.value = [item]
  retargetSessionUsesSelection.value = false
  batchRetargetOpen.value = true
}

function requestRetargetFromList(item: RecordListItem): void {
  requestRetarget(item)
}

async function handleBatchDeleteComplete(
  result: BatchRecordDeleteResponse,
  refreshRequiredRecordIds: string[],
): Promise<void> {
  const completedIds = new Set(result.items.filter(item => item.status === 'success').map(item => item.record_id))
  const discardedIds = new Set([...completedIds, ...refreshRequiredRecordIds])
  if (discardedIds.size) {
    const next = new Map(selectedRecords.value)
    for (const id of discardedIds) next.delete(id)
    selectedRecords.value = next
  }
  if (selectedId.value && completedIds.has(selectedId.value)) closeDetail()
  await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
  if (selectedId.value && refreshRequiredRecordIds.includes(selectedId.value)) await refreshOpenDetail()
  if (refreshRequiredRecordIds.length) {
    batchOperationNotice.value = '部分记录的状态已变化或执行被中止。列表已刷新，请重新选择这些记录后再次确认删除。'
  }
  emit('action')
}

async function handleBatchDeleteRefreshRequired(message: string): Promise<void> {
  clearRecordSelection()
  batchDeleteSessionRecords.value = []
  await loadPage({ silent: true, requestedPage: isDesktop.value ? page.value : 1 })
  if (selectedId.value) await refreshOpenDetail()
  batchOperationNotice.value = `批量删除未开始：${message}。列表已刷新，请重新选择并确认。`
}
</script>

<template>
  <section class="view-shell" aria-labelledby="records-view-title">
    <div class="view-controls">
      <header class="view-header">
        <div>
          <h2 id="records-view-title">匹配记录</h2>
          <p>查看已落盘字幕、可复用暂存字幕与未匹配文件。</p>
        </div>
        <div class="view-header-actions">
          <div v-if="selectedCount" class="selection-actions selection-actions--header" role="status" aria-live="polite">
            <span class="selection-count">{{ selectionCountLabel }}</span>
            <VBtn variant="text" size="small" prepend-icon="mdi-close" @click="clearRecordSelection">清空</VBtn>
            <VBtn color="primary" size="small" prepend-icon="mdi-swap-horizontal-bold" :disabled="Boolean(selectionLimitError)" @click="openBatchRetarget">批量改配</VBtn>
            <VBtn color="error" variant="tonal" size="small" prepend-icon="mdi-delete-outline" :disabled="Boolean(selectionLimitError)" @click="openBatchDelete">批量删除</VBtn>
          </div>
          <VTooltip v-else text="刷新匹配记录">
            <template #activator="{ props: tooltipProps }">
              <VBtn v-bind="tooltipProps" icon="mdi-refresh" size="small" variant="text" :loading="refreshing" aria-label="刷新匹配记录" @click="loadPage({ silent: loaded, requestedPage: isDesktop ? page : 1 })" />
            </template>
          </VTooltip>
        </div>
      </header>

      <div class="filter-bar">
        <VTextField v-model="searchInput" label="搜索匹配记录" placeholder="字幕、媒体、来源或路径" prepend-inner-icon="mdi-magnify" clearable hide-details :density="isDesktop ? 'compact' : 'comfortable'" />
        <VSelect v-model="status" label="状态" :items="statusOptions" hide-details :density="isDesktop ? 'compact' : 'comfortable'" />
      </div>
    </div>

    <VAlert v-if="selectionLimitError" type="error" variant="tonal" density="compact" class="mb-3">{{ selectionLimitError }}</VAlert>
    <VAlert v-if="batchOperationNotice" type="warning" variant="tonal" density="compact" class="mb-3">
      <div class="inline-alert">
        <span>{{ batchOperationNotice }}</span>
        <VBtn size="small" variant="text" prepend-icon="mdi-refresh" @click="loadPage({ silent: true, requestedPage: isDesktop ? page : 1 })">刷新</VBtn>
      </div>
    </VAlert>

    <VAlert v-if="staleError" type="warning" variant="tonal" density="compact" class="mb-3">
      <div class="inline-alert">
        <span>刷新失败，当前数据可能已过期：{{ staleError }}</span>
        <VBtn size="small" variant="text" prepend-icon="mdi-refresh" @click="loadPage({ silent: true })">重试</VBtn>
      </div>
    </VAlert>

    <div v-if="loading" class="table-frame" aria-label="正在加载匹配记录">
      <VSkeletonLoader :type="isDesktop ? 'table-heading, table-row-divider@7' : 'list-item-three-line@6'" />
    </div>
    <VAlert v-else-if="error" type="error" variant="tonal" class="mt-3" title="匹配记录加载失败">
      <div>{{ error }}</div>
      <VBtn class="mt-2" size="small" variant="text" prepend-icon="mdi-refresh" @click="loadPage()">重试</VBtn>
    </VAlert>
    <EmptyState v-else-if="!items.length && !hasFilters && !selectedId" icon="mdi-archive-outline" title="还没有匹配记录" message="字幕任务产出文件后，已匹配、暂存和未匹配结果会显示在这里。" />
    <EmptyState v-else-if="!items.length && !selectedId" icon="mdi-filter-off-outline" title="没有符合条件的记录" message="调整搜索内容或状态筛选后再试。">
      <template #actions><VBtn variant="tonal" prepend-icon="mdi-filter-remove-outline" @click="clearFilters">清除条件</VBtn></template>
    </EmptyState>

    <div v-else class="master-detail">
      <div class="master-pane">
        <div v-if="isDesktop" ref="tableFrame" class="table-frame">
          <VTable hover fixed-header height="100%" class="data-table">
            <thead><tr><th class="selection-column"><VCheckboxBtn :model-value="allCurrentPageSelected" :indeterminate="someCurrentPageSelected" aria-label="选择当前页匹配记录" @update:model-value="setCurrentPageSelected(Boolean($event))" /></th><th>字幕文件</th><th>关联媒体</th><th>状态</th><th>来源</th><th>位置</th><th>时间</th><th class="actions-column">操作</th></tr></thead>
            <tbody>
              <tr
                v-for="item in items"
                :key="item.id"
                class="selectable-row"
                :class="{ 'selectable-row--active': selectedId === item.id }"
                tabindex="0"
                role="button"
                :data-subtitleassistant-detail-trigger="`record:${item.id}`"
                :aria-label="`查看匹配记录 ${item.subtitle_file_name}`"
                @click="openDetail(item)"
                @keydown.enter.prevent="openDetail(item)"
                @keydown.space.prevent="openDetail(item)"
              >
                <td class="selection-column" @click.stop @keydown.stop><VCheckboxBtn :model-value="selectedRecords.has(item.id)" :aria-label="`选择匹配记录 ${item.subtitle_file_name}`" @update:model-value="setRecordSelected(item, Boolean($event))" /></td>
                <td><strong class="file-name" :title="item.subtitle_file_name">{{ item.subtitle_file_name }}</strong><span class="cell-note">{{ item.format }}</span></td>
                <td><span class="media-text">{{ mediaLabel(item.media_title, item.year, item.season, item.episode) }}</span></td>
                <td><StateChip :state="recordStates[item.status]" /></td>
                <td><span>{{ sourceLabels[item.source] }}</span><span class="cell-note">{{ packageLabels[item.package_scope] }}</span></td>
                <td><span>{{ locationLabels[item.location] }}</span><span class="cell-note" :title="item.path">{{ shortPath(item.path) }}</span></td>
                <td><span class="time-text">{{ formatDate(item.created_at) }}</span><span v-if="item.consumed_at" class="cell-note">消费：{{ formatDate(item.consumed_at) }}</span></td>
                <td class="actions-column" @click.stop @keydown.stop>
                  <VTooltip text="查看详情"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-chevron-right" size="small" variant="text" aria-label="查看匹配记录详情" @click="openDetail(item)" /></template></VTooltip>
                  <VTooltip text="改配目标"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-swap-horizontal" size="small" variant="text" aria-label="改配目标" @click="requestRetargetFromList(item)" /></template></VTooltip>
                  <VTooltip text="删除匹配记录"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-delete-outline" size="small" variant="text" color="error" aria-label="删除匹配记录" @click="requestDelete(item)" /></template></VTooltip>
                </td>
              </tr>
            </tbody>
          </VTable>
        </div>

        <VList v-else class="mobile-list" lines="three" role="list" aria-label="匹配记录列表">
          <VListItem v-for="item in items" :key="item.id" class="mobile-list__item" role="listitem">
            <template #prepend><VCheckboxBtn :model-value="selectedRecords.has(item.id)" :aria-label="`选择匹配记录 ${item.subtitle_file_name}`" @click.stop @keydown.stop @update:model-value="setRecordSelected(item, Boolean($event))" /></template>
            <button
              type="button"
              class="mobile-list__detail"
              :data-subtitleassistant-detail-trigger="`record:${item.id}`"
              :aria-label="`查看匹配记录 ${item.subtitle_file_name}`"
              @click="openDetail(item)"
            >
              <VListItemTitle>{{ item.subtitle_file_name }}</VListItemTitle>
              <VListItemSubtitle>{{ mediaLabel(item.media_title, item.year, item.season, item.episode) }}</VListItemSubtitle>
              <VListItemSubtitle class="mobile-meta"><StateChip :state="recordStates[item.status]" size="x-small" /><span>{{ sourceLabels[item.source] }} · {{ formatDate(item.created_at) }}</span></VListItemSubtitle>
            </button>
            <template #append>
              <VMenu>
                <template #activator="{ props: menuProps }">
                  <VBtn v-bind="menuProps" icon="mdi-dots-vertical" size="small" variant="text" aria-label="匹配记录操作" @click.stop />
                </template>
                <VList density="compact" role="menu" aria-label="匹配记录操作">
                  <VListItem role="menuitem" title="查看详情" prepend-icon="mdi-text-box-search-outline" @click="openDetail(item)" />
                  <VListItem role="menuitem" title="改配目标" prepend-icon="mdi-swap-horizontal" @click="requestRetargetFromList(item)" />
                  <VListItem role="menuitem" title="删除匹配记录" prepend-icon="mdi-delete-outline" base-color="error" @click="requestDelete(item)" />
                </VList>
              </VMenu>
            </template>
          </VListItem>
        </VList>

        <div v-if="isDesktop" class="pagination-bar">
          <span>共 {{ total }} 条</span>
          <VPagination v-model="page" :length="totalPages" :total-visible="5" density="comfortable" class="table-pagination" />
          <VSelect v-model="pageSize" :items="pageSizeOptions" label="每页" density="compact" class="page-size" />
        </div>
        <div v-else-if="canLoadMore" class="load-more"><VBtn variant="tonal" prepend-icon="mdi-chevron-down" :loading="refreshing" @click="loadMore">加载更多</VBtn></div>
      </div>

      <DetailDrawer
        :model-value="Boolean(selectedId)"
        title="匹配记录详情"
        :subtitle="detail?.subtitle_file_name || selectedListItem?.subtitle_file_name"
        close-label="关闭匹配记录详情"
        :return-focus-key="selectedId ? `record:${selectedId}` : null"
        @update:model-value="updateDetailOpen"
      >
        <template #actions>
          <VTooltip v-if="detail" text="改配目标"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-swap-horizontal" variant="text" aria-label="改配目标" @click="requestRetarget(detail)" /></template></VTooltip>
          <VTooltip v-if="selectedListItem" text="删除匹配记录"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-delete-outline" color="error" variant="text" aria-label="删除匹配记录" @click="requestDelete(selectedListItem)" /></template></VTooltip>
        </template>

        <VSkeletonLoader v-if="detailLoading" class="detail-state" type="heading, paragraph, list-item-three-line@4" />
        <VAlert v-else-if="detailError" class="detail-state" type="error" variant="tonal"><div>{{ detailError }}</div><VBtn v-if="selectedListItem" class="mt-2" size="small" variant="text" prepend-icon="mdi-refresh" @click="openDetail(selectedListItem)">重试</VBtn></VAlert>
        <VExpansionPanels v-else-if="detail" v-model="openDetailPanels" multiple variant="accordion" class="detail-sections">
          <VExpansionPanel>
            <VExpansionPanelTitle>文件</VExpansionPanelTitle>
            <VExpansionPanelText><dl>
              <DetailRow label="记录 ID"><CopyValue :value="detail.id" label="记录 ID" /></DetailRow>
              <DetailRow label="字幕文件">{{ detail.subtitle_file_name }}</DetailRow>
              <DetailRow label="格式">{{ detail.format }}</DetailRow>
              <DetailRow label="大小">{{ formatBytes(detail.size) }}</DetailRow>
              <DetailRow label="位置">{{ locationLabels[detail.location] }}</DetailRow>
              <DetailRow label="完整路径"><CopyValue :value="detail.current_file_path" label="字幕路径" /></DetailRow>
              <DetailRow label="逻辑来源路径"><CopyValue :value="detail.logical_source_path" label="逻辑来源路径" /></DetailRow>
              <DetailRow v-if="detail.final_subtitle_path" label="最终字幕"><CopyValue :value="detail.final_subtitle_path" label="最终字幕路径" /></DetailRow>
            </dl></VExpansionPanelText>
          </VExpansionPanel>
          <VExpansionPanel>
            <VExpansionPanelTitle>关联媒体</VExpansionPanelTitle>
            <VExpansionPanelText><dl>
              <DetailRow label="媒体">{{ mediaLabel(detail.media_title, detail.year, detail.season, detail.episode) }}</DetailRow>
              <DetailRow label="类型">{{ mediaTypeLabels[detail.media_type] }}</DetailRow>
              <DetailRow label="规范身份">{{ detail.canonical_identity_type && detail.canonical_identity_value ? `${detail.canonical_identity_type.toUpperCase()} ${detail.canonical_identity_value}` : '未识别' }}</DetailRow>
              <DetailRow label="TMDB ID">{{ detail.tmdb_id ?? '未记录' }}</DetailRow>
              <DetailRow label="IMDb ID">{{ detail.imdb_id || '未记录' }}</DetailRow>
              <DetailRow label="整理历史 ID"><CopyValue :value="detail.target_history_id == null ? null : String(detail.target_history_id)" label="整理历史 ID" /></DetailRow>
              <DetailRow label="历史目标路径"><CopyValue :value="detail.history_target_path" label="历史目标路径" /></DetailRow>
              <DetailRow label="实际字幕目标"><CopyValue :value="detail.target_path" label="实际字幕目标路径" /></DetailRow>
              <DetailRow label="命中路径映射">
                {{ detail.matched_path_mapping
                  ? `${detail.matched_path_mapping.source_prefix} → ${detail.matched_path_mapping.target_prefix}`
                  : '未命中' }}
              </DetailRow>
              <DetailRow label="目标视频存在">{{ detail.target_file_exists == null ? '未记录' : (detail.target_file_exists ? '是' : '否') }}</DetailRow>
            </dl></VExpansionPanelText>
          </VExpansionPanel>
          <VExpansionPanel>
            <VExpansionPanelTitle>来源</VExpansionPanelTitle>
            <VExpansionPanelText><dl>
              <DetailRow label="字幕源">{{ sourceLabels[detail.source] }}</DetailRow>
              <DetailRow label="候选">{{ detail.candidate_name || detail.candidate_key }}</DetailRow>
              <DetailRow label="候选键"><CopyValue :value="detail.candidate_key" label="候选键" /></DetailRow>
              <DetailRow label="包范围">{{ packageLabels[detail.package_scope] }}</DetailRow>
              <DetailRow label="语言标记">{{ detail.language }}</DetailRow>
              <DetailRow label="翻译类型">{{ translationLabels[detail.translation_type] }}</DetailRow>
              <DetailRow label="听障字幕">{{ detail.hearing_impaired ? '是（SDH/CC）' : '否' }}</DetailRow>
              <DetailRow label="文件归属方式">{{ detail.file_attribution_method ? fileAttributionLabels[detail.file_attribution_method] : '未记录' }}</DetailRow>
              <DetailRow label="季号证据">{{ detail.season_evidence ? attributionEvidenceLabels[detail.season_evidence] : '未记录' }}</DetailRow>
              <DetailRow label="集号证据">{{ detail.episode_evidence ? attributionEvidenceLabels[detail.episode_evidence] : '未记录' }}</DetailRow>
              <DetailRow v-if="detail.unmatched_reason" label="未匹配原因">{{ unmatchedReasonLabels[detail.unmatched_reason] }}</DetailRow>
              <DetailRow v-if="detail.candidate_attribution_snapshot" label="候选归属快照">{{ displayValue(detail.candidate_attribution_snapshot) }}</DetailRow>
              <DetailRow v-if="detail.host_recognition_summary && Object.keys(detail.host_recognition_summary).length" label="媒体识别摘要">{{ displayValue(detail.host_recognition_summary) }}</DetailRow>
            </dl></VExpansionPanelText>
          </VExpansionPanel>
          <VExpansionPanel v-if="detail.ai_takeover_audit || detail.ai_attribution_audit">
            <VExpansionPanelTitle>AI 智能接管审计</VExpansionPanelTitle>
            <VExpansionPanelText>
              <dl class="ai-audit-list">
                <DetailRow
                  v-for="[key, value] in detailEntries(detail.ai_takeover_audit || detail.ai_attribution_audit || {})"
                  :key="key"
                  :label="friendlyKey(key)"
                >{{ displayValue(value) }}</DetailRow>
              </dl>
              <p class="cell-note">仅展示规范化审计字段，不包含原始提示、响应、密钥或 Token 用量。</p>
            </VExpansionPanelText>
          </VExpansionPanel>
          <VExpansionPanel>
            <VExpansionPanelTitle>生命周期</VExpansionPanelTitle>
            <VExpansionPanelText><dl>
              <DetailRow label="状态"><StateChip :state="recordStates[detail.status]" /></DetailRow>
              <DetailRow label="来源任务"><CopyValue :value="detail.source_task_id" label="来源任务 ID" /></DetailRow>
              <DetailRow label="创建时间">{{ formatDate(detail.created_at) }}</DetailRow>
              <DetailRow label="首次暂存">{{ formatDate(detail.staged_at) }}</DetailRow>
              <DetailRow label="最后更新">{{ formatDate(detail.updated_at) }}</DetailRow>
              <DetailRow label="消费任务"><CopyValue :value="detail.consumed_task_id" label="消费任务 ID" /></DetailRow>
              <DetailRow label="消费时间">{{ formatDate(detail.consumed_at) }}</DetailRow>
            </dl></VExpansionPanelText>
          </VExpansionPanel>
          <VExpansionPanel>
            <VExpansionPanelTitle>改配历史</VExpansionPanelTitle>
            <VExpansionPanelText>
              <VTimeline v-if="detail.retarget_history?.length" density="compact" side="end">
                <VTimelineItem v-for="entry in detail.retarget_history" :key="`${entry.operated_at}-${entry.new_subtitle_path}`" dot-color="primary" size="x-small">
                  <strong>{{ formatDate(entry.operated_at) }}</strong>
                  <dl class="retarget-audit">
                    <DetailRow label="整理历史 ID">{{ entry.old_target_history_id ?? '未记录' }} → {{ entry.new_target_history_id ?? '未记录' }}</DetailRow>
                    <DetailRow label="历史路径">{{ entry.old_history_target_path || '未记录' }} → {{ entry.new_history_target_path || '未记录' }}</DetailRow>
                    <DetailRow label="实际目标">{{ entry.old_target_path || '未记录' }} → {{ entry.new_target_path || '未记录' }}</DetailRow>
                    <DetailRow label="字幕路径">{{ entry.old_subtitle_path }} → {{ entry.new_subtitle_path }}</DetailRow>
                  </dl>
                </VTimelineItem>
              </VTimeline>
              <p v-else class="cell-note">尚无成功改配记录</p>
            </VExpansionPanelText>
          </VExpansionPanel>
        </VExpansionPanels>
      </DetailDrawer>
    </div>

    <RecordDeleteDialog
      v-model="deleteOpen"
      :record="deleteTarget"
      :loading="deleting"
      :error="deleteError"
      @confirm="confirmDelete"
    />

    <BatchRecordDeleteDialog
      v-model="batchDeleteOpen"
      :api="props.api"
      :plugin-id="props.pluginId"
      :records="batchDeleteSessionRecords"
      @complete="handleBatchDeleteComplete"
      @refresh-required="handleBatchDeleteRefreshRequired"
    />

    <BatchRetargetDialog
      v-model="batchRetargetOpen"
      :api="props.api"
      :plugin-id="props.pluginId"
      :records="retargetSessionRecords"
      @complete="handleBatchComplete"
      @remove="removeFromRetargetSession"
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
.view-header { display: flex; min-block-size: 3rem; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-block-end: 0.75rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 1rem; font-weight: 650; letter-spacing: 0; }
.view-header p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.view-header-actions, .selection-actions { display: flex; align-items: center; gap: 0.25rem; }
.selection-actions--header { flex-wrap: nowrap; justify-content: flex-end; }
.selection-count { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; white-space: nowrap; }
.filter-bar { display: grid; grid-template-columns: minmax(15rem, 1fr) minmax(10rem, 14rem); gap: 0.75rem; }
.inline-alert { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.master-detail, .master-pane { min-width: 0; }
.table-frame { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.72); }
.table-frame :deep(.v-table__wrapper) { overscroll-behavior: contain; scrollbar-gutter: stable; }
.selectable-row { cursor: pointer; transition: background-color 180ms ease; }
.selectable-row:hover, .selectable-row--active { background: rgba(var(--v-theme-primary), 0.08); }
.selectable-row:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; scroll-margin-block-start: var(--v-table-header-height, 3rem); }
.file-name { display: block; max-width: 15rem; overflow: hidden; color: rgb(var(--v-theme-on-surface)); font-size: 0.8125rem; text-overflow: ellipsis; white-space: nowrap; }
.media-text { display: block; max-width: 16rem; font-size: 0.8125rem; }
.cell-note, .time-text { display: block; margin-top: 0.125rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.45; }
.actions-column { width: 6.5rem; text-align: end !important; white-space: nowrap; }
.selection-column { width: 3.25rem; padding-inline: 0.25rem !important; }
.pagination-bar { display: grid; grid-template-columns: auto 1fr 7rem; align-items: center; gap: 1rem; padding: 0.75rem 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.table-pagination { justify-self: center; }
.page-size { min-width: 7rem; }
.detail-state { margin: 1rem; }
.detail-sections { border-radius: 0; }
.retarget-audit { margin-top: 0.5rem; }
.ai-audit-list { margin: 0; }
.mobile-list { padding: 0; border-block: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: transparent; }
.mobile-list__item { min-height: 6rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.mobile-list__detail { display: grid; width: 100%; min-width: 0; padding: 0; border: 0; color: inherit; text-align: start; background: transparent; cursor: pointer; font: inherit; }
.mobile-list__detail:focus-visible { border-radius: 0.125rem; outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
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
@media (max-width: 959px) { .filter-bar { grid-template-columns: 1fr; } }
@media (max-width: 37.5rem) {
  .view-controls { margin-block-end: 0.75rem; padding-block-end: 0.5rem; }
  .view-header { gap: 0.5rem; margin-block-end: 0.5rem; }
  .view-header p { display: none; }
  .view-header-actions { min-width: 0; }
  .selection-actions--header { flex-wrap: wrap; row-gap: 0.125rem; }
  .filter-bar { grid-template-columns: minmax(0, 1fr) minmax(7.5rem, 9rem); gap: 0.5rem; }
  .selection-count { max-width: 7rem; overflow: hidden; text-overflow: ellipsis; }
}
@media (max-width: 26rem) { .filter-bar { grid-template-columns: minmax(0, 1fr); } }
@media (prefers-reduced-motion: reduce) { .selectable-row { transition: none; } }
</style>
