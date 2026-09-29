<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { getErrorMessage, listTargets } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import { useDebouncedValue } from '@/composables/useDebouncedValue'
import type { HistoryRow, PluginApi, TargetItem } from '@/types'
import {
  commonDirectory,
  formatDate,
  historyDate,
  historyFileName,
  historyId,
  historyLabel,
  historyMediaType,
  historyPath,
  parentDirectory,
  relativeToDirectory,
  sameHistoryId,
} from '@/types/presentation'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId: string
  modelValue?: HistoryRow | TargetItem | null
  label?: string
  hint?: string
  disabled?: boolean
  compact?: boolean
  /** 由外层容器给定高度：搜索与分页固定，仅候选列表滚动。 */
  fillHeight?: boolean
  showHeading?: boolean
  searchable?: boolean
  searchPlaceholder?: string
}>(), {
  modelValue: null,
  label: 'MoviePilot 整理历史',
  hint: '显示 MoviePilot 当前页整理历史；选中后才验证是否可用于字幕操作。',
  disabled: false,
  compact: false,
  fillHeight: false,
  showHeading: true,
  searchable: false,
  searchPlaceholder: '搜索标题或路径文本',
})

const emit = defineEmits<{ 'update:modelValue': [value: HistoryRow | null] }>()
const items = ref<HistoryRow[]>([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = 25
const canNext = ref(false)
const total = ref(0)
const searchInput = ref('')
const search = useDebouncedValue(searchInput)
let requestId = 0

const normalizedSearch = computed(() => search.value.trim())
/** 同一媒体库的候选往往只差中间一两段；公共目录提到列表上方写一次，行内只留差异。 */
const commonRoot = computed(() => commonDirectory(items.value.map(item => historyPath(item))))
const resultSummary = computed(() => normalizedSearch.value
  ? `第 ${page.value} 页 · 显示 ${items.value.length} / ${total.value} 条`
  : `第 ${page.value} 页 · ${items.value.length} 条`)

watch(page, () => void load())
watch(search, () => {
  if (page.value !== 1) page.value = 1
  else void load()
})
watch(() => historyId(props.modelValue), selectedHistoryId => {
  if (selectedHistoryId == null) return
  if (!items.value.some(item => sameHistoryId(item, props.modelValue))) void load()
})
watch(() => props.api, () => void load(), { immediate: true })

async function load(): Promise<void> {
  const current = ++requestId
  loading.value = true
  error.value = ''
  try {
    const response = await listTargets(props.api, props.pluginId, {
      page: page.value,
      pageSize,
      search: search.value,
    })
    if (current !== requestId) return
    items.value = Array.isArray(response?.items) ? response.items : []
    total.value = Number.isFinite(response?.total) ? response.total : items.value.length
    canNext.value = page.value * pageSize < total.value
  } catch (requestError) {
    if (current === requestId) error.value = getErrorMessage(requestError, '目标视频加载失败')
  } finally {
    if (current === requestId) loading.value = false
  }
}

function updateSearch(value: string | null): void {
  searchInput.value = value || ''
}

function choose(item: HistoryRow): void {
  if (props.disabled || historyId(item) == null) return
  emit('update:modelValue', item)
}

/** 目标文件名已单独一行，这里只给目录，避免把判断依据重复一遍又两头截断。 */
function rowDirectory(item: HistoryRow): string {
  return relativeToDirectory(parentDirectory(historyPath(item)), commonRoot.value)
}

function itemKey(item: HistoryRow, index: number): string {
  const id = historyId(item)
  return id == null ? `invalid-${index}` : `${String(id)}-${index}`
}

function goPrevious(): void {
  if (page.value > 1) page.value -= 1
}

function goNext(): void {
  if (canNext.value) page.value += 1
}

</script>

<template>
  <section
    class="target-selector"
    :class="{ 'target-selector--compact': compact, 'target-selector--fill': fillHeight }"
    aria-label="MoviePilot 整理历史选择"
    :aria-busy="loading"
  >
    <template v-if="showHeading">
      <h3 class="target-label">{{ label }}</h3>
      <p class="target-hint">{{ hint }}</p>
    </template>
    <VTextField
      v-if="searchable"
      :model-value="searchInput"
      class="target-search"
      :label="label ? `搜索 ${label}` : '搜索整理历史'"
      :placeholder="searchPlaceholder"
      prepend-inner-icon="mdi-magnify"
      clearable
      hide-details
      density="compact"
      @update:model-value="updateSearch"
      @click:clear="updateSearch('')"
    />
    <VAlert v-if="error" type="error" variant="tonal" density="compact" class="target-error">
      {{ error }}
      <VBtn size="small" variant="text" prepend-icon="mdi-refresh" :disabled="disabled" @click="load">重试</VBtn>
    </VAlert>
    <p v-if="commonRoot" class="target-root">
      共同目录 <code :title="commonRoot">{{ commonRoot }}</code>
      <span>候选只显示相对该目录的差异</span>
    </p>
    <!-- 加载、空态和候选列表共用这一个盒子：弹层高度在数据到达前就已确定，不随内容跳动。 -->
    <div class="target-scroll" :class="{ 'target-scroll--loading': loading }">
      <div v-if="loading" class="target-loading" role="status" aria-live="polite">
        <VProgressCircular indeterminate size="16" width="2" aria-hidden="true" />
        <span>正在加载整理历史目标…</span>
      </div>
      <VSkeletonLoader v-if="loading" type="list-item-three-line@4" aria-hidden="true" />
      <EmptyState v-else-if="!items.length && !error && !normalizedSearch" icon="mdi-filmstrip-off" title="没有整理历史" message="MoviePilot 当前页没有返回整理历史。">
        <template #actions>
          <VBtn variant="tonal" size="small" prepend-icon="mdi-refresh" :disabled="disabled" @click="load">刷新目标</VBtn>
        </template>
      </EmptyState>
      <EmptyState v-else-if="normalizedSearch && !items.length && !error" icon="mdi-filter-off-outline" title="没有符合条件的整理历史" message="调整搜索内容后再试。">
        <template #actions>
          <VBtn variant="tonal" size="small" prepend-icon="mdi-filter-remove-outline" @click="updateSearch('')">清除搜索</VBtn>
        </template>
      </EmptyState>
      <VList
        v-else-if="items.length"
        class="target-list"
        lines="three"
        select-strategy="single-independent"
        role="listbox"
        :aria-label="`${label}候选`"
      >
        <VListItem
          v-for="(item, index) in items"
          :key="itemKey(item, index)"
          :active="sameHistoryId(item, props.modelValue)"
          :disabled="disabled || historyId(item) == null"
          color="primary"
          role="option"
          :aria-selected="sameHistoryId(item, props.modelValue)"
          tabindex="0"
          @click="choose(item)"
          @keydown.enter.prevent="choose(item)"
          @keydown.space.prevent="choose(item)"
        >
          <template #prepend>
            <VIcon :icon="historyMediaType(item) === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" />
          </template>
          <VListItemTitle>{{ historyLabel(item) }}</VListItemTitle>
          <VListItemSubtitle>{{ historyFileName(item) }}</VListItemSubtitle>
          <VListItemSubtitle class="target-meta" :title="historyPath(item) || '未记录路径'">
            <template v-if="rowDirectory(item)">{{ rowDirectory(item) }} · </template>整理于 {{ formatDate(historyDate(item)) }}
          </VListItemSubtitle>
          <template #append><VIcon v-if="sameHistoryId(item, props.modelValue)" icon="mdi-check-circle" color="primary" /></template>
        </VListItem>
      </VList>
    </div>
    <nav class="target-pagination" aria-label="整理历史分页">
      <span aria-live="polite">{{ resultSummary }}</span>
      <div class="target-pagination__actions">
        <VBtn icon="mdi-chevron-left" size="small" variant="text" aria-label="上一页" :disabled="disabled || loading || page <= 1" @click="goPrevious" />
        <VBtn icon="mdi-chevron-right" size="small" variant="text" aria-label="下一页" :disabled="disabled || loading || !canNext" @click="goNext" />
      </div>
    </nav>
  </section>
</template>

<style scoped>
.target-selector { min-width: 0; }
.target-label { margin: 0 0 0.35rem; font-size: 1rem; font-weight: 600; }
.target-hint { margin: -0.25rem 0 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-search { margin-bottom: 0.75rem; }
.target-error { margin-bottom: 0.75rem; }
.target-loading { display: flex; align-items: center; gap: 0.4rem; padding: 0.5rem 0.6rem 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-root { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.35rem; margin: 0 0 0.5rem; font-size: 0.75rem; }
.target-root code { max-width: 100%; overflow: hidden; padding: 0.1rem 0.3rem; border-radius: 0.25rem; background: rgba(var(--v-theme-on-surface), 0.06); text-overflow: ellipsis; white-space: nowrap; }
.target-root span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
/* 骨架、空态和候选列表共用这个盒子，所以边框和滚动都归它，状态切换时边界不动。 */
.target-scroll { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
/* 骨架高于盒子时不要冒出一条随即消失的滚动条。 */
.target-scroll--loading { overflow: hidden; }
.target-selector--compact .target-hint { margin-block: -0.25rem 0.5rem; }
.target-selector--compact .target-error { margin-bottom: 0.5rem; }
.target-list { background: transparent; }
.target-list :deep(.v-list-item) { min-height: 5.25rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.target-list :deep(.v-list-item:last-child) { border-bottom: 0; }
.target-list :deep(.v-list-item:focus-visible) { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.target-meta { margin-top: 0.2rem; overflow-wrap: anywhere; }
.target-pagination { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding-top: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-pagination__actions { display: flex; gap: 0.125rem; }

/* 填充模式：本组件既是外层弹窗的伸缩项，也是内部的伸缩容器。
   搜索、错误、加载与分页固定，候选列表是唯一滚动面。 */
.target-selector--fill { display: flex; min-block-size: 0; flex: 1 1 auto; flex-direction: column; }
.target-selector--fill .target-search,
.target-selector--fill .target-error,
.target-selector--fill .target-root,
.target-selector--fill .target-pagination { flex: 0 0 auto; }
.target-selector--fill .target-scroll {
  min-block-size: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: rgba(var(--v-theme-on-surface), 0.25) transparent;
}
.target-selector--fill .target-scroll--loading { overflow: hidden; }
@media (max-width: 37.5rem) {
  .target-pagination { align-items: flex-start; flex-direction: column; }
}
</style>
