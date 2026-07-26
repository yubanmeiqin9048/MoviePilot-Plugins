<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref, watch } from 'vue'

import { clearCredentials, getErrorMessage, updateCredentials } from '@/api/client'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import type {
  ConfigModel,
  HostToast,
  NonSensitiveConfig,
  PackageAttributionStrategy,
  PathMapping,
  PluginApi,
  SubtitleSource,
} from '@/types'
import { sourceLabels } from '@/types/presentation'

const props = withDefaults(defineProps<{
  initialConfig?: Partial<ConfigModel>
  api: PluginApi
}>(), {
  initialConfig: () => ({}),
})

const emit = defineEmits<{
  save: [config: NonSensitiveConfig]
  close: []
  layout: [layout: { maxWidth: string }]
}>()
const toast = inject<HostToast | null>('moviepilot:toast', null)

type ExternalSource = 'opensubtitles' | 'assrt'
type SourceMeta = { source: SubtitleSource; icon: string; description: string }

const sourceMeta: SourceMeta[] = [
  { source: 'moviepilot', icon: 'mdi-server-network', description: '使用 MoviePilot 当前有效的字幕站点。' },
  { source: 'opensubtitles', icon: 'mdi-closed-caption-outline', description: '使用 OpenSubtitles.com REST API。' },
  { source: 'assrt', icon: 'mdi-subtitles-outline', description: '使用 ASSRT 字幕服务。' },
]
const defaultSources: SubtitleSource[] = ['moviepilot', 'assrt', 'opensubtitles']
const defaultFormats = ['ASS', 'SSA', 'SRT', 'SUP']

const form = reactive<{
  enabled: boolean
  moviepilot_enabled: boolean
  opensubtitles_enabled: boolean
  assrt_enabled: boolean
  allow_machine_translation: boolean
  ai_attribution_takeover_enabled: boolean
  max_candidate_attempts: number
  package_attribution_strategy: PackageAttributionStrategy
}>({
  enabled: false,
  moviepilot_enabled: true,
  opensubtitles_enabled: false,
  assrt_enabled: false,
  allow_machine_translation: false,
  ai_attribution_takeover_enabled: false,
  max_candidate_attempts: 3,
  package_attribution_strategy: 'trust_package',
})
const pathMappings = ref<PathMapping[]>([])
const sourcePriority = ref<SubtitleSource[]>([...defaultSources])
const formatPriority = ref<string[]>([...defaultFormats])
const allowedFormats = ref<string[]>([...defaultFormats])
const opensubtitlesConfigured = ref(false)
const assrtConfigured = ref(false)
const credentials = reactive({
  opensubtitles: { api_key: '', username: '', password: '' },
  assrt: { token: '' },
})
const openSources = ref<number[]>([])
const saving = ref(false)
const saveError = ref('')
const showApiKey = ref(false)
const showPassword = ref(false)
const showAssrtToken = ref(false)
const clearOpen = ref(false)
const clearSource = ref<ExternalSource | null>(null)
const clearing = ref(false)

