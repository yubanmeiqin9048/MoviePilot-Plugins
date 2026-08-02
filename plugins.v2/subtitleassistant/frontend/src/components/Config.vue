<script setup lang="ts">
import { computed, inject, nextTick, onMounted, reactive, ref, watch } from 'vue'

import { clearCredentials, getErrorMessage, savePluginConfig, updateCredentials } from '@/api/client'
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
  close: []
  layout: [layout: { maxWidth: string }]
}>()
const toast = inject<HostToast | null>('moviepilot:toast', null)

type ExternalSource = 'opensubtitles' | 'assrt'
type SourceMeta = { source: SubtitleSource; icon: string; description: string }
type RepairKey = 'runtime' | 'sources' | 'paths' | 'strategy'
type AttentionTone = 'error' | 'warning' | 'success' | 'info'
type AttentionItem = {
  key: RepairKey
  icon: string
  title: string
  detail: string
  label: string
  tone: AttentionTone
}

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
const activeRepair = ref<RepairKey>('sources')
const savingCredential = ref<ExternalSource | null>(null)
const saveError = ref('')
const normalSaveState = ref<'synced' | 'saving' | 'failed'>('synced')
const initialized = ref(false)
const credentialFeedback = reactive<Record<ExternalSource, { message: string; tone: 'success' | 'warning' | 'error' }>>({
  opensubtitles: { message: '', tone: 'success' },
  assrt: { message: '', tone: 'success' },
})
const baselineConfig = ref<NonSensitiveConfig | null>(null)
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
const osError = computed(() => form.opensubtitles_enabled && !opensubtitlesConfigured.value ? '启用前需先单独保存 API Key、用户名和密码。' : '')
const assrtError = computed(() => form.assrt_enabled && !assrtConfigured.value ? '启用前需先单独保存 ASSRT Token。' : '')
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
const canSave = computed(() => !clearing.value && !savingCredential.value && !osError.value && !assrtError.value && !attemptsError.value && !pathMappingsError.value)
const isDirty = computed(() => baselineConfig.value !== null && JSON.stringify(nonSensitiveConfig()) !== JSON.stringify(baselineConfig.value))
const sourceBlockers = computed(() => sourceMeta.flatMap(meta => {
  const error = sourceValidation(meta.source)
  return error ? [error] : []
}))
const attentionItems = computed<AttentionItem[]>(() => {
  const sourceItem: AttentionItem = sourceBlockers.value.length
    ? {
        key: 'sources', icon: 'mdi-key-alert-outline', title: '来源已启用但无法鉴权',
        detail: sourceBlockers.value[0], label: '阻止保存', tone: 'error',
      }
    : !opensubtitlesConfigured.value || !assrtConfigured.value
      ? {
          key: 'sources', icon: 'mdi-key-outline', title: '外部来源尚可完善',
          detail: `${[opensubtitlesConfigured.value, assrtConfigured.value].filter(Boolean).length} / 2 个外部来源已配置；关闭的来源不会影响当前运行。`,
          label: '可选完善', tone: 'warning',
        }
      : {
          key: 'sources', icon: 'mdi-database-check-outline', title: '来源接入完整',
          detail: '所有外部来源均已配置，可按当前开关参与搜索。', label: '正常', tone: 'success',
        }
  const pathsItem: AttentionItem = pathMappingsError.value
    ? { key: 'paths', icon: 'mdi-map-marker-alert-outline', title: '整理历史路径需要修正', detail: pathMappingsError.value, label: '阻止保存', tone: 'error' }
    : { key: 'paths', icon: 'mdi-map-marker-check-outline', title: '整理历史路径可定位', detail: pathMappings.value.length ? `${pathMappings.value.length} 条路径映射已生效。` : '未配置映射时使用整理历史中的原始目标路径。', label: '正常', tone: 'success' }
  const strategyItem: AttentionItem = attemptsError.value
    ? { key: 'strategy', icon: 'mdi-alert-circle-outline', title: '候选尝试次数需要修正', detail: attemptsError.value, label: '阻止保存', tone: 'error' }
    : { key: 'strategy', icon: 'mdi-tune-variant', title: '候选选择偏好', detail: `最多尝试 ${form.max_candidate_attempts} 个候选；${form.allow_machine_translation ? '允许' : '不允许'}机器或 AI 翻译字幕。`, label: '偏好', tone: 'info' }
  const runtimeItem: AttentionItem = form.enabled
    ? { key: 'runtime', icon: 'mdi-play-circle-outline', title: '自动处理正在接收事件', detail: '仅处理插件运行期间新收到的整理完成事件。', label: '正常', tone: 'success' }
    : { key: 'runtime', icon: 'mdi-pause-circle-outline', title: '自动处理已暂停', detail: '可在事件范围中恢复后续新整理事件的处理。', label: '偏好', tone: 'info' }
  return [sourceItem, pathsItem, strategyItem, runtimeItem]
})
const blockingAttentionCount = computed(() => attentionItems.value.filter(item => item.tone === 'error').length)
const activeRepairLabel = computed(() => ({
  sources: '来源接入',
  runtime: '事件范围',
  paths: '路径落点',
  strategy: '候选选择',
})[activeRepair.value])
const saveStatus = computed(() => {
  if (normalSaveState.value === 'saving') return { icon: 'mdi-progress-clock', tone: 'info' as const, title: '正在保存普通配置', detail: '已提交给 MoviePilot，等待最新配置同步。' }
  if (normalSaveState.value === 'failed') return { icon: 'mdi-alert-circle-outline', tone: 'error' as const, title: '普通配置保存失败', detail: saveError.value || '请修正配置后重试；当前编辑内容已保留。' }
  if (!canSave.value) return { icon: 'mdi-alert-circle-outline', tone: 'error' as const, title: '校验阻止保存', detail: sourceBlockers.value[0] || attemptsError.value || pathMappingsError.value || '请完成正在进行的凭据操作。' }
  if (isDirty.value) return { icon: 'mdi-circle-medium', tone: 'warning' as const, title: '普通配置有未保存更改', detail: '凭据由来源内的独立操作写入或清除。' }
  return { icon: 'mdi-check-circle-outline', tone: 'success' as const, title: '普通配置已同步', detail: '凭据由来源内的独立操作写入或清除。' }
})
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
  credentialFeedback.opensubtitles.message = ''
  credentialFeedback.assrt.message = ''
  if (initialized.value && normalSaveState.value === 'saving') {
    normalSaveState.value = 'synced'
  }
  saveError.value = ''
  baselineConfig.value = nonSensitiveConfig()
  initialized.value = true
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

