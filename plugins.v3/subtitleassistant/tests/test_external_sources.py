"""OpenSubtitles 与 ASSRT 查询、临时链接及限流测试。"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.plugins.subtitleassistant.schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
from app.plugins.subtitleassistant.schemas.source import (
    CandidateHandle,
    AssrtDownloadHandle,
    OpenSubtitlesDownloadHandle,
    SourceHealth,
    SubtitleSource,
)
from app.plugins.subtitleassistant.schemas.target import MediaType, SubtitleTarget
from app.plugins.subtitleassistant.source import assrt as assrt_module
from app.plugins.subtitleassistant.source import limiter as limiter_module
from app.plugins.subtitleassistant.source import opensubtitles as opensubtitles_module
from app.plugins.subtitleassistant.source.assrt import AssrtSource
from app.plugins.subtitleassistant.source.limiter import SlidingWindowLimiter
from app.plugins.subtitleassistant.source.opensubtitles import OpenSubtitlesSource

pytestmark = pytest.mark.anyio


class _FakeCache:
    """记录候选缓存访问的内存替身。"""

    def __init__(self) -> None:
        """创建空缓存及调用记录。"""

        self.values: dict[str, Any] = {}
        self.get_calls: list[str] = []
        self.set_calls: list[tuple[str, Any, int | None]] = []
        self.clear_calls = 0

    async def get(self, key: str, region: str | None = None) -> Any:
        """读取缓存值并记录键。"""

        del region
        self.get_calls.append(key)
        return self.values.get(key)

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """保存缓存值并记录 TTL。"""

        del region
        self.values[key] = value
        self.set_calls.append((key, value, ttl))

    async def clear(self, region: str | None = None) -> None:
        """清空全部测试缓存。"""

        del region
        self.clear_calls += 1
        self.values.clear()

    async def close(self) -> None:
        """关闭无资源的测试缓存。"""

        return


def _context() -> SubtitleTarget:
    """构造同时具备 ID、文件名和备选标题的剧集上下文。"""

    return SubtitleTarget(
        title="测试主标题",
        original_title="原始标题",
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


def _handle(source: SubtitleSource, candidate_key: str = "candidate") -> CandidateHandle:
    """构造外部来源测试候选句柄。"""

    return CandidateHandle(
        candidate=SubtitleCandidate(
            candidate_key=candidate_key,
            source=source,
            name="候选字幕",
            file_name="candidate.srt",
            language="zh-cn" if source is SubtitleSource.OPENSUBTITLES else "简体中文",
            translation_type=TranslationType.HUMAN,
            package_scope=PackageScope.EPISODE,
        ),
        download_handle=(
            OpenSubtitlesDownloadHandle(file_id=42)
            if source is SubtitleSource.OPENSUBTITLES
            else AssrtDownloadHandle(subtitle_id=42)
        ),
    )


async def test_opensubtitles_plan_and_page_normalization_preserve_query_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 计划保留媒体参数，自定义词只替换查询词并归一化下载定位。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )
    plan = source._plan(_context(), "Custom Show")

    assert [query.label for query in plan.queries] == ["Custom Show"]
    assert plan.queries[0].query == "Custom Show"
    assert len(plan.queries) == 1
    params = plan.queries[0].identity["params"]
    assert params == {
        "languages": "zh-cn",
        "type": "episode",
        "query": "Custom Show",
        "year": 2024,
        "season_number": 2,
    }

    monkeypatch.setattr(
        source,
        "_request_page",
        AsyncMock(
            return_value={
                "total_pages": 2,
                "data": [
                    [],
                    {"id": "without-file", "attributes": {"files": [{"file_id": 0}]}},
                    {
                        "id": "resource",
                        "attributes": {
                            "language": "zh-cn",
                            "files": [{"file_id": 8, "file_name": "candidate.srt"}],
                        },
                    },
                ],
            }
        ),
    )

    page = await source._fetch_page(plan.queries[0], 1)

    assert page.raw_count == 1
    assert page.download_locator_excluded_count == 1
    assert page.malformed_count == 1
    assert page.has_next is True
    assert len(page.candidates) == 1
    assert page.candidates[0].candidate.metadata["actual_query"] == "Custom Show"
    assert isinstance(page.candidates[0].download_handle, OpenSubtitlesDownloadHandle)


def test_opensubtitles_default_queries_expose_media_id_query_parameter() -> None:
    """OpenSubtitles 媒体 ID 默认条目的 query 为实际 ID 参数而非展示标签。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )

    entries = source.default_queries(_context())

    assert [(entry.kind, entry.label, entry.query) for entry in entries] == [
        ("id", "IMDb ID: tt0012345", "12345"),
        ("title", "English Title", "English Title"),
    ]