const pluginId = computed(() => typeof props.initialConfig?.plugin_id === 'string' ? props.initialConfig.plugin_id.trim() : '')
const hostAiEnabled = computed(() => Boolean(
  props.initialConfig?.ai_agent_enabled
  ?? props.initialConfig?.ai_agent_available
  ?? props.initialConfig?.host_ai_enabled
  ?? props.initialConfig?.moviepilot_ai_enabled,
))
const osDraftValues = computed(() => Object.values(credentials.opensubtitles).map(value => value.trim()))
const hasOsUpdate = computed(() => osDraftValues.value.some(Boolean))
const hasAssrtUpdate = computed(() => Boolean(credentials.assrt.token.trim()))
const osComplete = computed(() => opensubtitlesConfigured.value || osDraftValues.value.every(Boolean))
const assrtComplete = computed(() => assrtConfigured.value || hasAssrtUpdate.value)
const osError = computed(() => form.opensubtitles_enabled && !osComplete.value ? '启用前需提供 API Key、用户名和密码。' : '')
const assrtError = computed(() => form.assrt_enabled && !assrtComplete.value ? '启用前需提供 ASSRT Token。' : '')
const attemptsError = computed(() => {
  const value = Number(form.max_candidate_attempts)
  return Number.isInteger(value) && value >= 1 && value <= 10 ? '' : '最大尝试数必须是 1 到 10 的整数。'
})
const pathMappingsError = computed(() => {
  for (let index = 0; index < pathMappings.value.length; index += 1) {
    const sourceError = pathMappingFieldError(index, 'source_prefix')
    if (sourceError) return sourceError
    const targetError = pathMappingFieldError(index, 'target_prefix')
    if (targetError) return targetError
  }
  return ''
})
const canSave = computed(() => !saving.value && !clearing.value && !osError.value && !assrtError.value && !attemptsError.value && !pathMappingsError.value)
const clearTitle = computed(() => clearSource.value === 'opensubtitles' ? '清除 OpenSubtitles 凭据' : '清除 ASSRT 凭据')
const clearMessage = computed(() => clearSource.value === 'opensubtitles'
  ? '将永久删除 API Key、用户名、密码和当前登录会话，并立即关闭 OpenSubtitles 来源。旧凭据无法恢复。'
  : '将永久删除 ASSRT Token，并立即关闭 ASSRT 来源。旧 Token 无法恢复。')

watch(() => props.initialConfig, applyInitialConfig, { immediate: true, deep: true })
onMounted(() => emit('layout', { maxWidth: '72rem' }))

function applyInitialConfig(): void {
  const initial = props.initialConfig || {}
  form.enabled = Boolean(initial.enabled)
  form.moviepilot_enabled = initial.moviepilot_enabled !== false
  form.opensubtitles_enabled = Boolean(initial.opensubtitles_enabled)
  form.assrt_enabled = Boolean(initial.assrt_enabled)
  form.allow_machine_translation = Boolean(initial.allow_machine_translation)
  form.ai_attribution_takeover_enabled = Boolean(initial.ai_attribution_takeover_enabled)
  form.max_candidate_attempts = validAttemptCount(initial.max_candidate_attempts) ? Number(initial.max_candidate_attempts) : 3
  form.package_attribution_strategy = initial.package_attribution_strategy === 'host_recognition'
    ? 'host_recognition'
    : 'trust_package'
  pathMappings.value = normalizePathMappings(initial.path_mappings)
  opensubtitlesConfigured.value = Boolean(initial.opensubtitles_configured)
  assrtConfigured.value = Boolean(initial.assrt_configured)

  const normalizedAllowed = normalizeFormats(initial.allowed_formats)
  allowedFormats.value = normalizedAllowed.length ? normalizedAllowed : [...defaultFormats]
  formatPriority.value = mergeOrder(normalizeFormats(initial.format_priority), allowedFormats.value)
  sourcePriority.value = mergeSourceOrder(initial.source_priority)
  clearCredentialDrafts()
  saveError.value = ''
}

function validAttemptCount(value: unknown): boolean {
  const number = Number(value)
  return Number.isInteger(number) && number >= 1 && number <= 10
}

function normalizeFormats(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value
    .filter((item): item is string => typeof item === 'string')
    .map(item => item.trim().replace(/^\./, '').toUpperCase())
    .filter(Boolean))]
}

function mergeOrder(saved: string[], allowed: string[]): string[] {
  const allowedSet = new Set(allowed)
  return [...saved.filter(item => allowedSet.has(item)), ...allowed.filter(item => !saved.includes(item))]
}

function mergeSourceOrder(value: unknown): SubtitleSource[] {
  const valid = Array.isArray(value)
    ? value.filter((item): item is SubtitleSource => defaultSources.includes(item as SubtitleSource))
    : []
  return [...new Set([...valid, ...defaultSources])]
}

function normalizePathMappings(value: unknown): PathMapping[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    return [{
      source_prefix: typeof record.source_prefix === 'string' ? record.source_prefix : '',
      target_prefix: typeof record.target_prefix === 'string' ? record.target_prefix : '',
    }]
  })
}

