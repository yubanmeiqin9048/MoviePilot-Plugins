"""应用层依赖的最小能力协议。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from app.core.context import MediaInfo as HostMediaInfo

from ..domain.enums import PackageAttributionStrategy, RecordStatus, SubtitleSource
from ..domain.models import (
    CandidateAttributionSnapshot,
    CandidateRecognition,
    FileAttributionEvidence,
    MatchRecord,
    MediaContext,
    SourceDetails,
    SourceStatus,
    SubtitleCandidate,
    SubtitleTask,
)


@dataclass(slots=True)
class CandidateHandle:
    """安全候选与仅在当前任务内存存在的下载句柄。"""

    candidate: SubtitleCandidate
    opaque: Any


@dataclass(slots=True)
class SourceSearchResult:
    """一个字幕源的搜索结果与安全运行摘要。"""

    source: SubtitleSource
    candidates: list[CandidateHandle] = field(default_factory=list)
    duration_ms: int | None = None
    error_summary: str | None = None
    limited: bool = False
    raw_count: int = 0
    admitted_count: int = 0
    rejection_summary: dict[str, int] = field(default_factory=dict)
    skip_reason: str | None = None
    details: SourceDetails = field(default_factory=dict)


ManualSourceStatus = Literal["success", "limited", "error", "disabled", "unconfigured"]


@dataclass(slots=True)
class ManualSourceSearchResult:
    """单个字幕源的人工搜索结果与实际查询轨迹。"""

    source: SubtitleSource
    status: ManualSourceStatus
    candidates: list[CandidateHandle] = field(default_factory=list)
    default_queries: list[str] = field(default_factory=list)
    executed_queries: list[str] = field(default_factory=list)
    matched_query: str | None = None
    duration_ms: int | None = None
    error_summary: str | None = None
    raw_count: int = 0
    admitted_count: int = 0
    rejection_summary: dict[str, int] = field(default_factory=dict)
    skip_reason: str | None = None
    details: SourceDetails = field(default_factory=dict)


@dataclass(slots=True)
class DownloadedAsset:
    """字幕源写入临时目录后的下载文件。"""

    path: Path
    file_name: str


@dataclass(frozen=True, slots=True)
class ExtractedSubtitle:
    """描述解包后字幕的物理位置与可审计逻辑来源。"""

    physical_path: Path
    logical_source_path: str
    is_direct_file: bool


class SubtitleSourcePort(Protocol):
    """单个字幕源适配器协议。"""

    source: SubtitleSource
    enabled: bool

    async def search(self, context: MediaContext, allow_machine: bool) -> SourceSearchResult:
        """搜索并返回已完成来源准入过滤的候选。"""

    async def manual_search(
        self,
        context: MediaContext,
        custom_query: str | None = None,
    ) -> ManualSourceSearchResult:
        """搜索不经过自动准入过滤的人工候选。"""

    async def download(self, handle: CandidateHandle, directory: Path) -> DownloadedAsset:
        """串行下载候选到指定临时目录。"""

    async def refresh(self, manual: bool = False) -> SourceStatus:
        """刷新该来源的当前健康状态。"""

    async def close(self) -> None:
        """取消并释放来源当前运行态资源。"""


class StorePort(Protocol):
    """任务、记录、来源状态和凭据持久化协议。"""

    async def list_tasks(self) -> list[SubtitleTask]:
        """返回全部任务快照。"""

    async def save_task(self, task: SubtitleTask) -> None:
        """新增或更新任务并应用保留规则。"""

    async def delete_task(self, task_id: str) -> bool:
        """删除指定任务。"""

    async def list_records(self) -> list[MatchRecord]:
        """返回全部匹配记录快照。"""

    async def get_record(self, record_id: str) -> MatchRecord | None:
        """读取指定匹配记录快照。"""

    async def save_record(self, record: MatchRecord) -> None:
        """新增或更新匹配记录并应用保留规则。"""

    async def delete_record(self, record_id: str) -> bool:
        """删除指定匹配记录。"""

    async def delete_record_if_match(self, expected: MatchRecord) -> bool:
        """仅在记录确认版本未变化时原子删除。"""

    async def list_source_statuses(self) -> list[SourceStatus]:
        """返回三个来源的状态快照。"""

    async def save_source_status(self, status: SourceStatus) -> None:
        """保存单个来源状态。"""

    async def get_credentials(self, source: SubtitleSource) -> dict[str, str]:
        """读取单个外部来源的长期凭据。"""

    async def update_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """以非空字段增量更新来源凭据并返回配置完成状态。"""

    async def clear_credentials(self, source: SubtitleSource) -> None:
        """删除单个来源的全部长期凭据。"""


class FileSystemPort(Protocol):
    """字幕文件检查、落盘与插件数据文件操作协议。"""

    async def has_standard_subtitle(self, target: Path) -> Path | None:
        """查找严格关联的宿主标准简中外挂字幕。"""

    async def write_media_subtitle(self, source: Path, target: Path) -> Path:
        """以排他方式把字幕落到目标视频旁。"""

    async def target_directory_status(self, target: Path) -> tuple[bool, str | None]:
        """检查目标文件父目录是否可用于字幕落盘。"""

    async def save_plugin_file(self, source: Path, record_id: str, status: RecordStatus) -> str:
        """保存暂存或未匹配文件并返回相对路径。"""

    async def delete_plugin_file(self, relative_path: str) -> None:
        """只删除插件数据目录内的记录文件。"""

    async def delete_subtitle_file(self, path: Path) -> None:
        """删除一个精确字幕文件，目标不存在视为成功。"""

    async def stage_file_deletion(self, path: Path) -> Path | None:
        """把待删除普通文件移到可回滚的临时备份并返回备份路径。"""

    async def commit_file_deletion(self, backup: Path | None) -> None:
        """提交文件删除并清理临时备份。"""

    async def rollback_file_deletion(self, original: Path, backup: Path | None) -> None:
        """恢复一次尚未提交的文件删除。"""

    async def plugin_file_path(self, relative_path: str) -> Path:
        """安全解析插件数据目录内的相对路径。"""

    async def make_task_directory(self, task_id: str) -> Path:
        """创建当前任务独立临时目录。"""

    async def cleanup_task_directory(self, task_id: str) -> None:
        """删除当前任务临时目录。"""


class ArchivePort(Protocol):
    """统一归档解包协议。"""

    async def extract(
        self,
        asset: DownloadedAsset,
        output: Path,
        allowed_formats: set[str],
    ) -> list[ExtractedSubtitle]:
        """通过 unar 解包或返回直链字幕文件。"""

    async def cancel(self) -> None:
        """终止当前正在运行的 unar 子进程。"""


class MediaMatcherPort(Protocol):
    """MoviePilot 公共媒体识别规则适配协议。"""

    def normalize_candidate(
        self,
        candidate: SubtitleCandidate,
        context: MediaContext,
        host_mediainfo: HostMediaInfo,
    ) -> SubtitleCandidate | None:
        """使用宿主规则确认候选归属并补充包范围。"""

    def recognize_candidate(
        self,
        candidate: SubtitleCandidate,
        context: MediaContext,
        host_mediainfo: HostMediaInfo | None,
    ) -> CandidateRecognition:
        """使用宿主规则识别人工候选，但不执行自动准入过滤。"""

    def candidate_snapshot(
        self,
        candidate: SubtitleCandidate,
    ) -> CandidateAttributionSnapshot:
        """只根据候选自身字段形成不可变归属快照。"""

    async def attribute_file(
        self,
        path: Path,
        logical_source_path: str,
        context: MediaContext,
        snapshot: CandidateAttributionSnapshot,
        strategy: PackageAttributionStrategy,
    ) -> FileAttributionEvidence:
        """按任务策略识别一个包内具体字幕文件。"""