async def test_opensubtitles_retries_one_5xx_response_before_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 搜索遇到 5xx 时只重试一次。"""

    class _Response:
        """构造可关闭的 OpenSubtitles HTTP 响应替身。"""

        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            """保存状态和安全 JSON 响应。"""

            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict[str, Any]:
            """返回已构造的 JSON 响应。"""

            return self._payload

        async def aclose(self) -> None:
            """关闭无资源的测试响应。"""

            return

    class _Request:
        """按顺序提供一次 5xx 与一次成功响应。"""

        def __init__(self) -> None:
            """初始化调用计数。"""

            self.calls = 0

        async def get_res(self, _url: str, params: dict[str, Any]) -> _Response:
            """返回预期状态码的响应。"""

            assert params["page"] == 1
            self.calls += 1
            return _Response(503 if self.calls == 1 else 200, {"data": []})

    request = _Request()
    monkeypatch.setattr(opensubtitles_module, "AsyncRequestUtils", lambda **_kwargs: request)
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )

    assert await source._request_page({}, 1) == {"data": []}
    assert request.calls == 2


async def test_assrt_plan_and_page_normalization_preserve_title_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASSRT 计划保持标题回退并把无字幕 ID 结果排除在来源候选池外。"""

    source = AssrtSource(enabled=True, credentials={"token": "token"})
    plan = source._plan(_context(), None)

    assert [query.label for query in plan.queries] == ["测试主标题", "English Title"]
    assert [query.query for query in plan.queries] == ["测试主标题", "English Title"]
    assert plan.queries[0].identity.as_dict() == {
        "path": "sub/search",
        "params": {"q": "测试主标题", "cnt": 15, "pos": 0},
    }

    monkeypatch.setattr(
        source,
        "_request_json",
        AsyncMock(
            return_value={
                "status": 0,
                "sub": {
                    "subs": [
                        {"id": None, "native_name": "无定位", "lang": {"desc": "简体中文"}},
                        {"id": 42, "native_name": "候选字幕", "lang": {"desc": "简体中文"}},
                    ]
                },
            }
        ),
    )

    page = await source._fetch_page(plan.queries[0], 1)

    assert page.raw_count == 1
    assert page.download_locator_excluded_count == 1
    assert [item.candidate.candidate_key for item in page.candidates] == ["assrt:42:0"]
    assert isinstance(page.candidates[0].download_handle, AssrtDownloadHandle)


async def test_assrt_short_custom_keyword_skips_without_default_fallback() -> None:
    """不足四字符的自定义词不请求来源，也不回退到默认计划。"""

    source = AssrtSource(enabled=True, credentials={"token": "token"})

    result = await source.search(_context(), "abc")

    assert result.status == "success"
    assert result.skip_reason == "keyword_too_short"
    assert result.candidates == []


async def test_external_source_cache_payload_excludes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源候选缓存只保存查询结果，绝不写入长期凭据。"""

    cache = _FakeCache()
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "old-key", "username": "old-user", "password": "old-password"},
        cache=cache,
    )
    monkeypatch.setattr(source, "_request_page", AsyncMock(return_value={"total_pages": 1, "data": []}))

    await source.search(_context(), "Custom")

    assert len(cache.set_calls) == 1
    assert all(
        secret not in repr(item) for secret in ("old-key", "old-user", "old-password") for item in cache.set_calls
    )


async def test_opensubtitles_rate_limit_is_normalized_by_shared_query_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 原生限流在共享查询结果中保持 limited 状态并停止回退。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
        cache=_FakeCache(),
    )
    retry_at = datetime.now(UTC)
    monkeypatch.setattr(
        source,
        "_request_page",
        AsyncMock(side_effect=opensubtitles_module.SourceLimitedError("OpenSubtitles 暂时受限", retry_at=retry_at)),
    )

    run = await source.search(_context(), "Custom")

    assert run.status == "limited"
    assert run.error_summary == "OpenSubtitles 暂时受限"
    assert source.runtime_details()["limited_until"] == retry_at.isoformat()


async def test_assrt_rate_limit_is_normalized_by_shared_query_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASSRT 原生限流在共享查询结果中保持 limited 状态并停止回退。"""

    source = AssrtSource(
        enabled=True,
        credentials={"token": "token"},
        cache=_FakeCache(),
    )
    retry_at = datetime.now(UTC)
    monkeypatch.setattr(
        source,
        "_request_json",
        AsyncMock(side_effect=assrt_module.SourceLimitedError("ASSRT 分钟请求额度暂时受限", retry_at=retry_at)),
    )

    run = await source.search(_context(), "Custom")

    assert run.status == "limited"
    assert run.error_summary == "ASSRT 分钟请求额度暂时受限"
    assert source.runtime_details()["limited_until"] == retry_at.isoformat()


