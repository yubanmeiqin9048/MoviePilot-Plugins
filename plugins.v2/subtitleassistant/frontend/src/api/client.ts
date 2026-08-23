import type {
  BatchRecordDeleteItem,
  BatchRecordDeleteResponse,
  BatchRetargetMapping,
  BatchRetargetPreviewResponse,
  BatchRetargetResponse,
  CredentialUpdateResponse,
  DownloadResponse,
  PageResponse,
  PluginApi,
  NonSensitiveConfig,
  RecordDetail,
  RecordDeleteMode,
  RecordDeleteSnapshot,
  RecordListItem,
  RecordStatus,
  RawHistoryPage,
  SourceStatusItem,
  SearchRequest,
  SearchResponse,
  StandardResponse,
  SubtitleSource,
  TargetItem,
  TaskDetail,
  TaskListItem,
  TaskStatus,
} from '@/types'

type QueryOptions<TStatus extends string> = {
  page: number
  pageSize: 25 | 50 | 100
  search?: string
  status?: TStatus | ''
}

function pluginPath(pluginId: string, path: string): string {
  return `plugin/${encodeURIComponent(pluginId)}/${path.replace(/^\//, '')}`
}

function compactParams(values: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).filter(([, value]) => value !== undefined && value !== null && value !== ''),
  )
}

function responseMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null
  const record = payload as Record<string, unknown>
  if (typeof record.message === 'string' && record.message) return record.message
  if (typeof record.detail === 'string' && record.detail) return record.detail
  if (record.detail && typeof record.detail === 'object' && !Array.isArray(record.detail)) {
    const detail = record.detail as Record<string, unknown>
    if (typeof detail.message === 'string' && detail.message) return detail.message
    if (typeof detail.error_message === 'string' && detail.error_message) return detail.error_message
    if (typeof detail.code === 'string' && detail.code) return detail.code
  }
  if (Array.isArray(record.detail)) {
    const messages = record.detail
      .map(item => (item && typeof item === 'object' ? (item as Record<string, unknown>).msg : null))
      .filter((item): item is string => typeof item === 'string')
    if (messages.length) return messages.join('；')
  }
  return null
}

function responseCode(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null
  const record = payload as Record<string, unknown>
  if (typeof record.code === 'string' && record.code) return record.code
  if (record.detail && typeof record.detail === 'object' && !Array.isArray(record.detail)) {
    const detail = record.detail as Record<string, unknown>
    if (typeof detail.code === 'string' && detail.code) return detail.code
  }
  return null
}

export function getErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const response = (error as { response?: { data?: unknown } }).response
    const fromResponse = responseMessage(response?.data)
    if (fromResponse) return fromResponse
  }
  if (error instanceof Error && error.message) return error.message
  return fallback
}

export function getErrorCode(error: unknown): string | null {
  if (!error || typeof error !== 'object') return null
  const response = (error as { response?: { data?: unknown } }).response
  return responseCode(response?.data)
}

export async function savePluginConfig(
  api: PluginApi,
  pluginId: string,
  config: NonSensitiveConfig,
): Promise<StandardResponse> {
  if (!api.put) throw new Error('当前 MoviePilot 前端不支持插件配置更新')
  const response = await api.put<StandardResponse>(`plugin/${encodeURIComponent(pluginId)}`, config)
  return requireSuccess(response, '普通配置保存失败')
}

function requireSuccess(response: StandardResponse, fallback: string): StandardResponse {
  if (!response?.success) throw new Error(responseMessage(response) || fallback)
  return response
}

export function listTasks(
  api: PluginApi,
  pluginId: string,
  options: QueryOptions<TaskStatus>,
): Promise<PageResponse<TaskListItem>> {
  return api.get(pluginPath(pluginId, 'tasks'), {
    params: compactParams({
      page: options.page,
      page_size: options.pageSize,
      search: options.search?.trim(),
      status: options.status,
    }),
  })
}

export function getTask(api: PluginApi, pluginId: string, taskId: string): Promise<TaskDetail> {
  return api.get(pluginPath(pluginId, `tasks/${encodeURIComponent(taskId)}`))
}

export async function deleteTask(api: PluginApi, pluginId: string, taskId: string): Promise<StandardResponse> {
  if (!api.delete) throw new Error('当前 MoviePilot 前端不支持删除请求')
  const response = await api.delete<StandardResponse>(pluginPath(pluginId, `tasks/${encodeURIComponent(taskId)}`))
  return requireSuccess(response, '任务记录删除失败')
}

