<script setup lang="ts">
import { DEFAULT_FILE_AGE_POLICY } from '@/fileAgePolicy'
import { computed, inject, onMounted, onUnmounted, ref, watch } from 'vue'

import { getErrorMessage, readConfiguration, saveConfiguration } from '@/api'
import { configurationEditorState } from '@/configState'
import type { ConfigModel, DirectoryMapping, Notices, OpenListInstance, PluginApi } from '@/types'

const DEFAULT_NOTICES: Notices = {
  admin_token: '仅支持 OpenList 管理员 API Token，不支持普通用户 Token、账号或密码。',
  root: 'OpenList 管理员根目录必须为 /；插件不探测或换算用户根目录。',
  sign: '请关闭 OpenList 的 sign；插件不探测、生成或续签 sign。',
  output_directory: '完整扫描成功后，会清理该目录中不再需要的受管文件，包括手工放入的文件；请使用专用输出目录。',
}

const LIST_DELIMITERS = [',', '，', ';', '；']
type HostToast = {
  success(message: string): unknown
  error(message: string): unknown
}

const props = withDefaults(defineProps<{
  initialConfig?: Partial<ConfigModel>
  api: PluginApi
  pluginId?: string
}>(), {
  initialConfig: () => ({}),
  pluginId: '',
})

const emit = defineEmits<{
  close: []
  layout: [layout: { maxWidth: string }]
}>()

const toast = inject<HostToast | null>('moviepilot:toast', null)
const draft = ref<ConfigModel>(emptyModel())
const baseline = ref('')
const saveError = ref('')
const saving = ref(false)
let configRequestSequence = 0

/** 当前视图：某个实例或通用设置。 */
const view = ref<'instance' | 'general'>('instance')
const activeIndex = ref(0)
const revealToken = ref(false)
const pendingDelete = ref<number | null>(null)

const pluginId = computed(() => props.pluginId.trim() || props.initialConfig?.plugin_id?.trim() || '')
const dirty = computed(() => serialize(toPayload(draft.value)) !== baseline.value)
const notices = computed(() => ({ ...DEFAULT_NOTICES, ...(draft.value.notices || {}) }))
const outputConflicts = computed(() => findOutputConflicts(draft.value.instances))
const pruningError = computed(() => {
  const days = draft.value.active_recheck_days
  if (!Number.isInteger(days) || days < 1) return '天数必须为正整数'
  const policy = draft.value.file_age_policy
  if (Object.values(policy).some(value => !Number.isInteger(value) || value <= 0)) return '天数必须为正整数'
  if (!(policy.recent_days < policy.cold_after_days && policy.cold_after_days < policy.archive_after_days)) return '年龄阈值必须满足：近期窗口 < 冷目录阈值 < 长期静态阈值'
  if (!(days <= policy.stale_recheck_days && policy.stale_recheck_days <= policy.cold_recheck_days && policy.cold_recheck_days <= policy.archive_recheck_days)) return '复查间隔必须满足：近期目录 ≤ 陈旧目录 ≤ 冷目录 ≤ 长期静态目录'
  return ''
})
const canSave = computed(() => !saving.value && dirty.value && !outputConflicts.value.size && !pruningError.value)
const activeInstance = computed(() => draft.value.instances[activeIndex.value] ?? null)
const mappingTotal = computed(() => draft.value.instances.reduce((sum, item) => sum + item.mappings.length, 0))
const confirmOpen = computed({
  get: () => pendingDelete.value !== null,
  set: value => { if (!value) pendingDelete.value = null },
})

watch(() => props.initialConfig, async value => {
  const requestSequence = ++configRequestSequence
  const next = normalizeConfig(value)
  draft.value = next
  baseline.value = serialize(toPayload(next))
  saveError.value = ''
  configurationEditorState.dirty = false
  clampActive()
  if (!pluginId.value) return
  try {
    // 宿主用持久化配置浅覆盖初始模型，会丢失后缀与 Token 状态等展示字段。
    const response = await readConfiguration(props.api, pluginId.value)
    if (requestSequence !== configRequestSequence || dirty.value || saving.value) return
    const current = normalizeConfig(response.data)
    draft.value = current
    baseline.value = serialize(toPayload(current))
    configurationEditorState.dirty = false
    clampActive()
  } catch (error) {
    if (requestSequence !== configRequestSequence || dirty.value || saving.value) return
    saveError.value = getErrorMessage(error, '配置读取失败，请关闭后重新打开设置。')
  }
}, { immediate: true, deep: true })

watch(draft, () => {
  configurationEditorState.dirty = dirty.value
}, { deep: true })

watch([view, activeIndex], () => { revealToken.value = false })

onMounted(() => emit('layout', { maxWidth: '68rem' }))
onUnmounted(() => { configRequestSequence += 1 })

