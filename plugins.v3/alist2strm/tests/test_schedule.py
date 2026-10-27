"""共用定时计划的宿主调度契约测试。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import app.scheduler.oncejob as oncejob_module
import app.scheduler.reconcile as reconcile_module
import app.sdk.scheduler as scheduler_sdk
from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import (  # ty: ignore[unresolved-import]
    ConfigurationValidationError,
    build_configuration,
)
from app.plugins.alist2strm.history import HistoryStore  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import RemoteEntry  # ty: ignore[unresolved-import]
from app.scheduler.oncejob import SchedulerPluginOnceJobOwner
from app.scheduler.reconcile import SchedulerReconcileOwner


class _Queue:
    """捕获注入的后台任务，测试显式决定何时执行。"""

    def __init__(self) -> None:
        """初始化任务列表。"""

        self.jobs: list[Any] = []

    def __call__(self, task: Any) -> bool:
        """接收一次扫描任务提交。"""

        self.jobs.append(task)
        return True

    def run_one(self) -> None:
        """执行当前唯一的后台任务。"""

        assert len(self.jobs) == 1
        self.jobs.pop()()


class _HostOwner(SchedulerReconcileOwner, SchedulerPluginOnceJobOwner):
    """按宿主调度器的对账与一次性任务 owner 组合测试。"""

    plugin: Alist2Strm
    manager: Any


@pytest.fixture
def _host_scheduler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Alist2Strm, _HostOwner]]:
    """构造真实宿主一次性任务保留逻辑使用的离线调度器。"""

    plugin = _plugin(_payload(enabled=True, output_dir=str(tmp_path / "old")))

    def get_plugin_services(*, pid: str) -> list[dict[str, Any]]:
        """按宿主的关键字参数契约返回当前插件服务声明。"""

        assert pid == plugin._plugin_id()
        return plugin.get_service()

    manager = SimpleNamespace(
        running_plugins={plugin._plugin_id(): plugin},
        get_plugin_attr=lambda _pid, _attr: "Alist2Strm",
        get_plugin_services=get_plugin_services,
    )
    monkeypatch.setattr(reconcile_module, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(oncejob_module, "get_plugin_manager", lambda: manager)

    scheduler = BackgroundScheduler(timezone="UTC")
    owner = _HostOwner.__new__(_HostOwner)
    owner._scheduler = scheduler
    owner._lock = threading.RLock()
    owner._jobs = {}
    owner._assign_job_generation = lambda _job_id, job: job.setdefault("_generation", 0)
    owner._accepting_submissions = lambda: True
    owner._is_job_active = lambda _job_id: False

    def start(job_id: str) -> bool:
        """执行测试中明确选定的一次性任务回调。"""

        job = owner._jobs[job_id]
        job["func"](**job["kwargs"])
        return True

    owner.start = start
    owner.plugin = plugin
    owner.manager = manager

    def add_once(
        pid: str,
        job_id: str,
        func: Any,
        name: str,
        delay_seconds: float = 0,
        func_kwargs: dict[str, Any] | None = None,
    ) -> bool:
        """将插件提交的任务延迟到测试显式执行，保留宿主登记状态。"""

        del delay_seconds
        return owner.add_plugin_once_job(pid, job_id, func, name, delay_seconds=60, func_kwargs=func_kwargs)

    monkeypatch.setattr(scheduler_sdk, "add_plugin_once_job", add_once)
    plugin._schedule_update = owner.update_plugin_job
    scheduler.start()
    try:
        yield plugin, owner
    finally:
        scheduler.shutdown(wait=False)


class _Reader:
    """提供一个不访问网络的 OpenList 目录替身。"""

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """返回单个视频或空目录。"""

        if path == "/library":
            return [RemoteEntry("Movie.mkv", False)]
        return []


def _payload(
    *, enabled: bool, cron: str = "0 3 * * *", output_dir: str = "/strm", retention_days: int = 90
) -> dict[str, object]:
    """构造共用计划测试使用的已保存配置载荷。"""

    return {
        "instances": [
            {
                "id": "instance-1",
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "api_token": "configured",
                "mappings": [{"source_dir": "/library", "output_dir": output_dir}],
            }
        ],
        "schedule_enabled": enabled,
        "schedule_cron": cron,
        "history_retention_days": retention_days,
    }


def _plugin(payload: dict[str, object] | None = None) -> Alist2Strm:
    """创建不触发宿主数据库的插件运行态替身。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._configuration = build_configuration(payload or _payload(enabled=False))
    plugin._secret_tokens = {"instance-1": "configured"}
    plugin._configuration_valid = True
    plugin._stopped = False
    plugin.scan_invocations = 0
    plugin.get_data = lambda key=None, plugin_id=None: None
    plugin.save_data = lambda key, value, plugin_id=None: None
    return plugin


