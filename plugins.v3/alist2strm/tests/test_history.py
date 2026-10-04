"""扫描历史存储、恢复和管理 API 的公共 seam 测试。"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from app.api.dependencies.auth import get_current_active_superuser_async
from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import (  # ty: ignore[unresolved-import]
    ConfigurationValidationError,
    DirectoryMapping,
    OpenListInstance,
    PluginConfiguration,
)
from app.plugins.alist2strm.history import HistoryStore  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import ScanCoordinator, ScanTarget  # ty: ignore[unresolved-import]


def _status(
    run_id: str,
    started_at: str,
    *,
    state: str = "success",
    generated: int = 1,
    error: str | None = None,
) -> dict[str, object]:
    """构造不含触发元数据的扫描状态快照。"""

    mapping: dict[str, object] = {
        "instance_id": "instance-1",
        "instance_name": "主实例",
        "mapping_index": 1,
        "source_dir": "/library",
        "output_dir": "/strm",
        "state": state,
        "started_at": started_at,
        "finished_at": started_at if state in {"success", "failed", "stopped"} else None,
        "counters": {
            "generated": generated,
            "downloaded": 2,
            "updated": 3,
            "skipped": 4,
            "deleted": 5,
            "failed": 6,
        },
    }
    if error is not None:
        mapping["error"] = error
    return {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": started_at if state in {"success", "failed", "stopped"} else None,
        "state": state,
        "counters": mapping["counters"],
        "mappings": [mapping],
    }


def _store(
    initial: object = None,
    *,
    now: float = 1_704_326_400.0,
    saved: list[object] | None = None,
) -> tuple[HistoryStore, dict[str, object]]:
    """构造内存 PluginData 替身。"""

    state: dict[str, object] = {"value": initial}

    def load() -> object:
        """读取替身载荷。"""

        return state["value"]

    def save(value: object) -> None:
        """记录替身载荷。"""

        state["value"] = value
        if saved is not None:
            saved.append(value)

    return HistoryStore(load, save, clock=lambda: now, secrets=lambda: ("credential-value",)), state


def test_history_keeps_multiple_runs_descending_pages_summaries_and_six_counters() -> None:
    """多次实际运行分别保存，分页和映射投影只含安全汇总。"""

    store, _ = _store()
    store.record_status(
        _status(
            "run-old",
            "2024-01-01T00:00:00+00:00",
            error="文件 /library/Movie.mkv：Authorization: credential-value",
        )
    )
    store.record_status(_status("run-new", "2024-01-02T00:00:00+00:00", generated=7))

    first_page = store.page(1, 1)
    second_page = store.page(2, 1)

    assert first_page["total"] == 2
    assert first_page["page"] == 1
    assert first_page["page_size"] == 1
    assert first_page["items"][0]["id"] == "run-new"  # type: ignore[index]
    assert second_page["items"][0]["id"] == "run-old"  # type: ignore[index]
    item = second_page["items"][0]  # type: ignore[index]
    mapping = item["mappings"][0]
    assert mapping["identity"] == "instance-1|/library|/strm"
    assert mapping["started_at"] == "2024-01-01T00:00:00+00:00"
    assert mapping["finished_at"] == "2024-01-01T00:00:00+00:00"
    assert mapping["counters"] == {
        "generated": 1,
        "downloaded": 2,
        "updated": 3,
        "skipped": 4,
        "deleted": 5,
        "failed": 6,
    }
    assert "credential-value" not in str(item)
    assert "Movie.mkv" not in str(item)
    assert "error" not in item["mappings"][0] or "credential-value" not in item["mappings"][0]["error"]  # type: ignore[index]
    assert "trigger" not in item and "cleanup_reason" not in item and "files" not in item


def test_history_suppresses_stop_sentinel_but_retains_failure_errors() -> None:
    """干净停止只保留状态，真实失败仍保留通用错误提示。"""

    store, _ = _store()
    stopped = _status("stopped", "2024-01-03T00:00:00+00:00", state="stopped", error="扫描已停止")
    stopped["error"] = "扫描已停止"
    failed = _status("failed", "2024-01-04T00:00:00+00:00", state="failed", error="权限检查失败")
    failed["error"] = "权限检查失败"

    assert store.record_status(stopped) is True
    assert store.record_status(failed) is True
    items = {item["id"]: item for item in store.page()["items"]}  # type: ignore[index]

    assert "error" not in items["stopped"]
    assert "error" not in items["stopped"]["mappings"][0]  # type: ignore[index]
    assert items["failed"]["error"] == "扫描失败，详情请查阅宿主日志"
    assert items["failed"]["mappings"][0]["error"] == "扫描失败，详情请查阅宿主日志"  # type: ignore[index]


def test_history_normalizes_malformed_data_and_marks_restart_interrupted() -> None:
    """空或损坏旧数据安全忽略，重启恢复保留计数、映射和错误。"""

    store, state = _store(
        {
            "version": 1,
            "items": [
                None,
                {"state": "running"},
                {
                    **_status("run-active", "2024-01-01T00:00:00+00:00", state="running", generated=8),
                    "mappings": [
                        {
                            "instance_id": "instance-1",
                            "instance_name": "主实例",
                            "mapping_index": 1,
                            "source_dir": "/library",
                            "output_dir": "/strm",
                            "state": "running",
                            "error": "先前错误",
                            "counters": {"generated": 8, "failed": 2},
                        }
                    ],
                },
            ],
        }
    )

    recovered = store.recover(90)

    assert len(recovered) == 1
    assert recovered[0]["state"] == "interrupted"
    assert recovered[0]["counters"]["generated"] == 8  # type: ignore[index]
    assert recovered[0]["mappings"][0]["state"] == "interrupted"  # type: ignore[index]
    assert recovered[0]["mappings"][0]["error"] == "扫描失败，详情请查阅宿主日志"  # type: ignore[index]
    stored = cast(Mapping[str, Any], state["value"])
    stored_items = cast(list[Mapping[str, object]], stored["items"])
    assert stored_items[0]["state"] == "interrupted"


def test_retention_prunes_expired_records_without_schedule() -> None:
    """保留策略通过独立存储入口生效，不读取定时开关。"""

    store, _ = _store(now=1_704_153_600.0)  # 2024-01-02 UTC
    store.record_status(_status("expired", "2023-12-30T00:00:00+00:00"))
    store.record_status(_status("kept", "2024-01-01T12:00:00+00:00"))

    page = store.page(1, 20, retention_days=1)

    assert page["total"] == 1
    assert page["items"][0]["id"] == "kept"  # type: ignore[index]


def test_history_persistence_failure_is_non_fatal() -> None:
    """PluginData 写入失败不会让历史入口抛出或改变调用方生命周期。"""

    def fail_save(_value: object) -> None:
        """模拟宿主 PluginData 故障。"""

        raise OSError("synthetic persistence failure")

    store = HistoryStore(lambda: None, fail_save)

    assert store.record_status(_status("run", "2024-01-01T00:00:00+00:00")) is True
    assert store.page()["total"] == 0


def test_history_loader_failure_never_persists_empty_replacement() -> None:
    """PluginData 读取异常不得用空历史覆盖既有持久数据。"""

    writes: list[object] = []

    def fail_load() -> object:
        """模拟宿主读取故障。"""

        raise OSError("history load failed")

    store = HistoryStore(fail_load, writes.append)

    assert store.record_status(_status("run", "2024-01-01T00:00:00+00:00")) is False
    assert store.discard("run") is False
    assert store.recover(90) is None
    assert store.prune(90) == []
    assert store.page()["total"] == 0
    assert writes == []


def test_history_error_projection_discards_permission_file_details() -> None:
    """清理异常中的文件名、路径和底层原因不得进入历史。"""

    store, _ = _store()
    store.record_status(
        _status(
            "cleanup-failure",
            "2024-01-01T00:00:00+00:00",
            state="failed",
            error="清理文件 /srv/secret/movie.strm：PermissionError: [Errno 13] Permission denied",
        )
    )

    response = store.page()
    rendered = str(response)
    assert "movie.strm" not in rendered
    assert "/srv/secret" not in rendered
    assert "PermissionError" not in rendered
    assert "扫描失败，详情请查阅宿主日志" in rendered


def _configuration(output_dir: str = "/strm") -> PluginConfiguration:
    """构造无需网络凭据的离线扫描配置。"""

    return PluginConfiguration(
        instances=[
            OpenListInstance(
                id="instance-1",
                name="主实例",
                scan_url="https://openlist.example.invalid",
                mappings=[DirectoryMapping(source_dir="/library", output_dir=output_dir)],
            )
        ]
    )


def test_repeated_coordinator_runs_keep_distinct_history_records(tmp_path: Path) -> None:
    """协调器每次接受新任务都写入新记录并最终收敛到终态。"""

    configuration = _configuration(str(tmp_path / "strm"))
    jobs: list[Any] = []
    store, _ = _store()
    coordinator = ScanCoordinator(
        lambda: configuration,  # type: ignore[arg-type]
        dispatch=lambda task: jobs.append(task) or True,
        client_factory=lambda _url, _token: object(),
        media_suffix_provider=lambda: (".mkv",),
        history_callback=store.record_status,
        history_discard=store.discard,
    )

    assert coordinator.trigger().accepted is True
    jobs.pop()()
    first = coordinator.status().as_dict()
    assert first["state"] == "failed"
    assert coordinator.trigger().accepted is True
    jobs.pop()()
    second = coordinator.status().as_dict()
    assert second["state"] == "failed"

    assert first["mappings"][0]["started_at"]
    assert first["mappings"][0]["finished_at"]
    assert second["mappings"][0]["started_at"]
    assert second["mappings"][0]["finished_at"]

    page = store.page()
    assert page["total"] == 2
    assert page["items"][0]["id"] != page["items"][1]["id"]  # type: ignore[index]
    assert page["items"][0]["mappings"][0]["started_at"]  # type: ignore[index]
    assert page["items"][0]["mappings"][0]["finished_at"]  # type: ignore[index]


def test_in_progress_mapping_snapshot_recovers_with_open_finish_time() -> None:
    """网络边界前已持久化的映射进度可在新存储实例中恢复为中断。"""

    configuration = PluginConfiguration(
        instances=[
            OpenListInstance(
                id="instance-1",
                name="主实例",
                scan_url="https://openlist.example.invalid",
                api_token="configured",
                mappings=[DirectoryMapping(source_dir="/library", output_dir="/strm")],
            )
        ]
    )
    jobs: list[Any] = []
    snapshots: list[dict[str, object]] = []
    store, _ = _store(now=1_704_326_400.0)

    def client_factory(_url: str, _token: str) -> object:
        """在客户端创建前观察已写入的运行快照。"""

        snapshots.append(store.page())
        raise OSError("offline client failure")

    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=lambda task: jobs.append(task) or True,
        client_factory=client_factory,
        clock=lambda: 1_704_326_400.0,
        history_callback=store.record_status,
        history_discard=store.discard,
    )

    assert coordinator.trigger().accepted is True
    jobs.pop()()
    assert snapshots
    persisted = {"version": 1, "items": snapshots[0]["items"]}
    snapshot_items = cast(list[dict[str, Any]], snapshots[0]["items"])
    snapshot_mapping = cast(dict[str, Any], snapshot_items[0]["mappings"][0])
    assert snapshot_mapping["state"] == "running"
    assert snapshot_mapping["started_at"] is not None
    assert snapshot_mapping["finished_at"] is None

    fresh_store = HistoryStore(lambda: persisted, lambda _value: None, clock=lambda: 1_704_326_400.0)
    recovered = fresh_store.recover(90)
    assert recovered is not None
    recovered_mapping = recovered[0]["mappings"][0]
    assert recovered[0]["state"] == "interrupted"
    assert recovered_mapping["state"] == "interrupted"
    assert recovered_mapping["started_at"] is not None
    assert recovered_mapping["finished_at"] is None


def test_recovery_retries_after_loader_failure_and_interrupts_orphan() -> None:
    """读取故障后的后续初始化重试恢复并只中断旧孤儿记录。"""

    allow_load = False
    persisted: dict[str, object] = {
        "version": 1,
        "items": [_status("orphan", "2024-01-01T00:00:00+00:00", state="running")],
    }
    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._history_recovery_done = False
    plugin._history_clock = lambda: 1_704_326_400.0
    plugin.get_config = lambda _plugin_id=None: None

    def get_data(key=None, plugin_id=None) -> object:
        """模拟可恢复的 PluginData 读取故障。"""

        if key == plugin.HISTORY_DATA_KEY and not allow_load:
            raise OSError("history unavailable")
        return {} if key == plugin.SECRET_DATA_KEY else persisted

    plugin.get_data = get_data
    plugin.save_data = lambda key, value, plugin_id=None: (
        persisted.update(value) if key == plugin.HISTORY_DATA_KEY else None
    )
    plugin.init_plugin({"instances": [], "schedule_enabled": False})
    assert plugin._history_recovery_done is False
    allow_load = True
    plugin.init_plugin({"instances": [], "schedule_enabled": False})
    assert plugin._history_recovery_done is True
    assert plugin.get_history_page()["items"][0]["state"] == "interrupted"  # type: ignore[index]


def test_recovery_retries_after_save_failure() -> None:
    """恢复写回失败时不宣告完成，下一次初始化可再次持久化。"""

    allow_save = False
    persisted: dict[str, object] = {
        "version": 1,
        "items": [_status("orphan", "2024-01-01T00:00:00+00:00", state="running")],
    }
    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._history_recovery_done = False
    plugin._history_clock = lambda: 1_704_326_400.0
    plugin.get_config = lambda _plugin_id=None: None
    plugin.get_data = lambda key=None, plugin_id=None: {} if key == plugin.SECRET_DATA_KEY else persisted

    def save_data(key, value, plugin_id=None) -> None:
        """模拟恢复写回先失败后成功。"""

        if key == plugin.HISTORY_DATA_KEY:
            if not allow_save:
                raise OSError("history save unavailable")
            persisted.clear()
            persisted.update(value)

    plugin.save_data = save_data
    plugin.init_plugin({"instances": [], "schedule_enabled": False})
    assert plugin._history_recovery_done is False
    allow_save = True
    plugin.init_plugin({"instances": [], "schedule_enabled": False})
    assert plugin._history_recovery_done is True
    assert plugin.get_history_page()["items"][0]["state"] == "interrupted"  # type: ignore[index]


def test_recovery_retry_excludes_same_runtime_live_run() -> None:
    """真实接受的当前运行在恢复重试时保留，旧孤儿被中断。"""

    allow_load = False
    jobs: list[Any] = []
    configuration = {
        "instances": [
            {
                "id": "instance-1",
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "api_token": "configured",
                "mappings": [{"source_dir": "/library", "output_dir": "/strm"}],
            }
        ],
        "schedule_enabled": False,
    }
    persisted: dict[str, object] = {
        "version": 1,
        "items": [_status("orphan", "2024-01-01T00:00:00+00:00", state="running")],
    }
    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._history_recovery_done = False
    plugin._history_clock = lambda: 1_704_326_400.0
    plugin._scan_clock = lambda: 1_704_326_400.0
    plugin._scan_dispatch = lambda task: jobs.append(task) or True
    plugin.get_config = lambda _plugin_id=None: None

    def get_data(key=None, plugin_id=None) -> object:
        """让历史读取在第二次初始化恢复。"""

        if key == plugin.HISTORY_DATA_KEY and not allow_load:
            raise OSError("history unavailable")
        return {} if key == plugin.SECRET_DATA_KEY else persisted

    plugin.get_data = get_data
    plugin.save_data = lambda key, value, plugin_id=None: (
        persisted.update(value) if key == plugin.HISTORY_DATA_KEY else None
    )
    plugin.init_plugin(configuration)
    assert plugin._history_recovery_done is False
    accepted = plugin.trigger_scan()
    assert accepted["accepted"] is True
    run_id = plugin.get_scan_status()["run_id"]
    assert isinstance(run_id, str)
    assert jobs
    persisted["items"] = [
        _status("orphan", "2024-01-01T00:00:00+00:00", state="running"),
        _status(run_id, "2024-01-02T00:00:00+00:00", state="running"),
    ]
    allow_load = True
    plugin.init_plugin(configuration)

    items = {item["id"]: item for item in plugin.get_history_page()["items"]}  # type: ignore[index]
    assert items["orphan"]["state"] == "interrupted"
    assert items[run_id]["state"] == "running"


def test_never_started_mappings_keep_null_timestamps_when_stopped() -> None:
    """排队前停止的映射保持未开始的空时间戳。"""

    configuration = PluginConfiguration(
        instances=[
            OpenListInstance(
                id="instance-1",
                name="主实例",
                scan_url="https://openlist.example.invalid",
                mappings=[
                    DirectoryMapping(source_dir="/one", output_dir="/strm/one"),
                    DirectoryMapping(source_dir="/two", output_dir="/strm/two"),
                ],
            )
        ]
    )
    jobs: list[Any] = []
    store, _ = _store()
    coordinator = ScanCoordinator(
        lambda: configuration,
        dispatch=lambda task: jobs.append(task) or True,
        client_factory=lambda _url, _token: object(),
        history_callback=store.record_status,
        history_discard=store.discard,
    )

    assert coordinator.trigger().accepted is True
    assert coordinator.stop().accepted is True
    status = coordinator.status().as_dict()
    assert all(mapping["state"] == "stopped" for mapping in status["mappings"])  # type: ignore[index]
    assert all(mapping["started_at"] is None for mapping in status["mappings"])  # type: ignore[index]
    assert all(mapping["finished_at"] is None for mapping in status["mappings"])  # type: ignore[index]
    jobs.pop()()

    item = store.page()["items"][0]  # type: ignore[index]
    assert all(mapping["started_at"] is None for mapping in item["mappings"])
    assert all(mapping["finished_at"] is None for mapping in item["mappings"])


def test_duplicate_or_invalid_trigger_does_not_create_actual_history_run(tmp_path: Path) -> None:
    """重复或无效触发不新增运行记录，已接受任务仍只占一项。"""

    configuration = _configuration(str(tmp_path / "strm"))
    jobs: list[Any] = []
    store, _ = _store()
    coordinator = ScanCoordinator(
        lambda: configuration,  # type: ignore[arg-type]
        dispatch=lambda task: jobs.append(task) or True,
        client_factory=lambda _url, _token: object(),
        media_suffix_provider=lambda: (".mkv",),
        history_callback=store.record_status,
        history_discard=store.discard,
    )

    assert coordinator.trigger(ScanTarget("missing", mapping_index=1)).accepted is False
    assert store.page()["total"] == 0
    assert coordinator.trigger().accepted is True
    assert coordinator.trigger().accepted is False
    assert store.page()["total"] == 1
    jobs.pop()()
    assert store.page()["total"] == 1


def test_plugin_init_recovers_history_without_auto_scanning() -> None:
    """插件初始化标记旧任务中断，但不提交新的扫描任务。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin.scan_invocations = 0
    plugin._history_clock = lambda: 1_704_326_400.0
    plugin._scan_dispatch = lambda _task: pytest.fail("初始化不应提交扫描")
    persisted: dict[str, object] = {
        plugin.SECRET_DATA_KEY: {},
        plugin.HISTORY_DATA_KEY: {
            "version": 1,
            "items": [_status("previous", "2024-01-01T00:00:00+00:00", state="running", generated=4)],
        },
    }
    plugin.get_config = lambda _plugin_id=None: None
    plugin.get_data = lambda key=None, plugin_id=None: persisted.get(key)
    plugin.save_data = lambda key, value, plugin_id=None: persisted.__setitem__(key, value)

    plugin.init_plugin(
        {
            "instances": [],
            "schedule_enabled": False,
        }
    )

    page = plugin.get_history_page()
    assert page["total"] == 1
    assert page["items"][0]["state"] == "interrupted"  # type: ignore[index]
    assert page["items"][0]["counters"]["generated"] == 4  # type: ignore[index]
    assert plugin.scan_invocations == 0


