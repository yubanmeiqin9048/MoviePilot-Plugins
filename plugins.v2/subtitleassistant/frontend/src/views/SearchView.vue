<script setup lang="ts">
import { computed, inject, ref } from 'vue'

import { downloadCandidate, getErrorMessage, searchSubtitles } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import StateChip from '@/components/StateChip.vue'
import TargetPickerDialog from '@/components/TargetPickerDialog.vue'
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
const pickerOpen = ref(false)
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
const searchButtonLabel = computed(() => {
  if (!response.value) return '搜索字幕'
  return resultsStale.value ? '按新条件搜索' : '重新搜索'
})

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

function applyTarget(next: TargetItem): void {
  if (target.value?.history_id === next.history_id) return
  searchRequestId += 1
  loading.value = false
  target.value = next
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
        <p>选定整理历史目标后跨来源搜索，并在统一候选流中判断与下载。</p>
      </div>
      <VBtn
        v-if="target"
        class="header-search-button"
        color="primary"
        prepend-icon="mdi-magnify"
        :loading="loading"
        @click="search"
      >
        {{ searchButtonLabel }}
      </VBtn>
    </header>

    <VAlert v-if="resultsStale" type="warning" variant="tonal" density="compact" class="search-feedback">
      搜索条件已变化。当前候选仍来自上一次搜索；重新搜索后才能下载。
    </VAlert>

    <section class="target-panel" :class="{ 'target-panel--empty': !target }" aria-labelledby="target-panel-title">
      <h3 id="target-panel-title" class="sr-only">搜索目标</h3>

      <div v-if="!target" class="target-empty">
        <span class="target-badge target-badge--empty" aria-hidden="true"><VIcon icon="mdi-crosshairs-question" size="26" /></span>
        <div class="target-empty-copy">
          <strong>先确定要补字幕的目标</strong>
          <span>从 MoviePilot 整理历史中选择一条记录，插件会据此生成各来源的默认查询。</span>
        </div>
        <VBtn color="primary" prepend-icon="mdi-format-list-bulleted" @click="pickerOpen = true">选择目标</VBtn>
      </div>

      <div v-else class="target-summary">
        <div class="target-summary-main">
          <span class="target-badge" aria-hidden="true">
            <VIcon :icon="target.media_type === 'movie' ? 'mdi-movie-outline' : 'mdi-television-classic'" size="24" />
          </span>
          <div class="target-identity">
            <strong>{{ mediaLabel(target.media_title, target.year, target.season, target.episode) }}</strong>
            <span class="target-meta">
              <VChip size="x-small" variant="tonal" label>{{ mediaTypeLabels[target.media_type] }}</VChip>
              <em :title="target.target_file_name">{{ target.target_file_name }}</em>
            </span>
            <small class="target-path target-path--full">{{ fullPath(target.target_path) }}</small>
            <small class="target-path target-path--short">{{ shortPath(target.target_path) }}</small>
          </div>
        </div>
        <div class="target-actions">
          <VBtn variant="text" size="small" prepend-icon="mdi-swap-horizontal" @click="pickerOpen = true">更换目标</VBtn>
        </div>
      </div>

      <VExpansionPanels v-if="target" v-model="keywordPanel" multiple variant="accordion" class="keyword-panel">
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

      <div v-if="target" class="source-strip" aria-label="本次来源执行结果">
        <div v-if="loading" class="source-placeholder" role="status">
          <VProgressCircular indeterminate color="primary" size="16" width="2" />
          <span>正在查询全部可用字幕源…</span>
        </div>
        <template v-else-if="groups.length">
          <article v-for="group in groups" :key="group.source" class="source-cell">
            <div class="source-cell-head">
              <strong>{{ sourceLabels[group.source] }}</strong>
              <StateChip :state="sourceGroupState(group)" size="x-small" />
            </div>
            <p class="source-cell-metric">{{ group.candidate_count }} 个候选 · {{ formatDuration(group.duration_ms) }}</p>
            <small :class="{ 'error-text': group.status === 'error' }" :title="searchExecutionSummary(group)">{{ searchExecutionSummary(group) }}</small>
          </article>
        </template>
        <div v-else class="source-placeholder" role="status">
          <VIcon icon="mdi-source-branch" size="18" aria-hidden="true" />
          <span>搜索后在这里查看 MoviePilot、OpenSubtitles 和 ASSRT 的执行结论。</span>
        </div>
      </div>
    </section>

    <VAlert v-if="searchError" type="error" variant="tonal" density="compact" class="search-feedback">
      {{ searchError }}
    </VAlert>
    <VAlert v-if="notice" type="info" variant="tonal" density="compact" closable class="search-feedback" @click:close="notice = ''">
      {{ notice }}
    </VAlert>

    <section class="candidate-results" aria-labelledby="candidate-results-title">
      <div class="results-heading">
        <div class="results-title">
          <h3 id="candidate-results-title">候选结果</h3>
          <p v-if="orderedCandidates.length">显示 {{ visibleCandidates.length }} / {{ orderedCandidates.length }} 条 · 已识别候选排列在前</p>
          <p v-else-if="response">本次搜索没有可下载候选</p>
          <p v-else>跨来源候选将在这里统一展示</p>
        </div>
        <div v-if="orderedCandidates.length" class="results-filters">
          <VBtnToggle v-model="recognitionFilter" mandatory color="primary" variant="outlined" density="compact" class="recognition-filter" aria-label="按识别状态筛选候选">
            <VBtn value="all">全部 {{ orderedCandidates.length }}</VBtn>
            <VBtn value="recognized">已识别 {{ recognizedCandidates.length }}</VBtn>
            <VBtn value="unrecognized">未识别 {{ unrecognizedCandidates.length }}</VBtn>
          </VBtnToggle>
          <VSelect v-model="sourceFilter" class="source-filter" label="字幕源" :items="sourceFilterItems" variant="outlined" density="compact" hide-details />
        </div>
      </div>

      <VSkeletonLoader v-if="loading" type="table-heading, table-row-divider@6" class="results-loading" />
      <EmptyState v-else-if="!target" icon="mdi-crosshairs-question" title="等待选择目标" message="先在上方选择整理历史目标，插件会据此生成各来源的默认查询。" />
      <EmptyState v-else-if="!response" icon="mdi-text-search" title="等待搜索" message="按当前目标和关键词开始搜索，结果将按固定来源顺序汇总。">
        <template #actions><VBtn color="primary" variant="tonal" prepend-icon="mdi-magnify" :loading="loading" @click="search">搜索字幕</VBtn></template>
      </EmptyState>
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
                <div class="candidate-action-inner">
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
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <TargetPickerDialog
      v-model="pickerOpen"
      :api="props.api"
      :plugin-id="props.pluginId"
      :current="target"
      @select="applyTarget"
    />
  </section>
</template>

<style scoped>
.search-view {
  --panel-border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  --panel-surface: rgba(var(--v-theme-surface), 0.55);
  --panel-radius: 0.5rem;
  --muted: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  min-width: 0;
}
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 1rem; font-weight: 650; letter-spacing: 0; }
.view-header p { margin: 0.25rem 0 0; color: var(--muted); font-size: 0.8125rem; line-height: 1.5; }
.header-search-button { flex: 0 0 auto; }
.search-feedback { margin-bottom: 0.75rem; }
.target-panel { overflow: hidden; border: var(--panel-border); border-radius: var(--panel-radius); background: var(--panel-surface); }
.target-panel--empty { border-style: dashed; background: rgba(var(--v-theme-surface), 0.35); }
.target-empty { display: flex; align-items: center; gap: 1rem; padding: 1.5rem 1.25rem; }
.target-empty-copy { display: grid; min-width: 0; flex: 1 1 auto; gap: 0.2rem; }
.target-empty-copy strong { font-size: 0.9375rem; font-weight: 650; }
.target-empty-copy span { color: var(--muted); font-size: 0.8125rem; line-height: 1.5; }
.target-summary { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 0.75rem; padding: 1rem 1.15rem; }
.target-summary-main { display: flex; min-width: 0; flex: 1 1 auto; align-items: center; gap: 0.85rem; }
.target-badge { display: inline-flex; width: 2.75rem; height: 2.75rem; flex: 0 0 auto; align-items: center; justify-content: center; border-radius: 0.5rem; color: rgb(var(--v-theme-primary)); background: rgba(var(--v-theme-primary), 0.12); }
.target-badge--empty { color: var(--muted); background: rgba(var(--v-theme-on-surface), 0.06); }
.target-identity { display: grid; min-width: 0; flex: 1 1 auto; gap: 0.25rem; }
.target-identity strong { min-width: 0; overflow: hidden; font-size: 0.9375rem; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.target-meta { display: flex; min-width: 0; align-items: center; gap: 0.4rem; }
.target-meta em { min-width: 0; overflow: hidden; color: var(--muted); font-size: 0.75rem; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.target-path { color: var(--muted); font-size: 0.7rem; line-height: 1.45; }
.target-path--full { overflow-wrap: anywhere; }
.target-path--short { display: none; }
.target-actions { display: flex; flex: 0 0 auto; align-items: center; gap: 0.35rem; }
.keyword-panel { border-top: var(--panel-border); border-radius: 0; }
.keyword-panel :deep(.v-expansion-panel) { background: transparent; }
.keyword-panel :deep(.v-expansion-panel-title) { min-height: 2.85rem; padding: 0.6rem 1.15rem; }
.keyword-panel :deep(.v-expansion-panel-text__wrapper) { padding: 0 1.15rem 0.9rem; }
.keyword-panel-title { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 1rem; padding-right: 0.5rem; }
.keyword-panel-title span { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.8125rem; font-weight: 650; }
.keyword-panel-title small, .control-note { color: var(--muted); font-size: 0.6875rem; }
.control-note { margin: 0 0 0.75rem; line-height: 1.5; }
.keyword-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }
.keyword-block { min-width: 0; }
.default-plan { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; margin-top: 0.4rem; color: var(--muted); font-size: 0.6875rem; line-height: 1.4; }
.default-plan--editable { cursor: pointer; }
.source-strip { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: var(--panel-border); background: rgba(var(--v-theme-on-surface), 0.02); }
.source-cell { display: grid; min-width: 0; align-content: start; gap: 0.3rem; padding: 0.75rem 1.15rem; }
.source-cell + .source-cell { border-left: var(--panel-border); }
.source-cell-head { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 0.5rem; }
.source-cell-head strong { min-width: 0; overflow: hidden; font-size: 0.75rem; font-weight: 650; text-overflow: ellipsis; white-space: nowrap; }
.source-cell-metric { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.75rem; font-variant-numeric: tabular-nums; }
.source-cell small { min-width: 0; overflow: hidden; color: var(--muted); font-size: 0.6875rem; text-overflow: ellipsis; white-space: nowrap; }
.source-placeholder { display: flex; min-height: 3.25rem; align-items: center; justify-content: center; gap: 0.5rem; grid-column: 1 / -1; padding: 0.75rem 1.15rem; color: var(--muted); font-size: 0.75rem; text-align: center; }
.error-text { color: rgb(var(--v-theme-error)) !important; }
.candidate-results { margin-top: 0.75rem; border: var(--panel-border); border-radius: var(--panel-radius); background: var(--panel-surface); }
.results-heading { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.7rem 1.15rem; border-bottom: var(--panel-border); }
.results-title { min-width: 0; }
.results-title h3 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.875rem; font-weight: 650; letter-spacing: 0; }
.results-title p { margin: 0.2rem 0 0; color: var(--muted); font-size: 0.75rem; font-variant-numeric: tabular-nums; }
.results-filters { display: flex; flex: 0 0 auto; align-items: center; gap: 0.5rem; }
.recognition-filter :deep(.v-btn-toggle) { height: 2.5rem; }
.recognition-filter :deep(.v-btn) { min-width: 0; height: 2.5rem; padding-inline: 0.7rem; font-size: 0.75rem; letter-spacing: 0; }
.source-filter { width: 12rem; }
.results-loading { min-height: 20rem; }
.candidate-table-wrap { min-width: 0; }
.candidate-table { width: 100%; table-layout: fixed; border-collapse: separate; border-spacing: 0; font-size: 0.75rem; }
.candidate-table th {
  position: sticky;
  z-index: 2;
  inset-block-start: 0;
  padding: 0.6rem 0.9rem;
  border-bottom: var(--panel-border);
  color: var(--muted);
  font-size: 0.6875rem;
  font-weight: 650;
  text-align: left;
  background-color: rgb(var(--v-theme-surface));
  background-image: linear-gradient(rgba(var(--v-theme-on-surface), 0.04), rgba(var(--v-theme-on-surface), 0.04));
}
.candidate-table th:nth-child(1) { width: 6.25rem; }
.candidate-table th:nth-child(2) { width: 33%; }
.candidate-table th:nth-child(3) { width: 10%; }
.candidate-table th:nth-child(4) { width: 12%; }
.candidate-table th:nth-child(5) { width: 16%; }
.candidate-table th:last-child { width: 9.5rem; }
.candidate-table td { padding: 0.8rem 0.9rem; border-bottom: var(--panel-border); vertical-align: top; overflow-wrap: anywhere; }
.candidate-table tbody tr:last-child td { border-bottom: 0; }
.candidate-table tbody tr:last-child td:first-child { border-end-start-radius: var(--panel-radius); }
.candidate-table tbody tr:last-child td:last-child { border-end-end-radius: var(--panel-radius); }
.candidate-table tbody tr { transition: background-color 160ms ease; }
.candidate-table tbody tr:hover { background: rgba(var(--v-theme-primary), 0.05); }
.candidate-table .candidate-value, .candidate-table .candidate-secondary, .candidate-table .candidate-note { display: block; }
.candidate-table td strong { color: rgb(var(--v-theme-on-surface)); font-size: 0.75rem; font-weight: 650; }
.candidate-table .candidate-secondary, .candidate-table .candidate-note { margin-top: 0.18rem; color: var(--muted); font-size: 0.6875rem; line-height: 1.45; }
.candidate-name strong { font-size: 0.8125rem; line-height: 1.4; }
.candidate-action-inner { display: grid; gap: 0.45rem; }
.candidate-warning { display: flex; align-items: flex-start; gap: 0.25rem; margin: 0; color: var(--muted); font-size: 0.6875rem; line-height: 1.4; }
.download-error { margin: 0; font-size: 0.6875rem; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 75rem) {
  .results-heading { align-items: stretch; flex-direction: column; gap: 0.6rem; }
  .results-filters { justify-content: space-between; }
  .recognition-filter { flex: 1 1 auto; }
  .recognition-filter :deep(.v-btn-toggle) { display: flex; width: 100%; }
  .recognition-filter :deep(.v-btn) { flex: 1 1 0; }
}
@media (max-width: 59.99rem) {
  .source-strip { grid-template-columns: 1fr; }
  .source-cell + .source-cell { border-top: var(--panel-border); border-left: 0; }
  .source-cell small { white-space: normal; }
}
@media (max-width: 50rem) {
  .view-header p { display: none; }
  .keyword-grid { grid-template-columns: 1fr; }
  .target-empty { align-items: flex-start; flex-direction: column; gap: 0.75rem; padding: 1.15rem; }
  .target-summary { align-items: stretch; flex-direction: column; gap: 0.6rem; padding: 0.9rem 1rem; }
  .target-summary-main { align-items: flex-start; }
  .target-path--full { display: none; }
  .target-path--short { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .target-actions { justify-content: flex-end; }
  .results-filters { align-items: stretch; flex-direction: column; }
  .source-filter { width: 100%; }
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
    border: var(--panel-border);
    border-radius: 0.45rem;
    background: rgba(var(--v-theme-surface), 0.58);
  }
  .candidate-table tbody tr:hover { background: rgba(var(--v-theme-surface), 0.58); }
  .candidate-table td, .candidate-table tbody tr:last-child td { display: block; min-width: 0; padding: 0; border: 0; }
  .candidate-table td::before { display: block; margin-bottom: 0.22rem; color: var(--muted); content: attr(data-label); font-size: 0.625rem; line-height: 1.2; }
  .candidate-table .candidate-recognition { display: flex; grid-column: 1 / -1; align-items: center; justify-content: space-between; padding-bottom: 0.7rem; border-bottom: var(--panel-border); }
  .candidate-table .candidate-recognition::before { margin: 0; }
  .candidate-table .candidate-recognition :deep([class~="v-chip"]) { display: inline-flex; width: auto; max-width: 100%; }
  .candidate-table .candidate-name { grid-column: 1 / -1; padding-top: 0.05rem; }
  .candidate-table .candidate-name::before { margin-bottom: 0.35rem; }
  .candidate-table .candidate-source,
  .candidate-table .candidate-range,
  .candidate-table .candidate-language { min-width: 0; }
  .candidate-table .candidate-action { grid-column: 1 / -1; margin-top: 0.05rem; padding-top: 0.7rem; border-top: var(--panel-border); }
  .candidate-table .candidate-action::before { display: none; }  .candidate-download-button { width: 100%; min-height: 2.75rem; }
}
@media (max-width: 26rem) {
  .view-header { align-items: stretch; flex-direction: column; gap: 0.6rem; }
  .header-search-button { width: 100%; }
  .keyword-panel-title { align-items: flex-start; flex-direction: column; gap: 0.2rem; }
}
@media (prefers-reduced-motion: reduce) {
  .search-view *, .search-view *::before, .search-view *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
