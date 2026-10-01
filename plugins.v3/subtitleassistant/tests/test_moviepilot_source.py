"""MoviePilot 站点字幕源适配器测试。"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest

from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    MoviePilotDownloadHandle,
    SourceHealth,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, SubtitleTarget
from app.plugins.subtitleassistant.source import moviepilot as moviepilot_module
from app.plugins.subtitleassistant.source.base import SourcePage, SourcePlan
from app.plugins.subtitleassistant.source.common import SourceRequestError, response_file_name
from app.plugins.subtitleassistant.source.common import download_file as common_download_file
from app.plugins.subtitleassistant.source.moviepilot import MoviePilotSource

pytestmark = pytest.mark.anyio


class _FakeCache:
    """提供当前用例私有的来源候选池缓存。"""

    def __init__(self) -> None:
        """创建空缓存。"""

        self.values: dict[tuple[str | None, str], Any] = {}
        self.closed = False

    async def get(self, key: str, region: str | None = None) -> Any:
        """读取指定来源区域的缓存值。"""

        return self.values.get((region, key))

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """保存指定来源区域的缓存值。"""

        del ttl
        self.values[(region, key)] = value

    async def clear(self, region: str | None = None) -> None:
        """清除指定来源区域。"""

        self.values = {item_key: value for item_key, value in self.values.items() if item_key[0] != region}

    async def close(self) -> None:
        """关闭无资源的测试缓存。"""

        self.closed = True


class _HostSubtitle:
    """禁止敏感字典序列化的宿主 SubtitleInfo 替身。"""

    def __init__(self, **overrides: Any) -> None:
        """以可覆盖默认值创建宿主字幕对象。"""

        values: dict[str, Any] = {
            "site": 7,
            "site_name": "字幕站",
            "site_order": 2,
            "subtitle_id": "42",
            "torrent_id": None,
            "title": "Show.S02E03.zh-cn.srt",
            "description": "人工字幕",
            "file_name": "Show.S02E03.zh-cn.srt",
            "enclosure": "https://subtitle.example/download/42",
            "language": "zh-CN",
            "grabs": 12,
            "pubdate": "2026-07-01 12:30:00",
        }
        values.update(overrides)
        for key, value in values.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        """一旦来源错误调用宿主敏感序列化方法就令测试失败。"""

        raise AssertionError("MoviePilotSource 不得调用 SubtitleInfo.to_dict()")


def _context(title: str = "媒体标题", english_title: str | None = "The Brilliant Adventure") -> SubtitleTarget:
    """构造 MoviePilot 标题搜索需要的最小媒体上下文。"""

    return SubtitleTarget(
        title=title,
        english_title=english_title,
        media_type=MediaType.TV,
        season=2,
        episode=3,
        target_path=Path("/media/Show.S02E03.mkv"),
        target_file_name="Show.S02E03.mkv",
    )


def _handle() -> CandidateHandle:
    """构造可执行 MoviePilot 下载的内存候选句柄。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key="moviepilot:7:subtitle:42",
            source=SubtitleSource.MOVIEPILOT,
            name="Show.S02E03.zh-cn.srt",
            file_name="Show.S02E03.zh-cn.srt",
            language="zh-CN",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
            site_id=7,
        ),
        download_handle=MoviePilotDownloadHandle(
            site_id=7,
            enclosure="https://subtitle.example/download/42",
        ),
    )


def test_moviepilot_candidate_key_without_formal_id_excludes_enclosure() -> None:
    """无正式 ID 的稳定键不因临时下载地址变化而改变。"""

    first = _HostSubtitle(subtitle_id=None, torrent_id=None, enclosure="https://example.invalid/one")
    second = _HostSubtitle(subtitle_id=None, torrent_id=None, enclosure="https://example.invalid/two")

    assert MoviePilotSource._candidate_key(first) == MoviePilotSource._candidate_key(second)


