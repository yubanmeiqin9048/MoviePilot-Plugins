<script setup lang="ts">
import { computed, useId } from 'vue'

import { useDialogFocusReturn } from '@/composables/useDialogFocusReturn'
import StateChip from '@/components/StateChip.vue'
import type { SearchSourceGroup } from '@/types'
import {
  formatDuration,
  formatSearchPlan,
  manualSourceState,
  sourceLabels,
  subtitleSourceOrder,
} from '@/types/presentation'

const props = withDefaults(defineProps<{
  modelValue: boolean
  sources: SearchSourceGroup[]
  disabled?: boolean
}>(), {
  disabled: false,
})

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const dialogTitleId = `subtitleassistant-search-details-title-${useId()}`
const triggerId = `subtitleassistant-search-details-trigger-${useId()}`
const { captureFocus, restoreFocus } = useDialogFocusReturn(triggerId)

const dialogOpen = computed({
  get: () => props.modelValue,
  set: value => emit('update:modelValue', value),
})

function openDialog(): void {
  captureFocus()
  emit('update:modelValue', true)
}

function cacheLabel(group: SearchSourceGroup): string {
  const cache = Array.isArray(group.details.cache)
    ? group.details.cache.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>
    : []
  if (group.details.cache_hit === true || cache.some(item => item.hit === true)) return '是，复用来源查询缓存'
  if (group.details.cache_hit === false || cache.some(item => item.hit === false || item.state === 'miss' || item.state === 'invalid')) {
    return '否，本次执行来源查询'
  }
  return '未记录'
}

function formatPageCount(group: SearchSourceGroup): string {
  const pagination = Array.isArray(group.details.pagination)
    ? group.details.pagination.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>
    : []
  const tracedPages = pagination.reduce((total, item) => {
    const pagesFetched = item.pages_fetched
    return total + (typeof pagesFetched === 'number' && Number.isFinite(pagesFetched) && pagesFetched > 0 ? pagesFetched : 0)
  }, 0)
  const value = tracedPages || group.details.page_count
  return typeof value === 'number' && Number.isFinite(value) ? `${value} 页` : '未记录'
}

function paginationLabel(group: SearchSourceGroup): string {
  const pagination = Array.isArray(group.details.pagination)
    ? group.details.pagination.filter(item => item && typeof item === 'object') as Array<Record<string, unknown>>
    : []
  if (pagination.some(item => item.complete === false) || group.details.pagination_complete === false) return '不完整'
  if (pagination.length > 0 || group.details.pagination_complete === true) return '完整'
  return '未记录'
}

function sourceGroups(): SearchSourceGroup[] {
  return subtitleSourceOrder
    .map(source => props.sources.find(group => group.source === source))
    .filter((group): group is SearchSourceGroup => Boolean(group))
}
</script>

