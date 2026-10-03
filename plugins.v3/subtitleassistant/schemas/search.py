"""搜索展示与会话选择的公共契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .candidate import CandidateRecognition
from .source import CandidateHandle, SourceSearchResult
from .target import SearchTarget

__all__ = [
    "ManualSearchResult",
    "ManualSourceView",
    "SearchSelection",
    "SearchSelectionError",
]


@dataclass(slots=True)
class ManualSourceView:
    """人工搜索的一个来源结果：直用来源最小结果并附加该来源候选的识别标注。"""

    run: SourceSearchResult
    candidates: list[CandidateRecognition] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        """返回该来源展示给用户的候选数。"""

        return len(self.candidates)


@dataclass(slots=True)
class ManualSearchResult:
    """一次人工字幕搜索的安全汇总。"""

    session_id: str | None
    target: SearchTarget
    sources: list[ManualSourceView]


class SearchSelectionError(StrEnum):
    """搜索会话无法解析用户选择的原因。"""

    SESSION_NOT_FOUND = "session_not_found"
    CANDIDATE_NOT_FOUND = "candidate_not_found"


@dataclass(frozen=True, slots=True)
class SearchSelection:
    """通过会话校验的目标与完整下载句柄。"""

    target: SearchTarget
    candidate: CandidateHandle