async def test_moviepilot_candidate_pool_uses_ranked_english_keywords_until_valid_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAKE 英文单词按评分串行搜索，首个可下载候选池即停。"""

    valid = _HostSubtitle(site_order="bad", grabs="bad")
    ambiguous = _HostSubtitle(
        subtitle_id="43",
        title="Show.S02E03.简体中文.srt",
        file_name="Show.S02E03.简体中文.srt",
        language="Chinese",
        enclosure="https://subtitle.example/download/43",
    )
    machine = _HostSubtitle(
        subtitle_id="44",
        title="Show.S02E03.AI翻译.srt",
        file_name="Show.S02E03.AI翻译.srt",
        language="CHS",
        enclosure="https://subtitle.example/download/44",
    )
    unusable = _HostSubtitle(subtitle_id="45", site=None)
    search = AsyncMock(side_effect=[[], [valid, ambiguous, machine, unusable]])

    class _Extractor:
        """返回固定的 YAKE 评分顺序。"""

        def extract_keywords(self, _title: str) -> list[tuple[str, float]]:
            return [("Adventure", 0.1), ("Brilliant", 0.2), ("Ignored", 0.3)]

    monkeypatch.setattr(moviepilot_module, "_create_keyword_extractor", lambda: _Extractor())

    class _SearchChain:
        """只暴露允许调用的异步标题搜索接口。"""

        async_search_subtitles_by_title = search

    monkeypatch.setattr(moviepilot_module, "SearchChain", _SearchChain)
    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(return_value=[{"id": 7, "subtitles": True}]),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))
    source = MoviePilotSource(enabled=True, cache=_FakeCache())
    result = await source.search(_context(), None)

    assert search.await_args_list[0].kwargs == {
        "title": "Adventure",
        "page": 0,
        "sites": [7],
        "cache_local": False,
    }
    assert search.await_args_list[1].kwargs == {
        "title": "Brilliant",
        "page": 0,
        "sites": [7],
        "cache_local": False,
    }
    assert search.await_count == 2
    assert len(result.candidates) == 3
    handle = result.candidates[0]
    assert handle.candidate.candidate_key == "moviepilot:7:subtitle:42"
    assert handle.candidate.site_priority is None
    assert handle.candidate.download_count is None
    assert handle.candidate.uploaded_at is not None
    assert handle.download_handle.enclosure == "https://subtitle.example/download/42"
    assert "subtitle.example" not in handle.candidate.model_dump_json()
    assert result.matched_query == "Brilliant"


async def test_moviepilot_search_skips_when_english_title_is_missing() -> None:
    """英文标题缺失时不回退中文标题或文件名。"""

    source = MoviePilotSource(True)
    result = await source.search(_context(english_title=None), None)

    assert result.candidates == []
    assert result.skip_reason == "english_title_missing"


async def test_moviepilot_search_skips_when_yake_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """YAKE 不可用时明确跳过来源，不自行拆分英文标题。"""

    monkeypatch.setattr(moviepilot_module, "_create_keyword_extractor", lambda: None)

    source = MoviePilotSource(True)
    result = await source.search(_context(), None)

    assert result.candidates == []
    assert result.skip_reason == "yake_unavailable"


async def test_moviepilot_search_returns_safe_error_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """宿主搜索异常时返回脱敏错误且不执行自动重试。"""

    search = AsyncMock(side_effect=RuntimeError("包含内部细节"))

    class _SearchChain:
        """模拟一次即失败的宿主搜索链。"""

        async_search_subtitles_by_title = search

    monkeypatch.setattr(moviepilot_module, "SearchChain", _SearchChain)
    monkeypatch.setattr(
        moviepilot_module,
        "_create_keyword_extractor",
        lambda: SimpleNamespace(extract_keywords=lambda _title: [("Adventure", 0.1)]),
    )
    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(return_value=[{"id": 7, "subtitles": True}]),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))

    source = MoviePilotSource(True, cache=_FakeCache())
    result = await source.search(_context(), None)

    assert search.await_count == 1
    assert result.candidates == []
    assert result.status == "error"
    assert result.error_summary == "字幕源请求失败"
    assert "内部细节" not in result.error_summary


async def test_moviepilot_download_rehydrates_current_site_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载前重新读取站点，并只把当前 Cookie、UA、代理和超时交给请求工具。"""

    site = SimpleNamespace(
        is_active=True,
        cookie="fresh-cookie=1",
        ua="Fresh-UA",
        proxy=1,
        timeout=37,
    )
    site_oper = SimpleNamespace(async_get=AsyncMock(return_value=site))
    monkeypatch.setattr(moviepilot_module, "SiteOper", lambda: site_oper)
    request = object()
    request_kwargs: list[dict[str, Any]] = []

    def fake_request_utils(**kwargs: Any) -> object:
        """记录异步请求工具收到的当前站点配置。"""

        request_kwargs.append(kwargs)
        return request

    monkeypatch.setattr(moviepilot_module, "AsyncRequestUtils", fake_request_utils)
    download_file = AsyncMock(return_value=tmp_path / "Show.S02E03.zh-cn.srt")
    monkeypatch.setattr(moviepilot_module, "download_file", download_file)

    asset = await MoviePilotSource(True).download(_handle(), tmp_path)

    site_oper.async_get.assert_awaited_once_with(7)
    assert request_kwargs == [
        {
            "cookies": "fresh-cookie=1",
            "ua": "Fresh-UA",
            "proxies": moviepilot_module.settings.PROXY,
            "timeout": 37,
        }
    ]
    download_file.assert_awaited_once_with(
        request,
        "https://subtitle.example/download/42",
        tmp_path,
        "Show.S02E03.zh-cn.srt",
        prefer_response_name=True,
    )
    assert asset.path == tmp_path / "Show.S02E03.zh-cn.srt"
    assert asset.file_name == "Show.S02E03.zh-cn.srt"


