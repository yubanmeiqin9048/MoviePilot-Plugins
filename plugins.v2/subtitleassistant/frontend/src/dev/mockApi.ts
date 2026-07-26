import {
  MAX_RECORD_BATCH_SIZE,
  type BatchRecordDeleteResponse,
  type BatchRetargetPreviewResponse,
  type BatchRetargetResponse,
  type ConfigModel,
  type PluginApi,
  type RecordDeleteMode,
  type RecordDetail,
  type RecordListItem,
  type SearchResponse,
  type SourceStatusItem,
  type TargetItem,
  type TaskDetail,
  type TaskListItem,
} from '@/types'
import { reactive } from 'vue'

export type MockMode = 'normal' | 'empty' | 'error'

export interface MockState {
  mode: MockMode
  config: ConfigModel
  tasks: TaskListItem[]
  taskDetails: Record<string, TaskDetail>
  records: RecordListItem[]
  recordDetails: Record<string, RecordDetail>
  targets: TargetItem[]
  sources: SourceStatusItem[]
}

const now = Date.now()
const iso = (offset: number): string => new Date(now - offset).toISOString()

function seedState(): MockState {
  const tasks: TaskListItem[] = [
    {
      id: 'task-processing',
      media_title: '示例剧集',
      year: 2024,
      media_type: 'tv',
      season: 1,
      episode: 3,
      target_file_name: 'Example.Show.S01E03.1080p.WEB-DL.mkv',
      target_path: '/media/剧集/示例剧集/Season 01/Example.Show.S01E03.1080p.WEB-DL.mkv',
      target_history_id: 4103,
      history_target_path: '/legacy/media/剧集/示例剧集/Season 01/Example.Show.S01E03.1080p.WEB-DL.mkv',
      status: 'processing',
      stage: 'search',
      reason_code: null,
      reason_message: null,
      result_source: null,
      result_package_scope: null,
      result_format: null,
      created_at: iso(45_000),
      started_at: iso(42_000),
      finished_at: null,
      duration_ms: null,
      warning_count: 0,
      trigger: 'transfer_event',
    },
    {
      id: 'task-queued',
      media_title: '夜航星',
      year: 2023,
      media_type: 'movie',
      season: null,
      episode: null,
      target_file_name: 'Night.Flight.2023.2160p.mkv',
      target_path: '/media/电影/夜航星/Night.Flight.2023.2160p.mkv',
      target_history_id: 4102,
      history_target_path: '/media/电影/夜航星/Night.Flight.2023.2160p.mkv',
      status: 'queued',
      stage: null,
      reason_code: null,
      reason_message: null,
      result_source: null,
      result_package_scope: null,
      result_format: null,
      created_at: iso(20_000),
      started_at: null,
      finished_at: null,
      duration_ms: null,
      warning_count: 0,
      trigger: 'transfer_event',
    },
    {
      id: 'task-success',
      media_title: '山海之间',
      year: 2022,
      media_type: 'movie',
      season: null,
      episode: null,
      target_file_name: 'Between.Mountains.2022.1080p.mkv',
      target_path: '/media/电影/山海之间/Between.Mountains.2022.1080p.mkv',
      target_history_id: 4101,
      history_target_path: '/legacy/media/电影/山海之间/Between.Mountains.2022.1080p.mkv',
      status: 'success',
      stage: null,
      reason_code: null,
      reason_message: null,
      result_source: 'moviepilot',
      result_package_scope: 'episode',
      result_format: 'ass',
      created_at: iso(240_000),
      started_at: iso(235_000),
      finished_at: iso(228_000),
      duration_ms: 7_000,
      warning_count: 1,
      trigger: 'transfer_event',
    },
    {
      id: 'task-failed',
      media_title: '未找到字幕的电影',
      year: 2021,
      media_type: 'movie',
      season: null,
      episode: null,
      target_file_name: 'No.Subtitle.Movie.2021.mkv',
      target_path: '/media/电影/未找到字幕的电影/No.Subtitle.Movie.2021.mkv',
      target_history_id: 4100,
      history_target_path: '/media/电影/未找到字幕的电影/No.Subtitle.Movie.2021.mkv',
      status: 'failed',
      stage: null,
      reason_code: 'candidate_exhausted',
      reason_message: '候选尝试已耗尽，没有可落盘的简体中文字幕。',
      result_source: null,
      result_package_scope: null,
      result_format: null,
      created_at: iso(3_600_000),
      started_at: iso(3_590_000),
      finished_at: iso(3_580_000),
      duration_ms: 10_000,
      warning_count: 0,
      trigger: 'transfer_event',
    },
  ]

  const taskDetails: Record<string, TaskDetail> = Object.fromEntries(tasks.map(task => [task.id, {
    ...task,
    tmdb_id: task.id === 'task-processing' ? 12345 : (task.id === 'task-success' ? 67890 : null),
    imdb_id: task.id === 'task-processing' ? 'tt1234567' : null,
    target_storage: 'local',
    matched_path_mapping: task.history_target_path !== task.target_path
      ? { source_prefix: '/legacy/media', target_prefix: '/media' }
      : null,
    target_file_exists: task.id !== 'task-queued',
    package_attribution_strategy: 'trust_package',
    candidate_attribution_snapshot: task.media_type === 'tv'
      ? { media_type: 'tv', tmdb_id: 12345, imdb_id: null, seasons: [1], episodes: [3, 4], package_scope: 'season_pack', evidence: ['候选标题'] }
      : { media_type: 'movie', tmdb_id: null, imdb_id: null, seasons: [], episodes: [], package_scope: 'episode', evidence: ['候选标题'] },
    existing_subtitle_check: { found: false, checked_paths: [task.target_path.replace(/\.mkv$/i, '.chi.zh-cn.ass')] },
    inventory_result: task.id === 'task-processing' ? { hit: false, staged: 0 } : { hit: false, staged: 0 },
    stage_traces: task.id === 'task-processing'
      ? [{ stage: 'preflight', started_at: iso(42_000), finished_at: iso(41_000), duration_ms: 1000, summary: '目标文件通过前置检查' }, { stage: 'search', started_at: iso(40_000), finished_at: null, duration_ms: null, summary: '正在等待字幕源结果' }]
      : [{ stage: task.status === 'success' ? 'write' : 'search', started_at: task.started_at || task.created_at, finished_at: task.finished_at, duration_ms: task.duration_ms, summary: task.reason_message || '阶段完成' }],
    source_runs: [
      {
        source: 'moviepilot',
        status: task.status === 'failed' ? 'empty' : 'success',
        candidate_count: task.status === 'failed' ? 0 : 2,
        raw_count: task.status === 'failed' ? 0 : 5,
        admitted_count: task.status === 'failed' ? 0 : 3,
        media_matched_count: task.status === 'failed' ? 0 : 2,
        rejected_count: task.status === 'failed' ? 0 : 3,
        rejection_summary: task.status === 'failed'
          ? {} as Record<string, number>
          : { language: 2, media_or_episode_mismatch: 1 },
        duration_ms: 820,
        error_summary: null,
        details: { cache_hit: false, page_count: 1, pagination_complete: true, query: 'Adventure' },
      },
      {
        source: 'assrt',
        status: task.status === 'failed' ? 'limited' : 'success',
        candidate_count: task.status === 'failed' ? 0 : 1,
        raw_count: task.status === 'failed' ? 0 : 4,
        admitted_count: task.status === 'failed' ? 0 : 2,
        media_matched_count: task.status === 'failed' ? 0 : 1,
        rejected_count: task.status === 'failed' ? 0 : 3,
        rejection_summary: task.status === 'failed'
          ? {} as Record<string, number>
          : { machine_translation: 1, media_or_episode_mismatch: 2 },
        duration_ms: 1200,
        error_summary: task.status === 'failed' ? '请求频率受限' : null,
        details: { cache_hit: true, cache_stored_at: iso(120_000), page_count: 1, pagination_complete: true, query: '示例剧集' },
      },
      {
        source: 'opensubtitles',
        status: 'disabled',
        candidate_count: 0,
        raw_count: 0,
        admitted_count: 0,
        media_matched_count: 0,
        rejected_count: 0,
        rejection_summary: {},
        duration_ms: null,
        error_summary: null,
        details: {},
      },
    ],
    candidate_attempts: task.status === 'failed' ? [{
      candidate_key: 'demo-candidate-1', source: 'assrt', package_scope: 'episode', language: 'zh-CN', format: 'srt', translation_type: 'human', hearing_impaired: false,
      attribution_strategy: 'trust_package', candidate_snapshot: { media_type: task.media_type, tmdb_id: null, imdb_id: null, seasons: [], episodes: [], package_scope: 'episode', evidence: ['候选标题'] },
      extracted_count: 3, current_target_count: 0, same_media_other_episode_count: 1, ambiguous_count: 2, other_media_count: 0, written_count: 0, staged_count: 0, unmatched_count: 0,
      result: 'no_match', error_summary: '候选包中没有当前目标集',
    }] : [],
    final_subtitle_path: task.status === 'success' ? `${task.target_path.replace(/\.mkv$/i, '')}.chi.zh-cn.ass` : null,
    record_ids: task.status === 'success' ? ['record-matched'] : [],
    record_counts: task.status === 'success'
      ? { matched: 1, staged: 1, unmatched: 0 }
      : {} as Record<string, number>,
    warning_summaries: task.warning_count ? ['包内另一个字幕文件已暂存到插件数据目录。'] : [],
    manual_source: null,
    manual_candidate_key: null,
    manual_candidate_summary: {},
    actual_search_query: null,
  }]))

  const records: RecordListItem[] = [
    {
      id: 'record-matched', subtitle_file_name: 'Between.Mountains.2022.1080p.chi.zh-cn.ass', format: 'ASS', size: 183_200,
      media_title: '山海之间', year: 2022, media_type: 'movie', season: null, episode: null, status: 'matched', source: 'moviepilot', package_scope: 'episode', location: 'media_directory', path: '/media/电影/山海之间/Between.Mountains.2022.1080p.chi.zh-cn.ass', current_file_path: '/media/电影/山海之间/Between.Mountains.2022.1080p.chi.zh-cn.ass', target_history_id: 4101, history_target_path: '/legacy/media/电影/山海之间/Between.Mountains.2022.1080p.mkv', target_path: '/media/电影/山海之间/Between.Mountains.2022.1080p.mkv', created_at: iso(220_000), updated_at: iso(210_000), consumed_at: null,
    },
    {
      id: 'record-staged', subtitle_file_name: 'Example.Show.S01E04.zh-cn.srt', format: 'SRT', size: 76_800,
      media_title: '示例剧集', year: 2024, media_type: 'tv', season: 1, episode: 4, status: 'staged', source: 'assrt', package_scope: 'season_pack', location: 'plugin_data', path: '/config/plugins/subtitleassistant/staged/record-staged.srt', current_file_path: '/config/plugins/subtitleassistant/staged/record-staged.srt', target_history_id: null, history_target_path: null, target_path: null, created_at: iso(180_000), updated_at: iso(170_000), consumed_at: null,
    },
    {
      id: 'record-unmatched', subtitle_file_name: 'unknown.zh-cn.srt', format: 'SRT', size: 38_400,
      media_title: null, year: null, media_type: 'unknown', season: null, episode: null, status: 'unmatched', source: 'opensubtitles', package_scope: 'unknown', location: 'plugin_data', path: '/config/plugins/subtitleassistant/unmatched/unknown.zh-cn.srt', current_file_path: '/config/plugins/subtitleassistant/unmatched/unknown.zh-cn.srt', target_history_id: null, history_target_path: null, target_path: null, created_at: iso(680_000), updated_at: iso(670_000), consumed_at: null,
    },
  ]
  const recordDetails: Record<string, RecordDetail> = Object.fromEntries(records.map(record => [record.id, {
    ...record,
    canonical_identity_type: record.media_title ? 'tmdb' : null,
    canonical_identity_value: record.media_title ? '67890' : null,
    tmdb_id: record.media_title ? 67890 : null,
    imdb_id: null,
    matched_path_mapping: record.id === 'record-matched' ? { source_prefix: '/legacy/media', target_prefix: '/media' } : null,
    final_subtitle_path: record.location === 'media_directory' ? record.path : null,
    source_task_id: record.id === 'record-matched' ? 'task-success' : 'task-processing',
    consumed_task_id: record.consumed_at ? 'task-processing' : null,
    candidate_key: `demo-${record.id}`,
    candidate_name: record.id === 'record-unmatched' ? null : '示例字幕候选',
    language: 'zh-CN', translation_type: 'human', hearing_impaired: false,
    candidate_attribution_snapshot: record.media_title ? { media_type: record.media_type, tmdb_id: 67890, imdb_id: null, seasons: record.season == null ? [] : [record.season], episodes: record.episode == null ? [] : [record.episode], package_scope: record.package_scope, evidence: ['候选标题'] } : null,
    logical_source_path: record.id === 'record-staged' ? 'Example.Show.S01.Complete.zip/Season 01/Example.Show.S01E04.zh-cn.srt' : record.subtitle_file_name,
    file_attribution_method: record.id === 'record-matched' ? 'direct_file' : 'trust_package',
    host_recognition_summary: record.media_title ? { matched: true, identity: 'tmdb:67890' } : { matched: false },
    season_evidence: record.media_type === 'tv' ? 'path' : 'not_applicable',
    episode_evidence: record.media_type === 'tv' ? 'path' : 'not_applicable',
    unmatched_reason: record.status === 'unmatched' ? 'media_unrecognized' : null,
    target_file_exists: record.target_path ? false : null,
    staged_at: record.status === 'staged' ? record.created_at : null,
    retarget_history: [],
  }]))

  const sources: SourceStatusItem[] = [
    { source: 'moviepilot', enabled: true, configured: true, health: 'healthy', last_checked_at: iso(90_000), last_success_at: iso(95_000), last_error_at: null, last_error_summary: null, last_duration_ms: 420, details: { site_count: 4, site_names: ['站点 A', '站点 B', '站点 C', '站点 D'], total_candidates: 5, last_search_duration_ms: 820 } },
    { source: 'opensubtitles', enabled: false, configured: false, health: 'disabled', last_checked_at: null, last_success_at: null, last_error_at: null, last_error_summary: null, last_duration_ms: null, details: { session_active: false } },
    { source: 'assrt', enabled: true, configured: true, health: 'limited', last_checked_at: iso(70_000), last_success_at: iso(80_000), last_error_at: iso(65_000), last_error_summary: '达到当前窗口请求上限，稍后可重试。', last_duration_ms: 1200, details: { quota: '3 / 5', cooldown_until: iso(-20_000), last_request_at: iso(70_000) } },
  ]

  const targets: TargetItem[] = tasks.map(task => ({
    history_id: task.target_history_id ?? task.id,
    media_title: task.media_title,
    year: task.year,
    media_type: task.media_type,
    season: task.season,
    episode: task.episode,
    tmdb_id: taskDetails[task.id]?.tmdb_id ?? null,
    imdb_id: taskDetails[task.id]?.imdb_id ?? null,
    target_file_name: task.target_file_name,
    target_path: task.history_target_path || task.target_path,
    organized_at: task.finished_at || task.created_at,
    search_plans: {
      moviepilot: [{ kind: 'title', label: '英文关键词', query: 'Example', editable: true }],
      opensubtitles: [{ kind: 'id', label: '媒体 ID', query: taskDetails[task.id]?.tmdb_id ? String(taskDetails[task.id].tmdb_id) : null, editable: false }, { kind: 'title', label: '英文标题', query: 'Example Show', editable: true }],
      assrt: [{ kind: 'title', label: '中文标题', query: task.media_title, editable: true }],
    },
  }))

  return {
    mode: 'normal',
    config: {
      plugin_id: 'SubtitleAssistant', enabled: true, moviepilot_enabled: true, opensubtitles_enabled: false, assrt_enabled: true,
      opensubtitles_configured: false, assrt_configured: true, allow_machine_translation: false, ai_attribution_takeover_enabled: false, host_ai_enabled: true, max_candidate_attempts: 3,
      source_priority: ['moviepilot', 'assrt', 'opensubtitles'], format_priority: ['ASS', 'SSA', 'SRT', 'SUP'],
      path_mappings: [{ source_prefix: '/legacy/media', target_prefix: '/media' }], package_attribution_strategy: 'trust_package',
      allowed_formats: ['ASS', 'SSA', 'SRT', 'SUP'],
    },
    tasks, taskDetails, records, recordDetails, targets, sources,
  }
}

