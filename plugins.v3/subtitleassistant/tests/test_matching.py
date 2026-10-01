"""MoviePilot 公共媒体匹配适配器测试。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.plugins.subtitleassistant.attribution import matching as matching_module
from app.plugins.subtitleassistant.attribution import AttributionService
from app.plugins.subtitleassistant.attribution.matching import MoviePilotMatcher
from app.plugins.subtitleassistant.schemas.attribution import (
    AttributionEvidence,
    CandidateAttributionSnapshot,
    CandidateMatchContext,
    FileAttributionMethod,
    FileAttributionEvidence,
    FileAttributionRequest,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from app.plugins.subtitleassistant.schemas.candidate import CandidateRecognitionStatus, PackageScope, SubtitleCandidate
from app.plugins.subtitleassistant.schemas.source import SubtitleSource
from app.plugins.subtitleassistant.schemas.target import MediaType, SubtitleTarget

pytestmark = pytest.mark.anyio


def _context(**overrides: Any) -> SubtitleTarget:
    """构造当前目标为第二季第三集的媒体上下文。"""

    values: dict[str, Any] = {
        "title": "示例剧集",
        "year": 2024,
        "media_type": MediaType.TV,
        "season": 2,
        "episode": 3,
        "tmdb_id": 100,
        "imdb_id": "tt0012345",
        "target_path": Path("/media/Show.S02E03.mkv"),
        "target_file_name": "Show.S02E03.mkv",
    }
    values.update(overrides)
    return SubtitleTarget(**values)


def _candidate(**overrides: Any) -> SubtitleCandidate:
    """构造没有结构化媒体 ID 的简中候选。"""

    values: dict[str, Any] = {
        "candidate_key": "candidate",
        "source": SubtitleSource.ASSRT,
        "name": "Show subtitle",
        "file_name": "Show.S02.ass",
        "language": "简体中文",
        "metadata": {"description": "Show 第二季字幕"},
    }
    values.update(overrides)
    return SubtitleCandidate(**values)


def _meta(
    seasons: list[int] | None = None,
    episodes: list[int] | None = None,
    media_type: str = "未知",
) -> SimpleNamespace:
    """构造宿主元数据解析结果。"""

    return SimpleNamespace(
        season_list=seasons or [],
        episode_list=episodes or [],
        type=SimpleNamespace(value=media_type),
    )


def _snapshot(**overrides: Any) -> CandidateAttributionSnapshot:
    """构造候选归属快照。"""

    values: dict[str, Any] = {
        "media_type": MediaType.TV,
        "tmdb_id": 100,
        "imdb_id": "tt0012345",
        "seasons": [2],
        "episodes": [3],
        "package_scope": PackageScope.EPISODE,
        "evidence": ["test"],
    }
    values.update(overrides)
    return CandidateAttributionSnapshot(**values)


def _fail_host_call(*_args: Any, **_kwargs: Any) -> None:
    """在不允许访问的宿主分支被调用时令测试失败。"""

    raise AssertionError("不应调用该宿主能力")


def test_structured_id_prefers_tmdb_over_conflicting_imdb() -> None:
    """双方 TMDB 一致时，冲突的 IMDb 不得推翻主身份。"""

    candidate = _candidate(tmdb_id=100, imdb_id="tt0099999")
    context = _context(tmdb_id=100, imdb_id="tt0012345")

    assert MoviePilotMatcher._structured_id_result(candidate, context) is True


def test_manual_recognition_does_not_relax_automatic_candidate_admission() -> None:
    """人工流程保留未识别候选时，自动流程仍拒绝同一候选。"""

    candidate = _candidate(tmdb_id=999, name="Other.Show.S02E03")
    matcher = MoviePilotMatcher()

    recognition = matcher.recognize_candidate(candidate, _context(), None)

    assert recognition.status is CandidateRecognitionStatus.UNRECOGNIZED
    assert recognition.candidate.candidate_key == candidate.candidate_key
    assert matcher.normalize_candidate(candidate, _context(), None) is None


def test_default_file_attributor_rejects_candidate_for_another_media() -> None:
    """生产默认归属 facade 不得绕过自动媒体匹配。"""

    candidate = _candidate(tmdb_id=999, name="Other.Show.S02E03")

    assert AttributionService().normalize_candidate(candidate, _context(), None) is None


def test_normalize_candidate_preserves_all_parsed_ranges_without_target_writeback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动准入聚合候选三个文本的全部季集，不把当前目标写回单值字段。"""

    parsed = {
        "Show subtitle": _meta([1], [1], "电视剧"),
        "Show.S02.ass": _meta([2], [3], "电视剧"),
        "Show 第二季字幕": _meta([3], [4], "电视剧"),
    }
    monkeypatch.setattr(
        matching_module,
        "MetaInfo",
        lambda title, subtitle="": parsed[title],
    )
    monkeypatch.setattr(
        matching_module,
        "TorrentHelper",
        SimpleNamespace(
            match_season_episodes=_fail_host_call,
            match_torrent=_fail_host_call,
        ),
    )
    candidate = _candidate(tmdb_id=100)

    normalized = MoviePilotMatcher().normalize_candidate(candidate, _context(), None)

    assert normalized is not candidate
    assert normalized.seasons == [1, 2, 3]
    assert normalized.episodes == [1, 3, 4]
    assert normalized.season is None
    assert normalized.episode is None
    assert normalized.package_scope is PackageScope.EPISODE
    assert normalized.exact_id_match is True
    assert candidate.seasons == []
    assert candidate.episodes == []


