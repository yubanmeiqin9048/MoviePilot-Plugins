"""字幕候选与文件归属的公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, JsonValue, field_validator

from .base import StrictModel, utc_now
from .candidate import PackageScope
from .target import MediaIdentityKind, MediaType, SubtitleTarget

__all__ = [
    "AiAttributionAudit",
    "AiAttributionConfidence",
    "AiAttributionDecision",
    "AiAttributionEvidenceCode",
    "AiAttributionOutcome",
    "AiAttributionReasonCode",
    "AiAttributionTriggerReason",
    "AttributionEvidence",
    "CandidateAttributionSnapshot",
    "CandidateMatchContext",
    "FileAttributionBatchResult",
    "FileAttributionEvidence",
    "FileAttributionMethod",
    "FileAttributionRequest",
    "PackageAttributionStrategy",
    "UnmatchedReason",
]


class PackageAttributionStrategy(StrEnum):
    """压缩包内字幕的归属策略。"""

    TRUST_PACKAGE = "trust_package"
    HOST_RECOGNITION = "host_recognition"


class FileAttributionMethod(StrEnum):
    """具体字幕文件实际采用的归属方式。"""

    DIRECT_FILE = "direct_file"
    TRUST_PACKAGE = "trust_package"
    HOST_RECOGNITION = "host_recognition"
    AI_TAKEOVER = "ai_takeover"


class AttributionEvidence(StrEnum):
    """具体字幕季集字段的证据来源。"""

    PATH = "path"
    CANDIDATE_SNAPSHOT = "candidate_snapshot"
    AI_TAKEOVER = "ai_takeover"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


class AiAttributionEvidenceCode(StrEnum):
    """AI 归属建议允许使用的固定证据码。"""

    TITLE = "title"
    ALIAS = "alias"
    YEAR = "year"
    MEDIA_TYPE = "media_type"
    TMDB_ID = "tmdb_id"
    IMDB_ID = "imdb_id"
    FILENAME = "filename"
    PATH = "path"
    SEASON = "season"
    EPISODE = "episode"
    CANDIDATE_SNAPSHOT = "candidate_snapshot"
    HOST_RECOGNITION = "host_recognition"
    PACKAGE_SCOPE = "package_scope"


AI_ATTRIBUTION_EVIDENCE_CODES = frozenset(item.value for item in AiAttributionEvidenceCode)
"""AI 审计和 HTTP 投影共用的稳定证据码集合。"""


class AiAttributionTriggerReason(StrEnum):
    """触发字幕 AI 归属接管的稳定原因。"""

    MEDIA_UNRECOGNIZED = "media_unrecognized"
    IDENTITY_MISSING = "identity_missing"
    SEASON_AMBIGUOUS = "season_ambiguous"
    EPISODE_AMBIGUOUS = "episode_ambiguous"
    APPLICATION_VALIDATION = "application_validation"


class AiAttributionReasonCode(StrEnum):
    """字幕 AI 归属审计使用的稳定结果原因码。"""

    ACCEPTED = "accepted"
    ACCEPTED_WITHOUT_IDENTITY_TITLE = "accepted_without_identity_title"
    TARGET_CONFIRMED = "target_confirmed"
    ADAPTER_ERROR = "adapter_error"
    ADAPTER_RESULT_INVALID = "adapter_result_invalid"
    AUTHORIZATION_REVOKED_BEFORE_ADOPTION = "authorization_revoked_before_adoption"
    APPLICATION_VALIDATION_FAILED = "application_validation_failed"
    APPLICATION_AUDIT_INVALID = "application_audit_invalid"
    APPLICATION_EVIDENCE_MISSING = "application_evidence_missing"
    APPLICATION_EVIDENCE_SCHEMA_INVALID = "application_evidence_schema_invalid"
    APPLICATION_METHOD_INVALID = "application_method_invalid"
    APPLICATION_LOGICAL_PATH_CHANGED = "application_logical_path_changed"
    APPLICATION_MEDIA_BINDING_INVALID = "application_media_binding_invalid"
    APPLICATION_MEDIA_TYPE_INVALID = "application_media_type_invalid"
    APPLICATION_YEAR_CHANGED = "application_year_changed"
    APPLICATION_TMDB_IDENTITY_INVALID = "application_tmdb_identity_invalid"
    APPLICATION_IMDB_IDENTITY_INVALID = "application_imdb_identity_invalid"
    APPLICATION_MOVIE_SCOPE_INVALID = "application_movie_scope_invalid"
    APPLICATION_TV_SCOPE_INCOMPLETE = "application_tv_scope_incomplete"
    APPLICATION_SCOPE_NOT_UNIQUE = "application_scope_not_unique"
    APPLICATION_LOCKED_SEASON_CHANGED = "application_locked_season_changed"
    APPLICATION_LOCKED_EPISODE_CHANGED = "application_locked_episode_changed"
    APPLICATION_SEASON_OUT_OF_CANDIDATE_SCOPE = "application_season_out_of_candidate_scope"
    APPLICATION_EPISODE_OUT_OF_CANDIDATE_SCOPE = "application_episode_out_of_candidate_scope"
    APPLICATION_SEASON_PROVENANCE_INVALID = "application_season_provenance_invalid"
    APPLICATION_EPISODE_PROVENANCE_INVALID = "application_episode_provenance_invalid"
    APPLICATION_BEFORE_METHOD_INVALID = "application_before_method_invalid"
    APPLICATION_BEFORE_REASON_INVALID = "application_before_reason_invalid"
    APPLICATION_HOST_SUMMARY_CHANGED = "application_host_summary_changed"
    APPLICATION_TRIGGER_REASON_MISMATCH = "application_trigger_reason_mismatch"
    APPLICATION_CONFIDENCE_NOT_HIGH = "application_confidence_not_high"
    APPLICATION_EVIDENCE_CODE_INVALID = "application_evidence_code_invalid"
    APPLICATION_EVIDENCE_CODE_DUPLICATE = "application_evidence_code_duplicate"
    APPLICATION_ACCEPTED_REASON_INVALID = "application_accepted_reason_invalid"
    APPLICATION_IDENTITY_TITLE_EVIDENCE_MISSING = "application_identity_title_evidence_missing"
    APPLICATION_YEAR_MISMATCH = "application_year_mismatch"
    APPLICATION_AUDIT_MISMATCH = "application_audit_mismatch"
    APPLICATION_ORIGINAL_METHOD_INVALID = "application_original_method_invalid"
    APPLICATION_ORIGINAL_MEDIA_BINDING_INVALID = "application_original_media_binding_invalid"
    APPLICATION_ORIGINAL_REASON_INVALID = "application_original_reason_invalid"
    APPLICATION_ORIGINAL_MEDIA_TYPE_INVALID = "application_original_media_type_invalid"
    DECISION_NOT_TARGET = "decision_not_target"
    CONFIDENCE_NOT_HIGH = "confidence_not_high"
    MEDIA_TYPE_MISMATCH = "media_type_mismatch"
    EVIDENCE_CODE_INVALID = "evidence_code_invalid"
    EVIDENCE_CODE_DUPLICATE = "evidence_code_duplicate"
    LOCKED_SEASON_CHANGED = "locked_season_changed"
    LOCKED_EPISODE_CHANGED = "locked_episode_changed"
    MOVIE_HAS_SEASON_EPISODE = "movie_has_season_episode"
    SEASON_EPISODE_NOT_UNIQUE = "season_episode_not_unique"
    SEASON_EPISODE_INVALID = "season_episode_invalid"
    SEASON_OUT_OF_CANDIDATE_SCOPE = "season_out_of_candidate_scope"
    EPISODE_OUT_OF_CANDIDATE_SCOPE = "episode_out_of_candidate_scope"
    TARGET_MEDIA_TYPE_UNKNOWN = "target_media_type_unknown"
    IDENTITY_CREATED = "identity_created"
    IDENTITY_TITLE_EVIDENCE_MISSING = "identity_title_evidence_missing"
    YEAR_MISMATCH = "year_mismatch"
    TMDB_ID_MISMATCH = "tmdb_id_mismatch"
    TMDB_ID_CREATED = "tmdb_id_created"
    IMDB_ID_MISMATCH = "imdb_id_mismatch"
    IMDB_ID_CREATED = "imdb_id_created"
    DUPLICATE_ITEM_ID = "duplicate_item_id"
    ITEM_SCHEMA_INVALID = "item_schema_invalid"
    MISSING_ITEM_ID = "missing_item_id"
    UNKNOWN_ITEM_ID = "unknown_item_id"
    RESPONSE_INVALID = "response_invalid"
    AUTHORIZATION_DISABLED = "authorization_disabled"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    CANDIDATE_BATCH_LIMIT = "candidate_batch_limit"
    CANDIDATE_ITEM_LIMIT = "candidate_item_limit"
    CONTEXT_OVERSIZE = "context_oversize"
    TARGET_CONTEXT_OVERSIZE = "target_context_oversize"
    REQUEST_CONTEXT_OVERSIZE = "request_context_oversize"
    TARGET_CONTEXT_INVALID = "target_context_invalid"
    RESPONSE_OVERSIZE = "response_oversize"
    RESPONSE_JSON_INVALID = "response_json_invalid"
    RESPONSE_NOT_OBJECT = "response_not_object"
    RESPONSE_ITEMS_INVALID = "response_items_invalid"
    LLM_TIMEOUT = "llm_timeout"
    LLM_CALL_ERROR = "llm_call_error"


class AiAttributionDecision(StrEnum):
    """AI 字幕归属建议的固定决策。"""

    TARGET = "target"
    INSUFFICIENT = "insufficient"
    NOT_TARGET = "not_target"


class AiAttributionConfidence(StrEnum):
    """AI 字幕归属建议的固定置信等级。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AiAttributionOutcome(StrEnum):
    """一次 AI 字幕归属尝试的审计结果。"""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class UnmatchedReason(StrEnum):
    """具体字幕无法完整归属的稳定原因。"""

    MEDIA_UNRECOGNIZED = "media_unrecognized"
    SEASON_AMBIGUOUS = "season_ambiguous"
    EPISODE_AMBIGUOUS = "episode_ambiguous"
    CANDIDATE_FILE_SCOPE_CONFLICT = "candidate_file_scope_conflict"
    UNSUPPORTED_FORMAT = "unsupported_format"