function wait(milliseconds: number): Promise<void> {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds))
}

export function createMockApi() {
  const state = reactiveState(seedState())
  const api: PluginApi = {
    async get<T = unknown>(path: string, options?: Record<string, unknown>): Promise<T> {
      await wait(state.mode === 'error' ? 120 : 240)
      if (state.mode === 'error') throw new Error('开发壳模拟服务暂时不可用')
      if (path.includes('/tasks/')) return clone(state.taskDetails[path.split('/').pop() || '']) as T
      if (path.endsWith('/tasks')) return page(state.mode === 'empty' ? [] : state.tasks, options) as T
      if (path.includes('/records/')) return clone(state.recordDetails[path.split('/').pop() || '']) as T
      if (path.endsWith('/records')) return page(state.mode === 'empty' ? [] : state.records, options) as T
      if (path.endsWith('/targets')) return page(state.mode === 'empty' ? [] : state.targets, options) as T
      if (path.endsWith('/sources/status')) return clone(state.mode === 'empty' ? [] : state.sources) as T
      if (path.includes('/plugin/form/')) return { render_mode: 'vue', model: clone(state.config) } as T
      return {} as T
    },
    async post<T = unknown>(path: string, payload?: unknown): Promise<T> {
      await wait(300)
      if (state.mode === 'error') throw new Error('开发壳模拟服务暂时不可用')
      if (path.endsWith('/sources/refresh')) return { success: true, message: '字幕源状态已刷新' } as T
      if (path.endsWith('/searches')) {
        const targetId = (payload as { target_history_id?: string | number } | undefined)?.target_history_id
        const target = state.targets.find(item => String(item.history_id) === String(targetId))
        if (!target) throw new Error('整理历史目标不存在')
        return mockSearchResponse(target) as T
      }
      if (path.includes('/searches/') && path.endsWith('/downloads')) {
        return { task_id: state.tasks[0].id, reused: false, task: clone(state.tasks[0]) } as T
      }
      if (path.endsWith('/records/batch-retarget-preview')) {
        const mappings = ((payload as { items?: Array<{ record_id: string; target_history_id?: string | number | null }> } | undefined)?.items || [])
        if (!isValidBatch(mappings.map(item => item.record_id))) {
          throw mockHttpError(422, `改配记录数量必须为 1 至 ${MAX_RECORD_BATCH_SIZE} 条，且不能重复。`)
        }
        const items = mappings.map(mapping => {
          const record = state.recordDetails[mapping.record_id]
          const target = mapping.target_history_id == null
            ? suggestTarget(record, state.targets)
            : targetFromPayload(state.targets, { target_history_id: mapping.target_history_id })
          if (!record) return { record_id: mapping.record_id, current_subtitle_path: null, target_history_id: mapping.target_history_id ?? null, target: null, preview: null, executable: false, error_code: 'record_not_found', message: '匹配记录不存在' }
          if (!target) {
            const targetMissing = mapping.target_history_id != null
            return { record_id: mapping.record_id, current_subtitle_path: record.path, target_history_id: mapping.target_history_id ?? null, target: null, preview: null, executable: false, error_code: targetMissing ? 'target_not_found' : 'target_required', message: targetMissing ? '整理历史目标不存在' : '请选择整理历史目标' }
          }
          const preview = retargetPreview(state.config, record, target)
          const sameTarget = normalizeMockPath(record.final_subtitle_path || record.path) === normalizeMockPath(preview.final_subtitle_path)
          return {
            record_id: mapping.record_id,
            current_subtitle_path: record.path,
            target_history_id: target.history_id,
            target: clone(target),
            preview,
            executable: !sameTarget,
            error_code: sameTarget ? 'same_target' : null,
            message: sameTarget ? '预计最终字幕路径与当前路径相同' : null,
          }
        })
        const destinationCounts = new Map<string, number>()
        for (const item of items) {
          if (item.executable && item.preview) {
            const key = normalizeMockPath(item.preview.final_subtitle_path)
            destinationCounts.set(key, (destinationCounts.get(key) || 0) + 1)
          }
        }
        const checkedItems = items.map(item => {
          if (!item.executable || !item.preview) return item
          const key = normalizeMockPath(item.preview.final_subtitle_path)
          if ((destinationCounts.get(key) || 0) < 2) return item
          return { ...item, executable: false, error_code: 'batch_destination_conflict', message: '批次中多条记录将写入同一最终字幕路径' }
        })
        return { executable: checkedItems.length > 0 && checkedItems.every(item => item.executable), items: checkedItems } satisfies BatchRetargetPreviewResponse as T
      }
      if (path.endsWith('/records/batch-retarget')) {
        const mappings = ((payload as { items?: Array<{ record_id: string; target_history_id: string | number }> } | undefined)?.items || [])
        if (!isValidBatch(mappings.map(item => item.record_id))) {
          throw mockHttpError(422, `改配记录数量必须为 1 至 ${MAX_RECORD_BATCH_SIZE} 条，且不能重复。`)
        }
        const items = mappings.map(mapping => {
          const record = state.recordDetails[mapping.record_id]
          const target = targetFromPayload(state.targets, mapping)
          if (!record || !target) return { record_id: mapping.record_id, target_history_id: mapping.target_history_id, success: false, error_code: 'target_not_found', message: '记录或整理历史目标不存在', consistency_risk: false, record: null }
          const preview = retargetPreview(state.config, record, target)
          const updated = applyMockRetarget(record, target, preview)
          state.recordDetails[mapping.record_id] = updated
          state.records = state.records.map(item => item.id === mapping.record_id ? updated : item)
          return { record_id: mapping.record_id, target_history_id: target.history_id, success: true, error_code: null, message: null, consistency_risk: false, record: clone(updated) }
        })
        const result = { success_count: items.filter(item => item.success).length, failure_count: items.filter(item => !item.success).length, items }
        return result satisfies BatchRetargetResponse as T
      }
      if (path.endsWith('/records/batch-delete')) {
        const request = payload as {
          delete_mode?: RecordDeleteMode
          items?: Array<{
            record_id: string
            expected_status: RecordListItem['status']
            expected_location: RecordListItem['location']
            expected_path: string
            expected_updated_at: string
          }>
        } | undefined
        const deleteMode = request?.delete_mode
        const deleteItems = request?.items || []
        if ((deleteMode !== 'record_only' && deleteMode !== 'record_and_file') || !isValidBatch(deleteItems.map(item => item.record_id))) {
          throw mockHttpError(422, `删除记录数量必须为 1 至 ${MAX_RECORD_BATCH_SIZE} 条，且不能重复。`)
        }
        const selectedIds = new Set(deleteItems.map(item => item.record_id))
        for (const item of deleteItems) {
          const record = state.recordDetails[item.record_id]
          if (!record || !matchesDeleteSnapshot(record, item)) {
            throw mockHttpError(409, '匹配记录在确认后已发生变化，请刷新后重新选择。')
          }
          if (deleteMode === 'record_only' && record.status !== 'matched') {
            throw mockHttpError(409, '暂存和未匹配记录必须同时删除当前字幕文件。')
          }
          if (deleteMode === 'record_and_file') {
            const shared = state.records.some(other => other.id !== record.id && normalizeMockPath(other.current_file_path || other.path) === normalizeMockPath(record.current_file_path || record.path))
            if (shared || (selectedIds.has(record.id) && state.records.filter(other => normalizeMockPath(other.current_file_path || other.path) === normalizeMockPath(record.current_file_path || record.path)).length > 1)) {
              throw mockHttpError(409, '当前字幕文件仍被其他匹配记录引用，请刷新后重新选择。')
            }
          }
        }
        const resultItems = deleteItems.map(item => ({
          record_id: item.record_id,
          status: 'success' as const,
          error_code: null,
          message: deleteMode === 'record_only' ? '匹配记录已删除，字幕文件已保留。' : '匹配记录及当前字幕文件已删除。',
          consistency_risk: false,
        }))
        for (const item of deleteItems) {
          state.records = state.records.filter(record => record.id !== item.record_id)
          delete state.recordDetails[item.record_id]
        }
        return {
          success_count: resultItems.length,
          failure_count: 0,
          not_executed_count: 0,
          items: resultItems,
        } satisfies BatchRecordDeleteResponse as T
      }
      return { success: true, message: '开发壳操作完成' } as T
    },
    async put<T = unknown>(path: string, payload?: unknown): Promise<T> {
      await wait(300)
      if (state.mode === 'error') throw new Error('开发壳模拟服务暂时不可用')
      if (path.includes('/credentials/')) return { success: true, data: { configured: true }, message: '凭据已更新' } as T
      if (path.includes('/plugin/')) state.config = { ...state.config, ...(payload as Partial<ConfigModel>) }
      return { success: true, message: '配置已保存' } as T
    },
    async delete<T = unknown>(path: string, options?: Record<string, unknown>): Promise<T> {
      await wait(260)
      if (state.mode === 'error') throw new Error('开发壳模拟服务暂时不可用')
      if (path.includes('/credentials/')) return { success: true, message: '凭据已清除' } as T
      if (path.includes('/tasks/')) {
        const id = path.split('/').pop() || ''
        state.tasks = state.tasks.filter(item => item.id !== id)
        delete state.taskDetails[id]
      }
      if (path.includes('/records/')) {
        const id = path.split('/').pop() || ''
        const record = state.recordDetails[id]
        const request = (options?.data || {}) as {
          delete_mode?: RecordDeleteMode
          expected_status?: RecordListItem['status']
          expected_location?: RecordListItem['location']
          expected_path?: string
          expected_updated_at?: string
        }
        if (!record) throw mockHttpError(404, '匹配记录不存在')
        if ((request.delete_mode !== 'record_only' && request.delete_mode !== 'record_and_file') || !matchesDeleteSnapshot(record, request)) {
          throw mockHttpError(409, '匹配记录在确认后已发生变化，请刷新后重新确认删除。')
        }
        if (request.delete_mode === 'record_only' && record.status !== 'matched') {
          throw mockHttpError(409, '暂存和未匹配记录必须同时删除当前字幕文件。')
        }
        state.records = state.records.filter(item => item.id !== id)
        delete state.recordDetails[id]
      }
      return { success: true, message: '记录已删除' } as T
    },
  }
  return { api, state }
}

