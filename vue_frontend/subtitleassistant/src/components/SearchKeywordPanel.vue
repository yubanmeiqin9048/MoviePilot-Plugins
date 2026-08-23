<script setup lang="ts">
import { ref } from 'vue'

import type { SearchPlanItem, SubtitleSource } from '@/types'
import { formatSearchPlan, sourceLabels, subtitleSourceOrder } from '@/types/presentation'

const props = defineProps<{
  keywords: Record<SubtitleSource, string>
  plans: Record<SubtitleSource, SearchPlanItem[]>
}>()

const emit = defineEmits<{ change: [source: SubtitleSource, value: string] }>()
const expandedPanels = ref<number[]>([])

function updateKeyword(source: SubtitleSource, value: string | null): void {
  emit('change', source, value || '')
}

function usePlan(source: SubtitleSource, plan: SearchPlanItem): void {
  if (plan.editable && plan.query) emit('change', source, plan.query)
}

</script>

<template>
  <VExpansionPanels v-model="expandedPanels" multiple variant="accordion" class="keyword-panel">
    <VExpansionPanel>
      <VExpansionPanelTitle>
        <div class="keyword-panel__title">
          <span><VIcon icon="mdi-tune-variant" size="18" aria-hidden="true" />来源关键词</span>
          <small>可选，留空使用默认查询</small>
        </div>
      </VExpansionPanelTitle>
      <VExpansionPanelText>
        <p class="keyword-panel__note">按来源修改当前条件，不会自动发起搜索；准备好后请显式执行搜索。</p>
        <div class="keyword-grid">
          <div v-for="source in subtitleSourceOrder" :key="source" class="keyword-block">
            <VTextField
              :model-value="props.keywords[source]"
              :label="sourceLabels[source]"
              placeholder="可选，自定义搜索词"
              clearable
              hide-details="auto"
              density="compact"
              :aria-label="`${sourceLabels[source]}自定义关键词`"
              @update:model-value="updateKeyword(source, $event)"
            />
            <div class="default-plan" :aria-label="`${sourceLabels[source]}默认查询计划`">
              <span class="default-plan__label">默认查询计划</span>
              <template v-if="props.plans[source]?.length">
                <template v-for="(plan, index) in props.plans[source]" :key="`${source}-${plan.kind}-${index}`">
                  <VBtn
                    v-if="plan.editable && plan.query"
                    variant="text"
                    size="x-small"
                    class="default-plan__button"
                    :aria-label="`使用${sourceLabels[source]}查询计划：${formatSearchPlan(plan)}`"
                    @click="usePlan(source, plan)"
                  >
                    {{ formatSearchPlan(plan) }}
                  </VBtn>
                  <VChip v-else size="x-small" variant="tonal" label>{{ formatSearchPlan(plan) }}</VChip>
                </template>
              </template>
              <span v-else class="default-plan__empty">留空将使用默认计划；首次搜索后可查看实际计划</span>
            </div>
          </div>
        </div>
      </VExpansionPanelText>
    </VExpansionPanel>
  </VExpansionPanels>
</template>

<style scoped>
.keyword-panel { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0; }
.keyword-panel :deep(.v-expansion-panel) { background: transparent; }
.keyword-panel :deep(.v-expansion-panel-title) { min-height: 2.85rem; padding: 0.6rem 1.15rem; }
.keyword-panel :deep(.v-expansion-panel-text__wrapper) { padding: 0 1.15rem 0.9rem; }
.keyword-panel__title { display: flex; width: 100%; align-items: center; justify-content: space-between; gap: 1rem; padding-right: 0.5rem; }
.keyword-panel__title span { display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.8125rem; font-weight: 650; }
.keyword-panel__title small, .keyword-panel__note { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; }
.keyword-panel__note { margin: 0 0 0.75rem; line-height: 1.5; }
.keyword-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.65rem; }
.keyword-block { min-width: 0; }
.default-plan { display: flex; flex-wrap: wrap; align-items: center; gap: 0.3rem; margin-top: 0.35rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.6875rem; line-height: 1.4; }
.default-plan__label { flex: 0 0 auto; }
.default-plan__button { min-height: 1.5rem; padding-inline: 0.25rem; font-size: 0.6875rem; letter-spacing: 0; text-transform: none; }
.default-plan__empty { overflow-wrap: anywhere; }

@media (max-width: 800px) {
  .keyword-grid { grid-template-columns: 1fr; }
}

@media (max-width: 420px) {
  .keyword-panel__title { align-items: flex-start; flex-direction: column; gap: 0.2rem; }
}

@media (prefers-reduced-motion: reduce) {
  .keyword-panel *, .keyword-panel *::before, .keyword-panel *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
}
</style>