def test_moviepilot_download_name_prefers_response_then_url() -> None:
    """MoviePilot 下载文件名优先响应头，其次 URL，最终才用安全后备名。"""

    assert (
        response_file_name(
            {"content-disposition": 'attachment; filename="subtitle.ass"'},
            "https://example.invalid/download/42",
            "fallback.bin",
        )
        == "subtitle.ass"
    )
    assert (
        response_file_name(
            {},
            "https://example.invalid/files/subtitle.srt?token=secret",
            "fallback.bin",
        )
        == "subtitle.srt"
    )
    assert response_file_name({}, "https://example.invalid/download/", "fallback.bin") == "fallback.bin"


async def test_download_name_uses_redirected_response_url_before_original_url(tmp_path: Path) -> None:
    """响应头无文件名时优先使用重定向后的最终响应 URL。"""

    class _Response:
        """提供下载流和最终重定向 URL。"""

        status_code = 200
        headers: ClassVar[dict[str, str]] = {}
        url = "https://cdn.example.invalid/files/final-subtitle.ass?token=secret"

        async def aiter_bytes(self, _chunk_size: int):
            """返回单段测试字幕内容。"""

            yield b"subtitle"

    class _Stream:
        """异步上下文包装测试响应。"""

        async def __aenter__(self) -> _Response:
            """进入下载流。"""

            return _Response()

        async def __aexit__(self, *_args: object) -> None:
            """退出下载流。"""

            return

    class _Request:
        """返回固定下载流。"""

        def get_stream(self, url: str) -> _Stream:
            """校验原始地址并返回流。"""

            assert url == "https://origin.example.invalid/download/42"
            return _Stream()

    path = await common_download_file(
        _Request(),
        "https://origin.example.invalid/download/42",
        tmp_path,
        "fallback.bin",
        prefer_response_name=True,
    )

    assert path.name == "final-subtitle.ass"
    assert path.read_bytes() == b"subtitle"


