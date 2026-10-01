<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

import EmptyState from '@/components/EmptyState.vue'
import SearchDetailsDialog from '@/components/SearchDetailsDialog.vue'
import StateChip from '@/components/StateChip.vue'
import { useStickyTableClipping } from '@/composables/useStickyTableClipping'
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
  downloadFeedback: Record<string, 'queued'>
}>()

const emit = defineEmits<{ download: [candidate: SubtitleCandidate] }>()

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]

const detailsOpen = ref(false)
const page = ref(1)
const pageSize = ref(20)
const { mdAndUp } = useDisplay()

// 候选在会话内一次性返回，分页只切分本地列表：桌面端翻页，窄屏累加。
const isDesktop = computed(() => mdAndUp.value)
const totalPages = computed(() => Math.max(1, Math.ceil(props.candidates.length / pageSize.value)))
const pagedCandidates = computed(() => (isDesktop.value
  ? props.candidates.slice((page.value - 1) * pageSize.value, page.value * pageSize.value)
  : props.candidates.slice(0, page.value * pageSize.value)))
const canLoadMore = computed(() => !isDesktop.value && pagedCandidates.value.length < props.candidates.length)
const rangeStart = computed(() => (props.candidates.length ? (isDesktop.value ? page.value - 1 : 0) * pageSize.value + 1 : 0))
const rangeEnd = computed(() => Math.min(props.candidates.length, rangeStart.value - 1 + pagedCandidates.value.length))
const paginationSummary = computed(() => props.candidates.length
  ? `当前 ${rangeStart.value}-${rangeEnd.value} / ${props.candidates.length}`
  : '共 0 个候选')

watch(() => props.candidates, () => {
  page.value = 1
})

watch(pageSize, () => {
  page.value = 1
})

watch(isDesktop, () => {
  page.value = 1
})

function loadMore(): void {
  if (canLoadMore.value) page.value += 1
}

// 结果工具栏在桌面端冻结；表头粘贴位置需要它的实际高度，随指标行折行动态测量。
const toolbarElement = ref<HTMLElement | null>(null)
const tableElement = ref<HTMLTableElement | null>(null)
const toolbarHeight = ref(0)
let toolbarObserver: ResizeObserver | null = null
const scheduleBodyClipping = useStickyTableClipping(() => tableElement.value)

function measureToolbar(): void {
  const element = toolbarElement.value
  if (element) toolbarHeight.value = element.offsetHeight
  scheduleBodyClipping()
}

onMounted(() => {
  measureToolbar()
  const element = toolbarElement.value
  if (element && typeof ResizeObserver !== 'undefined') {
    toolbarObserver = new ResizeObserver(measureToolbar)
    toolbarObserver.observe(element)
  }
})

onBeforeUnmount(() => {
  toolbarObserver?.disconnect()
  toolbarObserver = null
})

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
  const attention = props.sources.filter(group => ['partial', 'limited', 'error', 'disabled', 'unconfigured'].includes(group.status))
  if (!attention.length) return ''
  return `需要注意：${attention.map(group => `${sourceLabels[group.source]}${manualSourceState(group.status, group.candidate_count).label}`).join('、')}`
})

