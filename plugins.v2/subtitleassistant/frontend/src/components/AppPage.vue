<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { PluginApi } from '@/types'
import RecordsView from '@/views/RecordsView.vue'
import SearchView from '@/views/SearchView.vue'
import SourcesView from '@/views/SourcesView.vue'
import TasksView from '@/views/TasksView.vue'

const props = withDefaults(defineProps<{
  api: PluginApi
  pluginId: string
  navKey?: string
}>(), {
  navKey: 'main',
})

const emit = defineEmits<{ action: [] }>()
type WorkbenchView = 'tasks' | 'records' | 'search' | 'sources'
const SCROLL_MEMORY_SETTLE_MS = 600

const activeView = ref<WorkbenchView>('tasks')
const workbenchElement = ref<HTMLElement | null>(null)
const workbenchHeight = ref<number | null>(null)
const viewScrollTop: Record<WorkbenchView, number> = {
  tasks: 0,
  records: 0,
  search: 0,
  sources: 0,
}
let resizeFrame: number | null = null
let scrollRestoreFrame: number | null = null
let scrollRestoreRequest = 0
let scrollMemoryElement: HTMLElement | null = null
let scrollMemoryTarget: EventTarget | null = null
let scrollMemoryView: WorkbenchView | null = null
let scrollMemoryTimer: number | null = null
let pendingScrollTop = 0
let resizeObserver: ResizeObserver | null = null

const isDataView = computed(() => activeView.value === 'tasks' || activeView.value === 'records')
const workbenchStyle = computed(() => ({
  '--subtitleassistant-workbench-height': workbenchHeight.value === null
    ? undefined
    : `${workbenchHeight.value}px`,
}))

function updateWorkbenchHeight(): void {
  const element = workbenchElement.value
  if (!element || !window.matchMedia('(min-width: 960px)').matches) {
    workbenchHeight.value = null
    bindViewScrollTracking(activeView.value)
    return
  }

  const hostPage = element.closest<HTMLElement>('.layout-page-content')
  const hostBottomPadding = hostPage
    ? Number.parseFloat(window.getComputedStyle(hostPage).paddingBottom) || 0
    : 16
  const viewportTop = Math.max(0, element.getBoundingClientRect().top)
  const availableHeight = Math.max(0, Math.floor(window.innerHeight - viewportTop - hostBottomPadding))
  if (workbenchHeight.value !== availableHeight) workbenchHeight.value = availableHeight
  bindViewScrollTracking(activeView.value)
}

function scheduleWorkbenchHeightUpdate(): void {
  if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame)
  resizeFrame = window.requestAnimationFrame(() => {
    resizeFrame = null
    updateWorkbenchHeight()
  })
}

function resolveViewScrollElement(view: WorkbenchView): HTMLElement | null {
  if (typeof document === 'undefined') return null
  if (window.matchMedia('(min-width: 960px)').matches) {
    const viewElement = workbenchElement.value
      ?.querySelector<HTMLElement>(`[data-subtitleassistant-view="${view}"]`) || null
    if (view === 'search' || view === 'sources') return viewElement
    return viewElement?.querySelector<HTMLElement>('.v-table__wrapper') || null
  }

  return document.scrollingElement instanceof HTMLElement ? document.scrollingElement : document.documentElement
}

function rememberViewScroll(view: WorkbenchView): void {
  const element = resolveViewScrollElement(view)
  if (element) viewScrollTop[view] = element.scrollTop
}

function commitPendingScroll(): void {
  if (scrollMemoryView) viewScrollTop[scrollMemoryView] = pendingScrollTop
  scrollMemoryTimer = null
}

function handleTrackedScroll(): void {
  if (!scrollMemoryElement || !scrollMemoryView) return
  pendingScrollTop = scrollMemoryElement.scrollTop
  if (pendingScrollTop > viewScrollTop[scrollMemoryView]) {
    viewScrollTop[scrollMemoryView] = pendingScrollTop
  }
  if (scrollMemoryTimer !== null) window.clearTimeout(scrollMemoryTimer)
  scrollMemoryTimer = window.setTimeout(commitPendingScroll, SCROLL_MEMORY_SETTLE_MS)
}

