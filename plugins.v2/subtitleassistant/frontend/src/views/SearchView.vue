<script setup lang="ts">
import { computed, inject, ref } from 'vue'

import { downloadCandidate, getErrorCode, getErrorMessage, searchSubtitles } from '@/api/client'
import CandidateComparison from '@/components/CandidateComparison.vue'
import CandidateFilterBar from '@/components/CandidateFilterBar.vue'
import SearchKeywordPanel from '@/components/SearchKeywordPanel.vue'
import SearchTargetSummary from '@/components/SearchTargetSummary.vue'
import TargetPickerDialog from '@/components/TargetPickerDialog.vue'
import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import type {
  HostToast,
  CandidateRecognitionFilter,
  CandidateSourceFilter,
  CandidateSourceFilterOption,
  HistoryRow,
  PluginApi,
  SearchPlanItem,
  SearchResponse,
  SearchSourceGroup,
  SubtitleCandidate,
  SubtitleSource,
  TargetItem,
} from '@/types'
import {
  historyId,
  historyTitle,
  sameHistoryId,
  sourceLabels,
  subtitleSourceOrder,
} from '@/types/presentation'

type SearchConditionSnapshot = {
  targetHistoryId: string | number
  keywords: Record<SubtitleSource, string | null>
}
type DownloadFeedback = 'queued' | 'reused'

const props = defineProps<{ api: PluginApi; pluginId: string; active: boolean }>()
const emit = defineEmits<{ action: [] }>()
const toast = inject<HostToast | null>('moviepilot:toast', null)

const selectedHistory = ref<HistoryRow | null>(null)
const target = ref<TargetItem | null>(null)
const keywords = ref<Record<SubtitleSource, string>>(emptyKeywords())
const response = ref<SearchResponse | null>(null)
const searchedConditions = ref<SearchConditionSnapshot | null>(null)
const recognitionFilter = ref<CandidateRecognitionFilter>('all')
const sourceFilter = ref<CandidateSourceFilter>('all')
const loading = ref(false)
const loadingCandidates = ref<Record<string, boolean>>({})
const searchError = ref('')
const downloadErrors = ref<Record<string, string>>({})
const downloadFeedback = ref<Record<string, DownloadFeedback>>({})
const sessionExpired = ref(false)
const notice = ref('')
const targetChangeConfirmOpen = ref(false)
const targetPickerOpen = ref(false)
const { captureFocus: captureTargetChangeFocus, restoreFocus: restoreTargetChangeFocus } = useDialogFocusReturn()
let searchRequestId = 0

const groups = computed<SearchSourceGroup[]>(() => response.value?.sources ?? [])
const partitionedCandidates = computed(() => {
  const recognized: SubtitleCandidate[] = []
  const unrecognized: SubtitleCandidate[] = []
  for (const group of groups.value) {
    for (const candidate of group.candidates) {
      if (candidate.recognition_status === 'recognized') recognized.push(candidate)
      else unrecognized.push(candidate)
    }
  }
  return { recognized, unrecognized }
})
const recognizedCandidates = computed(() => partitionedCandidates.value.recognized)
const unrecognizedCandidates = computed(() => partitionedCandidates.value.unrecognized)
const orderedCandidates = computed(() => [...recognizedCandidates.value, ...unrecognizedCandidates.value])
const visibleCandidates = computed(() => orderedCandidates.value
  .filter(candidate => recognitionFilter.value === 'all' || candidate.recognition_status === recognitionFilter.value)
  .filter(candidate => sourceFilter.value === 'all' || candidate.source === sourceFilter.value))
