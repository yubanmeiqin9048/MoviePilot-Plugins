"""ASSRT 标题搜索字幕源。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.core.cache import AsyncMemoryBackend
from app.core.config import settings
from app.log import logger
from app.utils.http import AsyncRequestUtils

from ..application.ports import (
    CandidateHandle,
    DownloadedAsset,
    ManualSourceSearchResult,
    SourceSearchResult,
)
from ..domain.enums import PackageScope, SourceHealth, SubtitleSource, TranslationType
from ..domain.language import candidate_is_allowed, has_simplified_chinese
from ..domain.models import MediaContext, SourceStatus, SubtitleCandidate, elapsed_ms, utc_now
from ..domain.query import assrt_title_queries
from .common import (
    SourceLimitedError,
    SourceRequestError,
    _proxy_kwargs,
    cache_key,
    decode_candidate_pool,
    download_file,
    encode_candidate_pool,
    parse_datetime,
    safe_file_name,
    subtitle_format,
)
from .limiter import SlidingWindowLimiter

SEARCH_CACHE_TTL_SECONDS = 30 * 60
SEARCH_CACHE_REGION = "subtitleassistant_source_assrt"


class AssrtSource:
    """统一按标题执行最多两轮搜索的 ASSRT 来源。"""

    source = SubtitleSource.ASSRT
    BASE_URL = "https://api.assrt.net/v1"

    def __init__(
        self,
        enabled: bool,
        credentials: dict[str, str],
        allowed_formats: set[str],
        limiter: SlidingWindowLimiter | None = None,
        cache: AsyncMemoryBackend | None = None,
    ) -> None:
        """创建 ASSRT 来源适配器。"""

        self.enabled = enabled
        self._token = credentials.get("token", "").strip()
        self._allowed_formats = {item.upper().lstrip(".") for item in allowed_formats}
        self._limiter = limiter or SlidingWindowLimiter(limit=5, window_seconds=60)
        self._cache = cache or AsyncMemoryBackend(
            cache_type="ttl",
            maxsize=256,
            ttl=SEARCH_CACHE_TTL_SECONDS,
        )
        self._last_details: dict[str, Any] = {"attribution": "https://assrt.net"}
        self._credential_generation = 0

    @property
    def configured(self) -> bool:
        """判断 ASSRT Token 是否已配置。"""

        return bool(self._token)

    async def replace_credentials(self, credentials: dict[str, str]) -> None:
        """替换运行期 Token 并清除该来源候选缓存。"""

        self._token = credentials.get("token", "").strip()
        self._credential_generation += 1
        await self.clear_cache()

    def _headers(self) -> dict[str, str]:
        """构造使用 Bearer Token 的 ASSRT 请求头。"""

        return {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}

    def _queries(self, context: MediaContext) -> list[str]:
        """构造中文标题、英文标题最多两轮且不重复的查询。"""

        return [item for item in assrt_title_queries(context) if item is not None]

    async def _request_json(
        self,
        path: str,
        params: dict[str, Any],
        wait: bool,
    ) -> dict[str, Any]:
        """经过统一限流器请求并校验 ASSRT 顶层状态。"""

        retry_at = await self._limiter.acquire(wait=wait)
        if retry_at is not None:
            raise SourceLimitedError("ASSRT 分钟请求额度暂时受限", retry_at=retry_at)
        request = AsyncRequestUtils(headers=self._headers(), **_proxy_kwargs(settings.PROXY))
        response = await request.get_res(f"{self.BASE_URL}/{path}", params=params)
        try:
            if response is None:
                raise SourceRequestError("ASSRT 请求失败")
            if response.status_code == 429:
                limited_until = await self._limiter.mark_limited()
                raise SourceLimitedError("ASSRT 分钟请求额度暂时受限", retry_at=limited_until)
            if response.status_code >= 400:
                raise SourceRequestError(f"ASSRT 请求返回 HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise SourceRequestError("ASSRT 响应结构无效")
            status = int(payload.get("status", -1))
            if status == 30900:
                limited_until = await self._limiter.mark_limited()
                raise SourceLimitedError("ASSRT 分钟请求额度暂时受限", retry_at=limited_until)
            if status != 0:
                raise SourceRequestError(f"ASSRT 返回错误状态 {status}")
            self._last_details["last_request_at"] = utc_now().isoformat()
            return payload
        except ValueError as exc:
            raise SourceRequestError("ASSRT 响应无法解析") from exc
        finally:
            if response is not None:
                await response.aclose()

    def _normalize_pool(
        self,
        payload: dict[str, Any],
        query: str,
    ) -> tuple[list[CandidateHandle], int, dict[str, int]]:
        """归一化自动规则之前的 ASSRT 来源候选池。"""

        result: list[CandidateHandle] = []
        raw_count = 0
        rejected: dict[str, int] = {}
        subs = (payload.get("sub") or {}).get("subs") or []
        for item in subs:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            raw_count += 1
            language_data = item.get("lang") or {}
            marker = str(language_data.get("desc") or "")
            flags = language_data.get("langlist") if isinstance(language_data.get("langlist"), dict) else {}
            machine_vote = item.get("vote_machine_translate")
            translation = (
                TranslationType.MACHINE if machine_vote not in (None, False, 0, "0", "") else TranslationType.UNKNOWN
            )
            revision = int(item.get("revision") or 0)
            candidate = SubtitleCandidate(
                stable_key=f"assrt:{item['id']}:{revision}",
                source=self.source,
                name=str(item.get("native_name") or item.get("videoname") or item["id"]),
                file_name=None,
                format=subtitle_format(None, str(item.get("subtype") or "")) or "UNKNOWN",
                language=marker,
                translation_type=translation,
                package_scope=PackageScope.UNKNOWN,
                score=float(item.get("vote_score") or 0),
                uploaded_at=parse_datetime(item.get("upload_time"), "%Y-%m-%d %H:%M:%S"),
                revision=revision,
                metadata={
                    "videoname": str(item.get("videoname") or ""),
                    "native_name": str(item.get("native_name") or ""),
                    "description": str(item.get("videoname") or ""),
                    "actual_query": query,
                    "language_flags": flags,
                },
            )
            result.append(CandidateHandle(candidate=candidate, opaque={"id": int(item["id"])}))
        return result, raw_count, rejected

    async def _candidate_pool(
        self,
        query: str,
        query_type: str,
    ) -> tuple[list[CandidateHandle], int, dict[str, int], dict[str, Any]]:
        """读取或构建一轮自动与人工共享的 ASSRT 来源候选池。"""

        generation = self._credential_generation
        params = {"q": query, "cnt": 15, "pos": 0}
        key = cache_key(self.source.value, {"params": params})
        decoded = decode_candidate_pool(await self._cache.get(key, region=SEARCH_CACHE_REGION))
        if decoded is not None:
            handles, cached = decoded
            details = {
                "cache_hit": True,
                "cache_stored_at": cached.get("cache_stored_at"),
                "cache_ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
                "page_count": 1,
                "pagination_complete": True,
                "query_type": query_type,
                "query": query,
            }
            logger.debug(f"ASSRT 查询“{query}”复用了来源候选池缓存")
            return handles, int(cached["raw_count"]), dict(cached["rejection_summary"]), details
        payload = await self._request_json("sub/search", params, wait=True)
        handles, raw_count, rejected = self._normalize_pool(payload, query)
        value = encode_candidate_pool(handles, raw_count, rejected)
        if generation == self._credential_generation:
            await self._cache.set(
                key,
                value,
                ttl=SEARCH_CACHE_TTL_SECONDS,
                region=SEARCH_CACHE_REGION,
            )
        details = {
            "cache_hit": False,
            "cache_stored_at": value["stored_at"] if generation == self._credential_generation else None,
            "cache_ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
            "page_count": 1,
            "pagination_complete": True,
            "query_type": query_type,
            "query": query,
        }
        return handles, raw_count, rejected, details

    def _filter_automatic(
        self,
        handles: list[CandidateHandle],
        allow_machine: bool,
    ) -> tuple[list[CandidateHandle], dict[str, int]]:
        """在共享候选池之后应用自动简中与翻译类型规则。"""

        result: list[CandidateHandle] = []
        rejected: dict[str, int] = {}
        for handle in handles:
            candidate = handle.candidate
            flags = candidate.metadata.get("language_flags")
            if not has_simplified_chinese(
                self.source,
                candidate.language,
                flags if isinstance(flags, dict) else None,
            ):
                rejected["language"] = rejected.get("language", 0) + 1
                continue
            if not candidate_is_allowed(candidate, allow_machine):
                rejected["machine_translation"] = rejected.get("machine_translation", 0) + 1
                continue
            result.append(handle)
        return result, rejected

    async def search(self, context: MediaContext, allow_machine: bool) -> SourceSearchResult:
        """按主标题和不同备选标题串行搜索合格简中候选。"""

        started = time.monotonic()
        if not self.enabled or not self.configured:
            return SourceSearchResult(source=self.source)
        try:
            total_raw = 0
            rejection: dict[str, int] = {}
            last_details: dict[str, Any] = {}
            candidates: list[CandidateHandle] = []
            for index, query in enumerate(self._queries(context)):
                query_type = "keyword" if index == 0 else "english_title"
                pool, raw_count, pool_rejection, last_details = await self._candidate_pool(query, query_type)
                total_raw += raw_count
                for key, value in pool_rejection.items():
                    rejection[key] = rejection.get(key, 0) + int(value)
                candidates, automatic_rejection = self._filter_automatic(pool, allow_machine)
                for key, value in automatic_rejection.items():
                    rejection[key] = rejection.get(key, 0) + int(value)
                if pool:
                    break
            return SourceSearchResult(
                source=self.source,
                candidates=candidates,
                raw_count=total_raw,
                admitted_count=len(candidates),
                rejection_summary=rejection,
                duration_ms=int((time.monotonic() - started) * 1000),
                details=last_details,
            )
        except SourceLimitedError as exc:
            if exc.retry_at:
                self._last_details["limited_until"] = exc.retry_at.isoformat()
            return SourceSearchResult(
                source=self.source,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary=str(exc),
                limited=True,
            )
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全结果
            return SourceSearchResult(
                source=self.source,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary="ASSRT 搜索失败",
            )

    async def manual_search(
        self,
        context: MediaContext,
        custom_query: str | None = None,
    ) -> ManualSourceSearchResult:
        """按最多两轮标题回退搜索无自动准入过滤的人工候选。"""

        started = time.monotonic()
        defaults = self._queries(context)
        custom = (custom_query or "").strip()
        queries = [custom] if custom else defaults
        if not self.enabled:
            return ManualSourceSearchResult(source=self.source, status="disabled", default_queries=defaults)
        if not self.configured:
            return ManualSourceSearchResult(source=self.source, status="unconfigured", default_queries=defaults)
        executed: list[str] = []
        last_details: dict[str, Any] = {}
        total_raw = 0
        try:
            for index, query in enumerate(queries):
                executed.append(query)
                query_type = "custom" if custom else ("keyword" if index == 0 else "english_title")
                candidates, raw_count, _rejected, last_details = await self._candidate_pool(query, query_type)
                total_raw += raw_count
                if candidates:
                    duration = int((time.monotonic() - started) * 1000)
                    return ManualSourceSearchResult(
                        source=self.source,
                        status="success",
                        candidates=candidates,
                        default_queries=defaults,
                        executed_queries=executed,
                        matched_query=query,
                        duration_ms=duration,
                        raw_count=total_raw,
                        admitted_count=len(candidates),
                        details=last_details,
                    )
            return ManualSourceSearchResult(
                source=self.source,
                status="success",
                default_queries=defaults,
                executed_queries=executed,
                duration_ms=int((time.monotonic() - started) * 1000),
                raw_count=total_raw,
                admitted_count=0,
                details=last_details,
            )
        except SourceLimitedError as exc:
            return ManualSourceSearchResult(
                source=self.source,
                status="limited",
                default_queries=defaults,
                executed_queries=executed,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary=str(exc),
                details=last_details,
            )
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全结果
            duration = int((time.monotonic() - started) * 1000)
            return ManualSourceSearchResult(
                source=self.source,
                status="error",
                default_queries=defaults,
                executed_queries=executed,
                duration_ms=duration,
                error_summary="ASSRT 搜索失败",
                details=last_details,
            )

    async def _detail(self, subtitle_id: int) -> dict[str, Any]:
        """下载前请求最新字幕详情。"""

        payload = await self._request_json("sub/detail", {"id": subtitle_id}, wait=True)
        subs = (payload.get("sub") or {}).get("subs") or []
        detail = next(
            (item for item in subs if isinstance(item, dict) and int(item.get("id") or 0) == subtitle_id), None
        )
        if not detail:
            raise SourceRequestError("ASSRT 详情缺少目标字幕")
        return detail

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """请求最新详情并优先下载完整候选包。"""

        subtitle_id = int(handle.opaque["id"])
        detail = await self._detail(subtitle_id)
        url = detail.get("url")
        if not isinstance(url, str) or not url:
            raise SourceRequestError("ASSRT 详情缺少临时下载链接")
        file_name = safe_file_name(detail.get("filename"), f"assrt-{subtitle_id}.bin")
        path = await download_file(AsyncRequestUtils(**_proxy_kwargs(settings.PROXY)), url, directory, file_name)
        return DownloadedAsset(path=path, file_name=file_name)

    async def refresh(self, manual: bool = False) -> SourceStatus:
        """查询 ASSRT 配额并服从统一分钟限流。"""

        status = SourceStatus(source=self.source, enabled=self.enabled, configured=self.configured)
        status.details = dict(self._last_details)
        if not self.enabled or not self.configured:
            status.health = SourceHealth.DISABLED
            return status
        started = utc_now()
        status.last_checked_at = started
        try:
            payload = await self._request_json("user/quota", {}, wait=not manual)
            quota = (payload.get("user") or {}).get("quota")
            self._last_details.update({"quota": quota, "limited_until": None})
            status.health = SourceHealth.HEALTHY
            status.last_success_at = utc_now()
        except SourceLimitedError as exc:
            status.health = SourceHealth.LIMITED
            status.last_error_at = utc_now()
            status.last_error_summary = str(exc)
            if exc.retry_at:
                self._last_details["limited_until"] = exc.retry_at.isoformat()
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全状态
            status.health = SourceHealth.ERROR
            status.last_error_at = utc_now()
            status.last_error_summary = "ASSRT 配额检查失败"
        status.details = dict(self._last_details)
        status.last_duration_ms = elapsed_ms(started)
        return status

    async def close(self) -> None:
        """关闭 ASSRT 插件级缓存。"""

        await self._cache.close()

    async def clear_cache(self) -> None:
        """清除 ASSRT 插件级候选缓存。"""

        await self._cache.clear(region=SEARCH_CACHE_REGION)

    def runtime_details(self) -> dict[str, Any]:
        """返回 ASSRT 非敏感运行观测。"""

        return dict(self._last_details)
