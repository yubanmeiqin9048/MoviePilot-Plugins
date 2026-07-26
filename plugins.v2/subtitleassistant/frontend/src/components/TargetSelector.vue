<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { getErrorMessage, listTargets } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import type { PluginApi, TargetItem } from '@/types'
import { formatDate, mediaLabel, shortPath } from '@/types/presentation'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId: string
  modelValue?: TargetItem | null
  label?: string
  hint?: string
  disabled?: boolean
  compact?: boolean
}>(), {
  modelValue: null,
  label: '整理历史目标',
  hint: '显示 MoviePilot 成功的本地单文件整理历史；目标视频当前可以不存在。',
  disabled: false,
  compact: false,
})

const emit = defineEmits<{ 'update:modelValue': [value: TargetItem | null] }>()
const search = ref('')
const items = ref<TargetItem[]>([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const total = ref(0)
let requestId = 0

const selectedId = computed(() => props.modelValue?.history_id ?? '')
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / 25)))
const displayedItems = computed(() => {
  if (!props.modelValue || items.value.some(item => String(item.history_id) === String(props.modelValue?.history_id))) {
    return items.value
  }
  return [props.modelValue, ...items.value]
})

watch(search, () => {
  page.value = 1
  void load()
})
watch(page, () => void load())
watch(() => props.modelValue?.history_id, historyId => {
  if (!historyId) return
  if (!items.value.some(item => item.history_id === historyId)) void load()
})
watch(() => props.api, () => void load(), { immediate: true })

async function load(): Promise<void> {
  const current = ++requestId
  loading.value = true
  error.value = ''
  try {
    const response = await listTargets(props.api, props.pluginId, { page: page.value, pageSize: 25, search: search.value })
    if (current !== requestId) return
    items.value = response.items
    total.value = response.total
  } catch (requestError) {
    if (current === requestId) error.value = getErrorMessage(requestError, '目标视频加载失败')
  } finally {
    if (current === requestId) loading.value = false
  }
}

function choose(item: TargetItem): void {
  if (props.disabled) return
  emit('update:modelValue', item)
}

</script>

<template>
  <section class="target-selector" :class="{ 'target-selector--compact': compact }" aria-label="整理历史目标选择">
    <VTextField
      v-model="search"
      :label="label"
      placeholder="搜索媒体、文件名或完整路径"
      prepend-inner-icon="mdi-magnify"
      clearable
      hide-details="auto"
      :density="compact ? 'compact' : 'default'"
      class="target-search"
      :disabled="disabled"
    />
    <p class="target-hint">{{ hint }}</p>
    <VAlert v-if="error" type="error" variant="tonal" density="compact" class="target-error">
      {{ error }}
      <VBtn size="small" variant="text" prepend-icon="mdi-refresh" :disabled="disabled" @click="load">重试</VBtn>
    </VAlert>
    <VSkeletonLoader v-if="loading" type="list-item-three-line@4" />
    <EmptyState v-else-if="!items.length" icon="mdi-filmstrip-off" title="没有可选整理历史" message="请先在 MoviePilot 中完成一次本地媒体整理。" />
    <VList
      v-else
      class="target-list"
      lines="three"
      select-strategy="single-independent"
      role="listbox"
      :aria-label="`${label}候选`"
    >
      <VListItem
        v-for="item in displayedItems"
        :key="item.history_id"
        :active="selectedId === item.history_id"
        :disabled="disabled"
        color="primary"
        role="option"
        :aria-selected="selectedId === item.history_id"
        tabindex="0"
        @click="choose(item)"
        @keydown.enter.prevent="choose(item)"
        @keydown.space.prevent="choose(item)"
      >
        <template #prepend>
          <VIcon :icon="item.media_type === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" />
        </template>
        <VListItemTitle>{{ mediaLabel(item.media_title, item.year, item.season, item.episode) }}</VListItemTitle>
        <VListItemSubtitle>{{ item.target_file_name }}</VListItemSubtitle>
        <VListItemSubtitle class="target-meta">{{ shortPath(item.target_path) }} · 整理于 {{ formatDate(item.organized_at) }}</VListItemSubtitle>
        <template #append><VIcon v-if="selectedId === item.history_id" icon="mdi-check-circle" color="primary" /></template>
      </VListItem>
    </VList>
    <div v-if="totalPages > 1" class="target-pagination">
      <span>共 {{ total }} 个目标</span>
      <VPagination v-model="page" :length="totalPages" density="compact" total-visible="5" :disabled="disabled" />
    </div>
  </section>
</template>

<style scoped>
.target-selector { min-width: 0; }
.target-search { margin-bottom: 0.75rem; }
.target-hint { margin: -0.25rem 0 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.target-error { margin-bottom: 0.75rem; }
.target-selector--compact .target-search { margin-bottom: 0.5rem; }
.target-selector--compact .target-hint { margin-block: -0.25rem 0.5rem; }
.target-selector--compact .target-error { margin-bottom: 0.5rem; }
.target-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: transparent; }
.target-list :deep(.v-list-item) { min-height: 5.25rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.target-list :deep(.v-list-item:last-child) { border-bottom: 0; }
.target-list :deep(.v-list-item:focus-visible) { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.target-meta { margin-top: 0.2rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.target-pagination { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding-top: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
@media (max-width: 37.5rem) { .target-pagination { align-items: flex-start; flex-direction: column; } }
</style>