async def test_opensubtitles_uses_season_id_then_english_title_without_episode_or_moviehash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 电视剧只按整季 ID、英文标题回退且从不构造 MovieHash。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
        cache=_FakeCache(),
    )
    context = _context()
    query_plan = source._plan(context, None)
    expected_queries = [dict(query.identity["params"]) for query in query_plan.queries]
    candidate_payload = {
        "total_pages": 1,
        "data": [
            {
                "id": "resource",
                "attributes": {
                    "language": "zh-cn",
                    "release": "English release",
                    "files": [{"file_id": 8, "file_name": None}],
                },
            }
        ],
    }
    responses = iter([{"total_pages": 1, "data": []}, candidate_payload])
    request_page = AsyncMock(side_effect=lambda _params, _page: next(responses))
    monkeypatch.setattr(source, "_request_page", request_page)

    result = await source.search(context, None)

    assert len(expected_queries) == 2
    assert expected_queries[0]["parent_imdb_id"] == 12345
    assert "parent_tmdb_id" not in expected_queries[0]
    assert expected_queries[0]["season_number"] == 2
    assert "episode_number" not in expected_queries[0]
    assert expected_queries[1]["query"] == "English Title"
    assert expected_queries[1]["year"] == 2024
    assert expected_queries[1]["season_number"] == 2
    assert "episode_number" not in expected_queries[1]
    assert all(
        "moviehash" not in key.casefold() and "movie_hash" not in key.casefold()
        for query in expected_queries
        for key in query
    )
    assert [call.args[0] for call in request_page.await_args_list] == expected_queries
    assert len(result.candidates) == 1
    assert "format" not in result.candidates[0].candidate.model_dump()


async def test_opensubtitles_reads_all_pages_and_caches_complete_empty_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 串行读取全部页面并缓存正常空结果。"""

    cache = _FakeCache()
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
        cache=cache,
    )
    calls: list[int] = []

    async def request_page(_params: dict[str, Any], page: int) -> dict[str, Any]:
        calls.append(page)
        return {"total_pages": 3, "page": page, "data": []}

    monkeypatch.setattr(source, "_request_page", request_page)
    first = await source.search(_context(), "No Result")
    second = await source.search(_context(), None)

    assert calls == [1, 2, 3, 1, 2, 3, 1, 2, 3]
    assert first.cache_hit is False
    assert any(call[2] == 1800 for call in cache.set_calls)
    assert second.status == "success"


async def test_opensubtitles_partial_pagination_keeps_results_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后续页失败时保留已取得候选但不写入缓存。"""

    cache = _FakeCache()
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
        cache=cache,
    )

    async def request_page(_params: dict[str, Any], page: int) -> dict[str, Any]:
        if page == 2:
            raise opensubtitles_module.SourceRequestError("第二页失败")
        return {
            "total_pages": 3,
            "data": [
                {
                    "id": "resource",
                    "attributes": {
                        "language": "zh-cn",
                        "files": [{"file_id": 8, "file_name": None}],
                    },
                }
            ],
        }

    monkeypatch.setattr(source, "_request_page", request_page)
    result = await source.search(_context(), "English Title")

    assert len(result.candidates) == 1
    assert result.status == "partial"
    assert cache.set_calls == []


