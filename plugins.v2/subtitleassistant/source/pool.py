"""来源候选池、缓存、分页与限流协调实现。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.cache import AsyncCache

from ..schemas.candidate import SubtitleCandidate
from ..schemas.source import (
    CacheTrace,
    CacheTraceState,
    CandidateHandle,
    CandidatePoolQueryBatchResult,
    CandidatePoolStatus,
    OpaqueCandidateHandle,
    PaginationTrace,
    SourceCandidatePoolResult,
    SubtitleSource,
)
from ..schemas.target import SubtitleTarget

CANDIDATE_POOL_CACHE_REGION = "subtitleassistant_candidate_pool"
CANDIDATE_POOL_CACHE_VERSION = 1
MAX_PAGINATION_PAGES = 1000
SOURCE_CACHE_TTL_SECONDS: dict[SubtitleSource, int] = {
    SubtitleSource.MOVIEPILOT: 10 * 60,
    SubtitleSource.OPENSUBTITLES: 30 * 60,
    SubtitleSource.ASSRT: 30 * 60,
}


class CandidatePoolQueryError(RuntimeError):
    """表示来源候选池请求失败且错误文本可安全返回。"""


class CandidatePoolQueryLimitedError(CandidatePoolQueryError):
    """表示来源候选池请求被限流。"""


@dataclass(frozen=True, slots=True)
class SourceQuery:
    """来源适配器生成的一项有序查询计划。"""

    label: str
    identity: Mapping[str, Any] = field(default_factory=dict)
    query_type: str | None = None


@dataclass(frozen=True, slots=True)
class SourceQueryPlan:
    """来源适配器提供的默认查询摘要与有序执行计划。"""

    queries: Sequence[SourceQuery] = field(default_factory=tuple)
    default_queries: Sequence[str] = field(default_factory=tuple)
    configured: bool = True
    skip_reason: str | None = None


@dataclass(slots=True)
class CandidatePage:
    """来源适配器完成单页请求与归一化后返回的安全结果。"""

    candidates: list[CandidateHandle] = field(default_factory=list)
    raw_count: int = 0
    download_locator_excluded: int = 0
    has_next: bool = False


class CandidatePoolCachePort(Protocol):
    """共享来源候选池所需的最小异步缓存协议。"""

    async def get(self, key: str, region: str | None = None) -> Any:
        """读取一个缓存值。"""

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """写入一个带 TTL 的缓存值。"""


class SourceCandidatePoolAdapter(Protocol):
    """共享查询模块使用的可替换来源适配器协议。"""

    source: SubtitleSource
    enabled: bool

    @property
    def configured(self) -> bool:
        """返回来源是否具备执行查询所需的配置。"""

        ...

    @property
    def configuration_generation(self) -> int:
        """返回影响来源查询缓存身份的配置代次。"""

        ...

    def query_plan(
        self,
        context: SubtitleTarget,
        custom_query: str | None,
    ) -> SourceQueryPlan | Sequence[SourceQuery]:
        """根据媒体上下文和来源自定义关键词生成有序计划。"""

    async def fetch_page(self, query: SourceQuery, page_number: int) -> CandidatePage:
        """执行一页来源请求并返回归一化候选。"""

    def is_valid_download_locator(self, handle: CandidateHandle) -> bool:
        """判断候选是否具有来源可使用的内部下载定位。"""


class CandidatePoolQueryPort(Protocol):
    """自动与人工业务共同使用的来源候选池查询边界。"""

    async def query(
        self,
        context: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> CandidatePoolQueryBatchResult:
        """根据媒体上下文返回逐来源候选池运行结果。"""


@dataclass(slots=True)
class _PendingCache:
    """一个等待来源运行成功后写入的候选池缓存。"""

    key: str
    source: SubtitleSource
    generation: int
    candidates: list[CandidateHandle]
    raw_count: int
    excluded_count: int
    page_count: int
    query_identity: str
    trace: CacheTrace


class CandidatePoolQueryService:
    """并发调度来源并统一形成来源候选池。"""

    def __init__(
        self,
        adapters: Mapping[SubtitleSource, SourceCandidatePoolAdapter],
        cache: CandidatePoolCachePort | None = None,
    ) -> None:
        """创建共享候选池查询服务。"""

        self._adapters = dict(adapters)
        self._cache = cache if cache is not None else AsyncCache(cache_type="ttl", maxsize=512)

    async def query(
        self,
        context: SubtitleTarget,
        custom_queries: Mapping[SubtitleSource, str | None] | None = None,
    ) -> CandidatePoolQueryBatchResult:
        """并发查询全部已注册来源并返回统一运行结果。"""

        runs = await asyncio.gather(
            *(
                self._query_source(
                    adapter,
                    context,
                    self._normalize_custom_query((custom_queries or {}).get(source)),
                )
                for source, adapter in self._adapters.items()
            )
        )
        return CandidatePoolQueryBatchResult(
            sources={run.source: run for run in runs},
        )

    async def _query_source(
        self,
        adapter: SourceCandidatePoolAdapter,
        context: SubtitleTarget,
        custom_query: str | None,
    ) -> SourceCandidatePoolResult:
        """串行执行一个来源的查询计划。"""

        started = time.monotonic()
        source = adapter.source
        if not adapter.enabled:
            return self._result(source, "disabled", started)

        try:
            raw_plan = adapter.query_plan(context, custom_query)
            if isinstance(raw_plan, SourceQueryPlan):
                plan = list(raw_plan.queries)
                default_queries = [self._safe_query_label(item) for item in raw_plan.default_queries]
                configured = raw_plan.configured
            else:
                plan = list(raw_plan)
                default_queries = [self._safe_query_label(item.label) for item in plan]
                configured = bool(adapter.configured)
        except CandidatePoolQueryLimitedError as exc:
            return self._result(source, "limited", started, error_summary=self._safe_error(exc))
        except CandidatePoolQueryError as exc:
            return self._result(source, "error", started, error_summary=self._safe_error(exc))
        except Exception:  # noqa: BLE001 - 来源计划异常必须收敛为安全结果
            return self._result(source, "error", started, error_summary="字幕源查询计划生成失败")

        if not plan:
            plan_skip_reason = "query_unavailable"
            if isinstance(raw_plan, SourceQueryPlan):
                plan_skip_reason = raw_plan.skip_reason or plan_skip_reason
            return self._result(
                source,
                "success",
                started,
                default_queries=default_queries,
                skip_reason=plan_skip_reason,
            )

        if not configured:
            return self._result(source, "unconfigured", started, default_queries=default_queries)

        executed_queries: list[str] = []
        cache_trace: list[CacheTrace] = []
        pagination_trace: list[PaginationTrace] = []
        all_candidates: list[CandidateHandle] = []
        raw_count = 0
        excluded_count = 0
        pending_cache: list[_PendingCache] = []

        for query in plan:
            safe_label = self._safe_query_label(query.label)
            executed_queries.append(safe_label)
            generation = self._configuration_generation(adapter)
            key = self._cache_key(source, generation, query)
            cache_value = await self._read_cache(key)
            cached = self._decode_cache(cache_value, adapter, source, generation, query)
            cache_entry = CacheTrace(
                query=safe_label,
                state=(
                    CacheTraceState.HIT
                    if cached is not None
                    else (CacheTraceState.INVALID if cache_value is not None else CacheTraceState.MISS)
                ),
                hit=cached is not None,
                ttl_seconds=self._ttl(source),
            )
            cache_trace.append(cache_entry)

            if cached is not None:
                handles, cached_raw, cached_excluded, page_count, stored_at = cached
                all_candidates.extend(handles)
                raw_count += cached_raw
                excluded_count += cached_excluded
                cache_entry.stored_at = stored_at
                pagination_trace.append(
                    PaginationTrace(
                        query=safe_label,
                        pages_fetched=page_count,
                        complete=True,
                        cached=True,
                    )
                )
                if handles:
                    await self._flush_pending_cache(adapter, pending_cache)
                    return self._result(
                        source,
                        "success",
                        started,
                        candidates=all_candidates,
                        raw_count=raw_count,
                        excluded_count=excluded_count,
                        default_queries=default_queries,
                        executed_queries=executed_queries,
                        matched_query=safe_label,
                        cache_trace=cache_trace,
                        pagination_trace=pagination_trace,
                    )
                continue

            query_candidates: list[CandidateHandle] = []
            query_raw_count = 0
            query_excluded_count = 0
            page_number = 1
            pages_fetched = 0
            complete = False
            try:
                while True:
                    page = await adapter.fetch_page(query, page_number)
                    if not isinstance(page, CandidatePage):
                        raise CandidatePoolQueryError("来源分页结果结构无效")
                    pages_fetched += 1
                    safe_candidates, invalid_count = self._safe_candidates(adapter, source, page.candidates)
                    query_candidates.extend(safe_candidates)
                    query_raw_count += max(0, int(page.raw_count), len(page.candidates))
                    query_excluded_count += max(0, int(page.download_locator_excluded)) + invalid_count
                    if not page.has_next:
                        complete = True
                        break
                    if pages_fetched >= MAX_PAGINATION_PAGES:
                        raise CandidatePoolQueryError("来源分页超过安全上限")
                    page_number += 1
            except asyncio.CancelledError:
                raise
            except CandidatePoolQueryLimitedError as exc:
                pagination_trace.append(
                    PaginationTrace(
                        query=safe_label,
                        pages_fetched=pages_fetched,
                        complete=False,
                        failed_page=page_number,
                    )
                )
                if pages_fetched:
                    return self._result(
                        source,
                        "partial",
                        started,
                        candidates=all_candidates + query_candidates,
                        raw_count=raw_count + query_raw_count,
                        excluded_count=excluded_count + query_excluded_count,
                        default_queries=default_queries,
                        executed_queries=executed_queries,
                        cache_trace=cache_trace,
                        pagination_trace=pagination_trace,
                        error_summary=self._safe_error(exc),
                    )
                return self._result(
                    source,
                    "limited",
                    started,
                    candidates=all_candidates,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    default_queries=default_queries,
                    executed_queries=executed_queries,
                    cache_trace=cache_trace,
                    pagination_trace=pagination_trace,
                    error_summary=self._safe_error(exc),
                )
            except CandidatePoolQueryError as exc:
                pagination_trace.append(
                    PaginationTrace(
                        query=safe_label,
                        pages_fetched=pages_fetched,
                        complete=False,
                        failed_page=page_number,
                    )
                )
                if pages_fetched:
                    return self._result(
                        source,
                        "partial",
                        started,
                        candidates=all_candidates + query_candidates,
                        raw_count=raw_count + query_raw_count,
                        excluded_count=excluded_count + query_excluded_count,
                        default_queries=default_queries,
                        executed_queries=executed_queries,
                        cache_trace=cache_trace,
                        pagination_trace=pagination_trace,
                        error_summary=self._safe_error(exc),
                    )
                return self._result(
                    source,
                    "error",
                    started,
                    candidates=all_candidates,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    default_queries=default_queries,
                    executed_queries=executed_queries,
                    cache_trace=cache_trace,
                    pagination_trace=pagination_trace,
                    error_summary=self._safe_error(exc),
                )
            except Exception:  # noqa: BLE001 - 来源请求异常必须收敛为安全结果
                pagination_trace.append(
                    PaginationTrace(
                        query=safe_label,
                        pages_fetched=pages_fetched,
                        complete=False,
                        failed_page=page_number,
                    )
                )
                error_summary = "字幕源请求失败"
                if pages_fetched:
                    return self._result(
                        source,
                        "partial",
                        started,
                        candidates=all_candidates + query_candidates,
                        raw_count=raw_count + query_raw_count,
                        excluded_count=excluded_count + query_excluded_count,
                        default_queries=default_queries,
                        executed_queries=executed_queries,
                        cache_trace=cache_trace,
                        pagination_trace=pagination_trace,
                        error_summary=error_summary,
                    )
                return self._result(
                    source,
                    "error",
                    started,
                    candidates=all_candidates,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    default_queries=default_queries,
                    executed_queries=executed_queries,
                    cache_trace=cache_trace,
                    pagination_trace=pagination_trace,
                    error_summary=error_summary,
                )

            all_candidates.extend(query_candidates)
            raw_count += query_raw_count
            excluded_count += query_excluded_count
            pagination_trace.append(
                PaginationTrace(
                    query=safe_label,
                    pages_fetched=pages_fetched,
                    complete=complete,
                )
            )
            if complete and self._configuration_generation(adapter) == generation:
                pending_cache.append(
                    _PendingCache(
                        key=key,
                        source=source,
                        generation=generation,
                        candidates=query_candidates,
                        raw_count=query_raw_count,
                        excluded_count=query_excluded_count,
                        page_count=pages_fetched,
                        query_identity=self._query_identity(query),
                        trace=cache_entry,
                    )
                )

            if query_candidates:
                await self._flush_pending_cache(adapter, pending_cache)
                return self._result(
                    source,
                    "success",
                    started,
                    candidates=all_candidates,
                    raw_count=raw_count,
                    excluded_count=excluded_count,
                    default_queries=default_queries,
                    executed_queries=executed_queries,
                    matched_query=safe_label,
                    cache_trace=cache_trace,
                    pagination_trace=pagination_trace,
                )

        await self._flush_pending_cache(adapter, pending_cache)
        return self._result(
            source,
            "success",
            started,
            candidates=all_candidates,
            raw_count=raw_count,
            excluded_count=excluded_count,
            default_queries=default_queries,
            executed_queries=executed_queries,
            cache_trace=cache_trace,
            pagination_trace=pagination_trace,
        )

    async def close(self) -> None:
        """关闭共享候选池缓存，缓存后端没有关闭能力时保持成功。"""

        closer = getattr(self._cache, "close", None)
        if not callable(closer):
            return
        result = closer()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _normalize_custom_query(value: str | None) -> str | None:
        """清理空白自定义关键词，避免空词替换默认计划。"""

        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _safe_query_label(value: str) -> str:
        """把查询摘要限制为可安全记录的单行文本。"""

        return " ".join(str(value).split())[:256]

    @staticmethod
    def _safe_error(error: CandidatePoolQueryError) -> str:
        """把适配器约定的安全错误限制为单行文本。"""

        return " ".join(str(error).split())[:256] or "字幕源请求失败"

    @staticmethod
    def _configuration_generation(adapter: SourceCandidatePoolAdapter) -> int:
        """读取来源配置 generation，并为替身提供稳定整数边界。"""

        try:
            return int(adapter.configuration_generation)
        except (AttributeError, TypeError, ValueError):
            return 0

    @staticmethod
    def _ttl(source: SubtitleSource) -> int:
        """返回来源固定候选缓存 TTL。"""

        return SOURCE_CACHE_TTL_SECONDS.get(source, 10 * 60)

    @staticmethod
    def _cache_key(source: SubtitleSource, generation: int, query: SourceQuery) -> str:
        """根据来源、配置 generation 和结构化查询身份生成缓存键。"""

        payload = {
            "source": source.value,
            "configuration_generation": generation,
            "query": CandidatePoolQueryService._query_identity(query),
        }
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return f"{source.value}:{digest}"

    @staticmethod
    def _query_identity(query: SourceQuery) -> str:
        """把结构化远端请求身份编码为稳定比较值。"""

        return json.dumps(query.identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)

    async def _read_cache(self, key: str) -> Any:
        """读取缓存失败时按未命中继续远端查询。"""

        try:
            return await self._cache.get(key, region=CANDIDATE_POOL_CACHE_REGION)
        except Exception:  # noqa: BLE001 - 缓存异常不得改变来源查询结果
            return None

    async def _write_cache(
        self,
        key: str,
        source: SubtitleSource,
        generation: int,
        candidates: list[CandidateHandle],
        raw_count: int,
        excluded_count: int,
        page_count: int,
        query_identity: str,
    ) -> str | None:
        """编码并写入一次完整来源候选池，失败时保持业务成功。"""

        stored_at = datetime.now(UTC).isoformat()
        try:
            value = {
                "version": CANDIDATE_POOL_CACHE_VERSION,
                "source": source.value,
                "configuration_generation": generation,
                "query_identity": query_identity,
                "handles": [
                    {
                        "candidate": handle.candidate.model_dump(mode="json"),
                        "opaque": handle.opaque.token,
                    }
                    for handle in candidates
                ],
                "raw_count": max(0, raw_count),
                "download_locator_excluded_count": max(0, excluded_count),
                "page_count": max(1, page_count),
                "stored_at": stored_at,
            }
            await self._cache.set(
                key,
                value,
                ttl=self._ttl(source),
                region=CANDIDATE_POOL_CACHE_REGION,
            )
        except Exception:  # noqa: BLE001 - 缓存写入失败不得改变业务结果
            return None
        return stored_at

    async def _flush_pending_cache(
        self,
        adapter: SourceCandidatePoolAdapter,
        pending: list[_PendingCache],
    ) -> None:
        """仅在来源计划完整成功后写入已完成查询的缓存。"""

        for item in pending:
            if self._configuration_generation(adapter) != item.generation:
                continue
            stored_at = await self._write_cache(
                item.key,
                item.source,
                item.generation,
                item.candidates,
                item.raw_count,
                item.excluded_count,
                item.page_count,
                item.query_identity,
            )
            if stored_at is not None:
                item.trace.stored = True
                item.trace.stored_at = stored_at

    @staticmethod
    def _decode_cache(
        value: Any,
        adapter: SourceCandidatePoolAdapter,
        source: SubtitleSource,
        generation: int,
        query: SourceQuery,
    ) -> tuple[list[CandidateHandle], int, int, int, str | None] | None:
        """运行时校验并解码缓存值，畸形值按未命中处理。"""

        if not isinstance(value, dict):
            return None
        if value.get("version") != CANDIDATE_POOL_CACHE_VERSION:
            return None
        if (
            value.get("source") != source.value
            or value.get("configuration_generation") != generation
            or value.get("query_identity") != CandidatePoolQueryService._query_identity(query)
        ):
            return None
        encoded_handles = value.get("handles")
        if not isinstance(encoded_handles, list):
            return None
        handles: list[CandidateHandle] = []
        for item in encoded_handles:
            if not isinstance(item, dict) or "candidate" not in item or "opaque" not in item:
                return None
            opaque_value = item["opaque"]
            if isinstance(opaque_value, str):
                opaque_token = opaque_value
            elif isinstance(opaque_value, Mapping):
                try:
                    opaque_token = json.dumps(
                        dict(opaque_value),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                except (TypeError, ValueError):
                    return None
            else:
                return None
            try:
                handle = CandidateHandle(
                    candidate=SubtitleCandidate.model_validate_json(json.dumps(item["candidate"], ensure_ascii=False)),
                    opaque=OpaqueCandidateHandle(token=opaque_token),
                )
            except (TypeError, ValueError):
                return None
            if handle.candidate.source is not source or not CandidatePoolQueryService._valid_locator(adapter, handle):
                return None
            handles.append(handle)
        raw_count = CandidatePoolQueryService._cache_count(value.get("raw_count"))
        excluded_count = CandidatePoolQueryService._cache_count(value.get("download_locator_excluded_count"))
        page_count = CandidatePoolQueryService._cache_count(value.get("page_count"))
        if raw_count is None or excluded_count is None or page_count is None or page_count < 1:
            return None
        stored_at = value.get("stored_at")
        if stored_at is not None and not isinstance(stored_at, str):
            return None
        return handles, raw_count, excluded_count, page_count, stored_at

    @staticmethod
    def _cache_count(value: Any) -> int | None:
        """校验缓存中的非负计数。"""

        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    @staticmethod
    def _safe_candidates(
        adapter: SourceCandidatePoolAdapter,
        source: SubtitleSource,
        candidates: Sequence[CandidateHandle],
    ) -> tuple[list[CandidateHandle], int]:
        """只保留来源一致且具有内部下载定位的候选句柄。"""

        result: list[CandidateHandle] = []
        invalid_count = 0
        for handle in candidates:
            valid_locator = CandidatePoolQueryService._valid_locator(adapter, handle)
            if handle.candidate.source is not source or not valid_locator:
                invalid_count += 1
            else:
                result.append(handle)
        return result, invalid_count

    @staticmethod
    def _valid_locator(adapter: SourceCandidatePoolAdapter, handle: CandidateHandle) -> bool:
        """通过来源适配器确认不透明句柄仍可用于下载。"""

        try:
            return bool(adapter.is_valid_download_locator(handle))
        except Exception:  # noqa: BLE001 - 定位校验失败按无定位处理
            return False

    @staticmethod
    def _result(
        source: SubtitleSource,
        status: CandidatePoolStatus | str,
        started: float,
        *,
        candidates: list[CandidateHandle] | None = None,
        raw_count: int = 0,
        excluded_count: int = 0,
        default_queries: list[str] | None = None,
        executed_queries: list[str] | None = None,
        matched_query: str | None = None,
        cache_trace: list[CacheTrace] | None = None,
        pagination_trace: list[PaginationTrace] | None = None,
        error_summary: str | None = None,
        skip_reason: str | None = None,
    ) -> SourceCandidatePoolResult:
        """构造并冻结一项来源安全运行结果的可观察快照。"""

        safe_candidates = list(candidates or [])
        return SourceCandidatePoolResult(
            source=source,
            status=CandidatePoolStatus(status),
            candidates=safe_candidates,
            raw_count=max(raw_count, len(safe_candidates)),
            candidate_pool_count=len(safe_candidates),
            download_locator_excluded_count=max(0, excluded_count),
            default_queries=list(default_queries or []),
            executed_queries=list(executed_queries or []),
            matched_query=matched_query,
            cache_trace=list(cache_trace or []),
            pagination_trace=list(pagination_trace or []),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_summary=error_summary,
            skip_reason=skip_reason,
        )