def test_enabled_configuration_registers_shared_and_history_cron_services() -> None:
    """启用且有效的共用计划注册扫描服务，并始终注册历史清理服务。"""

    plugin = _plugin(_payload(enabled=True, cron="*/15 * * * *"))

    services = plugin.get_service()

    assert len(services) == 2
    service = services[0]
    assert service["id"] == "shared-scan"
    assert isinstance(service["trigger"], CronTrigger)
    assert service["func"].__self__ is plugin
    assert service["kwargs"]["coalesce"] is True
    assert service["kwargs"]["max_instances"] == 1
    assert service["kwargs"]["misfire_grace_time"] is None
    assert service["kwargs"]["next_run_time"] is not None
    assert str(service["trigger"]) == str(CronTrigger.from_crontab("* * * * *"))
    retention = services[1]
    assert retention["id"] == "history-retention"
    assert isinstance(retention["trigger"], CronTrigger)
    assert str(retention["trigger"]) == str(CronTrigger.from_crontab("0 4 * * *"))
    assert retention["func"].__self__ is plugin


def test_disabled_configuration_keeps_daily_history_retention_service() -> None:
    """关闭扫描计划仍声明每日历史保留清理，执行时不触发扫描。"""

    plugin = _plugin(_payload(enabled=False, retention_days=1))
    now = 1_704_153_600.0
    state: dict[str, object] = {
        "value": {
            "version": 1,
            "items": [
                {
                    "id": "expired",
                    "started_at": "2023-12-30T00:00:00+00:00",
                    "state": "success",
                    "counters": {},
                    "mappings": [],
                }
            ],
        }
    }
    plugin._history_store = HistoryStore(
        lambda: state["value"],
        lambda value: state.__setitem__("value", value),
        clock=lambda: now,
    )

    services = plugin.get_service()
    assert len(services) == 1
    service = services[0]
    assert service["id"] == "history-retention"
    service["func"]()
    assert state["value"] == {"version": 1, "items": []}

    assert plugin.get_history_page()["total"] == 0
    assert plugin.scan_invocations == 0


def test_disabled_or_invalid_configuration_registers_history_only_or_no_service() -> None:
    """关闭扫描计划仍保留历史服务；无效配置不声明任何服务。"""

    disabled_services = _plugin(_payload(enabled=False)).get_service()
    assert len(disabled_services) == 1
    assert disabled_services[0]["id"] == "history-retention"

    plugin = _plugin(_payload(enabled=True))
    plugin._configuration = None
    plugin._configuration_valid = False
    assert plugin.get_service() == []


def test_invalid_active_interval_is_rejected_before_persistence_or_schedule_refresh() -> None:
    """无效 活跃间隔 在保存前失败，既不写入也不刷新宿主计划。"""

    plugin = _plugin()
    refreshes: list[str] = []
    plugin._schedule_update = lambda plugin_id: refreshes.append(plugin_id)
    plugin.update_config = lambda *args, **kwargs: pytest.fail("活跃间隔 校验失败不应写入宿主")

    with pytest.raises(ConfigurationValidationError):
        plugin.save_configuration({**_payload(enabled=True), "active_recheck_days": 0})

    assert refreshes == []
    assert plugin._configuration.schedule_enabled is False


