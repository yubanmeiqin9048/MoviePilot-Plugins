"""领域枚举、语言准入、格式归一与候选排序测试。"""

from typing import Any

import pytest
from app.plugins.subtitleassistant.config import load_config, public_config
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.candidate import (
    candidate_is_allowed,
    has_simplified_chinese,
    normalize_format_priority,
)
from app.plugins.subtitleassistant.candidate import candidate_rank, sort_candidates
from app.plugins.subtitleassistant.schemas.attribution import PackageAttributionStrategy
from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.record import FileLocation, RecordStatus
from app.plugins.subtitleassistant.schemas.source import SourceHealth, SubtitleSource
from app.plugins.subtitleassistant.schemas.target import MediaType
from app.plugins.subtitleassistant.schemas.task import AttemptResult, TaskStatus


def _candidate(candidate_key: str, **overrides: Any) -> SubtitleCandidate:
    """构造可覆盖单一排序维度的字幕候选。"""

    values: dict[str, Any] = {
        "candidate_key": candidate_key,
        "source": SubtitleSource.MOVIEPILOT,
        "name": candidate_key,
        "file_name": f"{candidate_key}.srt",
        "language": "简体中文",
        "translation_type": TranslationType.UNKNOWN,
        "package_scope": PackageScope.EPISODE,
    }
    values.update(overrides)
    return SubtitleCandidate(**values)


def test_enum_values_are_stable_api_contracts() -> None:
    """所有领域枚举保持已确认的稳定英文值。"""

    expected = {
        TaskStatus: ["queued", "processing", "success", "skipped", "failed", "interrupted"],
        RecordStatus: ["matched", "staged", "unmatched"],
        SubtitleSource: ["moviepilot", "opensubtitles", "assrt"],
        PackageScope: ["season_pack", "episode", "unknown"],
        TranslationType: ["human", "unknown", "machine", "ai"],
        SourceHealth: ["pending", "healthy", "limited", "error", "disabled"],
        FileLocation: ["media_directory", "plugin_data"],
        MediaType: ["movie", "tv", "unknown"],
        AttemptResult: [
            "success",
            "download_failed",
            "extract_failed",
            "no_match",
            "write_failed",
            "interrupted",
        ],
    }

    for enum_type, values in expected.items():
        assert [item.value for item in enum_type] == values


@pytest.mark.parametrize(
    ("source", "marker", "flags", "expected"),
    [
        (SubtitleSource.MOVIEPILOT, "简体中文", None, True),
        (SubtitleSource.MOVIEPILOT, "简中", None, True),
        (SubtitleSource.MOVIEPILOT, "zh-CN", None, True),
        (SubtitleSource.MOVIEPILOT, "zh_Hans", None, True),
        (SubtitleSource.MOVIEPILOT, "CHS", None, True),
        (SubtitleSource.MOVIEPILOT, "Chinese", None, False),
        (SubtitleSource.MOVIEPILOT, "zh", None, False),
        (SubtitleSource.MOVIEPILOT, "中英", None, False),
        (SubtitleSource.MOVIEPILOT, "双语", None, False),
        (SubtitleSource.MOVIEPILOT, "繁体中文", None, False),
        (SubtitleSource.OPENSUBTITLES, "zh-cn", None, True),
        (SubtitleSource.OPENSUBTITLES, "ZH_CN", None, True),
        (SubtitleSource.OPENSUBTITLES, "zh", None, False),
        (SubtitleSource.OPENSUBTITLES, "Chinese", None, False),
        (SubtitleSource.OPENSUBTITLES, "zh-hans", None, False),
        (SubtitleSource.ASSRT, "简体中文", None, True),
        (SubtitleSource.ASSRT, "zh-Hans", None, True),
        (SubtitleSource.ASSRT, "", {"langchs": 1}, True),
        (SubtitleSource.ASSRT, "", {"zh_cn": True}, True),
        (SubtitleSource.ASSRT, "双语", {"langdou": 1}, False),
        (SubtitleSource.ASSRT, "中英", None, False),
        (SubtitleSource.ASSRT, "", {"langchs": 0}, False),
    ],
)
def test_simplified_chinese_admission_matrix(
    source: SubtitleSource,
    marker: str,
    flags: dict[str, object] | None,
    expected: bool,
) -> None:
    """三来源只接受明确包含简体中文的语言证据。"""

    assert has_simplified_chinese(source, marker, flags) is expected


