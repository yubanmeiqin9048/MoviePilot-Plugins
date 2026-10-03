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
  api_token: string
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

export interface ConfigModel {
  plugin_id?: string
  instances: OpenListInstance[]
  schedule_enabled: boolean
  schedule_cron: string
  history_retention_days: number
  notices?: Notices
}

export interface ConfigResponse {
  success: boolean
  message?: string
  data: ConfigModel
}
