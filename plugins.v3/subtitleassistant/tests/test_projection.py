"""宿主上下文投影统一实现测试。

覆盖事件链表载荷投影、历史行投影、MediaInfo 匹配上下文投影与手动链
宿主媒体补充，验证自动链与手动链共用同一套投影事实。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.subtitleassistant.schemas.attribution import CandidateMatchContext
from app.plugins.subtitleassistant.schemas.target import MediaType, SearchTarget, SubtitleTarget
from app.plugins.subtitleassistant.target import (
    MediaResolution,
    build_media_context,
    enrich_search_target,
    match_context_from_history,
    match_context_from_mediainfo,
    target_from_history,
)

pytestmark = pytest.mark.anyio


def _context() -> SubtitleTarget:
    """构造一个带外部 ID 的字幕目标。"""

    return SubtitleTarget(
        title="中文标题",
        original_title="Original",
        english_title="English",
        year=2026,
        media_type=MediaType.TV,
        season=2,
        episode=3,
        tmdb_id=1234,
        imdb_id="tt0012345",
        target_path=Path("/media/Show.S02E03.mkv"),
        target_file_name="Show.S02E03.mkv",
        target_storage="local",
    )


def _search_target(context: SubtitleTarget) -> SearchTarget:
    """把字幕目标包装成整理历史目标。"""

    return SearchTarget(history_id=7, context=context, transferred_at=datetime(2026, 7, 20, tzinfo=UTC))


def test_match_context_from_mediainfo_aggregates_aliases_and_external_ids() -> None:
    """MediaInfo 投影聚合别名、季年份与外部 ID，并跳过坏值。"""

    mediainfo = SimpleNamespace(
        title="识别标题",
        original_title="Original",
        en_title="English",
        names=["别名一", "English", "   "],
        season_years={1: 2023, 2: None},
        douban_id=123,
        bangumi_id="456",
        anilist_id="not-a-number",
    )

    result = match_context_from_mediainfo(_context(), mediainfo)

    assert result is not None
    assert result.title == "识别标题"
    assert result.aliases == ("English", "Original", "别名一")
    assert result.original_title == "Original"
    assert result.season_years == (("1", "2023"),)
    assert result.douban_id == "123"
    assert result.bangumi_id == 456
    assert result.anilist_id is None
    assert result.year == 2026
    assert result.tmdb_id == 1234
    assert result.imdb_id == "tt0012345"


def test_match_context_from_mediainfo_returns_none_without_mediainfo() -> None:
    """没有宿主 MediaInfo 时事件链不构造匹配上下文。"""

    assert match_context_from_mediainfo(_context(), None) is None


def test_match_context_from_history_uses_distinct_history_shape() -> None:
    """历史行投影没有季年份，别名只来自英文与原始标题。"""

    context = _context()
    history = SimpleNamespace(media_source="douban", media_id="789")

    result = match_context_from_history(context, history)

    assert result.title == context.title
    assert result.aliases == ("English", "Original")
    assert result.season_years == ()
    assert result.douban_id == "789"
    assert result.bangumi_id is None
    assert result.anilist_id is None


def test_match_context_from_history_skips_duplicate_alias_and_blank_douban() -> None:
    """历史投影跳过等于主标题的别名，并保持空白豆瓣 ID 的既有形状。"""

    context = _context().model_copy(update={"original_title": "中文标题"})
    history = SimpleNamespace(media_source="douban", media_id="  ")

    result = match_context_from_history(context, history)

    assert result.aliases == ("English",)
    assert result.douban_id == ""


def test_build_media_context_projects_event_payload() -> None:
    """事件载荷投影同时读取目标文件与宿主 MediaInfo。"""

    target = SimpleNamespace(
        path="/media/Show.S02E03.mkv",
        name="Show.S02E03.mkv",
        storage="local",
        type="file",
        extension="mkv",
    )
    meta = SimpleNamespace(name="元数据标题", year="2023", begin_season=2, begin_episode=3)
    mediainfo = SimpleNamespace(
        title="识别标题",
        original_title="Original",
        en_title="English",
        year=2024,
        season=2,
        tmdb_id=987,
        imdb_id="tt1234567",
        type=SimpleNamespace(name="TV", value="TV"),
    )

    context = build_media_context(target, meta, mediainfo)

    assert context is not None
    assert context.title == "识别标题"
    assert context.original_title == "Original"
    assert context.english_title == "English"
    assert context.year == 2024
    assert context.media_type is MediaType.TV
    assert (context.season, context.episode) == (2, 3)
    assert (context.tmdb_id, context.imdb_id) == (987, "tt1234567")
    assert context.target_path == Path(target.path)
    assert context.target_file_name == target.name
    assert context.target_extension == "mkv"


def test_build_media_context_returns_none_without_target_path() -> None:
    """缺少可用目标路径时事件投影返回空。"""

    assert build_media_context(SimpleNamespace(path="   "), None, None) is None


def test_target_from_history_rejects_remote_or_non_file_rows() -> None:
    """历史行投影只接受成功的本地文件整理记录。"""

    base: dict[str, object] = {
        "status": True,
        "dest": "/media/a.mkv",
        "dest_storage": "local",
        "dest_fileitem": {"type": "file", "name": "a.mkv"},
    }

    assert target_from_history(None) is None
    assert target_from_history(SimpleNamespace(id=1, **{**base, "dest_storage": "alist"})) is None
    assert target_from_history(SimpleNamespace(id=1, **{**base, "status": False})) is None
    assert target_from_history(SimpleNamespace(id=1, **{**base, "dest_fileitem": {"type": "folder"}})) is None


def test_target_from_history_projects_context_and_history_match_context() -> None:
    """历史行投影产出目标上下文与历史形状的匹配上下文。"""

    history = SimpleNamespace(
        id=9,
        status=True,
        dest="/media/Show.S01E02.mkv",
        dest_storage="local",
        dest_fileitem={"type": "file", "name": "Show.S01E02.mkv", "extension": "mkv", "container": "mkv"},
        title="剧集",
        original_title="Original",
        en_title="English",
        year="2026",
        type="电视剧",
        seasons="S01",
        episodes="E02",
        media_source="themoviedb",
        media_id="90",
        date="2026-07-20T12:00:00+00:00",
    )

    target = target_from_history(history)

    assert target is not None
    assert target.history_id == 9
    assert target.context.media_type is MediaType.TV
    assert (target.context.season, target.context.episode) == (1, 2)
    assert target.context.target_extension == "mkv"
    assert target.context.target_container == "mkv"
    assert target.transferred_at == datetime(2026, 7, 20, 12, tzinfo=UTC)
    assert target.match_context is not None
    assert target.match_context.aliases == ("English", "Original")
    assert target.context.tmdb_id == 90
    assert target.match_context.tmdb_id == 90
    assert target.match_context.douban_id is None


async def test_enrich_search_target_skips_when_english_title_present() -> None:
    """已有英文标题时手动链不调用宿主媒体补充。"""

    target = _search_target(_context())
    calls: list[SubtitleTarget] = []

    async def resolver(context: SubtitleTarget) -> MediaResolution | None:
        """记录一次补充调用。"""

        calls.append(context)
        return None

    result = await enrich_search_target(target, resolver)

    assert result is target
    assert calls == []


async def test_enrich_search_target_skips_without_media_id() -> None:
    """没有 TMDB/IMDb ID 时无法补充英文标题。"""

    context = _context().model_copy(update={"english_title": None, "tmdb_id": None, "imdb_id": None})
    calls: list[SubtitleTarget] = []

    async def resolver(value: SubtitleTarget) -> MediaResolution | None:
        """记录一次补充调用。"""

        calls.append(value)
        return None

    result = await enrich_search_target(_search_target(context), resolver)

    assert result is not None
    assert result.context.english_title is None
    assert calls == []


async def test_enrich_search_target_applies_resolution() -> None:
    """宿主媒体补充成功时更新目标上下文与匹配上下文。"""

    context = _context().model_copy(update={"english_title": None})
    enriched = context.model_copy(update={"english_title": "Enriched"})

    async def resolver(value: SubtitleTarget) -> MediaResolution | None:
        """返回预置补充结果。"""

        assert value is context
        return MediaResolution(
            context=enriched,
            match_context=CandidateMatchContext(title="富化标题", aliases=("Enriched",)),
        )

    target = _search_target(context)
    result = await enrich_search_target(target, resolver)

    assert result.context is enriched
    assert result.match_context is not None
    assert result.match_context.title == "富化标题"


async def test_enrich_search_target_degrades_on_resolver_error() -> None:
    """宿主媒体补充异常时保留原目标继续查询。"""

    context = _context().model_copy(update={"english_title": None})

    async def resolver(value: SubtitleTarget) -> MediaResolution | None:
        """模拟宿主媒体能力失败。"""

        del value
        raise RuntimeError("host media failed")

    target = _search_target(context)
    result = await enrich_search_target(target, resolver)

    assert result is target
    assert result.context.english_title is None
    assert result.match_context is None


async def test_event_and_manual_chains_share_one_mediainfo_projection() -> None:
    """事件链与手动链的 MediaInfo 匹配上下文来自同一实现。"""

    context = _context().model_copy(update={"english_title": None})
    enriched = context.model_copy(update={"english_title": "English"})
    mediainfo = SimpleNamespace(
        title="识别标题",
        original_title="Original",
        en_title="English",
        names=["别名一"],
        season_years={2: 2026},
        douban_id="42",
        bangumi_id=7,
        anilist_id=9,
    )
    event_side = match_context_from_mediainfo(context, mediainfo)

    async def resolver(value: SubtitleTarget) -> MediaResolution | None:
        """复用同一 MediaInfo 投影构造补充结果。"""

        return MediaResolution(
            context=enriched,
            match_context=match_context_from_mediainfo(enriched, mediainfo),
        )

    manual = await enrich_search_target(_search_target(context), resolver)

    assert event_side is not None
    assert manual.match_context == event_side
