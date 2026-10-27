"""插件保存与重载 seam 测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.plugins.alist2strm import Alist2Strm  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.config import build_configuration, public_configuration  # ty: ignore[unresolved-import]
from app.plugins.alist2strm.scan import RemoteEntry  # ty: ignore[unresolved-import]


def _payload(*, instance_id: str = "instance-1", token: str = "configured") -> dict[str, object]:
    """构造保存接口使用的非真实配置。"""

    return {
        "instances": [
            {
                "id": instance_id,
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "api_token": token,
                "mappings": [{"source_dir": "/library", "output_dir": "/strm"}],
            }
        ],
        "schedule_enabled": False,
        "schedule_cron": "0 3 * * *",
        "history_retention_days": 90,
    }


def _plugin() -> Alist2Strm:
    """创建不触发宿主数据库的插件对象。"""

    plugin = object.__new__(Alist2Strm)
    plugin._plugin_instance_id = "Alist2Strm"
    plugin._configuration = build_configuration(_payload())
    plugin._secret_tokens = {"instance-1": "configured"}
    plugin._configuration_valid = True
    plugin.get_data = lambda key=None, plugin_id=None: None
    plugin.save_data = lambda key, value, plugin_id=None: None
    return plugin


def test_save_configuration_persists_public_model_and_secret_partition_separately() -> None:
    """成功保存应只把公开字段交给宿主配置，并返回可重新编辑的安全投影。"""

    plugin = _plugin()
    writes: list[tuple[str, Any]] = []
    plugin.update_config = lambda config, plugin_id=None: writes.append(("config", config)) or True
    plugin.save_data = lambda key, value, plugin_id=None: writes.append((key, value))

    result = plugin.save_configuration(_payload(token="new-configured"))

    assert result["success"] is True
    public_payload = next(value for key, value in writes if key == "config")
    secret_payload = next(value for key, value in writes if key == plugin.SECRET_DATA_KEY)
    assert "api_token" not in public_payload["instances"][0]
    assert public_payload["instances"][0].get("api_token_configured", True)
    assert secret_payload == {"instance-1": "new-configured"}
    assert result["data"]["instances"][0]["api_token_configured"] is True
    assert "api_token" not in result["data"]["instances"][0]


def test_save_configuration_preserves_existing_token_when_public_form_omits_it() -> None:
    """重新打开配置后不填写隐藏字段也应保留原管理员 Token。"""

    plugin = _plugin()
    writes: list[tuple[str, Any]] = []
    plugin.update_config = lambda config, plugin_id=None: writes.append(("config", config)) or True
    plugin.save_data = lambda key, value, plugin_id=None: writes.append((key, value))
    public_payload = public_configuration(plugin._configuration)

    plugin.save_configuration(public_payload)

    assert next(value for key, value in writes if key == plugin.SECRET_DATA_KEY) == {"instance-1": "configured"}


def test_failed_persistence_does_not_publish_candidate_or_partially_mutate_configuration() -> None:
    """宿主配置写入失败时旧配置与密钥快照必须保持不变。"""

    plugin = _plugin()
    old_public = public_configuration(plugin._configuration)
    plugin.update_config = lambda config, plugin_id=None: False
    save_calls: list[Any] = []
    plugin.save_data = lambda key, value, plugin_id=None: save_calls.append((key, value))

    with pytest.raises(plugin.PersistenceError):
        plugin.save_configuration(_payload(token="candidate-not-published"))

    assert public_configuration(plugin._configuration) == old_public
    assert plugin._secret_tokens == {"instance-1": "configured"}
    assert [key for key, _value in save_calls] == [plugin.OBSERVATIONS_DATA_KEY]


def test_secret_partition_failure_rolls_back_public_candidate() -> None:
    """Token 分区写入失败时公开配置也必须恢复旧快照。"""

    plugin = _plugin()
    old_public = plugin._storage_payload(plugin._configuration)
    persisted: dict[str, object] = {"config": old_public}
    save_count = 0

    def update_config(config: dict[str, object], plugin_id: str | None = None) -> bool:
        """记录公开配置写入和回滚。"""

        persisted["config"] = config
        return True

    def save_data(key: str, value: object, plugin_id: str | None = None) -> None:
        """Token 首次写入失败，第二次接收回滚快照；目录身份保护写入不受影响。"""

        nonlocal save_count
        if key != plugin.SECRET_DATA_KEY:
            return
        save_count += 1
        if save_count == 1:
            raise OSError("synthetic persistence failure")

    plugin.update_config = update_config
    plugin.save_data = save_data

    with pytest.raises(plugin.PersistenceError):
        plugin.save_configuration(_payload(token="candidate-not-published"))

    assert persisted["config"] == old_public
    assert plugin._secret_tokens == {"instance-1": "configured"}


def test_reload_reads_saved_public_config_and_secret_partition_without_running_scan() -> None:
    """重载只恢复配置投影，不自动启动扫描。"""

    plugin = _plugin()
    plugin.get_data = lambda key=None, plugin_id=None: {"instance-1": "configured"}
    plugin.init_plugin(_payload(token=""))

    assert plugin.get_state() is True
    assert plugin.public_configuration()["instances"][0]["api_token_configured"] is True
    assert plugin.scan_invocations == 0


def test_manual_trigger_uses_saved_snapshot_and_injected_high_level_io(tmp_path) -> None:
    """插件级手动入口捕获配置快照，后台替身执行时不读取后续保存。"""

    plugin = _plugin()
    old_output = tmp_path / "old"
    new_output = tmp_path / "new"
    plugin._configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "instance-1",
                    "name": "实例",
                    "scan_url": "https://openlist.example.invalid",
                    "api_token": "configured",
                    "mappings": [{"source_dir": "/library", "output_dir": str(old_output)}],
                }
            ]
        }
    )
    jobs: list[Any] = []
    plugin._scan_dispatch = lambda task: jobs.append(task) or True
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)

    assert plugin.trigger_scan() == {"accepted": True, "message": "扫描已在后台启动"}
    assert plugin.get_scan_status()["running"] is True
    plugin._configuration = build_configuration(
        {
            "instances": [
                {
                    "id": "instance-1",
                    "name": "实例替换",
                    "scan_url": "https://openlist.example.invalid",
                    "api_token": "replacement",
                    "mappings": [{"source_dir": "/library", "output_dir": str(new_output)}],
                }
            ]
        }
    )
    assert len(jobs) == 1
    jobs.pop()()

    assert (old_output / "Movie.strm").is_file()
    assert not new_output.exists()
    assert plugin.get_scan_status()["state"] == "success"


def test_save_during_queued_scan_keeps_snapshot_until_next_run(tmp_path: Path) -> None:
    """通过插件保存新地址后，已排队任务仍使用启动时快照。"""

    plugin = _plugin()
    output = tmp_path / "strm"
    old_payload = {
        "instances": [
            {
                "id": "instance-1",
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "playback_url": "https://play-old.example.invalid",
                "api_token": "configured",
                "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
            }
        ]
    }
    new_payload = {
        "instances": [
            {
                "id": "instance-1",
                "name": "主实例",
                "scan_url": "https://openlist.example.invalid",
                "playback_url": "https://play-new.example.invalid",
                "api_token": "configured",
                "mappings": [{"source_dir": "/library", "output_dir": str(output)}],
            }
        ]
    }
    plugin._configuration = build_configuration(old_payload)
    jobs: list[Any] = []
    plugin._scan_dispatch = lambda task: jobs.append(task) or True
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)
    plugin.update_config = lambda _config, plugin_id=None: True
    plugin.save_data = lambda _key, _value, plugin_id=None: None

    assert plugin.trigger_scan() == {"accepted": True, "message": "扫描已在后台启动"}
    jobs.pop()()
    target = output / "Movie.strm"
    assert target.read_text(encoding="utf-8") == "https://play-old.example.invalid/d/library/Movie.mkv"

    assert plugin.trigger_scan() == {"accepted": True, "message": "扫描已在后台启动"}
    plugin.save_configuration(new_payload)
    jobs.pop()()
    assert target.read_text(encoding="utf-8") == "https://play-old.example.invalid/d/library/Movie.mkv"
    assert plugin.get_scan_status()["counters"]["skipped"] == 0  # type: ignore[index]
    assert plugin.get_scan_status()["counters"]["updated"] == 0  # type: ignore[index]

    assert plugin.trigger_scan() == {"accepted": True, "message": "扫描已在后台启动"}
    jobs.pop()()
    assert target.read_text(encoding="utf-8") == "https://play-new.example.invalid/d/library/Movie.mkv"
    assert plugin.get_scan_status()["counters"]["updated"] == 1  # type: ignore[index]


class _Reader:
    """插件扫描 seam 使用的 OpenList 目录替身。"""

    def list_dir(self, path: str) -> list[RemoteEntry]:
        """返回一个视频或空目录。"""

        if path == "/library":
            return [RemoteEntry("Movie.mkv", False)]
        return []


def test_stop_service_cancels_pending_job_and_stops_coordinator(monkeypatch: pytest.MonkeyPatch) -> None:
    """卸载时移除宿主一次性任务并使排队扫描不可执行。"""

    plugin = _plugin()
    jobs: list[Any] = []
    removed: list[tuple[str, str]] = []
    plugin._scan_dispatch = lambda task: jobs.append(task) or True
    plugin._scan_client_factory = lambda _url, _token: _Reader()
    plugin._scan_media_suffix_provider = lambda: (".mkv",)
    monkeypatch.setattr(
        "app.sdk.scheduler.remove_plugin_once_job",
        lambda plugin_id, job_id: removed.append((plugin_id, job_id)),
    )

    assert plugin.trigger_scan() == {"accepted": True, "message": "扫描已在后台启动"}
    plugin.stop_service()

    assert removed == [("Alist2Strm", "manual-scan")]
    assert plugin.get_scan_status()["state"] == "stopped"
    jobs.pop()()
    assert plugin.get_scan_status()["state"] == "stopped"
