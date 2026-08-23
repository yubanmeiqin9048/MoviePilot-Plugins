<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { getErrorMessage, listTargets } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import type { HistoryRow, PluginApi, TargetItem } from '@/types'
import {
  formatDate,
  historyDate,
  historyFileName,
  historyId,
  historyLabel,
  historyMediaType,
  historyPath,
  sameHistoryId,
  shortPath,
} from '@/types/presentation'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId: string
  modelValue?: HistoryRow | TargetItem | null
  label?: string
  hint?: string
  disabled?: boolean
  compact?: boolean
  showHeading?: boolean
  searchable?: boolean
  searchPlaceholder?: string
}>(), {
  modelValue: null,
  label: 'MoviePilot 整理历史',
  hint: '显示 MoviePilot 当前页整理历史；选中后才验证是否可用于字幕操作。',
  disabled: false,
  compact: false,
  showHeading: true,
  searchable: false,
  searchPlaceholder: '搜索标题、文件名或路径',
})

const emit = defineEmits<{ 'update:modelValue': [value: HistoryRow | null] }>()
const items = ref<HistoryRow[]>([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = 25
const canNext = ref(false)
const searchInput = ref('')
let requestId = 0

const normalizedSearch = computed(() => searchInput.value.trim().toLocaleLowerCase())
const visibleItems = computed(() => {
  const query = normalizedSearch.value
  if (!query) return items.value
  return items.value.filter(item => [historyLabel(item), historyFileName(item), historyPath(item)]
    .some(value => value.toLocaleLowerCase().includes(query)))
})
const resultSummary = computed(() => normalizedSearch.value
  ? `第 ${page.value} 页 · 显示 ${visibleItems.value.length} / ${items.value.length} 条`
  : `第 ${page.value} 页 · ${items.value.length} 条`)

watch(page, () => void load())
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
    const response = await listTargets(props.api, props.pluginId, { page: page.value, pageSize })
    if (current !== requestId) return
    items.value = Array.isArray(response?.items) ? response.items : []
    canNext.value = items.value.length >= pageSize
  } catch (requestError) {
    if (current === requestId) error.value = getErrorMessage(requestError, '目标视频加载失败')
  } finally {
    if (current === requestId) loading.value = false
  }
}

function choose(item: HistoryRow): void {
  if (props.disabled || historyId(item) == null) return
  emit('update:modelValue', item)
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
    :class="{ 'target-selector--compact': compact }"
    aria-label="MoviePilot 整理历史选择"
    :aria-busy="loading"
  >
    <template v-if="showHeading">
      <h3 class="target-label">{{ label }}</h3>
      <p class="target-hint">{{ hint }}</p>
    </template>
    <VTextField
      v-if="searchable"
      v-model="searchInput"
      class="target-search"
      :label="label ? `搜索 ${label}` : '搜索整理历史'"
      :placeholder="searchPlaceholder"
      prepend-inner-icon="mdi-magnify"
      clearable
      hide-details
      density="compact"
    />
    <VAlert v-if="error" type="error" variant="tonal" density="compact" class="target-error">
      {{ error }}
      <VBtn size="small" variant="text" prepend-icon="mdi-refresh" :disabled="disabled" @click="load">重试</VBtn>
    </VAlert>
    <div v-if="loading" class="target-loading" role="status" aria-live="polite">
      <VProgressCircular indeterminate size="16" width="2" aria-hidden="true" />
      <span>正在加载整理历史目标…</span>
    </div>
    <VSkeletonLoader v-if="loading" type="list-item-three-line@4" aria-hidden="true" />
    <EmptyState v-else-if="!items.length && !error" icon="mdi-filmstrip-off" title="没有整理历史" message="MoviePilot 当前页没有返回整理历史。">
      <template #actions>
        <VBtn variant="tonal" size="small" prepend-icon="mdi-refresh" :disabled="disabled" @click="load">刷新目标</VBtn>
      </template>
    </EmptyState>
    <EmptyState v-else-if="items.length > 0 && !visibleItems.length" icon="mdi-filter-off-outline" title="没有符合条件的整理历史" message="调整搜索内容后再试。">
      <template #actions>
        <VBtn variant="tonal" size="small" prepend-icon="mdi-filter-remove-outline" @click="searchInput = ''">清除搜索</VBtn>
      </template>
    </EmptyState>
    <VList
      v-else-if="visibleItems.length"
      class="target-list"
      lines="three"
      select-strategy="single-independent"
      role="listbox"
      :aria-label="`${label}候选`"
    >
      <VListItem
        v-for="(item, index) in visibleItems"
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
        <VListItemSubtitle class="target-meta target-meta--full">{{ historyPath(item) || '未记录路径' }} · 整理于 {{ formatDate(historyDate(item)) }}</VListItemSubtitle>
        <VListItemSubtitle class="target-meta target-meta--short">{{ shortPath(historyPath(item)) }} · 整理于 {{ formatDate(historyDate(item)) }}</VListItemSubtitle>
        <template #append><VIcon v-if="sameHistoryId(item, props.modelValue)" icon="mdi-check-circle" color="primary" /></template>
      </VListItem>
    </VList>
    <nav v-if="items.length || page > 1" class="target-pagination" aria-label="整理历史分页">
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
.target-loading { display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.5rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-selector--compact .target-hint { margin-block: -0.25rem 0.5rem; }
.target-selector--compact .target-error { margin-bottom: 0.5rem; }
.target-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: transparent; }
.target-list :deep(.v-list-item) { min-height: 5.25rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.target-list :deep(.v-list-item:last-child) { border-bottom: 0; }
.target-list :deep(.v-list-item:focus-visible) { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.target-meta--full { margin-top: 0.2rem; overflow-wrap: anywhere; }
.target-meta--short { display: none; }
.target-pagination { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding-top: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-pagination__actions { display: flex; gap: 0.125rem; }
@media (max-width: 37.5rem) {
  .target-meta--full { display: none; }
  .target-meta--short { display: block; margin-top: 0.2rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .target-pagination { align-items: flex-start; flex-direction: column; }
}
</style>