function stopViewScrollTracking(): void {
  scrollMemoryTarget?.removeEventListener('scroll', handleTrackedScroll)
  scrollMemoryElement = null
  scrollMemoryTarget = null
  scrollMemoryView = null
  if (scrollMemoryTimer !== null) window.clearTimeout(scrollMemoryTimer)
  scrollMemoryTimer = null
}

function bindViewScrollTracking(view: WorkbenchView): void {
  const element = resolveViewScrollElement(view)
  const target = element === document.scrollingElement ? document : element
  if (scrollMemoryElement === element && scrollMemoryView === view) return
  stopViewScrollTracking()
  scrollMemoryElement = element
  scrollMemoryTarget = target
  scrollMemoryView = element ? view : null
  pendingScrollTop = element?.scrollTop || 0
  target?.addEventListener('scroll', handleTrackedScroll, { passive: true })
}

function scheduleViewScrollRestore(view: WorkbenchView): void {
  const request = ++scrollRestoreRequest
  void nextTick(() => {
    if (request !== scrollRestoreRequest) return
    if (scrollRestoreFrame !== null) window.cancelAnimationFrame(scrollRestoreFrame)
    scrollRestoreFrame = window.requestAnimationFrame(() => {
      if (request !== scrollRestoreRequest) {
        scrollRestoreFrame = null
        return
      }
      scrollRestoreFrame = window.requestAnimationFrame(() => {
        scrollRestoreFrame = null
        if (request !== scrollRestoreRequest) return
        const element = resolveViewScrollElement(view)
        if (element) element.scrollTop = viewScrollTop[view]
        bindViewScrollTracking(view)
      })
    })
  })
}

watch(activeView, (view, previousView) => {
  const wasTrackingPreviousView = scrollMemoryView === previousView
  stopViewScrollTracking()
  if (!wasTrackingPreviousView) rememberViewScroll(previousView)
  scheduleViewScrollRestore(view)
})

onMounted(() => {
  window.addEventListener('resize', scheduleWorkbenchHeightUpdate, { passive: true })
  window.visualViewport?.addEventListener('resize', scheduleWorkbenchHeightUpdate, { passive: true })
  if (workbenchElement.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(scheduleWorkbenchHeightUpdate)
    resizeObserver.observe(workbenchElement.value.parentElement || workbenchElement.value)
  }
  void nextTick(scheduleWorkbenchHeightUpdate)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', scheduleWorkbenchHeightUpdate)
  window.visualViewport?.removeEventListener('resize', scheduleWorkbenchHeightUpdate)
  resizeObserver?.disconnect()
  stopViewScrollTracking()
  if (resizeFrame !== null) window.cancelAnimationFrame(resizeFrame)
  scrollRestoreRequest += 1
  if (scrollRestoreFrame !== null) window.cancelAnimationFrame(scrollRestoreFrame)
})
</script>