function addPathMapping(): void {
  pathMappings.value.push({ source_prefix: '', target_prefix: '' })
}

function removePathMapping(index: number): void {
  pathMappings.value.splice(index, 1)
}

function isAbsolutePath(value: string): boolean {
  return value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value) || /^\\\\[^\\]+/.test(value)
}

function comparablePath(value: string): string {
  const normalized = value.trim().replaceAll('\\', '/').replace(/\/+$/, '') || '/'
  return /^[A-Za-z]:\//.test(normalized) ? normalized.toLowerCase() : normalized
}

function pathMappingFieldError(index: number, field: keyof PathMapping): string {
  const row = pathMappings.value[index]
  if (!row) return ''
  const value = row[field].trim()
  const fieldLabel = field === 'source_prefix' ? '历史目录前缀' : '当前目录前缀'
  if (!value) return `第 ${index + 1} 行${fieldLabel}不能为空。`
  if (!isAbsolutePath(value)) return `第 ${index + 1} 行${fieldLabel}必须是绝对路径。`
  if (/[*?]/.test(value)) return `第 ${index + 1} 行${fieldLabel}不能包含通配符或正则表达式。`

  const source = comparablePath(row.source_prefix)
  const target = comparablePath(row.target_prefix)
  if (field === 'target_prefix' && source === target) return `第 ${index + 1} 行的历史目录与当前目录不能相同。`
  if (field === 'source_prefix') {
    const duplicate = pathMappings.value.some((item, itemIndex) => (
      itemIndex !== index && comparablePath(item.source_prefix) === source
    ))
    if (duplicate) return `第 ${index + 1} 行的历史目录前缀重复。`
  }
  const chained = pathMappings.value.some((item, itemIndex) => (
    itemIndex !== index && comparablePath(item.source_prefix) === target
  ))
  if (field === 'target_prefix' && chained) return `第 ${index + 1} 行会形成链式映射，请直接填写最终目录。`
  return ''
}

function enabledKey(source: SubtitleSource): 'moviepilot_enabled' | 'opensubtitles_enabled' | 'assrt_enabled' {
  return `${source}_enabled` as 'moviepilot_enabled' | 'opensubtitles_enabled' | 'assrt_enabled'
}

function configured(source: SubtitleSource): boolean {
  if (source === 'moviepilot') return true
  return source === 'opensubtitles' ? opensubtitlesConfigured.value : assrtConfigured.value
}

function sourceValidation(source: SubtitleSource): string {
  if (source === 'opensubtitles') return osError.value
  if (source === 'assrt') return assrtError.value
  return ''
}

function move<T>(items: T[], index: number, direction: -1 | 1): void {
  const next = index + direction
  if (next < 0 || next >= items.length) return
  const [item] = items.splice(index, 1)
  items.splice(next, 0, item)
}

function nonSensitiveConfig(): NonSensitiveConfig {
  return {
    enabled: form.enabled,
    moviepilot_enabled: form.moviepilot_enabled,
    opensubtitles_enabled: form.opensubtitles_enabled,
    assrt_enabled: form.assrt_enabled,
    allow_machine_translation: form.allow_machine_translation,
    ai_attribution_takeover_enabled: form.ai_attribution_takeover_enabled,
    max_candidate_attempts: Number(form.max_candidate_attempts),
    source_priority: [...sourcePriority.value],
    format_priority: [...formatPriority.value],
    path_mappings: pathMappings.value.map(item => ({
      source_prefix: item.source_prefix.trim(),
      target_prefix: item.target_prefix.trim(),
    })),
    package_attribution_strategy: form.package_attribution_strategy,
  }
}

