"""共用定时计划的宿主调度契约测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import (  # ty: ignore[unresolved-import]
    ConfigurationValidationError,
    build_configuration,
)
from app.plugins.alist2strm.scan import RemoteEntry  # ty: ignore[unresolved-import]


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


class _Reader:
    """提供一个不访问网络的 OpenList 目录替身。"""

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """返回单个视频或空目录。"""

        if path == "/library":
            return [RemoteEntry("Movie.mkv", False)]
        return []


def _payload(*, enabled: bool, cron: str = "0 3 * * *", output_dir: str = "/strm") -> dict[str, object]:
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
    return plugin


def test_enabled_configuration_registers_one_host_cron_service() -> None:
    """启用且有效的共用计划只注册一个宿主 Cron 服务。"""

    plugin = _plugin(_payload(enabled=True, cron="*/15 * * * *"))

    services = plugin.get_service()

    assert len(services) == 1
    service = services[0]
    assert service["id"] == "shared-scan"
    assert isinstance(service["trigger"], CronTrigger)
    assert service["func"].__self__ is plugin
    assert service["kwargs"] == {"coalesce": True, "max_instances": 1, "misfire_grace_time": None}


def test_disabled_or_invalid_configuration_registers_no_service() -> None:
    """关闭计划或插件配置无效时不得向宿主注册服务。"""

    assert _plugin(_payload(enabled=False)).get_service() == []

    plugin = _plugin(_payload(enabled=True))
    plugin._configuration = None
    plugin._configuration_valid = False
    assert plugin.get_service() == []


def test_invalid_cron_is_rejected_before_persistence_or_schedule_refresh() -> None:
    """无效 Cron 在保存前失败，既不写入也不刷新宿主计划。"""

    plugin = _plugin()
    refreshes: list[str] = []
    plugin._schedule_update = lambda plugin_id: refreshes.append(plugin_id)
    plugin.update_config = lambda *args, **kwargs: pytest.fail("Cron 校验失败不应写入宿主")

    with pytest.raises(ConfigurationValidationError):
        plugin.save_configuration(_payload(enabled=True, cron="not a cron"))

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
        """记录凭据分区写入。"""

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
