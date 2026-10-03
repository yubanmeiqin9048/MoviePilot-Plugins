<script setup lang="ts">
import { computed, inject, onMounted, ref, watch } from 'vue'

import { getErrorMessage, saveConfiguration } from '@/api'
import { configurationEditorState } from '@/configState'
import type { ConfigModel, DirectoryMapping, Notices, OpenListInstance, PluginApi } from '@/types'

const DEFAULT_NOTICES: Notices = {
  admin_token: '仅支持 OpenList 管理员 API Token，不支持普通用户 Token、账号或密码。',
  root: 'OpenList 管理员根目录必须为 /；插件不探测或换算用户根目录。',
  sign: '请关闭 OpenList 的 sign；插件不探测、生成或续签 sign。',
  output_directory: '完整扫描成功后，会清理该目录中不再需要的受管文件，包括手工放入的文件；请使用专用输出目录。',
}

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

const pluginId = computed(() => props.pluginId.trim() || props.initialConfig?.plugin_id?.trim() || '')
const dirty = computed(() => serialize(toPayload(draft.value)) !== baseline.value)
const notices = computed(() => ({ ...DEFAULT_NOTICES, ...(draft.value.notices || {}) }))
const outputConflicts = computed(() => findOutputConflicts(draft.value.instances))
const canSave = computed(() => !saving.value && !outputConflicts.value.length)

watch(() => props.initialConfig, value => {
  const next = normalizeConfig(value)
  draft.value = next
  baseline.value = serialize(toPayload(next))
  saveError.value = ''
  configurationEditorState.dirty = false
}, { immediate: true, deep: true })

watch(draft, () => {
  configurationEditorState.dirty = dirty.value
}, { deep: true })

onMounted(() => emit('layout', { maxWidth: '78rem' }))

