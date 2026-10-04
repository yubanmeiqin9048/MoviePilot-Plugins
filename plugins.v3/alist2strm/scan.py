"""Alist2Strm 手动扫描任务、OpenList 读取边界与 STRM 产物。"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from app.sdk.config import settings
from app.sdk.logging import logger

from .config import PluginConfiguration

FS_LIST_PATH = "/api/fs/list"
LIST_PER_PAGE = 100
DOWNLOAD_PREFIX = "/d"
STRM_SUFFIX = ".strm"
MAX_DOWNLOAD_REDIRECTS = 10
MAX_RETRIES = 2
DEFAULT_RETRY_DELAY = 0.1


class ScanStopped(RuntimeError):
    """扫描在当前原子 I/O 操作结束后收到停止请求。"""


@dataclass(frozen=True)
class HttpResponse:
    """一次 OpenList HTTP 请求的最小响应投影。"""

    status_code: int
    payload: object = None


@dataclass
class DownloadResponse:
    """一次受控下载响应，正文以字节迭代器延迟读取。"""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Iterable[bytes] = field(default_factory=tuple)
    url: str = ""
    _close: Callable[[], None] | None = None

    def iter_bytes(self) -> Iterator[bytes]:
        """返回响应正文的字节迭代器。"""

        if isinstance(self.body, bytes):
            return iter((self.body,))
        return iter(self.body)

    def close(self) -> None:
        """关闭底层响应，允许替身记录资源释放。"""

        if self._close is not None:
            close, self._close = self._close, None
            close()


class HttpTransport(Protocol):
    """可替换的 OpenList JSON 与下载传输边界。"""

    def post_json(self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> HttpResponse:
        """发送 JSON POST 并返回状态码及已解析响应体。"""

    def get_stream(self, url: str, headers: Mapping[str, str]) -> DownloadResponse:
        """发送不自动跟随重定向的流式 GET。"""


class OutputFileSystem(Protocol):
    """可替换的本地产物文件系统边界。"""

    def exists(self, target: Path) -> bool:
        """判断目标是否已经存在。"""

    def write_text_atomic(self, target: Path, content: str) -> bool:
        """原子创建文本目标，返回是否由本次调用创建。"""

    def replace_text_atomic(self, target: Path, content: str) -> None:
        """原子替换已有文本目标，失败时保留原目标。"""

    def write_bytes_atomic(self, target: Path, chunks: Iterable[bytes]) -> bool:
        """原子创建二进制目标，返回是否由本次调用创建。"""

    def iter_files(self, root: Path) -> Iterable[Path]:
        """枚举当前输出根目录下的文件。"""

    def unlink(self, target: Path) -> None:
        """删除一个受管产物文件。"""


class LocalFileSystem:
    """使用本地临时文件和硬链接完成原子创建的默认边界。"""

    def exists(self, target: Path) -> bool:
        """判断本地目标是否存在。"""

        return target.exists()

    def write_text_atomic(self, target: Path, content: str) -> bool:
        """完整写入并原子创建文本目标。"""

        return self._write_atomic(target, lambda stream: stream.write(content), text=True)

    def replace_text_atomic(self, target: Path, content: str) -> None:
        """完整写入临时目标后原子替换已有文本目标。"""

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
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def write_bytes_atomic(self, target: Path, chunks: Iterable[bytes]) -> bool:
        """流式写入并原子创建二进制目标。"""

        def write(stream: Any) -> None:
            """把下载块写入临时文件。"""

            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("下载响应必须返回字节块")
                stream.write(chunk)

        return self._write_atomic(target, write, text=False)

    def iter_files(self, root: Path) -> Iterable[Path]:
        """显式递归枚举输出根目录下的文件并传播目录或 stat 错误。"""

        try:
            os.stat(root)
        except FileNotFoundError, NotADirectoryError:
            return

        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    entry_path = Path(entry.path)
                    entry_stat = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(entry_stat.st_mode):
                        pending.append(entry_path)
                        continue
                    if stat.S_ISREG(entry_stat.st_mode):
                        yield entry_path
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode):
                        target_stat = entry.stat(follow_symlinks=True)
                        if stat.S_ISREG(target_stat.st_mode):
                            yield entry_path

    def unlink(self, target: Path) -> None:
        """删除一个本地产物文件。"""

        target.unlink()

    @staticmethod
    def _write_atomic(target: Path, writer: Callable[[Any], None], *, text: bool) -> bool:
        """在同一目录完成临时写入和原子创建。"""

        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            mode = "w" if text else "wb"
            with os.fdopen(
                descriptor, mode, encoding="utf-8" if text else None, newline="" if text else None
            ) as stream:
                writer(stream)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                return False
            return True
        finally:
            temporary.unlink(missing_ok=True)


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

    def get_stream(self, url: str, headers: Mapping[str, str]) -> DownloadResponse:
        """发送不自动跟随重定向的流式 GET。"""

        from app.sdk.network import RequestUtils

        request = RequestUtils(headers=dict(headers), timeout=self._timeout, proxies=self._proxies)
        try:
            manager = request.get_stream(url, allow_redirects=False)
            response = manager.__enter__()
        except Exception as error:
            raise OpenListError("OpenList 下载请求失败", retryable=True) from error
        if response is None:
            manager.__exit__(None, None, None)
            return DownloadResponse(status_code=0, url=url)

        def close() -> None:
            """释放宿主流式响应和其临时连接池。"""

            manager.__exit__(None, None, None)

        return DownloadResponse(
            status_code=int(getattr(response, "status_code", 0) or 0),
            headers={str(key): str(value) for key, value in getattr(response, "headers", {}).items()},
            body=response.iter_content(chunk_size=1024 * 1024),
            url=str(getattr(response, "url", "") or url),
            _close=close,
        )


def default_transport() -> HttpTransport:
    """返回使用宿主网络设置的默认传输边界。"""

    proxies = getattr(settings, "PROXY", None)
    return HostHttpTransport(proxies=proxies if isinstance(proxies, Mapping) else None)


class OpenListError(RuntimeError):
    """OpenList 读取失败，错误信息不包含管理员 Token。"""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        attempts_exhausted: bool = False,
        auth_failure: bool = False,
    ) -> None:
        """记录脱敏错误、重试分类和认证失败标记。"""

        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.attempts_exhausted = attempts_exhausted
        self.auth_failure = auth_failure or status_code in {401, 403}

    def exhausted(self) -> OpenListError:
        """返回标记为已耗尽重试次数的同类错误。"""

        return OpenListError(
            self.message,
            retryable=self.retryable,
            status_code=self.status_code,
            attempts_exhausted=True,
            auth_failure=self.auth_failure,
        )


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
        sleep: Callable[[float], None] | None = None,
        retry_backoff: Callable[[int], float] | None = None,
        max_retries: int = MAX_RETRIES,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """绑定扫描地址、Token 和可替换的 HTTP、重试与停止边界。"""

        self._scan_url = scan_url.rstrip("/")
        self._token = token
        self._transport = transport if transport is not None else default_transport()
        self._per_page = max(int(per_page), 1)
        self._sleep = sleep if sleep is not None else time.sleep
        self._retry_backoff = retry_backoff if retry_backoff is not None else _retry_delay
        self._max_retries = max(int(max_retries), 0)
        self._should_stop = should_stop

    def _headers(self) -> dict[str, str]:
        """返回 OpenList 要求的原始 Token 头，不添加 Bearer。"""

        return {"Authorization": self._token}

    def close(self) -> None:
        """释放传输替身或客户端资源，并丢弃内存中的 Token。"""

        close = getattr(self._transport, "close", None)
        if callable(close):
            close()
        self._token = ""

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
            raise OpenListError(
                f"OpenList 请求被拒绝（HTTP {response.status_code}）",
                retryable=False,
                status_code=response.status_code,
            )
        body = response.payload
        if not isinstance(body, Mapping):
            raise OpenListError("OpenList 响应格式无效", retryable=False)
        code = body.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise OpenListError("OpenList 响应状态无效", retryable=False)
        if code != 200:
            raise OpenListError(
                "OpenList 业务请求失败",
                retryable=code >= 500,
                status_code=code,
            )
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise OpenListError("OpenList 目录响应格式无效", retryable=False)
        return data

    def _post_list_with_retry(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """按页重试临时 OpenList 错误，最多增加两次请求。"""

        for retry_index in range(self._max_retries + 1):
            if self._should_stop is not None and self._should_stop():
                raise ScanStopped()
            try:
                return self._post_list(payload)
            except OpenListError as error:
                if not error.retryable or error.attempts_exhausted or retry_index >= self._max_retries:
                    if error.retryable and retry_index >= self._max_retries:
                        raise error.exhausted() from error
                    raise
                self._sleep(max(float(self._retry_backoff(retry_index + 1)), 0.0))
        raise AssertionError("OpenList 重试循环未返回")

    def list_dir(self, remote_path: str) -> list[RemoteEntry]:
        """完整读取一个全站绝对目录，不限制页数并支持空目录。"""

        if not remote_path.startswith("/"):
            raise OpenListError("源目录必须是全站绝对路径", retryable=False)
        entries: list[RemoteEntry] = []
        page = 1
        collected = 0
        while True:
            data = self._post_list_with_retry(
                {
                    "path": remote_path,
                    "password": "",
                    "refresh": False,
                    "page": page,
                    "per_page": self._per_page,
                }
            )
            content = data.get("content")
            total = data.get("total")
            if not isinstance(content, list) or isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise OpenListError("OpenList 目录响应格式无效", retryable=False)
            for item in content:
                if not isinstance(item, Mapping):
                    raise OpenListError("OpenList 目录项格式无效", retryable=False)
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    raise OpenListError("OpenList 目录项名称无效", retryable=False)
                entries.append(RemoteEntry(name=name, is_dir=bool(item.get("is_dir"))))
            collected += len(content)
            if collected >= total:
                break
            if not content:
                raise OpenListError("OpenList 分页响应不完整", retryable=False)
            page += 1
        return entries

    @contextmanager
    def open_download(self, remote_path: str) -> Iterator[Iterable[bytes]]:
        """打开一个无签名下载流，并在跨源重定向时移除管理员 Token。"""

        if not remote_path.startswith("/"):
            raise OpenListError("下载路径必须是全站绝对路径", retryable=False)
        url = build_download_url(self._scan_url, remote_path)
        headers: Mapping[str, str] = self._headers()
        response: DownloadResponse | None = None
        for _ in range(MAX_DOWNLOAD_REDIRECTS + 1):
            if self._should_stop is not None and self._should_stop():
                raise ScanStopped()
            try:
                response = self._transport.get_stream(url, headers)
            except OpenListError:
                raise
            except Exception as error:
                raise OpenListError("OpenList 下载请求失败", retryable=True) from error
            if response.status_code in {301, 302, 303, 307, 308}:
                location = _response_header(response.headers, "location")
                response.close()
                response = None
                if not location:
                    raise OpenListError("OpenList 下载重定向缺少目标", retryable=False)
                next_url = _strip_url_credentials(urljoin(url, location))
                headers = self._headers() if _same_origin(url, next_url) else {}
                url = next_url
                continue
            if 200 <= response.status_code < 300:
                response_for_body = response

                def iter_response(response: DownloadResponse = response_for_body) -> Iterator[bytes]:
                    """读取响应正文并将远端流中断归类为可重试错误。"""

                    try:
                        yield from response.iter_bytes()
                    except OpenListError:
                        raise
                    except Exception as error:
                        raise OpenListError("OpenList 下载流读取失败", retryable=True) from error

                try:
                    yield iter_response()
                finally:
                    response.close()
                return
            status_code = response.status_code
            response.close()
            response = None
            retryable = status_code == 0 or status_code >= 500
            raise OpenListError(
                f"OpenList 下载失败（HTTP {status_code}）",
                retryable=retryable,
                status_code=status_code,
            )
        if response is not None:
            response.close()
        raise OpenListError("OpenList 下载重定向过多", retryable=False)


def _retry_delay(retry_number: int) -> float:
    """返回短暂且可预测的默认退避时长。"""

    return DEFAULT_RETRY_DELAY * (2 ** max(retry_number - 1, 0))


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


def build_download_url(scan_base: str, remote_path: str) -> str:
    """拼出扫描地址下的无签名附件下载链接。"""

    return build_playback_url(scan_base, remote_path)


def _response_header(headers: Mapping[str, str], name: str) -> str | None:
    """不区分大小写读取一个 HTTP 响应头。"""

    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def _strip_url_credentials(url: str) -> str:
    """移除重定向地址中的用户信息，避免凭据进入后续 URL。"""

    parsed = urlsplit(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    return urlunsplit((parsed.scheme, f"{hostname}{port}", parsed.path, parsed.query, parsed.fragment))


def _same_origin(first: str, second: str) -> bool:
    """判断两个下载地址是否属于同一协议、主机和端口。"""

    first_parts = urlsplit(first)
    second_parts = urlsplit(second)
    if not first_parts.scheme or not first_parts.netloc or not second_parts.scheme or not second_parts.netloc:
        return False
    try:
        first_port = first_parts.port or (443 if first_parts.scheme.casefold() == "https" else 80)
        second_port = second_parts.port or (443 if second_parts.scheme.casefold() == "https" else 80)
    except ValueError:
        return False
    return (
        first_parts.scheme.casefold() == second_parts.scheme.casefold()
        and (first_parts.hostname or "").casefold() == (second_parts.hostname or "").casefold()
        and first_port == second_port
    )


def is_video_name(name: str, suffixes: frozenset[str]) -> bool:
    """按文件最后一个扩展名匹配宿主媒体后缀，且忽略大小写。"""

    suffix = PurePosixPath(name).suffix.casefold()
    return bool(suffix) and suffix in suffixes


def local_strm_path(output_dir: Path, source_dir: str, remote_path: str) -> Path:
    """保留源目录下相对结构，仅替换最后扩展名为 ``.strm``。"""

    return local_file_path(output_dir, source_dir, remote_path).with_suffix(STRM_SUFFIX)


def local_file_path(output_dir: Path, source_dir: str, remote_path: str) -> Path:
    """保留源目录下相对结构，原样返回附件目标路径。"""

    try:
        relative = PurePosixPath(remote_path).relative_to(PurePosixPath(source_dir))
    except ValueError as error:
        raise ValueError("远端文件不在映射源目录下") from error
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("远端文件相对路径无效")
    return Path(output_dir).joinpath(*relative.parts)


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
    cleanup_eligible: bool = False

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
            "cleanup_eligible": self.cleanup_eligible,
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
    run_id: str | None = None

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
            "run_id": self.run_id,
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
    allowed_suffixes: frozenset[str]
    exclude_keywords: tuple[str, ...]
    status: MappingScanStatus
    playback_changed: bool = False
    expected_outputs: set[Path] = field(default_factory=set)
    expected_output_identities: set[Path] = field(default_factory=set)
    expected_output_resolution_failed: bool = False

    @property
    def identity(self) -> tuple[str, str, str]:
        """返回用于比较有效播放地址的稳定映射身份。"""

        return (self.instance_id, self.source_dir, str(self.output_dir))

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
    run_id: str
    cleanup_disqualified: bool = False
    state: str = "running"
    current_mapping: str | None = None
    finished_at: float | None = None
    stop_requested: bool = False
    started: bool = False


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
        whitelist_suffix_provider: Callable[[], Sequence[str]] | None = None,
        filesystem: OutputFileSystem | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
        retry_backoff: Callable[[int], float] | None = None,
        max_retries: int = MAX_RETRIES,
        history_callback: Callable[[ScanStatus], None] | None = None,
        history_discard: Callable[[str], None] | None = None,
    ) -> None:
        """绑定已保存配置、后台提交器和可替换 OpenList、退避与资源边界。"""

        self._configuration_provider = configuration_provider
        self._plugin_id = plugin_id
        self._dispatch = dispatch if dispatch is not None else self._dispatch_with_host_scheduler
        self._client_factory = client_factory if client_factory is not None else self._default_client_factory
        self._media_suffix_provider = (
            media_suffix_provider if media_suffix_provider is not None else self._host_suffixes
        )
        self._whitelist_suffix_provider = (
            whitelist_suffix_provider if whitelist_suffix_provider is not None else self._host_whitelist_suffixes
        )
        self._filesystem = filesystem if filesystem is not None else LocalFileSystem()
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._retry_backoff = retry_backoff if retry_backoff is not None else _retry_delay
        self._max_retries = max(int(max_retries), 0)
        self._history_callback = history_callback
        self._history_discard = history_discard
        self._lock = threading.Lock()
        self._run: _ScanRun | None = None
        self._active_readers: list[Any] = []
        self._last_playback_urls: dict[tuple[str, str, str], str] = {}

    def _dispatch_with_host_scheduler(self, task: Callable[[], None]) -> bool:
        """通过宿主一次性任务注册扫描，不创建插件私有线程池。"""

        from app.sdk.scheduler import add_plugin_once_job

        return bool(add_plugin_once_job(self._plugin_id, "manual-scan", task, "Alist2Strm 手动扫描"))

    def _default_client_factory(self, scan_url: str, token: str) -> OpenListClient:
        """构造带当前任务重试和停止边界的默认 OpenList 客户端。"""

        with self._lock:
            run = self._run
        return OpenListClient(
            scan_url,
            token,
            sleep=self._sleep,
            retry_backoff=self._retry_backoff,
            max_retries=self._max_retries,
            should_stop=(lambda: self._is_stop_requested(run)) if run is not None else None,
        )

    @staticmethod
    def _host_suffixes() -> Sequence[str]:
        """读取宿主当前媒体后缀设置。"""

        return getattr(settings, "RMT_MEDIAEXT", ())

    @staticmethod
    def _host_whitelist_suffixes() -> Sequence[str]:
        """动态读取宿主当前媒体与字幕后缀。"""

        return [*getattr(settings, "RMT_MEDIAEXT", ()), *getattr(settings, "RMT_SUBEXT", ())]

    def trigger(self, target: ScanTarget | None = None) -> ScanTriggerResult:
        """捕获已保存配置并提交一次后台扫描；运行中触发不排队。"""

        with self._lock:
            if self._run is not None and self._run.state in {"running", "stopping"}:
                message = (
                    "扫描正在停止，已跳过本次触发" if self._run.state == "stopping" else "扫描正在运行，已跳过本次触发"
                )
                return ScanTriggerResult(False, message)
            run_or_error = self._build_run(target)
            if isinstance(run_or_error, ScanTriggerResult):
                return run_or_error
            self._run = run_or_error
        # 先保存运行中的初始快照，重启发生在后台回调开始前也能识别中断。
        self._notify_history(run_or_error)
        try:
            accepted = self._dispatch(self.execute)
        except Exception:  # noqa: BLE001 - 调度边界失败不把后台细节返回前端
            accepted = False
        if not accepted:
            with self._lock:
                if self._run is run_or_error:
                    self._run = None
            self._discard_history(run_or_error.run_id)
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
                running=state in {"running", "stopping"},
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
                        cleanup_eligible=item.status.cleanup_eligible,
                    )
                    for item in run.mappings
                ],
                started_at=_timestamp(run.started_at),
                finished_at=_timestamp(run.finished_at),
                run_id=run.run_id,
            )

    def _notify_history(self, run: _ScanRun) -> None:
        """把运行安全快照交给插件历史持久化边界。"""

        callback = self._history_callback
        if callback is None:
            return
        try:
            callback(self._status_for_run(run))
        except Exception:  # noqa: BLE001 - 历史故障不得改变扫描结果
            logger.warning("Alist2Strm 扫描历史回调失败，当前扫描仍继续")

    def _discard_history(self, run_id: str) -> None:
        """删除调度拒绝的初始记录，不影响调度错误返回。"""

        callback = self._history_discard
        if callback is None:
            return
        try:
            callback(run_id)
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖调度结果
            logger.warning("Alist2Strm 被拒绝扫描的历史记录清理失败")

    def _status_for_run(self, run: _ScanRun) -> ScanStatus:
        """返回指定运行的独立状态快照，避免历史回调读取后续运行。"""

        with self._lock:
            counters = ScanCounters()
            for mapping in run.mappings:
                counters.add(mapping.status.counters)
            return ScanStatus(
                run_id=run.run_id,
                state=run.state,
                running=run.state in {"running", "stopping"},
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
                        cleanup_eligible=item.status.cleanup_eligible,
                    )
                    for item in run.mappings
                ],
                started_at=_timestamp(run.started_at),
                finished_at=_timestamp(run.finished_at),
            )

    def stop(self) -> ScanTriggerResult:
        """请求停止当前任务；当前原子 I/O 结束后不再开始下一项。"""

        notify = False
        with self._lock:
            run = self._run
            if run is None or run.state not in {"running", "stopping"}:
                return ScanTriggerResult(False, "当前没有正在运行的扫描")
            run.stop_requested = True
            if run.state == "running":
                run.state = "stopping"
            if not run.started:
                self._mark_pending_stopped_locked(run)
                run.state = "stopped"
                run.current_mapping = None
                run.finished_at = self._clock()
                notify = True
            result = ScanTriggerResult(True, "扫描正在停止")
        if notify:
            self._notify_history(run)
        return result

    def shutdown(self) -> None:
        """请求停止并在没有活动任务时释放已注入的客户端资源。"""

        self.stop()
        with self._lock:
            run = self._run
            readers = list(self._active_readers) if run is None or not run.started else []
            if readers:
                self._active_readers = []
        self._close_resources(readers)

    close = shutdown

    def execute(self) -> None:
        """执行已捕获的配置快照，并保证任务最终进入稳定状态。"""

        with self._lock:
            run = self._run
            if run is not None and run.state == "stopped":
                return
            if run is not None:
                run.started = True
        if run is None:
            return
        try:
            self._execute_mappings(run)
        except ScanStopped:
            pass
        except Exception:  # noqa: BLE001 - 后台任务不得把异常传播回调度器
            logger.error("Alist2Strm 扫描任务异常终止")
            with self._lock:
                if run.stop_requested:
                    self._mark_pending_stopped_locked(run)
                else:
                    for mapping in run.mappings:
                        if mapping.status.state in {"pending", "running"}:
                            mapping.status.state = "failed"
                            mapping.status.error = "扫描任务异常终止"
                            mapping.status.cleanup_eligible = False
                            mapping.status.counters.failed += 1
        finally:
            with self._lock:
                if run.stop_requested:
                    self._mark_pending_stopped_locked(run)
                    run.state = "stopped"
                elif any(item.status.state == "failed" for item in run.mappings):
                    completed = any(
                        item.status.state == "success"
                        or any(
                            getattr(item.status.counters, name) > 0
                            for name in ("generated", "downloaded", "updated", "skipped", "deleted")
                        )
                        for item in run.mappings
                    )
                    run.state = "partial_failure" if completed else "failed"
                else:
                    run.state = "success"
                run.current_mapping = None
                run.finished_at = self._clock()
            self._notify_history(run)

    def _is_stop_requested(self, run: _ScanRun | None) -> bool:
        """读取任务停止标志，供客户端和扫描循环共享。"""

        with self._lock:
            return run is None or run.stop_requested

    @staticmethod
    def _mark_pending_stopped_locked(run: _ScanRun) -> None:
        """在锁内将尚未开始的映射标为停止，不增加失败数量。"""

        for mapping in run.mappings:
            if mapping.status.state in {"pending", "running"}:
                mapping.status.state = "stopped"
                mapping.status.cleanup_eligible = False
                if not mapping.status.error:
                    mapping.status.error = "扫描已停止"

    @staticmethod
    def _close_resources(resources: Sequence[Any]) -> None:
        """尽力关闭注入客户端，不把释放异常传播回后台任务。"""

        seen: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            for method_name in ("close", "release", "shutdown"):
                method = getattr(resource, method_name, None)
                if callable(method):
                    try:
                        method()
                    except Exception:  # noqa: BLE001 - 资源释放不得掩盖扫描结果
                        logger.warning("Alist2Strm 扫描资源释放失败")
                    break

    def _build_run(self, target: ScanTarget | None) -> _ScanRun | ScanTriggerResult:
        """从当前已保存配置构造深拷贝快照，不读取浏览器草稿。"""

        try:
            configuration = self._configuration_provider()
        except Exception:  # noqa: BLE001 - 无效配置按不可运行处理
            configuration = None
        if not isinstance(configuration, PluginConfiguration):
            return ScanTriggerResult(False, "没有可运行的已保存配置")
        snapshot = configuration.model_copy(deep=True)
        video_suffixes = self._normalise_suffixes(self._media_suffix_provider)
        cleanup_disqualified = self._has_overlapping_output_roots(
            Path(mapping.output_dir) for instance in snapshot.instances for mapping in instance.mappings
        )
        try:
            host_whitelist = self._normalise_values(self._whitelist_suffix_provider())
        except Exception:  # noqa: BLE001 - 宿主设置异常按空白名单处理
            host_whitelist = frozenset()
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
                identity = (instance.id, mapping.source_dir, mapping.output_dir)
                effective_playback_url = instance.effective_playback_url
                allowed_suffixes = (
                    host_whitelist
                    if mapping.whitelist_mode == "follow_host"
                    else self._normalise_values(mapping.extensions)
                )
                mappings.append(
                    _MappingRun(
                        instance_id=instance.id,
                        instance_name=instance.name,
                        scan_url=instance.scan_url,
                        playback_url=effective_playback_url,
                        token=instance.api_token,
                        source_dir=mapping.source_dir,
                        output_dir=Path(mapping.output_dir),
                        mapping_index=mapping_index,
                        allowed_suffixes=allowed_suffixes,
                        exclude_keywords=tuple(mapping.exclude_keywords),
                        playback_changed=playback_address_changed(
                            self._last_playback_urls.get(identity),
                            effective_playback_url,
                        ),
                        status=status,
                    )
                )
        if target is not None and not mappings:
            return ScanTriggerResult(False, "未找到指定的目录映射")
        if not mappings:
            return ScanTriggerResult(False, "没有可运行的目录映射")
        return _ScanRun(
            mappings=mappings,
            video_suffixes=video_suffixes,
            started_at=self._clock(),
            run_id=uuid.uuid4().hex,
            cleanup_disqualified=cleanup_disqualified,
        )

    @staticmethod
    def _has_overlapping_output_roots(roots: Iterable[Path]) -> bool:
        """检查所有配置输出根的解析路径，解析失败时保守禁止清理。"""

        resolved: list[Path] = []
        for root in roots:
            try:
                resolved.append(root.resolve(strict=False))
            except OSError, RuntimeError:
                return True
        for index, first in enumerate(resolved):
            for second in resolved[index + 1 :]:
                if first == second or first in second.parents or second in first.parents:
                    return True
        return False

    @classmethod
    def _normalise_suffixes(cls, provider: Callable[[], Sequence[str]]) -> frozenset[str]:
        """读取并规范一组大小写不敏感的文件后缀。"""

        try:
            values = provider()
        except Exception:  # noqa: BLE001 - 宿主设置异常按无视频后缀处理
            values = ()
        return cls._normalise_values(values)

    @staticmethod
    def _normalise_values(values: Sequence[str]) -> frozenset[str]:
        """把后缀序列规范为带点的小写集合。"""

        result: set[str] = set()
        for value in values:
            suffix = str(value).strip().casefold()
            if suffix and not suffix.startswith("."):
                suffix = f".{suffix}"
            if suffix:
                result.add(suffix)
        return frozenset(result)

    def _execute_mappings(self, run: _ScanRun) -> None:
        """逐映射执行并隔离实例、目录和本地写入失败。"""

        readers: dict[str, Any] = {}
        failed_instances: set[str] = set()
        try:
            for mapping in run.mappings:
                if self._is_stop_requested(run):
                    raise ScanStopped()
                with self._lock:
                    mapping.status.state = "running"
                    run.current_mapping = mapping.label
                try:
                    if mapping.instance_id in failed_instances:
                        self._mark_mapping_failed(mapping, "实例认证或权限失败，已跳过该实例剩余映射")
                        continue
                    reader = readers.get(mapping.instance_id)
                    if reader is None:
                        if not mapping.token:
                            raise OpenListError(
                                "实例未配置管理员 API Token",
                                retryable=False,
                                auth_failure=True,
                            )
                        reader = self._client_factory(mapping.scan_url, mapping.token)
                        readers[mapping.instance_id] = reader
                        with self._lock:
                            self._active_readers = list(readers.values())
                    self._scan_mapping(reader, mapping, run.video_suffixes)
                    self._observe_stop(run)
                    if mapping.status.counters.failed:
                        mapping.status.state = "failed"
                        mapping.status.cleanup_eligible = False
                        if not mapping.status.error:
                            mapping.status.error = "映射内存在失败操作"
                    else:
                        mapping.status.cleanup_eligible = True
                        self._cleanup_mapping(mapping, run)
                        self._observe_stop(run)
                        if mapping.status.counters.failed:
                            mapping.status.state = "failed"
                            mapping.status.cleanup_eligible = False
                            if not mapping.status.error:
                                mapping.status.error = "映射内存在失败操作"
                        else:
                            self._remember_playback_identity(mapping)
                            mapping.status.state = "success"
                            mapping.status.cleanup_eligible = (
                                not run.cleanup_disqualified and not mapping.expected_output_resolution_failed
                            )
                except ScanStopped:
                    self._mark_mapping_stopped(mapping)
                    raise
                except OpenListError as error:
                    self._mark_mapping_failed(mapping, error.message)
                    if self._is_auth_failure(error):
                        failed_instances.add(mapping.instance_id)
                    self._log_mapping_failure(mapping)
                except (OSError, ValueError) as error:
                    self._mark_mapping_failed(mapping, str(error))
                    self._log_mapping_failure(mapping)
                except Exception:  # noqa: BLE001 - 单组不可用不得阻断独立映射
                    self._mark_mapping_failed(mapping, "映射扫描失败")
                    self._log_mapping_failure(mapping)
                finally:
                    with self._lock:
                        run.current_mapping = None
                    self._notify_history(run)
        finally:
            with self._lock:
                self._active_readers = []
            self._close_resources(list(readers.values()))

    def _retry_operation(self, operation: Callable[[], Any], run: _ScanRun) -> Any:
        """在 I/O 边界有限重试已分类的临时 OpenList 错误。"""

        for retry_index in range(self._max_retries + 1):
            self._observe_stop(run)
            try:
                return operation()
            except ScanStopped:
                raise
            except OpenListError as error:
                if not error.retryable or error.attempts_exhausted or retry_index >= self._max_retries:
                    if error.retryable and retry_index >= self._max_retries:
                        raise error.exhausted() from error
                    raise
                self._sleep(max(float(self._retry_backoff(retry_index + 1)), 0.0))
        raise AssertionError("扫描重试循环未返回")

    def _observe_stop(self, run: _ScanRun) -> None:
        """在下一项 I/O 前观察停止请求。"""

        if self._is_stop_requested(run):
            raise ScanStopped()

    def _list_directory(self, reader: Any, remote_path: str, run: _ScanRun) -> list[RemoteEntry]:
        """读取一个目录；失败分类由客户端或注入边界提供。"""

        result = self._retry_operation(lambda: reader.list_dir(remote_path), run)
        self._observe_stop(run)
        return result

    def _remember_playback_identity(self, mapping: _MappingRun) -> None:
        """仅在映射无失败时记录本轮已应用的有效播放地址。"""

        with self._lock:
            self._last_playback_urls[mapping.identity] = mapping.playback_url

    @staticmethod
    def _safe_error(mapping: _MappingRun, error: str) -> str:
        """移除映射 Token，保留可定位的错误上下文。"""

        safe_error = error or "映射扫描失败"
        return safe_error.replace(mapping.token, "<redacted>") if mapping.token else safe_error

    @classmethod
    def _record_mapping_error(cls, mapping: _MappingRun, error: str) -> None:
        """记录首个失败位置和原因，避免逐文件历史明细。"""

        safe_error = cls._safe_error(mapping, error)
        if not mapping.status.error:
            mapping.status.error = safe_error

    @classmethod
    def _mark_mapping_failed(cls, mapping: _MappingRun, error: str) -> None:
        """设置脱敏映射失败结果并保留已完成计数。"""

        cls._record_mapping_error(mapping, error)
        mapping.status.state = "failed"
        mapping.status.cleanup_eligible = False
        mapping.status.counters.failed += 1

    @staticmethod
    def _mark_mapping_stopped(mapping: _MappingRun) -> None:
        """保留已完成数量并使停止映射失去清理资格。"""

        mapping.status.state = "stopped"
        mapping.status.cleanup_eligible = False
        if not mapping.status.error:
            mapping.status.error = "扫描已停止"

    @staticmethod
    def _is_auth_failure(error: OpenListError) -> bool:
        """判断实例级认证或权限失败。"""

        return error.auth_failure or error.status_code in {401, 403}

    def _log_mapping_failure(self, mapping: _MappingRun) -> None:
        """记录含实例、映射和安全原因的中文失败日志。"""

        logger.warning(
            f"Alist2Strm 映射扫描失败：{mapping.instance_name} / {mapping.source_dir} / "
            f"{self._safe_error(mapping, mapping.status.error or '映射扫描失败')}"
        )

    def _scan_mapping(self, reader: Any, mapping: _MappingRun, video_suffixes: frozenset[str]) -> None:
        """按任务快照递归筛选并生成视频 STRM 或下载其他白名单文件。"""

        pending = [mapping.source_dir]
        while pending:
            self._observe_stop(self._run_for_mapping(mapping))
            current = pending.pop()
            try:
                entries = self._list_directory(reader, current, self._run_for_mapping(mapping))
            except ScanStopped:
                raise
            except OpenListError as error:
                if self._is_auth_failure(error):
                    raise
                mapping.status.counters.failed += 1
                self._record_mapping_error(mapping, f"目录 {current}：{error.message}")
                continue
            except Exception as error:  # noqa: BLE001 - 单个目录失败不阻断兄弟目录
                mapping.status.counters.failed += 1
                self._record_mapping_error(mapping, f"目录 {current}：目录读取失败（{error}）")
                continue
            for raw_entry in entries:
                run = self._run_for_mapping(mapping)
                self._observe_stop(run)
                try:
                    entry = self._entry(raw_entry)
                    if entry is None:
                        mapping.status.counters.failed += 1
                        self._record_mapping_error(mapping, f"目录 {current}：目录项格式无效")
                        continue
                    if self._excluded(entry.name, mapping.exclude_keywords):
                        continue
                    remote_path = _join_remote_path(current, entry.name)
                    if entry.is_dir:
                        pending.append(remote_path)
                        continue
                    suffix = PurePosixPath(entry.name).suffix.casefold()
                    if not suffix or suffix not in mapping.allowed_suffixes:
                        continue
                    if is_video_name(entry.name, video_suffixes):
                        target = local_strm_path(mapping.output_dir, mapping.source_dir, remote_path)
                        self._remember_expected_output(mapping, target)
                        if self._filesystem.exists(target):
                            if mapping.playback_changed:
                                content = build_playback_url(mapping.playback_url, remote_path)
                                failure = self._update_existing_target(target, content, mapping.status.counters)
                            else:
                                mapping.status.counters.skipped += 1
                                failure = None
                        else:
                            content = build_playback_url(mapping.playback_url, remote_path)
                            failure = self._write_new_target(target, content, mapping.status.counters)
                    else:
                        target = local_file_path(mapping.output_dir, mapping.source_dir, remote_path)
                        self._remember_expected_output(mapping, target)
                        if self._filesystem.exists(target):
                            mapping.status.counters.skipped += 1
                            failure = None
                        else:
                            failure = self._download_target(
                                reader,
                                remote_path,
                                target,
                                mapping.status.counters,
                                run,
                            )
                    if failure:
                        self._record_mapping_error(mapping, f"文件 {remote_path}：{failure}")
                except ScanStopped:
                    raise
                except OpenListError as error:
                    if self._is_auth_failure(error):
                        raise
                    mapping.status.counters.failed += 1
                    self._record_mapping_error(mapping, f"文件处理失败：{error.message}")
                except Exception as error:  # noqa: BLE001 - 单文件失败不得阻断同目录文件
                    mapping.status.counters.failed += 1
                    self._record_mapping_error(mapping, f"文件处理失败：{error}")

    def _remember_expected_output(self, mapping: _MappingRun, target: Path) -> None:
        """记录预期产物的词法与解析身份，解析失败时保守禁用清理。"""

        mapping.expected_outputs.add(target)
        try:
            mapping.expected_output_identities.add(target.resolve(strict=False))
        except OSError, RuntimeError:
            mapping.expected_output_resolution_failed = True

    def _cleanup_mapping(self, mapping: _MappingRun, run: _ScanRun) -> None:
        """按本轮完整快照清理当前输出根目录内的受管文件。"""

        if mapping.expected_output_resolution_failed:
            mapping.status.cleanup_eligible = False
            logger.warning(
                f"Alist2Strm 已跳过受管产物清理：实例“{mapping.instance_name}”，"
                f"映射 {mapping.mapping_index}（{mapping.source_dir}），预期产物路径解析失败"
            )
            return

        if run.cleanup_disqualified:
            mapping.status.cleanup_eligible = False
            logger.warning(
                f"Alist2Strm 已跳过受管产物清理：实例“{mapping.instance_name}”，"
                f"映射 {mapping.mapping_index}（{mapping.source_dir}），解析后的输出目录存在重叠"
            )
            return

        managed_suffixes = self._managed_output_suffixes(mapping.allowed_suffixes, run.video_suffixes)
        self._observe_stop(run)
        candidates: list[Path] = []
        for raw_target in self._filesystem.iter_files(mapping.output_dir):
            self._observe_stop(run)
            target = Path(raw_target)
            if not self._within_output_root(mapping.output_dir, target):
                continue
            try:
                target_identity = target.resolve(strict=False)
            except OSError, RuntimeError:
                continue
            if target in mapping.expected_outputs or target_identity in mapping.expected_output_identities:
                continue
            if target.suffix.casefold() not in managed_suffixes:
                continue
            candidates.append(target)

        for target in candidates:
            self._observe_stop(run)
            try:
                self._filesystem.unlink(target)
            except ScanStopped:
                raise
            except Exception as error:  # noqa: BLE001 - 单项删除失败不得阻断其他产物
                mapping.status.counters.failed += 1
                self._log_cleanup_failure(mapping, target, error)
                self._record_mapping_error(mapping, f"清理文件 {target}：删除失败（{error}）")
                continue
            mapping.status.counters.deleted += 1

    def _log_cleanup_failure(self, mapping: _MappingRun, target: Path, error: Exception) -> None:
        """在宿主日志中记录每项脱敏的清理失败。"""

        safe_instance = self._safe_error(mapping, mapping.instance_name)
        safe_source = self._safe_error(mapping, mapping.source_dir)
        safe_output = self._safe_error(mapping, str(mapping.output_dir))
        safe_path = self._safe_error(mapping, str(target))
        safe_reason = self._safe_error(mapping, str(error))
        logger.warning(
            f"Alist2Strm 受管产物清理失败：实例“{safe_instance}”，"
            f"映射 {mapping.mapping_index}（源目录“{safe_source}”，输出目录“{safe_output}”），"
            f"路径“{safe_path}”，原因：{safe_reason}"
        )

    @staticmethod
    def _managed_output_suffixes(allowed_suffixes: frozenset[str], video_suffixes: frozenset[str]) -> frozenset[str]:
        """返回当前映射仍管理的本地产物后缀。"""

        managed = set(allowed_suffixes - video_suffixes)
        if allowed_suffixes.intersection(video_suffixes):
            managed.add(STRM_SUFFIX)
        return frozenset(managed)

    @staticmethod
    def _within_output_root(root: Path, target: Path) -> bool:
        """判断候选文件按解析路径位于当前输出根目录下。"""

        if target == root:
            return False
        try:
            relative = target.relative_to(root)
            resolved_root = root.resolve(strict=False)
            resolved_target = target.resolve(strict=False)
        except OSError, RuntimeError, ValueError:
            return False
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return False
        return resolved_target != resolved_root and resolved_root in resolved_target.parents

    def _run_for_mapping(self, mapping: _MappingRun) -> _ScanRun:
        """取得映射所属运行，供目录与文件边界观察停止请求。"""

        with self._lock:
            run = self._run
        if run is None:
            raise ScanStopped()
        return run

    def _download_target(
        self,
        reader: Any,
        remote_path: str,
        target: Path,
        counters: ScanCounters,
        run: _ScanRun,
    ) -> str | None:
        """有限重试附件下载，且本地原子写入失败不重试。"""

        def download_once() -> bool:
            """执行一次完整的打开、读取和原子写入。"""

            with reader.open_download(remote_path) as chunks:
                return bool(self._filesystem.write_bytes_atomic(target, chunks))

        try:
            created = bool(self._retry_operation(download_once, run))
        except ScanStopped:
            raise
        except OpenListError as error:
            counters.failed += 1
            if self._is_auth_failure(error):
                raise
            return error.message
        except Exception as error:  # noqa: BLE001 - 本地写入或编程错误不重试
            counters.failed += 1
            return f"附件下载或写入失败（{error}）"
        if created:
            counters.downloaded += 1
        else:
            counters.skipped += 1
        return None

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

    def _update_existing_target(self, target: Path, content: str, counters: ScanCounters) -> str | None:
        """通过注入文件系统原子替换已有 STRM，失败时保留原文件。"""

        try:
            self._filesystem.replace_text_atomic(target, content)
        except Exception as error:  # noqa: BLE001 - 更新失败必须保留原件并支持重试
            counters.failed += 1
            return f"原子更新失败（{error}）"
        counters.updated += 1
        return None

    def _write_new_target(self, target: Path, content: str, counters: ScanCounters) -> str | None:
        """以注入文件系统原子创建新 STRM，避免留下半成品。"""

        try:
            created = self._filesystem.write_text_atomic(target, content)
        except Exception as error:  # noqa: BLE001 - 本地写入失败记入映射结果
            counters.failed += 1
            return f"原子创建失败（{error}）"
        if created:
            counters.generated += 1
        else:
            counters.skipped += 1
        return None


def playback_address_changed(previous: str | None, current: str) -> bool:
    """只把已知有效播放身份的变化判定为 STRM 更新信号。"""

    return previous is not None and previous != current


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
    "DownloadResponse",
    "HostHttpTransport",
    "HttpResponse",
    "HttpTransport",
    "LocalFileSystem",
    "MappingScanStatus",
    "OpenListClient",
    "OpenListError",
    "OutputFileSystem",
    "RemoteEntry",
    "ScanCoordinator",
    "ScanCounters",
    "ScanStatus",
    "ScanStopped",
    "ScanTarget",
    "ScanTriggerResult",
    "build_download_url",
    "build_playback_url",
    "default_transport",
    "encode_remote_path",
    "is_video_name",
    "local_file_path",
    "local_strm_path",
    "playback_address_changed",
]
