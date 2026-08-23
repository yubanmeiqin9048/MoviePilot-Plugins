"""单个字幕候选尝试的下载、解包、归属与落盘流水线。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypedDict, cast

from anyio import Path as AsyncPath

from app.log import logger

from ..attribution import CandidateRecognizer, FileAttributor
from ..record import RecordCommitter
from ..schemas.attribution import (
    AttributionEvidence,
    CandidateAttributionSnapshot,
    FileAttributionEvidence,
    FileAttributionMethod,
    FileAttributionRequest,
    PackageAttributionStrategy,
    UnmatchedReason,
)
from ..schemas.candidate import SubtitleCandidate
from ..schemas.config import PluginConfig
from ..schemas.event import SubtitleWrittenOperation
from ..schemas.file import ExtractedSubtitle
from ..schemas.record import (
    FileLocation,
    MatchRecord,
    RecordStatus,
)
from ..schemas.source import CandidateHandle, DownloadedAsset, SubtitleSource
from ..schemas.target import (
    MediaType,
    SubtitleTarget,
)
from ..schemas.task import (
    AttemptResult,
    CandidateAttempt,
    CandidateAttemptReasonCode,
    SubtitleTask,
    TaskStage,
)
from ..source import CandidatePool


class FailureResultRetention(StrEnum):
    """定义候选失败时下载结果的处理方式。"""

    PRESERVE = "preserve"
    DISCARD = "discard"


@dataclass(slots=True)
class AttributedSubtitle:
    """运行期物理字幕文件及其持久化归属证据。"""

    extracted: ExtractedSubtitle
    evidence: FileAttributionEvidence


@dataclass(frozen=True, slots=True)
class CandidateAttemptResult:
    """候选尝试返回的唯一结构化结论。"""

    records: list[MatchRecord]
    attempt: CandidateAttempt
    reason_code: CandidateAttemptReasonCode | None = None


@dataclass(frozen=True, slots=True)
class _CandidateWriteResult:
    """单个候选字幕写入的结构化结果。"""

    record: MatchRecord | None = None
    error_summary: str | None = None
    reason_code: CandidateAttemptReasonCode | None = None


class _CandidateFileMetrics(TypedDict):
    """一次候选文件归属处理的完整计数指标。"""

    extracted_count: int
    current_target_count: int
    same_media_other_episode_count: int
    ambiguous_count: int
    other_media_count: int
    ai_attempt_count: int
    ai_accepted_count: int
    ai_rejected_count: int
    ai_error_count: int
    ai_over_limit_count: int
    ai_reason_summary: dict[str, int]


class CandidateAttemptFileSystemPort(Protocol):
    """候选尝试所需的字幕文件操作能力。"""

    async def make_task_directory(self, task_id: str) -> Path:
        """创建候选尝试临时目录。"""

    async def target_directory_status(self, target: Path) -> tuple[bool, str | None]:
        """检查目标字幕目录是否可用。"""


class CandidateAttemptArchivePort(Protocol):
    """候选尝试所需的字幕归档解包能力。"""

    async def extract(
        self,
        asset: DownloadedAsset,
        output: Path,
        allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """解包下载结果并返回受支持的字幕文件。"""


class CandidateAttemptSourcePort(Protocol):
    """候选尝试所需的字幕源下载能力。"""

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """下载候选到指定临时目录。"""


_ATTEMPT_RESULT_BY_STAGE: Mapping[TaskStage, AttemptResult] = {
    TaskStage.DOWNLOAD: AttemptResult.DOWNLOAD_FAILED,
    TaskStage.EXTRACT: AttemptResult.EXTRACT_FAILED,
    TaskStage.MATCH: AttemptResult.NO_MATCH,
    TaskStage.AI_ATTRIBUTION: AttemptResult.NO_MATCH,
    TaskStage.WRITE: AttemptResult.WRITE_FAILED,
}


StageCallback = Callable[[TaskStage], Awaitable[None]]
CandidateLabel = Callable[[SubtitleCandidate], str]
TaskLabel = Callable[[SubtitleTask], str]


class CandidateAttemptService:
    """处理一个字幕候选，不读取任务触发方式或任务队列状态。"""

    def __init__(
        self,
        filesystem: CandidateAttemptFileSystemPort,
        archive: CandidateAttemptArchivePort,
        matcher: CandidateRecognizer,
        sources: CandidatePool,
        config: PluginConfig,
        inventory: RecordCommitter,
        attributor: FileAttributor | None = None,
        source_adapters: Mapping[SubtitleSource, CandidateAttemptSourcePort] | None = None,
        *,
        task_label: TaskLabel | None = None,
        candidate_label: CandidateLabel | None = None,
    ) -> None:
        """绑定候选流水线所需的文件、来源、归属和记录协作者。"""

        self.filesystem = filesystem
        self.archive = archive
        self.matcher = matcher
        if attributor is not None:
            self.attributor = attributor
        elif callable(getattr(matcher, "attribute_requests", None)):
            self.attributor = cast(FileAttributor, matcher)
        else:
            raise TypeError("候选流水线必须注入文件归属 facade")
        self._source_downloader = sources
        self._source_adapters = source_adapters
        self.sources = sources
        self.config = config
        self.inventory = inventory
        self._task_label = task_label or (lambda task: f"任务 {task.id}")
        self._candidate_label = candidate_label or (lambda candidate: f"候选“{candidate.name}”")

    async def attempt(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        handle: CandidateHandle,
        retention: FailureResultRetention,
        *,
        on_stage: StageCallback,
    ) -> CandidateAttemptResult:
        """执行单个候选的下载、解包、归属和落盘并返回结论。"""

        candidate = handle.candidate
        snapshot = self.matcher.candidate_snapshot(candidate)
        task.candidate_attribution_snapshot = snapshot
        attempt_result = AttemptResult.NO_MATCH
        error_summary: str | None = None
        active_stage = TaskStage.DOWNLOAD
        metrics: _CandidateFileMetrics = {
            "extracted_count": 0,
            "current_target_count": 0,
            "same_media_other_episode_count": 0,
            "ambiguous_count": 0,
            "other_media_count": 0,
            "ai_attempt_count": 0,
            "ai_accepted_count": 0,
            "ai_rejected_count": 0,
            "ai_error_count": 0,
            "ai_over_limit_count": 0,
            "ai_reason_summary": {},
        }
        written_count = 0
        staged_count = 0
        unmatched_count = 0
        selected_results: list[AttributedSubtitle] = []
        additional: list[AttributedSubtitle] = []
        first_write_failure_seen = False
        write_reason_code: CandidateAttemptReasonCode | None = None

        async def announce_stage(stage: TaskStage) -> None:
            """先记录候选当前阶段，再交给协调器处理阶段快照。"""

            nonlocal active_stage
            active_stage = stage
            await on_stage(stage)

        def result_reason_code(
            reason_code: CandidateAttemptReasonCode | None,
        ) -> CandidateAttemptReasonCode | None:
            """把通用失败收敛为保留策略对应的人工失败原因。"""

            if reason_code is not None or retention is FailureResultRetention.DISCARD:
                return reason_code
            if attempt_result is AttemptResult.INTERRUPTED:
                return None
            return CandidateAttemptReasonCode.MANUAL_CANDIDATE_FAILED

        def conclude(
            records: Sequence[MatchRecord],
            reason_code: CandidateAttemptReasonCode | None,
        ) -> CandidateAttemptResult:
            """构造审计记录与稳定原因码，不修改任务审计列表。"""

            attempt = CandidateAttempt(
                candidate_key=candidate.stable_key,
                source=candidate.source,
                package_scope=candidate.package_scope,
                language=candidate.language,
                format=candidate.format,
                translation_type=candidate.translation_type,
                hearing_impaired=candidate.hearing_impaired,
                attribution_strategy=task.package_attribution_strategy,
                candidate_snapshot=snapshot,
                extracted_count=metrics["extracted_count"],
                current_target_count=metrics["current_target_count"],
                same_media_other_episode_count=metrics["same_media_other_episode_count"],
                ambiguous_count=metrics["ambiguous_count"],
                other_media_count=metrics["other_media_count"],
                written_count=written_count,
                staged_count=staged_count,
                unmatched_count=unmatched_count,
                result=attempt_result,
                error_summary=error_summary,
                ai_attempt_count=metrics["ai_attempt_count"],
                ai_accepted_count=metrics["ai_accepted_count"],
                ai_rejected_count=metrics["ai_rejected_count"],
                ai_error_count=metrics["ai_error_count"],
                ai_over_limit_count=metrics["ai_over_limit_count"],
                ai_reason_summary=metrics["ai_reason_summary"],
            )
            return CandidateAttemptResult(
                records=list(records),
                attempt=attempt,
                reason_code=result_reason_code(reason_code),
            )

        task_dir = await self.filesystem.make_task_directory(task.id)
        candidate_dir = task_dir / f"candidate-{len(task.candidate_attempts) + 1}"
        await AsyncPath(candidate_dir).mkdir(parents=True, exist_ok=True)
        try:
            await announce_stage(TaskStage.DOWNLOAD)
            logger_message = f"{self._task_label(task)}开始下载{self._candidate_label(candidate)}"
            logger.info(logger_message)
            downloader = getattr(self._source_downloader, "download", None)
            if callable(downloader):
                asset = await downloader(handle, candidate_dir)
            else:
                getter = getattr(self._source_downloader, "__getitem__", None)
                if callable(getter):
                    asset = await getter(candidate.source).download(handle, candidate_dir)
                elif self._source_adapters is not None:
                    asset = await self._source_adapters[candidate.source].download(handle, candidate_dir)
                else:
                    raise TypeError("来源下载能力未装配")
            logger.info(
                f"{self._task_label(task)}已下载{self._candidate_label(candidate)}，得到文件“{asset.file_name}”"
            )
            asset_extension = asset.path.suffix.lower().lstrip(".")
            archive_extensions = {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "cab", "iso"}
            allowed_extensions = {value.lower().lstrip(".") for value in self.config.format_priority}
            if (
                retention is FailureResultRetention.PRESERVE
                and asset_extension not in allowed_extensions | archive_extensions
            ):
                unsupported = AttributedSubtitle(
                    extracted=ExtractedSubtitle(
                        physical_path=asset.path,
                        logical_source_path=Path(asset.file_name),
                        is_direct_file=True,
                    ),
                    evidence=FileAttributionEvidence(
                        logical_source_path=Path(asset.file_name),
                        method=FileAttributionMethod.DIRECT_FILE,
                        belongs_to_target_media=None,
                        unmatched_reason=UnmatchedReason.UNSUPPORTED_FORMAT,
                    ),
                )
                record = await self._save_plugin_result(
                    task,
                    context,
                    candidate,
                    unsupported,
                    snapshot,
                    bind_target=True,
                )
                unmatched_count = 1
                attempt_result = AttemptResult.NO_MATCH
                error_summary = "字幕格式未知或不受宿主支持，文件已保存为未匹配"
                logger.warning(
                    f"{self._task_label(task)}下载的文件“{asset.file_name}”格式不受宿主支持，"
                    f"已保存为未匹配记录 {record.id}"
                )
                return conclude([], CandidateAttemptReasonCode.UNSUPPORTED_FORMAT)

            await announce_stage(TaskStage.EXTRACT)
            extracted = await self.archive.extract(asset, candidate_dir / "extracted", set(self.config.format_priority))
            if not extracted:
                attempt_result = AttemptResult.NO_MATCH
                error_summary = "候选包没有允许格式字幕"
                return conclude([], None)
            logger.info(
                f"{self._task_label(task)}已从{self._candidate_label(candidate)}中取得 "
                f"{len(extracted)} 个受支持的字幕文件"
            )
            await announce_stage(TaskStage.MATCH)
            selected_results, additional, snapshot, metrics = await self._candidate_files(
                task,
                context,
                handle,
                extracted,
                on_stage=announce_stage,
            )
            if not selected_results:
                attempt_result = AttemptResult.NO_MATCH
                if retention is FailureResultRetention.PRESERVE:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        context,
                        candidate,
                        additional,
                        snapshot,
                    )
                    error_summary = "候选包未找到当前目标字幕，其他有效结果已保留"
                else:
                    error_summary = "候选包未找到当前目标字幕"
                return conclude([], CandidateAttemptReasonCode.CANDIDATE_MISSING_TARGET_SUBTITLE)

            if retention is FailureResultRetention.PRESERVE:
                directory_available, directory_error = await self.filesystem.target_directory_status(
                    Path(task.target_path)
                )
                if not directory_available:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        context,
                        candidate,
                        selected_results + additional,
                        snapshot,
                    )
                    attempt_result = AttemptResult.WRITE_FAILED
                    error_summary = f"目标目录不可用：{directory_error or '无法写入'}，下载结果已保留"
                    return conclude([], CandidateAttemptReasonCode.TARGET_DIRECTORY_UNAVAILABLE)

            await announce_stage(TaskStage.WRITE)
            written_records: list[MatchRecord] = []
            write_errors: list[str] = []
            for selected in selected_results:
                write_result = await self._write_candidate_file(
                    task,
                    context,
                    candidate,
                    selected,
                    snapshot,
                )
                record = write_result.record
                if record is None:
                    if write_result.error_summary is not None:
                        write_errors.append(write_result.error_summary)
                    if not first_write_failure_seen:
                        first_write_failure_seen = True
                        write_reason_code = write_result.reason_code
                    continue
                written_records.append(record)
                task.final_subtitle_path = record.final_subtitle_path
                logger.info(
                    f"{self._task_label(task)}已将字幕"
                    f"“{selected.extracted.logical_source_path}”写入“{record.final_subtitle_path}”，"
                    f"匹配记录为 {record.id}"
                )
            written_count = len(written_records)
            if not written_records:
                attempt_result = AttemptResult.WRITE_FAILED
                error_summary = write_errors[0] if write_errors else "没有形成已匹配记录"
                if retention is FailureResultRetention.PRESERVE:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        context,
                        candidate,
                        selected_results + additional,
                        snapshot,
                    )
                    error_summary += "，下载结果已保留"
                return conclude([], write_reason_code)
            if write_errors:
                task.warning_count += len(write_errors)
                task.warning_summaries.extend(f"部分字幕文件落盘失败：{error}" for error in write_errors)
                error_summary = f"部分字幕文件落盘失败：{'；'.join(write_errors)}"
            await announce_stage(TaskStage.MATCH)
            staged_count, unmatched_count = await self._save_additional_results(
                task,
                context,
                candidate,
                additional,
                snapshot,
            )
            task.result_source = candidate.source
            task.result_package_scope = candidate.package_scope
            task.result_format = selected_results[0].extracted.physical_path.suffix.lstrip(".").upper()
            attempt_result = AttemptResult.SUCCESS
            return conclude(written_records, None)
        except asyncio.CancelledError:
            attempt_result = AttemptResult.INTERRUPTED
            return conclude([], None)
        except FileExistsError:
            attempt_result = AttemptResult.WRITE_FAILED
            error_summary = "目标字幕已存在，未覆盖"
            if retention is FailureResultRetention.PRESERVE and selected_results:
                try:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        context,
                        candidate,
                        selected_results + additional,
                        snapshot,
                    )
                    error_summary = "目标字幕已存在，下载结果已保留"
                except Exception as exc:  # noqa: BLE001 - 保留下载结果失败应返回安全失败
                    error_summary = f"目标字幕已存在，下载结果保留失败：{type(exc).__name__}"
            return conclude([], CandidateAttemptReasonCode.SUBTITLE_DESTINATION_CONFLICT)
        except OSError as exc:
            attempt_result = AttemptResult.WRITE_FAILED
            error_summary = f"文件操作失败：{type(exc).__name__}"
            if retention is FailureResultRetention.PRESERVE and active_stage is TaskStage.WRITE and selected_results:
                try:
                    staged_count, unmatched_count = await self._save_additional_results(
                        task,
                        context,
                        candidate,
                        selected_results + additional,
                        snapshot,
                    )
                    error_summary += "，下载结果已保留"
                except Exception as preserve_exc:  # noqa: BLE001 - 保留下载结果失败应返回安全失败
                    error_summary += f"，下载结果保留失败：{type(preserve_exc).__name__}"
            return conclude([], None)
        except RuntimeError as exc:
            attempt_result = _ATTEMPT_RESULT_BY_STAGE[active_stage]
            if type(exc).__name__ in {"SourceRequestError", "SourceLimitedError"}:
                error_summary = str(exc)
            else:
                error_summary = f"{self._stage_name(active_stage)}阶段失败：{exc}"
            return conclude([], None)
        except Exception as exc:  # noqa: BLE001 - 候选边界必须收敛运行时失败
            attempt_result = _ATTEMPT_RESULT_BY_STAGE[active_stage]
            error_summary = f"候选处理失败：{type(exc).__name__}"
            return conclude([], None)

    async def _candidate_files(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        handle: CandidateHandle,
        files: list[ExtractedSubtitle],
        *,
        on_stage: StageCallback,
    ) -> tuple[
        list[AttributedSubtitle],
        list[AttributedSubtitle],
        CandidateAttributionSnapshot,
        _CandidateFileMetrics,
    ]:
        """逐文件归属并选出当前目标第一优先字幕。"""

        candidate = handle.candidate
        snapshot = self.matcher.candidate_snapshot(candidate)
        task.candidate_attribution_snapshot = snapshot
        attributed: list[AttributedSubtitle] = []
        attribution_metrics = {
            "attempt_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "error_count": 0,
            "over_limit_count": 0,
            "reason_summary": {},
        }
        other_media_count = 0
        host_file_count = sum(1 for extracted in files if not extracted.is_direct_file)
        if task.package_attribution_strategy is PackageAttributionStrategy.HOST_RECOGNITION and host_file_count:
            logger.info(
                f"{self._task_label(task)}开始调用 MoviePilot 文件识别处理"
                f"{self._candidate_label(candidate)}中的 {host_file_count} 个字幕"
            )
        for extracted in files:
            if extracted.is_direct_file:
                evidence = FileAttributionEvidence(
                    logical_source_path=Path(extracted.logical_source_path),
                    method=FileAttributionMethod.DIRECT_FILE,
                    belongs_to_target_media=True,
                    media_type=context.media_type,
                    tmdb_id=context.tmdb_id,
                    imdb_id=context.imdb_id,
                    season=context.season,
                    episode=context.episode,
                    season_evidence=AttributionEvidence.NOT_APPLICABLE,
                    episode_evidence=AttributionEvidence.NOT_APPLICABLE,
                )
            else:
                request = FileAttributionRequest(
                    path=extracted.physical_path,
                    logical_source_path=Path(extracted.logical_source_path),
                    target=context,
                    candidate_snapshot=snapshot,
                    strategy=task.package_attribution_strategy,
                )

                async def announce_attribution_batch(_data: dict[str, object]) -> None:
                    """仅在归属 facade 实际开始一批补充归属时推进任务阶段。"""

                    await on_stage(TaskStage.AI_ATTRIBUTION)

                async def finish_attribution_batch(_data: dict[str, object]) -> None:
                    """归属 facade 完成一批补充归属后恢复常规匹配阶段。"""

                    await on_stage(TaskStage.MATCH)

                batch = await self.attributor.attribute_requests(
                    context,
                    candidate,
                    snapshot,
                    [request],
                    task.package_attribution_strategy,
                    evidence_by_key={},
                    on_batch_start=announce_attribution_batch,
                    on_batch_end=finish_attribution_batch,
                )
                evidence = next(iter(batch.evidence_by_key.values()), None)
                if evidence is None:
                    raise RuntimeError("文件归属能力未返回证据")
                attribution_metrics["attempt_count"] += batch.submitted_count
                attribution_metrics["accepted_count"] += batch.accepted_count
                attribution_metrics["rejected_count"] += batch.rejected_count
                attribution_metrics["error_count"] += batch.error_count
                attribution_metrics["over_limit_count"] += batch.over_limit_count
                for reason, count in batch.reason_summary.items():
                    attribution_metrics["reason_summary"][reason] = (
                        attribution_metrics["reason_summary"].get(reason, 0) + count
                    )
            if evidence.belongs_to_target_media is False:
                other_media_count += 1
                continue
            attributed.append(AttributedSubtitle(extracted=extracted, evidence=evidence))
        if task.package_attribution_strategy is PackageAttributionStrategy.HOST_RECOGNITION and host_file_count:
            logger.info(
                f"{self._task_label(task)}已完成 MoviePilot 文件识别，"
                f"处理 {host_file_count} 个字幕，其中明确属于其他媒体 {other_media_count} 个"
            )

        current_files, additional, ambiguous_count, same_media_other_episode_count = self._classify_files(
            attributed,
            context,
        )
        metrics: _CandidateFileMetrics = {
            "extracted_count": len(files),
            "current_target_count": len(current_files),
            "same_media_other_episode_count": same_media_other_episode_count,
            "ambiguous_count": ambiguous_count,
            "other_media_count": other_media_count,
            "ai_attempt_count": attribution_metrics["attempt_count"],
            "ai_accepted_count": attribution_metrics["accepted_count"],
            "ai_rejected_count": attribution_metrics["rejected_count"],
            "ai_error_count": attribution_metrics["error_count"],
            "ai_over_limit_count": attribution_metrics["over_limit_count"],
            "ai_reason_summary": attribution_metrics["reason_summary"],
        }
        if not current_files:
            return [], additional, snapshot, metrics
        format_order = {value.upper().lstrip("."): index for index, value in enumerate(self.config.format_priority)}
        current_files.sort(
            key=lambda result: (
                format_order.get(result.extracted.physical_path.suffix.lstrip(".").upper(), 999),
                result.extracted.logical_source_path,
            )
        )
        return current_files, additional, snapshot, metrics

    def _classify_files(
        self,
        attributed: list[AttributedSubtitle],
        context: SubtitleTarget,
    ) -> tuple[list[AttributedSubtitle], list[AttributedSubtitle], int, int]:
        """按最新证据重算当前集、附加集和漏斗计数。"""

        current: list[AttributedSubtitle] = []
        extra: list[AttributedSubtitle] = []
        ambiguous = 0
        other_episode = 0
        for candidate_result in attributed:
            evidence = candidate_result.evidence
            season_value, season_count = self._unique_scope_value(evidence, "season")
            episode_value, episode_count = self._unique_scope_value(evidence, "episode")
            complete = evidence.belongs_to_target_media is True and (
                context.media_type is MediaType.MOVIE
                or (season_count == 1 and episode_count == 1 and season_value is not None and episode_value is not None)
            )
            if not complete or evidence.unmatched_reason is not None:
                ambiguous += 1
                extra.append(candidate_result)
                continue
            is_current = context.media_type is MediaType.MOVIE or (
                season_value == context.season and episode_value == context.episode
            )
            if is_current:
                current.append(candidate_result)
            else:
                other_episode += 1
                extra.append(candidate_result)
        return current, extra, ambiguous, other_episode

    async def _make_record(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        result: AttributedSubtitle,
        snapshot: CandidateAttributionSnapshot,
        status: RecordStatus,
        location: FileLocation,
        path: str | Path,
        final_path: str | Path | None,
        bind_target: bool,
    ) -> MatchRecord:
        """构造一条安全匹配记录，提交副作用由记录能力统一处理。"""

        source_path = result.extracted.physical_path
        evidence = result.evidence
        size: int | None = None
        try:
            size = (await AsyncPath(source_path).stat()).st_size
        except OSError:
            size = None
        identity = context.canonical_identity if evidence.belongs_to_target_media is True else None
        record = MatchRecord(
            subtitle_file_name=source_path.name,
            format=source_path.suffix.lstrip(".").upper(),
            size=size,
            media_title=context.title if evidence.belongs_to_target_media is True else None,
            year=context.year if evidence.belongs_to_target_media is True else None,
            media_type=evidence.media_type,
            season=evidence.season,
            episode=evidence.episode,
            status=status,
            source=candidate.source,
            package_scope=candidate.package_scope,
            location=location,
            path=Path(path),
            canonical_identity_type=identity[0] if identity else None,
            canonical_identity_value=identity[1] if identity else None,
            tmdb_id=evidence.tmdb_id,
            imdb_id=evidence.imdb_id,
            target_history_id=task.target_history_id if bind_target else None,
            history_target_path=task.history_target_path if bind_target else None,
            target_path=task.target_path if bind_target else None,
            matched_path_mapping=task.matched_path_mapping if bind_target else None,
            target_file_exists=task.target_file_exists if bind_target else None,
            final_subtitle_path=Path(final_path) if final_path is not None else None,
            source_task_id=task.id,
            candidate_key=candidate.stable_key,
            candidate_name=candidate.name,
            candidate_attribution_snapshot=snapshot,
            logical_source_path=evidence.logical_source_path,
            file_attribution_method=evidence.method,
            season_evidence=evidence.season_evidence,
            episode_evidence=evidence.episode_evidence,
            unmatched_reason=evidence.unmatched_reason,
            host_recognition_summary=evidence.host_recognition_summary,
            language=candidate.language,
            translation_type=candidate.translation_type,
            hearing_impaired=candidate.hearing_impaired,
            exact_id_match=candidate.exact_id_match,
            site_priority=candidate.site_priority,
            trusted=candidate.trusted,
            score=candidate.score,
            votes=candidate.votes,
            download_count=candidate.download_count,
            uploaded_at=candidate.uploaded_at,
            revision=candidate.revision,
            ai_takeover_audit=evidence.ai_takeover_audit,
        )
        if status is RecordStatus.STAGED:
            record.staged_at = record.created_at
        return record

    @staticmethod
    def _record_task_result(task: SubtitleTask, record: MatchRecord) -> None:
        """在记录能力完成提交后更新任务审计摘要。"""

        task.record_ids.append(record.id)
        task.record_counts[record.status.value] = task.record_counts.get(record.status.value, 0) + 1

    async def _write_candidate_file(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        result: AttributedSubtitle,
        snapshot: CandidateAttributionSnapshot,
    ) -> _CandidateWriteResult:
        """写入一个候选字幕并只在匹配记录保存成功后返回文件事实。"""

        try:
            record = await self._make_record(
                task,
                context,
                candidate,
                result,
                snapshot,
                RecordStatus.MATCHED,
                FileLocation.MEDIA_DIRECTORY,
                "",
                None,
                True,
            )
            operation = (
                SubtitleWrittenOperation.MANUAL_CANDIDATE
                if task.trigger.value == "manual_candidate"
                else SubtitleWrittenOperation.AUTOMATIC_CANDIDATE
            )
            record = await self.inventory.commit_media(
                record,
                result.extracted.physical_path,
                Path(task.target_path),
                operation,
            )
            self._record_task_result(task, record)
        except asyncio.CancelledError:
            raise
        except FileExistsError:
            return _CandidateWriteResult(
                error_summary="目标字幕已存在，未覆盖",
                reason_code=CandidateAttemptReasonCode.SUBTITLE_DESTINATION_CONFLICT,
            )
        except OSError as exc:
            return _CandidateWriteResult(error_summary=f"文件操作失败：{type(exc).__name__}")
        except Exception as exc:  # noqa: BLE001 - 单文件记录失败不吞掉其它已提交文件
            return _CandidateWriteResult(error_summary=f"匹配记录保存失败：{type(exc).__name__}")
        return _CandidateWriteResult(record=record)

    @staticmethod
    def _can_stage(result: AttributedSubtitle, context: SubtitleTarget) -> bool:
        """判断具体字幕归属是否足够进入暂存库存。"""

        evidence = result.evidence
        if evidence.belongs_to_target_media is not True or evidence.unmatched_reason is not None:
            return False
        if context.media_type is MediaType.MOVIE:
            return context.canonical_identity is not None
        return bool(
            context.canonical_identity is not None and evidence.season is not None and evidence.episode is not None
        )

    async def _save_plugin_result(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        result: AttributedSubtitle,
        snapshot: CandidateAttributionSnapshot,
        *,
        bind_target: bool,
    ) -> MatchRecord:
        """把归属完整或不完整的字幕保存为暂存或未匹配记录。"""

        status = RecordStatus.STAGED if self._can_stage(result, context) else RecordStatus.UNMATCHED
        record = await self._make_record(
            task,
            context,
            candidate,
            result,
            snapshot,
            status,
            FileLocation.PLUGIN_DATA,
            "",
            None,
            bind_target,
        )
        record = await self.inventory.commit_plugin(record, result.extracted.physical_path)
        self._record_task_result(task, record)
        return record

    async def _save_additional_results(
        self,
        task: SubtitleTask,
        context: SubtitleTarget,
        candidate: SubtitleCandidate,
        results: list[AttributedSubtitle],
        snapshot: CandidateAttributionSnapshot,
    ) -> tuple[int, int]:
        """保存附加字幕，单文件失败仅形成任务警告。"""

        staged_count = 0
        unmatched_count = 0
        for result in results:
            evidence = result.evidence
            bind_target = bool(
                evidence.belongs_to_target_media is True
                and (
                    context.media_type is MediaType.MOVIE
                    or (evidence.season == context.season and evidence.episode == context.episode)
                )
            )
            try:
                record = await self._save_plugin_result(
                    task,
                    context,
                    candidate,
                    result,
                    snapshot,
                    bind_target=bind_target,
                )
            except Exception as exc:  # noqa: BLE001 - 附加字幕失败不能中断候选处理
                task.warning_count += 1
                task.warning_summaries.append(f"附加字幕保存失败：{type(exc).__name__}")
                logger.warning(
                    f"{self._task_label(task)}保存附加字幕"
                    f"“{result.extracted.logical_source_path}”失败，将继续处理任务："
                    f"{type(exc).__name__}"
                )
                continue
            if record.status is RecordStatus.STAGED:
                staged_count += 1
                logger.info(
                    f"{self._task_label(task)}已将附加字幕"
                    f"“{result.extracted.logical_source_path}”保存为暂存记录 {record.id}"
                )
            else:
                unmatched_count += 1
                logger.info(
                    f"{self._task_label(task)}无法完整确认附加字幕"
                    f"“{result.extracted.logical_source_path}”的归属，"
                    f"已保存为未匹配记录 {record.id}"
                )
        return staged_count, unmatched_count

    @staticmethod
    def _unique_scope_value(evidence: FileAttributionEvidence, field: str) -> tuple[int | None, int]:
        """读取保留基数的季集字段，兼容旧测试替身的单值证据。"""

        values = list(getattr(evidence, f"{field}_values", []) or [])
        scalar = getattr(evidence, field, None)
        if not values and scalar is not None:
            values = [scalar]
        return (values[0], 1) if len(values) == 1 else (None, len(values))

    @staticmethod
    def _stage_name(stage: TaskStage) -> str:
        """返回候选流水线阶段的中文名称。"""

        return {
            TaskStage.DOWNLOAD: "候选下载",
            TaskStage.EXTRACT: "下载结果解包",
            TaskStage.MATCH: "字幕匹配",
            TaskStage.AI_ATTRIBUTION: "AI 智能接管",
            TaskStage.WRITE: "字幕落盘",
        }[stage]