function emptyModel(): ConfigModel {
  return {
    instances: [],
    schedule_enabled: false,
    active_recheck_days: 1,
    file_age_policy: { ...DEFAULT_FILE_AGE_POLICY },
    history_retention_days: 90,
    notices: { ...DEFAULT_NOTICES },
  }
}

function normalizeConfig(value?: Partial<ConfigModel>): ConfigModel {
  const input = (value || {}) as Record<string, unknown>
  const rawInstances = Array.isArray(input.instances) ? input.instances : []
  return {
    instances: rawInstances.map((item, instanceIndex) => normalizeInstance(item, instanceIndex)),
    schedule_enabled: input.schedule_enabled === true,
    file_age_policy: { ...DEFAULT_FILE_AGE_POLICY, ...(value?.file_age_policy || {}) },
    active_recheck_days: typeof input.active_recheck_days === 'number' ? input.active_recheck_days : 1,
    history_retention_days: Number.isInteger(Number(input.history_retention_days)) && Number(input.history_retention_days) > 0
      ? Number(input.history_retention_days)
      : 90,
    notices: typeof input.notices === 'object' && input.notices !== null
      ? { ...DEFAULT_NOTICES, ...(input.notices as Partial<Notices>) }
      : { ...DEFAULT_NOTICES },
  }
}

function normalizeInstance(value: unknown, index: number): OpenListInstance {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const rawMappings = Array.isArray(input.mappings) ? input.mappings : []
  return {
    id: typeof input.id === 'string' ? input.id : `instance-${index + 1}`,
    name: typeof input.name === 'string' ? input.name : '',
    scan_url: typeof input.scan_url === 'string' ? input.scan_url : '',
    api_token: typeof input.api_token === 'string' ? input.api_token : '',
    api_token_configured: input.api_token_configured === true,
    playback_url: typeof input.playback_url === 'string' ? input.playback_url : '',
    mappings: rawMappings.map(normalizeMapping),
  }
}

function normalizeMapping(value: unknown): DirectoryMapping {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  return {
    source_dir: typeof input.source_dir === 'string' ? input.source_dir : '',
    output_dir: typeof input.output_dir === 'string' ? input.output_dir : '',
    whitelist_mode: input.whitelist_mode === 'custom' ? 'custom' : 'follow_host',
    extensions: normalizeLines(input.extensions),
    effective_extensions: normalizeLines(input.effective_extensions),
    exclude_keywords: normalizeLines(input.exclude_keywords),
    output_directory_warning: typeof input.output_directory_warning === 'string'
      ? input.output_directory_warning
      : DEFAULT_NOTICES.output_directory,
  }
}