function page<T>(items: T[], options?: Record<string, unknown>): { items: T[]; total: number; page: number; page_size: 25 | 50 | 100 } {
  const params = (options?.params || {}) as Record<string, unknown>
  const requestedPage = Math.max(1, Number(params.page) || 1)
  const requestedSize = [25, 50, 100].includes(Number(params.page_size)) ? Number(params.page_size) as 25 | 50 | 100 : 25
  const query = typeof params.search === 'string' ? params.search.trim().toLowerCase() : ''
  const status = typeof params.status === 'string' ? params.status : ''
  const filtered = items.filter(item => {
    const matchesStatus = !status || (item as { status?: string }).status === status
    const haystack = JSON.stringify(item).toLowerCase()
    return matchesStatus && (!query || haystack.includes(query))
  })
  const start = (requestedPage - 1) * requestedSize
  return { items: clone(filtered.slice(start, start + requestedSize)), total: filtered.length, page: requestedPage, page_size: requestedSize }
}

function isValidBatch(recordIds: string[]): boolean {
  return recordIds.length >= 1
    && recordIds.length <= MAX_RECORD_BATCH_SIZE
    && new Set(recordIds).size === recordIds.length
}

function matchesDeleteSnapshot(
  record: RecordListItem,
  snapshot: {
    expected_status?: RecordListItem['status']
    expected_location?: RecordListItem['location']
    expected_path?: string
    expected_updated_at?: string
  },
): boolean {
  return record.status === snapshot.expected_status
    && record.location === snapshot.expected_location
    && record.path === snapshot.expected_path
    && record.updated_at === snapshot.expected_updated_at
}

