"""字幕助手核心领域模型。"""

from datetime import UTC, datetime
from typing import Any, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from .enums import (
    AI_ATTRIBUTION_EVIDENCE_CODES,
    AiAttributionConfidence,
    AiAttributionDecision,
    AiAttributionOutcome,
    AttemptResult,
    AttributionEvidence,
    CandidateRecognitionStatus,
    FileAttributionMethod,
    FileLocation,
    MediaType,
    PackageAttributionStrategy,
    PackageScope,
    RecordStatus,
    SourceHealth,
    SourceRunStatus,
    SubtitleSource,
    TaskStage,
    TaskStatus,
    TaskTrigger,
    TranslationType,
    UnmatchedReason,
)

SourceDetails: TypeAlias = dict[str, JsonValue]


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""

    return datetime.now(UTC)


def new_id() -> str:
    """生成 UUIDv4 字符串标识。"""

    return str(uuid4())


def elapsed_ms(start: datetime | None, end: datetime | None = None) -> int | None:
    """计算两个时间点之间的毫秒数。"""

    if start is None:
        return None
    finish = end or utc_now()
    return max(0, int((finish - start).total_seconds() * 1000))


class StrictModel(BaseModel):
    """拒绝未知字段的领域模型基类。"""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MediaContext(StrictModel):
    """一次字幕任务所需的安全媒体上下文。"""

    title: str
    original_title: str | None = None
    english_title: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_path: str
    target_file_name: str
    target_storage: str | None = None

    @property
    def canonical_identity(self) -> tuple[str, str] | None:
        """返回库存使用的规范媒体身份。"""

        if self.tmdb_id is not None:
            return "tmdb", str(self.tmdb_id)
        if self.imdb_id:
            return "imdb", self.imdb_id.strip().lower()
        return None


class SubtitleCandidate(StrictModel):
    """不含下载定位和敏感字段的字幕候选。"""

    stable_key: str
    source: SubtitleSource
    name: str
    file_name: str | None = None
    format: str
    language: str
    translation_type: TranslationType = TranslationType.UNKNOWN
    hearing_impaired: bool = False
    foreign_parts_only: bool = False
    package_scope: PackageScope = PackageScope.UNKNOWN
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[int] = Field(default_factory=list)
    tmdb_id: int | None = None
    imdb_id: str | None = None
    exact_id_match: bool = False
    site_id: int | None = None
    site_priority: int | None = None
    trusted: bool = False
    score: float | None = None
    votes: int | None = None
    download_count: int | None = None
    uploaded_at: datetime | None = None
    revision: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateRecognition(StrictModel):
    """人工搜索候选及其对当前目标的识别状态。"""

    candidate: SubtitleCandidate
    status: CandidateRecognitionStatus


class PathMappingSnapshot(StrictModel):
    """一次任务实际命中的整理历史路径映射。"""

    source_prefix: str
    target_prefix: str


class CandidateAttributionSnapshot(StrictModel):
    """下载前只由候选自身事实形成的不可变归属快照。"""

    media_type: MediaType = MediaType.UNKNOWN
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[int] = Field(default_factory=list)
    package_scope: PackageScope = PackageScope.UNKNOWN
    evidence: list[str] = Field(default_factory=list)

    @property
    def canonical_identity(self) -> tuple[str, str] | None:
        """返回候选自身可比较的规范媒体身份。"""

        if self.tmdb_id is not None:
            return "tmdb", str(self.tmdb_id)
        if self.imdb_id:
            return "imdb", self.imdb_id.strip().lower()
        return None


class AiAttributionSuggestion(StrictModel):
    """LLM 返回的单项字幕归属建议。"""

    item_id: str = Field(pattern=r"^item_[0-9]{3}$")
    decision: AiAttributionDecision
    media_type: MediaType
    tmdb_id: int | None = Field(ge=0, le=2_147_483_647, strict=True)
    imdb_id: str | None = Field(max_length=64, pattern=r"^(?:tt)?[0-9]{1,20}$")
    season: int | None = Field(ge=0, le=9999, strict=True)
    episode: int | None = Field(ge=0, le=9999, strict=True)
    confidence: AiAttributionConfidence
    evidence_codes: list[str] = Field(min_length=1, max_length=12)

    @field_validator("tmdb_id")
    @classmethod
    def _validate_nonnegative_number(cls, value: int | None) -> int | None:
        """限制模型返回的结构化数字范围。"""

        if value is not None and (value < 0 or value > 2_147_483_647):
            raise ValueError("结构化数字超出允许范围")
        return value

    @field_validator("season", "episode")
    @classmethod
    def _validate_scope_number(cls, value: int | None) -> int | None:
        """限制模型返回的季集数字范围。"""

        if value is not None and (value < 0 or value > 9999):
            raise ValueError("季集数字超出允许范围")
        return value

    @field_validator("imdb_id")
    @classmethod
    def _validate_imdb_format(cls, value: str | None) -> str | None:
        """限制模型返回的 IMDb ID 形态。"""

        if value is not None:
            import re

            if not re.fullmatch(r"(?:tt)?[0-9]{1,20}", value.strip(), re.IGNORECASE):
                raise ValueError("IMDb ID 格式无效")
        return value