function emptyModel(): ConfigModel {
  return {
    instances: [],
    schedule_enabled: false,
    schedule_cron: '0 3 * * *',
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
    schedule_cron: typeof input.schedule_cron === 'string' && input.schedule_cron.trim() ? input.schedule_cron : '0 3 * * *',
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
  if (typeof value === 'string') return value.split(/[,\n]/).map(item => item.trim()).filter(Boolean)
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string').map(item => item.trim()).filter(Boolean) : []
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

function addInstance(): void {
  draft.value.instances.push({
    id: `instance-${Date.now()}`,
    name: '',
    scan_url: '',
    api_token: '',
    playback_url: '',
    mappings: [],
  })
}

function removeInstance(index: number): void {
  draft.value.instances.splice(index, 1)
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

function extensionsText(mapping: DirectoryMapping): string {
  return mapping.extensions.join(', ')
}

function updateExtensions(mapping: DirectoryMapping, value: string): void {
  mapping.extensions = normalizeLines(value)
}

function keywordsText(mapping: DirectoryMapping): string {
  return mapping.exclude_keywords.join('\n')
}

function updateKeywords(mapping: DirectoryMapping, value: string): void {
  mapping.exclude_keywords = normalizeLines(value)
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

function findOutputConflicts(instances: OpenListInstance[]): string[] {
  const entries: Array<{ instance: string; index: number; output: string }> = []
  instances.forEach(instance => instance.mappings.forEach((mapping, index) => {
    if (mapping.output_dir.trim()) entries.push({ instance: instance.name || `实例 ${instances.indexOf(instance) + 1}`, index: index + 1, output: mapping.output_dir })
  }))
  const conflicts: string[] = []
  entries.forEach((first, firstIndex) => entries.slice(firstIndex + 1).forEach(second => {
    if (overlaps(first.output, second.output)) {
      conflicts.push(`实例“${first.instance}”映射 ${first.index}（${first.output}）与实例“${second.instance}”映射 ${second.index}（${second.output}）输出目录重叠。`)
    }
  }))
  return conflicts
}

async function save(): Promise<void> {
  saveError.value = ''
  if (!canSave.value) {
    saveError.value = outputConflicts.value[0] || '请先修正配置。'
    return
  }
  if (!pluginId.value) {
    saveError.value = '缺少插件实例 ID，无法保存配置。'
    return
  }
  saving.value = true
  const payload = toPayload(draft.value)
  try {
    const response = await saveConfiguration(props.api, pluginId.value, payload)
    const returned = response?.data ? normalizeConfig(response.data) : draft.value
    draft.value = returned
    baseline.value = serialize(toPayload(returned))
    configurationEditorState.dirty = false
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
  <VCard class="alist2strm-config" flat>
    <VCardTitle class="d-flex align-center justify-space-between">
      <span>Alist2Strm 配置</span>
      <VChip v-if="dirty" color="warning" size="small" variant="tonal">有未保存更改</VChip>
    </VCardTitle>
    <VCardText>
      <VAlert class="mb-3" type="info" variant="tonal">{{ notices.admin_token }}</VAlert>
      <VAlert class="mb-3" type="warning" variant="tonal">{{ notices.root }}</VAlert>
      <VAlert class="mb-3" type="warning" variant="tonal">{{ notices.sign }}</VAlert>

      <VExpansionPanels multiple>
        <VExpansionPanel v-for="(instance, instanceIndex) in draft.instances" :key="instance.id || instanceIndex">
          <VExpansionPanelTitle>实例 {{ instanceIndex + 1 }}{{ instance.name ? `：${instance.name}` : '' }}</VExpansionPanelTitle>
          <VExpansionPanelText>
            <VTextField v-model="instance.name" label="实例名称" class="mb-2" />
            <VTextField v-model="instance.scan_url" label="扫描地址" hint="OpenList 服务的完整 HTTP(S) 地址" persistent-hint class="mb-2" />
            <VTextField v-model="instance.api_token" type="password" autocomplete="new-password" label="管理员 API Token" :placeholder="instance.api_token_configured ? '已配置，留空表示保持不变' : ''" class="mb-2" />
            <VTextField v-model="instance.playback_url" label="播放地址（可选）" hint="留空时使用扫描地址" persistent-hint class="mb-4" />

            <div class="d-flex align-center justify-space-between mb-2">
              <h3 class="text-subtitle-1">目录映射</h3>
              <VBtn size="small" variant="tonal" prepend-icon="mdi-plus" @click="addMapping(instance)">添加映射</VBtn>
            </div>
            <VAlert class="mb-3" type="info" variant="tonal">{{ notices.output_directory }}</VAlert>
            <VCard v-for="(mapping, mappingIndex) in instance.mappings" :key="`${instance.id}-${mappingIndex}`" variant="outlined" class="pa-3 mb-3">
              <div class="d-flex align-center justify-space-between mb-2">
                <span class="text-subtitle-2">映射 {{ mappingIndex + 1 }}</span>
                <VBtn icon="mdi-delete-outline" size="small" variant="text" aria-label="删除映射" @click="removeMapping(instance, mappingIndex)" />
              </div>
              <VTextField v-model="mapping.source_dir" label="源目录（绝对路径）" class="mb-2" />
              <VTextField v-model="mapping.output_dir" label="输出目录（绝对路径）" class="mb-2" />
              <VSelect v-model="mapping.whitelist_mode" :items="[{ title: '跟随宿主媒体与字幕后缀', value: 'follow_host' }, { title: '自定义后缀白名单', value: 'custom' }]" label="后缀白名单模式" class="mb-2" />
              <VTextField v-if="mapping.whitelist_mode === 'custom'" :model-value="extensionsText(mapping)" label="后缀白名单" hint="多个后缀用逗号或换行分隔" persistent-hint class="mb-2" @update:model-value="updateExtensions(mapping, String($event))" />
              <div v-else class="text-caption mb-2">当前生效后缀：{{ mapping.effective_extensions?.join(', ') || '保存后读取宿主设置' }}</div>
              <VTextarea :model-value="keywordsText(mapping)" label="排除关键字" hint="每行一个，按名称包含关系匹配且不区分大小写" persistent-hint rows="2" @update:model-value="updateKeywords(mapping, String($event))" />
            </VCard>
            <div v-if="!instance.mappings.length" class="text-caption mb-3">尚未添加目录映射。</div>
            <VBtn color="error" variant="text" prepend-icon="mdi-delete-outline" @click="removeInstance(instanceIndex)">删除实例</VBtn>
          </VExpansionPanelText>
        </VExpansionPanel>
      </VExpansionPanels>
      <VBtn class="mt-3" variant="tonal" prepend-icon="mdi-plus" @click="addInstance">添加 OpenList 实例</VBtn>

      <VAlert v-for="conflict in outputConflicts" :key="conflict" class="mt-3" type="error" variant="tonal">{{ conflict }}</VAlert>

      <VDivider class="my-5" />
      <h3 class="text-subtitle-1 mb-2">全局设置</h3>
      <VSwitch v-model="draft.schedule_enabled" label="启用共用定时计划" color="primary" />
      <VTextField v-model="draft.schedule_cron" label="Cron（5 个字段）" :disabled="!draft.schedule_enabled" class="mb-2" />
      <VTextField v-model.number="draft.history_retention_days" type="number" min="1" step="1" label="历史保留天数" />

      <VAlert v-if="saveError" class="mt-4" type="error" variant="tonal">{{ saveError }}</VAlert>
    </VCardText>
    <VCardActions>
      <VSpacer />
      <VBtn variant="text" @click="emit('close')">关闭</VBtn>
      <VBtn color="primary" :loading="saving" :disabled="!canSave" prepend-icon="mdi-content-save" @click="save">保存</VBtn>
    </VCardActions>
  </VCard>
</template>