const sourceFilterItems = computed<CandidateSourceFilterOption[]>(() => {
  const counts = new Map<SubtitleSource, number>()
  for (const candidate of orderedCandidates.value) {
    counts.set(candidate.source, (counts.get(candidate.source) || 0) + 1)
  }
  return [
    { title: `全部来源 · ${orderedCandidates.value.length}`, value: 'all' },
    ...groups.value.map(group => ({
      title: `${sourceLabels[group.source]} · ${counts.get(group.source) || 0}`,
      value: group.source,
    })),
  ]
})
const currentConditions = computed<SearchConditionSnapshot | null>(() => {
  const targetHistoryId = historyId(selectedHistory.value)
  if (targetHistoryId == null) return null
  return {
    targetHistoryId,
    keywords: {
      moviepilot: cleanKeyword(keywords.value.moviepilot),
      opensubtitles: cleanKeyword(keywords.value.opensubtitles),
      assrt: cleanKeyword(keywords.value.assrt),
    },
  }
})
const resultsStale = computed(() => Boolean(
  response.value && !sameSearchConditions(searchedConditions.value, currentConditions.value),
))
const hasCustomKeywords = computed(() => subtitleSourceOrder.some(source => Boolean(cleanKeyword(keywords.value[source]))))
const hasSearchState = computed(() => Boolean(response.value) || hasCustomKeywords.value)
const preliminaryKeywordPlans = computed<Record<SubtitleSource, SearchPlanItem[]>>(() => {
  const selected = selectedHistory.value
  const title = selected ? historyTitle(selected) : ''
  const usableTitle = title !== '未识别媒体' && title.length >= 3 ? title : null
  const mediaId = selected?.imdbid || (selected?.tmdbid == null ? null : String(selected.tmdbid))
  return {
    moviepilot: [{ kind: 'title', label: '英文标题关键词（搜索时生成）', query: null, editable: false }],
    opensubtitles: [
      { kind: 'id', label: '媒体 ID', query: mediaId, editable: false },
      { kind: 'title', label: '英文标题（搜索时补充）', query: null, editable: false },
    ],
    assrt: [{ kind: 'title', label: '主标题', query: usableTitle, editable: Boolean(usableTitle) }],
  }
})
const keywordPlans = computed<Record<SubtitleSource, SearchPlanItem[]>>(() => {
  const plans = {} as Record<SubtitleSource, SearchPlanItem[]>
  for (const source of subtitleSourceOrder) {
    plans[source] = response.value?.sources.find(group => group.source === source)?.default_plans
      || target.value?.search_plans?.[source]
      || preliminaryKeywordPlans.value[source]
      || []
  }
  return plans
})

async function search(): Promise<void> {
  const requestConditions = currentConditions.value
  if (!requestConditions) {
    searchError.value = '请先选择 MoviePilot 整理历史，再开始搜索。'
    return
  }
  const requestId = ++searchRequestId
  loading.value = true
  searchError.value = ''
  sessionExpired.value = false
  notice.value = ''
  downloadErrors.value = {}
  downloadFeedback.value = {}
  loadingCandidates.value = {}
  response.value = null
  recognitionFilter.value = 'all'
  sourceFilter.value = 'all'
  try {
    const searchResponse = await searchSubtitles(props.api, props.pluginId, {
      target_history_id: requestConditions.targetHistoryId,
      moviepilot_keyword: requestConditions.keywords.moviepilot,
      opensubtitles_keyword: requestConditions.keywords.opensubtitles,
      assrt_keyword: requestConditions.keywords.assrt,
    })
    if (requestId !== searchRequestId) return
    response.value = searchResponse
    target.value = searchResponse.target
    searchedConditions.value = requestConditions
    if (!response.value.session_id) notice.value = '本次没有可下载候选，请调整关键词或检查字幕源状态。'
  } catch (requestError) {
    if (requestId === searchRequestId) searchError.value = getErrorMessage(requestError, '字幕搜索失败')
  } finally {
    if (requestId === searchRequestId) loading.value = false
  }
}

async function download(candidate: SubtitleCandidate): Promise<void> {
  if (loading.value || sessionExpired.value || !response.value?.session_id || resultsStale.value) return
  const requestId = searchRequestId
  const candidateKey = candidate.candidate_key
  const sessionId = response.value.session_id
  loadingCandidates.value = { ...loadingCandidates.value, [candidateKey]: true }
  downloadErrors.value = { ...downloadErrors.value, [candidateKey]: '' }
  const nextFeedback = { ...downloadFeedback.value }
  delete nextFeedback[candidateKey]
  downloadFeedback.value = nextFeedback
  try {
    const result = await downloadCandidate(props.api, props.pluginId, sessionId, candidateKey)
    if (requestId !== searchRequestId) return
    downloadFeedback.value = {
      ...downloadFeedback.value,
      [candidateKey]: result.reused ? 'reused' : 'queued',
    }
    if (result.reused) toast?.info('该候选已有处理中任务，已复用原任务')
    else toast?.success('已加入下载队列')
    emit('action')
  } catch (requestError) {
    if (requestId !== searchRequestId) return
    if (getErrorCode(requestError) === 'manual_search_session_expired') {
      sessionExpired.value = true
      return
    }
    const message = getErrorMessage(requestError, '字幕下载任务提交失败')
    downloadErrors.value = { ...downloadErrors.value, [candidateKey]: message }
  } finally {
    if (requestId !== searchRequestId) return
    const nextLoadingCandidates = { ...loadingCandidates.value }
    delete nextLoadingCandidates[candidateKey]
    loadingCandidates.value = nextLoadingCandidates
  }
}