const candidateGroups = computed(() => [
  {
    status: 'recognized' as const,
    label: '已识别候选',
    candidates: pagedCandidates.value.filter(candidate => candidate.recognition_status === 'recognized'),
  },
  {
    status: 'unrecognized' as const,
    label: '未识别候选',
    candidates: pagedCandidates.value.filter(candidate => candidate.recognition_status === 'unrecognized'),
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

function candidateTargetMismatch(candidate: SubtitleCandidate): boolean {
  if (!props.target || props.target.media_type !== 'tv') return false
  const seasons = candidate.seasons.length ? candidate.seasons : (candidate.season == null ? [] : [candidate.season])
  const episodes = candidate.episodes.length ? candidate.episodes : (candidate.episode == null ? [] : [candidate.episode])
  const seasonMismatch = seasons.length > 0 && props.target.season != null && !seasons.includes(props.target.season)
  const episodeMismatch = episodes.length > 0 && props.target.episode != null && !episodes.includes(props.target.episode)
  return seasonMismatch || episodeMismatch
}

/**
 * 候选是否已提交下载。
 * 提交成功后按钮自身承载该状态,桌面端与窄屏都不再额外占一行提示。
 */
function isDownloaded(candidate: SubtitleCandidate): boolean {
  return props.downloadFeedback[candidate.candidate_key] === 'queued'
}

/** 按钮文案:提交中优先,其次为终态「已下载」,否则为可执行的「下载」。 */
function candidateActionText(candidate: SubtitleCandidate): string {
  if (props.loadingCandidates[candidate.candidate_key]) return '提交中'
  return isDownloaded(candidate) ? '已下载' : '下载'
}

function candidateActionIcon(candidate: SubtitleCandidate): string {
  return isDownloaded(candidate) ? 'mdi-check' : 'mdi-download'
}

function candidateActionLabel(candidate: SubtitleCandidate): string {
  if (props.loadingCandidates[candidate.candidate_key]) return `正在提交 ${candidate.name}`
  return isDownloaded(candidate) ? `${candidate.name} 已加入下载队列` : `下载 ${candidate.name}`
}
</script>

<template>
  <section
    class="candidate-results"
    aria-labelledby="candidate-results-title"
    :style="{ '--candidate-toolbar-height': `${toolbarHeight}px` }"
  >
    <div ref="toolbarElement" class="results-toolbar">
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
      <table ref="tableElement" class="candidate-table">
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
              <span class="candidate-secondary">文件名：{{ candidate.file_name || '未提供' }}</span>
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
              <small class="candidate-note">{{ translationLabels[candidate.translation_type] }}</small>
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
                <span v-if="isDownloaded(candidate)" class="sr-only" role="status" aria-live="polite">
                  已加入下载队列
                </span>
                <VBtn
                  class="candidate-download-button"
                  :class="{ 'candidate-download-button--done': isDownloaded(candidate) }"
                  :color="isDownloaded(candidate) ? 'success' : 'primary'"
                  variant="tonal"
                  size="small"
                  :prepend-icon="candidateActionIcon(candidate)"
                  :aria-label="candidateActionLabel(candidate)"
                  :loading="Boolean(props.loadingCandidates[candidate.candidate_key])"
                  :disabled="props.downloadDisabled || Boolean(props.loadingCandidates[candidate.candidate_key]) || isDownloaded(candidate)"
                  @click="emit('download', candidate)"
                >
                  {{ candidateActionText(candidate) }}
                </VBtn>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="candidate-pagination">
        <span class="candidate-pagination__summary" role="status" aria-live="polite">{{ paginationSummary }}</span>
        <VPagination
          v-if="isDesktop"
          v-model="page"
          :length="totalPages"
          :total-visible="5"
          density="comfortable"
          aria-label="候选结果分页"
        />
        <VSelect
          v-if="isDesktop"
          v-model="pageSize"
          :items="PAGE_SIZE_OPTIONS"
          label="每页"
          density="compact"
          hide-details
          class="candidate-pagination__size"
        />
        <VBtn
          v-else-if="canLoadMore"
          variant="tonal"
          prepend-icon="mdi-chevron-down"
          @click="loadMore"
        >
          加载更多
        </VBtn>
      </div>
    </div>
  </section>
</template>

<style scoped>
.candidate-results {
  min-width: 0;
  margin-top: 0.75rem;
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

.candidate-table thead {
  position: sticky;
  z-index: 2;
  inset-block-start: 0;
  background: var(--glass-sheen, rgb(var(--v-theme-surface)));
}

.candidate-table thead th {
  padding: 0.6rem 1rem;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
  font-weight: 650;
  text-align: left;
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

.candidate-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem 1rem;
  padding: 0.6rem 1rem;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.candidate-pagination__summary {
  min-width: 0;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.6875rem;
}

.candidate-pagination__size {
  flex: 0 0 7rem;
  max-width: 7rem;
}

/* 文案在「下载 / 提交中 / 已下载」之间切换,统一按最长终态留宽,避免列内抖动。 */
.candidate-download-button {
  min-width: 6rem;
}

/* 终态按钮仍需被读作成功而非禁用:Vuetify 默认 0.26 透明度会埋掉绿色,这里回调到可读区间。 */
.candidate-download-button--done {
  opacity: 1 !important;
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

/* 桌面端滚动容器是工作台视图自身：工具栏与表头依次冻结，表头让出工具栏实测高度。 */
@media (min-width: 960px) {
  .results-toolbar {
    position: sticky;
    z-index: 3;
    inset-block-start: 0;
    border-start-start-radius: 0.45rem;
    border-start-end-radius: 0.45rem;
    background: var(--glass-sheen, rgb(var(--v-theme-surface)));
  }

  .candidate-table thead {
    inset-block-start: var(--candidate-toolbar-height, 0px);
  }
}

@media (max-width: 800px) {
  .results-toolbar {
    align-items: stretch;
    flex-direction: column;
  }

  .candidate-pagination {
    justify-content: center;
    flex-wrap: wrap;
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
