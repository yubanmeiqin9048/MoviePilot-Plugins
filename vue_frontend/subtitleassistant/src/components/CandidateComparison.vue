<script setup lang="ts">
import { computed, ref } from 'vue'

import EmptyState from '@/components/EmptyState.vue'
import SearchDetailsDialog from '@/components/SearchDetailsDialog.vue'
import StateChip from '@/components/StateChip.vue'
import type { SearchSourceGroup, SubtitleCandidate, TargetItem } from '@/types'
import {
  manualSourceState,
  packageLabels,
  sourceLabels,
  subtitleSourceOrder,
  translationLabels,
} from '@/types/presentation'
import type { StatePresentation } from '@/types/presentation'

const props = defineProps<{
  candidates: SubtitleCandidate[]
  totalCandidates: number
  hasResponse: boolean
  hasTarget: boolean
  loading: boolean
  target: TargetItem | null
  sources: SearchSourceGroup[]
  downloadDisabled: boolean
  loadingCandidates: Record<string, boolean>
  downloadErrors: Record<string, string>
  downloadFeedback: Record<string, 'queued' | 'reused'>
}>()

const emit = defineEmits<{ download: [candidate: SubtitleCandidate] }>()

const detailsOpen = ref(false)

const executedSourceCount = computed(() => props.sources
  .filter(group => !['disabled', 'unconfigured'].includes(group.status)).length)

// 来源覆盖只在候选栏顶部占一行事实文字，避免诊断信息夺走候选比较的主层级。
const sourceCoverage = computed(() => {
  if (props.loading) return '正在执行三源搜索'
  if (!props.hasTarget) return '选择整理历史目标后才能搜索'
  if (!props.hasResponse) return '搜索后显示来源覆盖'
  return `已执行 ${executedSourceCount.value}/${subtitleSourceOrder.length} 个来源 · ${props.totalCandidates} 个候选`
})

const statusSummary = computed(() => props.sources
  .map(group => `${sourceLabels[group.source]}：${manualSourceState(group.status, group.candidate_count).label}`)
  .join(' · '))

const attentionSummary = computed(() => {
  const attention = props.sources.filter(group => ['limited', 'error', 'disabled', 'unconfigured'].includes(group.status))
  if (!attention.length) return ''
  return `需要注意：${attention.map(group => `${sourceLabels[group.source]}${manualSourceState(group.status, group.candidate_count).label}`).join('、')}`
})

const candidateGroups = computed(() => [
  {
    status: 'recognized' as const,
    label: '已识别候选',
    candidates: props.candidates.filter(candidate => candidate.recognition_status === 'recognized'),
  },
  {
    status: 'unrecognized' as const,
    label: '未识别候选',
    candidates: props.candidates.filter(candidate => candidate.recognition_status === 'unrecognized'),
  },
].filter(group => group.candidates.length > 0))

function recognitionState(candidate: SubtitleCandidate): StatePresentation {
  return candidate.recognition_status === 'recognized'
    ? { label: '已识别', icon: 'mdi-check-circle-outline', color: 'success' }
    : { label: '未识别', icon: 'mdi-help-circle-outline', color: 'warning' }
}

function candidateRange(candidate: SubtitleCandidate): string {
  const seasons = candidate.seasons.length ? candidate.seasons : (candidate.season == null ? [] : [candidate.season])
  const episodes = candidate.episodes.length ? candidate.episodes : (candidate.episode == null ? [] : [candidate.episode])
  const range = [
    seasons.length ? seasons.map(value => `S${String(value).padStart(2, '0')}`).join('/') : '',
    episodes.length ? episodes.map(value => `E${String(value).padStart(2, '0')}`).join('/') : '',
  ].filter(Boolean).join(' · ')
  return range || packageLabels[candidate.package_scope]
}

function candidateFormat(candidate: SubtitleCandidate): string {
  if (!candidate.format || candidate.format.toUpperCase() === 'UNKNOWN') return ''
  return `格式：${candidate.format.toUpperCase()}`
}

function candidateLanguageFacts(candidate: SubtitleCandidate): string {
  return [
    translationLabels[candidate.translation_type],
    `听障：${candidate.hearing_impaired ? '是' : '否'}`,
    candidateFormat(candidate),
  ].filter(Boolean).join(' · ')
}

