"""整理历史路径映射纯规则测试。"""

from pathlib import Path

import pytest
from app.plugins.subtitleassistant.target.mapping import (
    PathMappingValidationError,
    resolve_path,
    validate_path_mappings,
)
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.target import MediaType, PathMapping, ResolvedTarget, SubtitleTarget
from app.plugins.subtitleassistant.target import TargetCatalog


def test_validate_path_mappings_normalizes_and_keeps_order() -> None:
    """配置校验规范化目录并保留用户配置顺序。"""

    mappings = validate_path_mappings(
        [
            {
                "source_prefix": "/history/tv/../tv",
                "target_prefix": "/current/tv/",
            },
            PathMapping("/history/movie", "/current/movie").as_dict(),
        ]
    )

    assert [item.source_prefix for item in mappings] == [Path("/history/tv"), Path("/history/movie")]
    assert [item.target_prefix for item in mappings] == [Path("/current/tv"), Path("/current/movie")]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([{"source_prefix": "relative", "target_prefix": "/current"}], "绝对路径"),
        ([{"source_prefix": "/history", "target_prefix": "/history"}], "不能相同"),
        (
            [
                {"source_prefix": "/history", "target_prefix": "/current"},
                {"source_prefix": "/history/", "target_prefix": "/other"},
            ],
            "重复",
        ),
        ([{"source_prefix": "/history/*", "target_prefix": "/current"}], "通配符"),
        (
            [
                {"source_prefix": "/history", "target_prefix": "/current"},
                {"source_prefix": "/current", "target_prefix": "/library"},
            ],
            "链式",
        ),
        ([{"source_prefix": "/history"}], "缺少"),
    ],
)
def test_validate_path_mappings_rejects_invalid_config(values: list[dict[str, str]], message: str) -> None:
    """配置校验拒绝不安全或不完整的规则。"""

    with pytest.raises(PathMappingValidationError, match=message):
        validate_path_mappings(values)


def test_resolve_path_uses_longest_segment_prefix_once() -> None:
    """多条规则命中时取最长目录段且不链式替换。"""

    mappings = validate_path_mappings(
        [
            {"source_prefix": "/history", "target_prefix": "/current"},
            {"source_prefix": "/history/tv", "target_prefix": "/library/tv"},
            {"source_prefix": "/history/tv/season", "target_prefix": "/library/season"},
        ]
    )

    result = resolve_path("/history/tv/season/S01/E01.srt", mappings)
    assert result.original_path == Path("/history/tv/season/S01/E01.srt")
    assert result.resolved_path == Path("/library/season/S01/E01.srt")
    assert result.mapping == mappings[2]
    assert result.mapping_applied is True

    # `/history/tv-archive` 不是 `/history/tv` 的完整目录段。
    boundary = resolve_path("/history/tv-archive/E01.srt", mappings)
    assert boundary.resolved_path == Path("/current/tv-archive/E01.srt")


def test_resolve_path_without_match_returns_normalized_original() -> None:
    """未命中规则时直接使用历史路径。"""

    result = resolve_path(Path("/unmapped/../unmapped/E01.srt"), ())

    assert result.resolved_path == Path("/unmapped/E01.srt")
    assert result.mapping is None


def test_target_catalog_resolver_freezes_execution_target_facts() -> None:
    """目标目录能力返回路径映射与执行所需目标事实的不可变快照。"""

    mapping = PathMapping(Path("/history"), Path("/current"))
    target = SubtitleTarget(
        title="剧集",
        original_title="Original",
        english_title="Show",
        year=2026,
        media_type=MediaType.TV,
        season=1,
        episode=2,
        tmdb_id=42,
        target_path=Path("/history/Show.S01E02.mkv"),
        target_file_name="Show.S01E02.mkv",
        target_storage="local",
        target_extension="mkv",
    )
    catalog = TargetCatalog(
        config_provider=lambda: PluginConfig(path_mappings=(mapping,)),
    )

    result = catalog.resolve_actual_subtitle_path(target)

    assert isinstance(result, ResolvedTarget)
    assert result.original_path == target.target_path
    assert result.resolved_path == Path("/current/Show.S01E02.mkv")
    assert result.mapping == mapping
    assert result.title == target.title
    assert result.media_type is MediaType.TV
    assert result.season == 1
    assert result.episode == 2
    assert result.target_file_name == target.target_file_name


def test_resolve_path_accepts_literal_brackets_in_media_name() -> None:
    """媒体文件名中的常见方括号不会被误判为路径映射表达式。"""

    mappings = validate_path_mappings([{"source_prefix": "/history", "target_prefix": "/current"}])

    result = resolve_path("/history/Show [1080p]/Episode [01].mkv", mappings)

    assert result.resolved_path == Path("/current/Show [1080p]/Episode [01].mkv")