function mockHttpError(status: number, message: string): Error & { response: { status: number; data: { detail: string } } } {
  const error = new Error(message) as Error & { response: { status: number; data: { detail: string } } }
  error.response = { status, data: { detail: message } }
  return error
}

function targetFromPayload(targets: TargetItem[], payload: unknown): TargetItem | undefined {
  const historyId = (payload as { target_history_id?: string | number } | undefined)?.target_history_id
  return targets.find(item => String(item.history_id) === String(historyId))
}

function normalizeMockPath(path: string): string {
  return path.replaceAll('\\', '/').replace(/\/+/g, '/').replace(/\/$/, '').toLowerCase()
}

function exactTargetMatch(record: RecordDetail, target: TargetItem): boolean {
  if (record.media_type === 'unknown' || target.media_type === 'unknown' || record.media_type !== target.media_type) return false
  const recordImdb = (record.imdb_id || '').trim().toLowerCase()
  const targetImdb = (target.imdb_id || '').trim().toLowerCase()
  const hasCommonTmdb = record.tmdb_id != null && target.tmdb_id != null
  const hasCommonImdb = Boolean(recordImdb && targetImdb)
  if (hasCommonTmdb && record.tmdb_id !== target.tmdb_id) return false
  if (hasCommonImdb && recordImdb !== targetImdb) return false
  const identityMatches = (hasCommonTmdb && record.tmdb_id === target.tmdb_id)
    || (!hasCommonTmdb && hasCommonImdb && recordImdb === targetImdb)
  if (!identityMatches) return false
  if (record.media_type === 'tv') {
    return record.season != null
      && record.episode != null
      && record.season === target.season
      && record.episode === target.episode
  }
  return true
}

