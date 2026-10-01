export interface PluginApi {
  get<T = unknown>(path: string, options?: Record<string, unknown>): Promise<T>
  post<T = unknown>(path: string, payload?: unknown, options?: Record<string, unknown>): Promise<T>
  put?<T = unknown>(path: string, payload?: unknown, options?: Record<string, unknown>): Promise<T>
  delete?<T = unknown>(path: string, options?: Record<string, unknown>): Promise<T>
}

export interface HostToast {
  error(message: string): unknown
  info(message: string): unknown
  success(message: string): unknown
  warning(message: string): unknown
}

export type TaskStatus = 'queued' | 'processing' | 'success' | 'skipped' | 'failed' | 'interrupted'
export type TaskTrigger = 'transfer_event' | 'manual_candidate'
export type RecordStatus = 'matched' | 'staged' | 'unmatched'
export type RecordDeleteMode = 'record_only' | 'record_and_file'
export const MAX_RECORD_BATCH_SIZE = 100
export type SubtitleSource = 'moviepilot' | 'opensubtitles' | 'assrt'
export type CandidateRecognitionStatus = 'recognized' | 'unrecognized'
export type CandidateRecognitionFilter = 'all' | CandidateRecognitionStatus
export type CandidateSourceFilter = 'all' | SubtitleSource
export type PackageScope = 'season_pack' | 'episode' | 'unknown'
export type TranslationType = 'human' | 'unknown' | 'machine' | 'ai'
export type SourceHealth = 'pending' | 'healthy' | 'limited' | 'error' | 'disabled'
export type FileLocation = 'media_directory' | 'plugin_data'
export type MediaType = 'movie' | 'tv' | 'unknown'
export type PackageAttributionStrategy = 'trust_package' | 'host_recognition'
export type FileAttributionMethod = 'direct_file' | PackageAttributionStrategy
export type UnmatchedReason =
  | 'media_unrecognized'
  | 'season_ambiguous'
  | 'episode_ambiguous'
  | 'candidate_file_scope_conflict'
  | 'unsupported_format'

export interface PathMapping {
  source_prefix: string
  target_prefix: string
}

export interface ConfigModel {
  plugin_id?: string
  enabled: boolean
  moviepilot_enabled: boolean
  opensubtitles_enabled: boolean
  assrt_enabled: boolean
  opensubtitles_configured: boolean
  assrt_configured: boolean
  allow_machine_translation: boolean
  max_candidate_attempts: number
  source_priority: SubtitleSource[]
  format_priority: string[]
  path_mappings: PathMapping[]
  package_attribution_strategy: PackageAttributionStrategy
  allowed_formats: string[]
  [key: string]: unknown
}

export type NonSensitiveConfig = Pick<
  ConfigModel,
  | 'enabled'
  | 'moviepilot_enabled'
  | 'opensubtitles_enabled'
  | 'assrt_enabled'
  | 'allow_machine_translation'
  | 'max_candidate_attempts'
  | 'source_priority'
  | 'format_priority'
  | 'path_mappings'
  | 'package_attribution_strategy'
>

export interface TaskListItem {
  id: string
  media_title: string
  year: number | null
  media_type: MediaType
  season: number | null
  episode: number | null
  target_file_name: string
  target_path: string
  target_history_id: string | number | null
  history_target_path: string | null
  status: TaskStatus
  reason_code: string | null
  reason_message: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  trigger: TaskTrigger
}

export interface TaskDetail extends TaskListItem {
  subtitle_directory?: string | null
  tmdb_id: number | null
  imdb_id: string | null
  target_storage: string | null
  matched_path_mapping?: PathMapping | null
  target_file_exists?: boolean | null
}

export interface PageResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: 25 | 50 | 100
}

export interface RawHistoryPage {
  items: HistoryRow[]
  page: number
  page_size: 25 | 50 | 100
  total: number
}

export interface HistoryRow {
  [key: string]: unknown
  id?: string | number | null
  status?: boolean | string | number | null
  dest?: string | null
  dest_storage?: string | null
  dest_fileitem?: Record<string, unknown> | null
  title?: string | null
  year?: string | number | null
  type?: string | null
  seasons?: string | number | null
  episodes?: string | number | null
  tmdbid?: string | number | null
  imdbid?: string | null
  date?: string | null
}

export interface RecordListItem {
  id: string
  subtitle_file_name: string
  format: string
  size: number | null
  media_title: string | null
  year: number | null
  media_type: MediaType
  season: number | null
  episode: number | null
  status: RecordStatus
  source: SubtitleSource
  package_scope: PackageScope
  location: FileLocation
  path: string
  current_file_path: string
  target_history_id: string | number | null
  history_target_path: string | null
  target_path: string | null
  created_at: string
  updated_at: string
  consumed_at: string | null
}

