"""MoviePilot 站点字幕源适配器。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.chain.search import SearchChain
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.db.systemconfig_oper import SystemConfigOper

# SitesHelper 由 MoviePilot 的动态站点资源提供，基础源码树不包含该模块。
from app.helper.sites import SitesHelper  # ty: ignore[unresolved-import]
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.http import AsyncRequestUtils

from ..schemas.base import elapsed_ms, utc_now
from ..schemas.candidate import SubtitleCandidate, TranslationType
from ..schemas.source import (
    CandidateHandle,
    DownloadedAsset,
    OpaqueCandidateHandle,
    SourceHealth,
    SourceStatus,
    SubtitleSource,
)
from ..schemas.target import SubtitleTarget
from .common import (
    SourceRequestError,
    _proxy_kwargs,
    download_file,
    parse_datetime,
    safe_file_name,
    subtitle_format,
)
from .pool import (
    CandidatePage,
    SourceQuery,
    SourceQueryPlan,
)


def _create_keyword_extractor() -> Any | None:
    """创建轻量 YAKE 英文单词提取器，依赖缺失时返回空。"""

    try:
        import yake  # type: ignore[import-not-found]
    except ImportError:
        return None
    return yake.KeywordExtractor(lan="en", n=1, top=12)


def _opaque_payload(handle: CandidateHandle) -> dict[str, Any] | None:
    """解码 MoviePilot 来源的内部下载句柄。"""

    if not isinstance(handle.opaque, OpaqueCandidateHandle):
        return None
    try:
        payload = json.loads(handle.opaque.token)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


class MoviePilotSource:
    """只调用宿主标题搜索接口并在下载前重新读取站点配置。"""

    source = SubtitleSource.MOVIEPILOT

    def __init__(
        self,
        enabled: bool,
        allowed_formats: set[str],
    ) -> None:
        """创建 MoviePilot 站点来源适配器。"""

        self.enabled = enabled
        self._allowed_formats = {item.upper().lstrip(".") for item in allowed_formats}
        self._last_details: dict[str, Any] = {}
        self._site_ids: tuple[int, ...] | None = None
        self._configuration_generation = 0

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
            opaque=OpaqueCandidateHandle(
                token=json.dumps(
                    {
                        "site_id": candidate.site_id,
                        "enclosure": enclosure,
                        "file_name": str(file_name or ""),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        )

    @staticmethod
    def _default_queries(context: SubtitleTarget) -> tuple[list[str], str | None]:
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

    def _remember_site_ids(self, site_ids: tuple[int, ...]) -> None:
        """记录宿主有效站点集合并在集合变化时推进配置代次。"""

        if self._site_ids is not None and site_ids != self._site_ids:
            self._configuration_generation += 1
        self._site_ids = site_ids

    async def _subtitle_site_indexers(self) -> list[dict[str, Any]]:
        """返回当前启用且声明支持字幕搜索的宿主索引站点。"""

        enabled_sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
        result: list[dict[str, Any]] = []
        for indexer in await SitesHelper().async_get_indexers():
            if not indexer.get("subtitles"):
                continue
            if not enabled_sites or indexer.get("id") in enabled_sites:
                result.append(indexer)
        site_ids = tuple(sorted(int(indexer["id"]) for indexer in result if indexer.get("id") is not None))
        self._remember_site_ids(site_ids)
        return result

    def _sync_subtitle_site_ids(self) -> tuple[int, ...]:
        """读取宿主本地站点快照，为查询计划预先固定缓存身份。"""

        get_indexers = getattr(SitesHelper(), "get_indexers", None)
        if not callable(get_indexers):
            return ()
        try:
            enabled_sites = SystemConfigOper().get(SystemConfigKey.IndexerSites) or []
            indexers = get_indexers()
            site_ids = [
                int(indexer["id"])
                for indexer in indexers
                if indexer.get("subtitles")
                and (not enabled_sites or indexer.get("id") in enabled_sites)
                and indexer.get("id") is not None
            ]
        except Exception:  # noqa: BLE001 - 本地站点快照不可用时交给异步查询确认
            return ()
        return tuple(sorted(site_ids))

    @property
    def configuration_generation(self) -> int:
        """返回当前宿主字幕站点配置代次。"""

        return self._configuration_generation

    def query_plan(
        self,
        context: SubtitleTarget,
        custom_query: str | None,
    ) -> SourceQueryPlan:
        """根据媒体上下文生成 MoviePilot 默认或自定义查询计划。"""

        site_ids = self._sync_subtitle_site_ids()
        self._remember_site_ids(site_ids)
        default_queries, skip_reason = self._default_queries(context)
        custom = (custom_query or "").strip()
        labels = [custom] if custom else default_queries
        query_type = "custom" if custom else "keyword"
        queries = [
            SourceQuery(
                label=label,
                query_type=query_type,
                identity={
                    "title": label,
                    "query_type": query_type,
                },
            )
            for label in labels
        ]
        return SourceQueryPlan(
            queries=queries,
            default_queries=default_queries,
            configured=bool(site_ids),
            skip_reason=skip_reason,
        )

    async def fetch_page(self, query: SourceQuery, page_number: int) -> CandidatePage:
        """执行一次 MoviePilot 原生标题查询并归一化为安全候选页。"""

        indexers = await self._subtitle_site_indexers()
        site_ids = sorted(int(indexer["id"]) for indexer in indexers if indexer.get("id") is not None)
        if not site_ids:
            return CandidatePage()
        logger.info(f"开始查询 MoviePilot 站点字幕源，查询词为“{query.label}”")
        items = await SearchChain().async_search_subtitles_by_title(
            title=query.label,
            page=max(0, page_number - 1),
            sites=site_ids,
            cache_local=False,
        )
        page = self._normalize_page(list(items or []), query.label)
        logger.info(
            f"MoviePilot 站点字幕源查询“{query.label}”完成，共返回 {page.raw_count} 个结果，"
            f"其中 {len(page.candidates)} 个具备下载定位"
        )
        return page

    def is_valid_download_locator(self, handle: CandidateHandle) -> bool:
        """判断候选句柄是否保留可供服务端下载的站点和定位。"""

        if handle.candidate.source is not self.source:
            return False
        payload = _opaque_payload(handle)
        if payload is None:
            return False
        site_id = payload.get("site_id")
        enclosure = payload.get("enclosure")
        try:
            valid_site = int(site_id) > 0 if isinstance(site_id, (str, int, float)) else False
        except (TypeError, ValueError):
            valid_site = False
        return valid_site and isinstance(enclosure, str) and bool(enclosure)

    def _normalize_page(self, items: list[Any], query: str) -> CandidatePage:
        """把一页宿主结果转换为共享候选池可接受的安全结果。"""

        handles: list[CandidateHandle] = []
        excluded = 0
        for item in items:
            handle = self._normalize_pool(item, query)
            if handle is None:
                excluded += 1
            else:
                handles.append(handle)
        return CandidatePage(
            candidates=handles,
            raw_count=len(items),
            download_locator_excluded=excluded,
        )

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """按站点 ID 重新水合凭据后下载候选字幕。"""

        payload = _opaque_payload(handle)
        if payload is None:
            raise SourceRequestError("MoviePilot 候选缺少有效下载句柄")
        site_id = payload.get("site_id")
        enclosure = payload.get("enclosure")
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
        """释放 MoviePilot 来源运行态。"""

        return

    def runtime_details(self) -> dict[str, Any]:
        """返回不含站点凭据和下载链接的运行观测。"""

        return dict(self._last_details)
