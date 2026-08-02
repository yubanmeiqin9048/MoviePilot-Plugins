<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'

import { downloadCandidate, getErrorMessage, searchSubtitles } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import StateChip from '@/components/StateChip.vue'
import TargetSelector from '@/components/TargetSelector.vue'
import type {
  HostToast,
  PluginApi,
  SearchResponse,
  SearchSourceGroup,
  SubtitleCandidate,
  SubtitleSource,
  TargetItem,
} from '@/types'
import {
  formatDate,
  formatDuration,
  fullPath,
  mediaLabel,
  mediaTypeLabels,
  packageLabels,
  shortPath,
  sourceLabels,
  translationLabels,
} from '@/types/presentation'
import type { StatePresentation } from '@/types/presentation'

type RecognitionFilter = 'all' | 'recognized' | 'unrecognized'
type SourceFilter = 'all' | SubtitleSource

const props = defineProps<{ api: PluginApi; pluginId: string; active: boolean }>()
const emit = defineEmits<{ action: [] }>()
const toast = inject<HostToast | null>('moviepilot:toast', null)

const sourceOrder: SubtitleSource[] = ['moviepilot', 'opensubtitles', 'assrt']
const target = ref<TargetItem | null>(null)
const keywords = ref<Record<SubtitleSource, string>>({ moviepilot: '', opensubtitles: '', assrt: '' })
const response = ref<SearchResponse | null>(null)
const searchedConditions = ref('')
const recognitionFilter = ref<RecognitionFilter>('all')
const sourceFilter = ref<SourceFilter>('all')
const loading = ref(false)
const loadingCandidates = ref<Record<string, boolean>>({})
const searchError = ref('')
const downloadErrors = ref<Record<string, string>>({})
const notice = ref('')
const keywordPanel = ref<number[]>([])
let searchRequestId = 0

const groups = computed<SearchSourceGroup[]>(() => sourceOrder
  .map(source => response.value?.sources.find(item => item.source === source))
  .filter((group): group is SearchSourceGroup => Boolean(group)))
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
const sourceFilterItems = computed(() => [
  { title: `全部来源 · ${orderedCandidates.value.length}`, value: 'all' },
  ...groups.value.map(group => ({ title: `${sourceLabels[group.source]} · ${group.candidate_count}`, value: group.source })),
])
const currentConditions = computed(() => JSON.stringify({
  target: target.value?.history_id || null,
  keywords: sourceOrder.map(source => cleanKeyword(keywords.value[source])),
}))
const resultsStale = computed(() => Boolean(response.value && searchedConditions.value !== currentConditions.value))

async function search(): Promise<void> {
  if (!target.value) {
    searchError.value = '请先选择整理历史目标，再开始搜索。'
    return
  }
  const requestId = ++searchRequestId
  const requestConditions = currentConditions.value
  loading.value = true
  searchError.value = ''
  notice.value = ''
  downloadErrors.value = {}
  response.value = null
  recognitionFilter.value = 'all'
  sourceFilter.value = 'all'
  try {
    const searchResponse = await searchSubtitles(props.api, props.pluginId, {
      target_history_id: target.value.history_id,
      moviepilot_keyword: cleanKeyword(keywords.value.moviepilot),
      opensubtitles_keyword: cleanKeyword(keywords.value.opensubtitles),
      assrt_keyword: cleanKeyword(keywords.value.assrt),
    })
    if (requestId !== searchRequestId) return
    response.value = searchResponse
    searchedConditions.value = requestConditions
    if (!response.value.session_id) notice.value = '本次没有可下载候选，请调整关键词或检查字幕源状态。'
  } catch (requestError) {
    if (requestId === searchRequestId) searchError.value = getErrorMessage(requestError, '字幕搜索失败')
  } finally {
    if (requestId === searchRequestId) loading.value = false
  }
}