class AiAttributionAudit(StrictModel):
    """一次字幕 AI 接管尝试的脱敏、强类型审计。"""

    attempted_at: datetime = Field(default_factory=utc_now)
    strategy_version: str = Field(default="1", max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    before_strategy: PackageAttributionStrategy
    original_unmatched_reason: UnmatchedReason | None = None
    trigger_reason: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    outcome: AiAttributionOutcome
    reason_code: str = Field(max_length=64, pattern=r"^[a-z0-9_]+$")
    media_type: MediaType = MediaType.UNKNOWN
    tmdb_id: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    imdb_id: str | None = Field(
        default=None,
        max_length=64,
        pattern=r"^(?:tt)?[0-9]{1,20}$",
    )
    season: int | None = Field(default=None, ge=0, le=9999, strict=True)
    episode: int | None = Field(default=None, ge=0, le=9999, strict=True)
    confidence: AiAttributionConfidence | None = None
    evidence_codes: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("evidence_codes")
    @classmethod
    def _validate_evidence_codes(cls, values: list[str]) -> list[str]:
        """限制审计证据码为固定白名单且不得重复。"""

        if any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in values):
            raise ValueError("AI 审计证据码不在允许集合中")
        if len(set(values)) != len(values):
            raise ValueError("AI 审计证据码不能重复")
        return values

    @property
    def result(self) -> AiAttributionOutcome:
        """兼容 API 文档使用的 result 命名。"""

        return self.outcome


class FileAttributionEvidence(StrictModel):
    """一个下载后字幕文件的安全归属证据。"""

    logical_source_path: str
    method: FileAttributionMethod
    belongs_to_target_media: bool | None = None
    media_type: MediaType = MediaType.UNKNOWN
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    season: int | None = None
    episode: int | None = None
    # 保留宿主/路径解析的最终集合基数，供 AI 判断“缺失/多值”而不压缩为 None。
    # 领域证据必须保留宿主解析出的完整集合；AI 请求白名单另行限制集合大小。
    season_values: list[int] = Field(default_factory=list)
    episode_values: list[int] = Field(default_factory=list)
    season_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    episode_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    unmatched_reason: UnmatchedReason | None = None
    host_recognition_summary: dict[str, Any] = Field(default_factory=dict)
    ai_takeover_audit: AiAttributionAudit | None = None
    ai_before_method: FileAttributionMethod | None = None
    ai_before_unmatched_reason: UnmatchedReason | None = None


class StageTrace(StrictModel):
    """字幕任务单阶段执行轨迹。"""

    stage: TaskStage
    started_at: datetime
    finished_at: datetime | None = None
    duration_ms: int | None = None
    summary: str | None = None


class SourceRun(StrictModel):
    """单个字幕源在任务中的安全执行摘要。"""

    source: SubtitleSource
    status: SourceRunStatus
    candidate_count: int = 0
    raw_count: int = 0
    admitted_count: int = 0
    media_matched_count: int = 0
    rejected_count: int = 0
    rejection_summary: dict[str, int] = Field(default_factory=dict)
    duration_ms: int | None = None
    error_summary: str | None = None
    details: SourceDetails = Field(default_factory=dict)


class CandidateAttempt(StrictModel):
    """一次候选下载与落盘尝试的安全摘要。"""

    candidate_key: str
    source: SubtitleSource
    package_scope: PackageScope
    language: str
    format: str
    translation_type: TranslationType
    hearing_impaired: bool
    attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    candidate_snapshot: CandidateAttributionSnapshot | None = None
    extracted_count: int = 0
    current_target_count: int = 0
    same_media_other_episode_count: int = 0
    ambiguous_count: int = 0
    other_media_count: int = 0
    written_count: int = 0
    staged_count: int = 0
    unmatched_count: int = 0
    result: AttemptResult
    error_summary: str | None = None
    ai_attempt_count: int = 0
    ai_accepted_count: int = 0
    ai_rejected_count: int = 0
    ai_error_count: int = 0
    ai_over_limit_count: int = 0
    ai_reason_summary: dict[str, int] = Field(default_factory=dict)