function sourceEnabled(source: SubtitleSource): boolean {
  return form[enabledKey(source)]
}

function credentialState(source: SubtitleSource): string {
  if (source === 'moviepilot') return '由 MoviePilot 管理'
  return configured(source) ? '凭据已保存' : '凭据未配置'
}

function credentialTone(source: SubtitleSource): 'success' | 'warning' | 'info' {
  if (source === 'moviepilot') return 'info'
  return configured(source) ? 'success' : 'warning'
}

function sourceValidation(source: SubtitleSource): string {
  if (source === 'opensubtitles') return osError.value
  if (source === 'assrt') return assrtError.value
  return ''
}

function toggleSource(index: number): void {
  const openIndex = openSources.value.indexOf(index)
  if (openIndex >= 0) openSources.value.splice(openIndex, 1)
  else openSources.value.push(index)
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
  if (normalSaveState.value === 'saving') return
  if (!canSave.value) {
    saveError.value = osError.value || assrtError.value || attemptsError.value || pathMappingsError.value || '请修正配置后再保存。'
    normalSaveState.value = 'failed'
    return
  }
  if (!pluginId.value) {
    saveError.value = '缺少插件实例 ID，无法保存普通配置。'
    normalSaveState.value = 'failed'
    return
  }
  const config = nonSensitiveConfig()
  normalSaveState.value = 'saving'
  try {
    const response = await savePluginConfig(props.api, pluginId.value, config)
    baselineConfig.value = config
    normalSaveState.value = 'synced'
    showNotice(response.message || '普通配置已保存', 'success')
  } catch (requestError) {
    saveError.value = getErrorMessage(requestError, '普通配置保存失败')
    normalSaveState.value = 'failed'
    showNotice(saveError.value, 'error')
  }
}