function normalizeLines(value: unknown): string[] {
  const items = typeof value === 'string'
    ? value.split(/[,\n]/)
    : Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
  return [...new Set(items.map(item => item.trim()).filter(Boolean))]
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function serialize(value: unknown): string {
  return JSON.stringify(value)
}

function toPayload(value: ConfigModel): ConfigModel {
  const payload = clone(value)
  delete payload.notices
  payload.instances = payload.instances.map(instance => ({
    ...instance,
    api_token_configured: undefined,
    mappings: instance.mappings.map(mapping => ({
      source_dir: mapping.source_dir.trim(),
      output_dir: mapping.output_dir.trim(),
      whitelist_mode: mapping.whitelist_mode,
      extensions: [...mapping.extensions],
      exclude_keywords: [...mapping.exclude_keywords],
    })),
  }))
  return payload
}

/** 为映射对象分配稳定的渲染 key，删除中间项时不复用输入框状态。 */
const renderKeys = new WeakMap<object, string>()
let renderSeq = 0
function keyOf(item: object): string {
  let key = renderKeys.get(item)
  if (!key) {
    key = `m${++renderSeq}`
    renderKeys.set(item, key)
  }
  return key
}

function clampActive(): void {
  const count = draft.value.instances.length
  activeIndex.value = count ? Math.min(Math.max(activeIndex.value, 0), count - 1) : 0
}

function instanceLabel(instance: OpenListInstance, index: number): string {
  return instance.name.trim() || `实例 ${index + 1}`
}

function instanceHasIssue(instance: OpenListInstance, index: number): boolean {
  if (!instance.scan_url.trim()) return true
  if (!instance.api_token?.trim() && !instance.api_token_configured) return true
  return instance.mappings.some((mapping, mappingIndex) =>
    !mapping.source_dir.trim() || !mapping.output_dir.trim() || outputConflicts.value.has(conflictKey(index, mappingIndex)))
}

function selectInstance(index: number): void {
  view.value = 'instance'
  activeIndex.value = index
}

function addInstance(): void {
  draft.value.instances.push({
    id: `instance-${Date.now()}`,
    name: '',
    scan_url: '',
    api_token: '',
    playback_url: '',
    mappings: [],
  })
  selectInstance(draft.value.instances.length - 1)
}

function confirmRemoveInstance(): void {
  if (pendingDelete.value === null) return
  draft.value.instances.splice(pendingDelete.value, 1)
  pendingDelete.value = null
  clampActive()
}

function addMapping(instance: OpenListInstance): void {
  instance.mappings.push({
    source_dir: '',
    output_dir: '',
    whitelist_mode: 'follow_host',
    extensions: [],
    effective_extensions: [],
    exclude_keywords: [],
    output_directory_warning: DEFAULT_NOTICES.output_directory,
  })
}

function removeMapping(instance: OpenListInstance, index: number): void {
  instance.mappings.splice(index, 1)
}

function setList(mapping: DirectoryMapping, field: 'extensions' | 'exclude_keywords', value: unknown): void {
  mapping[field] = normalizeLines(value)
}

function required(value: string): true | string {
  return !!value?.trim() || '必填'
}

function comparablePath(value: string): string {
  const normalized = value.trim().replaceAll('\\', '/').replace(/\/+$/, '')
  return normalized || '/'
}

function overlaps(first: string, second: string): boolean {
  const left = comparablePath(first)
  const right = comparablePath(second)
  return left === right || right.startsWith(`${left}/`) || left.startsWith(`${right}/`)
}

function conflictKey(instanceIndex: number, mappingIndex: number): string {
  return `${instanceIndex}:${mappingIndex}`
}

/** 返回“实例:映射”到冲突说明的映射表，用于字段级报错。 */
function findOutputConflicts(instances: OpenListInstance[]): Map<string, string[]> {
  const entries: Array<{ key: string; label: string; output: string }> = []
  instances.forEach((instance, instanceIndex) => instance.mappings.forEach((mapping, mappingIndex) => {
    if (mapping.output_dir.trim()) {
      entries.push({
        key: conflictKey(instanceIndex, mappingIndex),
        label: `${instanceLabel(instance, instanceIndex)} #${mappingIndex + 1}`,
        output: mapping.output_dir,
      })
    }
  }))
  const conflicts = new Map<string, string[]>()
  const push = (key: string, message: string) => conflicts.set(key, [...(conflicts.get(key) || []), message])
  entries.forEach((first, firstIndex) => entries.slice(firstIndex + 1).forEach(second => {
    if (overlaps(first.output, second.output)) {
      push(first.key, `与 ${second.label} 的输出目录重叠`)
      push(second.key, `与 ${first.label} 的输出目录重叠`)
    }
  }))
  return conflicts
}

async function save(): Promise<void> {
  saveError.value = ''
  if (pruningError.value) {
    saveError.value = pruningError.value
    return
  }
  if (outputConflicts.value.size) {
    saveError.value = '存在重叠的输出目录，请先修正。'
    return
  }
  if (!pluginId.value) {
    saveError.value = '缺少插件实例 ID，无法保存配置。'
    return
  }
  configRequestSequence += 1
  saving.value = true
  const payload = toPayload(draft.value)
  try {
    const response = await saveConfiguration(props.api, pluginId.value, payload)
    const returned = response?.data ? normalizeConfig(response.data) : draft.value
    draft.value = returned
    baseline.value = serialize(toPayload(returned))
    configurationEditorState.dirty = false
    clampActive()
    toast?.success(response.message || '配置已保存。')
  } catch (error) {
    // 不改动 draft/baseline；管理员可以直接修正或重试。
    saveError.value = getErrorMessage(error, '配置保存失败，当前编辑内容已保留。')
    toast?.error(saveError.value)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="a2s-config">
    <!-- 顶栏 -->
    <header class="a2s-head">
      <div class="a2s-head-text">
        <div class="a2s-head-title">Alist2Strm 设置</div>
        <div class="a2s-head-sub">{{ draft.instances.length }} 个实例 · {{ mappingTotal }} 条映射</div>
      </div>
      <VChip v-if="dirty" size="small" color="warning" variant="tonal" label>未保存</VChip>
      <VBtn icon="mdi-close" size="small" variant="text" aria-label="关闭" @click="emit('close')" />
    </header>
    <VDivider />

    <div class="a2s-body">
      <!-- 分区导航：宽屏为侧栏，窄屏为横向滚动条 -->
      <nav class="a2s-nav" aria-label="设置分区">
        <div class="a2s-nav-label">OpenList 实例</div>
        <button
          v-for="(instance, index) in draft.instances"
          :key="instance.id"
          type="button"
          class="a2s-nav-item"
          :class="{ 'a2s-nav-item--active': view === 'instance' && activeIndex === index }"
          :aria-current="view === 'instance' && activeIndex === index ? 'page' : undefined"
          @click="selectInstance(index)"
        >
          <VIcon size="18" icon="mdi-server-network-outline" class="a2s-nav-icon" />
          <span class="a2s-nav-name">{{ instanceLabel(instance, index) }}</span>
          <VIcon
            v-if="instanceHasIssue(instance, index)"
            size="16"
            icon="mdi-alert-circle"
            color="warning"
            aria-label="配置不完整"
          />
          <span v-else class="a2s-nav-count">{{ instance.mappings.length }}</span>
        </button>
        <button type="button" class="a2s-nav-item a2s-nav-item--add" @click="addInstance">
          <VIcon size="18" icon="mdi-plus" class="a2s-nav-icon" />
          <span class="a2s-nav-name">添加实例</span>
        </button>

        <div class="a2s-nav-label a2s-nav-label--gap">通用</div>
        <button
          type="button"
          class="a2s-nav-item"
          :class="{ 'a2s-nav-item--active': view === 'general' }"
          :aria-current="view === 'general' ? 'page' : undefined"
          @click="view = 'general'"
        >
          <VIcon size="18" icon="mdi-cog-outline" class="a2s-nav-icon" />
          <span class="a2s-nav-name">通用设置</span>
          <VIcon v-if="draft.schedule_enabled" size="10" icon="mdi-circle" color="success" aria-label="定时已启用" />
        </button>
      </nav>

      <main class="a2s-main">
        <!-- 实例编辑 -->
        <template v-if="view === 'instance'">
          <div v-if="!activeInstance" class="a2s-empty">
            <VIcon size="48" icon="mdi-server-plus-outline" color="medium-emphasis" />
            <div class="a2s-empty-title">还没有 OpenList 实例</div>
            <div class="a2s-empty-text">添加一个实例，再为它配置目录映射。</div>
            <VBtn color="primary" variant="flat" prepend-icon="mdi-plus" @click="addInstance">添加实例</VBtn>
          </div>

          <template v-else>
            <section class="a2s-section">
              <div class="a2s-section-head">
                <div>
                  <h3 class="a2s-section-title">连接</h3>
                  <p class="a2s-section-desc">扫描通过管理员 API 访问，播放链接可使用独立的外部地址。</p>
                </div>
                <VBtn
                  variant="text"
                  color="error"
                  size="small"
                  prepend-icon="mdi-delete-outline"
                  @click="pendingDelete = activeIndex"
                >删除实例</VBtn>
              </div>

              <div class="a2s-grid">
                <VTextField
                  v-model="activeInstance.name"
                  label="名称"
                  :placeholder="`实例 ${activeIndex + 1}`"
                  hide-details="auto"
                />
                <VTextField
                  v-model="activeInstance.scan_url"
                  label="扫描地址"
                  placeholder="http://openlist:5244"
                  :rules="[required]"
                  hide-details="auto"
                />
                <VTextField
                  v-model="activeInstance.api_token"
                  class="a2s-span-2"
                  label="管理员 API Token"
                  :type="revealToken ? 'text' : 'password'"
                  autocomplete="new-password"
                  :placeholder="activeInstance.api_token_configured ? '已配置，留空保持不变' : '粘贴管理员 Token'"
                  :append-inner-icon="revealToken ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
                  hide-details="auto"
                  @click:append-inner="revealToken = !revealToken"
                >
                  <template v-if="activeInstance.api_token_configured && !activeInstance.api_token" #prepend-inner>
                    <VIcon size="18" icon="mdi-check-circle" color="success" />
                  </template>
                </VTextField>
                <VTextField
                  v-model="activeInstance.playback_url"
                  class="a2s-span-2"
                  label="播放地址"
                  placeholder="留空时使用扫描地址"
                  hint="写入 STRM 的地址前缀，供媒体服务器访问"
                  persistent-hint
                  hide-details="auto"
                />
              </div>

              <div class="a2s-requirements">
                <div class="a2s-requirements-title">
                  <VIcon size="16" icon="mdi-information-outline" />
                  OpenList 侧要求
                </div>
                <ul>
                  <li>{{ notices.admin_token }}</li>
                  <li>{{ notices.root }}</li>
                  <li>{{ notices.sign }}</li>
                </ul>
              </div>
            </section>

            <section class="a2s-section">
              <div class="a2s-section-head">
                <div>
                  <h3 class="a2s-section-title">
                    目录映射
                    <span class="a2s-section-count">{{ activeInstance.mappings.length }}</span>
                  </h3>
                  <p class="a2s-section-desc a2s-section-desc--warn">
                    <VIcon size="14" icon="mdi-alert-outline" color="warning" />
                    {{ notices.output_directory }}
                  </p>
                </div>
                <VBtn
                  variant="tonal"
                  color="primary"
                  size="small"
                  prepend-icon="mdi-plus"
                  @click="addMapping(activeInstance)"
                >添加映射</VBtn>
              </div>

              <button
                v-if="!activeInstance.mappings.length"
                type="button"
                class="a2s-mapping-placeholder"
                @click="addMapping(activeInstance)"
              >
                <VIcon icon="mdi-folder-plus-outline" />
                添加第一条映射：OpenList 目录 → 本地 STRM 目录
              </button>

              <div
                v-for="(mapping, mappingIndex) in activeInstance.mappings"
                :key="keyOf(mapping)"
                class="a2s-mapping"
                :class="{ 'a2s-mapping--error': outputConflicts.has(conflictKey(activeIndex, mappingIndex)) }"
              >
                <div class="a2s-mapping-head">
                  <span class="a2s-mapping-index">{{ mappingIndex + 1 }}</span>
                  <span class="a2s-mapping-title">{{ mapping.source_dir.trim() || '新映射' }}</span>
                  <VBtn
                    icon="mdi-close"
                    size="x-small"
                    variant="text"
                    aria-label="删除映射"
                    @click="removeMapping(activeInstance, mappingIndex)"
                  />
                </div>

                <div class="a2s-mapping-paths">
                  <VTextField
                    v-model="mapping.source_dir"
                    label="OpenList 源目录"
                    placeholder="/media/movies"
                    prepend-inner-icon="mdi-cloud-outline"
                    :rules="[required]"
                    hide-details="auto"
                  />
                  <VIcon class="a2s-mapping-arrow" icon="mdi-arrow-right" aria-hidden="true" />
                  <VTextField
                    v-model="mapping.output_dir"
                    label="本地输出目录"
                    placeholder="/strm/movies"
                    prepend-inner-icon="mdi-folder-outline"
                    :rules="[required]"
                    :error-messages="outputConflicts.get(conflictKey(activeIndex, mappingIndex)) || []"
                    hide-details="auto"
                  />
                </div>

                <div class="a2s-mapping-filters">
                  <div class="a2s-filter">
                    <div class="a2s-filter-head">
                      <span class="a2s-filter-label">文件后缀</span>
                      <VBtnToggle
                        v-model="mapping.whitelist_mode"
                        mandatory
                        density="compact"
                        variant="outlined"
                        divided
                        color="primary"
                        class="a2s-toggle"
                      >
                        <VBtn value="follow_host" size="small">跟随宿主</VBtn>
                        <VBtn value="custom" size="small">自定义</VBtn>
                      </VBtnToggle>
                    </div>
                    <VCombobox
                      v-if="mapping.whitelist_mode === 'custom'"
                      :model-value="mapping.extensions"
                      :items="[]"
                      :delimiters="LIST_DELIMITERS"
                      multiple
                      chips
                      closable-chips
                      hide-no-data
                      density="compact"
                      placeholder="输入后缀后回车，如 .mkv"
                      hide-details
                      @update:model-value="setList(mapping, 'extensions', $event)"
                    />
                    <div v-else class="a2s-filter-hint">
                      {{ mapping.effective_extensions?.length
                        ? mapping.effective_extensions.join('  ')
                        : '使用 MoviePilot 的媒体与字幕后缀' }}
                    </div>
                  </div>

                  <div class="a2s-filter">
                    <div class="a2s-filter-head">
                      <span class="a2s-filter-label">排除关键字</span>
                      <span class="a2s-filter-note">名称包含即跳过，不区分大小写</span>
                    </div>
                    <VCombobox
                      :model-value="mapping.exclude_keywords"
                      :items="[]"
                      :delimiters="LIST_DELIMITERS"
                      multiple
                      chips
                      closable-chips
                      hide-no-data
                      density="compact"
                      placeholder="输入关键字后回车"
                      hide-details
                      @update:model-value="setList(mapping, 'exclude_keywords', $event)"
                    />
                  </div>
                </div>
              </div>
            </section>
          </template>
        </template>

        <!-- 通用设置 -->
        <template v-else>
          <section class="a2s-section">
            <div class="a2s-section-head">
              <div>
                <h3 class="a2s-section-title">自动扫描</h3>
                <p class="a2s-section-desc">自动检查到期目录，按子树最新文件修改时间区分冷热。</p>
              </div>
              <VSwitch
                v-model="draft.schedule_enabled"
                color="primary"
                inset
                hide-details
                density="compact"
                aria-label="启用自动扫描"
                class="a2s-switch"
              />
            </div>
            <div class="a2s-grid a2s-age-row">
              <VTextField
                v-model.number="draft.file_age_policy.recent_days"
                type="number"
                min="1"
                step="1"
                suffix="天"
                label="近期窗口上限"
                hint="最新文件年龄不超过此天数。"
                persistent-hint
              />
              <VTextField
                v-model.number="draft.active_recheck_days"
                label="近期目录复查间隔"
                hint="检查失败后也按此间隔重试。"
                type="number"
                min="1"
                :error-messages="pruningError"
                step="1"
                suffix="天"
                persistent-hint
              />
            </div>
            <div class="a2s-grid a2s-age-row">
              <VTextField
                v-model.number="draft.file_age_policy.cold_after_days"
                type="number"
                min="1"
                step="1"
                suffix="天"
                label="陈旧窗口上限"
                hint="超过近期窗口，且不超过此天数。"
                persistent-hint
              />
              <VTextField
                v-model.number="draft.file_age_policy.stale_recheck_days"
                label="陈旧目录复查间隔"
                hint="对应左侧陈旧窗口。"
                type="number"
                min="1"
                step="1"
                suffix="天"
                persistent-hint
              />
            </div>
            <div class="a2s-grid a2s-age-row">
              <VTextField
                v-model.number="draft.file_age_policy.archive_after_days"
                type="number"
                min="1"
                step="1"
                suffix="天"
                label="冷目录窗口上限"
                hint="超过陈旧窗口，且不超过此天数。"
                persistent-hint
              />
              <VTextField
                v-model.number="draft.file_age_policy.cold_recheck_days"
                label="冷目录复查间隔"
                hint="对应左侧冷目录窗口。"
                type="number"
                min="1"
                step="1"
                suffix="天"
                persistent-hint
              />
            </div>
            <div class="a2s-grid a2s-age-row">
              <VTextField
                :model-value="`超过 ${draft.file_age_policy.archive_after_days} 天`"
                readonly
                label="长期静态窗口"
                hint="超过冷目录窗口上限。"
                persistent-hint
              />
              <VTextField
                v-model.number="draft.file_age_policy.archive_recheck_days"
                label="长期静态目录复查间隔"
                hint="对应左侧长期静态窗口。"
                type="number"
                min="1"
                step="1"
                suffix="天"
                persistent-hint
              />
            </div>
            <div class="a2s-grid a2s-age-row">
              <VTextField
                model-value="空目录或时间不确定"
                readonly
                label="空目录／异常时间"
                hint="没有文件，或文件修改时间缺失、异常。"
                persistent-hint
              />
              <VTextField
                v-model.number="draft.file_age_policy.uncertain_recheck_days"
                label="空目录／异常时间复查间隔"
                hint="混合有效时间时，取本间隔与正常档位间隔中的较短值。"
                type="number"
                min="1"
                step="1"
                suffix="天"
                persistent-hint
              />
            </div>
            <p class="a2s-section-desc">
              首次扫描后即可按上述文件年龄阈值分档。所有映射共用设置，一天按 24 小时计算。缩短间隔可提前复查，延长间隔不推迟已经取得的期限；新档位在复查后使用。父目录跳过不阻挡已知子目录独立到期复查。
              冷却期间新增、删除等变化可能延迟发现。需要立即检查时，可在总览运行全部强制完整扫描，或单独运行某个映射的强制完整扫描。
            </p>
          </section>

          <section class="a2s-section">
            <div class="a2s-section-head">
              <div>
                <h3 class="a2s-section-title">扫描历史</h3>
                <p class="a2s-section-desc">超过保留期的运行记录会被自动清理。</p>
              </div>
            </div>
            <div class="a2s-grid">
              <VTextField
                v-model.number="draft.history_retention_days"
                label="保留天数"
                type="number"
                min="1"
                step="1"
                suffix="天"
                hide-details="auto"
              />
            </div>
          </section>
        </template>
      </main>
    </div>

    <!-- 底栏 -->
    <VDivider />
    <footer class="a2s-foot">
      <div class="a2s-foot-status" role="status">
        <template v-if="saveError">
          <VIcon size="18" icon="mdi-alert-circle-outline" color="error" />
          <span class="text-error">{{ saveError }}</span>
        </template>
        <template v-else-if="outputConflicts.size">
          <VIcon size="18" icon="mdi-alert-circle-outline" color="error" />
          <span class="text-error">{{ outputConflicts.size }} 条映射的输出目录重叠</span>
        </template>
        <span v-else-if="pruningError" class="text-error">{{ pruningError }}</span>
        <span v-else-if="dirty" class="text-medium-emphasis">修改尚未保存</span>
      </div>
      <div class="a2s-foot-actions">
        <VBtn variant="text" @click="emit('close')">关闭</VBtn>
        <VBtn
          color="primary"
          variant="flat"
          prepend-icon="mdi-content-save-outline"
          :loading="saving"
          :disabled="!canSave"
          @click="save"
        >保存</VBtn>
      </div>
    </footer>

    <VDialog v-model="confirmOpen" max-width="24rem">
      <VCard>
        <VCardTitle class="text-subtitle-1 pt-4">删除实例？</VCardTitle>
        <VCardText class="text-body-2">
          “{{ pendingDelete !== null && draft.instances[pendingDelete]
            ? instanceLabel(draft.instances[pendingDelete], pendingDelete)
            : '' }}”及其全部映射将被移除，保存后生效。
        </VCardText>
        <VCardActions>
          <VSpacer />
          <VBtn variant="text" @click="pendingDelete = null">取消</VBtn>
          <VBtn color="error" variant="flat" @click="confirmRemoveInstance">删除</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>
  </div>
</template>

<style scoped>
.a2s-config {
  container-type: inline-size;
  display: flex;
  flex-direction: column;
  height: min(85dvh, 52rem);
  background: rgb(var(--v-theme-surface));
}

/* 宿主在小屏下全屏显示弹窗 */
@media (max-width: 959.98px) {
  .a2s-config { height: 100dvh; }
}

/* ── 顶栏 / 底栏 ── */
.a2s-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.75rem 0.75rem 0.75rem 1.25rem;
  flex-shrink: 0;
}
.a2s-head-text { flex: 1; min-width: 0; }
.a2s-head-title { font-size: 1.0625rem; font-weight: 600; line-height: 1.4; }
.a2s-head-sub { font-size: 0.75rem; color: rgba(var(--v-theme-on-surface), 0.6); }

.a2s-foot {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem 1rem;
  padding: 0.625rem 1rem 0.625rem 1.25rem;
  padding-bottom: calc(0.625rem + env(safe-area-inset-bottom, 0px));
  flex-shrink: 0;
}
.a2s-foot-status {
  flex: 1 1 12rem;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-size: 0.8125rem;
}
.a2s-foot-actions { display: flex; gap: 0.5rem; margin-inline-start: auto; }

/* ── 主体布局 ── */
.a2s-body {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: 15rem minmax(0, 1fr);
}

.a2s-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0.75rem 0.5rem;
  overflow-y: auto;
  border-inline-end: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  background: rgba(var(--v-theme-on-surface), 0.02);
}
.a2s-nav-label {
  padding: 0.25rem 0.75rem 0.375rem;
  font-size: 0.6875rem;
  font-weight: 600;
  letter-spacing: 0.06em;
  color: rgba(var(--v-theme-on-surface), 0.5);
}
.a2s-nav-label--gap { margin-top: 0.875rem; }
.a2s-nav-item {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  width: 100%;
  min-height: 2.5rem;
  padding: 0 0.75rem;
  border-radius: 8px;
  font-size: 0.875rem;
  text-align: start;
  color: rgba(var(--v-theme-on-surface), 0.85);
  transition: background-color 0.15s;
}
.a2s-nav-item:hover { background: rgba(var(--v-theme-on-surface), 0.06); }
.a2s-nav-item:focus-visible { outline: 2px solid rgb(var(--v-theme-primary)); outline-offset: -2px; }
.a2s-nav-item--active,
.a2s-nav-item--active:hover {
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  font-weight: 600;
}
.a2s-nav-item--add { color: rgba(var(--v-theme-on-surface), 0.6); }
.a2s-nav-icon { flex-shrink: 0; opacity: 0.8; }
.a2s-nav-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.a2s-nav-count {
  min-width: 1.25rem;
  padding: 0 0.375rem;
  border-radius: 999px;
  font-size: 0.6875rem;
  line-height: 1.25rem;
  text-align: center;
  background: rgba(var(--v-theme-on-surface), 0.08);
}

.a2s-main {
  min-width: 0;
  overflow-y: auto;
  padding: 1.25rem 1.5rem 2rem;
  display: flex;
  flex-direction: column;
  gap: 2rem;
}

/* ── 分区 ── */
.a2s-section { display: flex; flex-direction: column; gap: 1rem; }
.a2s-section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}
.a2s-section-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.9375rem;
  font-weight: 600;
  line-height: 1.5;
}
.a2s-section-count {
  padding: 0 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 500;
  background: rgba(var(--v-theme-on-surface), 0.08);
}
.a2s-section-desc {
  margin: 0.125rem 0 0;
  font-size: 0.8125rem;
  line-height: 1.5;
  color: rgba(var(--v-theme-on-surface), 0.6);
}
.a2s-section-desc--warn .v-icon { vertical-align: -2px; margin-inline-end: 0.125rem; }