function candidateTargetMismatch(candidate: SubtitleCandidate): boolean {
  if (!props.target || props.target.media_type !== 'tv') return false
  const seasons = candidate.seasons.length ? candidate.seasons : (candidate.season == null ? [] : [candidate.season])
  const episodes = candidate.episodes.length ? candidate.episodes : (candidate.episode == null ? [] : [candidate.episode])
  const seasonMismatch = seasons.length > 0 && props.target.season != null && !seasons.includes(props.target.season)
  const episodeMismatch = episodes.length > 0 && props.target.episode != null && !episodes.includes(props.target.episode)
  return seasonMismatch || episodeMismatch
}
</script>

<template>
  <section class="candidate-results" aria-labelledby="candidate-results-title">
    <div class="results-toolbar">
      <div class="results-heading">
        <div class="results-heading__title">
          <h3 id="candidate-results-title">候选结果</h3>
          <SearchDetailsDialog
            v-model="detailsOpen"
            :sources="props.sources"
            :disabled="!props.hasResponse || props.loading"
          />
        </div>
        <p class="results-heading__coverage" aria-live="polite">{{ sourceCoverage }}</p>
        <p v-if="props.hasResponse && statusSummary" class="results-heading__status">{{ statusSummary }}</p>
        <p v-if="attentionSummary" class="results-heading__attention" role="status">
          <VIcon icon="mdi-alert-outline" size="15" aria-hidden="true" />
          {{ attentionSummary }}
        </p>
      </div>

      <slot name="filters" />
    </div>

    <p v-if="props.loading" class="sr-only" role="status" aria-live="polite">正在搜索字幕源，候选结果加载中。</p>
    <VSkeletonLoader v-if="props.loading" type="list-item-three-line@6" class="results-loading" aria-hidden="true" />
    <EmptyState
      v-else-if="!props.hasTarget"
      icon="mdi-crosshairs-question"
      title="等待选择目标"
      message="先在上方选择整理历史目标，插件会据此生成各来源的默认查询。"
    />
    <EmptyState
      v-else-if="!props.hasResponse"
      icon="mdi-text-search"
      title="等待搜索"
      message="确认目标后开始搜索，结果将按来源原始顺序汇总。"
    />
    <EmptyState
      v-else-if="!props.totalCandidates"
      icon="mdi-file-search-outline"
      title="没有候选结果"
      message="本次所有来源均未返回可下载字幕，请查看来源状态或调整搜索关键词。"
    />
    <EmptyState
      v-else-if="!props.candidates.length"
      icon="mdi-filter-off-outline"
      title="当前筛选没有候选"
      message="当前筛选隐藏了全部候选，请清除或切换筛选条件。"
    />
    <div v-else class="candidate-table-wrap" aria-live="polite">
      <table class="candidate-table">
        <caption class="sr-only">人工字幕搜索候选比较表；移动设备上按字段标签顺序阅读。</caption>
        <thead>
          <tr>
            <th scope="col">候选</th>
            <th scope="col">范围</th>
            <th scope="col">语言 / 类型</th>
            <th scope="col">来源</th>
            <th scope="col"><span class="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody v-for="group in candidateGroups" :key="group.status">
          <tr class="candidate-group-row">
            <th colspan="5" scope="rowgroup">
              <span>{{ group.label }}</span>
              <small>{{ group.candidates.length }} 个</small>
            </th>
          </tr>
          <tr v-for="candidate in group.candidates" :key="candidate.candidate_key">
            <td data-label="候选" class="candidate-name">
              <span class="candidate-field-label">候选</span>
              <div class="candidate-name__top">
                <StateChip :state="recognitionState(candidate)" size="x-small" />
                <span class="candidate-name__source">{{ sourceLabels[candidate.source] }}</span>
              </div>
              <strong class="candidate-value">{{ candidate.name }}</strong>
              <span v-if="candidate.file_name" class="candidate-secondary">文件名：{{ candidate.file_name }}</span>
            </td>
            <td data-label="范围" class="candidate-range">
              <span class="candidate-field-label">范围</span>
              <strong class="candidate-value">{{ candidateRange(candidate) }}</strong>
              <small class="candidate-note">{{ packageLabels[candidate.package_scope] }}</small>
              <span v-if="candidateTargetMismatch(candidate)" class="candidate-warning" role="status">
                <VIcon icon="mdi-alert-outline" size="15" aria-hidden="true" />与当前目标集不同，仍可人工选择
              </span>
            </td>
            <td data-label="语言 / 类型" class="candidate-language">
              <span class="candidate-field-label">语言 / 类型</span>
              <strong class="candidate-value">{{ candidate.language || '语言未标记' }}</strong>
              <small class="candidate-note">{{ candidateLanguageFacts(candidate) }}</small>
            </td>
            <td data-label="来源" class="candidate-source">
              <span class="candidate-field-label">来源</span>
              <strong class="candidate-value">{{ sourceLabels[candidate.source] }}</strong>
            </td>
            <td data-label="操作" class="candidate-action">
              <div class="candidate-action__inner">
                <VAlert v-if="props.downloadErrors[candidate.candidate_key]" type="error" variant="tonal" density="compact" class="download-error">
                  {{ props.downloadErrors[candidate.candidate_key] }}
                </VAlert>
                <VAlert
                  v-else-if="props.downloadFeedback[candidate.candidate_key] === 'reused'"
                  type="info"
                  variant="tonal"
                  density="compact"
                  class="download-feedback"
                  role="status"
                >
                  已有处理中任务已复用
                </VAlert>
                <VAlert
                  v-else-if="props.downloadFeedback[candidate.candidate_key] === 'queued'"
                  type="success"
                  variant="tonal"
                  density="compact"
                  class="download-feedback"
                  role="status"
                >
                  已加入下载队列
                </VAlert>
                <VBtn
                  class="candidate-download-button"
                  color="primary"
                  variant="tonal"
                  size="small"
                  prepend-icon="mdi-download"
                  :aria-label="props.loadingCandidates[candidate.candidate_key] ? '正在提交 ' + candidate.name : '下载 ' + candidate.name"
                  :loading="Boolean(props.loadingCandidates[candidate.candidate_key])"
                  :disabled="props.downloadDisabled || Boolean(props.loadingCandidates[candidate.candidate_key])"
                  @click="emit('download', candidate)"
                >
                  {{ props.loadingCandidates[candidate.candidate_key] ? '提交中' : '下载' }}
                </VBtn>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<style scoped>
