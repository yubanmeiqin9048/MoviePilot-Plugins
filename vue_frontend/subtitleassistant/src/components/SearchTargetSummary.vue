<script setup lang="ts">
import { computed } from 'vue'

import type { HistoryRow, TargetItem } from '@/types'
import {
  fullPath,
  historyEpisode,
  historyFileName,
  historyMediaType,
  historyPath,
  historySeason,
  historyTitle,
  historyYear,
  mediaLabel,
  mediaTypeLabels,
  shortPath,
} from '@/types/presentation'

type TargetRecord = HistoryRow | TargetItem

const props = withDefaults(defineProps<{
  target: TargetRecord
  resolvedTarget?: TargetItem | null
  busy?: boolean
  hasResponse?: boolean
  hasSearchError?: boolean
  stale?: boolean
  disabled?: boolean
}>(), {
  resolvedTarget: null,
  busy: false,
  hasResponse: false,
  hasSearchError: false,
  stale: false,
  disabled: false,
})

const emit = defineEmits<{
  change: []
  search: []
}>()

const summaryTarget = computed<TargetRecord>(() => props.resolvedTarget || props.target)

function title(): string { return historyTitle(summaryTarget.value) }

function year(): number | null { return historyYear(summaryTarget.value) }

function mediaType() { return historyMediaType(summaryTarget.value) }

function season(): number | null { return historySeason(summaryTarget.value) }

function episode(): number | null { return historyEpisode(summaryTarget.value) }

function fileName(): string { return historyFileName(summaryTarget.value) }

function path(): string { return historyPath(summaryTarget.value) }

const searchLabel = computed(() => {
  if (props.busy) return '搜索中'
  if (props.hasSearchError) return '重新搜索'
  if (props.hasResponse && props.stale) return '按新条件搜索'
  if (props.hasResponse) return '重新搜索'
  return '搜索字幕'
})

const searchDescription = computed(() => {
  if (props.busy) return '正在查询已配置的字幕源'
  if (props.hasSearchError) return '当前搜索失败，请重试'
  if (props.hasResponse && props.stale) return '当前候选仅供参考，请按新条件重新搜索'
  if (props.hasResponse) return '针对当前整理历史目标重新搜索'
  return '针对当前整理历史目标开始人工字幕搜索'
})
</script>

<template>
  <section class="target-summary" aria-labelledby="search-target-summary-title">
    <div class="target-summary__heading">
      <div class="target-summary__identity">
        <span class="target-summary__badge" aria-hidden="true">
          <VIcon
            :icon="mediaType() === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'"
            size="24"
          />
        </span>
        <div>
          <h3 id="search-target-summary-title">{{ mediaLabel(title(), year(), season(), episode()) }}</h3>
        </div>
      </div>
      <div class="target-summary__actions">
        <VBtn
          type="button"
          variant="text"
          prepend-icon="mdi-swap-horizontal"
          aria-label="更换整理历史目标"
          :disabled="disabled"
          @click="emit('change')"
        >
          更换目标
        </VBtn>
        <VBtn
          type="button"
          color="primary"
          prepend-icon="mdi-magnify"
          :loading="busy"
          :disabled="disabled || busy"
          :aria-label="searchDescription"
          @click="emit('search')"
        >
          {{ searchLabel }}
        </VBtn>
      </div>
    </div>

    <dl class="target-summary__facts">
      <div>
        <dt>媒体标题</dt>
        <dd>{{ title() }}</dd>
      </div>
      <div>
        <dt>媒体类型</dt>
        <dd>{{ mediaTypeLabels[mediaType()] }}</dd>
      </div>
      <div>
        <dt>年份</dt>
        <dd>{{ year() ?? '未记录' }}</dd>
      </div>
      <div>
        <dt>季集</dt>
        <dd>{{ season() == null && episode() == null ? '不适用' : mediaLabel(null, null, season(), episode()) }}</dd>
      </div>
      <div class="target-summary__file">
        <dt>目标文件名</dt>
        <dd :title="fileName()">{{ fileName() }}</dd>
      </div>
      <div class="target-summary__path">
        <dt>目标路径</dt>
        <dd :title="fullPath(path())" :aria-label="`目标路径：${fullPath(path())}`">{{ shortPath(path()) }}</dd>
      </div>
    </dl>
  </section>
</template>

<style scoped>
.target-summary {
  min-width: 0;
  padding: 1rem 1.15rem;
  background: transparent;
}

.target-summary__heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.target-summary__identity {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 0.85rem;
}

.target-summary__badge {
  display: inline-flex;
  width: 2.5rem;
  height: 2.5rem;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border-radius: 0.5rem;
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.12);
}

.target-summary__identity > div { min-width: 0; }
.target-summary h3 { margin: 0; overflow: hidden; color: rgb(var(--v-theme-on-surface)); font-size: 0.9375rem; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.target-summary__actions { display: flex; flex: 0 0 auto; align-items: center; gap: 0.35rem; }
.target-summary__facts { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 0.7rem 1rem; margin: 0.9rem 0 0; padding-top: 0.85rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.target-summary__facts > div { min-width: 0; }
.target-summary__facts dt { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.625rem; line-height: 1.25; }
.target-summary__facts dd { margin: 0.18rem 0 0; overflow: hidden; color: rgb(var(--v-theme-on-surface)); font-size: 0.75rem; font-weight: 600; line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.target-summary__path dd { text-align: left; }

@media (max-width: 959px) {
  .target-summary__facts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}

@media (max-width: 600px) {
  .target-summary { padding: 0.9rem 1rem; }
  .target-summary__heading { align-items: stretch; flex-direction: column; gap: 0.75rem; }
  .target-summary__actions { width: 100%; }
  .target-summary__actions :deep(.v-btn) { flex: 1 1 0; }
  .target-summary__facts { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.7rem 0.9rem; }
  .target-summary__file, .target-summary__path { grid-column: 1 / -1; }
}

@media (max-width: 360px) {
  .target-summary__actions { align-items: stretch; flex-direction: column-reverse; }
  .target-summary__actions :deep(.v-btn) { flex: 0 0 auto; width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .target-summary *, .target-summary *::before, .target-summary *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