async def test_opensubtitles_default_manual_reuses_automatic_candidate_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles 自动与默认人工共享相同参数的候选池缓存。"""

    cache = _FakeCache()
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
        cache=cache,
    )
    request_page = AsyncMock(
        return_value={
            "total_pages": 1,
            "data": [
                {
                    "id": "resource",
                    "attributes": {
                        "language": "zh-cn",
                        "files": [{"file_id": 8, "file_name": "untrusted.ass"}],
                        "hearing_impaired": True,
                    },
                }
            ],
        }
    )
    monkeypatch.setattr(source, "_request_page", request_page)

    automatic = await source.search(_context(), None)
    manual = await source.search(_context(), None)

    assert request_page.await_count == 1
    assert "format" not in automatic.candidates[0].candidate.model_dump()
    assert "hearing_impaired" not in automatic.candidates[0].candidate.model_dump()
    assert manual.cache_hit is True


async def test_opensubtitles_download_requests_fresh_link_each_time_without_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次下载都重新请求临时链接，候选缓存不保存链接。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )
    handle = _handle(SubtitleSource.OPENSUBTITLES)
    source._download_link = AsyncMock(
        side_effect=[
            ("https://temporary.example/one", "one.srt"),
            ("https://temporary.example/two", "two.srt"),
        ]
    )
    download_file = AsyncMock(side_effect=[tmp_path / "one.srt", tmp_path / "two.srt"])
    monkeypatch.setattr(opensubtitles_module, "download_file", download_file)

    first = await source.download(handle, tmp_path)
    second = await source.download(handle, tmp_path)

    assert source._download_link.await_count == 2
    assert [call.args[1] for call in download_file.await_args_list] == [
        "https://temporary.example/one",
        "https://temporary.example/two",
    ]
    assert first.file_name == "one.srt"
    assert second.file_name == "two.srt"
    assert handle.download_handle == OpenSubtitlesDownloadHandle(file_id=42)


async def test_opensubtitles_download_authorization_retries_once_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下载授权遇到 401 时清除 JWT、强制重登一次并重试成功。"""

    class _Response:
        """构造可关闭的下载授权响应替身。"""

        def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
            """保存状态码与安全 JSON 响应。"""

            self.status_code = status_code
            self._payload = payload
            self.is_closed = False

        def json(self) -> dict[str, Any]:
            """返回 JSON 响应。"""

            return self._payload

        async def aclose(self) -> None:
            """标记响应已关闭。"""

            self.is_closed = True

    class _Request:
        """首次返回 401、重试返回临时链接。"""

        def __init__(self) -> None:
            """初始化调用计数。"""

            self.posts = 0

        async def post_res(self, _url: str, json: dict[str, Any]) -> _Response:
            """按调用次序返回 401 或成功响应。"""

            del json
            self.posts += 1
            if self.posts == 1:
                return _Response(401, {})
            return _Response(200, {"link": "https://download.example/x", "file_name": "x.srt"})

    request = _Request()
    monkeypatch.setattr(opensubtitles_module, "AsyncRequestUtils", lambda **_kwargs: request)
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )
    login = AsyncMock()
    monkeypatch.setattr(source, "_login", login)

    link, file_name = await source._download_link(7)

    assert (link, file_name) == ("https://download.example/x", "x.srt")
    assert request.posts == 2
    assert any(call.kwargs == {"force": True} for call in login.await_args_list)


async def test_opensubtitles_concurrent_login_coalesces_into_single_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发登录共享一把锁，只向 OpenSubtitles 发起一次登录请求。"""

    class _Response:
        """构造登录成功响应替身。"""

        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            """保存登录响应。"""

            self._payload = payload

        def json(self) -> dict[str, Any]:
            """返回登录 JSON。"""

            return self._payload

        async def aclose(self) -> None:
            """关闭无资源响应。"""

            return

    class _Request:
        """记录登录调用并阻塞首个请求以制造并发。"""

        def __init__(self) -> None:
            """初始化调用计数与并发闸门。"""

            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def post_res(self, _url: str, json: dict[str, Any]) -> _Response:
            """首次请求等待第二个登录进入后返回令牌。"""

            del json
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return _Response({"token": "jwt-token", "base_url": "api.opensubtitles.com"})

    request = _Request()
    monkeypatch.setattr(opensubtitles_module, "AsyncRequestUtils", lambda **_kwargs: request)
    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )

    first = asyncio.create_task(source._login())
    await request.started.wait()
    second = asyncio.create_task(source._login())
    await asyncio.sleep(0)
    request.release.set()
    tokens = [await first, await second]

    assert tokens == ["jwt-token", "jwt-token"]
    assert request.calls == 1


async def test_opensubtitles_cooldown_honors_retry_after_before_next_request() -> None:
    """OpenSubtitles 命中 Retry-After 后进入冷却并阻止后续请求。"""

    source = OpenSubtitlesSource(
        enabled=True,
        credentials={"api_key": "key", "username": "user", "password": "password"},
    )
    response = SimpleNamespace(headers={"Retry-After": "120"})

    until = source._mark_limited(response)

    assert until > datetime.now(UTC)
    assert source.runtime_details()["limited_until"] == until.isoformat()
    with pytest.raises(opensubtitles_module.SourceLimitedError) as exc_info:
        source._ensure_available()
    assert exc_info.value.retry_at == until


async def test_assrt_uses_at_most_two_title_queries_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASSRT 仅按中文标题及一个英文备选标题串行搜索。"""

    calls: list[str] = []
    responses = iter(
        [
            {"status": 0, "sub": {"subs": []}},
            {
                "status": 0,
                "sub": {
                    "subs": [
                        {
                            "id": 42,
                            "native_name": "候选字幕",
                            "lang": {"desc": "简体中文"},
                            "subtype": "srt",
                        }
                    ]
                },
            },
        ]
    )

    class _Response:
        """构造 ASSRT HTTP 响应替身。"""

        status_code = 200

        def __init__(self, payload: dict[str, Any]) -> None:
            """保存响应内容。"""

            self._payload = payload

        def json(self) -> dict[str, Any]:
            """返回 JSON 响应。"""

            return self._payload

        async def aclose(self) -> None:
            """关闭无资源的测试响应。"""

            return

    class _Request:
        """记录 ASSRT 实际查询词。"""

        async def get_res(self, _url: str, params: dict[str, Any]) -> _Response:
            """返回下一轮搜索响应。"""

            calls.append(params["q"])
            return _Response(next(responses))

    monkeypatch.setattr(assrt_module, "AsyncRequestUtils", lambda **_kwargs: _Request())
    source = AssrtSource(
        enabled=True,
        credentials={"token": "token"},
        cache=_FakeCache(),
    )
    expected_queries = ["测试主标题", "English Title"]

    result = await source.search(_context(), None)

    assert calls == expected_queries
    assert [item.candidate.candidate_key for item in result.candidates] == ["assrt:42:0"]


