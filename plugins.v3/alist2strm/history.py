"""Alist2Strm 扫描历史的 PluginData 存储与安全投影。"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime

from app.sdk.logging import logger

HISTORY_DATA_KEY = "scan_history_v1"
HISTORY_SCHEMA_VERSION = 1
DEFAULT_HISTORY_PAGE_SIZE = 20
MAX_HISTORY_PAGE_SIZE = 100
COUNTER_NAMES = ("generated", "downloaded", "updated", "skipped", "deleted", "failed")
NON_TERMINAL_STATES = frozenset({"pending", "queued", "running", "stopping"})
TERMINAL_STATES = frozenset({"success", "partial_failure", "failed", "stopped", "interrupted"})
KNOWN_STATES = NON_TERMINAL_STATES | TERMINAL_STATES

HistoryLoader = Callable[[], object]
HistorySaver = Callable[[object], object]
SecretProvider = Callable[[], Sequence[str]]


class HistoryRecord(dict[str, object]):
    """一个仅包含安全任务和映射汇总的历史记录。"""


class HistoryStore:
    """通过宿主 ``PluginData`` 保存、恢复、分页和清理扫描历史。"""

    def __init__(
        self,
        load: HistoryLoader,
        save: HistorySaver,
        *,
        clock: Callable[[], float] = time.time,
        secrets: SecretProvider | None = None,
    ) -> None:
        """绑定可替换持久化、时钟和凭据脱敏边界。"""

        self._load = load
        self._save = save
        self._clock = clock
        self._secrets = secrets if secrets is not None else lambda: ()
        self._lock = threading.RLock()

    def recover(self, retention_days: int, *, exclude_run_ids: Sequence[str] = ()) -> list[HistoryRecord] | None:
        """在插件初始化时标记旧进程遗留任务并清理过期记录。"""

        excluded = {str(run_id) for run_id in exclude_run_ids}
        with self._lock:
            items, changed, loaded = self._read_items()
            if not loaded:
                return None
            now = self._clock()
            now_text = _timestamp(now)
            for item in items:
                if item.get("id") in excluded or item["state"] not in NON_TERMINAL_STATES:
                    continue
                item["state"] = "interrupted"
                item["finished_at"] = now_text
                changed = True
                mappings = item.get("mappings", [])
                if isinstance(mappings, list):
                    for mapping in mappings:
                        if isinstance(mapping, Mapping) and mapping.get("state") in NON_TERMINAL_STATES:
                            mapping["state"] = "interrupted"
            items, pruned = _prune(items, retention_days, now)
            changed = changed or pruned
            if changed and not self._persist(items):
                return None
            return deepcopy(items)

    def prune(self, retention_days: int) -> list[HistoryRecord]:
        """按正整数保留天数删除到期记录，不依赖定时扫描开关。"""

        with self._lock:
            items, changed, loaded = self._read_items()
            if not loaded:
                return []
            items, pruned = _prune(items, retention_days, self._clock())
            if changed or pruned:
                self._persist(items)
            return deepcopy(items)

    def record_status(self, status: object) -> bool:
        """写入或更新一次已接受扫描的安全状态快照。"""

        with self._lock:
            items, _, loaded = self._read_items()
            if not loaded:
                return False
            record = _record_from_status(status, self._secret_values())
            if record is None:
                return False
            run_id = record["id"]
            replaced = False
            for index, item in enumerate(items):
                if item.get("id") == run_id:
                    items[index] = record
                    replaced = True
                    break
            if not replaced:
                items.append(record)
            self._persist(items)
            return True

    def discard(self, run_id: object) -> bool:
        """删除调度拒绝的临时记录，避免把拒绝请求计为实际运行。"""

        identifier = str(run_id).strip() if run_id is not None else ""
        if not identifier:
            return False
        with self._lock:
            items, changed, loaded = self._read_items()
            if not loaded:
                return False
            retained = [item for item in items if item.get("id") != identifier]
            removed = len(retained) != len(items)
            if removed or changed:
                self._persist(retained)
            return removed

    def page(
        self, page: int = 1, page_size: int = DEFAULT_HISTORY_PAGE_SIZE, *, retention_days: int = 90
    ) -> dict[str, object]:
        """返回时间倒序的安全历史分页。"""

        safe_page = _safe_page(page, minimum=1)
        safe_page_size = _safe_page(page_size, minimum=1, maximum=MAX_HISTORY_PAGE_SIZE)
        with self._lock:
            items, changed, loaded = self._read_items()
            if not loaded:
                return {
                    "total": 0,
                    "page": safe_page,
                    "page_size": safe_page_size,
                    "items": [],
                }
            items, pruned = _prune(items, retention_days, self._clock())
            if changed or pruned:
                self._persist(items)
            items.sort(key=lambda item: _timestamp_epoch(item.get("started_at")), reverse=True)
            total = len(items)
            start = (safe_page - 1) * safe_page_size
            selected = deepcopy(items[start : start + safe_page_size])
        return {
            "total": total,
            "page": safe_page,
            "page_size": safe_page_size,
            "items": selected,
        }

    def _secret_values(self) -> tuple[str, ...]:
        """读取当前凭据值；读取失败时按无凭据继续。"""

        try:
            values = self._secrets()
        except Exception:  # noqa: BLE001 - 脱敏边界失败不得阻断扫描
            return ()
        return tuple(
            sorted({str(value) for value in values if isinstance(value, str) and value}, key=len, reverse=True)
        )

    def _read_items(self) -> tuple[list[HistoryRecord], bool, bool]:
        """读取并规范 PluginData，区分读取失败和合法空历史。"""

        try:
            raw = self._load()
        except Exception:  # noqa: BLE001 - 历史读取失败不阻断插件生命周期
            return [], False, False
        items = normalize_history(raw, secrets=self._secret_values())
        canonical = _storage_payload(items)
        return items, raw is not None and raw != canonical, True

    def _persist(self, items: Sequence[HistoryRecord]) -> bool:
        """尽力写回完整记录集，持久化失败不影响扫描任务。"""

        try:
            self._save(_storage_payload(items))
        except Exception:  # noqa: BLE001 - PluginData 故障不能终止后台扫描
            logger.warning("Alist2Strm 扫描历史持久化失败，当前扫描仍继续")
            return False
        return True


def normalize_history(raw: object, *, secrets: Sequence[str] = ()) -> list[HistoryRecord]:
    """把空、旧版或损坏的 PluginData 安全规范为独立运行记录。"""

    raw_items: object = raw
    if isinstance(raw, Mapping):
        raw_items = raw.get("items", raw.get("runs", []))
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        return []
    result: list[HistoryRecord] = []
    for raw_item in raw_items:
        record = _normalise_record(raw_item, secrets)
        if record is not None:
            result.append(record)
    return result


def _normalise_record(raw: object, secrets: Sequence[str]) -> HistoryRecord | None:
    """规范一项运行记录并丢弃无法安全分页的结构。"""

    if not isinstance(raw, Mapping):
        return None
    run_id = _string(raw.get("id", raw.get("run_id", raw.get("identity"))))
    started_at = _canonical_timestamp(raw.get("started_at"))
    state = _state(raw.get("state", raw.get("status")))
    if not run_id or started_at is None or state is None:
        return None
    finished_at = _canonical_timestamp(raw.get("finished_at"))
    mappings_raw = raw.get("mappings", raw.get("mapping_summaries", []))
    mappings: list[dict[str, object]] = []
    if isinstance(mappings_raw, Sequence) and not isinstance(mappings_raw, (str, bytes, bytearray)):
        for mapping in mappings_raw:
            normalized = _normalise_mapping(mapping, secrets)
            if normalized is not None:
                mappings.append(normalized)
    record = HistoryRecord(
        id=_sanitize_text(run_id, secrets),
        identity=_sanitize_text(_string(raw.get("identity")) or run_id, secrets),
        started_at=started_at,
        finished_at=finished_at,
        state=state,
        counters=_normalise_counters(raw.get("counters")),
        mappings=mappings,
    )
    error = _history_error(raw.get("error"), secrets)
    if error:
        record["error"] = error
    return record


def _normalise_mapping(raw: object, secrets: Sequence[str]) -> dict[str, object] | None:
    """规范一项映射汇总，不接收逐文件详情或清理元数据。"""

    if not isinstance(raw, Mapping):
        return None
    instance_id = _sanitize_text(_string(raw.get("instance_id", raw.get("instance"))) or "", secrets)
    instance_name = _sanitize_text(_string(raw.get("instance_name", raw.get("name"))) or instance_id, secrets)
    mapping_index = _positive_int(raw.get("mapping_index", raw.get("mapping")))
    source_dir = _sanitize_text(_string(raw.get("source_dir", raw.get("source"))) or "", secrets)
    output_dir = _sanitize_text(_string(raw.get("output_dir", raw.get("output"))) or "", secrets)
    state = _state(raw.get("state", raw.get("status")))
    if not instance_id or mapping_index is None or not source_dir or not output_dir or state is None:
        return None
    identity = _sanitize_text(_string(raw.get("identity")) or f"{instance_id}|{source_dir}|{output_dir}", secrets)
    started_at = _canonical_timestamp(raw.get("started_at"))
    finished_at = _canonical_timestamp(raw.get("finished_at"))
    result: dict[str, object] = {
        "identity": identity,
        "instance_id": instance_id,
        "instance_name": instance_name,
        "mapping_index": mapping_index,
        "source_dir": source_dir,
        "output_dir": output_dir,
        "state": state,
        "started_at": started_at,
        "finished_at": finished_at,
        "counters": _normalise_counters(raw.get("counters")),
    }
    error = _history_error(raw.get("error"), secrets)
    if error:
        result["error"] = error
    return result


def _record_from_status(status: object, secrets: Sequence[str]) -> HistoryRecord | None:
    """从扫描状态构造不含触发和逐文件信息的持久投影。"""

    method = getattr(status, "as_dict", None)
    values: object = method() if callable(method) else status
    return _normalise_record(values, secrets)


def _normalise_counters(raw: object) -> dict[str, int]:
    """保留既定六类计数并将损坏值安全归零。"""

    values = raw if isinstance(raw, Mapping) else {}
    return {name: _nonnegative_int(values.get(name, 0)) for name in COUNTER_NAMES}


def _state(value: object) -> str | None:
    """规范运行或映射状态，兼容少量历史别名。"""

    if not isinstance(value, str):
        return None
    state = value.strip().lower()
    aliases = {
        "complete": "success",
        "completed": "success",
        "partial": "partial_failure",
        "interrupted_by_restart": "interrupted",
    }
    state = aliases.get(state, state)
    return state if state in KNOWN_STATES else None


HISTORY_ERROR_HINT = "扫描失败，详情请查阅宿主日志"
STOP_ERROR_SENTINEL = "扫描已停止"


def _history_error(value: object, secrets: Sequence[str]) -> str | None:
    """将任意持久化错误折叠为不含文件路径的通用提示。"""

    if value is None:
        return None
    text = _sanitize_text(str(value), secrets).strip()
    return None if not text or text == STOP_ERROR_SENTINEL else HISTORY_ERROR_HINT


def _sanitize_text(value: str, secrets: Sequence[str]) -> str:
    """删除已知凭据及常见认证头中的值。"""

    text = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")
    return re.sub(
        r"(?i)(authorization|api[_ -]?token|password|token)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )


def _prune(items: list[HistoryRecord], retention_days: int, now: float) -> tuple[list[HistoryRecord], bool]:
    """按开始时间删除早于保留窗口的记录。"""

    days = _retention_days(retention_days)
    cutoff = now - days * 86400
    retained = [item for item in items if _timestamp_epoch(item.get("started_at")) >= cutoff]
    return retained, len(retained) != len(items)


def _storage_payload(items: Sequence[HistoryRecord]) -> dict[str, object]:
    """构造版本化 PluginData 载荷。"""

    return {"version": HISTORY_SCHEMA_VERSION, "items": deepcopy(list(items))}


def _timestamp(value: float) -> str:
    """生成 UTC ISO 时间戳。"""

    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _canonical_timestamp(value: object) -> str | None:
    """校验并统一可分页的时间戳。"""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), tz=UTC)
        except OverflowError, OSError, ValueError:
            return None
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _timestamp_epoch(value: object) -> float:
    """将规范时间转换为排序或清理用秒数。"""

    canonical = _canonical_timestamp(value)
    if canonical is None:
        return 0.0
    try:
        return datetime.fromisoformat(canonical).timestamp()
    except OverflowError, ValueError:
        return 0.0


def _string(value: object) -> str | None:
    """读取非空字符串字段。"""

    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _nonnegative_int(value: object) -> int:
    """读取非负整数计数，拒绝布尔和浮点隐式转换。"""

    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _positive_int(value: object) -> int | None:
    """读取正整数映射序号。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _retention_days(value: object) -> int:
    """将保留天数限制为正整数，异常调用回退到默认值。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return 90
    return value


def _safe_page(value: object, *, minimum: int, maximum: int | None = None) -> int:
    """规范分页参数，避免负数、布尔和超大查询切片。"""

    result = minimum if isinstance(value, bool) or not isinstance(value, int) else max(value, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result


__all__ = [
    "COUNTER_NAMES",
    "DEFAULT_HISTORY_PAGE_SIZE",
    "HISTORY_DATA_KEY",
    "HISTORY_SCHEMA_VERSION",
    "MAX_HISTORY_PAGE_SIZE",
    "NON_TERMINAL_STATES",
    "TERMINAL_STATES",
    "HistoryRecord",
    "HistoryStore",
    "normalize_history",
]