.a2s-age-row + .a2s-age-row {
  margin-top: 1rem;
}

.a2s-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.a2s-span-2 { grid-column: 1 / -1; }
.a2s-mono :deep(input) { font-family: ui-monospace, 'SF Mono', Consolas, monospace; }
.a2s-switch { flex: 0 0 auto; }

.a2s-requirements {
  padding: 0.75rem 1rem;
  border-radius: 8px;
  background: rgba(var(--v-theme-info), 0.08);
  font-size: 0.8125rem;
  line-height: 1.6;
}
.a2s-requirements-title {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  font-weight: 600;
  color: rgb(var(--v-theme-info));
  margin-bottom: 0.25rem;
}
.a2s-requirements ul { margin: 0; padding-inline-start: 1.25rem; color: rgba(var(--v-theme-on-surface), 0.75); }

/* ── 映射 ── */
.a2s-mapping {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  padding: 0.875rem 1rem 1rem;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 10px;
}
.a2s-mapping--error { border-color: rgba(var(--v-theme-error), 0.6); }
.a2s-mapping-head { display: flex; align-items: center; gap: 0.625rem; margin-inline-end: -0.375rem; }
.a2s-mapping-index {
  display: inline-grid;
  place-items: center;
  width: 1.5rem;
  height: 1.5rem;
  border-radius: 6px;
  font-size: 0.75rem;
  font-weight: 600;
  background: rgba(var(--v-theme-primary), 0.12);
  color: rgb(var(--v-theme-primary));
  flex-shrink: 0;
}
.a2s-mapping-title {
  flex: 1;
  min-width: 0;
  font-family: ui-monospace, 'SF Mono', Consolas, monospace;
  font-size: 0.8125rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: rgba(var(--v-theme-on-surface), 0.75);
}
.a2s-mapping-paths {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: start;
  gap: 0.5rem;
}
.a2s-mapping-arrow { margin-top: 1rem; color: rgba(var(--v-theme-on-surface), 0.4); }
.a2s-mapping-filters {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.a2s-filter { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
.a2s-filter-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.25rem 0.5rem;
  min-height: 2rem;
}
.a2s-filter-label { font-size: 0.8125rem; font-weight: 600; }
.a2s-filter-note { font-size: 0.75rem; color: rgba(var(--v-theme-on-surface), 0.55); }
.a2s-filter-hint {
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  font-size: 0.75rem;
  line-height: 1.6;
  word-spacing: 0.25rem;
  color: rgba(var(--v-theme-on-surface), 0.65);
  background: rgba(var(--v-theme-on-surface), 0.04);
  overflow-wrap: anywhere;
}
.a2s-toggle { height: 1.875rem !important; }

.a2s-mapping-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 1.5rem 1rem;
  border: 1px dashed rgba(var(--v-border-color), 0.4);
  border-radius: 10px;
  font-size: 0.8125rem;
  color: rgba(var(--v-theme-on-surface), 0.6);
  transition: background-color 0.15s, color 0.15s;
}
.a2s-mapping-placeholder:hover { background: rgba(var(--v-theme-primary), 0.05); color: rgb(var(--v-theme-primary)); }