async function download(candidate: SubtitleCandidate): Promise<void> {
  if (!response.value?.session_id || resultsStale.value) return
  const candidateKey = candidate.candidate_key
  loadingCandidates.value = { ...loadingCandidates.value, [candidateKey]: true }
  downloadErrors.value = { ...downloadErrors.value, [candidateKey]: '' }
  try {
    const result = await downloadCandidate(props.api, props.pluginId, response.value.session_id, candidateKey)
    if (result.reused) toast?.info('该候选已有处理中任务，已复用原任务')
    else toast?.success('已加入下载队列')
    emit('action')
  } catch (requestError) {
    const message = getErrorMessage(requestError, '字幕下载任务提交失败')
    downloadErrors.value = { ...downloadErrors.value, [candidateKey]: message }
    toast?.error(message)
  } finally {
    const nextLoadingCandidates = { ...loadingCandidates.value }
    delete nextLoadingCandidates[candidateKey]
    loadingCandidates.value = nextLoadingCandidates
  }
}

function changeTarget(): void {
  searchRequestId += 1
  loading.value = false
  target.value = null
  response.value = null
  searchedConditions.value = ''
  recognitionFilter.value = 'all'
  sourceFilter.value = 'all'
  searchError.value = ''
  notice.value = ''
  downloadErrors.value = {}
}

function cleanKeyword(value: string | null | undefined): string | null {
  const trimmed = (value || '').trim()
  return trimmed || null
}

function setKeyword(source: SubtitleSource, value: string | null): void {
  keywords.value[source] = value || ''
}

function plansForSource(source: SubtitleSource) {
  return response.value?.sources.find(item => item.source === source)?.default_plans
    || target.value?.search_plans?.[source]
    || []
}