function requestTargetChange(): void {
  if (hasSearchState.value) {
    captureTargetChangeFocus()
    targetChangeConfirmOpen.value = true
    return
  }
  targetPickerOpen.value = true
}

function confirmTargetChange(): void {
  targetChangeConfirmOpen.value = false
  clearTargetState()
  targetPickerOpen.value = true
}

function applyTarget(history: HistoryRow): void {
  if (sameHistoryId(history, selectedHistory.value)) return
  clearTargetState()
  selectedHistory.value = history
}

function clearTargetState(): void {
  searchRequestId += 1
  loading.value = false
  selectedHistory.value = null
  target.value = null
  response.value = null
  searchedConditions.value = null
  keywords.value = emptyKeywords()
  recognitionFilter.value = 'all'
  sourceFilter.value = 'all'
  searchError.value = ''
  notice.value = ''
  downloadErrors.value = {}
  downloadFeedback.value = {}
  sessionExpired.value = false
  loadingCandidates.value = {}
}

function emptyKeywords(): Record<SubtitleSource, string> {
  return { moviepilot: '', opensubtitles: '', assrt: '' }
}

function sameSearchConditions(
  left: SearchConditionSnapshot | null,
  right: SearchConditionSnapshot | null,
): boolean {
  if (!left || !right || left.targetHistoryId !== right.targetHistoryId) return false
  return subtitleSourceOrder.every(source => left.keywords[source] === right.keywords[source])
}

function cleanKeyword(value: string | null | undefined): string | null {
  const trimmed = (value || '').trim()
  return trimmed || null
}

function setKeyword(source: SubtitleSource, value: string | null): void {
  keywords.value[source] = value || ''
}

function clearCandidateFilters(): void {
  recognitionFilter.value = 'all'
  sourceFilter.value = 'all'
}
</script>