@pytest.mark.parametrize(
    "site",
    [None, SimpleNamespace(is_active=False)],
)
async def test_moviepilot_download_rejects_missing_or_inactive_site(
    site: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """站点不存在或停用时不回退使用搜索结果中的旧凭据。"""

    site_oper = SimpleNamespace(async_get=AsyncMock(return_value=site))
    monkeypatch.setattr(moviepilot_module, "SiteOper", lambda: site_oper)

    def fail_request(*_args: Any, **_kwargs: Any) -> None:
        """禁止无效站点分支构造网络请求。"""

        raise AssertionError("无效站点不得构造 AsyncRequestUtils")

    monkeypatch.setattr(moviepilot_module, "AsyncRequestUtils", fail_request)

    with pytest.raises(SourceRequestError, match="不存在或已停用"):
        await MoviePilotSource(True).download(_handle(), tmp_path)

    site_oper.async_get.assert_awaited_once_with(7)


async def test_moviepilot_refresh_only_reads_active_site_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源刷新只读取宿主有效站点，不构造字幕搜索或第三方 HTTP。"""

    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(
            return_value=[
                {"id": 1, "name": "站点甲", "subtitles": {}},
                {"id": 2, "name": "站点乙", "subtitles": {}},
            ]
        ),
    )

    def fail_http(*_args: Any, **_kwargs: Any) -> None:
        """禁止状态刷新构造网络请求。"""

        raise AssertionError("MoviePilot 状态刷新不得发起 HTTP")

    monkeypatch.setattr(moviepilot_module, "AsyncRequestUtils", fail_http)
    source = MoviePilotSource(True)

    status = await source.refresh(manual=True)

    assert status.health is SourceHealth.HEALTHY
    assert status.details["site_names"] == ["站点甲", "站点乙"]
    assert status.details["site_count"] == 2


async def test_moviepilot_search_skips_without_enabled_subtitle_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有启用字幕站点时不调用 SearchChain 并返回明确跳过原因。"""

    search = AsyncMock()
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: ())

    source = MoviePilotSource(True)
    result = await source.search(_context(), None)

    assert result.status == "unconfigured"
    search.assert_not_awaited()


async def test_moviepilot_default_query_reuses_shared_candidate_pool_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自动与默认人工对同一关键词共享候选池缓存。"""

    item = _HostSubtitle(file_name="untrusted.ass", title="Example SDH 听障")
    search = AsyncMock(return_value=[item])
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(return_value=[{"id": 7, "subtitles": True}]),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))

    class _Extractor:
        """固定只返回一个默认关键词。"""

        def extract_keywords(self, _title: str) -> list[tuple[str, float]]:
            return [("Adventure", 0.1)]

    monkeypatch.setattr(moviepilot_module, "_create_keyword_extractor", lambda: _Extractor())
    cache = _FakeCache()
    source = MoviePilotSource(True, cache=cache)

    automatic = await source.search(_context(), None)
    manual = await source.search(_context(), None)

    assert search.await_count == 1
    assert "format" not in automatic.candidates[0].candidate.model_dump()
    assert "hearing_impaired" not in automatic.candidates[0].candidate.model_dump()
    assert manual.cache_hit is True


async def test_moviepilot_refresh_reports_no_subtitle_site_as_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源刷新在无字幕站点时不误报健康。"""

    monkeypatch.setattr(MoviePilotSource, "_subtitle_site_indexers", AsyncMock(return_value=[]))

    status = await MoviePilotSource(True).refresh()

    assert status.health is SourceHealth.DISABLED
    assert status.configured is False
    assert status.details["site_count"] == 0
    assert status.last_error_summary == "没有启用且支持字幕搜索的站点"


