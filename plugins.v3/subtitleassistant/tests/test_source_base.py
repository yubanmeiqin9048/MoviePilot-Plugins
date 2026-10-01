"""字幕来源基类的查询编排、缓存、去重与状态收敛测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.source import (
    AssrtDownloadHandle,
    CandidateHandle,
    DownloadedAsset,
    MoviePilotDownloadHandle,
    OpenSubtitlesDownloadHandle,
    SourceErrorCode,
    SourceHealth,
    SourceSearchStatus,
    SourceStatus,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import SubtitleTarget
from app.plugins.subtitleassistant.source import SourceAdministration
from app.plugins.subtitleassistant.source import base as base_module
from app.plugins.subtitleassistant.source.base import (
    SourcePage,
    SourcePlan,
    SourcePlanQuery,
    SubtitleSourceBase,
)
from app.plugins.subtitleassistant.source.common import SourceLimitedError, SourceRequestError

pytestmark = pytest.mark.anyio


class _FakeCache:
    """记录来源缓存读取与写入的内存替身。"""

    def __init__(self) -> None:
        """创建空缓存。"""

        self.values: dict[tuple[str | None, str], Any] = {}
        self.get_calls: list[tuple[str, str | None]] = []
        self.set_calls: list[tuple[str, Any, int | None, str | None]] = []

    async def get(self, key: str, region: str | None = None) -> Any:
        """读取一个缓存值。"""

        self.get_calls.append((key, region))
        return self.values.get((region, key))

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """保存一个带 TTL 的缓存值。"""

        self.set_calls.append((key, value, ttl, region))
        self.values[(region, key)] = value


class _ConcurrencyGate:
    """只允许全部来源进入后继续的并发测试闸门。"""

    def __init__(self, expected: int) -> None:
        """创建指定参与数的闸门。"""

        self.expected = expected
        self.arrived = 0
        self.release = asyncio.Event()

    async def wait(self) -> None:
        """等待所有来源进入查询。"""

        self.arrived += 1
        if self.arrived == self.expected:
            self.release.set()
        await self.release.wait()


class _FakeSource(SubtitleSourceBase):
    """通过计划、分页脚本与探针模拟一个字幕来源。"""

    def __init__(
        self,
        source: SubtitleSource,
        plans: Sequence[SourcePlanQuery] | SourcePlan = (),
        pages: Mapping[tuple[str, int], SourcePage | BaseException] | None = None,
        *,
        enabled: bool = True,
        configured: bool = True,
        skip_reason: str | None = None,
        ttl: int | None = None,
        probe: Any = None,
        cache: _FakeCache | None = None,
        gate: _ConcurrencyGate | None = None,
    ) -> None:
        """保存来源状态、计划、分页脚本与探针结论。"""

        super().__init__(enabled=enabled, cache=cache)
        self.source = source
        self._configured = configured
        self._plans = plans
        self._skip_reason = skip_reason
        self._pages = dict(pages or {})
        self._probe_result = probe
        self.gate = gate
        self.calls: list[tuple[str, int]] = []
        if ttl is not None:
            self.CACHE_TTL_SECONDS = ttl

    @property
    def configured(self) -> bool:
        """返回预置的配置完整性。"""

        return self._configured

    def _plan(self, _context: SubtitleTarget, _custom_query: str | None) -> SourcePlan:
        """返回预置有序查询计划。"""

        if isinstance(self._plans, SourcePlan):
            return self._plans
        return SourcePlan(
            queries=self._plans,
            configured=self._configured,
            skip_reason=self._skip_reason,
        )

    async def _fetch_page(self, query: SourcePlanQuery, page: int) -> SourcePage:
        """返回预置页或抛出预置来源错误。"""

        self.calls.append((query.label, page))
        if self.gate is not None:
            await self.gate.wait()
        await asyncio.sleep(0)
        value = self._pages[(query.label, page)]
        if isinstance(value, BaseException):
            raise value
        return value

    async def _probe(self, _manual: bool) -> Any:
        """返回预置探针结论或异常。"""

        if isinstance(self._probe_result, BaseException):
            raise self._probe_result
        return self._probe_result

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """返回一个不落盘的假下载资产。"""

        del handle
        return DownloadedAsset(path=directory / "candidate.srt", file_name="candidate.srt")


def _context() -> SubtitleTarget:
    """构造测试媒体上下文。"""

    return SubtitleTarget(
        title="测试标题",
        english_title="Test Title",
        year=2026,
        target_path=Path("/media/Test.mkv"),
        target_file_name="Test.mkv",
    )


def _handle(source: SubtitleSource, key: str = "candidate") -> CandidateHandle:
    """构造带安全下载定位的候选句柄。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key=key,
            source=source,
            name="测试候选",
            language="zh-cn",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
        ),
        download_handle=(
            MoviePilotDownloadHandle(site_id=1, enclosure=f"https://example.invalid/{key}")
            if source is SubtitleSource.MOVIEPILOT
            else OpenSubtitlesDownloadHandle(file_id=1)
            if source is SubtitleSource.OPENSUBTITLES
            else AssrtDownloadHandle(subtitle_id=1)
        ),
    )


