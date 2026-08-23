<script setup lang="ts">
import { computed, provide, ref, watch } from 'vue'
import { useTheme } from 'vuetify'

import AppPage from '@/components/AppPage.vue'
import Config from '@/components/Config.vue'
import type { HostToast, ThemeName } from '@/types'
import { createMockApi, mockScenarioOptions, type MockMode } from './mockApi'

const { api, state } = createMockApi()
const theme = useTheme()
const query = new URLSearchParams(window.location.search)
const requestedView = query.get('view')
const requestedTheme = query.get('theme')
const requestedMode = query.get('mode')
const view = ref<'workbench' | 'config'>(requestedView === 'config' ? 'config' : 'workbench')
const initialWorkbenchView = ref<'tasks' | 'records' | 'search' | 'sources'>(requestedView === 'search' ? 'search' : 'tasks')
const selectedTheme = ref<ThemeName>(['light', 'dark', 'purple', 'transparent'].includes(requestedTheme || '') ? requestedTheme as ThemeName : 'light')
const mockModes: MockMode[] = mockScenarioOptions.map(option => option.value)
const selectedMode = ref<MockMode>(mockModes.includes(requestedMode as MockMode) ? requestedMode as MockMode : 'normal')
const renderKey = ref(0)
const notice = ref('')
const noticeOpen = ref(false)
const noticeColor = ref<'success' | 'info' | 'warning' | 'error'>('success')

const themeOptions: Array<{ title: string; value: ThemeName }> = [
  { title: '亮色', value: 'light' },
  { title: '暗色', value: 'dark' },
  { title: '紫色', value: 'purple' },
  { title: '透明', value: 'transparent' },
]
const selectedScenario = computed(() => mockScenarioOptions.find(option => option.value === selectedMode.value) || mockScenarioOptions[0])

const shellClass = computed(() => ({
  'dev-shell--transparent': selectedTheme.value === 'transparent',
  'dev-shell--dark': ['dark', 'purple', 'transparent'].includes(selectedTheme.value),
}))
const hostToast: HostToast = {
  error: message => showHostToast(message, 'error'),
  info: message => showHostToast(message, 'info'),
  success: message => showHostToast(message, 'success'),
  warning: message => showHostToast(message, 'warning'),
}

provide('moviepilot:toast', hostToast)

state.mode = selectedMode.value
theme.global.name.value = selectedTheme.value

watch(selectedTheme, value => {
  theme.global.name.value = value
})

watch(selectedMode, value => {
  state.mode = value
  state.search_round = 0
  renderKey.value += 1
  updateQuery('mode', value)
})

function updateQuery(key: string, value: string): void {
  const nextQuery = new URLSearchParams(window.location.search)
  nextQuery.set(key, value)
  window.history.replaceState({}, '', `${window.location.pathname}?${nextQuery.toString()}${window.location.hash}`)
}

function showHostToast(message: string, color: 'success' | 'info' | 'warning' | 'error'): void {
  notice.value = message
  noticeColor.value = color
  noticeOpen.value = true
}
</script>

<template>
  <VApp :class="shellClass">
    <VAppBar density="compact" flat border>
      <VAppBarTitle class="dev-title">字幕助手开发验收</VAppBarTitle>
      <template #append>
        <VBtnToggle v-model="view" mandatory density="compact" divided class="mr-2">
          <VBtn value="workbench" prepend-icon="mdi-view-dashboard-outline">工作台</VBtn>
          <VBtn value="config" prepend-icon="mdi-cog-outline">配置</VBtn>
        </VBtnToggle>
      </template>
    </VAppBar>

    <VMain>
      <div class="dev-controls">
        <VSelect v-model="selectedTheme" label="主题" :items="themeOptions" density="compact" />
        <VSelect v-model="selectedMode" label="验收场景" :items="mockScenarioOptions" item-title="title" item-value="value" density="compact" />
        <span class="dev-viewport">调整 Chrome 视口可验收桌面与窄屏布局</span>
      </div>
      <div class="dev-scenario-note" role="status" aria-live="polite">
        <VIcon icon="mdi-flask-outline" size="16" aria-hidden="true" />
        <span><strong>{{ selectedScenario.title }}</strong>：{{ selectedScenario.description }}</span>
      </div>

      <AppPage v-if="view === 'workbench'" :key="`app-${renderKey}`" :api="api" plugin-id="SubtitleAssistant" nav-key="main" :initial-view="initialWorkbenchView" />
      <div v-else class="config-preview">
        <Config :key="`config-${renderKey}`" :api="api" :initial-config="state.config" @close="view = 'workbench'" />
      </div>
    </VMain>

    <VSnackbar v-model="noticeOpen" :timeout="2600" :color="noticeColor" location="bottom">{{ notice }}<template #actions><VBtn variant="text" @click="noticeOpen = false">关闭</VBtn></template></VSnackbar>
  </VApp>
</template>

<style scoped>
.dev-shell--transparent { background: rgba(var(--v-theme-background), 0.84); }
.dev-title { font-size: 0.9375rem; font-weight: 650; letter-spacing: 0; }
.dev-controls { display: grid; width: min(100% - 2rem, 94rem); grid-template-columns: 11rem 11rem minmax(0, 1fr); align-items: center; gap: 0.75rem; margin: 1rem auto 0; padding: 0.75rem; border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.dev-scenario-note { display: flex; width: min(100% - 2rem, 94rem); align-items: flex-start; gap: 0.4rem; margin: 0.5rem auto 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.45; }
.dev-scenario-note strong { color: rgb(var(--v-theme-on-surface)); font-weight: 650; }
.dev-viewport { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; text-align: end; }
.config-preview { width: min(100% - 2rem, 72rem); margin: 1rem auto 2rem; overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
@media (max-width: 48rem) { .dev-controls { grid-template-columns: 1fr 1fr; } .dev-viewport { grid-column: 1 / -1; text-align: start; } .dev-title { display: none; } }
@media (max-width: 37.5rem) { .dev-controls, .dev-scenario-note { width: calc(100% - 1rem); } .dev-controls { grid-template-columns: 1fr; } .dev-viewport { grid-column: auto; } .config-preview { width: 100%; margin-top: 0.5rem; border-inline: 0; border-radius: 0; } }
</style>
