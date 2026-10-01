"""插件组合根与运行态生命周期测试。"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.plugins.subtitleassistant import SubtitleAssistant
from app.plugins.subtitleassistant.config import load_config
from app.plugins.subtitleassistant.plugin import PluginRuntime, RuntimeInitializationError, build_runtime
from app.plugins.subtitleassistant.schemas.config import PluginConfig
from app.plugins.subtitleassistant.schemas.source import SourceHealth, SubtitleSource

pytestmark = pytest.mark.anyio


def _runtime() -> PluginRuntime:
    """创建只包含生命周期依赖的未装配运行态。"""

    runtime = object.__new__(PluginRuntime)
    runtime._stopped = False
    runtime._enabled = True
    runtime.coordinator = SimpleNamespace(shutdown=AsyncMock())
    runtime.manual_search = SimpleNamespace(clear_sessions=AsyncMock())
    runtime.source_service = SimpleNamespace(close=AsyncMock())
    runtime.archive = SimpleNamespace(cancel=AsyncMock())
    runtime.api_controller = object()
    runtime.record_catalog = object()
    runtime.record_maintenance = object()
    runtime.record_committer = object()
    runtime.targets = object()
    runtime.filesystem = SimpleNamespace(clear_data_directory=AsyncMock())
    runtime.store = SimpleNamespace(reset=AsyncMock())
    return runtime


async def test_runtime_stop_releases_resources_once_in_reverse_dependency_order() -> None:
    """停止按任务、搜索、来源、归档顺序执行且释放运行引用。"""

    calls: list[str] = []
    runtime = _runtime()
    runtime.coordinator.shutdown.side_effect = lambda: calls.append("task")
    runtime.manual_search.clear_sessions.side_effect = lambda: calls.append("search")
    runtime.source_service.close.side_effect = lambda: calls.append("source")
    runtime.archive.cancel.side_effect = lambda: calls.append("archive")

    await runtime.stop()
    await runtime.stop()

    assert calls == ["task", "search", "source", "archive"]
    assert runtime.coordinator is None
    assert runtime.manual_search is None
    assert runtime.source_service is None
    assert runtime.archive is None


async def test_runtime_reset_stops_then_clears_data_and_remains_empty() -> None:
    """重置不遗留运行资源，并保持可重复的空状态。"""

    runtime = _runtime()
    filesystem = runtime.filesystem
    store = runtime.store

    await runtime.reset()
    await runtime.reset()

    filesystem.clear_data_directory.assert_awaited_once()
    store.reset.assert_awaited_once()
    assert runtime.filesystem is None
    assert runtime.store is None
    assert runtime.coordinator is None
    assert runtime.manual_search is None


async def test_runtime_cleanup_continues_after_a_resource_failure() -> None:
    """任一资源释放失败时仍继续清理其他资源和数据。"""

    runtime = _runtime()
    runtime.coordinator.shutdown.side_effect = RuntimeError("task failure")
    search = runtime.manual_search
    source_service = runtime.source_service
    archive = runtime.archive
    filesystem = runtime.filesystem
    store = runtime.store

    await runtime.reset()
    await runtime.reset()

    search.clear_sessions.assert_awaited_once()
    source_service.close.assert_awaited_once()
    archive.cancel.assert_awaited_once()
    filesystem.clear_data_directory.assert_awaited_once()
    store.reset.assert_awaited_once()
    assert runtime.coordinator is None
    assert runtime.manual_search is None


async def test_runtime_reset_continues_after_data_cleanup_failures() -> None:
    """目录或 PluginData 清理失败不阻断剩余重置与引用释放。"""

    runtime = _runtime()
    filesystem = runtime.filesystem
    store = runtime.store
    filesystem.clear_data_directory.side_effect = OSError("filesystem failure")
    store.reset.side_effect = RuntimeError("store failure")

    await runtime.reset()
    await runtime.reset()

    filesystem.clear_data_directory.assert_awaited_once()
    store.reset.assert_awaited_once()
    assert runtime.filesystem is None
    assert runtime.store is None


def test_reset_data_sync_degrades_when_host_loop_is_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """V3 主事件循环未登记时同步重置降级为当前线程执行，不再向上抛错。"""

    from app.plugins.subtitleassistant.plugin.runtime import global_vars

    def _require_loop() -> Any:
        """模拟 V3 应用生命周期尚未登记主循环。"""
        raise RuntimeError("主事件循环尚未启动或已经停止")

    monkeypatch.setattr(global_vars.__class__, "loop", property(lambda _self: _require_loop()))

    runtime = _runtime()
    store = runtime.store

    runtime.reset_data_sync()

    store.reset.assert_awaited_once()
    assert runtime.store is None


def test_build_runtime_normalizes_unexpected_initialization_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """组合根只向宿主暴露稳定的初始化异常。"""

    monkeypatch.setattr(PluginRuntime, "initialize", lambda _self, _config: (_ for _ in ()).throw(OSError("secret")))

    with pytest.raises(RuntimeInitializationError, match="插件运行态初始化失败"):
        build_runtime(SimpleNamespace(), {})


class _CredentialStore:
    """记录凭据读写与来源状态保存的内存替身。"""

    def __init__(self) -> None:
        """初始化三个来源的持久化凭据。"""

        self.credentials: dict[str, dict[str, str]] = {
            SubtitleSource.OPENSUBTITLES.value: {"api_key": "key", "username": "user", "password": "password"},
            SubtitleSource.ASSRT.value: {"token": "token"},
        }
        self.updated: list[tuple[SubtitleSource, dict[str, str]]] = []
        self.cleared: list[SubtitleSource] = []
        self.saved_statuses: list[Any] = []

    def get_credentials_sync(self, source: SubtitleSource) -> dict[str, str]:
        """返回来源持久化凭据快照。"""

        return dict(self.credentials.get(source.value, {}))

    async def update_credentials(self, source: SubtitleSource, values: dict[str, str]) -> bool:
        """合并非空凭据并返回配置完整状态。"""

        self.updated.append((source, dict(values)))
        self.credentials.setdefault(source.value, {}).update(values)
        return True

    async def clear_credentials(self, source: SubtitleSource) -> None:
        """删除来源全部凭据。"""

        self.cleared.append(source)
        self.credentials.pop(source.value, None)

    async def save_source_status(self, status: Any) -> None:
        """记录保存的来源状态。"""

        self.saved_statuses.append(status)


class _CredentialHost:
    """记录公开配置保存结果的宿主替身。"""

    def __init__(self, result: bool = True) -> None:
        """保存 update_config 返回值与调用记录。"""

        self.result = result
        self.config_calls: list[tuple[dict[str, object], str]] = []

    def update_config(self, config: dict[str, object], plugin_id: str) -> bool:
        """记录一次非敏感配置保存。"""

        self.config_calls.append((dict(config), plugin_id))
        return self.result


class _RebuildableSources:
    """记录来源层重建输入的管理 facade 替身。"""

    def __init__(self) -> None:
        """初始化重建调用记录。"""

        self.rebuilds: list[tuple[dict[SubtitleSource, bool], dict[SubtitleSource, dict[str, str]]]] = []

    def rebuild(
        self,
        *,
        enabled: dict[SubtitleSource, bool],
        credentials: dict[SubtitleSource, dict[str, str]],
    ) -> None:
        """记录一次来源层重建。"""

        self.rebuilds.append((dict(enabled), {key: dict(value) for key, value in credentials.items()}))


def _credential_runtime(host: _CredentialHost | None = None) -> PluginRuntime:
    """构造带凭据端点的运行态替身。"""

    runtime = object.__new__(PluginRuntime)
    runtime._plugin_id = "SubtitleAssistant"
    runtime.config = PluginConfig()
    runtime.store = _CredentialStore()
    runtime.source_service = _RebuildableSources()
    runtime._host = host or _CredentialHost()
    return runtime


async def test_update_credentials_persists_then_rebuilds_sources() -> None:
    """凭据更新先持久化，再以当前开关与凭据整体重建来源层。"""

    runtime = _credential_runtime()

    configured = await runtime.update_source_credentials(
        SubtitleSource.OPENSUBTITLES,
        {"api_key": "new-key"},
    )

    assert configured is True
    assert runtime.store.updated == [(SubtitleSource.OPENSUBTITLES, {"api_key": "new-key"})]
    enabled, credentials = runtime.source_service.rebuilds[-1]
    assert enabled == {
        SubtitleSource.MOVIEPILOT: True,
        SubtitleSource.OPENSUBTITLES: False,
        SubtitleSource.ASSRT: False,
    }
    assert credentials[SubtitleSource.OPENSUBTITLES]["api_key"] == "new-key"


async def test_update_credentials_rejects_foreign_fields_before_persisting() -> None:
    """来源专属字段错误在持久化之前抛出，供路由投影为 422。"""

    runtime = _credential_runtime()

    with pytest.raises(ValueError, match="凭据字段"):
        await runtime.update_source_credentials(SubtitleSource.OPENSUBTITLES, {"token": "not-here"})

    assert runtime.store.updated == []
    assert runtime.source_service.rebuilds == []


async def test_update_credentials_without_runtime_state_only_persists() -> None:
    """运行态不可用时仅持久化凭据，等待下次初始化生效。"""

    runtime = _credential_runtime()
    runtime.source_service = None

    configured = await runtime.update_source_credentials(SubtitleSource.ASSRT, {"token": "new-token"})

    assert configured is True
    assert runtime.store.updated == [(SubtitleSource.ASSRT, {"token": "new-token"})]


async def test_clear_credentials_persists_switch_rebuilds_and_marks_disabled() -> None:
    """清除凭据持久化停用开关、重建来源层并保存 DISABLED 状态。"""

    runtime = _credential_runtime()
    runtime.config.opensubtitles_enabled = True

    persisted = await runtime.clear_source_credentials(SubtitleSource.OPENSUBTITLES)

    assert persisted is True
    assert runtime.config.opensubtitles_enabled is False
    assert runtime.store.cleared == [SubtitleSource.OPENSUBTITLES]
    payload, plugin_id = runtime._host.config_calls[-1]
    assert payload["opensubtitles_enabled"] is False
    assert plugin_id == "SubtitleAssistant"
    enabled, credentials = runtime.source_service.rebuilds[-1]
    assert enabled[SubtitleSource.OPENSUBTITLES] is False
    assert credentials[SubtitleSource.OPENSUBTITLES] == {}
    status = runtime.store.saved_statuses[-1]
    assert status.source is SubtitleSource.OPENSUBTITLES
    assert status.enabled is False
    assert status.configured is False
    assert status.health is SourceHealth.DISABLED


async def test_clear_credentials_reports_switch_save_failure_after_marking_disabled() -> None:
    """来源开关持久化失败时仍完成清凭据、重建与 DISABLED 标记，并返回失败。"""

    runtime = _credential_runtime(host=_CredentialHost(result=False))

    persisted = await runtime.clear_source_credentials(SubtitleSource.ASSRT)

    assert persisted is False
    assert runtime.store.cleared == [SubtitleSource.ASSRT]
    assert runtime.source_service.rebuilds[-1][1][SubtitleSource.ASSRT] == {}
    assert runtime.store.saved_statuses[-1].health is SourceHealth.DISABLED


@pytest.mark.parametrize("change", ["mapping", "other", "same", "stopped", "invalid"])
def test_plugin_config_save_only_hot_updates_mapping_changes(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """宿主保存仅映射配置时保留运行态，其他变更或已停止的实例仍重建。"""

    from app.plugins import subtitleassistant as entry
    from app.plugins.subtitleassistant.plugin.runtime import settings

    runtime = object.__new__(PluginRuntime)
    runtime._stopped = change == "stopped"
    runtime.config = load_config({"enabled": True}, settings.RMT_SUBEXT)
    original_config = runtime.config
    plugin = object.__new__(SubtitleAssistant)
    plugin._runtime = runtime
    calls: list[str] = []
    replacement = object.__new__(PluginRuntime)

    def rebuild(_host: object, _config: object) -> PluginRuntime:
        """记录入口是否重新装配运行态。"""

        calls.append("rebuild")
        return replacement

    monkeypatch.setattr(runtime, "stop_sync", lambda: calls.append("stop"))
    monkeypatch.setattr(entry, "build_runtime", rebuild)
    values = runtime.config.saved_payload()
    if change != "same":
        values["path_mappings"] = [{"source_prefix": "/media", "target_prefix": "/subtitles"}]
    if change == "other":
        values["enabled"] = False
    if change == "invalid":
        values["path_mappings"] = [{"source_prefix": "relative", "target_prefix": "/subtitles"}]

    plugin.init_plugin(values)

    if change == "mapping":
        assert plugin._runtime is runtime
        assert runtime.config is original_config
        assert runtime.config.path_mappings[0].as_dict() == {"source_prefix": "/media", "target_prefix": "/subtitles"}
        assert calls == []
    else:
        assert plugin._runtime is replacement
        assert calls == ["stop", "rebuild"]
        assert original_config.path_mappings == ()
