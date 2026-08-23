"""人工字幕搜索 HTTP 请求响应模型。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, JsonValue

from ...schemas.candidate import (
    CandidateRecognitionStatus,
    PackageScope,
    TranslationType,
)
from ...schemas.source import (
    SubtitleSource,
)
from .base import ApiModel
from .target import SearchPlanItem, TargetListItem
from .task import TaskListItem

__all__ = [
    "ManualCandidateItem",
    "ManualDownloadRequest",
    "ManualDownloadResponse",
    "ManualSearchRequest",
    "ManualSearchResponse",
    "ManualSourceResult",
    "SearchPlanItem",
]

_ManualSearchStatus = Literal["success", "limited", "error", "disabled", "unconfigured"]
type SourceDetails = dict[str, JsonValue]


def _enum_parser[EnumType: Enum](enum_type: type[EnumType]) -> Callable[[object], object]:
    """把 HTTP JSON 中的枚举值显式解析为对应枚举。"""

    def parse(value: object) -> object:
        if value is None or isinstance(value, enum_type):
            return value
        if isinstance(value, str):
            try:
                return enum_type(value)
            except ValueError:
                return value
        return value

    return parse


def _history_id_parser(value: object) -> object:
    """保留历史接口对十进制数字字符串的兼容校验。"""

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return value


def _datetime_parser(value: object) -> object:
    """把 ISO-8601 字符串显式解析为时间值。"""

    if isinstance(value, datetime) or not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


_CandidateRecognitionStatus = Annotated[
    CandidateRecognitionStatus,
    BeforeValidator(_enum_parser(CandidateRecognitionStatus)),
]
_SubtitleSource = Annotated[SubtitleSource, BeforeValidator(_enum_parser(SubtitleSource))]
_PackageScope = Annotated[PackageScope, BeforeValidator(_enum_parser(PackageScope))]
_TranslationType = Annotated[TranslationType, BeforeValidator(_enum_parser(TranslationType))]
_DateTimeOptional = Annotated[datetime | None, BeforeValidator(_datetime_parser)]
_HistoryId = Annotated[int, BeforeValidator(_history_id_parser)]


class ManualSearchRequest(ApiModel):
    """人工搜索请求。"""

    target_history_id: _HistoryId
    moviepilot_keyword: str | None = Field(default=None, max_length=512)
    opensubtitles_keyword: str | None = Field(default=None, max_length=512)
    assrt_keyword: str | None = Field(default=None, max_length=512)


class ManualCandidateItem(ApiModel):
    """不含任何下载定位的人工字幕候选。"""

    candidate_key: str
    recognition_status: _CandidateRecognitionStatus
    source: _SubtitleSource
    name: str
    file_name: str | None = None
    language: str | None = None
    format: str | None = None
    package_scope: _PackageScope
    season: int | None = None
    episode: int | None = None
    seasons: list[int] = Field(default_factory=list)
    episodes: list[int] = Field(default_factory=list)
    translation_type: _TranslationType
    hearing_impaired: bool
    rating: float | None = None
    votes: int | None = None
    downloads: int | None = None
    uploaded_at: _DateTimeOptional = None
    query: str | None = None
    source_details: SourceDetails = Field(default_factory=dict)


class ManualSourceResult(ApiModel):
    """单个来源的一次人工搜索结果。"""

    source: _SubtitleSource
    status: _ManualSearchStatus
    default_plans: list[SearchPlanItem] = Field(default_factory=list)
    executed_queries: list[str] = Field(default_factory=list)
    matched_query: str | None = None
    candidate_count: int = 0
    duration_ms: int | None = None
    error_summary: str | None = None
    details: SourceDetails = Field(default_factory=dict)
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
