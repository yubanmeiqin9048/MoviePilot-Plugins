<script setup lang="ts">
import { computed, inject, ref } from 'vue'

import { downloadCandidate, getErrorMessage, searchSubtitles } from '@/api/client'
import TargetSelector from '@/components/TargetSelector.vue'
import EmptyState from '@/components/EmptyState.vue'
import StateChip from '@/components/StateChip.vue'
import type { HostToast, PluginApi, SearchResponse, SearchSourceGroup, SubtitleCandidate, TargetItem, SubtitleSource } from '@/types'
import { formatDate, formatDuration, packageLabels, sourceLabels, translationLabels } from '@/types/presentation'
import type { StatePresentation } from '@/types/presentation'

const props = defineProps<{ api: PluginApi; pluginId: string; active: boolean }>()
const emit = defineEmits<{ action: [] }>()
const toast = inject<HostToast | null>('moviepilot:toast', null)

const target = ref<TargetItem | null>(null)
const keywords = ref<Record<SubtitleSource, string>>({ moviepilot: '', opensubtitles: '', assrt: '' })
const response = ref<SearchResponse | null>(null)
const searchedConditions = ref('')
const loading = ref(false)
const loadingCandidate = ref('')
const error = ref('')
const notice = ref('')
const openSources = ref<number[]>([])

const sourceOrder: SubtitleSource[] = ['moviepilot', 'opensubtitles', 'assrt']
const groups = computed<SearchSourceGroup[]>(() => sourceOrder.map(source => response.value?.sources.find(item => item.source === source)).filter(Boolean) as SearchSourceGroup[])
const currentConditions = computed(() => JSON.stringify({
  target: target.value?.history_id || null,
  keywords: sourceOrder.map(source => cleanKeyword(keywords.value[source])),
}))
const resultsStale = computed(() => Boolean(response.value && searchedConditions.value !== currentConditions.value))

async function search(): Promise<void> {
  if (!target.value) {
    error.value = '请先选择目标视频'
    return
  }
  loading.value = true
  error.value = ''
  notice.value = ''
  response.value = null
  try {
    response.value = await searchSubtitles(props.api, props.pluginId, {
      target_history_id: target.value.history_id,
      moviepilot_keyword: cleanKeyword(keywords.value.moviepilot),
      opensubtitles_keyword: cleanKeyword(keywords.value.opensubtitles),
      assrt_keyword: cleanKeyword(keywords.value.assrt),
    })
    openSources.value = groups.value.flatMap((group, index) => group.candidate_count > 0 ? [index] : [])
    searchedConditions.value = currentConditions.value
    if (!response.value.session_id) notice.value = '本次没有可下载候选，请调整关键词或检查字幕源状态。'
  } catch (requestError) {
    error.value = getErrorMessage(requestError, '字幕搜索失败')
  } finally {
    loading.value = false
  }
}