function suggestTarget(record: RecordDetail | undefined, targets: TargetItem[]): TargetItem | undefined {
  if (!record) return undefined
  const matches = targets.filter(target => exactTargetMatch(record, target))
  return matches.length === 1 ? matches[0] : undefined
}

function retargetPreview(config: ConfigModel, record: RecordDetail, target: TargetItem) {
  const historyPath = target.target_path
  const mapping = config.path_mappings
    .filter(item => historyPath === item.source_prefix || historyPath.startsWith(`${item.source_prefix}/`))
    .sort((left, right) => right.source_prefix.length - left.source_prefix.length)[0]
  const targetPath = mapping
    ? `${mapping.target_prefix}${historyPath.slice(mapping.source_prefix.length)}`
    : historyPath
  const basePath = targetPath.replace(/\.[^./]+$/, '')
  return {
    target_history_id: target.history_id,
    history_target_path: historyPath,
    target_path: targetPath,
    final_subtitle_path: `${basePath}.chi.zh-cn.${record.format.toLowerCase()}`,
    directory_available: true,
    directory_error: null,
  }
}

function applyMockRetarget(record: RecordDetail, target: TargetItem, preview: ReturnType<typeof retargetPreview>): RecordDetail {
  const operatedAt = new Date().toISOString()
  return {
    ...record,
    media_title: target.media_title,
    year: target.year,
    media_type: target.media_type,
    season: target.season,
    episode: target.episode,
    tmdb_id: target.tmdb_id,
    imdb_id: target.imdb_id,
    target_history_id: target.history_id,
    history_target_path: preview.history_target_path,
    target_path: preview.target_path,
    final_subtitle_path: preview.final_subtitle_path,
    path: preview.final_subtitle_path,
    current_file_path: preview.final_subtitle_path,
    subtitle_file_name: preview.final_subtitle_path.split('/').pop() || record.subtitle_file_name,
    status: 'matched',
    location: 'media_directory',
    updated_at: operatedAt,
    retarget_history: [...(record.retarget_history || []), {
      operated_at: operatedAt,
      old_target_history_id: record.target_history_id,
      new_target_history_id: target.history_id,
      old_history_target_path: record.history_target_path,
      new_history_target_path: preview.history_target_path,
      old_target_path: record.target_path,
      new_target_path: preview.target_path,
      old_subtitle_path: record.path,
      new_subtitle_path: preview.final_subtitle_path,
    }],
  }
}

