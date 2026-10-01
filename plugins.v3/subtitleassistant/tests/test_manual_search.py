"""人工字幕搜索来源与会话应用服务测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.db.models.transferhistory import TransferHistory
from app.db.session import async_session_scope
from app.plugins.subtitleassistant.attribution import AttributionService
from app.plugins.subtitleassistant.candidate import admit_automatic_candidates
from app.plugins.subtitleassistant.schemas.candidate import (
    CandidateRecognition,
    CandidateRecognitionStatus,
    SubtitleCandidate,
)
from app.plugins.subtitleassistant.schemas.source import (
    AssrtDownloadHandle,
    CandidateHandle,
    MoviePilotDownloadHandle,
    OpenSubtitlesDownloadHandle,
    SourceSearchBatch,
    SourceSearchResult,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, SearchTarget, SubtitleTarget
from app.plugins.subtitleassistant.schemas.task import SubtitleTask
from app.plugins.subtitleassistant.search import ManualSearch
from app.plugins.subtitleassistant.search import service as searches_module
from app.plugins.subtitleassistant.search.service import ManualSearchSession
from app.plugins.subtitleassistant.target import TargetCatalog

pytestmark = pytest.mark.anyio


@pytest.fixture
def configured_history_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """把稳定历史查询 SDK 绑定到当前隔离测试数据库。"""

    from app.application import query as data_query_module
    from app.application.query import DataQueryService
    from app.db.adapters.query import SqlAlchemyDataQueryAdapter
    from app.db.session import SessionFactory

    class _Executor:
        """在线程中执行同步查询，模拟宿主数据库 worker。"""

        async def run(self, operation: Any) -> Any:
            """在线程中执行一次查询。"""

            return await asyncio.to_thread(operation)

    adapter = SqlAlchemyDataQueryAdapter(SessionFactory)
    service = DataQueryService(subscriptions=adapter, histories=adapter, async_executor=_Executor())
    monkeypatch.setattr(data_query_module, "_configured_data_query_service", service)


class _FakeResponse:
    """提供来源搜索所需最小异步响应。"""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        """保存 JSON 响应与状态码。"""

        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.is_closed = False

    def json(self) -> dict[str, Any]:
        """返回预置 JSON。"""

        return self._payload

    async def aclose(self) -> None:
        """标记响应已关闭。"""

        self.is_closed = True


class _FakeCache:
    """支持区域与 TTL 的测试内存缓存。"""

    def __init__(self) -> None:
        """创建空缓存。"""

        self.values: dict[tuple[str | None, str], Any] = {}
        self.set_calls: list[tuple[str, int | None, str | None]] = []
        self.expired = False

    async def get(self, key: str, region: str | None = None) -> Any:
        """按区域读取缓存。"""

        if self.expired:
            return None
        return self.values.get((region, key))

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """按区域保存缓存。"""

        self.set_calls.append((key, ttl, region))
        self.values[(region, key)] = value

    async def clear(self, region: str | None = None) -> None:
        """清除指定区域。"""

        self.values = {item_key: value for item_key, value in self.values.items() if item_key[0] != region}

    async def close(self) -> None:
        """关闭无资源缓存。"""

        return


class _ManualCoordinator:
    """记录人工提交工作项并返回可控的任务快照。"""

    def __init__(self, *, reject: bool = False) -> None:
        """创建人工提交测试替身。"""

        self.reject = reject
        self.items: list[Any] = []

    async def enqueue(self, item: Any) -> SubtitleTask | None:
        """记录完整运行期工作项并返回任务快照。"""

        self.items.append(item)
        if self.reject:
            return None
        return SubtitleTask(
            media_title=item.context.title,
            media_type=item.context.media_type,
            target_file_name=item.context.target_file_name,
            target_path=item.context.target_path,
            target_history_id=item.target_history_id,
        )


def _context() -> SubtitleTarget:
    """构造人工搜索上下文。"""

    return SubtitleTarget(
        title="中文标题",
        english_title="English Title",
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


async def test_target_query_passes_through_one_history_page() -> None:
    """目标目录保留稳定 SDK 当前页的顺序、重复项与异常字段。"""

    histories = [
        SimpleNamespace(id=20, status=False, dest="/remote/movie.mkv", dest_storage="alist"),
        SimpleNamespace(id=20, status=True, dest="/media/movie.mkv", dest_storage="local", title=None),
    ]

    class _HistoryQuery:
        """返回一个稳定 SDK 分页结果并记录查询参数。"""

        def __init__(self) -> None:
            """初始化查询记录。"""

            self.calls: list[tuple[int, int]] = []

        async def async_list_transfer_history(self, *, filters: Any, page: Any) -> Any:
            """只按页码与数量返回稳定 SDK 分页结果。"""

            assert filters.status is None
            assert filters.text is None
            self.calls.append((page.page, page.count))
            return SimpleNamespace(
                items=histories,
                page=page.page,
                count=page.count,
                total=len(histories),
                has_next=False,
            )

        async def async_get_transfer_history(self, history_id: int) -> Any | None:
            """测试分页场景不读取单条历史。"""

            del history_id
            return None

    history_query = _HistoryQuery()
    result = await TargetCatalog(history_query=history_query, batch_size=100).list_targets(page=2, page_size=2)

    assert result.items == histories
    assert result.page == 2
    assert result.page_size == 2
    assert result.total == len(histories)
    assert history_query.calls == [(2, 2)]


@pytest.mark.parametrize("histories", [[], [SimpleNamespace(id=1, title="only")]])
async def test_target_query_preserves_empty_and_short_host_pages(histories: list[Any]) -> None:
    """稳定 SDK 空页和短页不触发插件补查。"""

    class _HistoryQuery:
        """返回固定稳定 SDK 分页结果并记录调用。"""

        def __init__(self) -> None:
            """初始化调用记录。"""

            self.calls: list[tuple[int, int]] = []

        async def async_list_transfer_history(self, *, filters: Any, page: Any) -> Any:
            """返回当前页。"""

            assert filters.status is None
            assert filters.text is None
            self.calls.append((page.page, page.count))
            return SimpleNamespace(
                items=histories,
                page=page.page,
                count=page.count,
                total=len(histories),
                has_next=False,
            )

        async def async_get_transfer_history(self, history_id: int) -> Any | None:
            """测试分页场景不读取单条历史。"""

            del history_id
            return None

    history_query = _HistoryQuery()
    result = await TargetCatalog(history_query=history_query).list_targets(page=3, page_size=25)

    assert result.items == histories
    assert result.total == len(histories)
    assert history_query.calls == [(3, 25)]


async def test_target_query_propagates_host_page_error_without_followup() -> None:
    """稳定 SDK 分页异常直接返回，插件不再尝试读取其他页。"""

    class _HistoryQuery:
        """抛出稳定 SDK 查询异常。"""

        def __init__(self) -> None:
            """初始化查询记录。"""

            self.calls: list[tuple[int, int]] = []

        async def async_list_transfer_history(self, *, filters: Any, page: Any) -> Any:
            """模拟宿主查询失败。"""

            del filters
            self.calls.append((page.page, page.count))
            raise RuntimeError("host page failed")

        async def async_get_transfer_history(self, history_id: int) -> Any | None:
            """测试分页场景不读取单条历史。"""

            del history_id
            return None

    history_query = _HistoryQuery()
    with pytest.raises(RuntimeError, match="host page failed"):
        await TargetCatalog(history_query=history_query).list_targets(page=4, page_size=25)
    assert history_query.calls == [(4, 25)]


async def test_default_target_catalog_searches_host_history_with_text(configured_history_sdk: None) -> None:
    """默认目标目录通过宿主查询 SDK 返回文本命中的真实记录。"""

    token = uuid4().hex
    destination = f"/media/subtitleassistant-{token}-S02E03.mkv"
    history = TransferHistory(
        src=f"/downloads/subtitleassistant-{token}.mkv",
        dest=destination,
        dest_storage="local",
        dest_fileitem={"type": "file", "name": Path(destination).name, "extension": "mkv"},
        type="tv",
        title=f"subtitleassistant-{token}",
        status=True,
        date=datetime.now(UTC).isoformat(),
    )

    async with async_session_scope() as session:
        session.add(history)
        await session.commit()
        history_id = history.id

    try:
        result = await TargetCatalog().list_targets(page=1, page_size=25, search=f"subtitleassistant-{token}")

        assert result.total == 1
        assert [(item.id, item.dest) for item in result.items] == [(history_id, destination)]
    finally:
        async with async_session_scope() as session:
            stored = await session.get(TransferHistory, history_id)
            if stored is not None:
                await session.delete(stored)
                await session.commit()


async def test_target_query_accepts_missing_local_target_file(tmp_path: Path) -> None:
    """整理历史目标文件已被移走时仍可作为人工搜索目标。"""

    missing = tmp_path / "removed" / "Episode.S01E02.mkv"
    history = SimpleNamespace(
        id=9,
        status=True,
        dest=str(missing),
        dest_storage="local",
        dest_fileitem={"path": str(missing), "storage": "local", "type": "file", "name": missing.name},
        title="已移走剧集",
        year="2026",
        type="电视剧",
        seasons="S01",
        episodes="E02",
        media_source="themoviedb",
        media_id="90",
        date="2026-07-20 12:00:00",
    )

    class _HistoryQuery:
        """返回一条目标文件不存在的成功历史。"""

        async def async_list_transfer_history(self, *, filters: Any, page: Any) -> Any:
            """只在第一页返回成功历史。"""

            assert filters.status is True
            return SimpleNamespace(
                items=[history] if page.page == 1 else [],
                page=page.page,
                count=page.count,
                total=1,
                has_next=False,
            )

        async def async_get_transfer_history(self, history_id: int) -> Any | None:
            """按 ID 返回历史。"""

            return history if history_id == 9 else None

    target = await TargetCatalog(history_query=_HistoryQuery(), batch_size=100).get_target(9)

    assert target is not None
    assert target.history_id == 9
    assert target.context.target_path == missing


async def test_manual_search_service_groups_partial_results_and_caches_handles() -> None:
    """人工搜索并发汇总单源失败并把完整句柄保存到短期会话。"""

    target = SearchTarget(
        history_id=7,
        context=_context(),
        transferred_at=datetime.now(UTC),
    )

    class _Targets:
        """返回固定目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回目标。"""

            return target if history_id == 7 else None

    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="moviepilot:7:subtitle:42",
            source=SubtitleSource.MOVIEPILOT,
            name="候选",
            language="zh-CN",
        ),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://secret.example/item"),
    )

    class _CandidatePool:
        """返回固定来源查询结果。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """返回预置逐来源运行结果。"""

            del context, custom_queries
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(
                        source=SubtitleSource.MOVIEPILOT,
                        status="success",
                        candidates=[handle],
                        matched_query="中文标题",
                    ),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="error",
                        error_summary="OpenSubtitles 搜索失败",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(
                        source=SubtitleSource.ASSRT,
                        status="disabled",
                    ),
                }
            )

    coordinator = _ManualCoordinator()
    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=AttributionService(),
        cache=_FakeCache(),
        coordinator=coordinator,
    )

    result = await service.search(7, custom_queries={})

    assert result.session_id
    assert [run.run.source for run in result.sources] == list(SubtitleSource)
    submitted = await service.submit(result.session_id, handle.candidate.candidate_key)
    assert submitted.status == "success"
    assert submitted.task is not None
    assert coordinator.items[0].manual_handle.download_handle == handle.download_handle
    assert "https://secret.example/item" not in str(submitted.task.model_dump(mode="json"))


