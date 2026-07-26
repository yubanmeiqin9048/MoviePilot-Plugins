"""插件 Bearer API 的 Pydantic 请求与响应模型。"""

from datetime import datetime
from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..domain.enums import (
    AttributionEvidence,
    FileAttributionMethod,
    FileLocation,
    MediaType,
    PackageAttributionStrategy,
    PackageScope,
    RecordStatus,
    SourceHealth,
    SubtitleSource,
    TaskStage,
    TaskStatus,
    TaskTrigger,
    TranslationType,
    UnmatchedReason,
)
from ..domain.models import (
    AiAttributionAudit,
    CandidateAttempt,
    CandidateAttributionSnapshot,
    PathMappingSnapshot,
    RetargetHistoryEntry,
    SourceRun,
    StageTrace,
)


class PageSize(IntEnum):
    """分页查询允许的每页记录数。"""

    ITEMS_25 = 25
    ITEMS_50 = 50
    ITEMS_100 = 100


class ApiModel(BaseModel):
    """拒绝未知字段并支持从领域对象读取的 API 模型。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class TaskListItem(ApiModel):
    """字幕任务列表项。"""

    id: str
    trigger: TaskTrigger
    media_title: str
    year: int | None
    media_type: MediaType
    season: int | None
    episode: int | None
    target_file_name: str
    target_path: str
    target_history_id: int | None
    history_target_path: str | None
    status: TaskStatus
    stage: TaskStage | None
    reason_code: str | None
    reason_message: str | None
    result_source: SubtitleSource | None
    result_package_scope: PackageScope | None
    result_format: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    warning_count: int


class TaskDetail(TaskListItem):
    """字幕任务详情。"""

    tmdb_id: int | None
    imdb_id: str | None
    target_storage: str | None
    matched_path_mapping: PathMappingSnapshot | None
    target_file_exists: bool | None
    package_attribution_strategy: PackageAttributionStrategy
    candidate_attribution_snapshot: CandidateAttributionSnapshot | None
    existing_subtitle_check: dict[str, Any]
    inventory_result: dict[str, Any]
    stage_traces: list[StageTrace]
    source_runs: list[SourceRun]
    candidate_attempts: list[CandidateAttempt]
    final_subtitle_path: str | None
    record_ids: list[str]
    record_counts: dict[str, int]
    warning_summaries: list[str]
    manual_source: SubtitleSource | None
    manual_candidate_key: str | None
    manual_candidate_summary: dict[str, Any]
    actual_search_query: str | None


class TaskPage(ApiModel):
    """字幕任务分页响应。"""

    items: list[TaskListItem]
    total: int
    page: int
    page_size: PageSize


class RecordListItem(ApiModel):
    """字幕匹配记录列表项。"""

    id: str
    subtitle_file_name: str
    format: str
    size: int | None
    media_title: str | None
    year: int | None
    media_type: MediaType
    season: int | None
    episode: int | None
    status: RecordStatus
    source: SubtitleSource
    package_scope: PackageScope
    location: FileLocation
    path: str
    current_file_path: str = ""
    target_history_id: int | None
    history_target_path: str | None
    target_path: str | None
    created_at: datetime
    updated_at: datetime
    consumed_at: datetime | None


class RecordDetail(RecordListItem):
    """字幕匹配记录详情。"""

    canonical_identity_type: str | None
    canonical_identity_value: str | None
    tmdb_id: int | None
    imdb_id: str | None
    matched_path_mapping: PathMappingSnapshot | None
    target_file_exists: bool | None
    final_subtitle_path: str | None
    source_task_id: str
    consumed_task_id: str | None
    candidate_key: str
    candidate_name: str | None
    candidate_attribution_snapshot: CandidateAttributionSnapshot | None
    logical_source_path: str | None
    file_attribution_method: FileAttributionMethod | None
    season_evidence: AttributionEvidence
    episode_evidence: AttributionEvidence
    unmatched_reason: UnmatchedReason | None
    host_recognition_summary: dict[str, Any]
    language: str
    translation_type: TranslationType
    hearing_impaired: bool
    staged_at: datetime | None
    retarget_history: list[RetargetHistoryEntry]
    ai_takeover_audit: AiAttributionAudit | None = None


class RecordDeleteRequest(ApiModel):
    """匹配记录删除请求及用户确认时看到的版本快照。"""

    delete_mode: Literal["record_only", "record_and_file"]
    expected_status: RecordStatus
    expected_location: FileLocation
    expected_path: str = Field(min_length=1, max_length=4096)
    expected_updated_at: datetime


# 提供直观的兼容导出名，路由和外部调用均使用同一严格模型。
DeleteRecordRequest = RecordDeleteRequest


class BatchRecordDeleteConfirmation(ApiModel):
    """批量删除中的一条匹配记录确认版本。"""

    record_id: str = Field(min_length=1, max_length=128)
    expected_status: RecordStatus
    expected_location: FileLocation
    expected_path: str = Field(min_length=1, max_length=4096)
    expected_updated_at: datetime


class BatchRecordDeleteRequest(ApiModel):
    """提交一批使用统一删除模式的匹配记录确认项。"""

    delete_mode: Literal["record_only", "record_and_file"]
    items: list[BatchRecordDeleteConfirmation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_duplicate_records(self) -> "BatchRecordDeleteRequest":
        """拒绝同一匹配记录在一个删除批次中重复出现。"""

        record_ids = [item.record_id for item in self.items]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("同一匹配记录不能在批次中重复出现")
        return self


class BatchRecordDeletePreflightItem(ApiModel):
    """批量删除整体预检中的单条记录结果。"""

    record_id: str
    executable: bool
    error_code: str | None = None
    message: str | None = None


class BatchRecordDeleteResultItem(ApiModel):
    """批量删除执行后的单条记录结果。"""

    record_id: str
    status: Literal["success", "failed", "not_executed"]
    error_code: str | None = None
    message: str | None = None
    consistency_risk: bool = False


class BatchRecordDeleteResponse(ApiModel):
    """批量删除已开始执行后的逐条汇总响应。"""

    success_count: int
    failure_count: int
    not_executed_count: int
    items: list[BatchRecordDeleteResultItem]


class TargetListItem(ApiModel):
    """人工字幕搜索可选的整理目标。"""

    history_id: int | str
    media_title: str
    year: int | None = None
    media_type: MediaType
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_file_name: str
    target_path: str
    organized_at: datetime
    search_plans: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class TargetPage(ApiModel):
    """可选整理目标分页响应。"""

    items: list[TargetListItem]
    total: int
    page: int
    page_size: PageSize


class ManualSearchRequest(ApiModel):
    """人工搜索请求。"""

    target_history_id: int
    moviepilot_keyword: str | None = Field(default=None, max_length=512)
    opensubtitles_keyword: str | None = Field(default=None, max_length=512)
    assrt_keyword: str | None = Field(default=None, max_length=512)


class ManualCandidateItem(ApiModel):
    """不含任何下载定位的人工字幕候选。"""

    candidate_key: str
    source: SubtitleSource
    name: str
    file_name: str | None = None
    language: str | None = None
    format: str | None = None
    package_scope: PackageScope
    season: int | None = None
    episode: int | None = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[int] = Field(default_factory=list)
    translation_type: TranslationType
    hearing_impaired: bool
    rating: float | None = None
    votes: int | None = None
    downloads: int | None = None
    uploaded_at: datetime | None = None
    query: str | None = None
    source_details: dict[str, Any] = Field(default_factory=dict)


class ManualSourceResult(ApiModel):
    """单个来源的一次人工搜索结果。"""

    source: SubtitleSource
    status: str
    default_plans: list[dict[str, Any]] = Field(default_factory=list)
    executed_queries: list[str] = Field(default_factory=list)
    matched_query: str | None = None
    candidate_count: int = 0
    duration_ms: int | None = None
    error_summary: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    candidates: list[ManualCandidateItem] = Field(default_factory=list)


class ManualSearchResponse(ApiModel):
    """三源人工搜索响应。"""

    session_id: str | None = None
    target: TargetListItem
    sources: list[ManualSourceResult]


class ManualDownloadRequest(ApiModel):
    """提交一个人工候选下载。"""

    candidate_key: str


class ManualDownloadResponse(ApiModel):
    """人工候选入队结果。"""

    task_id: str
    reused: bool = False
    task: TaskListItem


class RetargetRequest(ApiModel):
    """改配目标请求。"""

    target_history_id: int


class RetargetPreviewResponse(ApiModel):
    """改配目标弹窗的服务端路径预览。"""

    target_history_id: int
    history_target_path: str
    target_path: str
    final_subtitle_path: str
    directory_available: bool
    directory_error: str | None = None


class BatchRetargetPreviewMapping(ApiModel):
    """批量改配预览中的一条记录与可空目标配对。"""

    record_id: str = Field(min_length=1, max_length=128)
    target_history_id: int | None = None


class BatchRetargetPreviewRequest(ApiModel):
    """请求批量改配自动建议与路径预检。"""

    items: list[BatchRetargetPreviewMapping] = Field(min_length=1, max_length=100)


class BatchRetargetSubmitMapping(ApiModel):
    """批量改配提交中的一条已确认记录目标配对。"""

    record_id: str = Field(min_length=1, max_length=128)
    target_history_id: int


class BatchRetargetSubmitRequest(ApiModel):
    """提交一批已经确认的改配映射。"""

    items: list[BatchRetargetSubmitMapping] = Field(min_length=1, max_length=100)


class BatchRetargetPreviewItem(ApiModel):
    """批量改配中单条映射的安全预览结果。"""

    record_id: str
    current_subtitle_path: str | None = None
    target_history_id: int | None = None
    target: TargetListItem | None = None
    preview: RetargetPreviewResponse | None = None
    executable: bool
    error_code: str | None = None
    message: str | None = None


class BatchRetargetPreviewResponse(ApiModel):
    """批量改配整体预检响应。"""

    executable: bool
    items: list[BatchRetargetPreviewItem]


class BatchRetargetResultItem(ApiModel):
    """批量改配中单条映射的执行响应。"""

    record_id: str
    target_history_id: int
    success: bool
    error_code: str | None = None
    message: str | None = None
    consistency_risk: bool = False
    record: RecordDetail | None = None


class BatchRetargetResponse(ApiModel):
    """批量改配执行完成后的逐条结果响应。"""

    success_count: int
    failure_count: int
    items: list[BatchRetargetResultItem]


class RecordPage(ApiModel):
    """字幕匹配记录分页响应。"""

    items: list[RecordListItem]
    total: int
    page: int
    page_size: PageSize


class SourceStatusItem(ApiModel):
    """字幕源状态安全响应项。"""

    source: SubtitleSource
    enabled: bool
    configured: bool
    health: SourceHealth
    last_checked_at: datetime | None
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_summary: str | None
    last_duration_ms: int | None
    details: dict[str, Any]


class CredentialUpdate(ApiModel):
    """外部字幕源凭据增量更新请求。"""

    api_key: str | None = Field(default=None, max_length=512)
    username: str | None = Field(default=None, max_length=512)
    password: str | None = Field(default=None, max_length=2048)
    token: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def require_nonempty_value(self) -> "CredentialUpdate":
        """要求请求至少携带一个非空更新值。"""

        if not any(
            isinstance(value, str) and bool(value.strip())
            for value in (self.api_key, self.username, self.password, self.token)
        ):
            raise ValueError("至少提供一个非空凭据字段")
        return self

    def cleaned(self) -> dict[str, str]:
        """返回去除首尾空白后的非空字段。"""

        return {
            key: value.strip() for key, value in self.model_dump().items() if isinstance(value, str) and value.strip()
        }
