"""MoviePilot 站点字幕源适配器。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from app.chain.search import SearchChain
from app.core.cache import AsyncMemoryBackend
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper

# SitesHelper 由 MoviePilot 的动态站点资源提供，基础源码树不包含该模块。
from app.helper.sites import SitesHelper  # ty: ignore[unresolved-import]
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import AsyncRequestUtils

from ..application.ports import (
    CandidateHandle,
    DownloadedAsset,
    ManualSourceSearchResult,
    SourceSearchResult,
)
from ..domain.enums import SourceHealth, SubtitleSource, TranslationType
from ..domain.language import candidate_is_allowed, has_simplified_chinese
from ..domain.models import MediaContext, SourceStatus, SubtitleCandidate, elapsed_ms, utc_now
from .common import (
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

SEARCH_CACHE_TTL_SECONDS = 10 * 60
SEARCH_CACHE_REGION = "subtitleassistant_source_moviepilot"


def _create_keyword_extractor() -> Any | None:
    """创建轻量 YAKE 英文单词提取器，依赖缺失时返回空。"""

    try:
        import yake  # type: ignore[import-not-found]
    except ImportError:
        return None
    return yake.KeywordExtractor(lan="en", n=1, top=12)


class MoviePilotSource:
    """只调用宿主标题搜索接口并在下载前重新读取站点配置。"""

    source = SubtitleSource.MOVIEPILOT

    def __init__(
        self,
        enabled: bool,
        allowed_formats: set[str],
        cache: AsyncMemoryBackend | None = None,
    ) -> None:
        """创建 MoviePilot 站点来源适配器。"""

        self.enabled = enabled
        self._allowed_formats = {item.upper().lstrip(".") for item in allowed_formats}
        self._cache = cache or AsyncMemoryBackend(
            cache_type="ttl",
            maxsize=256,
            ttl=SEARCH_CACHE_TTL_SECONDS,
        )
        self._last_details: dict[str, Any] = {}

    @property
    def configured(self) -> bool:
        """MoviePilot 站点源不需要插件外部凭据。"""

        return True

    @staticmethod
    def _field(item: Any, name: str, default: Any = None) -> Any:
        """兼容宿主字幕 dataclass 与测试替身的属性读取。"""

        return getattr(item, name, default)

    @staticmethod
    def _integer(value: Any) -> int | None:
        """把宿主可空数值字段安全转换为整数。"""

        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stable_key(item: Any) -> str:
        """构造不包含原始下载链接的稳定来源键。"""

        site = MoviePilotSource._field(item, "site")
        subtitle_id = MoviePilotSource._field(item, "subtitle_id")
        torrent_id = MoviePilotSource._field(item, "torrent_id")
        if subtitle_id not in (None, ""):
            return f"moviepilot:{site}:subtitle:{subtitle_id}"
        if torrent_id not in (None, ""):
            return f"moviepilot:{site}:torrent:{torrent_id}"
        payload = {
            "site": site,
            "title": MoviePilotSource._field(item, "title", ""),
            "file_name": MoviePilotSource._field(item, "file_name", ""),
            "enclosure": MoviePilotSource._field(item, "enclosure", ""),
        }
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")).hexdigest()
        return f"moviepilot:sha256:{digest}"

    def _normalize_pool(self, item: Any, query: str) -> CandidateHandle | None:
        """把宿主字幕对象转换为自动过滤前的来源候选池项。"""

        site_id = self._integer(self._field(item, "site"))
        enclosure = self._field(item, "enclosure")
        if site_id is None or not isinstance(enclosure, str) or not enclosure:
            return None
        file_name = self._field(item, "file_name")
        title = str(self._field(item, "title", "MoviePilot 字幕") or "MoviePilot 字幕")
        description = str(self._field(item, "description", "") or "")
        marker = f"{title} {description}".casefold()
        translation = TranslationType.UNKNOWN
        if "机器翻译" in marker or "machine translated" in marker:
            translation = TranslationType.MACHINE
        elif "ai翻译" in marker or "ai translated" in marker:
            translation = TranslationType.AI
        candidate = SubtitleCandidate(
            stable_key=self._stable_key(item),
            source=self.source,
            name=title,
            file_name=str(file_name or "") or None,
            format=subtitle_format(str(file_name or "")) or "UNKNOWN",
            language=str(self._field(item, "language", "") or ""),
            translation_type=translation,
            hearing_impaired=any(token in marker for token in ("sdh", "cc", "听障")),
            foreign_parts_only=any(token in marker for token in ("foreign_parts_only", "foreign parts only", "仅外语")),
            site_id=site_id,
            site_priority=self._integer(self._field(item, "site_order")),
            download_count=self._integer(self._field(item, "grabs")),
            uploaded_at=parse_datetime(self._field(item, "pubdate"), "%Y-%m-%d %H:%M:%S"),
            metadata={
                "description": description,
                "site_name": self._field(item, "site_name", ""),
                "actual_query": query,
            },
        )
        return CandidateHandle(
            candidate=candidate,
            opaque={
                "site_id": candidate.site_id,
                "enclosure": enclosure,
                "file_name": str(file_name or ""),
            },
        )

    def _filter_automatic(
        self,
        handles: list[CandidateHandle],
        allow_machine: bool,
    ) -> tuple[list[CandidateHandle], dict[str, int]]:
        """在共享来源候选池之后应用自动语言与翻译规则。"""

        result: list[CandidateHandle] = []
        rejected: dict[str, int] = {}
        for handle in handles:
            if not has_simplified_chinese(self.source, handle.candidate.language):
                rejected["language"] = rejected.get("language", 0) + 1
                continue
            if not candidate_is_allowed(handle.candidate, allow_machine):
                reason = (
                    "machine_translation"
                    if handle.candidate.translation_type in {TranslationType.MACHINE, TranslationType.AI}
                    else "foreign_parts_only"
                )
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            result.append(handle)
        return result, rejected

    @staticmethod
    def _default_queries(context: MediaContext) -> tuple[list[str], str | None]:
        """从英文标题提取最多三个不重复的英文单词。"""

        title = (context.english_title or "").strip()
        if not title:
            return [], "english_title_missing"
        extractor = _create_keyword_extractor()
        if extractor is None:
            return [], "yake_unavailable"
        try:
            ranked = extractor.extract_keywords(title)
        except Exception:  # noqa: BLE001 - 第三方关键词提取失败必须降级
            return [], "keyword_extraction_failed"
        queries: list[str] = []
        seen: set[str] = set()
        for keyword, _score in ranked:
            value = str(keyword).strip()
            normalized = value.casefold()
            if not re.fullmatch(r"[A-Za-z]+(?:['-][A-Za-z]+)*", value) or normalized in seen:
                continue
            seen.add(normalized)
            queries.append(value)
            if len(queries) == 3:
                break
        return queries, None if queries else "keyword_extraction_empty"

    async def _has_subtitle_sites(self) -> bool:
        """判断宿主当前是否存在启用且支持字幕搜索的站点。"""

        return bool(await self._subtitle_site_indexers())

    async def _subtitle_site_indexers(self) -> list[dict[str, Any]]:
        """返回当前启用且声明支持字幕搜索的宿主索引站点。"""

        enabled_sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
        result: list[dict[str, Any]] = []
        for indexer in await SitesHelper().async_get_indexers():
            if not indexer.get("subtitles"):
                continue
            if not enabled_sites or indexer.get("id") in enabled_sites:
                result.append(indexer)
        return result

    async def _candidate_pool(
        self,
        query: str,
        query_type: str,
    ) -> tuple[list[CandidateHandle], int, dict[str, int], dict[str, Any]]:
        """读取或构建一轮自动与人工共享的 MoviePilot 候选池。"""

        indexers = await self._subtitle_site_indexers()
        site_ids = sorted(int(indexer["id"]) for indexer in indexers if indexer.get("id") is not None)
        if not site_ids:
            raise SourceRequestError("MoviePilot 没有启用且支持字幕搜索的站点")
        key = cache_key(self.source.value, {"query": query, "site_ids": site_ids})
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
            logger.debug(f"MoviePilot 站点字幕源查询“{query}”复用了来源候选池缓存")
            return handles, int(cached["raw_count"]), dict(cached["rejection_summary"]), details
        logger.info(f"开始查询 MoviePilot 站点字幕源，查询词为“{query}”")
        items = await SearchChain().async_search_subtitles_by_title(
            title=query,
            sites=site_ids,
            cache_local=False,
        )
        raw_items = list(items or [])
        handles: list[CandidateHandle] = []
        rejected: dict[str, int] = {}
        for item in raw_items:
            handle = self._normalize_pool(item, query)
            if handle is None:
                rejected["download_locator"] = rejected.get("download_locator", 0) + 1
            else:
                handles.append(handle)
        logger.info(
            f"MoviePilot 站点字幕源查询“{query}”完成，共返回 {len(raw_items)} 个结果，"
            f"其中 {len(handles)} 个具备下载定位"
        )
        value = encode_candidate_pool(handles, len(raw_items), rejected)
        await self._cache.set(
            key,
            value,
            ttl=SEARCH_CACHE_TTL_SECONDS,
            region=SEARCH_CACHE_REGION,
        )
        details = {
            "cache_hit": False,
            "cache_stored_at": value["stored_at"],
            "cache_ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
            "page_count": 1,
            "pagination_complete": True,
            "query_type": query_type,
            "query": query,
        }
        return handles, len(raw_items), rejected, details

    async def search(self, context: MediaContext, allow_machine: bool) -> SourceSearchResult:
        """按 YAKE 英文关键词串行搜索，首轮有准入候选即停。"""

        started = time.monotonic()
        if not self.enabled:
            return SourceSearchResult(source=self.source)
        queries, skip_reason = self._default_queries(context)
        if not queries:
            return SourceSearchResult(
                source=self.source,
                rejection_summary={"query_unavailable": 1},
                skip_reason=skip_reason,
                details={"skip_reason": skip_reason},
            )
        if not await self._has_subtitle_sites():
            return SourceSearchResult(
                source=self.source,
                skip_reason="no_subtitle_sites",
                details={"skip_reason": "no_subtitle_sites"},
            )
        try:
            handles: list[CandidateHandle] = []
            site_counts: dict[str, int] = {}
            raw_count = 0
            rejection_summary: dict[str, int] = {}
            executed_queries: list[str] = []
            for query in queries:
                executed_queries.append(query)
                pool, pool_raw, pool_rejected, query_details = await self._candidate_pool(query, "keyword")
                raw_count += pool_raw
                for reason, count in pool_rejected.items():
                    rejection_summary[reason] = rejection_summary.get(reason, 0) + count
                handles, automatic_rejected = self._filter_automatic(pool, allow_machine)
                for reason, count in automatic_rejected.items():
                    rejection_summary[reason] = rejection_summary.get(reason, 0) + count
                for handle in handles:
                    site_name = str(
                        handle.candidate.metadata.get("site_name") or handle.candidate.site_id or "未知站点"
                    )
                    site_counts[site_name] = site_counts.get(site_name, 0) + 1
                if pool:
                    break
            self._last_details = {
                "site_candidate_counts": site_counts,
                "candidate_total": len(handles),
                "last_search_at": utc_now().isoformat(),
                "default_queries": queries,
                "executed_queries": executed_queries,
                **query_details,
            }
            return SourceSearchResult(
                source=self.source,
                candidates=handles,
                duration_ms=int((time.monotonic() - started) * 1000),
                raw_count=raw_count,
                admitted_count=len(handles),
                rejection_summary=rejection_summary,
                details=self._last_details,
            )
        except SourceRequestError as exc:
            return SourceSearchResult(
                source=self.source,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary=str(exc),
            )
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全结果
            return SourceSearchResult(
                source=self.source,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary="MoviePilot 站点字幕搜索失败",
            )

    async def manual_search(
        self,
        context: MediaContext,
        custom_query: str | None = None,
    ) -> ManualSourceSearchResult:
        """按英文默认关键词或完整自定义词搜索人工候选。"""

        started = time.monotonic()
        default_queries, skip_reason = self._default_queries(context)
        if not self.enabled:
            return ManualSourceSearchResult(
                source=self.source,
                status="disabled",
                default_queries=default_queries,
            )
        if not await self._has_subtitle_sites():
            return ManualSourceSearchResult(
                source=self.source,
                status="unconfigured",
                default_queries=default_queries,
                error_summary="没有启用且支持字幕搜索的站点",
                skip_reason="no_subtitle_sites",
                details={"skip_reason": "no_subtitle_sites"},
            )
        custom = (custom_query or "").strip()
        queries = [custom] if custom else default_queries
        if not queries:
            return ManualSourceSearchResult(
                source=self.source,
                status="success",
                default_queries=default_queries,
                rejection_summary={"query_unavailable": 1},
                skip_reason=skip_reason,
            )
        try:
            handles: list[CandidateHandle] = []
            executed_queries: list[str] = []
            raw_count = 0
            matched_query: str | None = None
            last_details: dict[str, Any] = {}
            for query in queries:
                executed_queries.append(query)
                handles, pool_raw, _rejected, last_details = await self._candidate_pool(
                    query,
                    "custom" if custom else "keyword",
                )
                raw_count += pool_raw
                if handles:
                    matched_query = query
                    break
            duration = int((time.monotonic() - started) * 1000)
            return ManualSourceSearchResult(
                source=self.source,
                status="success",
                candidates=handles,
                default_queries=default_queries,
                executed_queries=executed_queries,
                matched_query=matched_query,
                duration_ms=duration,
                raw_count=raw_count,
                admitted_count=len(handles),
                details=last_details,
            )
        except Exception as exc:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全结果
            duration = int((time.monotonic() - started) * 1000)
            return ManualSourceSearchResult(
                source=self.source,
                status="error",
                default_queries=default_queries,
                executed_queries=executed_queries,
                duration_ms=duration,
                error_summary=str(exc) if isinstance(exc, SourceRequestError) else "MoviePilot 站点字幕搜索失败",
            )

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """按站点 ID 重新水合凭据后下载候选字幕。"""

        site_id = handle.opaque.get("site_id")
        enclosure = handle.opaque.get("enclosure")
        if site_id is None or not isinstance(enclosure, str) or not enclosure:
            raise SourceRequestError("MoviePilot 候选缺少站点或下载定位")
        site = await SiteOper().async_get(int(site_id))
        if not site or not bool(getattr(site, "is_active", False)):
            raise SourceRequestError("MoviePilot 字幕站点不存在或已停用")
        request_options: dict[str, Any] = {
            "cookies": getattr(site, "cookie", None),
            "ua": getattr(site, "ua", None) or settings.USER_AGENT,
            "timeout": getattr(site, "timeout", None) or 20,
            **_proxy_kwargs(settings.PROXY if bool(getattr(site, "proxy", False)) else None),
        }
        request = AsyncRequestUtils(**request_options)
        fallback_name = safe_file_name(
            handle.candidate.file_name,
            f"moviepilot-{handle.candidate.stable_key.rsplit(':', 1)[-1]}.bin",
        )
        path = await download_file(
            request,
            enclosure,
            directory,
            fallback_name,
            prefer_response_name=True,
        )
        return DownloadedAsset(path=path, file_name=path.name)

    async def refresh(self, manual: bool = False) -> SourceStatus:
        """重新读取宿主当前有效站点列表，不发起测试搜索。"""

        del manual
        status = SourceStatus(source=self.source, enabled=self.enabled, configured=True)
        if not self.enabled:
            status.health = SourceHealth.DISABLED
            return status
        started = utc_now()
        status.last_checked_at = started
        try:
            indexers = await self._subtitle_site_indexers()
            site_names = [
                str(indexer.get("name") or indexer.get("domain") or indexer.get("id")) for indexer in indexers
            ]
            self._last_details = {
                "site_names": site_names,
                "site_count": len(site_names),
                **{
                    key: value
                    for key, value in self._last_details.items()
                    if key in {"site_candidate_counts", "candidate_total", "last_search_at"}
                },
            }
            status.details = self._last_details
            if site_names:
                status.health = SourceHealth.HEALTHY
                status.last_success_at = utc_now()
            else:
                status.configured = False
                status.health = SourceHealth.DISABLED
                status.last_error_summary = "没有启用且支持字幕搜索的站点"
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全状态
            status.health = SourceHealth.ERROR
            status.last_error_at = utc_now()
            status.last_error_summary = "MoviePilot 站点状态读取失败"
        status.last_duration_ms = elapsed_ms(started)
        return status

    async def close(self) -> None:
        """释放来源运行态。"""

        await self._cache.close()

    async def clear_cache(self) -> None:
        """清除 MoviePilot 插件级来源候选池缓存。"""

        await self._cache.clear(region=SEARCH_CACHE_REGION)

    def runtime_details(self) -> dict[str, Any]:
        """返回不含站点凭据和下载链接的运行观测。"""

        return dict(self._last_details)