async def test_assrt_default_manual_search_reuses_automatic_empty_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASSRT 自动与默认人工对相同实际查询共享包含空结果的缓存。"""

    calls: list[str] = []

    async def request_json(_path: str, params: dict[str, Any], wait: bool) -> dict[str, Any]:
        assert wait is True
        calls.append(params["q"])
        return {"status": 0, "sub": {"subs": []}}

    source = AssrtSource(
        enabled=True,
        credentials={"token": "token"},
        cache=_FakeCache(),
        limiter=SlidingWindowLimiter(limit=20, window_seconds=60),
    )
    monkeypatch.setattr(source, "_request_json", request_json)

    automatic = await source.search(_context(), None)
    manual = await source.search(_context(), None)

    assert calls == ["测试主标题", "English Title"]
    assert automatic.candidates == []
    assert manual.candidates == []
    assert manual.cache_hit is True


@pytest.mark.parametrize("subtype", [None, "SubRip", "unknown"])
async def test_assrt_search_ignores_source_format(
    monkeypatch: pytest.MonkeyPatch,
    subtype: str | None,
) -> None:
    """ASSRT 搜索保留正常简中候选但不映射来源格式。"""

    source = AssrtSource(
        enabled=True,
        credentials={"token": "token"},
        cache=_FakeCache(),
    )
    monkeypatch.setattr(
        source,
        "_request_json",
        AsyncMock(
            return_value={
                "status": 0,
                "sub": {"subs": [{"id": 9, "native_name": "候选", "subtype": subtype, "lang": {"desc": "简体中文"}}]},
            }
        ),
    )

    result = await source.search(_context(), None)

    assert len(result.candidates) == 1
    assert "format" not in result.candidates[0].candidate.model_dump()


async def test_assrt_sliding_window_allows_twenty_requests_per_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享滑动窗口在六十秒内只发放二十次请求许可。"""

    clock = {"now": 100.0}
    monkeypatch.setattr(limiter_module.time, "monotonic", lambda: clock["now"])
    limiter = SlidingWindowLimiter(limit=20, window_seconds=60)

    assert [await limiter.acquire(wait=False) for _ in range(20)] == [None] * 20
    retry_at = await limiter.acquire(wait=False)
    assert isinstance(retry_at, datetime)
    assert await limiter.retry_at() is not None

    clock["now"] = 160.001
    assert await limiter.acquire(wait=False) is None


async def test_assrt_manual_refresh_reports_shared_limit_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手动状态刷新在共享额度耗尽时立即返回受限且不发 HTTP。"""

    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    await limiter.acquire(wait=False)
    source = AssrtSource(
        enabled=True,
        credentials={"token": "token"},
        limiter=limiter,
    )

    def fail_http(*_args: Any, **_kwargs: Any) -> None:
        """禁止限流分支构造真实 HTTP 客户端。"""

        raise AssertionError("受限时不应发起 HTTP")

    monkeypatch.setattr(assrt_module, "AsyncRequestUtils", fail_http)

    status = await source.refresh(manual=True)

    assert status.health is SourceHealth.LIMITED
    assert status.last_error_summary == "ASSRT 分钟请求额度暂时受限"
    assert status.details.get("limited_until")