export interface RecordDetail extends RecordListItem {
  canonical_identity_type: string | null
  canonical_identity_value: string | null
  tmdb_id: number | null
  imdb_id: string | null
  matched_path_mapping?: PathMapping | null
  final_subtitle_path: string | null
  source_task_id: string
  consumed_task_id: string | null
  candidate_key: string
  candidate_name: string | null
  language: string
  translation_type: TranslationType
  logical_source_path?: string | null
  file_attribution_method?: FileAttributionMethod | null
  unmatched_reason?: UnmatchedReason | null
  target_file_exists?: boolean | null
  staged_at: string | null
  retarget_history: RetargetHistoryItem[]
}

export interface RetargetHistoryItem {
  operated_at: string
  old_target_history_id: string | number | null
  new_target_history_id: string | number | null
  old_history_target_path: string | null
  new_history_target_path: string | null
  old_target_path: string | null
  new_target_path: string
  old_subtitle_path: string
  new_subtitle_path: string
}

export interface TargetItem {
  history_id: string | number
  media_title: string
  year: number | null
  media_type: MediaType
  season: number | null
  episode: number | null
  tmdb_id: number | null
  imdb_id: string | null
  target_file_name: string
  target_path: string
  organized_at: string
  search_plans: Record<SubtitleSource, SearchPlanItem[]>
}

export interface SearchPlanItem {
  kind: 'id' | 'title' | 'filename' | 'fallback'
  label: string
  query: string | null
  editable: boolean
}

export interface SearchRequest {
  target_history_id: string | number
  moviepilot_keyword?: string | null
  opensubtitles_keyword?: string | null
  assrt_keyword?: string | null
}

export interface CandidateSourceFilterOption {
  title: string
  value: CandidateSourceFilter
}

export type ManualSourceResult = 'success' | 'partial' | 'limited' | 'error' | 'disabled' | 'unconfigured'

export interface SubtitleCandidate {
  candidate_key: string
  recognition_status: CandidateRecognitionStatus
  name: string
  file_name: string | null
  source: SubtitleSource
  language: string | null
  package_scope: PackageScope
  season: number | null
  episode: number | null
  seasons: number[]
  episodes: number[]
  translation_type: TranslationType
}

export interface SearchSourceGroup {
  source: SubtitleSource
  status: ManualSourceResult
  default_plans: SearchPlanItem[]
  matched_query: string | null
  candidate_count: number
  cache_hit: boolean
  duration_ms: number | null
  error_code: string | null
  error_summary: string | null
  retry_after_seconds: number | null
  candidates: SubtitleCandidate[]
}

export interface SearchResponse {
  session_id: string | null
  target: TargetItem
  sources: SearchSourceGroup[]
}

export interface DownloadResponse {
  task_id: string
  task: TaskListItem
}

export interface RetargetPreview {
  target_history_id: string | number
  history_target_path: string
  target_path: string
  final_subtitle_path: string
  directory_available: boolean
  directory_error?: string | null
}

export interface BatchRetargetMapping {
  record_id: string
  target_history_id?: string | number | null
}

export interface BatchRetargetPreviewItem {
  record_id: string
  current_subtitle_path: string | null
  target_history_id: string | number | null
  target: TargetItem | null
  preview: RetargetPreview | null
  executable: boolean
  error_code: string | null
  message: string | null
}

export interface BatchRetargetPreviewResponse {
  executable: boolean
  items: BatchRetargetPreviewItem[]
}

export interface BatchRetargetResultItem {
  record_id: string
  target_history_id: string | number
  success: boolean
  error_code: string | null
  message: string | null
  consistency_risk: boolean
  record: RecordDetail | null
}

export interface BatchRetargetResponse {
  success_count: number
  failure_count: number
  items: BatchRetargetResultItem[]
}

export interface RecordDeleteSnapshot {
  expected_status: RecordStatus
  expected_location: FileLocation
  expected_path: string
  expected_updated_at: string
}

export interface BatchRecordDeleteItem extends RecordDeleteSnapshot {
  record_id: string
}

export type BatchRecordDeleteStatus = 'success' | 'failed' | 'not_executed'

export interface BatchRecordDeleteResultItem {
  record_id: string
  status: BatchRecordDeleteStatus
  error_code: string | null
  message: string | null
  consistency_risk: boolean
}

export interface BatchRecordDeleteResponse {
  success_count: number
  failure_count: number
  not_executed_count: number
  items: BatchRecordDeleteResultItem[]
}

export interface SourceStatusItem {
  source: SubtitleSource
  enabled: boolean
  configured: boolean
  health: SourceHealth
  last_checked_at: string | null
  last_success_at: string | null
  last_error_at: string | null
  last_error_summary: string | null
  last_duration_ms: number | null
  details: Record<string, unknown>
}

export interface StandardResponse {
  success: boolean
  message?: string | null
  data?: unknown
}

export interface CredentialUpdateResponse extends StandardResponse {
  data?: { configured?: boolean }
}