class SubtitleTask(StrictModel):
    """持久化的字幕任务。"""

    id: str = Field(default_factory=new_id)
    trigger: TaskTrigger = TaskTrigger.TRANSFER_EVENT
    media_title: str
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_file_name: str
    target_path: str
    target_history_id: int | None = None
    history_target_path: str | None = None
    matched_path_mapping: PathMappingSnapshot | None = None
    target_file_exists: bool | None = None
    target_storage: str | None = None
    package_attribution_strategy: PackageAttributionStrategy = PackageAttributionStrategy.TRUST_PACKAGE
    candidate_attribution_snapshot: CandidateAttributionSnapshot | None = None
    status: TaskStatus = TaskStatus.QUEUED
    stage: TaskStage | None = None
    reason_code: str | None = None
    reason_message: str | None = None
    result_source: SubtitleSource | None = None
    result_package_scope: PackageScope | None = None
    result_format: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    warning_count: int = 0
    warning_summaries: list[str] = Field(default_factory=list)
    existing_subtitle_check: dict[str, Any] = Field(default_factory=dict)
    inventory_result: dict[str, Any] = Field(default_factory=dict)
    stage_traces: list[StageTrace] = Field(default_factory=list)
    source_runs: list[SourceRun] = Field(default_factory=list)
    candidate_attempts: list[CandidateAttempt] = Field(default_factory=list)
    final_subtitle_path: str | None = None
    record_ids: list[str] = Field(default_factory=list)
    record_counts: dict[str, int] = Field(default_factory=dict)
    manual_source: SubtitleSource | None = None
    manual_candidate_key: str | None = None
    manual_candidate_summary: dict[str, Any] = Field(default_factory=dict)
    actual_search_query: str | None = None

    @property
    def is_terminal(self) -> bool:
        """判断任务是否已经进入终态。"""

        return self.status in {
            TaskStatus.SUCCESS,
            TaskStatus.SKIPPED,
            TaskStatus.FAILED,
            TaskStatus.INTERRUPTED,
        }


class RetargetHistoryEntry(StrictModel):
    """一次成功改配目标的审计记录。"""

    operated_at: datetime = Field(default_factory=utc_now)
    old_target_history_id: int | None = None
    new_target_history_id: int | None = None
    old_history_target_path: str | None = None
    new_history_target_path: str | None = None
    old_target_path: str | None = None
    new_target_path: str
    old_subtitle_path: str
    new_subtitle_path: str


class MatchRecord(StrictModel):
    """已落盘、暂存或未匹配字幕的持久记录。"""

    id: str = Field(default_factory=new_id)
    subtitle_file_name: str
    format: str
    size: int | None = None
    media_title: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: int | None = None
    episode: int | None = None
    status: RecordStatus
    source: SubtitleSource
    package_scope: PackageScope = PackageScope.UNKNOWN
    location: FileLocation
    path: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    staged_at: datetime | None = None
    consumed_at: datetime | None = None
    canonical_identity_type: str | None = None
    canonical_identity_value: str | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_history_id: int | None = None
    history_target_path: str | None = None
    target_path: str | None = None
    matched_path_mapping: PathMappingSnapshot | None = None
    target_file_exists: bool | None = None
    final_subtitle_path: str | None = None
    source_task_id: str
    consumed_task_id: str | None = None
    candidate_key: str
    candidate_name: str | None = None
    candidate_attribution_snapshot: CandidateAttributionSnapshot | None = None
    logical_source_path: str | None = None
    file_attribution_method: FileAttributionMethod | None = None
    season_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    episode_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    unmatched_reason: UnmatchedReason | None = None
    host_recognition_summary: dict[str, Any] = Field(default_factory=dict)
    language: str
    translation_type: TranslationType = TranslationType.UNKNOWN
    hearing_impaired: bool = False
    exact_id_match: bool = False
    site_priority: int | None = None
    trusted: bool = False
    score: float | None = None
    votes: int | None = None
    download_count: int | None = None
    uploaded_at: datetime | None = None
    revision: int | None = None
    retarget_history: list[RetargetHistoryEntry] = Field(default_factory=list)
    ai_takeover_audit: AiAttributionAudit | None = None

    @property
    def inventory_key(self) -> tuple[str, str, str, int, int] | None:
        """返回暂存字幕的精确库存键。"""

        if (
            self.media_type is MediaType.UNKNOWN
            or not self.canonical_identity_type
            or not self.canonical_identity_value
            or self.season is None
            or self.episode is None
        ):
            return None
        return (
            self.media_type.value,
            self.canonical_identity_type,
            self.canonical_identity_value,
            self.season,
            self.episode,
        )


class SourceStatus(StrictModel):
    """字幕源当前状态与非敏感观测。"""

    source: SubtitleSource
    enabled: bool = False
    configured: bool = False
    health: SourceHealth = SourceHealth.PENDING
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_summary: str | None = None
    last_duration_ms: int | None = None
    details: SourceDetails = Field(default_factory=dict)


# 文档与前端历史代码曾使用 Takeover 命名；保留显式别名不复制模型结构。
AiTakeoverAudit = AiAttributionAudit