.candidate-results {
  min-width: 0;
  margin-top: 0.75rem;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 0.5rem;
  background: rgba(var(--v-theme-surface), 0.55);
}

.results-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.75rem 1rem;
  padding: 0.8rem 1rem;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.results-heading {
  min-width: 0;
}

.results-heading__title {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.results-heading h3 {
  margin: 0;
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.9375rem;
  font-weight: 650;
}

.results-heading__coverage,
.results-heading__status {
  margin: 0.22rem 0 0;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
  line-height: 1.45;
}

.results-heading__attention {
  display: flex;
  align-items: flex-start;
  gap: 0.25rem;
  margin: 0.3rem 0 0;
  color: rgb(var(--v-theme-warning));
  font-size: 0.6875rem;
  line-height: 1.4;
}

.results-loading {
  min-height: 20rem;
}

/* 表头冻结依赖没有 overflow 祖先：一旦这里裁剪，sticky 会粘在不滚动的容器上而失效。 */
.candidate-table-wrap {
  min-width: 0;
}

.candidate-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: separate;
  border-spacing: 0;
  font-size: 0.75rem;
}

.candidate-table thead th {
  position: sticky;
  z-index: 2;
  inset-block-start: 0;
  padding: 0.6rem 1rem;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
  font-weight: 650;
  text-align: left;
  background-color: rgb(var(--v-theme-surface));
  background-image: linear-gradient(
    rgba(var(--v-theme-on-surface), 0.04),
    rgba(var(--v-theme-on-surface), 0.04)
  );
}

.candidate-table thead th:nth-child(1) { width: 38%; }
.candidate-table thead th:nth-child(2) { width: 12%; }
.candidate-table thead th:nth-child(3) { width: 20%; }
.candidate-table thead th:nth-child(4) { width: 16%; }
.candidate-table thead th:last-child { width: 14%; }

/* 识别状态与来源随候选名称同格展示，桌面端优先横向比较候选事实。 */
.candidate-name__top {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  margin-bottom: 0.3rem;
}

/* 桌面端来源在独立列比较，此处不重复；窄屏无来源列时才显示。 */
.candidate-name__source { display: none; }

.candidate-table td {
  padding: 0.8rem 1rem;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  vertical-align: top;
  overflow-wrap: anywhere;
}

.candidate-table tbody > tr:not(.candidate-group-row) {
  transition: background-color 160ms ease;
}