def test_normalize_candidate_exact_id_uses_host_season_gate_without_copying_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """精确 ID 但候选无范围时仍执行季集门禁，且不把目标季集变成候选证据。"""

    events: list[dict[int, list[int]]] = []
    monkeypatch.setattr(matching_module, "MetaInfo", lambda **_kwargs: _meta())
    monkeypatch.setattr(
        matching_module,
        "TorrentInfo",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    class _HostHelper:
        """只允许精确 ID 分支的季集门禁。"""

        @staticmethod
        def match_season_episodes(**kwargs: Any) -> bool:
            """记录门禁参数并接受第一项候选文本。"""

            events.append(kwargs["season_episodes"])
            return True

        match_torrent = staticmethod(_fail_host_call)

    monkeypatch.setattr(matching_module, "TorrentHelper", _HostHelper)
    candidate = _candidate(tmdb_id=100)

    normalized = MoviePilotMatcher().normalize_candidate(candidate, _context(), None)

    assert normalized is not candidate
    assert normalized.seasons == []
    assert normalized.episodes == []
    assert normalized.season is None
    assert normalized.episode is None
    assert normalized.package_scope is PackageScope.UNKNOWN
    assert events == [{2: [3]}]
    assert candidate.seasons == []
    assert candidate.episodes == []


def test_normalize_candidate_without_id_uses_public_season_then_media_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选无可比 ID 时按候选文本顺序执行宿主季集与媒体准入。"""

    events: list[tuple[str, str]] = []
    monkeypatch.setattr(matching_module, "MetaInfo", lambda **_kwargs: _meta())
    monkeypatch.setattr(
        matching_module,
        "TorrentInfo",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    class _HostHelper:
        """模拟第三个候选文本才通过完整自动准入。"""

        @staticmethod
        def match_season_episodes(torrent: Any, **_kwargs: Any) -> bool:
            """只让描述文本覆盖当前季集。"""

            events.append(("season", torrent.title))
            return torrent.title == "Show 第二季字幕"

        @staticmethod
        def match_torrent(torrent: Any, **_kwargs: Any) -> bool:
            """记录季集门禁之后的媒体匹配。"""

            events.append(("media", torrent.title))
            return True

    monkeypatch.setattr(matching_module, "TorrentHelper", _HostHelper)

    normalized = MoviePilotMatcher().normalize_candidate(
        _candidate(),
        _context(),
        CandidateMatchContext(title="示例剧集", media_type=MediaType.TV, year=2024, tmdb_id=100),
    )

    assert normalized is not None
    assert normalized.seasons == []
    assert normalized.episodes == []
    assert events == [
        ("season", "Show subtitle"),
        ("season", "Show.S02.ass"),
        ("season", "Show 第二季字幕"),
        ("media", "Show 第二季字幕"),
    ]


def test_candidate_snapshot_uses_only_candidate_fields_and_parsed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选快照保留自身结构化和文本证据，但从不解析搜索关键词。"""

    seen: list[str] = []

    def fake_meta_info(title: str, **_kwargs: Any) -> SimpleNamespace:
        """按候选自身文本返回不同季集。"""

        seen.append(title)
        values = {
            "Title.S01E01": _meta([1], [1], "电视剧"),
            "Description.S03E04": _meta([3], [4], "电视剧"),
        }
        return values[title]

    monkeypatch.setattr(matching_module, "MetaInfo", fake_meta_info)
    candidate = _candidate(
        name="Title.S01E01",
        file_name=None,
        season=2,
        seasons=[5],
        episode=2,
        episodes=[8],
        tmdb_id=100,
        imdb_id="tt0012345",
        metadata={
            "description": "Description.S03E04",
            "actual_query": "Forbidden.S09E09",
        },
    )

    snapshot = MoviePilotMatcher().candidate_snapshot(candidate)

    assert seen == ["Title.S01E01", "Description.S03E04"]
    assert snapshot.media_type is MediaType.TV
    assert snapshot.tmdb_id == 100
    assert snapshot.imdb_id == "tt0012345"
    assert snapshot.seasons == [1, 2, 3, 5]
    assert snapshot.episodes == [1, 2, 4, 8]
    assert snapshot.package_scope is PackageScope.EPISODE
    assert "structured_season" in snapshot.evidence
    assert "title_season" in snapshot.evidence
    assert "description_episode" in snapshot.evidence


async def test_trust_package_prefers_explicit_logical_path_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """信任模式从完整逻辑来源路径读取明确季集并继承目标媒体身份。"""

    logical_path = Path("outer.zip/inner.zip/Show/Show.S02E04.ass")

    def fake_meta_path(path: Path) -> SimpleNamespace:
        """确认解析的是逻辑路径而不是任务临时物理路径。"""

        assert path == logical_path
        return _meta([2], [4], "电视剧")

    monkeypatch.setattr(matching_module, "MetaInfoPath", fake_meta_path)

    evidence = await MoviePilotMatcher().attribute_file(
        logical_path,
        _context(),
        _snapshot(episodes=[3, 4]),
        PackageAttributionStrategy.TRUST_PACKAGE,
    )

    assert evidence.method is FileAttributionMethod.TRUST_PACKAGE
    assert evidence.belongs_to_target_media is True
    assert evidence.media_type is MediaType.TV
    assert evidence.tmdb_id == 100
    assert evidence.season == 2
    assert evidence.episode == 4
    assert evidence.season_evidence is AttributionEvidence.PATH
    assert evidence.episode_evidence is AttributionEvidence.PATH
    assert evidence.unmatched_reason is None


async def test_trust_package_fills_only_unique_candidate_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """逻辑路径缺失季集时只允许候选快照的唯一值补齐。"""

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta())

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/subtitle.ass"),
        _context(),
        _snapshot(seasons=[2], episodes=[3]),
        PackageAttributionStrategy.TRUST_PACKAGE,
    )

    assert evidence.season == 2
    assert evidence.episode == 3
    assert evidence.season_evidence is AttributionEvidence.CANDIDATE_SNAPSHOT
    assert evidence.episode_evidence is AttributionEvidence.CANDIDATE_SNAPSHOT
    assert evidence.unmatched_reason is None