<template>
  <main ref="workbenchElement" class="subtitleassistant-workbench" :style="workbenchStyle">
    <header class="workbench-heading">
      <div class="workbench-title">
        <VIcon icon="mdi-subtitles-outline" size="24" aria-hidden="true" />
        <div>
          <h1 id="subtitleassistant-workbench-title" tabindex="-1">字幕助手</h1>
          <p>覆盖搜索、匹配、落盘、库存与记录的字幕全生命周期管理</p>
        </div>
      </div>
    </header>

    <VTabs
      v-model="activeView"
      class="workbench-tabs"
      color="primary"
      show-arrows
      density="comfortable"
      aria-label="字幕助手工作台视图"
    >
      <VTab value="tasks" prepend-icon="mdi-format-list-checks">任务</VTab>
      <VTab value="records" prepend-icon="mdi-archive-outline">匹配记录</VTab>
      <VTab value="search" prepend-icon="mdi-text-search">字幕搜索</VTab>
      <VTab value="sources" prepend-icon="mdi-database-outline">字幕源状态</VTab>
    </VTabs>

    <VWindow
      v-model="activeView"
      :touch="false"
      class="workbench-window"
      :class="{ 'workbench-window--data-view': isDataView }"
    >
      <VWindowItem value="tasks" eager class="workbench-view" data-subtitleassistant-view="tasks" :transition="false" :reverse-transition="false">
        <TasksView :api="props.api" :plugin-id="props.pluginId" :active="activeView === 'tasks'" @action="emit('action')" />
      </VWindowItem>
      <VWindowItem value="records" eager class="workbench-view" data-subtitleassistant-view="records" :transition="false" :reverse-transition="false">
        <RecordsView :api="props.api" :plugin-id="props.pluginId" :active="activeView === 'records'" @action="emit('action')" />
      </VWindowItem>
      <VWindowItem value="search" eager class="workbench-view" data-subtitleassistant-view="search" :transition="false" :reverse-transition="false">
        <SearchView :api="props.api" :plugin-id="props.pluginId" :active="activeView === 'search'" @action="emit('action')" />
      </VWindowItem>
      <VWindowItem value="sources" eager class="workbench-view" data-subtitleassistant-view="sources" :transition="false" :reverse-transition="false">
        <SourcesView :api="props.api" :plugin-id="props.pluginId" :active="activeView === 'sources'" @action="emit('action')" />
      </VWindowItem>
    </VWindow>
  </main>
</template>

<style scoped>
.subtitleassistant-workbench {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  margin: 0;
  padding: 1rem 1.25rem 2rem;
  color: rgb(var(--v-theme-on-surface));
}

.workbench-heading {
  display: flex;
  min-height: 3.5rem;
  align-items: center;
  justify-content: space-between;
}

.workbench-title {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 0.75rem;
}

.workbench-title h1 {
  margin: 0;
  font-size: 1.125rem;
  font-weight: 650;
  letter-spacing: 0;
  line-height: 1.3;
}

.workbench-title h1:focus-visible {
  border-radius: 0.125rem;
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 3px;
}

.workbench-title p {
  margin: 0.125rem 0 0;
  color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity));
  font-size: 0.8125rem;
}

.workbench-tabs {
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.workbench-window {
  min-height: 28rem;
  padding-top: 1.25rem;
  overflow: clip;
}

.subtitleassistant-workbench :deep(.v-btn:focus-visible),
.subtitleassistant-workbench :deep(.v-tab:focus-visible),
.subtitleassistant-workbench :deep(input:focus-visible) {
  outline: 2px solid rgb(var(--v-theme-primary));
  outline-offset: 2px;
}

@media (min-width: 960px) {
  :global(.layout-page-content:has(.subtitleassistant-workbench) + .layout-footer) {
    display: none;
  }

  .subtitleassistant-workbench {
    display: grid;
    block-size: var(--subtitleassistant-workbench-height, calc(100dvh - 6rem));
    grid-template-rows: auto auto minmax(0, 1fr);
    overflow: hidden;
    padding-inline: 0;
    padding-block: 0.75rem 1rem;
  }

  .workbench-heading { min-height: 3rem; }

  .workbench-window {
    block-size: 100%;
    min-height: 0;
    padding-top: 0.75rem;
  }

  .workbench-view {
    block-size: 100%;
    min-block-size: 0;
    min-inline-size: 0;
    overflow: hidden;
  }

  .workbench-window:not(.workbench-window--data-view) .workbench-view {
    overflow-y: auto;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
  }
}

@media (max-width: 959px) {
  .subtitleassistant-workbench { padding: 0.75rem 1rem 1.5rem; }
  .workbench-tabs :deep(.v-slide-group__content) { min-width: max-content; }
}

@media (max-width: 37.5rem) {
  .subtitleassistant-workbench { padding-inline: 0.75rem; }
  .workbench-title p { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .subtitleassistant-workbench *,
  .subtitleassistant-workbench *::before,
  .subtitleassistant-workbench *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
</style>
