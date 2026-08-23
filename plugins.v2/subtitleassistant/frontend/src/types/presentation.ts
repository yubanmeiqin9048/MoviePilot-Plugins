import type {
  AttemptResult,
  AttributionEvidence,
  FileLocation,
  FileAttributionMethod,
  HistoryRow,
  MediaType,
  ManualSourceResult,
  PackageScope,
  PackageAttributionStrategy,
  RecordStatus,
  SearchPlanItem,
  SourceHealth,
  SourceRunStatus,
  SubtitleSource,
  TaskStage,
  TaskStatus,
  TaskTrigger,
  TargetItem,
  TranslationType,
  UnmatchedReason,
} from '@/types'

export interface StatePresentation {
  label: string
  icon: string
  color: 'default' | 'primary' | 'success' | 'info' | 'warning' | 'error'
}

type HistoryRecord = HistoryRow | TargetItem

export const taskStates: Record<TaskStatus, StatePresentation> = {
  queued: { label: '等待中', icon: 'mdi-clock-outline', color: 'info' },
  processing: { label: '处理中', icon: 'mdi-progress-clock', color: 'primary' },
  success: { label: '成功', icon: 'mdi-check-circle-outline', color: 'success' },
  skipped: { label: '跳过', icon: 'mdi-skip-next-circle-outline', color: 'default' },
  failed: { label: '失败', icon: 'mdi-alert-circle-outline', color: 'error' },
  interrupted: { label: '已中断', icon: 'mdi-stop-circle-outline', color: 'warning' },
}

export const taskTriggerLabels: Record<TaskTrigger, string> = {
  transfer_event: '整理事件',
  manual_candidate: '人工下载',
}

export const recordStates: Record<RecordStatus, StatePresentation> = {
  matched: { label: '已匹配', icon: 'mdi-check-circle-outline', color: 'success' },
  staged: { label: '暂存', icon: 'mdi-archive-arrow-down-outline', color: 'info' },
  unmatched: { label: '未匹配', icon: 'mdi-help-circle-outline', color: 'warning' },
}

export const sourceHealthStates: Record<SourceHealth, StatePresentation> = {
  pending: { label: '待检测', icon: 'mdi-clock-outline', color: 'default' },
  healthy: { label: '正常', icon: 'mdi-check-circle-outline', color: 'success' },
  limited: { label: '受限', icon: 'mdi-timer-alert-outline', color: 'warning' },
  error: { label: '异常', icon: 'mdi-alert-circle-outline', color: 'error' },
  disabled: { label: '已禁用', icon: 'mdi-minus-circle-outline', color: 'default' },
}

export const stageLabels: Record<TaskStage, string> = {
  preflight: '前置检查',
  inventory: '查询字幕库存',
  search: '搜索字幕源',
  download: '下载候选',
  extract: '解包',
  match: '匹配',
  ai_attribution: 'AI 智能接管',
  write: '落盘',
}

export const sourceLabels: Record<SubtitleSource, string> = {
  moviepilot: 'MoviePilot 站点源',
  opensubtitles: 'OpenSubtitles',
  assrt: 'ASSRT',
}

export const subtitleSourceOrder: SubtitleSource[] = ['moviepilot', 'opensubtitles', 'assrt']

export function formatSearchPlan(plan: SearchPlanItem): string {
  return [plan.label, plan.query].filter(Boolean).join(' · ')
}

export const packageLabels: Record<PackageScope, string> = {
  season_pack: '整季包',
  episode: '单集',
  unknown: '范围未知',
}

export const attributionStrategyLabels: Record<PackageAttributionStrategy, string> = {
  trust_package: '信任候选包',
  host_recognition: 'MoviePilot 文件识别',
}

export const fileAttributionLabels: Record<FileAttributionMethod, string> = {
  direct_file: '直接字幕文件',
  trust_package: '信任候选包',
  host_recognition: 'MoviePilot 文件识别',
  ai_takeover: 'AI 智能接管',
}

export const attributionEvidenceLabels: Record<AttributionEvidence, string> = {
  path: '文件路径识别',
  candidate_snapshot: '候选唯一范围继承',
  ai_takeover: 'AI 智能接管',
  not_applicable: '不适用',
  unknown: '无法确定',
}