def test_moviepilot_plan_preserves_ranked_defaults_and_replaces_them_for_custom_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MoviePilot 计划保留默认关键词展示，并让非空自定义词独占执行计划。"""

    class _Extractor:
        """返回固定的 YAKE 关键词顺序。"""

        def extract_keywords(self, _title: str) -> list[tuple[str, float]]:
            return [("Adventure", 0.1), ("Brilliant", 0.2)]

    monkeypatch.setattr(moviepilot_module, "_create_keyword_extractor", lambda: _Extractor())
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))
    source = MoviePilotSource(True)

    default_plan = source._plan(_context(), None)
    custom_plan = source._plan(_context(), "Full Custom Query")

    assert isinstance(default_plan, SourcePlan)
    assert [query.label for query in default_plan.queries] == ["Adventure", "Brilliant"]
    assert [query.query for query in default_plan.queries] == ["Adventure", "Brilliant"]
    assert [query.kind for query in default_plan.queries] == ["title", "title"]
    assert [query.label for query in custom_plan.queries] == ["Full Custom Query"]
    assert custom_plan.queries[0].kind == "filename"


async def test_moviepilot_fetch_page_safe_candidates_and_host_page_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MoviePilot 单页查询只调用宿主一次并排除缺少安全下载定位的结果。"""

    valid = _HostSubtitle()
    invalid = _HostSubtitle(subtitle_id="missing-locator", site=None)
    search = AsyncMock(return_value=[valid, invalid])
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(return_value=[{"id": 7, "subtitles": True}]),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))
    source = MoviePilotSource(True)
    query = source._plan(_context(), "Adventure").queries[0]

    page = await source._fetch_page(query, 1)

    assert isinstance(page, SourcePage)
    assert search.await_args.kwargs == {
        "title": "Adventure",
        "page": 0,
        "sites": [7],
        "cache_local": False,
    }
    assert page.raw_count == 1
    assert page.download_locator_excluded_count == 1
    assert len(page.candidates) == 1
    assert page.candidates[0].download_handle == MoviePilotDownloadHandle(
        site_id=7,
        enclosure="https://subtitle.example/download/42",
    )


async def test_moviepilot_candidate_pool_reuses_shared_cache_without_source_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复查询复用共享候选池缓存，而不是由 MoviePilot 来源保存缓存。"""

    item = _HostSubtitle()
    search = AsyncMock(return_value=[item])
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    monkeypatch.setattr(
        MoviePilotSource,
        "_subtitle_site_indexers",
        AsyncMock(return_value=[{"id": 7, "subtitles": True}]),
    )
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: (7,))
    source = MoviePilotSource(True, cache=_FakeCache())

    first = await source.search(_context(), "Adventure")
    second = await source.search(_context(), "Adventure")

    assert search.await_count == 1
    assert first.candidates[0].candidate.candidate_key == "moviepilot:7:subtitle:42"
    assert second.cache_hit is True


async def test_moviepilot_missing_english_title_is_a_shared_skipped_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MoviePilot 无法生成默认关键词时由共享查询结果表达跳过原因。"""

    search = AsyncMock()
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    source = MoviePilotSource(True, cache=_FakeCache())

    result = await source.search(_context(english_title=None), None)

    assert result.status == "success"
    assert result.skip_reason == "english_title_missing"
    assert result.candidates == []
    search.assert_not_awaited()


async def test_moviepilot_without_enabled_sites_is_shared_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有可用字幕站点时共享查询返回未配置且不写缓存。"""

    search = AsyncMock()
    monkeypatch.setattr(
        moviepilot_module,
        "SearchChain",
        lambda: SimpleNamespace(async_search_subtitles_by_title=search),
    )
    monkeypatch.setattr(MoviePilotSource, "_subtitle_site_indexers", AsyncMock(return_value=[]))
    monkeypatch.setattr(MoviePilotSource, "_sync_subtitle_site_ids", lambda _self: ())
    cache = _FakeCache()
    source = MoviePilotSource(True, cache=cache)

    result = await source.search(_context(), None)

    assert result.status == "unconfigured"
    assert result.candidates == []
    assert cache.values == {}
    search.assert_not_awaited()