/* ── 空状态 ── */
.a2s-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 3rem 1rem;
  text-align: center;
}
.a2s-empty-title { font-size: 1rem; font-weight: 600; margin-top: 0.5rem; }
.a2s-empty-text { font-size: 0.8125rem; color: rgba(var(--v-theme-on-surface), 0.6); margin-bottom: 0.75rem; }

/* ── 中等宽度：映射内部改为单列 ── */
@container (max-width: 860px) {
  .a2s-mapping-filters { grid-template-columns: minmax(0, 1fr); }
}

/* ── 窄宽度：导航变为顶部横向滚动条 ── */
@container (max-width: 680px) {
  .a2s-body { grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr); }
  .a2s-nav {
    flex-direction: row;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 0.75rem;
    overflow-x: auto;
    overflow-y: hidden;
    border-inline-end: 0;
    border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    scrollbar-width: none;
  }
  .a2s-nav-label { display: none; }
  .a2s-nav-item {
    width: auto;
    flex: 0 0 auto;
    min-height: 2.25rem;
    max-width: 12rem;
    border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
    border-radius: 999px;
  }
  .a2s-nav-item--active { border-color: transparent; }
  .a2s-nav-item--add { order: 1; }
  .a2s-main { padding: 1rem 1rem 1.5rem; gap: 1.5rem; }
  .a2s-grid { grid-template-columns: minmax(0, 1fr); }
  .a2s-mapping { padding: 0.75rem; }
  .a2s-mapping-paths { grid-template-columns: minmax(0, 1fr); gap: 0.25rem; }
  .a2s-mapping-arrow { margin: 0 auto; transform: rotate(90deg); }
  .a2s-section-head { flex-wrap: wrap; }
}
</style>