async function saveConfig(): Promise<void> {
  saveError.value = ''
  if (!canSave.value) {
    saveError.value = osError.value || assrtError.value || attemptsError.value || pathMappingsError.value || '请修正配置后再保存。'
    return
  }
  if (!hasOsUpdate.value && !hasAssrtUpdate.value) {
    emit('save', nonSensitiveConfig())
    return
  }
  if (!pluginId.value) {
    saveError.value = '缺少插件实例 ID，无法安全写入凭据。'
    return
  }

  saving.value = true
  try {
    if (hasOsUpdate.value) {
      const payload = Object.fromEntries(
        Object.entries(credentials.opensubtitles)
          .map(([key, value]) => [key, value.trim()])
          .filter(([, value]) => Boolean(value)),
      )
      const response = await updateCredentials(props.api, pluginId.value, 'opensubtitles', payload)
      opensubtitlesConfigured.value = Boolean(response.data?.configured)
    }
    if (hasAssrtUpdate.value) {
      const response = await updateCredentials(props.api, pluginId.value, 'assrt', { token: credentials.assrt.token.trim() })
      assrtConfigured.value = Boolean(response.data?.configured)
    }
    clearCredentialDrafts()
    showNotice('凭据已更新', 'success')
    emit('save', nonSensitiveConfig())
  } catch (requestError) {
    saveError.value = getErrorMessage(requestError, '凭据更新失败，普通配置尚未保存。')
  } finally {
    saving.value = false
  }
}

function clearCredentialDrafts(): void {
  credentials.opensubtitles.api_key = ''
  credentials.opensubtitles.username = ''
  credentials.opensubtitles.password = ''
  credentials.assrt.token = ''
  showApiKey.value = false
  showPassword.value = false
  showAssrtToken.value = false
}

function requestClear(source: ExternalSource): void {
  clearSource.value = source
  clearOpen.value = true
}

async function confirmClear(): Promise<void> {
  const source = clearSource.value
  if (!source) return
  if (!pluginId.value) {
    clearOpen.value = false
    saveError.value = '缺少插件实例 ID，无法安全清除凭据。'
    return
  }
  clearing.value = true
  try {
    const response = await clearCredentials(props.api, pluginId.value, source)
    if (source === 'opensubtitles') {
      opensubtitlesConfigured.value = false
      form.opensubtitles_enabled = false
      credentials.opensubtitles.api_key = ''
      credentials.opensubtitles.username = ''
      credentials.opensubtitles.password = ''
    } else {
      assrtConfigured.value = false
      form.assrt_enabled = false
      credentials.assrt.token = ''
    }
    clearOpen.value = false
    clearSource.value = null
    if (response.success) {
      showNotice('凭据已清除，字幕源已关闭', 'success')
    } else {
      saveError.value = response.message || '凭据已清除且来源已关闭，但开关保存失败，请重试保存普通配置。'
      showNotice('凭据已清除，来源开关需要重新保存', 'warning')
    }
  } catch (requestError) {
    clearOpen.value = false
    saveError.value = getErrorMessage(requestError, '凭据清除失败')
  } finally {
    clearing.value = false
  }
}

function showNotice(text: string, color: 'success' | 'error' | 'warning'): void {
  toast?.[color](text)
}
</script>