function planSummary(group: SearchSourceGroup): string {
  return group.default_plans.map(plan => plan.query || plan.label).join(' → ')
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

function searchExecutionSummary(group: SearchSourceGroup): string {
  const details = group.details || {}
  const parts: string[] = []
  if (group.executed_queries.length) parts.push(`查询：${group.executed_queries.join(' → ')}`)
  else if (planSummary(group)) parts.push(`默认：${planSummary(group)}`)
  if (group.matched_query) parts.push(`命中：${group.matched_query}`)
  if (details.cache_hit === true) {
    parts.push(details.cache_stored_at ? `缓存：${formatDate(String(details.cache_stored_at))}` : '复用缓存')
  } else if (details.cache_hit === false) {
    parts.push('已查询字幕源')
  }
  const pageCount = typeof details.page_count === 'number' ? details.page_count : null
  if (pageCount && pageCount > 1) parts.push(`${pageCount} 页`)
  if (details.pagination_complete === false) parts.push('分页不完整')
  if (group.error_summary) parts.push(group.error_summary)
  return parts.join(' · ') || '没有可展示的查询摘要'
}

function candidateTargetMismatch(candidate: SubtitleCandidate): boolean {
  if (!target.value || target.value.media_type !== 'tv') return false
  const seasons = candidate.seasons.length ? candidate.seasons : (candidate.season == null ? [] : [candidate.season])
  const episodes = candidate.episodes.length ? candidate.episodes : (candidate.episode == null ? [] : [candidate.episode])
  const seasonMismatch = seasons.length > 0 && target.value.season != null && !seasons.includes(target.value.season)
  const episodeMismatch = episodes.length > 0 && target.value.episode != null && !episodes.includes(target.value.episode)
  return seasonMismatch || episodeMismatch
}

function sourceGroupState(group: SearchSourceGroup): StatePresentation {
  if (group.status === 'success' && group.candidate_count > 0) return { label: '已返回', icon: 'mdi-check-circle-outline', color: 'success' }
  if (group.status === 'success') return { label: '无结果', icon: 'mdi-file-search-outline', color: 'default' }
  if (group.status === 'limited') return { label: '受限', icon: 'mdi-timer-alert-outline', color: 'warning' }
  if (group.status === 'disabled') return { label: '已禁用', icon: 'mdi-minus-circle-outline', color: 'default' }
  if (group.status === 'unconfigured') return { label: '未配置', icon: 'mdi-cog-off-outline', color: 'default' }
  return { label: '异常', icon: 'mdi-alert-circle-outline', color: 'error' }
}

function candidateRecognitionState(candidate: SubtitleCandidate): StatePresentation {
  return candidate.recognition_status === 'recognized'
    ? { label: '已识别', icon: 'mdi-check-circle-outline', color: 'success' }
    : { label: '未识别', icon: 'mdi-help-circle-outline', color: 'warning' }
}

function sourceDetails(candidate: SubtitleCandidate): string {
  const labels: Record<string, string> = {
    site_name: '站点',
    site_priority: '站点优先级',
    description: '描述',
    trusted: '可信发布者',
    release: '发行信息',
    media_id: '来源媒体 ID',
    videoname: '视频名称',
    native_name: '原生字幕名',
    revision: '修订版',
  }
  return Object.entries(candidate.source_details || {})
    .filter(([key, value]) => key in labels && value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${labels[key]}：${String(value)}`)
    .join(' · ')
}

function copyPlan(source: SubtitleSource, query: string | null, editable: boolean): void {
  if (editable && query) keywords.value[source] = query
}
</script>

<template>
  <section class="view-shell search-view" aria-labelledby="search-view-title">
    <header class="view-header">
      <div>
        <h2 id="search-view-title">字幕搜索</h2>
        <p>{{ target ? '确认来源执行结果，在统一候选流中判断并下载。' : '先从 MoviePilot 整理历史中确认要补字幕的目标。' }}</p>
      </div>
    </header>

    <section v-if="!target" class="target-discovery" aria-labelledby="target-picker-title">
      <div class="section-heading">
        <div>
          <h3 id="target-picker-title">选择整理历史目标</h3>
          <p>按媒体名称、目标文件或路径查找最近整理记录。</p>
        </div>
      </div>
      <TargetSelector v-model="target" :api="props.api" :plugin-id="props.pluginId" />
      <VAlert v-if="searchError" type="error" variant="tonal" density="compact" class="search-feedback">
        {{ searchError }}
      </VAlert>
    </section>

    <template v-else>
      <VAlert v-if="resultsStale" type="warning" variant="tonal" density="compact" class="stale-alert">
        搜索条件已变化。当前候选仍来自上一次搜索；重新搜索后才能下载。
      </VAlert>

      <section class="execution-overview" aria-label="搜索执行总览">
        <div class="target-overview">
          <div class="target-overview-main">
            <VIcon :icon="target.media_type === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" size="24" aria-hidden="true" />
            <div>
              <strong>{{ mediaLabel(target.media_title, target.year, target.season, target.episode) }}</strong>
              <span>{{ mediaTypeLabels[target.media_type] }} · {{ target.target_file_name }}</span>
              <small class="target-path target-path--full">{{ fullPath(target.target_path) }}</small>
              <small class="target-path target-path--short">{{ shortPath(target.target_path) }}</small>
            </div>
          </div>
          <div class="target-actions">
            <VBtn variant="text" prepend-icon="mdi-swap-horizontal" @click="changeTarget">更换目标</VBtn>
            <VBtn class="target-search-button" color="primary" prepend-icon="mdi-magnify" :loading="loading" @click="search">
              {{ response ? (resultsStale ? '按新条件搜索' : '重新搜索') : '搜索字幕' }}
            </VBtn>
          </div>
        </div>

        <VExpansionPanels v-model="keywordPanel" multiple variant="accordion" class="keyword-panel">
          <VExpansionPanel>
            <VExpansionPanelTitle>
              <div class="keyword-panel-title">
                <span><VIcon icon="mdi-tune-variant" size="18" />来源关键词</span>
                <small>可选，留空使用默认查询</small>
              </div>
            </VExpansionPanelTitle>
            <VExpansionPanelText>
              <p class="control-note">填写后只覆盖对应来源的搜索文本，不改变其他来源的默认查询计划。</p>
              <div class="keyword-grid">
                <div v-for="source in sourceOrder" :key="source" class="keyword-block">
                  <VTextField
                    :model-value="keywords[source]"
                    :label="sourceLabels[source]"
                    placeholder="可选，自定义搜索词"
                    clearable
                    hide-details="auto"
                    density="compact"
                    @update:model-value="setKeyword(source, $event)"
                  />
                  <div v-if="plansForSource(source).length" class="default-plan">
                    <span>默认</span>
                    <VChip
                      v-for="(plan, index) in plansForSource(source)"
                      :key="`${source}-${plan.kind}-${index}`"
                      size="x-small"
                      variant="tonal"
                      label
                      :class="{ 'default-plan--editable': plan.editable }"
                      :clickable="plan.editable"
                      @click="copyPlan(source, plan.query, plan.editable)"
                    >
                      {{ plan.label }}{{ plan.query ? ` · ${plan.query}` : '' }}
                    </VChip>
                  </div>
                </div>
              </div>
            </VExpansionPanelText>
          </VExpansionPanel>
        </VExpansionPanels>

        <section class="source-execution" aria-labelledby="source-execution-title">
          <div class="source-execution-heading">
            <div>
              <h3 id="source-execution-title">本次来源</h3>
              <p>只读执行结论，不影响候选筛选。</p>
            </div>
          </div>
          <div v-if="loading" class="source-placeholder" role="status">
            <VProgressCircular indeterminate color="primary" size="18" width="2" />
            <span>正在查询全部可用字幕源…</span>
          </div>
          <div v-else-if="groups.length" class="execution-list">
            <article v-for="group in groups" :key="group.source" class="execution-row">
              <div class="execution-source">
                <StateChip :state="sourceGroupState(group)" size="x-small" />
                <strong>{{ sourceLabels[group.source] }}</strong>
              </div>
              <span>{{ group.candidate_count }} 个候选 · {{ formatDuration(group.duration_ms) }}</span>
              <small :class="{ 'error-text': group.status === 'error' }">{{ searchExecutionSummary(group) }}</small>
            </article>
          </div>
          <div v-else class="source-placeholder" role="status">
            <VIcon icon="mdi-source-branch" size="20" aria-hidden="true" />
            <span>搜索后在这里查看 MoviePilot、OpenSubtitles 和 ASSRT 的执行结论。</span>
          </div>
        </section>
      </section>

      <VAlert v-if="searchError" type="error" variant="tonal" density="compact" class="search-feedback">
        {{ searchError }}
      </VAlert>
      <VAlert v-if="notice" type="info" variant="tonal" density="compact" closable class="search-feedback" @click:close="notice = ''">
        {{ notice }}
      </VAlert>

      <section class="candidate-filter-band" aria-labelledby="candidate-filter-title">
        <div class="candidate-filter-heading">
          <h3 id="candidate-filter-title">筛选候选</h3>
          <span>当前显示 {{ visibleCandidates.length }} / {{ orderedCandidates.length }}</span>
        </div>
        <div class="filter-controls">
          <div class="recognition-filter">
            <span>识别状态</span>
            <VBtnToggle v-model="recognitionFilter" mandatory color="primary" variant="outlined" density="compact" aria-label="按识别状态筛选候选">
              <VBtn value="all">全部 {{ orderedCandidates.length }}</VBtn>
              <VBtn value="recognized">已识别 {{ recognizedCandidates.length }}</VBtn>
              <VBtn value="unrecognized">未识别 {{ unrecognizedCandidates.length }}</VBtn>
            </VBtnToggle>
          </div>
          <VSelect v-model="sourceFilter" class="source-filter" label="字幕源" :items="sourceFilterItems" density="compact" hide-details />
        </div>
      </section>

      <section class="candidate-results" aria-labelledby="candidate-results-title">
        <div class="results-heading">
          <div>
            <h3 id="candidate-results-title">候选结果</h3>
            <p v-if="response">已识别候选稳定排列在前；未识别候选仍可下载。</p>
            <p v-else>跨来源候选将在这里统一展示。</p>
          </div>
        </div>

        <VSkeletonLoader v-if="loading" type="list-item-three-line@6" class="results-loading" />
        <EmptyState v-else-if="!response" icon="mdi-text-search" title="等待搜索" message="确认目标后开始搜索，结果将按固定来源顺序汇总。" />
        <EmptyState v-else-if="!orderedCandidates.length" icon="mdi-file-search-outline" title="没有候选结果" message="本次所有来源均未返回可下载字幕，请查看来源状态或调整搜索关键词。" />
        <EmptyState v-else-if="!visibleCandidates.length" icon="mdi-filter-off-outline" title="当前筛选没有候选" message="切换筛选条件可继续查看本次搜索的全部结果。" />
        <div v-else class="candidate-table-wrap" aria-live="polite">
          <table class="candidate-table">
            <thead>
              <tr>
                <th>识别</th>
                <th>候选</th>
                <th>来源</th>
                <th>范围</th>
                <th>语言 / 格式</th>
                <th><span class="sr-only">操作</span></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="candidate in visibleCandidates" :key="candidate.candidate_key">
                <td data-label="识别状态" class="candidate-recognition"><StateChip :state="candidateRecognitionState(candidate)" size="x-small" /></td>
                <td data-label="候选" class="candidate-name">
                  <strong class="candidate-value">{{ candidate.name }}</strong>
                  <span class="candidate-secondary">{{ candidate.file_name || '文件名未提供' }}</span>
                  <small class="candidate-note">{{ sourceDetails(candidate) || (candidate.query ? `查询：${candidate.query}` : '无额外来源摘要') }}</small>
                </td>
                <td data-label="来源" class="candidate-source"><strong class="candidate-value">{{ sourceLabels[candidate.source] }}</strong></td>
                <td data-label="范围" class="candidate-range">
                  <strong class="candidate-value">{{ candidateRange(candidate) }}</strong>
                  <small class="candidate-note">{{ packageLabels[candidate.package_scope] }}</small>
                </td>
                <td data-label="语言 / 格式" class="candidate-language">
                  <strong class="candidate-value">{{ candidate.language || '未标记' }}</strong>
                  <small class="candidate-note">{{ candidate.format && candidate.format.toUpperCase() !== 'UNKNOWN' ? candidate.format.toUpperCase() : '未标记' }} · {{ translationLabels[candidate.translation_type] }}{{ candidate.hearing_impaired ? ' · SDH/CC' : '' }}</small>
                </td>
                <td data-label="操作" class="candidate-action">
                  <p v-if="candidateTargetMismatch(candidate)" class="candidate-warning">
                    <VIcon icon="mdi-alert-outline" size="15" color="warning" aria-hidden="true" />与当前目标集不同
                  </p>
                  <VAlert v-if="downloadErrors[candidate.candidate_key]" type="error" variant="tonal" density="compact" class="download-error">
                    {{ downloadErrors[candidate.candidate_key] }}
                  </VAlert>
                  <VBtn
                    class="candidate-download-button"
                    color="primary"
                    variant="tonal"
                    size="small"
                    prepend-icon="mdi-download"
                    :loading="Boolean(loadingCandidates[candidate.candidate_key])"
                    :disabled="Boolean(loadingCandidates[candidate.candidate_key]) || resultsStale"
                    @click="download(candidate)"
                  >
                    下载
                  </VBtn>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.view-shell { min-width: 0; }
.view-header { margin-bottom: 1rem; }
.view-header h2, .section-heading h3, .source-execution-heading h3, .candidate-filter-heading h3, .results-heading h3 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.9375rem; font-weight: 650; letter-spacing: 0; }
.view-header p, .section-heading p, .source-execution-heading p, .results-heading p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.target-discovery { width: min(100%, 54rem); margin: 2.5rem auto 0; padding: 1rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.55); }
.section-heading { margin-bottom: 1rem; }
.stale-alert, .search-feedback { margin-bottom: 0.75rem; }
.execution-overview { display: flex; flex-direction: column; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.55); }
.target-overview { display: flex; min-width: 0; flex-direction: row; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.75rem 1rem; padding: 0.9rem 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.target-overview-main { display: flex; min-width: 0; align-items: center; gap: 0.65rem; }
.target-overview-main > div { min-width: 0; }
.target-overview-main strong, .target-overview-main span, .target-overview-main small { display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.target-overview-main strong { font-size: 0.875rem; }
.target-overview-main span, .target-overview-main small { margin-top: 0.18rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.7rem; }
.target-overview-main .target-path--full { display: block; white-space: normal; overflow-wrap: anywhere; line-height: 1.45; }
.target-overview-main .target-path--short { display: none; }
.target-actions { display: flex; align-items: center; justify-content: flex-end; gap: 0.4rem; }
.target-search-button { width: fit-content; min-width: 0; padding-inline: 0.9rem; }
.keyword-panel { border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.keyword-panel :deep(.v-expansion-panel-title) { min-height: 3rem; padding: 0.65rem 0.8rem; }
.keyword-panel :deep(.v-expansion-panel-text__wrapper) { padding: 0 0.8rem 0.8rem; }
.keyword-panel-title { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 1rem; padding-right: 0.5rem; }
.keyword-panel-title span { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.8125rem; font-weight: 650; }
.keyword-panel-title small, .control-note { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; }
.control-note { margin: 0 0 0.75rem; line-height: 1.5; }
.keyword-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.65rem; }
.keyword-block { min-width: 0; }
.default-plan { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; margin-top: 0.35rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; line-height: 1.4; }
.default-plan--editable { cursor: pointer; }
.source-execution { min-width: 0; }
.source-execution-heading { display: flex; min-height: 2.85rem; align-items: center; padding: 0.45rem 0.8rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.source-placeholder { display: flex; min-height: 6.5rem; align-items: center; justify-content: center; gap: 0.5rem; padding: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; text-align: center; }
.execution-row { display: grid; min-height: 3.15rem; grid-template-columns: minmax(9rem, 0.7fr) 7rem minmax(0, 1fr); align-items: center; gap: 0.6rem; padding: 0.5rem 0.8rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.execution-row:first-child { border-top: 0; }
.execution-source { display: inline-flex; min-width: 0; align-items: center; gap: 0.4rem; }
.execution-source strong, .execution-row > span { font-size: 0.75rem; }
.execution-row > span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
.execution-row small { min-width: 0; overflow: hidden; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; text-overflow: ellipsis; white-space: nowrap; }
.error-text { color: rgb(var(--v-theme-error)) !important; }
.candidate-filter-band { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-top: 0.75rem; padding: 0.7rem 0.8rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.55); }
.candidate-filter-heading span { display: block; margin-top: 0.18rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; }
.filter-controls { display: grid; width: min(100%, 33rem); grid-template-columns: minmax(19rem, 1fr) minmax(10rem, 0.55fr); align-items: end; gap: 0.5rem; }
.recognition-filter { display: grid; gap: 0.2rem; }
.recognition-filter > span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; }
.recognition-filter :deep(.v-btn-toggle) { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
.recognition-filter :deep(.v-btn) { min-width: 0; letter-spacing: 0; }
.candidate-results { margin-top: 0.75rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: rgba(var(--v-theme-surface), 0.55); }
.results-heading { padding: 0.8rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.results-loading { min-height: 20rem; }
.candidate-table-wrap { min-width: 0; }
.candidate-table { width: 100%; table-layout: fixed; border-collapse: collapse; font-size: 0.75rem; }
.candidate-table th { padding: 0.55rem 0.7rem; background: rgba(var(--v-theme-on-surface), 0.035); color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; font-weight: 650; text-align: left; }
.candidate-table th:nth-child(1) { width: 6.25rem; }
.candidate-table th:nth-child(2) { width: 33%; }
.candidate-table th:nth-child(3) { width: 10%; }
.candidate-table th:nth-child(4) { width: 12%; }
.candidate-table th:nth-child(5) { width: 16%; }
.candidate-table th:last-child { width: 9.5rem; }
.candidate-table td { padding: 0.7rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); vertical-align: top; overflow-wrap: anywhere; }
.candidate-table .candidate-value, .candidate-table .candidate-secondary, .candidate-table .candidate-note { display: block; }
.candidate-table td strong { color: rgb(var(--v-theme-on-surface)); font-size: 0.75rem; font-weight: 650; }
.candidate-table .candidate-secondary, .candidate-table .candidate-note { margin-top: 0.16rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; line-height: 1.4; }
.candidate-name strong { font-size: 0.8125rem; }
.candidate-action { display: grid; gap: 0.45rem; }
.candidate-warning { display: flex; align-items: flex-start; gap: 0.25rem; margin: 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; line-height: 1.4; }
.download-error { margin: 0; font-size: 0.6875rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 959px) {
  .filter-controls { width: min(100%, 28rem); grid-template-columns: minmax(16rem, 1fr) minmax(9rem, 0.55fr); }
}
@media (max-width: 800px) {
  .view-header p { display: none; }
  .target-discovery { margin-top: 1rem; padding: 0.75rem; }
  .target-overview { flex-direction: column; align-items: stretch; gap: 0.6rem; }
  .target-overview-main { align-items: flex-start; }
  .target-overview-main .target-path--full { display: none; }
  .target-overview-main .target-path--short { display: block; }
  .target-actions { justify-content: stretch; }
  .target-search-button { flex: 1; }
  .keyword-grid { grid-template-columns: 1fr; }
  .candidate-filter-band { align-items: stretch; flex-direction: column; }
  .filter-controls { width: 100%; grid-template-columns: 1fr; }
  .recognition-filter :deep(.v-btn) { padding-inline: 0.3rem; font-size: 0.6875rem; }
  .candidate-table-wrap { padding: 0.7rem; }
  .candidate-table thead { display: none; }
  .candidate-table, .candidate-table tbody { display: block; width: 100%; }
  .candidate-table tbody { display: grid; gap: 0.7rem; }
  .candidate-table tr {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.7rem 0.9rem;
    padding: 0.9rem;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 0.45rem;
    background: rgba(var(--v-theme-surface), 0.58);
  }
  .candidate-table td { display: block; min-width: 0; padding: 0; border: 0; }
  .candidate-table td::before { display: block; margin-bottom: 0.22rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); content: attr(data-label); font-size: 0.625rem; line-height: 1.2; }
  .candidate-table .candidate-recognition { display: flex; grid-column: 1 / -1; align-items: center; justify-content: space-between; padding-bottom: 0.7rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
  .candidate-table .candidate-recognition::before { margin: 0; }
  .candidate-table .candidate-recognition :deep([class~="v-chip"]) { display: inline-flex; width: auto; max-width: 100%; }
  .candidate-table .candidate-name { grid-column: 1 / -1; padding-top: 0.05rem; }
  .candidate-table .candidate-name::before { margin-bottom: 0.35rem; }
  .candidate-table .candidate-source,
  .candidate-table .candidate-range,
  .candidate-table .candidate-language { min-width: 0; }
  .candidate-table .candidate-action { grid-column: 1 / -1; margin-top: 0.05rem; padding-top: 0.7rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
  .candidate-table .candidate-action::before { display: none; }
  .candidate-download-button { width: 100%; min-height: 2.75rem; }
}
@media (max-width: 420px) {
  .target-actions { align-items: stretch; flex-direction: column-reverse; }
  .target-actions :deep(.v-btn), .target-search-button { width: 100%; }
  .keyword-panel-title { align-items: flex-start; flex-direction: column; gap: 0.2rem; }
  .execution-row { grid-template-columns: minmax(0, 1fr) auto; }
  .execution-row small { grid-column: 1 / -1; white-space: normal; }
}
@media (prefers-reduced-motion: reduce) {
  .search-view *, .search-view *::before, .search-view *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
