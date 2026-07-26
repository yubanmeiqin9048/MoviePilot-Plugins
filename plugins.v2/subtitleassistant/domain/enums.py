"""字幕助手领域稳定枚举。"""

from enum import StrEnum


class TaskStatus(StrEnum):
    """字幕任务顶层状态。"""

    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskTrigger(StrEnum):
    """字幕任务的稳定触发方式。"""

    TRANSFER_EVENT = "transfer_event"
    MANUAL_CANDIDATE = "manual_candidate"


class TaskStage(StrEnum):
    """字幕任务处理阶段。"""

    PREFLIGHT = "preflight"
    INVENTORY = "inventory"
    SEARCH = "search"
    DOWNLOAD = "download"
    EXTRACT = "extract"
    MATCH = "match"
    AI_ATTRIBUTION = "ai_attribution"
    WRITE = "write"


class RecordStatus(StrEnum):
    """字幕匹配记录状态。"""

    MATCHED = "matched"
    STAGED = "staged"
    UNMATCHED = "unmatched"


class SubtitleSource(StrEnum):
    """字幕候选来源。"""

    MOVIEPILOT = "moviepilot"
    OPENSUBTITLES = "opensubtitles"
    ASSRT = "assrt"


class PackageScope(StrEnum):
    """字幕候选覆盖范围。"""

    SEASON_PACK = "season_pack"
    EPISODE = "episode"
    UNKNOWN = "unknown"


class TranslationType(StrEnum):
    """字幕翻译类型。"""

    HUMAN = "human"
    UNKNOWN = "unknown"
    MACHINE = "machine"
    AI = "ai"


class SourceHealth(StrEnum):
    """字幕源当前健康状态。"""

    PENDING = "pending"
    HEALTHY = "healthy"
    LIMITED = "limited"
    ERROR = "error"
    DISABLED = "disabled"


class FileLocation(StrEnum):
    """字幕文件保存位置。"""

    MEDIA_DIRECTORY = "media_directory"
    PLUGIN_DATA = "plugin_data"


class MediaType(StrEnum):
    """插件使用的媒体类型。"""

    MOVIE = "movie"
    TV = "tv"
    UNKNOWN = "unknown"


class SourceRunStatus(StrEnum):
    """单个字幕源在任务中的运行结果。"""

    SUCCESS = "success"
    EMPTY = "empty"
    FILTERED = "filtered"
    ERROR = "error"
    LIMITED = "limited"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class AttemptResult(StrEnum):
    """候选下载尝试结果。"""

    SUCCESS = "success"
    DOWNLOAD_FAILED = "download_failed"
    EXTRACT_FAILED = "extract_failed"
    NO_MATCH = "no_match"
    WRITE_FAILED = "write_failed"
    INTERRUPTED = "interrupted"


class PackageAttributionStrategy(StrEnum):
    """压缩包内具体字幕的归属策略。"""

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


AI_ATTRIBUTION_EVIDENCE_CODES = frozenset(
    {
        "title",
        "alias",
        "year",
        "media_type",
        "tmdb_id",
        "imdb_id",
        "filename",
        "path",
        "season",
        "episode",
        "candidate_snapshot",
        "host_recognition",
        "package_scope",
    }
)
"""AI 字幕归属建议允许使用的固定证据码。"""


class UnmatchedReason(StrEnum):
    """具体字幕无法完整归属的稳定原因。"""

    MEDIA_UNRECOGNIZED = "media_unrecognized"
    SEASON_AMBIGUOUS = "season_ambiguous"
    EPISODE_AMBIGUOUS = "episode_ambiguous"
    CANDIDATE_FILE_SCOPE_CONFLICT = "candidate_file_scope_conflict"
    UNSUPPORTED_FORMAT = "unsupported_format"