_AI_ATTRIBUTION_EVIDENCE_CODES = frozenset(AiAttributionEvidenceCode)


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
    def canonical_identity(self) -> tuple[MediaIdentityKind, str] | None:
        """返回候选自身可比较的规范媒体身份。"""

        if self.tmdb_id is not None:
            return MediaIdentityKind.TMDB, str(self.tmdb_id)
        if self.imdb_id:
            return MediaIdentityKind.IMDB, self.imdb_id.strip().lower()
        return None


class AiAttributionAudit(StrictModel):
    """一次字幕 AI 接管尝试的脱敏、强类型审计。"""

    attempted_at: datetime = Field(default_factory=utc_now)
    strategy_version: str = Field(default="1", max_length=16, pattern=r"^[A-Za-z0-9._-]+$")
    provider: str | None = Field(default=None, max_length=128)
    model: str | None = Field(default=None, max_length=128)
    before_strategy: PackageAttributionStrategy
    original_unmatched_reason: UnmatchedReason | None = None
    trigger_reason: AiAttributionTriggerReason
    outcome: AiAttributionOutcome
    reason_code: AiAttributionReasonCode
    media_type: MediaType = MediaType.UNKNOWN
    tmdb_id: int | None = Field(default=None, ge=0, le=2_147_483_647, strict=True)
    imdb_id: str | None = Field(default=None, max_length=64, pattern=r"^(?:tt)?[0-9]{1,20}$")
    season: int | None = Field(default=None, ge=0, le=9999, strict=True)
    episode: int | None = Field(default=None, ge=0, le=9999, strict=True)
    confidence: AiAttributionConfidence | None = None
    evidence_codes: list[AiAttributionEvidenceCode] = Field(default_factory=list, max_length=12)

    @field_validator("evidence_codes")
    @classmethod
    def _validate_evidence_codes(cls, values: list[AiAttributionEvidenceCode]) -> list[AiAttributionEvidenceCode]:
        """限制审计证据码为固定白名单且不得重复。"""

        if any(code not in _AI_ATTRIBUTION_EVIDENCE_CODES for code in values):
            raise ValueError("AI 审计证据码不在允许集合中")
        if len(set(values)) != len(values):
            raise ValueError("AI 审计证据码不能重复")
        return values