async def test_manual_search_uses_shared_candidate_pool_instead_of_source_search_methods() -> None:
    """人工搜索只调用来源查询，并把逐来源运行轨迹投影到安全响应。"""

    target = SimpleNamespace(history_id=7, context=_context(), transferred_at=datetime.now(UTC))

    class _Targets:
        """返回固定整理历史目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回固定目标。"""

            return target if history_id == 7 else None

    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="moviepilot:shared-candidate",
            source=SubtitleSource.MOVIEPILOT,
            name="共享候选",
            language="zh-CN",
        ),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://secret.example/shared"),
    )

    class _CandidatePool:
        """返回来源查询逐来源结果的测试替身。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """记录上下文与自定义关键词并返回安全候选池。"""

            assert context is target.context
            assert custom_queries == {
                SubtitleSource.MOVIEPILOT: "",
                SubtitleSource.OPENSUBTITLES: None,
                SubtitleSource.ASSRT: None,
            }
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(
                        source=SubtitleSource.MOVIEPILOT,
                        status="success",
                        candidates=[handle],
                        matched_query="共享查询",
                    ),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="disabled",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(
                        source=SubtitleSource.ASSRT,
                        status="unconfigured",
                    ),
                }
            )

    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=AttributionService(),
        cache=_FakeCache(),
    )

    result = await service.search(7, custom_queries={SubtitleSource.MOVIEPILOT: ""})

    assert result.session_id is not None
    assert result.sources[0].run.matched_query == "共享查询"
    assert result.sources[0].candidates[0].candidate.candidate_key == "moviepilot:shared-candidate"


async def test_manual_search_session_cache_uses_host_async_cache_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入会话缓存时使用宿主 AsyncCache 工厂及固定 TTL。"""

    cache = _FakeCache()
    factory_calls: list[tuple[str, int, int | None]] = []

    def fake_async_cache(
        *,
        cache_type: str,
        maxsize: int,
        ttl: int | None = None,
    ) -> _FakeCache:
        """记录宿主缓存工厂参数。"""

        factory_calls.append((cache_type, maxsize, ttl))
        return cache

    monkeypatch.setattr(searches_module, "AsyncCache", fake_async_cache)
    service = ManualSearch(
        targets=object(),
        candidate_pool=object(),
        matcher=object(),
    )

    assert service is not None
    assert factory_calls == [("ttl", 256, 30 * 60)]