async function saveSourceCredentials(source: ExternalSource): Promise<void> {
  credentialFeedback[source].message = ''
  if (!pluginId.value) {
    credentialFeedback[source] = { message: '缺少插件实例 ID，无法安全写入凭据。', tone: 'error' }
    return
  }
  if (source === 'opensubtitles' && !opensubtitlesConfigured.value && !osDraftValues.value.every(Boolean)) {
    credentialFeedback[source] = { message: '首次配置需填写 API Key、用户名和密码。', tone: 'error' }
    return
  }
  if (source === 'assrt' && !hasAssrtUpdate.value) {
    credentialFeedback[source] = { message: '请输入 ASSRT Token。', tone: 'error' }
    return
  }

  savingCredential.value = source
  try {
    const payload = source === 'opensubtitles'
      ? Object.fromEntries(Object.entries(credentials.opensubtitles)
        .map(([key, value]) => [key, value.trim()])
        .filter(([, value]) => Boolean(value)))
      : { token: credentials.assrt.token.trim() }
    const response = await updateCredentials(props.api, pluginId.value, source, payload)
    if (source === 'opensubtitles') {
      opensubtitlesConfigured.value = Boolean(response.data?.configured)
      credentials.opensubtitles.api_key = ''
      credentials.opensubtitles.username = ''
      credentials.opensubtitles.password = ''
    } else {
      assrtConfigured.value = Boolean(response.data?.configured)
      credentials.assrt.token = ''
    }
    const configuredNow = source === 'opensubtitles' ? opensubtitlesConfigured.value : assrtConfigured.value
    credentialFeedback[source] = configuredNow
      ? { message: `凭据已单独保存${isDirty.value ? '；普通配置仍有未保存更改。' : '。'}`, tone: 'success' }
      : { message: '凭据已写入，但当前信息仍不完整。', tone: 'warning' }
    showNotice(configuredNow ? '凭据已单独保存' : '凭据信息仍不完整', configuredNow ? 'success' : 'warning')
  } catch (requestError) {
    credentialFeedback[source] = { message: getErrorMessage(requestError, '凭据保存失败，普通配置未受影响。'), tone: 'error' }
  } finally {
    savingCredential.value = null
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

function cancelConfigChanges(): void {
  const baseline = baselineConfig.value
  if (!baseline) return
  form.enabled = baseline.enabled
  form.moviepilot_enabled = baseline.moviepilot_enabled
  form.opensubtitles_enabled = baseline.opensubtitles_enabled
  form.assrt_enabled = baseline.assrt_enabled
  form.allow_machine_translation = baseline.allow_machine_translation
  form.ai_attribution_takeover_enabled = baseline.ai_attribution_takeover_enabled
  form.max_candidate_attempts = baseline.max_candidate_attempts
  form.package_attribution_strategy = baseline.package_attribution_strategy
  sourcePriority.value = [...baseline.source_priority]
  formatPriority.value = [...baseline.format_priority]
  pathMappings.value = baseline.path_mappings.map(item => ({ ...item }))
  clearCredentialDrafts()
  saveError.value = ''
  normalSaveState.value = 'synced'
  showNotice('已恢复上次保存的普通配置', 'success')
}

function closeConfig(): void {
  if (normalSaveState.value === 'saving') return
  if (isDirty.value) cancelConfigChanges()
  emit('close')
}

async function focusRepair(repair: RepairKey): Promise<void> {
  activeRepair.value = repair
  await nextTick()
  const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
  document.getElementById('config-repair')?.scrollIntoView({ behavior, block: 'start' })
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
      if (baselineConfig.value) baselineConfig.value = { ...baselineConfig.value, [enabledKey(source)]: false }
      credentialFeedback[source] = { message: `凭据已清除，来源已关闭${isDirty.value ? '；其他普通配置仍有未保存更改。' : '。'}`, tone: 'success' }
      showNotice('凭据已清除，字幕源已关闭', 'success')
    } else {
      saveError.value = response.message || '凭据已清除且来源已关闭，但开关保存失败，请重试保存普通配置。'
      credentialFeedback[source] = { message: saveError.value, tone: 'warning' }
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
      <div><h2>配置维护</h2><p>先处理影响运行的事项，再确认本次改动。</p></div>
      <VTooltip text="关闭设置"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" icon="mdi-close" variant="text" aria-label="关闭设置" :disabled="normalSaveState === 'saving' || savingCredential !== null || clearing" @click="closeConfig" /></template></VTooltip>
    </header>

    <form class="config-form" @submit.prevent="saveConfig">
      <VAlert v-if="saveError" type="error" variant="tonal" density="compact" class="config-alert" closable @click:close="saveError = ''">{{ saveError }}</VAlert>

      <section class="issue-queue" aria-labelledby="attention-title">
        <div class="queue-heading"><div><h3 id="attention-title">需要关注</h3><p>依据当前配置和正在编辑的内容更新。</p></div><span>{{ blockingAttentionCount ? `${blockingAttentionCount} 项阻止保存` : '没有保存阻塞' }}</span></div>
        <button v-for="item in attentionItems" :key="item.key" type="button" class="queue-row" :class="`queue-row--${item.tone}`" @click="focusRepair(item.key)">
          <VIcon :icon="item.icon" :color="item.tone" /><span class="queue-copy"><strong>{{ item.title }}</strong><small>{{ item.detail }}</small></span><VChip :color="item.tone" size="small" variant="tonal" label>{{ item.label }}</VChip><VIcon icon="mdi-arrow-down" aria-hidden="true" />
        </button>
      </section>

      <section id="config-repair" class="repair-zone" aria-label="配置修复区">
        <div class="repair-toolbar"><span>当前修复焦点</span><strong>{{ activeRepairLabel }}</strong></div>

        <div class="repair-workspace">
          <section v-if="activeRepair === 'sources'" class="repair-content">
            <aside class="repair-context"><VIcon icon="mdi-database-cog-outline" size="28" /><h3>修复来源接入</h3><p>开关随普通配置保存；外部凭据只在所属来源内独立写入或清除。</p><strong>{{ sourceMeta.filter(meta => configured(meta.source)).length }} / 3 可用</strong></aside>
            <div class="repair-fields">
              <VAlert v-if="osError || assrtError" type="error" variant="tonal" density="compact" class="mb-3">{{ [osError, assrtError].filter(Boolean).join(' ') }}</VAlert>
              <div v-for="(meta, index) in sourceMeta" :key="meta.source" class="source-row">
                <VIcon :icon="meta.icon" size="20" />
                <div class="source-copy"><strong>{{ sourceLabels[meta.source] }}</strong><span>{{ meta.description }}</span></div>
                <div class="source-state-chips"><VChip :color="sourceEnabled(meta.source) ? 'success' : 'default'" variant="tonal" size="small" label>{{ sourceEnabled(meta.source) ? '已启用' : '已关闭' }}</VChip><VChip :color="credentialTone(meta.source)" variant="tonal" size="small" label>{{ credentialState(meta.source) }}</VChip></div>
                <VSwitch class="source-toggle" v-model="form[enabledKey(meta.source)]" :aria-label="`启用 ${sourceLabels[meta.source]}`" hide-details inset density="compact" @click.stop />
                <VBtn v-if="meta.source !== 'moviepilot'" class="source-credential-button" type="button" :icon="openSources.includes(index) ? 'mdi-chevron-up' : 'mdi-key-outline'" variant="text" :aria-label="`${openSources.includes(index) ? '收起' : '编辑'} ${sourceLabels[meta.source]} 凭据`" @click="toggleSource(index)" />

                <div v-if="openSources.includes(index)" class="source-detail">
                  <VAlert v-if="sourceValidation(meta.source)" type="error" variant="tonal" density="compact">{{ sourceValidation(meta.source) }}</VAlert>
                  <div v-if="meta.source === 'moviepilot'" class="source-body-note"><VIcon icon="mdi-shield-lock-outline" size="20" /><span>站点身份信息由 MoviePilot 在下载前重新读取，本插件不保存或展示 Cookie。</span></div>
                  <div v-else-if="meta.source === 'opensubtitles'" class="credential-fields">
                    <VAlert v-if="credentialFeedback.opensubtitles.message" :type="credentialFeedback.opensubtitles.tone" variant="tonal" density="compact" class="credential-feedback">{{ credentialFeedback.opensubtitles.message }}</VAlert>
                    <p class="credential-guidance"><VIcon icon="mdi-shield-key-outline" size="18" />已保存凭据不会回显；留空字段会保留现有值，也可单独清除全部凭据。</p>
                    <VTextField v-model="credentials.opensubtitles.api_key" label="API Key" :type="showApiKey ? 'text' : 'password'" autocomplete="new-password" placeholder="留空则保留现有值"><template #append-inner><VBtn type="button" :icon="showApiKey ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text" :aria-label="showApiKey ? '隐藏 API Key' : '显示 API Key'" @click="showApiKey = !showApiKey" /></template></VTextField>
                    <VTextField v-model="credentials.opensubtitles.username" label="用户名" autocomplete="off" placeholder="留空则保留现有值" />
                    <VTextField v-model="credentials.opensubtitles.password" label="密码" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" placeholder="留空则保留现有值"><template #append-inner><VBtn type="button" :icon="showPassword ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword" /></template></VTextField>
                    <div class="credential-actions"><VBtn type="button" variant="text" color="error" prepend-icon="mdi-key-remove" :disabled="!opensubtitlesConfigured || clearing || savingCredential !== null" @click="requestClear('opensubtitles')">清除凭据</VBtn><VBtn type="button" color="primary" variant="tonal" prepend-icon="mdi-content-save-key-outline" :loading="savingCredential === 'opensubtitles'" :disabled="!hasOsUpdate || savingCredential !== null || clearing" @click="saveSourceCredentials('opensubtitles')">单独保存凭据</VBtn></div>
                  </div>
                  <div v-else class="credential-fields credential-fields--single">
                    <VAlert v-if="credentialFeedback.assrt.message" :type="credentialFeedback.assrt.tone" variant="tonal" density="compact" class="credential-feedback">{{ credentialFeedback.assrt.message }}</VAlert>
                    <p class="credential-guidance"><VIcon icon="mdi-shield-key-outline" size="18" />已保存 Token 不会回显；留空会保留现有值，也可单独清除。</p>
                    <VTextField v-model="credentials.assrt.token" label="Token" :type="showAssrtToken ? 'text' : 'password'" autocomplete="new-password" placeholder="留空则保留现有值"><template #append-inner><VBtn type="button" :icon="showAssrtToken ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text" :aria-label="showAssrtToken ? '隐藏 Token' : '显示 Token'" @click="showAssrtToken = !showAssrtToken" /></template></VTextField>
                    <div class="credential-actions"><VBtn type="button" variant="text" color="error" prepend-icon="mdi-key-remove" :disabled="!assrtConfigured || clearing || savingCredential !== null" @click="requestClear('assrt')">清除凭据</VBtn><VBtn type="button" color="primary" variant="tonal" prepend-icon="mdi-content-save-key-outline" :loading="savingCredential === 'assrt'" :disabled="!hasAssrtUpdate || savingCredential !== null || clearing" @click="saveSourceCredentials('assrt')">单独保存凭据</VBtn></div>
                  </div>
                </div>
              </div>
            </div>
          </section>

          <section v-else-if="activeRepair === 'runtime'" class="repair-content"><aside class="repair-context"><VIcon icon="mdi-inbox-arrow-down-outline" size="28" /><h3>调整事件范围</h3><p>自动流程只响应插件运行期间的新整理完成事件，不扫描历史媒体。</p><strong>{{ form.enabled ? '自动处理正在运行' : '自动处理已暂停' }}</strong></aside><div class="repair-fields"><VSwitch v-model="form.enabled" label="启用自动处理" inset /><VAlert type="info" variant="tonal" density="compact" icon="mdi-information-outline">仅处理插件运行期间新收到的整理完成事件；历史媒体不会扫描，未完成任务不会在重启后恢复。</VAlert></div></section>

          <section v-else-if="activeRepair === 'paths'" class="repair-content"><aside class="repair-context"><VIcon icon="mdi-map-marker-path" size="28" /><h3>校正媒体落点</h3><p>将整理历史记录中的旧目录转换为当前媒体目录，不支持通配符或链式映射。</p><strong>{{ pathMappingsError || `${pathMappings.length} 条映射有效` }}</strong></aside><div class="repair-fields mapping-settings"><div v-if="pathMappings.length" class="mapping-list"><div v-for="(mapping, index) in pathMappings" :key="index" class="mapping-row"><VTextField v-model="mapping.source_prefix" label="历史目录前缀" placeholder="/旧挂载/媒体" prepend-inner-icon="mdi-history" :error-messages="pathMappingFieldError(index, 'source_prefix') ? [pathMappingFieldError(index, 'source_prefix')] : []" /><VIcon icon="mdi-arrow-right" class="mapping-arrow" aria-hidden="true" /><VTextField v-model="mapping.target_prefix" label="当前目录前缀" placeholder="/当前挂载/媒体" prepend-inner-icon="mdi-folder-outline" :error-messages="pathMappingFieldError(index, 'target_prefix') ? [pathMappingFieldError(index, 'target_prefix')] : []" /><VTooltip text="删除此路径映射"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" type="button" icon="mdi-delete-outline" variant="text" color="error" :aria-label="`删除第 ${index + 1} 条路径映射`" @click="removePathMapping(index)" /></template></VTooltip></div></div><div v-else class="mapping-empty"><VIcon icon="mdi-map-marker-off-outline" size="20" /><span>未配置映射时直接使用整理历史中的原始目标路径。</span></div><VBtn type="button" variant="tonal" prepend-icon="mdi-plus" class="mapping-add" @click="addPathMapping">添加路径映射</VBtn></div></section>

          <section v-else class="repair-content"><aside class="repair-context"><VIcon icon="mdi-filter-cog-outline" size="28" /><h3>调整候选选择</h3><p>设置字幕归属、AI 接管、翻译、尝试上限以及来源和格式顺序。</p><strong>{{ form.package_attribution_strategy === 'trust_package' ? '信任候选包身份' : '使用 MoviePilot 逐个识别' }}</strong></aside><div class="repair-fields candidate-settings"><div class="attribution-setting"><span id="attribution-strategy-label" class="field-label">压缩包字幕归属</span><VBtnToggle v-model="form.package_attribution_strategy" mandatory divided color="primary" variant="outlined" aria-labelledby="attribution-strategy-label" class="attribution-toggle"><VBtn value="trust_package" prepend-icon="mdi-package-variant-closed-check">信任候选包</VBtn><VBtn value="host_recognition" prepend-icon="mdi-text-box-search-outline">MoviePilot 识别</VBtn></VBtnToggle><p class="field-hint">{{ form.package_attribution_strategy === 'trust_package' ? '继承候选目标的媒体身份，仅从包内路径提取季集。' : '逐个字幕调用 MoviePilot 媒体识别，并核对媒体 ID。' }}</p></div><div><VSwitch v-model="form.ai_attribution_takeover_enabled" label="字幕归属失败时允许 AI 智能接管" inset :disabled="!hostAiEnabled" /><p class="field-hint">{{ hostAiEnabled ? '仅在常规字幕归属无法形成确定结论时请求 MoviePilot 当前配置的 LLM；会发送媒体名称、候选名称和包内相对文件名，AI 只提出结构化归属建议，不会直接移动或删除文件。' : '需先启用 MoviePilot 智能助手；当前插件开关偏好会保留，不会因宿主关闭而被改写。' }}</p></div><VSwitch v-model="form.allow_machine_translation" label="允许机器或 AI 翻译字幕" inset /><VTextField v-model.number="form.max_candidate_attempts" type="number" min="1" max="10" step="1" label="最大候选尝试数" :error-messages="attemptsError ? [attemptsError] : []" class="attempt-field" /><div class="priority-columns"><div class="priority-group"><h4>包内与库存格式优先级</h4><ol class="priority-list"><li v-for="(format, index) in formatPriority" :key="format"><span class="priority-index">{{ index + 1 }}</span><strong>{{ format }}</strong><VSpacer /><VTooltip text="上移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" type="button" icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0" :aria-label="`上移 ${format}`" @click="move(formatPriority, index, -1)" /></template></VTooltip><VTooltip text="下移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" type="button" icon="mdi-arrow-down" size="small" variant="text" :disabled="index === formatPriority.length - 1" :aria-label="`下移 ${format}`" @click="move(formatPriority, index, 1)" /></template></VTooltip></li></ol></div><div class="priority-group"><h4>字幕源优先级</h4><ol class="priority-list"><li v-for="(source, index) in sourcePriority" :key="source"><span class="priority-index">{{ index + 1 }}</span><strong>{{ sourceLabels[source] }}</strong><VSpacer /><VTooltip text="上移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" type="button" icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0" :aria-label="`上移 ${sourceLabels[source]}`" @click="move(sourcePriority, index, -1)" /></template></VTooltip><VTooltip text="下移"><template #activator="{ props: tooltipProps }"><VBtn v-bind="tooltipProps" type="button" icon="mdi-arrow-down" size="small" variant="text" :disabled="index === sourcePriority.length - 1" :aria-label="`下移 ${sourceLabels[source]}`" @click="move(sourcePriority, index, 1)" /></template></VTooltip></li></ol></div></div></div></section>
        </div>
      </section>

      <section class="current-settings" aria-labelledby="current-settings-title"><h3 id="current-settings-title">当前设置</h3><div><span><VIcon icon="mdi-power" />{{ form.enabled ? '自动处理已启用' : '自动处理已暂停' }}</span><span><VIcon icon="mdi-database-outline" />{{ sourceMeta.filter(meta => sourceEnabled(meta.source)).length }} 个来源已启用</span><span><VIcon icon="mdi-map-marker-path" />{{ pathMappings.length }} 条路径映射</span><span><VIcon icon="mdi-tune-variant" />最多尝试 {{ form.max_candidate_attempts }} 个候选</span></div></section>

      <footer class="save-dock"><div><VIcon :icon="saveStatus.icon" :color="saveStatus.tone" /><span><strong>{{ saveStatus.title }}</strong><small>{{ saveStatus.detail }}</small></span></div><VBtn type="button" variant="text" :disabled="!isDirty || clearing || savingCredential !== null || normalSaveState === 'saving'" @click="cancelConfigChanges">取消更改</VBtn><VBtn type="submit" color="primary" prepend-icon="mdi-content-save" :loading="normalSaveState === 'saving'" :disabled="!canSave || !isDirty">保存配置</VBtn></footer>
    </form>

    <ConfirmDialog v-model="clearOpen" :title="clearTitle" :message="clearMessage" confirm-text="确认清除" :loading="clearing" @confirm="confirmClear" />
  </div>
</template>

<style scoped>
.config-shell { width: 100%; color: rgb(var(--v-theme-on-surface)); background: rgba(var(--v-theme-surface), .78); }
.config-header, .queue-heading, .repair-toolbar, .save-dock { display: flex; align-items: center; justify-content: space-between; gap: 1rem; }
.config-header { min-height: 4rem; padding: .75rem 1.25rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.config-header h2, .queue-heading h3, .repair-context h3, .current-settings h3 { margin: 0; font-size: .9375rem; font-weight: 650; letter-spacing: 0; }
.config-header p, .queue-heading p, .queue-copy small, .repair-context p, .source-copy span, .field-hint, .credential-guidance, .save-dock small { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; line-height: 1.5; }
.config-header p, .queue-heading p, .field-hint, .credential-guidance, .repair-context p { margin: .2rem 0 0; }
.config-alert { margin: 1rem 1.25rem 0; }
.config-form { display: grid; gap: 1rem; padding: 1rem 1.25rem 0; }
.issue-queue, .repair-zone, .current-settings, .save-dock { border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .375rem; background: rgb(var(--v-theme-surface)); }
.queue-heading { padding: .9rem 1rem; }.queue-heading > span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; }
.queue-row { display: grid; width: 100%; min-height: 4rem; grid-template-columns: auto minmax(0, 1fr) auto auto; align-items: center; gap: .75rem; padding: .7rem 1rem; border: 0; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }.queue-row:hover, .queue-row:focus-visible { background: rgba(var(--v-theme-primary), .06); }.queue-copy { min-width: 0; }.queue-copy strong, .queue-copy small { display: block; }.queue-copy strong { font-size: .8125rem; }
.repair-toolbar { min-height: 3.5rem; padding: .65rem 1rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }.repair-toolbar > span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; }.repair-toolbar > strong { font-size: .8125rem; }
.repair-content { display: grid; grid-template-columns: 16rem minmax(0, 1fr); }.repair-context { padding: 1.25rem; border-inline-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: rgba(var(--v-theme-on-surface), .035); }.repair-context h3 { margin-top: .75rem; }.repair-context strong { font-size: .75rem; }.repair-fields { min-width: 0; padding: 1.25rem; }
.source-row { display: grid; grid-template-columns: auto minmax(9rem, 1fr) auto auto 2.5rem; align-items: center; gap: .7rem; padding: .7rem 0; }.source-row + .source-row { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }.source-copy { min-width: 0; }.source-copy strong, .source-copy span { display: block; }.source-copy strong { font-size: .8125rem; }.source-state-chips { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .35rem; }.source-detail { display: grid; grid-column: 2 / -1; gap: .75rem; padding: .55rem 0 .25rem; }.source-body-note { display: flex; align-items: flex-start; gap: .625rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .8125rem; line-height: 1.55; }
.credential-fields { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .75rem; }.credential-fields--single { grid-template-columns: minmax(0, 1fr); }.credential-feedback, .credential-guidance, .credential-actions { grid-column: 1 / -1; }.credential-guidance { display: flex; align-items: flex-start; gap: .5rem; }.credential-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .5rem; }
.mapping-settings, .candidate-settings, .attribution-setting { display: grid; gap: .75rem; }.mapping-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .375rem; }.mapping-row { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto; align-items: start; gap: .625rem; padding: .875rem; }.mapping-row + .mapping-row { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }.mapping-arrow { margin-top: 1rem; }.mapping-empty { display: flex; min-height: 3.25rem; align-items: center; gap: .625rem; padding: .75rem; border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .375rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .8125rem; }.mapping-add { justify-self: start; }
.field-label { font-size: .8125rem; font-weight: 650; }.attribution-toggle { width: fit-content; max-width: 100%; }.attribution-toggle :deep(.v-btn) { min-width: 11rem; }.attempt-field { max-width: 16rem; }.priority-columns { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1.25rem; }.priority-group h4 { margin: 0 0 .5rem; font-size: .8125rem; }.priority-list { display: grid; margin: 0; padding: 0; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .375rem; list-style: none; }.priority-list li { display: flex; min-height: 3rem; align-items: center; gap: .625rem; padding: .25rem .5rem .25rem .75rem; }.priority-list li + li { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }.priority-list strong { font-size: .8125rem; }.priority-index { display: grid; width: 1.5rem; height: 1.5rem; place-items: center; border-radius: 50%; background: rgba(var(--v-theme-on-surface), .08); font-size: .75rem; }
.current-settings { padding: .9rem 1rem; }.current-settings > div { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .5rem; margin-top: .75rem; }.current-settings span { display: flex; min-width: 0; align-items: center; gap: .4rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; }
.save-dock { position: sticky; z-index: 2; bottom: 0; min-height: 4rem; margin-bottom: 0; padding: .65rem 1rem; box-shadow: 0 .5rem 1.5rem rgba(0, 0, 0, .16); }.save-dock > div { display: flex; min-width: 0; align-items: center; gap: .55rem; margin-right: auto; }.save-dock strong, .save-dock small { display: block; }.save-dock strong { font-size: .75rem; }.save-dock small { overflow: hidden; margin-top: .1rem; text-overflow: ellipsis; white-space: nowrap; }
.config-shell :deep(.v-btn:focus-visible), .config-shell :deep(input:focus-visible), .config-shell button:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
@media (max-width: 59.99rem) { .repair-content { grid-template-columns: 12rem minmax(0, 1fr); }.credential-fields, .priority-columns { grid-template-columns: 1fr; }.current-settings > div { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 47.99rem) { .config-header, .config-form { padding-inline: 1rem; }.config-form { gap: .75rem; }.queue-heading { align-items: stretch; flex-direction: column; }.repair-content { grid-template-columns: 1fr; }.repair-context { border-inline-end: 0; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }.source-row { grid-template-columns: auto minmax(0, 1fr) auto; }.source-state-chips { grid-column: 2; justify-content: flex-start; }.source-row > :deep(.source-toggle) { grid-row: 1; grid-column: 3; justify-self: end; }.source-row > :deep(.source-credential-button) { grid-row: 2; grid-column: 3; justify-self: end; }.source-detail { grid-column: 1 / -1; }.queue-row { grid-template-columns: auto minmax(0, 1fr) auto; }.queue-row > .v-chip { grid-column: 2; justify-self: start; }.queue-row > .v-icon:last-child { grid-row: 1 / span 2; grid-column: 3; }.mapping-row { grid-template-columns: minmax(0, 1fr) auto; }.mapping-row > :nth-child(1), .mapping-row > :nth-child(3) { grid-column: 1; }.mapping-row > :nth-child(2) { grid-column: 1; margin: -.5rem 0; transform: rotate(90deg); }.mapping-row > :nth-child(4) { grid-row: 1 / span 3; grid-column: 2; align-self: center; }.attribution-toggle { display: grid; width: 100%; }.attribution-toggle :deep(.v-btn) { width: 100%; min-width: 0; }.current-settings > div { grid-template-columns: 1fr; }.save-dock { position: static; align-items: stretch; flex-wrap: wrap; margin-bottom: 1rem; }.save-dock > div { width: 100%; margin: 0; }.save-dock > .v-btn { flex: 1; } }
@media (max-width: 29rem) { .credential-actions { align-items: stretch; flex-direction: column; }.credential-actions .v-btn { width: 100%; }.save-dock small { white-space: normal; } }
@media (prefers-reduced-motion: reduce) { .config-shell *, .config-shell *::before, .config-shell *::after { scroll-behavior: auto !important; transition-duration: .01ms !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; } }
</style>