export const unmatchedReasonLabels: Record<UnmatchedReason, string> = {
  media_unrecognized: '媒体未识别',
  season_ambiguous: '季号不明确',
  episode_ambiguous: '集号不明确',
  candidate_file_scope_conflict: '候选范围与文件季集冲突',
  unsupported_format: '字幕格式不支持',
}

export const translationLabels: Record<TranslationType, string> = {
  human: '人工翻译',
  unknown: '翻译类型未知',
  machine: '机器翻译',
  ai: 'AI 翻译',
}

export const mediaTypeLabels: Record<MediaType, string> = {
  movie: '电影',
  tv: '电视剧',
  unknown: '未知类型',
}

export const locationLabels: Record<FileLocation, string> = {
  media_directory: '媒体目录',
  plugin_data: '插件数据目录',
}

export const sourceRunLabels: Record<SourceRunStatus, string> = {
  success: '已返回候选',
  empty: '未返回候选',
  filtered: '没有适用候选',
  error: '请求异常',
  limited: '请求受限',
  disabled: '未启用',
  unconfigured: '未配置',
}

export function manualSourceState(status: ManualSourceResult, candidateCount: number): StatePresentation {
  if (status === 'success' && candidateCount > 0) return { label: '已返回', icon: 'mdi-check-circle-outline', color: 'success' }
  if (status === 'success') return { label: '无结果', icon: 'mdi-file-search-outline', color: 'default' }
  if (status === 'limited') return { label: '受限', icon: 'mdi-timer-alert-outline', color: 'warning' }
  if (status === 'disabled') return { label: '已禁用', icon: 'mdi-minus-circle-outline', color: 'default' }
  if (status === 'unconfigured') return { label: '未配置', icon: 'mdi-cog-off-outline', color: 'default' }
  return { label: '异常', icon: 'mdi-alert-circle-outline', color: 'error' }
}

export const attemptLabels: Record<AttemptResult, string> = {
  success: '落盘成功',
  download_failed: '下载失败',
  extract_failed: '解包失败',
  no_match: '包内无匹配',
  write_failed: '落盘失败',
  interrupted: '已中断',
}

export function isTerminalTask(status: TaskStatus): boolean {
  return ['success', 'skipped', 'failed', 'interrupted'].includes(status)
}

export function formatDate(value?: string | null): string {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN')
}

function historyText(value: unknown): string | null {
  if (typeof value !== 'string' && typeof value !== 'number') return null
  const text = String(value).trim()
  return text || null
}

function historyNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string' || !value.trim()) return null
  const parsed = Number(value)
  if (Number.isFinite(parsed)) return parsed
  const match = value.match(/\d+/)
  return match ? Number(match[0]) : null
}

export function historyId(item: HistoryRecord | null | undefined): string | number | null {
  if (!item) return null
  const raw = item as HistoryRow
  const value = raw.id ?? raw['history_id']
  return typeof value === 'string' || (typeof value === 'number' && Number.isFinite(value)) ? value : null
}

export function sameHistoryId(
  left: HistoryRecord | null | undefined,
  right: HistoryRecord | null | undefined,
): boolean {
  const leftId = historyId(left)
  const rightId = historyId(right)
  return leftId != null && rightId != null && String(leftId) === String(rightId)
}

export function historyTitle(item: HistoryRecord): string {
  const raw = item as HistoryRow
  return historyText(raw.title ?? raw['media_title']) || '未识别媒体'
}

export function historyYear(item: HistoryRecord): number | null {
  return historyNumber(item.year)
}

export function historyMediaType(item: HistoryRecord): MediaType {
  const raw = item as HistoryRow
  const value = historyText(raw.type ?? raw['media_type'])?.toLowerCase() || ''
  if (value === 'movie' || value.includes('电影')) return 'movie'
  if (value === 'tv' || value === 'show' || value.includes('电视')) return 'tv'
  return 'unknown'
}

export function historySeason(item: HistoryRecord): number | null {
  const raw = item as HistoryRow
  return historyNumber(raw.seasons ?? raw['season'])
}

export function historyEpisode(item: HistoryRecord): number | null {
  const raw = item as HistoryRow
  return historyNumber(raw.episodes ?? raw['episode'])
}

export function historyPath(item: HistoryRecord): string {
  const raw = item as HistoryRow
  return historyText(raw.dest ?? raw['target_path']) || ''
}

