"""单候选下载能力的输入、策略与处理结果。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .attribution import PackageAttributionStrategy
from .record import CommittedFileFact, MatchRecord
from .source import CandidateHandle
from .target import PathMappingSnapshot, SubtitleDestination, SubtitleTarget

__all__ = ["DownloadReasonCode", "DownloadRequest", "DownloadResult", "DownloadRetention", "DownloadStatus"]


class DownloadStatus(StrEnum):
    """候选下载尝试的业务结论。"""

    SUCCESS = "success"
    DOWNLOAD_FAILED = "download_failed"
    EXTRACT_FAILED = "extract_failed"
    NO_MATCH = "no_match"
    WRITE_FAILED = "write_failed"
    INTERRUPTED = "interrupted"


class DownloadReasonCode(StrEnum):
    """单候选下载处理的稳定失败原因。"""

    TARGET_DIRECTORY_UNAVAILABLE = "target_directory_unavailable"
    SUBTITLE_DESTINATION_CONFLICT = "subtitle_destination_conflict"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CANDIDATE_MISSING_TARGET_SUBTITLE = "candidate_missing_target_subtitle"


class DownloadRetention(StrEnum):
    """下载结果无法归属或落盘时的保留策略。"""

    PRESERVE = "preserve"
    DISCARD = "discard"


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """单候选下载的冻结输入及明确处理策略。"""

    task_id: str
    handle: CandidateHandle
    target: SubtitleTarget
    trust_direct_file: bool
    retention: DownloadRetention
    package_attribution_strategy: PackageAttributionStrategy
    attempt_number: int
    target_history_id: int | None = None
    history_target_path: Path | None = None
    matched_path_mapping: PathMappingSnapshot | None = None
    target_file_exists: bool | None = None
    destination: SubtitleDestination | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """单候选下载的处理结论，不决定任务终态。"""

    records: tuple[MatchRecord, ...]
    result: DownloadStatus
    reason_code: DownloadReasonCode | None = None
    error_summary: str | None = None
    committed_media_records: tuple[MatchRecord, ...] = ()
    committed_files: tuple[CommittedFileFact, ...] = ()
    warnings: tuple[str, ...] = ()
