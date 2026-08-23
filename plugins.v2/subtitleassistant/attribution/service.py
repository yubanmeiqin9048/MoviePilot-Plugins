"""字幕归属能力的组合实现。

该模块是文件归属的唯一业务实现边界。任务只提交通用文件请求并接收
``FileAttributionBatchResult``，不会读取 AI adapter 的请求、响应或审计细节。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from pydantic import ValidationError

from app.log import logger

from ..schemas.attribution import (
    AiAttributionAudit,
    AiAttributionConfidence,
    AiAttributionEvidenceCode,
    AiAttributionOutcome,
    AiAttributionReasonCode,
    AiAttributionTriggerReason,
    AttributionEvidence,
    CandidateAttributionSnapshot,
    CandidateMatchContext,
    FileAttributionBatchResult,
    FileAttributionEvidence,
    FileAttributionMethod,
    FileAttributionRequest,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from ..schemas.candidate import SubtitleCandidate
from ..schemas.target import MediaType, SubtitleTarget
from .matching import MoviePilotMatcher

AI_ATTRIBUTION_TRIGGER_REASONS = frozenset(
    {
        "media_unrecognized",
        "identity_missing",
        "season_ambiguous",
        "episode_ambiguous",
    }
)
AI_ATTRIBUTION_EVIDENCE_CODES = frozenset(item.value for item in AiAttributionEvidenceCode)

BatchCallback = Callable[[dict[str, object]], Awaitable[None] | None]


class AttributionAdapterPort(Protocol):
    """文件归属 facade 使用的 AI 适配器最小端口。"""

    def authorized(self) -> bool:
        """返回当前是否允许执行接管。"""

    def should_takeover(
        self,
        evidence: FileAttributionEvidence,
        context: SubtitleTarget,
        snapshot: CandidateAttributionSnapshot,
    ) -> str | None:
        """根据常规证据返回候选接管触发原因。"""

    async def attribute_requests(
        self,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        requests: list[FileAttributionRequest],
        strategy: PackageAttributionStrategy,
        *,
        evidence_by_key: Mapping[str, FileAttributionEvidence],
        on_batch_start: BatchCallback | None = None,
        on_batch_end: BatchCallback | None = None,
    ) -> FileAttributionBatchResult:
        """返回 AI 适配器收敛后的文件归属批量结果。"""


class AttributionService(MoviePilotMatcher):
    """提供候选识别、常规文件归属与 AI 接管的统一 facade。"""

    def __init__(
        self,
        ai_adapter: AttributionAdapterPort | None = None,
        recognizer: object | None = None,
    ) -> None:
        """创建归属能力；宿主 matcher 只作为内部实现依赖。"""

        super().__init__()
        self.ai_adapter = ai_adapter
        self._recognizer = recognizer

    def normalize_candidate(
        self, candidate: SubtitleCandidate, context: SubtitleTarget, match_context: CandidateMatchContext | None
    ) -> SubtitleCandidate | None:
        """委托候选识别实现执行自动候选归一化。"""

        method = getattr(self._recognizer, "normalize_candidate", None)
        return method(candidate, context, match_context) if callable(method) else candidate

    def candidate_snapshot(self, candidate: SubtitleCandidate) -> CandidateAttributionSnapshot:
        """委托候选识别实现提取候选归属快照。"""

        method = getattr(self._recognizer, "candidate_snapshot", None)
        if callable(method):
            return method(candidate)
        return super().candidate_snapshot(candidate)

    async def attribute_requests(
        self,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        requests: list[FileAttributionRequest],
        strategy: PackageAttributionStrategy,
        *,
        evidence_by_key: Mapping[str, FileAttributionEvidence] | None = None,
        on_batch_start: BatchCallback | None = None,
        on_batch_end: BatchCallback | None = None,
    ) -> FileAttributionBatchResult:
        """完成常规归属并在内部决定是否补充接管，返回最终稳定证据。"""

        regular = await self._attribute_regular_requests(
            context,
            candidate,
            snapshot,
            requests,
            strategy,
            evidence_by_key=evidence_by_key,
        )
        takeover = await self._attribute_takeover(
            context,
            candidate,
            snapshot,
            requests,
            strategy,
            evidence_by_key=regular.evidence_by_key,
            on_batch_start=on_batch_start,
            on_batch_end=on_batch_end,
        )
        evidence = dict(regular.evidence_by_key)
        evidence.update(takeover.evidence_by_key)
        for key, audit in takeover.audits_by_key.items():
            original = evidence.get(key)
            if original is not None:
                evidence[key] = original.model_copy(update={"ai_takeover_audit": audit})
        reason_summary = dict(regular.reason_summary)
        for reason, count in takeover.reason_summary.items():
            reason_summary[reason] = reason_summary.get(reason, 0) + count
        return FileAttributionBatchResult(
            evidence_by_key=evidence,
            audits_by_key=takeover.audits_by_key,
            reason_summary=reason_summary,
            request_count=takeover.request_count,
            submitted_count=takeover.submitted_count,
            accepted_count=takeover.accepted_count,
            rejected_count=takeover.rejected_count,
            error_count=regular.error_count + takeover.error_count,
            over_limit_count=takeover.over_limit_count,
        )

    async def _attribute_regular_requests(
        self,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        requests: list[FileAttributionRequest],
        strategy: PackageAttributionStrategy,
        *,
        evidence_by_key: Mapping[str, FileAttributionEvidence] | None,
    ) -> FileAttributionBatchResult:
        """执行常规归属；测试或上游已有证据时只复用该稳定事实。"""

        if evidence_by_key:
            return FileAttributionBatchResult(evidence_by_key=dict(evidence_by_key))
        method = getattr(self._recognizer, "attribute_requests", None)
        if callable(method):
            return await method(context, candidate, snapshot, requests, strategy, evidence_by_key={})
        owner = self._recognizer if self._recognizer is not None else self
        method = getattr(owner, "attribute_file", None)
        if not callable(method):
            raise TypeError("文件归属能力未装配")
        result = FileAttributionBatchResult()
        for index, request in enumerate(requests, start=1):
            try:
                evidence = await method(
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
            if isinstance(evidence, FileAttributionEvidence):
                result.evidence_by_key[f"file_{index:04d}"] = evidence
            else:
                result.error_count += 1
                result.reason_summary["invalid_result"] = result.reason_summary.get("invalid_result", 0) + 1
        return result

    async def _attribute_takeover(
        self,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        snapshot: CandidateAttributionSnapshot,
        requests: list[FileAttributionRequest],
        strategy: PackageAttributionStrategy,
        *,
        evidence_by_key: Mapping[str, FileAttributionEvidence],
        on_batch_start: BatchCallback | None = None,
        on_batch_end: BatchCallback | None = None,
    ) -> FileAttributionBatchResult:
        """在归属内部执行一次接管，返回稳定的文件归属批量结果。"""

        adapter = self.ai_adapter
        authorized = getattr(adapter, "authorized", None)
        should = getattr(adapter, "should_takeover", None)
        if adapter is None or not callable(authorized) or not callable(should):
            return FileAttributionBatchResult()
        try:
            if not bool(authorized()):
                return FileAttributionBatchResult()
        except Exception:  # noqa: BLE001 - 授权读取失败必须失败关闭
            return FileAttributionBatchResult()

        selected: list[FileAttributionRequest] = []
        selected_evidence: dict[str, FileAttributionEvidence] = {}
        trigger_by_key: dict[str, str] = {}
        for index, request in enumerate(requests, start=1):
            key = f"file_{index:04d}"
            original = evidence_by_key.get(key)
            if original is None or self._ai_original_rejection_reason(original, context) is not None:
                continue
            canonical_trigger = self._canonical_ai_trigger(original, context, snapshot)
            if canonical_trigger is None:
                continue
            try:
                trigger = should(original, context, snapshot)
            except Exception:  # noqa: BLE001 - 不可信触发器失败关闭
                trigger = None
            if trigger != canonical_trigger or trigger not in AI_ATTRIBUTION_TRIGGER_REASONS:
                continue
            selected.append(request)
            selected_evidence[key] = original
            trigger_by_key[key] = canonical_trigger
        if not selected:
            return FileAttributionBatchResult()

        try:
            method = getattr(adapter, "attribute_requests", None) or getattr(adapter, "attribute_files", None)
            if not callable(method):
                return FileAttributionBatchResult()
            raw_result = await method(
                context,
                candidate,
                snapshot,
                selected,
                strategy,
                evidence_by_key=selected_evidence,
                on_batch_start=on_batch_start,
                on_batch_end=on_batch_end,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - AI 普通错误必须软失败
            logger.warning(f"字幕归属 AI 接管失败，将保留常规归属证据：{type(exc).__name__}")
            return FileAttributionBatchResult(
                request_count=len(selected),
                error_count=len(selected),
                reason_summary={"adapter_error": len(selected)},
            )

        normalized = self._normalize_ai_takeover_result(raw_result, local_keys=set(selected_evidence))
        if normalized is None:
            logger.warning("字幕归属 AI 接管返回非法结果，将保留常规归属证据")
            return FileAttributionBatchResult(
                request_count=len(selected),
                error_count=len(selected),
                reason_summary={"adapter_result_invalid": len(selected)},
            )
        result = FileAttributionBatchResult(
            request_count=max(normalized["request_count"], len(selected)),
            submitted_count=normalized["submitted_count"],
            rejected_count=normalized["rejected_count"],
            error_count=normalized["error_count"],
            over_limit_count=normalized["over_limit_count"],
            reason_summary=normalized["reason_summary"],
            audits_by_key={},
            evidence_by_key={},
        )
        try:
            adoption_authorized = bool(authorized())
        except Exception:  # noqa: BLE001 - 采用前授权失败关闭
            adoption_authorized = False
        proposed_by_key = normalized["evidence_by_key"]
        audits_by_key = normalized["audits_by_key"]
        for key, proposed in proposed_by_key.items():
            original = selected_evidence.get(key)
            if original is None:
                continue
            if adoption_authorized:
                validated, reason = self._validate_ai_takeover_evidence(
                    original=original,
                    proposed=proposed,
                    context=context,
                    snapshot=snapshot,
                    strategy=strategy,
                    expected_trigger=trigger_by_key.get(key),
                )
            else:
                validated, reason = None, "authorization_revoked_before_adoption"
            if validated is not None:
                result.evidence_by_key[key] = validated
                result.accepted_count += 1
                continue
            rejection_reason = reason or "application_validation_failed"
            result.reason_summary[rejection_reason] = result.reason_summary.get(rejection_reason, 0) + 1
            result.rejected_count += 1
            result.audits_by_key[key] = self._application_rejection_audit(
                audits_by_key.get(key), proposed, original, strategy, rejection_reason
            )
        for key, audit in audits_by_key.items():
            if key in result.evidence_by_key or key in result.audits_by_key:
                continue
            original = selected_evidence.get(key)
            if original is None:
                continue
            try:
                normalized_audit = AiAttributionAudit.model_validate(self._sanitize_audit_payload(audit))
            except (TypeError, ValueError, ValidationError):
                normalized_audit = self._application_rejection_audit(
                    None, None, original, strategy, "application_audit_invalid"
                )
                result.reason_summary["application_audit_invalid"] = (
                    result.reason_summary.get("application_audit_invalid", 0) + 1
                )
            if normalized_audit.outcome is AiAttributionOutcome.ACCEPTED:
                normalized_audit = self._application_rejection_audit(
                    normalized_audit, None, original, strategy, "application_evidence_missing"
                )
                result.reason_summary["application_evidence_missing"] = (
                    result.reason_summary.get("application_evidence_missing", 0) + 1
                )
            result.audits_by_key[key] = normalized_audit
            result.rejected_count += 1
        return result

    @staticmethod
    def _unique_scope_value(evidence: FileAttributionEvidence, field: str) -> tuple[int | None, int]:
        """读取保留基数的季集字段，兼容旧测试替身的单值证据。"""

        values = list(getattr(evidence, f"{field}_values", []) or [])
        scalar = getattr(evidence, field, None)
        if not values and scalar is not None:
            values = [scalar]
        return (values[0], 1) if len(values) == 1 else (None, len(values))

    @staticmethod
    def _normalized_imdb_id(value: str | None) -> str | None:
        """规范化 IMDb ID 用于应用层确定性比较。"""

        if not value:
            return None
        normalized = value.strip().lower().removeprefix("tt").lstrip("0")
        return normalized or "0"

    @staticmethod
    def _model_payload(value: Any) -> Any:
        """把 Pydantic 实例展开为字典，强制边界重新执行模型校验。"""

        dump = getattr(value, "model_dump", None)
        if callable(dump):
            return dump(mode="python")
        return value

    @staticmethod
    def _normalize_ai_takeover_result(
        value: FileAttributionBatchResult | object,
        *,
        local_keys: set[str],
    ) -> dict[str, Any] | None:
        """把适配器结果收敛为有界安全结构，非法结果整体拒绝。"""

        if isinstance(value, (Mapping, list, tuple, set, str, bytes, bytearray)):
            return None
        try:
            if not any(hasattr(value, field) for field in ("evidence_by_key", "audits_by_key", "reason_summary")):
                return None
        except Exception:  # noqa: BLE001 - 不可信适配器对象的属性探测必须失败关闭
            return None
        item_limit = max(1, len(local_keys))

        def normalize_mapping(field: str) -> dict[str, Any] | None:
            """读取并过滤一项有限大小的适配器映射。"""

            try:
                raw = getattr(value, field, {})
                if raw is None:
                    return {}
                if not isinstance(raw, Mapping) or len(raw) > item_limit * 2:
                    return None
                return {key: item for key, item in raw.items() if isinstance(key, str) and key in local_keys}
            except Exception:  # noqa: BLE001 - 不可信适配器映射读取必须失败关闭
                return None

        evidence_by_key = normalize_mapping("evidence_by_key")
        audits_by_key = normalize_mapping("audits_by_key")
        if evidence_by_key is None or audits_by_key is None:
            return None

        try:
            raw_reasons = getattr(value, "reason_summary", {})
            if raw_reasons is None:
                raw_reasons = {}
            if not isinstance(raw_reasons, Mapping) or len(raw_reasons) > 32:
                return None
            reason_summary: dict[str, int] = {}
            for key, count in raw_reasons.items():
                if (
                    not isinstance(key, str)
                    or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None
                    or isinstance(count, bool)
                    or not isinstance(count, int)
                    or count < 0
                    or count > item_limit
                ):
                    return None
                if count > 0:
                    reason_summary[key] = count

            counts: dict[str, int] = {}
            for field in (
                "request_count",
                "submitted_count",
                "accepted_count",
                "rejected_count",
                "error_count",
                "over_limit_count",
            ):
                count = getattr(value, field, 0)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > item_limit:
                    return None
                counts[field] = count
        except Exception:  # noqa: BLE001 - 不可信适配器统计读取必须失败关闭
            return None

        return {
            "evidence_by_key": evidence_by_key,
            "audits_by_key": audits_by_key,
            "reason_summary": reason_summary,
            **counts,
        }

    @staticmethod
    def _safe_audit_label(value: Any) -> str | None:
        """清洗 AI 审计中的 Provider/模型标签，拒绝控制字符和超长自由文本。"""

        if not isinstance(value, str):
            return None
        # Provider 与模型名只用于观测；isprintable 同时拒绝 C0/DEL 与
        # U+200B、U+202E 等不可见 Unicode 格式控制字符。
        if any(not char.isprintable() for char in value):
            return None
        cleaned = " ".join(value.split()).strip()
        if not cleaned or len(cleaned) > 128:
            return None
        return cleaned

    @classmethod
    def _sanitize_audit_payload(cls, value: Any) -> Any:
        """在重新验证审计模型前清洗动态 Provider/模型字段。"""

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return payload
        sanitized = dict(payload)
        sanitized["provider"] = cls._safe_audit_label(payload.get("provider"))
        sanitized["model"] = cls._safe_audit_label(payload.get("model"))
        return sanitized

    @classmethod
    def _sanitize_evidence_audit_payload(cls, value: Any) -> Any:
        """复制 AI 证据载荷并清洗其中的审计标签，不触碰其它字段。"""

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return payload
        audit = payload.get("ai_takeover_audit")
        if audit is None:
            return payload
        sanitized = dict(payload)
        sanitized["ai_takeover_audit"] = cls._sanitize_audit_payload(audit)
        return sanitized

    @classmethod
    def _raw_audit_evidence_reason(cls, value: Any) -> str | None:
        """在强类型模型校验前识别证据码篡改，保留稳定拒绝原因。

        ``model_copy(update=...)`` 不会重新运行 Pydantic 校验，因此测试替身或
        恶意适配器可能把未知、重复证据码嵌入一个看似合法的模型实例。若直接
        重新验证外层 ``FileAttributionEvidence``，这些情况都会被压缩成泛化的
        ``application_evidence_schema_invalid``，既不利于审计也无法区分契约
        违规类型。这里只读取证据码这一项，不信任或持久化其它原始字段；后续
        仍必须经过完整的 Pydantic 边界校验。
        """

        payload = cls._model_payload(value)
        if not isinstance(payload, dict):
            return None
        audit_payload = cls._model_payload(payload.get("ai_takeover_audit"))
        if not isinstance(audit_payload, dict):
            return None
        codes = audit_payload.get("evidence_codes")
        if not isinstance(codes, list):
            return None
        if any(not isinstance(code, str) or code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in codes):
            return "application_evidence_code_invalid"
        if len(set(codes)) != len(codes):
            return "application_evidence_code_duplicate"
        return None

    @staticmethod
    def _ai_original_rejection_reason(
        original: FileAttributionEvidence,
        context: SubtitleTarget,
    ) -> str | None:
        """返回不允许进入 AI 接管边界的原始文件证据原因。"""

        if original.method not in {
            FileAttributionMethod.TRUST_PACKAGE,
            FileAttributionMethod.HOST_RECOGNITION,
        }:
            return "application_original_method_invalid"
        if original.belongs_to_target_media is False:
            return "application_original_media_binding_invalid"
        if original.unmatched_reason in {
            UnmatchedReason.CANDIDATE_FILE_SCOPE_CONFLICT,
            UnmatchedReason.UNSUPPORTED_FORMAT,
        }:
            return "application_original_reason_invalid"
        if (
            original.media_type is not MediaType.UNKNOWN
            and context.media_type is not MediaType.UNKNOWN
            and original.media_type is not context.media_type
        ):
            return "application_original_media_type_invalid"
        return None

    @staticmethod
    def _canonical_ai_trigger(
        evidence: FileAttributionEvidence,
        context: SubtitleTarget,
        snapshot: CandidateAttributionSnapshot,
    ) -> str | None:
        """根据原始证据重新计算唯一允许的 AI 接管触发原因。"""

        del snapshot  # 候选范围只在建议校验阶段使用，触发判定不猜测范围。
        if evidence.method not in {
            FileAttributionMethod.TRUST_PACKAGE,
            FileAttributionMethod.HOST_RECOGNITION,
        }:
            return None
        if evidence.belongs_to_target_media is False:
            return None
        if (
            evidence.media_type is not MediaType.UNKNOWN
            and context.media_type is not MediaType.UNKNOWN
            and evidence.media_type is not context.media_type
        ):
            return None
        if evidence.unmatched_reason in {
            UnmatchedReason.CANDIDATE_FILE_SCOPE_CONFLICT,
            UnmatchedReason.UNSUPPORTED_FORMAT,
        }:
            return None
        if evidence.unmatched_reason is UnmatchedReason.MEDIA_UNRECOGNIZED:
            return "media_unrecognized"

        if context.canonical_identity is None:
            if context.media_type is MediaType.TV:
                season_value, season_count = AttributionService._unique_scope_value(
                    evidence,
                    "season",
                )
                episode_value, episode_count = AttributionService._unique_scope_value(
                    evidence,
                    "episode",
                )
                if (
                    season_count == 1
                    and episode_count == 1
                    and context.season is not None
                    and context.episode is not None
                    and (season_value != context.season or episode_value != context.episode)
                ):
                    return None
            return "identity_missing"
        if evidence.belongs_to_target_media is not True:
            return "media_unrecognized"
        if context.media_type is MediaType.TV:
            _season_value, season_count = AttributionService._unique_scope_value(
                evidence,
                "season",
            )
            _episode_value, episode_count = AttributionService._unique_scope_value(
                evidence,
                "episode",
            )
            if season_count != 1:
                return "season_ambiguous"
            if episode_count != 1:
                return "episode_ambiguous"
        return None

    @classmethod
    def _validate_ai_takeover_evidence(
        cls,
        *,
        original: FileAttributionEvidence,
        proposed: Any,
        context: SubtitleTarget,
        snapshot: CandidateAttributionSnapshot,
        strategy: PackageAttributionStrategy,
        expected_trigger: str | None = None,
    ) -> tuple[FileAttributionEvidence | None, str | None]:
        """在应用边界复核 AI 证据，防止适配器绕过既定媒体与来源约束。"""

        # AI 只能接管常规的候选包/宿主识别结果。直接字幕文件已经由用户
        # 选择的目标确定归属，已有 AI 结果也不得再次被包装成新的接管；明确
        # 属于其它媒体、候选范围冲突或格式不支持的结果同样不能由模型覆盖。
        original_rejection = cls._ai_original_rejection_reason(original, context)
        if original_rejection is not None:
            return None, original_rejection

        raw_evidence_code_reason = cls._raw_audit_evidence_reason(proposed)
        if raw_evidence_code_reason is not None:
            return None, raw_evidence_code_reason
        try:
            evidence = FileAttributionEvidence.model_validate(cls._sanitize_evidence_audit_payload(proposed))
        except (TypeError, ValueError, ValidationError):
            return None, "application_evidence_schema_invalid"
        if evidence.method is not FileAttributionMethod.AI_TAKEOVER:
            return None, "application_method_invalid"
        if evidence.logical_source_path != original.logical_source_path:
            return None, "application_logical_path_changed"
        if evidence.belongs_to_target_media is not True or evidence.unmatched_reason is not None:
            return None, "application_media_binding_invalid"
        if context.media_type is MediaType.UNKNOWN or evidence.media_type is not context.media_type:
            return None, "application_media_type_invalid"
        if evidence.year != original.year:
            return None, "application_year_changed"
        if context.tmdb_id is None:
            if evidence.tmdb_id is not None:
                return None, "application_tmdb_identity_invalid"
        elif evidence.tmdb_id != context.tmdb_id:
            return None, "application_tmdb_identity_invalid"
        if context.imdb_id is None:
            if evidence.imdb_id is not None:
                return None, "application_imdb_identity_invalid"
        elif cls._normalized_imdb_id(evidence.imdb_id) != cls._normalized_imdb_id(context.imdb_id):
            return None, "application_imdb_identity_invalid"

        original_season, original_season_count = cls._unique_scope_value(original, "season")
        original_episode, original_episode_count = cls._unique_scope_value(original, "episode")
        if context.media_type is MediaType.MOVIE:
            if (
                evidence.season is not None
                or evidence.episode is not None
                or evidence.season_values
                or evidence.episode_values
            ):
                return None, "application_movie_scope_invalid"
        else:
            if evidence.season is None or evidence.episode is None:
                return None, "application_tv_scope_incomplete"
            if evidence.season_values != [evidence.season] or evidence.episode_values != [evidence.episode]:
                return None, "application_scope_not_unique"
            if original_season_count == 1 and evidence.season != original_season:
                return None, "application_locked_season_changed"
            if original_episode_count == 1 and evidence.episode != original_episode:
                return None, "application_locked_episode_changed"
            if snapshot.seasons and evidence.season not in snapshot.seasons:
                return None, "application_season_out_of_candidate_scope"
            if snapshot.episodes and evidence.episode not in snapshot.episodes:
                return None, "application_episode_out_of_candidate_scope"
            expected_season_evidence = (
                original.season_evidence if original_season_count == 1 else AttributionEvidence.AI_TAKEOVER
            )
            expected_episode_evidence = (
                original.episode_evidence if original_episode_count == 1 else AttributionEvidence.AI_TAKEOVER
            )
            if evidence.season_evidence is not expected_season_evidence:
                return None, "application_season_provenance_invalid"
            if evidence.episode_evidence is not expected_episode_evidence:
                return None, "application_episode_provenance_invalid"

        if evidence.ai_before_method is not original.method:
            return None, "application_before_method_invalid"
        if evidence.ai_before_unmatched_reason is not original.unmatched_reason:
            return None, "application_before_reason_invalid"
        if evidence.host_recognition_summary != original.host_recognition_summary:
            return None, "application_host_summary_changed"
        raw_audit = evidence.ai_takeover_audit
        if raw_audit is None:
            return None, "application_audit_invalid"
        # 在强类型重验前保留稳定的应用层原因码；否则领域模型会把未知或
        # 重复证据码统一归类为 schema_invalid，丢失对调用方有用的拒绝原因。
        raw_audit_payload = cls._sanitize_audit_payload(raw_audit)
        if isinstance(raw_audit_payload, dict):
            raw_codes = raw_audit_payload.get("evidence_codes")
            if isinstance(raw_codes, list):
                if any(not isinstance(code, str) for code in raw_codes):
                    return None, "application_evidence_code_invalid"
                if len(set(raw_codes)) != len(raw_codes):
                    return None, "application_evidence_code_duplicate"
                if any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in raw_codes):
                    return None, "application_evidence_code_invalid"
        try:
            audit = AiAttributionAudit.model_validate(raw_audit_payload)
        except (TypeError, ValueError, ValidationError):
            return None, "application_audit_invalid"
        evidence = evidence.model_copy(update={"ai_takeover_audit": audit})
        if audit.outcome is not AiAttributionOutcome.ACCEPTED:
            return None, "application_audit_invalid"
        if expected_trigger is None or audit.trigger_reason != expected_trigger:
            return None, "application_trigger_reason_mismatch"
        if audit.confidence is not AiAttributionConfidence.HIGH:
            return None, "application_confidence_not_high"
        if not audit.evidence_codes or any(code not in AI_ATTRIBUTION_EVIDENCE_CODES for code in audit.evidence_codes):
            return None, "application_evidence_code_invalid"
        if len(set(audit.evidence_codes)) != len(audit.evidence_codes):
            return None, "application_evidence_code_duplicate"
        no_target_identity = context.tmdb_id is None and context.imdb_id is None
        expected_reason = "accepted_without_identity_title" if no_target_identity else "accepted"
        if audit.reason_code != expected_reason:
            return None, "application_accepted_reason_invalid"
        if no_target_identity:
            if not ({"title", "alias"} & set(audit.evidence_codes)):
                return None, "application_identity_title_evidence_missing"
            reference_years = {value for value in (original.year, snapshot.year) if isinstance(value, int)}
            if context.year is not None and any(year != context.year for year in reference_years):
                return None, "application_year_mismatch"
        if (
            audit.before_strategy is not strategy
            or audit.original_unmatched_reason is not original.unmatched_reason
            or audit.media_type is not evidence.media_type
            or audit.tmdb_id != evidence.tmdb_id
            or cls._normalized_imdb_id(audit.imdb_id) != cls._normalized_imdb_id(evidence.imdb_id)
            or audit.season != evidence.season
            or audit.episode != evidence.episode
        ):
            return None, "application_audit_mismatch"
        return evidence, None

    @staticmethod
    def _application_rejection_audit(
        audit_value: Any,
        proposed_value: Any,
        original: FileAttributionEvidence,
        strategy: PackageAttributionStrategy,
        reason: str,
    ) -> AiAttributionAudit:
        """把应用层拒绝转换为不保留错误建议字段的强类型审计。"""

        # 应用层拒绝时不信任适配器提供的 Provider、模型、触发文本或任何
        # 建议字段，统一生成最小脱敏审计，避免异常替身把秘密写入持久化记录。
        del audit_value, proposed_value
        return AiAttributionAudit(
            before_strategy=strategy,
            original_unmatched_reason=original.unmatched_reason,
            trigger_reason=AiAttributionTriggerReason.APPLICATION_VALIDATION,
            outcome=AiAttributionOutcome.REJECTED,
            reason_code=AiAttributionReasonCode(reason),
        )