.candidate-table tbody > tr:not(.candidate-group-row):hover {
  background: rgba(var(--v-theme-primary), 0.05);
}

.candidate-group-row th {
  padding: 0.6rem 1rem 0.45rem;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.75rem;
  font-weight: 650;
  text-align: left;
  background: rgba(var(--v-theme-on-surface), 0.02);
}

.candidate-group-row th span,
.candidate-group-row th small {
  display: inline-block;
}

.candidate-group-row th small {
  margin-left: 0.4rem;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
  font-weight: 500;
}

.candidate-table .candidate-value,
.candidate-table .candidate-secondary,
.candidate-table .candidate-note {
  display: block;
}

.candidate-field-label {
  display: none;
}

.candidate-table td strong {
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.75rem;
  font-weight: 650;
}

.candidate-table .candidate-secondary,
.candidate-table .candidate-note {
  margin-top: 0.16rem;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
  line-height: 1.4;
}

.candidate-name strong {
  font-size: 0.8125rem;
  overflow-wrap: anywhere;
}

.candidate-warning {
  display: flex;
  align-items: flex-start;
  gap: 0.25rem;
  margin-top: 0.35rem;
  color: rgb(var(--v-theme-warning));
  font-size: 0.6875rem;
  line-height: 1.4;
}

.candidate-action {
  text-align: end;
}

.candidate-action__inner {
  display: grid;
  justify-items: end;
  gap: 0.45rem;
}

.download-error {
  margin: 0;
  font-size: 0.6875rem;
}

.download-feedback {
  margin: 0;
  font-size: 0.6875rem;
}

.candidate-download-button {
  min-width: 4.75rem;
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

@media (max-width: 800px) {
  .results-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .candidate-table-wrap {
    padding: 0.7rem;
  }

  .candidate-table,
  .candidate-table tbody {
    display: block;
    width: 100%;
  }

  .candidate-table thead {
    display: none;
  }

  .candidate-table tbody {
    display: grid;
    gap: 0.7rem;
  }

  .candidate-table .candidate-group-row {
    display: block;
  }

  .candidate-table .candidate-group-row th {
    display: block;
    padding: 0.15rem 0.2rem 0;
    background: transparent;
  }

  .candidate-table tbody > tr:not(.candidate-group-row) {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.7rem 0.9rem;
    padding: 0.9rem;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 0.45rem;
    background: rgba(var(--v-theme-surface), 0.58);
  }

  .candidate-table tbody > tr:not(.candidate-group-row) td {
    display: block;
    min-width: 0;
    padding: 0;
    border: 0;
  }

  .candidate-table tbody > tr:not(.candidate-group-row) .candidate-field-label {
    display: block;
    margin-bottom: 0.22rem;
    color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
    font-size: 0.625rem;
    line-height: 1.2;
  }

  /* 候选格已含识别状态与来源，窄屏按“状态与来源 → 名称 → 范围/语言 → 下载”顺序阅读。 */
  .candidate-table .candidate-name {
    grid-column: 1 / -1;
  }

  .candidate-table .candidate-name .candidate-field-label {
    display: none !important;
  }

  .candidate-table .candidate-name__top {
    justify-content: space-between;
    margin-bottom: 0.4rem;
  }

  .candidate-table .candidate-name__source {
    display: inline-flex;
    max-width: 60%;
    min-width: 0;
    overflow: hidden;
    color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
    font-size: 0.6875rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* 来源已在候选格顶部显示，避免窄屏重复。 */
  .candidate-table .candidate-source {
    display: none !important;
  }

  .candidate-table .candidate-range,
  .candidate-table .candidate-language {
    min-width: 0;
  }

  .candidate-table tbody > tr:not(.candidate-group-row):hover {
    background: rgba(var(--v-theme-surface), 0.58);
  }

  .candidate-table .candidate-action {
    grid-column: 1 / -1;
    margin-top: 0.05rem;
    padding-top: 0.7rem !important;
    border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)) !important;
  }

  .candidate-table .candidate-action__inner {
    justify-items: stretch;
  }

  .candidate-table .candidate-action::before {
    display: none;
  }

  .candidate-download-button {
    width: 100%;
    min-height: 2.75rem;
  }
}

@media (prefers-reduced-motion: reduce) {
  .candidate-results *,
  .candidate-results *::before,
  .candidate-results *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
