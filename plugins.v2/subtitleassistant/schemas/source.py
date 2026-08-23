"""字幕源状态、候选池结果与下载交接公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, JsonValue

from .base import StrictModel

if TYPE_CHECKING:
    from .candidate import SubtitleCandidate

__all__ = [
    "CacheTrace",
    "CacheTraceState",
    "CandidateHandle",
    "CandidatePoolQueryBatchResult",
    "CandidatePoolStatus",
    "DownloadedAsset",
    "OpaqueCandidateHandle",
    "PaginationTrace",
    "SourceCandidatePoolResult",
    "SourceDetails",
    "SourceHealth",
    "SourceRun",
    "SourceRunStatus",
    "SourceStatus",
    "SubtitleSource",
]


class SubtitleSource(StrEnum):
    """首版字幕源。"""

    MOVIEPILOT = "moviepilot"
    OPENSUBTITLES = "opensubtitles"
    ASSRT = "assrt"


class SourceHealth(StrEnum):
    """字幕源当前健康状态。"""

    PENDING = "pending"
    HEALTHY = "healthy"
    LIMITED = "limited"
    ERROR = "error"
    DISABLED = "disabled"


class SourceRunStatus(StrEnum):
    """单个字幕源在一次任务中的运行结果。"""

    SUCCESS = "success"
    EMPTY = "empty"
    FILTERED = "filtered"
    ERROR = "error"
    LIMITED = "limited"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class CandidatePoolStatus(StrEnum):
    """单个字幕源候选池的运行结果。"""

    SUCCESS = "success"
    PARTIAL = "partial"
    LIMITED = "limited"
    ERROR = "error"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class CacheTraceState(StrEnum):
    """来源查询缓存读取状态。"""

    HIT = "hit"
    MISS = "miss"
    INVALID = "invalid"


type SourceDetails = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class OpaqueCandidateHandle:
    """来源自有下载句柄的安全、不透明标识。"""

    token: str

    def __post_init__(self) -> None:
        """拒绝空句柄，避免跨能力契约携带无效状态。"""

        if not self.token:
            raise ValueError("候选句柄不能为空")


@dataclass(slots=True)
class CandidateHandle:
    """来源交给任务的安全候选与内存下载句柄。"""

    candidate: SubtitleCandidate
    opaque: OpaqueCandidateHandle


@dataclass(slots=True)
class DownloadedAsset:
    """字幕源下载到临时目录后的文件。"""

    path: Path
    file_name: str


@dataclass(slots=True)
class CacheTrace:
    """一次来源查询的缓存执行轨迹。"""

    query: str
    state: CacheTraceState
    hit: bool = False
    stored: bool = False
    stored_at: str | None = None
    ttl_seconds: int = 0


@dataclass(slots=True)
class PaginationTrace:
    """一次来源查询的分页执行轨迹。"""

    query: str
    pages_fetched: int = 0
    complete: bool = True
    failed_page: int | None = None
    cached: bool = False


@dataclass(slots=True)
class SourceCandidatePoolResult:
    """单个字幕源的安全候选池运行结果。"""

    source: SubtitleSource
    status: CandidatePoolStatus
    candidates: list[CandidateHandle] = field(default_factory=list)
    raw_count: int = 0
    candidate_pool_count: int = 0
    download_locator_excluded_count: int = 0
    default_queries: list[str] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list)
    matched_query: str | None = None
    cache_trace: list[CacheTrace] = field(default_factory=list)
    pagination_trace: list[PaginationTrace] = field(default_factory=list)
    duration_ms: int = 0
    error_summary: str | None = None
    skip_reason: str | None = None

    @property
    def details(self) -> SourceDetails:
        """返回不包含下载定位的安全运行摘要。"""

        return {
            "default_queries": list(self.default_queries),
            "executed_queries": list(self.executed_queries),
            "matched_query": self.matched_query,
            "raw_count": self.raw_count,
            "candidate_pool_count": self.candidate_pool_count,
            "download_locator_excluded_count": self.download_locator_excluded_count,
            "cache": [
                {
                    "query": item.query,
                    "state": item.state.value,
                    "hit": item.hit,
                    "stored": item.stored,
                    "stored_at": item.stored_at,
                    "ttl_seconds": item.ttl_seconds,
                }
                for item in self.cache_trace
            ],
            "pagination": [
                {
                    "query": item.query,
                    "pages_fetched": item.pages_fetched,
                    "complete": item.complete,
                    "failed_page": item.failed_page,
                    "cached": item.cached,
                }
                for item in self.pagination_trace
            ],
            "duration_ms": self.duration_ms,
            "error_summary": self.error_summary,
            "skip_reason": self.skip_reason,
        }


@dataclass(slots=True)
class CandidatePoolQueryBatchResult:
    """批量候选池查询返回的逐来源结果。"""

    sources: dict[SubtitleSource, SourceCandidatePoolResult]


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
