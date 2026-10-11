"""目录最近成功本层观测及 PluginData 持久化边界；不决定扫描剪枝。"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, TypeGuard

from app.sdk.logging import logger

OBSERVATIONS_DATA_KEY = "directory_observations_v1"
MappingIdentity = tuple[str, str, str]


@dataclass(frozen=True)
class FileTimeSummary:
    """参与文件的时间摘要；未知和异常均不能解释为无文件。"""

    kind: Literal["unknown", "empty", "valid", "invalid"] = "unknown"
    modified_at: float | None = None

    def include(self, modified_at: float | None) -> FileTimeSummary:
        """加入一个未排除文件的绝对修改时刻，不保留逐文件清单。"""

        if self.kind == "invalid" or modified_at is None:
            return FileTimeSummary("invalid")
        return FileTimeSummary(
            "valid", max(self.modified_at, modified_at) if self.modified_at is not None else modified_at
        )


@dataclass(frozen=True)
class DirectoryObservation:
    """最近成功本层事实与未完成标记；后代观测时间可以不同。"""

    direct_files: FileTimeSummary = FileTimeSummary()
    children: tuple[str, ...] = ()
    successful_at: float | None = None
    needs_check: bool = True
    recheck_at: float | None = None


def parse_modified_time(value: object) -> float | None:
    """解析 OpenList 带时区时间为绝对时刻；缺失、无时区和异常值不替代为当前时间。"""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.timestamp()
    except OverflowError, OSError, ValueError:
        return None


@dataclass(frozen=True)
class MappingConfigurationObservation:
    """映射配置依据和最近处理的播放身份，不保留连接凭据。"""

    fingerprint: str | None = None
    needs_check: bool = True
    playback_fingerprint: str | None = None
    playback_needs_update: bool = False
    retired: bool = False
    revision: int = 0


class DirectoryObservationStore:
    """通过 PluginData 读写最近目录观测，局部损坏只使对应目录未知。"""

    def __init__(self, load: Callable[[], object], save: Callable[[object], object]) -> None:
        """绑定宿主或内存持久化边界，不执行远端请求。"""

        self._load = load
        self._save = save
        self._lock = threading.RLock()
        self._loaded = False
        self._mappings: dict[MappingIdentity, dict[str, DirectoryObservation]] = {}
        self._configurations: dict[MappingIdentity, MappingConfigurationObservation] = {}

    def reconcile_mappings(self, identities: Iterable[MappingIdentity]) -> bool:
        """持久化完整配置的身份过渡；退休后重新启用不能复用旧剪枝资格。"""

        with self._lock:
            if not self._ensure_loaded():
                return False
            active = set(identities)
            previous = dict(self._configurations)
            for identity in active | set(previous):
                configuration = previous.get(identity)
                retired = identity not in active
                if configuration is None:
                    self._configurations[identity] = MappingConfigurationObservation()
                elif configuration.retired != retired:
                    self._configurations[identity] = replace(
                        configuration, retired=retired, needs_check=True, revision=configuration.revision + 1
                    )
            if self._configurations == previous:
                return True
            if not self._persist():
                self._configurations = previous
                return False
            return True

    def mapping_configuration(self, identity: MappingIdentity) -> MappingConfigurationObservation:
        """读取映射配置依据和未完成要求；缺失状态必须重新完整处理。"""

        with self._lock:
            if not self._ensure_loaded():
                return MappingConfigurationObservation()
            return self._configurations.get(identity, MappingConfigurationObservation())

    def begin_mapping(
        self,
        identity: MappingIdentity,
        fingerprint: str,
        playback_fingerprint: str,
        playback_changed: bool,
        revision: int,
    ) -> None:
        """在映射开始前保存配置与播放身份，未完成播放更新不能随重试或改回配置丢失。"""

        with self._lock:
            if not self._ensure_loaded():
                return
            previous = self._configurations.get(identity, MappingConfigurationObservation())
            if previous.retired or previous.revision != revision:
                return
            self._configurations[identity] = replace(
                previous,
                fingerprint=fingerprint,
                needs_check=True,
                playback_fingerprint=playback_fingerprint,
                playback_needs_update=previous.playback_needs_update or playback_changed,
            )
            self._persist()

    def complete_mapping(
        self, identity: MappingIdentity, fingerprint: str, playback_fingerprint: str, revision: int
    ) -> bool:
        """仅在映射清理完成后解除相同配置依据和身份版本的必查标记。"""

        with self._lock:
            if not self._ensure_loaded():
                return False
            previous = self._configurations.get(identity)
            if (
                previous is None
                or previous.fingerprint != fingerprint
                or previous.retired
                or previous.revision != revision
            ):
                return False
            self._configurations[identity] = replace(
                previous, needs_check=False, playback_fingerprint=playback_fingerprint, playback_needs_update=False
            )
            if not self._persist():
                self._configurations[identity] = previous
                return False
            return True

    def directory(self, identity: MappingIdentity, path: str) -> DirectoryObservation:
        """读取本层观测；丢失或损坏记录按未知、待重建返回。"""

        with self._lock:
            self._ensure_loaded()
            return self._mappings.get(identity, {}).get(path, DirectoryObservation())

    def subtree(self, identity: MappingIdentity, path: str) -> FileTimeSummary:
        """汇总各层最近成功观测，不把异步观测宣称为同一时刻快照。"""

        with self._lock:
            self._ensure_loaded()
            directories = self._mappings.get(identity, {})
            pending = [path]
            visited: set[str] = set()
            result = FileTimeSummary("empty")
            while pending:
                current = pending.pop()
                if current in visited:
                    return FileTimeSummary()
                visited.add(current)
                observation = directories.get(current)
                if observation is None or observation.direct_files.kind == "unknown":
                    return FileTimeSummary()
                if observation.direct_files.kind == "invalid":
                    result = FileTimeSummary("invalid")
                elif observation.direct_files.kind == "valid" and result.kind != "invalid":
                    result = result.include(observation.direct_files.modified_at)
                pending.extend(observation.children)
            return result

    def begin_check(self, identity: MappingIdentity, path: str) -> bool:
        """在本层 I/O 开始前持久标记未完成；失败时保留运行态必查但不得开始 I/O。"""

        with self._lock:
            if not self._ensure_loaded():
                return False
            directories = self._mappings.setdefault(identity, {})
            directories[path] = replace(directories.get(path, DirectoryObservation()), needs_check=True)
            return self._persist()

    def record_success(
        self,
        identity: MappingIdentity,
        path: str,
        direct_files: FileTimeSummary,
        children: Iterable[str],
        successful_at: float,
        recheck_at: float | None = None,
    ) -> None:
        """提交完整本层观测；确认消失分支撤销贡献，清理前仍须复查。"""

        with self._lock:
            if not self._ensure_loaded():
                return
            directories = self._mappings.setdefault(identity, {})
            child_paths = tuple(dict.fromkeys(children))
            previous = directories.get(path, DirectoryObservation())
            previous_children = set(previous.children)
            if previous.successful_at is None:
                # 局部记录缺失时仍可能留有孤立后代，重建本层也须撤销已消失分支。
                prefix = f"{path.rstrip('/')}/"
                previous_children.update(
                    f"{prefix}{descendant[len(prefix) :].split('/', 1)[0]}"
                    for descendant in directories
                    if descendant != path and descendant.startswith(prefix)
                )
            for removed in previous_children - set(child_paths):
                for descendant in tuple(directories):
                    if descendant == removed or descendant.startswith(f"{removed}/"):
                        del directories[descendant]
            directories[path] = DirectoryObservation(direct_files, child_paths, successful_at, True, recheck_at)
            self._persist()

    def advance_recheck(self, identity: MappingIdentity, path: str, recheck_at: float) -> None:
        """建立或提前本层期限；后代变冷不能推迟已经取得的复查机会。"""

        with self._lock:
            if not self._ensure_loaded():
                return
            directories = self._mappings.get(identity, {})
            previous = directories.get(path)
            if previous is None or (previous.recheck_at is not None and previous.recheck_at <= recheck_at):
                return
            directories[path] = replace(previous, recheck_at=recheck_at)
            self._persist()

    def complete_cleanup(self, identity: MappingIdentity, paths: Iterable[str]) -> None:
        """仅在对应范围处理和清理成功后解除已检查目录的未完成标记。"""

        with self._lock:
            if not self._ensure_loaded():
                return
            directories = self._mappings.get(identity, {})
            previous = dict(directories)
            for path in paths:
                observation = directories.get(path)
                if observation is not None and observation.successful_at is not None:
                    directories[path] = replace(observation, needs_check=False)
            if not self._persist():
                self._mappings[identity] = previous

    def _ensure_loaded(self) -> bool:
        """读取失败不覆盖宿主旧数据，允许下一次边界调用重试。"""

        if self._loaded:
            return True
        try:
            payload = self._load()
        except Exception:  # noqa: BLE001 - 不泄露底层持久化错误或凭据
            logger.warning("Alist2Strm 目录观测读取失败，目录状态暂按未知处理")
            return False
        if isinstance(payload, Mapping) and payload.get("version") == 1:
            mappings = payload.get("mappings")
            if isinstance(mappings, list):
                for raw in mappings:
                    if not isinstance(raw, Mapping):
                        continue
                    identity = raw.get("identity")
                    directories = raw.get("directories")
                    if (
                        not isinstance(identity, list)
                        or len(identity) != 3
                        or not all(isinstance(value, str) and value for value in identity)
                        or not isinstance(directories, Mapping)
                    ):
                        continue
                    key: MappingIdentity = (identity[0], identity[1], identity[2])
                    self._mappings[key] = {
                        path: observation
                        for path, value in directories.items()
                        if isinstance(path, str)
                        and _valid_path(path, key[1])
                        and (observation := _decode_observation(path, value)) is not None
                    }
                    configuration = raw.get("configuration")
                    if isinstance(configuration, Mapping):
                        fingerprint = configuration.get("fingerprint")
                        needs_check = configuration.get("needs_check")
                        playback_fingerprint = configuration.get("playback_fingerprint")
                        playback_needs_update = configuration.get("playback_needs_update", False)
                        retired = configuration.get("retired", False)
                        revision = configuration.get("revision", 0)
                        if (
                            (fingerprint is None or (isinstance(fingerprint, str) and fingerprint))
                            and isinstance(needs_check, bool)
                            and (playback_fingerprint is None or isinstance(playback_fingerprint, str))
                            and isinstance(playback_needs_update, bool)
                            and isinstance(retired, bool)
                            and isinstance(revision, int)
                            and not isinstance(revision, bool)
                            and revision >= 0
                        ):
                            self._configurations[key] = MappingConfigurationObservation(
                                fingerprint, needs_check, playback_fingerprint, playback_needs_update, retired, revision
                            )
        self._loaded = True
        return True

    def _persist(self) -> bool:
        """保存目录级事实，不包含凭据或逐文件明细。"""

        payload = {
            "version": 1,
            "mappings": [
                {
                    "identity": list(identity),
                    "configuration": {
                        "fingerprint": configuration.fingerprint,
                        "needs_check": configuration.needs_check,
                        "playback_fingerprint": configuration.playback_fingerprint,
                        "playback_needs_update": configuration.playback_needs_update,
                        "retired": configuration.retired,
                        "revision": configuration.revision,
                    }
                    if (configuration := self._configurations.get(identity)) is not None
                    else None,
                    "directories": {
                        path: {
                            "direct_files": {
                                "kind": value.direct_files.kind,
                                "modified_at": value.direct_files.modified_at,
                            },
                            "children": list(value.children),
                            "successful_at": value.successful_at,
                            "needs_check": value.needs_check,
                            "recheck_at": value.recheck_at,
                        }
                        for path, value in self._mappings.get(identity, {}).items()
                    },
                }
                for identity in sorted(set(self._mappings) | set(self._configurations))
            ],
        }
        try:
            if self._save(payload) is False:
                raise OSError("目录观测保存未成功")
        except Exception:  # noqa: BLE001 - 返回失败供持久边界保护任务，不泄露底层信息
            logger.warning("Alist2Strm 目录观测保存失败，后续任务仍需重建或复查")
            return False
        return True


def _valid_path(path: str, root: str) -> bool:
    """只恢复规范的映射内绝对路径，避免损坏记录跨分支或形成环。"""

    return (
        path.startswith("/")
        and str(PurePosixPath(path)) == path
        and ".." not in PurePosixPath(path).parts
        and (path == root or path.startswith(f"{root.rstrip('/')}/"))
    )


def _finite_time(value: object) -> TypeGuard[int | float]:
    """排除布尔值、溢出整数和非有限数字。"""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _decode_observation(path: str, value: object) -> DirectoryObservation | None:
    """严格恢复单层记录，异常层不拖累有效兄弟层。"""

    if not isinstance(value, Mapping):
        return None
    files = value.get("direct_files")
    children = value.get("children")
    successful_at = value.get("successful_at")
    needs_check = value.get("needs_check")
    if not isinstance(files, Mapping) or not isinstance(children, list) or not isinstance(needs_check, bool):
        return None
    if not all(
        isinstance(child, str)
        and child != path
        and _valid_path(child, path)
        and str(PurePosixPath(child).parent) == path
        for child in children
    ) or len(set(children)) != len(children):
        return None
    kind = files.get("kind")
    modified_at = files.get("modified_at")
    if not isinstance(kind, str):
        return None
    if kind == "unknown" and successful_at is None and needs_check and not children and modified_at is None:
        return DirectoryObservation()
    if not _finite_time(successful_at):
        return None
    if kind == "valid" and _finite_time(modified_at):
        summary = FileTimeSummary("valid", float(modified_at))
    elif kind in {"empty", "invalid"} and modified_at is None:
        summary = FileTimeSummary(kind)
    else:
        return None
    recheck_at = value.get("recheck_at")
    if recheck_at is not None and (not _finite_time(recheck_at) or recheck_at < successful_at):
        return None
    return DirectoryObservation(
        summary,
        tuple(children),
        float(successful_at),
        needs_check,
        float(recheck_at) if recheck_at is not None else None,
    )