<template>
  <div class="search-details">
    <VBtn
      :id="triggerId"
      type="button"
      variant="text"
      size="small"
      prepend-icon="mdi-text-box-search-outline"
      aria-label="打开搜索详情"
      :disabled="props.disabled"
      @click="openDialog"
    >
      搜索详情
    </VBtn>

    <VDialog
      v-model="dialogOpen"
      :max-width="720"
      scrollable
      retain-focus
      :aria-labelledby="dialogTitleId"
      @after-leave="restoreFocus"
    >
      <VCard class="search-details-dialog" :aria-labelledby="dialogTitleId">
        <VCardTitle :id="dialogTitleId" class="search-details-dialog__title">
          <span>搜索详情</span>
          <VBtn icon="mdi-close" variant="text" size="small" aria-label="关闭搜索详情" @click="dialogOpen = false" />
        </VCardTitle>
        <VCardText class="search-details-dialog__body">
          <p class="search-details-dialog__intro">按来源查看本次人工字幕搜索的实际执行事实。</p>
          <section v-for="group in sourceGroups()" :key="group.source" class="source-detail" :aria-labelledby="`source-detail-${group.source}`">
            <div class="source-detail__heading">
              <div>
                <h3 :id="`source-detail-${group.source}`">{{ sourceLabels[group.source] }}</h3>
                <StateChip :state="manualSourceState(group.status, group.candidate_count)" size="x-small" />
              </div>
              <span class="source-detail__duration">耗时 {{ formatDuration(group.duration_ms) }}</span>
            </div>
            <dl class="source-detail__facts">
              <div><dt>候选数量</dt><dd>{{ group.candidate_count }}</dd></div>
              <div><dt>缓存是否复用</dt><dd>{{ cacheLabel(group) }}</dd></div>
              <div><dt>分页数量</dt><dd>{{ formatPageCount(group) }}</dd></div>
              <div><dt>分页完整性</dt><dd>{{ paginationLabel(group) }}</dd></div>
            </dl>
            <div class="source-detail__queries">
              <div>
                <h4>默认查询计划</h4>
                <ul v-if="group.default_plans.length">
                  <li v-for="(plan, index) in group.default_plans" :key="`${group.source}-plan-${index}`">{{ formatSearchPlan(plan) }}</li>
                </ul>
                <p v-else>未记录</p>
              </div>
              <div>
                <h4>已执行查询</h4>
                <p>{{ group.executed_queries.length ? group.executed_queries.join(' → ') : '未执行' }}</p>
              </div>
              <div>
                <h4>命中查询</h4>
                <p>{{ group.matched_query || '未命中' }}</p>
              </div>
            </div>
            <div class="source-detail__error" :class="{ 'source-detail__error--active': group.error_summary }" role="status">
              <strong>安全错误</strong>
              <span>{{ group.error_summary || '无' }}</span>
            </div>
          </section>
        </VCardText>
      </VCard>
    </VDialog>
  </div>
</template>

<style scoped>
.search-details { flex: 0 0 auto; }
.search-details-dialog { max-height: min(44rem, calc(100vh - 2rem)); }
.search-details-dialog__title { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.65rem 0.8rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); font-size: 0.95rem; font-weight: 650; }
.search-details-dialog__body { min-width: 0; padding: 0.8rem; }
.search-details-dialog__intro { margin: 0 0 0.8rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.source-detail { padding: 0.8rem 0; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.source-detail:first-of-type { padding-top: 0; border-top: 0; }
.source-detail__heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; }
.source-detail__heading > div { display: grid; min-width: 0; gap: 0.35rem; }
.source-detail__heading h3 { margin: 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.8125rem; font-weight: 650; }
.source-detail__duration { flex: 0 0 auto; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; }
.source-detail__facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.6rem; margin: 0.75rem 0 0; }
.source-detail__facts > div { min-width: 0; }
.source-detail__facts dt, .source-detail__queries h4 { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.625rem; font-weight: 500; }
.source-detail__facts dd { margin: 0.18rem 0 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.75rem; line-height: 1.4; overflow-wrap: anywhere; }
.source-detail__queries { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.7rem; margin-top: 0.8rem; }
.source-detail__queries h4 { margin: 0; }
.source-detail__queries p, .source-detail__queries ul { margin: 0.22rem 0 0; color: rgb(var(--v-theme-on-surface)); font-size: 0.6875rem; line-height: 1.5; overflow-wrap: anywhere; }
.source-detail__queries ul { padding-left: 1rem; }
.source-detail__error { display: flex; align-items: baseline; gap: 0.4rem; margin-top: 0.75rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; line-height: 1.5; }
.source-detail__error strong { color: rgb(var(--v-theme-on-surface)); font-weight: 650; }
.source-detail__error--active { color: rgb(var(--v-theme-error)); }

@media (max-width: 600px) {
  .search-details-dialog { max-height: calc(100vh - 1rem); }
  .search-details-dialog__body { padding: 0.7rem; }
  .source-detail__facts { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .source-detail__queries { grid-template-columns: 1fr; gap: 0.65rem; }
}

@media (prefers-reduced-motion: reduce) {
  .search-details-dialog *, .search-details-dialog *::before, .search-details-dialog *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