async def test_trust_package_marks_ambiguous_and_conflicting_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """多季缺季不猜测，文件范围超出候选范围时给出稳定冲突原因。"""

    matcher = MoviePilotMatcher()
    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta())
    ambiguous = await matcher.attribute_file(
        Path("pack.zip/subtitle.ass"),
        _context(),
        _snapshot(seasons=[1, 2], episodes=[3]),
        PackageAttributionStrategy.TRUST_PACKAGE,
    )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta([3], [3]))
    conflict = await matcher.attribute_file(
        Path("pack.zip/Show.S03E03.ass"),
        _context(),
        _snapshot(seasons=[2], episodes=[3]),
        PackageAttributionStrategy.TRUST_PACKAGE,
    )

    assert ambiguous.season is None
    assert ambiguous.episode == 3
    assert ambiguous.unmatched_reason is UnmatchedReason.SEASON_AMBIGUOUS
    assert conflict.season == 3
    assert conflict.episode == 3
    assert conflict.unmatched_reason is UnmatchedReason.CANDIDATE_FILE_SCOPE_CONFLICT


async def test_trust_package_movie_does_not_parse_season_or_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """电影信任模式直接继承目标媒体，季集证据明确标记为不适用。"""

    monkeypatch.setattr(matching_module, "MetaInfoPath", _fail_host_call)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("movie.zip/movie.ass"),
        _context(
            media_type=MediaType.MOVIE,
            season=None,
            episode=None,
            target_path=Path("/media/Movie.mkv"),
            target_file_name="Movie.mkv",
        ),
        _snapshot(media_type=MediaType.MOVIE, seasons=[], episodes=[]),
        PackageAttributionStrategy.TRUST_PACKAGE,
    )

    assert evidence.belongs_to_target_media is True
    assert evidence.season is None
    assert evidence.episode is None
    assert evidence.season_evidence is AttributionEvidence.NOT_APPLICABLE
    assert evidence.episode_evidence is AttributionEvidence.NOT_APPLICABLE


