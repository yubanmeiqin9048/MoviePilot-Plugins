"""字幕归属能力的组合实现。

该模块是文件归属的唯一业务实现边界。任务只提交通用文件请求并接收
``FileAttributionBatchResult``；归属只走规则路径（直传信任、信任包身份、
宿主识别），不再包含任何 AI 接管编排。
"""

from __future__ import annotations

from ..schemas.attribution import (
    FileAttributionBatchResult,
    FileAttributionRequest,
)
from .matching import MoviePilotMatcher


class AttributionService(MoviePilotMatcher):
    """统一提供候选识别与规则文件归属的单一实现。"""

    async def attribute_requests(
        self,
        requests: list[FileAttributionRequest],
    ) -> FileAttributionBatchResult:
        """逐文件执行规则归属，隔离单文件识别失败。"""

        result = FileAttributionBatchResult(request_count=len(requests), submitted_count=len(requests))
        for index, request in enumerate(requests, start=1):
            try:
                evidence = await self.attribute_file(
                    request.logical_source_path,
                    request.target,
                    request.candidate_snapshot,
                    request.strategy,
                )
            except Exception:  # noqa: BLE001 - 单文件归属失败必须隔离
                result.error_count += 1
                result.reason_summary["adapter_error"] = result.reason_summary.get("adapter_error", 0) + 1
                continue
            result.evidence_by_key[f"file_{index:04d}"] = evidence
        return result