def test_candidate_filter_rejects_foreign_only_and_optional_machine_translation() -> None:
    """仅外语字幕始终拒绝，机器与 AI 字幕服从配置开关。"""

    assert candidate_is_allowed(_candidate("human", translation_type=TranslationType.HUMAN), False)
    assert not candidate_is_allowed(
        _candidate("foreign", translation_type=TranslationType.HUMAN, foreign_parts_only=True),
        True,
    )
    assert not candidate_is_allowed(_candidate("machine", translation_type=TranslationType.MACHINE), False)
    assert not candidate_is_allowed(_candidate("ai", translation_type=TranslationType.AI), False)
    assert candidate_is_allowed(_candidate("machine", translation_type=TranslationType.MACHINE), True)


def test_format_priority_normalizes_allowed_formats_without_duplicates() -> None:
    """保存顺序被过滤、去重，并补齐所有宿主允许格式。"""

    assert normalize_format_priority(
        [".srt", "ASS", ".sup", "srt", "vtt"],
        [".SUP", "unknown", "ass", "ASS"],
    ) == ["SUP", "ASS", "SRT", "VTT"]
    assert normalize_format_priority([".srt", ".sup", ".ssa", ".ass"]) == [
        "ASS",
        "SSA",
        "SRT",
        "SUP",
    ]


@pytest.mark.parametrize(
    ("preferred", "other"),
    [
        (
            _candidate("human", translation_type=TranslationType.HUMAN),
            _candidate("unknown", translation_type=TranslationType.UNKNOWN),
        ),
        (
            _candidate("season", package_scope=PackageScope.SEASON_PACK),
            _candidate("episode", package_scope=PackageScope.EPISODE),
        ),
        (_candidate("exact", exact_id_match=True), _candidate("inexact", exact_id_match=False)),
        (
            _candidate("mp", source=SubtitleSource.MOVIEPILOT),
            _candidate("assrt", source=SubtitleSource.ASSRT),
        ),
        (
            _candidate("trusted", source=SubtitleSource.OPENSUBTITLES, trusted=True, score=1),
            _candidate("untrusted", source=SubtitleSource.OPENSUBTITLES, trusted=False, score=100),
        ),
        (_candidate("a"), _candidate("b")),
    ],
)
def test_candidate_rank_applies_confirmed_lexicographic_order(
    preferred: SubtitleCandidate,
    other: SubtitleCandidate,
) -> None:
    """跨搜索候选排序服从翻译、包范围、证据、来源质量和稳定键。"""

    source_priority = ["moviepilot", "opensubtitles", "assrt"]

    assert candidate_rank(preferred, source_priority) < candidate_rank(
        other,
        source_priority,
    )
    assert sort_candidates([other, preferred], source_priority) == [preferred, other]


def test_search_candidate_rank_does_not_infer_format_from_file_name() -> None:
    """搜索候选不根据来源文件名推断格式或改变排序。"""

    ass = _candidate("z-ass", file_name="example.ass")
    srt = _candidate("a-srt", file_name="example.srt")

    assert candidate_rank(ass, ["moviepilot"]) > candidate_rank(srt, ["moviepilot"])


def test_plugin_config_round_trips_path_mappings_and_attribution_strategy() -> None:
    """非敏感配置完整保存路径映射与包内归属策略。"""

    config = load_config(
        {
            "path_mappings": [{"source_prefix": "/history/media", "target_prefix": "/current/media"}],
            "package_attribution_strategy": "host_recognition",
        },
        [".srt", ".ass"],
    )

    assert config.package_attribution_strategy is PackageAttributionStrategy.HOST_RECOGNITION
    assert len(config.path_mappings) == 1
    assert config.saved_payload()["path_mappings"] == [
        {"source_prefix": "/history/media", "target_prefix": "/current/media"}
    ]
    assert config.saved_payload()["package_attribution_strategy"] == "host_recognition"


def test_legacy_ai_takeover_config_key_is_tolerated_and_dropped() -> None:
    """旧配置残留的 AI 接管键不导致加载失败，也不再出现在保存与公开投影中。"""

    config = load_config(
        {
            "ai_attribution_takeover_enabled": True,
            "package_attribution_strategy": "trust_package",
        },
        [".srt"],
    )

    assert config.package_attribution_strategy is PackageAttributionStrategy.TRUST_PACKAGE
    assert "ai_attribution_takeover_enabled" not in config.saved_payload()
    assert "ai_attribution_takeover_enabled" not in public_config(config)