async def test_host_recognition_calls_public_async_api_and_matches_tmdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主模式每个文件仅调用公开异步识别，并优先按 TMDB 精确命中。"""

    meta = _meta([2], [4], "电视剧")
    calls: list[Any] = []

    class _MediaChain:
        """记录公开识别调用的宿主替身。"""

        async def async_recognize_by_meta(self, value: Any) -> SimpleNamespace:
            """返回 TMDB 命中的电视剧。"""

            calls.append(value)
            return SimpleNamespace(
                type=SimpleNamespace(value="电视剧"),
                tmdb_id=100,
                imdb_id="tt9999999",
            )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: meta)
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Show.S02E04.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert calls == [meta]
    assert evidence.method is FileAttributionMethod.HOST_RECOGNITION
    assert evidence.belongs_to_target_media is True
    assert evidence.season == 2
    assert evidence.episode == 4
    assert evidence.unmatched_reason is None
    assert evidence.host_recognition_summary["identity_source"] == "tmdb"
    assert evidence.host_recognition_summary["identity_match"] is True


async def test_host_recognition_tmdb_conflict_does_not_fall_back_to_imdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双方都有 TMDB 时以其结果为准，即使 IMDb 相同也判定其他媒体。"""

    class _MediaChain:
        """返回 TMDB 冲突但 IMDb 相同的宿主替身。"""

        async def async_recognize_by_meta(self, _meta_value: Any) -> SimpleNamespace:
            """返回另一个媒体。"""

            return SimpleNamespace(
                type=SimpleNamespace(value="电视剧"),
                tmdb_id=999,
                imdb_id="tt0012345",
            )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta([2], [3]))
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Show.S02E03.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert evidence.belongs_to_target_media is False
    assert evidence.unmatched_reason is None
    assert evidence.host_recognition_summary["identity_source"] == "tmdb"
    assert evidence.host_recognition_summary["identity_match"] is False


async def test_host_recognition_falls_back_to_normalized_imdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TMDB 不可比较时按归一化 IMDb 后备精确命中。"""

    class _MediaChain:
        """返回仅 IMDb 可比较的宿主替身。"""

        async def async_recognize_by_meta(self, _meta_value: Any) -> SimpleNamespace:
            """返回省略前缀和前导零的 IMDb ID。"""

            return SimpleNamespace(
                type=SimpleNamespace(value="电视剧"),
                tmdb_id=None,
                imdb_id="12345",
            )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta([2], [3]))
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Show.S02E03.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert evidence.belongs_to_target_media is True
    assert evidence.host_recognition_summary["identity_source"] == "imdb"


@pytest.mark.parametrize("recognized", [None, SimpleNamespace(type=SimpleNamespace(value="电视剧"))])
async def test_host_recognition_without_comparable_identity_is_unmatched(
    monkeypatch: pytest.MonkeyPatch,
    recognized: SimpleNamespace | None,
) -> None:
    """宿主未识别或没有可比较 ID 时保留部分季集证据并标记未识别。"""

    class _MediaChain:
        """返回参数指定识别结果的宿主替身。"""

        async def async_recognize_by_meta(self, _meta_value: Any) -> SimpleNamespace | None:
            """返回无身份结果或完全未识别。"""

            return recognized

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta([2], []))
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Show.S02.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert evidence.belongs_to_target_media is None
    assert evidence.season == 2
    assert evidence.episode is None
    assert evidence.unmatched_reason is UnmatchedReason.MEDIA_UNRECOGNIZED


async def test_host_recognition_same_media_with_missing_episode_is_partial_unmatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主媒体精确命中但集号不明确时保留媒体季号并标记集号不明确。"""

    class _MediaChain:
        """返回目标媒体的宿主替身。"""

        async def async_recognize_by_meta(self, _meta_value: Any) -> SimpleNamespace:
            """返回目标 TMDB 媒体。"""

            return SimpleNamespace(
                type=SimpleNamespace(value="电视剧"),
                tmdb_id=100,
                imdb_id=None,
            )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta([2], []))
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Show.S02.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert evidence.belongs_to_target_media is True
    assert evidence.season == 2
    assert evidence.episode is None
    assert evidence.unmatched_reason is UnmatchedReason.EPISODE_AMBIGUOUS


