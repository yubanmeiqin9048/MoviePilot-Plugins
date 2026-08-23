<script setup lang="ts">
import { computed, inject, onMounted, reactive, ref, watch } from 'vue'
import { useDisplay } from 'vuetify'

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
const { mdAndUp } = useDisplay()

type ExternalSource = 'opensubtitles' | 'assrt'
type GroupKey = 'sources' | 'candidate' | 'attribution' | 'paths'
type Tone = 'error' | 'warning' | 'success' | 'info'

/** 配置分组：桌面端作为左栏导航项，移动端作为状态卡片。 */
const groups: Array<{ key: GroupKey; icon: string; label: string; purpose: string }> = [
  { key: 'sources', icon: 'mdi-database-cog-outline', label: '字幕来源', purpose: '去哪些站点搜字幕，以及按什么顺序搜' },
  { key: 'candidate', icon: 'mdi-filter-cog-outline', label: '候选筛选', purpose: '搜到多个候选时怎么挑、试几次' },
  { key: 'attribution', icon: 'mdi-package-variant-closed-check', label: '字幕归属', purpose: '压缩包里的字幕该算作哪一集' },
  { key: 'paths', icon: 'mdi-map-marker-path', label: '路径映射', purpose: '整理历史里的旧目录换算成当前目录' },
]
const sourceMeta: Array<{ source: SubtitleSource; short: string; icon: string; description: string }> = [
  { source: 'moviepilot', short: 'MoviePilot', icon: 'mdi-server-network', description: '使用 MoviePilot 当前有效的字幕站点，凭据由宿主管理' },
  { source: 'opensubtitles', short: 'OpenSubtitles', icon: 'mdi-closed-caption-outline', description: '使用 OpenSubtitles.com REST API' },
  { source: 'assrt', short: 'ASSRT', icon: 'mdi-subtitles-outline', description: '使用 ASSRT 字幕服务' },
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
const credentialFeedback = reactive<Record<ExternalSource, { message: string; tone: 'success' | 'warning' | 'error' }>>({
  opensubtitles: { message: '', tone: 'success' },
  assrt: { message: '', tone: 'success' },
})
const savingCredential = ref<ExternalSource | null>(null)
const saveError = ref('')
const normalSaveState = ref<'synced' | 'saving' | 'failed'>('synced')
const initialized = ref(false)
const baselineConfig = ref<NonSensitiveConfig | null>(null)
const showApiKey = ref(false)
const showPassword = ref(false)
const showAssrtToken = ref(false)
const clearOpen = ref(false)
const clearSource = ref<ExternalSource | null>(null)
const clearing = ref(false)

// 桌面端左栏一次只展开一个分组；移动端卡片各自折叠，默认全部收起只看状态。
const activeGroup = ref<GroupKey>('sources')
const expandedCard = ref<GroupKey | null>(null)
const openCredential = ref<ExternalSource | null>(null)

const isDesktop = computed(() => mdAndUp.value)
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

/** 每个分组的阻塞原因只在这里算一次，完整措辞只在出问题的那一行展示。 */
const groupErrors = computed<Record<GroupKey, string>>(() => ({
  sources: osError.value || assrtError.value,
  candidate: attemptsError.value,
  attribution: '',
  paths: pathMappingsError.value,
}))
/** 分组的当前状态摘要：桌面端显示在分组标题旁，移动端就是卡片正文。 */
const groupSummary = computed<Record<GroupKey, string>>(() => ({
  sources: `${enabledSourceCount.value}/3 已启用 · 搜索顺序 ${sourcePriority.value.map(item => shortLabel(item)).join(' → ')}`,
  candidate: `最多试 ${form.max_candidate_attempts} 个候选 · ${form.allow_machine_translation ? '允许' : '不允许'}机翻 · 格式 ${formatPriority.value.join(' → ')}`,
  attribution: `${form.package_attribution_strategy === 'trust_package' ? '信任候选包身份' : '交给 MoviePilot 逐个识别'} · AI 接管${form.ai_attribution_takeover_enabled ? '已开启' : '已关闭'}`,
  paths: pathMappings.value.length ? `${pathMappings.value.length} 条映射生效` : '未配置，直接使用整理历史中的原始目标路径',
}))

const enabledSourceCount = computed(() => sourceMeta.filter(meta => form[enabledKey(meta.source)]).length)
const blockedLabels = computed(() => groups.filter(group => groupErrors.value[group.key]).map(group => group.label))
const busy = computed(() => normalSaveState.value === 'saving' || savingCredential.value !== null || clearing.value)
const canSave = computed(() => !clearing.value && !savingCredential.value && blockedLabels.value.length === 0)
const isDirty = computed(() => baselineConfig.value !== null && JSON.stringify(nonSensitiveConfig()) !== JSON.stringify(baselineConfig.value))
/** 保存区只负责指路，不复述完整错误，避免同一状态被重复播报。 */
const saveHint = computed<{ tone: Tone; text: string }>(() => {
  if (normalSaveState.value === 'saving') return { tone: 'info', text: '正在保存……' }
  if (blockedLabels.value.length) return { tone: 'error', text: `${blockedLabels.value.length} 项待修正，见「${blockedLabels.value.join('、')}」` }
  if (savingCredential.value || clearing.value) return { tone: 'info', text: '凭据操作进行中……' }
  if (isDirty.value) return { tone: 'warning', text: '有未保存更改；凭据由来源内的独立操作单独写入' }
  return { tone: 'success', text: '已与 MoviePilot 同步' }
})
const saveIcon = computed(() => {
  if (saveHint.value.tone === 'error') return 'mdi-alert-circle-outline'
  if (saveHint.value.tone === 'warning') return 'mdi-circle-medium'
  if (saveHint.value.tone === 'info') return 'mdi-progress-clock'
  return 'mdi-check-circle-outline'
})
const clearTitle = computed(() => clearSource.value === 'opensubtitles' ? '清除 OpenSubtitles 凭据' : '清除 ASSRT 凭据')
const clearMessage = computed(() => clearSource.value === 'opensubtitles'
  ? '将永久删除 API Key、用户名、密码和当前登录会话，并立即关闭 OpenSubtitles 来源。旧凭据无法恢复。'
  : '将永久删除 ASSRT Token，并立即关闭 ASSRT 来源。旧 Token 无法恢复。')

watch(() => props.initialConfig, applyInitialConfig, { immediate: true, deep: true })
// 视口跨越断点时保持"正在看哪个分组"不变，避免切换后回到默认分组。
watch(isDesktop, desktop => {
  if (desktop && expandedCard.value) activeGroup.value = expandedCard.value
  else if (!desktop) expandedCard.value = activeGroup.value
})
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

function shortLabel(source: SubtitleSource): string {
  return sourceMeta.find(meta => meta.source === source)?.short || sourceLabels[source]
}

function enabledKey(source: SubtitleSource): 'moviepilot_enabled' | 'opensubtitles_enabled' | 'assrt_enabled' {
  return `${source}_enabled` as 'moviepilot_enabled' | 'opensubtitles_enabled' | 'assrt_enabled'
}

function configured(source: SubtitleSource): boolean {
  if (source === 'moviepilot') return true
  return source === 'opensubtitles' ? opensubtitlesConfigured.value : assrtConfigured.value
}

function credentialState(source: SubtitleSource): { tone: Tone; text: string } {
  if (source === 'moviepilot') return { tone: 'info', text: '由 MoviePilot 管理' }
  return configured(source) ? { tone: 'success', text: '凭据已保存' } : { tone: 'warning', text: '凭据未配置' }
}

function sourceValidation(source: SubtitleSource): string {
  if (source === 'opensubtitles') return osError.value
  if (source === 'assrt') return assrtError.value
  return ''
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

function addPathMapping(): void {
  pathMappings.value.push({ source_prefix: '', target_prefix: '' })
}

function removePathMapping(index: number): void {
  pathMappings.value.splice(index, 1)
}

function move<T>(items: T[], index: number, direction: -1 | 1): void {
  const next = index + direction
  if (next < 0 || next >= items.length) return
  const [item] = items.splice(index, 1)
  items.splice(next, 0, item)
}

/** 分组是否处于展开状态：桌面端由左栏选中决定，移动端由卡片折叠决定。 */
function groupOpen(key: GroupKey): boolean {
  return isDesktop.value ? activeGroup.value === key : expandedCard.value === key
}

function selectGroup(key: GroupKey): void {
  if (isDesktop.value) activeGroup.value = key
  else expandedCard.value = expandedCard.value === key ? null : key
}

function toggleCredential(source: ExternalSource): void {
  openCredential.value = openCredential.value === source ? null : source
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
  <!-- 桌面端为左栏导航 + 单分组面板；移动端为顶部操作条 + 分组状态卡片。两者共用同一份字段。 -->
  <div class="cfg" :class="isDesktop ? 'cfg--desk' : 'cfg--cards'">
    <header v-if="!isDesktop" class="cfg-topbar">
      <div class="cfg-master">
        <VSwitch v-model="form.enabled" color="primary" hide-details inset density="compact"
          :aria-label="form.enabled ? '暂停自动处理' : '启用自动处理'" />
        <div>
          <strong>{{ form.enabled ? '自动处理运行中' : '自动处理已暂停' }}</strong>
          <span>只处理插件运行期间新收到的整理完成事件</span>
        </div>
        <VBtn icon="mdi-close" variant="text" size="small" aria-label="关闭设置" :disabled="busy" @click="closeConfig" />
      </div>
      <div class="cfg-topbar__actions">
        <span :class="`cfg-state cfg-state--${saveHint.tone}`"><VIcon :icon="saveIcon" size="16" />{{ saveHint.text }}</span>
        <VBtn variant="text" size="small" :disabled="!isDirty || busy" @click="cancelConfigChanges">取消更改</VBtn>
        <VBtn size="small" prepend-icon="mdi-content-save" :loading="normalSaveState === 'saving'"
          :disabled="!canSave || !isDirty" @click="saveConfig">保存配置</VBtn>
      </div>
    </header>

    <nav v-else class="cfg-rail" aria-label="配置分组导航">
      <div class="cfg-master">
        <VSwitch v-model="form.enabled" color="primary" hide-details inset density="compact"
          :aria-label="form.enabled ? '暂停自动处理' : '启用自动处理'" />
        <div>
          <strong>自动处理</strong>
          <span>{{ form.enabled ? '运行中' : '已暂停' }}</span>
        </div>
      </div>
      <button v-for="group in groups" :key="group.key" type="button" class="cfg-tab"
        :class="{ 'cfg-tab--active': activeGroup === group.key }" :aria-current="activeGroup === group.key"
        @click="selectGroup(group.key)">
        <VIcon :icon="group.icon" size="18" />
        <span>{{ group.label }}</span>
        <span v-if="groupErrors[group.key]" class="cfg-dot" :title="groupErrors[group.key]" />
      </button>
      <div class="cfg-rail__foot">
        <p :class="`cfg-state cfg-state--${saveHint.tone}`"><VIcon :icon="saveIcon" size="16" /><span>{{ saveHint.text }}</span></p>
        <VBtn block prepend-icon="mdi-content-save" :loading="normalSaveState === 'saving'"
          :disabled="!canSave || !isDirty" @click="saveConfig">保存配置</VBtn>
        <VBtn block variant="text" :disabled="!isDirty || busy" @click="cancelConfigChanges">取消更改</VBtn>
        <VBtn block variant="text" :disabled="busy" @click="closeConfig">关闭设置</VBtn>
      </div>
    </nav>

    <div class="cfg-content">
      <VAlert v-if="saveError" type="error" variant="tonal" density="compact" class="cfg-alert" closable
        @click:close="saveError = ''">{{ saveError }}</VAlert>

      <section v-for="group in groups" v-show="isDesktop ? activeGroup === group.key : true" :key="group.key"
        class="cfg-group" :class="{ 'cfg-group--open': groupOpen(group.key), 'cfg-group--error': Boolean(groupErrors[group.key]) }">
        <header v-if="isDesktop" class="cfg-group__head">
          <div><h2>{{ group.label }}</h2><p>{{ group.purpose }}</p></div>
          <span class="cfg-group__summary">{{ groupSummary[group.key] }}</span>
        </header>
        <button v-else type="button" class="cfg-card__head" :aria-expanded="groupOpen(group.key)" @click="selectGroup(group.key)">
          <VIcon :icon="group.icon" size="20" />
          <div class="cfg-card__copy">
            <strong>{{ group.label }}</strong>
            <span class="cfg-card__now">{{ groupSummary[group.key] }}</span>
            <span v-if="groupErrors[group.key] && !groupOpen(group.key)" class="cfg-card__error"><VIcon icon="mdi-alert-circle-outline" size="14" />{{ groupErrors[group.key] }}</span>
          </div>
          <VIcon :icon="groupOpen(group.key) ? 'mdi-chevron-up' : 'mdi-pencil-outline'" size="18" />
        </button>

        <div v-show="groupOpen(group.key)" class="cfg-group__body">
          <template v-if="group.key === 'sources'">
            <div class="src-list">
              <div v-for="meta in sourceMeta" :key="meta.source" class="src-row"
                :class="{ 'src-row--error': Boolean(sourceValidation(meta.source)) }">
                <VIcon :icon="meta.icon" size="20" />
                <div class="src-row__copy"><strong>{{ sourceLabels[meta.source] }}</strong><span>{{ meta.description }}</span></div>
                <VChip :color="credentialState(meta.source).tone" variant="tonal" size="small" label>{{ credentialState(meta.source).text }}</VChip>
                <VSwitch v-model="form[enabledKey(meta.source)]" color="primary" hide-details inset density="compact"
                  :aria-label="`启用 ${meta.short}`" />
                <VBtn v-if="meta.source !== 'moviepilot'" variant="text" size="small"
                  :icon="openCredential === meta.source ? 'mdi-chevron-up' : 'mdi-key-outline'"
                  :aria-label="`${openCredential === meta.source ? '收起' : '编辑'} ${meta.short} 凭据`"
                  @click="toggleCredential(meta.source as ExternalSource)" />

                <div v-if="sourceValidation(meta.source)" class="src-row__error">
                  <VIcon icon="mdi-alert-circle-outline" size="14" />{{ sourceValidation(meta.source) }}
                </div>

                <p v-if="meta.source === 'moviepilot'" class="src-row__note">
                  <VIcon icon="mdi-shield-lock-outline" size="16" />站点身份信息由 MoviePilot 在下载前重新读取，本插件不保存或展示 Cookie。
                </p>

                <div v-if="openCredential === meta.source" class="cred">
                  <VAlert v-if="credentialFeedback[meta.source as ExternalSource].message"
                    :type="credentialFeedback[meta.source as ExternalSource].tone" variant="tonal" density="compact" class="cred__span">
                    {{ credentialFeedback[meta.source as ExternalSource].message }}
                  </VAlert>
                  <p class="cred__note"><VIcon icon="mdi-shield-key-outline" size="16" />已保存的凭据不会回显；留空字段会保留现有值，也可单独清除。</p>
                  <template v-if="meta.source === 'opensubtitles'">
                    <VTextField v-model="credentials.opensubtitles.api_key" label="API Key" :type="showApiKey ? 'text' : 'password'"
                      autocomplete="new-password" placeholder="留空则保留现有值">
                      <template #append-inner>
                        <VBtn :icon="showApiKey ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text"
                          :aria-label="showApiKey ? '隐藏 API Key' : '显示 API Key'" @click="showApiKey = !showApiKey" />
                      </template>
                    </VTextField>
                    <VTextField v-model="credentials.opensubtitles.username" label="用户名" autocomplete="off" placeholder="留空则保留现有值" />
                    <VTextField v-model="credentials.opensubtitles.password" label="密码" :type="showPassword ? 'text' : 'password'"
                      autocomplete="new-password" placeholder="留空则保留现有值">
                      <template #append-inner>
                        <VBtn :icon="showPassword ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text"
                          :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword" />
                      </template>
                    </VTextField>
                  </template>
                  <VTextField v-else v-model="credentials.assrt.token" label="Token" :type="showAssrtToken ? 'text' : 'password'"
                    autocomplete="new-password" placeholder="留空则保留现有值" class="cred__span">
                    <template #append-inner>
                      <VBtn :icon="showAssrtToken ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" size="small" variant="text"
                        :aria-label="showAssrtToken ? '隐藏 Token' : '显示 Token'" @click="showAssrtToken = !showAssrtToken" />
                    </template>
                  </VTextField>
                  <div class="cred__actions">
                    <VBtn variant="text" color="error" prepend-icon="mdi-key-remove"
                      :disabled="!configured(meta.source) || clearing || savingCredential !== null"
                      @click="requestClear(meta.source as ExternalSource)">清除凭据</VBtn>
                    <VBtn variant="tonal" prepend-icon="mdi-content-save-key-outline"
                      :loading="savingCredential === meta.source"
                      :disabled="(meta.source === 'opensubtitles' ? !hasOsUpdate : !hasAssrtUpdate) || savingCredential !== null || clearing"
                      @click="saveSourceCredentials(meta.source as ExternalSource)">单独保存凭据</VBtn>
                  </div>
                </div>
              </div>
            </div>
            <div class="rank">
              <h3>搜索顺序</h3>
              <ol class="rank-list">
                <li v-for="(source, index) in sourcePriority" :key="source">
                  <span class="rank-index">{{ index + 1 }}</span><strong>{{ shortLabel(source) }}</strong>
                  <VChip v-if="!form[enabledKey(source)]" size="x-small" variant="tonal" label>未启用</VChip>
                  <VSpacer />
                  <VBtn icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0"
                    :aria-label="`上移 ${shortLabel(source)}`" @click="move(sourcePriority, index, -1)" />
                  <VBtn icon="mdi-arrow-down" size="small" variant="text" :disabled="index === sourcePriority.length - 1"
                    :aria-label="`下移 ${shortLabel(source)}`" @click="move(sourcePriority, index, 1)" />
                </li>
              </ol>
            </div>
          </template>

          <template v-else-if="group.key === 'candidate'">
            <VTextField v-model.number="form.max_candidate_attempts" type="number" min="1" max="10" step="1"
              label="最大候选尝试数" :error-messages="attemptsError ? [attemptsError] : []" class="narrow" />
            <VSwitch v-model="form.allow_machine_translation" color="primary" label="允许机器或 AI 翻译字幕" hide-details inset />
            <div class="rank">
              <h3>包内与库存格式优先级</h3>
              <ol class="rank-list">
                <li v-for="(format, index) in formatPriority" :key="format">
                  <span class="rank-index">{{ index + 1 }}</span><strong>{{ format }}</strong><VSpacer />
                  <VBtn icon="mdi-arrow-up" size="small" variant="text" :disabled="index === 0"
                    :aria-label="`上移 ${format}`" @click="move(formatPriority, index, -1)" />
                  <VBtn icon="mdi-arrow-down" size="small" variant="text" :disabled="index === formatPriority.length - 1"
                    :aria-label="`下移 ${format}`" @click="move(formatPriority, index, 1)" />
                </li>
              </ol>
            </div>
          </template>

          <template v-else-if="group.key === 'attribution'">
            <div class="choice" role="radiogroup" aria-label="压缩包字幕归属">
              <button type="button" role="radio" :aria-checked="form.package_attribution_strategy === 'trust_package'"
                class="choice-card" :class="{ 'choice-card--active': form.package_attribution_strategy === 'trust_package' }"
                @click="form.package_attribution_strategy = 'trust_package'">
                <VIcon icon="mdi-package-variant-closed-check" size="22" /><strong>信任候选包</strong>
                <span>继承候选目标的媒体身份，仅从包内路径提取季集。</span>
              </button>
              <button type="button" role="radio" :aria-checked="form.package_attribution_strategy === 'host_recognition'"
                class="choice-card" :class="{ 'choice-card--active': form.package_attribution_strategy === 'host_recognition' }"
                @click="form.package_attribution_strategy = 'host_recognition'">
                <VIcon icon="mdi-text-box-search-outline" size="22" /><strong>MoviePilot 识别</strong>
                <span>逐个字幕调用 MoviePilot 媒体识别，并核对媒体 ID。</span>
              </button>
            </div>
            <div class="fallback">
              <VSwitch v-model="form.ai_attribution_takeover_enabled" color="primary" label="字幕归属失败时允许 AI 智能接管"
                hide-details inset :disabled="!hostAiEnabled" />
              <p>{{ hostAiEnabled
                ? '仅在常规字幕归属无法形成确定结论时请求 MoviePilot 当前配置的 LLM；会发送媒体名称、候选名称和包内相对文件名，AI 只提出结构化归属建议，不会直接移动或删除文件。'
                : '需先启用 MoviePilot 智能助手；当前插件开关偏好会保留，不会因宿主关闭而被改写。' }}</p>
            </div>
          </template>

          <template v-else>
            <div v-if="pathMappings.length" class="map-list">
              <div v-for="(mapping, index) in pathMappings" :key="index" class="map-row">
                <VTextField v-model="mapping.source_prefix" label="历史目录前缀" placeholder="/旧挂载/媒体" prepend-inner-icon="mdi-history"
                  :error-messages="pathMappingFieldError(index, 'source_prefix') ? [pathMappingFieldError(index, 'source_prefix')] : []" />
                <VIcon icon="mdi-arrow-right" class="map-arrow" aria-hidden="true" />
                <VTextField v-model="mapping.target_prefix" label="当前目录前缀" placeholder="/当前挂载/媒体" prepend-inner-icon="mdi-folder-outline"
                  :error-messages="pathMappingFieldError(index, 'target_prefix') ? [pathMappingFieldError(index, 'target_prefix')] : []" />
                <VBtn icon="mdi-delete-outline" variant="text" color="error"
                  :aria-label="`删除第 ${index + 1} 条路径映射`" @click="removePathMapping(index)" />
              </div>
            </div>
            <div v-else class="map-empty">
              <VIcon icon="mdi-map-marker-off-outline" size="20" /><span>未配置映射时直接使用整理历史中的原始目标路径。</span>
            </div>
            <VBtn variant="tonal" prepend-icon="mdi-plus" class="map-add" @click="addPathMapping">添加路径映射</VBtn>
          </template>
        </div>
      </section>
    </div>

    <ConfirmDialog v-model="clearOpen" :title="clearTitle" :message="clearMessage" confirm-text="确认清除"
      :loading="clearing" @confirm="confirmClear" />
  </div>
</template>

<style scoped>
.cfg { width: 100%; color: rgb(var(--v-theme-on-surface)); background: rgba(var(--v-theme-surface), .78); }
.cfg--desk { display: grid; align-items: start; gap: 1rem; grid-template-columns: 15rem minmax(0, 1fr); padding: 1rem; }
.cfg--cards { display: grid; gap: .75rem; padding: .75rem; }
.cfg-alert { margin-bottom: .75rem; }

/* 主开关：桌面端在左栏顶部，移动端在顶栏左侧，两处都是同一个 v-model。 */
.cfg-master { display: flex; align-items: center; gap: .55rem; }
.cfg-master strong { display: block; font-size: .8125rem; }
.cfg-master span { display: block; margin-top: .05rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; }
.cfg-master > div { min-width: 0; flex: 1; }
.cfg-state { display: flex; align-items: flex-start; gap: .3rem; margin: 0; font-size: .6875rem; line-height: 1.45; }
.cfg-state--success { color: rgb(var(--v-theme-success)); }
.cfg-state--warning { color: rgb(var(--v-theme-warning)); }
.cfg-state--error { color: rgb(var(--v-theme-error)); }
.cfg-state--info { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); }

/* 桌面端左栏 */
.cfg-rail { position: sticky; top: .5rem; display: grid; gap: .15rem; padding: .6rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .5rem; background: rgb(var(--v-theme-surface)); }
.cfg-rail .cfg-master { padding: .35rem .5rem .7rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.cfg-tab { display: flex; align-items: center; gap: .55rem; padding: .6rem .65rem; border: 0; border-radius: .35rem; background: transparent; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); cursor: pointer; font: inherit; font-size: .8125rem; text-align: left; }
.cfg-tab:first-of-type { margin-top: .5rem; }
.cfg-tab:hover { background: rgba(var(--v-theme-primary), .06); }
.cfg-tab:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.cfg-tab--active { background: rgba(var(--v-theme-primary), .12); color: rgb(var(--v-theme-primary)); font-weight: 650; }
.cfg-tab > span:first-of-type { min-width: 0; flex: 1; }
.cfg-dot { width: .5rem; height: .5rem; flex: 0 0 auto; border-radius: 50%; background: rgb(var(--v-theme-error)); }
.cfg-rail__foot { display: grid; gap: .35rem; margin-top: .6rem; padding-top: .7rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.cfg-rail__foot > .cfg-state { margin-bottom: .2rem; }

/* 移动端顶栏 */
.cfg-topbar { position: sticky; z-index: 2; top: 0; display: grid; gap: .55rem; padding: .7rem .75rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .5rem; background: rgba(var(--v-theme-surface), .97); backdrop-filter: blur(.5rem); }
.cfg-topbar__actions { display: flex; align-items: center; gap: .4rem; }
.cfg-topbar__actions > .cfg-state { min-width: 0; flex: 1; }

/* 分组容器：桌面端是面板，移动端是可折叠卡片 */
.cfg-content { min-width: 0; }
.cfg--cards .cfg-content { display: grid; gap: .6rem; }
.cfg-group { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .5rem; background: rgb(var(--v-theme-surface)); }
.cfg--cards .cfg-group--error { border-color: rgba(var(--v-theme-error), .55); }
.cfg--cards .cfg-group--open { border-color: rgb(var(--v-theme-primary)); }
.cfg-group__head { display: flex; align-items: flex-start; justify-content: space-between; gap: 1rem; padding: 1rem 1.15rem; border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); background: rgba(var(--v-theme-on-surface), .025); }
.cfg-group__head h2 { margin: 0; font-size: .9375rem; font-weight: 700; }
.cfg-group__head p { margin: .15rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; }
.cfg-group__summary { max-width: 22rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; line-height: 1.5; text-align: right; }
.cfg-group__body { display: grid; gap: 1.1rem; padding: 1.15rem; }
.cfg--cards .cfg-group__body { gap: .9rem; padding: .9rem; border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.cfg-card__head { display: grid; width: 100%; grid-template-columns: auto minmax(0, 1fr) auto; align-items: start; gap: .65rem; padding: .85rem .9rem; border: 0; background: transparent; color: inherit; cursor: pointer; font: inherit; text-align: left; }
.cfg-card__head:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.cfg-card__copy { min-width: 0; }
.cfg-card__copy strong { display: block; font-size: .875rem; font-weight: 700; }
.cfg-card__now { display: block; margin-top: .22rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .75rem; line-height: 1.55; }
.cfg-card__error { display: flex; align-items: center; gap: .3rem; margin-top: .35rem; color: rgb(var(--v-theme-error)); font-size: .6875rem; }

/* 字段块：桌面与移动共用同一份 markup */
.src-list, .map-list, .rank-list { overflow: hidden; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .45rem; }
.src-row { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto 2.25rem; align-items: center; gap: .65rem; padding: .75rem .85rem; }
.src-row + .src-row { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.src-row--error { background: rgba(var(--v-theme-error), .05); box-shadow: inset .1875rem 0 0 rgb(var(--v-theme-error)); }
.src-row__copy { min-width: 0; }
.src-row__copy strong { display: block; font-size: .8125rem; }
.src-row__copy span { display: block; margin-top: .1rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; line-height: 1.45; }
.src-row__error { display: flex; align-items: center; gap: .3rem; grid-column: 2 / -1; color: rgb(var(--v-theme-error)); font-size: .6875rem; }
.src-row__note { display: flex; align-items: flex-start; gap: .4rem; grid-column: 2 / -1; margin: .1rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; line-height: 1.5; }
.cred { display: grid; grid-column: 2 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .6rem; margin-top: .3rem; padding: .8rem; border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .4rem; background: rgba(var(--v-theme-on-surface), .025); }
.cred__note, .cred__actions, .cred__span { grid-column: 1 / -1; }
.cred__note { display: flex; align-items: center; gap: .4rem; margin: 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; }
.cred__actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: .5rem; }

.rank h3 { margin: 0 0 .5rem; font-size: .8125rem; font-weight: 650; }
.rank-list { display: grid; margin: 0; padding: 0; list-style: none; }
.rank-list li { display: flex; min-height: 2.75rem; align-items: center; gap: .6rem; padding: .2rem .4rem .2rem .75rem; }
.rank-list li + li { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.rank-list strong { font-size: .8125rem; }
.rank-index { display: grid; width: 1.375rem; height: 1.375rem; flex: 0 0 auto; place-items: center; border-radius: 50%; background: rgba(var(--v-theme-primary), .12); color: rgb(var(--v-theme-primary)); font-size: .6875rem; font-weight: 700; }

.choice { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .7rem; }
.choice-card { display: grid; gap: .3rem; padding: .9rem; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .45rem; background: transparent; color: inherit; cursor: pointer; font: inherit; text-align: left; }
.choice-card:hover { border-color: rgb(var(--v-theme-primary)); }
.choice-card:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }
.choice-card--active { border-color: rgb(var(--v-theme-primary)); background: rgba(var(--v-theme-primary), .07); box-shadow: inset 0 0 0 1px rgb(var(--v-theme-primary)); }
.choice-card strong { font-size: .8125rem; }
.choice-card span { color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; line-height: 1.5; }
.fallback p { margin: .25rem 0 0; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .6875rem; line-height: 1.55; }

.map-row { display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr) auto; align-items: start; gap: .6rem; padding: .85rem; }
.map-row + .map-row { border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); }
.map-arrow { margin-top: 1rem; }
.map-empty { display: flex; min-height: 3.25rem; align-items: center; gap: .6rem; padding: .75rem; border: 1px dashed rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: .45rem; color: rgba(var(--v-theme-on-surface), var(--v-medium-emphasis-opacity)); font-size: .8125rem; }
.map-add { justify-self: start; }
.narrow { max-width: 16rem; }

.cfg :deep(.v-btn:focus-visible), .cfg :deep(input:focus-visible), .cfg button:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: 2px; }

@media (max-width: 74.99rem) {
  .cfg-group__summary { max-width: 16rem; }
}
@media (max-width: 59.99rem) {
  .cred, .choice { grid-template-columns: minmax(0, 1fr); }
  .src-row { grid-template-columns: auto minmax(0, 1fr) auto; }
  .src-row > .v-chip { grid-column: 2; justify-self: start; }
  .src-row > :deep(.v-input) { grid-row: 1; grid-column: 3; justify-self: end; }
  .src-row > .v-btn { grid-row: 2; grid-column: 3; justify-self: end; }
  .map-row { grid-template-columns: minmax(0, 1fr) auto; }
  .map-row > :nth-child(1), .map-row > :nth-child(3) { grid-column: 1; }
  .map-row > :nth-child(2) { grid-column: 1; margin: -.45rem 0; transform: rotate(90deg); }
  .map-row > :nth-child(4) { grid-row: 1 / span 3; grid-column: 2; align-self: center; }
}
@media (prefers-reduced-motion: reduce) {
  .cfg *, .cfg *::before, .cfg *::after { transition-duration: .01ms !important; animation-duration: .01ms !important; }
}
</style>
