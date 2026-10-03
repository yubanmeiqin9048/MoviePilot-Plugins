"""Alist2Strm 手动扫描任务、OpenList 读取边界与 STRM 产物。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from app.sdk.config import settings
from app.sdk.logging import logger

from .config import PluginConfiguration

FS_LIST_PATH = "/api/fs/list"
LIST_PER_PAGE = 100
DOWNLOAD_PREFIX = "/d"
STRM_SUFFIX = ".strm"


@dataclass(frozen=True)
class HttpResponse:
    """一次 OpenList HTTP 请求的最小响应投影。"""

    status_code: int
    payload: object = None


class HttpTransport(Protocol):
    """可替换的 OpenList JSON 传输边界。"""

    def post_json(self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> HttpResponse:
        """发送 JSON POST 并返回状态码及已解析响应体。"""


class HostHttpTransport:
    """使用宿主公开 RequestUtils 的 OpenList 传输实现。"""

    def __init__(self, *, timeout: int = 20, proxies: Mapping[str, str] | None = None) -> None:
        """保存超时与代理设置。"""

        self._timeout = timeout
        self._proxies = dict(proxies) if proxies else None

    def post_json(self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> HttpResponse:
        """发送一次 JSON POST；管理员 Token 仅由调用方传入请求头。"""

        from app.sdk.network import RequestUtils

        request = RequestUtils(headers=dict(headers), timeout=self._timeout, proxies=self._proxies)
        try:
            response = request.post_res(url, json=dict(payload))
        except Exception as error:
            raise OpenListError("OpenList 请求失败", retryable=True) from error
        if response is None:
            return HttpResponse(status_code=0)
        try:
            body: object = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应由客户端处理
            body = None
        return HttpResponse(status_code=int(getattr(response, "status_code", 0) or 0), payload=body)


def default_transport() -> HttpTransport:
    """返回使用宿主网络设置的默认传输边界。"""

    proxies = getattr(settings, "PROXY", None)
    return HostHttpTransport(proxies=proxies if isinstance(proxies, Mapping) else None)


class OpenListError(RuntimeError):
    """OpenList 读取失败，错误信息不包含管理员 Token。"""

    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None) -> None:
        """记录脱敏错误及可重试标记。"""

        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


@dataclass(frozen=True)
class RemoteEntry:
    """OpenList 目录项的最小投影。"""

    name: str
    is_dir: bool


class OpenListClient:
    """通过管理员 API Token 递归扫描所需的 OpenList 只读客户端。"""

    def __init__(
        self,
        scan_url: str,
        token: str,
        *,
        transport: HttpTransport | None = None,
        per_page: int = LIST_PER_PAGE,
    ) -> None:
        """绑定扫描地址、Token 和可替换的 HTTP 传输边界。"""

        self._scan_url = scan_url.rstrip("/")
        self._token = token
        self._transport = transport if transport is not None else default_transport()
        self._per_page = max(int(per_page), 1)

    def _headers(self) -> dict[str, str]:
        """返回 OpenList 要求的原始 Token 头，不添加 Bearer。"""

        return {"Authorization": self._token}

    def _post_list(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """请求一页目录并检查 HTTP 与 OpenList 业务响应。"""

        try:
            response = self._transport.post_json(
                f"{self._scan_url}{FS_LIST_PATH}",
                payload,
                self._headers(),
            )
        except OpenListError:
            raise
        except Exception as error:
            raise OpenListError("OpenList 请求失败", retryable=True) from error
        if response.status_code == 0:
            raise OpenListError("OpenList 无响应", retryable=True)
        if response.status_code >= 500:
            raise OpenListError(f"OpenList 服务端错误（HTTP {response.status_code}）", retryable=True)
        if response.status_code >= 400:
            raise OpenListError(f"OpenList 请求被拒绝（HTTP {response.status_code}）", retryable=False)
        body = response.payload
        if not isinstance(body, Mapping):
            raise OpenListError("OpenList 响应格式无效", retryable=True)
        code = body.get("code")
        if isinstance(code, bool) or str(code) != "200":
            raise OpenListError("OpenList 业务请求失败", retryable=True)
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise OpenListError("OpenList 目录响应格式无效", retryable=True)
        return data

    def list_dir(self, remote_path: str) -> list[RemoteEntry]:
        """完整读取一个全站绝对目录，不限制页数并支持空目录。"""

        if not remote_path.startswith("/"):
            raise OpenListError("源目录必须是全站绝对路径", retryable=False)
        entries: list[RemoteEntry] = []
        page = 1
        collected = 0
        while True:
            data = self._post_list(
                {
                    "path": remote_path,
                    "password": "",
                    "refresh": False,
                    "page": page,
                    "per_page": self._per_page,
                }
            )
            content = data.get("content")
            if not isinstance(content, list):
                raise OpenListError("OpenList 目录响应缺少 content", retryable=True)
            for item in content:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                entries.append(RemoteEntry(name=name, is_dir=bool(item.get("is_dir"))))
            collected += len(content)
            total = data.get("total")
            if not content:
                break
            if isinstance(total, int) and not isinstance(total, bool) and collected >= total:
                break
            page += 1
        return entries


def encode_remote_path(remote_path: str) -> str:
    """按路径段编码绝对源路径，保留分隔符且不编码整条 URL。"""

    if not remote_path.startswith("/"):
        raise ValueError("远端路径必须是全站绝对路径")
    segments = remote_path.split("/")
    return "/".join("" if index == 0 else quote(segment, safe="") for index, segment in enumerate(segments))


def build_playback_url(playback_base: str, remote_path: str) -> str:
    """拼出无签名 OpenList ``/d`` 播放链接，并丢弃地址中的凭据与查询部分。"""

    parsed = urlsplit(playback_base)
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        safe_base = urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path.rstrip("/"), "", ""))
    else:
        safe_base = playback_base.rstrip("/")
    return f"{safe_base.rstrip('/')}{DOWNLOAD_PREFIX}{encode_remote_path(remote_path)}"


def is_video_name(name: str, suffixes: frozenset[str]) -> bool:
    """按文件最后一个扩展名匹配宿主媒体后缀，且忽略大小写。"""

    suffix = PurePosixPath(name).suffix.casefold()
    return bool(suffix) and suffix in suffixes


def local_strm_path(output_dir: Path, source_dir: str, remote_path: str) -> Path:
    """保留源目录下相对结构，仅替换最后扩展名为 ``.strm``。"""

    try:
        relative = PurePosixPath(remote_path).relative_to(PurePosixPath(source_dir))
    except ValueError as error:
        raise ValueError("远端文件不在映射源目录下") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("远端文件相对路径无效")
    return Path(output_dir).joinpath(*relative.parts).with_suffix(STRM_SUFFIX)


@dataclass
class ScanCounters:
    """扫描任务的六类可扩展产物计数。"""

    generated: int = 0
    downloaded: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: int = 0

    def copy(self) -> ScanCounters:
        """返回独立计数快照。"""

        return ScanCounters(**self.as_dict())

    def as_dict(self) -> dict[str, int]:
        """返回稳定的 JSON 计数对象。"""

        return {
            "generated": self.generated,
            "downloaded": self.downloaded,
            "updated": self.updated,
            "skipped": self.skipped,
            "deleted": self.deleted,
            "failed": self.failed,
        }

    def add(self, other: ScanCounters) -> None:
        """把一组映射计数累加到任务计数。"""

        for name, value in other.as_dict().items():
            setattr(self, name, getattr(self, name) + value)


@dataclass
class ScanTarget:
    """单组运行目标，可用映射序号或源目录定位。"""

    instance_id: str
    mapping_index: int | None = None
    source_dir: str | None = None


@dataclass
class ScanTriggerResult:
    """一次手动触发的安全结果，不把配置或凭据带回前端。"""

    accepted: bool
    message: str

    def as_dict(self) -> dict[str, object]:
        """返回 API JSON 结果。"""

        return {"accepted": self.accepted, "message": self.message}

    def __getitem__(self, key: str) -> object:
        """支持测试与调用方按 JSON 字段读取结果。"""

        return self.as_dict()[key]


@dataclass
class MappingScanStatus:
    """单组映射的运行状态与计数。"""

    instance_id: str
    instance_name: str
    mapping_index: int
    source_dir: str
    output_dir: str
    state: str = "pending"
    counters: ScanCounters = field(default_factory=ScanCounters)
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """返回稳定的映射状态对象。"""

        return {
            "instance_id": self.instance_id,
            "instance_name": self.instance_name,
            "mapping_index": self.mapping_index,
            "source_dir": self.source_dir,
            "output_dir": self.output_dir,
            "state": self.state,
            "status": self.state,
            "counters": self.counters.as_dict(),
            "error": self.error,
        }


@dataclass
class ScanStatus:
    """详情弹窗轮询使用的任务状态对象。"""

    state: str = "idle"
    running: bool = False
    current_mapping: str | None = None
    counters: ScanCounters = field(default_factory=ScanCounters)
    mappings: list[MappingScanStatus] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def status(self) -> str:
        """兼容前端和后续历史扩展使用的状态别名。"""

        return self.state

    def as_dict(self) -> dict[str, object]:
        """返回稳定且可扩展的状态 JSON。"""

        return {
            "state": self.state,
            "status": self.state,
            "running": self.running,
            "current_mapping": self.current_mapping,
            "counters": self.counters.as_dict(),
            "mappings": [mapping.as_dict() for mapping in self.mappings],
            "total_mappings": len(self.mappings),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def __getitem__(self, key: str) -> object:
        """支持按 JSON 字段读取状态。"""

        return self.as_dict()[key]


@dataclass
class _MappingRun:
    """任务启动时捕获的一组映射配置。"""

    instance_id: str
    instance_name: str
    scan_url: str
    playback_url: str
    token: str
    source_dir: str
    output_dir: Path
    mapping_index: int
    exclude_keywords: tuple[str, ...]
    status: MappingScanStatus

    @property
    def label(self) -> str:
        """返回当前映射的安全展示标签。"""

        return f"{self.instance_name}：映射 {self.mapping_index}（{self.source_dir}）"


@dataclass
class _ScanRun:
    """单次运行的不可变配置快照与可变进度。"""

    mappings: list[_MappingRun]
    video_suffixes: frozenset[str]
    started_at: float
    state: str = "running"
    current_mapping: str | None = None
    finished_at: float | None = None


class ScanCoordinator:
    """协调全插件唯一的手动扫描任务，并暴露高层触发与状态 seam。"""

    def __init__(
        self,
        configuration_provider: Callable[[], PluginConfiguration | None],
        *,
        plugin_id: str = "Alist2Strm",
        dispatch: Callable[[Callable[[], None]], bool] | None = None,
        client_factory: Callable[[str, str], Any] | None = None,
        media_suffix_provider: Callable[[], Sequence[str]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """绑定已保存配置、后台提交器和可替换 OpenList I/O。"""

        self._configuration_provider = configuration_provider
        self._plugin_id = plugin_id
        self._dispatch = dispatch if dispatch is not None else self._dispatch_with_host_scheduler
        self._client_factory = client_factory if client_factory is not None else self._default_client_factory
        self._media_suffix_provider = (
            media_suffix_provider if media_suffix_provider is not None else self._host_suffixes
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._run: _ScanRun | None = None

    def _dispatch_with_host_scheduler(self, task: Callable[[], None]) -> bool:
        """通过宿主一次性任务注册扫描，不创建插件私有线程池。"""

        from app.sdk.scheduler import add_plugin_once_job

        return bool(add_plugin_once_job(self._plugin_id, "manual-scan", task, "Alist2Strm 手动扫描"))

    @staticmethod
    def _default_client_factory(scan_url: str, token: str) -> OpenListClient:
        """构造默认 OpenList 只读客户端。"""

        return OpenListClient(scan_url, token)

    @staticmethod
    def _host_suffixes() -> Sequence[str]:
        """读取宿主当前媒体后缀设置。"""

        return getattr(settings, "RMT_MEDIAEXT", ())

    def trigger(self, target: ScanTarget | None = None) -> ScanTriggerResult:
        """捕获已保存配置并提交一次后台扫描；运行中触发不排队。"""

        with self._lock:
            if self._run is not None and self._run.state == "running":
                return ScanTriggerResult(False, "扫描正在运行，已跳过本次触发")
            run_or_error = self._build_run(target)
            if isinstance(run_or_error, ScanTriggerResult):
                return run_or_error
            self._run = run_or_error
        try:
            accepted = self._dispatch(self.execute)
        except Exception:  # noqa: BLE001 - 调度边界失败不把后台细节返回前端
            accepted = False
        if not accepted:
            with self._lock:
                self._run = None
            return ScanTriggerResult(False, "后台任务调度不可用，扫描未启动")
        return ScanTriggerResult(True, "扫描已在后台启动")

    def status(self) -> ScanStatus:
        """返回当前或最近一次任务的生命周期、映射和六类计数。"""

        with self._lock:
            run = self._run
            if run is None:
                return ScanStatus()
            counters = ScanCounters()
            for mapping in run.mappings:
                counters.add(mapping.status.counters)
            state = run.state
            return ScanStatus(
                state=state,
                running=state == "running",
                current_mapping=run.current_mapping,
                counters=counters,
                mappings=[
                    MappingScanStatus(
                        instance_id=item.status.instance_id,
                        instance_name=item.status.instance_name,
                        mapping_index=item.status.mapping_index,
                        source_dir=item.status.source_dir,
                        output_dir=item.status.output_dir,
                        state=item.status.state,
                        counters=item.status.counters.copy(),
                        error=item.status.error,
                    )
                    for item in run.mappings
                ],
                started_at=_timestamp(run.started_at),
                finished_at=_timestamp(run.finished_at),
            )

    def execute(self) -> None:
        """执行已捕获的配置快照，并保证任务最终进入稳定状态。"""

        with self._lock:
            run = self._run
        if run is None:
            return
        try:
            self._execute_mappings(run)
        except Exception:  # noqa: BLE001 - 后台任务不得把异常传播回调度器
            logger.error("Alist2Strm 扫描任务异常终止")
            with self._lock:
                for mapping in run.mappings:
                    if mapping.status.state in {"pending", "running"}:
                        mapping.status.state = "failed"
                        mapping.status.error = "扫描任务异常终止"
                        mapping.status.counters.failed += 1
        finally:
            with self._lock:
                if any(item.status.state == "failed" for item in run.mappings):
                    run.state = "failed"
                else:
                    run.state = "success"
                run.current_mapping = None
                run.finished_at = self._clock()

    def _build_run(self, target: ScanTarget | None) -> _ScanRun | ScanTriggerResult:
        """从当前已保存配置构造深拷贝快照，不读取浏览器草稿。"""

        try:
            configuration = self._configuration_provider()
        except Exception:  # noqa: BLE001 - 无效配置按不可运行处理
            configuration = None
        if not isinstance(configuration, PluginConfiguration):
            return ScanTriggerResult(False, "没有可运行的已保存配置")
        snapshot = configuration.model_copy(deep=True)
        mappings: list[_MappingRun] = []
        for instance in snapshot.instances:
            for mapping_index, mapping in enumerate(instance.mappings, start=1):
                if target is not None and instance.id != target.instance_id:
                    continue
                if target is not None and target.mapping_index is not None and mapping_index != target.mapping_index:
                    continue
                if target is not None and target.mapping_index is None and target.source_dir != mapping.source_dir:
                    continue
                status = MappingScanStatus(
                    instance_id=instance.id,
                    instance_name=instance.name,
                    mapping_index=mapping_index,
                    source_dir=mapping.source_dir,
                    output_dir=mapping.output_dir,
                )
                mappings.append(
                    _MappingRun(
                        instance_id=instance.id,
                        instance_name=instance.name,
                        scan_url=instance.scan_url,
                        playback_url=instance.effective_playback_url,
                        token=instance.api_token,
                        source_dir=mapping.source_dir,
                        output_dir=Path(mapping.output_dir),
                        mapping_index=mapping_index,
                        exclude_keywords=tuple(mapping.exclude_keywords),
                        status=status,
                    )
                )
        if target is not None and not mappings:
            return ScanTriggerResult(False, "未找到指定的目录映射")
        if not mappings:
            return ScanTriggerResult(False, "没有可运行的目录映射")
        suffixes: set[str] = set()
        try:
            values = self._media_suffix_provider()
        except Exception:  # noqa: BLE001 - 宿主设置异常按无视频后缀处理
            values = ()
        for value in values:
            suffix = str(value).strip().casefold()
            if suffix and not suffix.startswith("."):
                suffix = f".{suffix}"
            if suffix:
                suffixes.add(suffix)
        return _ScanRun(mappings=mappings, video_suffixes=frozenset(suffixes), started_at=self._clock())

    def _execute_mappings(self, run: _ScanRun) -> None:
        """逐映射执行并隔离实例、目录和本地写入失败。"""

        readers: dict[str, Any] = {}
        for mapping in run.mappings:
            with self._lock:
                mapping.status.state = "running"
                run.current_mapping = mapping.label
            try:
                reader = readers.get(mapping.instance_id)
                if reader is None:
                    if not mapping.token:
                        raise OpenListError("实例未配置管理员 API Token", retryable=False)
                    reader = self._client_factory(mapping.scan_url, mapping.token)
                    readers[mapping.instance_id] = reader
                self._scan_mapping(reader, mapping, run.video_suffixes)
                if mapping.status.counters.failed:
                    mapping.status.state = "failed"
                    mapping.status.error = "映射内存在失败操作"
                else:
                    mapping.status.state = "success"
            except OpenListError as error:
                self._mark_mapping_failed(mapping, error.message)
                logger.warning(f"Alist2Strm 映射扫描失败：{mapping.instance_name} / {mapping.source_dir}")
            except (OSError, ValueError) as error:
                self._mark_mapping_failed(mapping, str(error))
                logger.warning(f"Alist2Strm 映射产物处理失败：{mapping.instance_name} / {mapping.source_dir}")
            except Exception:  # noqa: BLE001 - 单组不可用不得阻断独立映射
                self._mark_mapping_failed(mapping, "映射扫描失败")
                logger.warning(f"Alist2Strm 映射扫描失败：{mapping.instance_name} / {mapping.source_dir}")
            finally:
                with self._lock:
                    run.current_mapping = None

    @staticmethod
    def _mark_mapping_failed(mapping: _MappingRun, error: str) -> None:
        """设置脱敏映射失败结果并保留已完成计数。"""

        safe_error = error or "映射扫描失败"
        mapping.status.state = "failed"
        mapping.status.error = safe_error.replace(mapping.token, "<redacted>") if mapping.token else safe_error
        mapping.status.counters.failed += 1

    def _scan_mapping(self, reader: Any, mapping: _MappingRun, suffixes: frozenset[str]) -> None:
        """递归读取源目录并生成视频 STRM；不下载附件或清理输出。"""

        pending = [mapping.source_dir]
        while pending:
            current = pending.pop()
            entries = reader.list_dir(current)
            for raw_entry in entries:
                entry = self._entry(raw_entry)
                if entry is None or self._excluded(entry.name, mapping.exclude_keywords):
                    continue
                remote_path = _join_remote_path(current, entry.name)
                if entry.is_dir:
                    pending.append(remote_path)
                    continue
                if not is_video_name(entry.name, suffixes):
                    continue
                target = local_strm_path(mapping.output_dir, mapping.source_dir, remote_path)
                if target.exists():
                    mapping.status.counters.skipped += 1
                    continue
                content = build_playback_url(mapping.playback_url, remote_path)
                self._write_new_target(target, content, mapping.status.counters)

    @staticmethod
    def _entry(raw_entry: object) -> RemoteEntry | None:
        """把注入替身或 OpenList 客户端返回值收敛为目录项。"""

        if isinstance(raw_entry, RemoteEntry):
            return raw_entry
        if isinstance(raw_entry, Mapping):
            name = raw_entry.get("name")
            if isinstance(name, str) and name:
                return RemoteEntry(name=name, is_dir=bool(raw_entry.get("is_dir")))
        return None

    @staticmethod
    def _excluded(name: str, keywords: tuple[str, ...]) -> bool:
        """按名称包含关系判断排除项，目录命中时由调用方跳过子树。"""

        folded = name.casefold()
        return any(keyword.casefold() in folded for keyword in keywords)

    @staticmethod
    def _write_new_target(target: Path, content: str, counters: ScanCounters) -> None:
        """以临时文件和原子创建写入新目标，避免半成品被视为完整文件。"""

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    counters.skipped += 1
                    return
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            counters.failed += 1
            return
        counters.generated += 1


def _join_remote_path(parent: str, name: str) -> str:
    """在全站绝对路径下拼接一个 OpenList 条目名称。"""

    return f"{parent.rstrip('/')}/{name.lstrip('/')}" if parent != "/" else f"/{name.lstrip('/')}"


def _timestamp(value: float | None) -> str | None:
    """把运行时钟转换为 UTC ISO 时间。"""

    return datetime.fromtimestamp(value, tz=UTC).isoformat() if value is not None else None


__all__ = [
    "DOWNLOAD_PREFIX",
    "FS_LIST_PATH",
    "LIST_PER_PAGE",
    "STRM_SUFFIX",
    "HostHttpTransport",
    "HttpResponse",
    "HttpTransport",
    "MappingScanStatus",
    "OpenListClient",
    "OpenListError",
    "RemoteEntry",
    "ScanCoordinator",
    "ScanCounters",
    "ScanStatus",
    "ScanTarget",
    "ScanTriggerResult",
    "build_playback_url",
    "default_transport",
    "encode_remote_path",
    "is_video_name",
    "local_strm_path",
]