async function download(candidate: SubtitleCandidate): Promise<void> {
  if (!response.value?.session_id || resultsStale.value) return
  loadingCandidate.value = candidate.candidate_key
  error.value = ''
  try {
    const result = await downloadCandidate(props.api, props.pluginId, response.value.session_id, candidate.candidate_key)
    if (result.reused) toast?.info('该候选已有处理中任务，已复用原任务')
    else toast?.success('已加入下载队列')
    emit('action')
  } catch (requestError) {
    const message = getErrorMessage(requestError, '字幕下载任务提交失败')
    error.value = message
    toast?.error(message)
  } finally {
    loadingCandidate.value = ''
  }
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

function candidateMeta(candidate: SubtitleCandidate): string {
  const format = candidate.format?.toUpperCase()
  const seasons = candidate.seasons.length ? candidate.seasons : (candidate.season == null ? [] : [candidate.season])
  const episodes = candidate.episodes.length ? candidate.episodes : (candidate.episode == null ? [] : [candidate.episode])
  return [
    format && format !== 'UNKNOWN' ? format : '',
    packageLabels[candidate.package_scope],
    seasons.length ? seasons.map(value => `S${String(value).padStart(2, '0')}`).join('/') : '',
    episodes.length ? episodes.map(value => `E${String(value).padStart(2, '0')}`).join('/') : '',
  ].filter(Boolean).join(' · ')
}

function searchExecutionSummary(group: SearchSourceGroup): string {
  const details = group.details || {}
  const parts: string[] = []
  if (details.cache_hit === true) {
    parts.push(details.cache_stored_at ? `复用 ${formatDate(String(details.cache_stored_at))} 的缓存` : '复用缓存')
  } else if (details.cache_hit === false) {
    parts.push('已查询字幕源')
  }
  const pageCount = typeof details.page_count === 'number' ? details.page_count : null
  if (pageCount && pageCount > 1) parts.push(`读取 ${pageCount} 页`)
  if (details.pagination_complete === false) parts.push('分页结果不完整，未写入缓存')
  return parts.join(' · ')
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
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => `${labels[key] || key.replaceAll('_', ' ')}：${String(value)}`)
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
        <p>选择一个已整理目标，按来源查看候选并手动下载。</p>
      </div>
      <VBtn color="primary" prepend-icon="mdi-magnify" :loading="loading" :disabled="!target" @click="search">搜索字幕</VBtn>
    </header>

    <VAlert v-if="error" type="error" variant="tonal" density="compact" class="mb-3">{{ error }}</VAlert>
    <VAlert v-if="notice" type="info" variant="tonal" density="compact" class="mb-3" closable @click:close="notice = ''">{{ notice }}</VAlert>
    <VAlert v-if="resultsStale" type="warning" variant="tonal" density="compact" class="mb-3">搜索条件已变化，请重新搜索后再下载候选。</VAlert>

    <div class="search-layout">
      <aside class="search-controls">
        <TargetSelector v-model="target" :api="props.api" :plugin-id="props.pluginId" />
        <VDivider class="my-4" />
        <h3 class="control-title">来源关键词</h3>
        <p class="control-note">不填写时按自动流程执行默认策略；填写后仅执行该来源的自定义关键词。</p>
        <div v-for="source in sourceOrder" :key="source" class="keyword-block">
          <VTextField :model-value="keywords[source]" :label="sourceLabels[source]" placeholder="可选，自定义搜索词" clearable hide-details="auto" @update:model-value="setKeyword(source, $event)" />
          <div v-if="plansForSource(source).length" class="default-plan">
            <span>默认：</span>
            <VChip v-for="(plan, index) in plansForSource(source)" :key="`${source}-${plan.kind}-${index}`" size="x-small" variant="tonal" label :class="{ 'default-plan--editable': plan.editable }" :clickable="plan.editable" @click="copyPlan(source, plan.query, plan.editable)">{{ plan.label }}{{ plan.query ? ` · ${plan.query}` : '' }}</VChip>
          </div>
        </div>
      </aside>

      <div class="search-results">
        <EmptyState v-if="!response && !loading" icon="mdi-text-search" title="等待搜索" message="选择目标并开始搜索，结果将按字幕源分组展示。" />
        <VSkeletonLoader v-else-if="loading" type="list-item-three-line@6" />
        <VExpansionPanels v-else-if="groups.length" v-model="openSources" multiple variant="accordion" class="source-results">
          <VExpansionPanel v-for="group in groups" :key="group.source">
            <VExpansionPanelTitle>
              <div class="group-heading">
                <div><strong>{{ sourceLabels[group.source] }}</strong><span>{{ group.candidate_count }} 个候选 · {{ formatDuration(group.duration_ms) }}</span></div>
                <StateChip :state="sourceGroupState(group)" />
              </div>
            </VExpansionPanelTitle>
            <VExpansionPanelText>
              <div class="query-summary">
                <span>默认策略：{{ planSummary(group) || '无可用默认策略' }}</span>
                <span v-if="group.executed_queries.length">实际查询：{{ group.executed_queries.join(' → ') }}</span>
                <span v-if="searchExecutionSummary(group)">{{ searchExecutionSummary(group) }}</span>
                <span v-if="group.error_summary" class="error-text">{{ group.error_summary }}</span>
              </div>
              <EmptyState v-if="!group.candidates.length" icon="mdi-file-search-outline" title="没有可下载候选" message="该来源本轮没有返回可下载字幕。" />
              <VList v-else class="candidate-list" lines="three">
                <VListItem v-for="candidate in group.candidates" :key="candidate.candidate_key" class="candidate-item">
                  <VListItemTitle>{{ candidate.name }}</VListItemTitle>
                  <VListItemSubtitle>{{ candidate.file_name || '文件名未提供' }} · {{ candidateMeta(candidate) }}</VListItemSubtitle>
                  <VListItemSubtitle class="candidate-note">{{ candidate.language || '语言未标记' }} · {{ translationLabels[candidate.translation_type] }}{{ candidate.hearing_impaired ? ' · SDH/CC' : '' }}{{ candidate.query ? ` · 查询：${candidate.query}` : '' }}</VListItemSubtitle>
                  <VListItemSubtitle v-if="candidateTargetMismatch(candidate)" class="candidate-warning">
                    <VIcon icon="mdi-alert-outline" size="14" color="warning" /> 与当前目标集不同，下载后仍会指向当前所选视频
                  </VListItemSubtitle>
                  <VListItemSubtitle v-if="sourceDetails(candidate)" class="candidate-note candidate-source-details">{{ sourceDetails(candidate) }}</VListItemSubtitle>
                  <template #append>
                    <VBtn color="primary" variant="tonal" size="small" prepend-icon="mdi-download" :loading="loadingCandidate === candidate.candidate_key" :disabled="Boolean(loadingCandidate) || resultsStale" @click="download(candidate)">下载</VBtn>
                  </template>
                </VListItem>
              </VList>
            </VExpansionPanelText>
          </VExpansionPanel>
        </VExpansionPanels>
      </div>
    </div>

  </section>
</template>

<style scoped>
.view-shell { min-width: 0; }
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; }
.view-header h2 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 1rem; font-weight: 650; }
.view-header p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.search-layout { display: grid; grid-template-columns: minmax(18rem, 0.82fr) minmax(0, 1.6fr); gap: 1rem; align-items: start; }
.search-controls, .search-results { min-width: 0; }
.search-controls { padding: 1rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.5rem; background: rgba(var(--v-theme-surface), 0.55); }
.control-title { margin: 0; font-size: 0.875rem; }
.control-note { margin: 0.25rem 0 1rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.keyword-block + .keyword-block { margin-top: 0.85rem; }
.default-plan { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; margin-top: 0.4rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.7rem; line-height: 1.4; }
.default-plan--editable { cursor: pointer; }
.source-results { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.5rem; }
.group-heading { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 0.75rem; padding-right: 0.5rem; }
.group-heading > div > strong, .group-heading > div > span { display: block; }
.group-heading > div > strong { font-size: 0.875rem; }
.group-heading > div > span { margin-top: 0.2rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.query-summary { display: grid; gap: 0.2rem; margin-bottom: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.error-text { color: rgb(var(--v-theme-error)); }
.candidate-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; background: transparent; }
.candidate-item { min-height: 5.5rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.candidate-item:last-child { border-bottom: 0; }
.candidate-note { margin-top: 0.2rem; }
.candidate-warning { display: flex !important; align-items: center; gap: 0.25rem; margin-top: 0.25rem; color: rgb(var(--v-theme-on-surface)); }
.candidate-source-details { color: rgba(var(--v-theme-primary), 0.88); }
@media (max-width: 959px) { .search-layout { grid-template-columns: 1fr; } .search-controls { padding: 0.85rem; } }
@media (max-width: 37.5rem) { .view-header { align-items: stretch; flex-direction: column; } .view-header :deep(.v-btn) { align-self: flex-start; } .group-heading { align-items: flex-start; flex-direction: column; } .candidate-item :deep(.v-list-item__append) { align-self: center; } }
</style>