<template>
  <div class="config-shell">
    <header class="config-header">
      <div>
        <h2>字幕助手设置</h2>
        <p>配置字幕全生命周期中的运行范围、整理历史路径、字幕源与候选处理方式。</p>
      </div>
      <VTooltip text="关闭设置">
        <template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-close" variant="text" aria-label="关闭设置" :disabled="saving || clearing" @click="emit('close')" /></template>
      </VTooltip>
    </header>

    <VAlert v-if="saveError" type="error" variant="tonal" density="compact" class="mx-5 mt-4" closable @click:close="saveError = ''">{{ saveError }}</VAlert>

    <form class="config-form" @submit.prevent="saveConfig">
      <section class="config-section" aria-labelledby="basic-settings-title">
        <div class="section-heading"><VIcon icon="mdi-tune-variant" /><div><h3 id="basic-settings-title">基本设置</h3><p>控制后续新整理事件是否进入自动处理流程。</p></div></div>
        <div class="section-content">
          <VSwitch v-model="form.enabled" label="启用自动处理" inset />
          <VAlert type="info" variant="tonal" density="compact" icon="mdi-information-outline">
            仅处理插件运行期间新收到的整理完成事件；历史媒体不会扫描，未完成任务不会在重启后恢复。
          </VAlert>
        </div>
      </section>

      <VDivider />

      <section class="config-section" aria-labelledby="path-mapping-title">
        <div class="section-heading">
          <VIcon icon="mdi-map-marker-path" />
          <div>
            <h3 id="path-mapping-title">整理历史路径映射</h3>
            <p>人工下载和改配时，将历史目录前缀替换为当前可用目录。</p>
          </div>
        </div>
        <div class="section-content mapping-settings">
          <div v-if="pathMappings.length" class="mapping-list">
            <div v-for="(mapping, index) in pathMappings" :key="index" class="mapping-row">
              <VTextField
                v-model="mapping.source_prefix"
                label="历史目录前缀"
                placeholder="/旧挂载/媒体"
                prepend-inner-icon="mdi-history"
                :error-messages="pathMappingFieldError(index, 'source_prefix') ? [pathMappingFieldError(index, 'source_prefix')] : []"
              />
              <VIcon icon="mdi-arrow-right" class="mapping-arrow" aria-hidden="true" />
              <VTextField
                v-model="mapping.target_prefix"
                label="当前目录前缀"
                placeholder="/当前挂载/媒体"
                prepend-inner-icon="mdi-folder-outline"
                :error-messages="pathMappingFieldError(index, 'target_prefix') ? [pathMappingFieldError(index, 'target_prefix')] : []"
              />
              <VTooltip text="删除此路径映射">
                <template #activator="{ props: tooltipProps }">
                  <VBtn
                    v-bind="tooltipProps"
                    icon="mdi-delete-outline"
                    variant="text"
                    color="error"
                    :aria-label="`删除第 ${index + 1} 条路径映射`"
                    @click="removePathMapping(index)"
                  />
                </template>
              </VTooltip>
            </div>
          </div>
          <div v-else class="mapping-empty">
            <VIcon icon="mdi-map-marker-off-outline" size="20" />
            <span>未配置映射时直接使用整理历史中的原始目标路径。</span>
          </div>
          <VBtn variant="tonal" prepend-icon="mdi-plus" class="mapping-add" @click="addPathMapping">添加路径映射</VBtn>
        </div>
      </section>

      <VDivider />

      <section class="config-section" aria-labelledby="source-settings-title">
        <div class="section-heading"><VIcon icon="mdi-database-outline" /><div><h3 id="source-settings-title">字幕源</h3><p>三个已启用来源会在同一任务内并发搜索。</p></div></div>
        <div class="section-content">
          <VAlert v-if="osError || assrtError" type="error" variant="tonal" density="compact" class="mb-3">
            {{ [osError, assrtError].filter(Boolean).join(' ') }}
          </VAlert>
          <VExpansionPanels v-model="openSources" multiple variant="accordion" class="source-config-list">
            <VExpansionPanel v-for="meta in sourceMeta" :key="meta.source" class="source-config-panel">
              <div class="source-config-header">
                <VExpansionPanelTitle>
                  <div class="source-config-summary">
                    <VIcon :icon="meta.icon" size="20" />
                    <div><strong>{{ sourceLabels[meta.source] }}</strong><span>{{ meta.description }}</span></div>
                    <VChip :color="configured(meta.source) ? 'success' : 'warning'" variant="tonal" size="small" label>{{ configured(meta.source) ? '配置完整' : '配置不完整' }}</VChip>
                  </div>
                </VExpansionPanelTitle>
                <VSwitch
                  v-model="form[enabledKey(meta.source)]"
                  class="source-config-toggle"
                  :aria-label="`启用 ${sourceLabels[meta.source]}`"
                  hide-details
                  inset
                  density="compact"
                  @click.stop
                  @keydown.stop
                />
              </div>
              <VExpansionPanelText>
                <VAlert v-if="sourceValidation(meta.source)" type="error" variant="tonal" density="compact" class="mb-3">{{ sourceValidation(meta.source) }}</VAlert>

                <div v-if="meta.source === 'moviepilot'" class="source-body-note">
                  <VIcon icon="mdi-shield-lock-outline" size="20" />
                  <span>站点身份信息由 MoviePilot 在下载前重新读取，本插件不保存或展示 Cookie。</span>
                </div>

                <div v-else-if="meta.source === 'opensubtitles'" class="credential-fields">
                  <VTextField
                    v-model="credentials.opensubtitles.api_key"
                    label="API Key"
                    :type="showApiKey ? 'text' : 'password'"
                    autocomplete="new-password"
                    placeholder="留空则保留现有值"
                  >
                    <template #append-inner>
                      <VBtn
                        :icon="showApiKey ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
                        size="small"
                        variant="text"
                        :aria-label="showApiKey ? '隐藏 API Key' : '显示 API Key'"
                        @click="showApiKey = !showApiKey"
                      />
                    </template>
                  </VTextField>
                  <VTextField v-model="credentials.opensubtitles.username" label="用户名" autocomplete="off" placeholder="留空则保留现有值" />
                  <VTextField
                    v-model="credentials.opensubtitles.password"
                    label="密码"
                    :type="showPassword ? 'text' : 'password'"
                    autocomplete="new-password"
                    placeholder="留空则保留现有值"
                  >
                    <template #append-inner>
                      <VBtn
                        :icon="showPassword ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
                        size="small"
                        variant="text"
                        :aria-label="showPassword ? '隐藏密码' : '显示密码'"
                        @click="showPassword = !showPassword"
                      />
                    </template>
                  </VTextField>
                  <div class="credential-actions"><VBtn variant="text" color="error" prepend-icon="mdi-key-remove" :disabled="!opensubtitlesConfigured || clearing" @click="requestClear('opensubtitles')">清除凭据</VBtn></div>
                </div>

                <div v-else class="credential-fields credential-fields--single">
                  <VTextField
                    v-model="credentials.assrt.token"
                    label="Token"
                    :type="showAssrtToken ? 'text' : 'password'"
                    autocomplete="new-password"
                    placeholder="留空则保留现有值"
                  >
                    <template #append-inner>
                      <VBtn
                        :icon="showAssrtToken ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
                        size="small"
                        variant="text"
                        :aria-label="showAssrtToken ? '隐藏 Token' : '显示 Token'"
                        @click="showAssrtToken = !showAssrtToken"
                      />
                    </template>
                  </VTextField>
                  <div class="credential-actions"><VBtn variant="text" color="error" prepend-icon="mdi-key-remove" :disabled="!assrtConfigured || clearing" @click="requestClear('assrt')">清除凭据</VBtn></div>
                </div>
              </VExpansionPanelText>
            </VExpansionPanel>
          </VExpansionPanels>
        </div>
      </section>

      <VDivider />

      <section class="config-section" aria-labelledby="candidate-settings-title">
        <div class="section-heading"><VIcon icon="mdi-sort-variant" /><div><h3 id="candidate-settings-title">候选策略</h3><p>控制压缩包内字幕归属，以及候选与格式的选择顺序。</p></div></div>
        <div class="section-content candidate-settings">
          <div class="attribution-setting">
            <span id="attribution-strategy-label" class="field-label">压缩包字幕归属</span>
            <VBtnToggle
              v-model="form.package_attribution_strategy"
              mandatory
              divided
              color="primary"
              variant="outlined"
              aria-labelledby="attribution-strategy-label"
              class="attribution-toggle"
            >
              <VBtn value="trust_package" prepend-icon="mdi-package-variant-closed-check">信任候选包</VBtn>
              <VBtn value="host_recognition" prepend-icon="mdi-text-box-search-outline">MoviePilot 识别</VBtn>
            </VBtnToggle>
            <p class="field-hint">
              {{ form.package_attribution_strategy === 'trust_package'
                ? '继承候选目标的媒体身份，仅从包内路径提取季集。'
                : '逐个字幕调用 MoviePilot 媒体识别，并核对媒体 ID。' }}
            </p>
          </div>
          <div class="ai-takeover-setting">
            <VSwitch
              v-model="form.ai_attribution_takeover_enabled"
              label="字幕归属失败时允许 AI 智能接管"
              inset
              :disabled="!hostAiEnabled"
            />
            <p class="field-hint">
              {{ hostAiEnabled
                ? '仅在常规字幕归属无法形成确定结论时请求 MoviePilot 当前配置的 LLM；会发送媒体名称、候选名称和包内相对文件名，AI 只提出结构化归属建议，不会直接移动或删除文件。'
                : '需先启用 MoviePilot 智能助手；当前插件开关偏好会保留，不会因宿主关闭而被改写。' }}
            </p>
          </div>
          <VSwitch v-model="form.allow_machine_translation" label="允许机器或 AI 翻译字幕" inset />
          <VTextField
            v-model.number="form.max_candidate_attempts"
            type="number"
            min="1"
            max="10"
            step="1"
            label="最大候选尝试数"
            :error-messages="attemptsError ? [attemptsError] : []"
            class="attempt-field"
          />

          <div class="priority-columns">
            <div class="priority-group">
              <h4>包内与库存格式优先级</h4>
              <ol class="priority-list">
                <li v-for="(format, index) in formatPriority" :key="format">
                  <span class="priority-index">{{ index + 1 }}</span><strong>{{ format }}</strong><VSpacer />
                  <VTooltip text="上移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0" :aria-label="`上移 ${format}`" @click="move(formatPriority, index, -1)" /></template></VTooltip>
                  <VTooltip text="下移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-arrow-down" size="small" variant="text" :disabled="index === formatPriority.length - 1" :aria-label="`下移 ${format}`" @click="move(formatPriority, index, 1)" /></template></VTooltip>
                </li>
              </ol>
            </div>
            <div class="priority-group">
              <h4>字幕源优先级</h4>
              <ol class="priority-list">
                <li v-for="(source, index) in sourcePriority" :key="source">
                  <span class="priority-index">{{ index + 1 }}</span><strong>{{ sourceLabels[source] }}</strong><VSpacer />
                  <VTooltip text="上移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0" :aria-label="`上移 ${sourceLabels[source]}`" @click="move(sourcePriority, index, -1)" /></template></VTooltip>
                  <VTooltip text="下移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-arrow-down" size="small" variant="text" :disabled="index === sourcePriority.length - 1" :aria-label="`下移 ${sourceLabels[source]}`" @click="move(sourcePriority, index, 1)" /></template></VTooltip>
                </li>
              </ol>
            </div>
          </div>
        </div>
      </section>

      <footer class="config-actions">
        <VBtn type="button" variant="text" color="default" :disabled="saving || clearing" @click="emit('close')">取消</VBtn>
        <VBtn type="submit" color="primary" variant="flat" prepend-icon="mdi-content-save" :loading="saving" :disabled="!canSave">保存配置</VBtn>
      </footer>
    </form>

    <ConfirmDialog v-model="clearOpen" :title="clearTitle" :message="clearMessage" confirm-text="确认清除" :loading="clearing" @confirm="confirmClear" />
  </div>