async def test_manual_search_keeps_recognized_and_unrecognized_candidates_downloadable() -> None:
    """人工搜索保留两种识别状态、解析范围、稳定键与下载句柄。"""

    target = SearchTarget(
        history_id=7,
        context=_context(),
        transferred_at=datetime.now(UTC),
    )

    class _Targets:
        """返回固定目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回目标。"""

            return target if history_id == 7 else None

    handles = [
        CandidateHandle(
            candidate=SubtitleCandidate(
                candidate_key="moviepilot:recognized",
                source=SubtitleSource.MOVIEPILOT,
                name="English.Title.S02E03",
                language="en",
                tmdb_id=1234,
            ),
            download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/recognized"),
        ),
        CandidateHandle(
            candidate=SubtitleCandidate(
                candidate_key="moviepilot:unrecognized",
                source=SubtitleSource.MOVIEPILOT,
                name="Other.Title.S02E04",
                language="ja",
                tmdb_id=9999,
            ),
            download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/unrecognized"),
        ),
        CandidateHandle(
            candidate=SubtitleCandidate(
                candidate_key="moviepilot:recognition-error",
                source=SubtitleSource.MOVIEPILOT,
                name="Broken candidate",
                language="zh-CN",
            ),
            download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/error"),
        ),
    ]

    class _CandidatePool:
        """返回原始顺序的来源查询。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """返回预置候选与两个正常空来源。"""

            del context, custom_queries
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(
                        source=SubtitleSource.MOVIEPILOT,
                        status="success",
                        candidates=handles,
                    ),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="success",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(
                        source=SubtitleSource.ASSRT,
                        status="success",
                    ),
                }
            )

    class _ExplodingMatcher:
        """为一个候选模拟识别边界异常。"""

        def recognize_candidate(
            self,
            candidate: SubtitleCandidate,
            context: SubtitleTarget,
            host_mediainfo: Any | None,
        ) -> CandidateRecognition:
            """仅让指定候选抛出异常。"""

            if candidate.candidate_key == "moviepilot:recognition-error":
                raise ValueError("unsafe parser detail")
            return AttributionService().recognize_candidate(candidate, context, host_mediainfo)

    cache = _FakeCache()
    coordinator = _ManualCoordinator()
    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        cache=cache,
        matcher=_ExplodingMatcher(),
        coordinator=coordinator,
    )

    result = await service.search(7)

    candidates = result.sources[0].candidates
    assert [item.candidate.candidate_key for item in candidates] == [
        "moviepilot:recognized",
        "moviepilot:unrecognized",
        "moviepilot:recognition-error",
    ]
    assert [item.status for item in candidates] == [
        CandidateRecognitionStatus.RECOGNIZED,
        CandidateRecognitionStatus.UNRECOGNIZED,
        CandidateRecognitionStatus.UNRECOGNIZED,
    ]
    assert candidates[0].candidate.seasons == [2]
    assert candidates[0].candidate.episodes == [3]
    assert candidates[1].candidate.seasons == [2]
    assert candidates[1].candidate.episodes == [4]
    assert result.session_id is not None
    for handle in handles:
        submitted = await service.submit(result.session_id, handle.candidate.candidate_key)
        assert submitted.status == "success"
        assert coordinator.items[-1].manual_handle.download_handle == handle.download_handle

    invalid = await service.submit("invalid", "moviepilot:recognized")
    assert invalid.status == "session_not_found"
    missing = await service.submit(result.session_id, "moviepilot:missing")
    assert missing.status == "candidate_not_found"
    assert cache.set_calls[-1][1] == 30 * 60

    stored = next(
        value for (region, _key), value in cache.values.items() if region == searches_module.SEARCH_SESSION_REGION
    )
    assert isinstance(stored, ManualSearchSession)
    assert not hasattr(stored, "session_id")
    assert not hasattr(stored, "target_session_id")
    stored_candidate = stored.candidates["moviepilot:recognized"]
    assert isinstance(stored_candidate, CandidateHandle)
    assert not hasattr(stored_candidate, "session_id")
    assert not hasattr(stored_candidate, "target")

    rejecting = _ManualCoordinator(reject=True)
    rejecting_service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=_ExplodingMatcher(),
        cache=cache,
        coordinator=rejecting,
    )
    rejected = await rejecting_service.submit(result.session_id, "moviepilot:recognized")
    assert rejected.status == "rejected"
    cache.expired = True
    expired = await service.submit(result.session_id, "moviepilot:recognized")
    assert expired.status == "session_not_found"
    cache.expired = False

    bad_candidate = await service.submit(result.session_id, "")
    assert bad_candidate.status == "candidate_not_found"


async def test_manual_session_shape_deduplicates_target_and_session_id() -> None:
    """会话只保存一份目标与候选映射，不再逐候选重复携带会话标识与目标。"""

    target = SearchTarget(
        history_id=7,
        context=_context(),
        transferred_at=datetime.now(UTC),
    )

    class _Targets:
        """返回固定目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回目标。"""

            return target if history_id == 7 else None

    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="moviepilot:dedup",
            source=SubtitleSource.MOVIEPILOT,
            name="候选",
            language="zh-CN",
        ),
        download_handle=MoviePilotDownloadHandle(site_id=1, enclosure="https://example.invalid/dedup"),
    )

    class _CandidatePool:
        """返回单个来源的固定结果。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """返回预置候选。"""

            del context, custom_queries
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(
                        source=SubtitleSource.MOVIEPILOT,
                        status="success",
                        candidates=[handle],
                        matched_query="查询词",
                    ),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="disabled",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(source=SubtitleSource.ASSRT, status="disabled"),
                }
            )

    cache = _FakeCache()
    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=AttributionService(),
        cache=cache,
    )

    result = await service.search(7)
    assert result.session_id is not None

    stored = next(
        value for (region, _key), value in cache.values.items() if region == searches_module.SEARCH_SESSION_REGION
    )
    assert stored.target is target
    assert set(stored.candidates) == {"moviepilot:dedup"}
    assert stored.candidates["moviepilot:dedup"] == handle