def _page(
    source: SubtitleSource,
    *keys: str,
    raw_count: int | None = None,
    excluded: int = 0,
    has_next: bool = False,
) -> SourcePage:
    """构造一个来源归一化页。"""

    handles = [_handle(source, key) for key in keys]
    return SourcePage(
        candidates=handles,
        raw_count=len(handles) if raw_count is None else raw_count,
        download_locator_excluded_count=excluded,
        has_next=has_next,
    )


def _query(label: str, identity: dict[str, Any] | None = None) -> SourcePlanQuery:
    """构造一个来源查询计划项。"""

    return SourcePlanQuery(label=label, identity=identity if identity is not None else {"query": label})


async def test_default_cache_is_created_through_host_async_cache_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未注入缓存时基类通过宿主工厂创建可替换缓存，而不绑定具体后端。"""

    cache = _FakeCache()
    factory_calls: list[tuple[str, int, int | None]] = []

    def fake_async_cache(
        *,
        cache_type: str,
        maxsize: int,
        ttl: int | None = None,
    ) -> _FakeCache:
        """记录宿主缓存工厂参数并返回内存替身。"""

        factory_calls.append((cache_type, maxsize, ttl))
        return cache

    monkeypatch.setattr(base_module, "AsyncCache", fake_async_cache)
    source = _FakeSource(SubtitleSource.ASSRT, pages={("query", 1): _page(SubtitleSource.ASSRT, "factory-cache")})
    source._plans = [_query("query")]

    result = await source.search(_context(), None)

    assert result.status == "success"
    assert factory_calls == [("ttl", 512, None)]
    assert cache.set_calls[0][3] == "subtitleassistant_source_query"


async def test_source_plan_falls_back_after_empty_and_locator_exclusions_then_stops_on_valid_pool() -> None:
    """来源在空结果和下载定位排除后回退，并在首个有效池后停止。"""

    source = SubtitleSource.ASSRT
    adapter = _FakeSource(
        source,
        [_query("first"), _query("second"), _query("third")],
        {
            ("first", 1): SourcePage(raw_count=2, download_locator_excluded_count=2),
            ("second", 1): _page(source, "hit", raw_count=1),
            ("third", 1): _page(source, "must-not-run"),
        },
    )

    run = await adapter.search(_context(), None)

    assert run.status == "success"
    assert run.matched_query == "second"
    assert len(run.candidates) == 1
    assert adapter.calls == [("first", 1), ("second", 1)]


async def test_limited_or_request_error_stops_the_source_plan() -> None:
    """限流和请求错误立即结束来源计划。"""

    for exception, expected_status in (
        (SourceLimitedError("暂时限流", retry_at=datetime.now(UTC)), "limited"),
        (SourceRequestError("请求失败"), "error"),
    ):
        source = SubtitleSource.MOVIEPILOT
        adapter = _FakeSource(
            source,
            [_query("first"), _query("fallback")],
            {
                ("first", 1): exception,
                ("fallback", 1): _page(source, "must-not-run"),
            },
        )

        run = await adapter.search(_context(), None)

        assert run.status == expected_status
        assert len(run.candidates) == 0
        assert adapter.calls == [("first", 1)]
        if isinstance(exception, SourceLimitedError):
            assert run.error_code is SourceErrorCode.LIMITED
            assert run.retry_after_seconds == 0


async def test_pagination_failure_keeps_candidates_and_does_not_cache_or_fallback() -> None:
    """分页中途失败保留已有候选并停止后续计划，且不写缓存。"""

    source = SubtitleSource.OPENSUBTITLES
    cache = _FakeCache()
    adapter = _FakeSource(
        source,
        [_query("paged"), _query("fallback")],
        {
            ("paged", 1): _page(source, "first-page", has_next=True),
            ("paged", 2): SourceRequestError("第二页失败"),
            ("fallback", 1): _page(source, "must-not-run"),
        },
        cache=cache,
    )

    run = await adapter.search(_context(), None)

    assert run.status == "partial"
    assert [item.candidate.candidate_key for item in run.candidates] == ["first-page"]
    assert cache.set_calls == []
    assert adapter.calls == [("paged", 1), ("paged", 2)]


async def test_cache_reuses_empty_and_non_empty_results_and_validates_malformed_values() -> None:
    """完整成功结果和正常空结果可复用，畸形缓存按未命中处理。"""

    source = SubtitleSource.ASSRT
    cache = _FakeCache()
    adapter = _FakeSource(
        source,
        [_query("query")],
        {("query", 1): _page(source, "cached")},
        cache=cache,
    )
    first = await adapter.search(_context(), None)
    assert len(cache.set_calls) == 1
    assert first.cache_hit is False
    assert first.candidates[0].candidate.candidate_key == "cached"

    hit = await adapter.search(_context(), None)
    assert hit.cache_hit is True
    assert isinstance(hit.candidates[0].download_handle, AssrtDownloadHandle)

    key, _value, _ttl, region = cache.set_calls[0]
    cache.values[(region, key)] = {"not": "a source query"}
    malformed_adapter = _FakeSource(
        source,
        [_query("query")],
        {("query", 1): _page(source, "reloaded")},
        cache=cache,
    )
    malformed = await malformed_adapter.search(_context(), None)
    assert malformed.cache_hit is False
    assert malformed_adapter.calls == [("query", 1)]


async def test_complete_empty_query_is_cached_even_when_later_query_fails() -> None:
    """查询完整即写缓存：先完成的空查询不因后续查询失败而失效。"""

    source = SubtitleSource.ASSRT
    cache = _FakeCache()
    adapter = _FakeSource(
        source,
        SourcePlan(queries=[_query("empty"), _query("failed")]),
        {
            ("empty", 1): _page(source, raw_count=0),
            ("failed", 1): SourceRequestError("请求失败"),
        },
        cache=cache,
    )

    run = await adapter.search(_context(), None)

    assert run.status == "error"
    assert len(cache.set_calls) == 1
    stored = cache.set_calls[0][1]
    assert stored["handles"] == []


@pytest.mark.parametrize(
    ("source", "expected_ttl"),
    [
        (SubtitleSource.MOVIEPILOT, 600),
        (SubtitleSource.OPENSUBTITLES, 1800),
        (SubtitleSource.ASSRT, 1800),
    ],
)
async def test_source_cache_uses_fixed_ttl_and_isolates_source_identity(
    source: SubtitleSource,
    expected_ttl: int,
) -> None:
    """来源缓存使用固定 TTL，且相同查询身份不会跨来源复用。"""

    cache = _FakeCache()
    adapter = _FakeSource(
        source,
        [_query("same")],
        {("same", 1): _page(source, "candidate")},
        cache=cache,
        ttl=expected_ttl,
    )
    run = await adapter.search(_context(), None)

    assert run.status == "success"
    assert cache.set_calls[0][2] == expected_ttl

    other = SubtitleSource.ASSRT if source is not SubtitleSource.ASSRT else SubtitleSource.MOVIEPILOT
    other_adapter = _FakeSource(
        other,
        [_query("same")],
        {("same", 1): _page(other, "other-candidate")},
        cache=cache,
    )
    other_run = await other_adapter.search(_context(), None)

    assert other_run.cache_hit is False
    assert other_adapter.calls == [("same", 1)]


async def test_structured_query_identity_is_part_of_the_cache_key() -> None:
    """同标签不同结构化远端身份不会错误复用缓存。"""

    source = SubtitleSource.MOVIEPILOT
    cache = _FakeCache()
    first = _FakeSource(
        source,
        [_query("same", {"query": "one"})],
        {("same", 1): _page(source, "one")},
        cache=cache,
    )
    await first.search(_context(), None)

    second = _FakeSource(
        source,
        [_query("same", {"query": "two"})],
        {("same", 1): _page(source, "two")},
        cache=cache,
    )
    run = await second.search(_context(), None)

    assert run.cache_hit is False
    assert second.calls == [("same", 1)]

    same_identity = _FakeSource(
        source,
        [_query("same", {"query": "one"})],
        {("same", 1): _page(source, "reloaded")},
        cache=cache,
    )
    reused = await same_identity.search(_context(), None)
    assert reused.cache_hit is True
    assert reused.candidates[0].candidate.candidate_key == "one"


@pytest.mark.parametrize(
    ("enabled", "configured", "plans", "expected_status", "expected_skip"),
    [
        (False, True, [_query("query")], "disabled", None),
        (True, False, [_query("query")], "unconfigured", None),
        (True, True, [], "success", "query_unavailable"),
    ],
)
async def test_stable_statuses_and_no_executable_query(
    enabled: bool,
    configured: bool,
    plans: list[SourcePlanQuery],
    expected_status: str,
    expected_skip: str | None,
) -> None:
    """禁用、未配置和无可执行查询使用稳定状态。"""

    source = SubtitleSource.MOVIEPILOT
    adapter = _FakeSource(
        source,
        plans,
        {("query", 1): _page(source, "must-not-run")},
        enabled=enabled,
        configured=configured,
    )

    run = await adapter.search(_context(), None)

    assert run.status == expected_status
    assert run.skip_reason == expected_skip
    assert run.candidates == []
    assert adapter.calls == []
    if enabled:
        assert [entry.label for entry in run.default_queries] == [item.label for item in plans]
    else:
        assert run.default_queries == []


async def test_query_deduplicates_candidates_in_first_seen_order() -> None:
    """跨页重复候选只保留首次出现项。"""

    source = SubtitleSource.ASSRT
    adapter = _FakeSource(
        source,
        [_query("query")],
        {
            ("query", 1): _page(source, "first", "second", has_next=True),
            ("query", 2): _page(source, "second", "third"),
        },
    )

    run = await adapter.search(_context(), None)

    assert [item.candidate.candidate_key for item in run.candidates] == ["first", "second", "third"]


async def test_pagination_upper_bound_stops_with_partial_result() -> None:
    """分页达到查询上限时保留已取得候选并收敛为 partial。"""

    source = SubtitleSource.OPENSUBTITLES
    adapter = _FakeSource(
        source,
        [SourcePlanQuery(label="paged", identity={"query": "paged"}, max_pages=2)],
        {
            ("paged", 1): _page(source, "first", has_next=True),
            ("paged", 2): _page(source, "second", has_next=True),
        },
    )

    run = await adapter.search(_context(), None)

    assert run.status == "partial"
    assert adapter.calls == [("paged", 1), ("paged", 2)]


async def test_refresh_short_circuits_when_disabled_or_unconfigured() -> None:
    """禁用或未配置时刷新直接返回禁用状态且不探测。"""

    adapter = _FakeSource(SubtitleSource.ASSRT, enabled=False)

    status = await adapter.refresh()

    assert status.health is SourceHealth.DISABLED
    assert status.last_checked_at is None


async def test_refresh_probe_conclusions_and_error_classification() -> None:
    """刷新骨架把探测结论与限流/错误异常收敛为来源状态。"""

    healthy = _FakeSource(SubtitleSource.MOVIEPILOT, probe=None)
    healthy._last_details = {"site_count": 1}
    healthy_status = await healthy.refresh(manual=True)
    assert healthy_status.health is SourceHealth.HEALTHY
    assert healthy_status.details == {"site_count": 1}
    assert healthy_status.last_duration_ms is not None

    limited = _FakeSource(SubtitleSource.ASSRT, probe=SourceLimitedError("暂时限流", retry_at=datetime.now(UTC)))
    limited_status = await limited.refresh()
    assert limited_status.health is SourceHealth.LIMITED

    failed = _FakeSource(SubtitleSource.ASSRT, probe=SourceRequestError("ASSRT 配额检查失败"))
    failed_status = await failed.refresh()
    assert failed_status.health is SourceHealth.ERROR
    assert failed_status.last_error_summary == "ASSRT 配额检查失败"


async def test_facade_query_runs_all_sources_concurrently_and_passes_custom_queries() -> None:
    """facade 扇出并发运行全部来源并按来源传递自定义关键词。"""

    sources = [SubtitleSource.MOVIEPILOT, SubtitleSource.OPENSUBTITLES, SubtitleSource.ASSRT]
    gate = _ConcurrencyGate(expected=len(sources))
    adapters: dict[SubtitleSource, _FakeSource] = {}
    for source in sources:
        adapter = _FakeSource(
            source,
            [SourcePlanQuery(label=f"{source.value}-query", identity={"query": source.value})],
            {(f"{source.value}-query", 1): _page(source, source.value)},
            gate=gate,
        )
        adapters[source] = adapter

    received: dict[SubtitleSource, str | None] = {}
    for source, adapter in adapters.items():
        original = adapter._plan

        def make_plan(original_plan: Any, source_value: SubtitleSource) -> Any:
            def plan(context: SubtitleTarget, custom_query: str | None) -> SourcePlan:
                received[source_value] = custom_query
                return original_plan(context, custom_query)

            return plan

        adapter._plan = make_plan(original, source)  # type: ignore[method-assign]

    facade = SourceAdministration()
    facade._adapters = adapters

    result = await asyncio.wait_for(
        facade.query(
            _context(),
            custom_queries={SubtitleSource.OPENSUBTITLES: "自定义关键词"},
        ),
        timeout=1,
    )

    assert set(result.sources) == set(sources)
    assert all(item.status is SourceSearchStatus.SUCCESS for item in result.sources.values())
    assert received == {
        SubtitleSource.MOVIEPILOT: None,
        SubtitleSource.OPENSUBTITLES: "自定义关键词",
        SubtitleSource.ASSRT: None,
    }
    assert all(adapter.calls == [(f"{source.value}-query", 1)] for source, adapter in adapters.items())


async def test_default_queries_project_actual_query_parameter_and_match_search_result() -> None:
    """默认计划投影取查询身份的实际参数，且独立 default_queries 与 search 结果一致。"""

    source = SubtitleSource.OPENSUBTITLES
    query = SourcePlanQuery(
        label="IMDb ID: tt1",
        identity={"params": {"imdb_id": 1}},
        kind="id",
        query="1",
    )
    adapter = _FakeSource(source, [query], {("IMDb ID: tt1", 1): _page(source, "hit")})

    standalone = adapter.default_queries(_context())
    run = await adapter.search(_context(), None)

    assert [(entry.kind, entry.label, entry.query) for entry in standalone] == [("id", "IMDb ID: tt1", "1")]
    assert run.default_queries == standalone
    assert run.default_queries[0].query != run.default_queries[0].label


@pytest.mark.parametrize(
    ("source", "bad_handle"),
    [
        (SubtitleSource.OPENSUBTITLES, {}),
        (SubtitleSource.MOVIEPILOT, {"enclosure": "https://example.invalid/x"}),
        (SubtitleSource.ASSRT, {}),
    ],
)
async def test_malformed_cached_download_handle_is_treated_as_cache_miss(
    source: SubtitleSource,
    bad_handle: dict[str, Any],
) -> None:
    """缓存下载定位缺少字段的畸形值按未命中处理，回退来源请求而不向外抛异常。"""

    cache = _FakeCache()
    query = _query("query")
    good = _handle(source, "cached")
    cache.values[(base_module.SOURCE_CACHE_REGION, SubtitleSourceBase._cache_key(source, query))] = {
        "source": source.value,
        "handles": [{"candidate": good.candidate.model_dump(mode="json"), "download_handle": bad_handle}],
        "raw_count": 1,
        "download_locator_excluded_count": 0,
        "malformed_count": 0,
    }
    adapter = _FakeSource(source, [query], {("query", 1): _page(source, "reloaded")}, cache=cache)

    run = await adapter.search(_context(), None)

    assert run.cache_hit is False
    assert [item.candidate.candidate_key for item in run.candidates] == ["reloaded"]
    assert adapter.calls == [("query", 1)]


async def test_facade_batch_query_survives_malformed_source_cache() -> None:
    """单源畸形缓存不使 facade 批量查询失败，各源仍交付真实计划条目。"""

    sources = list(SubtitleSource)
    cache = _FakeCache()
    adapters: dict[SubtitleSource, _FakeSource] = {}
    for source in sources:
        query = SourcePlanQuery(
            label=f"{source.value}-query",
            identity={"query": source.value},
            query=source.value,
        )
        adapter = _FakeSource(
            source,
            [query],
            {(f"{source.value}-query", 1): _page(source, source.value)},
            cache=cache,
        )
        adapters[source] = adapter
        if source is SubtitleSource.OPENSUBTITLES:
            broken = _handle(source, "broken")
            cache.values[(base_module.SOURCE_CACHE_REGION, SubtitleSourceBase._cache_key(source, query))] = {
                "source": source.value,
                "handles": [{"candidate": broken.candidate.model_dump(mode="json"), "download_handle": {}}],
                "raw_count": 1,
                "download_locator_excluded_count": 0,
                "malformed_count": 0,
            }

    facade = SourceAdministration()
    facade._adapters = adapters

    batch = await facade.query(_context())

    assert set(batch.sources) == set(sources)
    assert all(run.status is SourceSearchStatus.SUCCESS for run in batch.sources.values())
    assert batch.sources[SubtitleSource.OPENSUBTITLES].cache_hit is False
    assert adapters[SubtitleSource.OPENSUBTITLES].calls == [("opensubtitles-query", 1)]
    for source, adapter in adapters.items():
        assert batch.sources[source].default_queries == adapter.default_queries(_context())
