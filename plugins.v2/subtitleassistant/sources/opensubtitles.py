"""OpenSubtitles.com REST API 字幕源。"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
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
from ..domain.enums import (
    MediaType,
    PackageScope,
    SourceHealth,
    SubtitleSource,
    TranslationType,
)
from ..domain.language import candidate_is_allowed, has_simplified_chinese
from ..domain.models import MediaContext, SourceStatus, SubtitleCandidate, elapsed_ms, utc_now
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

SEARCH_CACHE_TTL_SECONDS = 30 * 60
SEARCH_CACHE_REGION = "subtitleassistant_source_opensubtitles"


class OpenSubtitlesSource:
    """按媒体 ID、英文标题串行查询 OpenSubtitles 来源候选池。"""

    source = SubtitleSource.OPENSUBTITLES
    BASE_URL = "https://api.opensubtitles.com/api/v1"
    USER_AGENT = "MoviePilot SubtitleAssistant/0.1.0"

    def __init__(
        self,
        enabled: bool,
        credentials: dict[str, str],
        allowed_formats: set[str],
        cache: AsyncMemoryBackend | None = None,
    ) -> None:
        """创建 OpenSubtitles 来源适配器。"""

        self.enabled = enabled
        self._credentials = dict(credentials)
        self._allowed_formats = {item.upper().lstrip(".") for item in allowed_formats}
        self._cache = cache or AsyncMemoryBackend(
            cache_type="ttl",
            maxsize=256,
            ttl=SEARCH_CACHE_TTL_SECONDS,
        )
        self._jwt: str | None = None
        self._cooldown_until: datetime | None = None
        self._login_lock = asyncio.Lock()
        self._last_details: dict[str, Any] = {}
        self._credential_generation = 0

    @property
    def configured(self) -> bool:
        """判断下载所需长期凭据是否完整。"""

        return all(self._credentials.get(key, "").strip() for key in ("api_key", "username", "password"))

    async def replace_credentials(self, credentials: dict[str, str]) -> None:
        """替换运行期凭据、清除登录会话与该来源候选缓存。"""

        self._credentials = dict(credentials)
        self._jwt = None
        self._cooldown_until = None
        self._credential_generation += 1
        await self.clear_cache()

    def _headers(self, authenticated: bool = False) -> dict[str, str]:
        """构造不写入日志的 OpenSubtitles 请求头。"""

        headers = {
            "Accept": "application/json",
            "Api-Key": self._credentials.get("api_key", ""),
            "User-Agent": self.USER_AGENT,
            "Content-Type": "application/json",
        }
        if authenticated and self._jwt:
            headers["Authorization"] = f"Bearer {self._jwt}"
        return headers

    def _ensure_available(self) -> None:
        """在明确冷却期内跳过后续来源请求。"""

        if self._cooldown_until and self._cooldown_until > utc_now():
            raise SourceLimitedError("OpenSubtitles 暂时受限", retry_at=self._cooldown_until)
        self._cooldown_until = None

    def _mark_limited(self, response: Any) -> datetime:
        """根据 Retry-After 或默认六十秒进入冷却。"""

        retry_after = response.headers.get("Retry-After") if response is not None else None
        try:
            seconds = max(1, int(retry_after if retry_after is not None else 60))
        except (TypeError, ValueError):
            seconds = 60
        self._cooldown_until = datetime.now(UTC) + timedelta(seconds=seconds)
        self._last_details["limited_until"] = self._cooldown_until.isoformat()
        return self._cooldown_until

    @staticmethod
    def normalize_imdb_id(value: str | None) -> int | None:
        """把 IMDb ID 转换为无前缀和前导零的十进制整数。"""

        if not value:
            return None
        normalized = value.strip().lower()
        normalized = normalized.removeprefix("tt")
        if not normalized.isdigit():
            return None
        return int(normalized)

    @staticmethod
    def _base_params(context: MediaContext) -> dict[str, Any]:
        """构造自动与人工默认查询共享的稳定公共参数。"""

        return {
            "languages": "zh-cn",
            "type": "episode" if context.media_type is MediaType.TV else "movie",
        }

    def _query_plan(
        self,
        context: MediaContext,
        custom_query: str | None = None,
    ) -> tuple[list[str], list[tuple[str, str, dict[str, Any]]]]:
        """构造 ID 到英文标题的查询计划，自定义词只替换 query。"""

        common = self._base_params(context)
        defaults: list[str] = []
        plan: list[tuple[str, str, dict[str, Any]]] = []
        imdb_id = self.normalize_imdb_id(context.imdb_id)
        id_params = dict(common)
        id_label = ""
        if context.media_type is MediaType.TV:
            if imdb_id is not None:
                id_params["parent_imdb_id"] = imdb_id
                id_label = f"IMDb ID: {context.imdb_id}"
            elif context.tmdb_id is not None:
                id_params["parent_tmdb_id"] = context.tmdb_id
                id_label = f"TMDB ID: {context.tmdb_id}"
            if context.season is not None:
                id_params["season_number"] = context.season
        else:
            if imdb_id is not None:
                id_params["imdb_id"] = imdb_id
                id_label = f"IMDb ID: {context.imdb_id}"
            elif context.tmdb_id is not None:
                id_params["tmdb_id"] = context.tmdb_id
                id_label = f"TMDB ID: {context.tmdb_id}"
        if id_label:
            defaults.append(id_label)
            plan.append((id_label, "media_id", id_params))

        english_title = (context.english_title or "").strip()
        if english_title:
            title_params = dict(common)
            title_params["query"] = english_title
            if context.year is not None:
                title_params["year"] = context.year
            if context.media_type is MediaType.TV and context.season is not None:
                title_params["season_number"] = context.season
            defaults.append(english_title)
            plan.append((english_title, "english_title", title_params))

        custom = (custom_query or "").strip()
        if custom:
            custom_params = dict(common)
            custom_params["query"] = custom
            if context.year is not None:
                custom_params["year"] = context.year
            if context.media_type is MediaType.TV and context.season is not None:
                custom_params["season_number"] = context.season
            return defaults, [(custom, "custom", custom_params)]
        return defaults, plan

    def _queries(self, context: MediaContext, allow_machine: bool) -> list[dict[str, Any]]:
        """兼容调用方返回自动查询参数；机器翻译开关不进入远端请求。"""

        del allow_machine
        return [params for _label, _query_type, params in self._query_plan(context)[1]]

    def _manual_queries(
        self,
        context: MediaContext,
        custom_query: str | None,
    ) -> tuple[list[str], list[tuple[str, dict[str, Any]]]]:
        """兼容调用方返回人工默认展示方案与实际参数。"""

        defaults, plan = self._query_plan(context, custom_query)
        return defaults, [(label, params) for label, _query_type, params in plan]

    @staticmethod
    def _candidate_ids(attributes: dict[str, Any], context: MediaContext) -> tuple[int | None, str | None]:
        """从 feature_details 选择与媒体层级一致的 TMDB/IMDb ID。"""

        feature = attributes.get("feature_details") or {}
        if context.media_type is MediaType.TV:
            tmdb_id = feature.get("parent_tmdb_id") or feature.get("tmdb_id")
            imdb_id = feature.get("parent_imdb_id") or feature.get("imdb_id")
        else:
            tmdb_id = feature.get("tmdb_id")
            imdb_id = feature.get("imdb_id")
        try:
            parsed_tmdb = int(tmdb_id) if tmdb_id not in (None, "") else None
        except (TypeError, ValueError):
            parsed_tmdb = None
        imdb_digits = str(imdb_id or "")
        parsed_imdb = f"tt{int(imdb_digits):07d}" if imdb_digits.isdigit() else None
        return parsed_tmdb, parsed_imdb

    def _normalize_pool(
        self,
        payload: dict[str, Any],
        context: MediaContext,
        query: str,
    ) -> tuple[list[CandidateHandle], int, dict[str, int]]:
        """把单页响应归一为自动过滤前的来源候选池。"""

        result: list[CandidateHandle] = []
        raw_count = 0
        rejected: dict[str, int] = {}
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes") or {}
            translation = TranslationType.HUMAN
            if bool(attributes.get("ai_translated")):
                translation = TranslationType.AI
            elif bool(attributes.get("machine_translated")):
                translation = TranslationType.MACHINE
            tmdb_id, imdb_id = self._candidate_ids(attributes, context)
            feature = attributes.get("feature_details") or {}
            season = feature.get("season_number")
            episode = feature.get("episode_number")
            season_digits = str(season or "")
            episode_digits = str(episode or "")
            package_scope = PackageScope.EPISODE
            if context.media_type is MediaType.TV and season is not None and episode is None:
                package_scope = PackageScope.SEASON_PACK
            files = attributes.get("files") or []
            if not files:
                raw_count += 1
                rejected["download_locator"] = rejected.get("download_locator", 0) + 1
            for file_info in files:
                if not isinstance(file_info, dict):
                    continue
                raw_count += 1
                if file_info.get("file_id") is None:
                    rejected["download_locator"] = rejected.get("download_locator", 0) + 1
                    continue
                file_name = str(file_info.get("file_name") or "")
                candidate = SubtitleCandidate(
                    stable_key=f"opensubtitles:{item.get('id')}:{file_info.get('file_id')}",
                    source=self.source,
                    name=str(attributes.get("release") or file_name or item.get("id") or "OpenSubtitles"),
                    file_name=file_name or None,
                    format=subtitle_format(file_name) or "UNKNOWN",
                    language=str(attributes.get("language") or ""),
                    translation_type=translation,
                    hearing_impaired=bool(attributes.get("hearing_impaired")),
                    foreign_parts_only=bool(attributes.get("foreign_parts_only")),
                    package_scope=package_scope,
                    season=int(season_digits) if season_digits.isdigit() else None,
                    episode=int(episode_digits) if episode_digits.isdigit() else None,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                    trusted=bool(attributes.get("from_trusted")),
                    score=float(attributes.get("ratings") or 0),
                    votes=int(attributes.get("votes") or 0),
                    download_count=int(attributes.get("download_count") or 0),
                    uploaded_at=parse_datetime(attributes.get("upload_date")),
                    metadata={"release": attributes.get("release"), "actual_query": query},
                )
                result.append(CandidateHandle(candidate=candidate, opaque={"file_id": int(file_info["file_id"])}))
        return result, raw_count, rejected

    def _filter_automatic(
        self,
        handles: list[CandidateHandle],
        context: MediaContext,
        allow_machine: bool,
    ) -> tuple[list[CandidateHandle], dict[str, int]]:
        """在共享来源候选池之后应用自动语言与翻译类型规则。"""

        result: list[CandidateHandle] = []
        rejected: dict[str, int] = {}
        target_imdb = self.normalize_imdb_id(context.imdb_id)
        for handle in handles:
            candidate = handle.candidate.model_copy(deep=True)
            if not has_simplified_chinese(self.source, candidate.language):
                rejected["language"] = rejected.get("language", 0) + 1
                continue
            if not candidate_is_allowed(candidate, allow_machine):
                key = (
                    "machine_translation"
                    if candidate.translation_type
                    in {
                        TranslationType.AI,
                        TranslationType.MACHINE,
                    }
                    else "foreign_parts_only"
                )
                rejected[key] = rejected.get(key, 0) + 1
                continue
            candidate.exact_id_match = bool(
                (context.tmdb_id is not None and candidate.tmdb_id == context.tmdb_id)
                or (target_imdb is not None and self.normalize_imdb_id(candidate.imdb_id) == target_imdb)
            )
            result.append(CandidateHandle(candidate=candidate, opaque=handle.opaque))
        return result, rejected

    @staticmethod
    def _cache_details(
        query_type: str,
        query: str,
        cache_hit: bool,
        stored_at: str | None,
        page_count: int,
        pagination_complete: bool,
    ) -> dict[str, Any]:
        """构造前端和任务详情可消费的安全查询摘要。"""

        return {
            "cache_hit": cache_hit,
            "cache_stored_at": stored_at,
            "cache_ttl_seconds": SEARCH_CACHE_TTL_SECONDS,
            "page_count": page_count,
            "pagination_complete": pagination_complete,
            "query_type": query_type,
            "query": query,
        }

    async def _request_page(self, params: dict[str, Any], page: int) -> dict[str, Any]:
        """请求并校验一页 OpenSubtitles 搜索响应。"""

        self._ensure_available()
        request_params = {**params, "page": page}
        request = AsyncRequestUtils(headers=self._headers(), **_proxy_kwargs(settings.PROXY))
        response = await request.get_res(f"{self.BASE_URL}/subtitles", params=request_params)
        try:
            if response is None:
                raise SourceRequestError("OpenSubtitles 搜索请求失败")
            if response.status_code == 429:
                raise SourceLimitedError("OpenSubtitles 搜索暂时受限", retry_at=self._mark_limited(response))
            if response.status_code >= 400:
                raise SourceRequestError(f"OpenSubtitles 搜索返回 HTTP {response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict):
                raise SourceRequestError("OpenSubtitles 搜索响应结构无效")
            return payload
        except ValueError as exc:
            raise SourceRequestError("OpenSubtitles 搜索响应无法解析") from exc
        finally:
            if response is not None:
                await response.aclose()

    async def _candidate_pool(
        self,
        params: dict[str, Any],
        context: MediaContext,
        query: str,
        query_type: str,
    ) -> tuple[list[CandidateHandle], int, dict[str, int], dict[str, Any]]:
        """读取或完整构建一轮共享来源候选池。"""

        generation = self._credential_generation
        key = cache_key(self.source.value, {"params": params})
        decoded = decode_candidate_pool(await self._cache.get(key, region=SEARCH_CACHE_REGION))
        if decoded is not None:
            handles, cached = decoded
            details = self._cache_details(
                query_type,
                query,
                True,
                cached.get("cache_stored_at"),
                int(cached["page_count"]),
                bool(cached["pagination_complete"]),
            )
            logger.debug(f"OpenSubtitles 查询“{query}”复用了来源候选池缓存")
            return handles, int(cached["raw_count"]), dict(cached["rejection_summary"]), details

        first = await self._request_page(params, 1)
        pages: list[dict[str, Any]] = [first]
        try:
            total_pages = max(1, int(first.get("total_pages") or 1))
        except (TypeError, ValueError):
            total_pages = 1
        complete = True
        for page in range(2, total_pages + 1):
            try:
                pages.append(await self._request_page(params, page))
                logger.debug(f"OpenSubtitles 查询“{query}”已读取第 {page}/{total_pages} 页")
            except (SourceLimitedError, SourceRequestError):
                complete = False
                logger.warning(
                    f"OpenSubtitles 查询“{query}”读取第 {page}/{total_pages} 页失败，将保留已经取得的候选且不写入缓存"
                )
                break

        handles: list[CandidateHandle] = []
        raw_count = 0
        rejected: dict[str, int] = {}
        for payload in pages:
            page_handles, page_raw, page_rejected = self._normalize_pool(payload, context, query)
            handles.extend(page_handles)
            raw_count += page_raw
            for reason, count in page_rejected.items():
                rejected[reason] = rejected.get(reason, 0) + count
        stored_at: str | None = None
        if complete and generation == self._credential_generation:
            value = encode_candidate_pool(
                handles,
                raw_count,
                rejected,
                page_count=len(pages),
                pagination_complete=True,
            )
            stored_at = str(value["stored_at"])
            await self._cache.set(
                key,
                value,
                ttl=SEARCH_CACHE_TTL_SECONDS,
                region=SEARCH_CACHE_REGION,
            )
        details = self._cache_details(
            query_type,
            query,
            False,
            stored_at,
            len(pages),
            complete,
        )
        return handles, raw_count, rejected, details

    async def search(self, context: MediaContext, allow_machine: bool) -> SourceSearchResult:
        """按媒体 ID、英文标题顺序搜索并在候选池后应用自动规则。"""

        started = time.monotonic()
        if not self.enabled or not self.configured:
            return SourceSearchResult(source=self.source)
        try:
            total_raw = 0
            total_rejected: dict[str, int] = {}
            last_details: dict[str, Any] = {}
            candidates: list[CandidateHandle] = []
            for label, query_type, params in self._query_plan(context)[1]:
                pool, raw_count, pool_rejected, details = await self._candidate_pool(
                    params,
                    context,
                    label,
                    query_type,
                )
                total_raw += raw_count
                for reason, count in pool_rejected.items():
                    total_rejected[reason] = total_rejected.get(reason, 0) + count
                candidates, automatic_rejected = self._filter_automatic(pool, context, allow_machine)
                for reason, count in automatic_rejected.items():
                    total_rejected[reason] = total_rejected.get(reason, 0) + count
                last_details = {**details, "pool_count": len(pool)}
                if pool:
                    break
            self._last_details.update({**last_details, "last_search_at": utc_now().isoformat()})
            return SourceSearchResult(
                source=self.source,
                candidates=candidates,
                raw_count=total_raw,
                admitted_count=len(candidates),
                rejection_summary=total_rejected,
                duration_ms=int((time.monotonic() - started) * 1000),
                details=last_details,
            )
        except SourceLimitedError as exc:
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
                error_summary="OpenSubtitles 搜索失败",
            )

    async def manual_search(
        self,
        context: MediaContext,
        custom_query: str | None = None,
    ) -> ManualSourceSearchResult:
        """共享来源候选池并按 ID、英文标题或单一自定义词搜索。"""

        started = time.monotonic()
        defaults, plan = self._query_plan(context, custom_query)
        if not self.enabled:
            return ManualSourceSearchResult(source=self.source, status="disabled", default_queries=defaults)
        if not self.configured:
            return ManualSourceSearchResult(source=self.source, status="unconfigured", default_queries=defaults)
        executed: list[str] = []
        last_details: dict[str, Any] = {}
        total_raw = 0
        try:
            for label, query_type, params in plan:
                executed.append(label)
                candidates, raw_count, _rejected, last_details = await self._candidate_pool(
                    params,
                    context,
                    label,
                    query_type,
                )
                total_raw += raw_count
                if candidates:
                    return ManualSourceSearchResult(
                        source=self.source,
                        status="success",
                        candidates=candidates,
                        default_queries=defaults,
                        executed_queries=executed,
                        matched_query=label,
                        duration_ms=int((time.monotonic() - started) * 1000),
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
            return ManualSourceSearchResult(
                source=self.source,
                status="error",
                default_queries=defaults,
                executed_queries=executed,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary="OpenSubtitles 搜索失败",
                details=last_details,
            )

    async def _login(self, force: bool = False) -> str:
        """合并并发登录并把 JWT 仅保存在当前运行内存。"""

        if not self.configured:
            raise SourceRequestError("OpenSubtitles 凭据不完整")
        self._ensure_available()
        async with self._login_lock:
            if self._jwt and not force:
                return self._jwt
            request = AsyncRequestUtils(headers=self._headers(), **_proxy_kwargs(settings.PROXY))
            response = await request.post_res(
                f"{self.BASE_URL}/login",
                json={"username": self._credentials["username"], "password": self._credentials["password"]},
            )
            try:
                if response is None:
                    raise SourceRequestError("OpenSubtitles 登录请求失败")
                if response.status_code >= 400:
                    if response.status_code == 429:
                        raise SourceLimitedError("OpenSubtitles 登录暂时受限", retry_at=self._mark_limited(response))
                    raise SourceRequestError(f"OpenSubtitles 登录返回 HTTP {response.status_code}")
                payload = response.json()
                token = payload.get("token") if isinstance(payload, dict) else None
                if not isinstance(token, str) or not token:
                    raise SourceRequestError("OpenSubtitles 登录响应缺少会话")
                self._jwt = token
                return token
            except ValueError as exc:
                raise SourceRequestError("OpenSubtitles 登录响应无法解析") from exc
            finally:
                if response is not None:
                    await response.aclose()

    async def _download_link(self, file_id: int, retry_auth: bool = True) -> tuple[str, str]:
        """请求一次临时下载链接，JWT 失效时只重试一次。"""

        await self._login()
        request = AsyncRequestUtils(
            headers=self._headers(authenticated=True),
            **_proxy_kwargs(settings.PROXY),
        )
        response = await request.post_res(f"{self.BASE_URL}/download", json={"file_id": file_id})
        try:
            if response is None:
                raise SourceRequestError("OpenSubtitles 下载授权请求失败")
            if response.status_code in {401, 403} and retry_auth:
                self._jwt = None
                await response.aclose()
                await self._login(force=True)
                return await self._download_link(file_id, retry_auth=False)
            if response.status_code in {406, 429}:
                raise SourceLimitedError("OpenSubtitles 下载额度暂时受限", retry_at=self._mark_limited(response))
            if response.status_code >= 400:
                raise SourceRequestError(f"OpenSubtitles 下载授权返回 HTTP {response.status_code}")
            payload = response.json()
            link = payload.get("link") if isinstance(payload, dict) else None
            if not isinstance(link, str) or not link:
                raise SourceRequestError("OpenSubtitles 下载授权缺少临时链接")
            self._last_details.update(
                {
                    "last_download_at": utc_now().isoformat(),
                    "remaining": payload.get("remaining"),
                    "reset_at": payload.get("reset_time_utc"),
                }
            )
            return link, safe_file_name(payload.get("file_name"), f"{file_id}.srt")
        except ValueError as exc:
            raise SourceRequestError("OpenSubtitles 下载授权响应无法解析") from exc
        finally:
            if response is not None and not response.is_closed:
                await response.aclose()

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """获取最新临时链接并下载字幕文件。"""

        file_id = int(handle.opaque["file_id"])
        link, file_name = await self._download_link(file_id)
        path = await download_file(AsyncRequestUtils(**_proxy_kwargs(settings.PROXY)), link, directory, file_name)
        return DownloadedAsset(path=path, file_name=file_name)

    async def refresh(self, manual: bool = False) -> SourceStatus:
        """重新登录验证长期凭据，不调用下载接口。"""

        del manual
        status = SourceStatus(source=self.source, enabled=self.enabled, configured=self.configured)
        if not self.enabled or not self.configured:
            status.health = SourceHealth.DISABLED
            return status
        started = utc_now()
        status.last_checked_at = started
        try:
            await self._login(force=True)
            status.health = SourceHealth.HEALTHY
            status.last_success_at = utc_now()
            status.details = {**self._last_details, "session_active": bool(self._jwt)}
        except SourceLimitedError as exc:
            status.health = SourceHealth.LIMITED
            status.last_error_at = utc_now()
            status.last_error_summary = str(exc)
            status.details = {**self._last_details, "session_active": bool(self._jwt)}
        except Exception:  # noqa: BLE001 - 外部字幕源异常必须收敛为安全状态
            status.health = SourceHealth.ERROR
            status.last_error_at = utc_now()
            status.last_error_summary = "OpenSubtitles 登录验证失败"
            status.details = {**self._last_details, "session_active": False}
        status.last_duration_ms = elapsed_ms(started)
        return status

    async def close(self) -> None:
        """清除内存 JWT 并关闭插件级缓存。"""

        self._jwt = None
        await self._cache.close()

    async def clear_cache(self) -> None:
        """清除 OpenSubtitles 插件级候选缓存。"""

        await self._cache.clear(region=SEARCH_CACHE_REGION)

    def runtime_details(self) -> dict[str, Any]:
        """返回不含秘密的当前运行观测。"""

        return {**self._last_details, "session_active": bool(self._jwt)}
