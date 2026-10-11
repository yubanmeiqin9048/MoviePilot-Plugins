import type {
  ConfigModel,
  ConfigResponse,
  HistoryPage,
  PluginApi,
  ScanStatus,
  ScanMode,
  ScanTarget,
  ScanTriggerResponse,
} from '@/types'

function pluginPath(pluginId: string, path: string): string {
  return `plugin/${encodeURIComponent(pluginId)}/${path.replace(/^\//, '')}`
}

function responseMessage(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null
  const record = payload as Record<string, unknown>
  if (typeof record.message === 'string' && record.message) return record.message
  if (typeof record.detail === 'string' && record.detail) return record.detail
  if (record.detail && typeof record.detail === 'object') {
    const detail = record.detail as Record<string, unknown>
    if (typeof detail.message === 'string' && detail.message) return detail.message
  }
  return null
}

/** 返回插件保存接口的安全错误文字，不展开任何 Token。 */
export function getErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === 'object') {
    const response = (error as { response?: { data?: unknown } }).response
    const message = responseMessage(response?.data)
    if (message) return message
  }
  return error instanceof Error && error.message ? error.message : fallback
}

/** 通过宿主注入 API 保存实例级配置。 */
export async function saveConfiguration(
  api: PluginApi,
  pluginId: string,
  payload: ConfigModel,
): Promise<ConfigResponse> {
  if (!api.put) throw new Error('当前 MoviePilot 前端不支持插件配置更新')
  return api.put<ConfigResponse>(pluginPath(pluginId, 'config'), payload)
}

/** 读取实例级安全配置；响应不包含管理员 Token。 */
export function readConfiguration(api: PluginApi, pluginId: string): Promise<ConfigResponse> {
  return api.get<ConfigResponse>(pluginPath(pluginId, 'config'))
}

/** 启动全部映射的手动扫描，服务端只读取已保存配置。 */
export function startScan(api: PluginApi, pluginId: string): Promise<ScanTriggerResponse> {
  return api.post<ScanTriggerResponse>(pluginPath(pluginId, 'scan'), { scan_mode: 'normal' })
}

/** 启动一个已保存映射的手动扫描。 */
export function startMappingScan(
  api: PluginApi,
  pluginId: string,
  target: ScanTarget,
  scanMode: ScanMode = 'normal',
): Promise<ScanTriggerResponse> {
  return api.post<ScanTriggerResponse>(pluginPath(pluginId, 'scan/mapping'), { scan_mode: scanMode, target })
}

/** 读取可轮询的扫描状态与六类计数。 */
export function readScanStatus(api: PluginApi, pluginId: string): Promise<ScanStatus> {
  return api.get<ScanStatus>(pluginPath(pluginId, 'scan/status'))
}

/** 分页读取管理员扫描历史，查询参数在客户端边界内规范化。 */
export function readHistory(
  api: PluginApi,
  pluginId: string,
  page = 1,
  pageSize = 20,
): Promise<HistoryPage> {
  const safePage = Number.isInteger(page) ? Math.max(page, 1) : 1
  const safePageSize = Number.isInteger(pageSize) ? Math.min(Math.max(pageSize, 1), 100) : 20
  return api.get<HistoryPage>(pluginPath(pluginId, 'history'), {
    params: { page: safePage, page_size: safePageSize },
  })
}

/** 请求停止当前扫描；服务端结束后台任务后才会返回 stopped 状态。 */
export function stopScan(api: PluginApi, pluginId: string): Promise<ScanTriggerResponse> {
  return api.post<ScanTriggerResponse>(pluginPath(pluginId, 'scan/stop'), {})
}