async def test_host_recognition_type_mismatch_is_other_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主识别媒体类型不同即作为包内其他媒体排除。"""

    class _MediaChain:
        """返回电影识别结果的宿主替身。"""

        async def async_recognize_by_meta(self, _meta_value: Any) -> SimpleNamespace:
            """返回与电视剧目标不同的电影。"""

            return SimpleNamespace(
                type=SimpleNamespace(value="电影"),
                tmdb_id=100,
                imdb_id="tt0012345",
            )

    monkeypatch.setattr(matching_module, "MetaInfoPath", lambda _path: _meta())
    monkeypatch.setattr(matching_module, "MediaChain", _MediaChain)

    evidence = await MoviePilotMatcher().attribute_file(
        Path("pack.zip/Movie.ass"),
        _context(),
        _snapshot(),
        PackageAttributionStrategy.HOST_RECOGNITION,
    )

    assert evidence.belongs_to_target_media is False
    assert evidence.media_type is MediaType.MOVIE
    assert evidence.unmatched_reason is None
    assert evidence.host_recognition_summary["type_match"] is False


async def test_batch_attribution_uses_each_request_and_isolates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """批量归属使用各自请求的目标与策略，单文件失败不影响后续文件。"""

    service = AttributionService()
    requests = [
        FileAttributionRequest(
            logical_source_path=Path(f"file_{index}.ass"),
            target=_context(tmdb_id=100 + index),
            candidate_snapshot=_snapshot(episodes=[index]),
            strategy=strategy,
        )
        for index, strategy in enumerate(
            [
                PackageAttributionStrategy.TRUST_PACKAGE,
                PackageAttributionStrategy.HOST_RECOGNITION,
                PackageAttributionStrategy.TRUST_PACKAGE,
            ],
            start=1,
        )
    ]
    calls = []

    async def attribute_file(
        logical_source_path: Path,
        context: SubtitleTarget,
        snapshot: CandidateAttributionSnapshot,
        strategy: PackageAttributionStrategy,
    ) -> FileAttributionEvidence:
        """记录规则输入并模拟中间文件识别失败。"""

        calls.append((logical_source_path, context, snapshot, strategy))
        if logical_source_path == Path("file_2.ass"):
            raise RuntimeError("测试单文件失败")
        return FileAttributionEvidence(
            logical_source_path=logical_source_path,
            method=FileAttributionMethod.TRUST_PACKAGE,
            tmdb_id=context.tmdb_id,
            episode=snapshot.episodes[0],
        )

    monkeypatch.setattr(service, "attribute_file", attribute_file)
    result = await service.attribute_requests(requests)

    assert calls == [
        (request.logical_source_path, request.target, request.candidate_snapshot, request.strategy)
        for request in requests
    ]
    assert result.request_count == result.submitted_count == 3
    assert result.error_count == 1
    assert result.reason_summary == {"adapter_error": 1}
    assert set(result.evidence_by_key) == {"file_0001", "file_0003"}
    assert result.evidence_by_key["file_0001"].tmdb_id == 101
    assert result.evidence_by_key["file_0003"].tmdb_id == 103
    assert result.evidence_by_key["file_0003"].episode == 3
