"""来源管理 facade 重建与进行中语义测试。"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    DownloadedAsset,
    OpenSubtitlesDownloadHandle,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, SubtitleTarget
from app.plugins.subtitleassistant.source import SourceAdministration

pytestmark = pytest.mark.anyio


def _context() -> SubtitleTarget:
    """构造具备英文标题与媒体 ID 的剧集上下文。"""

    return SubtitleTarget(
        title="测试主标题",
        english_title="English Title",
        year=2024,
        media_type=MediaType.TV,
        season=2,
        episode=3,
        tmdb_id=9876,
        imdb_id="tt0012345",
        target_path=Path("/media/Show.S02E03.mkv"),
        target_file_name="Show.S02E03.mkv",
    )


def _build() -> SourceAdministration:
    """构造只启用 OpenSubtitles 的来源管理 facade。"""

    return SourceAdministration.build(
        moviepilot_enabled=False,
        opensubtitles_enabled=True,
        assrt_enabled=False,
        opensubtitles_credentials={"api_key": "key", "username": "user", "password": "password"},
        assrt_credentials={},
    )


def _page(file_id: int) -> dict[str, Any]:
    """构造一页包含单个可下载候选的 OpenSubtitles 响应。"""

    return {
        "total_pages": 1,
        "data": [
            {
                "id": "resource",
                "attributes": {
                    "language": "zh-cn",
                    "files": [{"file_id": file_id, "file_name": "candidate.srt"}],
                },
            }
        ],
    }


async def test_in_flight_query_finishes_on_old_adapters_while_rebuild_swaps_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """凭据重建不取消进行中查询，后续查询改用新实例。"""

    facade = _build()
    old_adapter = facade._adapters[SubtitleSource.OPENSUBTITLES]
    seen: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def request_page(_self: Any, _params: Any, _page_number: int) -> dict[str, Any]:
        """首个查询阻塞到重建后返回，第二个查询返回不同候选。"""

        seen.append(id(_self))
        if len(seen) == 1:
            started.set()
            await release.wait()
            return _page(11)
        return _page(22)

    monkeypatch.setattr(type(old_adapter), "_request_page", request_page)

    in_flight = asyncio.create_task(facade.query(_context(), {SubtitleSource.OPENSUBTITLES: "First"}))
    await started.wait()
    facade.rebuild(
        enabled={
            SubtitleSource.MOVIEPILOT: False,
            SubtitleSource.OPENSUBTITLES: True,
            SubtitleSource.ASSRT: False,
        },
        credentials={
            SubtitleSource.OPENSUBTITLES: {"api_key": "new", "username": "new", "password": "new"},
            SubtitleSource.ASSRT: {},
        },
    )
    release.set()

    old_run = (await in_flight).sources[SubtitleSource.OPENSUBTITLES]
    new_run = (await facade.query(_context(), {SubtitleSource.OPENSUBTITLES: "Second"})).sources[
        SubtitleSource.OPENSUBTITLES
    ]

    assert [item.download_handle.file_id for item in old_run.candidates] == [11]
    assert [item.download_handle.file_id for item in new_run.candidates] == [22]
    assert seen[0] != seen[1]
    assert facade._adapters[SubtitleSource.OPENSUBTITLES] is not old_adapter


async def test_rebuild_swaps_adapters_and_cache_without_closing_old_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建整体换新 adapter 与新缓存对象，且不关闭旧实例。"""

    facade = _build()
    old_adapter = facade._adapters[SubtitleSource.OPENSUBTITLES]
    old_cache = facade._cache
    closed: list[int] = []

    async def close(self: Any) -> None:
        """记录被关闭的实例。"""

        closed.append(id(self))

    monkeypatch.setattr(type(old_adapter), "close", close)

    facade.rebuild(
        enabled={
            SubtitleSource.MOVIEPILOT: False,
            SubtitleSource.OPENSUBTITLES: False,
            SubtitleSource.ASSRT: False,
        },
        credentials={SubtitleSource.OPENSUBTITLES: {}, SubtitleSource.ASSRT: {}},
    )

    assert facade._adapters[SubtitleSource.OPENSUBTITLES] is not old_adapter
    assert facade._cache is not old_cache
    assert closed == []
    snapshot = facade.status_snapshot(SubtitleSource.OPENSUBTITLES)
    assert snapshot.enabled is False
    assert snapshot.configured is False


async def test_download_after_rebuild_uses_new_adapter_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重建后开始的下载在新实例上执行，句柄本身与凭据无关。"""

    facade = _build()
    old_adapter = facade._adapters[SubtitleSource.OPENSUBTITLES]
    used: list[int] = []

    async def download(self: Any, _handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """记录执行下载的实例并返回假资产。"""

        used.append(id(self))
        return DownloadedAsset(path=directory / "candidate.srt", file_name="candidate.srt")

    monkeypatch.setattr(type(old_adapter), "download", download)
    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="opensubtitles:1:2",
            source=SubtitleSource.OPENSUBTITLES,
            name="候选字幕",
            language="zh-cn",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
        ),
        download_handle=OpenSubtitlesDownloadHandle(file_id=2),
    )

    facade.rebuild(
        enabled={
            SubtitleSource.MOVIEPILOT: False,
            SubtitleSource.OPENSUBTITLES: True,
            SubtitleSource.ASSRT: False,
        },
        credentials={
            SubtitleSource.OPENSUBTITLES: {"api_key": "new", "username": "new", "password": "new"},
            SubtitleSource.ASSRT: {},
        },
    )
    new_adapter = facade._adapters[SubtitleSource.OPENSUBTITLES]

    asset = await facade.download(handle, tmp_path)

    assert used == [id(new_adapter)]
    assert asset.file_name == "candidate.srt"