def test_same_plugin_reapplication_does_not_interrupt_active_run() -> None:
    """同一插件实例重复应用配置只清理保留记录，不重复执行重启恢复。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin.scan_invocations = 0
    plugin._history_clock = lambda: 1_704_326_400.0
    persisted: dict[str, object] = {
        plugin.SECRET_DATA_KEY: {},
        plugin.HISTORY_DATA_KEY: {
            "version": 1,
            "items": [_status("previous", "2024-01-01T00:00:00+00:00", state="running", generated=4)],
        },
    }
    plugin.get_config = lambda _plugin_id=None: None
    plugin.get_data = lambda key=None, plugin_id=None: persisted.get(key)
    plugin.save_data = lambda key, value, plugin_id=None: persisted.__setitem__(key, value)

    plugin.init_plugin({"instances": [], "schedule_enabled": False})
    plugin._get_history_store().record_status(
        _status("active", "2024-01-02T00:00:00+00:00", state="running", generated=9)
    )
    plugin.init_plugin({"instances": [], "schedule_enabled": False})

    page = plugin.get_history_page()
    active = next(item for item in page["items"] if item["id"] == "active")  # type: ignore[index]
    assert active["state"] == "running"
    assert plugin.scan_invocations == 0

    fresh = object.__new__(Alist2Strm)
    fresh._plugin_instance_id = "Alist2Strm"
    fresh._history_clock = lambda: 1_704_326_400.0
    fresh.get_config = lambda _plugin_id=None: None
    fresh.get_data = lambda key=None, plugin_id=None: persisted.get(key)
    fresh.save_data = lambda key, value, plugin_id=None: persisted.__setitem__(key, value)
    fresh.init_plugin({"instances": [], "schedule_enabled": False})
    recovered = fresh.get_history_page()
    fresh_active = next(item for item in recovered["items"] if item["id"] == "active")  # type: ignore[index]
    assert fresh_active["state"] == "interrupted"


def _plugin_with_store(store: HistoryStore) -> Alist2Strm:
    """构造注入历史存储的插件 seam 对象。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._configuration = _configuration()
    plugin._configuration_valid = True
    plugin._secret_tokens = {}
    plugin._history_store = store
    return plugin


