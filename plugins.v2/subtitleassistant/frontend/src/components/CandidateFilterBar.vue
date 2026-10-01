<script setup lang="ts">
import { computed } from 'vue'

import type {
  CandidateRecognitionFilter,
  CandidateSourceFilter,
  CandidateSourceFilterOption,
} from '@/types'

const props = defineProps<{
  recognitionFilter: CandidateRecognitionFilter
  sourceFilter: CandidateSourceFilter
  totalCandidates: number
  visibleCandidates: number
  recognizedCandidates: number
  unrecognizedCandidates: number
  sourceFilterItems: CandidateSourceFilterOption[]
}>()

const emit = defineEmits<{
  'update:recognitionFilter': [value: CandidateRecognitionFilter]
  'update:sourceFilter': [value: CandidateSourceFilter]
  clear: []
}>()

const hasActiveFilter = computed(() => props.recognitionFilter !== 'all' || props.sourceFilter !== 'all')

function updateRecognitionFilter(value: unknown): void {
  if (value === 'all' || value === 'recognized' || value === 'unrecognized') {
    emit('update:recognitionFilter', value)
  }
}

function updateSourceFilter(value: unknown): void {
  if (value === 'all' || value === 'moviepilot' || value === 'opensubtitles' || value === 'assrt') {
    emit('update:sourceFilter', value)
  }
}
</script>

<template>
  <div class="candidate-filters" role="group" aria-label="筛选候选">
    <VBtnToggle
      class="recognition-filter"
      :model-value="props.recognitionFilter"
      mandatory
      color="primary"
      variant="outlined"
      density="compact"
      aria-label="按识别状态筛选候选"
      @update:model-value="updateRecognitionFilter"
    >
      <VBtn value="all">全部 {{ props.totalCandidates }}</VBtn>
      <VBtn value="recognized">已识别 {{ props.recognizedCandidates }}</VBtn>
      <VBtn value="unrecognized">未识别 {{ props.unrecognizedCandidates }}</VBtn>
    </VBtnToggle>
    <VSelect
      :model-value="props.sourceFilter"
      class="source-filter"
      label="字幕源"
      aria-label="按字幕源筛选候选"
      :items="props.sourceFilterItems"
      variant="outlined"
      density="compact"
      hide-details
      @update:model-value="updateSourceFilter"
    />
    <div class="filter-status">
      <span class="filter-count" role="status" aria-live="polite">
        当前显示 {{ props.visibleCandidates }} / {{ props.totalCandidates }}
      </span>
      <VBtn
        v-if="hasActiveFilter"
        class="clear-filter-button"
        variant="text"
        size="small"
        @click="emit('clear')"
      >
        清除筛选
      </VBtn>
    </div>
  </div>
</template>

<style scoped>
.candidate-filters {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.65rem;
}

.recognition-filter {
  height: 2.5rem;
  flex: 0 1 auto;
}

.recognition-filter :deep(.v-btn) {
  min-width: 0;
  height: 2.5rem;
  padding-inline: 0.7rem;
  font-size: 0.75rem;
  letter-spacing: 0;
}

.source-filter {
  flex: 0 1 11rem;
  min-width: 8.5rem;
}

.filter-status {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 0.35rem;
}

.filter-count {
  flex: 0 0 auto;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
}

.clear-filter-button {
  flex: 0 0 auto;
  white-space: nowrap;
}

@media (max-width: 800px) {
  .candidate-filters {
    align-items: stretch;
    flex-direction: column;
    gap: 0.5rem;
  }

  .recognition-filter {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .recognition-filter :deep(.v-btn) {
    padding-inline: 0.3rem;
    font-size: 0.6875rem;
  }

  .source-filter {
    flex: 0 0 auto;
    width: 100%;
  }

  .filter-status {
    justify-content: space-between;
  }
}
</style>
