"""共享搜索和搜索会话的独立能力契约。"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.plugins.subtitleassistant.schemas.candidate import SubtitleCandidate
from app.plugins.subtitleassistant.schemas.search import SearchSelection, SearchSelectionError
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    MoviePilotDownloadHandle,
    SourceSearchBatch,
    SourceSearchResult,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import SearchTarget, SubtitleTarget
from app.plugins.subtitleassistant.search import SearchSessions, SubtitleSearch

pytestmark = pytest.mark.anyio


def _target() -> SearchTarget:
    """构造尚未补充标题或识别信息的目标。"""

    return SearchTarget(
        history_id=7,
        transferred_at=datetime(2026, 10, 3, tzinfo=UTC),
        context=SubtitleTarget(title="目标影片", target_path=Path("/media/movie.mkv"), target_file_name="movie.mkv"),
    )


def _candidate(key: str, language: str = "en") -> CandidateHandle:
    """构造保留来源顺序和原始语言的候选。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(candidate_key=key, source=SubtitleSource.MOVIEPILOT, name=key, language=language),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/subtitle"),
    )


@pytest.mark.parametrize(
    ("queries", "expected_normalized"),
    [
        (None, {SubtitleSource.MOVIEPILOT: None, SubtitleSource.OPENSUBTITLES: None, SubtitleSource.ASSRT: None}),
        (
            {SubtitleSource.MOVIEPILOT: ""},
            {SubtitleSource.MOVIEPILOT: "", SubtitleSource.OPENSUBTITLES: None, SubtitleSource.ASSRT: None},
        ),
    ],
)
async def test_shared_search_preserves_candidates_source_results_and_query_inputs(
    queries: dict[SubtitleSource, str | None] | None,
    expected_normalized: dict[SubtitleSource, str | None],
) -> None:
    """共享搜索标准化查询词、不准入、不识别、不重排，并保留来源失败信息。"""

    target = _target().context
    candidates = [_candidate("unrecognized-english"), _candidate("chinese", "zh-CN")]
    batch = SourceSearchBatch(
        sources={
            SubtitleSource.MOVIEPILOT: SourceSearchResult(
                source=SubtitleSource.MOVIEPILOT, status="success", candidates=candidates, cache_hit=True
            ),
            SubtitleSource.ASSRT: SourceSearchResult(
                source=SubtitleSource.ASSRT, status="limited", error_summary="测试限流", retry_after_seconds=30
            ),
        }
    )
    source = SimpleNamespace(query=AsyncMock(return_value=batch))

    result = await SubtitleSearch(source).search(target, queries)

    assert result is batch
    assert result.sources[SubtitleSource.MOVIEPILOT].candidates == candidates
    assert candidates[0].candidate.language == "en"
    assert target.english_title is None
    source.query.assert_awaited_once_with(target, expected_normalized)


async def test_search_session_resolves_only_its_candidates_and_preserves_first_duplicate() -> None:
    """会话绑定原始目标、拒绝非本会话候选，同键候选沿用首次结果。"""

    target = _target()
    first = _candidate("same-key")
    duplicate = _candidate("same-key", "zh-CN")
    cache = SimpleNamespace(set=AsyncMock(), get=AsyncMock(), clear=AsyncMock())
    sessions = SearchSessions(cache)

    session_id = await sessions.create(target, [first, duplicate])

    assert session_id is not None
    cache.get.return_value = cache.set.await_args.args[1]
    assert cache.set.await_args.kwargs["ttl"] == 30 * 60
    selection = await sessions.resolve(session_id, "same-key")
    assert isinstance(selection, SearchSelection)
    assert selection.target is target
    assert selection.candidate is first
    assert await sessions.resolve(session_id, "not-in-session") is SearchSelectionError.CANDIDATE_NOT_FOUND
    cache.get.return_value = None
    assert await sessions.resolve(session_id, "same-key") is SearchSelectionError.SESSION_NOT_FOUND
    await sessions.clear_sessions()
    cache.clear.assert_awaited_once_with(region=cache.set.await_args.kwargs["region"])


async def test_empty_search_does_not_create_session() -> None:
    """没有候选时不创建空会话或写入缓存。"""

    cache = SimpleNamespace(set=AsyncMock())

    assert await SearchSessions(cache).create(_target(), []) is None
    cache.set.assert_not_awaited()