export function historyFileName(item: HistoryRecord): string {
  const fileItem = (item as HistoryRow).dest_fileitem
  if (fileItem && typeof fileItem === 'object') {
    const name = historyText(fileItem.name)
    if (name) return name
  }
  const path = historyPath(item)
  return path.split(/[\\/]/).filter(Boolean).pop() || '未记录文件名'
}

export function historyDate(item: HistoryRecord): string | null {
  const raw = item as HistoryRow
  return historyText(raw.date ?? raw['organized_at'])
}

export function historyLabel(item: HistoryRecord): string {
  return mediaLabel(historyTitle(item), historyYear(item), historySeason(item), historyEpisode(item))
}

export function formatDuration(milliseconds?: number | null): string {
  if (milliseconds === null || milliseconds === undefined) return '未记录'
  if (milliseconds < 1000) return `${milliseconds} 毫秒`
  const seconds = Math.round(milliseconds / 100) / 10
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  const remaining = Math.round(seconds % 60)
  return remaining ? `${minutes} 分 ${remaining} 秒` : `${minutes} 分`
}

export function elapsedDuration(start?: string | null): string {
  if (!start) return '未开始'
  const started = new Date(start).getTime()
  if (Number.isNaN(started)) return '时间未知'
  return formatDuration(Math.max(0, Date.now() - started))
}

export function formatBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined) return '未知'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

export function seasonEpisode(season?: number | null, episode?: number | null): string {
  if (season === null || season === undefined) return episode === null || episode === undefined ? '' : `E${pad(episode)}`
  return episode === null || episode === undefined ? `S${pad(season)}` : `S${pad(season)}E${pad(episode)}`
}

export function mediaLabel(
  title?: string | null,
  year?: number | null,
  season?: number | null,
  episode?: number | null,
): string {
  const identity = [title || '未识别', year ? String(year) : '', seasonEpisode(season, episode)].filter(Boolean)
  return identity.join(' · ')
}

export function shortPath(path?: string | null): string {
  if (!path) return '未记录'
  const parts = path.split(/[\\/]+/).filter(Boolean)
  if (parts.length <= 2) return path
  return `…/${parts.slice(-2).join('/')}`
}

export function fullPath(path?: string | null): string {
  return path || '未记录'
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '未记录'
  if (typeof value === 'boolean') return value ? '是' : '否'
  if (Array.isArray(value)) return value.length ? value.map(displayValue).join('、') : '无'
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>)
    .map(([key, item]) => `${friendlyKey(key)}：${displayValue(item)}`)
    .join('；') || '无'
  return String(value)
}

export function friendlyKey(key: string): string {
  const labels: Record<string, string> = {
    found: '是否发现',
    existing: '已有字幕',
    checked_paths: '检查路径',
    hit: '是否命中',
    matched: '匹配数量',
    staged: '暂存数量',
    unmatched: '未匹配数量',
    site_count: '站点数量',
    site_names: '有效站点',
    site_candidate_counts: '各站点候选数',
    candidate_total: '候选总数',
    candidate_count: '候选数量',
    total_candidates: '候选总数',
    raw_count: '原始返回',
    admitted_count: '自动规则保留',
    media_matched_count: '当前目标匹配',
    rejected_count: '过滤数量',
    language: '语言不符合',
    download_locator: '缺少下载定位',
    admission: '自动规则未通过',
    translation: '翻译类型不符合',
    duplicate: '重复候选',
    query_unavailable: '缺少可用查询词',
    media_or_episode_mismatch: '媒体或季集不匹配',
    foreign_parts_only: '仅外语对白',
    machine_translation: '机器或 AI 翻译',
    cache_hit: '是否复用缓存',
    cache_stored_at: '缓存生成时间',
    cache_ttl_seconds: '缓存有效秒数',
    page_count: '读取页数',
    pagination_complete: '分页是否完整',
    query_type: '查询方式',
    query: '查询词',
    session_active: '会话状态',
    last_search_at: '最近搜索',
    last_download_at: '最近下载',
    remaining: '下载剩余额度',
    reset_at: '额度重置时间',
    downloads_remaining: '下载剩余额度',
    downloads_reset_at: '额度重置时间',
    quota: '当前配额',
    cooldown_until: '冷却结束',
    limited_until: '受限结束',
    last_request_at: '最近请求',
    attribution: '服务来源',
  }
  return labels[key] || key.replaceAll('_', ' ')
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}
