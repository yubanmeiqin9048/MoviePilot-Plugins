import type { ConfigModel, ConfigResponse, PluginApi } from '@/types'

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