function mockSearchResponse(target: TargetItem): SearchResponse {
  return {
    session_id: 'mock-search-session',
    target: clone(target),
    sources: [
      {
        source: 'moviepilot', status: 'success', default_plans: target.search_plans.moviepilot, executed_queries: ['Example'], matched_query: 'Example', candidate_count: 1, duration_ms: 320, error_summary: null, details: { cache_hit: false },
        candidates: [{ candidate_key: 'mock-moviepilot', name: '示例字幕候选', file_name: null, source: 'moviepilot', language: 'zh-CN', format: null, package_scope: 'season_pack', season: target.season, episode: null, seasons: target.season == null ? [] : [target.season], episodes: [], translation_type: 'human', hearing_impaired: false, rating: null, votes: null, downloads: null, uploaded_at: null, query: 'Example', source_details: { site_name: '示例站点' } }],
      },
      { source: 'opensubtitles', status: 'success', default_plans: target.search_plans.opensubtitles, executed_queries: ['Example Show'], matched_query: null, candidate_count: 0, duration_ms: 410, error_summary: null, details: { cache_hit: true, page_count: 1 }, candidates: [] },
      { source: 'assrt', status: 'success', default_plans: target.search_plans.assrt, executed_queries: [target.media_title], matched_query: null, candidate_count: 0, duration_ms: 280, error_summary: null, details: { cache_hit: false }, candidates: [] },
    ],
  }
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

function reactiveState(initial: MockState): MockState {
  return reactive(initial) as MockState
}
