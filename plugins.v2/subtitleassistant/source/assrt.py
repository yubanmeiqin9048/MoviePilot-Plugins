"""ASSRT 标题搜索字幕源。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.utils.http import AsyncRequestUtils

from ..schemas.base import elapsed_ms, utc_now
from ..schemas.candidate import PackageScope, SubtitleCandidate, TranslationType
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
    SourceLimitedError,
    SourceRequestError,
    _proxy_kwargs,
    download_file,
    parse_datetime,
    safe_file_name,
    subtitle_format,
)
from .limiter import SlidingWindowLimiter
from .pool import (
    CandidatePage,
    CandidatePoolQueryError,
    CandidatePoolQueryLimitedError,
    SourceQuery,
    SourceQueryPlan,
)


class AssrtSource:
    """统一按标题执行最多两轮搜索的 ASSRT 来源。"""

    source = SubtitleSource.ASSRT
    BASE_URL = "https://api.assrt.net/v1"

    @staticmethod
    def _opaque_payload(handle: CandidateHandle) -> dict[str, Any] | None:
        """解码 ASSRT 来源的内部下载句柄。"""

        if not isinstance(handle.opaque, OpaqueCandidateHandle):
            return None
        try:
            payload = json.loads(handle.opaque.token)
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def __init__(
        self,
        enabled: bool,
        credentials: dict[str, str],
        allowed_formats: set[str],
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        """创建 ASSRT 来源适配器。"""

        self.enabled = enabled
        self._token = credentials.get("token", "").strip()
        self._allowed_formats = {item.upper().lstrip(".") for item in allowed_formats}
        self._limiter = limiter or SlidingWindowLimiter(limit=5, window_seconds=60)
        self._last_details: dict[str, Any] = {"attribution": "https://assrt.net"}
        self._credential_generation = 0

    @property
    def configured(self) -> bool:
        """判断 ASSRT Token 是否已配置。"""

        return bool(self._token)

    async def replace_credentials(self, credentials: dict[str, str]) -> None:
        """替换运行期 Token 并推进来源查询配置代次。"""

        self._token = credentials.get("token", "").strip()
        self._credential_generation += 1

    @property
    def configuration_generation(self) -> int:
        """返回影响来源候选缓存身份的 Token 配置代次。"""

        return self._credential_generation

    def _headers(self) -> dict[str, str]:
        """构造使用 Bearer Token 的 ASSRT 请求头。"""

        return {"Accept": "application/json", "Authorization": f"Bearer {self._token}"}

    def _default_queries(self, context: SubtitleTarget) -> list[str]:
        """构造中文标题、英文标题最多两轮且不重复的默认查询。"""

        primary = context.title.strip()
        alternate = next(
            (
                item.strip()
                for item in (context.english_title, context.original_title)
                if isinstance(item, str) and item.strip() and item.strip().casefold() != primary.casefold()
            ),
            "",
        )
        return [item for item in (primary, alternate) if len(item) >= 3]

    def query_plan(
        self,
        context: SubtitleTarget,
        custom_query: str | None,
    ) -> SourceQueryPlan:
        """根据标题回退或来源自定义关键词生成有序查询计划。"""

        defaults = self._default_queries(context)
        custom = (custom_query or "").strip()
        labels = [custom] if custom else defaults
        query_type = "custom" if custom else "keyword"
        queries = [
            SourceQuery(
                label=label,
                identity={"path": "sub/search", "params": {"q": label, "cnt": 15, "pos": 0}},
                query_type=query_type,
            )
            for label in labels
        ]
        return SourceQueryPlan(
            queries=queries,
            default_queries=defaults,
            configured=self.configured,
        )

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

    async def fetch_page(self, query: SourceQuery, page_number: int) -> CandidatePage:
        """执行一次 ASSRT 标题查询并归一化为安全候选页。"""

        if page_number != 1:
            raise SourceRequestError("ASSRT 不支持多页查询")
        path = query.identity.get("path")
        params = query.identity.get("params")
        if not isinstance(path, str) or not isinstance(params, dict):
            raise SourceRequestError("ASSRT 查询参数无效")
        try:
            payload = await self._request_json(path, dict(params), wait=True)
        except SourceLimitedError as exc:
            if exc.retry_at:
                self._last_details["limited_until"] = exc.retry_at.isoformat()
            raise CandidatePoolQueryLimitedError(str(exc)) from exc
        except SourceRequestError as exc:
            raise CandidatePoolQueryError(str(exc)) from exc
        handles, raw_count, rejected = self._normalize_pool(payload, query)
        return CandidatePage(
            candidates=handles,
            raw_count=raw_count,
            download_locator_excluded=rejected.get("download_locator", 0),
        )

    def is_valid_download_locator(self, handle: CandidateHandle) -> bool:
        """判断候选句柄是否包含有效的 ASSRT 字幕 ID。"""

        if handle.candidate.source is not self.source:
            return False
        payload = self._opaque_payload(handle)
        if payload is None:
            return False
        subtitle_id = payload.get("id")
        try:
            return int(subtitle_id) > 0 if isinstance(subtitle_id, (str, int, float)) else False
        except (TypeError, ValueError):
            return False

    def _normalize_pool(
        self,
        payload: dict[str, Any],
        query: SourceQuery,
    ) -> tuple[list[CandidateHandle], int, dict[str, int]]:
        """归一化自动规则之前的 ASSRT 来源候选池。"""

        result: list[CandidateHandle] = []
        raw_count = 0
        rejected: dict[str, int] = {}
        subs = (payload.get("sub") or {}).get("subs") or []
        for item in subs:
            if not isinstance(item, dict):
                continue
            raw_count += 1
            try:
                subtitle_id = int(item.get("id"))
            except (TypeError, ValueError):
                rejected["download_locator"] = rejected.get("download_locator", 0) + 1
                continue
            if subtitle_id <= 0:
                rejected["download_locator"] = rejected.get("download_locator", 0) + 1
                continue
            language_data = item.get("lang") or {}
            marker = str(language_data.get("desc") or "")
            flags = language_data.get("langlist") if isinstance(language_data.get("langlist"), dict) else {}
            machine_vote = item.get("vote_machine_translate")
            translation = (
                TranslationType.MACHINE if machine_vote not in (None, False, 0, "0", "") else TranslationType.UNKNOWN
            )
            try:
                revision = int(item.get("revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            candidate = SubtitleCandidate(
                stable_key=f"assrt:{subtitle_id}:{revision}",
                source=self.source,
                name=str(item.get("native_name") or item.get("videoname") or subtitle_id),
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
                    "actual_query": query.label,
                    "language_flags": flags,
                },
            )
            result.append(
                CandidateHandle(
                    candidate=candidate,
                    opaque=OpaqueCandidateHandle(
                        token=json.dumps({"id": subtitle_id}, separators=(",", ":"), sort_keys=True)
                    ),
                )
            )
        return result, raw_count, rejected

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

        payload = self._opaque_payload(handle)
        if payload is None or payload.get("id") is None:
            raise SourceRequestError("ASSRT 候选缺少有效下载句柄")
        subtitle_id = int(payload["id"])
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
        """释放 ASSRT 来源运行态。"""

        return

    def runtime_details(self) -> dict[str, Any]:
        """返回 ASSRT 非敏感运行观测。"""

        return dict(self._last_details)
