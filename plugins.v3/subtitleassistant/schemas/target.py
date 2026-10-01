"""字幕目标、整理历史目标与路径映射公共契约。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .base import StrictModel

if TYPE_CHECKING:
    from .attribution import CandidateMatchContext

__all__ = [
    "MediaIdentityKind",
    "MediaType",
    "PathMapping",
    "PathMappingResolution",
    "PathMappingSnapshot",
    "SearchTarget",
    "SubtitleDestination",
    "SubtitleTarget",
]


class MediaType(StrEnum):
    """插件使用的媒体类型。"""

    MOVIE = "movie"
    TV = "tv"
    UNKNOWN = "unknown"


class MediaIdentityKind(StrEnum):
    """库存媒体身份的稳定类型。"""

    TMDB = "tmdb"
    IMDB = "imdb"


class SubtitleTarget(StrictModel):
    """一次字幕任务所需的归一化字幕目标事实。"""

    title: str
    original_title: str | None = None
    english_title: str | None = None
    year: int | None = None
    media_type: MediaType = MediaType.UNKNOWN
    season: int | None = None
    episode: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    target_path: Path
    target_file_name: str
    target_storage: str | None = None
    target_type: str = "file"
    target_extension: str | None = None
    target_container: str | None = None

    @property
    def canonical_identity(self) -> tuple[MediaIdentityKind, str] | None:
        """返回库存使用的规范媒体身份。"""

        if self.tmdb_id is not None:
            return MediaIdentityKind.TMDB, str(self.tmdb_id)
        if self.imdb_id:
            return MediaIdentityKind.IMDB, self.imdb_id.strip().lower()
        return None


class PathMappingSnapshot(StrictModel):
    """一次任务或改配实际命中的字幕保存目录映射快照。"""

    source_prefix: Path
    target_prefix: Path


@dataclass(frozen=True, slots=True)
class PathMapping:
    """一组媒体目录前缀到字幕保存目录前缀的规则值。"""

    source_prefix: Path
    target_prefix: Path

    def __post_init__(self) -> None:
        """按目录文本规范化绝对前缀，不解析符号链接，并拒绝同路径。"""

        source = Path(self.source_prefix).expanduser()
        target = Path(self.target_prefix).expanduser()
        if not source.is_absolute() or not target.is_absolute():
            raise ValueError("路径映射前缀必须是绝对路径")
        source = Path(os.path.normpath(source))
        target = Path(os.path.normpath(target))
        if source == target:
            raise ValueError("路径映射前缀不能相同")
        object.__setattr__(self, "source_prefix", source)
        object.__setattr__(self, "target_prefix", target)

    def as_dict(self) -> dict[str, str]:
        """返回可安全保存的路径映射字典。"""

        return {"source_prefix": str(self.source_prefix), "target_prefix": str(self.target_prefix)}


@dataclass(frozen=True, slots=True)
class PathMappingResolution:
    """一次目录前缀替换的纯规则结果。"""

    original_path: Path
    resolved_path: Path
    mapping: PathMapping | None = None

    @property
    def mapping_applied(self) -> bool:
        """判断本次解析是否命中了路径映射。"""

        return self.mapping is not None


@dataclass(frozen=True, slots=True)
class SubtitleDestination:
    """一次执行固定的字幕保存目录，不携带或改写媒体事实。"""

    directory: Path
    mapping: PathMapping | None = None

    @property
    def required_root(self) -> Path:
        """返回必须事先存在、不能自动创建的保存根目录。"""

        return self.mapping.target_prefix if self.mapping else self.directory


@dataclass(slots=True)
class SearchTarget:
    """整理历史查询返回给人工搜索与 HTTP adapter 的插件自有目标。"""

    history_id: int
    context: SubtitleTarget
    transferred_at: datetime
    match_context: CandidateMatchContext | None = None
