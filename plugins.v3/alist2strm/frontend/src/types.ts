export interface PluginApi {
  get<T = unknown>(path: string, options?: Record<string, unknown>): Promise<T>
  post<T = unknown>(path: string, payload?: unknown, options?: Record<string, unknown>): Promise<T>
  put?<T = unknown>(path: string, payload?: unknown, options?: Record<string, unknown>): Promise<T>
}

export interface DirectoryMapping {
  source_dir: string
  output_dir: string
  whitelist_mode: 'follow_host' | 'custom'
  extensions: string[]
  effective_extensions?: string[]
  exclude_keywords: string[]
  output_directory_warning?: string
}

export interface OpenListInstance {
  id: string
  name: string
  scan_url: string
  api_token?: string
  api_token_configured?: boolean
  playback_url: string
  mappings: DirectoryMapping[]
}

export interface Notices {
  admin_token: string
  root: string
  sign: string
  output_directory: string
}

export interface TimePruningConfiguration {
  recent_days: number
  cold_after_days: number
  archive_after_days: number
  stale_recheck_days: number
  cold_recheck_days: number
  archive_recheck_days: number
  empty_recheck_days: number
}

export interface ConfigModel {
  plugin_id?: string
  instances: OpenListInstance[]
  schedule_enabled: boolean
  schedule_cron: string
  history_retention_days: number
  time_pruning: TimePruningConfiguration
  notices?: Notices
}

export interface ConfigResponse {
  success: boolean
  message?: string
  data: ConfigModel
}

export interface ScanCounters {
  generated: number
  downloaded: number
  updated: number
  skipped: number
  deleted: number
  failed: number
}

export interface ScanMappingStatus {
  instance_id: string
  instance_name: string
  mapping_index: number
  source_dir: string
  output_dir: string
  state: string
  status: string
  counters: ScanCounters
  error?: string | null
  cleanup_eligible?: boolean
  started_at?: string | null
  finished_at?: string | null
}

export interface ScanStatus {
  state: string
  status: string
  running: boolean
  run_id?: string | null
  current_mapping?: string | null
  total_mappings: number
  counters: ScanCounters
  mappings: ScanMappingStatus[]
  started_at?: string | null
  finished_at?: string | null
}

export interface HistoryMapping {
  identity: string
  instance_id: string
  instance_name: string
  mapping_index: number
  source_dir: string
  output_dir: string
  state: string
  started_at?: string | null
  finished_at?: string | null
  error?: string | null
  counters: ScanCounters
}

export interface HistoryItem {
  id: string
  identity: string
  state: string
  started_at: string
  finished_at?: string | null
  error?: string | null
  counters: ScanCounters
  mappings: HistoryMapping[]
}

export interface HistoryPage {
  total: number
  page: number
  page_size: number
  items: HistoryItem[]
}

export interface ScanTriggerResponse {
  accepted: boolean
  message: string
}

export type ScanMode = 'normal' | 'force_complete'

export interface ScanTarget {
  instance_id: string
  mapping_index: number
  source_dir?: string
}
