"""人工选择所需的短期搜索会话。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from app.sdk.cache import AsyncCache

from ..schemas.base import new_id
from ..schemas.search import SearchSelection, SearchSelectionError
from ..schemas.source import CandidateHandle
from ..schemas.target import SearchTarget

SEARCH_SESSION_REGION = "subtitleassistant_manual_search"
SEARCH_SESSION_TTL_SECONDS = 30 * 60


@dataclass(slots=True)
class SearchSession:
    """由宿主短期缓存保存的人工搜索会话。"""

    target: SearchTarget
    candidates: dict[str, CandidateHandle]


class SearchSessionCachePort(Protocol):
    """人工搜索会话使用的最小异步缓存端口。"""

    async def get(self, key: str, region: str | None = None) -> object:
        """读取一个未经信任的缓存值。"""

    async def set(
        self,
        key: str,
        value: object,
        ttl: int | None = None,
        region: str | None = None,
    ) -> None:
        """保存一个人工搜索会话。"""

    async def clear(self, region: str | None = None) -> None:
        """清除指定缓存区域。"""


class SearchSessions:
    """保存人工搜索结果并解析用户选择，不提交任务。"""

    def __init__(self, cache: SearchSessionCachePort | None = None) -> None:
        """绑定宿主短期缓存或注入的会话缓存。"""

        self._cache = cache or AsyncCache(cache_type="ttl", maxsize=256, ttl=SEARCH_SESSION_TTL_SECONDS)

    @staticmethod
    def _session_key(session_id: str) -> str:
        """构造独立区域内的结构化搜索会话键。"""

        return json.dumps({"session_id": session_id}, sort_keys=True, separators=(",", ":"))

    async def create(self, target: SearchTarget, handles: list[CandidateHandle]) -> str | None:
        """有候选时保存一份目标与候选映射，空搜索不创建会话。"""

        if not handles:
            return None
        candidates: dict[str, CandidateHandle] = {}
        for handle in handles:
            candidates.setdefault(handle.candidate.candidate_key, handle)
        session_id = new_id()
        await self._cache.set(
            self._session_key(session_id),
            SearchSession(target=target, candidates=candidates),
            ttl=SEARCH_SESSION_TTL_SECONDS,
            region=SEARCH_SESSION_REGION,
        )
        return session_id

    async def resolve(self, session_id: str, candidate_key: str) -> SearchSelection | SearchSelectionError:
        """校验会话与候选标识，返回所选候选及其目标。"""

        if not isinstance(candidate_key, str) or not candidate_key:
            return SearchSelectionError.CANDIDATE_NOT_FOUND
        session = await self._cache.get(self._session_key(session_id), region=SEARCH_SESSION_REGION)
        if not isinstance(session, SearchSession):
            return SearchSelectionError.SESSION_NOT_FOUND
        candidate = session.candidates.get(candidate_key)
        if candidate is None:
            return SearchSelectionError.CANDIDATE_NOT_FOUND
        return SearchSelection(target=session.target, candidate=candidate)

    async def clear_sessions(self) -> None:
        """按显式管理请求清除搜索会话缓存区域。"""

        await self._cache.clear(region=SEARCH_SESSION_REGION)