export function listRecords(
  api: PluginApi,
  pluginId: string,
  options: QueryOptions<RecordStatus>,
): Promise<PageResponse<RecordListItem>> {
  return api.get(pluginPath(pluginId, 'records'), {
    params: compactParams({
      page: options.page,
      page_size: options.pageSize,
      search: options.search?.trim(),
      status: options.status,
    }),
  })
}

export function getRecord(api: PluginApi, pluginId: string, recordId: string): Promise<RecordDetail> {
  return api.get(pluginPath(pluginId, `records/${encodeURIComponent(recordId)}`))
}

export async function deleteRecord(
  api: PluginApi,
  pluginId: string,
  recordId: string,
  payload: RecordDeleteSnapshot & { delete_mode: RecordDeleteMode },
): Promise<StandardResponse> {
  if (!api.delete) throw new Error('当前 MoviePilot 前端不支持删除请求')
  const response = await api.delete<StandardResponse>(pluginPath(pluginId, `records/${encodeURIComponent(recordId)}`), { data: payload })
  return requireSuccess(response, '匹配记录删除失败')
}

export function deleteBatchRecords(
  api: PluginApi,
  pluginId: string,
  payload: { delete_mode: RecordDeleteMode; items: BatchRecordDeleteItem[] },
): Promise<BatchRecordDeleteResponse> {
  return api.post<BatchRecordDeleteResponse>(pluginPath(pluginId, 'records/batch-delete'), payload)
}

export async function listSourceStatus(api: PluginApi, pluginId: string): Promise<SourceStatusItem[]> {
  const response = await api.get<SourceStatusItem[] | { items: SourceStatusItem[] }>(
    pluginPath(pluginId, 'sources/status'),
  )
  return Array.isArray(response) ? response : response?.items ?? []
}

export async function refreshSourceStatus(api: PluginApi, pluginId: string): Promise<StandardResponse> {
  const response = await api.post<StandardResponse>(pluginPath(pluginId, 'sources/refresh'))
  return requireSuccess(response, '字幕源状态刷新失败')
}

export async function updateCredentials(
  api: PluginApi,
  pluginId: string,
  source: Extract<SubtitleSource, 'opensubtitles' | 'assrt'>,
  payload: Record<string, string>,
): Promise<CredentialUpdateResponse> {
  if (!api.put) throw new Error('当前 MoviePilot 前端不支持凭据更新')
  const response = await api.put<CredentialUpdateResponse>(pluginPath(pluginId, `credentials/${source}`), payload)
  return requireSuccess(response, '凭据更新失败') as CredentialUpdateResponse
}

export async function clearCredentials(
  api: PluginApi,
  pluginId: string,
  source: Extract<SubtitleSource, 'opensubtitles' | 'assrt'>,
): Promise<StandardResponse> {
  if (!api.delete) throw new Error('当前 MoviePilot 前端不支持凭据清除')
  return api.delete<StandardResponse>(pluginPath(pluginId, `credentials/${source}`))
}

export function listTargets(
  api: PluginApi,
  pluginId: string,
  options: { page: number; pageSize: 25 | 50 | 100 },
): Promise<RawHistoryPage> {
  return api.get(pluginPath(pluginId, 'targets'), {
    params: compactParams({
      page: options.page,
      page_size: options.pageSize,
    }),
  })
}

export function searchSubtitles(
  api: PluginApi,
  pluginId: string,
  payload: SearchRequest,
): Promise<SearchResponse> {
  return api.post(pluginPath(pluginId, 'searches'), payload)
}

export async function downloadCandidate(
  api: PluginApi,
  pluginId: string,
  sessionId: string,
  candidateKey: string,
): Promise<DownloadResponse> {
  return api.post<DownloadResponse>(
    pluginPath(pluginId, `searches/${encodeURIComponent(sessionId)}/downloads`),
    { candidate_key: candidateKey },
  )
}

export function previewBatchRetargetRecords(
  api: PluginApi,
  pluginId: string,
  items: BatchRetargetMapping[],
): Promise<BatchRetargetPreviewResponse> {
  return api.post<BatchRetargetPreviewResponse>(pluginPath(pluginId, 'records/batch-retarget-preview'), { items })
}

export function retargetBatchRecords(
  api: PluginApi,
  pluginId: string,
  items: Array<Required<BatchRetargetMapping>>,
): Promise<BatchRetargetResponse> {
  return api.post<BatchRetargetResponse>(pluginPath(pluginId, 'records/batch-retarget'), { items })
}
