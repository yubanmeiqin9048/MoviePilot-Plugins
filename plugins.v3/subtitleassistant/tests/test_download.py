"""独立下载能力不依赖任务触发方式的结果契约。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.plugins.subtitleassistant.download import DownloadService
from app.plugins.subtitleassistant.schemas.attribution import PackageAttributionStrategy
from app.plugins.subtitleassistant.schemas.candidate import SubtitleCandidate
from app.plugins.subtitleassistant.schemas.download import DownloadRequest, DownloadRetention, DownloadStatus
from app.plugins.subtitleassistant.schemas.source import CandidateHandle, MoviePilotDownloadHandle, SubtitleSource
from app.plugins.subtitleassistant.schemas.target import SubtitleTarget

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("retention", list(DownloadRetention))
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("测试下载失败"), DownloadStatus.DOWNLOAD_FAILED),
        (asyncio.CancelledError(), DownloadStatus.INTERRUPTED),
    ],
)
async def test_download_returns_neutral_failure_or_cancellation(
    tmp_path: Path, retention: DownloadRetention, error: BaseException, expected: DownloadStatus
) -> None:
    """下载失败与取消返回通用结论，保留策略不隐式生成手动任务失败码。"""

    service = object.__new__(DownloadService)
    service.filesystem = SimpleNamespace(make_task_directory=AsyncMock(return_value=tmp_path))
    service.sources = SimpleNamespace(download=AsyncMock(side_effect=error))
    service._task_label = lambda task_id: task_id
    service._candidate_label = lambda candidate: candidate.name
    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="candidate", source=SubtitleSource.MOVIEPILOT, name="字幕", language="zh-CN"
        ),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/subtitle"),
    )
    request = DownloadRequest(
        task_id="download-test",
        handle=handle,
        target=SubtitleTarget(title="影片", target_path=tmp_path / "movie.mkv", target_file_name="movie.mkv"),
        trust_direct_file=False,
        retention=retention,
        package_attribution_strategy=PackageAttributionStrategy.HOST_RECOGNITION,
        attempt_number=1,
    )

    result = await service.download(request)

    assert result.result is expected
    assert result.reason_code is None
    assert result.records == ()
    assert result.committed_files == ()
    service.sources.download.assert_awaited_once_with(handle, tmp_path / "candidate-1")