def test_save_refreshes_host_schedule_after_both_persistence_writes() -> None:
    """保存公开配置和 Token 后才显式刷新宿主计划注册。"""

    plugin = _plugin()
    events: list[tuple[str, object]] = []

    def update_config(config: dict[str, object], plugin_id: str | None = None) -> bool:
        """记录公开配置写入。"""

        events.append(("config", config["schedule_enabled"]))
        return True

    def save_data(key: str, value: object, plugin_id: str | None = None) -> None:
        """记录身份保护与凭据分区的先后写入。"""

        events.append(("secret", key))

    def update_schedule(plugin_id: str) -> None:
        """观察刷新发生时已发布的新配置。"""

        events.append(("refresh", (plugin_id, plugin._configuration.schedule_enabled)))

    plugin.update_config = update_config
    plugin.save_data = save_data
    plugin._schedule_update = update_schedule

    result = plugin.save_configuration(_payload(enabled=True, cron="*/20 * * * *"))

    assert result["success"] is True
    assert events == [
        ("secret", plugin.OBSERVATIONS_DATA_KEY),
        ("config", True),
        ("secret", plugin.SECRET_DATA_KEY),
        ("refresh", ("Alist2Strm", True)),
    ]


def test_service_registration_and_save_do_not_scan() -> None:
    """启动配置恢复、读取服务和保存计划都不得隐式提交扫描。"""

    plugin = _plugin(_payload(enabled=True))
    queue = _Queue()
    plugin._scan_dispatch = queue
    plugin._schedule_update = lambda _plugin_id: None
    plugin.update_config = lambda _config, plugin_id=None: True
    plugin.save_data = lambda _key, _value, plugin_id=None: None

    assert plugin.get_service()
    plugin.save_configuration(_payload(enabled=True, cron="0 4 * * *"))

    assert plugin.scan_invocations == 0
    assert queue.jobs == []


def test_save_refresh_preserves_host_pending_scan_snapshot(
    _host_scheduler: tuple[Alist2Strm, _HostOwner], tmp_path: Path
) -> None:
    """宿主刷新计划时保留插件绑定的一次性任务及其原始配置快照。"""

    plugin, owner = _host_scheduler
    old_output = tmp_path / "old"
    new_output = tmp_path / "new"
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)
    plugin._scan_whitelist_suffix_provider = lambda: (".mkv",)
    plugin.update_config = lambda _config, plugin_id=None: True
    plugin.save_data = lambda _key, _value, plugin_id=None: None

    assert plugin.trigger_scan()["accepted"] is True
    once_job_id = "Alist2Strm_once_manual-scan"
    pending = owner._jobs[once_job_id]
    assert pending["_plugin_instance"] is plugin
    assert owner._scheduler.get_job(once_job_id) is not None

    plugin.save_configuration(_payload(enabled=True, cron="*/20 * * * *", output_dir=str(new_output)))

    periodic_job_id = "Alist2Strm_shared-scan"
    periodic_job = owner._scheduler.get_job(periodic_job_id)
    assert periodic_job is not None
    assert periodic_job_id in owner._jobs
    assert str(periodic_job.trigger) == str(CronTrigger.from_crontab("* * * * *"))
    assert owner._scheduler.get_job(once_job_id) is not None
    retained = owner._jobs[once_job_id]
    assert retained["_plugin_instance"] is plugin
    retained["func"](**retained["kwargs"])

    assert (old_output / "Movie.strm").is_file()
    assert not new_output.exists()
    assert plugin.get_scan_status()["state"] == "success"