class FileAttributionEvidence(StrictModel):
    """一个下载后字幕文件的安全归属证据。"""

    logical_source_path: Path
    method: FileAttributionMethod
    belongs_to_target_media: bool | None = None
    media_type: MediaType = MediaType.UNKNOWN
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    season: int | None = None
    episode: int | None = None
    season_values: list[int] = Field(default_factory=list)
    episode_values: list[int] = Field(default_factory=list)
    season_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    episode_evidence: AttributionEvidence = AttributionEvidence.UNKNOWN
    unmatched_reason: UnmatchedReason | None = None
    host_recognition_summary: dict[str, JsonValue] = Field(default_factory=dict)
    ai_takeover_audit: AiAttributionAudit | None = None
    ai_before_method: FileAttributionMethod | None = None
    ai_before_unmatched_reason: UnmatchedReason | None = None


@dataclass(frozen=True, slots=True)
class CandidateMatchContext:
    """供宿主匹配 adapter 使用的插件自有媒体事实快照。"""

    title: str
    aliases: tuple[str, ...] = ()
    original_title: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    tmdb_id: int | None = None
    imdb_id: str | None = None
    douban_id: str | None = None
    bangumi_id: int | None = None
    anilist_id: int | None = None
    season_years: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class FileAttributionRequest:
    """任务提交给文件归属 facade 的一般化单文件输入。"""

    path: Path
    logical_source_path: Path
    target: SubtitleTarget
    candidate_snapshot: CandidateAttributionSnapshot
    strategy: PackageAttributionStrategy


@dataclass(slots=True)
class FileAttributionBatchResult:
    """文件归属 facade 返回的批量证据与稳定结果。"""

    evidence_by_key: dict[str, FileAttributionEvidence] = field(default_factory=dict)
    audits_by_key: dict[str, AiAttributionAudit] = field(default_factory=dict)
    reason_summary: dict[str, int] = field(default_factory=dict)
    request_count: int = 0
    submitted_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    error_count: int = 0
    over_limit_count: int = 0

    @property
    def attempted_count(self) -> int:
        """返回已经提交给归属 facade 的字幕项数量。"""

        return self.submitted_count
