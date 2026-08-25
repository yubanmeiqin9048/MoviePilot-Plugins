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
export type TaskStage = 'preflight' | 'inventory' | 'search' | 'download' | 'extract' | 'match' | 'ai_attribution' | 'write'
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
export type FileAttributionMethod = 'direct_file' | PackageAttributionStrategy | 'ai_takeover'
export type AttributionEvidence = 'path' | 'candidate_snapshot' | 'ai_takeover' | 'not_applicable' | 'unknown'
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

export interface CandidateAttributionSnapshot {
  media_type: MediaType
  year?: number | null
  tmdb_id: number | null
  imdb_id: string | null
  seasons: number[]
  episodes: number[]
  package_scope: PackageScope
  evidence: string[]
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
  /** 匹配失败时是否允许插件请求字幕归属 AI 接管。 */
  ai_attribution_takeover_enabled: boolean
  /** MoviePilot 智能助手总开关状态，仅用于界面禁用提示，不参与保存。 */
  ai_agent_enabled?: boolean
  ai_agent_available?: boolean
  host_ai_enabled?: boolean
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
  | 'ai_attribution_takeover_enabled'
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
  stage: TaskStage | null
  reason_code: string | null
  reason_message: string | null
  result_source: SubtitleSource | null
  result_package_scope: PackageScope | null
  result_format: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  warning_count: number
  trigger: TaskTrigger
}

export interface StageTrace {
  stage: TaskStage
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  summary: string | null
}

export type SourceRunStatus = 'success' | 'empty' | 'filtered' | 'error' | 'limited' | 'disabled' | 'unconfigured'
export interface SourceRun {
  source: SubtitleSource
  status: SourceRunStatus
  candidate_count?: number
  raw_count?: number
  admitted_count?: number
  media_matched_count?: number
  rejected_count?: number
  rejection_summary?: Record<string, number>
  duration_ms: number | null
  error_summary: string | null
  details: Record<string, unknown>
}

export type AttemptResult =
  | 'success'
  | 'download_failed'
  | 'extract_failed'
  | 'no_match'
  | 'write_failed'
  | 'interrupted'
export interface CandidateAttempt {
  candidate_key: string
  source: SubtitleSource
  package_scope: PackageScope
  language: string
  format: string
  translation_type: TranslationType
  hearing_impaired: boolean
  attribution_strategy?: PackageAttributionStrategy | null
  candidate_snapshot?: CandidateAttributionSnapshot | null
  extracted_count?: number
  current_target_count?: number
  same_media_other_episode_count?: number
  ambiguous_count?: number
  other_media_count?: number
  written_count?: number
  staged_count?: number
  unmatched_count?: number
  ai_attempt_count?: number
  ai_accepted_count?: number
  ai_rejected_count?: number
  ai_error_count?: number
  ai_skipped_count?: number
  ai_over_limit_count?: number
  ai_reason_summary?: Record<string, number>
  result: AttemptResult
  error_summary: string | null
}

export interface TaskDetail extends TaskListItem {
  tmdb_id: number | null
  imdb_id: string | null
  target_storage: string | null
  matched_path_mapping?: PathMapping | null
  target_file_exists?: boolean | null
  package_attribution_strategy?: PackageAttributionStrategy
  candidate_attribution_snapshot?: CandidateAttributionSnapshot | null
  existing_subtitle_check: Record<string, unknown>
  inventory_result: Record<string, unknown>
  stage_traces: StageTrace[]
  source_runs: SourceRun[]
  candidate_attempts: CandidateAttempt[]
  final_subtitle_path: string | null
  record_ids: string[]
  record_counts: Record<string, number>
  warning_summaries: string[]
  manual_source: SubtitleSource | null
  manual_candidate_key: string | null
  manual_candidate_summary: Record<string, unknown>
  actual_search_query: string | null
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
  hearing_impaired: boolean
  candidate_attribution_snapshot?: CandidateAttributionSnapshot | null
  logical_source_path?: string | null
  file_attribution_method?: FileAttributionMethod | null
  host_recognition_summary?: Record<string, unknown> | null
  ai_takeover_audit?: Record<string, unknown> | null
  ai_attribution_audit?: Record<string, unknown> | null
  season_evidence?: AttributionEvidence | null
  episode_evidence?: AttributionEvidence | null
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

export type ManualSourceResult = 'success' | 'limited' | 'error' | 'disabled' | 'unconfigured'

export interface SubtitleCandidate {
  candidate_key: string
  recognition_status: CandidateRecognitionStatus
  name: string
  file_name: string | null
  source: SubtitleSource
  language: string | null
  format: string | null
  package_scope: PackageScope
  season: number | null
  episode: number | null
  seasons: number[]
  episodes: number[]
  translation_type: TranslationType
  hearing_impaired: boolean
  rating: number | null
  votes: number | null
  downloads: number | null
  uploaded_at: string | null
  query: string | null
  source_details: Record<string, string | number | boolean | null>
}

export interface SearchSourceGroup {
  source: SubtitleSource
  status: ManualSourceResult
  default_plans: SearchPlanItem[]
  executed_queries: string[]
  matched_query: string | null
  candidate_count: number
  duration_ms: number | null
  error_summary: string | null
  details: Record<string, unknown>
  candidates: SubtitleCandidate[]
}

export interface SearchResponse {
  session_id: string | null
  target: TargetItem
  sources: SearchSourceGroup[]
}

export interface DownloadResponse {
  task_id: string
  reused: boolean
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

export type ThemeName = 'light' | 'dark' | 'purple' | 'transparent'