def test_stop_service_removes_periodic_jobs_and_rejects_retained_schedule_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止先封闭扫描入口，再移除周期任务并拒绝已留存的定时回调。"""

    plugin = _plugin(_payload(enabled=True))
    retained_callback = plugin.get_service()[0]["func"]
    removals: list[tuple[str, str, bool]] = []

    def remove_periodic(plugin_id: str) -> None:
        """观察公开 SDK 的周期任务移除及停止状态顺序。"""

        removals.append(("periodic", plugin_id, plugin._stopped))

    def remove_once(plugin_id: str, job_id: str) -> None:
        """观察待执行一次性任务清理。"""

        removals.append((job_id, plugin_id, plugin._stopped))

    monkeypatch.setattr(scheduler_sdk, "remove_plugin_job", remove_periodic)
    monkeypatch.setattr(scheduler_sdk, "remove_plugin_once_job", remove_once)

    plugin.stop_service()

    assert removals == [
        ("periodic", "Alist2Strm", True),
        ("manual-scan", "Alist2Strm", True),
    ]
    assert retained_callback() == {"accepted": False, "message": "自动扫描未启用"}
    assert plugin.trigger_scan()["accepted"] is False
    assert plugin.scan_invocations == 0


def test_scheduled_callback_uses_manual_guard_and_start_snapshot(tmp_path: Path) -> None:
    """定时回调复用手动协调器，运行中触发跳过且任务保持启动快照。"""

    output = tmp_path / "old"
    plugin = _plugin(_payload(enabled=True, output_dir=str(output)))
    queue = _Queue()
    plugin._scan_dispatch = queue
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)

    service = plugin.get_service()[0]
    assert plugin.trigger_scan()["accepted"] is True
    duplicate = service["func"]()
    assert duplicate == {"accepted": False, "message": "扫描正在运行，已跳过本次触发"}
    assert len(queue.jobs) == 1

    plugin._configuration = build_configuration(_payload(enabled=True, output_dir=str(tmp_path / "new")))
    queue.run_one()

    assert (output / "Movie.strm").is_file()
    assert not (tmp_path / "new").exists()
    assert plugin.get_scan_status()["state"] == "success"


def test_stopping_plugin_exposes_no_periodic_service_and_releases_scan() -> None:
    """停止插件后不再暴露周期服务，并沿用已有扫描资源释放入口。"""

    plugin = _plugin(_payload(enabled=True))
    plugin._scan_dispatch = _Queue()
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)

    assert plugin.get_service()
    plugin.stop_service()

    assert plugin.get_service() == []
    assert plugin.get_scan_status()["state"] == "idle"


def test_manual_and_scheduled_scans_share_single_layer_pruning_deadline(tmp_path: Path) -> None:
    """手动和定时入口共用持久化期限，跳过不清理，到期由实际回调复查。"""

    plugin = _plugin(_payload(enabled=True, output_dir=str(tmp_path)))
    queue = _Queue()
    now = [1_720_000_000.0]
    calls: list[str] = []
    data: dict[str, object] = {}

    class Reader:
        """返回四十天前文件，不访问网络。"""

        def list_dir(self, path: str) -> list[RemoteEntry]:
            """记录是否真的发起列目录操作。"""

            calls.append(path)
            return [RemoteEntry("Film.mkv", False, 1_720_000_000.0 - 40 * 86400)]

    plugin._scan_dispatch = queue
    plugin._scan_client_factory = lambda _url, _token: Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)
    plugin._scan_whitelist_suffix_provider = lambda: (".mkv",)
    plugin._scan_clock = lambda: now[0]
    plugin.get_data = lambda key, plugin_id=None: data.get(key)
    plugin.save_data = lambda key, value, plugin_id=None: data.__setitem__(key, value)

    assert plugin.trigger_scan()["accepted"] is True
    queue.run_one()
    assert calls == ["/library"]
    orphan = tmp_path / "removed.strm"
    orphan.write_text("protected until checked", encoding="utf-8")
    service = next(item for item in plugin.get_service() if item["id"] == "shared-scan")
    now[0] += 7 * 86400 - 1
    assert service["func"]()["accepted"] is False
    assert not queue.jobs
    assert calls == ["/library"]
    assert orphan.exists()
    now[0] += 1
    service["func"]()
    queue.run_one()
    assert calls == ["/library", "/library"]
    assert not orphan.exists()
    assert plugin.scan_invocations == 2
