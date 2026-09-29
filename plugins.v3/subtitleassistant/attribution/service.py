"""字幕归属能力的组合实现。

该模块是文件归属的唯一业务实现边界。任务只提交通用文件请求并接收
``FileAttributionBatchResult``；归属只走规则路径（直传信任、信任包身份、
宿主识别），不再包含任何 AI 接管编排。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..schemas.attribution import (
    CandidateAttributionSnapshot,
    FileAttributionBatchResult,
    FileAttributionEvidence,
    FileAttributionRequest,
    PackageAttributionStrategy,
)
from ..schemas.candidate import SubtitleCandidate
from ..schemas.target import SubtitleTarget
from .matching import MoviePilotMatcher


class AttributionService(MoviePilotMatcher):
    """统一提供候选识别与规则文件归属的单一实现。"""

    async def attribute_requests(
        self,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        requests: list[FileAttributionRequest],
        strategy: PackageAttributionStrategy,
        *,
        evidence_by_key: Mapping[str, FileAttributionEvidence] | None = None,
    ) -> FileAttributionBatchResult:
        """执行规则归属；调用方已有稳定证据时只复用该事实。"""

        del candidate
        if evidence_by_key:
            return FileAttributionBatchResult(evidence_by_key=dict(evidence_by_key))
        result = FileAttributionBatchResult()
        for index, request in enumerate(requests, start=1):
            try:
                evidence = await self.attribute_file(
                    request.path,
                    request.logical_source_path,
                    context,
                    snapshot,
                    strategy,
                )
            except Exception:  # noqa: BLE001 - 单文件归属失败必须隔离
                result.error_count += 1
                result.reason_summary["adapter_error"] = result.reason_summary.get("adapter_error", 0) + 1
                continue
            result.evidence_by_key[f"file_{index:04d}"] = evidence
        return result