def test_history_api_has_superuser_auth_and_safe_projection() -> None:
    """历史 API 要求 Bearer 与显式超级管理员依赖，响应不含 Token。"""

    store, _ = _store()
    store.record_status(_status("run", "2024-01-01T00:00:00+00:00", error="credential-value"))
    plugin = _plugin_with_store(store)

    route = next(item for item in plugin.get_api() if item["path"] == "/history")
    endpoint = route["endpoint"]
    dependency = inspect.signature(endpoint).parameters["_"].default.dependency
    response = plugin.read_history(page=1, page_size=20, _=object())

    assert route["auth"] == "bear"
    assert dependency is get_current_active_superuser_async
    assert response["total"] == 1
    assert "credential-value" not in str(response)
    assert "trigger" not in str(response)


def test_invalid_retention_keeps_old_configuration_and_does_not_persist() -> None:
    """正整数校验失败时保留旧状态且不进入配置持久化。"""

    plugin = _plugin_with_store(_store()[0])
    writes: list[object] = []
    plugin.update_config = lambda *_args, **_kwargs: writes.append("config") or True
    plugin.save_data = lambda *_args, **_kwargs: writes.append("data")
    payload = {
        "instances": [
            {
                "id": "instance-1",
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "mappings": [{"source_dir": "/library", "output_dir": "/strm"}],
            }
        ],
        "history_retention_days": 0,
    }

    with pytest.raises(ConfigurationValidationError):
        plugin.save_configuration(payload)

    assert plugin._configuration.history_retention_days == 90
    assert writes == []