async def test_manual_chain_keeps_candidates_that_automatic_admission_would_reject() -> None:
    """准入是唯一分叉点：手动链全量展示并被自动准入拒绝的候选，仍可提交。"""

    target = SearchTarget(
        history_id=7,
        context=_context(),
        transferred_at=datetime.now(UTC),
    )

    class _Targets:
        """返回固定目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回目标。"""

            return target if history_id == 7 else None

    english_only = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="opensubtitles:english-only",
            source=SubtitleSource.OPENSUBTITLES,
            name="English only",
            language="en",
        ),
        download_handle=OpenSubtitlesDownloadHandle(file_id=88),
    )

    class _CandidatePool:
        """只返回一个非简中候选。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """返回自动准入会排除的英文候选。"""

            del context, custom_queries
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(source=SubtitleSource.MOVIEPILOT, status="disabled"),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="success",
                        candidates=[english_only],
                        matched_query="English Title",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(source=SubtitleSource.ASSRT, status="disabled"),
                }
            )

    # 自动侧准入会排除该候选，证明分叉语义差异真实存在。
    admitted, rejected = admit_automatic_candidates(
        [english_only],
        target.context,
        allow_machine_translation=False,
    )
    assert admitted == []
    assert rejected == {"language": 1}

    coordinator = _ManualCoordinator()
    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=AttributionService(),
        cache=_FakeCache(),
        coordinator=coordinator,
    )

    result = await service.search(7)

    assert result.session_id is not None
    manual_candidates = result.sources[1].candidates
    assert [item.candidate.candidate_key for item in manual_candidates] == ["opensubtitles:english-only"]
    submitted = await service.submit(result.session_id, "opensubtitles:english-only")
    assert submitted.status == "success"
    assert coordinator.items[-1].manual_handle.download_handle == english_only.download_handle


async def test_manual_search_logs_each_source_with_chinese_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """人工搜索逐源记录中文结论，且不输出机器式键值字段。"""

    target = SimpleNamespace(history_id=7, context=_context(), transferred_at=datetime.now(UTC))

    class _Targets:
        """返回固定搜索目标。"""

        async def get_target(self, history_id: int) -> Any:
            """按 ID 返回测试目标。"""

            return target if history_id == 7 else None

    handle = CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="assrt:42",
            source=SubtitleSource.ASSRT,
            name="候选",
            language="zh-CN",
        ),
        download_handle=AssrtDownloadHandle(subtitle_id=42),
    )

    class _CandidatePool:
        """返回固定来源查询结果。"""

        async def query(
            self,
            context: SubtitleTarget,
            custom_queries: dict[SubtitleSource, str | None] | None = None,
        ) -> SourceSearchBatch:
            """返回逐来源日志测试数据。"""

            del context, custom_queries
            return SourceSearchBatch(
                sources={
                    SubtitleSource.MOVIEPILOT: SourceSearchResult(
                        source=SubtitleSource.MOVIEPILOT,
                        status="disabled",
                    ),
                    SubtitleSource.OPENSUBTITLES: SourceSearchResult(
                        source=SubtitleSource.OPENSUBTITLES,
                        status="error",
                        error_summary="连接超时",
                    ),
                    SubtitleSource.ASSRT: SourceSearchResult(
                        source=SubtitleSource.ASSRT,
                        status="success",
                        candidates=[handle],
                        matched_query="中文标题",
                    ),
                }
            )

    class _Logs:
        """收集人工搜索业务日志。"""

        def __init__(self) -> None:
            """创建空日志列表。"""

            self.info: list[str] = []
            self.warning: list[str] = []

        def info_log(self, message: str) -> None:
            """保存信息日志。"""

            self.info.append(message)

        def warning_log(self, message: str) -> None:
            """保存警告日志。"""

            self.warning.append(message)

    logs = _Logs()
    monkeypatch.setattr(
        searches_module,
        "logger",
        SimpleNamespace(info=logs.info_log, warning=logs.warning_log),
    )
    service = ManualSearch(
        targets=_Targets(),
        candidate_pool=_CandidatePool(),
        matcher=AttributionService(),
        cache=_FakeCache(),
    )

    result = await service.search(7, custom_queries={})

    assert result.session_id
    messages = logs.info + logs.warning
    assert any("MoviePilot 站点字幕源 未执行：该来源未启用" in item for item in messages)
    assert any("OpenSubtitles 失败：连接超时" in item for item in messages)
    assert any("ASSRT 完成：字幕站返回 1 个候选" in item for item in messages)
    assert any("共返回 1 个候选" in item for item in messages)
    assert not any("event=" in item or "source=" in item or "status=" in item for item in messages)

    ManualSearch._log_source_result(
        7,
        SourceSearchResult(
            source=SubtitleSource.MOVIEPILOT,
            status="partial",
            candidates=[handle],
            error_summary="分页请求失败",
        ),
    )
    assert any("部分完成：分页请求失败，已取得 1 个候选" in item for item in logs.warning)
    assert not any("查询 MoviePilot 站点字幕源 受限" in item for item in logs.warning)
