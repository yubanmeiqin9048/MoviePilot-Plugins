"""人工字幕搜索对外结果的公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .candidate import CandidateRecognition
from .source import SourceDetails, SubtitleSource
from .target import SearchTarget
from .task import SubtitleTask

__all__ = [
    "ManualSearchResult",
    "ManualSearchStatus",
    "ManualSourceView",
    "ManualSubmitResult",
    "ManualSubmitStatus",
]


class ManualSearchStatus(StrEnum):
    """人工字幕搜索的来源状态。"""

    SUCCESS = "success"
    LIMITED = "limited"
    ERROR = "error"
    DISABLED = "disabled"
    UNCONFIGURED = "unconfigured"


class ManualSubmitStatus(StrEnum):
    """人工候选提交的稳定结果状态。"""

    SUCCESS = "success"
    SESSION_NOT_FOUND = "session_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"
    REJECTED = "rejected"


@dataclass(slots=True)
class ManualSourceView:
    """不包含下载句柄的人工来源搜索响应。"""

    source: SubtitleSource
    status: ManualSearchStatus
    candidates: list[CandidateRecognition] = field(default_factory=list)
    default_queries: list[str] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list)
    matched_query: str | None = None
    duration_ms: int | None = None
    error_summary: str | None = None
    details: SourceDetails = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把边界处的来源状态收敛为搜索能力枚举。"""

        if not isinstance(self.status, ManualSearchStatus):
            self.status = ManualSearchStatus(self.status)


@dataclass(slots=True)
class ManualSearchResult:
    """一次人工字幕搜索的安全汇总。"""

    session_id: str | None
    target: SearchTarget
    sources: list[ManualSourceView]


@dataclass(slots=True)
class ManualSubmitResult:
    """人工候选提交的稳定领域结果。"""

    status: ManualSubmitStatus
    task: SubtitleTask | None = None
    reused: bool = False

    def __post_init__(self) -> None:
        """把边界处的提交状态收敛为搜索能力枚举。"""

        if not isinstance(self.status, ManualSubmitStatus):
            self.status = ManualSubmitStatus(self.status)