<template>
  <section class="view-shell search-view" aria-labelledby="search-view-title">
    <header class="view-header">
      <div>
        <h2 id="search-view-title">字幕搜索</h2>
        <p>{{ selectedHistory ? '确认来源执行结果，在统一候选流中判断并下载。' : '先从 MoviePilot 整理历史中确认要补字幕的目标。' }}</p>
      </div>
    </header>

    <VAlert v-if="resultsStale" type="warning" variant="tonal" density="compact" class="stale-alert" role="status" aria-live="polite">
      搜索条件已变化。当前候选仍来自上一次搜索；重新搜索后才能下载。
    </VAlert>

    <section
      class="execution-overview"
      :class="{ 'execution-overview--empty': !selectedHistory }"
      aria-label="搜索执行总览"
    >
      <div v-if="!selectedHistory" class="target-empty">
        <span class="target-empty__badge" aria-hidden="true"><VIcon icon="mdi-crosshairs-question" size="26" /></span>
        <div class="target-empty__copy">
          <h3 id="target-picker-title">先确定要补字幕的整理历史目标</h3>
          <p>从 MoviePilot 整理历史中选择一条记录，插件会据此生成各来源的默认查询。</p>
        </div>
        <VBtn
          type="button"
          color="primary"
          variant="flat"
          prepend-icon="mdi-format-list-bulleted"
          @click="targetPickerOpen = true"
        >
          选择目标
        </VBtn>
      </div>

      <template v-else>
        <SearchTargetSummary
          :target="selectedHistory"
          :resolved-target="target"
          :busy="loading"
          :has-response="Boolean(response)"
          :has-search-error="Boolean(searchError)"
          :stale="resultsStale"
          :disabled="historyId(selectedHistory) == null"
          @change="requestTargetChange"
          @search="search"
        />

        <SearchKeywordPanel
          :keywords="keywords"
          :plans="keywordPlans"
          @change="setKeyword"
        />
      </template>
    </section>

    <VAlert v-if="searchError" type="error" variant="tonal" density="compact" class="search-feedback">
      {{ searchError }}
    </VAlert>
    <VAlert
      v-if="sessionExpired"
      type="warning"
      variant="tonal"
      density="compact"
      class="session-expired-feedback"
      role="alert"
      aria-live="polite"
    >
      <div class="session-expired-feedback__content">
        <span>搜索会话已失效，请重新搜索。当前候选仅供阅读，下载动作已禁用。</span>
        <VBtn type="button" color="warning" variant="text" size="small" @click="search">重新搜索</VBtn>
      </div>
    </VAlert>
    <VAlert v-if="notice" type="info" variant="tonal" density="compact" closable class="search-feedback" @click:close="notice = ''">
      {{ notice }}
    </VAlert>

    <CandidateComparison
      :candidates="visibleCandidates"
      :total-candidates="orderedCandidates.length"
      :has-response="Boolean(response)"
      :has-target="Boolean(selectedHistory)"
      :loading="loading"
      :target="target"
      :sources="groups"
      :download-disabled="resultsStale || sessionExpired"
      :loading-candidates="loadingCandidates"
      :download-errors="downloadErrors"
      :download-feedback="downloadFeedback"
      @download="download"
    >
      <template #filters>
        <CandidateFilterBar
          v-if="response && !loading && orderedCandidates.length"
          :recognition-filter="recognitionFilter"
          :source-filter="sourceFilter"
          :total-candidates="orderedCandidates.length"
          :visible-candidates="visibleCandidates.length"
          :recognized-candidates="recognizedCandidates.length"
          :unrecognized-candidates="unrecognizedCandidates.length"
          :source-filter-items="sourceFilterItems"
          @update:recognition-filter="recognitionFilter = $event"
          @update:source-filter="sourceFilter = $event"
          @clear="clearCandidateFilters"
        />
      </template>
    </CandidateComparison>

    <TargetPickerDialog
      v-model="targetPickerOpen"
      :api="props.api"
      :plugin-id="props.pluginId"
      :current="selectedHistory"
      :clears-search-state="hasSearchState"
      @select="applyTarget"
    />

    <VDialog
      v-model="targetChangeConfirmOpen"
      max-width="30rem"
      retain-focus
      aria-labelledby="target-change-confirm-title"
      @after-leave="restoreTargetChangeFocus"
    >
      <VCard>
        <VCardTitle id="target-change-confirm-title">确认更换目标</VCardTitle>
        <VCardText>更换目标将清空当前搜索结果和自定义关键词。</VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="targetChangeConfirmOpen = false">取消</VBtn>
          <VBtn color="primary" variant="flat" @click="confirmTargetChange">确认更换</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>
  </section>
</template>

<style scoped>
.view-shell { min-width: 0; }
.view-header { margin-bottom: 1rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.9375rem; font-weight: 650; letter-spacing: 0; }
.view-header p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.stale-alert, .search-feedback, .session-expired-feedback { margin-bottom: 0.75rem; }
.session-expired-feedback__content { display: flex; align-items: center; justify-content: space-between; gap: 0.75rem; }
.execution-overview { display: flex; flex-direction: column; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.5rem; background: rgba(var(--v-theme-surface), 0.55); }
.execution-overview--empty { border-style: dashed; background: rgba(var(--v-theme-surface), 0.35); }
.target-empty { display: flex; align-items: center; gap: 1rem; padding: 1.5rem 1.25rem; }
.target-empty__badge { display: inline-flex; width: 2.75rem; height: 2.75rem; flex: 0 0 auto; align-items: center; justify-content: center; border-radius: 0.5rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); background: rgba(var(--v-theme-on-surface), 0.06); }
.target-empty__copy { min-width: 0; flex: 1 1 auto; }
.target-empty__copy h3 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.9375rem; font-weight: 650; }
.target-empty__copy p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; line-height: 1.5; }
@media (max-width: 800px) {
  .view-header p { display: none; }
  .session-expired-feedback__content { align-items: flex-start; flex-direction: column; }
  .target-empty { align-items: flex-start; flex-direction: column; gap: 0.75rem; padding: 1.15rem; }
}
</style>