</template>

<style scoped>
.config-shell { width: 100%; color: rgb(var(--v-theme-on-surface)); background: rgba(var(--v-theme-surface), 0.78); }
.config-header { display: flex; min-height: 4rem; align-items: center; justify-content: space-between; gap: 1rem; padding: 0.75rem 1.25rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.config-header h2 { margin: 0; font-size: 1.0625rem; font-weight: 650; letter-spacing: 0; }
.config-header p { margin: 0.125rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.config-form { display: block; }
.config-section { display: grid; grid-template-columns: minmax(12rem, 16rem) minmax(0, 1fr); gap: 2rem; padding: 1.5rem 1.25rem; }
.section-heading { display: flex; align-items: flex-start; gap: 0.625rem; }
.section-heading h3 { margin: 0; font-size: 0.9375rem; font-weight: 650; letter-spacing: 0; }
.section-heading p { margin: 0.25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; line-height: 1.55; }
.section-content { min-width: 0; }
.mapping-settings { display: grid; gap: 0.75rem; }
.mapping-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.mapping-row { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto; align-items: start; gap: 0.625rem; padding: 0.875rem; }
.mapping-row + .mapping-row { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.mapping-arrow { margin-top: 1rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }
.mapping-row > :last-child { margin-top: 0.375rem; }
.mapping-empty { display: flex; min-height: 3.25rem; align-items: center; gap: 0.625rem; padding: 0.75rem; border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; }
.mapping-add { justify-self: start; }
.source-config-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; }
.source-config-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; }
.source-config-header :deep(.v-expansion-panel-title) { grid-row: 1; grid-column: 1 / -1; padding-right: 6.5rem; }
.source-config-toggle { z-index: 1; grid-row: 1; grid-column: 2; margin-right: 3rem; }
.source-config-summary { display: grid; width: 100%; min-width: 0; grid-template-columns: auto minmax(10rem, 1fr) auto; align-items: center; gap: 0.75rem; }
.source-config-summary > div > strong, .source-config-summary > div > span { display: block; }
.source-config-summary > div > strong { font-size: 0.875rem; }
.source-config-summary > div > span { margin-top: 0.125rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.source-body-note { display: flex; align-items: flex-start; gap: 0.625rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.8125rem; line-height: 1.55; }
.credential-fields { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; }
.credential-fields--single { grid-template-columns: minmax(0, 1fr); }
.credential-actions { grid-column: 1 / -1; display: flex; justify-content: flex-end; }
.candidate-settings { display: grid; gap: 1rem; }
.attribution-setting { display: grid; gap: 0.5rem; }
.field-label { color: rgb(var(--v-theme-on-surface)); font-size: 0.8125rem; font-weight: 650; }
.field-hint { max-width: 65ch; margin: 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; line-height: 1.5; }
.attribution-toggle { width: fit-content; max-width: 100%; }
.attribution-toggle :deep(.v-btn) { min-width: 11rem; }
.attempt-field { max-width: 16rem; }
.priority-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }
.priority-group h4 { margin: 0 0 0.5rem; font-size: 0.8125rem; font-weight: 650; }
.priority-list { display: grid; margin: 0; padding: 0; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 0.375rem; list-style: none; }
.priority-list li { display: flex; min-height: 3rem; align-items: center; gap: 0.625rem; padding: 0.25rem 0.5rem 0.25rem 0.75rem; }
.priority-list li + li { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.priority-list strong { font-size: 0.8125rem; }
.priority-index { display: grid; width: 1.5rem; height: 1.5rem; place-items: center; border-radius: 50%; background: rgba(var(--v-theme-on-surface), 0.08); color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: 0.75rem; }
.config-actions { display: flex; position: sticky; z-index: 2; bottom: 0; align-items: center; justify-content: flex-end; gap: 0.75rem; padding: 0.75rem 1.25rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: rgb(var(--v-theme-surface)); }
.config-shell :deep(.v-btn:focus-visible), .config-shell :deep(.v-expansion-panel-title:focus-visible), .config-shell :deep(input:focus-visible) { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
@media (max-width: 959px) { .config-section { grid-template-columns: 1fr; gap: 1rem; } .credential-fields, .priority-columns { grid-template-columns: 1fr; } }
@media (max-width: 47.5rem) { .mapping-row { grid-template-columns: minmax(0, 1fr) auto; } .mapping-row > :nth-child(1), .mapping-row > :nth-child(3) { grid-column: 1; } .mapping-row > :nth-child(2) { grid-column: 1; margin: -0.5rem 0 -0.25rem 1rem; transform: rotate(90deg); } .mapping-row > :nth-child(4) { grid-row: 1 / span 3; grid-column: 2; align-self: center; margin-top: 0; } }
@media (max-width: 37.5rem) { .config-header { padding-inline: 1rem; } .config-header p { display: none; } .config-section { padding: 1.25rem 1rem; } .source-config-summary { grid-template-columns: auto minmax(0, 1fr); } .source-config-summary :deep(.v-chip) { display: none; } .attribution-toggle { display: grid; width: 100%; } .attribution-toggle :deep(.v-btn) { width: 100%; min-width: 0; } .config-actions { padding-inline: 1rem; } }
@media (prefers-reduced-motion: reduce) { .config-shell *, .config-shell *::before, .config-shell *::after { transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; } }
</style>
